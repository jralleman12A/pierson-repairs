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

# Optional first-run New Story customer account.
BOOTSTRAP_NEW_STORY_USERNAME = os.getenv("NEW_STORY_USERNAME", "")
BOOTSTRAP_NEW_STORY_PASSWORD = os.getenv("NEW_STORY_PASSWORD", "")
BOOTSTRAP_NEW_STORY_CONTACT = os.getenv("NEW_STORY_CONTACT", "")

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


class BoxlightMessageThread(db.Model, RowLikeMixin):
    __tablename__ = "boxlight_message_threads"

    id = db.Column(db.Integer, primary_key=True)
    subject = db.Column(db.String(220), nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey("units.id"), nullable=True, index=True)
    created_by_type = db.Column(db.String(20), nullable=False, default="boxlight")
    created_by_name = db.Column(db.String(120), default="")
    is_closed = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)

    unit = db.relationship("Unit", foreign_keys=[unit_id])
    messages = db.relationship(
        "BoxlightMessage",
        backref="thread",
        lazy=True,
        cascade="all, delete-orphan",
        order_by="BoxlightMessage.created_at.asc()",
    )


class BoxlightMessage(db.Model, RowLikeMixin):
    __tablename__ = "boxlight_messages"

    id = db.Column(db.Integer, primary_key=True)
    thread_id = db.Column(
        db.Integer,
        db.ForeignKey("boxlight_message_threads.id"),
        nullable=False,
        index=True,
    )
    sender_type = db.Column(db.String(20), nullable=False)  # admin | boxlight
    sender_name = db.Column(db.String(120), default="")
    body = db.Column(db.Text, nullable=False)
    read_by_admin = db.Column(db.Boolean, nullable=False, default=False)
    read_by_boxlight = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


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
# NEW STORY — IT MANAGEMENT
# Separate data model from Boxlight/MCPS repairs. The two programs share
# authentication infrastructure only; operational records never overlap.
# ═══════════════════════════════════════════════════════════

NEW_STORY_REQUEST_STATUSES = [
    "Incoming",
    "Needs Review",
    "Awaiting Inventory",
    "Processing",
    "Ready to Ship",
    "Shipped",
    "Awaiting Return",
    "Complete",
    "Cancelled",
]

NEW_STORY_SERVICE_TYPES = [
    "Order",
    "New Hire",
    "Break/Fix",
    "Config Only B/F",
    "Aux Fund",
    "OPS",
    "Installation",
    "Other",
]

NEW_STORY_CATEGORIES = [
    "Chromebook",
    "Windows",
    "iPad",
    "Monitor",
    "Keyboard",
    "Docking Station",
    "Jabra / Headset",
    "Web Cam",
    "Interactive Panel",
    "Wall Mount",
    "Mobile Stand",
    "PCM11",
    "PCM13",
    "License",
    "Other",
]

NEW_STORY_ASSET_STATUSES = [
    "Expected",
    "Received",
    "Available",
    "Reserved",
    "Allocated",
    "Processing",
    "Ready to Ship",
    "Shipped",
    "Deployed",
    "In Use",
    "Return Pending",
    "Returned",
    "Repair",
    "Lost",
    "Retired",
    "Scrapped",
]


class NewStoryAccount(db.Model, RowLikeMixin):
    __tablename__ = "new_story_accounts"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    contact_name = db.Column(db.String(120), default="")
    email = db.Column(db.String(160), default="")
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, pw: str) -> None:
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw: str) -> bool:
        return check_password_hash(self.password_hash, pw)


class NewStoryLocation(db.Model, RowLikeMixin):
    __tablename__ = "new_story_locations"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(180), nullable=False, index=True)
    street = db.Column(db.String(220), default="")
    city = db.Column(db.String(120), default="")
    state = db.Column(db.String(40), default="")
    zip_code = db.Column(db.String(20), default="")
    location_type = db.Column(db.String(50), default="School")
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class NewStoryRequest(db.Model, RowLikeMixin):
    __tablename__ = "new_story_requests"

    id = db.Column(db.Integer, primary_key=True)
    ticket_number = db.Column(db.String(120), nullable=False, index=True)
    customer_po = db.Column(db.String(120), default="", index=True)
    project_name = db.Column(db.String(220), default="")
    service_type = db.Column(db.String(80), nullable=False, default="Order", index=True)
    status = db.Column(db.String(80), nullable=False, default="Incoming", index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("new_story_locations.id"), nullable=True, index=True)
    school_name = db.Column(db.String(180), default="", index=True)
    requester = db.Column(db.String(160), default="")
    recipient = db.Column(db.String(160), default="")
    street = db.Column(db.String(220), default="")
    city = db.Column(db.String(120), default="")
    state = db.Column(db.String(40), default="")
    zip_code = db.Column(db.String(20), default="")
    return_kit_required = db.Column(db.Boolean, nullable=False, default=False)
    attention_reason = db.Column(db.String(220), default="")
    source = db.Column(db.String(80), default="Manual")
    source_subject = db.Column(db.String(500), default="")
    source_raw = db.Column(db.Text, default="")
    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    location = db.relationship("NewStoryLocation", backref="requests")
    items = db.relationship("NewStoryRequestItem", backref="request", lazy=True, cascade="all, delete-orphan")
    activities = db.relationship("NewStoryActivity", backref="request", lazy=True, cascade="all, delete-orphan", order_by="NewStoryActivity.created_at.desc()")
    shipments = db.relationship("NewStoryShipment", backref="request", lazy=True, cascade="all, delete-orphan")

    @property
    def address_text(self) -> str:
        return ", ".join(x for x in [self.street, self.city, self.state, self.zip_code] if x)

    @property
    def requested_qty(self) -> int:
        return sum(max(i.quantity_requested or 0, 0) for i in self.items)


class NewStoryRequestItem(db.Model, RowLikeMixin):
    __tablename__ = "new_story_request_items"

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("new_story_requests.id"), nullable=False, index=True)
    category = db.Column(db.String(100), nullable=False, default="Other", index=True)
    description = db.Column(db.String(240), default="")
    model = db.Column(db.String(160), default="")
    quantity_requested = db.Column(db.Integer, nullable=False, default=1)
    quantity_fulfilled = db.Column(db.Integer, nullable=False, default=0)
    requirement_type = db.Column(db.String(80), default="Equipment")
    shortage_reason = db.Column(db.String(220), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class NewStoryAsset(db.Model, RowLikeMixin):
    __tablename__ = "new_story_assets"

    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(100), nullable=False, default="Other", index=True)
    description = db.Column(db.String(240), default="")
    model = db.Column(db.String(160), default="")
    raw_serial = db.Column(db.String(220), default="")
    serial_number = db.Column(db.String(220), default="", index=True)
    asset_tag = db.Column(db.String(160), default="", index=True)
    customer_po = db.Column(db.String(120), default="", index=True)
    vendor_order = db.Column(db.String(120), default="")
    status = db.Column(db.String(80), nullable=False, default="Available", index=True)
    current_location_id = db.Column(db.Integer, db.ForeignKey("new_story_locations.id"), nullable=True, index=True)
    assigned_request_id = db.Column(db.Integer, db.ForeignKey("new_story_requests.id"), nullable=True, index=True)
    assigned_to = db.Column(db.String(180), default="", index=True)
    room = db.Column(db.String(120), default="")
    received_at = db.Column(db.DateTime, nullable=True)
    deployed_at = db.Column(db.DateTime, nullable=True)
    retired_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    current_location = db.relationship("NewStoryLocation", foreign_keys=[current_location_id])
    assigned_request = db.relationship("NewStoryRequest", foreign_keys=[assigned_request_id], backref="assets")


class NewStoryStockItem(db.Model, RowLikeMixin):
    __tablename__ = "new_story_stock_items"

    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(100), nullable=False, default="Other", index=True)
    description = db.Column(db.String(240), default="")
    model = db.Column(db.String(160), default="", index=True)
    customer_po = db.Column(db.String(120), default="", index=True)
    vendor_order = db.Column(db.String(120), default="")
    location_id = db.Column(db.Integer, db.ForeignKey("new_story_locations.id"), nullable=True, index=True)
    quantity_on_hand = db.Column(db.Integer, nullable=False, default=0)
    quantity_reserved = db.Column(db.Integer, nullable=False, default=0)
    reorder_level = db.Column(db.Integer, nullable=False, default=0)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    location = db.relationship("NewStoryLocation")

    @property
    def quantity_available(self) -> int:
        return max((self.quantity_on_hand or 0) - (self.quantity_reserved or 0), 0)


class NewStoryInventoryMovement(db.Model, RowLikeMixin):
    __tablename__ = "new_story_inventory_movements"

    id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.Integer, db.ForeignKey("new_story_assets.id"), nullable=True, index=True)
    stock_item_id = db.Column(db.Integer, db.ForeignKey("new_story_stock_items.id"), nullable=True, index=True)
    request_id = db.Column(db.Integer, db.ForeignKey("new_story_requests.id"), nullable=True, index=True)
    shipment_id = db.Column(db.Integer, db.ForeignKey("new_story_shipments.id"), nullable=True, index=True)
    action = db.Column(db.String(80), nullable=False, index=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    from_status = db.Column(db.String(80), default="")
    to_status = db.Column(db.String(80), default="")
    from_location_id = db.Column(db.Integer, db.ForeignKey("new_story_locations.id"), nullable=True)
    to_location_id = db.Column(db.Integer, db.ForeignKey("new_story_locations.id"), nullable=True)
    actor = db.Column(db.String(120), default="Pierson")
    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    asset = db.relationship("NewStoryAsset", foreign_keys=[asset_id], backref=db.backref("inventory_movements", lazy=True, order_by="NewStoryInventoryMovement.created_at.desc()"))
    stock_item = db.relationship("NewStoryStockItem", foreign_keys=[stock_item_id], backref=db.backref("movements", lazy=True, order_by="NewStoryInventoryMovement.created_at.desc()"))
    request = db.relationship("NewStoryRequest", foreign_keys=[request_id])
    shipment = db.relationship("NewStoryShipment", foreign_keys=[shipment_id])
    from_location = db.relationship("NewStoryLocation", foreign_keys=[from_location_id])
    to_location = db.relationship("NewStoryLocation", foreign_keys=[to_location_id])


class NewStoryShipment(db.Model, RowLikeMixin):
    __tablename__ = "new_story_shipments"

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("new_story_requests.id"), nullable=False, index=True)
    direction = db.Column(db.String(20), nullable=False, default="Outbound")
    method = db.Column(db.String(80), default="FedEx Ground")
    tracking_number = db.Column(db.String(180), default="", index=True)
    shipped_at = db.Column(db.DateTime, nullable=True)
    delivered_at = db.Column(db.DateTime, nullable=True)
    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    items = db.relationship("NewStoryShipmentItem", backref="shipment", lazy=True, cascade="all, delete-orphan")


