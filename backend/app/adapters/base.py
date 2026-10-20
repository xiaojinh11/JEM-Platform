"""Abstract adapter base class."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable, Optional

from app.core.enums import AdapterCapability, CompatibilityLevel, Vendor


class AdapterError(RuntimeError):
    """Raised when an adapter fails to perform an action."""


@dataclass
class ConnectionHandle:
    """Opaque handle passed back from connect(); vendor-specific implementations."""
    vendor: Vendor
    address: str
    port: int
    meta: dict
    raw: object | None = None  # paramiko.Channel, etc.


class Adapter(ABC):
    vendor: Vendor
    default_compatibility: CompatibilityLevel
    declared_capabilities: frozenset[AdapterCapability]

    def capabilities(self) -> list[AdapterCapability]:
        return list(self.declared_capabilities)

    @abstractmethod
    def identify(self, handle: ConnectionHandle) -> dict:
        """Return identification info: hostname, software version, model."""

    @abstractmethod
    def collect_config(self, handle: ConnectionHandle) -> str:
        """Return full device configuration."""

    @abstractmethod
    def apply_config(self, handle: ConnectionHandle, commands: Iterable[str]) -> str:
        """Apply commands and return session log."""

    @abstractmethod
    def backup_config(self, handle: ConnectionHandle) -> str:
        """Same as collect_config but tagged as a backup."""

    @abstractmethod
    def interactive_session(self, handle: ConnectionHandle) -> "InteractiveShell":
        """Return an object with .read() / .write() / .close() for the proxy."""


class InteractiveShell(ABC):
    """An interactive shell session exposed to the WebSSH proxy."""
    handle: ConnectionHandle

    @abstractmethod
    def read(self, size: int = 4096, timeout: float = 0.1) -> bytes:
        ...

    @abstractmethod
    def write(self, data: bytes) -> int:
        ...

    @abstractmethod
    def resize(self, cols: int, rows: int) -> None:
        ...

    @abstractmethod
    def close(self) -> None:
        ...
