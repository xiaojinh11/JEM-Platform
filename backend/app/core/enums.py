"""Common enums used across the application."""
from __future__ import annotations

from enum import Enum


class Vendor(str, Enum):
    HUAWEI_VRP = "huawei_vrp"
    CISCO_IOS_XE = "cisco_ios_xe"
    RUIJIE_RGOS = "ruijie_rgos"
    SIMULATED = "simulated"
    UNKNOWN = "unknown"


class DeviceRole(str, Enum):
    ROUTER = "router"
    SWITCH = "switch"
    FIREWALL = "firewall"
    AC = "ac"
    AP = "ap"
    OLT = "olt"
    ONU = "onu"
    OTHER = "other"


class EndpointKind(str, Enum):
    INBAND_SSH = "inband_ssh"
    MGMT_SSH = "mgmt_ssh"
    CONSOLE = "console"
    RESTCONF = "restconf"
    NETCONF = "netconf"


class JobStatus(str, Enum):
    DRAFT = "draft"
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"
    CANCELED = "canceled"
    NEEDS_VERIFICATION = "needs_verification"


class JobStepKind(str, Enum):
    PRECHECK = "precheck"
    BACKUP_BEFORE = "backup_before"
    EXECUTE = "execute"
    VERIFY = "verify"
    BACKUP_AFTER = "backup_after"
    ROLLBACK = "rollback"


class AdapterCapability(str, Enum):
    IDENTIFY = "identify"
    COLLECT_CONFIG = "collect_config"
    APPLY_CONFIG = "apply_config"
    BACKUP_CONFIG = "backup_config"
    INTERACTIVE_SHELL = "interactive_shell"


class CompatibilityLevel(int, Enum):
    L0_DEMO = 0
    L1_PARTIAL = 1
    L2_REPEATABLE = 2
    L3_AUDITED = 3
    L4_PRODUCTION = 4