class NewStoryShipmentItem(db.Model, RowLikeMixin):
    __tablename__ = "new_story_shipment_items"

    id = db.Column(db.Integer, primary_key=True)
    shipment_id = db.Column(db.Integer, db.ForeignKey("new_story_shipments.id"), nullable=False, index=True)
    asset_id = db.Column(db.Integer, db.ForeignKey("new_story_assets.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    asset = db.relationship("NewStoryAsset")


class NewStoryActivity(db.Model, RowLikeMixin):
    __tablename__ = "new_story_activity"

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("new_story_requests.id"), nullable=False, index=True)
    event_type = db.Column(db.String(80), nullable=False, default="Note")
    summary = db.Column(db.Text, nullable=False)
    detail = db.Column(db.Text, default="")
    actor = db.Column(db.String(120), default="Pierson")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class NewStoryOrderLine(db.Model, RowLikeMixin):
    __tablename__ = "new_story_order_lines"

    id = db.Column(db.Integer, primary_key=True)
    status = db.Column(db.String(80), default="", index=True)
    order_date = db.Column(db.DateTime, nullable=True)
    order_date_raw = db.Column(db.String(80), default="")
    customer_po = db.Column(db.String(120), default="", index=True)
    project_name = db.Column(db.String(220), default="")
    vendor_invoice = db.Column(db.String(120), default="")
    vendor_order = db.Column(db.String(120), default="", index=True)
    description = db.Column(db.String(260), default="", index=True)
    tracking_number = db.Column(db.String(220), default="")
    quantity_ordered = db.Column(db.Integer, nullable=False, default=0)
    sales_order = db.Column(db.String(120), default="")
    quantity_received = db.Column(db.Integer, nullable=False, default=0)
    quantity_shipped = db.Column(db.Integer, nullable=False, default=0)
    quantity_remaining = db.Column(db.Integer, nullable=False, default=0)
    source = db.Column(db.String(80), default="Legacy Workbook")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class NewStoryServiceEvent(db.Model, RowLikeMixin):
    __tablename__ = "new_story_service_events"

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("new_story_requests.id"), nullable=True, index=True)
    asset_id = db.Column(db.Integer, db.ForeignKey("new_story_assets.id"), nullable=True, index=True)
    person = db.Column(db.String(180), default="")
    event_date = db.Column(db.DateTime, nullable=True, index=True)
    event_date_raw = db.Column(db.String(80), default="")
    device_type = db.Column(db.String(100), default="", index=True)
    serial_number = db.Column(db.String(220), default="", index=True)
    asset_tag = db.Column(db.String(160), default="", index=True)
    issue_category = db.Column(db.String(180), default="", index=True)
    tracking_number = db.Column(db.String(220), default="")
    ticket_number = db.Column(db.String(120), default="", index=True)
    outcome = db.Column(db.String(160), default="", index=True)
    resolved = db.Column(db.Boolean, nullable=False, default=False)
    comments = db.Column(db.Text, default="")
    customer_update = db.Column(db.Text, default="")
    source = db.Column(db.String(80), default="Legacy Workbook")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    request = db.relationship("NewStoryRequest", backref="service_events")
    asset = db.relationship("NewStoryAsset", backref="service_events")


class NewStoryInstallation(db.Model, RowLikeMixin):
    __tablename__ = "new_story_installations"

    id = db.Column(db.Integer, primary_key=True)
    location_id = db.Column(db.Integer, db.ForeignKey("new_story_locations.id"), nullable=True, index=True)
    install_date = db.Column(db.DateTime, nullable=True, index=True)
    install_date_raw = db.Column(db.String(80), default="")
    arrival_time = db.Column(db.String(80), default="")
    availability = db.Column(db.String(220), default="")
    school_name = db.Column(db.String(180), default="", index=True)
    address = db.Column(db.String(500), default="")
    contact = db.Column(db.String(260), default="")
    phone = db.Column(db.String(120), default="")
    panel_count = db.Column(db.Integer, nullable=False, default=0)
    rooms = db.Column(db.Text, default="")
    customer_po = db.Column(db.String(120), default="", index=True)
    status = db.Column(db.String(100), default="", index=True)
    expected_range = db.Column(db.String(180), default="")
    notes = db.Column(db.Text, default="")
    floor_plan = db.Column(db.String(220), default="")
    source = db.Column(db.String(80), default="Legacy Workbook")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    location = db.relationship("NewStoryLocation", backref="installations")


class NewStoryDomain(db.Model, RowLikeMixin):
    __tablename__ = "new_story_domains"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, default="", index=True)
    tenant = db.Column(db.String(180), default="")
    domain = db.Column(db.String(180), default="")
    parent_ou = db.Column(db.String(220), default="")
    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class NewStoryLegacyImport(db.Model, RowLikeMixin):
    __tablename__ = "new_story_legacy_imports"

    id = db.Column(db.Integer, primary_key=True)
    source_name = db.Column(db.String(260), nullable=False)
    source_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    counts_json = db.Column(db.Text, default="{}")
    imported_by = db.Column(db.String(120), default="Pierson")
    imported_at = db.Column(db.DateTime, default=datetime.utcnow)


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



def current_new_story() -> NewStoryAccount | None:
    account_id = session.get("new_story_account_id")
    if not account_id:
        return None
    account = db.session.get(NewStoryAccount, account_id)
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
        # Only count panels that have actually reached a completed/returned stage.
        # Open repairs must not be reported as "repaired" just because they are not scrapped.
        "repaired": counts.get("Completed", 0) + counts.get("Returned", 0),
        "repair_rate": (
            round(100 * (counts.get("Completed", 0) + counts.get("Returned", 0))
                  / (counts.get("Completed", 0) + counts.get("Returned", 0) + scrapped))
            if (counts.get("Completed", 0) + counts.get("Returned", 0) + scrapped)
            else None
        ),
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


def boxlight_unread_count() -> int:
    if current_boxlight() is None:
        return 0
    try:
        return BoxlightMessage.query.filter_by(
            sender_type="admin", read_by_boxlight=False
        ).count()
    except Exception:
        return 0


def admin_boxlight_unread_count() -> int:
    if current_user() is None:
        return 0
    try:
        return BoxlightMessage.query.filter_by(
            sender_type="boxlight", read_by_admin=False
        ).count()
    except Exception:
        return 0


