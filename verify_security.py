#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Quick security verification checklist for NAW deployment."""
import os
import sys
from pathlib import Path

# Force UTF-8 output on Windows
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

def check_env_file():
    """Verify .env file exists and contains required keys."""
    env_path = Path("backend/.env")
    if not env_path.exists():
        return False, ".env file not found"

    content = env_path.read_text()

    # Check for weak keys
    weak_patterns = [
        "CHANGE_ME",
        "ReplaceMeWith",
        "UmVwbGFjZS1tZS1iZWZvcmUtcHJvZHVjdGlvbi0zMmJ5dGVzIQ==",
    ]

    for pattern in weak_patterns:
        if pattern in content:
            return False, f"Weak key detected: contains '{pattern[:20]}...'"

    # Check for required keys
    required = ["SECRET_KEY=", "FERNET_KEY=", "REDIS_PASSWORD="]
    missing = [k for k in required if k not in content]

    if missing:
        return False, f"Missing keys: {', '.join(missing)}"

    return True, "✓ All keys configured"

def check_security_files():
    """Verify security enhancement files exist."""
    files = [
        "backend/app/utils/command_validator.py",
        "backend/app/services/template_preview.py",
        "backend/app/api/v1/job_approval.py",
        "backend/alembic/versions/add_job_approval.py",
        "frontend/src/components/RiskWarning.vue",
    ]

    missing = [f for f in files if not Path(f).exists()]

    if missing:
        return False, f"Missing files: {', '.join(missing)}"

    return True, f"✓ {len(files)} security modules deployed"

def main():
    print("=" * 60)
    print("  NAW Security Hardening - Pre-Deployment Verification")
    print("=" * 60)
    print()

    checks = [
        ("Environment Keys", check_env_file),
        ("Security Modules", check_security_files),
    ]

    passed = 0
    failed = 0

    for name, check_func in checks:
        try:
            ok, msg = check_func()
            status = "✓ PASS" if ok else "✗ FAIL"
            print(f"{status:8} | {name:20} | {msg}")
            if ok:
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"✗ ERROR | {name:20} | {str(e)}")
            failed += 1

    print()
    print("=" * 60)
    print(f"Result: {passed} passed, {failed} failed")

    if failed > 0:
        print()
        print("⚠️  Pre-deployment checks FAILED. Run deploy_secure.sh to fix.")
        sys.exit(1)
    else:
        print()
        print("✅ All checks passed. Ready for production deployment.")
        sys.exit(0)

if __name__ == "__main__":
    main()
