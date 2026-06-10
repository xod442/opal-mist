# Opal-Mist

**Mist Customer Risk Dashboard**

A customer escalation risk tracking dashboard for the HPE Networking Mist team. A Mist-only fork of Opal — imports exclusively Mist architecture records from the Microsoft Forms CSV export. Non-Mist rows are automatically skipped.

---

## Features

- **Heat-coded dashboard** — Critical, Hot, Concerned, Stable with sortable, filterable table
- **Metric cards** — clickable counts for Total, Critical, Hot, Concerned, Stable, and At Risk
- **Detail & edit pages** — full customer record with editable fields and notes
- **Support page** — focused engagement tracking per customer (state, category, sponsors, status, next actions, get well plan link)
- **Engagement tracker** — table view of all customers with engagement fields and direct edit links
- **Executive overview** — at-a-glance summary for leadership and QBRs
- **Stale records** — top 20 customers longest without an update
- **Weekly CSV ingest** — upload Microsoft Forms exports; only Mist architecture rows are imported, duplicates filtered automatically
- **Secure login** — bcrypt passwords, signed session cookies, forced password change on first login
- **Self-registration** — new users can create their own account from the login page (hpe.com email required; user level only; admins can promote)
- **User management** — create users, bulk import via CSV, enable/disable accounts, reset passwords (admin only)
- **Trend tracking** — week-over-week heat movement with dashboard indicators and a dedicated trends page
- **Email alerts** — automatic email notification when a new Critical customer is ingested
- **Audit trail** — every database change logged with the user who made it
- **Auto-backup** — database backed up at 6 AM and 6 PM daily, last 20 backups retained; optional secondary backup location configurable from the Admin UI
- **Admin tools** — manual backup, CSV upload, export, restore, delete database (auto-recreates fresh DB and redirects to login)
- **REST API** — full Swagger UI at `/docs` and ReDoc at `/redoc`; all routes are documented with request/response schemas

---

## Relationship to Opal

