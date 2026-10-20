"""System health and adapter registry endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.adapters.registry import all_adapters, autodiscover
from app.core.enums import AdapterCapability, CompatibilityLevel, Vendor
from app.core.logging import logger
from app.db.session import ping as db_ping

router = APIRouter()


class HealthCheck(BaseModel):
    database: bool
    adapters_loaded: int


class AdapterInfo(BaseModel):
    vendor: str
    compatibility_level: int
    capabilities: list[str]


@router.get("/health", response_model=HealthCheck)
def health(_user=Depends(get_current_user)):
    autodiscover()
    adapters = all_adapters()
    return HealthCheck(database=db_ping(), adapters_loaded=len(adapters))


@router.get("/adapters", response_model=list[AdapterInfo])
def list_adapters(_user=Depends(get_current_user)):
    autodiscover()
    result = []
    for vendor, adapter in all_adapters().items():
        result.append(
            AdapterInfo(
                vendor=vendor.value,
                compatibility_level=int(adapter.default_compatibility),
                capabilities=[c.value for c in adapter.capabilities()],
            )
        )
    return result
