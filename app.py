from __future__ import annotations

import csv
import io
import json
import os
import re
import smtplib
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from functools import wraps
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    Response,
    abort,
    send_from_directory,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import or_, text
from sqlalchemy.exc import IntegrityError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

try:
    from pypdf import PdfReader, PdfWriter
except ImportError:  # pragma: no cover
    PdfReader = PdfWriter = None


# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_ROOT = Path(os.getenv("UPLOAD_FOLDER", BASE_DIR / "uploads")).resolve()
CHECKOFF_FOLDER = UPLOAD_ROOT / "checkoff_slips"
CLIENT_FOLDER = UPLOAD_ROOT / "client_files"

CHECKOFF_FOLDER.mkdir(parents=True, exist_ok=True)
CLIENT_FOLDER.mkdir(parents=True, exist_ok=True)

ALLOWED_CHECKOFF_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "webp"}
ALLOWED_CLIENT_FILE_EXTENSIONS = {
    "pdf", "png", "jpg", "jpeg", "webp", "gif",
    "csv", "txt", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "zip",
}

STATUSES = [
    "Awaiting Diagnosis",
    "Picking up from MCPS",
    "In Repair",
    "Waiting on Parts",
    "Completed",
    "Delivering to MCPS",
    "Delivered to MCPS",
    "Shipped Back to MCPS",
    "Scrapped",
]

STATUS_BADGE_CLASSES = {
    "Awaiting Diagnosis": "customer-status-awaiting",
    "Picking up from MCPS": "customer-status-pickup",
    "In Repair": "customer-status-repair",
    "Waiting on Parts": "customer-status-parts",
    "Completed": "customer-status-complete",
    "Delivering to MCPS": "customer-status-delivering",
    "Delivered to MCPS": "customer-status-delivered",
    "Shipped Back to MCPS": "customer-status-shipped",
    "Scrapped": "customer-status-scrapped",
}


def normalize_database_url(url: str | None) -> str:
    if not url:
        return "sqlite:///repair_tracker_local.db"
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

app.secret_key = os.getenv("SECRET_KEY", "dev-only-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = normalize_database_url(os.getenv("DATABASE_URL"))
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 280}
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "25")) * 1024 * 1024

IS_DEBUG = os.getenv("FLASK_DEBUG") == "1"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() not in {"0", "false", "no"}
if IS_DEBUG:
    COOKIE_SECURE = False

app.config["SESSION_COOKIE_SECURE"] = COOKIE_SECURE
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
    minutes=int(os.getenv("SESSION_TIMEOUT_MINUTES", "120"))
)

app.config["WTF_CSRF_TIME_LIMIT"] = None  # tie CSRF lifetime to the session
csrf = CSRFProtect(app)

GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
BOOTSTRAP_ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
BOOTSTRAP_ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

# First-run client bootstrap, so MCPS can sign in without shell access.
BOOTSTRAP_CLIENT_COMPANY = os.getenv("BOOTSTRAP_CLIENT_COMPANY", "")
BOOTSTRAP_CLIENT_USERNAME = os.getenv("BOOTSTRAP_CLIENT_USERNAME", "")
BOOTSTRAP_CLIENT_PASSWORD = os.getenv("BOOTSTRAP_CLIENT_PASSWORD", "")

# Optional first-run Boxlight rep account.
BOOTSTRAP_BOXLIGHT_USERNAME = os.getenv("BOXLIGHT_USERNAME", "")
BOOTSTRAP_BOXLIGHT_PASSWORD = os.getenv("BOXLIGHT_PASSWORD", "")

db = SQLAlchemy(app)

# The landing page shows the real logo once static/pierson-logo.png exists,
# and a styled wordmark until then.
LOGO_PATH = BASE_DIR / "static" / "pierson-logo.png"

# EOD reports were built for the installer workflow, which is not in use.
# The models, routes and admin forms are all still here — flip this to True
# to bring the section back.
ENABLE_EOD_REPORTS = os.getenv("ENABLE_EOD_REPORTS", "false").lower() in {"1", "true", "yes"}


# ═══════════════════════════════════════════════════════════
# LOGIN THROTTLE (in-process; adequate for a single web instance)
# ═══════════════════════════════════════════════════════════

_ATTEMPTS: dict[str, list[float]] = defaultdict(list)
_ATTEMPTS_LOCK = threading.Lock()
MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "8"))
ATTEMPT_WINDOW = int(os.getenv("LOGIN_WINDOW_SECONDS", "900"))


def _throttle_key(scope: str) -> str:
    return f"{scope}:{request.remote_addr or 'unknown'}"


def is_throttled(scope: str) -> bool:
    key = _throttle_key(scope)
    now = time.time()
    with _ATTEMPTS_LOCK:
        recent = [t for t in _ATTEMPTS[key] if now - t < ATTEMPT_WINDOW]
        _ATTEMPTS[key] = recent
        return len(recent) >= MAX_ATTEMPTS


def record_failure(scope: str) -> None:
    with _ATTEMPTS_LOCK:
        _ATTEMPTS[_throttle_key(scope)].append(time.time())


def clear_failures(scope: str) -> None:
    with _ATTEMPTS_LOCK:
        _ATTEMPTS.pop(_throttle_key(scope), None)


# ═══════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════

class RowLikeMixin:
    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class User(db.Model, RowLikeMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(40), nullable=False, default="admin")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class ClientAccount(db.Model, RowLikeMixin):
    __tablename__ = "client_accounts"

    id = db.Column(db.Integer, primary_key=True)
    company = db.Column(db.String(120), nullable=False)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    contact_name = db.Column(db.String(120), default="")
    email = db.Column(db.String(120), default="")
    phone = db.Column(db.String(40), default="")
    notes = db.Column(db.Text, default="")
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    schedules = db.relationship("ClientSchedule", backref="client", lazy=True, cascade="all,delete-orphan")
    eod_reports = db.relationship("ClientEOD", backref="client", lazy=True, cascade="all,delete-orphan")
    files = db.relationship("ClientFile", backref="client", lazy=True, cascade="all,delete-orphan")

    def set_password(self, pw: str) -> None:
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        return check_password_hash(self.password_hash, pw)


class BoxlightAccount(db.Model, RowLikeMixin):
    __tablename__ = "boxlight_accounts"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    contact_name = db.Column(db.String(120), default="")
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, pw: str) -> None:
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        return check_password_hash(self.password_hash, pw)


class ReplacementPanel(db.Model, RowLikeMixin):
    __tablename__ = "replacement_panels"

    id = db.Column(db.Integer, primary_key=True)
    brand = db.Column(db.String(120), default="Boxlight")
    model = db.Column(db.String(160), default="")
    serial_number = db.Column(db.String(160), unique=True, nullable=False, index=True)
    date_received = db.Column(db.String(20), default="")
    status = db.Column(db.String(40), nullable=False, default="Available")
    used_for_unit_id = db.Column(db.Integer, db.ForeignKey("units.id"), nullable=True, index=True)
    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    used_for_unit = db.relationship("Unit", foreign_keys=[used_for_unit_id], backref="replacement_panel")


class Unit(db.Model, RowLikeMixin):
    __tablename__ = "units"

    id = db.Column(db.Integer, primary_key=True)
    intake_id = db.Column(db.String(120), unique=True, nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey("client_accounts.id"), index=True, nullable=True)
    brand = db.Column(db.String(120), default="")
    model = db.Column(db.String(160), default="")
    serial_number = db.Column(db.String(160), default="")
    screen_size = db.Column(db.String(80), default="")
    source = db.Column(db.String(160), default="")
    date_received = db.Column(db.String(20), default="")
    status = db.Column(db.String(80), nullable=False, default="Awaiting Diagnosis")
    reported_issue = db.Column(db.Text, default="")
    final_outcome = db.Column(db.Text, default="")
    repaired_date = db.Column(db.String(20), default="")
    delivery_date = db.Column(db.String(20), default="")
    checkoff_file = db.Column(db.String(255), default="")
    checkoff_uploaded_at = db.Column(db.String(40), default="")
    shipped_back_mcps = db.Column(db.Boolean, default=False)
    shipped_back_date = db.Column(db.String(20), default="")
    is_deleted = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    notes = db.relationship("RepairNote", backref="unit", lazy=True, cascade="all, delete-orphan")
    client = db.relationship("ClientAccount", backref="units", lazy=True)

    @property
    def badge_class(self) -> str:
        return STATUS_BADGE_CLASSES.get(self.status, "customer-status-default")

    @property
    def checkoff_status(self) -> str:
        return "Uploaded" if self.checkoff_file else "Not Uploaded"

    @property
    def checkoff_ext(self) -> str:
        if not self.checkoff_file or "." not in self.checkoff_file:
            return ""
        return self.checkoff_file.rsplit(".", 1)[1].lower()

    @property
    def checkoff_is_pdf(self) -> bool:
        return self.checkoff_ext == "pdf"

    @property
    def checkoff_is_image(self) -> bool:
        return self.checkoff_ext in {"png", "jpg", "jpeg", "webp", "gif"}


