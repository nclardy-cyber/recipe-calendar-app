"""User model and auth foundation.

Covers email normalization, password hashing, column defaults, the unique
normalized-email constraint, Flask-Login wiring, and the nullable recipe
ownership field (legacy recipes stay unowned).
"""
import datetime

import pytest

from conftest import reload_app


def _make_user(app_module, email, password='Str0ngPass!'):
    u = app_module.User(email=email)
    u.set_password(password)
    return u


def test_email_is_stored_normalized(app_module):
    with app_module.app.app_context():
        u = _make_user(app_module, '  Mixed.Case@Example.COM  ')
        app_module.db.session.add(u)
        app_module.db.session.commit()
        assert u.email == 'mixed.case@example.com'
        fetched = app_module.User.query.filter_by(
            email='mixed.case@example.com').first()
        assert fetched is not None and fetched.id == u.id


def test_password_is_hashed_and_verifiable(app_module):
    with app_module.app.app_context():
        u = _make_user(app_module, 'hash@example.com', 'S3cret!')
        app_module.db.session.add(u)
        app_module.db.session.commit()
        assert 'S3cret!' not in u.password_hash
        assert u.check_password('S3cret!')
        assert not u.check_password('wrong-password')


def test_unique_normalized_email_constraint(app_module):
    with app_module.app.app_context():
        u1 = _make_user(app_module, 'dup@example.com')
        u2 = _make_user(app_module, 'DUP@Example.COM')  # same normalized email
        app_module.db.session.add_all([u1, u2])
        with pytest.raises(Exception):
            app_module.db.session.commit()
        app_module.db.session.rollback()
        assert app_module.User.query.count() == 0


def test_account_defaults_and_timestamp(app_module):
    with app_module.app.app_context():
        u = _make_user(app_module, 'defaults@example.com')
        app_module.db.session.add(u)
        app_module.db.session.commit()
        assert u.email_verified is False
        assert u.status == 'active'
        assert u.created_at is not None


def test_flask_login_user_loader(app_module):
    with app_module.app.app_context():
        u = _make_user(app_module, 'loader@example.com')
        app_module.db.session.add(u)
        app_module.db.session.commit()
        uid = u.id
        loaded = app_module.load_user(str(uid))
        assert loaded is not None and loaded.id == uid
        assert app_module.load_user('999999') is None


def test_login_manager_wired(app_module):
    assert app_module.login_manager is not None
    assert app_module.login_manager.login_view == 'login'
    # UserMixin integration: get_id is available on the model
    u = _make_user(app_module, 'mixin@example.com')
    with app_module.app.app_context():
        app_module.db.session.add(u)
        app_module.db.session.commit()
        assert u.get_id() == str(u.id)


def test_legacy_recipe_remains_unowned(app_module):
    with app_module.app.app_context():
        r = app_module.Recipe(title='Legacy Row',
                              date=datetime.date(2026, 1, 5))
        app_module.db.session.add(r)
        app_module.db.session.commit()
        assert r.user_id is None
        assert r.user is None


def test_recipe_can_be_explicitly_owned(app_module):
    with app_module.app.app_context():
        u = _make_user(app_module, 'owner@example.com')
        app_module.db.session.add(u)
        app_module.db.session.flush()
        r = app_module.Recipe(title='Owned Row',
                              date=datetime.date(2026, 1, 6),
                              user_id=u.id)
        app_module.db.session.add(r)
        app_module.db.session.commit()
        assert r.user_id == u.id
        assert r.user.email == 'owner@example.com'


def test_migrate_ext_registered(app_module):
    # Flask-Migrate is attached to the app (enables `flask db` commands)
    assert 'migrate' in app_module.app.extensions
    assert app_module.migrate is not None
