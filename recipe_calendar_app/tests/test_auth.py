"""CMP-003B: public registration and authentication tests.

Covers: registration (normalization, duplicates, validation), password
hashing, login/logout, email verification (success / expiration / reuse),
password reset (success / expiration / reuse), anti-enumeration generic
responses, production behaviour without SMTP, session security settings,
and token storage (hashed, never raw).
"""
import hashlib

from datetime import timedelta

from flask_login import current_user  # noqa: F401


EMAIL = 'alice@example.com'
PASSWORD = 'correct-horse-battery-9'
NEW_PASSWORD = 'new-secret-password-42'


# --- helpers ----------------------------------------------------------------

def register(client, email=EMAIL, password=PASSWORD, follow=True):
    return client.post('/signup', data={'email': email, 'password': password},
                       follow_redirects=follow)


def get_user(app_module, email):
    return app_module.User.query.filter_by(
        email=app_module.normalize_email(email)).first()


def outbox_token(app_module, body_marker, last=True):
    """Extract a raw token from a captured dev-outbox email link."""
    box = app_module.dev_outbox
    msg = box[-1] if last else box[0]
    assert body_marker in msg['body'], \
        f'link marker missing from email body: {body_marker}'
    return msg['body'].split(body_marker)[1].split('\n')[0]


def verify_link(app_module, token=None, marker='/verify-email/'):
    if token is None:
        token = outbox_token(app_module, marker)
    return f'/verify-email/{token}'


def reset_link(app_module, token=None, marker='/reset-password/'):
    if token is None:
        token = outbox_token(app_module, marker)
    return f'/reset-password/{token}'


def login(client, email=EMAIL, password=PASSWORD, follow=True):
    return client.post('/login', data={'email': email, 'password': password},
                       follow_redirects=follow)


def assert_authenticated(app_module, client, email=EMAIL):
    with app_module.app.app_context():
        assert current_user.is_authenticated
        assert current_user.email == app_module.normalize_email(email)
    page = client.get('/calendar').get_data(as_text=True)
    assert email in page  # nav shows the signed-in email


def assert_anonymous(app_module, client, email=EMAIL):
    with app_module.app.app_context():
        assert not current_user.is_authenticated
    page = client.get('/calendar').get_data(as_text=True)
    assert email not in page


# --- registration ------------------------------------------------------------

def test_signup_creates_normalized_unverified_account(app_module, client):
    resp = register(client, email='  Alice@Example.COM  ')
    assert resp.status_code == 200
    assert 'Your account was created' in resp.get_data(as_text=True)

    user = get_user(app_module, 'alice@example.com')
    assert user is not None
    assert user.email == 'alice@example.com'  # normalized (trim + lowercase)
    assert user.email_verified is False
    assert user.status == 'active'
    assert user.created_at is not None

    # A verification email was captured in the dev outbox with a link.
    assert len(app_module.dev_outbox) == 1
    assert app_module.dev_outbox[0]['to'] == 'alice@example.com'
    assert '/verify-email/' in app_module.dev_outbox[0]['body']


def test_signup_rejects_duplicate_email(app_module, client):
    assert register(client).status_code == 200
    resp = register(client, email='ALICE@example.com')
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert 'already exists' in page
    assert app_module.User.query.count() == 1


def test_signup_validates_email_and_password(app_module, client):
    # Bad email format
    resp = register(client, email='not-an-email', password=PASSWORD)
    assert 'valid email address' in resp.get_data(as_text=True)
    # Too-short password
    resp = register(client, email=EMAIL, password='short')
    assert 'at least 8' in resp.get_data(as_text=True)
    # Too-long password
    resp = register(client, email=EMAIL, password='x' * 129)
    assert 'too long' in resp.get_data(as_text=True)
    assert app_module.User.query.count() == 0


def test_passwords_are_hashed_not_plaintext(app_module, client):
    register(client)
    user = get_user(app_module, EMAIL)
    assert user.password_hash != PASSWORD
    assert PASSWORD not in user.password_hash
    # Werkzeug method-prefixed salted hash (scrypt by default on this version)
    assert ':' in user.password_hash
    assert user.check_password(PASSWORD)
    assert not user.check_password('wrong-password')


def test_salt_makes_hashes_unique_per_account(app_module, client):
    register(client, email='one@example.com', password=PASSWORD)
    register(client, email='two@example.com', password=PASSWORD)
    h1 = get_user(app_module, 'one@example.com').password_hash
    h2 = get_user(app_module, 'two@example.com').password_hash
    assert h1 != h2


# --- login / logout -----------------------------------------------------------

