"""
Flask Calendar Web App
Simple calendar view with month navigation
"""

from flask import (Flask, Response, jsonify, render_template, request,
                   redirect, url_for, session, flash, abort)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, current_user, login_user,
                         logout_user, login_required)
from flask_migrate import Migrate
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect
import click
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta, date, timezone
from calendar import monthrange, month_name
import hashlib
import logging
import os
import re
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)

# --- Security configuration --------------------------------------------------
# SECRET_KEY is read from the environment. It is NEVER generated randomly on
# startup (that invalidates all sessions on every restart) and is never logged.
# Production: a missing or weak SECRET_KEY aborts startup. Development: a
# fixed, clearly-marked fallback key is used so local sessions survive
# restarts.
DEV_FALLBACK_SECRET_KEY = 'dev-only-insecure-secret-key-change-me'
_PRODUCTION_ENVS = {'production', 'prod'}
# Production keys must be at least this long (32 URL-safe chars, as produced
# by secrets.token_urlsafe(32), is the recommended minimum).
PRODUCTION_SECRET_KEY_MIN_LENGTH = 32


def _is_production() -> bool:
    return os.environ.get('FLASK_ENV', '').strip().lower() in _PRODUCTION_ENVS


def _resolve_secret_key() -> str:
    key = os.environ.get('SECRET_KEY', '').strip()
    if _is_production():
        if not key:
            raise RuntimeError(
                'SECRET_KEY environment variable is required in production. '
                'Generate one with: '
                'python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        if (len(key) < PRODUCTION_SECRET_KEY_MIN_LENGTH
                or key == DEV_FALLBACK_SECRET_KEY):
            raise RuntimeError(
                'SECRET_KEY is not strong enough for production: it must be '
                f'at least {PRODUCTION_SECRET_KEY_MIN_LENGTH} characters and '
                'must not be the development fallback key. Generate one with: '
                'python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        return key
    if not key:
        logger.warning(
            'SECRET_KEY not set - using the insecure development fallback '
            'key. Set SECRET_KEY before deploying.'
        )
        key = DEV_FALLBACK_SECRET_KEY
    return key


def _validate_production_environment() -> None:
    """Fail-safe production startup gate (CMP-005B).

    Production must never boot with a silently degraded security posture.
    Every problem found is collected and reported in ONE error so an
    operator fixes the whole configuration, not one gap at a time:

    * REDIS_URL       - required; rate limiting must run on shared Redis
                        storage, never per-process in-memory, in production.
    * SMTP_HOST / SMTP_USER / SMTP_PASSWORD
                      - required; account emails must be deliverable.
    * APP_BASE_URL    - must be https://; emailed links must use HTTPS.
    * DATABASE_URL    - required; must be a PostgreSQL URL. An unset
                        value would silently fall back to a local SQLite
                        file, and on Render the service filesystem is
                        ephemeral, so that data would be discarded on
                        every deploy (CMP-005C).

    (SECRET_KEY presence/strength is checked first, in _resolve_secret_key.)
    Development is intentionally lax: none of these are required.
    """
    problems = []

    redis_url = os.environ.get('REDIS_URL', '').strip()
    if not redis_url:
        problems.append(
            'REDIS_URL is required in production (a redis:// or rediss:// '
            'URL); rate limiting must never run on per-process in-memory '
            'storage in production'
        )
    elif not redis_url.lower().startswith(('redis://', 'rediss://')):
        problems.append('REDIS_URL must start with redis:// or rediss://')

    for var in ('SMTP_HOST', 'SMTP_USER', 'SMTP_PASSWORD'):
        if not os.environ.get(var, '').strip():
            problems.append(f'{var} is required in production')

    if not os.environ.get('APP_BASE_URL', '').strip().lower().startswith('https://'):
        problems.append(
            'APP_BASE_URL must be an https:// URL in production (emailed '
            'verification and reset links must use HTTPS)'
        )

    # DATABASE_URL: production must be pointed at PostgreSQL by an
    # explicit URL. Accept the plain schemes (rewritten to the psycopg
    # dialect below) and the SQLAlchemy psycopg URL directly.
    db_url = os.environ.get('DATABASE_URL', '').strip()
    if not db_url:
        problems.append(
            'DATABASE_URL is required in production (a PostgreSQL URL, '
            'e.g. postgres://user:pass@host:5432/dbname); an unset '
            'value would silently fall back to a local SQLite file, '
            'which a Render service discards on every deploy'
        )
    elif not db_url.lower().startswith(('postgres://', 'postgresql://',
                                        'postgresql+psycopg://')):
        problems.append(
            'DATABASE_URL must be a PostgreSQL URL in production '
            '(postgres://, postgresql:// or postgresql+psycopg://); '
            'SQLite is a development fallback only'
        )

    if problems:
        raise RuntimeError(
            'Production configuration is incomplete - refusing to start:\n  - '
            + '\n  - '.join(problems)
        )


app = Flask(__name__)
app.secret_key = _resolve_secret_key()
# Debug mode is OFF by default, so the Werkzeug debugger never runs. Only the
# local dev server may enable it via FLASK_DEBUG=1; gunicorn does not run the
# Werkzeug dev server, so the debugger cannot start under it.
app.debug = os.environ.get('FLASK_DEBUG', '').strip().lower() in {'1', 'true', 'yes'}
app.config['DEBUG'] = app.debug
# CSRF protection for every state-changing request (browser forms AND
# cookie-authenticated API writes). Tokens are session-bound; forms embed
# {{ csrf_token() }} and JSON API clients send the X-CSRFToken header.
csrf = CSRFProtect(app)
app.config['WTF_CSRF_TIME_LIMIT'] = 3600  # tokens valid for 1 hour

# Use Render's Postgres when DATABASE_URL is set; fall back to local SQLite
_db_url = os.environ.get('DATABASE_URL', 'sqlite:///calendar.db')
if _db_url.startswith('postgres://'):
    _db_url = 'postgresql+psycopg://' + _db_url[len('postgres://'):]
elif _db_url.startswith('postgresql://'):
    _db_url = 'postgresql+psycopg://' + _db_url[len('postgresql://'):]
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_CHANGES'] = False

# --- Session security ------------------------------------------------------
# Session policy (CMP-005B), two tiers:
#   * default sign-in (no "remember me"): BROWSER-CLOSE session. The session
#     cookie carries no expiry (no Max-Age/Expires), so the browser
#     discards it on close. session.permanent stays False.
#   * "remember me": PERSISTENT session. The login route sets
#     session.permanent = True, applying PERMANENT_SESSION_LIFETIME
#     (12 hours) and giving the cookie Max-Age=43200.
# Cookies are HttpOnly (invisible to JS), SameSite=Lax (CSRF mitigation for
# top-level navigation), and Secure in production (HTTPS only); Secure is
# off in development so local plain-HTTP testing keeps working. Flask-Login
# 'basic' session protection forces re-login if the request IP or user
# agent changes mid-session (a hijack signal) without the 'strong' mode
# false positives on mobile networks.
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = _is_production()
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)
app.config['SESSION_PROTECTION'] = 'basic'
# HTTPS-aware production: absolute URLs (login "next" handling, token links
# rendered client-side) resolve to https when production, http otherwise.
# APP_BASE_URL (env) still governs emailed token links.
app.config['PREFERRED_URL_SCHEME'] = 'https' if _is_production() else 'http'
# Fail-safe production gate: refuse to boot with missing/unsafe production
# settings (SECRET_KEY strength was already checked in _resolve_secret_key).
if _is_production():
    _validate_production_environment()

