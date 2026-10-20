"""Ruijie RGOS adapter skeleton."""
from __future__ import annotations

from typing import Iterable

from app.adapters.base import Adapter, AdapterError, ConnectionHandle, InteractiveShell
from app.adapters.registry import register
from app.core.enums import AdapterCapability, CompatibilityLevel, Vendor


@register(
    vendor=Vendor.RUIJIE_RGOS,
    capabilities=(
        AdapterCapability.IDENTIFY,
        AdapterCapability.COLLECT_CONFIG,
        AdapterCapability.APPLY_CONFIG,
        AdapterCapability.BACKUP_CONFIG,
        AdapterCapability.INTERACTIVE_SHELL,
    ),
    level=CompatibilityLevel.L2_REPEATABLE,
)
class RuijieRGOSAdapter(Adapter):
    def identify(self, handle: ConnectionHandle) -> dict:
        raise AdapterError("see app.services.executor.identify")

    def collect_config(self, handle: ConnectionHandle) -> str:
        raise AdapterError("see app.services.executor.collect_config")

    def apply_config(self, handle: ConnectionHandle, commands: Iterable[str]) -> str:
        raise AdapterError("see app.services.executor.apply_config")

    def backup_config(self, handle: ConnectionHandle) -> str:
        raise AdapterError("see app.services.executor.collect_config")

    def interactive_session(self, handle: ConnectionHandle) -> InteractiveShell:
        raise AdapterError("see app.services.executor.interactive_session")
