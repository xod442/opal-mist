"""
Opal — Operational Priority and At-Risk Likelihood
"""

import csv
import io
import os
import secrets
import shutil
import smtplib
import sqlite3
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from urllib.parse import quote_plus

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Form, Query, Request, UploadFile, File, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from passlib.context import CryptContext

# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH    = os.getenv("DB_PATH",    os.path.join(os.path.dirname(__file__), "opal.db"))
BACKUP_DIR = os.getenv("BACKUP_DIR", os.path.join(os.path.dirname(__file__), "data", "backups"))
SECRET_KEY = os.getenv("SECRET_KEY", "opal-change-me-in-production")
ROOT_PATH  = os.getenv("ROOT_PATH", "")
SESSION_MAX_AGE = 8 * 3600  # 8 hours

os.makedirs(BACKUP_DIR, exist_ok=True)

app = FastAPI(title="Opal-Mist", root_path=ROOT_PATH)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
templates.env.globals["rp"] = ROOT_PATH

pwd_ctx    = CryptContext(schemes=["bcrypt"], deprecated="auto")
serializer = URLSafeTimedSerializer(SECRET_KEY)

TEMP_ORDER = {
    "Critical - We are at risk of losing them as a customer": 1,
    "Critical - We are at risk of loosing them as a customer": 1,
    "Hot - they are escalating": 2,
    "Concerned - they are complaining": 3,
    "Stable - but needs attention": 4,
    "Happy - customer is satisfied": 5,
}
TEMP_LABEL = {
    "Critical - We are at risk of losing them as a customer": "Critical",
    "Critical - We are at risk of loosing them as a customer": "Critical",
    "Hot - they are escalating": "Hot",
    "Concerned - they are complaining": "Concerned",
    "Stable - but needs attention": "Stable",
    "Happy - customer is satisfied": "Happy",
}
ARCH_COL = "Current deployed Architecture - Not what they want to get to, but what are they running now"


# ── Auth helpers ──────────────────────────────────────────────────────────────

def get_session(request: Request):
    token = request.cookies.get("session")
    if not token:
        return None
    try:
        return serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def require_user(request: Request):
    session = get_session(request)
    if not session:
        return None
    return session


def require_admin(request: Request):
    session = get_session(request)
    if not session or session.get("role") != "admin":
        return None
    return session


