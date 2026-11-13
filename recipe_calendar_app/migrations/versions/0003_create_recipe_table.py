"""create recipe table on fresh databases

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-13

Makes ``flask db upgrade`` alone produce the complete production schema
on a truly empty database (CMP-005C).

Before this migration, a fresh database upgraded to head had the ``user``
table and bookkeeping but no ``recipe`` table: the recipe table only ever
appeared through the app's startup ``db.create_all()``. Production must
not run ``create_all()`` (it would silently bypass Alembic and could
diverge from the migration chain), so without this revision a fresh
PostgreSQL database on a new host could never serve recipes.

Upgrade:
* If the ``recipe`` table already exists (every pre-existing database,
  including ones shaped by the app's historical ``create_all()``), this
  migration is a no-op - 0001 has already added ``user_id`` and 0002 the
  token columns, so the table is already at its full current shape.
* If the ``recipe`` table is absent (a truly empty database), it is
  created with exactly the model's current shape:
  ``id`` PK, ``title`` NOT NULL, ``image``, ``date``, ``description``,
  ``user_id`` FK -> ``user.id`` (nullable, unowned rows allowed) and the
  ``ix_recipe_user_id`` index.

Downgrade:
* Deliberate no-op. The recipe table (and its ``user_id`` column/index)
  predates this migration on every real database, and 0001's downgrade
  already reverts the recipe ownership additions (drops ``user_id`` and
  its index) while keeping the table. Dropping the table here would
  destroy pre-existing user data on downgrade, which the rest of the
  chain never does. On a database that was created entirely by the
  chain (fresh database), the recipe table therefore survives a full
  downgrade to base in its pre-chain (legacy) shape - empty of the
  chain's own data model but present, which is the project's true base
  state and keeps the upgrade -> downgrade -> upgrade cycle lossless.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if 'recipe' in inspector.get_table_names():
        # Pre-existing database: the table is already at (or ahead of)
        # the shape this migration would create. Nothing to do.
        return
    # Truly empty database: create the recipe table exactly as the
    # SQLAlchemy model defines it (mirrors app.Recipe).
    op.create_table(
        'recipe',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=100), nullable=False),
        sa.Column('image', sa.String(length=200), nullable=True),
        sa.Column('date', sa.Date(), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'],
                                name='fk_recipe_user_id_user'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_recipe_user_id', 'recipe', ['user_id'], unique=False)


def downgrade():
    # No-op by design (see module docstring): 0001's downgrade reverts the
    # recipe ownership additions, and dropping the table would destroy
    # pre-existing user data.
    op.execute('SELECT 1')
