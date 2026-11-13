"""CMP-006A (M2): trusted-proxy client identification tests.

The rate-limit key function must identify clients using only the proxy
hops the deployment actually has:

* development (direct connection): REMOTE_ADDR only, X-Forwarded-For is
  never read - a forged header can neither split a bucket nor impersonate
  another client;
* production (behind Render's load balancer, one trusted hop): the LAST
  X-Forwarded-For entry (the one the trusted proxy appended) is the
  client identity; entries written upstream of it are client-controlled
  and never trusted;
* multi-hop topologies (TRUSTED_PROXY_HOPS=N): the N trusted-written
  suffix entries are validated, and the client is the entry just left of
  them;
* any ambiguity (missing/short header, invalid entry in the trusted
  suffix) falls back to the kernel-verified REMOTE_ADDR;
* in production every worker shares Redis, so the trusted identity is
  enforced globally across workers, and distinct clients keep distinct
  buckets.

The Redis tests use the same local disposable redis-server pattern as
``test_production_hardening.py`` and skip cleanly when no binary exists.
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
from helpers import csrf_wrapped

PROD_KEY = 'prod-trust-test-secret-key-0123456789'


# ---------------------------------------------------------------------------
# environment / loader helpers
# ---------------------------------------------------------------------------

def _load_app(monkeypatch, tmp_path, flask_env, trusted_hops=None,
              redis_url=None, pg_url=None):
    """Load a fresh app module under a fully controlled environment.

    ``flask_env`` is 'development' or 'production'; ``trusted_hops`` of
    None leaves TRUSTED_PROXY_HOPS unset (testing the mode defaults).
    Production loads get the full valid production environment with a
    PostgreSQL URL that is never contacted (import-only tests) or the
    disposable migrated *pg_url* (request-level tests).
    """
    if flask_env == 'production':
        monkeypatch.setenv(
            'DATABASE_URL', pg_url
            or 'postgres://unit-test:***@127.0.0.1:1/cmp006a_import_only')
        monkeypatch.setenv('SECRET_KEY', PROD_KEY)
        monkeypatch.setenv('FLASK_ENV', 'production')
        monkeypatch.setenv('REDIS_URL',
                           redis_url or 'redis://127.0.0.1:6390/9')
        monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
        monkeypatch.setenv('SMTP_PORT', '1')
        monkeypatch.setenv('SMTP_USER', 'unit')
        monkeypatch.setenv('SMTP_PASSWORD', 'unit')
        monkeypatch.setenv('APP_BASE_URL', 'https://unit-test.example')
    else:
        monkeypatch.setenv('DATABASE_URL', _tmp_db_url(tmp_path))
        monkeypatch.setenv('SECRET_KEY', 'unit-test-secret-key')
        monkeypatch.setenv('FLASK_ENV', flask_env)
        monkeypatch.delenv('REDIS_URL', raising=False)
    monkeypatch.delenv('FLASK_DEBUG', raising=False)
    if trusted_hops is None:
        monkeypatch.delenv('TRUSTED_PROXY_HOPS', raising=False)
    else:
        monkeypatch.setenv('TRUSTED_PROXY_HOPS', str(trusted_hops))
    return reload_app()


def _load_worker(monkeypatch, tmp_path, name, redis_url, pg_url):
    """Load app.py as an INDEPENDENT module object - one "worker" of a
    multi-worker deployment (own Flask app, limiter and key function)."""
    monkeypatch.setenv('DATABASE_URL', pg_url)
    monkeypatch.setenv('SECRET_KEY', PROD_KEY)
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.delenv('FLASK_DEBUG', raising=False)
    monkeypatch.setenv('REDIS_URL', redis_url)
    # Explicit trust config: Render's single load balancer is the one
    # verified proxy hop in this deployment.
    monkeypatch.setenv('TRUSTED_PROXY_HOPS', '1')
    monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
    monkeypatch.setenv('SMTP_PORT', '1')
    monkeypatch.setenv('SMTP_USER', 'unit')
    monkeypatch.setenv('SMTP_PASSWORD', 'unit')
    monkeypatch.setenv('APP_BASE_URL', 'https://unit-test.example')
    spec = importlib.util.spec_from_file_location(name, APP_DIR / 'app.py')
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    mod.limiter.enabled = True
    return mod


def _key(mod, remote_addr, xff=None):
    """Resolve _trusted_client_ip() inside a request context."""
    environ = {'REMOTE_ADDR': remote_addr}
    if xff is not None:
        environ['HTTP_X_FORWARDED_FOR'] = xff
    with mod.app.test_request_context('/login', environ_overrides=environ):
        return mod._trusted_client_ip()


# ---------------------------------------------------------------------------
# 1. key function: development (no proxy)
# ---------------------------------------------------------------------------

def test_dev_default_is_zero_hops(monkeypatch, tmp_path):
    mod = _load_app(monkeypatch, tmp_path, 'development')
    assert mod._trusted_proxy_hops() == 0


def test_dev_mode_ignores_forwarded_for(monkeypatch, tmp_path):
    """No proxy in front: REMOTE_ADDR is the identity, XFF never read."""
    mod = _load_app(monkeypatch, tmp_path, 'development')
    assert _key(mod, '203.0.113.7') == '203.0.113.7'
    # a (forged) XFF header must not change the identity
    assert _key(mod, '203.0.113.7',
                '198.51.100.1, 192.0.2.99') == '203.0.113.7'


def test_dev_explicit_zero_hops_ignores_forwarded_for(monkeypatch, tmp_path):
    mod = _load_app(monkeypatch, tmp_path, 'development', trusted_hops=0)
    assert _key(mod, '203.0.113.7', '198.51.100.1') == '203.0.113.7'


# ---------------------------------------------------------------------------
# 2. key function: production (single trusted hop)
# ---------------------------------------------------------------------------

def test_prod_default_is_one_hop(monkeypatch, tmp_path):
    mod = _load_app(monkeypatch, tmp_path, 'production')
    assert mod._trusted_proxy_hops() == 1


def test_prod_single_hop_uses_last_entry(monkeypatch, tmp_path):
    """The LAST XFF entry is the one the trusted proxy appended - that is
    the client. Earlier entries are client-controlled."""
    mod = _load_app(monkeypatch, tmp_path, 'production')
    # direct-to-proxy client: the proxy appends the real client address
    assert _key(mod, '10.0.0.2', '203.0.113.10') == '203.0.113.10'
    # client behind its own upstream hops / forged prefix: the trusted
    # suffix (last entry) still wins
    assert _key(mod, '10.0.0.2',
                '198.51.100.66, 192.0.2.77, 203.0.113.10') == '203.0.113.10'


def test_prod_missing_header_falls_back_to_remote_addr(monkeypatch, tmp_path):
    mod = _load_app(monkeypatch, tmp_path, 'production')
    assert _key(mod, '10.0.0.2') == '10.0.0.2'


def test_prod_short_header_falls_back_to_remote_addr(monkeypatch, tmp_path):
    """Fewer entries than the trusted hop count: the header is
    client-stripped, so only REMOTE_ADDR is trustworthy."""
    mod = _load_app(monkeypatch, tmp_path, 'production', trusted_hops=2)
    assert _key(mod, '10.0.0.2', '203.0.113.10') == '10.0.0.2'


def test_prod_invalid_trusted_entry_falls_back_to_remote_addr(
        monkeypatch, tmp_path):
    """A non-IP in the trusted suffix makes the whole header
    untrustworthy - fall back to REMOTE_ADDR, never the garbage."""
    mod = _load_app(monkeypatch, tmp_path, 'production')
    assert _key(mod, '10.0.0.2', '203.0.113.10, not-an-ip') == '10.0.0.2'
    # an invalid UPSTREAM entry is fine: only the trusted suffix matters
    assert _key(mod, '10.0.0.2',
                'not-an-ip, 203.0.113.10') == '203.0.113.10'


def test_ipv6_entries_are_accepted(monkeypatch, tmp_path):
    mod = _load_app(monkeypatch, tmp_path, 'production')
    assert _key(mod, '10.0.0.2', '2001:db8::1') == '2001:db8::1'


# ---------------------------------------------------------------------------
# 3. key function: multi-hop topologies
# ---------------------------------------------------------------------------

def test_two_hops_uses_entry_left_of_trusted_suffix(monkeypatch, tmp_path):
    """C -> P1 -> P2 -> app: XFF = [forged..., C, P1]. The last two
    entries were written by trusted proxies; the client is the one just
    left of them."""
    mod = _load_app(monkeypatch, tmp_path, 'production', trusted_hops=2)
    assert _key(mod, '10.0.0.2',
                '198.51.100.66, 203.0.113.10, 10.1.1.1') == '203.0.113.10'
    # exactly two entries: the client is the entry the FIRST trusted hop
    # observed (entries[-2]); only a SHORTER header falls back
    assert _key(mod, '10.0.0.2',
                '203.0.113.10, 10.1.1.1') == '203.0.113.10'


def test_invalid_value_in_trusted_suffix_falls_back(monkeypatch, tmp_path):
    mod = _load_app(monkeypatch, tmp_path, 'production', trusted_hops=2)
    assert _key(mod, '10.0.0.2',
                '203.0.113.10, garbage') == '10.0.0.2'


def test_invalid_trusted_proxy_hops_refuses_startup(monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_URL', _tmp_db_url(tmp_path))
    monkeypatch.setenv('SECRET_KEY', 'unit-test-secret-key')
    monkeypatch.setenv('FLASK_ENV', 'development')
    monkeypatch.delenv('FLASK_DEBUG', raising=False)
    monkeypatch.delenv('REDIS_URL', raising=False)
    monkeypatch.setenv('TRUSTED_PROXY_HOPS', 'not-a-number')
    with pytest.raises(RuntimeError, match='TRUSTED_PROXY_HOPS'):
        reload_app()


# ---------------------------------------------------------------------------
# 4. limiter integration: buckets follow the trusted identity
# ---------------------------------------------------------------------------

def _signup(client, email, environ=None):
    kwargs = {'environ_overrides': environ} if environ else {}
    return client.post('/signup',
                       data={'email': email, 'password': 'password-12345'},
                       **kwargs)


def test_distinct_clients_behind_proxy_have_separate_buckets(
        monkeypatch, tmp_path):
    """Two clients arriving through the same proxy (same REMOTE_ADDR)
    with different trusted suffix entries get independent signup
    budgets."""
    mod = _load_app(monkeypatch, tmp_path, 'development', trusted_hops=1)
    mod.limiter.enabled = True
    c = csrf_wrapped(mod.app.test_client())
    client_a = {'REMOTE_ADDR': '10.0.0.2',
                'HTTP_X_FORWARDED_FOR': '203.0.113.10'}
    client_b = {'REMOTE_ADDR': '10.0.0.2',
                'HTTP_X_FORWARDED_FOR': '203.0.113.11'}

    for i in range(3):
        resp = _signup(c, f'pa{i}@example.com', client_a)
        assert resp.status_code == 302, f'signup {i} throttled early'
    # client A is now out of budget
    resp = _signup(c, 'pa9@example.com', client_a)
    assert resp.status_code == 429
    # client B, same proxy, different identity: unaffected
    resp = _signup(c, 'pb0@example.com', client_b)
    assert resp.status_code == 302


def test_forged_header_cannot_ride_another_clients_bucket(
        monkeypatch, tmp_path):
    """An attacker forges the victim's address in the UPSTREAM part of
    the XFF header. The trusted proxy appends the attacker's real
    address last, so the attacker gets their OWN bucket - they cannot
    drain or spoof the victim's identity."""
    mod = _load_app(monkeypatch, tmp_path, 'development', trusted_hops=1)
    mod.limiter.enabled = True
    c = csrf_wrapped(mod.app.test_client())
    victim = {'REMOTE_ADDR': '10.0.0.2',
              'HTTP_X_FORWARDED_FOR': '203.0.113.50'}
    attacker = {'REMOTE_ADDR': '10.0.0.2',
                'HTTP_X_FORWARDED_FOR': '203.0.113.50, 203.0.113.60'}

    for i in range(3):
        resp = c.post('/signup', data={
            'email': f'victim{i}@example.com', 'password': 'password-12345'},
            environ_overrides=victim)
        assert resp.status_code == 302
    # victim's bucket is exhausted
    resp = c.post('/signup', data={
        'email': 'victim9@example.com', 'password': 'password-12345'},
        environ_overrides=victim)
    assert resp.status_code == 429
    # attacker forges the victim upstream but is identified by the
    # trusted last entry -> own (empty) bucket, not throttled
    resp = c.post('/signup', data={
        'email': 'attacker@example.com', 'password': 'password-12345'},
        environ_overrides=attacker)
    assert resp.status_code == 302
    # ...and the victim's remaining budget state is untouched: the victim
    # is still throttled, and the attacker now has their own budget
    resp = c.post('/signup', data={
        'email': 'victim10@example.com', 'password': 'password-12345'},
        environ_overrides=victim)
    assert resp.status_code == 429


