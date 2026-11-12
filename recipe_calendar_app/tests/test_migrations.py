"""Alembic migration tests against disposable databases.

Every test points Alembic at a fresh throwaway database (SQLite temp
file, or a disposable PostgreSQL database when CMP_PG_TEST_URL is set).
The project's real databases (db/calendar.db, instance/calendar.db) are
never opened.
"""
import os
import sqlite3

import pytest
from alembic import command
from alembic.config import Config

from conftest import APP_DIR

MIGRATIONS_DIR = APP_DIR / 'migrations'
ALEMBIC_INI = APP_DIR / 'alembic.ini'

LEGACY_ROWS = [
    (1, 'Legacy Pasta', 'img1', '2026-11-01', 'Legacy pasta description'),
    (2, 'Legacy Soup', 'img2', '2026-11-02', 'Legacy soup description'),
    (3, 'Legacy Salad', None, '2026-11-03', ''),
    (4, 'Legacy Cake', 'img4', '2026-11-04', 'Legacy cake description'),
]


def _build_legacy_db(db_path) -> None:
    """Create a pre-auth database with the legacy recipe schema + 4 rows."""
    con = sqlite3.connect(db_path)
    con.execute(
        '''CREATE TABLE recipe (
            id INTEGER NOT NULL,
            title VARCHAR(100) NOT NULL,
            image VARCHAR(200),
            date DATE,
            description TEXT,
            PRIMARY KEY (id))''')
    con.executemany('INSERT INTO recipe VALUES (?,?,?,?,?)', LEGACY_ROWS)
    con.commit()
    con.close()


def _alembic_cfg(db_path) -> Config:
    """Alembic config pointed at a disposable database (standalone mode)."""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option('script_location', str(MIGRATIONS_DIR))
    cfg.set_main_option(
        'sqlalchemy.url',
        'sqlite:///' + str(db_path).replace('\\', '/'))
    return cfg


def _table_names(db_path):
    con = sqlite3.connect(db_path)
    try:
        return {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def _recipe_columns(db_path):
    con = sqlite3.connect(db_path)
    try:
        return {r[1] for r in con.execute('PRAGMA table_info(recipe)')}
    finally:
        con.close()


def test_upgrade_empty_database(tmp_path):
    """Truly empty database: user table is created; the (nonexistent)
    recipe table is left for the app's db.create_all() to create with
    user_id already present."""
    db_path = tmp_path / 'fresh.db'
    command.upgrade(_alembic_cfg(db_path), 'head')

    tables = _table_names(db_path)
    assert {'user', 'alembic_version'} <= tables
    assert 'recipe' not in tables  # never created by the migration

    con = sqlite3.connect(db_path)
    try:
        assert con.execute('SELECT count(*) FROM user').fetchone()[0] == 0
    finally:
        con.close()


def test_upgrade_legacy_schema_without_rows(tmp_path):
    """Legacy schema, zero rows: recipe table gets user_id + FK, no rows
    to corrupt."""
    db_path = tmp_path / 'legacy_empty.db'
    con = sqlite3.connect(db_path)
    con.execute(
        '''CREATE TABLE recipe (
            id INTEGER NOT NULL,
            title VARCHAR(100) NOT NULL,
            image VARCHAR(200),
            date DATE,
            description TEXT,
            PRIMARY KEY (id))''')
    con.commit()
    con.close()

    command.upgrade(_alembic_cfg(db_path), 'head')

    cols = _recipe_columns(db_path)
    assert {'id', 'title', 'image', 'date', 'description', 'user_id'} <= cols
    con = sqlite3.connect(db_path)
    try:
        assert con.execute('SELECT count(*) FROM recipe').fetchone()[0] == 0
        # FK is in place
        fks = {r[2] for r in con.execute('PRAGMA foreign_key_list(recipe)')}
        assert 'user' in fks
    finally:
        con.close()


def test_upgrade_legacy_database_preserves_rows_and_keeps_them_unowned(tmp_path):
    db_path = tmp_path / 'legacy.db'
    _build_legacy_db(db_path)
    command.upgrade(_alembic_cfg(db_path), 'head')

    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            'SELECT id, title, image, date, description, user_id '
            'FROM recipe ORDER BY id').fetchall()
        # All 4 legacy rows preserved byte-for-byte in the original columns
        assert len(rows) == 4
        for orig, after in zip(LEGACY_ROWS, rows):
            assert orig == after[:5], (orig, after)
        # Every legacy recipe remains unowned (user_id IS NULL)
        assert all(r[5] is None for r in rows)
        assert con.execute('SELECT count(*) FROM user').fetchone()[0] == 0
    finally:
        con.close()