class RepairNote(db.Model, RowLikeMixin):
    __tablename__ = "repair_notes"

    id = db.Column(db.Integer, primary_key=True)
    unit_id = db.Column(db.Integer, db.ForeignKey("units.id"), nullable=False)
    note_text = db.Column(db.Text, nullable=False)
    technician = db.Column(db.String(120), default="")
    # Internal by default — nothing reaches the client portal unless opted in.
    is_internal = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class EmailSettings(db.Model, RowLikeMixin):
    __tablename__ = "email_settings"

    id = db.Column(db.Integer, primary_key=True)
    recipients = db.Column(db.Text, default="")
    frequency = db.Column(db.String(20), default="monthly")
    include_active = db.Column(db.Boolean, default=True)
    include_archived = db.Column(db.Boolean, default=False)
    last_sent = db.Column(db.String(40), default="")
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ClientSchedule(db.Model, RowLikeMixin):
    __tablename__ = "client_schedules"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("client_accounts.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    date = db.Column(db.String(20), nullable=False)
    time = db.Column(db.String(20), default="")
    location = db.Column(db.String(200), default="")
    technician = db.Column(db.String(100), default="")
    notes = db.Column(db.Text, default="")
    status = db.Column(db.String(40), default="Scheduled")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ClientEOD(db.Model, RowLikeMixin):
    __tablename__ = "client_eod_reports"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("client_accounts.id"), nullable=False)
    report_date = db.Column(db.String(20), nullable=False)
    technician = db.Column(db.String(100), default="")
    work_completed = db.Column(db.Text, default="")
    issues = db.Column(db.Text, default="")
    next_steps = db.Column(db.Text, default="")
    hours = db.Column(db.String(10), default="")
    sharepoint_url = db.Column(db.String(500), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ClientFile(db.Model, RowLikeMixin):
    __tablename__ = "client_files"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("client_accounts.id"), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    label = db.Column(db.String(200), default="")
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)


# ═══════════════════════════════════════════════════════════
# TEMPLATE HELPERS
# ═══════════════════════════════════════════════════════════

@app.template_filter("dt")
def format_datetime(value):
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


@app.template_filter("dash")
def dash(value):
    return value if value not in {None, ""} else "—"


@app.template_filter("nicedate")
def nicedate(value):
    """Dates are stored as free text, so normalise common formats on display."""
    if not value:
        return "—"
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            d = datetime.strptime(raw, fmt)
            # %-d is not portable to Windows, so strip the zero manually.
            return f"{d.strftime('%b')} {d.day}, {d.year}"
        except (ValueError, TypeError):
            continue
    return raw


@app.template_filter("screensize")
def screensize(value):
    """A bare number means inches — show it that way."""
    if not value:
        return ""
    raw = str(value).strip()
    if raw.replace(".", "", 1).isdigit():
        return f'{raw}"'
    return raw


def current_user() -> User | None:
    user_id = session.get("admin_user_id")
    if not user_id:
        return None
    return db.session.get(User, user_id)


def current_client() -> ClientAccount | None:
    client_id = session.get("client_portal_id")
    if not client_id:
        return None
    client = db.session.get(ClientAccount, client_id)
    if client and not client.active:
        return None
    return client


def current_boxlight() -> BoxlightAccount | None:
    account_id = session.get("boxlight_account_id")
    if not account_id:
        return None
    account = db.session.get(BoxlightAccount, account_id)
    if account and not account.active:
        return None
    return account


def upcoming_deliveries(client_id: int, limit: int | None = None):
    """Panels heading back to the customer, newest delivery date first.

    Derived from the repair records themselves — nothing to enter twice.
    Covers units actively on the way out, plus any unit with a delivery
    date recorded that has not been marked delivered yet.
    """
    query = Unit.query.filter(
        Unit.client_id == client_id,
        Unit.is_deleted.is_(False),
        Unit.status.in_(["Delivering to MCPS", "Completed"]),
    )
    units = query.all()

    # Sort by delivery date when present; undated entries fall to the end.
    def sort_key(u):
        return (u.delivery_date == "", u.delivery_date or "")

    units.sort(key=sort_key)
    return units[:limit] if limit else units


# Where each status sits in the repair journey, used for the progress
# indicator on a repair and for grouping the overview breakdown.
REPAIR_STAGES = [
    ("Received",  ["Awaiting Diagnosis", "Picking up from MCPS"]),
    ("In repair", ["In Repair", "Waiting on Parts"]),
    ("Completed", ["Completed"]),
    ("Returned",  ["Delivering to MCPS", "Delivered to MCPS", "Shipped Back to MCPS"]),
]

STAGE_COLORS = {
    "Received":  "#EF9F27",
    "In repair": "#378ADD",
    "Completed": "#639922",
    "Returned":  "#1D9E75",
    "Scrapped":  "#E24B4A",
}


def stage_for_status(status: str) -> str:
    for name, statuses in REPAIR_STAGES:
        if status in statuses:
            return name
    return "Scrapped" if status == "Scrapped" else "Received"


def stage_index(status: str) -> int:
    """0-based position in REPAIR_STAGES, or -1 for scrapped units."""
    if status == "Scrapped":
        return -1
    for i, (_, statuses) in enumerate(REPAIR_STAGES):
        if status in statuses:
            return i
    return 0


def client_repair_stats(client_id: int) -> dict:
    """Status mix and turnaround, computed from the client's own records."""
    units = Unit.query.filter_by(client_id=client_id, is_deleted=False).all()
    total = len(units)

    counts: dict[str, int] = {}
    for u in units:
        key = stage_for_status(u.status)
        counts[key] = counts.get(key, 0) + 1

    # Pipeline view: how far the batch has moved through the stages.
    stage_names = [name for name, _ in REPAIR_STAGES]
    last = len(stage_names) - 1
    live = [u for u in units if u.status != "Scrapped"]

    pipeline = [
        {
            "label": name,
            "count": sum(1 for u in live if stage_for_status(u.status) == name),
            "reached": sum(1 for u in live if stage_index(u.status) >= i),
            "color": STAGE_COLORS[name],
        }
        for i, name in enumerate(stage_names)
    ]

    # Weighted progress: a panel at the final stage counts fully, one at the
    # start counts nothing, and the batch average drives the fill.
    if live and last:
        progress = round(
            100 * sum(max(stage_index(u.status), 0) for u in live) / (len(live) * last)
        )
    else:
        progress = 100 if live else 0

    scrapped = sum(1 for u in units if u.status == "Scrapped")

    order = [name for name, _ in REPAIR_STAGES] + ["Scrapped"]
    breakdown = [
        {
            "label": name,
            "count": counts.get(name, 0),
            "pct": round(100 * counts.get(name, 0) / total, 1) if total else 0,
            "color": STAGE_COLORS[name],
        }
        for name in order if counts.get(name, 0)
    ]

    # Turnaround: days from received to returned, for units where both are known.
    spans = []
    for u in units:
        if not u.date_received or not u.shipped_back_date:
            continue
        for fmt_a in ("%Y-%m-%d", "%m/%d/%Y"):
            try:
                a = datetime.strptime(u.date_received.strip(), fmt_a)
                break
            except ValueError:
                a = None
        if a is None:
            continue
        for fmt_b in ("%Y-%m-%d", "%m/%d/%Y"):
            try:
                b = datetime.strptime(u.shipped_back_date.strip(), fmt_b)
                break
            except ValueError:
                b = None
        if b is None or b < a:
            continue
        spans.append((b - a).days)

    return {
        "total": total,
        "breakdown": breakdown,
        "pipeline": pipeline,
        "progress": progress,
        "scrapped": scrapped,
        "live": len(live),
        "returned": counts.get("Returned", 0),
        "active": counts.get("Received", 0) + counts.get("In repair", 0),
        "avg_turnaround": round(sum(spans) / len(spans)) if spans else None,
        "repaired": total - scrapped,
        "repair_rate": round(100 * (total - scrapped) / total) if total else None,
        "fastest": min(spans) if spans else None,
        "measured": len(spans),
    }


def upcoming_pickups(client_id: int, limit: int | None = None):
    """Panels booked for collection from the customer, so they can prepare.

    Derived from repair status — no separate schedule to maintain.
    """
    units = Unit.query.filter(
        Unit.client_id == client_id,
        Unit.is_deleted.is_(False),
        Unit.status == "Picking up from MCPS",
    ).all()

    def sort_key(u):
        return (u.date_received == "", u.date_received or "")

    units.sort(key=sort_key)
    return units[:limit] if limit else units


def client_section_counts() -> dict[str, int]:
    """Empty portal sections are hidden rather than shown as dead links."""
    client = current_client()
    if client is None:
        return {"schedule": 0, "eod": 0, "files": 0, "repairs": 0}
    return {
        "schedule": len(upcoming_deliveries(client.id)),
        "pickups": len(upcoming_pickups(client.id)),
        "eod": (ClientEOD.query.filter_by(client_id=client.id).count()
                if ENABLE_EOD_REPORTS else 0),
        "files": ClientFile.query.filter_by(client_id=client.id).count(),
        "repairs": Unit.query.filter_by(client_id=client.id, is_deleted=False).count(),
    }


@app.context_processor
def inject_globals():
    return {
        "current_user": current_user(),
        "current_client": current_client(),
        "current_boxlight": current_boxlight(),
        "STATUSES": STATUSES,
        "STATUS_BADGE_CLASSES": STATUS_BADGE_CLASSES,
        "logo_available": LOGO_PATH.exists(),
        "cp_counts": client_section_counts(),
        "eod_enabled": ENABLE_EOD_REPORTS,
        "REPAIR_STAGES": REPAIR_STAGES,
        "stage_index": stage_index,
    }


@app.before_request
def refresh_session_timeout():
    session.permanent = True
    session.modified = True


# ═══════════════════════════════════════════════════════════
# AUTH DECORATORS
#
# Admin and client sessions live in separate keys and never touch
# each other. Hitting an admin URL as a client no longer destroys
# the client session, and vice versa. That was the root cause of
# the old redirect loops.
# ═══════════════════════════════════════════════════════════

def admin_login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("admin_user_id") or current_user() is None:
            session.pop("admin_user_id", None)
            session.pop("admin_role", None)
            return redirect(url_for("login", next=request.full_path))
        return view_func(*args, **kwargs)
    return wrapped


def client_login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if current_client() is None:
            session.pop("client_portal_id", None)
            session.pop("client_portal_company", None)
            return redirect(url_for("cp_login", next=request.full_path))
        return view_func(*args, **kwargs)
    return wrapped


def boxlight_login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if current_boxlight() is None:
            session.pop("boxlight_account_id", None)
            return redirect(url_for("boxlight_login", next=request.full_path))
        return view_func(*args, **kwargs)
    return wrapped


