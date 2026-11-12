"""Shared helpers for CMP-003C tests (ownership + security).

``get_csrf_token`` pulls a session-bound CSRF token out of any rendered
form page; ``make_user``/``login`` are convenience builders for the two-user
isolation tests.
"""
import re

CSRF_TOKEN_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')

DEFAULT_PASSWORD = 'test-password-123'


def get_csrf_token(client) -> str:
    """Return the CSRF token rendered on the sign-in page.

    Flask-WTF tokens are bound to the session cookie the client already
    holds, so a single token is reusable across subsequent requests from
    the same client (within the 1-hour token lifetime).
    """
    html = client.get('/login').get_data(as_text=True)
    match = CSRF_TOKEN_RE.search(html)
    assert match, 'no csrf_token input rendered on /login'
    return match.group(1)


def make_user(app_module, email, password=DEFAULT_PASSWORD,
              verified=True, status='active'):
    """Create an account directly via the ORM (no registration flow)."""
    with app_module.app.app_context():
        user = app_module.User(email=email)
        user.set_password(password)
        user.email_verified = verified
        user.status = status
        app_module.db.session.add(user)
        app_module.db.session.commit()
    return user


def login(client, email, password=DEFAULT_PASSWORD):
    """Sign in over HTTP (with a valid CSRF token). Returns the 302."""
    token = get_csrf_token(client)
    resp = client.post('/login', data={
        'email': email, 'password': password, 'csrf_token': token},
        follow_redirects=False)
    assert resp.status_code == 302, f'login for {email} failed: {resp.status_code}'
    return resp


def csrf_wrapped(client):
    """Shadow the mutating methods so they inject a valid CSRF token.

    A same-origin ``Referer`` header is added too: real browsers send it on
    form POSTs, and in production mode the app's SSL-strict CSRF check
    (Flask-WTF ``WTF_CSRF_SSL_STRICT``) requires it because the test client
    speaks HTTPS there (``PREFERRED_URL_SCHEME``).
    """
    def _wrap(method_name):
        original = getattr(client, method_name)

        def wrapper(url, *args, **kwargs):
            headers = dict(kwargs.get('headers') or {})
            # Emulate a same-origin browser referrer (production mode
            # rejects secure state-changing requests without one).
            headers.setdefault('Referer', 'https://localhost/')
            if isinstance(kwargs.get('data'), dict):
                # form-encoded: token as a form field
                data = dict(kwargs['data'])
                data.setdefault('csrf_token', get_csrf_token(client))
                kwargs['data'] = data
            else:
                # JSON, raw, or bodyless mutation: token as a header
                headers.setdefault('X-CSRFToken', get_csrf_token(client))
            kwargs['headers'] = headers
            return original(url, *args, **kwargs)
        return wrapper

    for name in ('post', 'put', 'patch', 'delete'):
        setattr(client, name, _wrap(name))
    return client
