#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GramSatark - Digital Village Transparency & Accountability Platform
Every Rupee. Every Project. Every Voice.

ONE FILE. The entire product lives here:
  * Flask configuration, SQLite schema, migrations-on-start, demo seed data
  * Authentication, sessions, password hashing, role-based authorization
  * Projects, budgets, status history, evidence upload + four-eyes verification
  * Complaint engine with unique IDs, public tracking and visible timelines
  * Citizen questions & representative answers
  * Representative applications, election, one-person-one-vote, top-three appointment,
    six-month terms with automatic expiry
  * Notifications, analytics, public summaries, audit log, CSV exports
  * Page routes that render YOUR html files from ./templates (backend only - no frontend included)

Put your HTML in templates/ and static/ , then run:
    pip install flask
    python app.py                 # http://127.0.0.1:5000
    PORT=8000 python app.py
    gunicorn app:app              # production (set GRAMSATARK_SECRET_KEY, GRAMSATARK_ADMIN_PASSWORD)

Environment variables (all optional):
    GRAMSATARK_SECRET_KEY, GRAMSATARK_DB, GRAMSATARK_UPLOADS, GRAMSATARK_ADMIN_PASSWORD,
    GRAMSATARK_DEBUG=1, GRAMSATARK_HTTPS=1 (secure cookies), PORT, HOST