def test_login_success_then_logout(app_module, client):
    register(client)  # dev mode: unverified login allowed locally
    assert_anonymous(app_module, client)

    resp = login(client)
    assert resp.status_code == 200
    assert_authenticated(app_module, client)

    resp = client.get('/logout', follow_redirects=True)
    assert 'You have been logged out' in resp.get_data(as_text=True)
    assert_anonymous(app_module, client)


def test_login_wrong_password_shows_generic_message(app_module, client):
    register(client)
    resp = login(client, password='definitely-wrong-1')
    assert 'Invalid email or password.' in resp.get_data(as_text=True)
    assert_anonymous(app_module, client)


def test_login_unknown_email_same_message_as_wrong_password(app_module, client):
    """Anti-enumeration: unknown email and wrong password are indistinguishable."""
    wrong_pw = login(client, email='nobody@example.com',
                     password='whatever-12345')
    missing = login(client, email='ghost@example.com',
                    password='whatever-12345')
    # Both fail with exactly the same visible message.
    assert wrong_pw.get_data(as_text=True).count('Invalid email or password.') == 1
    assert missing.get_data(as_text=True).count('Invalid email or password.') == 1
    # No account was created by the failed logins.
    assert get_user(app_module, 'ghost@example.com') is None
    assert get_user(app_module, 'nobody@example.com') is None


def test_disabled_account_login_is_generic(app_module, client):
    register(client)
    user = get_user(app_module, EMAIL)
    user.status = 'disabled'
    app_module.db.session.commit()
    resp = login(client)
    assert 'Invalid email or password.' in resp.get_data(as_text=True)
    assert_anonymous(app_module, client)


# --- email verification --------------------------------------------------------

def test_verification_success(app_module, client):
    register(client)
    link = verify_link(app_module)
    resp = client.get(link)
    assert resp.status_code == 200
    assert 'Email verified' in resp.get_data(as_text=True)

    user = get_user(app_module, EMAIL)
    assert user.email_verified is True
    assert user.verification_token_hash is None
    assert user.verification_token_expires_at is None


def test_verification_expired_link(app_module, client):
    register(client)
    link = verify_link(app_module)
    user = get_user(app_module, EMAIL)
    user.verification_token_expires_at = (
        app_module._utcnow() - timedelta(minutes=1))
    app_module.db.session.commit()

    resp = client.get(link)
    assert resp.status_code == 200
    assert 'invalid or has expired' in resp.get_data(as_text=True)
    assert get_user(app_module, EMAIL).email_verified is False


def test_verification_token_is_single_use(app_module, client):
    register(client)
    link = verify_link(app_module)
    assert 'Email verified' in client.get(link).get_data(as_text=True)
    # Second use of the same link must be rejected.
    resp = client.get(link)
    assert 'invalid or has expired' in resp.get_data(as_text=True)
    # (Account stays verified; the link itself is dead.)
    assert get_user(app_module, EMAIL).email_verified is True


def test_verification_bad_token(app_module, client):
    resp = client.get('/verify-email/definitely-not-a-real-token')
    assert resp.status_code == 200
    assert 'invalid or has expired' in resp.get_data(as_text=True)


def test_stored_token_is_sha256_of_raw_token(app_module, client):
    """The DB holds only the SHA-256 hash of the emailed raw token."""
    register(client)
    raw = outbox_token(app_module, '/verify-email/')
    user = get_user(app_module, EMAIL)
    assert user.verification_token_hash == hashlib.sha256(
        raw.encode('utf-8')).hexdigest()
    assert raw not in user.verification_token_hash
    assert len(user.verification_token_hash) == 64


# --- password reset -------------------------------------------------------------

def test_password_reset_success(app_module, client):
    register(client)
    before = len(app_module.dev_outbox)

    resp = client.post('/reset-password', data={'email': EMAIL},
                       follow_redirects=True)
    assert 'reset link has been sent' in resp.get_data(as_text=True)
    assert len(app_module.dev_outbox) == before + 1

    link = reset_link(app_module)
    page = client.get(link)
    assert page.status_code == 200
    assert 'New Password' in page.get_data(as_text=True)

    resp = client.post(link, data={'password': NEW_PASSWORD,
                                   'confirm': NEW_PASSWORD},
                       follow_redirects=True)
    assert 'password has been updated' in resp.get_data(as_text=True)

    user = get_user(app_module, EMAIL)
    assert user.check_password(NEW_PASSWORD)
    assert not user.check_password(PASSWORD)
    assert user.password_reset_token_hash is None

    # New password works, old one no longer does.
    login(client, password=NEW_PASSWORD)
    assert_authenticated(app_module, client)
    resp = client.get('/logout', follow_redirects=True)
    assert 'logged out' in resp.get_data(as_text=True)
    resp = login(client, password=PASSWORD)
    assert 'Invalid email or password.' in resp.get_data(as_text=True)


