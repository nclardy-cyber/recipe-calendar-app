# Database Migrations (CMP-003A + CMP-003B)

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
  yet (it is created later by the app's `db.create_all()`, which already
  includes `user_id`), so the migration only creates the `user` table.
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

The app also runs `db.create_all()` at startup, which only ever **creates
missing tables** on a fresh database; it never alters or drops existing
tables. For databases that already have a `recipe` table, **always use the
migrations above** (or `flask db upgrade`) to add the new schema.

## Rollback

```bash
python -m flask db downgrade -1      # roll back the last applied migration
python -m flask db downgrade base    # roll everything back to an empty schema
python -m flask db downgrade 0001    # roll back to a specific revision
```

`0002`'s `downgrade()` drops the four token columns (invalidating any
pending verification/reset tokens). `0001`'s `downgrade()` removes
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

- upgrade on a completely empty database (user table only),
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

PostgreSQL compatibility is ensured by the portable constructs above; a live
PostgreSQL instance is not available in the dev environment, so the first
`flask db upgrade` against the production Postgres (at deploy time) is the
remaining live verification step.

## Constraints honored by this design

- Legacy recipes are **never** assigned to an account automatically;
  `recipe.user_id` is nullable and defaults to NULL for all pre-existing
  rows.
- No migration touches existing recipe rows (no `UPDATE`, no data
  reassignment) — schema-only changes.
