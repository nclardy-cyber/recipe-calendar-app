"""Pytest fixtures for the recipe calendar app.

Every test runs against a throwaway SQLite database in a temp directory.
The original db/calendar.db and instance/calendar.db are never touched.
"""
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


def _tmp_db_url(tmp_path: Path) -> str:
    db_path = tmp_path / 'test_calendar.db'
    return 'sqlite:///' + str(db_path).replace('\\', '/')


def reload_app():
    """Import/reload the app module with the current environment."""
    import app as app_module
    importlib.reload(app_module)
    return app_module


# ---------------------------------------------------------------------------
# Disposable PostgreSQL (embedded_postgres) for production-mode tests
# ---------------------------------------------------------------------------
# Production mode requires a real PostgreSQL URL (CMP-005C), so tests that
# exercise the production app against a database use a disposable embedded
# PostgreSQL server started once per test session: fresh data directory,
# 127.0.0.1 only, unique database names, dropped afterwards. No external
# or production database is ever touched. When the package is not
# installed the affected tests skip (the rest of the suite stays
# dependency-free).

try:
    from embedded_postgres import get_server as _get_pg_server
    _HAS_EMBEDDED_PG = True
except ImportError:
    _get_pg_server = None
    _HAS_EMBEDDED_PG = False


def _alembic_upgrade(database_url: str) -> None:
    """Run `flask db upgrade` (the documented pre-start procedure) against
    *database_url* in a subprocess with a production-safe environment.
    Raises on failure."""
    env = os.environ.copy()
    env.update({
        'DATABASE_URL': database_url,
        'SECRET_KEY': 'unit-test-prod-key-0123456789abcdef',
        'FLASK_ENV': 'production',
        'FLASK_DEBUG': '0',
        'REDIS_URL': 'redis://127.0.0.1:6390/9',  # never contacted
        'SMTP_HOST': '127.0.0.1',
        'SMTP_PORT': '1',
        'SMTP_USER': 'unit',
        'SMTP_PASSWORD': 'unit',
        'APP_BASE_URL': 'https://unit-test.example',
    })
    proc = subprocess.run(
        [sys.executable, '-m', 'flask', 'db', 'upgrade'],
        cwd=str(APP_DIR), env=env, capture_output=True, text=True,
        timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(
            f'flask db upgrade failed (exit {proc.returncode}):\n'
            f'{proc.stdout}\n{proc.stderr}')


@pytest.fixture(scope='session')
def pg_server():
    """A disposable embedded PostgreSQL server for the whole session.

    The server's data directory lives inside a fresh mkdtemp root. On
    teardown - and on the failure path where the server never started -
    the whole root is removed so no temporary data survives the run
    (CMP-006A).
    """
    if not _HAS_EMBEDDED_PG:
        pytest.skip('embedded_postgres is not installed '
                    '(pip install embedded_postgres)')
    assert _get_pg_server is not None  # guaranteed by _HAS_EMBEDDED_PG
    tmp_root = Path(tempfile.mkdtemp(prefix='cmp006a-pg-'))
    server = None
    try:
        # cleanup_mode='delete' is the embedded_postgres documented
        # pattern for temporary servers: the server stops itself and
        # removes its own data directory when closed.
        server = _get_pg_server(tmp_root / 'data', cleanup_mode='delete')
        yield server
    finally:
        if server is not None:
            try:
                server.cleanup()
            except Exception:
                pass
        # Belt and braces: remove the whole mkdtemp root so nothing
        # survives - including when the server failed to start (the data
        # directory may be half-initialised) or cleanup() already removed
        # it (rmtree of a missing path is a no-op with ignore_errors).
        shutil.rmtree(tmp_root, ignore_errors=True)


@pytest.fixture()
def pg_database_raw(pg_server):
    """A fresh, EMPTY database on the session server (no migrations).

    Yields its postgres:// URL; the database is dropped afterwards.
    """
    import psycopg
    name = f"cmp_{uuid.uuid4().hex[:12]}"
    admin = psycopg.connect(pg_server.get_uri('postgres'),
                            connect_timeout=15)
    admin.autocommit = True
    admin.execute(f'CREATE DATABASE {name}')
    admin.close()
    url = pg_server.get_uri(name)
    yield url
    try:
        admin = psycopg.connect(pg_server.get_uri('postgres'),
                                connect_timeout=15)
        admin.autocommit = True
        admin.execute(f'DROP DATABASE IF EXISTS {name}')
        admin.close()
    except Exception:
        pass


@pytest.fixture()
def pg_database(pg_database_raw):
    """A fresh, fully-migrated (head) database on the session server."""
    _alembic_upgrade(pg_database_raw)
    yield pg_database_raw


@pytest.fixture()
def pg_admin_url(request):
    """An admin PostgreSQL URL whose role can CREATE/DROP DATABASE.

    Uses the external ``CMP_PG_TEST_URL`` when set, otherwise the
    session's embedded server's admin database. This lets the PostgreSQL
    migration regression tests run against a fully disposable server with
    no external dependency, and skip gracefully when neither is available.
    """
    url = os.environ.get('CMP_PG_TEST_URL')
    if url:
        return url
    return request.getfixturevalue('pg_server').get_uri('postgres')


@pytest.fixture()
def app_module(tmp_path, monkeypatch):
    """Fresh app instance backed by a temporary SQLite database.

    Rate limiting is disabled here so individual auth/feature tests are not
    throttled; ``test_rate_limits.py`` re-enables it on a fresh app.
    """
    monkeypatch.setenv('DATABASE_URL', _tmp_db_url(tmp_path))
    monkeypatch.setenv('SECRET_KEY', 'unit-test-secret-key')
    # Explicit development opt-in (CMP-006A): an unset FLASK_ENV resolves
    # to PRODUCTION, which would refuse these dev-mode test loads.
    monkeypatch.setenv('FLASK_ENV', 'development')
    monkeypatch.delenv('FLASK_DEBUG', raising=False)
    mod = reload_app()
    # Flask-Limiter v4 reads `enabled` from the limiter instance (config is
    # only consulted at init_app time), so toggle the attribute directly.
    mod.limiter.enabled = False
    yield mod


@pytest.fixture()
def prod_app(pg_database, monkeypatch):
    """Fresh app instance in PRODUCTION mode against a disposable,
    fully-migrated PostgreSQL database (CMP-005C: production requires a
    real PostgreSQL URL and a migration-managed schema - no SQLite
    fallback, no create_all()).

    Supplies the full production environment (strong SECRET_KEY, REDIS_URL,
    SMTP credentials, HTTPS APP_BASE_URL) so the production startup gate
    passes. The limiter is disabled on this fixture, so REDIS_URL/SMTP are
    never actually contacted by the tests that use it. Skips when no
    disposable PostgreSQL is available.
    """
    monkeypatch.setenv('DATABASE_URL', pg_database)
    monkeypatch.setenv('SECRET_KEY', 'unit-test-production-secret-key-0123456789')
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.delenv('FLASK_DEBUG', raising=False)
    monkeypatch.setenv('REDIS_URL', 'redis://127.0.0.1:6390/9')
    monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
    # Port 1: nothing listens there, so any email send fails fast with a
    # connection refusal (non-fatal) instead of hanging.
    monkeypatch.setenv('SMTP_PORT', '1')
    monkeypatch.setenv('SMTP_USER', 'unit-test-smtp-user')
    monkeypatch.setenv('SMTP_PASSWORD', 'unit-test-smtp-password')
    monkeypatch.setenv('APP_BASE_URL', 'https://unit-test.example')
    mod = reload_app()
    mod.limiter.enabled = False
    yield mod


@pytest.fixture()
def client(app_module):
    """Test client with CSRF handling done for it.

    Every mutating request (POST/PUT/PATCH/DELETE) sent through this client
    automatically carries a valid token: form data gets a ``csrf_token``
    field, JSON bodies get an ``X-CSRFToken`` header. This is a test-harness
    convenience only - the app enforces CSRF for every request, and the
    ``raw_client`` fixture below is used to test missing/invalid tokens.
    """
    from helpers import csrf_wrapped

    app_module.app.config['TESTING'] = True
    # NOTE: no ``with`` around the client. Entering the context manager sets
    # Flask 3's ``preserve_context`` flag, which keeps ONE app context (and
    # its ``g``) alive across every subsequent request from ANY client in
    # this thread. Flask-Login only re-resolves ``current_user`` when
    # ``g._login_user`` is unset, so a preserved context makes the first
    # logged-in user stick for every later request - a false ownership leak
    # in tests. Cookies still persist via the client's own cookie jar.
    c = app_module.app.test_client()
    yield csrf_wrapped(c)


@pytest.fixture()
def raw_client(app_module):
    """Unmodified test client - mutating requests carry NO CSRF token.

    Use this for the security tests that verify missing/invalid tokens are
    rejected with 400.
    """
    app_module.app.config['TESTING'] = True
    # Same no-``with`` rationale as the ``client`` fixture above.
    c = app_module.app.test_client()
    yield c


@pytest.fixture()
def prod_client(prod_app):
    from helpers import csrf_wrapped

    prod_app.app.config['TESTING'] = True
    # Same no-``with`` rationale as the ``client`` fixture above.
    c = prod_app.app.test_client()
    yield csrf_wrapped(c)
