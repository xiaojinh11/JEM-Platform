"""Centralised place that re-exports all ORM models.

Importing this module registers models with Base.metadata for Alembic and
create_all fallbacks.
"""
from app.db.session import Base  # noqa: F401 - re-export for Alembic

from app.models.user import User  # noqa: F401
from app.models.device import Device, DeviceEndpoint  # noqa: F401
from app.models.credential import Credential  # noqa: F401
from app.models.config import ConfigArchive, ConfigRevision  # noqa: F401
from app.models.document import Document  # noqa: F401
from app.models.job import Job, JobStep, BackupSnapshot  # noqa: F401
from app.models.audit import AuditEvent  # noqa: F401
from app.models.adapter import AdapterRegistration  # noqa: F401
from app.models.drift import ConfigDrift, DriftBaseline  # noqa: F401
from app.models.ai import AIConfig, AIChatMessage, AIAgentLog  # noqa: F401
from app.models.template import Template  # noqa: F401
from app.models.revoked_token import RevokedToken  # noqa: F401
from app.models.ai_alert import AIAlert  # noqa: F401

__all__ = ["Base"]