def test_dev_mode_forged_xff_neither_splits_nor_impersonates(
        monkeypatch, tmp_path):
    """Development (no proxy): forged XFF headers are ignored entirely.
    Same REMOTE_ADDR = same client regardless of XFF; a different
    REMOTE_ADDR is a different client."""
    mod = _load_app(monkeypatch, tmp_path, 'development')
    mod.limiter.enabled = True
    c = csrf_wrapped(mod.app.test_client())
    same_ip_xff_1 = {'REMOTE_ADDR': '203.0.113.7',
                     'HTTP_X_FORWARDED_FOR': '198.51.100.1'}
    same_ip_xff_2 = {'REMOTE_ADDR': '203.0.113.7',
                     'HTTP_X_FORWARDED_FOR': '192.0.2.99'}
    other_ip = {'REMOTE_ADDR': '203.0.113.8'}

    for i in range(3):
        resp = c.post('/signup', data={
            'email': f'dev{i}@example.com', 'password': 'password-12345'},
            environ_overrides=same_ip_xff_1)
        assert resp.status_code == 302
    # same REMOTE_ADDR, different forged XFF: SAME bucket -> throttled
    resp = c.post('/signup', data={
        'email': 'dev9@example.com', 'password': 'password-12345'},
        environ_overrides=same_ip_xff_2)
    assert resp.status_code == 429
    # different REMOTE_ADDR: distinct bucket -> allowed
    resp = c.post('/signup', data={
        'email': 'dev8@example.com', 'password': 'password-12345'},
        environ_overrides=other_ip)
    assert resp.status_code == 302


