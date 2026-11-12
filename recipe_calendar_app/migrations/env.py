"""Alembic environment for the recipe calendar app.

Database URL resolution:
  - Under Flask (``flask db migrate/upgrade``): the app's
    ``SQLALCHEMY_DATABASE_URI`` is used (honoring ``DATABASE_URL``).
  - Standalone (tests, scripts): the ``sqlalchemy.url`` set on the
    Alembic Config is used. Never hardcode a production URL here.
"""
from alembic import context
from flask import current_app, has_app_context
from sqlalchemy import engine_from_config, pool

config = context.config

if has_app_context():
    config.set_main_option(
        'sqlalchemy.url',
        current_app.config.get('SQLALCHEMY_DATABASE_URI', ''))

# Metadata for autogenerate; only needed under Flask (flask db migrate).
target_metadata = None
if has_app_context():
    target_metadata = current_app.extensions['migrate'].db.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Batch mode keeps autogenerate output SQLite-compatible
        # (recreates tables for ALTERs SQLite cannot do in place).
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (with a live connection)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
