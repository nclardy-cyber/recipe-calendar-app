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

    Used to verify production behaviour (e.g. unverified accounts cannot
    log in, no dev outbox capture when SMTP is unconfigured).
    """
    monkeypatch.setenv('DATABASE_URL', _tmp_db_url(tmp_path))
    monkeypatch.setenv('SECRET_KEY', 'unit-test-secret-key')
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.delenv('FLASK_DEBUG', raising=False)
    mod = reload_app()
    mod.limiter.enabled = False
    yield mod


@pytest.fixture()
def client(app_module):
    app_module.app.config['TESTING'] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture()
def prod_client(prod_app):
    prod_app.app.config['TESTING'] = True
    with prod_app.app.test_client() as c:
        yield c