@app.context_processor
def inject_globals():
    return {
        "current_user": current_user(),
        "current_client": current_client(),
        "current_boxlight": current_boxlight(),
        "current_new_story": current_new_story(),
        "NEW_STORY_REQUEST_STATUSES": NEW_STORY_REQUEST_STATUSES,
        "NEW_STORY_SERVICE_TYPES": NEW_STORY_SERVICE_TYPES,
        "NEW_STORY_CATEGORIES": NEW_STORY_CATEGORIES,
        "NEW_STORY_ASSET_STATUSES": NEW_STORY_ASSET_STATUSES,
        "boxlight_unread": boxlight_unread_count(),
        "admin_boxlight_unread": admin_boxlight_unread_count(),
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
            return redirect(url_for("cp_login", next=request.full_path))
        return view_func(*args, **kwargs)
    return wrapped



def new_story_login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if current_new_story() is None:
            session.pop("new_story_account_id", None)
            return redirect(url_for("cp_login", next=request.full_path))
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
    if db.engine.dialect.name == "postgresql" and _column_exists("new_story_activity", "summary"):
        statements.append("ALTER TABLE new_story_activity ALTER COLUMN summary TYPE TEXT")
    if not _column_exists("new_story_assets", "assigned_to"):
        statements.append("ALTER TABLE new_story_assets ADD COLUMN assigned_to VARCHAR(180) DEFAULT ''")
    if not _column_exists("new_story_assets", "room"):
        statements.append("ALTER TABLE new_story_assets ADD COLUMN room VARCHAR(120) DEFAULT ''")
    if not _column_exists("new_story_assets", "deployed_at"):
        statements.append("ALTER TABLE new_story_assets ADD COLUMN deployed_at TIMESTAMP")
    if not _column_exists("new_story_assets", "retired_at"):
        statements.append("ALTER TABLE new_story_assets ADD COLUMN retired_at TIMESTAMP")
    for stmt in statements:
        try:
            db.session.execute(text(stmt))
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Database migration statement failed: %s", stmt)


def sync_postgres_sequences() -> None:
    """Keep PostgreSQL auto-increment sequences ahead of imported IDs.

    The original SQLite migration inserted explicit primary-key values. PostgreSQL
    sequences do not automatically advance when that happens, so a later INSERT
    can fail with a duplicate *id* even though the Intake ID is brand new.
    """
    if db.engine.dialect.name != "postgresql":
        return

    for table_name in ("units", "repair_notes", "replacement_panels", "boxlight_accounts", "boxlight_message_threads", "boxlight_messages", "new_story_accounts", "new_story_locations", "new_story_requests", "new_story_request_items", "new_story_assets", "new_story_shipments", "new_story_shipment_items", "new_story_activity", "new_story_stock_items", "new_story_inventory_movements"):
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
    """One-time migration helper for legacy unassigned units.

    IMPORTANT: this is opt-in. New/unassigned repair records must stay unassigned
    unless an admin explicitly chooses a client. Automatically assigning them on
    every app restart can expose records in a client portal.
    """
    enabled = os.getenv("BACKFILL_UNASSIGNED_UNITS", "false").lower() in {"1", "true", "yes"}
    if not enabled:
        return
    if not Unit.query.filter(Unit.client_id.is_(None)).count():
        return
    clients = ClientAccount.query.all()
    if len(clients) != 1:
        app.logger.warning(
            "BACKFILL_UNASSIGNED_UNITS requested, but expected exactly one client and found %s; skipping.",
            len(clients),
        )
        return
    updated = Unit.query.filter(Unit.client_id.is_(None)).update(
        {Unit.client_id: clients[0].id}, synchronize_session=False
    )
    db.session.commit()
    app.logger.warning("Backfilled %s unassigned unit(s) to client id %s.", updated, clients[0].id)


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


        if BOOTSTRAP_NEW_STORY_USERNAME and BOOTSTRAP_NEW_STORY_PASSWORD:
            existing_ns = NewStoryAccount.query.filter(
                db.func.lower(NewStoryAccount.username) == BOOTSTRAP_NEW_STORY_USERNAME.strip().lower()
            ).first()
            if not existing_ns:
                ns_account = NewStoryAccount(
                    username=BOOTSTRAP_NEW_STORY_USERNAME.strip(),
                    contact_name=BOOTSTRAP_NEW_STORY_CONTACT.strip(),
                )
                ns_account.set_password(BOOTSTRAP_NEW_STORY_PASSWORD)
                db.session.add(ns_account)
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
    try:
        db.session.execute(text("SELECT 1"))
        return {"status": "ok", "app": "pierson-repairs", "database": "ok"}, 200
    except Exception:
        db.session.rollback()
        app.logger.exception("Health check failed: database unavailable")
        return {"status": "error", "app": "pierson-repairs", "database": "unavailable"}, 503


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
    review_imported = 0
    errors: list[str] = []

    for i in range(len(serials)):
        if i not in selected:
            continue

        serial_number = (serials[i] if i < len(serials) else "").strip()
        model = (models[i] if i < len(models) else "").strip()
        missing_fields = []
        if not model:
            missing_fields.append("model")
        if not serial_number:
            missing_fields.append("serial number")

        # Incomplete Boxlight data is still useful intake data. Import it and
        # clearly flag the record for the team instead of throwing it away.
        needs_review = bool(missing_fields)

        # Avoid accidentally importing the same physical panel twice when a
        # serial was actually supplied. Blank serials are allowed for review.
        if serial_number and Unit.query.filter(db.func.lower(Unit.serial_number) == serial_number.lower(), Unit.is_deleted.is_(False)).first():
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
        if needs_review:
            issue_bits.append("IMPORT REVIEW: Missing " + " and ".join(missing_fields))
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
            if needs_review:
                review_imported += 1
        except IntegrityError as exc:
            db.session.rollback()
            sync_postgres_sequences()
            skipped += 1
            errors.append(f"{serial_number}: database rejected the row ({getattr(exc, 'orig', exc)}).")

    if imported:
        if review_imported:
            flash(
                f"Imported {imported} panel{'s' if imported != 1 else ''} from the support email. "
                f"{review_imported} imported record{'s' if review_imported != 1 else ''} need review for missing model/serial data.",
                "success",
            )
        else:
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
        except IntegrityError:
            db.session.rollback()
            flash("Could not save that unit because the Intake ID already exists.", "danger")
        except Exception:
            db.session.rollback()
            app.logger.exception("Unexpected error while updating unit %s", unit_id)
            flash("Could not save that unit because of a database error. Check the Render logs for details.", "danger")

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
    """Single public sign-in for district clients and Boxlight reps."""
    if request.method == "GET":
        if current_client():
            return redirect(safe_next(request.args.get("next"), "cp_dashboard"))
        if current_boxlight():
            return redirect(safe_next(request.args.get("next"), "boxlight_dashboard"))
        if current_new_story():
            return redirect(safe_next(request.args.get("next"), "new_story_portal_dashboard"))

    if request.method == "POST":
        if is_throttled("public_portal"):
            flash("Too many failed attempts. Please wait and try again.", "danger")
            return render_template("portal/cp_login.html"), 429

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        # District / customer account
        client = find_client_by_username(username)
        if client and client.check_password(password):
            clear_failures("public_portal")
            session.pop("boxlight_account_id", None)
            session.pop("new_story_account_id", None)
            session["client_portal_id"] = client.id
            session["client_portal_company"] = client.company
            session.permanent = True
            return redirect(safe_next(request.args.get("next"), "cp_dashboard"))

        # Manufacturer / Boxlight service-provider account
        boxlight = BoxlightAccount.query.filter(
            db.func.lower(BoxlightAccount.username) == username.lower(),
            BoxlightAccount.active.is_(True),
        ).first()
        if boxlight and boxlight.check_password(password):
            clear_failures("public_portal")
            session.pop("client_portal_id", None)
            session.pop("client_portal_company", None)
            session["boxlight_account_id"] = boxlight.id
            session.pop("new_story_account_id", None)
            session.permanent = True
            return redirect(safe_next(request.args.get("next"), "boxlight_dashboard"))

        # New Story IT management account
        new_story = NewStoryAccount.query.filter(
            db.func.lower(NewStoryAccount.username) == username.lower(),
            NewStoryAccount.active.is_(True),
        ).first()
        if new_story and new_story.check_password(password):
            clear_failures("public_portal")
            session.pop("client_portal_id", None)
            session.pop("client_portal_company", None)
            session.pop("boxlight_account_id", None)
            session["new_story_account_id"] = new_story.id
            session.permanent = True
            return redirect(safe_next(request.args.get("next"), "new_story_portal_dashboard"))

        record_failure("public_portal")

        if find_user_by_username(username):
            flash("That looks like a Pierson staff account. Please use the staff sign-in instead.", "warning")
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
    """Legacy Boxlight URL; all external users now share one polished sign-in."""
    if current_boxlight():
        return redirect(url_for("boxlight_dashboard"))
    return redirect(url_for("cp_login", next=url_for("boxlight_dashboard")))


@app.route("/boxlight/logout")
def boxlight_logout():
    session.pop("boxlight_account_id", None)
    return redirect(url_for("cp_login"))


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


# ───────────────────────────────────────────────────────────
# BOXLIGHT ↔ PIERSON SERVICE COMMUNICATIONS
# ───────────────────────────────────────────────────────────

def _mark_boxlight_messages_read(thread: BoxlightMessageThread) -> None:
    changed = False
    for message in thread.messages:
        if message.sender_type == "admin" and not message.read_by_boxlight:
            message.read_by_boxlight = True
            changed = True
    if changed:
        db.session.commit()


def _mark_admin_messages_read(thread: BoxlightMessageThread) -> None:
    changed = False
    for message in thread.messages:
        if message.sender_type == "boxlight" and not message.read_by_admin:
            message.read_by_admin = True
            changed = True
    if changed:
        db.session.commit()


@app.route("/boxlight/messages")
@boxlight_login_required
def boxlight_messages():
    threads = BoxlightMessageThread.query.order_by(
        BoxlightMessageThread.updated_at.desc(), BoxlightMessageThread.id.desc()
    ).all()
    units = Unit.query.filter(Unit.is_deleted.is_(False)).order_by(Unit.id.desc()).all()
    return render_template("boxlight/messages.html", threads=threads, units=units)


@app.route("/boxlight/messages/new", methods=["POST"])
@boxlight_login_required
def boxlight_message_new():
    rep = current_boxlight()
    subject = request.form.get("subject", "").strip()
    body = request.form.get("message", "").strip()
    unit_id_raw = request.form.get("unit_id", "").strip()

    if not subject or not body:
        flash("Add a subject and message before sending.", "warning")
        return redirect(url_for("boxlight_messages"))

    unit = None
    if unit_id_raw:
        try:
            unit = Unit.query.filter_by(id=int(unit_id_raw), is_deleted=False).first()
        except ValueError:
            unit = None

    sender_name = (rep.contact_name or rep.username or "Boxlight").strip()
    thread = BoxlightMessageThread(
        subject=subject[:220],
        unit_id=unit.id if unit else None,
        created_by_type="boxlight",
        created_by_name=sender_name,
    )
    db.session.add(thread)
    db.session.flush()
    db.session.add(BoxlightMessage(
        thread_id=thread.id,
        sender_type="boxlight",
        sender_name=sender_name,
        body=body,
        read_by_admin=False,
        read_by_boxlight=True,
    ))
    thread.updated_at = datetime.utcnow()
    db.session.commit()
    flash("Message sent to Pierson service administration.", "success")
    return redirect(url_for("boxlight_message_thread", thread_id=thread.id))


@app.route("/boxlight/messages/<int:thread_id>")
@boxlight_login_required
def boxlight_message_thread(thread_id: int):
    thread = BoxlightMessageThread.query.get_or_404(thread_id)
    _mark_boxlight_messages_read(thread)
    return render_template("boxlight/message_thread.html", thread=thread)


@app.route("/boxlight/messages/<int:thread_id>/reply", methods=["POST"])
@boxlight_login_required
def boxlight_message_reply(thread_id: int):
    thread = BoxlightMessageThread.query.get_or_404(thread_id)
    if thread.is_closed:
        flash("This conversation is closed.", "warning")
        return redirect(url_for("boxlight_message_thread", thread_id=thread.id))

    body = request.form.get("message", "").strip()
    if not body:
        flash("Type a message before sending.", "warning")
        return redirect(url_for("boxlight_message_thread", thread_id=thread.id))

    rep = current_boxlight()
    sender_name = (rep.contact_name or rep.username or "Boxlight").strip()
    db.session.add(BoxlightMessage(
        thread_id=thread.id,
        sender_type="boxlight",
        sender_name=sender_name,
        body=body,
        read_by_admin=False,
        read_by_boxlight=True,
    ))
    thread.updated_at = datetime.utcnow()
    db.session.commit()
    return redirect(url_for("boxlight_message_thread", thread_id=thread.id))


@app.route("/boxlight-messages")
@admin_login_required
def admin_boxlight_messages():
    threads = BoxlightMessageThread.query.order_by(
        BoxlightMessageThread.updated_at.desc(), BoxlightMessageThread.id.desc()
    ).all()
    return render_template("boxlight/admin_messages.html", threads=threads)


@app.route("/boxlight-messages/<int:thread_id>")
@admin_login_required
def admin_boxlight_message_thread(thread_id: int):
    thread = BoxlightMessageThread.query.get_or_404(thread_id)
    _mark_admin_messages_read(thread)
    return render_template("boxlight/admin_message_thread.html", thread=thread)


@app.route("/boxlight-messages/<int:thread_id>/reply", methods=["POST"])
@admin_login_required
def admin_boxlight_message_reply(thread_id: int):
    thread = BoxlightMessageThread.query.get_or_404(thread_id)
    if thread.is_closed:
        flash("This conversation is closed. Reopen it before replying.", "warning")
        return redirect(url_for("admin_boxlight_message_thread", thread_id=thread.id))

    body = request.form.get("message", "").strip()
    if not body:
        flash("Type a message before sending.", "warning")
        return redirect(url_for("admin_boxlight_message_thread", thread_id=thread.id))

    admin = current_user()
    sender_name = (admin.username if admin else "Pierson Admin")
    db.session.add(BoxlightMessage(
        thread_id=thread.id,
        sender_type="admin",
        sender_name=sender_name,
        body=body,
        read_by_admin=True,
        read_by_boxlight=False,
    ))
    thread.updated_at = datetime.utcnow()
    db.session.commit()
    return redirect(url_for("admin_boxlight_message_thread", thread_id=thread.id))


@app.route("/boxlight-messages/<int:thread_id>/toggle", methods=["POST"])
@admin_login_required
def admin_boxlight_message_toggle(thread_id: int):
    thread = BoxlightMessageThread.query.get_or_404(thread_id)
    thread.is_closed = not thread.is_closed
    thread.updated_at = datetime.utcnow()
    db.session.commit()
    flash("Conversation reopened." if not thread.is_closed else "Conversation closed.", "success")
    return redirect(url_for("admin_boxlight_message_thread", thread_id=thread.id))


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
        try:
            parsed_unit_id = int(unit_id)
        except (TypeError, ValueError):
            flash("That repair selection is invalid.", "danger")
            return redirect(url_for("replacement_stock_admin"))
        unit = get_active_unit(parsed_unit_id)
        if not unit:
            flash("That repair record could not be found.", "danger")
            return redirect(url_for("replacement_stock_admin"))
        panel.used_for_unit_id = unit.id
        panel.status = "Used"
    db.session.commit()
    flash("Replacement stock assignment updated.", "success")
    return redirect(url_for("replacement_stock_admin"))



# ═══════════════════════════════════════════════════════════
# NEW STORY — IT MANAGEMENT ADMIN
# ═══════════════════════════════════════════════════════════


def normalize_new_story_serial(category: str, raw: str) -> str:
    """Preserve the original serial while applying the legacy tracker rules."""
    value = (raw or "").strip()
    if not value:
        return ""
    if category == "Chromebook" and len(value) > 8:
        return value[:8]
    if category == "Windows" and len(value) > 8:
        last9 = value[-9:]
        if last9.upper().startswith("SS"):
            return last9[1:]
        return value[-8:]
    return value


def new_story_activity(req: NewStoryRequest, summary: str, event_type: str = "Update", detail: str = "") -> None:
    actor = current_user().username if current_user() else "Pierson"
    db.session.add(NewStoryActivity(
        request_id=req.id,
        event_type=event_type,
        summary=summary,
        detail=detail,
        actor=actor,
    ))


def next_new_story_ticket() -> str:
    prefix = "NS-"
    highest = 0
    for row in NewStoryRequest.query.filter(NewStoryRequest.ticket_number.like(f"{prefix}%")).all():
        try:
            highest = max(highest, int(row.ticket_number.split("-")[-1]))
        except (ValueError, IndexError):
            pass
    return f"{prefix}{highest + 1:06d}"


def _ns_actor() -> str:
    user = current_user()
    return user.username if user else "Pierson"


def record_inventory_movement(asset: NewStoryAsset | None = None, *, stock_item: NewStoryStockItem | None = None, action: str, quantity: int = 1, request_obj: NewStoryRequest | None = None, shipment: NewStoryShipment | None = None, from_status: str = "", to_status: str = "", from_location_id: int | None = None, to_location_id: int | None = None, notes: str = "") -> None:
    db.session.add(NewStoryInventoryMovement(
        asset_id=asset.id if asset else None,
        stock_item_id=stock_item.id if stock_item else None,
        request_id=request_obj.id if request_obj else None,
        shipment_id=shipment.id if shipment else None,
        action=action, quantity=max(int(quantity or 1), 1),
        from_status=from_status or "", to_status=to_status or "",
        from_location_id=from_location_id, to_location_id=to_location_id,
        actor=_ns_actor(), notes=notes or "",
    ))


def find_new_story_asset(scan_value: str) -> NewStoryAsset | None:
    value = (scan_value or "").strip()
    if not value:
        return None
    return NewStoryAsset.query.filter(or_(
        db.func.lower(NewStoryAsset.serial_number) == value.lower(),
        db.func.lower(NewStoryAsset.raw_serial) == value.lower(),
        db.func.lower(NewStoryAsset.asset_tag) == value.lower(),
    )).first()


@app.route("/new-story")
@admin_login_required
def new_story_dashboard():
    open_statuses = [s for s in NEW_STORY_REQUEST_STATUSES if s not in {"Complete", "Cancelled"}]
    open_requests = NewStoryRequest.query.filter(NewStoryRequest.status.in_(open_statuses)).count()
    needs_attention = NewStoryRequest.query.filter(
        or_(NewStoryRequest.status == "Needs Review", NewStoryRequest.attention_reason != "")
    ).count()
    available_assets = NewStoryAsset.query.filter_by(status="Available").count()
    awaiting_return = NewStoryRequest.query.filter_by(status="Awaiting Return").count()
    open_order_lines = NewStoryOrderLine.query.filter(NewStoryOrderLine.quantity_remaining > 0).count()
    service_events = NewStoryServiceEvent.query.count()
    active_installs = NewStoryInstallation.query.filter(~NewStoryInstallation.status.ilike("%complete%")).count()
    recent = NewStoryRequest.query.order_by(NewStoryRequest.updated_at.desc()).limit(15).all()
    status_counts = {
        status: NewStoryRequest.query.filter_by(status=status).count()
        for status in NEW_STORY_REQUEST_STATUSES
    }
    return render_template(
        "new_story/dashboard.html",
        open_requests=open_requests,
        needs_attention=needs_attention,
        available_assets=available_assets,
        awaiting_return=awaiting_return,
        open_order_lines=open_order_lines,
        service_events=service_events,
        active_installs=active_installs,
        recent=recent,
        status_counts=status_counts,
    )




def _ns_email_field(body: str, labels: list[str], anchored: bool = True) -> str:
    parsing = not anchored
    for line in (body or "").splitlines():
        if re.match(r"^\s*Order\s*Type\s*:", line, re.I):
            parsing = True
        if not parsing:
            continue
        for label in labels:
            match = re.match(r"^\s*" + re.escape(label) + r"\s*:?\s*(.*)$", line, re.I)
            if match and match.group(1).strip():
                return match.group(1).strip()
    return ""


def _ns_route_equipment(raw: str) -> str:
    e = (raw or "").lower()
    e = re.sub(r"\(.*?\)", "", e)
    e = re.sub(r"\bqty\b|\bx\b|\d+", "", e)
    e = re.sub(r"[^a-z\s]", "", e)
    e = re.sub(r"\s+", " ", e).strip()
    if "ipad" in e: return "iPad"
    if "chromebook" in e: return "Chromebook"
    if any(x in e for x in ("laptop", "windows", " pc ")) or e == "pc": return "Windows"
    if "monitor" in e: return "Monitor"
    if "keyboard" in e: return "Keyboard"
    if "dock" in e: return "Docking Station"
    if "jabra" in e or "headset" in e: return "Jabra / Headset"
    if "webcam" in e or "web cam" in e: return "Web Cam"
    if "panel" in e or "smartboard" in e or "smart board" in e: return "Interactive Panel"
    return "Other"


def parse_new_story_manageengine_email(subject: str, body: str) -> dict[str, Any]:
    ticket_match = re.search(r"##\s*(\d+)\s*##", subject or "")
    requester = _ns_email_field(body, ["Request submitted by"])
    recipient = (
        _ns_email_field(body, ["Name of Person Using Device"])
        or _ns_email_field(body, ["Delivery Recipient Name"])
    )
    if not recipient:
        m = re.search(r"(?:equipment\s+)?request\s+for\s+(.+?)\s+has\s+been\s+approved", body or "", re.I | re.S)
        if m:
            recipient = re.sub(r"\s{2,}", " ", m.group(1).strip())
    recipient = recipient or requester
    equipment = _ns_email_field(body, ["Equipment"], anchored=False)
    qty_raw = _ns_email_field(body, ["Quantity"], anchored=False)
    qty_match = re.search(r"\d+", qty_raw or "")
    qty = max(int(qty_match.group(0)), 1) if qty_match else 1
    return {
        "ticket_number": ticket_match.group(1) if ticket_match else (subject or "").strip(),
        "service_type": _ns_email_field(body, ["Order Type"]) or "Other",
        "school_name": _ns_email_field(body, ["Company"]),
        "requester": requester,
        "recipient": recipient,
        "equipment_raw": equipment,
        "category": _ns_route_equipment(equipment),
        "quantity": qty,
        "street": _ns_email_field(body, ["Shipping Address - Street # and Name", "Shipping Address - Street"]),
        "city": _ns_email_field(body, ["Shipping Address - City"]),
        "state": _ns_email_field(body, ["Shipping address - State"]),
        "zip_code": _ns_email_field(body, ["Shipping address - Zip"]),
    }


@app.route("/new-story/intake", methods=["GET", "POST"])
@admin_login_required
def new_story_intake():
    preview = None
    subject = request.form.get("subject", "").strip() if request.method == "POST" else ""
    body = request.form.get("body", "") if request.method == "POST" else ""
    action = request.form.get("action", "preview") if request.method == "POST" else "preview"
    if request.method == "POST":
        preview = parse_new_story_manageengine_email(subject, body)
        if action == "import":
            missing = []
            if not preview["ticket_number"]: missing.append("ticket")
            if not preview["school_name"]: missing.append("school/company")
            if not preview["equipment_raw"]: missing.append("equipment")
            status = "Needs Review" if missing or preview["category"] == "Other" else "Incoming"
            attention = "Import review: missing " + ", ".join(missing) if missing else ("Import review: unclassified equipment" if preview["category"] == "Other" else "")
            req = NewStoryRequest(
                ticket_number=preview["ticket_number"] or next_new_story_ticket(),
                service_type=preview["service_type"],
                status=status,
                school_name=preview["school_name"],
                requester=preview["requester"],
                recipient=preview["recipient"],
                street=preview["street"], city=preview["city"], state=preview["state"], zip_code=preview["zip_code"],
                return_kit_required=preview["service_type"].lower().replace("/", "") == "breakfix",
                attention_reason=attention,
                source="ManageEngine Email",
                source_subject=subject,
                source_raw=body,
            )
            db.session.add(req)
            db.session.flush()
            db.session.add(NewStoryRequestItem(
                request_id=req.id,
                category=preview["category"],
                description=preview["equipment_raw"],
                quantity_requested=preview["quantity"],
            ))
            new_story_activity(req, "Imported from ManageEngine email", "Created")
            db.session.commit()
            flash(f"Imported ticket {req.ticket_number}." + (" Review required." if status == "Needs Review" else ""), "warning" if status == "Needs Review" else "success")
            return redirect(url_for("new_story_request_detail", request_id=req.id))
    return render_template("new_story/intake.html", preview=preview, subject=subject, body=body)


@app.route("/new-story/requests")
@admin_login_required
def new_story_requests():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    service = request.args.get("service", "").strip()
    query = NewStoryRequest.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            NewStoryRequest.ticket_number.ilike(like),
            NewStoryRequest.customer_po.ilike(like),
            NewStoryRequest.school_name.ilike(like),
            NewStoryRequest.requester.ilike(like),
            NewStoryRequest.recipient.ilike(like),
        ))
    if status:
        query = query.filter_by(status=status)
    if service:
        query = query.filter_by(service_type=service)
    rows = query.order_by(NewStoryRequest.updated_at.desc()).limit(500).all()
    return render_template("new_story/requests.html", rows=rows, q=q, status=status, service=service)


