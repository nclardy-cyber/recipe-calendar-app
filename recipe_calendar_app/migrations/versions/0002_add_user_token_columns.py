"""add verification and password-reset token columns to user

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-12

Adds four nullable columns to the ``user`` table (from migration 0001):

* ``verification_token_hash`` - SHA-256 hex digest of the active
  single-use email verification token (raw tokens are never stored).
* ``verification_token_expires_at`` - UTC expiration of that token.
* ``password_reset_token_hash`` - SHA-256 hex digest of the active
  single-use password reset token.
* ``password_reset_token_expires_at`` - UTC expiration of that token.

SQLite + PostgreSQL compatible. The downgrade deliberately avoids batch
mode: a SQLite batch rebuild re-emits the reflected ``status`` string
default without quotes (``DEFAULT (active)``), which SQLite rejects as
"not constant". Native ``ALTER TABLE ... DROP COLUMN`` is supported by
PostgreSQL and by SQLite >= 3.35 (every Python 3.11+ runtime ships newer).

Ordering-robust: if the ``user`` table does not exist yet (0001 not
applied), the migration is a no-op rather than failing; if the app's
``db.create_all()`` already created the token columns, the upgrade adds
only what is missing.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

# (column name, type) — dropped in reverse order on downgrade.
TOKEN_COLUMNS = [
    ('verification_token_hash', sa.String(length=64)),
    ('verification_token_expires_at', sa.DateTime()),
    ('password_reset_token_hash', sa.String(length=64)),
    ('password_reset_token_expires_at', sa.DateTime()),
]


def _existing_columns(table):
    return {c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if 'user' not in inspector.get_table_names():
        return
    existing = _existing_columns('user')
    missing = [(name, coltype) for name, coltype in TOKEN_COLUMNS
               if name not in existing]
    if missing:
        with op.batch_alter_table('user') as batch_op:
            for name, coltype in missing:
                batch_op.add_column(sa.Column(name, coltype, nullable=True))


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if 'user' not in inspector.get_table_names():
        return
    existing = _existing_columns('user')
    for name, _coltype in reversed(TOKEN_COLUMNS):
        if name in existing:
            # Column names above are fixed literals — no interpolation risk.
            # The table name must be quoted: ``user`` is a PostgreSQL
            # reserved word and unquoted DDL on it fails on PG (it only
            # works silently on SQLite).
            op.execute('ALTER TABLE "user" DROP COLUMN ' + name)