def set_session_cookie(response, user_id, username, role):
    token = serializer.dumps({"user_id": user_id, "username": username, "role": role})
    response.set_cookie("session", token, httponly=True, samesite="lax", max_age=SESSION_MAX_AGE)


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def migrate_db():
    _db_dir = os.path.dirname(DB_PATH)
    if _db_dir:
        os.makedirs(_db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)

    # customers table — create if missing, then apply any column migrations
    conn.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id                  INTEGER PRIMARY KEY,
            submission_time     TEXT,
            email               TEXT,
            submitter_name      TEXT,
            customer_name       TEXT,
            location            TEXT,
            account_manager     TEXT,
            sales_engineer      TEXT,
            temperature         TEXT,
            temperature_label   TEXT,
            temperature_order   INTEGER,
            at_risk             TEXT,
            risk_reasons        TEXT,
            architecture        TEXT,
            near_term_goals     TEXT,
            bu_contact          TEXT,
            ask_from_bu         TEXT,
            background          TEXT,
            last_modified       TEXT,
            notes               TEXT,
            state               TEXT,
            category            TEXT,
            bu_plm_sponsor      TEXT,
            bu_tme_sponsor      TEXT,
            current_status      TEXT,
            next_actions        TEXT,
            get_well_plan       TEXT
        )
    """)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(customers)").fetchall()]
    if "last_modified" not in cols:
        conn.execute("ALTER TABLE customers ADD COLUMN last_modified TEXT")
        conn.execute("UPDATE customers SET last_modified = submission_time WHERE last_modified IS NULL")
    if "notes" not in cols:
        conn.execute("ALTER TABLE customers ADD COLUMN notes TEXT")
    for col in ("state", "category", "bu_plm_sponsor", "bu_tme_sponsor",
                "current_status", "next_actions", "get_well_plan", "custodian"):
        if col not in cols:
            conn.execute(f"ALTER TABLE customers ADD COLUMN {col} TEXT")

    # users table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            username     TEXT UNIQUE NOT NULL,
            email        TEXT,
            password_hash TEXT NOT NULL,
            role         TEXT NOT NULL DEFAULT 'user',
            is_active    INTEGER NOT NULL DEFAULT 1,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT,
            last_login   TEXT
        )
    """)

    # Default admin if no users exist
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count == 0:
        conn.execute("""
            INSERT INTO users (username, email, password_hash, role, is_active, must_change_password, created_at)
            VALUES (?, ?, ?, ?, 1, 1, ?)
        """, ("admin", "", pwd_ctx.hash("admin"), "admin", datetime.now().isoformat()))

    # audit_log table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       TEXT NOT NULL,
            username TEXT NOT NULL,
            action   TEXT NOT NULL,
            target   TEXT,
            detail   TEXT
        )
    """)

    # settings table (key/value store)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )
    """)

    # heat_history table — one row per customer per heat change
    conn.execute("""
        CREATE TABLE IF NOT EXISTS heat_history (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id       INTEGER NOT NULL,
            ts                TEXT NOT NULL,
            temperature_label TEXT NOT NULL,
            temperature_order INTEGER NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_heat_history_customer ON heat_history(customer_id)")

    # Fix records ingested with the old 'loosing' typo before it was aliased
    conn.execute("""
        UPDATE customers
        SET temperature_label = 'Critical', temperature_order = 1
        WHERE temperature LIKE '%loosing%'
          AND temperature_label != 'Critical'
    """)

    conn.commit()
    conn.close()


# ── Audit log ─────────────────────────────────────────────────────────────────

def log_action(username: str, action: str, target: str = "", detail: str = ""):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO audit_log (ts, username, action, target, detail) VALUES (?,?,?,?,?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), username, action, target, detail),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # never let logging crash the app


# ── Heat history ──────────────────────────────────────────────────────────────

def record_heat_snapshot(conn, customer_id: int, label: str, order: int):
    """Record a heat snapshot only when the label has changed from the last entry."""
    last = conn.execute(
        "SELECT temperature_label FROM heat_history WHERE customer_id = ? ORDER BY id DESC LIMIT 1",
        (customer_id,)
    ).fetchone()
    if last and last[0] == label:
        return  # no change — skip
    conn.execute(
        "INSERT INTO heat_history (customer_id, ts, temperature_label, temperature_order) VALUES (?,?,?,?)",
        (customer_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), label, order),
    )


def get_trend_map(conn) -> dict:
    """Return {customer_id: {direction, from_label, from_ts}} for all customers with history."""
    rows = conn.execute("""
        WITH ranked AS (
            SELECT customer_id, temperature_label, temperature_order, ts,
                   ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY id DESC) AS rn
            FROM heat_history
        )
        SELECT c.customer_id,
               c.temperature_label  AS curr_label,
               c.temperature_order  AS curr_order,
               p.temperature_label  AS prev_label,
               p.temperature_order  AS prev_order,
               p.ts                 AS prev_ts
        FROM ranked c
        LEFT JOIN ranked p ON p.customer_id = c.customer_id AND p.rn = 2
        WHERE c.rn = 1
    """).fetchall()

    trend_map = {}
    for r in rows:
        if r["prev_label"] is None:
            direction = "new"
        elif r["curr_order"] < r["prev_order"]:
            direction = "worse"
        elif r["curr_order"] > r["prev_order"]:
            direction = "better"
        else:
            direction = "same"
        trend_map[r["customer_id"]] = {
            "direction":  direction,
            "from_label": r["prev_label"] or "",
            "from_ts":    r["prev_ts"] or "",
            "curr_label": r["curr_label"],
        }
    return trend_map


# ── Settings helpers ──────────────────────────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        conn.close()
        return row[0] if row else default
    except Exception:
        return default


def set_setting(key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))
    conn.commit()
    conn.close()


def get_email_config() -> dict:
    return {
        "enabled":   get_setting("email_alerts_enabled") == "true",
        "host":      get_setting("email_smtp_host"),
        "port":      int(get_setting("email_smtp_port") or "587"),
        "username":  get_setting("email_username"),
        "password":  get_setting("email_password"),
        "from_addr": get_setting("email_from"),
        "to_addr":   get_setting("email_to"),
    }


# ── Email helpers ──────────────────────────────────────────────────────────────

def _make_smtp(host: str, port: int):
    if port == 465:
        return smtplib.SMTP_SSL(host, port, timeout=10)
    return smtplib.SMTP(host, port, timeout=10)


def _smtp_send(cfg: dict, subject: str, body: str, to_addr: str = None):
    to_addr   = to_addr or cfg["to_addr"]
    from_addr = cfg["from_addr"] or cfg["username"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to_addr
    msg.attach(MIMEText(body, "plain"))
    with _make_smtp(cfg["host"], cfg["port"]) as smtp:
        if cfg["port"] != 465:
            smtp.ehlo()
            smtp.starttls()
        smtp.login(cfg["username"], cfg["password"])
        smtp.sendmail(from_addr, to_addr, msg.as_string())


def send_alert(subject: str, body: str):
    """Send an alert email. Silently no-ops if alerts are disabled or misconfigured."""
    try:
        cfg = get_email_config()
        if not cfg["enabled"]:
            return
        if not all([cfg["host"], cfg["username"], cfg["password"], cfg["to_addr"]]):
            return
        _smtp_send(cfg, subject, body)
    except Exception:
        pass  # never let email crash the app


def send_test_email():
    """Send a test email. Raises on failure so the admin sees the error."""
    cfg = get_email_config()
    if not all([cfg["host"], cfg["username"], cfg["password"], cfg["to_addr"]]):
        raise ValueError("Email settings incomplete — fill in SMTP host, username, password, and recipient.")
    _smtp_send(
        cfg,
        "Opal — Test Email",
        "This is a test email from Opal.\n\nYour SMTP settings are working correctly.",
    )


def send_manual_email(to_addr: str, subject: str, body: str):
    """Send a user-directed email to a chosen recipient. Raises on failure so the
    sender sees the error. Unlike send_alert, this is an explicit action — it does
    not require the alerts toggle or the global recipient, only working SMTP creds."""
    cfg = get_email_config()
    if not all([cfg["host"], cfg["username"], cfg["password"]]):
        raise ValueError("Email is not configured — set SMTP host, username, and password on the Admin page first.")
    if not to_addr:
        raise ValueError("No recipient selected.")
    _smtp_send(cfg, subject, body, to_addr=to_addr)


# ── Backup helpers ────────────────────────────────────────────────────────────

def do_backup():
    if not os.path.exists(DB_PATH):
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"opal_backup_{ts}.db"
    dest = os.path.join(BACKUP_DIR, filename)
    shutil.copy2(DB_PATH, dest)
    backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith(".db")], reverse=True)
    for old in backups[20:]:
        os.remove(os.path.join(BACKUP_DIR, old))

    # Copy to secondary backup location if configured
    try:
        secondary = get_setting("backup_dir_2", "").strip()
        if secondary:
            os.makedirs(secondary, exist_ok=True)
            shutil.copy2(DB_PATH, os.path.join(secondary, filename))
    except Exception:
        pass  # never let secondary failure break primary backup

    return dest


def list_backups():
    if not os.path.exists(BACKUP_DIR):
        return []
    files = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith(".db")], reverse=True)
    result = []
    for f in files:
        path = os.path.join(BACKUP_DIR, f)
        size = os.path.getsize(path)
        mtime = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")
        result.append({"name": f, "size": f"{size // 1024} KB", "modified": mtime})
    return result


def ingest_fileobj(fileobj):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY,
            submission_time TEXT, email TEXT, submitter_name TEXT,
            customer_name TEXT, location TEXT, account_manager TEXT,
            sales_engineer TEXT, temperature TEXT, temperature_label TEXT,
            temperature_order INTEGER, at_risk TEXT, risk_reasons TEXT,
            architecture TEXT, near_term_goals TEXT, bu_contact TEXT,
            ask_from_bu TEXT, background TEXT, last_modified TEXT, notes TEXT
        )
    """)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(customers)").fetchall()]
    for col in ("last_modified", "notes", "state", "category", "bu_plm_sponsor",
                "bu_tme_sponsor", "current_status", "next_actions", "get_well_plan", "custodian"):
        if col not in cols:
            conn.execute(f"ALTER TABLE customers ADD COLUMN {col} TEXT")

    inserted = skipped_mist = skipped_dup = 0
    new_critical = []
    reader = csv.DictReader(fileobj)
    for row in reader:
        arch = row.get(ARCH_COL, "").strip()
        if not arch.lower().startswith("mist"):
            skipped_mist += 1
            continue
        temp = row.get("Customer Temperature", "").strip()
        submission_time = row.get("Start time", "").strip()
        customer_name = row.get("Customer Name", "").strip()
        try:
            conn.execute("""
                INSERT OR IGNORE INTO customers (
                    id, submission_time, email, submitter_name,
                    customer_name, location, account_manager, sales_engineer,
                    temperature, temperature_label, temperature_order,
                    at_risk, risk_reasons, architecture,
                    near_term_goals, bu_contact, ask_from_bu, background, last_modified
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                int(row.get("ID", 0)), submission_time,
                row.get("Email", "").strip(), row.get("Name", "").strip(),
                customer_name, row.get("Location", "").strip(),
                row.get("Account Manager", "").strip(), row.get("Sales Engineer", "").strip(),
                temp, TEMP_LABEL.get(temp, temp), TEMP_ORDER.get(temp, 99),
                row.get("Is the customer actively at risk?", "").strip(),
                row.get("Primarily reason for risk", "").strip(), arch,
                row.get("What are the customers near term goals", "").strip(),
                row.get("Are you currently working with anyone in the business unit?", "").strip(),
                row.get("Your specific ask of what you would want from the business unit to make this customer happy? ", "").strip(),
                row.get("Any other background you want us to know", "").strip(),
                submission_time,
            ))
            if conn.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
                record_heat_snapshot(conn, int(row.get("ID", 0)),
                                     TEMP_LABEL.get(temp, temp), TEMP_ORDER.get(temp, 99))
                if TEMP_LABEL.get(temp, "") == "Critical":
                    new_critical.append({
                        "name":    customer_name,
                        "am":      row.get("Account Manager", "").strip(),
                        "se":      row.get("Sales Engineer", "").strip(),
                        "at_risk": row.get("Is the customer actively at risk?", "").strip(),
                        "reasons": row.get("Primarily reason for risk", "").strip(),
                    })
            else:
                skipped_dup += 1
        except Exception:
            skipped_dup += 1
    conn.commit()
    conn.close()
    return inserted, skipped_dup, skipped_mist, new_critical


# ── Startup ───────────────────────────────────────────────────────────────────

migrate_db()
scheduler = BackgroundScheduler()
scheduler.add_job(do_backup, "cron", hour="6,18", minute=0)
scheduler.start()


# ── Login / Logout ────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = "", msg: str = ""):
    if get_session(request):
        return RedirectResponse(url=f"{ROOT_PATH}/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": error, "msg": msg, "motd": get_setting("motd", "")})


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE username = ? AND is_active = 1", (username,)).fetchone()
    if not user or not pwd_ctx.verify(password, user["password_hash"]):
        conn.close()
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Invalid username or password."},
            status_code=401,
        )
    conn.execute("UPDATE users SET last_login = ? WHERE id = ?",
                 (datetime.now().isoformat(), user["id"]))
    conn.commit()
    conn.close()

    redirect_to = f"{ROOT_PATH}/change-password" if user["must_change_password"] else f"{ROOT_PATH}/"
    response = RedirectResponse(url=redirect_to, status_code=303)
    set_session_cookie(response, user["id"], user["username"], user["role"])
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    response.delete_cookie("session")
    return response


# ── Self-registration ─────────────────────────────────────────────────────────

@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, error: str = "", success: str = ""):
    if get_session(request):
        return RedirectResponse(url=f"{ROOT_PATH}/", status_code=303)
    return templates.TemplateResponse(request=request, name="register.html",
                                      context={"error": error, "success": success})


@app.post("/register")
def register_submit(
    request: Request,
    username: str = Form(...),
    email: str = Form(""),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    if get_session(request):
        return RedirectResponse(url=f"{ROOT_PATH}/", status_code=303)
    username = username.strip()
    email = email.strip()
    if not username or not password or not email:
        return templates.TemplateResponse(request=request, name="register.html",
                                          context={"error": "Username, email, and password are required."})
    if not username.lower().endswith("@hpe.com"):
        return templates.TemplateResponse(request=request, name="register.html",
                                          context={"error": "Username must be a valid @hpe.com email address."})
    if not email.lower().endswith("@hpe.com"):
        return templates.TemplateResponse(request=request, name="register.html",
                                          context={"error": "Email must be an @hpe.com address."})
    if password != confirm_password:
        return templates.TemplateResponse(request=request, name="register.html",
                                          context={"error": "Passwords do not match."})
    if len(password) < 8:
        return templates.TemplateResponse(request=request, name="register.html",
                                          context={"error": "Password must be at least 8 characters."})
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO users (username, email, password_hash, role, is_active, created_at)
            VALUES (?, ?, ?, 'user', 1, ?)
        """, (username, email, pwd_ctx.hash(password), datetime.now().isoformat()))
        conn.commit()
        log_action("self-register", "create_user", username, "role=user")
    except sqlite3.IntegrityError:
        conn.close()
        return templates.TemplateResponse(request=request, name="register.html",
                                          context={"error": f"Username '{username}' is already taken."})
    conn.close()
    return RedirectResponse(url=f"{ROOT_PATH}/login?msg=Account+created.+You+can+now+sign+in.", status_code=303)