@app.route("/new-story/requests/new", methods=["GET", "POST"])
@admin_login_required
def new_story_request_new():
    if request.method == "POST":
        ticket = request.form.get("ticket_number", "").strip() or next_new_story_ticket()
        req = NewStoryRequest(
            ticket_number=ticket,
            customer_po=request.form.get("customer_po", "").strip(),
            project_name=request.form.get("project_name", "").strip(),
            service_type=request.form.get("service_type", "Order").strip() or "Order",
            status=request.form.get("status", "Incoming").strip() or "Incoming",
            school_name=request.form.get("school_name", "").strip(),
            requester=request.form.get("requester", "").strip(),
            recipient=request.form.get("recipient", "").strip(),
            street=request.form.get("street", "").strip(),
            city=request.form.get("city", "").strip(),
            state=request.form.get("state", "").strip(),
            zip_code=request.form.get("zip_code", "").strip(),
            return_kit_required=request.form.get("return_kit_required") == "on",
            source="Manual",
            notes=request.form.get("notes", "").strip(),
        )
        db.session.add(req)
        db.session.flush()
        category = request.form.get("category", "").strip()
        qty_raw = request.form.get("quantity", "1").strip()
        try:
            qty = max(int(qty_raw), 1)
        except ValueError:
            qty = 1
        if category:
            db.session.add(NewStoryRequestItem(
                request_id=req.id,
                category=category,
                description=request.form.get("item_description", "").strip(),
                model=request.form.get("model", "").strip(),
                quantity_requested=qty,
            ))
        new_story_activity(req, "Request created", "Created")
        db.session.commit()
        flash(f"New Story request {ticket} created.", "success")
        return redirect(url_for("new_story_request_detail", request_id=req.id))
    return render_template("new_story/request_form.html", ticket_number=next_new_story_ticket())


@app.route("/new-story/requests/<int:request_id>")
@admin_login_required
def new_story_request_detail(request_id: int):
    req = db.session.get(NewStoryRequest, request_id) or abort(404)
    available_assets = NewStoryAsset.query.filter(
        NewStoryAsset.status.in_(["Available", "Received"])
    ).order_by(NewStoryAsset.category, NewStoryAsset.serial_number).limit(300).all()
    return render_template("new_story/request_detail.html", req=req, available_assets=available_assets)


@app.route("/new-story/requests/<int:request_id>/status", methods=["POST"])
@admin_login_required
def new_story_request_status(request_id: int):
    req = db.session.get(NewStoryRequest, request_id) or abort(404)
    new_status = request.form.get("status", "").strip()
    if new_status not in NEW_STORY_REQUEST_STATUSES:
        flash("Invalid request status.", "danger")
        return redirect(url_for("new_story_request_detail", request_id=req.id))
    old = req.status
    req.status = new_status
    if new_status != "Needs Review" and request.form.get("clear_attention") == "1":
        req.attention_reason = ""
    new_story_activity(req, f"Status changed: {old} → {new_status}", "Status")
    db.session.commit()
    return redirect(url_for("new_story_request_detail", request_id=req.id))


@app.route("/new-story/requests/<int:request_id>/items/add", methods=["POST"])
@admin_login_required
def new_story_request_item_add(request_id: int):
    req = db.session.get(NewStoryRequest, request_id) or abort(404)
    try:
        qty = max(int(request.form.get("quantity", "1")), 1)
    except ValueError:
        qty = 1
    item = NewStoryRequestItem(
        request_id=req.id,
        category=request.form.get("category", "Other").strip() or "Other",
        description=request.form.get("description", "").strip(),
        model=request.form.get("model", "").strip(),
        quantity_requested=qty,
        requirement_type=request.form.get("requirement_type", "Equipment").strip() or "Equipment",
        shortage_reason=request.form.get("shortage_reason", "").strip(),
    )
    db.session.add(item)
    if item.shortage_reason:
        req.attention_reason = item.shortage_reason
    new_story_activity(req, f"Added request item: {item.category} × {qty}", "Item")
    db.session.commit()
    return redirect(url_for("new_story_request_detail", request_id=req.id))


@app.route("/new-story/requests/<int:request_id>/note", methods=["POST"])
@admin_login_required
def new_story_request_note(request_id: int):
    req = db.session.get(NewStoryRequest, request_id) or abort(404)
    note = request.form.get("note", "").strip()
    if note:
        new_story_activity(req, "Note added", "Note", note)
        db.session.commit()
    return redirect(url_for("new_story_request_detail", request_id=req.id))


