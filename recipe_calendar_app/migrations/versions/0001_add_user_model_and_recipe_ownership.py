"""Add public user accounts and nullable recipe ownership.

Revision ID: 0001
Revises:
Create Date: 2026-11-12

Creates the ``user`` table (unique normalized email, salted password hash,
creation timestamp, verification flag, account status) and adds a nullable
``user_id`` ownership column to ``recipe``.

Existing recipe rows are left untouched: their ``user_id`` stays NULL,
meaning "legacy/shared, owned by no account".

Ordering-robust: the app runs ``db.create_all()`` at startup, so the
``user`` table (and even a fully-featured ``recipe`` table) may already
exist when this migration runs (boot-before-migration ordering). Table,
column, and index existence are checked before every DDL step, so both
orderings complete cleanly and a re-run is a no-op.

SQLite + PostgreSQL compatible: the recipe ALTER runs in batch mode, which
SQLite requires (table rebuild) and PostgreSQL also supports.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(table: str) -> set:
    return {c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _index_names(table: str) -> set:
    return {i['name'] for i in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    table_names = set(inspector.get_table_names())

    # user table (skipped if the app's startup db.create_all() already
    # created it — same schema, same column definitions)
    if 'user' not in table_names:
        op.create_table(
            'user',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('email', sa.String(length=255), nullable=False),
            sa.Column('password_hash', sa.String(length=256), nullable=False),
            sa.Column('created_at', sa.DateTime(),
                      server_default=sa.text('CURRENT_TIMESTAMP'),
                      nullable=False),
            sa.Column('email_verified', sa.Boolean(),
                      server_default=sa.false(), nullable=False),
            # Plain-string server default (matches the model): SQLAlchemy
            # renders it as ``DEFAULT 'active'`` — the string is quoted,
            # which PostgreSQL requires. (``sa.text('active')`` rendered the
            # unquoted ``DEFAULT active`` that PostgreSQL rejects.)
            sa.Column('status', sa.String(length=20),
                      server_default='active', nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(op.f('ix_user_email'), 'user', ['email'],
                        unique=True)

    # Add ownership to an EXISTING recipe table (legacy upgrade path).
    # On a fresh database the recipe table does not exist yet; it is created
    # later by the app's db.create_all() (or a subsequent migration) already
    # containing user_id. If the app booted first and create_all() already
    # created user_id + its FK + index, every step below is skipped.
    if 'recipe' in table_names:
        if 'user_id' not in _column_names('recipe'):
            with op.batch_alter_table('recipe') as batch_op:
                batch_op.add_column(sa.Column('user_id', sa.Integer(),
                                              nullable=True))
                batch_op.create_foreign_key(
                    'fk_recipe_user_id_user', 'user', ['user_id'], ['id'])
        if 'ix_recipe_user_id' not in _index_names('recipe'):
            op.create_index(op.f('ix_recipe_user_id'), 'recipe',
                            ['user_id'], unique=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    table_names = set(inspector.get_table_names())

    if 'recipe' in table_names:
        recipe_cols = _column_names('recipe')
        index_names = _index_names('recipe')
        if 'user_id' in recipe_cols:
            if 'ix_recipe_user_id' in index_names:
                op.drop_index(op.f('ix_recipe_user_id'),
                              table_name='recipe')
            # Drop every FK on recipe that references user, whether created
            # by this migration (named) or by the app's db.create_all()
            # (PostgreSQL auto-names it; SQLite inlines it and the batch
            # rebuild below removes it).
            fk_names = [fk['name'] for fk in
                        inspector.get_foreign_keys('recipe')
                        if fk.get('referred_table') == 'user'
                        and fk.get('name')]
            with op.batch_alter_table('recipe') as batch_op:
                for fk_name in fk_names:
                    batch_op.drop_constraint(fk_name, type_='foreignkey')
                batch_op.drop_column('user_id')
        elif 'ix_recipe_user_id' in index_names:
            # Column already gone but its index survived (partial state).
            op.drop_index(op.f('ix_recipe_user_id'), table_name='recipe')

    if 'user' in table_names:
        if 'ix_user_email' in _index_names('user'):
            op.drop_index(op.f('ix_user_email'), table_name='user')
        op.drop_table('user')
