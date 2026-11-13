"""CMP-005B: production security hardening tests.

Covers:

* fail-safe production startup validation (SECRET_KEY strength, REDIS_URL,
  SMTP credentials, HTTPS APP_BASE_URL)
* session policy: browser-close default, 12h persistent remember-me,
  cookie flags, logout invalidation, session fixation/hijack protection
* Redis-backed rate limiting shared across INDEPENDENT app instances
  (two "workers"), and fail-closed behaviour when Redis is unreachable
* SMTP delivery through a local controlled SMTP server (aiosmtpd):
  credential use, base-URL link construction, dev-outbox isolation, and
  that credentials are never logged

Redis tests skip cleanly when no local redis-server binary is available;
SMTP delivery tests skip when aiosmtpd is not installed.
"""
import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from conftest import APP_DIR, _tmp_db_url, reload_app
from helpers import DEFAULT_PASSWORD, csrf_wrapped, get_csrf_token, login, make_user

# 41 characters - comfortably above the 32-char production minimum.
PROD_KEY = 'prod-hardening-test-secret-key-0123456789'

try:
    import aiosmtpd  # noqa: F401
    _HAS_AIOSMTPD = True
except ImportError:
    _HAS_AIOSMTPD = False


# ---------------------------------------------------------------------------
# environment builders
# ---------------------------------------------------------------------------

def _prod_env(monkeypatch, tmp_path, **overrides):
    """A complete, valid production environment with per-test overrides.

    Pass ``name=None`` in *overrides* to delete a variable.
    """
    monkeypatch.setenv('DATABASE_URL', _tmp_db_url(tmp_path))
    monkeypatch.setenv('SECRET_KEY', PROD_KEY)
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.delenv('FLASK_DEBUG', raising=False)
    monkeypatch.setenv('REDIS_URL', 'redis://127.0.0.1:6390/9')
    monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
    monkeypatch.setenv('SMTP_PORT', '1')
    monkeypatch.setenv('SMTP_USER', 'test-smtp-user')
    monkeypatch.setenv('SMTP_PASSWORD', 'test-smtp-password')
    monkeypatch.setenv('APP_BASE_URL', 'https://unit-test.example')
    for key, value in overrides.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def _load_worker(monkeypatch, tmp_path, name, redis_url,
                 smtp_port=None, base_url='https://unit-test.example'):
    """Load app.py as an INDEPENDENT module object (its own Flask app,
    limiter and database) - i.e. one "worker" of a multi-worker deployment.

    Environment is re-read at load time, so it must be set before the call.
    """
    db_dir = tmp_path / name
    db_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('DATABASE_URL', _tmp_db_url(db_dir))
    monkeypatch.setenv('SECRET_KEY', PROD_KEY)
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.delenv('FLASK_DEBUG', raising=False)
    monkeypatch.setenv('REDIS_URL', redis_url)
    monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
    if smtp_port is not None:
        monkeypatch.setenv('SMTP_PORT', str(smtp_port))
    else:
        monkeypatch.setenv('SMTP_PORT', '1')
    monkeypatch.setenv('SMTP_USER', 'test-smtp-user')
    monkeypatch.setenv('SMTP_PASSWORD', 'test-smtp-password')
    monkeypatch.setenv('APP_BASE_URL', base_url)
    spec = importlib.util.spec_from_file_location(name, APP_DIR / 'app.py')
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    mod.limiter.enabled = True
    return mod


def _session_cookie(resp) -> str:
    """Return the 'session' Set-Cookie header from a response."""
    for cookie in resp.headers.getlist('Set-Cookie'):
        if cookie.startswith('session='):
            return cookie
    raise AssertionError('no session cookie in response')


def _cookie_expiry_seconds(cookie: str):
    """Seconds until the cookie expires, from Max-Age or Expires."""
    import re
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime
    match = re.search(r'Max-Age=(\d+)', cookie)
    if match:
        return int(match.group(1))
    match = re.search(r'Expires=([^;]+)', cookie)
    if match:
        expires = parsedate_to_datetime(match.group(1).strip())
        return (expires - datetime.now(timezone.utc)).total_seconds()
    return None