# ── Change password ───────────────────────────────────────────────────────────

@app.get("/change-password", response_class=HTMLResponse)
def change_password_page(request: Request):
    session = get_session(request)
    if not session:
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    conn.close()
    forced = bool(user and user["must_change_password"])
    return templates.TemplateResponse(
        request=request, name="change_password.html",
        context={"session": session, "forced": forced, "username": session["username"], "error": ""},
    )


@app.post("/change-password")
def change_password_submit(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    session = get_session(request)
    if not session:
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    forced = bool(user and user["must_change_password"])

    def render(error=""):
        return templates.TemplateResponse(
            request=request, name="change_password.html",
            context={"session": session, "forced": forced, "username": session["username"], "error": error},
        )

    if new_password != confirm_password:
        conn.close()
        return render(error="New passwords do not match.")
    if len(new_password) < 8:
        conn.close()
        return render(error="Password must be at least 8 characters.")

    if not forced:
        if not current_password or not pwd_ctx.verify(current_password, user["password_hash"]):
            conn.close()
            return render(error="Current password is incorrect.")

    conn.execute("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
                 (pwd_ctx.hash(new_password), session["user_id"]))
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"{ROOT_PATH}/?msg=Password+changed+successfully", status_code=303)


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    search: str = Query(""),
    filter_temp: str = Query(""),
    filter_risk: str = Query(""),
    filter_am: str = Query(""),
    msg: str = Query(""),
):
    session = get_session(request)
    if not session:
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)

    conn = get_db()
    metrics = {}
    for label in ("Critical", "Hot", "Concerned", "Stable", "Happy"):
        metrics[label] = conn.execute(
            "SELECT COUNT(*) FROM customers WHERE temperature_label = ?", (label,)
        ).fetchone()[0]
    metrics["Total"] = sum(metrics.values())
    metrics["At Risk"] = conn.execute(
        "SELECT COUNT(*) FROM customers WHERE at_risk = 'Yes – actively evaluating other vendors'"
    ).fetchone()[0]

    ams = [r[0] for r in conn.execute(
        "SELECT DISTINCT account_manager FROM customers WHERE account_manager != '' ORDER BY account_manager"
    ).fetchall()]

    where, params = [], []
    if search:
        where.append("""(customer_name LIKE ? OR sales_engineer LIKE ? OR account_manager LIKE ?
            OR location LIKE ? OR bu_plm_sponsor LIKE ? OR bu_tme_sponsor LIKE ?
            OR current_status LIKE ? OR next_actions LIKE ? OR state LIKE ? OR category LIKE ?
            OR custodian LIKE ?)""")
        params += [f"%{search}%"] * 11
    if filter_temp:
        where.append("temperature_label = ?")
        params.append(filter_temp)
    if filter_risk:
        where.append("at_risk = ?")
        params.append(filter_risk)
    if filter_am:
        where.append("account_manager = ?")
        params.append(filter_am)

    sql = "SELECT * FROM customers"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY temperature_order ASC, customer_name ASC"

    customers = conn.execute(sql, params).fetchall()
    trend_map = get_trend_map(conn)
    conn.close()

    return templates.TemplateResponse(
        request=request, name="dashboard.html",
        context={
            "customers": customers, "metrics": metrics, "ams": ams,
            "search": search, "filter_temp": filter_temp,
            "filter_risk": filter_risk, "filter_am": filter_am,
            "session": session, "msg": msg, "trend_map": trend_map,
        },
    )