def safe_next(target: str | None, fallback_endpoint: str) -> str:
    """Only allow same-site relative redirects."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for(fallback_endpoint)


# ═══════════════════════════════════════════════════════════
# DB INIT + LIGHTWEIGHT MIGRATION
# ═══════════════════════════════════════════════════════════

def _column_exists(table: str, column: str) -> bool:
    try:
        insp = db.inspect(db.engine)
        return column in {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return False


def run_migrations() -> None:
    """Add columns that db.create_all() cannot add to already-existing tables."""
    statements = []
    if not _column_exists("units", "client_id"):
        statements.append("ALTER TABLE units ADD COLUMN client_id INTEGER")
    if not _column_exists("repair_notes", "is_internal"):
        default = "TRUE" if db.engine.dialect.name == "postgresql" else "1"
        statements.append(
            f"ALTER TABLE repair_notes ADD COLUMN is_internal BOOLEAN NOT NULL DEFAULT {default}"
        )
    for stmt in statements:
        try:
            db.session.execute(text(stmt))
            db.session.commit()
        except Exception:
            db.session.rollback()


def sync_postgres_sequences() -> None:
    """Keep PostgreSQL auto-increment sequences ahead of imported IDs.

    The original SQLite migration inserted explicit primary-key values. PostgreSQL
    sequences do not automatically advance when that happens, so a later INSERT
    can fail with a duplicate *id* even though the Intake ID is brand new.
    """
    if db.engine.dialect.name != "postgresql":
        return

    for table_name in ("units", "repair_notes", "replacement_panels", "boxlight_accounts"):
        try:
            db.session.execute(text(f"""
                SELECT setval(
                    pg_get_serial_sequence('{table_name}', 'id'),
                    COALESCE((SELECT MAX(id) FROM {table_name}), 1),
                    (SELECT COUNT(*) > 0 FROM {table_name})
                )
            """))
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Could not synchronize sequence for %s", table_name)


def backfill_unit_clients() -> None:
    """Attach orphaned units to the only client, if there is exactly one."""
    if not Unit.query.filter(Unit.client_id.is_(None)).count():
        return
    clients = ClientAccount.query.all()
    if len(clients) != 1:
        return
    Unit.query.filter(Unit.client_id.is_(None)).update(
        {Unit.client_id: clients[0].id}, synchronize_session=False
    )
    db.session.commit()


def init_database() -> None:
    with app.app_context():
        db.create_all()
        run_migrations()
        sync_postgres_sequences()

        if BOOTSTRAP_ADMIN_USERNAME and BOOTSTRAP_ADMIN_PASSWORD:
            if not find_user_by_username(BOOTSTRAP_ADMIN_USERNAME):
                user = User(username=BOOTSTRAP_ADMIN_USERNAME, role="admin")
                user.set_password(BOOTSTRAP_ADMIN_PASSWORD)
                db.session.add(user)
                db.session.commit()

        if BOOTSTRAP_CLIENT_USERNAME and BOOTSTRAP_CLIENT_PASSWORD:
            if not find_client_by_username(BOOTSTRAP_CLIENT_USERNAME, active_only=False):
                client = ClientAccount(
                    company=BOOTSTRAP_CLIENT_COMPANY or BOOTSTRAP_CLIENT_USERNAME,
                    username=BOOTSTRAP_CLIENT_USERNAME,
                )
                client.set_password(BOOTSTRAP_CLIENT_PASSWORD)
                db.session.add(client)
                db.session.commit()

        if BOOTSTRAP_BOXLIGHT_USERNAME and BOOTSTRAP_BOXLIGHT_PASSWORD:
            existing = BoxlightAccount.query.filter(
                db.func.lower(BoxlightAccount.username) == BOOTSTRAP_BOXLIGHT_USERNAME.strip().lower()
            ).first()
            if not existing:
                rep = BoxlightAccount(username=BOOTSTRAP_BOXLIGHT_USERNAME.strip())
                rep.set_password(BOOTSTRAP_BOXLIGHT_PASSWORD)
                db.session.add(rep)
                db.session.commit()

        backfill_unit_clients()


# ═══════════════════════════════════════════════════════════
# SHARED HELPERS
# ═══════════════════════════════════════════════════════════

def validate_date(date_text: str) -> bool:
    if not date_text:
        return True
    try:
        datetime.strptime(date_text, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _ext_ok(filename: str, allowed: set[str]) -> bool:
    if not filename or "." not in filename:
        return False
    return filename.rsplit(".", 1)[1].lower() in allowed


def generate_next_intake_id() -> str:
    year = datetime.now().year
    prefix = f"BX-{year}-"
    highest = 0
    for unit in Unit.query.filter(Unit.intake_id.like(f"{prefix}%")).all():
        try:
            highest = max(highest, int(unit.intake_id.rsplit("-", 1)[-1]))
        except (ValueError, IndexError):
            continue
    return f"{prefix}{highest + 1:04d}"


def get_dashboard_counts() -> dict[str, int]:
    counts = {s: Unit.query.filter_by(status=s, is_deleted=False).count() for s in STATUSES}
    counts["Total"] = Unit.query.filter_by(is_deleted=False).count()
    counts["Archived"] = Unit.query.filter_by(is_deleted=True).count()
    return counts


def apply_status_side_effects(unit: Unit, status: str) -> None:
    unit.status = status
    if status in {"Delivered to MCPS", "Shipped Back to MCPS"}:
        unit.shipped_back_mcps = True
        if not unit.shipped_back_date:
            unit.shipped_back_date = datetime.now().strftime("%Y-%m-%d")
    else:
        unit.shipped_back_mcps = False
        unit.shipped_back_date = ""


def get_active_unit(unit_id: int) -> Unit | None:
    return Unit.query.filter_by(id=unit_id, is_deleted=False).first()


def get_client_unit(client_id: int, unit_id: int) -> Unit | None:
    """Scoped lookup — a client can only ever reach their own units."""
    return Unit.query.filter_by(id=unit_id, client_id=client_id, is_deleted=False).first()


def find_client_by_username(username: str, active_only: bool = True):
    """Usernames are stored with the capitalisation you choose, but matched
    case-insensitively — so MCPS, mcps and Mcps all reach the same account."""
    if not username:
        return None
    query = ClientAccount.query.filter(
        db.func.lower(ClientAccount.username) == username.strip().lower()
    )
    if active_only:
        query = query.filter(ClientAccount.active.is_(True))
    return query.first()


def find_user_by_username(username: str):
    if not username:
        return None
    return User.query.filter(
        db.func.lower(User.username) == username.strip().lower()
    ).first()


def parse_client_id(raw: str) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        cid = int(raw)
    except ValueError:
        return None
    return cid if db.session.get(ClientAccount, cid) else None


# ═══════════════════════════════════════════════════════════
# BOXLIGHT SUPPORT EMAIL IMPORT
# ═══════════════════════════════════════════════════════════

MODEL_SN_RE = re.compile(
    r"(?P<model>(?:MIMIOPRO|MIMIODS|PROCOLOR)\s+\d+[A-Z]?)\s+SN\s*:\s*(?P<serial>[A-Z0-9-]+)",
    re.IGNORECASE,
)
INVOICE_RE = re.compile(
    r"(?P<invoice>INV\d+)\s*,\s*(?P<purchase_date>\d{1,2}/\d{1,2}/\d{2,4})(?:\s*-\s*(?P<flag>.*))?",
    re.IGNORECASE,
)
ISSUE_RE = re.compile(r"^ISSUE\s*[-:]\s*(?P<issue>.+)$", re.IGNORECASE)
LOOSE_SERIAL_RE = re.compile(r"^(?P<prefix>.*?)(?P<serial>\d{12,16})\s+(?P<issue>.+)$")
SCHOOL_RE = re.compile(
    r"\b(?:ES|MS|HS|Elementary(?: School)?|Middle(?: School)?|High(?: School)?)\b",
    re.IGNORECASE,
)


def infer_screen_size(model: str) -> str:
    """The Mimio/ProColor 75x models in these support emails are 75-inch panels."""
    match = re.search(r"\b(75\d)\b", model or "")
    return "75" if match else ""


def parse_boxlight_support_email(raw_text: str) -> list[dict[str, str]]:
    """Parse the plain-text body of a Boxlight/Zendesk support email.

    The parser is intentionally conservative: anything incomplete is preserved and
    flagged for review instead of inventing missing panel information.
    """
    text_value = (raw_text or "").replace("\xa0", " ").replace("\r", "")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text_value.split("\n")]
    lines = [line for line in lines if line]

    records: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    current_location = ""

    def finish_current() -> None:
        nonlocal current
        if not current:
            return
        current.setdefault("model", "")
        current.setdefault("serial_number", "")
        current.setdefault("invoice", "")
        current.setdefault("location", current_location)
        current.setdefault("issue", "")
        current.setdefault("flag", "")
        current["brand"] = (current["model"].split()[0] if current["model"] else "")
        current["screen_size"] = infer_screen_size(current["model"])
        current["needs_review"] = "yes" if not current["model"] or not current["serial_number"] else ""
        records.append(current)
        current = None

    for line in lines:
        # Ignore mail headers / markdown wrappers from copied email clients.
        if line.lower().startswith(("from:", "date:", "subject:", "to:")):
            continue
        if "mailto:" in line.lower() and "support@" in line.lower():
            continue

        model_match = MODEL_SN_RE.search(line)
        if model_match:
            finish_current()
            current = {
                "model": model_match.group("model").upper(),
                "serial_number": model_match.group("serial").strip(),
                "location": current_location,
                "issue": "",
                "invoice": "",
                "flag": "",
            }
            # Preserve meaningful text appearing before the model as a location.
            before = line[:model_match.start()].strip(" -–—")
            if before and SCHOOL_RE.search(before):
                current_location = before
                current["location"] = before
            continue

        invoice_match = INVOICE_RE.search(line)
        if invoice_match and current:
            current["invoice"] = invoice_match.group("invoice").upper()
            current["flag"] = (invoice_match.group("flag") or "").strip()
            continue

        issue_match = ISSUE_RE.match(line)
        if issue_match and current:
            current["issue"] = issue_match.group("issue").strip()
            continue

        # A standalone school/location heading starts a new location block.
        # Finish the prior panel first so a heading that appears AFTER a panel
        # does not get incorrectly attached to that previous record.
        if SCHOOL_RE.search(line) and not re.search(r"\d{12,16}", line):
            finish_current()
            current_location = line.strip(" -–—")
            continue

        loose = LOOSE_SERIAL_RE.match(line)
        if loose and "SN:" not in line.upper():
            finish_current()
            prefix = loose.group("prefix").strip(" -–—")
            if prefix and SCHOOL_RE.search(prefix):
                current_location = prefix
            current = {
                "model": "",
                "serial_number": loose.group("serial"),
                "location": current_location,
                "issue": loose.group("issue").strip(" -–—"),
                "invoice": "",
                "flag": "",
            }
            continue

        # Continuation text after an Issue line stays attached to that panel.
        if current and line and not line.startswith("http"):
            if current.get("issue"):
                current["issue"] = f"{current['issue']} {line}".strip()
            elif line.upper().startswith("ISSUE"):
                current["issue"] = re.sub(r"^ISSUE\s*[-:]?\s*", "", line, flags=re.I).strip()

    finish_current()
    return records


# ═══════════════════════════════════════════════════════════
# PUBLIC / ERRORS
# ═══════════════════════════════════════════════════════════

@app.route("/health")
def health():
    return {"status": "ok", "app": "pierson-repairs"}, 200


@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", code=404,
                           message="That page could not be found."), 404


@app.errorhandler(413)
def too_large(_):
    return render_template("error.html", code=413,
                           message="That file is too large to upload."), 413


@app.errorhandler(500)
def server_error(_):
    db.session.rollback()
    return render_template("error.html", code=500,
                           message="Something went wrong on our end."), 500


# ═══════════════════════════════════════════════════════════
# ADMIN AUTH
# ═══════════════════════════════════════════════════════════

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET" and current_user():
        return redirect(safe_next(request.args.get("next"), "index"))

    if request.method == "POST":
        if is_throttled("admin"):
            flash("Too many failed attempts. Please wait and try again.", "danger")
            return render_template("login.html"), 429

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = find_user_by_username(username)

        if user and user.check_password(password):
            clear_failures("admin")
            session["admin_user_id"] = user.id
            session["admin_role"] = user.role
            session.permanent = True
            return redirect(safe_next(request.args.get("next"), "index"))

        record_failure("admin")

        # A client typing their credentials into the staff form is the most
        # common failure here — point them at the right door instead of
        # leaving them stuck on "invalid password".
        if find_client_by_username(username):
            flash("That looks like a client account. Please use the client portal "
                  "sign-in instead.", "warning")
            return redirect(url_for("cp_login"))

        flash("Invalid username or password.", "danger")

    return render_template("login.html")


@app.route("/account", methods=["GET", "POST"])
@admin_login_required
def admin_account():
    user = current_user()

    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        if not user.check_password(current):
            record_failure("admin")
            flash("Your current password is not correct.", "danger")
            return redirect(url_for("admin_account"))

        error = validate_new_password(new, confirm)
        if error:
            flash(error, "danger")
            return redirect(url_for("admin_account"))

        if user.check_password(new):
            flash("That is already your current password.", "danger")
            return redirect(url_for("admin_account"))

        user.set_password(new)
        db.session.commit()
        flash("Your password has been changed.", "success")
        return redirect(url_for("admin_account"))

    return render_template("account.html", user=user, min_length=MIN_PASSWORD_LENGTH)


@app.route("/logout")
def logout():
    session.pop("admin_user_id", None)
    session.pop("admin_role", None)
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


# ═══════════════════════════════════════════════════════════
# ADMIN — REPAIR TRACKER
# ═══════════════════════════════════════════════════════════

@app.route("/dashboard")
@admin_login_required
def index():
    search = request.args.get("search", "").strip()
    status_filter = request.args.get("status", "").strip()
    client_filter = request.args.get("client", "").strip()

    query = Unit.query.filter_by(is_deleted=False)

    if search:
        like = f"%{search}%"
        query = query.filter(or_(
            Unit.intake_id.ilike(like),
            Unit.brand.ilike(like),
            Unit.model.ilike(like),
            Unit.serial_number.ilike(like),
            Unit.reported_issue.ilike(like),
            Unit.source.ilike(like),
        ))

    if status_filter:
        query = query.filter_by(status=status_filter)

    if client_filter:
        try:
            query = query.filter_by(client_id=int(client_filter))
        except ValueError:
            pass

    return render_template(
        "index.html",
        units=query.order_by(Unit.id.desc()).all(),
        search=search,
        status_filter=status_filter,
        client_filter=client_filter,
        clients=ClientAccount.query.order_by(ClientAccount.company.asc()).all(),
        counts=get_dashboard_counts(),
        next_intake_id=generate_next_intake_id(),
    )


@app.route("/import-email", methods=["GET", "POST"])
@admin_login_required
def import_email():
    raw_email = request.form.get("raw_email", "") if request.method == "POST" else ""
    records = parse_boxlight_support_email(raw_email) if raw_email.strip() else []
    return render_template(
        "email_import.html",
        raw_email=raw_email,
        records=records,
        clients=ClientAccount.query.order_by(ClientAccount.company.asc()).all(),
        today=datetime.now().strftime("%Y-%m-%d"),
    )


@app.route("/import-email/commit", methods=["POST"])
@admin_login_required
def import_email_commit():
    client_id = parse_client_id(request.form.get("client_id", ""))
    date_received = request.form.get("date_received", "").strip()
    status = request.form.get("status", "Awaiting Diagnosis").strip()

    if date_received and not validate_date(date_received):
        flash("Date Received must be in YYYY-MM-DD format.", "danger")
        return redirect(url_for("import_email"))
    if status not in STATUSES:
        status = "Awaiting Diagnosis"

    models = request.form.getlist("model")
    serials = request.form.getlist("serial_number")
    brands = request.form.getlist("brand")
    screens = request.form.getlist("screen_size")
    invoices = request.form.getlist("invoice")
    locations = request.form.getlist("location")
    issues = request.form.getlist("issue")
    flags = request.form.getlist("flag")
    selected = {int(v) for v in request.form.getlist("include") if v.isdigit()}

    imported = 0
    skipped = 0
    errors: list[str] = []

    for i in range(len(serials)):
        if i not in selected:
            continue

        serial_number = (serials[i] if i < len(serials) else "").strip()
        model = (models[i] if i < len(models) else "").strip()
        if not serial_number or not model:
            skipped += 1
            errors.append(f"Row {i + 1}: model or serial number is missing; review it manually.")
            continue

        # Avoid accidentally importing the same physical panel twice.
        if Unit.query.filter(db.func.lower(Unit.serial_number) == serial_number.lower(), Unit.is_deleted.is_(False)).first():
            skipped += 1
            errors.append(f"{serial_number}: already exists in the tracker.")
            continue

        invoice = (invoices[i] if i < len(invoices) else "").strip()
        location = (locations[i] if i < len(locations) else "").strip()
        issue = (issues[i] if i < len(issues) else "").strip()
        flag = (flags[i] if i < len(flags) else "").strip()

        source_bits = ["Boxlight Support"]
        if invoice:
            source_bits.append(invoice)
        source = " | ".join(source_bits)[:160]

        issue_bits = []
        if location:
            issue_bits.append(location)
        if issue:
            issue_bits.append(issue)
        if flag:
            issue_bits.append(flag)

        unit = Unit(
            intake_id=generate_next_intake_id(),
            client_id=client_id,
            brand=(brands[i] if i < len(brands) else "").strip(),
            model=model,
            serial_number=serial_number,
            screen_size=(screens[i] if i < len(screens) else "").strip(),
            source=source,
            date_received=date_received,
            reported_issue=" — ".join(issue_bits),
        )
        apply_status_side_effects(unit, status)

        try:
            db.session.add(unit)
            db.session.commit()
            imported += 1
        except IntegrityError as exc:
            db.session.rollback()
            sync_postgres_sequences()
            skipped += 1
            errors.append(f"{serial_number}: database rejected the row ({getattr(exc, 'orig', exc)}).")

    if imported:
        flash(f"Imported {imported} panel{'s' if imported != 1 else ''} from the support email.", "success")
    if skipped:
        flash(f"Skipped {skipped} row{'s' if skipped != 1 else ''}. " + " ".join(errors[:4]), "warning")
    return redirect(url_for("index"))


@app.route("/archived")
@admin_login_required
def archived_units():
    units = Unit.query.filter_by(is_deleted=True).order_by(Unit.updated_at.desc()).all()
    return render_template("archived.html", units=units)


@app.route("/add", methods=["POST"])
@admin_login_required
def add_unit():
    intake_id = request.form.get("intake_id", "").strip() or generate_next_intake_id()
    model = request.form.get("model", "").strip()
    serial_number = request.form.get("serial_number", "").strip()
    date_received = request.form.get("date_received", "").strip()
    status = request.form.get("status", "Awaiting Diagnosis").strip()

    if not model and not serial_number:
        flash("Please enter at least a model or a serial number.", "danger")
        return redirect(url_for("index"))

    if not validate_date(date_received):
        flash("Date Received must be in YYYY-MM-DD format.", "danger")
        return redirect(url_for("index"))

    if status not in STATUSES:
        status = "Awaiting Diagnosis"

    unit = Unit(
        intake_id=intake_id,
        client_id=parse_client_id(request.form.get("client_id", "")),
        brand=request.form.get("brand", "").strip(),
        model=model,
        serial_number=serial_number,
        screen_size=request.form.get("screen_size", "").strip(),
        source=request.form.get("source", "").strip(),
        date_received=date_received,
        reported_issue=request.form.get("reported_issue", "").strip(),
    )
    apply_status_side_effects(unit, status)

    try:
        db.session.add(unit)
        db.session.commit()
        flash(f"Unit {intake_id} added successfully.", "success")
    except IntegrityError as exc:
        db.session.rollback()
        app.logger.exception("Database integrity error while adding unit %s", intake_id)
        detail = str(getattr(exc, "orig", exc)).lower()
        if "intake_id" in detail:
            flash(f"Could not add {intake_id} — that Intake ID already exists.", "danger")
        elif "units_pkey" in detail or "duplicate key value" in detail and "id" in detail:
            # This can happen after importing explicit IDs from SQLite. Repair the
            # sequence immediately so the next attempt succeeds.
            sync_postgres_sequences()
            flash("The database ID counter was out of sync after the old data migration. "
                  "I repaired it — please click Add Unit once more.", "warning")
        else:
            flash(f"Could not add that unit: {getattr(exc, 'orig', exc)}", "danger")
    except Exception as exc:
        db.session.rollback()
        app.logger.exception("Unexpected error while adding unit %s", intake_id)
        flash(f"Could not add that unit: {exc}", "danger")

    return redirect(url_for("index"))


@app.route("/unit/<int:unit_id>")
@admin_login_required
def unit_detail(unit_id: int):
    unit = get_active_unit(unit_id)
    if unit is None:
        flash("Unit not found.", "danger")
        return redirect(url_for("index"))
    notes = RepairNote.query.filter_by(unit_id=unit_id).order_by(RepairNote.id.desc()).all()
    return render_template("detail.html", unit=unit, notes=notes)


@app.route("/unit/<int:unit_id>/edit", methods=["GET", "POST"])
@admin_login_required
def edit_unit(unit_id: int):
    unit = get_active_unit(unit_id)
    if unit is None:
        flash("Unit not found.", "danger")
        return redirect(url_for("index"))

    if request.method == "POST":
        intake_id = request.form.get("intake_id", "").strip()
        date_received = request.form.get("date_received", "").strip()
        shipped_back_date = request.form.get("shipped_back_date", "").strip()
        repaired_date = request.form.get("repaired_date", "").strip()
        delivery_date = request.form.get("delivery_date", "").strip()
        status = request.form.get("status", "").strip()

        if not intake_id:
            flash("Intake ID is required.", "danger")
            return redirect(url_for("edit_unit", unit_id=unit_id))

        if not all(validate_date(v) for v in
                   [date_received, shipped_back_date, repaired_date, delivery_date]):
            flash("Dates must be in YYYY-MM-DD format.", "danger")
            return redirect(url_for("edit_unit", unit_id=unit_id))

        if status not in STATUSES:
            status = unit.status

        unit.intake_id = intake_id
        unit.client_id = parse_client_id(request.form.get("client_id", ""))
        unit.brand = request.form.get("brand", "").strip()
        unit.model = request.form.get("model", "").strip()
        unit.serial_number = request.form.get("serial_number", "").strip()
        unit.screen_size = request.form.get("screen_size", "").strip()
        unit.source = request.form.get("source", "").strip()
        unit.date_received = date_received
        unit.reported_issue = request.form.get("reported_issue", "").strip()
        unit.final_outcome = request.form.get("final_outcome", "").strip()
        unit.repaired_date = repaired_date
        unit.delivery_date = delivery_date
        apply_status_side_effects(unit, status)

        if shipped_back_date:
            unit.shipped_back_date = shipped_back_date

        try:
            db.session.commit()
            flash(f"Unit {intake_id} updated successfully.", "success")
            return redirect(url_for("unit_detail", unit_id=unit_id))
        except Exception:
            db.session.rollback()
            flash("Could not save that unit — the Intake ID may already exist.", "danger")

    return render_template(
        "edit_unit.html",
        unit=unit,
        clients=ClientAccount.query.order_by(ClientAccount.company.asc()).all(),
    )


@app.route("/unit/<int:unit_id>/add_note", methods=["POST"])
@admin_login_required
def add_note(unit_id: int):
    unit = get_active_unit(unit_id)
    if unit is None:
        flash("Unit not found.", "danger")
        return redirect(url_for("index"))

    note_text = request.form.get("note_text", "").strip()
    if not note_text:
        flash("Note text cannot be blank.", "danger")
        return redirect(url_for("unit_detail", unit_id=unit_id))

    technician = request.form.get("technician", "").strip()
    if not technician and current_user():
        technician = current_user().username

    db.session.add(RepairNote(
        unit_id=unit_id,
        note_text=note_text,
        technician=technician,
        is_internal="share_with_client" not in request.form,
    ))
    db.session.commit()
    flash("Repair note added.", "success")
    return redirect(url_for("unit_detail", unit_id=unit_id))


@app.route("/unit/<int:unit_id>/update_status", methods=["POST"])
@admin_login_required
def update_status(unit_id: int):
    unit = get_active_unit(unit_id)
    if unit is None:
        flash("Unit not found.", "danger")
        return redirect(url_for("index"))

    status = request.form.get("status", "").strip()
    if status not in STATUSES:
        flash("Invalid status selected.", "danger")
        return redirect(url_for("unit_detail", unit_id=unit_id))

    apply_status_side_effects(unit, status)
    db.session.commit()
    flash("Status updated.", "success")
    return redirect(url_for("unit_detail", unit_id=unit_id))


@app.route("/unit/<int:unit_id>/update_dates", methods=["POST"])
@admin_login_required
def update_dates(unit_id: int):
    unit = get_active_unit(unit_id)
    if unit is None:
        flash("Unit not found.", "danger")
        return redirect(url_for("index"))

    repaired_date = request.form.get("repaired_date", "").strip()
    delivery_date = request.form.get("delivery_date", "").strip()

    if not validate_date(repaired_date) or not validate_date(delivery_date):
        flash("Dates must be in YYYY-MM-DD format.", "danger")
        return redirect(url_for("unit_detail", unit_id=unit_id))

    unit.repaired_date = repaired_date
    unit.delivery_date = delivery_date
    db.session.commit()
    flash("Repair and delivery dates updated.", "success")
    return redirect(url_for("unit_detail", unit_id=unit_id))


@app.route("/unit/<int:unit_id>/upload_checkoff", methods=["POST"])
@admin_login_required
def upload_checkoff(unit_id: int):
    unit = get_active_unit(unit_id)
    if unit is None:
        flash("Unit not found.", "danger")
        return redirect(url_for("index"))

    uploaded_file = request.files.get("checkoff_file")
    if not uploaded_file or not uploaded_file.filename:
        flash("Please choose a check-off slip file to upload.", "danger")
        return redirect(url_for("unit_detail", unit_id=unit_id))

    if not _ext_ok(uploaded_file.filename, ALLOWED_CHECKOFF_EXTENSIONS):
        flash("Allowed file types are PDF, PNG, JPG, JPEG, and WEBP.", "danger")
        return redirect(url_for("unit_detail", unit_id=unit_id))

    extension = secure_filename(uploaded_file.filename).rsplit(".", 1)[1].lower()
    safe_intake_id = secure_filename(unit.intake_id or f"unit_{unit_id}")
    filename = f"{safe_intake_id}_checkoff.{extension}"

    if unit.checkoff_file and unit.checkoff_file != filename:
        (CHECKOFF_FOLDER / unit.checkoff_file).unlink(missing_ok=True)

    uploaded_file.save(CHECKOFF_FOLDER / filename)
    unit.checkoff_file = filename
    unit.checkoff_uploaded_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    db.session.commit()
    flash("Tech check-off slip uploaded.", "success")
    return redirect(url_for("unit_detail", unit_id=unit_id))


@app.route("/unit/<int:unit_id>/checkoff")
@admin_login_required
def view_checkoff(unit_id: int):
    unit = get_active_unit(unit_id)
    if unit is None or not unit.checkoff_file:
        flash("No check-off slip uploaded for this unit.", "danger")
        return redirect(url_for("unit_detail", unit_id=unit_id))

    filepath = CHECKOFF_FOLDER / unit.checkoff_file
    if not filepath.exists():
        flash("The uploaded check-off slip file could not be found.", "danger")
        return redirect(url_for("unit_detail", unit_id=unit_id))

    return send_file(filepath, as_attachment=False)


def rotate_checkoff_file(path: Path, degrees: int) -> tuple[bool, str]:
    """Rewrite the stored slip rotated clockwise by `degrees`."""
    ext = path.suffix.lower().lstrip(".")

    if ext == "pdf":
        if PdfReader is None:
            return False, "PDF rotation needs the pypdf package."
        try:
            reader = PdfReader(str(path))
            writer = PdfWriter()
            for page in reader.pages:
                page.rotate(degrees)
                writer.add_page(page)
            with open(path, "wb") as fh:
                writer.write(fh)
            return True, "Check-off slip rotated."
        except Exception as e:
            return False, f"Could not rotate that PDF: {e}"

    if ext in {"png", "jpg", "jpeg", "webp", "gif"}:
        if Image is None:
            return False, "Image rotation needs the Pillow package."
        try:
            with Image.open(path) as img:
                # PIL rotates counter-clockwise; negate for clockwise.
                rotated = img.rotate(-degrees, expand=True)
                rotated.save(path)
            return True, "Check-off slip rotated."
        except Exception as e:
            return False, f"Could not rotate that image: {e}"

    return False, "That file type cannot be rotated."


@app.route("/unit/<int:unit_id>/rotate_checkoff", methods=["POST"])
@admin_login_required
def rotate_checkoff(unit_id: int):
    unit = get_active_unit(unit_id)
    if unit is None or not unit.checkoff_file:
        flash("No check-off slip to rotate.", "danger")
        return redirect(url_for("unit_detail", unit_id=unit_id))

    try:
        degrees = int(request.form.get("degrees", "90"))
    except ValueError:
        degrees = 90
    if degrees not in {90, 180, 270}:
        degrees = 90

    filepath = CHECKOFF_FOLDER / unit.checkoff_file
    if not filepath.exists():
        flash("The uploaded check-off slip file could not be found.", "danger")
        return redirect(url_for("unit_detail", unit_id=unit_id))

    ok, message = rotate_checkoff_file(filepath, degrees)
    if ok:
        # Bust any cached copy in the client's browser.
        unit.checkoff_uploaded_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        db.session.commit()
    flash(message, "success" if ok else "danger")
    return redirect(url_for("unit_detail", unit_id=unit_id))


@app.route("/unit/<int:unit_id>/archive", methods=["POST"])
@admin_login_required
def archive_unit(unit_id: int):
    unit = get_active_unit(unit_id)
    if unit:
        unit.is_deleted = True
        db.session.commit()
        flash("Unit archived.", "success")
    return redirect(url_for("index"))


@app.route("/unit/<int:unit_id>/restore", methods=["POST"])
@admin_login_required
def restore_unit(unit_id: int):
    unit = Unit.query.filter_by(id=unit_id, is_deleted=True).first()
    if unit:
        unit.is_deleted = False
        db.session.commit()
        flash("Unit restored.", "success")
    return redirect(url_for("archived_units"))


@app.route("/unit/<int:unit_id>/packing-slip")
@admin_login_required
def packing_slip_for_unit(unit_id: int):
    unit = get_active_unit(unit_id)
    if unit is None:
        flash("Unit not found.", "danger")
        return redirect(url_for("index"))
    return render_template("packing_slip.html", unit=unit,
                           today=datetime.now().strftime("%Y-%m-%d"), portal="admin")


@app.route("/unit/<int:unit_id>/quick-status", methods=["POST"])
@admin_login_required
def quick_status_update(unit_id: int):
    unit = get_active_unit(unit_id)
    if unit is None:
        return {"ok": False, "error": "Unit not found"}, 404
    new_status = request.form.get("status", "").strip()
    if new_status not in STATUSES:
        return {"ok": False, "error": "Invalid status"}, 400
    apply_status_side_effects(unit, new_status)
    db.session.commit()
    return {"ok": True, "status": unit.status}


@app.route("/export/csv")
@admin_login_required
def export_csv():
    """Streamed from memory — no files left behind on the persistent disk."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "intake_id", "client", "brand", "model", "serial_number", "screen_size",
        "source", "date_received", "status", "reported_issue", "final_outcome",
        "repaired_date", "delivery_date", "checkoff_file", "checkoff_uploaded_at",
        "shipped_back_mcps", "shipped_back_date", "is_deleted", "created_at", "updated_at",
    ])
    for row in Unit.query.order_by(Unit.id.desc()).all():
        writer.writerow([
            row.id, row.intake_id, row.client.company if row.client else "",
            row.brand, row.model, row.serial_number, row.screen_size, row.source,
            row.date_received, row.status, row.reported_issue, row.final_outcome,
            row.repaired_date, row.delivery_date, row.checkoff_file,
            row.checkoff_uploaded_at, "Yes" if row.shipped_back_mcps else "No",
            row.shipped_back_date, "Yes" if row.is_deleted else "No",
            row.created_at, row.updated_at,
        ])

    filename = f"repair_tracker_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ═══════════════════════════════════════════════════════════