@app.route("/new-story/requests/<int:request_id>/allocate", methods=["POST"])
@admin_login_required
def new_story_allocate_asset(request_id: int):
    req = db.session.get(NewStoryRequest, request_id) or abort(404)
    try:
        asset_id = int(request.form.get("asset_id", ""))
    except ValueError:
        flash("Choose a valid asset.", "danger")
        return redirect(url_for("new_story_request_detail", request_id=req.id))
    asset = db.session.get(NewStoryAsset, asset_id) or abort(404)
    if asset.status not in {"Available", "Received"}:
        flash("That asset is not currently available.", "warning")
        return redirect(url_for("new_story_request_detail", request_id=req.id))
    old_status = asset.status
    asset.assigned_request_id = req.id
    asset.status = "Allocated"
    label = asset.serial_number or asset.asset_tag or f"Asset #{asset.id}"
    record_inventory_movement(asset, action="Allocate", request_obj=req, from_status=old_status, to_status="Allocated")
    new_story_activity(req, f"Allocated {asset.category}: {label}", "Allocation")
    db.session.commit()
    return redirect(url_for("new_story_request_detail", request_id=req.id))


@app.route("/new-story/assets")
@admin_login_required
def new_story_assets():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    category = request.args.get("category", "").strip()
    location_id = request.args.get("location_id", "").strip()
    query = NewStoryAsset.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            NewStoryAsset.serial_number.ilike(like),
            NewStoryAsset.raw_serial.ilike(like),
            NewStoryAsset.asset_tag.ilike(like),
            NewStoryAsset.customer_po.ilike(like),
            NewStoryAsset.model.ilike(like),
            NewStoryAsset.assigned_to.ilike(like),
            NewStoryAsset.room.ilike(like),
        ))
    if status:
        query = query.filter_by(status=status)
    if category:
        query = query.filter_by(category=category)
    if location_id.isdigit():
        query = query.filter_by(current_location_id=int(location_id))
    rows = query.order_by(NewStoryAsset.updated_at.desc()).limit(1500).all()
    locations = NewStoryLocation.query.filter_by(active=True).order_by(NewStoryLocation.name).all()
    status_counts = dict(db.session.query(NewStoryAsset.status, db.func.count(NewStoryAsset.id)).group_by(NewStoryAsset.status).all())
    category_counts = db.session.query(NewStoryAsset.category, db.func.count(NewStoryAsset.id)).group_by(NewStoryAsset.category).order_by(NewStoryAsset.category).all()
    return render_template("new_story/assets.html", rows=rows, q=q, status=status, category=category, location_id=location_id, locations=locations, status_counts=status_counts, category_counts=category_counts)


@app.route("/new-story/assets/<int:asset_id>")
@admin_login_required
def new_story_asset_detail(asset_id: int):
    asset = db.session.get(NewStoryAsset, asset_id) or abort(404)
    locations = NewStoryLocation.query.filter_by(active=True).order_by(NewStoryLocation.name).all()
    open_requests = NewStoryRequest.query.filter(~NewStoryRequest.status.in_(["Complete", "Cancelled"])).order_by(NewStoryRequest.updated_at.desc()).limit(500).all()
    return render_template("new_story/asset_detail.html", asset=asset, locations=locations, open_requests=open_requests)


@app.route("/new-story/assets/<int:asset_id>/update", methods=["POST"])
@admin_login_required
def new_story_asset_update(asset_id: int):
    asset = db.session.get(NewStoryAsset, asset_id) or abort(404)
    old_status, old_loc = asset.status, asset.current_location_id
    status = request.form.get("status", asset.status).strip() or asset.status
    if status not in NEW_STORY_ASSET_STATUSES:
        flash("Invalid asset status.", "danger")
        return redirect(url_for("new_story_asset_detail", asset_id=asset.id))
    loc_raw = request.form.get("location_id", "").strip()
    new_loc = int(loc_raw) if loc_raw.isdigit() else None
    req_raw = request.form.get("request_id", "").strip()
    new_req = int(req_raw) if req_raw.isdigit() else None
    asset.status = status
    asset.current_location_id = new_loc
    asset.assigned_request_id = new_req
    asset.assigned_to = request.form.get("assigned_to", "").strip()
    asset.room = request.form.get("room", "").strip()
    asset.model = request.form.get("model", asset.model).strip()
    asset.description = request.form.get("description", asset.description).strip()
    if status in {"Deployed", "In Use"} and not asset.deployed_at:
        asset.deployed_at = datetime.utcnow()
    if status in {"Retired", "Scrapped"} and not asset.retired_at:
        asset.retired_at = datetime.utcnow()
    if status != old_status or new_loc != old_loc:
        record_inventory_movement(asset, action="Asset Update", request_obj=asset.assigned_request, from_status=old_status, to_status=status, from_location_id=old_loc, to_location_id=new_loc, notes=request.form.get("notes", "").strip())
    db.session.commit()
    flash("Asset updated.", "success")
    return redirect(url_for("new_story_asset_detail", asset_id=asset.id))


@app.route("/new-story/assets/bulk", methods=["POST"])
@admin_login_required
def new_story_assets_bulk():
    ids = [int(x) for x in request.form.getlist("asset_ids") if x.isdigit()]
    if not ids:
        flash("Select at least one asset.", "warning")
        return redirect(url_for("new_story_assets"))
    action = request.form.get("bulk_action", "").strip()
    status = request.form.get("bulk_status", "").strip()
    loc_raw = request.form.get("bulk_location_id", "").strip()
    location_id = int(loc_raw) if loc_raw.isdigit() else None
    assets = NewStoryAsset.query.filter(NewStoryAsset.id.in_(ids)).all()
    for asset in assets:
        old_status, old_loc = asset.status, asset.current_location_id
        if action == "status" and status in NEW_STORY_ASSET_STATUSES:
            asset.status = status
        elif action == "location":
            asset.current_location_id = location_id
        elif action == "clear_request":
            asset.assigned_request_id = None
            if asset.status in {"Allocated", "Reserved"}: asset.status = "Available"
        record_inventory_movement(asset, action="Bulk Update", request_obj=asset.assigned_request, from_status=old_status, to_status=asset.status, from_location_id=old_loc, to_location_id=asset.current_location_id)
    db.session.commit()
    flash(f"Updated {len(assets)} asset(s).", "success")
    return redirect(url_for("new_story_assets"))


@app.route("/new-story/assets/add", methods=["POST"])
@admin_login_required
def new_story_asset_add():
    category = request.form.get("category", "Other").strip() or "Other"
    raw_serial = request.form.get("serial_number", "").strip()
    serial = normalize_new_story_serial(category, raw_serial)
    if serial and NewStoryAsset.query.filter(db.func.lower(NewStoryAsset.serial_number) == serial.lower()).first():
        flash("That normalized serial already exists in New Story inventory.", "warning")
        return redirect(url_for("new_story_assets"))
    asset = NewStoryAsset(
        category=category,
        description=request.form.get("description", "").strip(),
        model=request.form.get("model", "").strip(),
        raw_serial=raw_serial,
        serial_number=serial,
        asset_tag=request.form.get("asset_tag", "").strip(),
        customer_po=request.form.get("customer_po", "").strip(),
        vendor_order=request.form.get("vendor_order", "").strip(),
        status=request.form.get("status", "Available").strip() or "Available",
        received_at=datetime.utcnow(),
    )
    db.session.add(asset)
    db.session.flush()
    record_inventory_movement(asset, action="Manual Add", from_status="", to_status=asset.status, notes="Asset created manually")
    db.session.commit()
    flash("Asset added to New Story inventory.", "success")
    return redirect(url_for("new_story_assets"))


@app.route("/new-story/inventory")
@admin_login_required
def new_story_inventory():
    serialized_total = NewStoryAsset.query.count()
    on_hand_statuses = ["Received", "Available", "Reserved", "Allocated", "Processing", "Ready to Ship", "Returned", "Repair"]
    on_hand = NewStoryAsset.query.filter(NewStoryAsset.status.in_(on_hand_statuses)).count()
    deployed = NewStoryAsset.query.filter(NewStoryAsset.status.in_(["Shipped", "Deployed", "In Use", "Return Pending"])).count()
    exceptions = NewStoryAsset.query.filter(NewStoryAsset.status.in_(["Lost", "Repair"])).count()
    by_category = db.session.query(NewStoryAsset.category, db.func.count(NewStoryAsset.id)).group_by(NewStoryAsset.category).order_by(NewStoryAsset.category).all()
    by_status = db.session.query(NewStoryAsset.status, db.func.count(NewStoryAsset.id)).group_by(NewStoryAsset.status).order_by(NewStoryAsset.status).all()
    stock = NewStoryStockItem.query.filter_by(active=True).order_by(NewStoryStockItem.category, NewStoryStockItem.description).all()
    recent = NewStoryInventoryMovement.query.order_by(NewStoryInventoryMovement.created_at.desc()).limit(40).all()
    return render_template("new_story/inventory.html", serialized_total=serialized_total, on_hand=on_hand, deployed=deployed, exceptions=exceptions, by_category=by_category, by_status=by_status, stock=stock, recent=recent)


@app.route("/new-story/inventory/scan", methods=["GET", "POST"])
@admin_login_required
def new_story_inventory_scan():
    locations = NewStoryLocation.query.filter_by(active=True).order_by(NewStoryLocation.name).all()
    open_requests = NewStoryRequest.query.filter(~NewStoryRequest.status.in_(["Complete", "Cancelled"])).order_by(NewStoryRequest.updated_at.desc()).limit(600).all()
    result = None
    if request.method == "POST":
        value = request.form.get("scan_value", "").strip()
        action = request.form.get("action", "Lookup").strip()
        asset = find_new_story_asset(value)
        if not asset and action == "Receive New":
            category = request.form.get("category", "Other").strip() or "Other"
            serial = normalize_new_story_serial(category, value)
            if serial and NewStoryAsset.query.filter(db.func.lower(NewStoryAsset.serial_number) == serial.lower()).first():
                flash("That normalized serial already exists.", "warning")
            else:
                asset = NewStoryAsset(category=category, raw_serial=value, serial_number=serial, model=request.form.get("model", "").strip(), customer_po=request.form.get("customer_po", "").strip(), vendor_order=request.form.get("vendor_order", "").strip(), status="Available", received_at=datetime.utcnow())
                db.session.add(asset); db.session.flush()
                record_inventory_movement(asset, action="Receive", from_status="", to_status="Available", notes="Scanner receive")
                db.session.commit(); flash(f"Received {serial or value}.", "success")
        elif not asset:
            flash("Serial / asset tag not found.", "danger")
        else:
            old_status, old_loc = asset.status, asset.current_location_id
            req_raw = request.form.get("request_id", "").strip()
            req = db.session.get(NewStoryRequest, int(req_raw)) if req_raw.isdigit() else asset.assigned_request
            loc_raw = request.form.get("location_id", "").strip()
            loc = int(loc_raw) if loc_raw.isdigit() else asset.current_location_id
            if action == "Lookup":
                pass
            elif action == "Make Available":
                asset.status = "Available"; asset.assigned_request_id = None
            elif action == "Reserve":
                asset.status = "Reserved"; asset.assigned_request_id = req.id if req else None
            elif action == "Allocate":
                if not req: flash("Choose a request before allocating.", "warning")
                else: asset.status = "Allocated"; asset.assigned_request_id = req.id
            elif action == "Stage":
                asset.status = "Ready to Ship"; asset.assigned_request_id = req.id if req else asset.assigned_request_id
            elif action == "Deploy":
                asset.status = "Deployed"; asset.current_location_id = loc; asset.assigned_to = request.form.get("assigned_to", asset.assigned_to).strip(); asset.room = request.form.get("room", asset.room).strip(); asset.deployed_at = asset.deployed_at or datetime.utcnow()
            elif action == "Receive Return":
                asset.status = "Returned"; asset.current_location_id = loc; asset.assigned_request_id = req.id if req else asset.assigned_request_id
            elif action == "Send to Repair":
                asset.status = "Repair"
            elif action == "Scrap":
                asset.status = "Scrapped"; asset.retired_at = datetime.utcnow()
            elif action == "Lost":
                asset.status = "Lost"
            if action != "Lookup" and (asset.status != old_status or asset.current_location_id != old_loc or asset.assigned_request_id != (req.id if req else asset.assigned_request_id)):
                record_inventory_movement(asset, action=action, request_obj=req, from_status=old_status, to_status=asset.status, from_location_id=old_loc, to_location_id=asset.current_location_id, notes=request.form.get("notes", "").strip())
                if req: new_story_activity(req, f"{action}: {asset.serial_number or asset.asset_tag or 'asset'}", "Inventory")
                db.session.commit(); flash(f"{asset.serial_number or asset.asset_tag or 'Asset'} → {asset.status}", "success")
            result = asset
    recent = NewStoryInventoryMovement.query.order_by(NewStoryInventoryMovement.created_at.desc()).limit(20).all()
    return render_template("new_story/scan.html", locations=locations, open_requests=open_requests, result=result, recent=recent)