# --- Mail / account token configuration (all environment-driven) -----------
# SMTP delivery is fully configured via environment variables; no
# credentials are hardcoded, and SMTP_USER/SMTP_PASSWORD are never logged.
# When SMTP_HOST is unset, the dev-only outbox below captures messages in
# memory so local flows are testable; the outbox is NEVER used when
# SMTP_HOST is set, and in production it is never used at all (production
# additionally REQUIRES SMTP_HOST/SMTP_USER/SMTP_PASSWORD - see
# _validate_production_environment). A delivery failure in production logs a
# warning (no token details) and the request continues.
app.config['SMTP_HOST'] = os.environ.get('SMTP_HOST', '').strip()
try:
    app.config['SMTP_PORT'] = int(os.environ.get('SMTP_PORT', '587') or 587)
except ValueError:
    raise RuntimeError('SMTP_PORT must be an integer (e.g. 587)')
app.config['SMTP_USER'] = os.environ.get('SMTP_USER', '').strip()
app.config['SMTP_PASSWORD'] = os.environ.get('SMTP_PASSWORD', '')
app.config['SMTP_FROM'] = os.environ.get(
    'SMTP_FROM', 'CalendarMealPlanner <no-reply@calendarmealplanner.com>')
app.config['SMTP_USE_TLS'] = os.environ.get('SMTP_USE_TLS', 'true').strip().lower() in {'1', 'true', 'yes'}
# Base URL used to build verification / reset links in emails.
app.config['APP_BASE_URL'] = os.environ.get('APP_BASE_URL', 'http://127.0.0.1:5000').rstrip('/')
# Time-to-live for the single-use account tokens, in minutes.
app.config['VERIFICATION_TTL_MINUTES'] = int(os.environ.get('VERIFICATION_TTL_MINUTES', '60') or 60)
app.config['PASSWORD_RESET_TTL_MINUTES'] = int(os.environ.get('PASSWORD_RESET_TTL_MINUTES', '30') or 30)
app.config['PASSWORD_MIN_LENGTH'] = 8
# Dev-only capture of emails "sent" while SMTP is unconfigured. NEVER
# populated in production (see send_email()).
dev_outbox = []

# The real registration/authentication routes are defined below, after the
# User model and Flask-Login wiring.

db = SQLAlchemy(app)
migrate = Migrate(app, db)


def normalize_email(email) -> str:
    """Normalize an email address for storage and lookup.

    Strips surrounding whitespace and lowercases, so ' User@Example.COM '
    and 'user@example.com' resolve to the same account.
    """
    return (email or '').strip().lower()


class User(db.Model, UserMixin):
    """Public multi-user account.

    - ``email`` is unique and always stored in normalized form.
    - ``password_hash`` holds a salted Werkzeug password hash (scrypt by
      default); plaintext passwords are never stored.
    - ``created_at`` is the account creation timestamp (DB-side default).
    - ``email_verified`` / ``status`` track verification and lifecycle.
      Valid ``status`` values: 'active', 'pending_verification', 'disabled'.
    """
    __tablename__ = 'user'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False,
                           server_default=db.func.now())
    email_verified = db.Column(db.Boolean, nullable=False,
                               server_default=db.false())
    status = db.Column(db.String(20), nullable=False,
                       server_default='active', default='active')
    # Time-limited, single-use account tokens. Only SHA-256 hashes of the raw
    # tokens are stored, so a database leak does not expose usable tokens.
    # ``expires_at`` is naive UTC (see _utcnow).
    verification_token_hash = db.Column(db.String(64), nullable=True)
    verification_token_expires_at = db.Column(db.DateTime, nullable=True)
    password_reset_token_hash = db.Column(db.String(64), nullable=True)
    password_reset_token_expires_at = db.Column(db.DateTime, nullable=True)

    def __init__(self, email=None, **kwargs):
        if email is not None:
            email = normalize_email(email)
        super().__init__(email=email, **kwargs)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def new_verification_token(self) -> str:
        """Generate a fresh single-use verification token for this account."""
        token = secrets.token_urlsafe(32)
        self.verification_token_hash = _hash_token(token)
        self.verification_token_expires_at = (
            _utcnow() + timedelta(minutes=app.config['VERIFICATION_TTL_MINUTES'])
        )
        return token

    def new_password_reset_token(self) -> str:
        """Generate a fresh single-use password reset token for this account."""
        token = secrets.token_urlsafe(32)
        self.password_reset_token_hash = _hash_token(token)
        self.password_reset_token_expires_at = (
            _utcnow() + timedelta(minutes=app.config['PASSWORD_RESET_TTL_MINUTES'])
        )
        return token