def _decode_session_cookie(app, value: str) -> dict:
    """Decode a Flask signed session cookie VALUE (no 'session=' prefix)
    into its dict, using the app's own session-serializer config."""
    serializer = app.session_interface.get_signing_serializer(app)
    return serializer.loads(value)


# ---------------------------------------------------------------------------
# 1. production startup validation (fail-safe)
# ---------------------------------------------------------------------------

def test_prod_missing_redis_url_refuses_startup(monkeypatch, tmp_path):
    _prod_env(monkeypatch, tmp_path, REDIS_URL=None)
    with pytest.raises(RuntimeError, match='REDIS_URL'):
        reload_app()


def test_prod_redis_url_must_use_redis_scheme(monkeypatch, tmp_path):
    _prod_env(monkeypatch, tmp_path, REDIS_URL='http://127.0.0.1:6379/0')
    with pytest.raises(RuntimeError, match='REDIS_URL'):
        reload_app()


def test_prod_missing_smtp_credentials_refuse_startup(monkeypatch, tmp_path):
    _prod_env(monkeypatch, tmp_path, SMTP_USER=None, SMTP_PASSWORD=None)
    with pytest.raises(RuntimeError) as exc:
        reload_app()
    msg = str(exc.value)
    assert 'SMTP_USER' in msg and 'SMTP_PASSWORD' in msg
    assert 'SMTP_HOST' not in msg  # the host WAS provided


def test_prod_non_https_app_base_url_refuses_startup(monkeypatch, tmp_path):
    _prod_env(monkeypatch, tmp_path, APP_BASE_URL='http://insecure.example')
    with pytest.raises(RuntimeError, match='APP_BASE_URL'):
        reload_app()


def test_prod_weak_secret_key_refuses_startup(monkeypatch, tmp_path):
    _prod_env(monkeypatch, tmp_path, SECRET_KEY='too-short')
    with pytest.raises(RuntimeError, match='SECRET_KEY'):
        reload_app()


def test_prod_development_fallback_key_refused(monkeypatch, tmp_path):
    _prod_env(monkeypatch, tmp_path,
              SECRET_KEY='dev-only-insecure-secret-key-change-me')
    with pytest.raises(RuntimeError, match='SECRET_KEY'):
        reload_app()


def test_prod_multiple_missing_vars_reported_together(monkeypatch, tmp_path):
    _prod_env(monkeypatch, tmp_path, REDIS_URL=None, SMTP_USER=None,
              APP_BASE_URL='http://insecure.example')
    with pytest.raises(RuntimeError) as exc:
        reload_app()
    msg = str(exc.value)
    assert 'REDIS_URL' in msg and 'SMTP_USER' in msg and 'APP_BASE_URL' in msg


def test_prod_startup_with_full_config_loads(monkeypatch, tmp_path):
    _prod_env(monkeypatch, tmp_path)
    mod = reload_app()
    assert mod.app.secret_key == PROD_KEY
    assert mod.limiter._storage_uri == 'redis://127.0.0.1:6390/9'
    assert mod.limiter._in_memory_fallback_enabled is False
    assert mod.limiter._swallow_errors is False
    assert mod.app.config['SESSION_COOKIE_SECURE'] is True


