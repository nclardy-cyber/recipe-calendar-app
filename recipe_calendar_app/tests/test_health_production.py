"""CMP-006A (M4/M5): production health checks and migration lifecycle.

Every test runs against disposable databases on the session's embedded
PostgreSQL server (or a CMP_PG_TEST_URL server when set); the project's
real SQLite databases are never touched:

* /health (liveness) answers 200 regardless of database state;
* /health/ready returns 503 on an UNMIGRATED database and 200 once the
  schema is at the migration head;
* application routes return 503 until migrations reach head (the
  before_request backstop) and serve again after the upgrade - without a
  process restart;
* a legacy recipe-only database (no user table, recipes with no owner
  column) upgrades successfully through `flask db upgrade`, preserving
  every recipe with user_id = NULL, and then reports ready.
"""
import uuid

import pytest

from conftest import _alembic_upgrade, reload_app
from helpers import csrf_wrapped

PROD_KEY = 'health-prod-test-secret-key-0123456789'

LEGACY_ROWS = [
    (1, 'Legacy Pasta', 'img1', '2026-11-01', 'Legacy pasta description'),
    (2, 'Legacy Soup', 'img2', '2026-11-02', 'Legacy soup description'),
    (3, 'Legacy Salad', None, '2026-11-03', ''),
    (4, 'Legacy Cake', 'img4', '2026-11-04', 'Legacy cake description'),
]


@pytest.fixture()
def raw_prod_app(pg_database_raw, monkeypatch):
    """Production-mode app against a disposable, UNMIGRATED PostgreSQL
    database (fresh, no alembic_version, no tables)."""
    monkeypatch.setenv('DATABASE_URL', pg_database_raw)
    monkeypatch.setenv('SECRET_KEY', PROD_KEY)
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.delenv('FLASK_DEBUG', raising=False)
    monkeypatch.setenv('REDIS_URL', 'redis://127.0.0.1:6390/9')
    monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
    monkeypatch.setenv('SMTP_PORT', '1')
    monkeypatch.setenv('SMTP_USER', 'unit-test-smtp-user')
    monkeypatch.setenv('SMTP_PASSWORD', 'unit-test-smtp-password')
    monkeypatch.setenv('APP_BASE_URL', 'https://unit-test.example')
    mod = reload_app()
    mod.limiter.enabled = False
    yield mod


def test_health_liveness_independent_of_database(prod_client):
    """/health never touches the database: 200 with the process up."""
    resp = prod_client.get('/health')
    assert resp.status_code == 200
    assert resp.get_json() == {'status': 'ok'}


def test_health_and_ready_200_after_migration(prod_client, prod_app):
    """Migrated database at head: both /health and /health/ready are 200
    with the full readiness body."""
    resp = prod_client.get('/health')
    assert resp.status_code == 200
    assert resp.get_json() == {'status': 'ok'}
    resp = prod_client.get('/health/ready')
    assert resp.status_code == 200
    assert resp.get_json() == {'status': 'ok', 'database': 'ok',
                               'schema': 'ok'}
    # application traffic is served once ready
    assert prod_client.get('/').status_code == 200


def test_unmigrated_db_readiness_503(raw_prod_app):
    """A fresh database with no alembic_version: readiness fails with the
    coarse 'unmigrated' body (no schema details, no connection strings)."""
    c = csrf_wrapped(raw_prod_app.app.test_client())
    resp = c.get('/health/ready')
    assert resp.status_code == 503
    body = resp.get_json()
    assert body['status'] == 'unavailable'
    assert body['schema'] == 'not ready'
    # liveness still answers 200 (the process is up)
    assert c.get('/health').status_code == 200


def test_app_routes_503_until_migrations_reach_head(raw_prod_app,
                                                    pg_database_raw):
    """The before_request backstop refuses application traffic against an
    unmigrated database; after `flask db upgrade` the SAME process (no
    restart) reports ready and serves."""
    c = csrf_wrapped(raw_prod_app.app.test_client())
    # calendar, API and login page all refuse while unmigrated
    assert c.get('/').status_code == 503
    assert c.get('/api/recipes').status_code == 503
    assert c.get('/login').status_code == 503
    # and mutating traffic is refused too - the backstop runs before CSRF
    # validation, so no token is needed (or obtainable) for the 503
    raw = raw_prod_app.app.test_client()
    resp = raw.post('/signup', data={
        'email': 'm4@example.com', 'password': 'password-12345'})
    assert resp.status_code == 503

    # apply the documented pre-start migration step
    _alembic_upgrade(pg_database_raw)

    # readiness re-checks after a failure: the same process is ready now
    resp = c.get('/health/ready')
    assert resp.status_code == 200
    assert resp.get_json() == {'status': 'ok', 'database': 'ok',
                               'schema': 'ok'}
    assert c.get('/').status_code == 200


