# Database Migrations (CMP-003A + CMP-003B + CMP-005C)

This app uses **Flask-Migrate / Alembic** for schema changes. All migration
files live in `migrations/`; the Alembic configuration is `alembic.ini`.

## Supported databases

Migrations are written for **both SQLite and PostgreSQL**:

- Column/table definitions use portable Alembic/SQLAlchemy types
  (`sa.String`, `sa.DateTime`, `sa.Boolean`, `sa.Integer`).
- The `recipe` ALTER (adding `user_id` + foreign key) runs in **batch mode**
  (`op.batch_alter_table`), which rebuilds the table on SQLite — where
  `ALTER TABLE ADD COLUMN ... REFERENCES` is not supported in place — and is
  also fully supported on PostgreSQL. The ALTER is guarded by a table
  existence check: on a **fresh** database the `recipe` table does not exist
  yet, so 0001's ALTER is skipped and `0003` creates the table directly with
  `user_id` already present (see the chain below).
- Server defaults use portable expressions (`CURRENT_TIMESTAMP`, `false`,
  text literal `'active'`) accepted by both engines.
- The `0002` downgrade (dropping the four token columns) uses native
  `ALTER TABLE user DROP COLUMN` instead of batch mode: a SQLite batch
  rebuild re-emits the reflected `status` string default without quotes
  (`DEFAULT (active)`), which SQLite rejects as "not constant". Native
  `DROP COLUMN` requires SQLite >= 3.35 (every Python 3.11+ runtime ships
  newer) and works as-is on PostgreSQL.

The live database is selected via `SQLALCHEMY_DATABASE_URI`, which the app
derives from the `DATABASE_URL` environment variable (Postgres in
production, local SQLite in development).

## Current migration chain