# EMAIL REPORTING
# ═══════════════════════════════════════════════════════════

def send_report_email(settings, units):
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        return False, ("Gmail credentials not configured. Add GMAIL_USER and "
                       "GMAIL_APP_PASSWORD in the Render environment variables.")

    recipients = [r.strip() for r in settings.recipients.split(",") if r.strip()]
    if not recipients:
        return False, "No recipient email addresses configured."

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Intake ID", "Client", "Brand", "Model", "Serial Number", "Screen Size",
        "Date Received", "Status", "Shipped Back", "Shipped Back Date",
        "Final Outcome", "Last Updated",
    ])
    for unit in units:
        writer.writerow([
            unit.intake_id, unit.client.company if unit.client else "",
            unit.brand, unit.model, unit.serial_number, unit.screen_size,
            unit.date_received, unit.status,
            "Yes" if unit.shipped_back_mcps else "No",
            unit.shipped_back_date, unit.final_outcome, unit.updated_at,
        ])
    csv_data = output.getvalue()

    status_counts: dict[str, int] = {}
    for unit in units:
        status_counts[unit.status] = status_counts.get(unit.status, 0) + 1
    summary_lines = "\n".join(f"  - {s}: {c}" for s, c in sorted(status_counts.items()))
    today = datetime.now().strftime("%B %d, %Y")

    msg = MIMEMultipart()
    msg["From"] = GMAIL_USER
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = f"Pierson Repairs — Boxlight Report ({today})"
    msg.attach(MIMEText(
        f"Boxlight Repair Report\nGenerated: {today}\n\n"
        f"Total Units: {len(units)}\n\nStatus Breakdown:\n{summary_lines}\n\n"
        f"A full CSV report is attached.\n\n---\nPierson Repairs Tracker\n",
        "plain",
    ))

    part = MIMEBase("application", "octet-stream")
    part.set_payload(csv_data.encode("utf-8"))
    encoders.encode_base64(part)
    part.add_header(
        "Content-Disposition",
        f"attachment; filename=Boxlight_Report_{datetime.now().strftime('%Y%m%d')}.csv",
    )
    msg.attach(part)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, recipients, msg.as_string())
        return True, f"Report sent to {', '.join(recipients)}"
    except Exception as e:
        return False, f"Failed to send email: {e}"