"""
import calendar
import csv
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from functools import wraps

from flask import Flask, Response, abort, g, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("GRAMSATARK_DB", os.path.join(BASE_DIR, "gramsatark_live.db"))
UPLOAD_DIR = os.environ.get("GRAMSATARK_UPLOADS", os.path.join(BASE_DIR, "uploads"))
os.makedirs(UPLOAD_DIR, exist_ok=True)

IST = timezone(timedelta(hours=5, minutes=30))
MAX_UPLOAD_MB = 8
ALLOWED_EXT = {"jpg", "jpeg", "png", "webp", "pdf"}
PROJECT_STATUS = ("Planned", "Ongoing", "Completed", "Delayed", "Pending")
CATEGORIES = ("Road", "Water Supply", "School", "Health Centre", "Drainage", "Street Light", "Other")
COMPLAINT_STATUS = ("Registered", "Verified", "In Progress", "Resolved", "Rejected")
EVIDENCE_TYPES = ("photo", "document", "inspection_report")
TERM_MONTHS = 6
TOP_N = 3


def _secret_key():
    env = os.environ.get("GRAMSATARK_SECRET_KEY")
    if env:
        return env
    path = os.path.join(os.path.dirname(DB_PATH) or BASE_DIR, ".gramsatark_secret")
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        key = secrets.token_hex(32)
        try:
            with open(path, "w") as fh:
                fh.write(key)
            os.chmod(path, 0o600)
        except OSError:
            pass
        return key


app = Flask(__name__)
app.config.update(
    SECRET_KEY=_secret_key(),
    MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("GRAMSATARK_HTTPS") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    JSON_SORT_KEYS=False,
)
app.json.ensure_ascii = False
app.json.sort_keys = False


# --------------------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today():
    return datetime.now(IST).date()


def add_months(d, m):
    y = d.year + (d.month - 1 + m) // 12
    mo = (d.month - 1 + m) % 12 + 1
    return date(y, mo, min(d.day, calendar.monthrange(y, mo)[1]))


def ok(**kw):
    return jsonify({"ok": True, **kw})


def body():
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form.to_dict() if request.form else {}


def text(d, key, maxlen=500, required=False, label=None):
    v = d.get(key)
    v = "" if v is None else str(v).strip()
    label = label or key.replace("_", " ").capitalize()
    if required and not v:
        raise ApiError(f"{label} is required.")
    if len(v) > maxlen:
        raise ApiError(f"{label} is too long (maximum {maxlen} characters).")
    return v


def money(d, key, default=None):
    v = d.get(key)
    if v in (None, ""):
        if default is None:
            raise ApiError(f"{key.replace('_', ' ').capitalize()} is required.")
        return default
    try:
        n = round(float(v), 2)
    except (TypeError, ValueError):
        raise ApiError(f"{key.replace('_', ' ').capitalize()} must be a number.")
    if n < 0 or n > 1e12:
        raise ApiError(f"{key.replace('_', ' ').capitalize()} is out of range.")
    return n


def intval(v, label="Value", default=None, lo=None, hi=None):
    if v in (None, ""):
        if default is None:
            raise ApiError(f"{label} is required.")
        return default
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ApiError(f"{label} must be a whole number.")
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        raise ApiError(f"{label} is out of range.")
    return n


def pick(v, choices, label):
    if v not in choices:
        raise ApiError(f"{label} must be one of: {', '.join(choices)}.")
    return v


def vdate(v, label, required=False):
    v = (v or "").strip() if isinstance(v, str) else v
    if not v:
        if required:
            raise ApiError(f"{label} is required.")
        return None
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        raise ApiError(f"{label} must be a date in YYYY-MM-DD format.")
    return v


def norm_phone(p):
    p = re.sub(r"[\s\-]", "", p or "")
    p = re.sub(r"^(\+91|91|0)(?=\d{10}$)", "", p)
    if not re.fullmatch(r"[6-9]\d{9}", p):
        raise ApiError("Enter a valid 10-digit Indian mobile number.")
    return p


_hits = {}


def rate_limit(bucket, limit=15, window=300):
    key, t = (bucket, request.remote_addr), time.time()
    arr = [x for x in _hits.get(key, []) if t - x < window]
    if len(arr) >= limit:
        raise ApiError("Too many attempts. Please wait a few minutes and try again.", 429)
    arr.append(t)
    _hits[key] = arr


# --------------------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS villages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, district TEXT NOT NULL DEFAULT 'Nagpur',
  state TEXT NOT NULL DEFAULT 'Maharashtra', pincode TEXT, created_at TEXT NOT NULL,
  UNIQUE(name, district));
CREATE TABLE IF NOT EXISTS village_onboarding_requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT, applicant_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
  village_name TEXT NOT NULL, district TEXT NOT NULL, state TEXT NOT NULL, pincode TEXT,
  note TEXT, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
  reviewed_by INTEGER REFERENCES users(id), review_note TEXT, village_id INTEGER REFERENCES villages(id),
  created_at TEXT NOT NULL, reviewed_at TEXT);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  phone TEXT, password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'citizen' CHECK(role IN ('citizen','representative','admin')),
  village_id INTEGER REFERENCES villages(id), is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY AUTOINCREMENT, village_id INTEGER NOT NULL REFERENCES villages(id),
  name TEXT NOT NULL, category TEXT NOT NULL, description TEXT, department TEXT,
  allocated_budget REAL NOT NULL DEFAULT 0, released_amount REAL NOT NULL DEFAULT 0,
  spent_amount REAL NOT NULL DEFAULT 0, start_date TEXT, expected_completion TEXT, actual_completion TEXT,
  contractor TEXT,
  status TEXT NOT NULL DEFAULT 'Planned' CHECK(status IN ('Planned','Ongoing','Completed','Delayed','Pending')),
  progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
  created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL,
  CHECK(released_amount <= allocated_budget), CHECK(spent_amount <= released_amount));
CREATE TABLE IF NOT EXISTS project_updates(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL REFERENCES projects(id),
  updated_by INTEGER REFERENCES users(id), old_status TEXT, new_status TEXT NOT NULL,
  progress INTEGER, notes TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS complaints(
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE,
  citizen_id INTEGER NOT NULL REFERENCES users(id), village_id INTEGER NOT NULL REFERENCES villages(id),
  project_id INTEGER REFERENCES projects(id), category TEXT NOT NULL, title TEXT NOT NULL,
  description TEXT NOT NULL, location TEXT, photo_path TEXT,
  status TEXT NOT NULL DEFAULT 'Registered'
    CHECK(status IN ('Registered','Verified','In Progress','Resolved','Rejected')),
  assigned_representative INTEGER REFERENCES users(id), created_at TEXT NOT NULL, resolved_at TEXT);
CREATE TABLE IF NOT EXISTS complaint_updates(
  id INTEGER PRIMARY KEY AUTOINCREMENT, complaint_id INTEGER NOT NULL REFERENCES complaints(id),
  updated_by INTEGER REFERENCES users(id), status TEXT NOT NULL, notes TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence(
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER REFERENCES projects(id),
  complaint_id INTEGER REFERENCES complaints(id), uploaded_by INTEGER NOT NULL REFERENCES users(id),
  type TEXT NOT NULL CHECK(type IN ('photo','document','inspection_report')),
  file_path TEXT NOT NULL, description TEXT, latitude REAL, longitude REAL,
  verification_status TEXT NOT NULL DEFAULT 'pending'
    CHECK(verification_status IN ('pending','verified','rejected')),
  verified_by INTEGER REFERENCES users(id), verified_at TEXT, created_at TEXT NOT NULL,
  CHECK(project_id IS NOT NULL OR complaint_id IS NOT NULL));
CREATE TABLE IF NOT EXISTS questions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, citizen_id INTEGER NOT NULL REFERENCES users(id),
  village_id INTEGER NOT NULL REFERENCES villages(id), project_id INTEGER REFERENCES projects(id),
  question TEXT NOT NULL, answer TEXT, answered_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL, answered_at TEXT);
CREATE TABLE IF NOT EXISTS representative_applications(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
  village_id INTEGER NOT NULL REFERENCES villages(id), education TEXT NOT NULL, address TEXT NOT NULL,
  identity_reference TEXT NOT NULL, declaration INTEGER NOT NULL CHECK(declaration = 1),
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
  review_note TEXT, verified_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS representatives(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
  village_id INTEGER NOT NULL REFERENCES villages(id), term_start TEXT NOT NULL, term_end TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed','expired','removed')),
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS elections(
  id INTEGER PRIMARY KEY AUTOINCREMENT, village_id INTEGER NOT NULL REFERENCES villages(id),
  start_date TEXT NOT NULL, end_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candidates(
  id INTEGER PRIMARY KEY AUTOINCREMENT, election_id INTEGER NOT NULL REFERENCES elections(id),
  user_id INTEGER NOT NULL REFERENCES users(id),
  approval_status TEXT NOT NULL DEFAULT 'approved' CHECK(approval_status IN ('approved','removed')),
  UNIQUE(election_id, user_id));
CREATE TABLE IF NOT EXISTS votes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, election_id INTEGER NOT NULL REFERENCES elections(id),
  voter_id INTEGER NOT NULL REFERENCES users(id), candidate_id INTEGER NOT NULL REFERENCES candidates(id),
  created_at TEXT NOT NULL, UNIQUE(election_id, voter_id));
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
  title TEXT NOT NULL, message TEXT, link TEXT, is_read INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT NOT NULL, entity TEXT, entity_id INTEGER,
  old_value TEXT, new_value TEXT, ip_address TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_proj_v ON projects(village_id, status);
CREATE INDEX IF NOT EXISTS ix_comp_v ON complaints(village_id, status);
CREATE INDEX IF NOT EXISTS ix_comp_c ON complaints(citizen_id);
CREATE INDEX IF NOT EXISTS ix_ev_p ON evidence(project_id);
CREATE INDEX IF NOT EXISTS ix_notif_u ON notifications(user_id, is_read);
CREATE INDEX IF NOT EXISTS ix_audit_t ON audit_logs(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_rep_active ON representatives(user_id) WHERE status='active';
CREATE UNIQUE INDEX IF NOT EXISTS ux_open_election ON elections(village_id) WHERE status='open';
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=20)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


def q(sql, args=(), one=False):
    rows = get_db().execute(sql, args).fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def qd(sql, args=()):
    return [dict(r) for r in q(sql, args)]


def x(sql, args=()):
    return get_db().execute(sql, args).lastrowid


@app.teardown_appcontext
def _close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        if exc:
            db.rollback()
        db.close()


@app.after_request
def _finish(resp):
    db = g.get("db")
    if db is not None:
        if resp.status_code < 400:
            db.commit()
        else:
            db.rollback()
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(self)"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# --------------------------------------------------------------------------------------
# Audit + notifications
# --------------------------------------------------------------------------------------
def audit(action, entity=None, entity_id=None, old=None, new=None, user_id=None):
    uid = user_id if user_id is not None else session.get("uid")
    ip = request.remote_addr if request else None
    x(
        "INSERT INTO audit_logs(user_id,action,entity,entity_id,old_value,new_value,ip_address,created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (uid, action, entity, entity_id,
         json.dumps(old, ensure_ascii=False, default=str) if old is not None else None,
         json.dumps(new, ensure_ascii=False, default=str) if new is not None else None, ip, now()),
    )


def notify(user_id, title, message="", link=""):
    if user_id:
        x("INSERT INTO notifications(user_id,title,message,link,created_at) VALUES(?,?,?,?,?)",
          (user_id, title, message, link, now()))


def notify_reps(village_id, title, message="", link=""):
    for r in q("SELECT user_id FROM representatives WHERE village_id=? AND status='active'", (village_id,)):
        notify(r["user_id"], title, message, link)


# --------------------------------------------------------------------------------------
# Auth helpers
# --------------------------------------------------------------------------------------
def cu():
    if "_user" not in g:
        g._user = None
        uid = session.get("uid")
        if uid:
            u = q("SELECT * FROM users WHERE id=? AND is_active=1", (uid,), True)
            if u:
                g._user = u
            else:
                session.clear()
    return g._user


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not cu():
            raise ApiError("Please log in to continue.", 401)
        return f(*a, **k)
    return w


def roles(*allowed):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            u = cu()
            if not u:
                raise ApiError("Please log in to continue.", 401)
            if u["role"] not in allowed:
                raise ApiError("You do not have permission to do this.", 403)
            return f(*a, **k)
        return w
    return deco


def is_staff_for(u, village_id):
    return bool(u) and (u["role"] == "admin" or (u["role"] == "representative" and u["village_id"] == village_id))


def user_public(u, private=False):
    d = {"id": u["id"], "name": u["name"], "role": u["role"], "village_id": u["village_id"]}
    if private:
        d.update(email=u["email"], phone=u["phone"], created_at=u["created_at"])
    return d


def password_ok(pw):
    if len(pw or "") < 8 or not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        raise ApiError("Password must be at least 8 characters and include a letter and a number.")


def village_must_exist(vid):
    v = q("SELECT * FROM villages WHERE id=?", (vid,), True)
    if not v:
        raise ApiError("Village not found.", 404)
    return v


# --------------------------------------------------------------------------------------
# Uploads
# --------------------------------------------------------------------------------------
def _sniff(head):
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"%PDF"):
        return "pdf"
    return None


def save_upload(field="file", required=True, images_only=False):
    f = request.files.get(field)
    if not f or not f.filename:
        if required:
            raise ApiError("Please choose a file to upload.")
        return None
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in ALLOWED_EXT:
        raise ApiError("Only JPG, PNG, WEBP images and PDF documents are allowed.")
    head = f.stream.read(16)
    f.stream.seek(0)
    kind = _sniff(head)
    if not kind or not (kind == ext or (kind == "jpg" and ext == "jpeg")):
        raise ApiError("The file content does not match its type. Upload a real image or PDF.")
    if images_only and kind == "pdf":
        raise ApiError("A photo must be an image (JPG, PNG or WEBP).")
    name = f"{uuid.uuid4().hex}.{kind}"
    f.save(os.path.join(UPLOAD_DIR, name))
    return name


def geo(d):
    lat, lng = d.get("latitude"), d.get("longitude")
    if lat in (None, "") or lng in (None, ""):
        return None, None
    try:
        lat, lng = float(lat), float(lng)
    except ValueError:
        raise ApiError("Location must be numeric.")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ApiError("Location is out of range.")
    return lat, lng


# --------------------------------------------------------------------------------------
# Request hooks, error handlers
# --------------------------------------------------------------------------------------
_last_maint = 0.0


def maintenance(force=False):
    """Auto-close elections past their end date and expire representative terms."""
    global _last_maint
    if not force and time.time() - _last_maint < 60:
        return
    _last_maint = time.time()
    t = today().isoformat()
    for e in q("SELECT id FROM elections WHERE status='open' AND end_date < ?", (t,)):
        finalize_election(e["id"], None)
    for r in q("SELECT id,user_id,village_id FROM representatives WHERE status='active' AND term_end < ?", (t,)):
        x("UPDATE representatives SET status='expired' WHERE id=?", (r["id"],))
        x("UPDATE users SET role='citizen' WHERE id=? AND role='representative'", (r["user_id"],))
        notify(r["user_id"], "Your term has ended", "Thank you for serving your village.", "#/reps")
        audit("representative.term_expired", "representatives", r["id"], user_id=0)


def _same_origin():
    """Block cross-site writes: Origin/Referer must match this host (X-Requested-With also accepted)."""
    if request.headers.get("X-Requested-With"):
        return True
    src = request.headers.get("Origin") or request.headers.get("Referer")
    if not src:
        return True  # non-browser clients (curl, Postman, tests); cookies are SameSite=Lax anyway
    m = re.match(r"https?://([^/]+)", src)
    return bool(m) and m.group(1) == request.host


@app.before_request
def _guard():
    if request.path.startswith("/api/"):
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not _same_origin():
            raise ApiError("Request blocked (cross-site request).", 403)
        maintenance()


@app.errorhandler(ApiError)
def _api_error(e):
    return jsonify({"ok": False, "error": e.message}), e.status


@app.errorhandler(sqlite3.IntegrityError)
def _integrity(e):
    msg = str(e)
    if "votes.election_id" in msg or "votes" in msg:
        return jsonify({"ok": False, "error": "You have already voted in this election."}), 409
    if "users.email" in msg:
        return jsonify({"ok": False, "error": "This email is already registered."}), 409
    if "CHECK constraint" in msg:
        return jsonify({"ok": False, "error": "Budget rule violated: spent <= released <= allocated."}), 400
    return jsonify({"ok": False, "error": "That action conflicts with existing data."}), 409


@app.errorhandler(404)
def _404(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "Not found."}), 404
    return Response("Not found", 404)


@app.errorhandler(405)
def _405(e):
    return jsonify({"ok": False, "error": "Method not allowed."}), 405


@app.errorhandler(413)
def _413(e):
    return jsonify({"ok": False, "error": f"File too large. Maximum size is {MAX_UPLOAD_MB} MB."}), 413


@app.errorhandler(500)
def _500(e):
    app.logger.exception("Server error")
    return jsonify({"ok": False, "error": "Something went wrong on our side. Please try again."}), 500


# --------------------------------------------------------------------------------------
# Auth API
# --------------------------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    q("SELECT 1")
    return ok(status="healthy", time=now())


@app.post("/api/auth/register")
def auth_register():
    rate_limit("register")
    d = body()
    name = text(d, "name", 80, True, "Name")
    email = text(d, "email", 120, True, "Email").lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ApiError("Enter a valid email address.")
    phone = norm_phone(text(d, "phone", 20, True, "Mobile number"))
    password_ok(d.get("password"))
    vid = intval(d.get("village_id"), "Village")
    village_must_exist(vid)
    uid = x("INSERT INTO users(name,email,phone,password_hash,role,village_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (name, email, phone, generate_password_hash(d["password"]), "citizen", vid, now()))
    session.clear()
    session["uid"] = uid
    session.permanent = True
    audit("auth.register", "users", uid, user_id=uid)
    g.pop("_user", None)
    return ok(user=user_public(cu(), True))


@app.post("/api/village-onboarding")
def village_onboarding_create():
    """Register a citizen and their missing village for administrator approval."""
    rate_limit("village_onboarding", 8, 600)
    d = body()
    name = text(d, "name", 80, True, "Name")
    email = text(d, "email", 120, True, "Email").lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ApiError("Enter a valid email address.")
    phone = norm_phone(text(d, "phone", 20, True, "Mobile number"))
    password_ok(d.get("password"))
    village = text(d, "village_name", 100, True, "Village name")
    district = text(d, "district", 80, True, "District")
    state = text(d, "state", 80, True, "State")
    pincode = text(d, "pincode", 10)
    existing = q("SELECT id FROM villages WHERE name=? AND district=?", (village, district), True)
    if existing:
        raise ApiError("This village is already onboarded. Choose it from citizen registration.", 409)
    uid = x("INSERT INTO users(name,email,phone,password_hash,role,village_id,is_active,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (name, email, phone, generate_password_hash(d["password"]), "citizen", None, 0, now()))
    rid = x("INSERT INTO village_onboarding_requests(applicant_id,village_name,district,state,pincode,note,created_at)"
            " VALUES(?,?,?,?,?,?,?)", (uid, village, district, state, pincode, text(d, "note", 600), now()))
    audit("village.onboarding_requested", "village_onboarding_requests", rid, new={"village": village}, user_id=uid)
    return ok(request_id=rid, message="Village request submitted. Your account will activate after administrator approval."), 201


@app.post("/api/auth/login")
def auth_login():
    rate_limit("login", 10)
    d = body()
    email = text(d, "email", 120, True, "Email").lower()
    u = q("SELECT * FROM users WHERE email=?", (email,), True)
    if not u or not check_password_hash(u["password_hash"], d.get("password") or ""):
        raise ApiError("Email or password is incorrect.", 401)
    if not u["is_active"]:
        raise ApiError("This account is disabled. Please contact the administrator.", 403)
    # Clear prior failed attempts once credentials are successfully verified.
    _hits.pop(("login", request.remote_addr), None)
    session.clear()
    session["uid"] = u["id"]
    session.permanent = True
    audit("auth.login", "users", u["id"], user_id=u["id"])
    g.pop("_user", None)
    return ok(user=user_public(cu(), True))


@app.get("/api/bootstrap/status")
def bootstrap_status():
    """Public, read-only first-run check used by the setup screen."""
    return ok(needs_setup=not bool(q("SELECT 1 FROM users LIMIT 1", one=True)))


@app.post("/api/bootstrap")
def bootstrap():
    """Create the first administrator and their first real village exactly once."""
    if q("SELECT 1 FROM users LIMIT 1", one=True):
        raise ApiError("Setup is already complete.", 409)
    d = body()
    name = text(d, "name", 80, True, "Administrator name")
    email = text(d, "email", 120, True, "Email").lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ApiError("Enter a valid email address.")
    phone = norm_phone(text(d, "phone", 20, True, "Mobile number"))
    password_ok(d.get("password"))
    village = text(d, "village_name", 100, True, "Village name")
    district = text(d, "district", 80, True, "District")
    state = text(d, "state", 80, True, "State")
    pincode = text(d, "pincode", 10, True, "Pincode")
    vid = x("INSERT INTO villages(name,district,state,pincode,created_at) VALUES(?,?,?,?,?)",
            (village, district, state, pincode, now()))
    uid = x("INSERT INTO users(name,email,phone,password_hash,role,village_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (name, email, phone, generate_password_hash(d["password"]), "admin", vid, now()))
    session.clear(); session["uid"] = uid; session.permanent = True
    audit("system.bootstrap", "villages", vid, new={"village": village}, user_id=uid)
    g.pop("_user", None)
    return ok(user=user_public(cu(), True), village_id=vid), 201


@app.post("/api/auth/logout")
def auth_logout():
    session.clear()
    return ok()


@app.get("/api/auth/me")
def auth_me():
    u = cu()
    if not u:
        return ok(user=None)
    unread = q("SELECT COUNT(*) c FROM notifications WHERE user_id=? AND is_read=0", (u["id"],), True)["c"]
    rep = q("SELECT term_start,term_end FROM representatives WHERE user_id=? AND status='active'", (u["id"],), True)
    return ok(user=user_public(u, True), unread=unread, term=dict(rep) if rep else None)


@app.post("/api/auth/password")
@login_required
def auth_password():
    d, u = body(), cu()
    if not check_password_hash(u["password_hash"], d.get("current_password") or ""):
        raise ApiError("Current password is incorrect.", 403)
    password_ok(d.get("new_password"))
    x("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(d["new_password"]), u["id"]))
    audit("auth.password_change", "users", u["id"])
    return ok()


# --------------------------------------------------------------------------------------
# Villages & public summary
# --------------------------------------------------------------------------------------
@app.get("/api/villages")
def villages_list():
    return ok(villages=qd("SELECT id,name,district,state,pincode FROM villages ORDER BY name"))


def summary_for(village_id=None):
    w, a = ("WHERE village_id=?", (village_id,)) if village_id else ("", ())
    pr = q(f"SELECT status, COUNT(*) c FROM projects {w} GROUP BY status", a)
    pc = {r["status"]: r["c"] for r in pr}
    b = q(f"SELECT COALESCE(SUM(allocated_budget),0) al, COALESCE(SUM(released_amount),0) rel,"
          f" COALESCE(SUM(spent_amount),0) sp FROM projects {w}", a, True)
    cr = q(f"SELECT status, COUNT(*) c FROM complaints {w} GROUP BY status", a)
    cc = {r["status"]: r["c"] for r in cr}
    total_c = sum(cc.values())
    resolved = cc.get("Resolved", 0)
    ev = q("SELECT COUNT(*) c, SUM(verification_status='verified') v FROM evidence e "
           + ("JOIN projects p ON p.id=e.project_id WHERE p.village_id=?" if village_id else ""),
           a, True)
    qn = q(f"SELECT COUNT(*) c, SUM(answer IS NOT NULL) a FROM questions {w}", a, True)
    return {
        "projects": {"total": sum(pc.values()), "by_status": pc, "completed": pc.get("Completed", 0)},
        "budget": {"allocated": b["al"], "released": b["rel"], "spent": b["sp"], "remaining": b["al"] - b["sp"],
                   "utilization": round(b["sp"] / b["al"] * 100, 1) if b["al"] else 0},
        "complaints": {"total": total_c, "by_status": cc, "resolved": resolved,
                       "resolution_rate": round(resolved / total_c * 100, 1) if total_c else 0},
        "evidence": {"total": ev["c"] or 0, "verified": int(ev["v"] or 0)},
        "questions": {"total": qn["c"] or 0, "answered": int(qn["a"] or 0)},
    }


@app.get("/api/reports/summary")
def public_summary():
    vid = request.args.get("village_id", type=int)
    if vid:
        village_must_exist(vid)
    return ok(summary=summary_for(vid))


# --------------------------------------------------------------------------------------
# Projects, budgets, history
# --------------------------------------------------------------------------------------
def project_dict(p):
    d = dict(p)
    d["remaining"] = round(d["allocated_budget"] - d["spent_amount"], 2)
    d["utilization"] = round(d["spent_amount"] / d["allocated_budget"] * 100, 1) if d["allocated_budget"] else 0
    return d


PROJECT_SELECT = (
    "SELECT p.*, v.name village_name,"
    " (SELECT COUNT(*) FROM evidence e WHERE e.project_id=p.id) evidence_count,"
    " (SELECT COUNT(*) FROM evidence e WHERE e.project_id=p.id AND e.verification_status='verified') verified_count,"
    " (SELECT MAX(created_at) FROM project_updates pu WHERE pu.project_id=p.id) last_update"
    " FROM projects p JOIN villages v ON v.id=p.village_id"
)


@app.get("/api/projects")
def projects_list():
    cond, args = [], []
    for key, col in (("village_id", "p.village_id"), ("category", "p.category"), ("status", "p.status")):
        val = request.args.get(key)
        if val:
            cond.append(f"{col}=?")
            args.append(val)
    term = (request.args.get("q") or "").strip()
    if term:
        cond.append("(p.name LIKE ? OR p.description LIKE ? OR p.contractor LIKE ? OR p.department LIKE ?)")
        args += [f"%{term}%"] * 4
    limit = min(request.args.get("limit", 100, type=int), 200)
    offset = max(request.args.get("offset", 0, type=int), 0)
    sql = PROJECT_SELECT + (" WHERE " + " AND ".join(cond) if cond else "") + \
        " ORDER BY p.created_at DESC, p.id DESC LIMIT ? OFFSET ?"
    return ok(projects=[project_dict(r) for r in q(sql, (*args, limit, offset))], categories=CATEGORIES,
              statuses=PROJECT_STATUS)


def evidence_for(where, args):
    return qd(
        "SELECT e.id,e.project_id,e.complaint_id,e.type,e.file_path,e.description,e.latitude,e.longitude,"
        " e.verification_status,e.verified_at,e.created_at, u.name uploaded_by_name, vb.name verified_by_name,"
        " e.uploaded_by FROM evidence e JOIN users u ON u.id=e.uploaded_by LEFT JOIN users vb ON vb.id=e.verified_by"
        f" WHERE {where} ORDER BY e.created_at DESC", args)


def get_project(pid):
    p = q(PROJECT_SELECT + " WHERE p.id=?", (pid,), True)
    if not p:
        raise ApiError("Project not found.", 404)
    return p


@app.get("/api/projects/<int:pid>")
def project_get(pid):
    p = project_dict(get_project(pid))
    p["complaint_count"] = q("SELECT COUNT(*) c FROM complaints WHERE project_id=?", (pid,), True)["c"]
    return ok(project=p, history=project_history_rows(pid), evidence=evidence_for("e.project_id=?", (pid,)))


def project_history_rows(pid):
    return qd("SELECT pu.id,pu.old_status,pu.new_status,pu.progress,pu.notes,pu.created_at,"
              " u.name updated_by_name, u.role updated_by_role FROM project_updates pu"
              " LEFT JOIN users u ON u.id=pu.updated_by WHERE pu.project_id=? ORDER BY pu.created_at DESC, pu.id DESC",
              (pid,))


@app.get("/api/projects/<int:pid>/history")
def project_history(pid):
    get_project(pid)
    return ok(history=project_history_rows(pid))


def read_project_fields(d, base=None):
    """Validate project fields; `base` is the existing row for partial updates."""
    b = dict(base) if base else {}
    out = {}
    out["name"] = text(d, "name", 160, base is None, "Project name") if ("name" in d or base is None) else b["name"]
    cat = d.get("category", b.get("category"))
    out["category"] = pick(cat, CATEGORIES, "Category")
    for k, lbl, ln in (("description", "Description", 2000), ("department", "Department", 120),
                       ("contractor", "Contractor", 160)):
        out[k] = text(d, k, ln, False, lbl) if k in d else b.get(k)
    for k in ("allocated_budget", "released_amount", "spent_amount"):
        out[k] = money(d, k, b.get(k, 0.0) if base else 0.0) if (k in d or base is None) else b[k]
    for k, lbl in (("start_date", "Start date"), ("expected_completion", "Expected completion"),
                   ("actual_completion", "Actual completion")):
        out[k] = vdate(d.get(k), lbl) if k in d else b.get(k)
    if out["released_amount"] > out["allocated_budget"]:
        raise ApiError("Released amount cannot be more than the allocated budget.")
    if out["spent_amount"] > out["released_amount"]:
        raise ApiError("Spent amount cannot be more than the released amount.")
    return out


@app.post("/api/projects")
@roles("admin", "representative")
def project_create():
    d, u = body(), cu()
    vid = u["village_id"] if u["role"] == "representative" else intval(d.get("village_id"), "Village")
    village_must_exist(vid)
    f = read_project_fields(d)
    status = pick(d.get("status") or "Planned", PROJECT_STATUS, "Status")
    progress = 100 if status == "Completed" else intval(d.get("progress"), "Progress", 0, 0, 100)
    pid = x("INSERT INTO projects(village_id,name,category,description,department,allocated_budget,released_amount,"
            "spent_amount,start_date,expected_completion,actual_completion,contractor,status,progress,created_by,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (vid, f["name"], f["category"], f["description"], f["department"], f["allocated_budget"],
             f["released_amount"], f["spent_amount"], f["start_date"], f["expected_completion"],
             f["actual_completion"], f["contractor"], status, progress, u["id"], now()))
    x("INSERT INTO project_updates(project_id,updated_by,old_status,new_status,progress,notes,created_at)"
      " VALUES(?,?,?,?,?,?,?)", (pid, u["id"], None, status, progress, "Project added to GramSatark.", now()))
    audit("project.create", "projects", pid, new={**f, "status": status, "village_id": vid})
    return ok(project=project_dict(get_project(pid))), 201


def apply_status(p, new_status, progress, notes, uid):
    old = p["status"]
    progress = 100 if new_status == "Completed" else (p["progress"] if progress is None else progress)
    acd = p["actual_completion"] or (today().isoformat() if new_status == "Completed" else None)
    x("UPDATE projects SET status=?, progress=?, actual_completion=? WHERE id=?",
      (new_status, progress, acd, p["id"]))
    x("INSERT INTO project_updates(project_id,updated_by,old_status,new_status,progress,notes,created_at)"
      " VALUES(?,?,?,?,?,?,?)", (p["id"], uid, old, new_status, progress, notes, now()))
    for c in q("SELECT DISTINCT citizen_id FROM complaints WHERE project_id=?", (p["id"],)):
        notify(c["citizen_id"], f"Update: {p['name']}", f"Status is now {new_status} ({progress}% done).",
               f"#/project/{p['id']}")
    audit("project.status", "projects", p["id"], {"status": old, "progress": p["progress"]},
          {"status": new_status, "progress": progress, "notes": notes})


@app.put("/api/projects/<int:pid>")
@roles("admin", "representative")
def project_update(pid):
    d, u = body(), cu()
    p = get_project(pid)
    if not is_staff_for(u, p["village_id"]):
        raise ApiError("You can only edit projects of your own village.", 403)
    f = read_project_fields(d, p)
    old = {k: p[k] for k in f}
    x("UPDATE projects SET name=?,category=?,description=?,department=?,allocated_budget=?,released_amount=?,"
      "spent_amount=?,start_date=?,expected_completion=?,actual_completion=?,contractor=? WHERE id=?",
      (f["name"], f["category"], f["description"], f["department"], f["allocated_budget"], f["released_amount"],
       f["spent_amount"], f["start_date"], f["expected_completion"], f["actual_completion"], f["contractor"], pid))
    changed = {k: f[k] for k in f if f[k] != old[k]}
    if changed:
        audit("project.edit", "projects", pid, {k: old[k] for k in changed}, changed)
    if d.get("status") and d["status"] != p["status"]:
        apply_status(p, pick(d["status"], PROJECT_STATUS, "Status"),
                     intval(d.get("progress"), "Progress", None, 0, 100) if d.get("progress") not in (None, "") else None,
                     text(d, "notes", 1000) or "Status updated.", u["id"])
    return ok(project=project_dict(get_project(pid)))


@app.post("/api/projects/<int:pid>/updates")
@roles("admin", "representative")
def project_add_update(pid):
    d, u = body(), cu()
    p = get_project(pid)
    if not is_staff_for(u, p["village_id"]):
        raise ApiError("You can only update projects of your own village.", 403)
    status = pick(d.get("status"), PROJECT_STATUS, "Status")
    notes = text(d, "notes", 1000, True, "Update note")
    progress = intval(d.get("progress"), "Progress", None, 0, 100) if d.get("progress") not in (None, "") else None
    apply_status(p, status, progress, notes, u["id"])
    return ok(project=project_dict(get_project(pid)), history=project_history_rows(pid)), 201


# --------------------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------------------
@app.get("/api/projects/<int:pid>/evidence")
def evidence_list(pid):
    get_project(pid)
    return ok(evidence=evidence_for("e.project_id=?", (pid,)))


@app.post("/api/projects/<int:pid>/evidence")
@roles("admin", "representative")
def evidence_add(pid):
    u, d = cu(), body()
    p = get_project(pid)
    if not is_staff_for(u, p["village_id"]):
        raise ApiError("You can only add evidence to projects of your own village.", 403)
    etype = pick(d.get("type") or "photo", EVIDENCE_TYPES, "Evidence type")
    fname = save_upload("file", True, images_only=(etype == "photo"))
    lat, lng = geo(d)
    admin = u["role"] == "admin"
    eid = x("INSERT INTO evidence(project_id,uploaded_by,type,file_path,description,latitude,longitude,"
            "verification_status,verified_by,verified_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (pid, u["id"], etype, fname, text(d, "description", 500), lat, lng,
             "verified" if admin else "pending", u["id"] if admin else None, now() if admin else None, now()))
    audit("evidence.upload", "evidence", eid, new={"project_id": pid, "type": etype})
    if not admin:
        for r in q("SELECT user_id FROM representatives WHERE village_id=? AND status='active' AND user_id<>?",
                   (p["village_id"], u["id"])):
            notify(r["user_id"], "Evidence awaiting verification", p["name"], f"#/project/{pid}")
    return ok(evidence=evidence_for("e.id=?", (eid,))[0]), 201


@app.put("/api/evidence/<int:eid>/verify")
@roles("admin", "representative")
def evidence_verify(eid):
    u, d = cu(), body()
    e = q("SELECT e.*, p.village_id pv, c.village_id cv FROM evidence e LEFT JOIN projects p ON p.id=e.project_id"
          " LEFT JOIN complaints c ON c.id=e.complaint_id WHERE e.id=?", (eid,), True)
    if not e:
        raise ApiError("Evidence not found.", 404)
    if not is_staff_for(u, e["pv"] or e["cv"]):
        raise ApiError("You can only verify evidence from your own village.", 403)
    if e["uploaded_by"] == u["id"] and u["role"] != "admin":
        raise ApiError("Evidence must be verified by a different representative (two-person check).", 403)
    st = pick(d.get("status"), ("verified", "rejected"), "Status")
    x("UPDATE evidence SET verification_status=?, verified_by=?, verified_at=? WHERE id=?", (st, u["id"], now(), eid))
    audit("evidence.verify", "evidence", eid, {"status": e["verification_status"]}, {"status": st})
    notify(e["uploaded_by"], f"Evidence {st}", text(d, "note", 300), f"#/project/{e['project_id']}" if e["project_id"] else "")
    return ok(evidence=evidence_for("e.id=?", (eid,))[0])


@app.get("/uploads/<path:fn>")
def serve_upload(fn):
    if not re.fullmatch(r"[0-9a-f]{32}\.(jpg|png|webp|pdf)", fn):
        abort(404)
    e = q("SELECT project_id, complaint_id FROM evidence WHERE file_path=?", (fn,), True)
    cid = None
    if e:
        if e["project_id"]:
            return send_from_directory(UPLOAD_DIR, fn, max_age=3600)
        cid = e["complaint_id"]
    else:
        c = q("SELECT id FROM complaints WHERE photo_path=?", (fn,), True)
        if not c:
            abort(404)
        cid = c["id"]
    u = cu()
    c = q("SELECT * FROM complaints WHERE id=?", (cid,), True)
    if not u or not c or not (u["id"] == c["citizen_id"] or is_staff_for(u, c["village_id"])):
        abort(403)
    return send_from_directory(UPLOAD_DIR, fn, max_age=0)


# --------------------------------------------------------------------------------------
# Complaints
# --------------------------------------------------------------------------------------
COMPLAINT_SELECT = (
    "SELECT c.*, v.name village_name, p.name project_name, u.name citizen_name, a.name assigned_name"
    " FROM complaints c JOIN villages v ON v.id=c.village_id LEFT JOIN projects p ON p.id=c.project_id"
    " JOIN users u ON u.id=c.citizen_id LEFT JOIN users a ON a.id=c.assigned_representative"
)


def complaint_timeline(cid, public=False):
    rows = qd("SELECT cu.status, cu.notes, cu.created_at, u.name by_name, u.role by_role FROM complaint_updates cu"
              " LEFT JOIN users u ON u.id=cu.updated_by WHERE cu.complaint_id=? ORDER BY cu.created_at, cu.id", (cid,))
    if public:
        rows = [r for r in rows if r["by_role"] != "citizen" or r["notes"] == "Complaint registered."]
        for r in rows:
            if r["by_role"] == "citizen":
                r["by_name"] = "Citizen"
    return rows


def get_complaint(cid, u=None):
    c = q(COMPLAINT_SELECT + " WHERE c.id=?", (cid,), True)
    if not c:
        raise ApiError("Complaint not found.", 404)
    if u is not None and not (u["id"] == c["citizen_id"] or is_staff_for(u, c["village_id"])):
        raise ApiError("You can only view your own complaints.", 403)
    return c


@app.post("/api/complaints")
@login_required
def complaint_create():
    rate_limit("complaint", 20, 600)
    u, d = cu(), body()
    title = text(d, "title", 140, True, "Title")
    desc = text(d, "description", 3000, True, "Description")
    cat = pick(d.get("category") or "Other", CATEGORIES, "Category")
    vid = u["village_id"]
    if u["role"] == "admin" and d.get("village_id"):
        vid = intval(d["village_id"], "Village")
    if not vid:
        raise ApiError("Your account is not linked to a village.")
    pid = None
    if d.get("project_id") not in (None, ""):
        pid = intval(d["project_id"], "Project")
        p = q("SELECT village_id FROM projects WHERE id=?", (pid,), True)
        if not p or p["village_id"] != vid:
            raise ApiError("That project does not belong to your village.")
    photo = save_upload("photo", False, images_only=True)
    cid = None
    for _ in range(5):
        code = f"GS{today().year}-{secrets.token_hex(3).upper()}"
        try:
            cid = x("INSERT INTO complaints(code,citizen_id,village_id,project_id,category,title,description,location,"
                    "photo_path,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (code, u["id"], vid, pid, cat, title, desc, text(d, "location", 200), photo, "Registered", now()))
            break
        except sqlite3.IntegrityError as e:
            if "complaints.code" not in str(e):
                raise
    if cid is None:
        raise ApiError("Could not generate a complaint ID. Please try again.", 500)
    x("INSERT INTO complaint_updates(complaint_id,updated_by,status,notes,created_at) VALUES(?,?,?,?,?)",
      (cid, u["id"], "Registered", "Complaint registered.", now()))
    audit("complaint.create", "complaints", cid, new={"code": code, "category": cat})
    notify_reps(vid, f"New complaint {code}", title, f"#/complaint/{cid}")
    notify(u["id"], f"Complaint {code} registered", "Use this ID to track progress.", f"#/complaint/{cid}")
    return ok(complaint=dict(get_complaint(cid))), 201


@app.get("/api/complaints")
@login_required
def complaint_list():
    u = cu()
    cond, args = [], []
    if u["role"] == "citizen":
        cond.append("c.citizen_id=?")
        args.append(u["id"])
    elif u["role"] == "representative":
        cond.append("c.village_id=?")
        args.append(u["village_id"])
    elif request.args.get("village_id"):
        cond.append("c.village_id=?")
        args.append(request.args.get("village_id", type=int))
    if request.args.get("mine") == "1":
        cond.append("c.citizen_id=?")
        args.append(u["id"])
    for key in ("status", "project_id", "category"):
        if request.args.get(key):
            cond.append(f"c.{key}=?")
            args.append(request.args[key])
    term = (request.args.get("q") or "").strip()
    if term:
        cond.append("(c.title LIKE ? OR c.code LIKE ? OR c.description LIKE ?)")
        args += [f"%{term}%"] * 3
    limit = min(request.args.get("limit", 100, type=int), 300)
    rows = qd(COMPLAINT_SELECT + (" WHERE " + " AND ".join(cond) if cond else "") +
              " ORDER BY c.created_at DESC LIMIT ?", (*args, limit))
    return ok(complaints=rows, statuses=COMPLAINT_STATUS)


@app.get("/api/complaints/track/<code>")
def complaint_track(code):
    rate_limit("track", 60)
    c = q(COMPLAINT_SELECT + " WHERE c.code=?", (code.strip().upper(),), True)
    if not c:
        raise ApiError("No complaint found with that ID. Check the ID and try again.", 404)
    pub = {k: c[k] for k in ("code", "title", "category", "status", "village_name", "project_name", "created_at",
                             "resolved_at")}
    return ok(complaint=pub, timeline=complaint_timeline(c["id"], True))


@app.get("/api/complaints/<int:cid>")
@login_required
def complaint_get(cid):
    u = cu()
    c = get_complaint(cid, u)
    staff = is_staff_for(u, c["village_id"])
    reps = qd("SELECT u.id, u.name FROM representatives r JOIN users u ON u.id=r.user_id"
              " WHERE r.village_id=? AND r.status='active'", (c["village_id"],)) if staff else []
    return ok(complaint=dict(c), timeline=complaint_timeline(cid), staff=staff, representatives=reps,
              evidence=evidence_for("e.complaint_id=?", (cid,)), statuses=COMPLAINT_STATUS)


@app.put("/api/complaints/<int:cid>")
@roles("admin", "representative")
def complaint_update(cid):
    u, d = cu(), body()
    c = get_complaint(cid)
    if not is_staff_for(u, c["village_id"]):
        raise ApiError("You can only handle complaints of your own village.", 403)
    notes = text(d, "notes", 1500)
    status = d.get("status") or c["status"]
    pick(status, COMPLAINT_STATUS, "Status")
    if status == "Rejected" and not notes:
        raise ApiError("Please add a note explaining why the complaint is rejected.")
    if status == "Resolved" and not notes and status != c["status"]:
        raise ApiError("Please add a short note describing how it was resolved.")
    assigned = c["assigned_representative"]
    if d.get("assigned_representative") not in (None, ""):
        aid = intval(d["assigned_representative"], "Representative")
        ok_rep = q("SELECT 1 FROM representatives WHERE user_id=? AND village_id=? AND status='active'",
                   (aid, c["village_id"]), True) or (q("SELECT 1 FROM users WHERE id=? AND role='admin'", (aid,), True))
        if not ok_rep:
            raise ApiError("Choose an active representative of this village.")
        assigned = aid
    elif u["role"] == "representative" and not assigned:
        assigned = u["id"]
    resolved_at = now() if status == "Resolved" else (None if status != "Resolved" and c["status"] == "Resolved" else c["resolved_at"])
    x("UPDATE complaints SET status=?, assigned_representative=?, resolved_at=? WHERE id=?",
      (status, assigned, resolved_at, cid))
    if status != c["status"] or notes or assigned != c["assigned_representative"]:
        x("INSERT INTO complaint_updates(complaint_id,updated_by,status,notes,created_at) VALUES(?,?,?,?,?)",
          (cid, u["id"], status, notes or ("Status changed to " + status + "."), now()))
        notify(c["citizen_id"], f"Complaint {c['code']}: {status}", notes, f"#/complaint/{cid}")
    audit("complaint.update", "complaints", cid, {"status": c["status"], "assigned": c["assigned_representative"]},
          {"status": status, "assigned": assigned, "notes": notes})
    return ok(complaint=dict(get_complaint(cid)), timeline=complaint_timeline(cid))


@app.post("/api/complaints/<int:cid>/updates")
@login_required
def complaint_comment(cid):
    u, d = cu(), body()
    c = get_complaint(cid, u)
    if is_staff_for(u, c["village_id"]):
        return complaint_update(cid)
    note = text(d, "notes", 1500, True, "Message")
    x("INSERT INTO complaint_updates(complaint_id,updated_by,status,notes,created_at) VALUES(?,?,?,?,?)",
      (cid, u["id"], c["status"], note, now()))
    notify_reps(c["village_id"], f"New message on {c['code']}", note[:120], f"#/complaint/{cid}")
    audit("complaint.comment", "complaints", cid)
    return ok(timeline=complaint_timeline(cid)), 201


@app.post("/api/complaints/<int:cid>/evidence")
@login_required
def complaint_evidence(cid):
    u, d = cu(), body()
    c = get_complaint(cid, u)
    etype = pick(d.get("type") or "photo", EVIDENCE_TYPES, "Evidence type")
    fname = save_upload("file", True, images_only=(etype == "photo"))
    lat, lng = geo(d)
    eid = x("INSERT INTO evidence(complaint_id,uploaded_by,type,file_path,description,latitude,longitude,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)", (cid, u["id"], etype, fname, text(d, "description", 500), lat, lng, now()))
    audit("evidence.upload", "evidence", eid, new={"complaint_id": cid})
    return ok(evidence=evidence_for("e.id=?", (eid,))[0]), 201


# --------------------------------------------------------------------------------------
# Questions (Ask)
# --------------------------------------------------------------------------------------
@app.post("/api/questions")
@login_required
def question_create():
    rate_limit("question", 30, 600)
    u, d = cu(), body()
    qtext = text(d, "question", 1000, True, "Question")
    pid = None
    if d.get("project_id") not in (None, ""):
        pid = intval(d["project_id"], "Project")
        if not q("SELECT 1 FROM projects WHERE id=?", (pid,), True):
            raise ApiError("Project not found.", 404)
    vid = u["village_id"]
    if not vid:
        raise ApiError("Your account is not linked to a village.")
    qid = x("INSERT INTO questions(citizen_id,village_id,project_id,question,created_at) VALUES(?,?,?,?,?)",
            (u["id"], vid, pid, qtext, now()))
    notify_reps(vid, "New question from a villager", qtext[:120], "#/desk")
    audit("question.create", "questions", qid)
    return ok(id=qid), 201


@app.get("/api/questions")
def question_list():
    u = cu()
    vid = request.args.get("village_id", type=int)
    pid = request.args.get("project_id", type=int)
    cond, args = [], []
    if vid:
        cond.append("qq.village_id=?")
        args.append(vid)
    if pid:
        cond.append("qq.project_id=?")
        args.append(pid)
    if u and u["role"] != "admin":
        if u["role"] == "representative":
            visible, va = "(qq.answer IS NOT NULL OR qq.village_id=?)", (u["village_id"],)
        else:
            visible, va = "(qq.answer IS NOT NULL OR qq.citizen_id=?)", (u["id"],)
        cond.append(visible)
        args += va
    elif not u:
        cond.append("qq.answer IS NOT NULL")
    if request.args.get("unanswered") == "1" and u and u["role"] in ("admin", "representative"):
        cond.append("qq.answer IS NULL")
    rows = qd("SELECT qq.id,qq.project_id,qq.village_id,qq.question,qq.answer,qq.created_at,qq.answered_at,"
              " qq.citizen_id, p.name project_name, a.name answered_by_name FROM questions qq"
              " LEFT JOIN projects p ON p.id=qq.project_id LEFT JOIN users a ON a.id=qq.answered_by"
              + (" WHERE " + " AND ".join(cond) if cond else "") + " ORDER BY qq.created_at DESC LIMIT 200", args)
    for r in rows:
        r["mine"] = bool(u and u["id"] == r["citizen_id"])
        del r["citizen_id"]
    return ok(questions=rows)


@app.put("/api/questions/<int:qid>/answer")
@roles("admin", "representative")
def question_answer(qid):
    u, d = cu(), body()
    qq = q("SELECT * FROM questions WHERE id=?", (qid,), True)
    if not qq:
        raise ApiError("Question not found.", 404)
    if not is_staff_for(u, qq["village_id"]):
        raise ApiError("You can only answer questions from your own village.", 403)
    ans = text(d, "answer", 3000, True, "Answer")
    x("UPDATE questions SET answer=?, answered_by=?, answered_at=? WHERE id=?", (ans, u["id"], now(), qid))
    notify(qq["citizen_id"], "Your question was answered", ans[:140], "#/ask")
    audit("question.answer", "questions", qid, {"answer": qq["answer"]}, {"answer": ans})
    return ok()


# --------------------------------------------------------------------------------------
# Representatives
# --------------------------------------------------------------------------------------
@app.get("/api/representatives")
def reps_list():
    vid = request.args.get("village_id", type=int)
    sql = ("SELECT r.id, r.user_id, r.village_id, r.term_start, r.term_end, u.name, u.phone, v.name village_name,"
           " (SELECT COUNT(*) FROM project_updates pu WHERE pu.updated_by=r.user_id) updates_made,"
           " (SELECT COUNT(*) FROM evidence e WHERE e.uploaded_by=r.user_id) evidence_uploaded,"
           " (SELECT COUNT(DISTINCT cu.complaint_id) FROM complaint_updates cu WHERE cu.updated_by=r.user_id) complaints_handled,"
           " (SELECT COUNT(*) FROM questions qq WHERE qq.answered_by=r.user_id) questions_answered"
           " FROM representatives r JOIN users u ON u.id=r.user_id JOIN villages v ON v.id=r.village_id"
           " WHERE r.status='active'" + (" AND r.village_id=?" if vid else "") + " ORDER BY r.id")
    return ok(representatives=qd(sql, (vid,) if vid else ()))


@app.post("/api/representatives/apply")
@roles("citizen")
def rep_apply():
    u, d = cu(), body()
    if not u["village_id"]:
        raise ApiError("Your account is not linked to a village.")
    if q("SELECT 1 FROM representative_applications WHERE user_id=? AND status IN ('pending','approved')",
         (u["id"],), True):
        raise ApiError("You already have an application in progress or approved.", 409)
    edu = text(d, "education", 200, True, "Education")
    addr = text(d, "address", 300, True, "Address")
    ref = text(d, "identity_reference", 40, True, "Identity reference")
    if re.fullmatch(r"\d{12}|\d{4}\s\d{4}\s\d{4}", ref):
        raise ApiError("Please do not enter a full Aadhaar number. Give only the last 4 digits or an ID type and last 4 digits.")
    if str(d.get("declaration")).lower() not in ("1", "true", "on", "yes"):
        raise ApiError("You must accept the Code of Ethics and confirm you have no political affiliation.")
    aid = x("INSERT INTO representative_applications(user_id,village_id,education,address,identity_reference,"
            "declaration,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (u["id"], u["village_id"], edu, addr, ref, 1, "pending", now()))
    audit("rep.apply", "representative_applications", aid)
    for a in q("SELECT id FROM users WHERE role='admin' AND is_active=1"):
        notify(a["id"], "New representative application", u["name"], "#/admin")
    return ok(id=aid), 201


@app.get("/api/representatives/applications")
@login_required
def rep_applications():
    u = cu()
    sql = ("SELECT a.id,a.user_id,a.village_id,a.education,a.address,a.identity_reference,a.status,a.review_note,"
           "a.created_at, u.name, u.phone, v.name village_name FROM representative_applications a"
           " JOIN users u ON u.id=a.user_id JOIN villages v ON v.id=a.village_id")
    if u["role"] == "admin":
        return ok(applications=qd(sql + " ORDER BY a.status='pending' DESC, a.created_at DESC"))
    return ok(applications=qd(sql + " WHERE a.user_id=? ORDER BY a.created_at DESC", (u["id"],)))


@app.put("/api/representatives/<int:aid>/verify")
@roles("admin")
def rep_verify(aid):
    u, d = cu(), body()
    a = q("SELECT * FROM representative_applications WHERE id=?", (aid,), True)
    if not a:
        raise ApiError("Application not found.", 404)
    st = pick(d.get("status"), ("approved", "rejected"), "Status")
    note = text(d, "note", 500)
    if st == "rejected" and not note:
        raise ApiError("Please give a reason for rejection.")
    x("UPDATE representative_applications SET status=?, review_note=?, verified_by=? WHERE id=?",
      (st, note, u["id"], aid))
    if st == "approved":
        for e in q("SELECT id FROM elections WHERE village_id=? AND status='open'", (a["village_id"],)):
            x("INSERT OR IGNORE INTO candidates(election_id,user_id,approval_status) VALUES(?,?,?)",
              (e["id"], a["user_id"], "approved"))
    notify(a["user_id"], f"Your application was {st}", note, "#/reps")
    audit("rep.verify", "representative_applications", aid, {"status": a["status"]}, {"status": st, "note": note})
    return ok()


# --------------------------------------------------------------------------------------
# Elections
# --------------------------------------------------------------------------------------
def election_phase(e):
    if e["status"] == "closed":
        return "closed"
    t = today().isoformat()
    return "scheduled" if t < e["start_date"] else ("open" if t <= e["end_date"] else "ended")


def finalize_election(eid, actor_id):
    e = q("SELECT * FROM elections WHERE id=?", (eid,), True)
    if not e or e["status"] == "closed":
        return None
    tally = q("SELECT c.id cid, c.user_id, u.name, COUNT(v.id) votes FROM candidates c JOIN users u ON u.id=c.user_id"
              " LEFT JOIN votes v ON v.candidate_id=c.id WHERE c.election_id=? AND c.approval_status='approved'"
              " GROUP BY c.id ORDER BY votes DESC, c.id ASC", (eid,))
    winners = [r for r in tally if r["votes"] > 0][:TOP_N]
    x("UPDATE elections SET status='closed' WHERE id=?", (eid,))
    if winners:
        for o in q("SELECT id,user_id FROM representatives WHERE village_id=? AND status='active'", (e["village_id"],)):
            x("UPDATE representatives SET status='completed' WHERE id=?", (o["id"],))
            x("UPDATE users SET role='citizen' WHERE id=? AND role='representative'", (o["user_id"],))
        start = today()
        end = add_months(start, TERM_MONTHS)
        for w in winners:
            x("INSERT INTO representatives(user_id,village_id,term_start,term_end,status,created_at)"
              " VALUES(?,?,?,?,?,?)", (w["user_id"], e["village_id"], start.isoformat(), end.isoformat(), "active", now()))
            x("UPDATE users SET role='representative' WHERE id=? AND role='citizen'", (w["user_id"],))
            notify(w["user_id"], "You were elected as a representative",
                   f"Your term runs until {end.isoformat()}.", "#/desk")
    audit("election.finalize", "elections", eid, None,
          {"winners": [{"user_id": w["user_id"], "votes": w["votes"]} for w in winners]}, user_id=actor_id or 0)
    return winners


def election_payload(e, u):
    phase = election_phase(e)
    cands = qd("SELECT c.id, c.user_id, u.name, a.education FROM candidates c JOIN users u ON u.id=c.user_id"
               " LEFT JOIN representative_applications a ON a.user_id=c.user_id AND a.status='approved'"
               " WHERE c.election_id=? AND c.approval_status='approved' ORDER BY u.name", (e["id"],))
    turnout = q("SELECT COUNT(*) c FROM votes WHERE election_id=?", (e["id"],), True)["c"]
    eligible = q("SELECT COUNT(*) c FROM users WHERE village_id=? AND is_active=1", (e["village_id"],), True)["c"]
    voted = bool(u and q("SELECT 1 FROM votes WHERE election_id=? AND voter_id=?", (e["id"], u["id"]), True))
    out = {"election": {**dict(e), "phase": phase,
                        "village_name": q("SELECT name FROM villages WHERE id=?", (e["village_id"],), True)["name"]},
           "candidates": cands, "turnout": turnout, "eligible": eligible, "has_voted": voted,
           "can_vote": bool(u and u["village_id"] == e["village_id"] and phase == "open" and not voted)}
    if phase == "closed":
        res = qd("SELECT c.id candidate_id, u.name, COUNT(v.id) votes FROM candidates c JOIN users u ON u.id=c.user_id"
                 " LEFT JOIN votes v ON v.candidate_id=c.id WHERE c.election_id=? AND c.approval_status='approved'"
                 " GROUP BY c.id ORDER BY votes DESC, c.id ASC", (e["id"],))
        for i, r in enumerate(res):
            r["elected"] = i < TOP_N and r["votes"] > 0
        out["results"] = res
    return out


@app.get("/api/election/current")
def election_current():
    u = cu()
    vid = request.args.get("village_id", type=int) or (u["village_id"] if u else None)
    if not vid:
        raise ApiError("Choose a village first.")
    village_must_exist(vid)
    e = q("SELECT * FROM elections WHERE village_id=? ORDER BY (status='open') DESC, id DESC LIMIT 1", (vid,), True)
    if not e:
        return ok(election=None)
    return ok(**election_payload(e, u))


@app.get("/api/election/candidates")
def election_candidates():
    e = q("SELECT * FROM elections WHERE id=?", (request.args.get("election_id", type=int),), True)
    if not e:
        raise ApiError("Election not found.", 404)
    return ok(candidates=election_payload(e, cu())["candidates"])


@app.get("/api/election/results")
def election_results():
    e = q("SELECT * FROM elections WHERE id=?", (request.args.get("election_id", type=int),), True)
    if not e:
        raise ApiError("Election not found.", 404)
    if election_phase(e) != "closed":
        raise ApiError("Results are published after the election closes.", 403)
    return ok(results=election_payload(e, cu())["results"])


@app.post("/api/election/vote")
@login_required
def election_vote():
    u, d = cu(), body()
    e = q("SELECT * FROM elections WHERE id=?", (intval(d.get("election_id"), "Election"),), True)
    if not e:
        raise ApiError("Election not found.", 404)
    if u["village_id"] != e["village_id"]:
        raise ApiError("Only residents of this village can vote in its election.", 403)
    if election_phase(e) != "open":
        raise ApiError("Voting is not open for this election.", 409)
    cid = intval(d.get("candidate_id"), "Candidate")
    if not q("SELECT 1 FROM candidates WHERE id=? AND election_id=? AND approval_status='approved'", (cid, e["id"]), True):
        raise ApiError("Choose a candidate from the list.")
    x("INSERT INTO votes(election_id,voter_id,candidate_id,created_at) VALUES(?,?,?,?)", (e["id"], u["id"], cid, now()))
    audit("election.vote", "elections", e["id"], new={"voter": u["id"]})  # candidate choice stays secret
    notify(u["id"], "Your vote was recorded", "Thank you for taking part.", "#/election")
    return ok(), 201


@app.post("/api/admin/elections")
@roles("admin")
def admin_election_create():
    d = body()
    vid = intval(d.get("village_id"), "Village")
    village_must_exist(vid)
    s, en = vdate(d.get("start_date"), "Start date", True), vdate(d.get("end_date"), "End date", True)
    if en < s:
        raise ApiError("End date must be on or after the start date.")
    if q("SELECT 1 FROM elections WHERE village_id=? AND status='open'", (vid,), True):
        raise ApiError("This village already has an open election. Close it first.", 409)
    eid = x("INSERT INTO elections(village_id,start_date,end_date,status,created_at) VALUES(?,?,?,?,?)",
            (vid, s, en, "open", now()))
    for a in q("SELECT user_id FROM representative_applications WHERE village_id=? AND status='approved'", (vid,)):
        x("INSERT OR IGNORE INTO candidates(election_id,user_id,approval_status) VALUES(?,?,?)", (eid, a["user_id"], "approved"))
    audit("election.create", "elections", eid, new={"village_id": vid, "start": s, "end": en})
    for c in q("SELECT id FROM users WHERE village_id=? AND is_active=1", (vid,)):
        notify(c["id"], "Representative election announced", f"Voting: {s} to {en}", "#/election")
    return ok(id=eid), 201


@app.post("/api/admin/elections/<int:eid>/close")
@roles("admin")
def admin_election_close(eid):
    e = q("SELECT * FROM elections WHERE id=?", (eid,), True)
    if not e:
        raise ApiError("Election not found.", 404)
    if e["status"] == "closed":
        raise ApiError("This election is already closed.", 409)
    winners = finalize_election(eid, cu()["id"])
    return ok(winners=[{"user_id": w["user_id"], "name": w["name"], "votes": w["votes"]} for w in winners])


@app.put("/api/election/candidates/<int:cid>")
@roles("admin")
def admin_candidate(cid):
    d = body()
    c = q("SELECT c.*, e.status es FROM candidates c JOIN elections e ON e.id=c.election_id WHERE c.id=?", (cid,), True)
    if not c:
        raise ApiError("Candidate not found.", 404)
    if c["es"] == "closed":
        raise ApiError("This election is already closed.", 409)
    st = pick(d.get("approval_status"), ("approved", "removed"), "Approval status")
    x("UPDATE candidates SET approval_status=? WHERE id=?", (st, cid))
    audit("candidate.status", "candidates", cid, {"status": c["approval_status"]}, {"status": st})
    return ok()


@app.get("/api/admin/elections")
@roles("admin")
def admin_elections():
    return ok(elections=qd("SELECT e.*, v.name village_name, (SELECT COUNT(*) FROM votes WHERE election_id=e.id) votes,"
                           " (SELECT COUNT(*) FROM candidates WHERE election_id=e.id AND approval_status='approved') candidates"
                           " FROM elections e JOIN villages v ON v.id=e.village_id ORDER BY e.id DESC"))


# --------------------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------------------
@app.get("/api/notifications")
@login_required
def notif_list():
    return ok(notifications=qd("SELECT id,title,message,link,is_read,created_at FROM notifications WHERE user_id=?"
                               " ORDER BY id DESC LIMIT 60", (cu()["id"],)))


@app.put("/api/notifications/<int:nid>/read")
@login_required
def notif_read(nid):
    x("UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?", (nid, cu()["id"]))
    return ok()


@app.post("/api/notifications/read-all")
@login_required
def notif_read_all():
    x("UPDATE notifications SET is_read=1 WHERE user_id=?", (cu()["id"],))
    return ok()


# --------------------------------------------------------------------------------------
# Representative desk (daily to-do list)
# --------------------------------------------------------------------------------------
@app.get("/api/rep/desk")
@roles("representative", "admin")
def rep_desk():
    u = cu()
    vid = u["village_id"] if u["role"] == "representative" else request.args.get("village_id", type=int)
    w, a = ("AND c.village_id=?", (vid,)) if vid else ("", ())
    complaints = qd(COMPLAINT_SELECT + f" WHERE c.status NOT IN ('Resolved','Rejected') {w} ORDER BY c.created_at", a)
    ev = qd("SELECT e.id, e.type, e.file_path, e.description, e.created_at, p.name project_name, p.id project_id,"
            " uu.name uploaded_by_name FROM evidence e JOIN projects p ON p.id=e.project_id JOIN users uu ON uu.id=e.uploaded_by"
            " WHERE e.verification_status='pending' AND e.uploaded_by<>? " + ("AND p.village_id=?" if vid else ""),
            (u["id"], *((vid,) if vid else ())))
    qs = qd("SELECT qq.id, qq.question, qq.created_at, p.name project_name FROM questions qq"
            " LEFT JOIN projects p ON p.id=qq.project_id WHERE qq.answer IS NULL " + ("AND qq.village_id=?" if vid else "")
            + " ORDER BY qq.created_at", (vid,) if vid else ())
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = [project_dict(p) for p in q(PROJECT_SELECT + " WHERE p.status IN ('Ongoing','Delayed','Pending')"
             + (" AND p.village_id=?" if vid else ""), (vid,) if vid else ())
             if not p["last_update"] or p["last_update"] < cutoff]
    return ok(complaints=complaints, evidence=ev, questions=qs, stale_projects=stale, village_id=vid)


@app.get("/api/rep/overview")
@roles("representative", "admin")
def rep_overview():
    u = cu()
    vid = u["village_id"] if u["role"] == "representative" else request.args.get("village_id", type=int)
    if not vid:
        raise ApiError("Choose a village to view the representative overview.")
    village_must_exist(vid)
    projects = [project_dict(p) for p in q(PROJECT_SELECT + " WHERE p.village_id=? ORDER BY p.created_at DESC", (vid,))]
    term = q("SELECT term_start,term_end,status FROM representatives WHERE user_id=? AND village_id=? AND status='active'",
             (u["id"], vid), True)
    by_dept = qd("SELECT COALESCE(department,'Unassigned') department,COUNT(*) projects,"
                 "COALESCE(SUM(allocated_budget),0) allocated,COALESCE(SUM(spent_amount),0) spent"
                 " FROM projects WHERE village_id=? GROUP BY department ORDER BY allocated DESC", (vid,))
    return ok(village=qd("SELECT id,name,district,state,pincode FROM villages WHERE id=?", (vid,))[0],
              summary=summary_for(vid), projects=projects, department_budget=by_dept,
              term=dict(term) if term else None,
              notifications=qd("SELECT id,title,message,link,is_read,created_at FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 8", (u["id"],)))


# --------------------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------------------
@app.get("/api/admin/dashboard")
@roles("admin")
def admin_dashboard():
    one = lambda s, a=(): q(s, a, True)["c"]
    s = summary_for(None)
    return ok(
        summary=s,
        counts={"users": one("SELECT COUNT(*) c FROM users"), "villages": one("SELECT COUNT(*) c FROM villages"),
                "representatives": one("SELECT COUNT(*) c FROM representatives WHERE status='active'"),
                "pending_applications": one("SELECT COUNT(*) c FROM representative_applications WHERE status='pending'"),
                "pending_evidence": one("SELECT COUNT(*) c FROM evidence WHERE verification_status='pending'"),
                "open_complaints": one("SELECT COUNT(*) c FROM complaints WHERE status NOT IN ('Resolved','Rejected')"),
                "open_elections": one("SELECT COUNT(*) c FROM elections WHERE status='open'")},
        by_village=qd("SELECT v.id, v.name, v.district, v.state, v.pincode,"
                      " (SELECT COUNT(*) FROM users WHERE village_id=v.id AND role='citizen') citizens,"
                      " (SELECT COUNT(*) FROM representatives WHERE village_id=v.id AND status='active') representatives,"
                      " (SELECT COUNT(*) FROM projects WHERE village_id=v.id) projects,"
                      " (SELECT COALESCE(SUM(allocated_budget),0) FROM projects WHERE village_id=v.id) allocated,"
                      " (SELECT COALESCE(SUM(spent_amount),0) FROM projects WHERE village_id=v.id) spent,"
                      " (SELECT COUNT(*) FROM complaints WHERE village_id=v.id) complaints,"
                      " (SELECT COUNT(*) FROM complaints WHERE village_id=v.id AND status='Resolved') resolved"
                      " FROM villages v ORDER BY v.name"),
        representatives=reps_list().get_json()["representatives"],
    )


@app.get("/api/admin/control-center")
@roles("admin")
def admin_control_center():
    """One authenticated admin payload for the governance workspace."""
    return ok(
        projects=[project_dict(p) for p in q(PROJECT_SELECT + " ORDER BY p.created_at DESC LIMIT 200")],
        complaints=qd(COMPLAINT_SELECT + " ORDER BY c.created_at DESC LIMIT 200"),
        evidence=qd("SELECT e.id,e.project_id,e.complaint_id,e.type,e.file_path,e.description,e.latitude,e.longitude,"
                    "e.verification_status,e.created_at,u.name uploaded_by_name,p.name project_name,c.code complaint_code"
                    " FROM evidence e JOIN users u ON u.id=e.uploaded_by LEFT JOIN projects p ON p.id=e.project_id"
                    " LEFT JOIN complaints c ON c.id=e.complaint_id ORDER BY e.id DESC LIMIT 200"),
        applications=qd("SELECT a.*,u.name user_name,u.email user_email,v.name village_name FROM representative_applications a"
                        " JOIN users u ON u.id=a.user_id JOIN villages v ON v.id=a.village_id ORDER BY a.id DESC"),
        users=qd("SELECT u.id,u.name,u.email,u.phone,u.role,u.is_active,u.created_at,v.name village_name"
                 " FROM users u LEFT JOIN villages v ON v.id=u.village_id ORDER BY u.id DESC LIMIT 300"),
        onboarding=qd("SELECT r.*,u.name applicant_name,u.email applicant_email FROM village_onboarding_requests r"
                      " JOIN users u ON u.id=r.applicant_id ORDER BY r.id DESC"),
        audits=qd("SELECT a.id,a.action,a.entity,a.entity_id,a.created_at,u.name user_name FROM audit_logs a"
                  " LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 100"),
        elections=qd("SELECT e.*,v.name village_name,(SELECT COUNT(*) FROM candidates c WHERE c.election_id=e.id) candidate_count"
                     " FROM elections e JOIN villages v ON v.id=e.village_id ORDER BY e.id DESC")
    )


@app.get("/api/admin/users")
@roles("admin")
def admin_users():
    cond, args = [], []
    if request.args.get("role"):
        cond.append("u.role=?")
        args.append(request.args["role"])
    term = (request.args.get("q") or "").strip()
    if term:
        cond.append("(u.name LIKE ? OR u.email LIKE ? OR u.phone LIKE ?)")
        args += [f"%{term}%"] * 3
    return ok(users=qd("SELECT u.id,u.name,u.email,u.phone,u.role,u.is_active,u.created_at,v.name village_name"
                       " FROM users u LEFT JOIN villages v ON v.id=u.village_id"
                       + (" WHERE " + " AND ".join(cond) if cond else "") + " ORDER BY u.id DESC LIMIT 300", args))


@app.put("/api/admin/users/<int:uid>")
@roles("admin")
def admin_user_update(uid):
    me, d = cu(), body()
    t = q("SELECT * FROM users WHERE id=?", (uid,), True)
    if not t:
        raise ApiError("User not found.", 404)
    if uid == me["id"]:
        raise ApiError("You cannot change your own role or status here.", 409)
    if "is_active" in d:
        x("UPDATE users SET is_active=? WHERE id=?", (1 if d["is_active"] in (True, 1, "1", "true") else 0, uid))
    if "role" in d:
        role = pick(d["role"], ("citizen", "admin"), "Role")
        if t["role"] == "representative" and role == "citizen":
            x("UPDATE representatives SET status='removed' WHERE user_id=? AND status='active'", (uid,))
        x("UPDATE users SET role=? WHERE id=?", (role, uid))
    if d.get("village_id"):
        village_must_exist(intval(d["village_id"], "Village"))
        x("UPDATE users SET village_id=? WHERE id=?", (int(d["village_id"]), uid))
    audit("admin.user_update", "users", uid, {"role": t["role"], "active": t["is_active"]}, d)
    return ok()


@app.post("/api/admin/villages")
@roles("admin")
def admin_village_create():
    d = body()
    name = text(d, "name", 100, True, "Village name")
    vid = x("INSERT INTO villages(name,district,state,pincode,created_at) VALUES(?,?,?,?,?)",
            (name, text(d, "district", 80) or "Nagpur", text(d, "state", 80) or "Maharashtra", text(d, "pincode", 10), now()))
    audit("village.create", "villages", vid, new={"name": name})
    return ok(id=vid), 201


@app.get("/api/admin/village-onboarding")
@roles("admin")
def admin_village_onboarding_list():
    status = request.args.get("status")
    sql = ("SELECT r.*,u.name applicant_name,u.email applicant_email,u.phone applicant_phone,"
           "rv.name reviewer_name FROM village_onboarding_requests r JOIN users u ON u.id=r.applicant_id "
           "LEFT JOIN users rv ON rv.id=r.reviewed_by")
    args = ()
    if status:
        sql += " WHERE r.status=?"; args = (pick(status, ("pending", "approved", "rejected"), "Status"),)
    return ok(requests=qd(sql + " ORDER BY r.id DESC", args))


@app.put("/api/admin/village-onboarding/<int:rid>")
@roles("admin")
def admin_village_onboarding_review(rid):
    d, admin = body(), cu()
    req = q("SELECT * FROM village_onboarding_requests WHERE id=?", (rid,), True)
    if not req:
        raise ApiError("Village onboarding request not found.", 404)
    if req["status"] != "pending":
        raise ApiError("This village request has already been reviewed.", 409)
    decision = pick(d.get("status"), ("approved", "rejected"), "Decision")
    note = text(d, "review_note", 600)
    vid = None
    if decision == "approved":
        existing = q("SELECT id FROM villages WHERE name=? AND district=?", (req["village_name"], req["district"]), True)
        vid = existing["id"] if existing else x("INSERT INTO villages(name,district,state,pincode,created_at) VALUES(?,?,?,?,?)",
                                               (req["village_name"], req["district"], req["state"], req["pincode"], now()))
        x("UPDATE users SET village_id=?,is_active=1 WHERE id=?", (vid, req["applicant_id"]))
        notify(req["applicant_id"], "Village approved", "Your village has been onboarded. You can now use GramSatark as a citizen.", "/dashboard")
    else:
        notify(req["applicant_id"], "Village request needs attention", note or "Please contact the administrator for details.", "")
    x("UPDATE village_onboarding_requests SET status=?,reviewed_by=?,review_note=?,village_id=?,reviewed_at=? WHERE id=?",
      (decision, admin["id"], note, vid, now(), rid))
    audit("village.onboarding_review", "village_onboarding_requests", rid, {"status": "pending"}, {"status": decision}, admin["id"])
    return ok(village_id=vid, status=decision)


@app.get("/api/admin/audit")
@roles("admin")
def admin_audit():
    cond, args = [], []
    for key, col in (("action", "a.action"), ("entity", "a.entity")):
        if request.args.get(key):
            cond.append(f"{col} LIKE ?")
            args.append(request.args[key] + "%")
    return ok(logs=qd("SELECT a.id,a.action,a.entity,a.entity_id,a.old_value,a.new_value,a.ip_address,a.created_at,"
                      " u.name user_name FROM audit_logs a LEFT JOIN users u ON u.id=a.user_id"
                      + (" WHERE " + " AND ".join(cond) if cond else "") + " ORDER BY a.id DESC LIMIT 200", args))


def csv_response(name, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        # neutralise spreadsheet formula injection
        w.writerow([("'" + str(c)) if isinstance(c, str) and c[:1] in "=+-@" else c for c in r])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@app.get("/api/admin/export/<kind>.csv")
@roles("admin")
def admin_export(kind):
    if kind == "projects":
        rows = q("SELECT p.id,v.name,p.name,p.category,p.department,p.contractor,p.allocated_budget,p.released_amount,"
                 "p.spent_amount,p.status,p.progress,p.start_date,p.expected_completion,p.actual_completion"
                 " FROM projects p JOIN villages v ON v.id=p.village_id ORDER BY p.id")
        return csv_response("projects.csv", ["ID", "Village", "Project", "Category", "Department", "Contractor",
                                             "Allocated", "Released", "Spent", "Status", "Progress %", "Start",
                                             "Expected completion", "Actual completion"], [tuple(r) for r in rows])
    if kind == "complaints":
        rows = q("SELECT c.code,v.name,c.category,c.title,c.status,c.created_at,c.resolved_at FROM complaints c"
                 " JOIN villages v ON v.id=c.village_id ORDER BY c.id")
        return csv_response("complaints.csv", ["Complaint ID", "Village", "Category", "Title", "Status", "Registered",
                                               "Resolved"], [tuple(r) for r in rows])
    if kind == "audit":
        rows = q("SELECT id,created_at,user_id,action,entity,entity_id,old_value,new_value,ip_address FROM audit_logs ORDER BY id")
        return csv_response("audit.csv", ["ID", "Time", "User", "Action", "Entity", "Entity ID", "Old", "New", "IP"],
                            [tuple(r) for r in rows])
    raise ApiError("Unknown export.", 404)


# --------------------------------------------------------------------------------------
# Demo seed data (only when the database is empty)
# --------------------------------------------------------------------------------------
def init_db(seed=True):
    with app.app_context():
        db = get_db()
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        db.commit()
        # Demo content is intentionally never created in deployed environments.
        maintenance(force=True)
        db.commit()


def seed_demo():
    t = now()
    demo_pw = generate_password_hash("Demo@1234")
    admin_pw = os.environ.get("GRAMSATARK_ADMIN_PASSWORD") or "Admin@12345"
    nav = x("INSERT INTO villages(name,district,pincode,created_at) VALUES('Navegaon (Demo)','Nagpur','441501',?)", (t,))
    par = x("INSERT INTO villages(name,district,pincode,created_at) VALUES('Pardi (Demo)','Nagpur','441108',?)", (t,))

    def user(name, email, phone, role, vid, pw=demo_pw):
        return x("INSERT INTO users(name,email,phone,password_hash,role,village_id,created_at) VALUES(?,?,?,?,?,?,?)",
                 (name, email, phone, pw, role, vid, t))

    admin = user("GramSatark Admin", "admin@gramsatark.in", "9000000000", "admin", None, generate_password_hash(admin_pw))
    reps = [user("Priya Deshmukh", "priya@demo.in", "9822000001", "representative", nav),
            user("Ramesh Kale", "ramesh@demo.in", "9822000002", "representative", nav),
            user("Sunita Wankhede", "sunita@demo.in", "9822000003", "representative", nav)]
    citizen = user("Anil Bhoyar", "citizen@demo.in", "9822000010", "citizen", nav)
    start, end = today() - timedelta(days=40), add_months(today() - timedelta(days=40), TERM_MONTHS)
    for r in reps:
        x("INSERT INTO representatives(user_id,village_id,term_start,term_end,status,created_at) VALUES(?,?,?,?,?,?)",
          (r, nav, start.isoformat(), end.isoformat(), "active", t))
    cand = [user(n, e, p, "citizen", par) for n, e, p in (
        ("Meena Raut", "meena@demo.in", "9822000021"), ("Suresh Ingle", "suresh@demo.in", "9822000022"),
        ("Kavita More", "kavita@demo.in", "9822000023"), ("Dinesh Pawar", "dinesh@demo.in", "9822000024"))]
    user("Pooja Gawande", "voter@demo.in", "9822000030", "citizen", par)
    el = x("INSERT INTO elections(village_id,start_date,end_date,status,created_at) VALUES(?,?,?,?,?)",
           (par, (today() - timedelta(days=2)).isoformat(), (today() + timedelta(days=12)).isoformat(), "open", t))
    for c, edu in zip(cand, ("B.A., Nagpur University", "B.Com.", "12th pass, Computer diploma", "B.Sc. Agriculture")):
        x("INSERT INTO representative_applications(user_id,village_id,education,address,identity_reference,declaration,"
          "status,verified_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (c, par, edu, "Pardi", "ID ending 4821", 1, "approved", admin, t))
        x("INSERT INTO candidates(election_id,user_id,approval_status) VALUES(?,?,?)", (el, c, "approved"))

    def proj(vid, name, cat, desc, dept, al, rel, sp, status, prog, contractor, s, e, by):
        pid = x("INSERT INTO projects(village_id,name,category,description,department,allocated_budget,released_amount,"
                "spent_amount,start_date,expected_completion,contractor,status,progress,created_by,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (vid, name, cat, desc, dept, al, rel, sp, s, e, contractor, status, prog, by, t))
        x("INSERT INTO project_updates(project_id,updated_by,old_status,new_status,progress,notes,created_at)"
          " VALUES(?,?,?,?,?,?,?)", (pid, by, None, "Planned", 0, "Project added to GramSatark.", t))
        if status != "Planned":
            x("INSERT INTO project_updates(project_id,updated_by,old_status,new_status,progress,notes,created_at)"
              " VALUES(?,?,?,?,?,?,?)", (pid, by, "Planned", status, prog, "Site visited and progress recorded.", t))
        return pid
    d = today()
    p1 = proj(nav, "Gram Sadak: Main road to market", "Road", "1.8 km cement concrete road from the bus stop to the weekly market.",
              "PMGSY / Zilla Parishad", 2400000, 1800000, 1500000, "Ongoing", 62, "Shree Infra Works",
              (d - timedelta(days=90)).isoformat(), (d + timedelta(days=60)).isoformat(), reps[0])
    proj(nav, "Overhead water tank and pipeline", "Water Supply", "50,000 litre tank and 3.2 km distribution pipeline.",
         "Jal Jeevan Mission", 3100000, 3100000, 3050000, "Completed", 100, "Vidarbha Aqua Pvt Ltd",
         (d - timedelta(days=240)).isoformat(), (d - timedelta(days=30)).isoformat(), reps[1])
    proj(nav, "Primary school classroom block", "School", "Two classrooms and a toilet block for ZP Primary School.",
         "Samagra Shiksha", 1500000, 600000, 590000, "Delayed", 35, "Kale Constructions",
         (d - timedelta(days=150)).isoformat(), (d - timedelta(days=10)).isoformat(), reps[0])
    proj(nav, "Health centre repair and painting", "Health Centre", "Roof repair, new beds and ramp access.",
         "Health Department", 450000, 100000, 0, "Pending", 0, "To be assigned",
         (d + timedelta(days=15)).isoformat(), (d + timedelta(days=120)).isoformat(), reps[2])
    proj(nav, "Covered drainage, ward 3", "Drainage", "420 m covered drain to stop monsoon waterlogging.",
         "Gram Panchayat fund", 800000, 800000, 410000, "Ongoing", 50, "Patil & Sons",
         (d - timedelta(days=70)).isoformat(), (d + timedelta(days=45)).isoformat(), reps[1])
    proj(nav, "Solar street lights", "Street Light", "36 solar LED lights along the main lanes.",
         "MEDA", 540000, 0, 0, "Planned", 0, None, (d + timedelta(days=30)).isoformat(), (d + timedelta(days=90)).isoformat(), reps[2])
    proj(par, "Anganwadi building", "School", "New building with kitchen and play area.", "WCD Department", 1200000, 700000,
         650000, "Ongoing", 55, "Sai Builders", (d - timedelta(days=80)).isoformat(), (d + timedelta(days=70)).isoformat(), admin)
    proj(par, "Village approach road repair", "Road", "Pothole repair and resurfacing of 2.4 km.", "PWD", 900000, 900000, 880000,
         "Completed", 100, "Rathod Roadways", (d - timedelta(days=200)).isoformat(), (d - timedelta(days=60)).isoformat(), admin)

    def complaint(code, title, cat, desc, loc, status, pid, notes):
        cid = x("INSERT INTO complaints(code,citizen_id,village_id,project_id,category,title,description,location,status,"
                "assigned_representative,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (code, citizen, nav, pid, cat, title, desc, loc, status, reps[0] if status != "Registered" else None, t))
        x("INSERT INTO complaint_updates(complaint_id,updated_by,status,notes,created_at) VALUES(?,?,?,?,?)",
          (cid, citizen, "Registered", "Complaint registered.", t))
        if status != "Registered":
            x("INSERT INTO complaint_updates(complaint_id,updated_by,status,notes,created_at) VALUES(?,?,?,?,?)",
              (cid, reps[0], status, notes, t))
    complaint(f"GS{d.year}-A1B2C3", "Cracks in the new road near the temple", "Road",
              "Cracks appeared within a month of laying the concrete.", "Near Hanuman temple", "In Progress", p1,
              "Inspected the stretch; contractor asked to repair under warranty.")
    complaint(f"GS{d.year}-D4E5F6", "No water supply in ward 2 for 4 days", "Water Supply",
              "Taps have been dry since Monday.", "Ward 2", "Resolved", None, "Valve was jammed; fixed and supply restored.")
    complaint(f"GS{d.year}-778899", "Street light not working on school lane", "Street Light",
              "Dark at night, unsafe for children.", "School lane", "Registered", None, "")
    x("INSERT INTO questions(citizen_id,village_id,project_id,question,answer,answered_by,created_at,answered_at)"
      " VALUES(?,?,?,?,?,?,?,?)", (citizen, nav, p1, "When will the market road be finished?",
                                    "The contractor has until the expected completion date shown on the project page. "
                                    "We will post updates after every inspection.", reps[0], t, t))
    x("INSERT INTO questions(citizen_id,village_id,project_id,question,created_at) VALUES(?,?,?,?,?)",
      (citizen, nav, None, "How much money did our Gram Panchayat receive this year?", t))
    audit("system.seed", "system", None, user_id=0)
    print("\n  Demo data created. Log in with (password for demo users: Demo@1234):")
    print("    Admin           admin@gramsatark.in  /  " + ("(your GRAMSATARK_ADMIN_PASSWORD)" if os.environ.get("GRAMSATARK_ADMIN_PASSWORD") else "Admin@12345  <- change this!"))
    print("    Representative  priya@demo.in")
    print("    Citizen         citizen@demo.in")
    print("    Voter (Pardi)   voter@demo.in\n")


# --------------------------------------------------------------------------------------
# Page routes (render YOUR html files from ./templates) + API route list
# --------------------------------------------------------------------------------------
from flask import redirect, render_template, url_for
from jinja2 import TemplateNotFound

TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
os.makedirs(TEMPLATE_DIR, exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "static"), exist_ok=True)


@app.context_processor
def _template_globals():
    """Available in every template: {{ user.name }}, {{ user.role }}, {{ villages }} ..."""
    u = cu()
    return {"user": user_public(u, True) if u else None, "villages": qd("SELECT id,name,district FROM villages ORDER BY name"),
            "project_statuses": PROJECT_STATUS, "categories": CATEGORIES, "complaint_statuses": COMPLAINT_STATUS}


def page(template, allowed=None, **ctx):
    """Render a template. `allowed` = roles that may open the page (None = public)."""
    u = cu()
    if allowed is not None and (not u or u["role"] not in allowed):
        return redirect("/login?next=" + request.path) if not u else Response("403 - Not allowed", 403)
    try:
        return render_template(template, **ctx)
    except TemplateNotFound:
        return Response(f"Template '{template}' not found. Create templates/{template}", 404)


@app.get("/")
def page_index():
    return page("v_index.html")


@app.get("/login")
def page_login():
    return page("v_login.html")


@app.get("/admin/login")
def page_admin_login():
    return page("v_admin_login.html")


@app.get("/register")
def page_register():
    return page("v_register.html")


@app.get("/setup")
def page_setup():
    return page("v_setup.html")


@app.get("/village-onboarding")
def page_village_onboarding():
    return page("v_village_onboarding.html")


@app.get("/representative/login")
def page_representative_login():
    return page("v_representative_login.html")


@app.get("/representative/onboarding")
def page_representative_onboarding():
    return page("v_representative_onboarding.html")


@app.get("/dashboard")
def page_dashboard():
    return page("v_citizen_full.html")


@app.get("/projects/<int:pid>")
def page_project(pid):
    return page("v_project.html", project_id=pid)


@app.get("/complaints")
def page_complaints():
    return page("v_complaints.html")


@app.get("/complaints/<int:cid>")
def page_complaint(cid):
    return page("v_complaint_detail.html", complaint_id=cid)


@app.get("/representative")
def page_representative():
    return page("v_rep_full.html", ("representative", "admin"))


@app.get("/election")
def page_election():
    return page("v_election.html")


@app.get("/admin")
def page_admin():
    return page("v_admin_full.html", ("admin",))


@app.get("/api/routes")
def api_routes():
    """Handy list of every API endpoint for the frontend developer."""
    rows = []
    for r in app.url_map.iter_rules():
        if r.rule.startswith("/api/"):
            rows.append({"path": r.rule, "methods": sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS"))})
    return ok(routes=sorted(rows, key=lambda r: r["path"]))


# Initialise at import so `gunicorn app:app` works too.
init_db()

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  GramSatark running at http://{host}:{port}\n  Database: {DB_PATH}\n")
    app.run(host=host, port=port, debug=os.environ.get("GRAMSATARK_DEBUG") == "1")