# Calendar Model
class Recipe(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    image = db.Column(db.String(200))
    date = db.Column(db.Date)
    description = db.Column(db.Text)
    # Nullable ownership: NULL means a legacy/shared recipe that belongs to
    # no account. Legacy rows must NEVER be assigned to an arbitrary user.
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    user = db.relationship('User')


# Flask-Login wiring (foundation; endpoint rework lands in CMP-004)
login_manager = LoginManager(app)
login_manager.login_view = 'login'


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# --- Recipe ownership helpers ------------------------------------------------
# Every recipe query that returns data to a client goes through these
# helpers, so scoping is decided in exactly one place:
#
#   * authenticated user  -> only rows with user_id == current user
#   * guest               -> a query that can never match
#   * legacy rows (user_id IS NULL) -> invisible to every account and to
#     guests; nothing in the app ever assigns them to a user
#
# Lookups by id combine the id filter with the ownership filter in a single
# query, so a request for a recipe that exists but belongs to someone else
# is indistinguishable from a non-existent id (404, no existence leak).

def owned_recipe_query():
    """``Recipe`` query scoped to the current user's recipes (empty for guests)."""
    if current_user.is_authenticated:
        return Recipe.query.filter(Recipe.user_id == current_user.id)
    return Recipe.query.filter(db.false())


def get_owned_recipe_or_404(recipe_id) -> Recipe:
    """Fetch ``recipe_id`` only if it belongs to the current user.

    Aborts with 404 for guests, unknown ids, legacy (unowned) rows and rows
    owned by another user - all four cases produce the identical response.
    """
    if not current_user.is_authenticated:
        abort(404)
    recipe = db.session.execute(
        db.select(Recipe).where(
            Recipe.id == recipe_id,
            Recipe.user_id == current_user.id,
        )).scalar_one_or_none()
    if recipe is None:
        abort(404)
    return recipe


def _safe_next_url(value):
    """Only same-site relative paths are acceptable redirect targets."""
    if not value or not value.startswith('/') or value.startswith('//'):
        return None
    return value


# --- Token / email helpers --------------------------------------------------
# Raw tokens are never stored and never logged. The database holds only
# SHA-256 hashes; logs carry at most the recipient address and a note.

def _utcnow() -> datetime:
    """Current UTC time as a naive datetime (SQLite/PostgreSQL friendly)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


EMAIL_PATTERN = re.compile(r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$')


def _valid_email(email: str) -> bool:
    """Basic format + length check for user-supplied email addresses."""
    return bool(email) and len(email) <= 255 and bool(EMAIL_PATTERN.match(email))


# Pre-computed dummy hash used to equalize response timing on password-reset
# requests for email addresses with no account (anti-enumeration).
DUMMY_PASSWORD_HASH = generate_password_hash('dummy-timing-equalizer-not-a-password')


def send_email(recipient: str, subject: str, body: str) -> bool:
    """Send a plain-text email via the configured SMTP server.

    Falls back to the in-memory dev outbox when SMTP is unconfigured in
    non-production mode. In production without SMTP it logs a warning
    (recipient only - never the body or any token) and returns False.
    """
    host = app.config['SMTP_HOST']
    if not host:
        if _is_production():
            logger.warning(
                'SMTP not configured in production - email to %s was NOT sent',
                recipient,
            )
            return False
        dev_outbox.append({'to': recipient, 'subject': subject, 'body': body})
        logger.info('SMTP not configured - captured email to %s in dev outbox', recipient)
        return True

    from email.message import EmailMessage
    from smtplib import SMTP
    msg = EmailMessage()
    msg['From'] = app.config['SMTP_FROM']
    msg['To'] = recipient
    msg['Subject'] = subject
    msg.set_content(body)
    try:
        with SMTP(host, app.config['SMTP_PORT'], timeout=20) as smtp:
            if app.config['SMTP_USE_TLS']:
                smtp.starttls()
            if app.config['SMTP_USER']:
                smtp.login(app.config['SMTP_USER'], app.config['SMTP_PASSWORD'])
            smtp.send_message(msg)
        return True
    except Exception:
        # The exception text contains SMTP/server details, never the body or
        # tokens, so logging it is safe.
        logger.exception('failed to send email to %s', recipient)
        return False


def _issue_verification_email(user):
    """(Re)issue a verification token and email its link to the user.

    Returns the raw token (used only to build the link / dev-mode flash),
    or None when nothing was issued (already-verified account). The raw
    token is never persisted and never logged.
    """
    if user.email_verified:
        return None
    token = user.new_verification_token()
    db.session.commit()
    link = f"{app.config['APP_BASE_URL']}{url_for('verify_email', token=token)}"
    send_email(
        user.email,
        'Verify your email address - CalendarMealPlanner',
        'Hello,\n\n'
        'Please verify your email address by opening this link:\n\n'
        f'{link}\n\n'
        f'The link is single-use and expires in '
        f"{app.config['VERIFICATION_TTL_MINUTES']} minutes.\n\n"
        'If you did not create a CalendarMealPlanner account, you can ignore '
        'this email.\n',
    )
    return token


def _issue_reset_email(user) -> None:
    """Issue a password reset token and email its link to the user."""
    token = user.new_password_reset_token()
    db.session.commit()
    link = f"{app.config['APP_BASE_URL']}{url_for('reset_password_complete', token=token)}"
    send_email(
        user.email,
        'Reset your password - CalendarMealPlanner',
        'Hello,\n\n'
        'We received a request to reset the password for your account. '
        'Open this link to choose a new password:\n\n'
        f'{link}\n\n'
        f'The link is single-use and expires in '
        f"{app.config['PASSWORD_RESET_TTL_MINUTES']} minutes.\n\n"
        'If you did not request this, you can ignore this email - your '
        'password will remain unchanged.\n',
    )


# --- Rate limiting -----------------------------------------------------------
# Per-IP limits on the credential/account endpoints. The in-memory store is
# per process; behind multiple workers each worker enforces its own copy
# (limits therefore scale roughly with worker count - see README).
def _rate_limit_handler(request_limit):
    """Friendly 429 response (Flask-Limiter ``on_breach`` callback).

    One plain, non-enumerating message for every throttled endpoint.
    """
    return Response(
        'Too many attempts. Please try again later.',
        status=429,
        mimetype='text/plain; charset=utf-8',
    )


# Storage (CMP-005B):
#   * production: REDIS_URL from the environment (required at startup by
#     _validate_production_environment). Every gunicorn worker shares the
#     same Redis-backed counters, so the limits hold across workers.
#     In-memory storage is NEVER used in production.
#   * development/tests: an isolated per-process in-memory store, so local
#     work and the test suite never need (or touch) a Redis server.
#
# Redis outage behaviour (production): the limiter runs with
# in_memory_fallback_enabled=False and swallow_errors=False. If Redis is
# unreachable, requests to a rate-limited endpoint FAIL CLOSED with a 500
# error instead of silently degrading to per-worker in-memory counters
# (which would multiply every limit by the worker count). Unlimited
# endpoints (calendar, day view, search) are unaffected. The storage client
# connects lazily, so an unreachable Redis at boot does not block startup -
# the fail-closed behaviour applies from the first rate-limited request.
# storage_options protocol=2 keeps the client on RESP2, which every Redis
# server understands (newer redis-py versions default to a HELLO/RESP3
# handshake that very old servers reject).
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=os.environ.get('REDIS_URL', '').strip()
    if _is_production() else 'memory://',
    storage_options={'protocol': 2},
    default_limits=[],
    headers_enabled=True,
    in_memory_fallback_enabled=False,
    swallow_errors=False,
    on_breach=_rate_limit_handler,
)
limiter.init_app(app)


# Identical user-facing message for every failed login, regardless of which
# check failed (unknown email / wrong password / disabled / unverified in
# production). This is deliberate: it must not reveal account existence.
INVALID_CREDENTIALS_MSG = 'Invalid email or password.'
RESET_GENERIC_MSG = ('If that email address has an account with an active '
                     'status, a password reset link has been sent.')
RESEND_GENERIC_MSG = ('If that email address has an account that still needs '
                      'verification, a new verification link has been sent.')


# --- Registration / authentication routes -----------------------------------

@app.route('/signup', methods=['GET', 'POST'])
# methods=['POST']: only submission attempts count; rendering the form
# (GET) must never consume rate-limit budget or throttle page loads.
@limiter.limit('3 per hour', methods=['POST'])
def signup():
    """Public account registration.

    Creates the account in an unverified state and emails a single-use,
    time-limited verification link. Production never activates an account
    without a successful email verification.
    """
    if request.method == 'POST':
        email = normalize_email(request.form.get('email'))
        password = request.form.get('password', '')

        if not _valid_email(email):
            flash('Please enter a valid email address.', 'error')
            return redirect(url_for('signup'))
        if len(password) < app.config['PASSWORD_MIN_LENGTH']:
            flash(f'Password must be at least {app.config["PASSWORD_MIN_LENGTH"]} characters.', 'error')
            return redirect(url_for('signup'))
        if len(password) > 128:
            flash('Password is too long (maximum 128 characters).', 'error')
            return redirect(url_for('signup'))

        existing = db.session.execute(
            db.select(User).where(User.email == email)).scalar_one_or_none()
        if existing:
            flash('An account with that email address already exists.', 'error')
            return redirect(url_for('signup'))

        user = User(email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        verification_token = _issue_verification_email(user)

        if (not app.config['SMTP_HOST'] and not _is_production()
                and verification_token):
            # Development convenience: with no SMTP configured, surface the
            # link in the response so local development can complete the
            # flow. This branch is unreachable in production.
            link = (f"{app.config['APP_BASE_URL']}"
                    f"{url_for('verify_email', token=verification_token)}")
            flash(f'Account created (development mode, no SMTP configured). '
                  f'Your verification link: {link}', 'info')
            flash('Your account was created.', 'success')
        else:
            flash('Your account was created. Check your email for a '
                  'verification link before signing in.', 'success')
        return redirect(url_for('login'))

    return render_template('signup.html')


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit('5 per 15 minutes', methods=['POST'])
def login():
    """Authenticate a user. All failures produce one generic message."""
    if request.method == 'POST':
        email = normalize_email(request.form.get('email'))
        password = request.form.get('password', '')

        user = None
        if _valid_email(email):
            user = db.session.execute(
                db.select(User).where(User.email == email)).scalar_one_or_none()

        if user is not None and user.check_password(password):
            if user.status == 'disabled':
                # Do not reveal that the account exists / is disabled.
                flash(INVALID_CREDENTIALS_MSG, 'error')
                return redirect(url_for('login'))
            if _is_production() and not user.email_verified:
                # Production requires verified email. Silently re-issue the
                # verification email; the visible message is identical to a
                # failed login.
                _issue_verification_email(user)
                flash(INVALID_CREDENTIALS_MSG, 'error')
                return redirect(url_for('login'))
            remember = bool(request.form.get('remember'))
            login_user(user, remember=remember)
            if remember:
                # Session policy: remember-me sessions are persistent with a
                # 12-hour cookie lifetime (PERMANENT_SESSION_LIFETIME). Set
                # explicitly so the policy does not rely on Flask-Login
                # internals alone.
                session.permanent = True
            # Return the user to the page they came from (e.g. a day view
            # they tried to write before signing in). Only same-site
            # relative paths are accepted.
            next_url = _safe_next_url(request.form.get('next')
                                      or request.args.get('next'))
            if next_url:
                return redirect(next_url)
            return redirect(url_for('calendar'))

        flash(INVALID_CREDENTIALS_MSG, 'error')
        return redirect(url_for('login'))

    return render_template('login.html', email='')


@app.route('/logout', methods=['GET', 'POST'])
def logout():
    """End the current session.

    logout_user() drops the identity; session.clear() additionally removes
    any remaining session state (e.g. the CSRF token) so the re-issued
    cookie carries nothing that identifies the old session.
    """
    logout_user()
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))


@app.route('/resend-verification', methods=['POST'])
@limiter.limit('3 per hour', methods=['POST'])
def resend_verification():
    """Re-send a verification link. Generic response, no account enumeration."""
    email = normalize_email(request.form.get('email'))
    if _valid_email(email):
        user = db.session.execute(
            db.select(User).where(User.email == email)).scalar_one_or_none()
        if user is not None and user.status != 'disabled' and not user.email_verified:
            _issue_verification_email(user)
        else:
            # Equalize timing with the token-issuing path.
            check_password_hash(DUMMY_PASSWORD_HASH, 'timing-equalizer')
    flash(RESEND_GENERIC_MSG, 'success')
    return redirect(url_for('login'))


@app.route('/verify-email/<token>')
def verify_email(token):
    """Consume a single-use verification link."""
    user = db.session.execute(
        db.select(User).where(
            User.verification_token_hash == _hash_token(token),
            User.verification_token_expires_at > _utcnow(),
        )).scalar_one_or_none()

    if user is None:
        # One message for invalid / expired / already-used links.
        return render_template('verify_result.html',
                               title='Verification link problem',
                               ok=False,
                               message='This verification link is invalid or has expired.',
                               back_url=url_for('login'),
                               back_label='Back to sign in')

    user.email_verified = True
    user.verification_token_hash = None
    user.verification_token_expires_at = None
    db.session.commit()
    return render_template('verify_result.html',
                           title='Email verified',
                           ok=True,
                           message='Your email address has been verified. You can now sign in.',
                           back_url=url_for('login'),
                           back_label='Sign in')


@app.route('/reset-password', methods=['GET', 'POST'])
@limiter.limit('3 per hour', methods=['POST'])
def reset_password_request():
    """Request a password reset email.

    The response is identical whether or not the email has an account, and
    timing is equalized for unknown addresses.
    """
    if request.method == 'POST':
        email = normalize_email(request.form.get('email'))
        user = None
        if _valid_email(email):
            user = db.session.execute(
                db.select(User).where(User.email == email)).scalar_one_or_none()
        if user is not None and user.status != 'disabled':
            _issue_reset_email(user)
        else:
            # Equalize timing with the token-issuing path.
            check_password_hash(DUMMY_PASSWORD_HASH, 'timing-equalizer')
        flash(RESET_GENERIC_MSG, 'success')
        return redirect(url_for('reset_password_request'))
    return render_template('reset_request.html')


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password_complete(token):
    """Consume a single-use reset link and set a new password."""
    user = db.session.execute(
        db.select(User).where(
            User.password_reset_token_hash == _hash_token(token),
            User.password_reset_token_expires_at > _utcnow(),
        )).scalar_one_or_none()

    if user is None:
        # One message for invalid / expired / already-used links.
        return render_template('verify_result.html',
                               title='Reset link problem',
                               ok=False,
                               message='This password reset link is invalid or has expired. '
                                       'Request a new one to continue.',
                               back_url=url_for('reset_password_request'),
                               back_label='Request a new reset link')

    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm = request.form.get('confirm', '')
        if len(password) < app.config['PASSWORD_MIN_LENGTH']:
            flash(f'Password must be at least {app.config["PASSWORD_MIN_LENGTH"]} characters.', 'error')
            return render_template('reset_complete.html', token=token)
        if len(password) > 128:
            flash('Password is too long (maximum 128 characters).', 'error')
            return render_template('reset_complete.html', token=token)
        if password != confirm:
            flash('Passwords do not match.', 'error')
            return render_template('reset_complete.html', token=token)

        user.set_password(password)
        user.password_reset_token_hash = None
        user.password_reset_token_expires_at = None
        db.session.commit()
        # Invalidate any existing session for this user; require fresh login.
        logout_user()
        flash('Your password has been updated. Please sign in with your new password.', 'success')
        return redirect(url_for('login'))

    return render_template('reset_complete.html', token=token)


# Initialize the database.
#
# Development: db.create_all() keeps a fresh local database usable
# without running migrations first (existing tables are never altered;
# migration 0003 makes an Alembic-migrated database converge to the
# same shape, so both paths agree).
#
# Production (CMP-005C): NO create_all() and NO database contact at
# import time. The production schema is owned by the Alembic chain and
# is applied by the explicit pre-start step (`flask db upgrade` - see
# README "Render deployment"). Running create_all() here would silently
# bypass migrations: on a fresh database it would create a schema with
# no alembic_version row, which the readiness checks below correctly
# refuse to serve. An unreachable or unmigrated database must surface
# as a failed health check / 503, never as a silent fallback or a
# divergent schema.
if _is_production():
    logger.info(
        'production startup: schema is migration-managed - run '
        '"flask db upgrade" before starting the service')
else:
    try:
        with app.app_context():
            db.create_all()
            logger.info('local database initialized')
    except Exception as _db_exc:  # don't block boot if DB is briefly unavailable
        logger.warning('could not initialize local database: %s', _db_exc)

# Sample recipes (can be removed or customized)  # Disabled for now
# sample_recipes = [
#     {
#         'title': 'Sample Recipe 1',
#         'image': 'https://picsum.photos/300/200?random=1',
#         'date': '2026-11-05',
#         'description': 'A sample recipe description for November 5th, 2026.'
#     },
#     {
#         'title': 'Sample Recipe 2',
#         'image': 'https://picsum.photos/300/200?random=2',
#         'date': '2026-11-10',
#         'description': 'A sample recipe description for November 10th, 2026.'
#     }
# ]

# with app.app_context():
#     if sample_recipes:
#         from datetime import datetime
#         for recipe_data in sample_recipes:
#             new_recipe = Recipe(
#                 title=recipe_data['title'],
#                 image=recipe_data['image'],
#                 date=datetime.strptime(recipe_data['date'], '%Y-%m-%d').date(),
#                 description=recipe_data.get('description', '')
#             )
#             db.session.add(new_recipe)
#         db.session.commit()
#         print(f"Added {len(sample_recipes)} sample recipes to database!")

# Create sample users for testing (with unique passwords)  # Disabled for now
# with app.app_context():
#     test_users = [
#         User(email='test@example.com', password='password123'),
#         User(email='admin@example.com', password='admin123')
#     ]
#     existing_emails = [User.query.filter_by(email=u.email).first() for u in test_users]
#     for user, existing in zip(test_users, existing_emails):
#         if not existing:
#             db.session.add(user)
#     db.session.commit()
#     print("Created test users for demo!")

# NOTE: the former unauthenticated GET /clean-samples web endpoint (which
# deleted any recipe whose title contained 'Sample') was removed in
# CMP-003C. The equivalent local maintenance task is the CLI command
# ``flask clean-samples`` defined at the bottom of this file - it refuses to
# run in production and requires explicit confirmation.

# Calendar View - main landing page
@app.route('/')
def calendar():
    """
    Display calendar for the current month.
    Supports month/year navigation via query parameters.
    """
    # Serve the calendar directly. (Was: redirect(url_for('calendar')),
    # which resolves to this same route -> infinite redirect loop.)
    return calendar_view()


# Calendar View - main route
@app.route('/calendar')
def calendar_view():
    """
    Display calendar for the current month.
    Supports month/year navigation via query parameters.
    """
    # Get month and year from query params, default to current
    month = request.args.get('month')
    year = request.args.get('year')

    if month is None or year is None:
        now = datetime.now()
        month_i, year_i = now.month, now.year
    else:
        try:
            month_i, year_i = int(month), int(year)
        except ValueError:
            now = datetime.now()
            month_i, year_i = now.month, now.year
    if not (1 <= month_i <= 12 and 1 <= year_i <= 9999):
        now = datetime.now()
        month_i, year_i = now.month, now.year
    month = str(month_i).zfill(2)
    year = str(year_i)

    # Days in the displayed month - monthrange handles leap years (Feb 2024 = 29)
    days_in_month = monthrange(year_i, month_i)[1]

    # First weekday of the month (0=Monday, 6=Sunday) and the adjacent months
    start_day = date(year_i, month_i, 1).weekday()
    if month_i == 1:
        prev_month_i, prev_year_i = 12, year_i - 1
    else:
        prev_month_i, prev_year_i = month_i - 1, year_i
    if month_i == 12:
        next_month_i, next_year_i = 1, year_i + 1
    else:
        next_month_i, next_year_i = month_i + 1, year_i
    prev_month_days = monthrange(prev_year_i, prev_month_i)[1]

    # Generate calendar grid
    calendar_days = []
    for i in range(42):  # 6 weeks max
        day_num = i - start_day + 1
        if day_num <= 0:
            # Leading cells: real dates from the previous month (e.g. 30, 31)
            d = prev_month_days + day_num  # day_num is negative
            calendar_days.append({
                'day': d,
                'day_str': str(d).zfill(2),
                'is_current_month': False,
                'day_class': 'prev-month'
            })
        elif day_num > days_in_month:
            # Trailing cells: real dates from the next month (1, 2, 3, ...)
            d = day_num - days_in_month
            calendar_days.append({
                'day': d,
                'day_str': str(d).zfill(2),
                'is_current_month': False,
                'day_class': 'next-month'
            })
        else:
            # Current month - always a valid current-month date
            day_date = date(year_i, month_i, day_num)
            # Scoped to the signed-in user; guests see an empty calendar and
            # legacy (unowned) rows are invisible to everyone.
            recipes = owned_recipe_query().filter_by(date=day_date).all()
            # Serialize recipes for template
            calendar_days.append({
                'day': day_num,
                'day_str': str(day_num).zfill(2),
                'is_current_month': True,
                'day_class': 'current-month',
                'recipes': [
                    {
                        'id': r.id,
                        'title': r.title,
                        'image': r.image,
                        'description': r.description
                    }
                    for r in recipes
                ]
            })

    # Navigation URLs (year boundaries wrap correctly)
    prev_month_url = (f'?month=12&year={year_i - 1}' if month_i == 1
                      else f'?month={str(month_i - 1).zfill(2)}&year={year_i}')
    next_month_url = (f'?month=01&year={year_i + 1}' if month_i == 12
                      else f'?month={str(month_i + 1).zfill(2)}&year={year_i}')

    # Month name for display
    month_name_display = month_name[month_i]
    
    return render_template('calendar.html',
                         days=calendar_days,
                         month=month,
                         year=year,
                         prev_month_url=prev_month_url,
                         next_month_url=next_month_url,
                         month_name=month_name_display,
                         days_in_month=days_in_month,
                         user_email=(current_user.email if current_user.is_authenticated
                    else 'Guest'))

# Recipe API endpoints
# Authentication and ownership are enforced on EVERY endpoint:
#   * guests get 401 (JSON) - no listing, no reads, no writes
#   * authenticated users see only rows where user_id == their own id
#   * id lookups for another user's recipe, a legacy (unowned) recipe or a
#     non-existent id all produce the identical 404
@app.route('/api/recipes', methods=['GET'])
def get_recipes():
    """Get the current user's recipes for a specific date (401 for guests)."""
    if not current_user.is_authenticated:
        return {'error': 'authentication required'}, 401
    date = request.args.get('date')
    if date:
        try:
            date_obj = datetime.strptime(date, '%Y-%m-%d').date()
        except ValueError:
            return {'date': date, 'recipes': []}
        recipes = owned_recipe_query().filter_by(date=date_obj).all()
        return {
            'date': date,
            'recipes': [{'id': r.id, 'title': r.title, 'image': r.image, 'description': r.description} for r in recipes]
        }
    return {'recipes': []}

@app.route('/api/recipes', methods=['POST'])
def create_recipe():
    """Create a recipe from JSON or form data; the new row belongs to the
    signed-in user (401 for guests)."""
    if not current_user.is_authenticated:
        return {'error': 'authentication required'}, 401
    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = request.form.to_dict()
    title = (data.get('title') or '').strip()
    date_str = (data.get('date') or '').strip()
    if not title or not date_str:
        return {'error': 'title and date (YYYY-MM-DD) are required'}, 400
    try:
        recipe_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return {'error': 'date must be formatted YYYY-MM-DD'}, 400
    recipe = Recipe(
        title=title[:100],
        image=(data.get('image') or '').strip() or None,
        date=recipe_date,
        description=(data.get('description') or '').strip(),
        user_id=current_user.id,
    )
    db.session.add(recipe)
    db.session.commit()
    return {'id': recipe.id, 'title': recipe.title, 'date': str(recipe_date),
            'owner': current_user.email}, 201

@app.route('/api/recipes/<int:id>', methods=['GET', 'PUT', 'DELETE'])
def recipe_api(id):
    """CRUD API for the current user's recipes.

    Ownership is verified before any read or write; someone else's recipe id
    is indistinguishable from a missing id (404). Guests get 401.
    """
    if not current_user.is_authenticated:
        return {'error': 'authentication required'}, 401
    recipe = get_owned_recipe_or_404(id)

    if request.method == 'GET':
        return {
            'id': recipe.id,
            'title': recipe.title,
            'image': recipe.image,
            'description': recipe.description,
            'date': str(recipe.date)
        }

    elif request.method == 'PUT':
        data = request.get_json(silent=True) or {}
        recipe.title = data.get('title', recipe.title)
        recipe.image = data.get('image', recipe.image)
        recipe.description = data.get('description', recipe.description)
        if data.get('date'):
            try:
                recipe.date = datetime.strptime(data['date'], '%Y-%m-%d').date()
            except ValueError:
                return {'error': 'date must be formatted YYYY-MM-DD'}, 400
        db.session.commit()
        return {'id': recipe.id, 'title': recipe.title, 'image': recipe.image, 'description': recipe.description, 'date': str(recipe.date)}

    elif request.method == 'DELETE':
        db.session.delete(recipe)
        db.session.commit()
        return {'id': recipe.id, 'title': recipe.title, 'deleted': True}

    return {'error': 'method not allowed'}, 405

# Day View - show all recipes for a specific day
@app.route('/day/<int:day_num>', methods=['GET', 'POST'])
def day_view(day_num):
    """Show detailed view for a specific day"""
    month = request.args.get('month')
    year = request.args.get('year')
    search_query = request.args.get('search', '').strip()  # Capture search query

    # Resolve month/year FIRST (default to current month), then compute length
    if month is None or year is None:
        now = datetime.now()
        month_i, year_i = now.month, now.year
    else:
        try:
            month_i, year_i = int(month), int(year)
        except ValueError:
            now = datetime.now()
            month_i, year_i = now.month, now.year
    if not (1 <= month_i <= 12 and 1 <= year_i <= 9999):
        now = datetime.now()
        month_i, year_i = now.month, now.year
    month = str(month_i).zfill(2)
    year = str(year_i)

    # Days in this month (correct for leap years and short months)
    days_in_month = monthrange(year_i, month_i)[1]

    # Clamp the requested day into the month so dates and navigation agree
    day_num = max(1, min(day_num, days_in_month))

    # Calculate the date
    day_date = date(year_i, month_i, day_num)
    day_date_str = str(day_date)
    
    # Handle recipe submission (form POST from the day page).
    # Creating a recipe requires an authenticated user; guests are sent to
    # the login page and come back to this day afterwards (next=).
    if request.method == 'POST':
        if not current_user.is_authenticated:
            flash('Please sign in to add recipes.', 'info')
            return redirect(url_for('login', next=request.full_path))
        title = (request.form.get('title') or '').strip()
        if not title:
            flash('Title is required', 'error')
        else:
            recipe = Recipe(
                title=title[:100],
                image=(request.form.get('image') or '').strip() or None,
                date=day_date,
                description=(request.form.get('description') or '').strip(),
                user_id=current_user.id,
            )
            db.session.add(recipe)
            db.session.commit()
            flash(f'Added "{title}"', 'success')
        return redirect(f'/day/{day_num}?month={month}&year={year}')

    # Get recipes for this day, scoped to the signed-in user (guests and
    # other users' recipes are not listed; legacy rows are invisible).
    _rows = owned_recipe_query().filter_by(date=day_date).all()
    recipes = [
        {'id': r.id, 'title': r.title, 'image': r.image, 'description': r.description}
        for r in _rows
    ]
    
    # Get navigation links
    month_name_display = month_name[month_i]

    if month_i == 1:
        prev_month_i, prev_year_i = 12, year_i - 1
    else:
        prev_month_i, prev_year_i = month_i - 1, year_i
    if month_i == 12:
        next_month_i, next_year_i = 1, year_i + 1
    else:
        next_month_i, next_year_i = month_i + 1, year_i
    prev_month_days = monthrange(prev_year_i, prev_month_i)[1]

    # Prev/next day stay in the same month; only wrap across month boundaries
    if day_num > 1:
        prev_day_url = f'/day/{day_num - 1}?month={month}&year={year}'
    else:
        prev_day_url = (f'/day/{prev_month_days}?month={str(prev_month_i).zfill(2)}'
                        f'&year={prev_year_i}')
    if day_num < days_in_month:
        next_day_url = f'/day/{day_num + 1}?month={month}&year={year}'
    else:
        next_day_url = f'/day/1?month={str(next_month_i).zfill(2)}&year={next_year_i}'
    
    # Build back to calendar URL
    calendar_url = f'/calendar?month={month}&year={year}'
    
    return render_template('day_view.html',
                         day_num=day_num,
                         month=month,
                         year=year,
                         day_date=day_date_str,
                         day_date_display=day_date.strftime('%A, %B %d, %Y'),
                         recipes=recipes,
                         prev_day_url=prev_day_url,
                         next_day_url=next_day_url,
                         month_name_display=month_name_display,
                         year_display=day_date.strftime('%Y'),
                         days_in_month=days_in_month,
                         search_query=search_query,
                         calendar_url=calendar_url)

# Recipe API endpoints

# Search Routes - Search across three recipe websites
import re as _re
import requests
from urllib.parse import quote_plus

SEARCH_SOURCES = [
    {
        'key': 'allrecipes',
        'name': 'AllRecipes',
        'url_template': 'https://www.allrecipes.com/search?q={}',
        'scrape': False,  # JS-rendered page + bot protection: link-only
    },
    {
        'key': 'foodnetwork',
        'name': 'Food Network',
        'url_template': 'https://www.foodnetwork.com/search/{}',
        'scrape': False,  # blocks server requests: link-only
    },
    {
        'key': 'recipetineats',
        'name': 'Recipe Tin Eats',
        'url_template': 'https://www.recipetineats.com/search/{}',
        'scrape': True,
    },
]

_SEARCH_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}