@app.route("/settings", methods=["GET", "POST"])
@admin_login_required
def email_settings():
    settings = EmailSettings.query.first()
    if not settings:
        settings = EmailSettings()
        db.session.add(settings)
        db.session.commit()

    if request.method == "POST":
        action = request.form.get("action", "save")
        settings.recipients = request.form.get("recipients", "").strip()
        settings.frequency = request.form.get("frequency", "monthly")
        settings.include_active = "include_active" in request.form
        settings.include_archived = "include_archived" in request.form
        db.session.commit()

        if action == "send":
            query = Unit.query
            if settings.include_active and not settings.include_archived:
                query = query.filter_by(is_deleted=False)
            elif settings.include_archived and not settings.include_active:
                query = query.filter_by(is_deleted=True)
            success, message = send_report_email(settings, query.order_by(Unit.id.desc()).all())
            if success:
                settings.last_sent = datetime.now().strftime("%Y-%m-%d %H:%M")
                db.session.commit()
            flash(message, "success" if success else "danger")
        else:
            flash("Email settings saved.", "success")

        return redirect(url_for("email_settings"))

    return render_template("email_settings.html", settings=settings,
                           gmail_configured=bool(GMAIL_USER and GMAIL_APP_PASSWORD))


# ═══════════════════════════════════════════════════════════
# CLIENT PORTAL — the single customer-facing surface
# ═══════════════════════════════════════════════════════════

