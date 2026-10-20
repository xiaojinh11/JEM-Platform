"""Application configuration loaded from environment variables."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Process-wide settings.

    All secrets must come from environment variables or a local .env file.
    Never commit real credentials.
    """

    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Core
    app_name: str = "Network Automation Workbench"
    env: str = "local"  # local | dev | prod
    debug: bool = True
    api_v1_prefix: str = "/api/v1"

    # Database
    database_url: str = Field(
        default="postgresql+psycopg2://naw:naw@postgres:5432/naw",
        description="SQLAlchemy URL. Use SQLite file for fully offline.",
    )
    database_echo: bool = False
    database_pool_size: int = 20  # Increased from 10 for better concurrency
    database_max_overflow: int = 10  # Allow temporary burst connections

    # Redis / Celery
    redis_url: str = "redis://redis:6379/0"
    celery_broker_url: str = "redis://redis:6379/1"
    celery_result_backend: str = "redis://redis:6379/2"
    # When set to True (via env CELERY_TASK_ALWAYS_EAGER=1), Celery
    # executes tasks synchronously in the calling process. This avoids
    # the 30+ second DNS-retry hang on Windows when the Redis broker
    # is unreachable, and lets the API surface real errors fast.
    celery_task_always_eager: bool = False

    # Drift detection scheduler (in-process fallback for setups without
    # Celery + Beat). Production deployments should use Celery Beat instead.
    drift_scheduler_enabled: bool = True
    drift_scan_interval_seconds: int = 300  # 5 minutes

    # Security
    secret_key: str = Field(
        ...,  # Required, no default
        min_length=32,
        description="HS256 signing key. Generate with: openssl rand -hex 32"
    )
    jwt_algorithm: str = "HS256"
    # Short-lived access token — stolen tokens have a narrow blast radius.
    # Refresh tokens handle long-lived sessions (see /auth/refresh).
    jwt_expire_minutes: int = 15
    refresh_token_expire_days: int = 7
    allowed_origins: str = Field(
        default="http://localhost:5173,http://localhost:8080",
        description="Comma-separated CORS allowlist. Empty = deny all (prod-safe)."
    )

    # Credential encryption (Fernet). 32 url-safe base64 bytes.
    fernet_key: str = Field(
        ...,  # Required, no default
        description="Fernet key (32B base64). Generate with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
    )

    # Storage
    storage_root: Path = ROOT / "storage"
    storage_configs_dir: Path = ROOT / "storage" / "configs"
    storage_documents_dir: Path = ROOT / "storage" / "documents"
    storage_backups_dir: Path = ROOT / "storage" / "backups"
    storage_logs_dir: Path = ROOT / "storage" / "logs"

    # Obsidian integration (local, vault-as-folder strategy).
    # Set OBSIDIAN_VAULT_DIR in .env to point at your vault root.
    # Leave empty to disable mirror writes (feature off by default).
    obsidian_vault_dir: Path = Path("")
    obsidian_enabled: bool = False
    obsidian_category_root: str = "产品手册"

    # Bootstrap
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = "admin"

    # Public URL used to build password-reset links in emails.
    public_base_url: str = "http://localhost:8080"

    # Adapter defaults
    ssh_timeout_seconds: int = 30  # Increased from 10s for cross-region/high-latency scenarios
    ssh_read_timeout_seconds: int = 60  # Independent read timeout for large configs (20000+ lines)
    webssh_idle_timeout_seconds: int = 600

    # ── Concurrent collector (config + health) ───────────────────────
    # Replaces the hard-coded MAX_SSH_CONCURRENCY constant in
    # app/workers/tasks.py. All knobs are env-overridable so operators
    # can tune per deployment (small fleet vs. campus-scale).
    collect_max_concurrency: int = 5        # simultaneous in-flight devices per worker
    collect_per_device_timeout_sec: int = 60 # hard per-device ceiling (connect + collect)
    collect_max_retries: int = 1            # retries on transient SSH/timeout (0 disables)
    collect_retry_backoff_sec: float = 1.5  # exponential backoff base; multiplied by 2**attempt
    collect_command_timeout_sec: int = 30   # per-command read ceiling
    collect_jitter_ms: int = 50             # anti-thundering-herd delay after each device

    # ── AI RAG layer ───────────────────────────────────────────────────
    # HyDE generates a hypothetical manual passage; we BM25-search that
    # passage to lift recall on procedural queries.  Disable if you want
    # to skip the extra LLM call (each HyDE costs ~1s + a few hundred tokens).
    rag_hyde_enabled: bool = True
    rag_hyde_max_tokens: int = 220
    # Refusal gate: below this BM25 score, the tool returns no_recall=True.
    rag_min_score: float = 1.0
    # Dense embedding leg — requires fastembed + a downloadable model.
    # Off by default because the sandbox cannot reach HuggingFace; turn
    # it on in production once the model is cached locally.
    rag_dense_enabled: bool = False
    # Reciprocal Rank Fusion constant (Cormack et al.).
    rag_rrf_k: int = 60

    # ── ReAct reflection layer ────────────────────────────────────────
    react_enabled: bool = True
    react_same_tool_fail_threshold: int = 2
    react_empty_result_threshold: int = 2

    # ── Notification channels (all optional ─ feature disabled when empty) ──
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@localhost"
    smtp_to: str = ""
    webhook_url: str = ""
    dingtalk_webhook_url: str = ""

    # SSRF protection
    ssrf_allowed_extra_ports: str = ""  # comma-separated, e.g. "8080,9443"
    ssrf_block_internal_networks: bool = True  # Set False to allow internal networks (use with caution)

    @property
    def allowed_extra_ports(self) -> set[int]:
        """Parse configured additional allowed ports for SSRF."""
        if not self.ssrf_allowed_extra_ports:
            return set()
        out = set()
        for part in self.ssrf_allowed_extra_ports.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                port = int(part)
                if 1 <= port <= 65535:
                    out.add(port)
            except ValueError:
                continue
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor with mandatory security validation."""
    s = Settings()

    # Security: reject weak or default keys
    _FORBIDDEN = [
        "CHANGE_ME", "ReplaceMeWith", "admin", "test", "demo", "local-dev",
        "UmVwbGFjZS1tZS1iZWZvcmUtcHJvZHVjdGlvbi0zMmJ5dGVzIQ==",  # old default fernet
        "ReplaceMeWithARealFernetKey32BytesBase64",  # .env.example default
        "ReplaceMeWithALongRandomSecretKeyAtLeast32Chars",  # .env.example default
    ]
    for forbidden in _FORBIDDEN:
        if forbidden.lower() in s.secret_key.lower() or forbidden in s.fernet_key:
            raise RuntimeError(
                f"❌ UNSAFE KEY DETECTED: SECRET_KEY or FERNET_KEY contains default/weak value.\n"
                f"Generate secure keys:\n"
                f"  SECRET_KEY:  openssl rand -hex 32\n"
                f"  FERNET_KEY:  python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
            )

    # Validate Fernet key format (44 base64 chars ending with '=')
    if len(s.fernet_key) != 44 or not s.fernet_key.endswith("="):
        raise RuntimeError("❌ FERNET_KEY must be 44-char base64 (32 bytes). Generate with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'")

    for p in (s.storage_root, s.storage_configs_dir, s.storage_documents_dir, s.storage_backups_dir, s.storage_logs_dir):
        p.mkdir(parents=True, exist_ok=True)
    return s


settings = get_settings()