def test_dev_defaults_never_require_production_vars(monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_URL', _tmp_db_url(tmp_path))
    monkeypatch.setenv('SECRET_KEY', 'unit-test-secret-key')
    for var in ('FLASK_ENV', 'FLASK_DEBUG', 'REDIS_URL', 'SMTP_HOST',
                'SMTP_USER', 'SMTP_PASSWORD', 'APP_BASE_URL'):
        monkeypatch.delenv(var, raising=False)
    mod = reload_app()
    assert mod.limiter._storage_uri == 'memory://'
    assert mod.app.config['SESSION_COOKIE_SECURE'] is False
    assert mod.app.secret_key == 'unit-test-secret-key'


def test_production_secret_key_never_regenerated(monkeypatch, tmp_path):
    """The same environment must yield the same key on every start -
    never a fresh random key that would log everyone out on each restart.
    """
    _prod_env(monkeypatch, tmp_path)
    key1 = reload_app().app.secret_key
    key2 = reload_app().app.secret_key
    assert key1 == key2 == PROD_KEY


# ---------------------------------------------------------------------------
# 2. session policy and cookie flags
# ---------------------------------------------------------------------------

def test_default_login_is_browser_close_session(client, app_module):
    """No 'remember me' -> session cookie with NO expiry (dies when the
    browser closes); the 12h permanent lifetime must not apply."""
    make_user(app_module, 'sess@example.com')
    token = get_csrf_token(client)
    resp = client.post('/login', data={
        'email': 'sess@example.com', 'password': DEFAULT_PASSWORD,
        'csrf_token': token})
    assert resp.status_code == 302
    cookie = _session_cookie(resp)
    assert _cookie_expiry_seconds(cookie) is None, cookie


def test_remember_me_login_gets_12h_persistent_cookie(client, app_module):
    """'remember me' -> session cookie expiring 12 hours out (Flask emits
    this as Max-Age=43200 and/or an Expires header 12h ahead)."""
    make_user(app_module, 'remember@example.com')
    token = get_csrf_token(client)
    resp = client.post('/login', data={
        'email': 'remember@example.com', 'password': DEFAULT_PASSWORD,
        'csrf_token': token, 'remember': 'y'})
    assert resp.status_code == 302
    cookie = _session_cookie(resp)
    expiry = _cookie_expiry_seconds(cookie)
    assert expiry is not None, cookie
    # 12 hours = 43200 s; allow generous tolerance for clock granularity
    assert 40000 < expiry <= 43200, (expiry, cookie)


def test_session_cookie_flags_in_production(prod_client, prod_app):
    make_user(prod_app, 'flags@example.com')
    token = get_csrf_token(prod_client)
    resp = prod_client.post('/login', data={
        'email': 'flags@example.com', 'password': DEFAULT_PASSWORD,
        'csrf_token': token})
    assert resp.status_code == 302
    cookie = _session_cookie(resp)
    assert 'Secure' in cookie, cookie
    assert 'HttpOnly' in cookie, cookie
    assert 'SameSite=Lax' in cookie, cookie


def test_logout_invalidates_session(client, app_module):
    make_user(app_module, 'logout@example.com')
    login(client, 'logout@example.com')
    assert client.get('/api/recipes').status_code == 200
    resp = client.post('/logout')
    assert resp.status_code == 302
    # the session is gone: API access is refused again
    assert client.get('/api/recipes').status_code == 401


def test_login_reissues_session_cookie(client, app_module):
    """Session-fixation guard: the pre-login anonymous cookie value must
    not survive as the signed-in session's cookie (logging in rewrites
    the session content, so the value changes)."""
    pre_cookie = _session_cookie(client.get('/login'))
    make_user(app_module, 'fix@example.com')
    resp = login(client, 'fix@example.com')
    assert _session_cookie(resp) != pre_cookie


def test_session_protection_marks_session_stale_on_ip_change(client, app_module):
    """SESSION_PROTECTION='basic': a mid-session change of remote address
    (same user agent) is detected as a hijack signal. In 'basic' mode the
    user stays signed in (no false logouts on mobile networks) but the
    session is re-issued and marked not-fresh (``_fresh`` = False) - the
    hijack indicator the app relies on. ('strong' mode would log the user
    out instead; 'basic' was chosen deliberately.)"""
    make_user(app_module, 'prot@example.com')
    env_same = {'REMOTE_ADDR': '10.1.2.3', 'HTTP_USER_AGENT': 'TestAgent/1.0'}
    env_other = {'REMOTE_ADDR': '10.9.9.9', 'HTTP_USER_AGENT': 'TestAgent/1.0'}
    token = get_csrf_token(client)
    resp = client.post('/login', data={
        'email': 'prot@example.com', 'password': DEFAULT_PASSWORD,
        'csrf_token': token}, environ_overrides=env_same)
    assert resp.status_code == 302
    login_cookie = _session_cookie(resp)
    assert client.get('/api/recipes',
                      environ_overrides=env_same).status_code == 200

    # same user agent, different IP: the session identifier no longer matches
    resp = client.get('/api/recipes', environ_overrides=env_other)
    assert resp.status_code == 200  # 'basic' mode: no forced logout
    new_cookie = _session_cookie(resp)
    assert new_cookie != login_cookie  # the session was re-issued
    decoded = _decode_session_cookie(
        app_module.app, new_cookie[len('session='):].split(';', 1)[0])
    assert decoded.get('_user_id') is not None  # still the logged-in user
    assert decoded.get('_fresh') is False  # ...but marked stale (hijack signal)


# ---------------------------------------------------------------------------
# 3. Redis-backed rate limiting (shared storage, fail-closed outages)
# ---------------------------------------------------------------------------

def _find_redis_server():
    candidates = [
        shutil.which('redis-server'),
        os.path.join(os.environ.get('PROGRAMFILES', r'C:\Program Files'),
                     'Redis', 'redis-server.exe'),
    ]
    for cand in candidates:
        if cand and os.path.exists(cand):
            return cand
    return None


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_redis(port, timeout=15.0):
    import redis as redis_lib
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if redis_lib.Redis(host='127.0.0.1', port=port,
                               socket_connect_timeout=1).ping():
                return True
        except Exception:
            time.sleep(0.2)
    return False


@pytest.fixture()
def redis_instance():
    """A short-lived local Redis server for the shared-storage tests.

    Yields SimpleNamespace(url=..., stop=callable). Skips when no local
    redis-server binary is available.
    """
    exe = _find_redis_server()
    if not exe:
        pytest.skip('no local redis-server binary available')
    port = _free_port()
    proc = subprocess.Popen(
        [exe, '--port', str(port), '--bind', '127.0.0.1',
         '--save', '', '--appendonly', 'no'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not _wait_for_redis(port):
        proc.terminate()
        pytest.skip('local redis-server did not start in time')

    import redis as redis_lib
    redis_lib.Redis(host='127.0.0.1', port=port).flushdb()

    stopped = {'done': False}

    def _stop():
        if not stopped['done']:
            stopped['done'] = True
            proc.terminate()

    yield SimpleNamespace(url=f'redis://127.0.0.1:{port}/0', stop=_stop)
    _stop()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


def test_redis_signup_limit_shared_across_instances(monkeypatch, tmp_path,
                                                    redis_instance):
    """Two independent app instances sharing one Redis: the 3/hour signup
    limit is global, not per-worker."""
    mod_a = _load_worker(monkeypatch, tmp_path, 'cmp005b_worker_a',
                         redis_instance.url)
    mod_b = _load_worker(monkeypatch, tmp_path, 'cmp005b_worker_b',
                         redis_instance.url)
    c_a = csrf_wrapped(mod_a.app.test_client())
    c_b = csrf_wrapped(mod_b.app.test_client())

    for i in range(3):
        resp = c_a.post('/signup', data={
            'email': f'shared-a{i}@example.com', 'password': 'password-12345'})
        assert resp.status_code == 302, f'signup {i} unexpectedly throttled'

    # 4th signup - from the OTHER worker - must be throttled: the counters
    # live in shared Redis, not in worker A's memory.
    resp = c_b.post('/signup', data={
        'email': 'shared-b@example.com', 'password': 'password-12345'})
    assert resp.status_code == 429
    # and worker A is still throttled too (the budget is shared both ways)
    resp = c_a.post('/signup', data={
        'email': 'shared-a9@example.com', 'password': 'password-12345'})
    assert resp.status_code == 429


def test_redis_login_limit_shared_across_instances(monkeypatch, tmp_path,
                                                   redis_instance):
    """The 5/15-minute login limit is shared across workers."""
    mod_a = _load_worker(monkeypatch, tmp_path, 'cmp005b_login_a',
                         redis_instance.url)
    mod_b = _load_worker(monkeypatch, tmp_path, 'cmp005b_login_b',
                         redis_instance.url)
    c_a = csrf_wrapped(mod_a.app.test_client())
    c_b = csrf_wrapped(mod_b.app.test_client())

    for _ in range(5):
        resp = c_a.post('/login', data={
            'email': 'nobody@example.com', 'password': 'wrong-password'})
        assert resp.status_code == 302  # generic failure redirect

    # 6th login attempt from the OTHER worker -> throttled
    resp = c_b.post('/login', data={
        'email': 'nobody@example.com', 'password': 'wrong-password'})
    assert resp.status_code == 429


def test_redis_outage_fails_closed(monkeypatch, tmp_path, redis_instance):
    """With Redis unreachable in production, rate-limited endpoints fail
    CLOSED with a 500 (no silent per-worker in-memory fallback), while
    unlimited endpoints keep working."""
    mod = _load_worker(monkeypatch, tmp_path, 'cmp005b_outage',
                       redis_instance.url)
    # make sure a 500 error response (not a re-raised exception) is returned
    mod.app.config['PROPAGATE_EXCEPTIONS'] = False
    c = csrf_wrapped(mod.app.test_client())

    # while Redis is up, the rate-limited endpoint works
    resp = c.post('/login', data={
        'email': 'nobody@example.com', 'password': 'wrong-password'})
    assert resp.status_code == 302

    # kill Redis
    redis_instance.stop()

    # next rate-limited request fails closed with 500 (not 429, not 200)
    resp = c.post('/login', data={
        'email': 'nobody@example.com', 'password': 'wrong-password'})
    assert resp.status_code == 500

    # unlimited endpoints are unaffected by the outage
    assert c.get('/').status_code == 200


# ---------------------------------------------------------------------------
# 4. SMTP delivery through a local controlled server
# ---------------------------------------------------------------------------

@pytest.fixture()
def smtp_server():
    """A local controlled SMTP server (aiosmtpd) that records every
    envelope and every authentication attempt."""
    if not _HAS_AIOSMTPD:
        pytest.skip('aiosmtpd is not installed')
    import aiosmtpd.controller
    import aiosmtpd.smtp

    class _Handler:
        def __init__(self):
            self.envelopes = []

        async def handle_DATA(self, server, session, envelope):
            self.envelopes.append(envelope)
            return '250 Message accepted for delivery'

    handler = _Handler()
    auth = []

    def _authenticator(server, session, envelope, mechanism, auth_data):
        # aiosmtpd >=1.4.6 callback: (server, session, envelope, mechanism,
        # LoginPassword(login, password)); must return an AuthResult
        auth.append((auth_data.login.decode(), auth_data.password.decode()))
        return aiosmtpd.smtp.AuthResult(success=True, handled=True,
                                        auth_data=auth_data)

    port = _free_port()
    ctrl = aiosmtpd.controller.Controller(
        handler,
        hostname='127.0.0.1',
        port=port,
        auth_required=True,
        auth_require_tls=False,
        authenticator=_authenticator,
        decode_data=False,
    )
    ctrl.start()
    yield SimpleNamespace(port=port, handler=handler, auth=auth,
                          stop=ctrl.stop)
    ctrl.stop()


def _load_app_for_smtp(monkeypatch, tmp_path, smtp_port, base_url,
                       production=False):
    d = tmp_path / 'db'
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('DATABASE_URL', _tmp_db_url(d))
    monkeypatch.setenv('SECRET_KEY', PROD_KEY if production
                       else 'unit-test-secret-key')
    if production:
        monkeypatch.setenv('FLASK_ENV', 'production')
        # required by the production gate; the limiter is disabled so it is
        # never actually contacted
        monkeypatch.setenv('REDIS_URL', 'redis://127.0.0.1:6390/9')
    else:
        monkeypatch.delenv('FLASK_ENV', raising=False)
        monkeypatch.delenv('REDIS_URL', raising=False)
    monkeypatch.delenv('FLASK_DEBUG', raising=False)
    monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
    monkeypatch.setenv('SMTP_PORT', str(smtp_port))
    # the local mock server is plaintext-only; the app's safe default
    # (TLS on) stays untouched for real deployments
    monkeypatch.setenv('SMTP_USE_TLS', 'false')
    monkeypatch.setenv('SMTP_USER', 'smtp-test-user')
    monkeypatch.setenv('SMTP_PASSWORD', 'smtp-test-secret')
    monkeypatch.setenv('APP_BASE_URL', base_url)
    mod = reload_app()
    mod.limiter.enabled = False
    return mod


def test_smtp_delivers_to_configured_server(monkeypatch, tmp_path,
                                             smtp_server):
    mod = _load_app_for_smtp(monkeypatch, tmp_path, smtp_server.port,
                             'http://127.0.0.1:9977')
    c = csrf_wrapped(mod.app.test_client())
    resp = c.post('/signup', data={
        'email': 'mail@example.com', 'password': 'password-12345'},
        follow_redirects=True)
    assert 'Your account was created' in resp.get_data(as_text=True)

    assert len(smtp_server.handler.envelopes) == 1
    env = smtp_server.handler.envelopes[0]
    assert env.rcpt_tos == ['mail@example.com']
    body = env.content.decode('utf-8')
    # emailed link uses the configured base URL
    assert 'http://127.0.0.1:9977/verify-email/' in body
    # the configured credentials were used on the wire - and nothing else
    assert smtp_server.auth == [('smtp-test-user', 'smtp-test-secret')]
    # the dev outbox is never used when a real SMTP server is configured
    assert len(mod.dev_outbox) == 0


def test_smtp_production_uses_https_base_url_and_no_dev_outbox(
        monkeypatch, tmp_path, smtp_server):
    mod = _load_app_for_smtp(monkeypatch, tmp_path, smtp_server.port,
                             'https://unit-test.example', production=True)
    c = csrf_wrapped(mod.app.test_client())
    c.post('/signup', data={
        'email': 'mail-prod@example.com', 'password': 'password-12345'})

    assert len(smtp_server.handler.envelopes) == 1
    body = smtp_server.handler.envelopes[0].content.decode('utf-8')
    assert 'https://unit-test.example/verify-email/' in body
    assert len(mod.dev_outbox) == 0


def test_smtp_credentials_never_logged(monkeypatch, tmp_path, smtp_server,
                                       caplog):
    mod = _load_app_for_smtp(monkeypatch, tmp_path, smtp_server.port,
                             'http://127.0.0.1:9977')
    c = csrf_wrapped(mod.app.test_client())
    c.post('/signup', data={
        'email': 'logcheck@example.com', 'password': 'password-12345'})
    assert len(smtp_server.handler.envelopes) == 1  # delivery did happen
    assert 'smtp-test-secret' not in caplog.text
    assert 'smtp-test-user' not in caplog.text


def test_smtp_failure_is_non_fatal(monkeypatch, tmp_path):
    """SMTP configured but unreachable: signup still succeeds (the account
    is created) and the response is the generic one."""
    monkeypatch.setenv('DATABASE_URL', _tmp_db_url(tmp_path))
    monkeypatch.setenv('SECRET_KEY', 'unit-test-secret-key')
    monkeypatch.delenv('FLASK_ENV', raising=False)
    monkeypatch.delenv('FLASK_DEBUG', raising=False)
    monkeypatch.delenv('REDIS_URL', raising=False)
    monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
    monkeypatch.setenv('SMTP_PORT', '1')  # nothing listens here
    monkeypatch.setenv('SMTP_USER', 'u')
    monkeypatch.setenv('SMTP_PASSWORD', 'p')
    monkeypatch.setenv('APP_BASE_URL', 'http://127.0.0.1:9977')
    mod = reload_app()
    mod.limiter.enabled = False

    c = csrf_wrapped(mod.app.test_client())
    resp = c.post('/signup', data={
        'email': 'fail@example.com', 'password': 'password-12345'},
        follow_redirects=True)
    assert resp.status_code == 200
    assert 'Your account was created' in resp.get_data(as_text=True)
    with mod.app.app_context():
        user = mod.db.session.execute(
            mod.db.select(mod.User).where(
                mod.User.email == 'fail@example.com')).scalar_one()
    assert user is not None
    assert user.email_verified is False
