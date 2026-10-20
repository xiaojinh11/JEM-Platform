# Development Scripts

This folder contains developer-only debug / diagnostic scripts.
They are **not** part of the runtime — they were either:

* Used during the initial bring-up of this project (to probe a running server)
* Useful when a developer needs to verify a build / front-end asset

## ⚠️  Do not run with production credentials

Some scripts (e.g. `_api_test.py`, `_check_login.py`) contain **hard-coded
dev-only credentials** (`admin / admin1234`).  They are kept here only so
existing maintainers can re-run them locally; **never** point them at a
production server.

For real smoke tests, write proper integration tests under
`backend/tests/` and use the dev seed account from your local `.env`.