def test_upgrade_after_app_boot_created_user_table(app_module, tmp_path):
    """Realistic wrong ordering: the app booted against a legacy DB and its
    startup db.create_all() already created the user table. The migration
    must still complete: skip the user table, add user_id to recipe, and
    keep every legacy row."""
    db_path = tmp_path / 'boot_first.db'
    _build_legacy_db(db_path)

    # Simulate the app's startup create_all() creating ONLY the missing
    # user table (create_all never alters the existing recipe table)
    from sqlalchemy import create_engine
    engine = create_engine('sqlite:///' + str(db_path).replace('\\', '/'))
    with engine.begin() as conn:
        app_module.db.metadata.create_all(
            conn, tables=[app_module.db.metadata.tables['user']])
    engine.dispose()

    command.upgrade(_alembic_cfg(db_path), 'head')

    con = sqlite3.connect(db_path)
    try:
        # user table exists exactly once
        user_tables = [r for r in con.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='user'")]
        assert len(user_tables) == 1
        # recipe gained the owner column, all 4 rows intact and unowned
        rows = con.execute(
            'SELECT id, title, user_id FROM recipe ORDER BY id').fetchall()
        assert len(rows) == 4
        assert all(r[2] is None for r in rows)
    finally:
        con.close()


def test_upgrade_after_full_create_all_boot(app_module, tmp_path):
    """App booted first against a truly empty DB: startup db.create_all()
    created BOTH tables at their full current shape (user with token
    columns, recipe with user_id + FK + index). The migrations must
    complete as a no-op instead of failing with duplicate column/index
    errors, and the schema must be left intact."""
    db_path = tmp_path / 'boot_first_full.db'

    from sqlalchemy import create_engine
    engine = create_engine('sqlite:///' + str(db_path).replace('\\', '/'))
    with engine.begin() as conn:
        app_module.db.metadata.create_all(conn)
    engine.dispose()

    command.upgrade(_alembic_cfg(db_path), 'head')

    tables = _table_names(db_path)
    assert 'user' in tables
    assert 'recipe' in tables
    assert 'user_id' in _recipe_columns(db_path)
    con = sqlite3.connect(db_path)
    try:
        user_cols = {r[1] for r in con.execute('PRAGMA table_info(user)')}
        assert {'verification_token_hash', 'verification_token_expires_at',
                'password_reset_token_hash',
                'password_reset_token_expires_at'} <= user_cols
    finally:
        con.close()


def test_downgrade_reverts_schema_and_preserves_rows(tmp_path):
    db_path = tmp_path / 'legacy.db'
    _build_legacy_db(db_path)
    cfg = _alembic_cfg(db_path)
    command.upgrade(cfg, 'head')
    command.downgrade(cfg, 'base')

    tables = _table_names(db_path)
    assert 'user' not in tables
    assert 'recipe' in tables
    assert 'user_id' not in _recipe_columns(db_path)

    con = sqlite3.connect(db_path)
    try:
        rows = con.execute('SELECT id, title FROM recipe ORDER BY id').fetchall()
        assert [(r[0], r[1]) for r in rows] == [
            (r[0], r[1]) for r in LEGACY_ROWS]
    finally:
        con.close()


def test_alembic_version_bookkeeping(tmp_path):
    db_path = tmp_path / 'fresh.db'
    cfg = _alembic_cfg(db_path)
    command.upgrade(cfg, 'head')
    from alembic.script import ScriptDirectory
    head = ScriptDirectory.from_config(cfg).get_current_head()
    con = sqlite3.connect(db_path)
    try:
        assert con.execute(
            'SELECT version_num FROM alembic_version').fetchone()[0] == head
    finally:
        con.close()
    # A full downgrade to base drops the user table; alembic keeps the
    # (now empty) alembic_version bookkeeping table, which is expected.
    command.downgrade(cfg, 'base')
    tables = _table_names(db_path)
    assert 'user' not in tables
    if 'alembic_version' in tables:
        con = sqlite3.connect(db_path)
        try:
            assert con.execute(
                'SELECT count(*) FROM alembic_version').fetchone()[0] == 0
        finally:
            con.close()


# --------------------------------------------------------------------------
# 0001 status server default (CMP-005A-FIX2)
# --------------------------------------------------------------------------


def test_upgrade_status_default_is_quoted_string_literal(tmp_path):
    """0001's status server default must render as a quoted literal.

    ``sa.text('active')`` renders the unquoted ``DEFAULT active``, which
    PostgreSQL rejects with a syntax error (and which a SQLite batch
    rebuild would re-emit as ``DEFAULT (active)``). The plain string
    ``server_default='active'`` — matching the model — renders
    ``DEFAULT 'active'`` on every dialect.
    """
    db_path = tmp_path / 'fresh.db'
    command.upgrade(_alembic_cfg(db_path), 'head')
    con = sqlite3.connect(db_path)
    try:
        sql = con.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='user'").fetchone()[0]
    finally:
        con.close()
    for line in sql.splitlines():
        if line.strip().startswith('status'):
            assert "DEFAULT 'active'" in line, line
            return
    pytest.fail('status column not found in user table DDL')


# --------------------------------------------------------------------------
# PostgreSQL regression tests (CMP-005A / CMP-005A-FIX / CMP-005A-FIX2)
# --------------------------------------------------------------------------


def _pg_admin_url():
    """Admin PostgreSQL URL for disposable-database regression tests.

    Point CMP_PG_TEST_URL at an admin connection string (it must name an
    existing database on a server where the role can CREATE DATABASE,
    e.g. a local throwaway server). When unset the PostgreSQL test is
    skipped so the default suite stays dependency-free.
    """
    return os.environ.get('CMP_PG_TEST_URL')


def test_pg_downgrade_reserved_user_table_succeeds():
    """PostgreSQL downgrade over the reserved ``user`` table (CMP-005A).

    0002's downgrade drops its token columns with a raw
    ``ALTER TABLE ... DROP COLUMN`` statement. On PostgreSQL the table
    name must be quoted (``user`` is a reserved word); unquoted, the
    statement fails with a syntax error. SQLite is lenient and accepts
    it, which is why this defect was invisible in the SQLite tests above.

    The post-upgrade (head) schema state is built directly and stamped
    at revision 0002 so this test stays focused on 0002's quoted
    ``ALTER TABLE "user" DROP COLUMN`` regression. The complete,
    unstamped upgrade -> downgrade -> re-upgrade chain — including
    0001's real CREATE TABLE and its quoted status default — is
    covered by ``test_pg_full_migration_chain`` below. A disposable
    database is created and dropped around the test.
    """
    admin_url = _pg_admin_url()
    if not admin_url:
        pytest.skip('CMP_PG_TEST_URL not set (no disposable PostgreSQL server)')
    assert admin_url is not None  # narrowing only; skip() already bailed

    import uuid

    import psycopg

    db_name = f'cmp005a_reg_{uuid.uuid4().hex[:12]}'
    admin = psycopg.connect(admin_url, connect_timeout=10)
    admin.autocommit = True
    admin.execute(f'CREATE DATABASE {db_name}')
    admin.close()
    # Replace the last path segment (the existing database name) with the
    # disposable database name.
    base = admin_url.rstrip('/')
    url = base[:base.rfind('/')] + '/' + db_name
    try:
        con = psycopg.connect(url, connect_timeout=10)
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
                # user table exactly as 0001's upgrade would create it
                cur.execute('''CREATE TABLE "user" (
                    id SERIAL NOT NULL,
                    email VARCHAR(255) NOT NULL,
                    password_hash VARCHAR(256) NOT NULL,
                    created_at TIMESTAMP WITHOUT TIME ZONE
                        DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    email_verified BOOLEAN DEFAULT false NOT NULL,
                    status VARCHAR(20) DEFAULT 'active' NOT NULL,
                    PRIMARY KEY (id))''')
                cur.execute('CREATE UNIQUE INDEX ix_user_email '
                            'ON "user" (email)')
                # 0001's recipe ownership additions
                cur.execute('ALTER TABLE recipe ADD COLUMN user_id INTEGER')
                cur.execute('ALTER TABLE recipe ADD CONSTRAINT '
                            'fk_recipe_user_id_user FOREIGN KEY (user_id) '
                            'REFERENCES "user" (id)')
                cur.execute('CREATE INDEX ix_recipe_user_id '
                            'ON recipe (user_id)')
                # 0002's token columns
                cur.execute('ALTER TABLE "user" ADD COLUMN '
                            'verification_token_hash VARCHAR(64)')
                cur.execute('ALTER TABLE "user" ADD COLUMN '
                            'verification_token_expires_at '
                            'TIMESTAMP WITHOUT TIME ZONE')
                cur.execute('ALTER TABLE "user" ADD COLUMN '
                            'password_reset_token_hash VARCHAR(64)')
                cur.execute('ALTER TABLE "user" ADD COLUMN '
                            'password_reset_token_expires_at '
                            'TIMESTAMP WITHOUT TIME ZONE')
                cur.execute('CREATE TABLE alembic_version '
                            '(version_num VARCHAR(32) NOT NULL, '
                            'PRIMARY KEY (version_num))')
                cur.execute("INSERT INTO alembic_version VALUES ('0002')")
        con.close()

        cfg = Config(str(ALEMBIC_INI))
        cfg.set_main_option('script_location', str(MIGRATIONS_DIR))
        cfg.set_main_option('sqlalchemy.url', url)

        # This is the regression under test: 0002.downgrade() must quote
        # the reserved table name on PostgreSQL.
        command.downgrade(cfg, 'base')

        con = psycopg.connect(url, connect_timeout=10)
        try:
            with con.cursor() as cur:
                cur.execute("SELECT tablename FROM pg_tables "
                            "WHERE schemaname='public'")
                tables = {r[0] for r in cur.fetchall()}
                cur.execute("SELECT column_name FROM information_schema.columns "
                            "WHERE table_name='recipe'")
                cols = {r[0] for r in cur.fetchall()}
                cur.execute('SELECT id, title, image, date::text, description '
                            'FROM recipe ORDER BY id')
                rows = cur.fetchall()
                cur.execute('SELECT count(*) FROM alembic_version')
                nver = cur.fetchone()[0]
        finally:
            con.close()

        assert 'user' not in tables
        assert 'user_id' not in cols
        assert rows == [
            (r[0], r[1], r[2], r[3], r[4]) for r in LEGACY_ROWS]
        assert nver == 0
    finally:
        admin = psycopg.connect(admin_url, connect_timeout=10)
        admin.autocommit = True
        admin.execute(f'DROP DATABASE {db_name}')
        admin.close()


def test_pg_full_migration_chain():
    """Full real migration chain on PostgreSQL (CMP-005A-FIX2).

    No stamping and no pre-built schema: 0001's real CREATE TABLE runs
    (its status column default must be the quoted ``'active'`` string
    PostgreSQL accepts — the old ``sa.text('active')`` rendered the
    unquoted ``DEFAULT active`` that PostgreSQL rejected), then the
    complete upgrade -> downgrade base -> re-upgrade cycle is exercised
    with data, mirroring the SQLite tests above. A disposable database
    is created and dropped around the test.
    """
    admin_url = _pg_admin_url()
    if not admin_url:
        pytest.skip('CMP_PG_TEST_URL not set (no disposable PostgreSQL server)')
    assert admin_url is not None  # narrowing only; skip() already bailed

    import uuid

    import psycopg

    db_name = f'cmp005a_fix2_{uuid.uuid4().hex[:12]}'
    admin = psycopg.connect(admin_url, connect_timeout=10)
    admin.autocommit = True
    admin.execute(f'CREATE DATABASE {db_name}')
    admin.close()
    base = admin_url.rstrip('/')
    url = base[:base.rfind('/')] + '/' + db_name
    try:
        def q(sql, params=()):
            con = psycopg.connect(url, connect_timeout=10)
            try:
                with con.cursor() as cur:
                    cur.execute(sql, params)
                    return cur.fetchall()
            finally:
                con.close()

        # Legacy pre-auth state: recipe table with rows, no user table.
        con = psycopg.connect(url, connect_timeout=10)
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

        cfg = Config(str(ALEMBIC_INI))
        cfg.set_main_option('script_location', str(MIGRATIONS_DIR))
        cfg.set_main_option('sqlalchemy.url', url)

        # Real full-chain upgrade: 0001's CREATE TABLE executes here.
        command.upgrade(cfg, 'head')

        # 0001's fixed default: a quoted string literal.
        status_default = q(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name='user' AND column_name='status'")[0][0]
        assert status_default.startswith("'active'"), status_default
        # Legacy rows preserved and unowned; bookkeeping at head.
        rows = q('SELECT id, title, user_id FROM recipe ORDER BY id')
        assert [r[:2] for r in rows] == [
            (r[0], r[1]) for r in LEGACY_ROWS]
        assert all(r[2] is None for r in rows)
        assert q('SELECT version_num FROM alembic_version') == [('0002',)]

        # Representative data: a user and a recipe owned by that user.
        con = psycopg.connect(url, connect_timeout=10)
        with con:
            with con.cursor() as cur:
                cur.execute(
                    'INSERT INTO "user" (email, password_hash, '
                    'email_verified, status) VALUES (%s, %s, true, %s)',
                    ('carol@example.com',
                     'pbkdf2:scrypt:1:1:16:32:CCCCCCCCCCCCCCCCCCCCCCCC:hashc',
                     'active'))
        con.close()
        user_id = q('SELECT id FROM "user" WHERE email = %s',
                    ('carol@example.com',))[0][0]
        con = psycopg.connect(url, connect_timeout=10)
        with con:
            with con.cursor() as cur:
                cur.execute(
                    'INSERT INTO recipe (id, title, image, date, '
                    'description, user_id) '
                    'VALUES (99, %s, %s, %s, %s, %s)',
                    ('Carol Special', 'img99', '2026-11-09', 'owned',
                     user_id))
        con.close()
        assert q('SELECT count(*) FROM recipe')[0][0] == len(LEGACY_ROWS) + 1

        # Downgrade to base: user table gone, recipe rows survive.
        command.downgrade(cfg, 'base')
        tables = set(r[0] for r in q(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"))
        assert 'user' not in tables
        cols = set(r[0] for r in q(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='recipe'"))
        assert 'user_id' not in cols
        assert q('SELECT count(*) FROM recipe')[0][0] == len(LEGACY_ROWS) + 1

        # Re-upgrade: full schema back, every row unowned again.
        command.upgrade(cfg, 'head')
        assert q('SELECT version_num FROM alembic_version') == [('0002',)]
        rows = q('SELECT id, user_id FROM recipe ORDER BY id')
        assert len(rows) == len(LEGACY_ROWS) + 1
        assert all(r[1] is None for r in rows)
        status_default = q(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name='user' AND column_name='status'")[0][0]
        assert status_default.startswith("'active'"), status_default
    finally:
        admin = psycopg.connect(admin_url, connect_timeout=10)
        admin.autocommit = True
        admin.execute(f'DROP DATABASE {db_name}')
        admin.close()
