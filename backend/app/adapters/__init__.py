"""Adapter package.

Adapters wrap SSH / RESTCONF / NETCONF connections for a specific vendor and
declare which capabilities are implemented and at which compatibility level.
"""
from app.adapters.base import Adapter, AdapterError, ConnectionHandle  # noqa: F401
from app.adapters.registry import all_adapters, autodiscover, get_adapter  # noqa: F401
from app.adapters.simulated import SimulatedAdapter  # noqa: F401
from app.adapters.huawei_vrp import HuaweiVRPAdapter  # noqa: F401
from app.adapters.cisco_ios_xe import CiscoIOSXEAdapter  # noqa: F401
from app.adapters.ruijie_rgos import RuijieRGOSAdapter  # noqa: F401
