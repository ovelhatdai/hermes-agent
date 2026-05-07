"""
WhatsApp platform adapter.

WhatsApp integration is more complex than Telegram/Discord because:
- No official bot API for personal accounts
- Business API requires Meta Business verification
- Most solutions use web-based automation

This adapter supports multiple backends:
1. WhatsApp Business API (requires Meta verification)
2. whatsapp-web.js (via Node.js subprocess) - for personal accounts
3. Baileys (via Node.js subprocess) - alternative for personal accounts

For simplicity, we'll implement a generic interface that can work
with different backends via a bridge pattern.
"""

import asyncio
import importlib.util
import json
import logging
import os
import platform
import re
import signal
import subprocess
import sys

_IS_WINDOWS = platform.system() == "Windows"
from pathlib import Path
from typing import Dict, Optional, Any

from hermes_constants import get_hermes_dir

logger = logging.getLogger(__name__)

_CONTENT_CAPTURE_TRIGGERS = (
    "#pilula",
    "pilula:",
    "ideia de conteudo:",
    "guarda essa ideia:",
    "anota ai",
    "guarda essa",
)
_CONTENT_CAPTURE_ALLOWED_SENDERS = (
    "143658066157619@lid",
    "5551991987972",
    "5551991987972@s.whatsapp.net",
    "+5551991987972",
)
_CONTENT_CAPTURE_WORKER = os.getenv(
    "CONTENT_CAPTURE_WORKER",
    "/opt/hermes-content/content_capture.py",
)
_CONTENT_LINK_ANALYZER_TRIGGERS = (
    "#pilula",
    "copia esse jeito",
    "monta um igual",
    "gera um pra mim no estilo",
)
_CONTENT_LINK_ANALYZER_FOLLOWUPS = (
    "gera",
    "gerar",
    "gera ai",
    "gera pra mim",
    "so guarda",
    "só guarda",
    "guarda so",
    "guarda só",
    "guarda",
)
_CONTENT_LINK_ANALYZER_WORKER = os.getenv(
    "CONTENT_LINK_ANALYZER_WORKER",
    "/opt/hermes-content/content_link_analyzer.py",
)
_CONTENT_MEDIA_ROOT = Path(os.getenv("CONTENT_MEDIA_ROOT", "/opt/hermes-content"))
_CONTENT_MEDIA_AVATAR_REGISTRY = Path(
    os.getenv(
        "CONTENT_MEDIA_AVATAR_REGISTRY",
        str(_CONTENT_MEDIA_ROOT / "avatar_registry.py"),
    )
)
_CONTENT_VIDEO_WORKER = os.getenv(
    "CONTENT_VIDEO_WORKER",
    str(_CONTENT_MEDIA_ROOT / "content_video_pipeline.py"),
)
_CONTENT_IMAGE_WORKER = os.getenv(
    "CONTENT_IMAGE_WORKER",
    str(_CONTENT_MEDIA_ROOT / "content_image_pipeline.py"),
)
_CONTENT_MEDIA_APPROVAL_WORKER = os.getenv(
    "CONTENT_MEDIA_APPROVAL_WORKER",
    str(_CONTENT_MEDIA_ROOT / "content_media_approval.py"),
)
_CONTENT_MEDIA_VIDEO_COMMANDS = ("#video", "#video-audio")
_CONTENT_MEDIA_IMAGE_COMMANDS = ("#imagem", "#thumb", "#cinema")
_CONTENT_MEDIA_LIST_COMMAND = "#avatares"
_CONTENT_MEDIA_DECISION_PREFIXES = (
    "sim",
    "aprovado",
    "bom",
    "ok",
    "ta bom",
    "tá bom",
    "nao",
    "não",
    "arquiva",
    "descarta",
    "lixo",
    "outra",
    "outra versao",
    "outra versão",
    "mais uma",
    "regera",
    "regenera",
    "muda",
    "ajusta",
)
_CONTENT_MEDIA_ALIAS_HINTS = {
    "carro",
    "daiane",
    "formal",
    "terno",
    "qualquer",
    "escritorio",
    "vinicius1",
    "vinicius2",
}


def _kill_port_process(port: int) -> None:
    """Kill any process listening on the given TCP port."""
    try:
        if _IS_WINDOWS:
            # Use netstat to find the PID bound to this port, then taskkill
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[3] == "LISTENING":
                    local_addr = parts[1]
                    if local_addr.endswith(f":{port}"):
                        try:
                            subprocess.run(
                                ["taskkill", "/PID", parts[4], "/F"],
                                capture_output=True, timeout=5,
                            )
                        except subprocess.SubprocessError:
                            pass
        else:
            # Try fuser first (Linux), fall back to lsof (macOS / WSL2)
            killed = False
            try:
                result = subprocess.run(
                    ["fuser", f"{port}/tcp"],
                    capture_output=True, timeout=5,
                )
                if result.returncode == 0:
                    subprocess.run(
                        ["fuser", "-k", f"{port}/tcp"],
                        capture_output=True, timeout=5,
                    )
                    killed = True
            except FileNotFoundError:
                pass  # fuser not installed

            if not killed:
                try:
                    result = subprocess.run(
                        ["lsof", "-ti", f":{port}"],
                        capture_output=True, text=True, timeout=5,
                    )
                    for pid_str in result.stdout.strip().splitlines():
                        try:
                            os.kill(int(pid_str), signal.SIGTERM)
                        except (ValueError, ProcessLookupError, PermissionError):
                            pass
                except FileNotFoundError:
                    pass  # lsof not installed either
    except Exception:
        pass


def _kill_stale_bridge_by_pidfile(session_path: Path) -> None:
    """Kill a bridge process recorded in a PID file from a previous run.

    The bridge writes ``bridge.pid`` into the session directory when it
    starts.  If the gateway crashed without a clean shutdown the old bridge
    process becomes orphaned — this helper finds and kills it.
    """
    pid_file = session_path / "bridge.pid"
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError, TypeError):
        try:
            pid_file.unlink()
        except OSError:
            pass
        return
    try:
        os.kill(pid, 0)  # check existence
        os.kill(pid, signal.SIGTERM)
        logger.info("[whatsapp] Killed stale bridge PID %d from pidfile", pid)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        pid_file.unlink()
    except OSError:
        pass


def _write_bridge_pidfile(session_path: Path, pid: int) -> None:
    """Write the bridge PID to a file for later cleanup."""
    try:
        (session_path / "bridge.pid").write_text(str(pid))
    except OSError:
        pass


def _terminate_bridge_process(proc, *, force: bool = False) -> None:
    """Terminate the bridge process using process-tree semantics where possible."""
    if _IS_WINDOWS:
        cmd = ["taskkill", "/PID", str(proc.pid), "/T"]
        if force:
            cmd.append("/F")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError:
            if force:
                proc.kill()
            else:
                proc.terminate()
            return

        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            raise OSError(details or f"taskkill failed for PID {proc.pid}")
        return

    import signal

    sig = signal.SIGTERM if not force else signal.SIGKILL
    os.killpg(os.getpgid(proc.pid), sig)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gateway.config import Platform, PlatformConfig
from gateway.platforms._custom.compat import ensure_media_dispatch_pool
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    SUPPORTED_DOCUMENT_TYPES,
    cache_image_from_url,
    cache_audio_from_url,
)