| Revision | Description |
|----------|-------------|
| `0001`   | Adds the `user` table (unique normalized email, password hash, `created_at`, `email_verified`, `status`) and adds nullable `recipe.user_id` + FK to `user.id`. Existing recipe rows keep `user_id = NULL` (legacy/unowned). |
| `0002`   | Adds four nullable token columns to `user`: `verification_token_hash`, `verification_token_expires_at`, `password_reset_token_hash`, `password_reset_token_expires_at`. Only SHA-256 hashes are stored — raw tokens never touch the database. |
| `0003`   | Creates the `recipe` table on truly empty databases (CMP-005C) — full current shape: `id` PK, `title`, `image`, `date`, `description`, `user_id` FK → `user.id` (nullable) + `ix_recipe_user_id`. On every pre-existing database the table already exists at this shape (created historically by the app's `db.create_all()`, then shaped by 0001/0002), so the migration is a no-op there. Its `downgrade()` is a deliberate no-op (see Rollback). |

## Upgrade

```bash
# From the app directory (recipe_calendar_app/), with DATABASE_URL set:
python -m flask db upgrade            # apply all pending migrations
python -m flask db upgrade 0001       # apply up to a specific revision
python -m flask db current            # show the current revision
python -m flask db history            # show the revision chain
```

Equivalent standalone form (uses `sqlalchemy.url` from alembic.ini or a
Config override, no Flask app involved):

```bash
alembic -c alembic.ini upgrade head
```

`db.create_all()` now runs at startup in **development only** (it only ever
creates missing tables on a fresh local database; it never alters or drops
existing tables, and migration `0003` makes an Alembic-migrated database
converge to the same shape, so both paths agree). **Production never runs
`create_all()`** (CMP-005C): the schema is owned exclusively by the
migration chain and is applied by the pre-start step below. For databases
that already have a `recipe` table, **always use the migrations above**
(`flask db upgrade`) to add new schema.

## Production (Render) procedure (CMP-005C)

The Render start command (the `web` line of the `Procfile`) is:

```bash
python -m flask db upgrade && gunicorn -c gunicorn.conf.py --bind 0.0.0.0:$PORT app:app
```

* Migrations run **before** the server starts, on every deploy, against
  `DATABASE_URL`. On a brand-new Postgres this creates the complete schema
  (`user`, `recipe`, `alembic_version` at head `0003`); on an existing
  database it applies only pending revisions (a no-op when current).
* If `flask db upgrade` fails (bad connection, migration error), the `&&`
  chain stops and the instance never starts: Render's health check then
  fails, the deploy is cancelled, and the previous version keeps serving. A
  migration failure can never leave a half-migrated database serving
  traffic.
* As a backstop, the app also refuses to serve application traffic until the
  database reports the chain head: `/health/ready` returns 503 and every
  non-health route returns 503 (see README "Health checks").

## Rollback

```bash
python -m flask db downgrade -1      # roll back the last applied migration
python -m flask db downgrade base    # roll everything back to an empty schema
python -m flask db downgrade 0001    # roll back to a specific revision
```

`0003`'s `downgrade()` is a deliberate **no-op**: dropping the recipe table
would destroy pre-existing user data, which the rest of the chain never
does. On a database created entirely by the chain, the table therefore
survives `downgrade base` in its pre-chain (legacy) shape — empty of the
chain's own data model but present — which keeps
upgrade → downgrade → upgrade lossless (rows survive; `user_id` is dropped
by `0001`'s downgrade and re-added as NULL on re-upgrade). `0002`'s
`downgrade()` drops the four token columns (invalidating any pending
verification/reset tokens). `0001`'s `downgrade()` removes
`recipe.user_id` (with its FK) and the `user` table. **Warning:** rolling
back after users have been created or recipes have been assigned to users
destroys that data. Back up the database first (see below).

## Pre-migration backup procedure (required)

Before running any migration against a database that contains real data:

```bash
# SQLite: consistent copy (close the app or use sqlite3's .backup)
sqlite3 db/calendar.db ".backup /backups/calendar.db.pre-0001"
# or, if the app is stopped:
cp db/calendar.db /backups/calendar.db.pre-0001
sha256sum /backups/calendar.db.pre-0001   # record the hash for verification
```

For PostgreSQL:

```bash
pg_dump "$DATABASE_URL" -f /backups/calendar.pre-0001.sql
sha256sum /backups/calendar.pre-0001.sql
```

After upgrading, verify row counts are unchanged:

```bash
sqlite3 db/calendar.db "select count(*) from recipe;"
sqlite3 db/calendar.db "select count(*) from recipe where user_id is null;"
```

## Migration testing

`tests/test_migrations.py` exercises the migration chain against
**disposable SQLite databases only** (temp files, never the project's real
databases):

- upgrade on a completely empty database (user AND recipe tables — the
  production contract: `flask db upgrade` alone produces the complete
  schema),
- the `recipe` table created by `0003` matches the `Recipe` model
  column-for-column (dev boot via `create_all()` and production via
  migrations converge to the same schema),
- downgrade from a fresh upgrade is lossless (recipe rows survive;
  `user_id` drops and returns as NULL),
- upgrade on a legacy schema with no rows,
- upgrade on a legacy database with pre-existing recipe rows
  (rows preserved, `user_id` stays NULL),
- upgrade after the app booted first (startup `db.create_all()` pre-created
  the `user` table, or both tables at their full current shape — the
  migrations no-op cleanly instead of failing with duplicate columns),
- full downgrade back to `base` (schema reverts, rows preserved),
- `alembic_version` bookkeeping (asserted against the dynamic head).

Run them with the test venv:

```bash
.venv-test/Scripts/python.exe -m pytest tests/ -v
```

PostgreSQL is verified by live tests in the same suite: `tests/test_migrations.py`
runs the real migration chain (`upgrade head` → `downgrade base` →
re-upgrade, row preservation, and the reserved-`user`-table downgrade)
against **disposable databases on a disposable embedded PostgreSQL server**
that the test session starts locally on 127.0.0.1 (`embedded_postgres`,
installed from `requirements-dev.txt`; the server's data directory is
temporal and dropped after the session). No external or production database
is ever touched, and when the package is not installed those tests skip
cleanly, so the default suite stays dependency-free.

## Constraints honored by this design

- Legacy recipes are **never** assigned to an account automatically;
  `recipe.user_id` is nullable and defaults to NULL for all pre-existing
  rows.
- No migration touches existing recipe rows (no `UPDATE`, no data
  reassignment) — schema-only changes.
