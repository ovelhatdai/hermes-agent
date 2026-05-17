"""Evolution API wrapper for group participant operations."""

from __future__ import annotations

import json
import os
from typing import Any

import aiohttp


class EvolutionGroupsClient:
    """Small async wrapper around Evolution group endpoints."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        instance: str | None = None,
        timeout_seconds: int = 30,
    ) -> None:
        self.base_url = (base_url or os.getenv("EVOLUTION_API_URL") or "http://127.0.0.1:3100").rstrip("/")
        self.api_key = (api_key or os.getenv("EVOLUTION_API_KEY") or "").strip()
        self.instance = (instance or os.getenv("EVOLUTION_INSTANCE_GROUPS") or "group-broadcaster").strip()
        self.timeout_seconds = max(5, int(timeout_seconds))

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["apikey"] = self.api_key
        return headers

    async def fetch_all_groups(self, *, get_participants: bool = True) -> tuple[int, dict[str, Any]]:
        """Fetch all groups from Evolution API."""
        query = "true" if get_participants else "false"
        url = f"{self.base_url}/group/fetchAllGroups/{self.instance}?getParticipants={query}"
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=self._headers()) as response:
                text = await response.text()
                try:
                    parsed = json.loads(text) if text else {}
                except Exception:
                    parsed = {"raw": text[:1000]}
                if isinstance(parsed, list):
                    return response.status, {"groups": parsed}
                if isinstance(parsed, dict):
                    return response.status, parsed
                return response.status, {"raw": parsed}

    async def remove_participant(
        self,
        *,
        group_jid: str,
        participant_phone: str,
    ) -> tuple[int, dict[str, Any]]:
        """
        Remove one participant from one group.

        Evolution endpoint accepts ``action=remove`` plus group and participants.
        """
        url = f"{self.base_url}/group/updateParticipant/{self.instance}"
        payload = {
            "groupJid": group_jid,
            "action": "remove",
            "participants": [participant_phone],
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=self._headers(), json=payload) as response:
                text = await response.text()
                try:
                    parsed = json.loads(text) if text else {}
                except Exception:
                    parsed = {"raw": text[:1000]}
                if isinstance(parsed, dict):
                    return response.status, parsed
                return response.status, {"raw": parsed}

