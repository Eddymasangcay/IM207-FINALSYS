from flask import Flask, render_template, request, redirect, session, url_for, abort, flash, send_file
from io import BytesIO
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect as sql_inspect, text, func, or_
from sqlalchemy.orm import joinedload
from datetime import datetime, timedelta, date
import base64
import hashlib
import hmac
import json
import os
import random
import secrets
import string
import urllib.error
import urllib.request


def _load_local_env_file():
    """Load KEY=VALUE pairs from .env for local development."""
    env_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), '.env')
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                if not key:
                    continue
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
    except OSError:
        return


_load_local_env_file()

app = Flask(__name__, static_folder='.', static_url_path='/static', template_folder='templates')
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

# Set up database path - use Database folder
basedir = os.path.abspath(os.path.dirname(__file__))
database_folder = os.path.join(basedir, 'Database')
os.makedirs(database_folder, exist_ok=True)

# Set up upload folder for images
UPLOAD_FOLDER = os.path.join(basedir, 'static', 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
PROFILE_UPLOAD_FOLDER = os.path.join(UPLOAD_FOLDER, 'profiles')
VEHICLE_UPLOAD_FOLDER = os.path.join(UPLOAD_FOLDER, 'vehicles')
RENTAL_ID_UPLOAD_FOLDER = os.path.join(UPLOAD_FOLDER, 'rental_ids')
os.makedirs(PROFILE_UPLOAD_FOLDER, exist_ok=True)
os.makedirs(VEHICLE_UPLOAD_FOLDER, exist_ok=True)
os.makedirs(RENTAL_ID_UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Database configuration - use absolute path with proper formatting
db_path = os.path.join(database_folder, 'CCRS-BMBB.db')
# Convert Windows backslashes to forward slashes for SQLite URI
db_uri = db_path.replace('\\', '/')
database_url = (os.environ.get("DATABASE_URL") or "").strip()
if database_url:
    # Some hosts provide postgres://, while SQLAlchemy expects postgresql://
    if database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url[len("postgres://"):]
    # Use psycopg v3 driver explicitly to avoid psycopg2 import errors on managed hosts.
    if database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url[len("postgresql://"):]
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_uri}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

SITE_BRAND = "Chukakoy Car Rental"
MIN_RENTAL_AGE = 21
MAX_RENTAL_ID_IMAGE_BYTES = 8 * 1024 * 1024  # per-file cap for ID photos stored in DB
DAMAGE_WORKFLOW_STATUSES = ('under_review', 'repair_scheduled', 'in_repair')
# Fleet prices are stored in USD; PH listings are displayed in PHP using this rate.
USD_TO_PHP_RATE = float(os.environ.get('USD_TO_PHP_RATE', '56.0'))
DELIVERY_DELAY_GRACE_MINUTES = int(os.environ.get('DELIVERY_DELAY_GRACE_MINUTES', '60'))


def _mime_for_id_extension(ext: str) -> str:
    ext = (ext or '').lower().lstrip('.')
    return {
        'png': 'image/png',
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'gif': 'image/gif',
        'webp': 'image/webp',
    }.get(ext, 'application/octet-stream')

# GCash QR (simulated InstaPay-style) â€” display strings only; QR encodes a reference payload, not a live P2M string.
GCASH_QR_MERCHANT_TITLE = "C.K Cars"
GCASH_QR_MASKED_NAME = "ED****O M."
GCASH_QR_MASKED_MOBILE = "+63 992 152 â€¢â€¢â€¢â€¢"


def _get_csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_hex(24)
        session['_csrf_token'] = token
        session.modified = True
    return token


@app.template_global()
def csrf_input():
    return f'<input type="hidden" name="csrf_token" value="{_get_csrf_token()}">'


@app.before_request
def csrf_protect():
    if request.method != 'POST':
        return
    # External providers (e.g., PayMongo webhooks) cannot provide our form CSRF token.
    if request.path == '/webhooks/paymongo':
        return
    token = request.form.get('csrf_token', '')
    expected = session.get('_csrf_token', '')
    if not token or not expected or not secrets.compare_digest(token, expected):
        abort(400)


@app.context_processor
def inject_site_globals():
    nav_staff = False
    try:
        if 'username' in session:
            cu = User.query.filter_by(username=session['username']).first()
            if cu and cu.is_staff():
                nav_staff = True
    except Exception:
        nav_staff = False
    return {
        "site_brand": SITE_BRAND,
        "site_year": datetime.utcnow().year,
        "nav_staff": nav_staff,
        "csrf_token": _get_csrf_token(),
        "USD_TO_PHP_RATE": USD_TO_PHP_RATE,
    }


@app.template_global()
def vehicle_image_url(transport):
    if not transport:
        return ''
    local = getattr(transport, 'image_local', None)
    if local:
        return url_for('static', filename=local)
    return getattr(transport, 'image_url', None) or ''


@app.template_global()
def region_label(code):
    if code == 'US':
        return 'United States'
    return 'Philippines'


def _currency_for_region(region_code):
    code = (region_code or 'PH').upper()
    if code == 'US':
        return '$', 'USD'
    return 'PHP ', 'PHP'


def _display_amount_by_region(amount, region_code):
    """Convert stored USD base amount into region display currency."""
    try:
        usd_value = float(amount)
    except (TypeError, ValueError):
        usd_value = 0.0
    if (region_code or '').upper() == 'PH':
        return usd_value * USD_TO_PHP_RATE
    return usd_value


@app.template_global()
def money_by_region(amount, region_code):
    symbol, _ = _currency_for_region(region_code)
    value = _display_amount_by_region(amount, region_code)
    return f"{symbol}{value:,.2f}"


@app.template_filter('rental_id_view_url')
def rental_id_view_url_filter(rr):
    """URL for staff to open a rental ID image (database BLOB or legacy static file)."""
    if not rr:
        return ''
    if getattr(rr, 'id_image_record', None) and rr.id_image_record.image_data:
        return url_for('admin_rental_id_image', request_id=rr.id)
    if rr.id_image_local:
        return url_for('static', filename=rr.id_image_local)
    return ''


def _parse_iso_date(s):
    if not s or not isinstance(s, str):
        return None
    s = s.strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _inclusive_rental_days(pickup_d: date, return_d: date) -> int:
    """Calendar days from pickup through return (both inclusive)."""
    if return_d < pickup_d:
        return 0
    return (return_d - pickup_d).days + 1


def _default_pickup_return_from_qty(quantity: int):
    """Pickup = today (UTC), return chosen so inclusive day count matches quantity."""
    quantity = max(1, min(int(quantity), 365))
    pickup = datetime.utcnow().date()
    ret = pickup + timedelta(days=quantity - 1)
    return pickup, ret


def _birthdate_max_for_min_age(min_years: int = MIN_RENTAL_AGE):
    """Latest birth date so the person is at least min_years old today (UTC)."""
    d = datetime.utcnow().date()
    y = d.year - min_years
    try:
        return date(y, d.month, d.day)
    except ValueError:
        return date(y, 2, 28)


def _age_years(birth: date, ref=None):
    ref = ref or datetime.utcnow().date()
    years = ref.year - birth.year
    if (ref.month, ref.day) < (birth.month, birth.day):
        years -= 1
    return years


def _normalize_pending_booking(pending, transport):
    """Ensure pending has pickup_date, return_date (ISO), quantity, and total_price."""
    pending = dict(pending)
    price = float(transport.price)
    q_hint = max(1, min(int(pending.get('quantity', 1)), 365))

    pu = _parse_iso_date(pending.get('pickup_date'))
    re = _parse_iso_date(pending.get('return_date'))

    if pu and re:
        if re < pu:
            pu, re = _default_pickup_return_from_qty(q_hint)
        days = _inclusive_rental_days(pu, re)
        if days > 365:
            re = pu + timedelta(days=364)
            days = 365
    else:
        pu, re = _default_pickup_return_from_qty(q_hint)
        days = _inclusive_rental_days(pu, re)

    pending['pickup_date'] = pu.isoformat()
    pending['return_date'] = re.isoformat()
    pending['quantity'] = days
    pending['total_price'] = round(price * days, 2)
    pending['delivery_address'] = (pending.get('delivery_address') or '').strip()
    return pending


def _clean_delivery_address(raw: str) -> str:
    addr = (raw or '').strip()
    if len(addr) > 300:
        addr = addr[:300].strip()
    return addr


def _paymongo_secret_key() -> str:
    return (os.environ.get('PAYMONGO_SECRET_KEY') or '').strip()


def _paymongo_basic_auth_header() -> str | None:
    key = _paymongo_secret_key()
    if not key:
        return None
    token = base64.b64encode(f'{key}:'.encode('utf-8')).decode('ascii')
    return f'Basic {token}'


def _absolute_url_for(endpoint: str, **values) -> str:
    """Build an absolute URL for PayMongo redirects. Set PUBLIC_APP_URL when behind a tunnel or reverse proxy."""
    base = (os.environ.get('PUBLIC_APP_URL') or '').rstrip('/')
    path = url_for(endpoint, **values)
    if base:
        return f'{base}{path}'
    return url_for(endpoint, _external=True, **values)


def _paymongo_default_payment_method_types() -> list:
    raw = (os.environ.get('PAYMONGO_PAYMENT_METHOD_TYPES') or 'card,gcash,paymaya').strip()
    return [p.strip() for p in raw.split(',') if p.strip()]


def _paymongo_webhook_secret() -> str:
    return (os.environ.get('PAYMONGO_WEBHOOK_SECRET') or '').strip()


def _parse_paymongo_signature_header(header: str) -> dict:
    parsed = {}
    for part in (header or '').split(','):
        k, sep, v = part.strip().partition('=')
        if not sep:
            continue
        parsed[k.strip()] = v.strip()
    return parsed


def _verify_paymongo_webhook_signature(raw_body: bytes, header: str) -> bool:
    secret = _paymongo_webhook_secret()
    if not secret:
        return False
    sig = _parse_paymongo_signature_header(header)
    ts = sig.get('t', '')
    digest_test = sig.get('te', '')
    digest_live = sig.get('li', '')
    if not ts or (not digest_test and not digest_live):
        return False
    payload = f'{ts}.{raw_body.decode("utf-8", errors="replace")}'
    computed = hmac.new(secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()
    target = digest_live or digest_test
    return bool(target) and hmac.compare_digest(computed, target)


class PayMongoAPIError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


def _paymongo_request(method: str, path: str, payload: dict | None) -> dict:
    auth = _paymongo_basic_auth_header()
    if not auth:
        raise PayMongoAPIError('PayMongo is not configured.')
    url = f'https://api.paymongo.com/v1{path}'
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, method=method.upper())
    req.add_header('Authorization', auth)
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', errors='replace')
        raise PayMongoAPIError(f'PayMongo request failed ({e.code}).', status=e.code, body=raw) from e


def _paymongo_checkout_line_amount_centavos(usd_total: float) -> int:
    php = round(float(usd_total) * USD_TO_PHP_RATE, 2)
    cents = int(round(php * 100))
    return max(cents, 100)


def _paymongo_extract_checkout_url_and_id(body: dict) -> tuple[str, str]:
    data = body.get('data') or {}
    cs_id = data.get('id') or ''
    attrs = data.get('attributes') or {}
    checkout_url = attrs.get('checkout_url') or ''
    if not cs_id or not checkout_url:
        raise PayMongoAPIError('Invalid PayMongo checkout response.')
    return checkout_url, cs_id


def _paymongo_checkout_paid_payment_refs(body: dict) -> list[str]:
    """Return PayMongo payment ids with status paid for a checkout session JSON."""
    data = body.get('data') or {}
    attrs = data.get('attributes') or {}
    payments = attrs.get('payments') or []
    refs = []
    for p in payments:
        if not isinstance(p, dict):
            continue
        pa = p.get('attributes') or {}
        if (pa.get('status') or '').lower() == 'paid':
            pid = p.get('id')
            if pid:
                refs.append(pid)
    return refs


def _finalize_rental_request_after_payment_by_id(booking, rental_request_id):
    """Webhook-safe variant: no Flask session dependency."""
    if not rental_request_id:
        return
    try:
        rid = int(rental_request_id)
    except (TypeError, ValueError):
        return
    rr = RentalRequest.query.get(rid)
    if rr and rr.user_id == booking.user_id and rr.status == 'approved':
        rr.booking_id = booking.id
        rr.status = 'paid'
        db.session.commit()


def _paymongo_begin_hosted_checkout():
    """Validate checkout session and redirect the browser to PayMongo hosted checkout."""
    if 'username' not in session:
        return redirect(url_for('index'))
    if 'pending_booking' not in session:
        return redirect(url_for('transportations'))
    if not _paymongo_secret_key():
        flash('PayMongo is not configured. Set the PAYMONGO_SECRET_KEY environment variable.', 'error')
        return redirect(url_for('payment'))

    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))

    pending = dict(session['pending_booking'])
    transport = Transportation.query.get_or_404(pending['transportation_id'])
    pending = _normalize_pending_booking(pending, transport)
    session['pending_booking'] = pending
    session.modified = True

    rid = session.get('checkout_rental_request_id')
    if rid:
        rr = RentalRequest.query.get(rid)
        if not rr or rr.user_id != current_user.id or rr.status != 'approved':
            flash('Invalid checkout session.', 'error')
            return redirect(url_for('dashboard'))
        if rr.booking_id:
            flash('This rental request is already paid.', 'error')
            return redirect(url_for('receipt', booking_id=rr.booking_id))
    else:
        rr = None

    delivery_address = _clean_delivery_address(request.form.get('delivery_address', ''))
    if not delivery_address:
        flash('Please enter a delivery address for the vehicle.', 'error')
        return redirect(url_for('payment'))
    pending['delivery_address'] = delivery_address
    pending = _normalize_pending_booking(pending, transport)
    session['pending_booking'] = pending
    session.modified = True

    line_cents = _paymongo_checkout_line_amount_centavos(pending['total_price'])
    desc = f"{transport.name} rental ({pending.get('quantity', 1)} day(s))"
    if len(desc) > 255:
        desc = desc[:252] + '...'

    success_url = _absolute_url_for('paymongo_checkout_complete')
    cancel_url = _absolute_url_for('payment')

    meta = {
        'user_id': str(current_user.id),
        'rental_request_id': str(rid) if rid else '',
        'expected_centavos': str(line_cents),
    }

    payload = {
        'data': {
            'attributes': {
                'line_items': [
                    {
                        'amount': line_cents,
                        'currency': 'PHP',
                        'name': (transport.name or 'Vehicle rental')[:255],
                        'quantity': 1,
                        'description': desc,
                    }
                ],
                'payment_method_types': _paymongo_default_payment_method_types(),
                'success_url': success_url,
                'cancel_url': cancel_url,
                'description': 'Car rental checkout',
                'send_email_receipt': False,
                'metadata': meta,
            }
        }
    }

    try:
        resp = _paymongo_request('POST', '/checkout_sessions', payload)
        checkout_url, cs_id = _paymongo_extract_checkout_url_and_id(resp)
    except PayMongoAPIError as exc:
        msg = 'Could not start PayMongo checkout.'
        if exc.status and exc.body:
            try:
                err_json = json.loads(exc.body)
                errs = (err_json.get('errors') or [])
                if errs and isinstance(errs, list):
                    first = errs[0] if errs else {}
                    detail = (first.get('detail') or first.get('title') or '').strip()
                    if detail:
                        msg = detail[:200]
            except (json.JSONDecodeError, TypeError):
                pass
        flash(msg, 'error')
        return redirect(url_for('payment'))

    session['paymongo_return_cs_id'] = cs_id
    session['paymongo_expected_centavos'] = str(line_cents)
    session.modified = True
    return redirect(checkout_url)


def _mock_gps_coords(region: str, seed: int):
    """Demo GPS coordinates (not real tracking)."""
    if region == 'US':
        lat, lng = 34.0522, -118.2437
    else:
        lat, lng = 14.5995, 120.9842
    jitter = (seed % 40) * 0.003
    return lat + jitter, lng + jitter * 1.2


def _normalize_damage_workflow_status(raw_status, is_resolved=False):
    status = (raw_status or '').strip().lower()
    if status in DAMAGE_WORKFLOW_STATUSES:
        return status
    if status in ('logged', ''):
        return 'under_review'
    if status == 'resolved':
        return 'in_repair'
    if is_resolved:
        return 'in_repair'
    return 'under_review'


