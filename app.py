from __future__ import annotations

import csv
import io
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from functools import wraps
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_ROOT = Path(os.getenv("UPLOAD_FOLDER", BASE_DIR / "uploads")).resolve()
CHECKOFF_FOLDER = UPLOAD_ROOT / "checkoff_slips"
EXPORT_FOLDER = Path(os.getenv("EXPORT_FOLDER", BASE_DIR / "exports")).resolve()

CHECKOFF_FOLDER.mkdir(parents=True, exist_ok=True)
EXPORT_FOLDER.mkdir(parents=True, exist_ok=True)

ALLOWED_CHECKOFF_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "webp"}

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
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "25")) * 1024 * 1024

COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() not in {"0", "false", "no"}
if os.getenv("FLASK_DEBUG") == "1":
    COOKIE_SECURE = False

app.config["SESSION_COOKIE_SECURE"] = COOKIE_SECURE
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

CUSTOMER_PORTAL_PASSWORD = os.getenv("CUSTOMER_PORTAL_PASSWORD", "MCPS1234")
DRIVER_PORTAL_PASSWORD = os.getenv("DRIVER_PORTAL_PASSWORD", "Driver1234")
GMAIL_USER = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
BOOTSTRAP_ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
BOOTSTRAP_ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")


db = SQLAlchemy(app)


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


class Unit(db.Model, RowLikeMixin):
    __tablename__ = "units"

    id = db.Column(db.Integer, primary_key=True)
    intake_id = db.Column(db.String(120), unique=True, nullable=False)
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

    @property
    def badge_class(self) -> str:
        return STATUS_BADGE_CLASSES.get(self.status, "customer-status-default")

    @property
    def checkoff_status(self) -> str:
        return "Uploaded" if self.checkoff_file else "Not Uploaded"