# ── Detail ────────────────────────────────────────────────────────────────────

@app.get("/customer/{customer_id}", response_class=HTMLResponse)
def detail(request: Request, customer_id: int):
    session = get_session(request)
    if not session:
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    conn = get_db()
    customer = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    users = conn.execute(
        "SELECT username, email FROM users WHERE is_active = 1 AND email IS NOT NULL AND email != '' ORDER BY username"
    ).fetchall()
    conn.close()
    if not customer:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request=request, name="detail.html",
                                      context={"c": customer, "session": session,
                                               "users": users,
                                               "sent": request.query_params.get("sent", "")})


@app.post("/customer/{customer_id}/email")
def email_user(
    request: Request,
    customer_id: int,
    recipient: str = Form(""),
    note: str = Form(""),
):
    session = get_session(request)
    if not session:
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)

    conn = get_db()
    c = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    recipient = recipient.strip()
    # Only allow sending to a known, active user's address — never an arbitrary input.
    valid = conn.execute(
        "SELECT username FROM users WHERE email = ? AND is_active = 1 AND email != ''",
        (recipient,),
    ).fetchone()
    conn.close()

    if not c:
        return HTMLResponse("Not found", status_code=404)
    if not recipient or not valid:
        return RedirectResponse(
            url=f"{ROOT_PATH}/customer/{customer_id}?sent=err:Pick+a+valid+recipient.",
            status_code=303)

    base = str(request.base_url).rstrip("/") + ROOT_PATH
    record_url = f"{base}/customer/{customer_id}"

    subject = f"[Opal] {c['temperature_label']} — {c['customer_name']}"
    lines = [
        f"{session['username']} flagged this customer in Opal-Mist:",
        "",
        f"  Customer:        {c['customer_name'] or '—'}",
        f"  Heat level:      {c['temperature_label'] or '—'}",
        f"  Actively at risk: {c['at_risk'] or '—'}",
        f"  Account Manager: {c['account_manager'] or '—'}",
        f"  Sales Engineer:  {c['sales_engineer'] or '—'}",
        f"  Location:        {c['location'] or '—'}",
        f"  Risk reasons:    {c['risk_reasons'] or 'None specified'}",
    ]
    if note.strip():
        lines += ["", f"Note from {session['username']}:", note.strip()]
    lines += ["", f"View the full record: {record_url}", "", "— Opal-Mist"]
    body = "\n".join(lines)

    try:
        send_manual_email(recipient, subject, body)
    except Exception as e:
        msg = quote_plus(f"err:Send failed — {e}")
        return RedirectResponse(url=f"{ROOT_PATH}/customer/{customer_id}?sent={msg}", status_code=303)

    log_action(session["username"], "email_user", c["customer_name"],
               f"to={valid['username']} <{recipient}>")
    msg = quote_plus(f"Email sent to {valid['username']}.")
    return RedirectResponse(url=f"{ROOT_PATH}/customer/{customer_id}?sent={msg}", status_code=303)


# ── Edit ──────────────────────────────────────────────────────────────────────

@app.get("/customer/{customer_id}/edit", response_class=HTMLResponse)
def edit_form(request: Request, customer_id: int):
    session = get_session(request)
    if not session:
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    conn = get_db()
    customer = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    conn.close()
    if not customer:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(
        request=request, name="edit.html",
        context={"c": customer, "temp_options": list(TEMP_ORDER.keys()), "session": session},
    )


@app.post("/customer/{customer_id}/edit")
def edit_save(
    request: Request,
    customer_id: int,
    customer_name: str = Form(...),
    temperature: str = Form(...),
    at_risk: str = Form(...),
    risk_reasons: str = Form(""),
    architecture: str = Form(""),
    near_term_goals: str = Form(""),
    bu_contact: str = Form(""),
    ask_from_bu: str = Form(""),
    background: str = Form(""),
    notes: str = Form(""),
    state: str = Form(""),
    category: str = Form(""),
    bu_plm_sponsor: str = Form(""),
    bu_tme_sponsor: str = Form(""),
    current_status: str = Form(""),
    next_actions: str = Form(""),
    get_well_plan: str = Form(""),
    custodian: str = Form(""),
):
    session = get_session(request)
    if not session:
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    if temperature not in TEMP_ORDER:
        return HTMLResponse("Invalid temperature value", status_code=400)
    conn = get_db()
    conn.execute("""
        UPDATE customers SET
            customer_name=?, temperature=?, temperature_label=?, temperature_order=?,
            at_risk=?, risk_reasons=?, architecture=?, near_term_goals=?,
            bu_contact=?, ask_from_bu=?, background=?, notes=?, last_modified=?,
            state=?, category=?, bu_plm_sponsor=?, bu_tme_sponsor=?,
            current_status=?, next_actions=?, get_well_plan=?, custodian=?
        WHERE id=?
    """, (
        customer_name, temperature,
        TEMP_LABEL.get(temperature, temperature),
        TEMP_ORDER.get(temperature, 99),
        at_risk, risk_reasons, architecture, near_term_goals,
        bu_contact, ask_from_bu, background, notes,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        state, category, bu_plm_sponsor, bu_tme_sponsor,
        current_status, next_actions, get_well_plan, custodian,
        customer_id,
    ))
    temp_label = TEMP_LABEL.get(temperature, temperature)
    record_heat_snapshot(conn, customer_id, temp_label, TEMP_ORDER.get(temperature, 99))
    conn.commit()
    conn.close()
    log_action(session["username"], "edit_customer", customer_name,
               f"heat={temp_label}, at_risk={at_risk}")
    return RedirectResponse(url=f"{ROOT_PATH}/", status_code=303)


# ── Engagement Tracker ────────────────────────────────────────────────────────

@app.get("/engagement", response_class=HTMLResponse)
def engagement(request: Request):
    session = get_session(request)
    if not session:
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    conn = get_db()
    customers = conn.execute(
        "SELECT * FROM customers ORDER BY temperature_order, customer_name"
    ).fetchall()
    conn.close()
    return templates.TemplateResponse(
        request=request, name="engagement.html",
        context={"customers": customers, "session": session},
    )


# ── Support page ──────────────────────────────────────────────────────────────

@app.get("/customer/{customer_id}/support", response_class=HTMLResponse)
def support_form(request: Request, customer_id: int):
    session = get_session(request)
    if not session:
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    conn = get_db()
    customer = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    conn.close()
    if not customer:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(
        request=request, name="support.html",
        context={"c": customer, "session": session},
    )


