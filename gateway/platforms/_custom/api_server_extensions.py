"""Custom API server extension mounts for SPEC-115 Hermes v0.12 migration.

This module keeps local Hermes routers outside the upstream v0.12
``gateway.platforms.api_server`` file. It is loaded through
``custom_extensions`` in the staging ``config.yaml``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class CustomAPIServerExtensions:
    """Mount local Hermes subapps on the v0.12 aiohttp API server."""

    def register(self, app: Any, adapter: Any) -> None:
        mounts = [
            ("alerts", self._mount_alerts),
            ("asaas", self._mount_asaas),
            ("media_dispatch", self._mount_media_dispatch),
            ("groups", self._mount_groups),
            ("trafego", self._mount_trafego),
            ("mentor_hub", self._mount_mentor_hub),
            ("kanban", self._mount_kanban),
            ("thumbnail", self._mount_thumbnail),
            ("eventos", self._mount_eventos),
            ("postador_ads", self._mount_postador_ads),
            ("relatorios", self._mount_relatorios),
            ("feedback", self._mount_feedback),
            ("supervisor", self._mount_supervisor),
        ]
        for name, mount in mounts:
            try:
                mount(app, adapter)
                logger.info("[custom_extensions] mounted %s", name)
            except Exception as exc:
                logger.exception("[custom_extensions] failed to mount %s: %s", name, exc)
                raise

    @staticmethod
    def _mount_alerts(app: Any, adapter: Any) -> None:
        from gateway.platforms._custom.alerts_router import mount_alerts_subapp

        mount_alerts_subapp(app, adapter)

    @staticmethod
    def _mount_asaas(app: Any, adapter: Any) -> None:
        from gateway.platforms._custom.asaas_router import mount_asaas_subapps

        mount_asaas_subapps(app, adapter)

    @staticmethod
    def _mount_media_dispatch(app: Any, adapter: Any) -> None:
        from gateway.platforms._custom.media_dispatch_router import mount_media_dispatch_subapp

        mount_media_dispatch_subapp(app, adapter)

    @staticmethod
    def _mount_groups(app: Any, adapter: Any) -> None:
        from gateway.platforms._custom.groups_router import mount_groups_subapp

        mount_groups_subapp(app, adapter)

    @staticmethod
    def _mount_trafego(app: Any, adapter: Any) -> None:
        from gateway.platforms._custom.trafego_router import mount_trafego_subapp

        mount_trafego_subapp(app, adapter)

    @staticmethod
    def _mount_mentor_hub(app: Any, adapter: Any) -> None:
        from gateway.platforms._custom.mentor_hub_router import mount_mentor_hub_subapp

        mount_mentor_hub_subapp(app, adapter)

    @staticmethod
    def _mount_kanban(app: Any, adapter: Any) -> None:
        from gateway.platforms._custom.kanban_router import mount_kanban_subapp

        mount_kanban_subapp(app, adapter)

    @staticmethod
    def _mount_thumbnail(app: Any, adapter: Any) -> None:
        from gateway.platforms._custom.thumbnail_router import mount_thumbnail_subapp

        mount_thumbnail_subapp(app, adapter)

    @staticmethod
    def _mount_eventos(app: Any, adapter: Any) -> None:
        from gateway.platforms._custom.eventos_router import mount_eventos_subapp

        mount_eventos_subapp(app, adapter)

    @staticmethod
    def _mount_postador_ads(app: Any, adapter: Any) -> None:
        from gateway.platforms._custom.postador_ads_router import mount_postador_ads_subapp

        mount_postador_ads_subapp(app, adapter)

    @staticmethod
    def _mount_relatorios(app: Any, adapter: Any) -> None:
        from gateway.platforms._custom.relatorios_router import mount_relatorios_subapp

        mount_relatorios_subapp(app, adapter)

    @staticmethod
    def _mount_feedback(app: Any, adapter: Any) -> None:
        from gateway.platforms._custom.feedback_router import mount_feedback_subapp

        mount_feedback_subapp(app, adapter)

    @staticmethod
    def _mount_supervisor(app: Any, adapter: Any) -> None:
        from gateway.platforms._custom.supervisor_router import mount_supervisor_subapp

        mount_supervisor_subapp(app, adapter)