@app.route("/new-story/inventory/stock/add", methods=["POST"])
@admin_login_required
def new_story_stock_add():
    try: qty=max(int(request.form.get("quantity", "0")),0)
    except ValueError: qty=0
    try: reorder=max(int(request.form.get("reorder_level", "0")),0)
    except ValueError: reorder=0
    loc_raw=request.form.get("location_id", "").strip()
    item=NewStoryStockItem(category=request.form.get("category","Other").strip() or "Other", description=request.form.get("description","").strip(), model=request.form.get("model","").strip(), customer_po=request.form.get("customer_po","").strip(), vendor_order=request.form.get("vendor_order","").strip(), location_id=int(loc_raw) if loc_raw.isdigit() else None, quantity_on_hand=qty, reorder_level=reorder)
    db.session.add(item); db.session.flush()
    if qty: record_inventory_movement(stock_item=item, action="Initial Stock", quantity=qty, notes="Stock item created")
    db.session.commit(); flash("Stock item added.", "success")
    return redirect(url_for("new_story_inventory"))


@app.route("/new-story/inventory/stock/<int:stock_id>/adjust", methods=["POST"])
@admin_login_required
def new_story_stock_adjust(stock_id: int):
    item=db.session.get(NewStoryStockItem, stock_id) or abort(404)
    try: qty=int(request.form.get("quantity", "0"))
    except ValueError: qty=0
    mode=request.form.get("mode", "adjust")
    old=item.quantity_on_hand
    if mode == "set": item.quantity_on_hand=max(qty,0); delta=item.quantity_on_hand-old
    else: item.quantity_on_hand=max(item.quantity_on_hand+qty,0); delta=item.quantity_on_hand-old
    if item.quantity_reserved > item.quantity_on_hand: item.quantity_reserved=item.quantity_on_hand
    if delta: record_inventory_movement(stock_item=item, action="Stock Adjustment", quantity=abs(delta), notes=("+" if delta>0 else "-")+str(abs(delta))+" | "+request.form.get("notes","").strip())
    db.session.commit(); flash("Stock quantity updated.", "success")
    return redirect(url_for("new_story_inventory"))


@app.route("/new-story/receiving", methods=["GET", "POST"])
@admin_login_required
def new_story_receiving():
    if request.method == "POST":
        category = request.form.get("category", "Other").strip() or "Other"
        try:
            qty = max(int(request.form.get("quantity", "1")), 1)
        except ValueError:
            qty = 1
        if qty > 1000:
            flash("Receive batches of 1,000 or fewer at a time.", "warning")
            return redirect(url_for("new_story_receiving"))
        po = request.form.get("customer_po", "").strip()
        vendor = request.form.get("vendor_order", "").strip()
        model = request.form.get("model", "").strip()
        desc = request.form.get("description", "").strip()
        serialized = request.form.get("serialized") == "on"
        if serialized:
            for _ in range(qty):
                asset = NewStoryAsset(category=category, description=desc, model=model, customer_po=po, vendor_order=vendor, status="Received", received_at=datetime.utcnow())
                db.session.add(asset); db.session.flush()
                record_inventory_movement(asset, action="Receive", from_status="", to_status="Received", notes=f"Bulk receipt {po} {vendor}".strip())
        else:
            stock = NewStoryStockItem.query.filter_by(category=category, description=desc, model=model, customer_po=po, vendor_order=vendor).first()
            if not stock:
                stock = NewStoryStockItem(category=category, description=desc, model=model, customer_po=po, vendor_order=vendor, quantity_on_hand=0)
                db.session.add(stock); db.session.flush()
            stock.quantity_on_hand += qty
            record_inventory_movement(stock_item=stock, action="Receive", quantity=qty, notes=f"Bulk receipt {po} {vendor}".strip())
        db.session.commit()
        flash(f"Received {qty} {category} item(s).", "success")
        return redirect(url_for("new_story_receiving"))
    recent = NewStoryAsset.query.filter(NewStoryAsset.received_at.isnot(None)).order_by(NewStoryAsset.received_at.desc()).limit(30).all()
    return render_template("new_story/receiving.html", recent=recent)


@app.route("/new-story/shipments", methods=["GET", "POST"])
@admin_login_required
def new_story_shipments():
    if request.method == "POST":
        try:
            req_id = int(request.form.get("request_id", ""))
        except ValueError:
            flash("Select a request.", "danger")
            return redirect(url_for("new_story_shipments"))
        req = db.session.get(NewStoryRequest, req_id) or abort(404)
        shipment = NewStoryShipment(
            request_id=req.id,
            direction=request.form.get("direction", "Outbound"),
            method=request.form.get("method", "FedEx Ground").strip(),
            tracking_number=request.form.get("tracking_number", "").strip(),
            shipped_at=datetime.utcnow() if request.form.get("mark_shipped") == "on" else None,
            notes=request.form.get("notes", "").strip(),
        )
        # Shipments are built explicitly by scanning assets into the batch.
        # Never auto-attach every allocated asset on a ticket; that can ship the wrong hardware.
        shipment.shipped_at = None
        db.session.add(shipment)
        db.session.flush()
        new_story_activity(req, f"{shipment.direction} shipment batch created" + (f": {shipment.tracking_number}" if shipment.tracking_number else ""), "Shipment")
        db.session.commit()
        flash("Shipment batch created. Scan the exact assets into it before marking it shipped.", "success")
        return redirect(url_for("new_story_shipment_detail", shipment_id=shipment.id))
    rows = NewStoryShipment.query.order_by(NewStoryShipment.created_at.desc()).limit(250).all()
    open_requests = NewStoryRequest.query.filter(~NewStoryRequest.status.in_(["Complete", "Cancelled"])).order_by(NewStoryRequest.updated_at.desc()).all()
    return render_template("new_story/shipments.html", rows=rows, open_requests=open_requests)


@app.route("/new-story/shipments/<int:shipment_id>", methods=["GET", "POST"])
@admin_login_required
def new_story_shipment_detail(shipment_id: int):
    shipment=db.session.get(NewStoryShipment, shipment_id) or abort(404)
    if request.method == "POST":
        value=request.form.get("scan_value", "").strip()
        asset=find_new_story_asset(value)
        if not asset:
            flash("Serial / asset tag not found.", "danger")
        elif NewStoryShipmentItem.query.filter_by(shipment_id=shipment.id, asset_id=asset.id).first():
            flash("Asset is already on this shipment.", "warning")
        else:
            old=asset.status
            target_status = "Return Pending" if shipment.direction.lower().startswith("in") else "Ready to Ship"
            asset.status=target_status; asset.assigned_request_id=shipment.request_id
            db.session.add(NewStoryShipmentItem(shipment_id=shipment.id, asset_id=asset.id))
            record_inventory_movement(asset, action="Add to Return" if target_status == "Return Pending" else "Add to Shipment", request_obj=shipment.request, shipment=shipment, from_status=old, to_status=target_status)
            new_story_activity(shipment.request, f"Added {asset.serial_number or asset.asset_tag or 'asset'} to shipment", "Shipment")
            db.session.commit(); flash("Asset added to shipment.", "success")
    return render_template("new_story/shipment_detail.html", shipment=shipment)


@app.route("/new-story/shipments/<int:shipment_id>/remove/<int:asset_id>", methods=["POST"])
@admin_login_required
def new_story_shipment_remove_asset(shipment_id: int, asset_id: int):
    shipment=db.session.get(NewStoryShipment, shipment_id) or abort(404)
    link=NewStoryShipmentItem.query.filter_by(shipment_id=shipment.id, asset_id=asset_id).first() or abort(404)
    asset=link.asset; old=asset.status
    db.session.delete(link); asset.status="Allocated" if asset.assigned_request_id else "Available"
    record_inventory_movement(asset, action="Remove from Shipment", request_obj=shipment.request, shipment=shipment, from_status=old, to_status=asset.status)
    db.session.commit(); flash("Asset removed from shipment.", "success")
    return redirect(url_for("new_story_shipment_detail", shipment_id=shipment.id))


@app.route("/new-story/shipments/<int:shipment_id>/ship", methods=["POST"])
@admin_login_required
def new_story_shipment_ship(shipment_id: int):
    shipment=db.session.get(NewStoryShipment, shipment_id) or abort(404)
    if not shipment.items:
        flash("Add at least one asset before shipping.", "warning")
        return redirect(url_for("new_story_shipment_detail", shipment_id=shipment.id))
    shipment.tracking_number=request.form.get("tracking_number", shipment.tracking_number).strip()
    shipment.method=request.form.get("method", shipment.method).strip() or shipment.method
    inbound = shipment.direction.lower().startswith("in")
    if inbound:
        shipment.delivered_at = shipment.delivered_at or datetime.utcnow()
        for link in shipment.items:
            a=link.asset; old=a.status; a.status="Returned"; a.current_location_id=None
            record_inventory_movement(a, action="Receive Return", request_obj=shipment.request, shipment=shipment, from_status=old, to_status="Returned", notes=shipment.tracking_number)
        shipment.request.status="Processing"
        new_story_activity(shipment.request, f"Return received: {shipment.tracking_number or shipment.method}", "Return")
        message="Return marked received."
    else:
        shipment.shipped_at=shipment.shipped_at or datetime.utcnow()
        for link in shipment.items:
            a=link.asset; old=a.status; a.status="Shipped"
            record_inventory_movement(a, action="Ship", request_obj=shipment.request, shipment=shipment, from_status=old, to_status="Shipped", notes=shipment.tracking_number)
        shipment.request.status="Awaiting Return" if shipment.request.return_kit_required else "Shipped"
        new_story_activity(shipment.request, f"Shipment sent: {shipment.tracking_number or shipment.method}", "Shipment")
        message="Shipment marked shipped."
    db.session.commit(); flash(message, "success")
    return redirect(url_for("new_story_shipment_detail", shipment_id=shipment.id))


@app.route("/new-story/exceptions")
@admin_login_required
def new_story_exceptions():
    requests = NewStoryRequest.query.filter(or_(
        NewStoryRequest.status == "Needs Review",
        NewStoryRequest.attention_reason != "",
    )).order_by(NewStoryRequest.updated_at.desc()).all()
    shortage_items = NewStoryRequestItem.query.filter(NewStoryRequestItem.shortage_reason != "").order_by(NewStoryRequestItem.created_at.desc()).all()
    return render_template("new_story/exceptions.html", requests=requests, shortage_items=shortage_items)


@app.route("/new-story/locations", methods=["GET", "POST"])
@admin_login_required
def new_story_locations():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Location name is required.", "danger")
            return redirect(url_for("new_story_locations"))
        db.session.add(NewStoryLocation(
            name=name,
            street=request.form.get("street", "").strip(),
            city=request.form.get("city", "").strip(),
            state=request.form.get("state", "").strip(),
            zip_code=request.form.get("zip_code", "").strip(),
            location_type=request.form.get("location_type", "School").strip() or "School",
        ))
        db.session.commit()
        flash("Location added.", "success")
        return redirect(url_for("new_story_locations"))
    rows = NewStoryLocation.query.order_by(NewStoryLocation.name).all()
    return render_template("new_story/locations.html", rows=rows)


