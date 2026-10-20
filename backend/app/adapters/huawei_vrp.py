"""Huawei VRP adapter — declarative capabilities (real SSH not bundled)."""
from __future__ import annotations

from typing import Iterable

from app.adapters.base import Adapter, AdapterError, ConnectionHandle, InteractiveShell
from app.adapters.registry import register
from app.core.enums import AdapterCapability, CompatibilityLevel, Vendor


@register(
    vendor=Vendor.HUAWEI_VRP,
    capabilities=(
        AdapterCapability.IDENTIFY,
        AdapterCapability.COLLECT_CONFIG,
        AdapterCapability.APPLY_CONFIG,
        AdapterCapability.BACKUP_CONFIG,
        AdapterCapability.INTERACTIVE_SHELL,
    ),
    level=CompatibilityLevel.L2_REPEATABLE,
)
class HuaweiVRPAdapter(Adapter):
    """Skeleton adapter. Real Paramiko/Netmiko wiring lives in workers."""

    def identify(self, handle: ConnectionHandle) -> dict:
        raise AdapterError(
            "HuaweiVRPAdapter.execute is invoked via worker.run_collect / worker.run_apply; "
            "this method should not be called directly outside of a device session."
        )

    def collect_config(self, handle: ConnectionHandle) -> str:
        raise AdapterError("see app.services.executor.collect_config")

    def apply_config(self, handle: ConnectionHandle, commands: Iterable[str]) -> str:
        raise AdapterError("see app.services.executor.apply_config")

    def backup_config(self, handle: ConnectionHandle) -> str:
        raise AdapterError("see app.services.executor.collect_config")

    def interactive_session(self, handle: ConnectionHandle) -> InteractiveShell:
        raise AdapterError("see app.services.executor.interactive_session")