Opal-Mist is a direct fork of [Opal](https://github.com/xod442/opal) with one key difference:

| | Opal | Opal-Mist |
|---|---|---|
| Imports | Non-Mist rows | Mist rows only |
| Skips | Mist rows | Non-Mist rows |
| Port | 9090 | 443 |
| Database | `opal.db` | `opal-mist.db` |

Both can run side by side on the same host.

---

## Requirements

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (includes Docker Compose v2)
- Available port **443**

No Python or other dependencies needed on the host.

---

## Quick Start

```bash
git clone https://github.com/xod442/opal-mist.git
cd opal-mist
docker compose up -d --build
```

Open **http://<host-ip>** in your browser (port 443 — no port number needed in the URL).

**Default credentials:** `admin` / `admin`
You will be required to change the password on first login.

---

## Clean Install

### 1. Prerequisites
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running
- Port **443** available on the host

### 2. Clone and start

```bash
git clone https://github.com/xod442/opal-mist.git
cd opal-mist
docker compose up -d --build
```

The container will:
- Pull the Python 3.12 base image and install dependencies
- Create `./data/opal-mist.db` with all tables on first startup
- Start the web server on port 443

### 3. First login

1. Open **http://localhost** (or **http://<host-ip>** on a VM)
2. Log in with `admin` / `admin`
3. You will be redirected to a forced password change — set a strong password and continue
4. The dashboard will load (empty until a CSV is ingested)

### 4. Ingest your first CSV

Upload a Microsoft Forms export via **Admin → Upload CSV**. Only rows where the *Current deployed Architecture* field starts with `Mist` will be imported — all other rows are skipped automatically.

### 5. Verify everything is working

| Check | Expected |
|---|---|
| `docker compose ps` | `opal-mist` container status **Up** |
| `docker compose logs` | No errors, `Application startup complete` |
| http://localhost/login | Login page loads |
| Login with new password | Redirects to dashboard |
| Admin page | CSV upload, user management, email settings visible |

---

### Forgot the admin password

Run this command from your host — it resets the admin password to `admin` and forces a password change on next login:

```bash
docker exec opal-mist-opal-mist-1 python3 -c "
from passlib.context import CryptContext
import sqlite3
pwd_ctx = CryptContext(schemes=['bcrypt'], deprecated='auto')
conn = sqlite3.connect('/data/opal-mist.db')
conn.execute(\"UPDATE users SET password_hash=?, must_change_password=1 WHERE username='admin'\", (pwd_ctx.hash('admin'),))
conn.commit()
conn.close()
print('Done')
"
```

> **Important:** Always run this inside the container via `docker exec`. Running it with the host Python will fail due to a bcrypt version mismatch.

---

### Resetting to factory defaults

**Option 1 — Via the Admin UI** (container stays running):
1. Log in as admin and go to **Admin → Delete Database**
2. Type `DELETE` and confirm
3. You are redirected to the login page — the database is already recreated
4. Log in with `admin` / `admin` and set a new password

**Option 2 — Via the command line** (wipes backups too):
```bash
docker compose down
rm -rf data/
docker compose up -d
```

---

## Ingesting CSV Data

### Via the Admin UI (recommended)
1. Log in as an admin and click **Admin** in the header
2. Under **Upload CSV**, select the Microsoft Forms export file
3. Click **Upload & ingest**

**Ingest rules:**
- Only rows where *Current deployed Architecture* starts with `Mist` are imported
- Rows are deduplicated on the Microsoft Forms `ID` column — re-ingesting the same file is safe
- Microsoft Forms BOM encoding (`utf-8-sig`) is handled automatically

---

## User Management

### Self-registration
Users can create their own account directly from the login page — click **Create an account**, fill in a username, an `@hpe.com` email address, and a password (minimum 8 characters), then submit. Accounts created this way are always user-level. An admin can promote the account to admin via **Admin → User Management** if needed.

### Creating users one at a time
Go to **Admin → User Management**, fill in the username, email, temporary password, and role, then click **Create user**. New users are required to change their password on first login.

### Bulk importing users via CSV
1. Download `example_users.csv` from the **Bulk Import** section of the Admin page (or use the file in the repo)
2. Edit the file with your users — columns are `username`, `email`, `password`, `role`
3. Go to **Admin → User Management → Bulk Import via CSV**, choose the file, and click **Import users**
4. The confirmation banner reports how many were created, skipped (duplicate username), or had errors

---

## Docker Commands

| Command | Description |
|---|---|
| `docker compose up -d` | Start the application |
| `docker compose down` | Stop the application |
| `docker compose up -d --build` | Rebuild and start after code changes |
| `docker compose logs -f` | View live logs |
| `docker compose restart` | Restart the container |

---

## Configuration

Set these in the `environment:` section of `docker-compose.yaml`:

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `/data/opal-mist.db` | Path to the SQLite database inside the container |
| `BACKUP_DIR` | `/data/backups` | Directory for backup files |
| `SECRET_KEY` | `opal-change-me-in-production` | HMAC key for session cookies — **change this in production** |

Data is persisted in `./data/` on the host filesystem and is unaffected by container restarts.

---

## Project Structure

```
opal-mist/
├── app.py                  # FastAPI application
├── ingest.py               # Standalone CSV ingester
├── requirements.txt        # Python dependencies
├── Dockerfile
├── docker-compose.yaml
├── admin-guide.html        # Full administrator guide (open in browser)
├── example_users.csv       # Template for bulk user import
└── templates/
    ├── dashboard.html
    ├── detail.html
    ├── edit.html
    ├── executive.html
    ├── stale.html
    ├── admin.html
    ├── audit.html
    ├── login.html
    ├── register.html
    └── change_password.html
```

---

## API Reference

Opal-Mist is built on **FastAPI**, which automatically generates interactive API documentation from the application's route definitions.

| Interface | URL | Description |
|---|---|---|
| Swagger UI | `http://localhost:443/docs` | Interactive — try requests directly in the browser |
| ReDoc | `http://localhost:443/redoc` | Read-only reference documentation |
| OpenAPI JSON | `http://localhost:443/openapi.json` | Machine-readable schema for tooling integration |

The **API** button on the Admin page opens the Swagger UI in a new tab.

> **Note:** The API uses session-cookie authentication. Browser-based Swagger requests work if you are already logged in. Programmatic access requires passing a valid session cookie obtained from a `POST /login` request.

---

## License

Internal use only — HPE Networking.