@app.route("/", methods=["GET", "POST"])
@app.route("/portal/login", methods=["GET", "POST"])
def cp_login():
    """The public front door. Clients are the overwhelming majority of
    visitors, so the root URL is their sign-in rather than a chooser."""
    if request.method == "GET" and current_client():
        return redirect(safe_next(request.args.get("next"), "cp_dashboard"))

    if request.method == "POST":
        if is_throttled("client"):
            flash("Too many failed attempts. Please wait and try again.", "danger")
            return render_template("portal/cp_login.html"), 429

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        client = find_client_by_username(username)

        if client and client.check_password(password):
            clear_failures("client")
            session["client_portal_id"] = client.id
            session["client_portal_company"] = client.company
            session.permanent = True
            return redirect(safe_next(request.args.get("next"), "cp_dashboard"))

        record_failure("client")

        if find_user_by_username(username):
            flash("That looks like a Pierson staff account. Please use the staff "
                  "sign-in instead.", "warning")
            return redirect(url_for("login"))

        flash("Invalid username or password.", "danger")

    return render_template("portal/cp_login.html")


MIN_PASSWORD_LENGTH = 10


def validate_new_password(new: str, confirm: str) -> str | None:
    """Returns an error message, or None when the password is acceptable."""
    if not new:
        return "Please enter a new password."
    if len(new) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if new != confirm:
        return "The two new passwords do not match."
    return None


@app.route("/portal/account", methods=["GET", "POST"])
@client_login_required
def cp_account():
    client = current_client()

    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        if not client.check_password(current):
            record_failure("client")
            flash("Your current password is not correct.", "danger")
            return redirect(url_for("cp_account"))

        error = validate_new_password(new, confirm)
        if error:
            flash(error, "danger")
            return redirect(url_for("cp_account"))

        if client.check_password(new):
            flash("That is already your current password.", "danger")
            return redirect(url_for("cp_account"))

        client.set_password(new)
        db.session.commit()
        flash("Your password has been changed.", "success")
        return redirect(url_for("cp_account"))

    return render_template("portal/cp_account.html", client=client,
                           min_length=MIN_PASSWORD_LENGTH)


@app.route("/portal/logout")
def cp_logout():
    session.pop("client_portal_id", None)
    session.pop("client_portal_company", None)
    flash("You have been signed out.", "success")
    return redirect(url_for("cp_login"))