def test_unreachable_db_readiness_503(monkeypatch, tmp_path):
    """An unreachable database (not just an unmigrated one): readiness is
    503 with the coarse 'database unavailable' body."""
    monkeypatch.setenv(
        'DATABASE_URL',
        'postgres://unit-test:***@127.0.0.1:1/cmp006a_unreachable')
    monkeypatch.setenv('SECRET_KEY', PROD_KEY)
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.delenv('FLASK_DEBUG', raising=False)
    monkeypatch.setenv('REDIS_URL', 'redis://127.0.0.1:6390/9')
    monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
    monkeypatch.setenv('SMTP_PORT', '1')
    monkeypatch.setenv('SMTP_USER', 'unit')
    monkeypatch.setenv('SMTP_PASSWORD', 'unit')
    monkeypatch.setenv('APP_BASE_URL', 'https://unit-test.example')
    mod = reload_app()
    mod.limiter.enabled = False
    c = csrf_wrapped(mod.app.test_client())
    resp = c.get('/health/ready')
    assert resp.status_code == 503
    body = resp.get_json()
    assert body['status'] == 'unavailable'
    assert body['database'] == 'unavailable'


def test_legacy_recipe_only_db_upgrades_preserving_rows(pg_database_raw,
                                                        monkeypatch):
    """M5: a pre-auth, recipe-only database (no user table, no user_id
    column) upgrades through the real `flask db upgrade` pre-start step,
    preserving every legacy recipe with user_id = NULL; afterwards the
    app reports ready and legacy rows stay invisible (unowned)."""
    import psycopg

    con = psycopg.connect(pg_database_raw, connect_timeout=15)
    with con:
        with con.cursor() as cur:
            cur.execute('''CREATE TABLE recipe (
                id INTEGER NOT NULL,
                title VARCHAR(100) NOT NULL,
                image VARCHAR(200),
                date DATE,
                description TEXT,
                PRIMARY KEY (id))''')
            cur.executemany(
                'INSERT INTO recipe VALUES (%s,%s,%s,%s,%s)', LEGACY_ROWS)
    con.close()

    # the documented pre-start migration step
    _alembic_upgrade(pg_database_raw)

    con = psycopg.connect(pg_database_raw, connect_timeout=15)
    try:
        with con.cursor() as cur:
            cur.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'")
            tables = {r[0] for r in cur.fetchall()}
            cur.execute(
                'SELECT id, title, image, date::text, description, user_id '
                'FROM recipe ORDER BY id')
            rows = cur.fetchall()
            cur.execute('SELECT version_num FROM alembic_version')
            version = cur.fetchone()[0]
    finally:
        con.close()

    assert {'user', 'recipe', 'alembic_version'} <= tables
    assert version == '0003'
    # all 4 legacy rows preserved, every one unowned
    assert len(rows) == 4
    for orig, after in zip(LEGACY_ROWS, rows):
        assert orig == (after[0], after[1], after[2], after[3], after[4])
        assert after[5] is None  # user_id stays NULL

    # the upgraded database now reports ready and serves
    monkeypatch.setenv('DATABASE_URL', pg_database_raw)
    monkeypatch.setenv('SECRET_KEY', PROD_KEY)
    monkeypatch.setenv('FLASK_ENV', 'production')
    monkeypatch.delenv('FLASK_DEBUG', raising=False)
    monkeypatch.setenv('REDIS_URL', 'redis://127.0.0.1:6390/9')
    monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
    monkeypatch.setenv('SMTP_PORT', '1')
    monkeypatch.setenv('SMTP_USER', 'unit')
    monkeypatch.setenv('SMTP_PASSWORD', 'unit')
    monkeypatch.setenv('APP_BASE_URL', 'https://unit-test.example')
    mod = reload_app()
    mod.limiter.enabled = False
    c = csrf_wrapped(mod.app.test_client())
    assert c.get('/health/ready').status_code == 200
    assert c.get('/').status_code == 200
    # guests never see legacy (unowned) recipes
    assert c.get('/api/recipes').status_code == 401