class RepairNote(db.Model, RowLikeMixin):
    __tablename__ = "repair_notes"

    id = db.Column(db.Integer, primary_key=True)
    unit_id = db.Column(db.Integer, db.ForeignKey("units.id"), nullable=False)
    note_text = db.Column(db.Text, nullable=False)
    technician = db.Column(db.String(120), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class EmailSettings(db.Model, RowLikeMixin):
    __tablename__ = "email_settings"

    id = db.Column(db.Integer, primary_key=True)
    recipients = db.Column(db.Text, default="")          # comma-separated emails
    frequency = db.Column(db.String(20), default="monthly")  # weekly, biweekly, monthly
    include_active = db.Column(db.Boolean, default=True)
    include_archived = db.Column(db.Boolean, default=False)
    last_sent = db.Column(db.String(40), default="")
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ArcadeScore(db.Model, RowLikeMixin):
    __tablename__ = "arcade_scores"

    id = db.Column(db.Integer, primary_key=True)
    player_name = db.Column(db.String(32), nullable=False)
    score = db.Column(db.Integer, nullable=False)
    wave = db.Column(db.Integer, default=1)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


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


def current_user() -> User | None:
    user_id = session.get("user_id")
    if not user_id:
        return None
    return db.session.get(User, user_id)


@app.context_processor
def inject_globals():
    return {
        "current_user": current_user(),
        "STATUSES": STATUSES,
        "STATUS_BADGE_CLASSES": STATUS_BADGE_CLASSES,
    }




def init_database() -> None:
    with app.app_context():
        db.create_all()
        if BOOTSTRAP_ADMIN_USERNAME and BOOTSTRAP_ADMIN_PASSWORD:
            existing = User.query.filter_by(username=BOOTSTRAP_ADMIN_USERNAME).first()
            if not existing:
                user = User(username=BOOTSTRAP_ADMIN_USERNAME, role="admin")
                user.set_password(BOOTSTRAP_ADMIN_PASSWORD)
                db.session.add(user)
                db.session.commit()


def validate_date(date_text: str) -> bool:
    if not date_text:
        return True
    try:
        datetime.strptime(date_text, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def allowed_checkoff_file(filename: str) -> bool:
    if not filename or "." not in filename:
        return False
    extension = filename.rsplit(".", 1)[1].lower()
    return extension in ALLOWED_CHECKOFF_EXTENSIONS


def admin_login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            # Clear any customer/driver sessions so they don't hijack the redirect
            session.pop("customer_portal_logged_in", None)
            session.pop("driver_portal_logged_in", None)
            return redirect(url_for("login", next=request.path))
        return view_func(*args, **kwargs)
    return wrapped


def customer_login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("customer_portal_logged_in"):
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)
    return wrapped


def driver_login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("driver_portal_logged_in"):
            return redirect(url_for("driver_login"))
        return view_func(*args, **kwargs)
    return wrapped


def generate_next_intake_id() -> str:
    year = datetime.now().year
    prefix = f"BX-{year}-"
    # Only look at units matching the BX-YEAR- prefix for sequencing
    last = (Unit.query
            .filter(Unit.intake_id.like(f"{prefix}%"))
            .order_by(
                db.func.cast(
                    db.func.substr(Unit.intake_id, len(prefix) + 1),
                    db.Integer
                ).desc()
            )
            .first())
    if last and last.intake_id:
        try:
            next_number = int(last.intake_id.split("-")[-1]) + 1
        except (ValueError, IndexError):
            next_number = 1
    else:
        next_number = 1
    return f"{prefix}{next_number:04d}"


def get_dashboard_counts() -> dict[str, int]:
    counts = {}
    for status in STATUSES:
        counts[status] = Unit.query.filter_by(status=status, is_deleted=False).count()
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


@app.route("/health")
def health():
    return {"status": "ok", "app": "pierson-repairs"}, 200


@app.route("/login", methods=["GET", "POST"])
def login():
    # Only auto-redirect if navigating to login directly (no 'next' param)
    # This prevents driver/customer sessions from hijacking admin navigation
    if not request.args.get("next"):
        if session.get("user_id"):
            return redirect(url_for("index"))
        if session.get("customer_portal_logged_in"):
            return redirect(url_for("customer_portal"))
        if session.get("driver_portal_logged_in"):
            return redirect(url_for("driver_portal"))

    if request.method == "POST":
        portal = request.form.get("portal", "").strip()
        password = request.form.get("password", "")

        # Always clear session before setting a new one
        session.clear()

        if portal == "driver":
            if password == DRIVER_PORTAL_PASSWORD:
                session["driver_portal_logged_in"] = True
                return redirect(url_for("driver_portal"))
            flash("Invalid driver portal password.", "danger")
            return render_template("login.html", active_tab="driver")

        elif portal == "customer":
            if password == CUSTOMER_PORTAL_PASSWORD:
                session["customer_portal_logged_in"] = True
                return redirect(url_for("customer_portal"))
            flash("Invalid customer portal password.", "danger")
            return render_template("login.html", active_tab="customer")

        else:  # admin
            username = request.form.get("username", "").strip()
            user = User.query.filter_by(username=username).first()
            if user and user.check_password(password):
                session["user_id"] = user.id
                session["role"] = user.role
                return redirect(request.args.get("next") or url_for("index"))
            flash("Invalid username or password.", "danger")
            return render_template("login.html", active_tab="admin")

    return render_template("login.html", active_tab="admin")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.route("/")
@admin_login_required
def index():
    search = request.args.get("search", "").strip()
    status_filter = request.args.get("status", "").strip()
    query = Unit.query.filter_by(is_deleted=False)

    if search:
        like_search = f"%{search}%"
        query = query.filter(or_(
            Unit.intake_id.ilike(like_search),
            Unit.brand.ilike(like_search),
            Unit.model.ilike(like_search),
            Unit.serial_number.ilike(like_search),
            Unit.reported_issue.ilike(like_search),
            Unit.source.ilike(like_search),
        ))

    if status_filter:
        query = query.filter_by(status=status_filter)

    units = query.order_by(Unit.id.desc()).all()
    return render_template(
        "index.html",
        units=units,
        search=search,
        status_filter=status_filter,
        counts=get_dashboard_counts(),
        next_intake_id=generate_next_intake_id(),
    )


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
    except Exception as exc:
        db.session.rollback()
        flash(f"Error adding unit: {exc}", "danger")

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

        if not all(validate_date(value) for value in [date_received, shipped_back_date, repaired_date, delivery_date]):
            flash("Dates must be in YYYY-MM-DD format.", "danger")
            return redirect(url_for("edit_unit", unit_id=unit_id))

        if status not in STATUSES:
            status = unit.status

        unit.intake_id = intake_id
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
        except Exception as exc:
            db.session.rollback()
            flash(f"Error updating unit: {exc}", "danger")

    return render_template("edit_unit.html", unit=unit)


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

    note = RepairNote(unit_id=unit_id, note_text=note_text, technician=technician)
    db.session.add(note)
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
    if not uploaded_file or uploaded_file.filename == "":
        flash("Please choose a check-off slip file to upload.", "danger")
        return redirect(url_for("unit_detail", unit_id=unit_id))

    if not allowed_checkoff_file(uploaded_file.filename):
        flash("Allowed file types are PDF, PNG, JPG, JPEG, and WEBP.", "danger")
        return redirect(url_for("unit_detail", unit_id=unit_id))

    original_filename = secure_filename(uploaded_file.filename)
    extension = original_filename.rsplit(".", 1)[1].lower()
    safe_intake_id = secure_filename(unit.intake_id or f"unit_{unit_id}")
    filename = f"{safe_intake_id}_checkoff.{extension}"
    filepath = CHECKOFF_FOLDER / filename

    if unit.checkoff_file and unit.checkoff_file != filename:
        old_path = CHECKOFF_FOLDER / unit.checkoff_file
        if old_path.exists():
            old_path.unlink(missing_ok=True)

    uploaded_file.save(filepath)
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
    today = datetime.now().strftime("%Y-%m-%d")
    return render_template("packing_slip.html", unit=unit, today=today, portal="admin")


@app.route("/export/csv")
@admin_login_required
def export_csv():
    try:
        rows = Unit.query.order_by(Unit.id.desc()).all()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"repair_tracker_export_{timestamp}.csv"
        filepath = EXPORT_FOLDER / filename

        with open(filepath, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow([
                "id", "intake_id", "brand", "model", "serial_number", "screen_size", "source",
                "date_received", "status", "reported_issue", "final_outcome", "repaired_date",
                "delivery_date", "checkoff_file", "checkoff_uploaded_at", "shipped_back_mcps",
                "shipped_back_date", "is_deleted", "created_at", "updated_at",
            ])
            for row in rows:
                writer.writerow([
                    row.id,
                    f'=\"{row.intake_id or ""}\"',
                    row.brand,
                    row.model,
                    f'=\"{row.serial_number or ""}\"',
                    f'=\"{row.screen_size or ""}\"',
                    row.source,
                    row.date_received,
                    row.status,
                    row.reported_issue,
                    row.final_outcome,
                    row.repaired_date,
                    row.delivery_date,
                    row.checkoff_file,
                    row.checkoff_uploaded_at,
                    "Yes" if row.shipped_back_mcps else "No",
                    row.shipped_back_date,
                    "Yes" if row.is_deleted else "No",
                    row.created_at,
                    row.updated_at,
                ])

        return send_file(filepath, as_attachment=True)
    except Exception as exc:
        flash(f"CSV export failed: {exc}", "danger")
        return redirect(url_for("index"))


@app.route("/customer-login")
def customer_login():
    # Redirect old customer login URL to the unified login page
    return redirect(url_for("login"))


@app.route("/customer-logout")
def customer_logout():
    session.pop("customer_portal_logged_in", None)
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.route("/driver-login", methods=["GET", "POST"])
def driver_login():
    if session.get("driver_portal_logged_in"):
        return redirect(url_for("driver_portal"))
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == DRIVER_PORTAL_PASSWORD:
            session.clear()
            session["driver_portal_logged_in"] = True
            return redirect(url_for("driver_portal"))
        flash("Invalid driver password.", "danger")
    return render_template("driver_login.html")


@app.route("/driver-logout")
def driver_logout():
    session.pop("driver_portal_logged_in", None)
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.route("/driver")
@driver_login_required
def driver_portal():
    units = Unit.query.filter_by(
        status="Picking up from MCPS", is_deleted=False
    ).order_by(Unit.id.desc()).all()
    return render_template("driver_portal.html", units=units)


@app.route("/driver/pickup/<int:unit_id>", methods=["POST"])
@driver_login_required
def driver_pickup(unit_id: int):
    unit = get_active_unit(unit_id)
    if unit is None:
        flash("Unit not found.", "danger")
        return redirect(url_for("driver_portal"))
    apply_status_side_effects(unit, "In Repair")
    db.session.commit()
    flash(f"{unit.intake_id} marked as picked up and In Repair.", "success")
    return redirect(url_for("driver_portal"))


@app.route("/driver/pickup-slip")
@driver_login_required
def driver_pickup_slip():
    try:
        units = Unit.query.filter_by(
            status="Picking up from MCPS", is_deleted=False
        ).order_by(Unit.id.desc()).all()
        today = datetime.now().strftime("%Y-%m-%d")
        return render_template("driver_pickup_slip.html", units=units, today=today)
    except Exception as exc:
        flash(f"Error loading pickup slip: {exc}", "danger")
        return redirect(url_for("driver_portal"))


@app.route("/customer")
@customer_login_required
def customer_portal():
    search = request.args.get("search", "").strip()
    query = Unit.query.filter_by(is_deleted=False)

    if search:
        like_search = f"%{search}%"
        query = query.filter(or_(
            Unit.intake_id.ilike(like_search),
            Unit.serial_number.ilike(like_search),
            Unit.model.ilike(like_search),
            Unit.brand.ilike(like_search),
            Unit.source.ilike(like_search),
            Unit.status.ilike(like_search),
        ))

    units = query.order_by(Unit.id.desc()).all()
    return render_template("customer_index.html", units=units, search=search)


@app.route("/customer/unit/<int:unit_id>")
@customer_login_required
def customer_unit_detail(unit_id: int):
    unit = get_active_unit(unit_id)
    if unit is None:
        flash("That repair record could not be found.", "danger")
        return redirect(url_for("customer_portal"))
    return render_template("customer_detail.html", unit=unit)


@app.route("/customer/unit/<int:unit_id>/checkoff")
@customer_login_required
def customer_view_checkoff(unit_id: int):
    unit = get_active_unit(unit_id)
    if unit is None:
        flash("That repair record could not be found.", "danger")
        return redirect(url_for("customer_portal"))

    if not unit.checkoff_file:
        flash("No check-off slip is available for this unit.", "danger")
        return redirect(url_for("customer_unit_detail", unit_id=unit_id))

    filepath = CHECKOFF_FOLDER / unit.checkoff_file
    if not filepath.exists():
        flash("The check-off slip file could not be found.", "danger")
        return redirect(url_for("customer_unit_detail", unit_id=unit_id))

    return send_file(filepath, as_attachment=False)


@app.route("/customer/unit/<int:unit_id>/packing-slip")
@customer_login_required
def customer_packing_slip(unit_id: int):
    unit = get_active_unit(unit_id)
    if unit is None:
        flash("That repair record could not be found.", "danger")
        return redirect(url_for("customer_portal"))
    today = datetime.now().strftime("%Y-%m-%d")
    return render_template("packing_slip.html", unit=unit, today=today, portal="customer")




@app.route("/arcade/scores")
@admin_login_required
def arcade_scores():
    scores = ArcadeScore.query.order_by(ArcadeScore.score.desc()).limit(10).all()
    return {
        "scores": [
            {
                "rank": i + 1,
                "name": s.player_name,
                "score": s.score,
                "wave": s.wave,
                "date": s.created_at.strftime("%Y-%m-%d") if s.created_at else ""
            }
            for i, s in enumerate(scores)
        ]
    }


@app.route("/arcade/submit", methods=["POST"])
@admin_login_required
def arcade_submit():
    data = request.get_json()
    name = (data.get("name") or "Anonymous").strip()[:32]
    score = int(data.get("score", 0))
    wave = int(data.get("wave", 1))
    entry = ArcadeScore(player_name=name, score=score, wave=wave)
    db.session.add(entry)
    db.session.commit()
    return {"ok": True}


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


def send_report_email(settings, units):
    """Send the repair report email with CSV attachment."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        return False, "Gmail credentials not configured. Add GMAIL_USER and GMAIL_APP_PASSWORD in Render environment variables."

    recipients = [r.strip() for r in settings.recipients.split(",") if r.strip()]
    if not recipients:
        return False, "No recipient email addresses configured."

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Intake ID", "Brand", "Model", "Serial Number", "Screen Size",
        "Date Received", "Status", "Shipped Back", "Shipped Back Date",
        "Final Outcome", "Last Updated"
    ])
    for unit in units:
        writer.writerow([
            unit.intake_id, unit.brand, unit.model, unit.serial_number,
            unit.screen_size, unit.date_received, unit.status,
            "Yes" if unit.shipped_back_mcps else "No",
            unit.shipped_back_date, unit.final_outcome, unit.updated_at,
        ])
    csv_data = output.getvalue()

    status_counts = {}
    for unit in units:
        status_counts[unit.status] = status_counts.get(unit.status, 0) + 1
    summary_lines = "\n".join(
        f"  - {s}: {c}" for s, c in sorted(status_counts.items())
    )
    today = datetime.now().strftime("%B %d, %Y")

    msg = MIMEMultipart()
    msg["From"] = GMAIL_USER
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = f"Pierson Repairs - MCPS Boxlight Report ({today})"

    body = (
        f"MCPS Boxlight Repair Report\n"
        f"Generated: {today}\n\n"
        f"Total Units: {len(units)}\n\n"
        f"Status Breakdown:\n{summary_lines}\n\n"
        f"A full CSV report is attached.\n\n---\nPierson Repairs Tracker\n"
    )
    msg.attach(MIMEText(body, "plain"))

    filename = f"MCPS_Boxlight_Report_{datetime.now().strftime('%Y%m%d')}.csv"
    part = MIMEBase("application", "octet-stream")
    part.set_payload(csv_data.encode("utf-8"))
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", f"attachment; filename={filename}")
    msg.attach(part)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
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
            units = query.order_by(Unit.id.desc()).all()
            success, message = send_report_email(settings, units)
            if success:
                settings.last_sent = datetime.now().strftime("%Y-%m-%d %H:%M")
                db.session.commit()
                flash(message, "success")
            else:
                flash(message, "danger")
        else:
            flash("Email settings saved.", "success")

        return redirect(url_for("email_settings"))

    return render_template("email_settings.html", settings=settings,
                           gmail_configured=bool(GMAIL_USER and GMAIL_APP_PASSWORD))


init_database()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG") == "1",
    )
