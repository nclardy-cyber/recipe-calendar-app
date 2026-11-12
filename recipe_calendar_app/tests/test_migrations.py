"""Alembic migration tests against disposable SQLite databases.

Every test points Alembic at a fresh temp-file database. The project's
real databases (db/calendar.db, instance/calendar.db) are never opened.
"""
import sqlite3

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
