"""Runtime loader for config-driven Hermes custom API extensions.

The upstream v0.12 API server has no first-class ``custom_extensions`` hook.
To avoid patching ``gateway/platforms/api_server.py`` directly, this loader
wraps ``aiohttp.web.AppRunner.setup`` and mounts configured extensions just
before aiohttp freezes the router.
"""

from __future__ import annotations

import importlib
import logging
import os
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)
_INSTALLED = False


def _load_config() -> dict[str, Any]:
    try:
        import yaml
        from hermes_constants import get_hermes_home

        path = Path(get_hermes_home()) / "config.yaml"
        if path.exists():
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("[custom_extensions] could not load config.yaml: %s", exc)
    return {}


def _extension_specs(cfg: dict[str, Any]) -> list[str]:
    specs = cfg.get("custom_extensions")
    if specs is None:
        specs = (((cfg.get("platforms") or {}).get("api_server") or {}).get("extra") or {}).get("custom_extensions")
    if isinstance(specs, str):
        return [specs]
    if isinstance(specs, Iterable):
        return [str(item).strip() for item in specs if str(item).strip()]
    return []


def _resolve(spec: str) -> Any:
    module_name, sep, attr = spec.partition(":")
    if not sep:
        module_name, _, attr = spec.rpartition(".")
    if not module_name or not attr:
        raise ValueError(f"invalid custom extension spec: {spec!r}")
    module = importlib.import_module(module_name)
    target = getattr(module, attr)
    return target() if isinstance(target, type) else target


def _register_extensions(app: Any) -> None:
    if app.get("_hermes_custom_extensions_loaded"):
        return
    adapter = app.get("api_server_adapter")
    if adapter is None:
        return
    cfg = _load_config()
    specs = _extension_specs(cfg)
    if not specs:
        return
    for spec in specs:
        extension = _resolve(spec)
        if hasattr(extension, "register"):
            extension.register(app, adapter)
        else:
            extension(app, adapter)
    app["_hermes_custom_extensions_loaded"] = True
    logger.info("[custom_extensions] loaded %d extension(s)", len(specs))


def install() -> None:
    global _INSTALLED
    if _INSTALLED or os.getenv("HERMES_CUSTOM_EXTENSIONS_AUTOLOAD", "1").lower() in {"0", "false", "no"}:
        return
    try:
        from aiohttp import web
    except Exception as exc:
        logger.debug("[custom_extensions] aiohttp unavailable: %s", exc)
        return

    original_setup = web.AppRunner.setup

    async def setup_with_custom_extensions(self, *args, **kwargs):
        app = getattr(self, "app", None) or getattr(self, "_app", None)
        if app is not None:
            _register_extensions(app)
        return await original_setup(self, *args, **kwargs)

    web.AppRunner.setup = setup_with_custom_extensions
    _INSTALLED = True
