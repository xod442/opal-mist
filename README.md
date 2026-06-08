# Opal

**Operational Priority and At-Risk Likelihood**

A customer escalation risk tracking dashboard for HPE Networking. Account managers and sales engineers submit weekly status via Microsoft Forms; a CSV export is ingested into Opal to keep the team aligned on which customers need attention.

---

## Features

- **Heat-coded dashboard** — Critical, Hot, Concerned, Stable with sortable, filterable table
- **Metric cards** — clickable counts for Total, Critical, Hot, Concerned, Stable, and At Risk
- **Detail & edit pages** — full customer record with editable fields and notes
- **Support page** — focused engagement tracking per customer (state, category, sponsors, status, next actions, get well plan link)
- **Engagement tracker** — table view of all customers with engagement fields and direct edit links
- **Executive overview** — at-a-glance summary for leadership and QBRs
- **Stale records** — top 20 customers longest without an update
- **Weekly CSV ingest** — upload Microsoft Forms exports; duplicates and Mist rows filtered automatically
- **Secure login** — bcrypt passwords, signed session cookies, forced password change on first login
- **Self-registration** — new users can create their own account from the login page (hpe.com email required; user level only; admins can promote)
- **User management** — create users, bulk import via CSV, enable/disable accounts, reset passwords (admin only)
- **Trend tracking** — week-over-week heat movement with dashboard indicators and a dedicated trends page
- **Email alerts** — automatic email notification when a new Critical customer is ingested
- **Audit trail** — every database change logged with the user who made it
- **Auto-backup** — database backed up at 6 AM and 6 PM daily, last 20 backups retained; optional secondary backup location configurable from the Admin UI
- **Admin tools** — manual backup, CSV upload, export, restore, delete database (auto-recreates fresh DB and redirects to login)

---

## Requirements

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (includes Docker Compose v2)
- Available port 9090

No Python or other dependencies needed on the host.

---

## Quick Start

```bash
git clone https://github.com/xod442/opal.git
cd opal
docker compose up -d --build
```

Open **http://localhost:9090** in your browser.

**Default credentials:** `admin` / `admin`
You will be required to change the password on first login.

---

## Docker Hub Deployment

Use this method to deploy Opal on a VM or any host without cloning the repo.

### Publishing the image (one time)

```bash
docker login
docker build -t xod442/opal:latest .
docker push xod442/opal:latest
```

### Deploying on a clean host

If Docker is not installed:
```bash
curl -fsSL https://get.docker.com | sh
```

Then:
```bash
mkdir -p data/backups
docker run -d \
  --name opal \
  -p 9090:9090 \
  -v $(pwd)/data:/data \
  -e SECRET_KEY=your-secret-here \
  xod442/opal:latest
```

Generate a strong secret key with:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Open **http://\<host-ip\>:9090** and log in with `admin` / `admin`. You will be prompted to set a new password.

> Keep the `SECRET_KEY` value the same every time you start the container — changing it invalidates all active sessions.

---

## Clean Install

These steps walk through a fresh deployment from scratch.

### 1. Prerequisites
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running
- Port **9090** available on the host

### 2. Clone and start

```bash
git clone https://github.com/xod442/opal.git
cd opal
docker compose up -d --build
```

The container will:
- Pull the Python 3.12 base image and install dependencies
- Create `./data/opal.db` with all tables on first startup
- Start the web server on port 9090

### 3. First login

1. Open **http://localhost:9090**
2. Log in with `admin` / `admin`
3. You will be redirected to a forced password change — set a strong password and continue
4. The dashboard will load (empty until a CSV is ingested)

### 4. Ingest your first CSV

Upload a Microsoft Forms export via **Admin → Upload CSV**, or use the command line:

```bash
mkdir -p csv
cp ~/Downloads/engagement.csv csv/
docker compose run --rm ingest
```

### 5. Verify everything is working

| Check | Expected |
|---|---|
| `docker compose ps` | `opal` container status **Up** |
| `docker compose logs` | No errors, `Application startup complete` |
| http://localhost:9090/login | Login page loads |
| Login with new password | Redirects to dashboard |
| Admin page | CSV upload, user management, email settings visible |

### Forgot the admin password

Run this command from your host — it resets the admin password to `admin` and forces a password change on next login:

```bash
docker exec opal_opal_1 python3 -c "
from passlib.context import CryptContext
import sqlite3
pwd_ctx = CryptContext(schemes=['bcrypt'], deprecated='auto')
conn = sqlite3.connect('/data/opal.db')
conn.execute(\"UPDATE users SET password_hash=?, must_change_password=1 WHERE username='admin'\", (pwd_ctx.hash('admin'),))
conn.commit()
conn.close()
print('Done')
"
```

> **Important:** Always run this inside the container via `docker exec`. Running it with the host Python will fail due to a bcrypt version mismatch.

Log in with `admin` / `admin` and you will be prompted to set a new password immediately.

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

Both options recreate all tables and the default `admin` account automatically.

---

## Ingesting CSV Data

### Via the Admin UI (recommended)
1. Log in as an admin and click **Admin** in the header
2. Under **Upload CSV**, select the Microsoft Forms export file
3. Click **Upload & ingest**

### Via command line
```bash
mkdir -p csv
cp ~/Downloads/engagement.csv csv/
docker compose run --rm ingest
```

**Ingest rules:**
- Rows are deduplicated on the Microsoft Forms `ID` column — re-ingesting the same file is safe
- Rows where the *Current deployed Architecture* field starts with `Mist` are skipped
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

**CSV format:**
```csv
username,email,password,role
jdoe,jdoe@example.com,TempPass1!,user
bsmith,bsmith@example.com,TempPass2!,admin
```

- `role` defaults to `user` if blank or omitted
- Re-uploading the same file is safe — duplicate usernames are skipped

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
| `DB_PATH` | `/data/opal.db` | Path to the SQLite database inside the container |
| `BACKUP_DIR` | `/data/backups` | Directory for backup files |
| `SECRET_KEY` | `opal-change-me-in-production` | HMAC key for session cookies — **change this in production** |

Data is persisted in `./data/` on the host filesystem and is unaffected by container restarts.

---

## Project Structure

```
opal/
├── app.py                  # FastAPI application
├── ingest.py               # Standalone CSV ingester
├── requirements.txt        # Python dependencies
├── Dockerfile
├── docker-compose.yaml
├── admin-guide.html        # Full administrator guide (open in browser)
├── example_users.csv       # Template for bulk user import
├── voiceover-script.md     # Voiceover script for intro video
└── templates/
    ├── dashboard.html
    ├── detail.html
    ├── edit.html
    ├── executive.html
    ├── stale.html
    ├── admin.html
    ├── audit.html
    ├── login.html
    └── change_password.html
```

---

## Administrator Guide

A full administrator guide covering installation, user management, backup/restore, data model, environment variables, and troubleshooting is included as a self-contained HTML file:

```
open admin-guide.html
```

---

## Roadmap

- [x] Heat-coded dashboard with metric cards
- [x] Weekly CSV ingest (dedup + Mist filter)
- [x] Detail and edit pages
- [x] Executive overview
- [x] Stale records page
- [x] Additional notes field
- [x] Secure login and user management
- [x] Audit trail
- [x] Email alerts for new Critical customers
- [x] Week-over-week trend tracking
- [x] Bulk user import via CSV
- [x] Support page with engagement tracking fields
- [x] Engagement tracker table with direct edit links

---

## License

Internal use only — HPE Networking.