def test_invalid_xff_shares_remote_addr_bucket(monkeypatch, tmp_path):
    """An unparseable trusted entry falls back to REMOTE_ADDR, so the
    request shares the bucket of requests without the header."""
    mod = _load_app(monkeypatch, tmp_path, 'development', trusted_hops=1)
    mod.limiter.enabled = True
    c = csrf_wrapped(mod.app.test_client())
    bad_header = {'REMOTE_ADDR': '10.0.0.2',
                  'HTTP_X_FORWARDED_FOR': 'garbage'}
    no_header = {'REMOTE_ADDR': '10.0.0.2'}

    for i in range(3):
        resp = c.post('/signup', data={
            'email': f'bad{i}@example.com', 'password': 'password-12345'},
            environ_overrides=bad_header)
        assert resp.status_code == 302
    # 4th attempt, this time WITHOUT the header: same bucket -> throttled
    resp = c.post('/signup', data={
        'email': 'bad9@example.com', 'password': 'password-12345'},
        environ_overrides=no_header)
    assert resp.status_code == 429


# ---------------------------------------------------------------------------
# 5. shared Redis enforcement across workers (trusted identity)
# ---------------------------------------------------------------------------

def _find_redis_server():
    candidates = [
        shutil.which('redis-server'),
        os.path.join(os.environ.get('PROGRAMFILES', r'C:\\Program Files'),
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
    """Short-lived local Redis server (same pattern as
    test_production_hardening.py). Skips without a binary."""
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


def test_redis_shared_enforcement_uses_trusted_identity(
        monkeypatch, tmp_path, redis_instance, pg_database):
    """Two independent workers sharing one Redis: the SAME trusted client
    identity (same XFF suffix entry, same proxy peer) is throttled
    GLOBALLY no matter which worker serves the request, while a distinct
    client identity keeps its own budget on both workers."""
    mod_a = _load_worker(monkeypatch, tmp_path, 'cmp006a_trust_a',
                         redis_instance.url, pg_database)
    mod_b = _load_worker(monkeypatch, tmp_path, 'cmp006a_trust_b',
                         redis_instance.url, pg_database)
    c_a = csrf_wrapped(mod_a.app.test_client())
    c_b = csrf_wrapped(mod_b.app.test_client())

    same_client = {'REMOTE_ADDR': '10.0.0.2',
                   'HTTP_X_FORWARDED_FOR': '203.0.113.77'}
    other_client = {'REMOTE_ADDR': '10.0.0.2',
                    'HTTP_X_FORWARDED_FOR': '203.0.113.78'}

    # client one exhausts its 3/hour signup budget on worker A
    for i in range(3):
        resp = c_a.post('/signup', data={
            'email': f'one-{i}@example.com', 'password': 'password-12345'},
            environ_overrides=same_client)
        assert resp.status_code == 302, f'signup {i} throttled early'

    # same identity arriving at the OTHER worker: globally throttled
    resp = c_b.post('/signup', data={
        'email': 'one-again@example.com', 'password': 'password-12345'},
        environ_overrides=same_client)
    assert resp.status_code == 429
    # ...and on worker A too
    resp = c_a.post('/signup', data={
        'email': 'one-again2@example.com', 'password': 'password-12345'},
        environ_overrides=same_client)
    assert resp.status_code == 429

    # a distinct client identity is unaffected on either worker
    resp = c_b.post('/signup', data={
        'email': 'two-0@example.com', 'password': 'password-12345'},
        environ_overrides=other_client)
    assert resp.status_code == 302
    resp = c_a.post('/signup', data={
        'email': 'two-1@example.com', 'password': 'password-12345'},
        environ_overrides=other_client)
    assert resp.status_code == 302