def check_whatsapp_requirements() -> bool:
    """
    Check if WhatsApp dependencies are available.
    
    WhatsApp requires a Node.js bridge for most implementations.
    """
    # Check for Node.js
    try:
        result = subprocess.run(
            ["node", "--version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


class WhatsAppAdapter(BasePlatformAdapter):
    """
    WhatsApp adapter.
    
    This implementation uses a simple HTTP bridge pattern where:
    1. A Node.js process runs the WhatsApp Web client
    2. Messages are forwarded via HTTP/IPC to this Python adapter
    3. Responses are sent back through the bridge
    
    The actual Node.js bridge implementation can vary:
    - whatsapp-web.js based
    - Baileys based
    - Business API based
    
    Configuration:
    - bridge_script: Path to the Node.js bridge script
    - bridge_port: Port for HTTP communication (default: 3000)
    - session_path: Path to store WhatsApp session data
    - dm_policy: "open" | "allowlist" | "disabled" — how DMs are handled (default: "open")
    - allow_from: List of sender IDs allowed in DMs (when dm_policy="allowlist")
    - group_policy: "open" | "allowlist" | "disabled" — which groups are processed (default: "open")
    - group_allow_from: List of group JIDs allowed (when group_policy="allowlist")
    """
    
    # WhatsApp message limits — practical UX limit, not protocol max.
    # WhatsApp allows ~65K but long messages are unreadable on mobile.
    MAX_MESSAGE_LENGTH = 4096
    DEFAULT_REPLY_PREFIX = "⚕ *Hermes Agent*\n────────────\n"
    
    # Default bridge location relative to the hermes-agent install
    _DEFAULT_BRIDGE_DIR = Path(__file__).resolve().parents[2] / "scripts" / "whatsapp-bridge"

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WHATSAPP)
        self._bridge_process: Optional[subprocess.Popen] = None
        self._bridge_port: int = config.extra.get("bridge_port", 3000)
        self._bridge_script: Optional[str] = config.extra.get(
            "bridge_script",
            str(self._DEFAULT_BRIDGE_DIR / "bridge.js"),
        )
        self._session_path: Path = Path(config.extra.get(
            "session_path",
            get_hermes_dir("platforms/whatsapp/session", "whatsapp/session")
        ))
        self._reply_prefix: Optional[str] = config.extra.get("reply_prefix")
        self._dm_policy = str(config.extra.get("dm_policy") or os.getenv("WHATSAPP_DM_POLICY", "open")).strip().lower()
        self._allow_from = self._coerce_allow_list(config.extra.get("allow_from") or config.extra.get("allowFrom"))
        self._group_policy = str(config.extra.get("group_policy") or os.getenv("WHATSAPP_GROUP_POLICY", "open")).strip().lower()
        self._group_allow_from = self._coerce_allow_list(config.extra.get("group_allow_from") or config.extra.get("groupAllowFrom"))
        self._mention_patterns = self._compile_mention_patterns()
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._bridge_log_fh = None
        self._bridge_log: Optional[Path] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._http_session: Optional["aiohttp.ClientSession"] = None
        # Set to True by disconnect() before we SIGTERM our child bridge so
        # _check_managed_bridge_exit() can distinguish an intentional
        # shutdown-time exit (returncode -15 / -2 / 0) from a real crash.
        # Without this, every graceful gateway shutdown/restart would log
        # "Fatal whatsapp adapter error" plus dispatch a fatal-error
        # notification before the normal "✓ whatsapp disconnected" fires.
        self._shutting_down: bool = False

    def _effective_reply_prefix(self) -> str:
        """Return the prefix the Node bridge will add in self-chat mode."""
        whatsapp_mode = os.getenv("WHATSAPP_MODE", "self-chat")
        if whatsapp_mode != "self-chat":
            return ""
        if self._reply_prefix is not None:
            return self._reply_prefix.replace("\\n", "\n")
        env_prefix = os.getenv("WHATSAPP_REPLY_PREFIX")
        if env_prefix is not None:
            return env_prefix.replace("\\n", "\n")
        return self.DEFAULT_REPLY_PREFIX

    def _outgoing_chunk_limit(self) -> int:
        """Reserve room for the bridge-side prefix so final WhatsApp text fits."""
        prefix_len = len(self._effective_reply_prefix())
        # Keep enough space for truncate_message's pagination indicator and
        # code-fence repair even if a user configures a very long prefix.
        return max(1024, self.MAX_MESSAGE_LENGTH - prefix_len)

    def _whatsapp_require_mention(self) -> bool:
        configured = self.config.extra.get("require_mention")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in ("true", "1", "yes", "on")
            return bool(configured)
        return os.getenv("WHATSAPP_REQUIRE_MENTION", "false").lower() in ("true", "1", "yes", "on")

    def _whatsapp_free_response_chats(self) -> set[str]:
        raw = self.config.extra.get("free_response_chats")
        if raw is None:
            raw = os.getenv("WHATSAPP_FREE_RESPONSE_CHATS", "")
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}

    @staticmethod
    def _coerce_allow_list(raw) -> set[str]:
        """Parse allow_from / group_allow_from from config or env var."""
        if raw is None:
            return set()
        if isinstance(raw, list):
            values = raw
        else:
            values = str(raw).split(",")
        return {
            normalized
            for part in values
            if (normalized := WhatsAppAdapter._normalize_allowlist_identifier(str(part)))
        }

    @staticmethod
    def _normalize_allowlist_identifier(value: Optional[str]) -> str:
        if not value:
            return ""
        normalized = str(value).strip()
        if normalized == "*":
            return normalized
        normalized = re.sub(r":.*@", "@", normalized)
        normalized = re.sub(r"@.*", "", normalized)
        return normalized.lstrip("+")

    def _read_lid_mapping(self, identifier: str, suffix: str = "") -> Optional[str]:
        file_path = self._session_path / f"lid-mapping-{identifier}{suffix}.json"
        if not file_path.exists():
            return None
        try:
            mapped = json.loads(file_path.read_text())
        except Exception:
            return None
        return self._normalize_allowlist_identifier(mapped)

    def _expand_allowlist_identifiers(self, identifier: str) -> set[str]:
        normalized = self._normalize_allowlist_identifier(identifier)
        if not normalized:
            return set()

        resolved: set[str] = set()
        queue = [normalized]
        while queue:
            current = queue.pop(0)
            if not current or current in resolved:
                continue
            resolved.add(current)
            for suffix in ("", "_reverse"):
                mapped = self._read_lid_mapping(current, suffix)
                if mapped and mapped not in resolved:
                    queue.append(mapped)
        return resolved

    def _is_dm_allowed(self, sender_id: str) -> bool:
        """Check whether a DM from the given sender should be processed."""
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            if "*" in self._allow_from:
                return True
            return bool(self._expand_allowlist_identifiers(sender_id) & self._allow_from)
        # "open" — all DMs allowed
        return True

    def _is_group_allowed(self, chat_id: str) -> bool:
        """Check whether a group chat should be processed."""
        if self._group_policy == "disabled":
            return False
        if self._group_policy == "allowlist":
            return chat_id in self._group_allow_from
        # "open" — all groups allowed
        return True

    def _compile_mention_patterns(self):
        patterns = self.config.extra.get("mention_patterns")
        if patterns is None:
            raw = os.getenv("WHATSAPP_MENTION_PATTERNS", "").strip()
            if raw:
                try:
                    patterns = json.loads(raw)
                except Exception:
                    patterns = [part.strip() for part in raw.splitlines() if part.strip()]
                    if not patterns:
                        patterns = [part.strip() for part in raw.split(",") if part.strip()]
        if patterns is None:
            return []
        if isinstance(patterns, str):
            patterns = [patterns]
        if not isinstance(patterns, list):
            logger.warning("[%s] whatsapp mention_patterns must be a list or string; got %s", self.name, type(patterns).__name__)
            return []

        compiled = []
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            try:
                compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                logger.warning("[%s] Invalid WhatsApp mention pattern %r: %s", self.name, pattern, exc)
        if compiled:
            logger.info("[%s] Loaded %d WhatsApp mention pattern(s)", self.name, len(compiled))
        return compiled

    @staticmethod
    def _normalize_whatsapp_id(value: Optional[str]) -> str:
        if not value:
            return ""
        normalized = str(value).strip()
        if ":" in normalized and "@" in normalized:
            normalized = normalized.replace(":", "@", 1)
        return normalized

    def _bot_ids_from_message(self, data: Dict[str, Any]) -> set[str]:
        bot_ids = set()
        for candidate in data.get("botIds") or []:
            normalized = self._normalize_whatsapp_id(candidate)
            if normalized:
                bot_ids.add(normalized)
        return bot_ids

    def _message_is_reply_to_bot(self, data: Dict[str, Any]) -> bool:
        quoted_participant = self._normalize_whatsapp_id(data.get("quotedParticipant"))
        if not quoted_participant:
            return False
        return quoted_participant in self._bot_ids_from_message(data)

    def _message_mentions_bot(self, data: Dict[str, Any]) -> bool:
        bot_ids = self._bot_ids_from_message(data)
        if not bot_ids:
            return False
        mentioned_ids = {
            nid
            for candidate in (data.get("mentionedIds") or [])
            if (nid := self._normalize_whatsapp_id(candidate))
        }
        if mentioned_ids & bot_ids:
            return True

        body = str(data.get("body") or "")
        lower_body = body.lower()
        for bot_id in bot_ids:
            bare_id = bot_id.split("@", 1)[0].lower()
            if bare_id and (f"@{bare_id}" in lower_body or bare_id in lower_body):
                return True
        return False

    def _message_matches_mention_patterns(self, data: Dict[str, Any]) -> bool:
        if not self._mention_patterns:
            return False
        body = str(data.get("body") or "")
        return any(pattern.search(body) for pattern in self._mention_patterns)

    def _clean_bot_mention_text(self, text: str, data: Dict[str, Any]) -> str:
        if not text:
            return text
        bot_ids = self._bot_ids_from_message(data)
        cleaned = text
        for bot_id in bot_ids:
            bare_id = bot_id.split("@", 1)[0]
            if bare_id:
                cleaned = re.sub(rf"@{re.escape(bare_id)}\b[,:\-]*\s*", "", cleaned)
        return cleaned.strip() or text

    def _should_process_message(self, data: Dict[str, Any]) -> bool:
        is_group = data.get("isGroup", False)
        if is_group:
            chat_id = str(data.get("chatId") or "")
            if not self._is_group_allowed(chat_id):
                return False
        else:
            sender_id = str(data.get("senderId") or data.get("from") or "")
            if not self._is_dm_allowed(sender_id):
                return False
            # DMs that pass the policy gate are always processed
            return True
        # Group messages: check mention / free-response settings
        chat_id = str(data.get("chatId") or "")
        if chat_id in self._whatsapp_free_response_chats():
            return True
        if not self._whatsapp_require_mention():
            return True
        body = str(data.get("body") or "").strip()
        if body.startswith("/"):
            return True
        if self._message_is_reply_to_bot(data):
            return True
        if self._message_mentions_bot(data):
            return True
        return self._message_matches_mention_patterns(data)
    
    async def connect(self) -> bool:
        """
        Start the WhatsApp bridge.
        
        This launches the Node.js bridge process and waits for it to be ready.
        """
        if not check_whatsapp_requirements():
            logger.warning("[%s] Node.js not found. WhatsApp requires Node.js.", self.name)
            return False
        
        bridge_path = Path(self._bridge_script)
        if not bridge_path.exists():
            logger.warning("[%s] Bridge script not found: %s", self.name, bridge_path)
            return False
        
        logger.info("[%s] Bridge found at %s", self.name, bridge_path)
        
        # Acquire scoped lock to prevent duplicate sessions
        lock_acquired = False
        try:
            if not self._acquire_platform_lock('whatsapp-session', str(self._session_path), 'WhatsApp session'):
                return False
            lock_acquired = True
        except Exception as e:
            logger.warning("[%s] Could not acquire session lock (non-fatal): %s", self.name, e)

        try:
            # Auto-install npm dependencies if node_modules doesn't exist
            bridge_dir = bridge_path.parent
            if not (bridge_dir / "node_modules").exists():
                print(f"[{self.name}] Installing WhatsApp bridge dependencies...")
                try:
                    install_result = subprocess.run(
                        ["npm", "install", "--silent"],
                        cwd=str(bridge_dir),
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    if install_result.returncode != 0:
                        print(f"[{self.name}] npm install failed: {install_result.stderr}")
                        return False
                    print(f"[{self.name}] Dependencies installed")
                except Exception as e:
                    print(f"[{self.name}] Failed to install dependencies: {e}")
                    return False

            # Ensure session directory exists
            self._session_path.mkdir(parents=True, exist_ok=True)
            
            # Check if bridge is already running and connected
            import aiohttp
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"http://127.0.0.1:{self._bridge_port}/health",
                        timeout=aiohttp.ClientTimeout(total=2)
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            bridge_status = data.get("status", "unknown")
                            if bridge_status == "connected":
                                print(f"[{self.name}] Using existing bridge (status: {bridge_status})")
                                self._mark_connected()
                                self._bridge_process = None  # Not managed by us
                                self._http_session = aiohttp.ClientSession()
                                self._poll_task = asyncio.create_task(self._poll_messages())
                                return True
                            else:
                                print(f"[{self.name}] Bridge found but not connected (status: {bridge_status}), restarting")
            except Exception:
                pass  # Bridge not running, start a new one
            
            # Kill any orphaned bridge from a previous gateway run
            _kill_stale_bridge_by_pidfile(self._session_path)
            _kill_port_process(self._bridge_port)
            await asyncio.sleep(1)
            
            # Start the bridge process in its own process group.
            # Route output to a log file so QR codes, errors, and reconnection
            # messages are preserved for troubleshooting.
            whatsapp_mode = os.getenv("WHATSAPP_MODE", "self-chat")
            self._bridge_log = self._session_path.parent / "bridge.log"
            bridge_log_fh = open(self._bridge_log, "a")
            self._bridge_log_fh = bridge_log_fh

            # Build bridge subprocess environment.
            # Pass WHATSAPP_REPLY_PREFIX from config.yaml so the Node bridge
            # can use it without the user needing to set a separate env var.
            bridge_env = os.environ.copy()
            if self._reply_prefix is not None:
                bridge_env["WHATSAPP_REPLY_PREFIX"] = self._reply_prefix

            self._bridge_process = subprocess.Popen(
                [
                    "node",
                    str(bridge_path),
                    "--port", str(self._bridge_port),
                    "--session", str(self._session_path),
                    "--mode", whatsapp_mode,
                ],
                stdout=bridge_log_fh,
                stderr=bridge_log_fh,
                preexec_fn=None if _IS_WINDOWS else os.setsid,
                env=bridge_env,
            )
            _write_bridge_pidfile(self._session_path, self._bridge_process.pid)
            
            # Wait for the bridge to connect to WhatsApp.
            # Phase 1: wait for the HTTP server to come up (up to 15s).
            # Phase 2: wait for WhatsApp status: connected (up to 15s more).
            import aiohttp
            http_ready = False
            data = {}
            for attempt in range(15):
                await asyncio.sleep(1)
                if self._bridge_process.poll() is not None:
                    print(f"[{self.name}] Bridge process died (exit code {self._bridge_process.returncode})")
                    print(f"[{self.name}] Check log: {self._bridge_log}")
                    self._close_bridge_log()
                    return False
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            f"http://127.0.0.1:{self._bridge_port}/health",
                            timeout=aiohttp.ClientTimeout(total=2)
                        ) as resp:
                            if resp.status == 200:
                                http_ready = True
                                data = await resp.json()
                                if data.get("status") == "connected":
                                    print(f"[{self.name}] Bridge ready (status: connected)")
                                    break
                except Exception:
                    continue

            if not http_ready:
                print(f"[{self.name}] Bridge HTTP server did not start in 15s")
                print(f"[{self.name}] Check log: {self._bridge_log}")
                self._close_bridge_log()
                return False
            
            # Phase 2: HTTP is up but WhatsApp may still be connecting.
            # Give it more time to authenticate with saved credentials.
            if data.get("status") != "connected":
                print(f"[{self.name}] Bridge HTTP ready, waiting for WhatsApp connection...")
                for attempt in range(15):
                    await asyncio.sleep(1)
                    if self._bridge_process.poll() is not None:
                        print(f"[{self.name}] Bridge process died during connection")
                        print(f"[{self.name}] Check log: {self._bridge_log}")
                        self._close_bridge_log()
                        return False
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(
                                f"http://127.0.0.1:{self._bridge_port}/health",
                                timeout=aiohttp.ClientTimeout(total=2)
                            ) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    if data.get("status") == "connected":
                                        print(f"[{self.name}] Bridge ready (status: connected)")
                                        break
                    except Exception:
                        continue
                else:
                    # Still not connected — warn but proceed (bridge may
                    # auto-reconnect later, e.g. after a code 515 restart).
                    print(f"[{self.name}] ⚠ WhatsApp not connected after 30s")
                    print(f"[{self.name}]   Bridge log: {self._bridge_log}")
                    print(f"[{self.name}]   If session expired, re-pair: hermes whatsapp")
            
            # Create a persistent HTTP session for all bridge communication
            self._http_session = aiohttp.ClientSession()

            # Start message polling task
            self._poll_task = asyncio.create_task(self._poll_messages())
            
            self._mark_connected()
            print(f"[{self.name}] Bridge started on port {self._bridge_port}")
            return True
            
        except Exception as e:
            logger.error("[%s] Failed to start bridge: %s", self.name, e, exc_info=True)
            return False
        finally:
            if not self._running:
                if lock_acquired:
                    self._release_platform_lock()
                self._close_bridge_log()
    
    def _close_bridge_log(self) -> None:
        """Close the bridge log file handle if open."""
        if self._bridge_log_fh:
            try:
                self._bridge_log_fh.close()
            except Exception:
                pass
            self._bridge_log_fh = None

    async def _check_managed_bridge_exit(self) -> Optional[str]:
        """Return a fatal error message if the managed bridge child exited."""
        if self._bridge_process is None:
            return None

        returncode = self._bridge_process.poll()
        if returncode is None:
            return None

        # Planned shutdown: disconnect() sets _shutting_down before it sends
        # SIGTERM to the bridge, so a returncode of -15 (SIGTERM), -2 (SIGINT),
        # or 0 (clean exit) at that point is expected, not a crash. Treat it
        # as informational and skip the fatal-error path.
        # getattr-with-default keeps tests that construct the adapter via
        # ``WhatsAppAdapter.__new__`` (bypassing __init__) working without
        # every _make_adapter() helper having to seed the attribute.
        if getattr(self, "_shutting_down", False) and returncode in (0, -2, -15):
            logger.info(
                "[%s] Bridge exited during shutdown (code %d).",
                self.name,
                returncode,
            )
            return None

        message = f"WhatsApp bridge process exited unexpectedly (code {returncode})."
        if not self.has_fatal_error:
            logger.error("[%s] %s", self.name, message)
            self._set_fatal_error("whatsapp_bridge_exited", message, retryable=True)
            self._close_bridge_log()
            await self._notify_fatal_error()
        return self.fatal_error_message or message

    async def disconnect(self) -> None:
        """Stop the WhatsApp bridge and clean up any orphaned processes."""
        # Flip the shutdown flag BEFORE signalling the child so the exit-check
        # path (which runs from other tasks like send() and the poll loop)
        # doesn't race us and report the intentional termination as fatal.
        self._shutting_down = True
        if self._bridge_process:
            try:
                try:
                    _terminate_bridge_process(self._bridge_process, force=False)
                except (ProcessLookupError, PermissionError):
                    self._bridge_process.terminate()
                await asyncio.sleep(1)
                if self._bridge_process.poll() is None:
                    try:
                        _terminate_bridge_process(self._bridge_process, force=True)
                    except (ProcessLookupError, PermissionError):
                        self._bridge_process.kill()
            except Exception as e:
                print(f"[{self.name}] Error stopping bridge: {e}")
        else:
            # Bridge was not started by us, don't kill it
            print(f"[{self.name}] Disconnecting (external bridge left running)")

        # Clean up PID file
        try:
            (self._session_path / "bridge.pid").unlink(missing_ok=True)
        except OSError:
            pass

        # Cancel the poll task explicitly
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
        self._poll_task = None

        # Close the persistent HTTP session
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
        self._http_session = None

        self._release_platform_lock()

        self._mark_disconnected()
        self._bridge_process = None
        self._close_bridge_log()
        print(f"[{self.name}] Disconnected")
    
    def format_message(self, content: str) -> str:
        """Convert standard markdown to WhatsApp-compatible formatting.

        WhatsApp supports: *bold*, _italic_, ~strikethrough~, ```code```,
        and monospaced `inline`. Standard markdown uses different syntax
        for bold/italic/strikethrough, so we convert here.

        Code blocks (``` fenced) and inline code (`) are protected from
        conversion via placeholder substitution.
        """
        if not content:
            return content

        # --- 1. Protect fenced code blocks from formatting changes ---
        _FENCE_PH = "\x00FENCE"
        fences: list[str] = []

        def _save_fence(m: re.Match) -> str:
            fences.append(m.group(0))
            return f"{_FENCE_PH}{len(fences) - 1}\x00"

        result = re.sub(r"```[\s\S]*?```", _save_fence, content)

        # --- 2. Protect inline code ---
        _CODE_PH = "\x00CODE"
        codes: list[str] = []

        def _save_code(m: re.Match) -> str:
            codes.append(m.group(0))
            return f"{_CODE_PH}{len(codes) - 1}\x00"

        result = re.sub(r"`[^`\n]+`", _save_code, result)

        # --- 3. Convert markdown formatting to WhatsApp syntax ---
        # Bold: **text** or __text__ → *text*
        result = re.sub(r"\*\*(.+?)\*\*", r"*\1*", result)
        result = re.sub(r"__(.+?)__", r"*\1*", result)
        # Strikethrough: ~~text~~ → ~text~
        result = re.sub(r"~~(.+?)~~", r"~\1~", result)
        # Italic: *text* is already WhatsApp italic — leave as-is
        # _text_ is already WhatsApp italic — leave as-is

        # --- 4. Convert markdown headers to bold text ---
        # # Header → *Header*
        result = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", result, flags=re.MULTILINE)

        # --- 5. Convert markdown links: [text](url) → text (url) ---
        result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", result)

        # --- 6. Restore protected sections ---
        for i, fence in enumerate(fences):
            result = result.replace(f"{_FENCE_PH}{i}\x00", fence)
        for i, code in enumerate(codes):
            result = result.replace(f"{_CODE_PH}{i}\x00", code)

        return result

    @staticmethod
    def _strip_accents_for_command(value: str) -> str:
        import unicodedata

        normalized = unicodedata.normalize("NFKD", value or "")
        return "".join(ch for ch in normalized if not unicodedata.combining(ch))

    def _parse_thumbnail_shortcut(self, event: MessageEvent) -> tuple[str, str, str] | None:
        text = (event.text or "").strip()
        if not text:
            return None

        normalized = self._strip_accents_for_command(text).lower().strip()
        normalized = re.sub(r"^\s*hermes[,:\-\s]+", "", normalized)
        thumb_word = r"(?:thumb(?:nail)?|tambine\w*)"
        command_prefix = r"(?:(?:gera(?:r)?|cria(?:r)?|crie|faz(?:er)?|faca)\s+(?:uma?\s+)?)?"
        person_word = r"(daiane|vinicius|vini)"
        match = re.match(
            command_prefix + thumb_word + r"\s+(?:(?:do|da|de)\s+)?" + person_word
            + r"\b(?:\s*(?:para|pra|dessa|desta|da|de)?\s*)?(.*)$",
            normalized,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            match = re.search(
                thumb_word + r"\s+(?:(?:do|da|de)\s+)?" + person_word
                + r"\b(?:\s*(?:para|pra|dessa|desta|da|de)?\s*)?(.*)$",
                normalized,
                flags=re.IGNORECASE | re.DOTALL,
            )
        if not match:
            return None

        person = "vinicius" if match.group(1) in {"vinicius", "vini"} else "daiane"
        original = re.sub(r"^\s*hermes[,:\-\s]+", "", text, flags=re.IGNORECASE)
        original_norm = self._strip_accents_for_command(original).lower()
        original_match = re.search(
            thumb_word + r"\s+(?:(?:do|da|de)\s+)?(?:daiane|vinicius|vini)\b"
            + r"(?:\s*(?:para|pra|dessa|desta|da|de)?\s*)?(.*)$",
            original_norm,
            flags=re.IGNORECASE | re.DOTALL,
        )
        legenda = (original[original_match.start(1):] if original_match else match.group(2)).strip()
        legenda = re.sub(
            r"^(?:essa|esta)?\s*legenda\s*:?\s*",
            "",
            legenda,
            flags=re.IGNORECASE,
        ).strip()
        legenda = re.sub(
            r"^(?:com\s+)?(?:essa|esta)?\s*foto\s*:?\s*",
            "",
            legenda,
            flags=re.IGNORECASE,
        ).strip()
        provider = "auto"
        normalized_legenda = self._strip_accents_for_command(legenda).lower()
        provider_match = re.search(
            r"\b(?:usar|usa|com|no|na|via)\s+(openai|gpt|chatgpt|gemini)\b",
            normalized_legenda,
        )
        if provider_match:
            raw_provider = provider_match.group(1)
            provider = "openai" if raw_provider in {"openai", "gpt", "chatgpt"} else "gemini"
            legenda = re.sub(
                r"\b(?:usar|usa|com|no|na|via)\s+(?:openai|gpt|chatgpt|gemini)\b",
                "",
                legenda,
                flags=re.IGNORECASE,
            ).strip(" :,-")
        return person, legenda, provider

    def _thumbnail_reference_context(self, event: MessageEvent, person: str) -> str:
        media_urls = event.media_urls or []
        media_types = event.media_types or []
        image_refs = [
            str(url)
            for idx, url in enumerate(media_urls)
            if str(media_types[idx] if idx < len(media_types) else "").startswith("image/")
            or str(url).lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
        ]
        if not image_refs:
            return ""
        if person == "vinicius":
            return f"using the attached face/body reference image: {image_refs[0]}"
        return f"using the attached visual reference image: {image_refs[0]}"

    @staticmethod
    def _content_capture_sender_aliases(value: Optional[str]) -> set[str]:
        if not value:
            return set()
        raw = str(value).strip().lower()
        aliases = {raw}
        if ":" in raw and "@" in raw:
            aliases.add(raw.replace(":", "@", 1))
        digits = "".join(ch for ch in raw if ch.isdigit())
        if digits:
            aliases.update(
                {
                    digits,
                    f"+{digits}",
                    f"{digits}@s.whatsapp.net",
                    f"{digits}@c.us",
                    f"{digits}@lid",
                }
            )
        return {alias for alias in aliases if alias}

    def _content_capture_sender_allowed(self, sender_id: Optional[str]) -> bool:
        configured = os.getenv("CONTENT_CAPTURE_ALLOWED_SENDERS", "").strip()
        allowed = (
            [part.strip() for part in configured.split(",") if part.strip()]
            if configured
            else list(_CONTENT_CAPTURE_ALLOWED_SENDERS)
        )
        sender_aliases = self._content_capture_sender_aliases(sender_id)
        if not sender_aliases:
            return False
        for candidate in allowed:
            if sender_aliases & self._content_capture_sender_aliases(candidate):
                return True
        return False

    @staticmethod
    def _find_content_capture_trigger(text: str) -> Optional[str]:
        haystack = (text or "").casefold()
        if not haystack:
            return None
        for trigger in _CONTENT_CAPTURE_TRIGGERS:
            if trigger.casefold() in haystack:
                return trigger
        return None

    @staticmethod
    def _extract_content_capture_audio(event: MessageEvent) -> Dict[str, Any]:
        media_urls = event.media_urls or []
        media_types = event.media_types or []
        for idx, url in enumerate(media_urls):
            media_type = str(media_types[idx] if idx < len(media_types) else "")
            url_str = str(url)
            if event.message_type not in (MessageType.AUDIO, MessageType.VOICE) and not media_type.startswith("audio/"):
                continue
            if os.path.isabs(url_str):
                return {"path": url_str, "mime_type": media_type or "audio/ogg"}
            if url_str.startswith(("http://", "https://")):
                return {"url": url_str, "mime_type": media_type or "audio/ogg"}
        return {}

    def _build_content_capture_payload(self, event: MessageEvent) -> Dict[str, Any]:
        raw_message = event.raw_message if isinstance(event.raw_message, dict) else {}
        payload: Dict[str, Any] = {
            "source_message_id": event.message_id or raw_message.get("messageId"),
            "sender_jid": event.source.user_id or raw_message.get("senderId") or raw_message.get("chatId"),
            "text": event.text or raw_message.get("body") or "",
            "chat_id": event.source.chat_id,
        }
        audio = self._extract_content_capture_audio(event)
        if audio:
            payload["audio"] = audio
        return payload

    async def _run_content_capture_worker(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            _CONTENT_CAPTURE_WORKER,
            "--stdin-json",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        if proc.returncode != 0:
            raise RuntimeError(
                (stderr or stdout or b"worker_failed").decode("utf-8", errors="replace").strip()
            )
        output = stdout.decode("utf-8", errors="replace").strip()
        if not output:
            raise RuntimeError("worker_sem_saida")
        return json.loads(output.splitlines()[-1])

    @staticmethod
    def _find_content_link_trigger(text: str) -> Optional[str]:
        haystack = (text or "").casefold()
        if not haystack:
            return None
        for trigger in _CONTENT_LINK_ANALYZER_TRIGGERS:
            if trigger.casefold() in haystack:
                return trigger
        return None

    @staticmethod
    def _find_content_link_source_url(text: str) -> Optional[str]:
        candidates = re.findall(r"https?://[^\s<>\"]+", text or "", flags=re.IGNORECASE)
        for url in candidates:
            lowered = url.lower()
            if any(
                host in lowered
                for host in (
                    "youtube.com/",
                    "youtu.be/",
                    "instagram.com/",
                    "tiktok.com/",
                    "vm.tiktok.com/",
                )
            ):
                return url
        return None

    @staticmethod
    def _is_content_link_followup(text: str) -> bool:
        normalized = (text or "").strip().casefold()
        return normalized in {value.casefold() for value in _CONTENT_LINK_ANALYZER_FOLLOWUPS}

    async def _run_content_link_worker(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            _CONTENT_LINK_ANALYZER_WORKER,
            "--stdin-json",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        if proc.returncode != 0:
            raise RuntimeError(
                (stderr or stdout or b"worker_failed").decode("utf-8", errors="replace").strip()
            )
        output = stdout.decode("utf-8", errors="replace").strip()
        if not output:
            raise RuntimeError("worker_sem_saida")
        return json.loads(output.splitlines()[-1])

    async def _handle_content_link_shortcut(self, event: MessageEvent) -> bool:
        if getattr(event.source, "chat_type", "") != "dm":
            return False
        sender_id = event.source.user_id or (
            event.raw_message.get("senderId") if isinstance(event.raw_message, dict) else None
        )
        if not self._content_capture_sender_allowed(sender_id):
            return False

        payload = self._build_content_capture_payload(event)
        text = str(payload.get("text") or "")
        trigger = self._find_content_link_trigger(text)
        source_url = self._find_content_link_source_url(text)
        if not ((trigger and source_url) or self._is_content_link_followup(text)):
            return False

        try:
            result = await self._run_content_link_worker(payload)
        except Exception as exc:
            logger.exception("[%s] content link shortcut failed: %s", self.name, exc)
            await self.send(
                event.source.chat_id,
                "Nao consegui analisar esse video agora. Tenta de novo com outro link ou me chama depois.",
                reply_to=event.message_id,
            )
            return True

        if result.get("status") == "ignored":
            return False

        status = str(result.get("status") or "").strip().lower()
        theme = result.get("theme") or "Video para revisar"

        if status == "too_long":
            duration = result.get("duration_seconds") or "?"
            limit = result.get("max_video_seconds") or 300
            confirmation = (
                f"Esse video tem {duration}s e passou do limite de {limit}s.\n"
                "Me manda uma versao de ate 5 minutos que eu analiso e transformo."
            )
        elif status == "scripted":
            script_path = result.get("script_path") or "roteiros/"
            confirmation = (
                "Roteiro inspirado pronto.\n"
                f"Tema: {theme}\n"
                f"Caminho: {script_path}"
            )
        elif status == "banked":
            confirmation = (
                "Fechado. Guardei esse video no banco de ideias.\n"
                f"Tema: {theme}"
            )
        elif status == "needs_review" and result.get("warning") == "download_failed_fallback_phase1":
            confirmation = (
                "Nao consegui baixar o audio do video, mas guardei titulo e descricao no banco para revisao manual.\n"
                f"Tema salvo: {theme}"
            )
        else:
            eixo = result.get("eixo") or "revisar eixo"
            duration = result.get("duration_seconds") or result.get("duracao_seg") or "?"
            confirmation = (
                f"Video {duration}s analisado.\n"
                f"Eixo: {eixo}\n\n"
                "Quer que eu gere uma versao sua mantendo o mesmo eixo? Responde `gera` ou `so guarda`."
            )

        await self.send(
            event.source.chat_id,
            confirmation,
            reply_to=event.message_id,
        )
        return True

    async def _handle_content_capture_shortcut(self, event: MessageEvent) -> bool:
        if getattr(event.source, "chat_type", "") != "dm":
            return False
        sender_id = event.source.user_id or (
            event.raw_message.get("senderId") if isinstance(event.raw_message, dict) else None
        )
        if not self._content_capture_sender_allowed(sender_id):
            return False

        payload = self._build_content_capture_payload(event)
        try:
            result = await self._run_content_capture_worker(payload)
        except Exception as exc:
            logger.exception("[%s] content capture shortcut failed: %s", self.name, exc)
            await self.send(
                event.source.chat_id,
                "Nao consegui salvar essa ideia agora. Tenta de novo em texto ou me chama depois.",
                reply_to=event.message_id,
            )
            return True

        if result.get("status") == "ignored":
            return False

        status = str(result.get("status") or "").strip().lower()
        theme = result.get("theme") or "Ideia capturada para revisar"
        hook = result.get("hook") or "Revisar manualmente"

        if status == "scripted":
            script_path = result.get("script_path") or "roteiros/"
            confirmation = (
                "Roteiro gerado.\n"
                f"Tema: {theme}\n"
                f"Caminho: {script_path}"
            )
        elif result.get("vini_decision") == "banked":
            confirmation = (
                "Fechado. Deixei essa ideia no banco.\n"
                f"Tema: {theme}"
            )
        else:
            confirmation = (
                "Ideia salva.\n"
                f"Tema sugerido: {theme}\n"
                f"Hook: {hook}\n\n"
                "Quer que eu transforme em roteiro agora ou deixo no banco?"
            )
        await self.send(
            event.source.chat_id,
            confirmation,
            reply_to=event.message_id,
        )
        return True

    def _load_content_media_avatar_module(self):
        module_path = _CONTENT_MEDIA_AVATAR_REGISTRY
        cached = getattr(self, "_content_media_avatar_module", None)
        cached_path = getattr(self, "_content_media_avatar_module_path", "")
        if cached is not None and cached_path == str(module_path):
            return cached
        if not module_path.is_file():
            raise FileNotFoundError(f"avatar_registry ausente em {module_path}")
        spec = importlib.util.spec_from_file_location("hermes_content_avatar_registry", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"nao foi possivel carregar avatar_registry de {module_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self._content_media_avatar_module = module
        self._content_media_avatar_module_path = str(module_path)
        return module

    @staticmethod
    def _split_content_media_command(text: str) -> tuple[str, str]:
        normalized = (text or "").strip()
        if not normalized:
            return "", ""
        parts = normalized.split(maxsplit=1)
        command = parts[0].casefold()
        remainder = parts[1].strip() if len(parts) > 1 else ""
        return command, remainder

    @staticmethod
    def _looks_like_content_media_alias(token: str) -> bool:
        normalized = re.sub(r"\s+", "", str(token or "").casefold())
        if not normalized:
            return False
        if normalized in _CONTENT_MEDIA_ALIAS_HINTS:
            return True
        if normalized.startswith(("vinicius", "escritorio")):
            return True
        return "-" in normalized or "_" in normalized

    def _parse_content_media_avatar(
        self,
        token: str | None,
        *,
        registry: Any,
        avatar_module: Any,
    ):
        normalized = str(token or "").strip().casefold() or None
        if normalized and normalized not in registry.aliases and normalized not in registry.groups:
            raise avatar_module.AvatarAliasError(normalized, registry)
        return avatar_module.resolve_alias_details(
            normalized,
            config_path=registry.source_path,
            registry=registry,
        )

    def _parse_content_media_video_request(self, text: str) -> dict[str, Any]:
        avatar_module = self._load_content_media_avatar_module()
        registry = avatar_module.load_avatar_registry()
        command, remainder = self._split_content_media_command(text)
        if command not in _CONTENT_MEDIA_VIDEO_COMMANDS:
            raise ValueError("unsupported_video_command")

        alias_token = None
        prompt = remainder
        if remainder:
            first_token, _, rest = remainder.partition(" ")
            normalized_first = first_token.strip().casefold()
            if normalized_first in registry.aliases or normalized_first in registry.groups:
                alias_token = normalized_first
                prompt = rest.strip()
            elif command == "#video" and self._looks_like_content_media_alias(normalized_first):
                raise avatar_module.AvatarAliasError(normalized_first, registry)
            elif command == "#video-audio" and self._looks_like_content_media_alias(normalized_first):
                raise avatar_module.AvatarAliasError(normalized_first, registry)

        resolution = self._parse_content_media_avatar(
            alias_token,
            registry=registry,
            avatar_module=avatar_module,
        )

        if command == "#video":
            prompt = prompt.strip()
            if not prompt:
                raise ValueError("missing_video_prompt")
        else:
            prompt = prompt.strip() or None

        return {
            "command": command,
            "prompt": prompt,
            "avatar_alias": resolution.resolved_alias,
            "avatar_id": resolution.avatar_id,
            "registry": registry,
        }

    @staticmethod
    def _parse_content_media_image_request(text: str) -> dict[str, Any]:
        command, remainder = WhatsAppAdapter._split_content_media_command(text)
        prompt = remainder.strip()
        if command not in _CONTENT_MEDIA_IMAGE_COMMANDS:
            raise ValueError("unsupported_image_command")
        if command == "#thumb":
            first, _, rest = prompt.partition(" ")
            if not first.strip() or not rest.strip():
                raise ValueError("missing_thumb_prompt")
        elif not prompt:
            raise ValueError("missing_image_prompt")
        return {
            "command": command,
            "prompt": prompt,
        }

    @staticmethod
    def _extract_content_media_audio_source(event: MessageEvent) -> str | None:
        audio = WhatsAppAdapter._extract_content_capture_audio(event)
        return str(audio.get("path") or audio.get("url") or "").strip() or None

    @staticmethod
    def _content_media_worker_path(command: str) -> Path:
        if command in _CONTENT_MEDIA_IMAGE_COMMANDS:
            return Path(_CONTENT_IMAGE_WORKER)
        return Path(_CONTENT_VIDEO_WORKER)

    @staticmethod
    def _content_media_approval_worker_path() -> Path:
        return Path(_CONTENT_MEDIA_APPROVAL_WORKER)

    async def _enqueue_content_media_job(
        self,
        *,
        job_type: str,
        command: str,
        prompt: str | None = None,
        source_audio_url: str | None = None,
        avatar_id: str | None = None,
        avatar_alias: str | None = None,
    ) -> str:
        pool = await ensure_media_dispatch_pool(self)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO content.media_jobs (
                    type,
                    command,
                    prompt,
                    source_audio_url,
                    avatar_id,
                    avatar_alias,
                    status
                )
                VALUES ($1, $2, $3, $4, $5, $6, 'queued')
                RETURNING id::text AS id
                """,
                job_type,
                command,
                prompt,
                source_audio_url,
                avatar_id,
                avatar_alias,
            )
        return str(row["id"])

    async def _run_content_media_worker(self, worker_path: Path, *, command: str, job_id: str) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(worker_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            logger.exception(
                "[%s] content media worker start failed command=%s job_id=%s err=%s",
                self.name,
                command,
                job_id,
                exc,
            )
            return

        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = (stderr or stdout or b"worker_failed").decode("utf-8", errors="replace").strip()
            logger.error(
                "[%s] content media worker failed command=%s job_id=%s detail=%s",
                self.name,
                command,
                job_id,
                detail[:500],
            )
            return

        output = stdout.decode("utf-8", errors="replace").strip()
        logger.info(
            "[%s] content media worker finished command=%s job_id=%s output=%s",
            self.name,
            command,
            job_id,
            output.splitlines()[-1] if output else "ok",
        )
        await self._send_content_media_preview(job_id=job_id)

    def _launch_content_media_worker(self, command: str, *, job_id: str) -> None:
        worker_path = self._content_media_worker_path(command)
        asyncio.create_task(self._run_content_media_worker(worker_path, command=command, job_id=job_id))

    async def _run_content_media_approval_cli(self, *args: str) -> dict[str, Any]:
        worker_path = self._content_media_approval_worker_path()
        if not worker_path.is_file():
            raise FileNotFoundError(f"approval worker ausente em {worker_path}")
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(worker_path),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        raw_output = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            detail = (stderr or stdout or b"approval_failed").decode("utf-8", errors="replace").strip()
            raise RuntimeError(detail[:500])
        if not raw_output:
            return {}
        try:
            return json.loads(raw_output.splitlines()[-1])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"approval_json_invalido:{raw_output[-500:]}") from exc

    async def _send_content_media_preview(self, *, job_id: str) -> None:
        try:
            result = await self._run_content_media_approval_cli(
                "--job-id",
                job_id,
                "--send-preview",
            )
            logger.info(
                "[%s] content media preview sent job_id=%s status=%s",
                self.name,
                job_id,
                result.get("status") or result.get("ok") or "sent",
            )
        except Exception as exc:
            logger.exception("[%s] content media preview failed job_id=%s err=%s", self.name, job_id, exc)

    @staticmethod
    def _looks_like_content_media_decision(text: str) -> bool:
        normalized = re.sub(r"\s+", " ", str(text or "").strip().casefold())
        if not normalized:
            return False
        return any(
            normalized == prefix or normalized.startswith(f"{prefix} ")
            for prefix in _CONTENT_MEDIA_DECISION_PREFIXES
        )

    async def _handle_content_media_decision(self, event: MessageEvent) -> bool:
        if getattr(event.source, "chat_type", "") != "dm":
            return False
        sender_id = event.source.user_id or (
            event.raw_message.get("senderId") if isinstance(event.raw_message, dict) else None
        )
        if not self._content_capture_sender_allowed(sender_id):
            return False
        text = (event.text or "").strip()
        if not self._looks_like_content_media_decision(text):
            return False

        try:
            result = await self._run_content_media_approval_cli("--decision-text", text)
        except FileNotFoundError:
            logger.exception("[%s] content media approval worker missing", self.name)
            return False
        except Exception as exc:
            logger.exception("[%s] content media decision failed: %s", self.name, exc)
            await self.send(
                event.source.chat_id,
                "Nao consegui aplicar essa aprovacao agora. Vou deixar o job pendente para nao perder nada.",
                reply_to=event.message_id,
            )
            return True

        status = str(result.get("status") or "").strip().lower()
        if status in {"ignored", "missing_job"}:
            return False

        new_job_id = result.get("new_job_id")
        command = result.get("command")
        if new_job_id and command:
            self._launch_content_media_worker(str(command), job_id=str(new_job_id))

        reply_text = str(result.get("reply_text") or "").strip()
        if reply_text:
            await self.send(event.source.chat_id, reply_text, reply_to=event.message_id)
        return True

    async def _send_content_media_aliases(self, event: MessageEvent) -> bool:
        avatar_module = self._load_content_media_avatar_module()
        registry = avatar_module.load_avatar_registry()
        if hasattr(avatar_module, "format_available_aliases_message"):
            message = avatar_module.format_available_aliases_message(registry)
        else:
            aliases = avatar_module.list_available_aliases(registry)
            message = (
                f"Avatar default: {registry.default_alias}\n"
                f"Aliases: {', '.join(aliases.get('aliases') or [])}\n"
                f"Grupos: {', '.join(aliases.get('groups') or []) or 'nenhum'}"
            )
        await self.send(
            event.source.chat_id,
            message,
            reply_to=event.message_id,
        )
        return True

    async def _handle_content_media_shortcut(self, event: MessageEvent) -> bool:
        if getattr(event.source, "chat_type", "") != "dm":
            return False
        sender_id = event.source.user_id or (
            event.raw_message.get("senderId") if isinstance(event.raw_message, dict) else None
        )
        if not self._content_capture_sender_allowed(sender_id):
            return False

        text = (event.text or "").strip()
        command, _ = self._split_content_media_command(text)
        if not command:
            return False

        if command == _CONTENT_MEDIA_LIST_COMMAND:
            try:
                return await self._send_content_media_aliases(event)
            except Exception as exc:
                logger.exception("[%s] content media aliases failed: %s", self.name, exc)
                await self.send(
                    event.source.chat_id,
                    "Nao consegui listar os avatares agora. Tenta de novo em seguida.",
                    reply_to=event.message_id,
                )
                return True

        if command not in _CONTENT_MEDIA_VIDEO_COMMANDS and command not in _CONTENT_MEDIA_IMAGE_COMMANDS:
            return False

        try:
            worker_path = self._content_media_worker_path(command)
            if not worker_path.is_file():
                raise FileNotFoundError(f"worker ausente em {worker_path}")

            if command in _CONTENT_MEDIA_VIDEO_COMMANDS:
                parsed = self._parse_content_media_video_request(text)
                source_audio_url = None
                if command == "#video-audio":
                    source_audio_url = self._extract_content_media_audio_source(event)
                    if not source_audio_url:
                        raise ValueError("missing_video_audio_attachment")
                job_id = await self._enqueue_content_media_job(
                    job_type="video",
                    command=command,
                    prompt=parsed.get("prompt"),
                    source_audio_url=source_audio_url,
                    avatar_id=parsed.get("avatar_id"),
                    avatar_alias=parsed.get("avatar_alias"),
                )
                confirmation = (
                    f"Video enfileirado com avatar {parsed.get('avatar_alias')}."
                    if command == "#video"
                    else f"Video com audio enfileirado com avatar {parsed.get('avatar_alias')}."
                )
            else:
                parsed = self._parse_content_media_image_request(text)
                job_id = await self._enqueue_content_media_job(
                    job_type="image",
                    command=command,
                    prompt=parsed.get("prompt"),
                )
                confirmation = f"{command.removeprefix('#').title()} enfileirada."

            self._launch_content_media_worker(command, job_id=job_id)
            await self.send(
                event.source.chat_id,
                f"{confirmation} Job {job_id[:8]}. Te aviso quando terminar.",
                reply_to=event.message_id,
            )
            return True
        except FileNotFoundError as exc:
            logger.exception("[%s] content media runtime missing: %s", self.name, exc)
            await self.send(
                event.source.chat_id,
                "Pipeline de midia ainda nao esta pronto no runtime. Me chama depois do deploy.",
                reply_to=event.message_id,
            )
            return True
        except Exception as exc:
            avatar_module = None
            try:
                avatar_module = self._load_content_media_avatar_module()
            except Exception:
                avatar_module = None

            message = "Nao consegui entender esse comando."
            if avatar_module is not None and isinstance(exc, avatar_module.AvatarAliasError):
                message = str(exc)
            elif str(exc) == "missing_video_prompt":
                message = "Usa assim: #video [avatar] seu texto aqui."
            elif str(exc) == "missing_video_audio_attachment":
                message = "Para #video-audio, anexa um audio e opcionalmente um avatar. Ex.: #video-audio carro"
            elif str(exc) == "missing_image_prompt":
                message = "Usa assim: #imagem seu prompt aqui ou #cinema seu prompt aqui."
            elif str(exc) == "missing_thumb_prompt":
                message = "Usa assim: #thumb PALAVRA seu prompt aqui."
            else:
                logger.exception("[%s] content media shortcut failed: %s", self.name, exc)

            await self.send(
                event.source.chat_id,
                message,
                reply_to=event.message_id,
            )
            return True

    @staticmethod
    def _format_thumbnail_response(person: str, result: dict[str, Any]) -> str:
        if person == "vinicius":
            return (
                "Thumb Vinicius pronta.\n"
                f"Formato: {result.get('aspect_ratio')} / {result.get('size')}\n"
                f"Headline: {result.get('headline')}\n"
                f"Palavra destaque: {result.get('gold_word')}\n\n"
                "Prompt principal:\n"
                f"```{result.get('prompt', '')}```\n\n"
                "Alternativa:\n"
                f"```{result.get('alternative_prompt', '')}```"
            )

        lines = [
            "Thumb Daiane pronta.",
            f"Formato: {result.get('aspect_ratio')} / {result.get('size')}",
            f"Tema: {result.get('theme')}",
            "",
        ]
        for variant in result.get("variants", [])[:3]:
            lines.extend(
                [
                    f"{variant.get('id')} - bloco {variant.get('block_word')} ({variant.get('block_color')})",
                    f"```{variant.get('prompt', '')}```",
                    "",
                ]
            )
        return "\n".join(lines).strip()

    async def _handle_thumbnail_shortcut(self, event: MessageEvent) -> bool:
        parsed = self._parse_thumbnail_shortcut(event)
        if not parsed:
            return False

        person, legenda, provider = parsed
        if not legenda:
            await self.send(
                event.source.chat_id,
                f"Me manda a legenda junto. Exemplo: thumb {person.title()} para essa legenda: ...",
                reply_to=event.message_id,
            )
            return True

        try:
            from gateway.platforms._custom.thumbnail_router import (
                generate_daiane_thumbnail_prompt,
                generate_thumbnail_prompt,
            )
            from gateway.platforms._custom.thumbnail_image_generator import (
                generate_thumbnail_image,
            )

            reference_images = [
                str(url)
                for url in (event.media_urls or [])
                if str(url).lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
            ]
            payload = {
                "legenda": legenda,
                "reference_context": self._thumbnail_reference_context(event, person),
            }
            if person == "vinicius" and (event.media_urls or []):
                payload["face_mode"] = "com_rosto"
            result = (
                generate_daiane_thumbnail_prompt(payload)
                if person == "daiane"
                else generate_thumbnail_prompt(payload)
            )

            progress = await self.send(
                event.source.chat_id,
                f"Gerando a thumb {person.title()} agora em 9:16. Se o provedor de imagem falhar, eu te mando o prompt pronto.",
                reply_to=event.message_id,
            )
            image_result = await generate_thumbnail_image(
                person=person,
                legenda=legenda,
                prompt_result=result,
                reference_images=reference_images,
                provider=provider,
            )
            caption = (
                f"Thumb {person.title()} pronta.\n"
                f"Provider: {image_result.get('provider')}\n"
                "Formato: 9:16 / 1080x1920"
            )
            await self.send_image_file(
                event.source.chat_id,
                image_result["path"],
                caption=caption,
                reply_to=getattr(progress, "message_id", None) or event.message_id,
            )
        except Exception as exc:
            logger.exception("[%s] thumbnail shortcut failed: %s", self.name, exc)
            await self.send(
                event.source.chat_id,
                "Nao consegui gerar a imagem agora. Vou te mandar o prompt pronto para usar manualmente.\n\n"
                + self._format_thumbnail_response(person, result if "result" in locals() else {"prompt": legenda}),
                reply_to=event.message_id,
            )
        return True

    async def _maybe_record_feedback_reaction(self, event: MessageEvent) -> None:
        try:
            from agent.extensions.feedback_middleware import detect_emoji_feedback, post_feedback
            raw = event.raw_message if isinstance(event.raw_message, dict) else {}
            sender_id = event.source.user_id or raw.get("senderId") or raw.get("chatId")
            payload = await detect_emoji_feedback(event.text, sender_id, raw.get("quotedText"))
            if payload:
                await post_feedback(payload)
        except Exception as exc:
            logger.warning("[%s] feedback reaction detection failed: %s", self.name, exc)

    async def handle_message(self, event: MessageEvent) -> None:
        await self._maybe_record_feedback_reaction(event)
        if await self._handle_content_media_decision(event):
            return
        if await self._handle_content_media_shortcut(event):
            return
        if await self._handle_thumbnail_shortcut(event):
            return
        if await self._handle_content_link_shortcut(event):
            return
        if await self._handle_content_capture_shortcut(event):
            return
        await super().handle_message(event)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> SendResult:
        """Send a message via the WhatsApp bridge.

        Formats markdown for WhatsApp, splits long messages into chunks
        that preserve code block boundaries, and sends each chunk sequentially.
        """
        if not self._running or not self._http_session:
            return SendResult(success=False, error="Not connected")
        bridge_exit = await self._check_managed_bridge_exit()
        if bridge_exit:
            return SendResult(success=False, error=bridge_exit)

        if not content or not content.strip():
            return SendResult(success=True, message_id=None)

        feedback_hash_id = None
        try:
            from agent.extensions.feedback_middleware import inject_feedback_hash
            content, feedback_hash_id = inject_feedback_hash(
                content,
                chat_id=chat_id,
                prompt=str((metadata or {}).get("prompt") or ""),
            )
        except Exception as exc:
            logger.warning("[%s] feedback hash injection failed: %s", self.name, exc)

        try:
            import aiohttp

            # Format and chunk the message
            formatted = self.format_message(content)
            chunks = self.truncate_message(formatted, self._outgoing_chunk_limit())

            last_message_id = None
            for chunk in chunks:
                payload: Dict[str, Any] = {
                    "chatId": chat_id,
                    "message": chunk,
                }
                if reply_to and last_message_id is None:
                    # Only reply-to on the first chunk
                    payload["replyTo"] = reply_to

                async with self._http_session.post(
                    f"http://127.0.0.1:{self._bridge_port}/send",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        last_message_id = data.get("messageId")
                    else:
                        error = await resp.text()
                        return SendResult(success=False, error=error)

                # Small delay between chunks to avoid rate limiting
                if len(chunks) > 1:
                    await asyncio.sleep(0.3)

            try:
                from agent.extensions.feedback_middleware import track_usage
                await track_usage(
                    "whatsapp_send",
                    "dm_response",
                    chat_id,
                    True,
                    metadata={"feedback_hash": feedback_hash_id} if feedback_hash_id else {},
                )
            except Exception as exc:
                logger.warning("[%s] usage tracking failed: %s", self.name, exc)

            return SendResult(
                success=True,
                message_id=last_message_id,
            )
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """Edit a previously sent message via the WhatsApp bridge."""
        if not self._running or not self._http_session:
            return SendResult(success=False, error="Not connected")
        bridge_exit = await self._check_managed_bridge_exit()
        if bridge_exit:
            return SendResult(success=False, error=bridge_exit)
        try:
            import aiohttp
            async with self._http_session.post(
                f"http://127.0.0.1:{self._bridge_port}/edit",
                json={
                    "chatId": chat_id,
                    "messageId": message_id,
                    "message": content,
                },
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    return SendResult(success=True, message_id=message_id)
                else:
                    error = await resp.text()
                    return SendResult(success=False, error=error)
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def _send_media_to_bridge(
        self,
        chat_id: str,
        file_path: str,
        media_type: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
    ) -> SendResult:
        """Send any media file via bridge /send-media endpoint."""
        if not self._running or not self._http_session:
            return SendResult(success=False, error="Not connected")
        bridge_exit = await self._check_managed_bridge_exit()
        if bridge_exit:
            return SendResult(success=False, error=bridge_exit)
        try:
            import aiohttp

            if not os.path.exists(file_path):
                return SendResult(success=False, error=f"File not found: {file_path}")

            payload: Dict[str, Any] = {
                "chatId": chat_id,
                "filePath": file_path,
                "mediaType": media_type,
            }
            if caption:
                payload["caption"] = caption
            if file_name:
                payload["fileName"] = file_name

            async with self._http_session.post(
                f"http://127.0.0.1:{self._bridge_port}/send-media",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return SendResult(
                        success=True,
                        message_id=data.get("messageId"),
                        raw_response=data,
                    )
                else:
                    error = await resp.text()
                    return SendResult(success=False, error=error)

        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        """Download image URL to cache, send natively via bridge."""
        try:
            local_path = await cache_image_from_url(image_url)
            return await self._send_media_to_bridge(chat_id, local_path, "image", caption)
        except Exception:
            return await super().send_image(chat_id, image_url, caption, reply_to)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local image file natively via bridge."""
        return await self._send_media_to_bridge(chat_id, image_path, "image", caption)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a video natively via bridge — plays inline in WhatsApp."""
        return await self._send_media_to_bridge(chat_id, video_path, "video", caption)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send an audio file as a WhatsApp voice message via bridge."""
        return await self._send_media_to_bridge(chat_id, audio_path, "audio", caption)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a document/file as a downloadable attachment via bridge."""
        return await self._send_media_to_bridge(
            chat_id, file_path, "document", caption,
            file_name or os.path.basename(file_path),
        )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Send typing indicator via bridge."""
        if not self._running or not self._http_session:
            return
        if await self._check_managed_bridge_exit():
            return
        
        try:
            import aiohttp

            # Must wrap in `async with` — a bare `await session.post(...)`
            # leaves the response object alive until GC, holding its TCP
            # socket in CLOSE_WAIT. See #18451.
            async with self._http_session.post(
                f"http://127.0.0.1:{self._bridge_port}/typing",
                json={"chatId": chat_id},
                timeout=aiohttp.ClientTimeout(total=5)
            ):
                pass
        except Exception:
            pass  # Ignore typing indicator failures
    
    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Get information about a WhatsApp chat."""
        if not self._running or not self._http_session:
            return {"name": "Unknown", "type": "dm"}
        if await self._check_managed_bridge_exit():
            return {"name": chat_id, "type": "dm"}
        
        try:
            import aiohttp

            async with self._http_session.get(
                f"http://127.0.0.1:{self._bridge_port}/chat/{chat_id}",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {
                        "name": data.get("name", chat_id),
                        "type": "group" if data.get("isGroup") else "dm",
                        "participants": data.get("participants", []),
                    }
        except Exception as e:
            logger.debug("Could not get WhatsApp chat info for %s: %s", chat_id, e)
        
        return {"name": chat_id, "type": "dm"}
    
    async def _poll_messages(self) -> None:
        """Poll the bridge for incoming messages."""
        import aiohttp

        while self._running:
            if not self._http_session:
                break
            bridge_exit = await self._check_managed_bridge_exit()
            if bridge_exit:
                print(f"[{self.name}] {bridge_exit}")
                break
            try:
                async with self._http_session.get(
                    f"http://127.0.0.1:{self._bridge_port}/messages",
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        messages = await resp.json()
                        for msg_data in messages:
                            event = await self._build_message_event(msg_data)
                            if event:
                                await self.handle_message(event)
            except asyncio.CancelledError:
                break
            except Exception as e:
                bridge_exit = await self._check_managed_bridge_exit()
                if bridge_exit:
                    print(f"[{self.name}] {bridge_exit}")
                    break
                print(f"[{self.name}] Poll error: {e}")
                await asyncio.sleep(5)
            
            await asyncio.sleep(1)  # Poll interval
    
    async def _build_message_event(self, data: Dict[str, Any]) -> Optional[MessageEvent]:
        """Build a MessageEvent from bridge message data, downloading images to cache."""
        try:
            if not self._should_process_message(data):
                return None

            # Determine message type
            msg_type = MessageType.TEXT
            if data.get("hasMedia"):
                media_type = data.get("mediaType", "")
                if "image" in media_type:
                    msg_type = MessageType.PHOTO
                elif "video" in media_type:
                    msg_type = MessageType.VIDEO
                elif "audio" in media_type or "ptt" in media_type:  # ptt = voice note
                    msg_type = MessageType.VOICE
                else:
                    msg_type = MessageType.DOCUMENT
            
            # Determine chat type
            is_group = data.get("isGroup", False)
            chat_type = "group" if is_group else "dm"
            
            # Build source
            source = self.build_source(
                chat_id=data.get("chatId", ""),
                chat_name=data.get("chatName"),
                chat_type=chat_type,
                user_id=data.get("senderId"),
                user_name=data.get("senderName"),
            )
            
            # Download media URLs to the local cache so agent tools
            # can access them reliably regardless of URL expiration.
            raw_urls = data.get("mediaUrls", [])
            cached_urls = []
            media_types = []
            for url in raw_urls:
                if msg_type == MessageType.PHOTO and url.startswith(("http://", "https://")):
                    try:
                        cached_path = await cache_image_from_url(url, ext=".jpg")
                        cached_urls.append(cached_path)
                        media_types.append("image/jpeg")
                        print(f"[{self.name}] Cached user image: {cached_path}", flush=True)
                    except Exception as e:
                        print(f"[{self.name}] Failed to cache image: {e}", flush=True)
                        cached_urls.append(url)
                        media_types.append("image/jpeg")
                elif msg_type == MessageType.PHOTO and os.path.isabs(url):
                    # Local file path — bridge already downloaded the image
                    cached_urls.append(url)
                    media_types.append("image/jpeg")
                    print(f"[{self.name}] Using bridge-cached image: {url}", flush=True)
                elif msg_type == MessageType.VOICE and url.startswith(("http://", "https://")):
                    try:
                        cached_path = await cache_audio_from_url(url, ext=".ogg")
                        cached_urls.append(cached_path)
                        media_types.append("audio/ogg")
                        print(f"[{self.name}] Cached user voice: {cached_path}", flush=True)
                    except Exception as e:
                        print(f"[{self.name}] Failed to cache voice: {e}", flush=True)
                        cached_urls.append(url)
                        media_types.append("audio/ogg")
                elif msg_type == MessageType.VOICE and os.path.isabs(url):
                    # Local file path — bridge already downloaded the audio
                    cached_urls.append(url)
                    media_types.append("audio/ogg")
                    print(f"[{self.name}] Using bridge-cached audio: {url}", flush=True)
                elif msg_type == MessageType.DOCUMENT and os.path.isabs(url):
                    # Local file path — bridge already downloaded the document
                    cached_urls.append(url)
                    ext = Path(url).suffix.lower()
                    mime = SUPPORTED_DOCUMENT_TYPES.get(ext, "application/octet-stream")
                    media_types.append(mime)
                    print(f"[{self.name}] Using bridge-cached document: {url}", flush=True)
                elif msg_type == MessageType.VIDEO and os.path.isabs(url):
                    cached_urls.append(url)
                    media_types.append("video/mp4")
                    print(f"[{self.name}] Using bridge-cached video: {url}", flush=True)
                else:
                    cached_urls.append(url)
                    media_types.append("unknown")

            # For text-readable documents, inject file content directly into
            # the message text so the agent can read it inline.
            # Cap at 100KB to match Telegram/Discord/Slack behaviour.
            body = data.get("body", "")
            if data.get("isGroup"):
                body = self._clean_bot_mention_text(body, data)
            MAX_TEXT_INJECT_BYTES = 100 * 1024
            if msg_type == MessageType.DOCUMENT and cached_urls:
                for doc_path in cached_urls:
                    ext = Path(doc_path).suffix.lower()
                    if ext in (".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".log", ".py", ".js", ".ts", ".html", ".css"):
                        try:
                            file_size = Path(doc_path).stat().st_size
                            if file_size > MAX_TEXT_INJECT_BYTES:
                                print(f"[{self.name}] Skipping text injection for {doc_path} ({file_size} bytes > {MAX_TEXT_INJECT_BYTES})", flush=True)
                                continue
                            content = Path(doc_path).read_text(errors="replace")
                            fname = Path(doc_path).name
                            # Remove the doc_<hex>_ prefix for display
                            display_name = fname
                            if "_" in fname:
                                parts = fname.split("_", 2)
                                if len(parts) >= 3:
                                    display_name = parts[2]
                            injection = f"[Content of {display_name}]:\n{content}"
                            if body:
                                body = f"{injection}\n\n{body}"
                            else:
                                body = injection
                            print(f"[{self.name}] Injected text content from: {doc_path}", flush=True)
                        except Exception as e:
                            print(f"[{self.name}] Failed to read document text: {e}", flush=True)

            return MessageEvent(
                text=body,
                message_type=msg_type,
                source=source,
                raw_message=data,
                message_id=data.get("messageId"),
                media_urls=cached_urls,
                media_types=media_types,
            )
        except Exception as e:
            print(f"[{self.name}] Error building event: {e}")
            return None