# Ensure database tables exist with correct schema
def ensure_tables():
    """Ensure database tables exist with correct schema"""
    try:
        with app.app_context():
            # Check if user table exists and has required columns
            inspector = sql_inspect(db.engine)
            tables = inspector.get_table_names()
            
            if 'user' in tables:
                # Check if required columns exist
                columns = [col['name'] for col in inspector.get_columns('user')]
                booking_columns = []
                transport_columns = []
                damage_columns = []
                if 'booking' in tables:
                    booking_columns = [col['name'] for col in inspector.get_columns('booking')]
                if 'transportation' in tables:
                    transport_columns = [col['name'] for col in inspector.get_columns('transportation')]
                if 'vehicle_damage' in tables:
                    damage_columns = [col['name'] for col in inspector.get_columns('vehicle_damage')]
                
                # Add missing columns to User table if they don't exist
                try:
                    if 'email' not in columns:
                        db.session.execute(text("ALTER TABLE user ADD COLUMN email VARCHAR(150)"))
                        db.session.commit()
                        print("Added 'email' column to user table.")
                    if 'full_name' not in columns:
                        db.session.execute(text("ALTER TABLE user ADD COLUMN full_name VARCHAR(150)"))
                        db.session.commit()
                        print("Added 'full_name' column to user table.")
                except Exception as e:
                    print(f"Error adding columns to user table: {e}")
                    db.session.rollback()

                # Add missing column to Booking table if it doesn't exist
                if 'booking' in tables and 'plate_number' not in booking_columns:
                    try:
                        db.session.execute(text("ALTER TABLE booking ADD COLUMN plate_number VARCHAR(20)"))
                        db.session.commit()
                        print("Added 'plate_number' column to booking table.")
                    except Exception as e:
                        print(f"Error adding column to booking table: {e}")
                        db.session.rollback()
                if 'booking' in tables and 'payment_method' not in booking_columns:
                    try:
                        db.session.execute(text("ALTER TABLE booking ADD COLUMN payment_method VARCHAR(40)"))
                        db.session.commit()
                        print("Added 'payment_method' column to booking table.")
                    except Exception as e:
                        print(f"Error adding payment_method to booking table: {e}")
                        db.session.rollback()
                if 'booking' in tables and 'pay_currency' not in booking_columns:
                    try:
                        db.session.execute(text("ALTER TABLE booking ADD COLUMN pay_currency VARCHAR(10)"))
                        db.session.commit()
                        print("Added 'pay_currency' column to booking table.")
                    except Exception as e:
                        print(f"Error adding pay_currency to booking table: {e}")
                        db.session.rollback()
                user_alters = [
                    ('profile_image', "ALTER TABLE user ADD COLUMN profile_image VARCHAR(500)"),
                    ('region', "ALTER TABLE user ADD COLUMN region VARCHAR(2) DEFAULT 'PH'"),
                    ('saved_payment_json', "ALTER TABLE user ADD COLUMN saved_payment_json TEXT"),
                    ('birthdate', "ALTER TABLE user ADD COLUMN birthdate DATE"),
                ]
                for col_name, stmt in user_alters:
                    if col_name not in columns:
                        try:
                            db.session.execute(text(stmt))
                            db.session.commit()
                            print(f"Added '{col_name}' to user table.")
                        except Exception as e:
                            print(f"Error adding {col_name} to user: {e}")
                            db.session.rollback()
                transport_alters = [
                    ('region', "ALTER TABLE transportation ADD COLUMN region VARCHAR(2) DEFAULT 'PH'"),
                    ('description', "ALTER TABLE transportation ADD COLUMN description TEXT"),
                    ('specs', "ALTER TABLE transportation ADD COLUMN specs TEXT"),
                    ('image_local', "ALTER TABLE transportation ADD COLUMN image_local VARCHAR(500)"),
                    ('seats', "ALTER TABLE transportation ADD COLUMN seats INTEGER"),
                    ('fuel_type', "ALTER TABLE transportation ADD COLUMN fuel_type VARCHAR(30)"),
                    ('transmission', "ALTER TABLE transportation ADD COLUMN transmission VARCHAR(30)"),
                ]
                for col_name, stmt in transport_alters:
                    if 'transportation' in tables and col_name not in transport_columns:
                        try:
                            db.session.execute(text(stmt))
                            db.session.commit()
                            print(f"Added '{col_name}' to transportation table.")
                        except Exception as e:
                            print(f"Error adding {col_name} to transportation: {e}")
                            db.session.rollback()
                booking_alters = [
                    ('rental_start', "ALTER TABLE booking ADD COLUMN rental_start TIMESTAMP"),
                    ('rental_end', "ALTER TABLE booking ADD COLUMN rental_end TIMESTAMP"),
                    ('rental_days', "ALTER TABLE booking ADD COLUMN rental_days INTEGER DEFAULT 1"),
                    ('mock_lat', "ALTER TABLE booking ADD COLUMN mock_lat FLOAT"),
                    ('mock_lng', "ALTER TABLE booking ADD COLUMN mock_lng FLOAT"),
                    ('booking_status', "ALTER TABLE booking ADD COLUMN booking_status VARCHAR(24) DEFAULT 'confirmed'"),
                    ('delivery_address', "ALTER TABLE booking ADD COLUMN delivery_address VARCHAR(300)"),
                    ('status_mode', "ALTER TABLE booking ADD COLUMN status_mode VARCHAR(10) DEFAULT 'auto'"),
                    ('delivery_received', "ALTER TABLE booking ADD COLUMN delivery_received BOOLEAN DEFAULT 0"),
                    ('delivery_received_at', "ALTER TABLE booking ADD COLUMN delivery_received_at TIMESTAMP"),
                    ('delivery_returned', "ALTER TABLE booking ADD COLUMN delivery_returned BOOLEAN DEFAULT 0"),
                    ('delivery_returned_at', "ALTER TABLE booking ADD COLUMN delivery_returned_at TIMESTAMP"),
                    ('delivery_returned_by_user', "ALTER TABLE booking ADD COLUMN delivery_returned_by_user BOOLEAN DEFAULT 0"),
                    ('delivery_returned_by_user_at', "ALTER TABLE booking ADD COLUMN delivery_returned_by_user_at TIMESTAMP"),
                    ('expected_delivery_at', "ALTER TABLE booking ADD COLUMN expected_delivery_at TIMESTAMP"),
                    ('actual_delivery_at', "ALTER TABLE booking ADD COLUMN actual_delivery_at TIMESTAMP"),
                    ('is_delivery_delayed', "ALTER TABLE booking ADD COLUMN is_delivery_delayed BOOLEAN DEFAULT 0"),
                    ('delivery_delay_minutes', "ALTER TABLE booking ADD COLUMN delivery_delay_minutes INTEGER DEFAULT 0"),
                    ('delay_reason', "ALTER TABLE booking ADD COLUMN delay_reason TEXT"),
                    ('delay_reported_by_id', "ALTER TABLE booking ADD COLUMN delay_reported_by_id INTEGER"),
                    ('delay_reported_at', "ALTER TABLE booking ADD COLUMN delay_reported_at TIMESTAMP"),
                ]
                for col_name, stmt in booking_alters:
                    if 'booking' in tables and col_name not in booking_columns:
                        try:
                            db.session.execute(text(stmt))
                            db.session.commit()
                            print(f"Added '{col_name}' to booking table.")
                        except Exception as e:
                            print(f"Error adding {col_name} to booking: {e}")
                            db.session.rollback()
                damage_alters = [
                    ('severity', "ALTER TABLE vehicle_damage ADD COLUMN severity VARCHAR(20) DEFAULT 'moderate'"),
                    ('repair_estimate_usd', "ALTER TABLE vehicle_damage ADD COLUMN repair_estimate_usd FLOAT"),
                    ('is_resolved', "ALTER TABLE vehicle_damage ADD COLUMN is_resolved BOOLEAN DEFAULT 0"),
                    ('damage_status', "ALTER TABLE vehicle_damage ADD COLUMN damage_status VARCHAR(32) DEFAULT 'under_review'"),
                ]
                for col_name, stmt in damage_alters:
                    if 'vehicle_damage' in tables and col_name not in damage_columns:
                        try:
                            db.session.execute(text(stmt))
                            db.session.commit()
                            print(f"Added '{col_name}' to vehicle_damage table.")
                        except Exception as e:
                            print(f"Error adding {col_name} to vehicle_damage: {e}")
                            db.session.rollback()

                # Ensure all tables exist
                db.create_all()
            else:
                # Tables don't exist, create them
                db.create_all()
    except Exception as e:
        print(f"Warning: Could not create database tables: {e}")
        # If inspection fails, avoid destructive reset and attempt create_all only.
        try:
            with app.app_context():
                db.create_all()
                print("Database create_all attempted after schema error.")
        except:
            pass

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    email = db.Column(db.String(150), nullable=True)
    full_name = db.Column(db.String(150), nullable=True)
    profile_image = db.Column(db.String(500), nullable=True)  # path under static, e.g. uploads/profiles/u1.jpg
    region = db.Column(db.String(2), default='PH', nullable=False)  # PH or US
    saved_payment_json = db.Column(db.Text, nullable=True)  # JSON list of saved method labels
    birthdate = db.Column(db.Date, nullable=True)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    role = db.Column(db.String(20), default='user', nullable=False)  # 'user', 'admin', or 'staff'
    posts = db.relationship('Post', backref='author', lazy=True)
    bookings = db.relationship('Booking', backref='user', foreign_keys='Booking.user_id', lazy=True)
    
    def is_staff(self):
        return self.role == 'staff' or self.is_admin

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

class Transportation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    price = db.Column(db.Float, nullable=False)
    image_url = db.Column(db.String(500), nullable=True)
    image_local = db.Column(db.String(500), nullable=True)  # uploaded file path under static
    description = db.Column(db.Text, nullable=True)
    specs = db.Column(db.Text, nullable=True)
    region = db.Column(db.String(2), default='PH', nullable=False)  # PH or US â€” fleet exclusive region
    seats = db.Column(db.Integer, nullable=True)
    fuel_type = db.Column(db.String(30), nullable=True)
    transmission = db.Column(db.String(30), nullable=True)
    bookings = db.relationship('Booking', backref='transportation', lazy=True)
    damages = db.relationship('VehicleDamage', backref='vehicle', lazy=True)