@app.route("/portal")
@app.route("/portal/dashboard")
@client_login_required
def cp_dashboard():
    client = current_client()
    upcoming = upcoming_deliveries(client.id, limit=5)
    pickups = upcoming_pickups(client.id, limit=5)
    recent_eod = ((ClientEOD.query.filter_by(client_id=client.id)
                   .order_by(ClientEOD.report_date.desc()).limit(5).all())
                  if ENABLE_EOD_REPORTS else [])
    files = (ClientFile.query.filter_by(client_id=client.id)
             .order_by(ClientFile.uploaded_at.desc()).all())

    open_repairs = Unit.query.filter(
        Unit.client_id == client.id,
        Unit.is_deleted.is_(False),
        ~Unit.status.in_(["Delivered to MCPS", "Shipped Back to MCPS", "Scrapped"]),
    ).count()
    total_repairs = Unit.query.filter_by(client_id=client.id, is_deleted=False).count()

    recent_repairs = (Unit.query
                      .filter_by(client_id=client.id, is_deleted=False)
                      .order_by(Unit.id.desc()).limit(5).all())

    return render_template(
        "portal/cp_dashboard.html",
        client=client, upcoming=upcoming, recent_eod=recent_eod, files=files,
        open_repairs=open_repairs, total_repairs=total_repairs,
        recent_repairs=recent_repairs, stats=client_repair_stats(client.id),
        pickups=pickups,
    )


@app.route("/portal/repairs")
@client_login_required
def cp_repairs():
    client = current_client()
    search = request.args.get("search", "").strip()
    query = Unit.query.filter_by(client_id=client.id, is_deleted=False)

    if search:
        like = f"%{search}%"
        query = query.filter(or_(
            Unit.intake_id.ilike(like),
            Unit.serial_number.ilike(like),
            Unit.model.ilike(like),
            Unit.brand.ilike(like),
            Unit.source.ilike(like),
            Unit.status.ilike(like),
        ))

    return render_template("portal/cp_repairs.html", client=client,
                           units=query.order_by(Unit.id.desc()).all(), search=search)


@app.route("/portal/repairs/<int:unit_id>")
@client_login_required
def cp_repair_detail(unit_id: int):
    client = current_client()
    unit = get_client_unit(client.id, unit_id)
    if unit is None:
        abort(404)
    notes = (RepairNote.query
             .filter_by(unit_id=unit.id, is_internal=False)
             .order_by(RepairNote.id.desc()).all())
    return render_template("portal/cp_repair_detail.html",
                           client=client, unit=unit, notes=notes)


@app.route("/portal/repairs/<int:unit_id>/checkoff")
@client_login_required
def cp_repair_checkoff(unit_id: int):
    client = current_client()
    unit = get_client_unit(client.id, unit_id)
    if unit is None or not unit.checkoff_file:
        abort(404)
    filepath = CHECKOFF_FOLDER / unit.checkoff_file
    if not filepath.exists():
        abort(404)
    return send_file(filepath, as_attachment=False)


@app.route("/portal/repairs/<int:unit_id>/packing-slip")
@client_login_required
def cp_repair_packing_slip(unit_id: int):
    client = current_client()
    unit = get_client_unit(client.id, unit_id)
    if unit is None:
        abort(404)
    return render_template("packing_slip.html", unit=unit,
                           today=datetime.now().strftime("%Y-%m-%d"), portal="customer")


@app.route("/portal/pickups")
@client_login_required
def cp_pickups():
    client = current_client()
    return render_template("portal/cp_pickups.html", client=client,
                           units=upcoming_pickups(client.id))


@app.route("/portal/deliveries")
@app.route("/portal/schedule")
@client_login_required
def cp_schedule():
    client = current_client()
    return render_template("portal/cp_schedule.html", client=client,
                           units=upcoming_deliveries(client.id))


@app.route("/portal/reports")
@client_login_required
def cp_reports():
    if not ENABLE_EOD_REPORTS:
        abort(404)
    client = current_client()
    page = request.args.get("page", 1, type=int)
    reports = (ClientEOD.query.filter_by(client_id=client.id)
               .order_by(ClientEOD.report_date.desc())
               .paginate(page=page, per_page=10, error_out=False))
    return render_template("portal/cp_reports.html", client=client, reports=reports)


@app.route("/portal/report/<int:report_id>")
@client_login_required
def cp_report_detail(report_id: int):
    if not ENABLE_EOD_REPORTS:
        abort(404)
    client = current_client()
    report = ClientEOD.query.filter_by(id=report_id, client_id=client.id).first_or_404()
    return render_template("portal/cp_report_detail.html", client=client, report=report)


@app.route("/portal/files")
@client_login_required
def cp_files():
    client = current_client()
    files = (ClientFile.query.filter_by(client_id=client.id)
             .order_by(ClientFile.uploaded_at.desc()).all())
    return render_template("portal/cp_files.html", client=client, files=files)


@app.route("/portal/files/download/<int:file_id>")
@client_login_required
def cp_download_file(file_id: int):
    client = current_client()
    cf = ClientFile.query.filter_by(id=file_id, client_id=client.id).first_or_404()
    return send_from_directory(CLIENT_FOLDER / str(client.id), cf.filename,
                               as_attachment=True, download_name=cf.label or cf.filename)


# ═══════════════════════════════════════════════════════════
# ADMIN — CLIENT MANAGEMENT
# ═══════════════════════════════════════════════════════════

@app.route("/admin/clients")
@admin_login_required
def cp_admin():
    clients = ClientAccount.query.order_by(ClientAccount.company.asc()).all()
    return render_template("portal/admin_clients.html", clients=clients)


@app.route("/admin/clients/new", methods=["GET", "POST"])
@admin_login_required
def cp_admin_new_client():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Username and password are both required.", "danger")
            return redirect(url_for("cp_admin_new_client"))
        if len(password) < 10:
            flash("Password must be at least 10 characters.", "danger")
            return redirect(url_for("cp_admin_new_client"))
        if find_client_by_username(username, active_only=False):
            flash("A client with that username already exists. Usernames are "
                  "matched without regard to capitalisation.", "danger")
            return redirect(url_for("cp_admin_new_client"))

        client = ClientAccount(
            company=request.form.get("company", "").strip(),
            username=username,
            contact_name=request.form.get("contact_name", "").strip(),
            email=request.form.get("email", "").strip(),
            phone=request.form.get("phone", "").strip(),
            notes=request.form.get("notes", "").strip(),
        )
        client.set_password(password)
        db.session.add(client)
        db.session.commit()
        flash(f"Client {client.company} created.", "success")
        return redirect(url_for("cp_admin_client", client_id=client.id))

    return render_template("portal/admin_client_form.html", client=None)


@app.route("/admin/clients/<int:client_id>")
@admin_login_required
def cp_admin_client(client_id: int):
    client = db.session.get(ClientAccount, client_id)
    if not client:
        flash("Client not found.", "danger")
        return redirect(url_for("cp_admin"))

    schedules = (ClientSchedule.query.filter_by(client_id=client_id)
                 .order_by(ClientSchedule.date.desc()).all())
    eods = (ClientEOD.query.filter_by(client_id=client_id)
            .order_by(ClientEOD.report_date.desc()).limit(20).all())
    files = (ClientFile.query.filter_by(client_id=client_id)
             .order_by(ClientFile.uploaded_at.desc()).all())
    unit_count = Unit.query.filter_by(client_id=client_id, is_deleted=False).count()

    return render_template("portal/admin_client_detail.html", client=client,
                           schedules=schedules, eods=eods, files=files,
                           unit_count=unit_count)


@app.route("/admin/clients/<int:client_id>/edit", methods=["GET", "POST"])
@admin_login_required
def cp_admin_edit_client(client_id: int):
    client = db.session.get(ClientAccount, client_id)
    if not client:
        flash("Client not found.", "danger")
        return redirect(url_for("cp_admin"))

    if request.method == "POST":
        client.company = request.form.get("company", "").strip()
        client.contact_name = request.form.get("contact_name", "").strip()
        client.email = request.form.get("email", "").strip()
        client.phone = request.form.get("phone", "").strip()
        client.notes = request.form.get("notes", "").strip()
        client.active = "active" in request.form

        new_pw = request.form.get("password", "").strip()
        if new_pw:
            if len(new_pw) < 10:
                flash("Password must be at least 10 characters.", "danger")
                return redirect(url_for("cp_admin_edit_client", client_id=client.id))
            client.set_password(new_pw)

        db.session.commit()
        flash("Client updated.", "success")
        return redirect(url_for("cp_admin_client", client_id=client.id))

    return render_template("portal/admin_client_form.html", client=client)


@app.route("/admin/clients/<int:client_id>/schedule/add", methods=["POST"])
@admin_login_required
def cp_admin_add_schedule(client_id: int):
    if not db.session.get(ClientAccount, client_id):
        abort(404)
    db.session.add(ClientSchedule(
        client_id=client_id,
        title=request.form.get("title", "").strip(),
        date=request.form.get("date", "").strip(),
        time=request.form.get("time", "").strip(),
        location=request.form.get("location", "").strip(),
        technician=request.form.get("technician", "").strip(),
        notes=request.form.get("notes", "").strip(),
        status=request.form.get("status", "Scheduled"),
    ))
    db.session.commit()
    flash("Schedule entry added.", "success")
    return redirect(url_for("cp_admin_client", client_id=client_id))


@app.route("/admin/schedule/<int:schedule_id>/delete", methods=["POST"])
@admin_login_required
def cp_admin_delete_schedule(schedule_id: int):
    s = db.session.get(ClientSchedule, schedule_id)
    if not s:
        return redirect(url_for("cp_admin"))
    cid = s.client_id
    db.session.delete(s)
    db.session.commit()
    flash("Schedule entry deleted.", "success")
    return redirect(url_for("cp_admin_client", client_id=cid))


@app.route("/admin/clients/<int:client_id>/eod/add", methods=["POST"])
@admin_login_required
def cp_admin_add_eod(client_id: int):
    if not db.session.get(ClientAccount, client_id):
        abort(404)
    db.session.add(ClientEOD(
        client_id=client_id,
        report_date=request.form.get("report_date", "").strip(),
        technician=request.form.get("technician", "").strip(),
        work_completed=request.form.get("work_completed", "").strip(),
        issues=request.form.get("issues", "").strip(),
        next_steps=request.form.get("next_steps", "").strip(),
        hours=request.form.get("hours", "").strip(),
        sharepoint_url=request.form.get("sharepoint_url", "").strip(),
    ))
    db.session.commit()
    flash("EOD report added.", "success")
    return redirect(url_for("cp_admin_client", client_id=client_id))