def _legacy_date(value: dict[str, str] | None) -> datetime | None:
    iso = (value or {}).get("iso", "")
    if not iso:
        return None
    try:
        return datetime.strptime(iso, "%Y-%m-%d")
    except ValueError:
        return None


def _legacy_date_raw(value: dict[str, str] | None) -> str:
    return (value or {}).get("raw", "")


@app.route("/new-story/procurement")
@admin_login_required
def new_story_procurement():
    q = request.args.get("q", "").strip()
    outstanding = request.args.get("outstanding", "").strip()
    query = NewStoryOrderLine.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            NewStoryOrderLine.customer_po.ilike(like),
            NewStoryOrderLine.vendor_order.ilike(like),
            NewStoryOrderLine.vendor_invoice.ilike(like),
            NewStoryOrderLine.description.ilike(like),
            NewStoryOrderLine.project_name.ilike(like),
        ))
    if outstanding == "1":
        query = query.filter(NewStoryOrderLine.quantity_remaining > 0)
    rows = query.order_by(NewStoryOrderLine.order_date.desc(), NewStoryOrderLine.id.desc()).limit(1000).all()
    totals = db.session.query(
        db.func.coalesce(db.func.sum(NewStoryOrderLine.quantity_ordered), 0),
        db.func.coalesce(db.func.sum(NewStoryOrderLine.quantity_received), 0),
        db.func.coalesce(db.func.sum(NewStoryOrderLine.quantity_shipped), 0),
        db.func.coalesce(db.func.sum(NewStoryOrderLine.quantity_remaining), 0),
    ).first()
    return render_template("new_story/procurement.html", rows=rows, q=q, outstanding=outstanding, totals=totals)


@app.route("/new-story/service-history")
@admin_login_required
def new_story_service_history():
    q = request.args.get("q", "").strip()
    outcome = request.args.get("outcome", "").strip()
    query = NewStoryServiceEvent.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            NewStoryServiceEvent.ticket_number.ilike(like),
            NewStoryServiceEvent.serial_number.ilike(like),
            NewStoryServiceEvent.asset_tag.ilike(like),
            NewStoryServiceEvent.person.ilike(like),
            NewStoryServiceEvent.issue_category.ilike(like),
        ))
    if outcome:
        query = query.filter(NewStoryServiceEvent.outcome == outcome)
    rows = query.order_by(NewStoryServiceEvent.event_date.desc(), NewStoryServiceEvent.id.desc()).limit(1000).all()
    outcomes = [r[0] for r in db.session.query(NewStoryServiceEvent.outcome).distinct().order_by(NewStoryServiceEvent.outcome).all() if r[0]]
    return render_template("new_story/service_history.html", rows=rows, q=q, outcome=outcome, outcomes=outcomes)


@app.route("/new-story/installations")
@admin_login_required
def new_story_installations():
    q = request.args.get("q", "").strip()
    query = NewStoryInstallation.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            NewStoryInstallation.school_name.ilike(like),
            NewStoryInstallation.customer_po.ilike(like),
            NewStoryInstallation.contact.ilike(like),
            NewStoryInstallation.rooms.ilike(like),
        ))
    rows = query.order_by(NewStoryInstallation.install_date.desc(), NewStoryInstallation.id.desc()).limit(750).all()
    return render_template("new_story/installations.html", rows=rows, q=q)


@app.route("/new-story/domains")
@admin_login_required
def new_story_domains():
    rows = NewStoryDomain.query.order_by(NewStoryDomain.name).all()
    return render_template("new_story/domains.html", rows=rows)


@app.route("/new-story/legacy-import", methods=["GET", "POST"])
@admin_login_required
def new_story_legacy_import():
    seed_path = BASE_DIR / "data" / "new_story_legacy_seed.json"
    if not seed_path.exists():
        abort(404)
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    source = payload.get("source", {})
    source_hash = source.get("sha256", "")
    prior = NewStoryLegacyImport.query.filter_by(source_hash=source_hash).first() if source_hash else None

    if request.method == "POST":
        if prior:
            flash("This workbook snapshot has already been imported. Nothing was duplicated.", "warning")
            return redirect(url_for("new_story_legacy_import"))

        # Locations first so imported requests/installations can link to them.
        location_map = {r.name.strip().lower(): r for r in NewStoryLocation.query.all() if r.name}
        pending_locations = {}
        for r in payload.get("requests", []):
            name = (r.get("school_name") or "").strip()
            if name and name.lower() not in location_map:
                pending_locations.setdefault(name.lower(), NewStoryLocation(
                    name=name, street=r.get("street", ""), city=r.get("city", ""),
                    state=r.get("state", ""), zip_code=r.get("zip_code", ""), location_type="School"
                ))
        for row in payload.get("installations", []):
            name = (row.get("school_name") or "").strip()
            if name and name.lower() not in location_map and name.lower() not in pending_locations:
                pending_locations[name.lower()] = NewStoryLocation(name=name, location_type="School")
        if pending_locations:
            db.session.add_all(list(pending_locations.values()))
            db.session.flush()
            location_map.update(pending_locations)

        request_map = {}
        ticket_map = {}
        request_objs = []
        for row in payload.get("requests", []):
            loc = location_map.get((row.get("school_name") or "").strip().lower())
            obj = NewStoryRequest(
                ticket_number=row.get("ticket_number", ""), customer_po=row.get("customer_po", ""),
                project_name=row.get("project_name", ""), service_type=row.get("service_type", "Order") or "Order",
                status=row.get("status", "Incoming") or "Incoming", location_id=loc.id if loc else None,
                school_name=row.get("school_name", ""), requester=row.get("requester", ""), recipient=row.get("recipient", ""),
                street=row.get("street", ""), city=row.get("city", ""), state=row.get("state", ""), zip_code=row.get("zip_code", ""),
                return_kit_required=bool(row.get("return_kit_required")), attention_reason=row.get("attention_reason", ""),
                source="Legacy Workbook", notes=row.get("notes", "")
            )
            request_objs.append(obj)
            request_map[row.get("legacy_key", "")] = obj
            if row.get("ticket_number"):
                ticket_map.setdefault(str(row.get("ticket_number")), obj)
        db.session.add_all(request_objs)
        db.session.flush()

        item_objs = []
        asset_objs = []
        asset_map = {}
        serial_map = {}
        shipment_specs = []
        for row in payload.get("requests", []):
            req = request_map[row.get("legacy_key", "")]
            for item in row.get("items", []):
                item_objs.append(NewStoryRequestItem(
                    request_id=req.id, category=item.get("category", "Other"), description=item.get("description", ""),
                    model=item.get("model", ""), quantity_requested=int(item.get("quantity_requested") or 0),
                    quantity_fulfilled=int(item.get("quantity_fulfilled") or 0), requirement_type=item.get("requirement_type", "Equipment"),
                    shortage_reason=item.get("shortage_reason", "")
                ))
            for a in row.get("assets", []):
                obj = NewStoryAsset(
                    category=a.get("category", "Other"), description=a.get("description", ""), model=a.get("model", ""),
                    raw_serial=a.get("raw_serial", ""), serial_number=a.get("serial_number", ""), asset_tag=a.get("asset_tag", ""),
                    customer_po=a.get("customer_po", ""), vendor_order=a.get("vendor_order", ""), status=a.get("status", "Available") or "Available",
                    assigned_request_id=req.id
                )
                asset_objs.append(obj)
                asset_map[a.get("legacy_key", "")] = obj
                if a.get("serial_number"):
                    serial_map.setdefault(a.get("serial_number").strip().lower(), obj)
            for sh in row.get("shipments", []):
                shipment_specs.append((req, sh))
        for a in payload.get("standalone_inventory", []):
            obj = NewStoryAsset(
                category=a.get("category", "Other"), description=a.get("description", ""), model=a.get("model", ""),
                raw_serial=a.get("raw_serial", ""), serial_number=a.get("serial_number", ""), asset_tag=a.get("asset_tag", ""),
                customer_po=a.get("customer_po", ""), vendor_order=a.get("vendor_order", ""), status=a.get("status", "Available") or "Available",
                received_at=_legacy_date(a.get("received_date"))
            )
            asset_objs.append(obj)
            asset_map[a.get("legacy_key", "")] = obj
            if a.get("serial_number"):
                serial_map.setdefault(a.get("serial_number").strip().lower(), obj)
        db.session.add_all(item_objs + asset_objs)
        db.session.flush()

        shipment_objs = []
        shipment_links = []
        for req, sh in shipment_specs:
            sobj = NewStoryShipment(
                request_id=req.id, direction=sh.get("direction", "Outbound"), method=sh.get("method", "Legacy shipment"),
                tracking_number=sh.get("tracking_number", ""), shipped_at=_legacy_date(sh.get("shipped_date")), notes=sh.get("notes", "")
            )
            shipment_objs.append(sobj)
            shipment_links.append((sobj, sh.get("asset_keys", [])))
        db.session.add_all(shipment_objs)
        db.session.flush()
        ship_item_objs = []
        for sobj, keys in shipment_links:
            for key in keys:
                asset = asset_map.get(key)
                if asset:
                    ship_item_objs.append(NewStoryShipmentItem(shipment_id=sobj.id, asset_id=asset.id))
        db.session.add_all(ship_item_objs)

        order_objs = [NewStoryOrderLine(
            status=r.get("status", ""), order_date=_legacy_date(r.get("order_date")), order_date_raw=_legacy_date_raw(r.get("order_date")),
            customer_po=r.get("customer_po", ""), project_name=r.get("project_name", ""), vendor_invoice=r.get("vendor_invoice", ""),
            vendor_order=r.get("vendor_order", ""), description=r.get("description", ""), tracking_number=r.get("tracking_number", ""),
            quantity_ordered=int(r.get("quantity_ordered") or 0), sales_order=r.get("sales_order", ""), quantity_received=int(r.get("quantity_received") or 0),
            quantity_shipped=int(r.get("quantity_shipped") or 0), quantity_remaining=int(r.get("quantity_remaining") or 0)
        ) for r in payload.get("order_lines", [])]
        db.session.add_all(order_objs)

        service_objs = []
        for r in payload.get("service_events", []):
            req = ticket_map.get(str(r.get("ticket_number", "")))
            serial = normalize_new_story_serial(r.get("device_type", ""), r.get("serial_number", ""))
            asset = serial_map.get(serial.strip().lower()) if serial else None
            service_objs.append(NewStoryServiceEvent(
                request_id=req.id if req else None, asset_id=asset.id if asset else None, person=r.get("person", ""),
                event_date=_legacy_date(r.get("event_date")), event_date_raw=_legacy_date_raw(r.get("event_date")),
                device_type=r.get("device_type", ""), serial_number=r.get("serial_number", ""), asset_tag=r.get("asset_tag", ""),
                issue_category=r.get("issue_category", ""), tracking_number=r.get("tracking_number", ""), ticket_number=r.get("ticket_number", ""),
                outcome=r.get("outcome", ""), resolved=bool(r.get("resolved")), comments=r.get("comments", ""), customer_update=r.get("customer_update", "")
            ))
        db.session.add_all(service_objs)

        install_objs = []
        for r in payload.get("installations", []):
            loc = location_map.get((r.get("school_name") or "").strip().lower())
            install_objs.append(NewStoryInstallation(
                location_id=loc.id if loc else None, install_date=_legacy_date(r.get("install_date")), install_date_raw=_legacy_date_raw(r.get("install_date")),
                arrival_time=r.get("arrival_time", ""), availability=r.get("availability", ""), school_name=r.get("school_name", ""),
                address=r.get("address", ""), contact=r.get("contact", ""), phone=r.get("phone", ""), panel_count=int(r.get("panel_count") or 0),
                rooms=r.get("rooms", ""), customer_po=r.get("customer_po", ""), status=r.get("status", ""), expected_range=r.get("expected_range", ""),
                notes=r.get("notes", ""), floor_plan=r.get("floor_plan", "")
            ))
        db.session.add_all(install_objs)

        db.session.add_all([NewStoryDomain(
            name=r.get("name", ""), tenant=r.get("tenant", ""), domain=r.get("domain", ""), parent_ou=r.get("parent_ou", ""), notes=r.get("notes", "")
        ) for r in payload.get("domains", [])])

        # Preserve the small CY operational list as request activity when a ticket match exists.
        matched_notes = 0
        for r in payload.get("operational_notes", []):
            req = ticket_map.get(str(r.get("ticket_number", "")))
            if not req:
                continue
            detail = " | ".join(x for x in [r.get("notes", ""), r.get("status", ""), r.get("resolution", "")] if x)
            db.session.add(NewStoryActivity(request_id=req.id, event_type="Legacy Note", summary=r.get("issue", "Legacy operational note") or "Legacy operational note", detail=detail, actor="Legacy Workbook", created_at=_legacy_date(r.get("event_date")) or datetime.utcnow()))
            matched_notes += 1

        counts = dict(payload.get("counts", {}))
        counts["locations_created"] = len(pending_locations)
        counts["operational_notes_matched"] = matched_notes
        db.session.add(NewStoryLegacyImport(
            source_name=source.get("filename", "New Story legacy workbook"), source_hash=source_hash,
            counts_json=json.dumps(counts), imported_by=current_user().username if current_user() else "Pierson"
        ))
        db.session.commit()
        flash(f"Legacy New Story data imported: {len(request_objs):,} requests and {len(asset_objs):,} assets are now live.", "success")
        return redirect(url_for("new_story_dashboard"))

    return render_template("new_story/legacy_import.html", source=source, counts=payload.get("counts", {}), prior=prior)


