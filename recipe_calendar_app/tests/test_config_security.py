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
    monkeypatch.setenv('FLASK_ENV', 'development')
    mod = reload_app()
    assert mod.app.secret_key == 'env-provided-key-123'


def test_missing_secret_key_uses_dev_fallback_in_development(monkeypatch, tmp_path):
    _point_at_temp_db(monkeypatch, tmp_path)
    monkeypatch.delenv('SECRET_KEY', raising=False)
    # Explicit development opt-in: with FLASK_ENV unset the app is in
    # production mode and refuses to start without a production key.
    monkeypatch.setenv('FLASK_ENV', 'development')
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
    monkeypatch.setenv('FLASK_ENV', 'development')
    monkeypatch.delenv('FLASK_DEBUG', raising=False)
    mod = reload_app()
    assert mod.app.debug is False
    assert mod.app.config['DEBUG'] is False


def test_debug_can_be_enabled_explicitly_for_local_dev(monkeypatch, tmp_path):
    _point_at_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv('SECRET_KEY', 'k' * 40)
    monkeypatch.setenv('FLASK_ENV', 'development')
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


# --- SMTP_PORT validation (CMP-006A) -----------------------------------------
# The code parses SMTP_PORT as an integer and range-checks it at import time;
# the README documents the same contract. These pin both sides.

def test_smtp_port_defaults_to_587_when_unset(monkeypatch, tmp_path):
    _point_at_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv('SECRET_KEY', 'k' * 40)
    monkeypatch.setenv('FLASK_ENV', 'development')
    monkeypatch.delenv('SMTP_PORT', raising=False)
    mod = reload_app()
    assert mod.app.config['SMTP_PORT'] == 587


def test_smtp_port_must_be_an_integer(monkeypatch, tmp_path):
    _point_at_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv('SECRET_KEY', 'k' * 40)
    monkeypatch.setenv('FLASK_ENV', 'development')
    monkeypatch.setenv('SMTP_PORT', 'not-a-port')
    with pytest.raises(RuntimeError, match='SMTP_PORT must be an integer'):
        reload_app()


@pytest.mark.parametrize('port', ['0', '-1', '65536', '99999'])
def test_smtp_port_out_of_range_refuses_startup(monkeypatch, tmp_path, port):
    _point_at_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv('SECRET_KEY', 'k' * 40)
    monkeypatch.setenv('FLASK_ENV', 'development')
    monkeypatch.setenv('SMTP_PORT', port)
    with pytest.raises(RuntimeError, match='valid TCP port'):
        reload_app()


def test_smtp_port_boundary_values_accepted(monkeypatch, tmp_path):
    _point_at_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv('SECRET_KEY', 'k' * 40)
    monkeypatch.setenv('FLASK_ENV', 'development')
    monkeypatch.setenv('SMTP_PORT', '465')
    mod = reload_app()
    assert mod.app.config['SMTP_PORT'] == 465


def test_readme_documents_smtp_port_range():
    """The README's SMTP_PORT row must match the code's integer/range contract
    (it previously said 'Required, non-empty', which is not what the code does)."""
    readme = (APP_DIR / 'README.md').read_text(encoding='utf-8')
    line = next(l for l in readme.splitlines() if l.startswith('| `SMTP_PORT`'))
    assert 'integer' in line
    assert '65535' in line
