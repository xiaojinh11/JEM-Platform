"""Simulated adapter used in offline demo mode."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable

from app.adapters.base import Adapter, AdapterError, ConnectionHandle, InteractiveShell
from app.adapters.registry import register
from app.core.enums import AdapterCapability, CompatibilityLevel, Vendor


@dataclass
class _SimulatedShell(InteractiveShell):
    handle: ConnectionHandle
    _buffer: list[str] = field(default_factory=list)
    _closed: bool = False
    cols: int = 120
    rows: int = 30

    def read(self, size: int = 4096, timeout: float = 0.1) -> bytes:
        if self._closed or not self._buffer:
            return b""
        msg = self._buffer.pop(0)
        return msg.encode("utf-8", errors="replace")

    def write(self, data: bytes) -> int:
        text = data.decode("utf-8", errors="replace")
        for line in text.splitlines():
            cmd = line.strip()
            if not cmd:
                continue
            self._buffer.append(self._respond(cmd))
        return len(data)

    def resize(self, cols: int, rows: int) -> None:
        self.cols = cols
        self.rows = rows

    def close(self) -> None:
        self._closed = True

    def _respond(self, cmd: str) -> str:
        banner = f"<HUAWEI> {cmd}\r\n" if self.handle.vendor == Vendor.HUAWEI_VRP else f"<SIMULATED> {cmd}\r\n"
        banner += f"% Unknown command or incomplete: '{cmd}'\r\n"
        return banner


@register(
    vendor=Vendor.SIMULATED,
    capabilities=(
        AdapterCapability.IDENTIFY,
        AdapterCapability.COLLECT_CONFIG,
        AdapterCapability.APPLY_CONFIG,
        AdapterCapability.BACKUP_CONFIG,
        AdapterCapability.INTERACTIVE_SHELL,
    ),
    level=CompatibilityLevel.L0_DEMO,
)
class SimulatedAdapter(Adapter):
    """Returns canned responses; used for offline demos and CI."""

    def identify(self, handle: ConnectionHandle) -> dict:
        time.sleep(0.05)
        return {
            "vendor": Vendor.SIMULATED.value,
            "hostname": handle.meta.get("hostname", "simulated-device"),
            "software_version": "simulated-9.9",
            "model": "simulated",
        }

    def collect_config(self, handle: ConnectionHandle) -> str:
        return (
            "!\nsysname simulated-device\n"
            "vlan 10\n description management\n"
            "interface Vlanif1\n ip address 10.0.0.1 255.255.255.0\n"
            "return\n"
        )

    def apply_config(self, handle: ConnectionHandle, commands: Iterable[str]) -> str:
        log = []
        for cmd in commands:
            log.append(f"apply: {cmd}")
        return "\n".join(log)

    def backup_config(self, handle: ConnectionHandle) -> str:
        return self.collect_config(handle)

    def interactive_session(self, handle: ConnectionHandle) -> InteractiveShell:
        return _SimulatedShell(handle=handle)
