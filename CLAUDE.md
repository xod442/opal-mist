# Opal (opal-mist) — working notes for Claude

Twin of `../opal` (Opal-Central). Same codebase, deployed behind HPE edge at
`theedge.ext.hpe.com/opal-mist`. Distinct cookie name and `ROOT_PATH=/opal-mist`.

## TWIN REPO
`opal` and `opal-mist` are parallel apps with near-identical code. **Any feature/fix in
`../opal` should also be applied here** and vice-versa. Nearby lines differ between the two,
so prefer direct `Edit` over `git apply` (patches have failed on this copy before). Watch
for duplicate matches (e.g. the create-customer form shares param tails with `edit_save`).

## Stack & layout
- FastAPI (`app.py`, single file) + Jinja2 (`templates/`) + SQLite.
- Auth: passlib bcrypt (`bcrypt==4.0.1`), itsdangerous signed session cookie.
- Fernet-encrypted secrets at rest; key from env (`OPAL_FERNET_KEY`) or `data/secret.key`.
- Prod: python:3.12-slim in Docker.

## Local dev / verify
- Shares the `../opal/venv` (Python 3.9) for quick checks — use `from __future__ import annotations`.
- Cheap verification (default): `python -m py_compile app.py` + Jinja parse; TestClient for logic. **Screenshots only when asked.**

## Git
- Remote `origin` → github.com/xod442/opal-mist (branch `main`). **PUBLIC repo.**
- Commit only when Rick asks. Author `Rick Kauffman <rick@rickkauffman.com>`, trailer
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Never stage** `*.db`, `*.db.bak*`, `.env`, `data/`, `secret.key`. Prefer explicit `git add <files>` over `git add -A`.

## Gotchas
- Login breaks on both twins at once, no deploy → suspect the HPE **edge/session policy** first.