def _extract_recipetineats(html_text):
    """Extract (title, url) pairs from a Recipe Tin Eats search results page."""
    pattern = _re.compile(
        r'<article[^>]*aria-label="([^"]+)"[^>]*>.*?'
        r'href="(https://www\.recipetineats\.com/[^"]+)"',
        _re.S,
    )
    seen = set()
    items = []
    for title, url in pattern.findall(html_text):
        if url in seen:
            continue
        seen.add(url)
        items.append({'title': title, 'url': url})
    return items


@app.route('/search')
def search():
    """Search recipes across multiple websites."""
    query = request.args.get('q', '').strip()
    source = request.args.get('source', '').strip()

    results = []

    if not query:
        # Show search form
        return render_template('search.html', query=query, source=source, results=results)

    encoded_query = quote_plus(query)

    if not source or source == 'all':
        selected = SEARCH_SOURCES
    else:
        selected = [s for s in SEARCH_SOURCES if s['key'] == source] or list(SEARCH_SOURCES)

    for src in selected:
        search_url = src['url_template'].format(encoded_query)
        items = []
        message = ''
        if src['scrape']:
            try:
                resp = requests.get(search_url, headers=_SEARCH_HEADERS, timeout=15)
                resp.raise_for_status()
                items = _extract_recipetineats(resp.text)[:10]
                if not items:
                    message = 'No recipes matched this query on this site.'
            except Exception as exc:
                message = 'Could not fetch results ({}). Use the link below to search directly.'.format(exc.__class__.__name__)
        else:
            message = 'This site blocks automated access, so use the link below to browse its results.'
        results.append({
            'source': src['name'],
            'url': search_url,
            'items': items,
            'message': message,
        })

    return render_template('search.html', query=query, source=source, results=results)



