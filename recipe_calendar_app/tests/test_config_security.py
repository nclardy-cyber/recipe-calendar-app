"""Configuration security: SECRET_KEY handling, debug mode, gunicorn safety.

These tests reload the app module under different environments. The
DATABASE_URL is always pointed at a temp database so the real databases are
never touched.
"""
from pathlib import Path

import pytest

from conftest import APP_DIR, _tmp_db_url, reload_app

PROCFILE = APP_DIR / 'Procfile'
APP_SOURCE = APP_DIR / 'app.py'


def _point_at_temp_db(monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_URL', _tmp_db_url(tmp_path))


def test_secret_key_read_from_environment(monkeypatch, tmp_path):
    _point_at_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv('SECRET_KEY', 'env-provided-key-123')
    monkeypatch.delenv('FLASK_ENV', raising=False)
    mod = reload_app()
    assert mod.app.secret_key == 'env-provided-key-123'


def test_missing_secret_key_uses_dev_fallback_in_development(monkeypatch, tmp_path):
    _point_at_temp_db(monkeypatch, tmp_path)
    monkeypatch.delenv('SECRET_KEY', raising=False)
    monkeypatch.delenv('FLASK_ENV', raising=False)
    mod = reload_app()
    assert mod.app.secret_key == mod.DEV_FALLBACK_SECRET_KEY
    # And it is clearly not the old per-startup random key pattern
    assert mod.app.secret_key == mod.DEV_FALLBACK_SECRET_KEY


def test_missing_secret_key_refuses_startup_in_production(monkeypatch, tmp_path):
    _point_at_temp_db(monkeypatch, tmp_path)
    monkeypatch.delenv('SECRET_KEY', raising=False)
    monkeypatch.setenv('FLASK_ENV', 'production')
    with pytest.raises(RuntimeError, match='SECRET_KEY'):
        reload_app()


def test_missing_secret_key_refuses_startup_when_prod_alias(monkeypatch, tmp_path):
    _point_at_temp_db(monkeypatch, tmp_path)
    monkeypatch.delenv('SECRET_KEY', raising=False)
    monkeypatch.setenv('FLASK_ENV', 'prod')
    with pytest.raises(RuntimeError, match='SECRET_KEY'):
        reload_app()


def test_debug_disabled_by_default(monkeypatch, tmp_path):
    _point_at_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv('SECRET_KEY', 'k' * 40)
    monkeypatch.delenv('FLASK_DEBUG', raising=False)
    mod = reload_app()
    assert mod.app.debug is False
    assert mod.app.config['DEBUG'] is False


def test_debug_can_be_enabled_explicitly_for_local_dev(monkeypatch, tmp_path):
    _point_at_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv('SECRET_KEY', 'k' * 40)
    monkeypatch.setenv('FLASK_DEBUG', '1')
    mod = reload_app()
    assert mod.app.debug is True


def test_gunicorn_never_enables_werkzeug_debugger():
    """Gunicorn serves app:app; the module must not force debug on and the
    Procfile must not pass debugger-related flags."""
    proc = PROCFILE.read_text(encoding='utf-8')
    assert 'debug' not in proc.lower()
    assert '--reload' not in proc
    # The app module sets debug from FLASK_DEBUG only (default off)
    src = APP_SOURCE.read_text(encoding='utf-8')
    assert 'app.run(host=\'0.0.0.0\'' not in src
    assert 'debug=True' not in src


def test_secret_key_never_logged():
    """The secret must not appear in any log statement in app.py."""
    src = APP_SOURCE.read_text(encoding='utf-8')
    for line in src.splitlines():
        if 'print(' in line or 'logger.' in line or 'logging.' in line:
            assert 'secret_key' not in line.lower(), line
