"""CMP-003B: rate limiting tests for registration, login, password reset.

Each test runs against a freshly reloaded app (function-scoped fixtures),
so the limiter's in-memory store starts empty per test.
"""


def test_signup_rate_limited_after_three_attempts(app_module, client):
    """signup allows 3 attempts/hour; the 4th is throttled (429)."""
    app_module.limiter.enabled = True

    for i in range(3):
        resp = client.post('/signup',
                           data={'email': f'user{i}@example.com',
                                 'password': 'password-12345'},
                           follow_redirects=True)
        assert resp.status_code == 200
        assert 'Too many attempts' not in resp.get_data(as_text=True)

    resp = client.post('/signup',
                       data={'email': 'user3@example.com',
                             'password': 'password-12345'})
    assert resp.status_code == 429
    assert 'Too many attempts' in resp.get_data(as_text=True)
    # The 4th user was NOT created (throttled before the handler ran).
    assert app_module.User.query.filter_by(email='user3@example.com').first() \
        is None


def test_login_rate_limited_after_five_attempts(app_module, client):
    """login allows 5 attempts/15 minutes; the 6th is throttled (429)."""
    app_module.limiter.enabled = True

    for _ in range(5):
        resp = client.post('/login',
                           data={'email': 'someone@example.com',
                                 'password': 'wrong-password-1'},
                           follow_redirects=True)
        assert resp.status_code == 200

    resp = client.post('/login',
                       data={'email': 'someone@example.com',
                             'password': 'wrong-password-1'})
    assert resp.status_code == 429
    assert 'Too many attempts' in resp.get_data(as_text=True)


def test_password_reset_rate_limited_after_three_attempts(app_module, client):
    """reset-password allows 3 attempts/hour; the 4th is throttled (429)."""
    app_module.limiter.enabled = True

    for i in range(3):
        resp = client.post('/reset-password',
                           data={'email': f'r{i}@example.com'},
                           follow_redirects=True)
        assert resp.status_code == 200

    resp = client.post('/reset-password', data={'email': 'r3@example.com'})
    assert resp.status_code == 429
    assert 'Too many attempts' in resp.get_data(as_text=True)


def test_throttled_login_stays_generic(app_module, client):
    """Even a correct login is refused while throttled - no account
    information leaks through the 429."""
    app_module.limiter.enabled = True

    client.post('/signup', data={'email': 'bob@example.com',
                                 'password': 'password-12345'})
    for _ in range(5):
        client.post('/login', data={'email': 'bob@example.com',
                                    'password': 'wrong-password-1'})
    resp = client.post('/login', data={'email': 'bob@example.com',
                                       'password': 'password-12345'})
    assert resp.status_code == 429
    assert 'Invalid email or password' not in resp.get_data(as_text=True)


def test_unlimited_endpoints_not_throttled(app_module, client):
    """Reading the calendar is not rate limited."""
    app_module.limiter.enabled = True
    for _ in range(10):
        assert client.get('/').status_code == 200