def test_password_reset_expired_link(app_module, client):
    register(client)
    client.post('/reset-password', data={'email': EMAIL})
    link = reset_link(app_module)

    user = get_user(app_module, EMAIL)
    user.password_reset_token_expires_at = (
        app_module._utcnow() - timedelta(minutes=1))
    app_module.db.session.commit()

    resp = client.get(link)
    assert 'invalid or has expired' in resp.get_data(as_text=True)
    # Password unchanged: old login still works.
    login(client)
    assert_authenticated(app_module, client)


def test_password_reset_token_is_single_use(app_module, client):
    register(client)
    client.post('/reset-password', data={'email': EMAIL})
    link = reset_link(app_module)
    resp = client.post(link, data={'password': NEW_PASSWORD,
                                   'confirm': NEW_PASSWORD},
                       follow_redirects=True)
    assert 'password has been updated' in resp.get_data(as_text=True)

    # Reusing the same link must fail (GET shows the problem page).
    resp = client.get(link)
    assert 'invalid or has expired' in resp.get_data(as_text=True)


def test_password_reset_rejects_mismatched_confirm(app_module, client):
    register(client)
    client.post('/reset-password', data={'email': EMAIL})
    link = reset_link(app_module)
    resp = client.post(link, data={'password': NEW_PASSWORD,
                                   'confirm': 'different-12345678'})
    page = resp.get_data(as_text=True)
    assert 'Passwords do not match.' in page
    user = get_user(app_module, EMAIL)
    assert user.check_password(PASSWORD)  # unchanged
    # Token still valid: a correct POST still works.
    resp = client.post(link, data={'password': NEW_PASSWORD,
                                   'confirm': NEW_PASSWORD},
                       follow_redirects=True)
    assert 'password has been updated' in resp.get_data(as_text=True)


def test_reset_request_unknown_email_is_generic_and_sends_nothing(app_module, client):
    before = len(app_module.dev_outbox)
    resp = client.post('/reset-password', data={'email': 'ghost@example.com'},
                       follow_redirects=True)
    assert 'reset link has been sent' in resp.get_data(as_text=True)
    assert len(app_module.dev_outbox) == before  # no email for unknown address


def test_resend_verification_unknown_email_is_generic(app_module, client):
    before = len(app_module.dev_outbox)
    resp = client.post('/resend-verification', data={'email': 'ghost@example.com'},
                       follow_redirects=True)
    assert 'verification link has been sent' in resp.get_data(as_text=True)
    assert len(app_module.dev_outbox) == before


def test_resend_verification_issues_fresh_link(app_module, client):
    register(client)
    first_link = verify_link(app_module)
    before = len(app_module.dev_outbox)
    client.post('/resend-verification', data={'email': EMAIL},
                follow_redirects=True)
    assert len(app_module.dev_outbox) == before + 1
    second_link = verify_link(app_module)
    assert second_link != first_link
    # The new link works; the old one is dead (token rotated).
    assert 'Email verified' in client.get(second_link).get_data(as_text=True)
    assert 'invalid or has expired' in client.get(first_link).get_data(as_text=True)


# --- production behaviour (no SMTP) ---------------------------------------------

def test_production_signup_never_exposes_link_and_requires_verification(
        prod_app, prod_client):
    resp = register(prod_client)
    assert 'Check your email' in resp.get_data(as_text=True)
    # No dev outbox capture in production.
    assert len(prod_app.dev_outbox) == 0
    # Account exists but is unverified and cannot log in.
    user = get_user(prod_app, EMAIL)
    assert user is not None
    assert user.email_verified is False
    resp = login(prod_client)
    assert 'Invalid email or password.' in resp.get_data(as_text=True)
    assert not user.email_verified


def test_production_login_verified_account_works(prod_app, prod_client):
    register(prod_client)
    # Simulate the user clicking the emailed verification link. Load and
    # commit inside one app context so the change lands in the same scoped
    # session that owns the object.
    with prod_app.app.app_context():
        user = get_user(prod_app, EMAIL)
        user.email_verified = True
        prod_app.db.session.commit()
    login(prod_client)
    assert_authenticated(prod_app, prod_client)


# --- session security ------------------------------------------------------------

def test_session_security_settings_dev(client, app_module):
    c = app_module.app.config
    assert c['SESSION_COOKIE_HTTPONLY'] is True
    assert c['SESSION_COOKIE_SAMESITE'] == 'Lax'
    assert c['SESSION_COOKIE_SECURE'] is False  # local dev, plain HTTP
    assert c['SESSION_PROTECTION'] == 'basic'


def test_session_secure_cookie_in_production(prod_client, prod_app):
    assert prod_app.app.config['SESSION_COOKIE_SECURE'] is True
