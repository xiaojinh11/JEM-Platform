#!/usr/bin/env python3
"""Generate secure SECRET_KEY and FERNET_KEY for production deployment."""
import secrets
from cryptography.fernet import Fernet

if __name__ == "__main__":
    secret_key = secrets.token_hex(32)
    fernet_key = Fernet.generate_key().decode()
    redis_password = secrets.token_urlsafe(24)

    print("# ============================================================")
    print("# NAW Production Keys — Add to backend/.env")
    print("# ============================================================")
    print(f"SECRET_KEY={secret_key}")
    print(f"FERNET_KEY={fernet_key}")
    print(f"REDIS_PASSWORD={redis_password}")
    print("\n# Also add REDIS_PASSWORD to docker-compose.yml or export as env var")
