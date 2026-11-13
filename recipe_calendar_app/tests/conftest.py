"""Pytest fixtures for the recipe calendar app.

Every test runs against a throwaway SQLite database in a temp directory.
The original db/calendar.db and instance/calendar.db are never touched.
"""
import importlib
import os
import sys
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


@pytest.fixture()
def app_module(tmp_path, monkeypatch):
    """Fresh app instance backed by a temporary SQLite database.

    Rate limiting is disabled here so individual auth/feature tests are not
    throttled; ``test_rate_limits.py`` re-enables it on a fresh app.
    """
    monkeypatch.setenv('DATABASE_URL', _tmp_db_url(tmp_path))
    monkeypatch.setenv('SECRET_KEY', 'unit-test-secret-key')
    monkeypatch.delenv('FLASK_ENV', raising=False)
    monkeypatch.delenv('FLASK_DEBUG', raising=False)
    mod = reload_app()
    # Flask-Limiter v4 reads `enabled` from the limiter instance (config is
    # only consulted at init_app time), so toggle the attribute directly.
    mod.limiter.enabled = False
    yield mod


@pytest.fixture()
def prod_app(tmp_path, monkeypatch):
    """Fresh app instance in PRODUCTION mode (its own temp database).

    Supplies the full production environment (strong SECRET_KEY, REDIS_URL,
    SMTP credentials, HTTPS APP_BASE_URL) so the production startup gate
    passes. The limiter is disabled on this fixture, so REDIS_URL/SMTP are
    never actually contacted by the tests that use it.
    """
    monkeypatch.setenv('DATABASE_URL', _tmp_db_url(tmp_path))
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