@app.post("/customer/{customer_id}/support")
def support_save(
    request: Request,
    customer_id: int,
    sales_engineer: str = Form(""),
    state: str = Form(""),
    category: str = Form(""),
    bu_plm_sponsor: str = Form(""),
    bu_tme_sponsor: str = Form(""),
    current_status: str = Form(""),
    next_actions: str = Form(""),
    get_well_plan: str = Form(""),
    custodian: str = Form(""),
):
    session = get_session(request)
    if not session:
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    conn = get_db()
    customer = conn.execute("SELECT customer_name FROM customers WHERE id = ?", (customer_id,)).fetchone()
    conn.execute("""
        UPDATE customers SET
            sales_engineer=?, state=?, category=?, bu_plm_sponsor=?, bu_tme_sponsor=?,
            current_status=?, next_actions=?, get_well_plan=?, last_modified=?
        WHERE id=?
    """, (sales_engineer, state, category, bu_plm_sponsor, bu_tme_sponsor,
          current_status, next_actions, get_well_plan,
          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
          customer_id))
    conn.commit()
    conn.close()
    if customer:
        log_action(session["username"], "edit_support", customer["customer_name"],
                   f"state={state}, category={category}")
    return RedirectResponse(url=f"{ROOT_PATH}/customer/{customer_id}", status_code=303)


# ── Stale Records ─────────────────────────────────────────────────────────────

@app.get("/stale", response_class=HTMLResponse)
def stale(request: Request):
    session = get_session(request)
    if not session:
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    conn = get_db()
    customers = conn.execute("""
        SELECT *,
            COALESCE(last_modified, submission_time) AS last_touched,
            CAST(julianday('now') - julianday(COALESCE(last_modified, submission_time)) AS INTEGER) AS days_since
        FROM customers
        WHERE last_touched != '' AND last_touched IS NOT NULL
        ORDER BY julianday(last_touched) ASC
        LIMIT 20
    """).fetchall()
    conn.close()
    return templates.TemplateResponse(request=request, name="stale.html",
                                      context={"customers": customers, "session": session})


# ── Executive Overview ────────────────────────────────────────────────────────

@app.get("/executive", response_class=HTMLResponse)
def executive(request: Request):
    session = get_session(request)
    if not session:
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    conn = get_db()
    metrics = {}
    for label in ("Critical", "Hot", "Concerned", "Stable", "Happy"):
        metrics[label] = conn.execute(
            "SELECT COUNT(*) FROM customers WHERE temperature_label = ?", (label,)
        ).fetchone()[0]
    metrics["Total"] = sum(metrics.values())
    metrics["At Risk"] = conn.execute(
        "SELECT COUNT(*) FROM customers WHERE at_risk = 'Yes – actively evaluating other vendors'"
    ).fetchone()[0]

    top_customers = conn.execute("""
        SELECT *,
            CASE WHEN at_risk = 'Yes – actively evaluating other vendors' THEN 0
                 WHEN at_risk = 'Not sure' THEN 1
                 ELSE 2 END AS risk_sort
        FROM customers
        WHERE temperature_label IN ('Critical', 'Hot')
        ORDER BY temperature_order ASC, risk_sort ASC, customer_name ASC
        LIMIT 20
    """).fetchall()

    risk_reasons = conn.execute("""
        SELECT risk_reasons, COUNT(*) as cnt
        FROM customers
        WHERE at_risk = 'Yes – actively evaluating other vendors' AND risk_reasons != ''
        ORDER BY cnt DESC
    """).fetchall()

    hot_locations = conn.execute("""
        SELECT location, COUNT(*) as cnt
        FROM customers
        WHERE temperature_label IN ('Critical', 'Hot') AND location != ''
        GROUP BY location ORDER BY cnt DESC LIMIT 8
    """).fetchall()

    conn.close()
    return templates.TemplateResponse(
        request=request, name="executive.html",
        context={
            "metrics": metrics, "top_customers": top_customers,
            "risk_reasons": risk_reasons, "hot_locations": hot_locations,
            "generated": datetime.now().strftime("%B %d, %Y at %I:%M %p"),
            "session": session,
        },
    )


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, msg: str = Query(""), error: str = Query("")):
    session = get_session(request)
    if not session:
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    if session.get("role") != "admin":
        return RedirectResponse(url=f"{ROOT_PATH}/", status_code=303)

    db_exists = os.path.exists(DB_PATH)
    db_size = f"{os.path.getsize(DB_PATH) // 1024} KB" if db_exists else "—"
    record_count = 0
    if db_exists:
        try:
            conn = get_db()
            record_count = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
            conn.close()
        except Exception:
            pass

    conn = get_db()
    users = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
    conn.close()

    email_cfg = get_email_config()
    backup_dir_2 = get_setting("backup_dir_2", "")
    flash_pw = get_setting("admin_flash_pw", "")
    if flash_pw:
        set_setting("admin_flash_pw", "")
    return templates.TemplateResponse(
        request=request, name="admin.html",
        context={
            "backups": list_backups(), "db_size": db_size,
            "record_count": record_count, "db_exists": db_exists,
            "msg": msg, "error": error, "session": session, "users": users,
            "email_cfg": email_cfg, "backup_dir_2": backup_dir_2,
            "flash_pw": flash_pw, "motd": get_setting("motd", ""),
        },
    )


@app.post("/admin/motd")
def admin_motd(request: Request, motd: str = Form("")):
    session = get_session(request)
    if not session or session.get("role") != "admin":
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    set_setting("motd", motd.strip())
    log_action(session["username"], "update_motd", "", f"motd={'set' if motd.strip() else 'cleared'}")
    return RedirectResponse(url=f"{ROOT_PATH}/admin?msg=MOTD+updated", status_code=303)


@app.post("/admin/backup")
def admin_backup(request: Request):
    session = get_session(request)
    if not session or session.get("role") != "admin":
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    dest = do_backup()
    name = os.path.basename(dest) if dest else "nothing to backup"
    log_action(session["username"], "backup", name)
    return RedirectResponse(url=f"{ROOT_PATH}/admin?msg=Backup+created%3A+{name}", status_code=303)


@app.post("/admin/backup-settings")
def admin_backup_settings(request: Request, backup_dir_2: str = Form("")):
    session = get_session(request)
    if not session or session.get("role") != "admin":
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    set_setting("backup_dir_2", backup_dir_2.strip())
    log_action(session["username"], "backup_settings", "", f"secondary={backup_dir_2.strip() or 'cleared'}")
    return RedirectResponse(url=f"{ROOT_PATH}/admin?msg=Backup+settings+saved", status_code=303)