# --- Health checks & production readiness (CMP-005C) ------------------------
# /health       liveness: the process is up. Never touches the database.
# /health/ready readiness: safe to serve traffic. Development - database
#               reachable; production - database reachable AND schema at
#               the migration head.
#
# Render's HTTP health check points at /health/ready: a deploy whose
# instances never report ready is cancelled and the previous version
# keeps serving (zero-downtime guarantee). Failure bodies are coarse on
# purpose - no connection strings, no exception text, no schema details.

_MIGRATIONS_DIR = Path(__file__).resolve().parent / 'migrations'
_ALEMBIC_INI = Path(__file__).resolve().parent / 'alembic.ini'
_prod_readiness = {'verified': False}
_dev_db_ok = {'verified': False}


def _chain_head_revision():
    """Head revision of the committed migration chain."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option('script_location', str(_MIGRATIONS_DIR))
    return ScriptDirectory.from_config(cfg).get_current_head()


def _production_readiness():
    """(ok, reason) with reason in {'ok', 'unreachable', 'unmigrated'}.

    Production only. Verifies the database is reachable and that its
    alembic_version matches the committed migration chain head. Success
    is cached for the life of the process; failures are re-checked on
    the next probe so a recovered database clears the instance without
    a restart. Exceptions never reach the response body: only the
    exception TYPE name is logged server-side.
    """
    if _prod_readiness['verified']:
        return True, 'ok'
    uri = app.config['SQLALCHEMY_DATABASE_URI']
    engine_kwargs = {'pool_pre_ping': True, 'pool_recycle': 1800}
    if uri.startswith('postgresql'):
        engine_kwargs['connect_args'] = {'connect_timeout': 5}
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(uri, **engine_kwargs)
        try:
            with engine.connect() as conn:
                conn.execute(text('SELECT 1'))
                has_table = conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_name = 'alembic_version'"
                    )).first()
                if not has_table:
                    return False, 'unmigrated'
                row = conn.execute(
                    text('SELECT version_num FROM alembic_version')).first()
        finally:
            engine.dispose()
    except Exception as exc:
        logger.warning('production readiness: database unreachable: %s',
                       exc.__class__.__name__)
        return False, 'unreachable'
    try:
        head = _chain_head_revision()
    except Exception as exc:
        logger.warning('production readiness: cannot read migration chain: %s',
                       exc.__class__.__name__)
        return False, 'unmigrated'
    if row is not None and row[0] == head:
        _prod_readiness['verified'] = True
        logger.info('production readiness: database at migration head %s',
                    head)
        return True, 'ok'
    logger.warning('production readiness: schema is not at migration head %s',
                   head)
    return False, 'unmigrated'


def _dev_database_reachable():
    """(ok, reason) for development: the configured database answers."""
    if _dev_db_ok['verified']:
        return True, 'ok'
    uri = app.config['SQLALCHEMY_DATABASE_URI']
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(uri, pool_pre_ping=True, pool_recycle=1800)
        try:
            with engine.connect() as conn:
                conn.execute(text('SELECT 1'))
        finally:
            engine.dispose()
    except Exception as exc:
        logger.warning('development health: database unreachable: %s',
                       exc.__class__.__name__)
        return False, 'unreachable'
    _dev_db_ok['verified'] = True
    return True, 'ok'


def _unavailable_body(reason):
    body = {'status': 'unavailable'}
    if reason == 'unmigrated':
        body['schema'] = 'not ready'
    else:
        body['database'] = 'unavailable'
    return body


@app.route('/health', methods=['GET'])
def health_check():
    """Liveness probe: the process is up and answering.

    Never touches the database and never exposes configuration, so it
    stays a pure process-health signal (Render's default TCP probe and
    any operator liveness check can use it).
    """
    return jsonify({'status': 'ok'}), 200


@app.route('/health/ready', methods=['GET'])
def health_ready():
    """Readiness probe: is this instance safe to route traffic to?

    Development: database reachable.
    Production: database reachable AND schema at the migration head -
    a fresh or partially migrated database fails the check so Render
    cancels the deploy instead of routing traffic to it.
    """
    if _is_production():
        ok, reason = _production_readiness()
        if not ok:
            return jsonify(_unavailable_body(reason)), 503
        return jsonify({'status': 'ok', 'database': 'ok', 'schema': 'ok'}), 200
    ok, reason = _dev_database_reachable()
    if not ok:
        return jsonify(_unavailable_body(reason)), 503
    return jsonify({'status': 'ok', 'database': 'ok'}), 200


@app.before_request
def _enforce_production_readiness():
    """Backstop: production serves no application route until ready.

    Defense in depth behind the deploy-time checks: even an instance
    whose health checks are misconfigured or not yet enabled must not
    answer application traffic against a fresh or unmigrated database.
    The health endpoints themselves are exempt - they report the state.
    """
    if not _is_production():
        return None
    path = request.path
    if path == '/health' or path.startswith('/health/'):
        return None
    ok, reason = _production_readiness()
    if not ok:
        logger.warning('rejected %s %s: production not ready (%s)',
                       request.method, path, reason)
        return jsonify(_unavailable_body(reason)), 503
    return None


# --- Safe error handling -----------------------------------------------------
# Error responses never include stack traces, SQL, config or secrets. The
# server-side log carries the details (exception traceback for 5xx) so the
# user-facing page can stay generic. JSON for API paths, HTML elsewhere.

def _is_api_request() -> bool:
    return (request.path.startswith('/api/')
            or request.accept_mimetypes.best_match(['application/json', 'text/html'])
            == 'application/json')


@app.errorhandler(404)
def _not_found(error):
    if _is_api_request():
        return {'error': 'not found'}, 404
    return render_template('error_404.html'), 404


@app.errorhandler(405)
def _method_not_allowed(error):
    if _is_api_request():
        return {'error': 'method not allowed'}, 405
    return render_template('error_405.html'), 405


@app.errorhandler(500)
def _internal_error(error):
    logger.exception('unhandled error on %s %s', request.method, request.path)
    if _is_api_request():
        return {'error': 'internal server error'}, 500
    return render_template('error_500.html'), 500


# --- Local maintenance CLI ----------------------------------------------------
# The web endpoint /clean-samples was removed (unauthenticated data deletion
# over HTTP). This CLI command is the safe replacement: it runs only where an
# operator can execute ``flask`` (the deployment host or a developer machine),
# refuses to run in production, and requires explicit confirmation.
@app.cli.command('clean-samples')
@click.option('--yes', is_flag=True,
              help='Skip the interactive confirmation prompt.')
def clean_samples_command(yes):
    """Delete recipes whose title contains 'Sample' (local maintenance only).

    Refuses to run when FLASK_ENV is production.
    """
    if _is_production():
        click.echo('Refusing to run in production. '
                   'Use the normal delete flow per user instead.', err=True)
        raise SystemExit(2)
    samples = Recipe.query.filter(Recipe.title.contains('Sample')).all()
    if not samples:
        click.echo('No sample recipes found; nothing to do.')
        return
    if not yes:
        answer = click.prompt(f'Delete {len(samples)} sample recipe(s)?',
                              default='n')
        if answer.strip().lower() not in {'y', 'yes'}:
            click.echo('Aborted - nothing deleted.')
            return
    for sample in samples:
        db.session.delete(sample)
    db.session.commit()
    click.echo(f'Deleted {len(samples)} sample recipe(s).')


# Start the development server
if __name__ == '__main__':
    # Local development only: loopback bind (never LAN/public), debug OFF by
    # default. Enable the dev server debugger explicitly with FLASK_DEBUG=1.
    app.run(host=os.environ.get('FLASK_RUN_HOST', '127.0.0.1'),
            port=int(os.environ.get('PORT', 5000)),
            debug=app.debug)
