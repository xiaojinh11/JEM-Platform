"""Adapter registry.

Adapters are registered via decorator @register(Vendor.X, capabilities=..., level=...)
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import Iterable, Optional

from app.adapters.base import Adapter
from app.core.enums import AdapterCapability, CompatibilityLevel, Vendor
from app.core.logging import logger


_REGISTRY: dict[Vendor, Adapter] = {}


def register(vendor: Vendor, capabilities: Iterable[AdapterCapability], level: CompatibilityLevel):
    def _decorator(cls: type[Adapter]) -> type[Adapter]:
        if not hasattr(cls, "vendor") or cls.vendor != vendor:
            cls.vendor = vendor
        cls.declared_capabilities = frozenset(capabilities)
        cls.default_compatibility = level
        instance = cls()
        _REGISTRY[vendor] = instance
        logger.info("Adapter registered: vendor={} caps={} level={}", vendor.value, [c.value for c in capabilities], level.value)
        return cls

    return _decorator


def get_adapter(vendor: Vendor) -> Optional[Adapter]:
    return _REGISTRY.get(vendor)


def all_adapters() -> dict[Vendor, Adapter]:
    return dict(_REGISTRY)


def autodiscover() -> None:
    """Import every module in app.adapters so @register decorators run."""
    import app.adapters as pkg
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"app.adapters.{mod_info.name}")
