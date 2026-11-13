# Recipe Calendar App

A Flask-based recipe calendar web application.

## Deployment

This app is configured for deployment on Render.com

### Root Directory
`recipe_calendar_app`

### Build Command
`pip install -r requirements.txt`

### Start Command
The `web` process in the `Procfile` runs, from the app root directory
(`recipe_calendar_app`), where `app.py` defines the `app` object:

```
python -m flask db upgrade && gunicorn -c gunicorn.conf.py --bind 0.0.0.0:$PORT app:app
```

* `python -m flask db upgrade` applies any pending Alembic migrations to
  `DATABASE_URL` **before** the server starts (see "Migrations at deploy
  time" below). If it fails, the chain stops and the instance never starts,
  so Render cancels the deploy and the previous version keeps serving.
* `gunicorn -c gunicorn.conf.py --bind 0.0.0.0:$PORT app:app` serves the app
  with the worker policy in `gunicorn.conf.py` (2 sync workers, 60 s request
  and graceful-shutdown timeouts). `$PORT` is the port assigned by Render;
  binding `0.0.0.0:$PORT` is required for Render to route traffic to the
  instance.

> Gunicorn does not run the Werkzeug dev server, and `app.debug` stays `False`
> unless `FLASK_DEBUG=1` is set explicitly in development — so the Werkzeug
> debugger can never be enabled in production. Running `python app.py` locally
> binds to `127.0.0.1` (loopback) only.

### Environment variables (production)

The following are **required in production** (i.e. when `FLASK_ENV=production`
is set): the app validates them at startup and **refuses to start**, listing
every missing or invalid variable in one message. Set them in Render's
environment settings. Never commit real credentials (`.env*` files are
git-ignored).

| Variable | Purpose | Validation |
|---|---|---|
| `SECRET_KEY` | Flask session signing key | Present, ≥ 32 characters, never the dev fallback. Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Never logged. |
| `FLASK_ENV` | Switches the app into production mode (activates all checks below) | `production` on Render |
| `DATABASE_URL` | PostgreSQL connection string, e.g. `postgres://user:***@host:5432/dbname` — use Render Postgres' **internal** connection string (the external one is rejected by Render's network for same-region services) | Must be a `postgres://`, `postgresql://` or `postgresql+psycopg://` URL. Required: an unset value would silently fall back to a local SQLite file, which Render's ephemeral filesystem discards on every deploy |
| `REDIS_URL` | Shared rate-limit storage | Must use the `redis://` or `rediss://` scheme. Rate limiting runs on shared Redis, never per-process in-memory, in production |
| `SMTP_HOST` | Mail server for verification/reset emails | Required, non-empty |
| `SMTP_PORT` | Mail server port | Must be an integer between 1 and 65535 (default `587`). Use `587` for STARTTLS or `465` for implicit TLS. |
| `SMTP_USER` | SMTP account | Required, non-empty |
| `SMTP_PASSWORD` | SMTP account password | Required, non-empty. Never logged |
| `APP_BASE_URL` | Base URL used in emailed links | Must start with `https://` — emailed links must use HTTPS |
| `FLASK_DEBUG` | (development only) `1` enables the Werkzeug debugger when running `python app.py` locally | **Never set in production** |

`PORT` is provided by Render itself and is not configured here — the start
command binds it directly.

### Migrations at deploy time

The schema is owned exclusively by the Alembic chain (see `MIGRATIONS.md`);
production never runs the app's `db.create_all()`. The pre-start
`flask db upgrade` in the start command is the explicit migration step:

* fresh Postgres → creates the complete schema at head (`0003`);
* existing database → applies only pending revisions (a no-op when current).

Failure semantics: an upgrade error stops the `&&` chain, the instance never
binds, the health check fails and Render cancels the deploy — the previous
version keeps serving. A partially applied migration can never serve
traffic: until the database reports the chain head, `/health/ready` returns
503 and the app refuses all non-health routes.

### Health checks

| Endpoint | Meaning |
|---|---|
| `GET /health` | Liveness: the process is up and answering. Never touches the database. |
| `GET /health/ready` | Readiness: safe to route traffic to. Development — database reachable. Production — database reachable **and** at the migration head. |

Point Render's HTTP health check at **`/health/ready`**: a deploy whose
instances never report ready is cancelled and the previous version keeps
serving. Failure responses are coarse on purpose —
`{"status": "unavailable", ...}` with no connection strings, exception text
or schema details (the server-side log carries only the exception type).
Rate-limited endpoints (signup/login/reset, POST) additionally fail closed
with 500 while Redis is unreachable, so an outage never disables limiting.

### Backup and recovery

* The Render service filesystem is **ephemeral**: everything not in the
  `DATABASE_URL` database (and the git checkout) is discarded on every
  deploy or restart. All durable state lives in the Render Postgres
  instance — do not rely on files written at runtime.
* Back up the database with
  `pg_dump "$DATABASE_URL" -f calendar-<date>.sql` from anywhere that can
  reach the Postgres connection string, and verify a restore before relying
  on the backup.
* The local SQLite databases (`db/calendar.db`, `instance/calendar.db`) are
  development-only, are not tracked in git, and never exist on Render.

## Local development

```
pip install -r requirements.txt
python app.py        # serves http://127.0.0.1:5000
```

## Tests

```
pip install -r requirements-dev.txt
pytest
```

Tests run against temporary SQLite databases in temp directories — they
never touch `db/calendar.db` or `instance/calendar.db`. The PostgreSQL tests
additionally spin up a **disposable embedded PostgreSQL server** locally
(`embedded_postgres` + `psycopg`, from `requirements-dev.txt`) with one
throwaway database per test; when those packages are not installed the
affected tests skip cleanly, so the suite stays runnable with the minimal
dependencies.