@app.post("/admin/upload")
async def admin_upload(request: Request, file: UploadFile = File(...)):
    session = get_session(request)
    if not session or session.get("role") != "admin":
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    content = await file.read()
    text = content.decode("utf-8-sig")

    # Guard: a headerless or wrong-format CSV would otherwise ingest silently
    # (DictReader treats the first data row as the header → zero rows processed,
    # no error). Require the Forms header before we touch the DB.
    header = {h.strip() for h in next(csv.reader(io.StringIO(text)), [])}
    if not {"ID", "Customer Name"}.issubset(header):
        return RedirectResponse(
            url=f"{ROOT_PATH}/admin?error=" + quote_plus(
                "No records ingested — the CSV is missing the Microsoft Forms header row "
                "(expected columns including ID and Customer Name). Nothing was imported."),
            status_code=303)

    inserted, skipped_dup, skipped_mist, new_critical = ingest_fileobj(io.StringIO(text))
    log_action(session["username"], "upload_csv", file.filename,
               f"{inserted} inserted, {skipped_dup} duplicates, {skipped_mist} non-Mist skipped")

    # Fire email alerts for each new Critical customer
    for c in new_critical:
        subject = f"[Opal] New Critical Customer: {c['name']}"
        body = (
            f"A new Critical customer has been added to Opal.\n\n"
            f"Customer:       {c['name']}\n"
            f"Account Manager:{c['am']}\n"
            f"Sales Engineer: {c['se']}\n"
            f"Actively at risk: {c['at_risk']}\n"
            f"Risk reasons:   {c['reasons']}\n\n"
            f"View in Opal-Mist: {get_setting('app_url', 'http://localhost:443')}\n"
        )
        send_alert(subject, body)
        log_action(session["username"], "email_alert", c['name'], "Critical customer alert sent")

    msg = f"{inserted}+inserted%2C+{skipped_dup}+duplicates+ignored%2C+{skipped_mist}+non-Mist+rows+skipped"
    if new_critical:
        msg += f"%2C+{len(new_critical)}+Critical+alert{'s' if len(new_critical) > 1 else ''}+sent"
    return RedirectResponse(url=f"{ROOT_PATH}/admin?msg={msg}", status_code=303)


@app.post("/admin/upload-engagement")
async def admin_upload_engagement(request: Request, file: UploadFile = File(...)):
    session = get_session(request)
    if not session or session.get("role") != "admin":
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)

    raw = await file.read()
    text = raw.decode("utf-8-sig")

    # Normalize header names: lowercase + strip
    def norm(s):
        return s.strip().lower() if s else ""

    # Skip metadata rows at the top — find the real header row by looking
    # for the line that contains "customer name" (case-insensitive)
    lines = text.splitlines()
    header_idx = 0
    for i, line in enumerate(lines):
        if "customer name" in line.lower():
            header_idx = i
            break
    data_text = "\n".join(lines[header_idx:])
    reader = csv.DictReader(io.StringIO(data_text))

    COL_MAP = {
        "customer name":      "customer_name",
        "sales engineer":     "sales_engineer",
        "state":              "state",
        "category":           "category",
        "bu plm sponsor":     "bu_plm_sponsor",
        "bu tme sponsor":     "bu_tme_sponsor",
        "current status":     "current_status",
        "next actions":       "next_actions",
        "get well plan link": "get_well_plan",
    }

    # Group rows by customer name (case-insensitive), merging duplicates
    groups: dict = {}
    for row in reader:
        normalized = {norm(k): v.strip() for k, v in row.items()}
        cname = normalized.get("customer name", "").strip()
        if not cname:
            continue
        key = cname.lower()
        if key not in groups:
            groups[key] = {"customer_name": cname}
        for csv_col, db_col in COL_MAP.items():
            if csv_col == "customer name":
                continue
            val = normalized.get(csv_col, "").strip()
            if not val:
                continue
            existing = groups[key].get(db_col, "")
            if not existing:
                groups[key][db_col] = val
            elif val not in existing:
                groups[key][db_col] = existing + "\n" + val

    conn = get_db()
    updated = skipped = 0
    skipped_names = []

    for key, fields in groups.items():
        cname = fields.pop("customer_name")
        row = conn.execute(
            "SELECT id FROM customers WHERE LOWER(customer_name) = ?", (cname.lower(),)
        ).fetchone()
        if not row:
            skipped += 1
            skipped_names.append(cname)
            continue
        sets = ", ".join(f"{col}=?" for col in fields)
        vals = list(fields.values()) + [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), row["id"]]
        conn.execute(f"UPDATE customers SET {sets}, last_modified=? WHERE id=?", vals)
        updated += 1

    conn.commit()
    conn.close()

    log_action(session["username"], "import_engagement", "",
               f"updated={updated}, skipped={skipped}")

    msg = f"{updated}+record(s)+updated+from+engagement+CSV"
    if skipped:
        names = "%2C+".join(skipped_names[:5])
        more = f"+%28and+{skipped - 5}+more%29" if skipped > 5 else ""
        msg += f"%2C+{skipped}+skipped+(no+match)%3A+{names}{more}"
    return RedirectResponse(url=f"{ROOT_PATH}/admin?msg={msg}", status_code=303)


@app.get("/admin/export")
def admin_export(request: Request):
    session = get_session(request)
    if not session or session.get("role") != "admin":
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    conn = get_db()
    rows = conn.execute("SELECT * FROM customers ORDER BY temperature_order, customer_name").fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID", "Submission Time", "Email", "Submitter", "Customer Name",
        "Location", "Account Manager", "Sales Engineer",
        "Temperature", "Heat Level", "At Risk", "Risk Reasons",
        "Architecture", "Near Term Goals", "BU Contact", "Ask from BU",
        "Background", "Notes", "Last Modified",
        "Custodian", "State", "Category", "BU PLM Sponsor", "BU TME Sponsor",
        "Current Status", "Next Actions", "Get Well Plan",
    ])
    for r in rows:
        writer.writerow([
            r["id"], r["submission_time"], r["email"], r["submitter_name"],
            r["customer_name"], r["location"], r["account_manager"], r["sales_engineer"],
            r["temperature"], r["temperature_label"], r["at_risk"], r["risk_reasons"],
            r["architecture"], r["near_term_goals"], r["bu_contact"],
            r["ask_from_bu"], r["background"], r["notes"], r["last_modified"],
            r["custodian"], r["state"], r["category"], r["bu_plm_sponsor"], r["bu_tme_sponsor"],
            r["current_status"], r["next_actions"], r["get_well_plan"],
        ])
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_action(session["username"], "export_csv", f"opal_export_{ts}.csv",
               f"{len(rows)} records exported")
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=opal_export_{ts}.csv"},
    )


@app.get("/admin/db-maintenance", response_class=HTMLResponse)
def db_maintenance(request: Request, msg: str = Query("")):
    session = get_session(request)
    if not session or session.get("role") != "admin":
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    conn = get_db()
    customers = conn.execute(
        "SELECT * FROM customers ORDER BY temperature_order, customer_name"
    ).fetchall()
    conn.close()
    return templates.TemplateResponse(
        request=request, name="db_maintenance.html",
        context={"customers": customers, "session": session, "msg": msg},
    )