@app.route("/admin/eod/<int:eod_id>/delete", methods=["POST"])
@admin_login_required
def cp_admin_delete_eod(eod_id: int):
    e = db.session.get(ClientEOD, eod_id)
    if not e:
        return redirect(url_for("cp_admin"))
    cid = e.client_id
    db.session.delete(e)
    db.session.commit()
    flash("EOD report deleted.", "success")
    return redirect(url_for("cp_admin_client", client_id=cid))


@app.route("/admin/clients/<int:client_id>/file/upload", methods=["POST"])
@admin_login_required
def cp_admin_upload_file(client_id: int):
    if not db.session.get(ClientAccount, client_id):
        abort(404)

    f = request.files.get("file")
    if not f or not f.filename:
        flash("No file selected.", "danger")
        return redirect(url_for("cp_admin_client", client_id=client_id))

    if not _ext_ok(f.filename, ALLOWED_CLIENT_FILE_EXTENSIONS):
        flash("That file type is not allowed.", "danger")
        return redirect(url_for("cp_admin_client", client_id=client_id))

    # secure_filename strips path components — no traversal out of the folder.
    cleaned = secure_filename(f.filename)
    if not cleaned:
        flash("That filename could not be used.", "danger")
        return redirect(url_for("cp_admin_client", client_id=client_id))

    folder = CLIENT_FOLDER / str(client_id)
    folder.mkdir(parents=True, exist_ok=True)
    safe_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{cleaned}"
    f.save(folder / safe_name)

    db.session.add(ClientFile(
        client_id=client_id,
        filename=safe_name,
        label=request.form.get("label", "").strip() or f.filename,
    ))
    db.session.commit()
    flash("File uploaded.", "success")
    return redirect(url_for("cp_admin_client", client_id=client_id))


@app.route("/admin/files/<int:file_id>/delete", methods=["POST"])
@admin_login_required
def cp_admin_delete_file(file_id: int):
    cf = db.session.get(ClientFile, file_id)
    if not cf:
        return redirect(url_for("cp_admin"))
    cid = cf.client_id
    try:
        (CLIENT_FOLDER / str(cid) / cf.filename).unlink(missing_ok=True)
    except OSError:
        pass
    db.session.delete(cf)
    db.session.commit()
    flash("File deleted.", "success")
    return redirect(url_for("cp_admin_client", client_id=cid))


# ═══════════════════════════════════════════════════════════
# BOXLIGHT SERVICE-PROVIDER PORTAL + REPLACEMENT STOCK
# ═══════════════════════════════════════════════════════════

@app.route("/boxlight/login", methods=["GET", "POST"])
def boxlight_login():
    if request.method == "GET" and current_boxlight():
        return redirect(url_for("boxlight_dashboard"))
    if request.method == "POST":
        if is_throttled("boxlight"):
            flash("Too many failed attempts. Please wait and try again.", "danger")
            return render_template("boxlight/login.html"), 429
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        account = BoxlightAccount.query.filter(
            db.func.lower(BoxlightAccount.username) == username.lower(),
            BoxlightAccount.active.is_(True),
        ).first()
        if account and account.check_password(password):
            clear_failures("boxlight")
            session["boxlight_account_id"] = account.id
            session.permanent = True
            return redirect(safe_next(request.args.get("next"), "boxlight_dashboard"))
        record_failure("boxlight")
        flash("Invalid username or password.", "danger")
    return render_template("boxlight/login.html")


@app.route("/boxlight/logout")
def boxlight_logout():
    session.pop("boxlight_account_id", None)
    return redirect(url_for("boxlight_login"))


@app.route("/boxlight")
@app.route("/boxlight/dashboard")
@boxlight_login_required
def boxlight_dashboard():
    active = Unit.query.filter(Unit.is_deleted.is_(False))
    repaired_statuses = ["Completed", "Delivering to MCPS", "Delivered to MCPS", "Shipped Back to MCPS"]
    counts = {
        "total_repairs": active.count(),
        "open_repairs": active.filter(~Unit.status.in_(repaired_statuses + ["Scrapped"])).count(),
        "repaired": active.filter(Unit.status.in_(repaired_statuses)).count(),
        "unrepairable": active.filter(Unit.status == "Scrapped").count(),
        "stock_available": ReplacementPanel.query.filter_by(status="Available").count(),
        "stock_total": ReplacementPanel.query.count(),
    }
    recent = active.order_by(Unit.updated_at.desc()).limit(15).all()
    return render_template("boxlight/dashboard.html", counts=counts, units=recent)


@app.route("/boxlight/repairs")
@boxlight_login_required
def boxlight_repairs():
    search = request.args.get("search", "").strip()
    status = request.args.get("status", "").strip()
    q = Unit.query.filter(Unit.is_deleted.is_(False))
    if search:
        like = f"%{search}%"
        q = q.filter(or_(Unit.serial_number.ilike(like), Unit.model.ilike(like),
                         Unit.intake_id.ilike(like), Unit.reported_issue.ilike(like)))
    if status:
        q = q.filter(Unit.status == status)
    return render_template("boxlight/repairs.html", units=q.order_by(Unit.updated_at.desc()).all(),
                           search=search, status_filter=status)


@app.route("/boxlight/repairs/<int:unit_id>")
@boxlight_login_required
def boxlight_repair_detail(unit_id: int):
    unit = Unit.query.filter_by(id=unit_id, is_deleted=False).first_or_404()
    return render_template("boxlight/repair_detail.html", unit=unit)


@app.route("/boxlight/stock")
@boxlight_login_required
def boxlight_stock():
    panels = ReplacementPanel.query.order_by(ReplacementPanel.id.desc()).all()
    return render_template("boxlight/stock.html", panels=panels)


@app.route("/boxlight/export/repairs.csv")
@boxlight_login_required
def boxlight_export_repairs():
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["Intake ID", "Serial Number", "Model", "Status", "Issue", "Date Received",
                "Repaired Date", "Delivery Date", "Final Outcome", "Replacement Serial"])
    for u in Unit.query.filter(Unit.is_deleted.is_(False)).order_by(Unit.id).all():
        replacement = u.replacement_panel[0].serial_number if u.replacement_panel else ""
        w.writerow([u.intake_id, u.serial_number, u.model, u.status, u.reported_issue,
                    u.date_received, u.repaired_date, u.delivery_date, u.final_outcome, replacement])
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=boxlight_repairs.csv"})


@app.route("/boxlight/export/stock.csv")
@boxlight_login_required
def boxlight_export_stock():
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["Serial Number", "Model", "Status", "Date Received", "Used For SN", "Notes"])
    for p in ReplacementPanel.query.order_by(ReplacementPanel.id).all():
        w.writerow([p.serial_number, p.model, p.status, p.date_received,
                    p.used_for_unit.serial_number if p.used_for_unit else "", p.notes])
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=boxlight_replacement_stock.csv"})


@app.route("/replacement-stock")
@admin_login_required
def replacement_stock_admin():
    return render_template("replacement_stock.html",
                           panels=ReplacementPanel.query.order_by(ReplacementPanel.id.desc()).all(),
                           units=Unit.query.filter(Unit.is_deleted.is_(False)).order_by(Unit.id.desc()).all())


@app.route("/replacement-stock/add", methods=["POST"])
@admin_login_required
def replacement_stock_add():
    serial = request.form.get("serial_number", "").strip()
    if not serial:
        flash("Replacement serial number is required.", "danger")
        return redirect(url_for("replacement_stock_admin"))
    if ReplacementPanel.query.filter(db.func.lower(ReplacementPanel.serial_number) == serial.lower()).first():
        flash("That replacement serial number is already in stock history.", "warning")
        return redirect(url_for("replacement_stock_admin"))
    panel = ReplacementPanel(serial_number=serial,
                             model=request.form.get("model", "").strip(),
                             date_received=request.form.get("date_received", "").strip(),
                             notes=request.form.get("notes", "").strip())
    db.session.add(panel)
    db.session.commit()
    flash("Replacement panel added to available stock.", "success")
    return redirect(url_for("replacement_stock_admin"))


@app.route("/replacement-stock/<int:panel_id>/assign", methods=["POST"])
@admin_login_required
def replacement_stock_assign(panel_id: int):
    panel = db.session.get(ReplacementPanel, panel_id) or abort(404)
    unit_id = request.form.get("unit_id", "").strip()
    if not unit_id:
        panel.used_for_unit_id = None
        panel.status = "Available"
    else:
        unit = get_active_unit(int(unit_id))
        if not unit:
            abort(404)
        panel.used_for_unit_id = unit.id
        panel.status = "Used"
    db.session.commit()
    flash("Replacement stock assignment updated.", "success")
    return redirect(url_for("replacement_stock_admin"))


# ═══════════════════════════════════════════════════════════
# LEGACY REDIRECTS — old bookmarks land somewhere sensible
# ═══════════════════════════════════════════════════════════

@app.route("/customer")
@app.route("/customer-login")
@app.route("/customer/<path:_rest>")
def legacy_customer(_rest=None):
    return redirect(url_for("cp_login"))


@app.route("/customer-logout")
def legacy_customer_logout():
    return redirect(url_for("cp_logout"))


@app.route("/driver")
@app.route("/driver-login")
@app.route("/driver/<path:_rest>")
def legacy_driver(_rest=None):
    return redirect(url_for("login"))


@app.route("/portal/admin")
@app.route("/portal/admin/<path:_rest>")
def legacy_portal_admin(_rest=None):
    """Client management moved out from under the client-facing /portal path."""
    return redirect(url_for("cp_admin"))


init_database()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=IS_DEBUG)