# ═══════════════════════════════════════════════════════════
# NEW STORY — CUSTOMER PORTAL (READ-ONLY PHASE 1)
# ═══════════════════════════════════════════════════════════

@app.route("/new-story/portal")
@new_story_login_required
def new_story_portal_dashboard():
    q = request.args.get("q", "").strip()
    open_statuses = [s for s in NEW_STORY_REQUEST_STATUSES if s not in {"Complete", "Cancelled"}]

    request_query = NewStoryRequest.query
    if q:
        like = f"%{q}%"
        request_query = request_query.filter(or_(
            NewStoryRequest.ticket_number.ilike(like),
            NewStoryRequest.school_name.ilike(like),
            NewStoryRequest.customer_po.ilike(like),
            NewStoryRequest.requester.ilike(like),
            NewStoryRequest.recipient.ilike(like),
        ))

    recent_requests = request_query.order_by(NewStoryRequest.updated_at.desc()).limit(12).all()
    open_requests = NewStoryRequest.query.filter(NewStoryRequest.status.in_(open_statuses)).count()
    awaiting_return = NewStoryRequest.query.filter_by(status="Awaiting Return").count()
    needs_attention = NewStoryRequest.query.filter(or_(
        NewStoryRequest.status == "Needs Review",
        NewStoryRequest.attention_reason != "",
    )).count()
    total_assets = NewStoryAsset.query.count()
    deployed_assets = NewStoryAsset.query.filter(NewStoryAsset.status.in_(["Shipped", "Deployed", "In Use"])).count()
    available_assets = NewStoryAsset.query.filter(NewStoryAsset.status.in_(["Received", "Available", "Reserved", "Allocated", "Processing", "Ready to Ship", "Returned", "Repair"])).count()

    recent_shipments = NewStoryShipment.query.order_by(NewStoryShipment.created_at.desc()).limit(8).all()
    recent_activity = NewStoryActivity.query.filter(NewStoryActivity.event_type != "Internal").order_by(NewStoryActivity.created_at.desc()).limit(10).all()
    status_counts = {
        status: NewStoryRequest.query.filter_by(status=status).count()
        for status in NEW_STORY_REQUEST_STATUSES
        if NewStoryRequest.query.filter_by(status=status).count()
    }

    return render_template(
        "new_story/portal_dashboard.html",
        q=q,
        recent_requests=recent_requests,
        recent_shipments=recent_shipments,
        recent_activity=recent_activity,
        open_requests=open_requests,
        awaiting_return=awaiting_return,
        needs_attention=needs_attention,
        total_assets=total_assets,
        deployed_assets=deployed_assets,
        available_assets=available_assets,
        status_counts=status_counts,
    )


@app.route("/new-story/portal/requests")
@new_story_login_required
def new_story_portal_requests():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    service = request.args.get("service", "").strip()
    query = NewStoryRequest.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            NewStoryRequest.ticket_number.ilike(like),
            NewStoryRequest.school_name.ilike(like),
            NewStoryRequest.customer_po.ilike(like),
            NewStoryRequest.requester.ilike(like),
            NewStoryRequest.recipient.ilike(like),
        ))
    if status:
        query = query.filter_by(status=status)
    if service:
        query = query.filter_by(service_type=service)
    rows = query.order_by(NewStoryRequest.updated_at.desc()).limit(500).all()
    services = [r[0] for r in db.session.query(NewStoryRequest.service_type).distinct().order_by(NewStoryRequest.service_type).all() if r[0]]
    return render_template("new_story/portal_requests.html", rows=rows, q=q, status=status, service=service, services=services, statuses=NEW_STORY_REQUEST_STATUSES)


@app.route("/new-story/portal/assets")
@new_story_login_required
def new_story_portal_assets():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    category = request.args.get("category", "").strip()
    view = request.args.get("view", "").strip()

    # Customer-facing custody buckets.  These intentionally describe where the
    # equipment is in its lifecycle rather than relying on a spreadsheet color.
    pierson_statuses = ["Received", "Available", "Reserved", "Allocated", "Processing", "Ready to Ship", "Returned", "Repair"]
    available_statuses = ["Received", "Available"]
    reserved_statuses = ["Reserved", "Allocated", "Processing", "Ready to Ship"]
    deployed_statuses = ["Shipped", "Deployed", "In Use"]
    return_pending_statuses = ["Return Pending"]
    repair_statuses = ["Repair"]

    view_statuses = {
        "pierson": pierson_statuses,
        "available": available_statuses,
        "reserved": reserved_statuses,
        "deployed": deployed_statuses,
        "return_pending": return_pending_statuses,
        "repair": repair_statuses,
    }

    query = NewStoryAsset.query
    if q:
        like = f"%{q}%"
        query = query.outerjoin(NewStoryLocation, NewStoryAsset.current_location_id == NewStoryLocation.id).filter(or_(
            NewStoryAsset.serial_number.ilike(like),
            NewStoryAsset.asset_tag.ilike(like),
            NewStoryAsset.model.ilike(like),
            NewStoryAsset.customer_po.ilike(like),
            NewStoryAsset.assigned_to.ilike(like),
            NewStoryAsset.room.ilike(like),
            NewStoryLocation.name.ilike(like),
        ))
    if status:
        query = query.filter(NewStoryAsset.status == status)
    elif view in view_statuses:
        query = query.filter(NewStoryAsset.status.in_(view_statuses[view]))
    if category:
        query = query.filter(NewStoryAsset.category == category)

    rows = query.order_by(NewStoryAsset.updated_at.desc()).limit(1000).all()
    categories = [r[0] for r in db.session.query(NewStoryAsset.category).distinct().order_by(NewStoryAsset.category).all() if r[0]]
    statuses = [r[0] for r in db.session.query(NewStoryAsset.status).distinct().order_by(NewStoryAsset.status).all() if r[0]]

    category_rows = []
    for cat in categories:
        base = NewStoryAsset.query.filter(NewStoryAsset.category == cat)
        category_rows.append({
            "category": cat,
            "total": base.count(),
            "pierson": base.filter(NewStoryAsset.status.in_(pierson_statuses)).count(),
            "available": base.filter(NewStoryAsset.status.in_(available_statuses)).count(),
            "reserved": base.filter(NewStoryAsset.status.in_(reserved_statuses)).count(),
            "deployed": base.filter(NewStoryAsset.status.in_(deployed_statuses)).count(),
            "return_pending": base.filter(NewStoryAsset.status.in_(return_pending_statuses)).count(),
            "repair": base.filter(NewStoryAsset.status.in_(repair_statuses)).count(),
        })

    status_counts = dict(db.session.query(NewStoryAsset.status, db.func.count(NewStoryAsset.id)).group_by(NewStoryAsset.status).all())
    total_assets = sum(status_counts.values())
    at_pierson = sum(status_counts.get(x, 0) for x in pierson_statuses)
    available_assets = sum(status_counts.get(x, 0) for x in available_statuses)
    deployed_assets = sum(status_counts.get(x, 0) for x in deployed_statuses)
    return_pending_assets = sum(status_counts.get(x, 0) for x in return_pending_statuses)
    repair_assets = sum(status_counts.get(x, 0) for x in repair_statuses)

    # Non-serialized stock is shown separately so it never inflates serialized
    # ownership totals or double-counts devices.
    stock_rows = (NewStoryStockItem.query
                  .filter_by(active=True)
                  .order_by(NewStoryStockItem.category.asc(), NewStoryStockItem.description.asc())
                  .all())
    bulk_on_hand = sum(max(x.quantity_on_hand or 0, 0) for x in stock_rows)
    bulk_available = sum(x.quantity_available for x in stock_rows)

    location_counts = (db.session.query(NewStoryLocation.name, db.func.count(NewStoryAsset.id))
                       .join(NewStoryAsset, NewStoryAsset.current_location_id == NewStoryLocation.id)
                       .filter(NewStoryAsset.status.in_(deployed_statuses + return_pending_statuses))
                       .group_by(NewStoryLocation.name)
                       .order_by(db.func.count(NewStoryAsset.id).desc())
                       .limit(12).all())

    return render_template(
        "new_story/portal_assets.html", rows=rows, q=q, status=status, category=category, view=view,
        categories=categories, statuses=statuses, category_rows=category_rows, status_counts=status_counts,
        total_assets=total_assets, at_pierson=at_pierson, available_assets=available_assets,
        deployed_assets=deployed_assets, return_pending_assets=return_pending_assets, repair_assets=repair_assets,
        stock_rows=stock_rows, bulk_on_hand=bulk_on_hand, bulk_available=bulk_available, location_counts=location_counts,
    )


@app.route("/new-story/portal/assets/<int:asset_id>")
@new_story_login_required
def new_story_portal_asset(asset_id: int):
    asset = db.session.get(NewStoryAsset, asset_id) or abort(404)
    return render_template("new_story/portal_asset.html", asset=asset)


@app.route("/new-story/portal/shipments")
@new_story_login_required
def new_story_portal_shipments():
    q = request.args.get("q", "").strip()
    direction = request.args.get("direction", "").strip()
    query = NewStoryShipment.query.join(NewStoryRequest)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            NewStoryShipment.tracking_number.ilike(like),
            NewStoryRequest.ticket_number.ilike(like),
            NewStoryRequest.school_name.ilike(like),
        ))
    if direction:
        query = query.filter(NewStoryShipment.direction == direction)
    rows = query.order_by(NewStoryShipment.created_at.desc()).limit(500).all()
    return render_template("new_story/portal_shipments.html", rows=rows, q=q, direction=direction)


@app.route("/new-story/portal/requests/<int:request_id>")
@new_story_login_required
def new_story_portal_request(request_id: int):
    req = db.session.get(NewStoryRequest, request_id) or abort(404)
    public_activity = [a for a in req.activities if a.event_type != "Internal"]
    return render_template("new_story/portal_request.html", req=req, public_activity=public_activity)


@app.route("/new-story/logout")
def new_story_logout():
    session.pop("new_story_account_id", None)
    flash("You have been logged out.", "success")
    return redirect(url_for("cp_login"))


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