TEXT_MERGE_FIELDS = [
    "notes", "background", "risk_reasons", "architecture",
    "near_term_goals", "ask_from_bu", "next_actions",
    "get_well_plan", "current_status",
    "state", "category", "bu_plm_sponsor", "bu_tme_sponsor", "custodian",
]


@app.post("/admin/db-maintenance/merge")
async def db_maintenance_merge(request: Request):
    session = get_session(request)
    if not session or session.get("role") != "admin":
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    form = await request.form()
    try:
        ids = [int(v) for v in form.getlist("ids")]
    except (ValueError, TypeError):
        return RedirectResponse(url=f"{ROOT_PATH}/admin/db-maintenance?msg=Invalid+selection", status_code=303)
    if len(ids) < 2:
        return RedirectResponse(url=f"{ROOT_PATH}/admin/db-maintenance?msg=Select+at+least+2+records+to+merge", status_code=303)

    conn = get_db()
    placeholders = ",".join("?" * len(ids))
    records = conn.execute(
        f"SELECT * FROM customers WHERE id IN ({placeholders})", ids
    ).fetchall()

    if len(records) < 2:
        conn.close()
        return RedirectResponse(url=f"{ROOT_PATH}/admin/db-maintenance?msg=Records+not+found", status_code=303)

    survivor = min(records, key=lambda r: (r["temperature_order"], r["id"]))
    others = [r for r in records if r["id"] != survivor["id"]]

    updates = {}
    for field in TEXT_MERGE_FIELDS:
        parts = []
        if survivor[field] and survivor[field].strip():
            parts.append(survivor[field].strip())
        for r in others:
            if r[field] and r[field].strip():
                parts.append(r[field].strip())
        updates[field] = "\n---\n".join(parts) if parts else None

    conn.execute("""
        UPDATE customers SET
            notes=?, background=?, risk_reasons=?, architecture=?,
            near_term_goals=?, ask_from_bu=?, next_actions=?,
            get_well_plan=?, current_status=?,
            state=?, category=?, bu_plm_sponsor=?, bu_tme_sponsor=?, custodian=?,
            last_modified=?
        WHERE id=?
    """, (
        updates["notes"], updates["background"], updates["risk_reasons"],
        updates["architecture"], updates["near_term_goals"], updates["ask_from_bu"],
        updates["next_actions"], updates["get_well_plan"], updates["current_status"],
        updates["state"], updates["category"], updates["bu_plm_sponsor"],
        updates["bu_tme_sponsor"], updates["custodian"],
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        survivor["id"],
    ))

    merged_names = []
    for r in others:
        conn.execute("UPDATE heat_history SET customer_id=? WHERE customer_id=?", (survivor["id"], r["id"]))
        conn.execute("DELETE FROM customers WHERE id=?", (r["id"],))
        merged_names.append(r["customer_name"])

    conn.commit()
    conn.close()

    log_action(session["username"], "merge_customers", survivor["customer_name"],
               f"merged into id={survivor['id']}; removed: {', '.join(merged_names)}")

    msg = f"Merged+into+{survivor['customer_name'].replace(' ', '+')}+%E2%80%94+{len(others)}+record{'s' if len(others)>1 else ''}+removed"
    return RedirectResponse(url=f"{ROOT_PATH}/admin/db-maintenance?msg={msg}", status_code=303)