class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    transportation_id = db.Column(db.Integer, db.ForeignKey('transportation.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)  # rental days
    total_price = db.Column(db.Float, nullable=False)
    plate_number = db.Column(db.String(20), nullable=True)
    payment_status = db.Column(db.String(20), default='pending', nullable=False)  # 'pending', 'paid', 'cancelled'
    booking_status = db.Column(db.String(24), default='confirmed', nullable=False)  # confirmed, active, completed, cancelled
    payment_method = db.Column(db.String(40), nullable=True)  # gcash, paymaya, credit_card, etc.
    pay_currency = db.Column(db.String(10), nullable=True)  # PHP, USD
    rental_start = db.Column(db.DateTime, nullable=True)
    rental_end = db.Column(db.DateTime, nullable=True)
    rental_days = db.Column(db.Integer, default=1, nullable=False)
    mock_lat = db.Column(db.Float, nullable=True)
    mock_lng = db.Column(db.Float, nullable=True)
    delivery_address = db.Column(db.String(300), nullable=True)
    # If user/admin manually changes status, we store it here and stop automatic sync.
    # Values: 'auto' or 'manual'
    status_mode = db.Column(db.String(10), default='auto', nullable=False)
    # Renter can notify delivery received; used to show "returned/done" workflow.
    delivery_received = db.Column(db.Boolean, default=False, nullable=False)
    delivery_received_at = db.Column(db.DateTime, nullable=True)
    # Admin can confirm that the vehicle was actually returned (finalizes booking as completed).
    delivery_returned = db.Column(db.Boolean, default=False, nullable=False)
    delivery_returned_at = db.Column(db.DateTime, nullable=True)
    # User can report that the vehicle was already returned to the branch.
    delivery_returned_by_user = db.Column(db.Boolean, default=False, nullable=False)
    delivery_returned_by_user_at = db.Column(db.DateTime, nullable=True)
    expected_delivery_at = db.Column(db.DateTime, nullable=True)
    actual_delivery_at = db.Column(db.DateTime, nullable=True)
    is_delivery_delayed = db.Column(db.Boolean, default=False, nullable=False)
    delivery_delay_minutes = db.Column(db.Integer, default=0, nullable=False)
    delay_reason = db.Column(db.Text, nullable=True)
    delay_reported_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    delay_reported_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class VehicleDamage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    transportation_id = db.Column(db.Integer, db.ForeignKey('transportation.id'), nullable=False)
    booking_id = db.Column(db.Integer, db.ForeignKey('booking.id'), nullable=True)
    area_on_vehicle = db.Column(db.String(120), nullable=False)
    damage_description = db.Column(db.Text, nullable=False)
    responsible_party = db.Column(db.String(200), nullable=True)  # e.g. renter name or "hail"
    severity = db.Column(db.String(20), default='moderate', nullable=False)
    repair_estimate_usd = db.Column(db.Float, nullable=True)
    is_resolved = db.Column(db.Boolean, default=False, nullable=False)
    damage_status = db.Column(db.String(32), default='under_review', nullable=False)
    reported_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    reporter = db.relationship('User', foreign_keys=[reported_by_id])
    booking = db.relationship('Booking', foreign_keys=[booking_id])


class VehicleDamageBookingLink(db.Model):
    """Maps a damage record to a booking so renters only see damage tied to their rental on track (not other bookings on the same vehicle)."""
    __tablename__ = 'vehicle_damage_booking_link'

    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey('booking.id'), nullable=False)
    vehicle_damage_id = db.Column(db.Integer, db.ForeignKey('vehicle_damage.id'), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    booking = db.relationship('Booking', backref=db.backref('damage_booking_links', lazy='dynamic'))
    damage = db.relationship('VehicleDamage', backref=db.backref('booking_track_link', uselist=False))


class RentalRequest(db.Model):
    """Pre-payment rental: terms (step 1) then ID upload (step 2); admin must approve before checkout."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    transportation_id = db.Column(db.Integer, db.ForeignKey('transportation.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    total_price = db.Column(db.Float, nullable=False)
    pickup_date = db.Column(db.String(12), nullable=False)
    return_date = db.Column(db.String(12), nullable=False)
    terms_accepted_at = db.Column(db.DateTime, nullable=True)
    id_document_type = db.Column(db.String(40), nullable=True)
    id_image_local = db.Column(db.String(500), nullable=True)  # legacy: path under static; prefer id_image_record
    status = db.Column(db.String(24), default='pending_review', nullable=False)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    rejection_reason = db.Column(db.Text, nullable=True)
    booking_id = db.Column(db.Integer, db.ForeignKey('booking.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    renter = db.relationship('User', foreign_keys=[user_id], backref=db.backref('rental_requests', lazy='dynamic'))
    vehicle = db.relationship('Transportation', foreign_keys=[transportation_id])
    reviewer = db.relationship('User', foreign_keys=[reviewed_by_id])
    paid_booking = db.relationship('Booking', foreign_keys=[booking_id])
    id_image_record = db.relationship(
        'RentalIdImage',
        uselist=False,
        back_populates='rental_request',
        cascade='all, delete-orphan',
    )


class RentalIdImage(db.Model):
    """Binary ID image tied to one rental request (proper handling vs ad-hoc static paths)."""
    __tablename__ = 'rental_id_image'

    id = db.Column(db.Integer, primary_key=True)
    rental_request_id = db.Column(db.Integer, db.ForeignKey('rental_request.id'), unique=True, nullable=False)
    mime_type = db.Column(db.String(80), nullable=False)
    image_data = db.Column(db.LargeBinary, nullable=False)
    byte_size = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    rental_request = db.relationship('RentalRequest', back_populates='id_image_record')


class PaymentTransaction(db.Model):
    __tablename__ = 'payment_transaction'

    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey('booking.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    method = db.Column(db.String(40), nullable=False)
    currency = db.Column(db.String(10), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    reference = db.Column(db.String(80), nullable=True)
    status = db.Column(db.String(24), default='paid', nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    booking = db.relationship('Booking', backref=db.backref('payment_transactions', lazy='dynamic'))
    user = db.relationship('User', backref=db.backref('payment_transactions', lazy='dynamic'))


class AdminAuditLog(db.Model):
    __tablename__ = 'admin_audit_log'

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    action = db.Column(db.String(80), nullable=False)
    target_type = db.Column(db.String(40), nullable=False)
    target_id = db.Column(db.Integer, nullable=True)
    details = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    actor = db.relationship('User', backref=db.backref('admin_audit_logs', lazy='dynamic'))


class Notification(db.Model):
    __tablename__ = 'notification'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(140), nullable=False)
    message = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(30), default='info', nullable=False)
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship('User', backref=db.backref('notifications', lazy='dynamic'))


class RenterReview(db.Model):
    """Staff/admin written review of a renter (internal record)."""
    id = db.Column(db.Integer, primary_key=True)
    renter_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    rating = db.Column(db.Integer, nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    renter = db.relationship('User', foreign_keys=[renter_id], backref=db.backref('reviews_about_renter', lazy='dynamic'))
    author = db.relationship('User', foreign_keys=[author_id], backref=db.backref('reviews_authored', lazy='dynamic'))


@app.route('/')
def index():
    fq = Transportation.query.order_by(Transportation.id.desc())
    if 'username' in session:
        u = User.query.filter_by(username=session['username']).first()
        if u and not (u.is_admin or u.is_staff()):
            fq = fq.filter(Transportation.region == u.region)
    featured_transports = fq.limit(6).all()
    fleet_count = Transportation.query.count()
    nq = Transportation.query.order_by(Transportation.name)
    if 'username' in session:
        u = User.query.filter_by(username=session['username']).first()
        if u and not (u.is_admin or u.is_staff()):
            nq = nq.filter(Transportation.region == u.region)
    fleet_names = [t.name for t in nq.limit(60).all()]
    return render_template(
        'Homepage.html',
        featured_transports=featured_transports,
        fleet_count=max(fleet_count, 0),
        fleet_names=fleet_names,
        today_iso=datetime.utcnow().date().isoformat(),
    )

@app.route('/loginpage')
def loginpage():
    return render_template('Loginpage.html')

@app.route('/about')
def aboutpage():
    return render_template('Aboutpage.html')

@app.route('/registerpage')
def registerpage():
    return render_template('Register.html', birthdate_max=_birthdate_max_for_min_age())

@app.route('/dashboard')
def dashboard():
    if 'username' not in session:
        return redirect(url_for('index'))

    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        # User doesn't exist in database, clear session and redirect
        session.pop('username', None)
        return redirect(url_for('index'))
    
    _sync_booking_lifecycle_states()
    user_bookings = (
        Booking.query
        .filter(Booking.user_id == current_user.id)
        .order_by(Booking.created_at.desc())
        .all()
    )
    admin_bookings = []
    if current_user.is_admin or current_user.is_staff():
        admin_bookings = (
            Booking.query
            .options(joinedload(Booking.transportation), joinedload(Booking.user))
            .filter(Booking.payment_status == 'paid')
            .order_by(Booking.created_at.desc())
            .limit(250)
            .all()
        )
    transports = Transportation.query.order_by(Transportation.id.desc()).all()
    total_rental_days = sum((b.rental_days or b.quantity or 1) for b in user_bookings)
    saved_methods = []
    if current_user.saved_payment_json:
        try:
            saved_methods = json.loads(current_user.saved_payment_json)
            if not isinstance(saved_methods, list):
                saved_methods = []
        except json.JSONDecodeError:
            saved_methods = []
    
    # Get all users for admin management
    all_users = []
    if current_user.is_admin:
        all_users = User.query.order_by(User.username).all()

    recent_bookings_for_damage = []
    users_for_damage_party = []
    if current_user.is_staff():
        recent_bookings_for_damage = (
            Booking.query.options(
                joinedload(Booking.transportation),
                joinedload(Booking.user),
            )
            .order_by(Booking.created_at.desc())
            .limit(50)
            .all()
        )
        users_for_damage_party = User.query.order_by(User.username.asc()).limit(250).all()

    user_rental_requests = (
        RentalRequest.query.options(joinedload(RentalRequest.vehicle))
        .filter(RentalRequest.user_id == current_user.id)
        .order_by(RentalRequest.created_at.desc())
        .limit(40)
        .all()
    )
    notifications = (
        Notification.query.filter_by(user_id=current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(25)
        .all()
    )

    admin_rr_pending = []
    renter_reviews_list = []
    recent_transactions = []
    admin_stats = None
    admin_charts = None
    damage_status_summary = None
    if current_user.is_staff():
        admin_rr_pending = (
            RentalRequest.query.options(
                joinedload(RentalRequest.vehicle),
                joinedload(RentalRequest.renter),
                joinedload(RentalRequest.id_image_record),
            )
            .filter(RentalRequest.status == 'pending_review')
            .order_by(RentalRequest.created_at.asc())
            .all()
        )
        renter_reviews_list = (
            RenterReview.query.options(joinedload(RenterReview.renter), joinedload(RenterReview.author))
            .order_by(RenterReview.created_at.desc())
            .limit(80)
            .all()
        )
        now = datetime.utcnow()
        fleet_n = Transportation.query.count()
        paid_n = Booking.query.filter(Booking.payment_status == 'paid').count()
        pending_rr_n = RentalRequest.query.filter_by(status='pending_review').count()
        active_rentals = Booking.query.filter(Booking.payment_status == 'paid', Booking.rental_end >= now).count()
        approved_unpaid = RentalRequest.query.filter_by(status='approved').count()
        month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_revenue = (
            db.session.query(func.coalesce(func.sum(Booking.total_price), 0.0))
            .filter(Booking.payment_status == 'paid', Booking.created_at >= month_start)
            .scalar()
        )
        revenue_total = (
            db.session.query(func.coalesce(func.sum(Booking.total_price), 0.0))
            .filter(Booking.payment_status == 'paid')
            .scalar()
        )
        utilized_vehicle_ids = (
            db.session.query(Booking.transportation_id)
            .filter(Booking.payment_status == 'paid', Booking.rental_end >= now)
            .distinct()
            .count()
        )
        utilization_pct = (float(utilized_vehicle_ids) / float(fleet_n) * 100.0) if fleet_n else 0.0
        admin_stats = {
            'fleet_listings': fleet_n,
            'paid_bookings_all_time': paid_n,
            'pending_id_requests': pending_rr_n,
            'active_rentals_now': active_rentals,
            'approved_awaiting_payment': approved_unpaid,
            'revenue_paid_bookings_usd': float(revenue_total or 0),
            'revenue_this_month_usd': float(month_revenue or 0),
            'fleet_utilization_pct': round(utilization_pct, 1),
        }
        rr_status_counts = {
            'pending_review': RentalRequest.query.filter_by(status='pending_review').count(),
            'approved': RentalRequest.query.filter_by(status='approved').count(),
            'paid': RentalRequest.query.filter_by(status='paid').count(),
            'rejected': RentalRequest.query.filter_by(status='rejected').count(),
        }
        booking_status_counts = {
            'confirmed': Booking.query.filter(Booking.payment_status == 'paid', Booking.booking_status == 'confirmed').count(),
            'active': Booking.query.filter(Booking.payment_status == 'paid', Booking.booking_status == 'active').count(),
            'completed': Booking.query.filter(Booking.payment_status == 'paid', Booking.booking_status == 'completed').count(),
            'cancelled': Booking.query.filter(Booking.payment_status == 'paid', Booking.booking_status == 'cancelled').count(),
        }
        rr_max = max(rr_status_counts.values()) if rr_status_counts else 1
        bk_max = max(booking_status_counts.values()) if booking_status_counts else 1
        admin_charts = {
            'rental_request_status': [
                {'label': 'Awaiting admin', 'value': rr_status_counts['pending_review'], 'pct': round((rr_status_counts['pending_review'] / rr_max) * 100, 1) if rr_max else 0},
                {'label': 'Approved', 'value': rr_status_counts['approved'], 'pct': round((rr_status_counts['approved'] / rr_max) * 100, 1) if rr_max else 0},
                {'label': 'Paid', 'value': rr_status_counts['paid'], 'pct': round((rr_status_counts['paid'] / rr_max) * 100, 1) if rr_max else 0},
                {'label': 'Rejected', 'value': rr_status_counts['rejected'], 'pct': round((rr_status_counts['rejected'] / rr_max) * 100, 1) if rr_max else 0},
            ],
            'booking_lifecycle': [
                {'label': 'Confirmed', 'value': booking_status_counts['confirmed'], 'pct': round((booking_status_counts['confirmed'] / bk_max) * 100, 1) if bk_max else 0},
                {'label': 'Active', 'value': booking_status_counts['active'], 'pct': round((booking_status_counts['active'] / bk_max) * 100, 1) if bk_max else 0},
                {'label': 'Completed', 'value': booking_status_counts['completed'], 'pct': round((booking_status_counts['completed'] / bk_max) * 100, 1) if bk_max else 0},
                {'label': 'Cancelled', 'value': booking_status_counts['cancelled'], 'pct': round((booking_status_counts['cancelled'] / bk_max) * 100, 1) if bk_max else 0},
            ],
        }
        dmg_counts = {k: 0 for k in DAMAGE_WORKFLOW_STATUSES}
        damage_rows = VehicleDamage.query.all()
        for d in damage_rows:
            st = _normalize_damage_workflow_status(d.damage_status, d.is_resolved)
            dmg_counts[st] += 1
        damage_status_summary = [{'key': k, 'count': dmg_counts[k]} for k in DAMAGE_WORKFLOW_STATUSES]
        recent_transactions = (
            PaymentTransaction.query.options(joinedload(PaymentTransaction.user), joinedload(PaymentTransaction.booking))
            .order_by(PaymentTransaction.created_at.desc())
            .limit(15)
            .all()
        )

    return render_template(
        'Dashboard.html',
        username=current_user.username,
        user=current_user,
        is_admin=current_user.is_admin,
        is_staff=current_user.is_staff(),
        bookings=user_bookings,
        admin_bookings=admin_bookings,
        transportations=transports,
        all_users=all_users,
        total_rental_days=total_rental_days,
        trip_count=len(user_bookings),
        saved_methods=saved_methods,
        recent_bookings_for_damage=recent_bookings_for_damage,
        users_for_damage_party=users_for_damage_party,
        user_rental_requests=user_rental_requests,
        admin_rr_pending=admin_rr_pending,
        renter_reviews_list=renter_reviews_list,
        recent_transactions=recent_transactions,
        admin_stats=admin_stats,
        admin_charts=admin_charts,
        damage_status_summary=damage_status_summary,
        notifications=notifications,
        min_rental_age=MIN_RENTAL_AGE,
        now_utc=datetime.utcnow(),
        birthdate_max=_birthdate_max_for_min_age(),
    )


def _record_audit(actor_id: int, action: str, target_type: str, target_id=None, details: str = ''):
    db.session.add(
        AdminAuditLog(
            actor_id=actor_id,
            action=action[:80],
            target_type=target_type[:40],
            target_id=target_id,
            details=(details or '')[:2000] or None,
        )
    )


def _notify_user(user_id: int, title: str, message: str, category: str = 'info'):
    db.session.add(
        Notification(
            user_id=user_id,
            title=(title or 'Update')[:140],
            message=(message or '')[:2000],
            category=(category or 'info')[:30],
        )
    )


def _compute_delay_minutes(expected: datetime | None, actual: datetime | None, grace_minutes: int = DELIVERY_DELAY_GRACE_MINUTES) -> int:
    """Return delay minutes beyond grace period (0 when on time/unknown)."""
    if not expected or not actual:
        return 0
    delta_minutes = int((actual - expected).total_seconds() // 60)
    over_grace = delta_minutes - max(int(grace_minutes or 0), 0)
    return max(over_grace, 0)


def _apply_delivery_delay_state(booking: 'Booking', actual_time: datetime | None, reason: str | None = None, reporter_id: int | None = None):
    """Set actual delivery time and mark delayed state using grace threshold."""
    booking.actual_delivery_at = actual_time
    delay_minutes = _compute_delay_minutes(getattr(booking, 'expected_delivery_at', None), actual_time)
    booking.delivery_delay_minutes = delay_minutes
    booking.is_delivery_delayed = delay_minutes > 0
    if reason is not None:
        booking.delay_reason = (reason or '').strip()[:500] or None
    if reporter_id is not None:
        booking.delay_reported_by_id = reporter_id
    booking.delay_reported_at = datetime.utcnow()


def _clear_delivery_delay_state(booking: 'Booking'):
    """Clear delay markers while keeping expected/actual timestamps."""
    booking.is_delivery_delayed = False
    booking.delivery_delay_minutes = 0
    booking.delay_reason = None
    booking.delay_reported_by_id = None
    booking.delay_reported_at = datetime.utcnow()


def _update_booking_delivery_workflow(
    booking: 'Booking',
    actor: 'User',
    *,
    user_received: bool | None,
    admin_returned: bool | None,
    set_completed: bool | None,
    audit_action: str,
    audit_details: str = '',
    notify_staff: bool = False,
    notify_owner: bool = False,
    notify_owner_title: str = '',
    notify_owner_message: str = '',
):
    """
    Shared workflow updater for:
    - user reports "vehicle received"
    - admin confirms "vehicle returned" (finalizes booking as completed)
    """
    now = datetime.utcnow()

    if user_received is not None:
        booking.delivery_received = user_received
        booking.delivery_received_at = now if user_received else None

    if admin_returned is not None:
        booking.delivery_returned = admin_returned
        booking.delivery_returned_at = now if admin_returned else None

    # Only admin-return (and completion) should freeze lifecycle syncing.
    # When the user just reports "received", we still want auto status to move
    # based on rental start/end timing.
    if admin_returned is not None or set_completed is True:
        booking.status_mode = 'manual'

    if set_completed is True:
        booking.booking_status = 'completed'

    _record_audit(
        actor.id,
        (audit_action or '')[:80],
        'booking',
        booking.id,
        (audit_details or '')[:2000] or None,
    )

    # Commit the state change first so a notification failure doesn't block delivery workflow.
    db.session.commit()

    # Notify staff/admins when the renter reports delivery received.
    if notify_staff and user_received:
        try:
            staff_users = User.query.filter(
                or_(User.is_admin == True, User.role == 'staff')
            ).all()
            for su in staff_users:
                db.session.add(Notification(
                    user_id=su.id,
                    title='Delivery received report',
                    message=f'Customer "{actor.username}" reported booking #{booking.id} as received.',
                    category='info',
                ))
            db.session.commit()
        except Exception:
            db.session.rollback()

    # Notify the booking owner when admin confirms returned.
    if notify_owner:
        try:
            _notify_user(
                booking.user_id,
                title=notify_owner_title or 'Booking updated',
                message=notify_owner_message or f'Booking #{booking.id} was updated by admin.',
                category='success',
            )
            db.session.commit()
        except Exception:
            db.session.rollback()


def _has_vehicle_booking_conflict(transport_id: int, pickup_d: date, return_d: date, ignore_booking_id=None):
    if not pickup_d or not return_d or return_d < pickup_d:
        return False
    start = datetime.combine(pickup_d, datetime.min.time())
    end = datetime.combine(return_d, datetime.max.time())
    q = Booking.query.filter(
        Booking.transportation_id == transport_id,
        Booking.payment_status == 'paid',
        # Cancelled bookings should never block availability.
        Booking.booking_status != 'cancelled',
        # A booking blocks dates until it is returned (by admin or by user report).
        or_(Booking.delivery_returned.is_(False), Booking.delivery_returned.is_(None)),
        or_(Booking.delivery_returned_by_user.is_(False), Booking.delivery_returned_by_user.is_(None)),
        Booking.rental_start.isnot(None),
        Booking.rental_end.isnot(None),
        Booking.rental_start <= end,
        Booking.rental_end >= start,
    )
    if ignore_booking_id:
        q = q.filter(Booking.id != ignore_booking_id)
    return db.session.query(q.exists()).scalar()


def _sync_booking_lifecycle_states():
    now = datetime.utcnow()
    changed = False
    bookings = Booking.query.filter(Booking.payment_status == 'paid').all()
    for b in bookings:
        # If the booking has a manual status override, don't auto-change it.
        if getattr(b, 'status_mode', 'auto') == 'manual':
            continue
        desired = b.booking_status or 'confirmed'
        if b.rental_start and b.rental_end:
            if b.rental_end < now:
                desired = 'completed'
            elif b.rental_start <= now <= b.rental_end:
                desired = 'active'
            elif b.rental_start > now:
                desired = 'confirmed'
        # Delivery workflow overrides:
        # - Admin returned OR user returned => completed
        # - User received => in-delivery only until rental_end (so it doesn't flip
        #   back to active after return time passes)
        if getattr(b, 'delivery_returned', False) or getattr(b, 'delivery_returned_by_user', False):
            desired = 'completed'
        elif getattr(b, 'delivery_received', False):
            if b.rental_end is None or b.rental_end >= now:
                desired = 'active'
        if desired != b.booking_status:
            b.booking_status = desired
            changed = True
    if changed:
        db.session.commit()

@app.route('/login', methods=['POST'])
def login():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')

    if not username or not password:
        return render_template('Loginpage.html', error="Please enter both username and password.")

    user = User.query.filter_by(username=username).first()
    if user and user.check_password(password):
        session['username'] = user.username
        return redirect(url_for('dashboard'))

    return render_template('Loginpage.html', error="Invalid username or password.")

@app.route('/register', methods=['POST'])
def register():
    if request.method == 'POST':
        try:
            username = request.form.get('new_username', '').strip()
            password = request.form.get('new_password', '').strip()
            confirm_password = request.form.get('confirm_password', '').strip()
            email = request.form.get('email', '').strip()
            full_name = request.form.get('fullname', '').strip()
            birth_raw = request.form.get('birthdate', '').strip()
            birthdate = _parse_iso_date(birth_raw)
            if not birthdate:
                return render_template(
                    'Register.html',
                    error="Please enter your date of birth.",
                    birthdate_max=_birthdate_max_for_min_age(),
                )
            if _age_years(birthdate) < MIN_RENTAL_AGE:
                return render_template(
                    'Register.html',
                    error=f"You must be at least {MIN_RENTAL_AGE} years old to register. Drivers must meet the minimum rental age.",
                    birthdate_max=_birthdate_max_for_min_age(),
                )
            
            if not username or not password:
                return render_template('Register.html', error="Username and password are required.", birthdate_max=_birthdate_max_for_min_age())
            if password != confirm_password:
                return render_template('Register.html', error="Passwords do not match.", birthdate_max=_birthdate_max_for_min_age())
            
            # Check if user already exists
            existing_user = User.query.filter_by(username=username).first()
            if existing_user:
                return render_template('Register.html', error="Username already exists. Please choose a different username.", birthdate_max=_birthdate_max_for_min_age())
            
            # Check if this is the first user (make them admin)
            is_first_user = User.query.first() is None
            
            # Create new user
            reg = request.form.get('region', '').strip().upper()[:2]
            if reg not in ('PH', 'US'):
                return render_template('Register.html', error="Please choose your region (Philippines or United States).", birthdate_max=_birthdate_max_for_min_age())
            new_user = User(
                username=username,
                email=email if email else None,
                full_name=full_name if full_name else None,
                birthdate=birthdate,
                region=reg,
                is_admin=is_first_user,
                role='admin' if is_first_user else 'user'
            )
            new_user.set_password(password)
            
            # Add to database
            db.session.add(new_user)
            db.session.commit()
            
            # Set session and redirect
            session['username'] = username
            return redirect(url_for('dashboard'))
            
        except Exception as e:
            db.session.rollback()
            error_msg = str(e)
            # Provide more user-friendly error messages
            if 'no such table' in error_msg.lower() or 'no such column' in error_msg.lower():
                # Tables don't exist or schema is outdated, try safe create/migrate path
                try:
                    with app.app_context():
                        db.create_all()
                    return render_template('Register.html', error="Database schema was initialized. Please try registering again.", birthdate_max=_birthdate_max_for_min_age())
                except Exception as recreate_error:
                    return render_template('Register.html', error=f"Database error: {str(recreate_error)}. Please contact support.", birthdate_max=_birthdate_max_for_min_age())
            return render_template('Register.html', error=f"Registration failed: {error_msg}. Please try again.", birthdate_max=_birthdate_max_for_min_age())

@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect(url_for('index'))


@app.route('/notifications/<int:notification_id>/read', methods=['POST'])
def mark_notification_read(notification_id):
    if 'username' not in session:
        return redirect(url_for('index'))
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))
    notif = Notification.query.get_or_404(notification_id)
    if notif.user_id != current_user.id:
        abort(404)
    notif.is_read = True
    db.session.commit()
    return redirect(url_for('dashboard'))

@app.route('/post', methods=['POST'])
def post():
    if 'username' not in session:
        return redirect(url_for('index'))

    user = User.query.filter_by(username=session['username']).first()
    if not user:
        session.pop('username', None)
        return redirect(url_for('index'))
    
    content = request.form['content']
    new_post = Post(content=content, user_id=user.id)
    db.session.add(new_post)
    db.session.commit()

    return redirect(url_for('dashboard'))

@app.route('/post/delete/<int:post_id>')
def delete_post(post_id):
    if 'username' not in session:
        return redirect(url_for('index'))

    user = User.query.filter_by(username=session['username']).first()
    if not user:
        session.pop('username', None)
        return redirect(url_for('index'))
    
    post = Post.query.get(post_id)
    if not post or post.user_id != user.id:
        abort(404)

    db.session.delete(post)
    db.session.commit()

    return redirect(url_for('dashboard'))


@app.route('/transportations')
def transportations():
    # Always ensure default transportations exist before rendering the page
    initialize_default_transportations()
    q = request.args.get('q', '').strip()
    location_hint = request.args.get('location', '').strip()
    pickup_hint = request.args.get('pickup_date', '').strip()
    return_hint = request.args.get('return_date', '').strip()
    min_price = request.args.get('min_price', type=float)
    max_price = request.args.get('max_price', type=float)
    seats = request.args.get('seats', type=int)
    fuel_type = request.args.get('fuel_type', '').strip().lower()
    transmission = request.args.get('transmission', '').strip().lower()
    region_filter = request.args.get('region', '').strip().upper()
    sort_by = request.args.get('sort', 'newest').strip().lower()
    query = Transportation.query
    if q:
        query = query.filter(Transportation.name.ilike(f'%{q}%'))
    if min_price is not None:
        query = query.filter(Transportation.price >= float(min_price))
    if max_price is not None:
        query = query.filter(Transportation.price <= float(max_price))
    if seats:
        query = query.filter(Transportation.seats >= seats)
    if fuel_type:
        query = query.filter(func.lower(Transportation.fuel_type) == fuel_type)
    if transmission:
        query = query.filter(func.lower(Transportation.transmission) == transmission)
    if region_filter in ('PH', 'US'):
        query = query.filter(Transportation.region == region_filter)
    current_user = None
    if 'username' in session:
        current_user = User.query.filter_by(username=session['username']).first()
        if current_user and not (current_user.is_admin or current_user.is_staff()):
            query = query.filter(Transportation.region == current_user.region)
    if sort_by == 'price_low':
        query = query.order_by(Transportation.price.asc(), Transportation.id.desc())
    elif sort_by == 'price_high':
        query = query.order_by(Transportation.price.desc(), Transportation.id.desc())
    elif sort_by == 'name_az':
        query = query.order_by(func.lower(Transportation.name).asc())
    else:
        sort_by = 'newest'
        query = query.order_by(Transportation.id.desc())
    transports = query.all()
    
    is_admin = False
    is_staff = False
    if current_user:
        is_admin = current_user.is_admin
        is_staff = current_user.is_staff()
    pickup_date = _parse_iso_date(pickup_hint)
    return_date = _parse_iso_date(return_hint)
    if pickup_date and return_date and return_date < pickup_date:
        return_date = pickup_date
    unavailable_ids = set()
    if pickup_date and return_date:
        start_dt = datetime.combine(pickup_date, datetime.min.time())
        end_dt = datetime.combine(return_date, datetime.max.time())
        unavailable_rows = (
            Booking.query.with_entities(Booking.transportation_id)
            .filter(
                Booking.payment_status == 'paid',
                Booking.rental_start.isnot(None),
                Booking.rental_end.isnot(None),
                Booking.rental_start <= end_dt,
                Booking.rental_end >= start_dt,
            )
            .distinct()
            .all()
        )
        unavailable_ids = {row[0] for row in unavailable_rows}
    fleet_payload = []
    for t in transports:
        fleet_payload.append(
            {
                'id': t.id,
                'name': t.name,
                'price': float(t.price),
                'region': t.region,
                'region_label': region_label(t.region),
                'formatted_price': money_by_region(t.price, t.region),
                'currency_code': _currency_for_region(t.region)[1],
                'description': (t.description or '').strip(),
                'specs': (t.specs or '').strip(),
                'image': vehicle_image_url(t),
                'seats': t.seats or 0,
                'fuel_type': (t.fuel_type or '').strip(),
                'transmission': (t.transmission or '').strip(),
                'available': t.id not in unavailable_ids,
            }
        )

    return render_template(
        'Transportation.html',
        transportations=transports,
        is_admin=is_admin,
        is_staff=is_staff,
        search_q=q,
        search_location=location_hint,
        search_pickup=pickup_hint,
        search_return=return_hint,
        filter_min_price=min_price if min_price is not None else '',
        filter_max_price=max_price if max_price is not None else '',
        filter_seats=seats if seats else '',
        filter_fuel=fuel_type,
        filter_transmission=transmission,
        filter_region=region_filter if region_filter in ('PH', 'US') else '',
        filter_sort=sort_by,
        has_date_filter=bool(pickup_date and return_date),
        unavailable_ids=unavailable_ids,
        viewer_region=current_user.region if current_user else None,
        viewer=current_user,
        fleet_json=json.dumps(fleet_payload, ensure_ascii=True),
    )


@app.route('/vehicle/<int:vehicle_id>')
def vehicle_detail(vehicle_id):
    initialize_default_transportations()
    transport = Transportation.query.get_or_404(vehicle_id)
    current_user = None
    if 'username' in session:
        current_user = User.query.filter_by(username=session['username']).first()
        if not current_user:
            session.pop('username', None)
        elif transport.region != current_user.region and not current_user.is_staff():
            flash(
                f'This vehicle is only listed in {region_label(transport.region)}. '
                f'Your profile is set to {region_label(current_user.region)}.',
                'error',
            )
            return redirect(url_for('transportations'))

    pickup_hint = request.args.get('pickup_date', '').strip()
    return_hint = request.args.get('return_date', '').strip()
    pickup_date = _parse_iso_date(pickup_hint)
    return_date = _parse_iso_date(return_hint)
    if pickup_date and return_date and return_date < pickup_date:
        return_date = pickup_date
    has_dates = bool(pickup_date and return_date)
    available_for_dates = True
    if has_dates:
        available_for_dates = not _has_vehicle_booking_conflict(transport.id, pickup_date, return_date)
    img = vehicle_image_url(transport)
    return render_template(
        'VehicleDetail.html',
        transport=transport,
        search_pickup=pickup_hint,
        search_return=return_hint,
        has_date_filter=has_dates,
        available_for_dates=available_for_dates,
        vehicle_image=img,
        viewer=current_user,
        today_iso=datetime.utcnow().date().isoformat(),
    )


@app.route('/book/<int:transportation_id>', methods=['POST'])
def book_transportation(transportation_id):
    if 'username' not in session:
        return redirect(url_for('index'))

    transport = Transportation.query.get_or_404(transportation_id)
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))

    if transport.region != current_user.region and not current_user.is_staff():
        flash(
            f'This vehicle is only available in {region_label(transport.region)}. '
            f'Your profile is set to {region_label(current_user.region)}.',
            'error',
        )
        return redirect(url_for('transportations'))

    if not current_user.is_staff():
        if not current_user.birthdate or _age_years(current_user.birthdate) < MIN_RENTAL_AGE:
            flash(
                f'You must be at least {MIN_RENTAL_AGE} and have your date of birth saved on your profile to rent. '
                'Update your profile with a valid birth date.',
                'error',
            )
            return redirect(url_for('dashboard'))
    
    try:
        quantity = int(request.form.get('quantity', '1'))
        if quantity <= 0:
            quantity = 1
        if quantity > 365:
            quantity = 365
    except ValueError:
        quantity = 1
    pickup_d = _parse_iso_date(request.form.get('pickup_date'))
    return_d = _parse_iso_date(request.form.get('return_date'))
    if pickup_d and return_d and return_d >= pickup_d:
        quantity = max(1, min(_inclusive_rental_days(pickup_d, return_d), 365))
    else:
        pickup_d, return_d = _default_pickup_return_from_qty(quantity)
    total_price = round(float(transport.price) * quantity, 2)
    if _has_vehicle_booking_conflict(transport.id, pickup_d, return_d):
        flash('This vehicle is not available for the selected period. Please choose another unit or adjust dates.', 'error')
        return redirect(url_for('transportations'))
    session.pop('rental_terms_accepted_at', None)
    # Store booking info in session for payment page
    session['pending_booking'] = {
        'transportation_id': transport.id,
        'quantity': quantity,
        'total_price': total_price,
        'pickup_date': pickup_d.isoformat(),
        'return_date': return_d.isoformat(),
    }

    return redirect(url_for('rental_terms'))


def _finalize_rental_request_after_payment(booking):
    rid = session.pop('checkout_rental_request_id', None)
    if not rid:
        return
    rr = RentalRequest.query.get(rid)
    if rr and rr.user_id == booking.user_id and rr.status == 'approved':
        rr.booking_id = booking.id
        rr.status = 'paid'
        db.session.commit()


@app.route('/rental/compliance')
def rental_compliance():
    """Legacy URL â€” terms and ID are now separate steps."""
    return redirect(url_for('rental_terms'))


@app.route('/rental/terms', methods=['GET', 'POST'])
def rental_terms():
    """Step 1: read and accept Terms & Conditions only (no ID upload)."""
    if 'username' not in session:
        return redirect(url_for('loginpage'))
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))

    pb = session.get('pending_booking')
    if not pb:
        flash('Start by choosing a vehicle from the fleet.', 'error')
        return redirect(url_for('transportations'))

    if request.method == 'GET':
        if request.args.get('restart'):
            session.pop('rental_terms_accepted_at', None)
            session.modified = True
        transport = Transportation.query.get(pb.get('transportation_id'))
        return render_template('RentalTerms.html', transport=transport, pending=pb)

    if not request.form.get('terms_agree'):
        flash('You must read and accept the Terms & Conditions to continue.', 'error')
        return redirect(url_for('rental_terms'))

    session['rental_terms_accepted_at'] = datetime.utcnow().isoformat()
    session.modified = True
    return redirect(url_for('rental_upload_id'))


@app.route('/rental/upload-id', methods=['GET', 'POST'])
def rental_upload_id():
    """Step 2: upload government ID (only after terms were accepted)."""
    if 'username' not in session:
        return redirect(url_for('loginpage'))
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))

    if not session.get('pending_booking'):
        flash('Your booking session expired. Please choose a vehicle again.', 'error')
        return redirect(url_for('transportations'))

    if not session.get('rental_terms_accepted_at'):
        flash('Please accept the Terms & Conditions first.', 'error')
        return redirect(url_for('rental_terms'))

    if request.method == 'GET':
        pb = session.get('pending_booking')
        transport = Transportation.query.get(pb.get('transportation_id'))
        return render_template(
            'RentalIdUpload.html',
            transport=transport,
            pending=pb,
        )

    id_type = request.form.get('id_document_type', '').strip().lower()
    if id_type not in ('drivers_license', 'passport', 'national_id'):
        flash('Please select a valid ID type (driver license, passport, or national ID).', 'error')
        return redirect(url_for('rental_upload_id'))

    if 'id_document' not in request.files:
        flash('Please upload a clear photo of your ID.', 'error')
        return redirect(url_for('rental_upload_id'))
    id_file = request.files['id_document']
    if not id_file or not id_file.filename or not allowed_file(id_file.filename):
        flash('Upload a valid image file (PNG, JPG, JPEG, GIF, or WEBP) for your ID.', 'error')
        return redirect(url_for('rental_upload_id'))

    pending = dict(session['pending_booking'])
    transport = Transportation.query.get_or_404(pending['transportation_id'])
    pending = _normalize_pending_booking(pending, transport)

    ext = secure_filename(id_file.filename).rsplit('.', 1)[1].lower()
    try:
        id_file.stream.seek(0)
    except (AttributeError, OSError):
        pass
    raw = id_file.read()
    if not raw:
        flash('Please upload a clear photo of your ID.', 'error')
        return redirect(url_for('rental_upload_id'))
    if len(raw) > MAX_RENTAL_ID_IMAGE_BYTES:
        flash('ID image is too large. Please use a file under 8 MB.', 'error')
        return redirect(url_for('rental_upload_id'))
    mime_type = _mime_for_id_extension(ext)

    terms_raw = session.get('rental_terms_accepted_at')
    try:
        terms_accepted_at = datetime.fromisoformat(terms_raw) if terms_raw else datetime.utcnow()
    except (TypeError, ValueError):
        terms_accepted_at = datetime.utcnow()

    rr = RentalRequest(
        user_id=current_user.id,
        transportation_id=pending['transportation_id'],
        quantity=pending['quantity'],
        total_price=pending['total_price'],
        pickup_date=pending['pickup_date'],
        return_date=pending['return_date'],
        terms_accepted_at=terms_accepted_at,
        id_document_type=id_type,
        id_image_local=None,
        status='pending_review',
    )
    db.session.add(rr)
    db.session.flush()
    db.session.add(
        RentalIdImage(
            rental_request_id=rr.id,
            mime_type=mime_type,
            image_data=raw,
            byte_size=len(raw),
        )
    )
    db.session.commit()

    session.pop('pending_booking', None)
    session.pop('rental_terms_accepted_at', None)
    session.modified = True
    flash(
        'Your ID was submitted for review. An administrator will verify your documents. '
        'You can pay once it is approved â€” check your dashboard.',
        'success',
    )
    return redirect(url_for('dashboard'))


@app.route('/rental/pay/<int:request_id>')
def rental_pay_start(request_id):
    """Load an approved rental request into checkout session."""
    if 'username' not in session:
        return redirect(url_for('loginpage'))
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))

    rr = RentalRequest.query.get_or_404(request_id)
    if rr.user_id != current_user.id:
        abort(404)
    if rr.status != 'approved':
        flash('This rental is not approved for payment yet.', 'error')
        return redirect(url_for('dashboard'))
    if rr.booking_id:
        flash('This rental request was already paid.', 'error')
        return redirect(url_for('dashboard'))

    session['pending_booking'] = {
        'transportation_id': rr.transportation_id,
        'quantity': rr.quantity,
        'total_price': rr.total_price,
        'pickup_date': rr.pickup_date,
        'return_date': rr.return_date,
    }
    session['checkout_rental_request_id'] = rr.id
    session.modified = True
    return redirect(url_for('payment'))


@app.route('/rental/request/<int:request_id>/cancel', methods=['POST'])
def rental_request_user_cancel(request_id):
    if 'username' not in session:
        return redirect(url_for('loginpage'))
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))
    rr = RentalRequest.query.get_or_404(request_id)
    if rr.user_id != current_user.id:
        abort(404)
    if rr.status != 'pending_review':
        flash('Only requests awaiting admin review can be cancelled.', 'error')
        return redirect(url_for('dashboard'))
    if rr.booking_id:
        flash('This request is already linked to a booking.', 'error')
        return redirect(url_for('dashboard'))
    db.session.delete(rr)
    db.session.commit()
    flash('Your rental request was cancelled. You can start a new booking from the fleet anytime.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/admin/rental-request/<int:request_id>/approve', methods=['POST'])
def admin_rental_request_approve(request_id):
    if 'username' not in session:
        return redirect(url_for('index'))
    actor = User.query.filter_by(username=session['username']).first()
    if not actor or not actor.is_staff():
        abort(404)
    rr = RentalRequest.query.get_or_404(request_id)
    if rr.status != 'pending_review':
        flash('That request is not awaiting review.', 'error')
        return redirect(url_for('dashboard'))
    rr.status = 'approved'
    rr.reviewed_at = datetime.utcnow()
    rr.reviewed_by_id = actor.id
    rr.rejection_reason = None
    _record_audit(actor.id, 'rental_request_approved', 'rental_request', rr.id, 'approved')
    _notify_user(
        rr.user_id,
        'Rental request approved',
        f'Reference RR-{rr.id:05d} is approved. You can now proceed to payment from your dashboard.',
        'success',
    )
    db.session.commit()
    flash(f'Rental request #{rr.id} approved. The renter can proceed to payment.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/admin/rental-request/<int:request_id>/reject', methods=['POST'])
def admin_rental_request_reject(request_id):
    if 'username' not in session:
        return redirect(url_for('index'))
    actor = User.query.filter_by(username=session['username']).first()
    if not actor or not actor.is_staff():
        abort(404)
    rr = RentalRequest.query.get_or_404(request_id)
    if rr.status != 'pending_review':
        flash('That request is not awaiting review.', 'error')
        return redirect(url_for('dashboard'))
    reason = request.form.get('rejection_reason', '').strip() or 'Did not meet verification requirements.'
    rr.status = 'rejected'
    rr.reviewed_at = datetime.utcnow()
    rr.reviewed_by_id = actor.id
    rr.rejection_reason = reason
    _record_audit(actor.id, 'rental_request_rejected', 'rental_request', rr.id, reason)
    _notify_user(
        rr.user_id,
        'Rental request rejected',
        f'Reference RR-{rr.id:05d} was rejected. Reason: {reason}',
        'error',
    )
    db.session.commit()
    flash(f'Rental request #{rr.id} was rejected.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/admin/rental-request/<int:request_id>/id-image')
def admin_rental_id_image(request_id):
    """Serve a rental ID image from the database (staff only)."""
    if 'username' not in session:
        abort(404)
    actor = User.query.filter_by(username=session['username']).first()
    if not actor or not actor.is_staff():
        abort(404)
    rr = RentalRequest.query.options(joinedload(RentalRequest.id_image_record)).get_or_404(request_id)
    rec = rr.id_image_record
    if rec and rec.image_data:
        return send_file(
            BytesIO(rec.image_data),
            mimetype=rec.mime_type or 'application/octet-stream',
            max_age=0,
            download_name='rental_id_verification',
        )
    if rr.id_image_local:
        path = os.path.normpath(os.path.join(basedir, 'static', rr.id_image_local.replace('/', os.sep)))
        static_root = os.path.normpath(os.path.join(basedir, 'static'))
        if path.startswith(static_root) and os.path.isfile(path):
            ext = path.rsplit('.', 1)[-1].lower() if '.' in path else ''
            return send_file(path, mimetype=_mime_for_id_extension(ext))
    abort(404)


@app.route('/admin/renter-review/add', methods=['POST'])
def admin_renter_review_add():
    if 'username' not in session:
        return redirect(url_for('index'))
    actor = User.query.filter_by(username=session['username']).first()
    if not actor or not actor.is_staff():
        abort(404)
    renter_id = request.form.get('renter_id', type=int)
    rating = request.form.get('rating', type=int)
    body = request.form.get('body', '').strip()
    if not renter_id or not body or rating is None or rating < 1 or rating > 5:
        flash('Renter, rating (1â€“5), and review text are required.', 'error')
        return redirect(url_for('dashboard'))
    renter = User.query.get_or_404(renter_id)
    if renter.is_staff():
        flash('Pick a customer account to review.', 'error')
        return redirect(url_for('dashboard'))
    rev = RenterReview(renter_id=renter.id, author_id=actor.id, rating=rating, body=body)
    db.session.add(rev)
    _record_audit(actor.id, 'renter_review_added', 'user', renter.id, f'rating={rating}')
    db.session.commit()
    flash('Renter review saved.', 'success')
    return redirect(url_for('dashboard'))


def _create_paid_booking(current_user, pending, payment_method, pay_currency, payment_reference=None, total_price_override=None):
    """Create a paid booking from a pending dict with transportation_id, quantity, total_price."""
    plate_number = generate_unique_plate_number()
    transport = Transportation.query.get(pending['transportation_id'])
    if not transport:
        raise ValueError('Invalid vehicle for booking.')
    reg = transport.region or (current_user.region or 'PH')
    pu = _parse_iso_date(pending.get('pickup_date'))
    re = _parse_iso_date(pending.get('return_date'))
    if pu and re and re >= pu:
        days = _inclusive_rental_days(pu, re)
        days = max(1, min(days, 365))
        start = datetime.combine(pu, datetime.min.time())
        end = datetime.combine(re, datetime.min.time())
    else:
        days = max(int(pending.get('quantity', 1)), 1)
        days = min(days, 365)
        start = datetime.utcnow()
        end = start + timedelta(days=days)
    seed = (Booking.query.count() or 0) + 1
    lat, lng = _mock_gps_coords(reg, seed)
    if total_price_override is not None:
        total_price = round(float(total_price_override), 2)
    else:
        total_price = round(float(transport.price) * days, 2)
    delivery_address = _clean_delivery_address(pending.get('delivery_address', ''))
    if _has_vehicle_booking_conflict(transport.id, start.date(), end.date()):
        raise ValueError('Selected vehicle is no longer available for those dates.')

    booking = Booking(
        user_id=current_user.id,
        transportation_id=pending['transportation_id'],
        quantity=days,
        total_price=total_price,
        plate_number=plate_number,
        payment_status='paid',
        booking_status='confirmed',
        payment_method=payment_method,
        pay_currency=pay_currency,
        rental_start=start,
        rental_end=end,
        rental_days=days,
        mock_lat=lat,
        mock_lng=lng,
        status_mode='auto',
        delivery_received=False,
        delivery_received_at=None,
        delivery_returned=False,
        delivery_returned_at=None,
        delivery_returned_by_user=False,
        delivery_returned_by_user_at=None,
        expected_delivery_at=start,
        actual_delivery_at=None,
        is_delivery_delayed=False,
        delivery_delay_minutes=0,
        delay_reason=None,
        delay_reported_by_id=None,
        delay_reported_at=None,
        delivery_address=delivery_address or None,
    )
    db.session.add(booking)
    db.session.flush()
    db.session.add(
        PaymentTransaction(
            booking_id=booking.id,
            user_id=current_user.id,
            method=payment_method,
            currency=pay_currency,
            amount=total_price,
            reference=(payment_reference or '')[:80] or None,
            status='paid',
        )
    )
    _notify_user(
        current_user.id,
        'Payment received',
        f'Booking #{booking.id} is confirmed as paid via {payment_method.upper()}.',
        'success',
    )
    db.session.commit()
    return booking


@app.route('/payment')
def payment():
    if 'username' not in session:
        return redirect(url_for('index'))
    
    if 'pending_booking' not in session:
        return redirect(url_for('transportations'))
    
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))

    rid = session.get('checkout_rental_request_id')
    if not rid:
        flash('Complete Terms (step 1) and ID upload (step 2), then wait for admin approval. After that, use Pay now on your dashboard.', 'error')
        return redirect(url_for('dashboard'))
    rr = RentalRequest.query.get(rid)
    if not rr or rr.user_id != current_user.id or rr.status != 'approved':
        session.pop('checkout_rental_request_id', None)
        session.pop('pending_booking', None)
        flash('That checkout is invalid or is no longer approved. Start again from the fleet if needed.', 'error')
        return redirect(url_for('dashboard'))

    session['pending_booking'] = {
        'transportation_id': rr.transportation_id,
        'quantity': rr.quantity,
        'total_price': rr.total_price,
        'pickup_date': rr.pickup_date,
        'return_date': rr.return_date,
    }
    session.modified = True

    pending = dict(session['pending_booking'])
    transport = Transportation.query.get_or_404(pending['transportation_id'])
    pending = _normalize_pending_booking(pending, transport)
    session['pending_booking'] = pending
    session.modified = True
    date_min = datetime.utcnow().date().isoformat()

    return render_template(
        'Payment.html',
        transport=transport,
        quantity=pending['quantity'],
        total_price=pending['total_price'],
        pickup_date=pending['pickup_date'],
        return_date=pending['return_date'],
        delivery_address=pending.get('delivery_address', ''),
        date_min=date_min,
        user=current_user,
    )


@app.route('/payment/update-rental', methods=['POST'])
def payment_update_rental():
    """Set pickup/return calendar dates before paying; recalculates total and clears GCash QR snapshot."""
    if 'username' not in session:
        return redirect(url_for('index'))
    if 'pending_booking' not in session:
        return redirect(url_for('transportations'))
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))

    rid_check = session.get('checkout_rental_request_id')
    rr_check = RentalRequest.query.get(rid_check) if rid_check else None
    if not rr_check or rr_check.user_id != current_user.id or rr_check.status != 'approved':
        flash('Checkout session is invalid.', 'error')
        return redirect(url_for('dashboard'))

    pending = dict(session['pending_booking'])
    transport = Transportation.query.get_or_404(pending['transportation_id'])
    if transport.region != current_user.region and not current_user.is_staff():
        flash('You cannot modify this booking.', 'error')
        return redirect(url_for('transportations'))

    pu = _parse_iso_date(request.form.get('pickup_date'))
    re = _parse_iso_date(request.form.get('return_date'))
    if not pu or not re:
        flash('Please choose both pickup (borrow) and return dates.', 'error')
        return redirect(url_for('payment'))
    if re < pu:
        flash('Return date must be on or after pickup date.', 'error')
        return redirect(url_for('payment'))
    days = _inclusive_rental_days(pu, re)
    if days > 365:
        flash('Maximum rental length is 365 days.', 'error')
        return redirect(url_for('payment'))
    if _has_vehicle_booking_conflict(transport.id, pu, re):
        flash('Vehicle is not available for the updated dates. Try another schedule.', 'error')
        return redirect(url_for('payment'))

    pending['pickup_date'] = pu.isoformat()
    pending['return_date'] = re.isoformat()
    pending['quantity'] = days
    pending['total_price'] = round(float(transport.price) * days, 2)
    session['pending_booking'] = pending
    session.modified = True

    rid = session.get('checkout_rental_request_id')
    if rid:
        rr = RentalRequest.query.get(rid)
        if rr and rr.user_id == current_user.id and rr.status == 'approved':
            rr.pickup_date = pending['pickup_date']
            rr.return_date = pending['return_date']
            rr.quantity = pending['quantity']
            rr.total_price = pending['total_price']
            db.session.commit()

    session.pop('gcash_qr', None)
    flash('Rental dates updated. Total was recalculated.', 'success')
    return redirect(url_for('payment'))


@app.route('/payment/gcash-qr/prepare', methods=['POST'])
def gcash_qr_prepare():
    """Start GCash QR flow: store checkout snapshot and show QR + confirmation step."""
    if 'username' not in session:
        return redirect(url_for('loginpage'))
    if 'pending_booking' not in session:
        return redirect(url_for('transportations'))
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))

    if request.form.get('payment_method', '').strip().lower() != 'gcash':
        return redirect(url_for('payment'))

    if not session.get('checkout_rental_request_id'):
        flash('Complete rental verification before using GCash checkout.', 'error')
        return redirect(url_for('dashboard'))

    pay_currency = request.form.get('pay_currency', 'PHP').strip().upper()
    if pay_currency not in ('PHP', 'USD'):
        pay_currency = 'PHP'
    delivery_address = _clean_delivery_address(request.form.get('delivery_address', ''))
    if not delivery_address:
        flash('Please enter a delivery address before continuing to GCash QR.', 'error')
        return redirect(url_for('payment'))

    pending = dict(session['pending_booking'])
    pending['delivery_address'] = delivery_address
    transport_gc = Transportation.query.get_or_404(pending['transportation_id'])
    pending = _normalize_pending_booking(pending, transport_gc)
    session['pending_booking'] = pending
    session.modified = True

    reference = secrets.token_hex(5).upper()
    session['gcash_qr'] = {
        'transportation_id': pending['transportation_id'],
        'quantity': pending['quantity'],
        'total_price': pending['total_price'],
        'pay_currency': pay_currency,
        'reference': reference,
        'pickup_date': pending.get('pickup_date'),
        'return_date': pending.get('return_date'),
        'delivery_address': delivery_address,
    }
    return redirect(url_for('payment_gcash_qr'))


@app.route('/payment/gcash-qr')
def payment_gcash_qr():
    if 'username' not in session:
        return redirect(url_for('loginpage'))
    g = session.get('gcash_qr')
    if not g:
        return redirect(url_for('payment'))
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))

    transport = Transportation.query.get_or_404(g['transportation_id'])
    masked_uid = f"Â·Â·Â·Â·Â·Â·Â·Â·Â·Â·Â·Â·{g['reference'][-6:].upper()}"

    return render_template(
        'PaymentGcashQr.html',
        transport=transport,
        total_price=g['total_price'],
        quantity=g.get('quantity', 1),
        pickup_date=g.get('pickup_date'),
        return_date=g.get('return_date'),
        delivery_address=g.get('delivery_address'),
        pay_currency=g['pay_currency'],
        reference=g['reference'],
        masked_uid=masked_uid,
        merchant_title=GCASH_QR_MERCHANT_TITLE,
        masked_account_name=GCASH_QR_MASKED_NAME,
        masked_mobile=GCASH_QR_MASKED_MOBILE,
        user=current_user,
    )


@app.route('/payment/gcash-qr/cancel')
def gcash_qr_cancel():
    session.pop('gcash_qr', None)
    return redirect(url_for('payment'))


@app.route('/payment/paymongo/start', methods=['POST'])
def paymongo_checkout_start():
    return _paymongo_begin_hosted_checkout()


@app.route('/payment/paymongo/complete', methods=['GET'])
def paymongo_checkout_complete():
    if 'username' not in session:
        return redirect(url_for('loginpage'))
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))

    cs_id = session.get('paymongo_return_cs_id')
    if not cs_id:
        flash('No PayMongo session found. Start checkout again from the payment page.', 'error')
        return redirect(url_for('dashboard'))

    rid = session.get('checkout_rental_request_id')
    if rid:
        rr_early = RentalRequest.query.get(rid)
        if rr_early and rr_early.booking_id:
            session.pop('paymongo_return_cs_id', None)
            session.pop('paymongo_expected_centavos', None)
            session.pop('pending_booking', None)
            return redirect(url_for('receipt', booking_id=rr_early.booking_id))

    try:
        body = _paymongo_request('GET', f'/checkout_sessions/{cs_id}', None)
    except PayMongoAPIError as exc:
        if exc.status == 404:
            flash(
                'PayMongo checkout session was not found. Make sure you are using the same mode/key (test vs live) for this payment.',
                'error',
            )
        else:
            flash('Could not verify payment with PayMongo. Try again or contact support.', 'error')
        return redirect(url_for('payment'))

    paid_refs = _paymongo_checkout_paid_payment_refs(body)
    if not paid_refs:
        flash(
            'Payment is not showing as completed yet. Wait a moment and refresh this page, or return to checkout.',
            'error',
        )
        return redirect(url_for('payment'))

    meta = ((body.get('data') or {}).get('attributes') or {}).get('metadata') or {}
    if str(meta.get('user_id', '')) != str(current_user.id):
        flash('This payment does not match your account.', 'error')
        return redirect(url_for('dashboard'))

    if rid and meta.get('rental_request_id') and str(meta.get('rental_request_id')) != str(rid):
        flash('Checkout data no longer matches this rental. Start again from your dashboard.', 'error')
        return redirect(url_for('dashboard'))

    pending = dict(session.get('pending_booking') or {})
    if not pending.get('transportation_id'):
        flash('Your booking session expired. Start payment again from the dashboard.', 'error')
        return redirect(url_for('dashboard'))

    transport = Transportation.query.get_or_404(pending['transportation_id'])
    pending = _normalize_pending_booking(pending, transport)

    exp = session.get('paymongo_expected_centavos')
    try:
        expected_cents = int(exp) if exp is not None else None
    except (TypeError, ValueError):
        expected_cents = None

    first_paid_attrs = None
    for p in ((body.get('data') or {}).get('attributes') or {}).get('payments') or []:
        if not isinstance(p, dict):
            continue
        pa = p.get('attributes') or {}
        if (pa.get('status') or '').lower() == 'paid':
            first_paid_attrs = pa
            break

    paid_amount = int((first_paid_attrs or {}).get('amount') or 0)
    try:
        meta_expected = int(meta.get('expected_centavos')) if meta.get('expected_centavos') else None
    except (TypeError, ValueError):
        meta_expected = None

    if meta_expected is not None and paid_amount and paid_amount != meta_expected:
        flash('Paid amount does not match this order. Contact support before continuing.', 'error')
        return redirect(url_for('payment'))
    if expected_cents is not None and paid_amount and paid_amount != expected_cents:
        flash('Paid amount does not match this order. Contact support before continuing.', 'error')
        return redirect(url_for('payment'))

    php_total = round(paid_amount / 100.0, 2) if paid_amount else round(float(pending['total_price']) * USD_TO_PHP_RATE, 2)

    rr = RentalRequest.query.get(rid) if rid else None
    if rr and rr.booking_id:
        session.pop('paymongo_return_cs_id', None)
        session.pop('paymongo_expected_centavos', None)
        session.pop('pending_booking', None)
        return redirect(url_for('receipt', booking_id=rr.booking_id))

    payment_reference = paid_refs[0]
    existing_tx = PaymentTransaction.query.filter_by(reference=payment_reference, status='paid').first()
    if existing_tx:
        session.pop('paymongo_return_cs_id', None)
        session.pop('paymongo_expected_centavos', None)
        session.pop('pending_booking', None)
        return redirect(url_for('receipt', booking_id=existing_tx.booking_id))

    try:
        booking = _create_paid_booking(
            current_user,
            pending,
            'paymongo',
            'PHP',
            payment_reference=payment_reference,
            total_price_override=php_total,
        )
    except ValueError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('payment'))

    _finalize_rental_request_after_payment(booking)
    session.pop('paymongo_return_cs_id', None)
    session.pop('paymongo_expected_centavos', None)
    session.pop('pending_booking', None)
    return redirect(url_for('receipt', booking_id=booking.id))


@app.route('/webhooks/paymongo', methods=['POST'])
def paymongo_webhook():
    raw = request.get_data(cache=False)
    signature = request.headers.get('Paymongo-Signature', '')
    if not _verify_paymongo_webhook_signature(raw, signature):
        return {'ok': False, 'error': 'invalid signature'}, 400

    try:
        payload = json.loads(raw.decode('utf-8'))
    except json.JSONDecodeError:
        return {'ok': False, 'error': 'invalid json'}, 400

    event_data = (payload.get('data') or {})
    event_attrs = event_data.get('attributes') or {}
    event_type = (event_attrs.get('type') or '').strip().lower()
    if event_type != 'checkout_session.payment.paid':
        return {'ok': True, 'ignored': True}, 200

    checkout = event_attrs.get('data') or {}
    checkout_attrs = checkout.get('attributes') or {}
    meta = checkout_attrs.get('metadata') or {}
    payments = checkout_attrs.get('payments') or []

    paid_payment = None
    for p in payments:
        if not isinstance(p, dict):
            continue
        attrs = p.get('attributes') or {}
        if (attrs.get('status') or '').lower() == 'paid':
            paid_payment = p
            break
    if not paid_payment:
        return {'ok': True, 'ignored': True}, 200

    payment_reference = (paid_payment.get('id') or '').strip()
    if not payment_reference:
        return {'ok': False, 'error': 'missing payment id'}, 400

    existing_tx = PaymentTransaction.query.filter_by(reference=payment_reference, status='paid').first()
    if existing_tx:
        return {'ok': True, 'idempotent': True}, 200

    try:
        user_id = int(meta.get('user_id'))
        rid = int(meta.get('rental_request_id'))
        expected_centavos = int(meta.get('expected_centavos'))
    except (TypeError, ValueError):
        return {'ok': False, 'error': 'invalid metadata'}, 400

    user = User.query.get(user_id)
    rr = RentalRequest.query.get(rid)
    if not user or not rr:
        return {'ok': False, 'error': 'invalid references'}, 400
    if rr.user_id != user.id:
        return {'ok': False, 'error': 'user mismatch'}, 400
    if rr.booking_id:
        return {'ok': True, 'idempotent': True}, 200
    if rr.status != 'approved':
        return {'ok': False, 'error': 'rental request not approved'}, 409

    paid_amount = int(((paid_payment.get('attributes') or {}).get('amount')) or 0)
    if paid_amount <= 0 or expected_centavos != paid_amount:
        return {'ok': False, 'error': 'amount mismatch'}, 409

    transport = Transportation.query.get(rr.transportation_id)
    if not transport:
        return {'ok': False, 'error': 'transport missing'}, 400

    pending = _normalize_pending_booking(
        {
            'transportation_id': rr.transportation_id,
            'quantity': rr.quantity,
            'total_price': rr.total_price,
            'pickup_date': rr.pickup_date,
            'return_date': rr.return_date,
            'delivery_address': '',
        },
        transport,
    )
    php_total = round(paid_amount / 100.0, 2)
    try:
        booking = _create_paid_booking(
            user,
            pending,
            'paymongo',
            'PHP',
            payment_reference=payment_reference,
            total_price_override=php_total,
        )
    except ValueError:
        return {'ok': False, 'error': 'booking creation failed'}, 409

    _finalize_rental_request_after_payment_by_id(booking, rid)
    return {'ok': True, 'booking_id': booking.id}, 200


@app.route('/payment/gcash-qr/confirm', methods=['POST'])
def gcash_qr_confirm():
    if 'username' not in session:
        return redirect(url_for('loginpage'))
    g = session.get('gcash_qr')
    if not g:
        return redirect(url_for('payment'))

    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))

    ref_form = request.form.get('reference', '').strip().upper()
    if ref_form != g.get('reference'):
        flash('Invalid payment session. Please start GCash checkout again.', 'error')
        session.pop('gcash_qr', None)
        return redirect(url_for('payment'))

    if not request.form.get('confirm_sent'):
        flash('Check the box to confirm you have sent the payment in GCash, then continue.', 'error')
        return redirect(url_for('payment_gcash_qr'))

    pending = {
        'transportation_id': g['transportation_id'],
        'quantity': g['quantity'],
        'total_price': g['total_price'],
        'pickup_date': g.get('pickup_date'),
        'return_date': g.get('return_date'),
        'delivery_address': g.get('delivery_address', ''),
    }
    transport_gc = Transportation.query.get_or_404(pending['transportation_id'])
    pending = _normalize_pending_booking(pending, transport_gc)
    booking = _create_paid_booking(current_user, pending, 'gcash', g['pay_currency'], payment_reference=g.get('reference'))
    _finalize_rental_request_after_payment(booking)
    session.pop('gcash_qr', None)
    session.pop('pending_booking', None)
    return redirect(url_for('receipt', booking_id=booking.id))


@app.route('/process_payment', methods=['POST'])
def process_payment():
    if 'username' not in session:
        return redirect(url_for('index'))
    
    if 'pending_booking' not in session:
        return redirect(url_for('transportations'))
    
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))
    
    pending = dict(session['pending_booking'])
    transport = Transportation.query.get_or_404(pending['transportation_id'])
    pending = _normalize_pending_booking(pending, transport)
    session['pending_booking'] = pending
    session.modified = True

    rid = session.get('checkout_rental_request_id')
    if rid:
        rr = RentalRequest.query.get(rid)
        if not rr or rr.user_id != current_user.id or rr.status != 'approved':
            flash('Invalid checkout session.', 'error')
            return redirect(url_for('dashboard'))

    allowed_methods = frozenset(
        {'gcash', 'paymaya', 'credit_card', 'debit_card', 'paypal', 'cash', 'paymongo'}
    )
    payment_method = request.form.get('payment_method', '').strip().lower()
    if payment_method not in allowed_methods:
        flash('Please choose a valid payment method.', 'error')
        return redirect(url_for('payment'))

    pay_currency = request.form.get('pay_currency', 'PHP').strip().upper()
    if pay_currency not in ('PHP', 'USD'):
        pay_currency = 'PHP'
    delivery_address = _clean_delivery_address(request.form.get('delivery_address', ''))
    if not delivery_address:
        flash('Please enter a delivery address for the vehicle.', 'error')
        return redirect(url_for('payment'))
    pending['delivery_address'] = delivery_address

    if payment_method == 'paymongo':
        return _paymongo_begin_hosted_checkout()

    if payment_method == 'gcash':
        flash('For GCash, use â€œContinue to GCash QRâ€, scan the code, then confirm payment.', 'error')
        return redirect(url_for('payment'))

    if payment_method in ('credit_card', 'debit_card'):
        card_no = request.form.get('card_number', '').replace(' ', '').strip()
        expiry = request.form.get('expiry', '').strip()
        cvv = request.form.get('cvv', '').strip()
        if len(card_no) < 12 or not expiry or len(cvv) < 3:
            flash('Please enter valid card number, expiry, and CVV for card payments.', 'error')
            return redirect(url_for('payment'))

    try:
        booking = _create_paid_booking(current_user, pending, payment_method, pay_currency)
    except ValueError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('payment'))
    _finalize_rental_request_after_payment(booking)
    session.pop('pending_booking', None)
    return redirect(url_for('receipt', booking_id=booking.id))

@app.route('/receipt/<int:booking_id>')
def receipt(booking_id):
    if 'username' not in session:
        return redirect(url_for('index'))

    _sync_booking_lifecycle_states()
    booking = Booking.query.get_or_404(booking_id)
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))
    
    if booking.user_id != current_user.id:
        abort(404)

    tx = (
        PaymentTransaction.query.filter_by(booking_id=booking.id)
        .order_by(PaymentTransaction.created_at.desc())
        .first()
    )
    return render_template('Receipt.html', booking=booking, user=current_user, payment_tx=tx)


def _normalize_booking_status_for_manual(raw_status: str):
    allowed = {'active', 'completed', 'confirmed', 'cancelled'}
    st = (raw_status or '').strip().lower()
    return st if st in allowed else ''


@app.route('/booking/<int:booking_id>/mark-received', methods=['POST'])
def booking_mark_received(booking_id):
    """User notifies that the vehicle was delivered and received."""
    if 'username' not in session:
        return redirect(url_for('index'))
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))

    booking = Booking.query.get_or_404(booking_id)
    if booking.user_id != current_user.id:
        abort(404)

    if booking.payment_status != 'paid':
        flash('This booking is not eligible for status updates yet.', 'error')
        return redirect(url_for('dashboard'))

    _update_booking_delivery_workflow(
        booking,
        current_user,
        user_received=True,
        admin_returned=None,
        set_completed=None,
        audit_action='booking_delivery_received',
        audit_details=str(booking.transportation_id),
        notify_staff=True,
    )
    if not booking.expected_delivery_at:
        booking.expected_delivery_at = booking.rental_start or booking.created_at
    _apply_delivery_delay_state(
        booking,
        actual_time=booking.delivery_received_at or datetime.utcnow(),
        reason=None,
        reporter_id=current_user.id,
    )
    # User confirmed they have received the vehicle: mark lifecycle as active now.
    if booking.booking_status != 'cancelled' and not booking.delivery_returned and not booking.delivery_returned_by_user:
        booking.booking_status = 'active'
    db.session.commit()

    if booking.is_delivery_delayed:
        try:
            staff_users = User.query.filter(or_(User.is_admin == True, User.role == 'staff')).all()
            for su in staff_users:
                db.session.add(Notification(
                    user_id=su.id,
                    title='Delivery delayed',
                    message=f'Booking #{booking.id} is delayed by {booking.delivery_delay_minutes} minute(s).',
                    category='warning',
                ))
            db.session.commit()
        except Exception:
            db.session.rollback()

    flash('Delivery marked as received. Thank you!', 'success')
    return redirect(url_for('dashboard'))


@app.route('/booking/<int:booking_id>/mark-returned', methods=['POST'])
def booking_mark_returned_by_user(booking_id):
    """User confirms that they already returned the vehicle to the branch."""
    if 'username' not in session:
        return redirect(url_for('index'))
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))

    booking = Booking.query.get_or_404(booking_id)
    if booking.user_id != current_user.id:
        abort(404)

    if booking.payment_status != 'paid':
        flash('This booking is not eligible for status updates yet.', 'error')
        return redirect(url_for('dashboard'))

    if not booking.delivery_received:
        flash('Please mark the delivery as received first.', 'error')
        return redirect(url_for('dashboard'))

    if booking.delivery_returned:
        flash('This booking was already marked as returned by admin.', 'error')
        return redirect(url_for('dashboard'))

    if booking.delivery_returned_by_user:
        flash('You already marked this booking as returned.', 'info')
        return redirect(url_for('dashboard'))

    if not booking.rental_end:
        flash('Return time is not available for this booking yet.', 'error')
        return redirect(url_for('dashboard'))

    now = datetime.utcnow()
    if booking.rental_end > now:
        flash('Return time is not yet satisfied. Please try again later.', 'error')
        return redirect(url_for('dashboard'))

    booking.delivery_returned_by_user = True
    booking.delivery_returned_by_user_at = now
    booking.booking_status = 'completed'

    _record_audit(
        current_user.id,
        'booking_delivery_returned_by_user',
        'booking',
        booking.id,
        str(booking.transportation_id),
    )

    # Notify staff/admins.
    try:
        staff_users = User.query.filter(or_(User.is_admin == True, User.role == 'staff')).all()
        for su in staff_users:
            db.session.add(Notification(
                user_id=su.id,
                title='Customer return reported',
                message=f'Customer "{current_user.username}" reported booking #{booking.id} as returned.',
                category='info',
            ))
        db.session.commit()
    except Exception:
        db.session.rollback()

    db.session.commit()

    flash('Returned confirmed. Thank you!', 'success')
    return redirect(url_for('dashboard'))


@app.route('/booking/<int:booking_id>/set-status', methods=['POST'])
def booking_set_status(booking_id):
    """User can only cancel (restricted).

    Users should not set booking to active/completed manually; instead they
    confirm delivery using the "mark received" flow.
    """
    if 'username' not in session:
        return redirect(url_for('index'))
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))

    booking = Booking.query.get_or_404(booking_id)
    if booking.user_id != current_user.id:
        abort(404)
    if booking.payment_status != 'paid':
        flash('This booking is not eligible for status updates yet.', 'error')
        return redirect(url_for('dashboard'))

    requested = request.form.get('booking_status', '')
    desired = _normalize_booking_status_for_manual(requested)
    if desired not in {'cancelled'}:
        flash('Invalid manual status selection.', 'error')
        return redirect(url_for('dashboard'))

    booking.status_mode = 'manual'
    booking.booking_status = 'cancelled'
    # Cancelling should clear workflow flags.
    booking.delivery_received = False
    booking.delivery_received_at = None
    booking.delivery_returned = False
    booking.delivery_returned_at = None
    booking.delivery_returned_by_user = False
    booking.delivery_returned_by_user_at = None

    _record_audit(current_user.id, 'booking_status_manual_set', 'booking', booking.id, f'{desired}')
    db.session.commit()

    flash('Booking cancelled.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/admin/booking/<int:booking_id>/mark-delayed', methods=['POST'])
def admin_booking_mark_delayed(booking_id):
    if 'username' not in session:
        return redirect(url_for('index'))
    actor = User.query.filter_by(username=session['username']).first()
    if not actor:
        session.pop('username', None)
        return redirect(url_for('index'))
    require_staff_or_admin(actor)

    booking = Booking.query.get_or_404(booking_id)
    if booking.booking_status != 'active':
        flash('Delay can only be set while booking is In-Delivery.', 'error')
        return redirect(url_for('dashboard'))
    reason = (request.form.get('delay_reason', '') or '').strip()[:500]
    if not booking.expected_delivery_at:
        booking.expected_delivery_at = booking.rental_start or booking.created_at
    actual_basis = booking.actual_delivery_at or datetime.utcnow()
    _apply_delivery_delay_state(
        booking,
        actual_time=actual_basis,
        reason=reason or 'Manually marked delayed by staff/admin.',
        reporter_id=actor.id,
    )
    if not booking.is_delivery_delayed:
        booking.is_delivery_delayed = True
        booking.delivery_delay_minutes = max(booking.delivery_delay_minutes or 0, 1)
    _record_audit(actor.id, 'booking_delivery_mark_delayed', 'booking', booking.id, reason or 'manual')
    db.session.commit()

    try:
        _notify_user(
            booking.user_id,
            'Delivery delayed',
            f'Your booking #{booking.id} was marked delayed. {reason or "Please check dashboard for updates."}',
            'warning',
        )
        db.session.commit()
    except Exception:
        db.session.rollback()

    flash('Booking marked as delayed.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/admin/booking/<int:booking_id>/clear-delay', methods=['POST'])
def admin_booking_clear_delay(booking_id):
    if 'username' not in session:
        return redirect(url_for('index'))
    actor = User.query.filter_by(username=session['username']).first()
    if not actor:
        session.pop('username', None)
        return redirect(url_for('index'))
    require_staff_or_admin(actor)

    booking = Booking.query.get_or_404(booking_id)
    if booking.booking_status != 'active':
        flash('Delay can only be cleared while booking is In-Delivery.', 'error')
        return redirect(url_for('dashboard'))
    _clear_delivery_delay_state(booking)
    _record_audit(actor.id, 'booking_delivery_delay_cleared', 'booking', booking.id, '')
    db.session.commit()

    try:
        _notify_user(
            booking.user_id,
            'Delivery delay cleared',
            f'Your booking #{booking.id} is no longer marked as delayed.',
            'info',
        )
        db.session.commit()
    except Exception:
        db.session.rollback()

    flash('Delay flag cleared.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/admin/booking/<int:booking_id>/mark-received', methods=['POST'])
def admin_booking_mark_received(booking_id):
    """Admin confirms the returned vehicle (finalizes booking as completed)."""
    if 'username' not in session:
        return redirect(url_for('index'))
    actor = User.query.filter_by(username=session['username']).first()
    if not actor:
        session.pop('username', None)
        return redirect(url_for('index'))
    require_staff_or_admin(actor)

    booking = Booking.query.get_or_404(booking_id)
    _update_booking_delivery_workflow(
        booking,
        actor,
        # Admin confirming "returned" should NOT toggle the user's "delivery_received" report.
        user_received=None,
        admin_returned=True,
        set_completed=True,
        audit_action='admin_booking_delivery_returned',
        audit_details=str(booking.transportation_id),
        notify_owner=True,
        notify_owner_title='Booking returned',
        notify_owner_message=f'Your rental for booking #{booking.id} has been marked as returned by admin.',
    )

    # Connect the action with the booking owner (user).
    # (Handled by _update_booking_delivery_workflow)

    flash('Delivery marked as received (admin).', 'success')
    return redirect(url_for('dashboard'))


@app.route('/admin/booking/<int:booking_id>/set-status', methods=['POST'])
def admin_booking_set_status(booking_id):
    """Admin status updates (restricted).

    Admin cannot set booking to active/completed manually and cannot modify
    delivery_received. Admin should only finalize via "mark returned".
    """
    if 'username' not in session:
        return redirect(url_for('index'))
    actor = User.query.filter_by(username=session['username']).first()
    if not actor:
        session.pop('username', None)
        return redirect(url_for('index'))
    require_staff_or_admin(actor)

    booking = Booking.query.get_or_404(booking_id)

    requested = request.form.get('booking_status', '')
    desired = _normalize_booking_status_for_manual(requested)
    if not desired:
        flash('Invalid booking status.', 'error')
        return redirect(url_for('dashboard'))

    if desired in {'active', 'completed'}:
        flash('Admin cannot set booking status to active/completed manually. Use "mark returned".', 'error')
        return redirect(url_for('dashboard'))

    if desired != 'cancelled':
        flash('Invalid admin status update. Only "cancel" is allowed here.', 'error')
        return redirect(url_for('dashboard'))

    booking.status_mode = 'manual'
    booking.booking_status = 'cancelled'
    booking.delivery_received = False
    booking.delivery_received_at = None
    booking.delivery_returned = False
    booking.delivery_returned_at = None

    _record_audit(actor.id, 'admin_booking_status_manual_set', 'booking', booking.id, f'{desired}')
    db.session.commit()

    # Notify booking owner.
    try:
        _notify_user(
            booking.user_id,
            title='Booking status updated',
            message=f'Your rental for booking #{booking.id} was updated to "{desired}".',
            category='info',
        )
        db.session.commit()
    except Exception:
        db.session.rollback()

    flash('Booking status updated (admin).', 'success')
    return redirect(url_for('dashboard'))


@app.route('/change_password', methods=['POST'])
def change_password():
    if 'username' not in session:
        return redirect(url_for('index'))
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))
    
    current_password = request.form.get('current_password', '')
    new_password = request.form.get('new_password', '')
    if not current_user.check_password(current_password):
        return redirect(url_for('dashboard'))
    if not new_password:
        return redirect(url_for('dashboard'))
    current_user.set_password(new_password)
    db.session.commit()
    flash('Password updated.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/account/profile', methods=['POST'])
def update_profile():
    if 'username' not in session:
        return redirect(url_for('index'))
    user = User.query.filter_by(username=session['username']).first()
    if not user:
        session.pop('username', None)
        return redirect(url_for('index'))

    user.full_name = request.form.get('full_name', '').strip() or None
    user.email = request.form.get('email', '').strip() or None
    reg = request.form.get('region', user.region or 'PH').strip().upper()[:2]
    user.region = reg if reg in ('PH', 'US') else 'PH'

    bd_raw = request.form.get('birthdate', '').strip()
    if bd_raw:
        bd = _parse_iso_date(bd_raw)
        if not bd:
            flash('Invalid birth date.', 'error')
            return redirect(url_for('dashboard'))
        if _age_years(bd) < MIN_RENTAL_AGE:
            flash(f'You must be at least {MIN_RENTAL_AGE} years old to rent a vehicle.', 'error')
            return redirect(url_for('dashboard'))
        user.birthdate = bd

    if 'avatar' in request.files:
        file = request.files['avatar']
        if file and file.filename and allowed_file(file.filename):
            ext = secure_filename(file.filename).rsplit('.', 1)[1].lower()
            fname = f"uploads/profiles/user_{user.id}.{ext}"
            path = os.path.join(basedir, 'static', fname.replace('/', os.sep))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            file.save(path)
            # Flask serves static files from `static_folder='.'`, so the filename must include `static/...`.
            user.profile_image = f"static/{fname}".replace('\\', '/')

    db.session.commit()
    flash('Profile updated.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/account/payment-methods', methods=['POST'])
def update_saved_payment_methods():
    if 'username' not in session:
        return redirect(url_for('index'))
    user = User.query.filter_by(username=session['username']).first()
    if not user:
        return redirect(url_for('index'))

    methods = []
    for i in range(1, 4):
        t = request.form.get(f'pm_type_{i}', '').strip().lower()
        note = request.form.get(f'pm_note_{i}', '').strip()
        if t in ('gcash', 'paymaya', 'card', 'paypal', 'bank') and note:
            methods.append({'type': t, 'note': note})
    user.saved_payment_json = json.dumps(methods) if methods else None
    db.session.commit()
    flash('Saved payment methods updated.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/booking/<int:booking_id>/track')
def booking_track(booking_id):
    if 'username' not in session:
        return redirect(url_for('index'))
    _sync_booking_lifecycle_states()
    booking = Booking.query.get_or_404(booking_id)
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))
    if booking.user_id != current_user.id and not current_user.is_staff():
        abort(404)
    if current_user.is_staff():
        damages = (
            VehicleDamage.query.filter_by(transportation_id=booking.transportation_id)
            .options(joinedload(VehicleDamage.reporter))
            .order_by(VehicleDamage.created_at.desc())
            .all()
        )
    else:
        damages = (
            VehicleDamage.query.join(
                VehicleDamageBookingLink,
                VehicleDamageBookingLink.vehicle_damage_id == VehicleDamage.id,
            )
            .filter(VehicleDamageBookingLink.booking_id == booking.id)
            .options(joinedload(VehicleDamage.reporter))
            .order_by(VehicleDamage.created_at.desc())
            .all()
        )
    show_damage_history = len(damages) > 0
    return render_template(
        'BookingTrack.html',
        booking=booking,
        user=current_user,
        damages=damages,
        show_damage_history=show_damage_history,
    )


@app.route('/staff/damage/add', methods=['POST'])
def staff_add_damage():
    if 'username' not in session:
        return redirect(url_for('index'))
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        return redirect(url_for('index'))
    require_staff_or_admin(current_user)

    tid = request.form.get('transportation_id', type=int)
    area = request.form.get('area_on_vehicle', '').strip()
    detail = request.form.get('damage_description', '').strip()
    severity = request.form.get('severity', 'moderate').strip().lower()
    repair_estimate = request.form.get('repair_estimate_usd', type=float)
    is_resolved = bool(request.form.get('is_resolved'))
    damage_status = request.form.get('damage_status', 'under_review').strip().lower()

    bid = request.form.get('booking_select', type=int)
    if not bid:
        bid = request.form.get('booking_id_manual', type=int)

    ru = request.form.get('responsible_user_id', type=int)
    rm = request.form.get('responsible_party_manual', '').strip()
    party = None
    if ru:
        acc = User.query.get(ru)
        if acc:
            label = acc.full_name or acc.username
            party = f'User account: {acc.username}' + (f' ({label})' if label != acc.username else '')
        elif rm:
            party = rm
    elif rm:
        party = rm

    if not tid or not area or not detail:
        flash('Vehicle, area, and damage description are required.', 'error')
        return redirect(url_for('dashboard'))
    if severity not in ('minor', 'moderate', 'major', 'critical'):
        severity = 'moderate'
    if damage_status not in DAMAGE_WORKFLOW_STATUSES:
        damage_status = 'under_review'
    if repair_estimate is not None and repair_estimate < 0:
        flash('Repair estimate cannot be negative.', 'error')
        return redirect(url_for('dashboard'))

    if bid:
        bok = Booking.query.get(bid)
        if not bok:
            flash('Booking ID not found.', 'error')
            return redirect(url_for('dashboard'))
        if bok.transportation_id != tid:
            flash('Selected booking does not match the vehicle you chose.', 'error')
            return redirect(url_for('dashboard'))

    d = VehicleDamage(
        transportation_id=tid,
        booking_id=bid if bid else None,
        area_on_vehicle=area,
        damage_description=detail,
        responsible_party=party,
        severity=severity,
        repair_estimate_usd=repair_estimate,
        is_resolved=is_resolved,
        damage_status=damage_status,
        reported_by_id=current_user.id,
    )
    db.session.add(d)
    db.session.flush()
    if bid and not VehicleDamageBookingLink.query.filter_by(vehicle_damage_id=d.id).first():
        db.session.add(
            VehicleDamageBookingLink(booking_id=bid, vehicle_damage_id=d.id),
        )
    _record_audit(current_user.id, 'vehicle_damage_logged', 'vehicle_damage', d.id, area)
    db.session.commit()
    flash('Damage record saved.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/staff/damage/<int:damage_id>/status', methods=['POST'])
def staff_update_damage_status(damage_id):
    if 'username' not in session:
        return redirect(url_for('index'))
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        return redirect(url_for('index'))
    require_staff_or_admin(current_user)

    damage = VehicleDamage.query.get_or_404(damage_id)
    new_status = request.form.get('damage_status', '').strip().lower()
    resolution_status = request.form.get('resolution_status', '').strip().lower()
    if new_status not in DAMAGE_WORKFLOW_STATUSES:
        flash('Please select a valid damage status.', 'error')
        return redirect(url_for('damage_reports'))
    if resolution_status not in ('open', 'resolved'):
        flash('Please select a valid resolution status.', 'error')
        return redirect(url_for('damage_reports'))

    old_status = _normalize_damage_workflow_status(damage.damage_status, damage.is_resolved)
    old_resolution = 'resolved' if damage.is_resolved else 'open'
    damage.damage_status = _normalize_damage_workflow_status(new_status, damage.is_resolved)
    damage.is_resolved = (resolution_status == 'resolved')

    _record_audit(
        current_user.id,
        'vehicle_damage_status_updated',
        'vehicle_damage',
        damage.id,
        f'workflow: {old_status} -> {damage.damage_status}; resolution: {old_resolution} -> {resolution_status}',
    )
    db.session.commit()
    flash(
        f'Damage #{damage.id} updated: workflow {damage.damage_status.replace("_", " ")}, resolution {resolution_status}.',
        'success',
    )
    return redirect(url_for('damage_reports'))


@app.route('/damage-reports')
def damage_reports():
    if 'username' not in session:
        return redirect(url_for('loginpage'))
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))
    base_query = VehicleDamage.query.options(
        joinedload(VehicleDamage.vehicle),
        joinedload(VehicleDamage.reporter),
        joinedload(VehicleDamage.booking).joinedload(Booking.user),
    )

    # Staff/admin can review all damage logs across the fleet.
    if current_user.is_staff():
        damages = base_query.order_by(VehicleDamage.created_at.desc()).all()
    else:
        # Regular renters only see records linked to their own booking or user account label.
        damages = (
            base_query.filter(
                or_(
                    VehicleDamage.booking.has(Booking.user_id == current_user.id),
                    VehicleDamage.responsible_party.ilike(f'User account: {current_user.username}%'),
                ),
            )
            .order_by(VehicleDamage.created_at.desc())
            .all()
        )

    vehicle_damage_counts = {}
    for item in damages:
        vname = item.vehicle.name if item.vehicle else 'Unknown vehicle'
        item.workflow_status = _normalize_damage_workflow_status(item.damage_status, item.is_resolved)
        vehicle_damage_counts[vname] = vehicle_damage_counts.get(vname, 0) + 1
    vehicle_damage_counts = sorted(vehicle_damage_counts.items(), key=lambda kv: kv[0].lower())

    return render_template(
        'DamageReports.html',
        damages=damages,
        vehicle_damage_counts=vehicle_damage_counts,
        can_view_all_damages=current_user.is_staff(),
        damage_workflow_statuses=DAMAGE_WORKFLOW_STATUSES,
    )


def require_admin(user: User):
    if not user or not user.is_admin:
        abort(404)


def require_staff_or_admin(user: User):
    if not user or not user.is_staff():
        abort(404)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def generate_plate_number():
    """Generate a random plate number in format: ABC-1234"""
    letters = ''.join(random.choices(string.ascii_uppercase, k=3))
    numbers = ''.join(random.choices(string.digits, k=4))
    return f"{letters}-{numbers}"


@app.route('/admin/transportation/<int:transport_id>/edit', methods=['POST'])
def admin_edit_transportation(transport_id):
    if 'username' not in session:
        return redirect(url_for('index'))
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))
    require_staff_or_admin(current_user)
    transport = Transportation.query.get_or_404(transport_id)
    name = request.form.get('name', '').strip()
    price_raw = request.form.get('price', '').strip()
    region = request.form.get('region', (transport.region or 'PH')).strip().upper()[:2]
    description = request.form.get('description', '').strip() or None
    specs = request.form.get('specs', '').strip() or None
    seats = request.form.get('seats', type=int)
    fuel_type = request.form.get('fuel_type', '').strip().lower() or None
    transmission = request.form.get('transmission', '').strip().lower() or None
    image_url = request.form.get('image_url', '').strip() or None

    if region not in ('PH', 'US'):
        region = 'PH'
    if not name:
        flash('Vehicle name is required.', 'error')
        return redirect(url_for('dashboard'))
    if not price_raw:
        flash('Vehicle price is required.', 'error')
        return redirect(url_for('dashboard'))
    if seats is not None and (seats < 1 or seats > 60):
        flash('Seats must be between 1 and 60.', 'error')
        return redirect(url_for('dashboard'))
    if fuel_type and fuel_type not in ('gasoline', 'diesel', 'electric', 'hybrid'):
        flash('Choose a valid fuel type.', 'error')
        return redirect(url_for('dashboard'))
    if transmission and transmission not in ('automatic', 'manual'):
        flash('Choose a valid transmission.', 'error')
        return redirect(url_for('dashboard'))
    try:
        price = float(price_raw)
        if price <= 0:
            raise ValueError
    except ValueError:
        flash('Enter a valid price greater than zero.', 'error')
        return redirect(url_for('dashboard'))

    previous = {
        'name': transport.name or '',
        'price': f"{float(transport.price):.2f}",
        'region': transport.region or '',
        'seats': '' if transport.seats is None else str(transport.seats),
        'fuel_type': transport.fuel_type or '',
        'transmission': transport.transmission or '',
        'image_url': transport.image_url or '',
    }

    transport.name = name
    transport.price = price
    transport.region = region
    transport.description = description
    transport.specs = specs
    transport.seats = seats if seats else None
    transport.fuel_type = fuel_type
    transport.transmission = transmission
    if image_url:
        transport.image_url = image_url

    current = {
        'name': transport.name or '',
        'price': f"{float(transport.price):.2f}",
        'region': transport.region or '',
        'seats': '' if transport.seats is None else str(transport.seats),
        'fuel_type': transport.fuel_type or '',
        'transmission': transport.transmission or '',
        'image_url': transport.image_url or '',
    }
    changes = [f"{k}: {previous[k]} -> {current[k]}" for k in previous if previous[k] != current[k]]
    if changes:
        _record_audit(
            current_user.id,
            'fleet_vehicle_updated',
            'transportation',
            transport.id,
            '; '.join(changes)[:1900],
        )
    db.session.commit()
    flash(f'Updated vehicle details for {transport.name}.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/staff/transportation/add', methods=['POST'])
def staff_add_transportation():
    """Publish a new vehicle to the fleet (admin or staff)."""
    if 'username' not in session:
        return redirect(url_for('loginpage'))
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))
    require_staff_or_admin(current_user)

    name = request.form.get('name', '').strip()
    price_raw = request.form.get('price', '').strip()
    image_url = request.form.get('image_url', '').strip() or None
    description = request.form.get('description', '').strip() or None
    specs = request.form.get('specs', '').strip() or None
    seats = request.form.get('seats', type=int)
    fuel_type = request.form.get('fuel_type', '').strip().lower() or None
    transmission = request.form.get('transmission', '').strip().lower() or None
    region = request.form.get('region', 'PH').strip().upper()[:2]
    if region not in ('PH', 'US'):
        region = 'PH'

    image_local = None
    if 'vehicle_image' in request.files:
        vf = request.files['vehicle_image']
        if vf and vf.filename and allowed_file(vf.filename):
            ext = secure_filename(vf.filename).rsplit('.', 1)[1].lower()
            safe_name = f"uploads/vehicles/v_{secrets.token_hex(6)}.{ext}"
            path = os.path.join(basedir, 'static', safe_name.replace('/', os.sep))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            vf.save(path)
            image_local = safe_name.replace('\\', '/')

    if not name or not price_raw:
        flash('Vehicle name and daily price are required.', 'error')
        return redirect(url_for('dashboard'))
    if seats is not None and (seats < 1 or seats > 60):
        flash('Seats must be between 1 and 60.', 'error')
        return redirect(url_for('dashboard'))
    if fuel_type and fuel_type not in ('gasoline', 'diesel', 'electric', 'hybrid'):
        flash('Choose a valid fuel type.', 'error')
        return redirect(url_for('dashboard'))
    if transmission and transmission not in ('automatic', 'manual'):
        flash('Choose a valid transmission.', 'error')
        return redirect(url_for('dashboard'))
    try:
        price = float(price_raw)
        if price <= 0:
            raise ValueError
    except ValueError:
        flash('Enter a valid price greater than zero.', 'error')
        return redirect(url_for('dashboard'))

    if not image_local and not image_url:
        flash('Provide either an image file or an image URL.', 'error')
        return redirect(url_for('dashboard'))

    vehicle = Transportation(
        name=name,
        price=price,
        image_url=image_url if not image_local else None,
        image_local=image_local,
        description=description,
        specs=specs,
        region=region,
        seats=seats if seats else None,
        fuel_type=fuel_type,
        transmission=transmission,
    )
    db.session.add(vehicle)
    _record_audit(current_user.id, 'fleet_vehicle_added', 'transportation', None, f'{name} ({region})')
    db.session.commit()
    flash(f'Added "{name}" to the fleet ({region_label(region)}).', 'success')
    return redirect(url_for('dashboard'))


@app.route('/admin/transportation/<int:transport_id>/delete', methods=['POST'])
def admin_delete_transportation(transport_id):
    if 'username' not in session:
        return redirect(url_for('index'))
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))
    require_admin(current_user)
    transport = Transportation.query.get_or_404(transport_id)
    has_bookings = Booking.query.filter_by(transportation_id=transport.id).first() is not None
    has_requests = RentalRequest.query.filter_by(transportation_id=transport.id).first() is not None
    has_damages = VehicleDamage.query.filter_by(transportation_id=transport.id).first() is not None
    if has_bookings or has_requests or has_damages:
        flash('Cannot remove this vehicle because it already has booking/request/damage history.', 'error')
        return redirect(url_for('dashboard'))
    _record_audit(current_user.id, 'fleet_vehicle_deleted', 'transportation', transport.id, transport.name)
    db.session.delete(transport)
    db.session.commit()
    flash('Vehicle removed from fleet.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/admin/init-transportations')
def admin_init_transportations():
    """Manual route to initialize transportations (for admin use)"""
    if 'username' not in session:
        return redirect(url_for('index'))
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))
    if not current_user.is_admin:
        abort(404)
    
    initialize_default_transportations(force=True)
    flash('Transportations initialized successfully!', 'success')
    return redirect(url_for('transportations'))


@app.route('/admin/user/<int:user_id>/update_role', methods=['POST'])
def admin_update_user_role(user_id):
    if 'username' not in session:
        return redirect(url_for('index'))
    current_user = User.query.filter_by(username=session['username']).first()
    if not current_user:
        session.pop('username', None)
        return redirect(url_for('index'))
    require_admin(current_user)
    
    user = User.query.get_or_404(user_id)
    new_role = request.form.get('role', 'user').strip()
    
    # Validate role
    if new_role not in ['user', 'admin', 'staff']:
        flash('Invalid role selection.', 'error')
        return redirect(url_for('dashboard'))
    if user.is_admin and new_role != 'admin':
        admin_count = User.query.filter_by(is_admin=True).count()
        if admin_count <= 1:
            flash('At least one admin account must remain.', 'error')
            return redirect(url_for('dashboard'))
    
    # Update role and is_admin flag
    old_role = 'admin' if user.is_admin else user.role
    user.role = new_role
    user.is_admin = (new_role == 'admin')
    _record_audit(
        current_user.id,
        'user_role_updated',
        'user',
        user.id,
        f'{old_role} -> {new_role}',
    )
    db.session.commit()
    flash(f'Updated role for {user.username} to {new_role}.', 'success')
    return redirect(url_for('dashboard'))


def generate_unique_plate_number(max_attempts: int = 10):
    """Generate a unique plate number, retrying to avoid collisions."""
    for attempt in range(max_attempts):
        plate_number = generate_plate_number()
        existing = Booking.query.filter_by(plate_number=plate_number).first()
        if not existing:
            return plate_number
    # Fallback: append timestamp fragment to guarantee uniqueness
    timestamp_fragment = datetime.utcnow().strftime('%H%M%S')
    return f"{generate_plate_number()}-{timestamp_fragment}"


def initialize_default_transportations(force: bool = False):
    """
    Ensure the catalog of default transportations exists with correct pricing.

    By default, defaults are only inserted when the transportation table is empty,
    so deleting default vehicles won't cause them to re-appear on refresh.
    """
    default_transportations = [
        {'name': 'Economy Car', 'price': 25.00, 'region': 'PH', 'seats': 4, 'fuel_type': 'gasoline', 'transmission': 'automatic', 'image_url': 'https://images.unsplash.com/photo-1503376780353-7e6692767b70?w=400',
         'description': 'Ideal for Metro Manila trafficâ€”easy to park and fuel-efficient.',
         'specs': '4 seats Â· Gas Â· Auto Â· A/C'},
        {'name': 'Sedan', 'price': 35.00, 'region': 'PH', 'seats': 5, 'fuel_type': 'gasoline', 'transmission': 'automatic', 'image_url': 'https://images.unsplash.com/photo-1492144534655-ae79c964c9d7?w=400',
         'description': 'Comfortable business and family trips with generous trunk space.',
         'specs': '5 seats Â· Auto Â· Android Auto Â· Cruise control'},
        {'name': 'SUV', 'price': 50.00, 'region': 'PH', 'seats': 7, 'fuel_type': 'diesel', 'transmission': 'automatic', 'image_url': 'https://images.unsplash.com/photo-1549317661-bd32c8ce0db2?w=400',
         'description': 'High clearance for provincial roads and weekend getaways.',
         'specs': '7 seats Â· AWD option Â· Roof rails'},
        {'name': 'Luxury Car', 'price': 75.00, 'region': 'PH', 'seats': 5, 'fuel_type': 'gasoline', 'transmission': 'automatic', 'image_url': 'https://images.unsplash.com/photo-1618843479313-40f8afb4b4d8?w=400',
         'description': 'Premium interior and smooth ride for VIP transfers.',
         'specs': 'Leather Â· Premium audio Â· 5 seats'},
        {'name': 'Van', 'price': 60.00, 'region': 'US', 'seats': 8, 'fuel_type': 'diesel', 'transmission': 'automatic', 'image_url': 'https://images.unsplash.com/photo-1605559424843-9e4c228bf1c2?w=400',
         'description': 'Airport shuttles and group travel with room for luggage.',
         'specs': '8 seats Â· Sliding doors Â· USB ports'},
        {'name': 'Motorcycle', 'price': 15.00, 'region': 'US', 'seats': 2, 'fuel_type': 'gasoline', 'transmission': 'manual', 'image_url': 'https://images.unsplash.com/photo-1558981806-ec527fa84c39?w=400',
         'description': 'Quick urban hops with helmet included (where applicable).',
         'specs': '2-up Â· ABS Â· 300cc class'},
        {'name': 'Truck', 'price': 80.00, 'region': 'US', 'seats': 5, 'fuel_type': 'diesel', 'transmission': 'automatic', 'image_url': 'https://images.unsplash.com/photo-1558618047-3c8c76ca7d13?w=400',
         'description': 'Hauling and contractor jobs with bed liner.',
         'specs': 'Crew cab Â· 4WD Â· Tow package'},
        {'name': 'Bus', 'price': 100.00, 'region': 'US', 'seats': 30, 'fuel_type': 'diesel', 'transmission': 'automatic', 'image_url': 'https://images.unsplash.com/photo-1567899378494-47b22a2ae96a?w=400',
         'description': 'Large groups and eventsâ€”driver packages available on request.',
         'specs': '30+ seats Â· A/C Â· PA system'},
    ]
    
    seed_missing = force or Transportation.query.count() == 0

    try:
        existing_transportations = {t.name: t for t in Transportation.query.all()}
        added_count = 0
        updated_count = 0

        for transport_data in default_transportations:
            transport = existing_transportations.get(transport_data['name'])
            if not transport:
                if not seed_missing:
                    continue
                db.session.add(Transportation(
                    name=transport_data['name'],
                    price=transport_data['price'],
                    image_url=transport_data.get('image_url'),
                    description=transport_data.get('description'),
                    specs=transport_data.get('specs'),
                    region=transport_data.get('region', 'PH'),
                    seats=transport_data.get('seats'),
                    fuel_type=transport_data.get('fuel_type'),
                    transmission=transport_data.get('transmission'),
                ))
                added_count += 1
                continue

            updated = False
            if abs(transport.price - transport_data['price']) > 1e-6:
                transport.price = transport_data['price']
                updated = True
            if transport_data.get('image_url') and transport.image_url != transport_data['image_url']:
                transport.image_url = transport_data['image_url']
                updated = True
            if getattr(transport, 'description', None) is None and transport_data.get('description'):
                transport.description = transport_data['description']
                updated = True
            if getattr(transport, 'specs', None) is None and transport_data.get('specs'):
                transport.specs = transport_data['specs']
                updated = True
            if getattr(transport, 'region', None) and transport.region != transport_data.get('region'):
                transport.region = transport_data.get('region', 'PH')
                updated = True
            elif getattr(transport, 'region', None) is None:
                transport.region = transport_data.get('region', 'PH')
                updated = True
            if getattr(transport, 'seats', None) is None and transport_data.get('seats'):
                transport.seats = transport_data['seats']
                updated = True
            if getattr(transport, 'fuel_type', None) is None and transport_data.get('fuel_type'):
                transport.fuel_type = transport_data['fuel_type']
                updated = True
            if getattr(transport, 'transmission', None) is None and transport_data.get('transmission'):
                transport.transmission = transport_data['transmission']
                updated = True
            if updated:
                updated_count += 1

        if added_count > 0 or updated_count > 0:
            db.session.commit()
            print(f"Transportations synchronized (added: {added_count}, updated: {updated_count})")
    except Exception as e:
        print(f"Error initializing transportations: {e}")
        import traceback
        traceback.print_exc()
        db.session.rollback()


def migrate_legacy_rental_id_images_to_db():
    """Copy legacy static-file ID paths into rental_id_image rows (run inside app context)."""
    try:
        insp = sql_inspect(db.engine)
        tables = insp.get_table_names()
        if 'rental_id_image' not in tables or 'rental_request' not in tables:
            return
        todo = RentalRequest.query.filter(
            RentalRequest.id_image_local.isnot(None),
            RentalRequest.id_image_local != '',
        ).all()
        migrated = 0
        unlink_paths = []
        for rr in todo:
            if RentalIdImage.query.filter_by(rental_request_id=rr.id).first():
                continue
            rel = (rr.id_image_local or '').replace('\\', '/').strip().lstrip('/')
            if not rel:
                continue
            path = os.path.normpath(os.path.join(basedir, 'static', rel.replace('/', os.sep)))
            static_root = os.path.normpath(os.path.join(basedir, 'static'))
            if not path.startswith(static_root) or not os.path.isfile(path):
                continue
            with open(path, 'rb') as f:
                data = f.read()
            if not data or len(data) > MAX_RENTAL_ID_IMAGE_BYTES:
                continue
            ext = path.rsplit('.', 1)[-1].lower() if '.' in path else 'jpeg'
            mime = _mime_for_id_extension(ext)
            db.session.add(
                RentalIdImage(
                    rental_request_id=rr.id,
                    mime_type=mime,
                    image_data=data,
                    byte_size=len(data),
                )
            )
            rr.id_image_local = None
            unlink_paths.append(path)
            migrated += 1
        if migrated:
            db.session.commit()
            for p in unlink_paths:
                try:
                    os.remove(p)
                except OSError:
                    pass
            print(f"Migrated {migrated} legacy rental ID image(s) into rental_id_image table.")
    except Exception as e:
        db.session.rollback()
        print(f"Note: legacy rental ID migration: {e}")


def migrate_vehicle_damage_booking_links():
    """Backfill vehicle_damage_booking_link from existing vehicle_damage.booking_id (run inside app context)."""
    try:
        insp = sql_inspect(db.engine)
        tables = insp.get_table_names()
        if 'vehicle_damage_booking_link' not in tables or 'vehicle_damage' not in tables:
            return
        added = 0
        for vd in VehicleDamage.query.filter(VehicleDamage.booking_id.isnot(None)).all():
            if VehicleDamageBookingLink.query.filter_by(vehicle_damage_id=vd.id).first():
                continue
            db.session.add(
                VehicleDamageBookingLink(booking_id=vd.booking_id, vehicle_damage_id=vd.id),
            )
            added += 1
        if added:
            db.session.commit()
            print(f"Added {added} vehicle_damage_booking_link row(s) for existing damage records.")
    except Exception as e:
        db.session.rollback()
        print(f"Note: vehicle_damage_booking_link migration: {e}")


# Initialize database when app starts
def init_db():
    """Initialize the database and create all tables"""
    with app.app_context():
        try:
            ensure_tables()
            migrate_vehicle_damage_booking_links()
            migrate_legacy_rental_id_images_to_db()
            # Update existing users without role field
            try:
                all_users = User.query.all()
                users_updated = 0
                for user in all_users:
                    if not hasattr(user, 'role') or user.role is None or user.role == '':
                        if user.is_admin:
                            user.role = 'admin'
                        else:
                            user.role = 'user'
                        users_updated += 1
                if users_updated > 0:
                    db.session.commit()
                    print(f"Updated {users_updated} users with role field")
            except Exception as e:
                # Role column might not exist yet, that's okay
                print(f"Note: {e}")
            
            # Initialize default transportations
            initialize_default_transportations()
            
            print(f"Database initialized at: {app.config['SQLALCHEMY_DATABASE_URI']}")
        except Exception as e:
            print(f"Error initializing database: {e}")

# Initialize database on startup
init_db()

if __name__ == '__main__':
    app.run(debug=True)