@app.post("/admin/db-maintenance/delete/{customer_id}")
def db_maintenance_delete(request: Request, customer_id: int):
    session = get_session(request)
    if not session or session.get("role") != "admin":
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    conn = get_db()
    customer = conn.execute("SELECT customer_name FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if customer:
        name = customer["customer_name"]
        conn.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
        conn.execute("DELETE FROM heat_history WHERE customer_id = ?", (customer_id,))
        conn.commit()
        log_action(session["username"], "delete_customer", name, f"id={customer_id}")
        msg = f"Deleted: {name}"
    else:
        msg = "Record not found"
    conn.close()
    return RedirectResponse(url=f"{ROOT_PATH}/admin/db-maintenance?msg={msg}", status_code=303)


@app.post("/admin/delete-db")
def admin_delete_db(request: Request, confirm: str = Form("")):
    session = get_session(request)
    if not session or session.get("role") != "admin":
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    if confirm.strip().upper() != "DELETE":
        return RedirectResponse(url=f"{ROOT_PATH}/admin?msg=Type+DELETE+to+confirm", status_code=303)
    conn = get_db()
    conn.execute("DELETE FROM customers")
    conn.execute("DELETE FROM heat_history")
    conn.commit()
    conn.close()
    log_action(session["username"], "delete_db", "", "All customer and engagement data wiped; users and settings preserved")
    return RedirectResponse(url=f"{ROOT_PATH}/admin?msg=Customer+data+deleted.+Users%2C+logs%2C+and+settings+preserved.", status_code=303)


@app.post("/admin/restore/{filename}")
def admin_restore(request: Request, filename: str):
    session = get_session(request)
    if not session or session.get("role") != "admin":
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    safe_name = os.path.basename(filename)
    if safe_name != filename:
        return RedirectResponse(url=f"{ROOT_PATH}/admin?msg=Invalid+filename", status_code=303)
    src = os.path.join(BACKUP_DIR, safe_name)
    if not os.path.exists(src):
        return RedirectResponse(url=f"{ROOT_PATH}/admin?msg=Backup+not+found", status_code=303)
    if os.path.exists(DB_PATH):
        do_backup()
    shutil.copy2(src, DB_PATH)
    migrate_db()
    log_action(session["username"], "restore_backup", filename)
    return RedirectResponse(url=f"{ROOT_PATH}/admin?msg=Restored+from+{filename}", status_code=303)


# ── User management ───────────────────────────────────────────────────────────

@app.post("/admin/users/create")
def admin_user_create(
    request: Request,
    username: str = Form(...),
    email: str = Form(""),
    password: str = Form(...),
    role: str = Form("user"),
):
    session = get_session(request)
    if not session or session.get("role") != "admin":
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO users (username, email, password_hash, role, is_active, created_at)
            VALUES (?, ?, ?, ?, 1, ?)
        """, (username.strip(), email.strip(), pwd_ctx.hash(password), role,
              datetime.now().isoformat()))
        conn.commit()
        log_action(session["username"], "create_user", username.strip(), f"role={role}")
        msg = f"User+{username}+created"
    except sqlite3.IntegrityError:
        msg = f"Username+{username}+already+exists"
    conn.close()
    return RedirectResponse(url=f"{ROOT_PATH}/admin?msg={msg}", status_code=303)


@app.get("/admin/users/example-csv")
def admin_users_example_csv(request: Request):
    session = get_session(request)
    if not session or session.get("role") != "admin":
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    content = "username,email,password,role\njdoe,jdoe@example.com,TempPass1!,user\nsjones,sjones@example.com,TempPass2!,user\nbsmith,bsmith@example.com,TempPass3!,admin\n"
    return StreamingResponse(
        io.BytesIO(content.encode()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=example_users.csv"},
    )


@app.post("/admin/users/import")
def admin_user_import(request: Request, file: UploadFile = File(...)):
    session = get_session(request)
    if not session or session.get("role") != "admin":
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)

    created = skipped = errors = 0
    try:
        content = file.file.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        conn = get_db()
        for row in reader:
            username = (row.get("username") or "").strip()
            email    = (row.get("email") or "").strip()
            password = (row.get("password") or "").strip()
            role     = (row.get("role") or "user").strip().lower()
            if not username or not password:
                errors += 1
                continue
            if role not in ("admin", "user"):
                role = "user"
            try:
                conn.execute("""
                    INSERT INTO users (username, email, password_hash, role, is_active,
                                       must_change_password, created_at)
                    VALUES (?, ?, ?, ?, 1, 1, ?)
                """, (username, email, pwd_ctx.hash(password), role, datetime.now().isoformat()))
                created += 1
            except sqlite3.IntegrityError:
                skipped += 1
        conn.commit()
        conn.close()
        log_action(session["username"], "import_users", file.filename,
                   f"created={created} skipped={skipped} errors={errors}")
    except Exception as e:
        return RedirectResponse(url=f"{ROOT_PATH}/admin?msg=Import+failed:+{e}", status_code=303)

    msg = f"Import+complete:+{created}+created,+{skipped}+skipped+(duplicate),+{errors}+errors"
    return RedirectResponse(url=f"{ROOT_PATH}/admin?msg={msg}", status_code=303)


@app.post("/admin/users/{user_id}/toggle")
def admin_user_toggle(request: Request, user_id: int):
    session = get_session(request)
    if not session or session.get("role") != "admin":
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    if session["user_id"] == user_id:
        return RedirectResponse(url=f"{ROOT_PATH}/admin?msg=Cannot+disable+your+own+account", status_code=303)
    conn = get_db()
    target_user = conn.execute("SELECT username, is_active FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.execute("UPDATE users SET is_active = 1 - is_active WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    if target_user:
        new_state = "disabled" if target_user["is_active"] else "enabled"
        log_action(session["username"], f"user_{new_state}", target_user["username"])
    return RedirectResponse(url=f"{ROOT_PATH}/admin?msg=User+updated", status_code=303)


@app.post("/admin/users/{user_id}/reset-password")
def admin_reset_password(request: Request, user_id: int):
    session = get_session(request)
    if not session or session.get("role") != "admin":
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    temp_pw = secrets.token_urlsafe(10)
    conn = get_db()
    target_user = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?",
        (pwd_ctx.hash(temp_pw), user_id)
    )
    conn.commit()
    conn.close()
    if target_user:
        log_action(session["username"], "reset_password", target_user["username"])
        set_setting("admin_flash_pw", f"Temporary password for {target_user['username']}: {temp_pw} — user must change on next login")
    return RedirectResponse(url=f"{ROOT_PATH}/admin", status_code=303)


# ── Email Settings ────────────────────────────────────────────────────────────

@app.post("/admin/email-settings")
def admin_email_settings(
    request: Request,
    email_alerts_enabled: str = Form("false"),
    email_smtp_host:      str = Form(""),
    email_smtp_port:      str = Form("587"),
    email_username:       str = Form(""),
    email_password:       str = Form(""),
    email_from:           str = Form(""),
    email_to:             str = Form(""),
):
    session = get_session(request)
    if not session or session.get("role") != "admin":
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    for key, value in [
        ("email_alerts_enabled", email_alerts_enabled),
        ("email_smtp_host",      email_smtp_host.strip()),
        ("email_smtp_port",      email_smtp_port.strip() or "587"),
        ("email_username",       email_username.strip()),
        ("email_from",           email_from.strip()),
        ("email_to",             email_to.strip()),
    ]:
        set_setting(key, value)
    # Only update password if one was provided (blank = keep existing)
    if email_password:
        set_setting("email_password", email_password)
    log_action(session["username"], "update_email_settings", "",
               f"enabled={email_alerts_enabled}, host={email_smtp_host}, to={email_to}")
    return RedirectResponse(url=f"{ROOT_PATH}/admin?msg=Email+settings+saved", status_code=303)


@app.post("/admin/email-test")
def admin_email_test(request: Request):
    session = get_session(request)
    if not session or session.get("role") != "admin":
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    try:
        send_test_email()
        log_action(session["username"], "email_test", "", "Test email sent successfully")
        return RedirectResponse(url=f"{ROOT_PATH}/admin?msg=Test+email+sent+successfully", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"{ROOT_PATH}/admin?msg=Email+error%3A+{str(e)[:120]}", status_code=303)


# ── Audit Log ─────────────────────────────────────────────────────────────────

@app.get("/admin/audit", response_class=HTMLResponse)
def audit_log_page(request: Request):
    session = get_session(request)
    if not session or session.get("role") != "admin":
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    conn = get_db()
    entries = conn.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT 500"
    ).fetchall()
    conn.close()
    return templates.TemplateResponse(
        request=request, name="audit.html",
        context={"entries": entries, "session": session},
    )


# ── Trends ────────────────────────────────────────────────────────────────────

@app.get("/trends", response_class=HTMLResponse)
def trends(request: Request):
    session = get_session(request)
    if not session:
        return RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    conn = get_db()

    # Full heat history with customer info, ordered most recent first
    rows = conn.execute("""
        WITH ranked AS (
            SELECT customer_id, temperature_label, temperature_order, ts,
                   ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY id DESC) AS rn
            FROM heat_history
        )
        SELECT
            cu.customer_name, cu.account_manager, cu.sales_engineer, cu.id AS customer_id,
            curr.temperature_label  AS curr_label,
            curr.temperature_order  AS curr_order,
            curr.ts                 AS curr_ts,
            prev.temperature_label  AS prev_label,
            prev.temperature_order  AS prev_order,
            prev.ts                 AS prev_ts
        FROM customers cu
        JOIN ranked curr ON curr.customer_id = cu.id AND curr.rn = 1
        LEFT JOIN ranked prev ON prev.customer_id = cu.id AND prev.rn = 2
        ORDER BY
            CASE
                WHEN prev.temperature_label IS NULL THEN 3
                WHEN curr.temperature_order < prev.temperature_order THEN 1
                WHEN curr.temperature_order > prev.temperature_order THEN 2
                ELSE 4
            END,
            cu.customer_name ASC
    """).fetchall()

    # Summary counts
    worse  = sum(1 for r in rows if r["prev_label"] and r["curr_order"] < r["prev_order"])
    better = sum(1 for r in rows if r["prev_label"] and r["curr_order"] > r["prev_order"])
    new    = sum(1 for r in rows if r["prev_label"] is None)
    same   = sum(1 for r in rows if r["prev_label"] and r["curr_order"] == r["prev_order"])

    conn.close()
    return templates.TemplateResponse(
        request=request, name="trends.html",
        context={
            "rows": rows, "session": session,
            "worse": worse, "better": better, "new": new, "same": same,
        },
    )
