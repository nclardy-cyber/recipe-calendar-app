# CMP-007 — Production Deployment Runbook

**Service:** CalendarMealPlanner.com — Flask recipe calendar app on Render
**Prepared:** 2026-11-13
**Evidence base:** commit `c3e820b` (CMP-006B-DOCFIX) on branch
`feature/cmp-003-public-auth`; ancestors include CMP-006A (`16d069f`) and
CMP-005C (`e92179c`).

> **Preparation only.** Nothing in this runbook has been executed against
> Render, DNS, any database, or any mail server. No services, databases,
> Redis instances, SMTP accounts, or DNS records were created or modified.
> Every external fact below that has not been observed live is explicitly
> marked **NOT VERIFIED**.

---

## 1. What was verified for this runbook

Verified by inspection of the repository at `c3e820b` (no live systems):

| Fact | Evidence |
|---|---|
| Start command is `python -m flask db upgrade && gunicorn -c gunicorn.conf.py --bind 0.0.0.0:$PORT app:app` | `recipe_calendar_app/Procfile` |
| Gunicorn: 2 sync workers, 60 s timeout, 60 s graceful timeout | `recipe_calendar_app/gunicorn.conf.py` |
| Build is `pip install -r requirements.txt`; all deps pinned | `recipe_calendar_app/requirements.txt` |
| Transitively resolved SQLAlchemy is 2.1.8, `Requires-Python >= 3.11` | `.venv-test/Lib/site-packages/sqlalchemy-2.1.8.dist-info/METADATA` |
| Production startup gate validates all required env vars and refuses to start on any gap | `app.py` `_validate_production_environment` (lines 82–148), `_resolve_secret_key` (lines 55–79) |
| `TRUSTED_PROXY_HOPS`: optional non-negative int; default 1 (production) / 0 (development); invalid value raises `RuntimeError` at import (startup fails) | `app.py` lines 542–561, called at module load |
| `/health` = liveness (no DB); `/health/ready` = readiness (production: DB reachable **and** `alembic_version` == chain head `0003`) | `app.py` lines 1400–1537 |
| Production serves **no** non-health route until ready (503 backstop, registered before CSRF) | `app.py` `_enforce_production_readiness` (lines 166–185) |
| Migration chain: `0001` (user table + `recipe.user_id` FK) → `0002` (token columns) → `0003` (recipe table on fresh DB); head = `0003`; schema-only, never rewrites existing recipe rows | `recipe_calendar_app/MIGRATIONS.md`, `migrations/versions/0001..0003` |
| Migrations run once per instance start, before gunicorn, against `DATABASE_URL`; failure stops the `&&` chain → Render cancels the deploy, previous version keeps serving | `Procfile`, `MIGRATIONS.md` "Production (Render) procedure" |
| Session cookies: `HttpOnly`, `SameSite=Lax`, `Secure` in production; remember-me = 12 h persistent session; `SESSION_PROTECTION=basic` | `app.py` lines 202–224 |
| CSRF on all state-changing requests (session-bound, 1 h TTL; JSON API uses `X-CSRFToken` header) | `app.py` lines 187–191 |
| Rate limits (POST only, Redis-backed in production, fail-closed 500 if Redis is down): `/signup` 3/h, `/login` 5/15 min, `/resend-verification` 3/h, `/reset-password` 3/h | `app.py` lines 660, 713, 784, 831; limiter config lines 596–641 |
| Recipe visibility: guests get 401 on the API; a user sees only rows with `user_id == their own id`; id lookup of another user's recipe, a legacy (unowned, `user_id IS NULL`) recipe, or a missing id all return the **identical 404** | `app.py` lines 361–392, 1094–1098 |
| Recipe titles: create paths truncate to 100 chars; `PUT` rejects >100 with 400 | `app.py` lines 1127–1177 |
| SMTP: plain `smtplib.SMTP` connect, then `starttls()` when `SMTP_USE_TLS` is true (default) — the code never uses `SMTP_SSL`, so **implicit TLS (port 465) is not supported**; STARTTLS on 587 is the supported production mode | `app.py` `send_email` lines 450–471 |
| `SMTP_FROM` default: `CalendarMealPlanner <no-reply@calendarmealplanner.com>` (env-overridable) | `app.py` lines 250–251 |
| No `render.yaml` / `render.json` anywhere in the repository — Render service configuration is **dashboard-only** | repository listing at `c3e820b` |

Not verified (nothing external was touched): Render account state, any live
service, DNS records, the domain's TLS certificate, Postgres/Redis/SMTP
connectivity, and Render's actual proxy/forwarding behavior.

---

## 2. Target architecture

Three managed pieces on one Render account, same region:

```
Internet ──> [Render LB / TLS termination] ──> Web service (1 instance)
                                                      │
                                   gunicorn (2 sync workers)
                                                      │
                          ┌───────────────────────────┼─────────────────────────┐
                          ▼                           ▼                         ▼
                 Managed PostgreSQL            Managed Redis         SMTP provider
                 (app data; internal URL)      (shared rate-limit   (verification /
                                                storage; rediss://)  reset mail)
```

* **Web service** — the app in `recipe_calendar_app/`, exactly **one**
  instance, autoscaling disabled (see §3, single-instance requirement).
* **Managed PostgreSQL** — Render Postgres; all durable state. The service
  filesystem is ephemeral and is never relied on for data.
* **Managed Redis** — Render Redis; shared rate-limit storage. Must use
  `rediss://` (TLS) when reachable only internally, or the URL Render
  provides for the internal connection.
* **SMTP** — external mail provider account (e.g. a transactional email
  service) used for verification and password-reset mail.
* **Custom domain** — `calendarmealplanner.com` with TLS terminated by
  Render; HTTPS only.

### Dashboard vs. repository settings

There is **no `render.yaml` in the repository**, so **every Render service
setting lives in the Render dashboard**; the repository contributes only the
build/start artifacts:

| Setting | Where | Value |
|---|---|---|
| Root directory | Dashboard (web service) | `recipe_calendar_app` |
| Runtime | Dashboard (web service) | Python **3.12** (pinned — see §3) |
| Build command | Dashboard (web service) | `pip install -r requirements.txt` |
| Start command | **Repository** — `Procfile` (`web:` line); Render auto-detects the Procfile | `python -m flask db upgrade && gunicorn -c gunicorn.conf.py --bind 0.0.0.0:$PORT app:app` |
| Worker policy | **Repository** — `gunicorn.conf.py` | 2 sync workers, 60 s timeouts |
| Health check | Dashboard (web service) | HTTP `GET /health/ready` |
| Instance count / autoscaling | Dashboard (web service) | 1 / off |
| Environment variables | Dashboard (web service) | §4 — never in the repository (`.env*` is git-ignored) |
| Postgres / Redis instances | Dashboard (databases) | Created separately; connection URLs referenced by `DATABASE_URL` / `REDIS_URL` |
| Custom domain + TLS | Dashboard (web service → Custom domains) | `calendarmealplanner.com` (and `www` variant if wanted) |

Do not invent or transcribe service IDs, hostnames, URLs, or credentials into
this document or into the repository: they exist only in the Render
dashboard and are referenced here as placeholders.

---

## 3. Render settings detail

### 3.1 Web service

* **Type:** Web Service, root directory `recipe_calendar_app`.
* **Python runtime:** **3.12**, explicitly pinned in the service's Runtime
  settings. **≥ 3.11 is the hard minimum**: the transitive SQLAlchemy
  dependency currently resolves to 2.1.x, which declares
  `Requires-Python >= 3.11`; an older runtime fails the build. Do not leave
  the runtime on a default that could silently change.
* **Build:** `pip install -r requirements.txt` (all pins in
  `requirements.txt` — Flask 3.1.3, Werkzeug 3.1.9, gunicorn 27.0.1,
  Flask-SQLAlchemy 3.1.1, Flask-Login 0.6.3, Flask-Migrate 4.1.0,
  Flask-Limiter 4.1.1, Flask-WTF 1.3.0, alembic 1.20.1, psycopg[binary]
  3.3.9, redis 5.2.1).
* **Start:** the Procfile `web` line (above). Migrations run at instance
  start, before gunicorn; on failure the instance never binds and Render
  cancels the deploy (previous version keeps serving).
* **Instances: exactly 1. Autoscaling: disabled.**
  **Single-instance requirement:** each instance runs `flask db upgrade` at
  startup and **no migration concurrency lock exists** in the Alembic chain —
  two instances starting together can race on the same migrations. Do not
  enable more instances or autoscaling until migration execution is moved to
  a separately controlled, single-run operation (e.g. a one-off Render job
  that runs before any web instance starts).
* **Health check:** HTTP **`GET /health/ready`**. Point Render's HTTP health
  check at this path (not `/health`). Expected ready body:
  `{"status": "ok", "database": "ok", "schema": "ok"}`. A deploy whose
  instance never reports ready is cancelled and the previous version keeps
  serving.
* **Instance type:** smallest plan that is comfortable (the app is small; 2
  sync workers). Revisit only after live load evidence — **NOT VERIFIED**
  that any specific plan is sufficient.
* **Zero-downtime deploys:** on by default; combined with the readiness gate
  above.

### 3.2 Managed PostgreSQL

* Create an **empty** managed Postgres database (no schema) in the same
  region as the web service.
* Use its **internal** connection string for `DATABASE_URL` (same-region
  Render services reach the internal host; the external string is rejected by
  Render's network). **Which exact URL applies to this account: NOT
  VERIFIED — read it from the dashboard at creation time.**
* The migration chain owns the schema: the first deploy's `flask db upgrade`
  creates `user`, `recipe`, and `alembic_version` at head `0003`. The app
  never runs `create_all()` in production.
* Enable/keep the provider's automated backups **in addition to** the manual
  `pg_dump` procedure in §6.

### 3.3 Managed Redis

* Create a managed Redis instance in the same region.
* `REDIS_URL` must be `redis://` or `rediss://`; prefer the internal/TLS URL
  Render provides. **Exact URL: NOT VERIFIED — read from the dashboard.**
* The app connects lazily and runs rate limiting with
  `in_memory_fallback_enabled=False`, `swallow_errors=False`: while Redis is
  unreachable, rate-limited endpoints fail closed with 500 (unlimited
  endpoints keep working).

### 3.4 HTTPS and custom domain

* Add `calendarmealplanner.com` (and optionally `www.calendarmealplanner.com`)
  as custom domains in the web service's dashboard settings; Render provisions
  the TLS certificate.
* DNS (set at the registrar, **outside Render**, by the operator — not done
  in this task): the record values Render shows for the domain (typically an
  A record or CNAME). **Record values: NOT VERIFIED — take them from the
  dashboard after the domain is attached.**
* Expected behavior: all app links and emails use `https://`
  (`APP_BASE_URL` must be `https://`); session cookies carry `Secure`;
  `PREFERRED_URL_SCHEME` is `https` in production.
* Redirect/HTTPS-enforcement options (e.g. force HTTPS, redirect `http→https`)
  are dashboard settings — choose enforcement ON. **Current state: NOT
  VERIFIED.**

---

## 4. Environment variables (web service, Render dashboard)

The production startup gate validates these at import time; **any** missing
or invalid value refuses to start, listing every problem in one message.
`FLASK_ENV` must be `production` (any value other than `development`/`dev`
already counts as production, but set it explicitly).

| Variable | Required | Format / validation | Purpose | Value source (operator supplies at execution) |
|---|---|---|---|---|
| `FLASK_ENV` | yes | `production` | Production mode: activates every gate, `Secure` cookies, HTTPS URL scheme | Fixed literal |
| `SECRET_KEY` | yes | ≥ 32 chars; never the dev fallback key. Generate **locally**: `python -c "import secrets; print(secrets.token_urlsafe(32))"` | Flask session signing | Generated once, pasted into dashboard; never committed, never logged, never shared in chat/tickets |
| `DATABASE_URL` | yes | `postgres://user:***@host:port/dbname` (or `postgresql://`, `postgresql+psycopg://`) | App database | Render Postgres **internal** connection string (dashboard → Postgres → connection info) |
| `REDIS_URL` | yes | `redis://…` or `rediss://…` | Shared rate-limit storage | Render Redis internal/TLS connection URL (dashboard) |
| `SMTP_HOST` | yes | non-empty | Mail server for verification/reset email | SMTP provider's dashboard |
| `SMTP_PORT` | yes (validated; default 587) | integer 1–65535 | SMTP port. Use **587 + STARTTLS** (the code uses `starttls()` and never `SMTP_SSL` — port 465 implicit TLS is **not supported**) | SMTP provider docs |
| `SMTP_USER` | yes | non-empty | SMTP account | SMTP provider dashboard; never logged |
| `SMTP_PASSWORD` | yes | non-empty | SMTP account password/app password | SMTP provider dashboard; never logged; prefer an app-specific password if the provider offers one |
| `APP_BASE_URL` | yes | must start with `https://` | Base URL for emailed links (verification, reset) | `https://calendarmealplanner.com` (final domain form confirmed at execution) |
| `TRUSTED_PROXY_HOPS` | optional | non-negative integer; **invalid values (non-integer or negative) prevent startup** | Trusted proxy hops for client-IP resolution in rate limiting. Unset: **1** in production, **0** in development | Leave unset initially (assumes one trusted hop = Render's LB). **Render's actual forwarding behavior is NOT VERIFIED** — validate with §7 item 4 before relying on it; set explicitly if it differs |

Optional/tunable (defaults are safe; do not set unless needed):

| Variable | Default | Note |
|---|---|---|
| `SMTP_FROM` | `CalendarMealPlanner <no-reply@calendarmealplanner.com>` | Override only if the SMTP account requires a different sender address (then SPF/DKIM for that address must be in place — §7) |
| `SMTP_USE_TLS` | `true` | Keep `true` for STARTTLS on 587 |
| `VERIFICATION_TTL_MINUTES` | 60 | Verification-link validity |
| `PASSWORD_RESET_TTL_MINUTES` | 30 | Reset-link validity |

**Never set in production:** `FLASK_DEBUG` (would enable the Werkzeug
debugger in development only, but must not exist in the production service at
all).

### Safe entry and storage practices

* Enter all values **only** in the Render dashboard's environment settings
  for the web service. Render masks secret values in the UI and API.
* Never commit credentials: `.env*` is git-ignored; do not add env files,
  and do not paste real values into this runbook, tickets, or chat.
* Generate `SECRET_KEY` and SMTP app passwords locally/on the provider side;
  copy them once into the dashboard. If a secret is ever exposed, rotate it
  (new `SECRET_KEY` invalidates all existing sessions — plan for that).
* Keep a private, operator-only record of which providers hold which values
  (names, not values), so rotation can be performed under incident pressure.

---

## 5. Setup sequence (execution order)

Preconditions: access to the Render account, the Postgres/Redis creation
permissions, an SMTP provider account, and registrar access for the domain.
**None of this has been done yet.**

1. **Postgres.** Create the empty managed PostgreSQL database (same region).
   Record its **internal** connection string as `DATABASE_URL`.
2. **Redis.** Create the managed Redis instance (same region). Record its
   internal/TLS URL as `REDIS_URL`.
3. **Web service (first deploy, no traffic).** Create the web service from
   the repository, branch `feature/cmp-003-public-auth` (or its eventual
   release branch — decide at execution), root `recipe_calendar_app`,
   Python **3.12** pinned, build `pip install -r requirements.txt`,
   1 instance, autoscaling off, health check `GET /health/ready`.
   Set all §4 variables (generate `SECRET_KEY` first).
   The first deploy runs `flask db upgrade` against the empty Postgres:
   this is the **single intended migration run** (§6).
4. **Verify migration head + readiness** on the pre-domain service (§6.5).
   Until this passes, do not attach the domain or announce anything.
5. **External readiness checks** (§7) — Redis, SMTP, TLS, proxy chain,
   health-check behavior. Fix any failure and redeploy before step 6.
6. **Custom domain.** Attach `calendarmealplanner.com` in the dashboard,
   let Render provision the certificate, then create the DNS records at the
   registrar using the values Render shows. Wait for propagation and
   certificate issuance.
7. **Launch smoke tests** (§8), in order. Each has a go/no-go gate.
8. **Go-live.** Only after all smoke tests pass: announce, and keep the
   pre-launch `pg_dump` + restore verification (§6) as the standing backup
   practice.

---

## 6. Database and recovery

### 6.1 Creating the managed PostgreSQL database

Create an **empty** database via the Render dashboard (no schema, no
sample data). The app and its migration chain own the schema; do not create
tables manually and do not run `db.create_all()` against it (production
never does).

### 6.2 Internal connection URL

Use the Postgres instance's **internal** connection string for `DATABASE_URL`
(same-region services). The external string is rejected by Render's network
for same-region services. The exact value is read from the dashboard at
creation time — it is never stored in this document.

### 6.3 Backing up and verifying before any risky operation

Before **any** operation that can change data (first migration against a
database that already contains data, a manual downgrade, a data import):

```bash
pg_dump "$DATABASE_URL" -f /backups/calendar-<date>-<reason>.sql
sha256sum /backups/calendar-<date>-<reason>.sql   # record the hash
```

**Verify the backup before relying on it:** restore it into a throwaway
Postgres database and compare row counts:

```sql
SELECT count(*) FROM user;
SELECT count(*) FROM recipe;
SELECT count(*) FROM recipe WHERE user_id IS NULL;
```

(For the first-ever deploy the database is empty, so the pre-migration
backup is trivially an empty dump — still take it, so the procedure is
identical every time.)

### 6.4 Running migrations once, via the existing startup process

* **Do not run `flask db upgrade` manually in production.** The start
  command already runs it before gunicorn, on the single instance
  (§3.1). With one instance, each deploy's start is a single, serialized
  migration run.
* Empty Postgres → the first deploy creates the complete schema at head
  `0003` (`user`, `recipe`, `alembic_version`).
* Later deploys apply only pending revisions (no-op when current).
* A failed upgrade stops the `&&` chain: the instance never binds, the
  health check fails, Render cancels the deploy, and the previous version
  keeps serving. A half-migrated database can never serve traffic because
  of the readiness backstop (§6.5).

### 6.5 Verifying migration head and readiness

* `GET /health/ready` must return **200** `{"status": "ok", "database":
  "ok", "schema": "ok"}`. In production this asserts: database reachable
  **and** `alembic_version.version_num` equals the committed chain head
  (`0003`).
* Optionally confirm directly (operator, read-only):
  `psql "$DATABASE_URL" -c "SELECT version_num FROM alembic_version;"`
  must show `0003`.
* While not ready, every non-health route returns **503** (coarse body, no
  details) — by design.

### 6.6 Preserving legacy data when importing recipes

There is no built-in import endpoint. If pre-existing recipes (e.g. from the
development SQLite databases `db/calendar.db` / `instance/calendar.db`)
should be moved to production:

1. Take and **verify** a `pg_dump` of the production database first (§6.3).
2. Export the legacy rows (title, image, date, description) from a local copy
   of the SQLite file — never from the app while it is serving.
3. Insert them into Postgres with **`user_id = NULL`** (legacy/unowned).
   Legacy rows must **never** be assigned to an account automatically.
4. Verify: `SELECT count(*) FROM recipe WHERE user_id IS NULL;` matches the
   exported row count; existing users' rows are untouched.
5. Note the consequence: legacy (NULL-owned) recipes are invisible in
   authenticated users' calendars and API listings (users see only
   `user_id == own id`; a direct id lookup of a legacy row returns the same
   404 as a missing id). They are inert data, not shared content.

This import is a manual operator operation — **NOT performed, NOT VERIFIED**.

### 6.7 Application rollback vs. database rollback

* **Rolling back the application is not rolling back the database.**
  Reverting or redeploying an older application version does **not** require
  a database downgrade: `flask db upgrade` only moves the schema forward,
  and an older app version keeps working against a newer schema as long as it
  only uses existing columns.
* **Never run `flask db downgrade` automatically as part of an application
  rollback.**
* Database downgrades are a separate, manual operation: take a backup,
  verify it, and explicitly review the specific migration's downgrade risks
  before running it. Some downgrades **destroy user data**:
  `0002`'s downgrade drops the four token columns (invalidating pending
  verification/reset tokens); `0001`'s downgrade removes `recipe.user_id`
  and the `user` table (all accounts gone); `0003`'s downgrade is a
  deliberate no-op precisely so recipe rows survive.

### 6.8 Recovery if migration or startup fails

Symptoms and actions (in order):

1. **Deploy cancelled, previous version serving** (expected, safe state):
   read the Render deploy logs for the `flask db upgrade` error.
   * *Invalid `DATABASE_URL` / connection refused* → fix the env var or the
     Postgres instance (paused? wrong region?); redeploy.
   * *Migration error on an existing database* → the database is at most
     partially migrated; it is not serving traffic (readiness backstop).
     Take a `pg_dump` **now**, then repair per the specific error (usually:
     the database was hand-modified — restore from the verified dump if in
     doubt), and redeploy.
2. **Instance starts but `/health/ready` is 503 `{"database":
   "unavailable"}`** → Postgres unreachable from the service: check the
   internal URL, Postgres running/paused, same-region.
3. **`/health/ready` is 503 `{"schema": "not ready"}`** → `alembic_version`
   is not at head (e.g. a migration was partially applied and the version
   table not updated). Take a `pg_dump`, restore from the verified pre-migration
   dump, and let the next deploy re-run the migration cleanly.
4. **Startup refused at import** (env-var gate): the log lists every missing
   or invalid variable in one message — fix all of them, redeploy.
5. **Redis down** (app is up, rate-limited endpoints 500): restore/point
   `REDIS_URL` at a healthy instance; no app restart needed for unlimited
   endpoints, but the limiter recovers on the next successful Redis
   connection.

At no point is user traffic served against an unmigrated or unreachable
database (readiness backstop), so recovery time does not equal data-risk
time.

---

## 7. External readiness checklist (all NOT VERIFIED until executed)

Every item below is a **live test**, distinct from (and in addition to) the
dashboard configuration steps in §5. Do not mark an item complete from
configuration alone.

| # | Check | How | Pass evidence | Status |
|---|---|---|---|---|
| 7.1 | Redis connectivity | From a machine that can reach the service (or via the app): trigger a rate-limited action and confirm Redis is the store | No 500 "fail closed" on rate-limited endpoints; `redis-cli`/dashboard shows the limiter keys | **NOT VERIFIED** |
| 7.2 | Cross-client rate limits hold through Redis | Two different source IPs each hit `/login` (POST) with bad credentials | Client A: 5 attempts/15 min; after exhaustion, 429; client B unaffected (separate bucket) — proves XFF-based identity resolution works through Render's LB | **NOT VERIFIED** |
| 7.3 | SMTP TLS/STARTTLS | `openssl s_client -starttls smtp -connect <smtp-host>:587` from a host with egress to the provider; then a real test signup | STARTTLS handshake succeeds; no auth errors in service logs | **NOT VERIFIED** |
| 7.4 | Verification-email delivery | Create a test account; check the real inbox (and spam) for the email; confirm the link host is `https://calendarmealplanner.com` | Email arrives with a working HTTPS link; sender/SPF/DKIM accepted | **NOT VERIFIED** |
| 7.5 | Reset-email delivery | Request a password reset for the test account; confirm email + working link | Same as 7.4 | **NOT VERIFIED** |
| 7.6 | Render TLS / HTTPS behavior | `curl -vI https://calendarmealplanner.com/health` after DNS is live; also `curl -I http://…` | Valid cert chain for the domain; HTTP is redirected/refused per the chosen enforcement setting | **NOT VERIFIED** |
| 7.7 | Actual proxy chain → `TRUSTED_PROXY_HOPS` | With the service live: hit a rate-limited endpoint from two known distinct client IPs and confirm they get separate buckets (or inspect the service log's client-IP handling); compare with the `TRUSTED_PROXY_HOPS` assumption | Distinct clients → distinct buckets with hops=1. If all clients share one bucket, Render's forwarding differs from the assumption → set `TRUSTED_PROXY_HOPS` explicitly and re-test | **NOT VERIFIED** — this is exactly the check the README defers to live evidence |
| 7.8 | Health-check configuration behaves as designed | Redeploy a trivial change; watch the deploy: new instance must pass `/health/ready` before traffic switches; also confirm a deliberately broken env var produces a cancelled deploy with the old version still serving | Deploy log shows readiness-gated switch; no traffic to a failed instance | **NOT VERIFIED** |
| 7.9 | Custom-domain DNS + certificate | Registrar records match what Render's dashboard shows; certificate issued | `dig` shows the expected records; Render shows certificate active; §7.6 passes | **NOT VERIFIED** |

Dashboard configuration (done, but not proof): instance count = 1,
autoscaling off, health path = `/health/ready`, Python 3.12, env vars set —
these are **configured** states; the live tests above are what make them
**verified**.

---

## 8. Launch smoke tests (ordered)

Run in order after §7 passes. Each test lists expected results and the
failure/rollback criteria. Test accounts and recipes created here are
acceptable to keep or delete afterwards (deletion only via the app or a
careful manual `DELETE` after a verified `pg_dump`).

### T1 — Health and readiness
* **Do:** `GET /health`, then `GET /health/ready` (and once over HTTPS on the custom domain).
* **Expect:** `/health` → 200 `{"status": "ok"}` (no DB touch); `/health/ready` → 200 `{"status": "ok", "database": "ok", "schema": "ok"}`; both fast and stable across repeated calls.
* **Fail/rollback:** any 5xx, or `/health/ready` ≠ 200 → stop; §6.8 applies. Do not proceed.

### T2 — Registration
* **Do:** `POST /signup` with a fresh email + valid password (≥ 8 chars).
* **Expect:** success page/redirect; a `user` row with `status =
  'pending_verification'`; a verification email sent (confirmed in §7.4's
  mailbox); a duplicate signup with the same email is rejected (case/whitespace
  normalized); the signup rate limit is 3/h per client.
* **Fail/rollback:** no email, wrong status, or duplicate accepted → stop; check SMTP config and logs.

### T3 — Email verification
* **Do:** open the verification link from the email (must be an `https://`
  URL on the domain).
* **Expect:** account becomes verified; login with the new account succeeds;
  the token link is single-use (second use rejected); an expired/unknown
  token is rejected without revealing account existence.
* **Fail/rollback:** link not https, login still blocked after a correct
  click, or token replayable → stop; check `APP_BASE_URL` and token flow.

### T4 — Login, logout, password reset
* **Do:** log in; confirm session works (calendar shows own recipes); log out
  (session cookie gone); request a password reset; use the emailed link; set
  a new password; confirm the **old** password no longer works and the new
  one does.
* **Expect:** all of the above; reset request rate limit 3/h per client;
  generic "if an account exists" message for unknown emails (no
  enumeration).
* **Fail/rollback:** session persists after logout, reset link https check
  fails, or old password still works → stop.

### T5 — Two-account isolation (calendar + recipes)
* **Do:** with accounts A and B (both verified): A adds a recipe on a given
  date (calendar form) and via `POST /api/recipes`; B logs in and attempts:
  (a) view A's recipe by id (`GET /api/recipes/<A's id>`), (b) update/delete
  it (`PUT`/`DELETE`), (c) list recipes (`GET /api/recipes`). Also: a guest
  (logged out) hits `GET /api/recipes` and the calendar API paths.
* **Expect:** A sees exactly A's recipes; B's list contains **only B's**; B's
  id-lookup/update/delete of A's recipe all return **404** (identical to a
  missing id — no 403/leak); guest gets 401 (JSON) on the API. A recipe
  created by A is visible in A's calendar view on its date and absent from B's.
* **Fail/rollback:** any cross-account visibility, mutation, or 403-instead-of-
  404 → **stop immediately; this is a data-privacy failure.** Roll back to the
  last known-good deploy and investigate before any public launch.

### T6 — CSRF and unauthenticated access
* **Do:** (a) from a logged-in session, submit a state-changing form from a
  different origin / without the CSRF token; (b) call `POST /api/recipes`
  with cookies but no `X-CSRFToken` header; (c) hit a protected page logged
  out.
* **Expect:** (a) 400 CSRF rejection, no state change; (b) 400, no state
  change; (c) redirect to login (no data served).
* **Fail/rollback:** any state-changing request succeeds without a token →
  stop; privacy/security failure, no launch.

### T7 — Cross-client rate limiting (live confirmation of 7.2)
* **Do:** from two different client IPs (e.g. two devices/networks), hammer
  `POST /login` with bad credentials.
* **Expect:** each client independently limited (5/15 min) with 429 after
  exhaustion; the other client unaffected; the 429 body is the generic
  "Too many attempts" message (no enumeration).
* **Fail/rollback:** both clients share one bucket (XFF identity broken →
  fix `TRUSTED_PROXY_HOPS` per §7.7 and re-test), or no limiting at all →
  stop.

### T8 — Persistence across restart
* **Do:** note A's recipe count; restart the web service from the Render
  dashboard (and, if possible, let one deploy cycle complete); re-check.
* **Expect:** all recipes and accounts intact after restart/redeploy (state
  lives in Postgres, not the ephemeral filesystem); `/health/ready` returns
  to 200 after the restart's migration no-op.
* **Fail/rollback:** data missing after restart → **critical stop**; take a
  `pg_dump`, compare against the pre-restart dump, and investigate the
  `DATABASE_URL` target.

### T9 — HTTPS, cookies, custom domain
* **Do:** inspect the login response headers on the domain: certificate
  chain, `Set-Cookie` attributes; visit via `http://` if not enforced;
  confirm emailed links use the domain + https.
* **Expect:** valid certificate for `calendarmealplanner.com`; session
  cookie `HttpOnly; SameSite=Lax; Secure`; remember-me cookies carry
  `Max-Age=43200` (12 h) only when requested; plain `http` is redirected or
  rejected per the chosen enforcement.
* **Fail/rollback:** missing `Secure`, cert mismatch, or `http` serving
  app content → stop; fix domain/TLS/enforcement before launch.

**Go/no-go rule:** all of T1–T9 pass, and §7 items 7.1–7.9 have live
evidence. Any single failure blocks launch; failures in T5/T6/T8 are
stop-everything, not just stop-this-test.

---

## 9. Verified vs. unverified (summary)

**Verified (repository inspection at `c3e820b`, this machine only):**
build/start artifacts, dependency pins and the Python ≥3.11 constraint
(resolved SQLAlchemy 2.1.8), the full production env-var gate, health/
readiness semantics, the readiness backstop, migration chain and its
downgrade risks, ownership/visibility rules, CSRF and session cookie policy,
rate-limit values, SMTP STARTTLS-only transport, single-instance rationale.

**NOT VERIFIED (no live system touched):**
* All Render account state: no services, Postgres, Redis, domains, or
  certificates exist or were checked for this deployment.
* Render's actual proxy/forwarding behavior vs. `TRUSTED_PROXY_HOPS=1`.
* SMTP provider connectivity, STARTTLS handshake, sender authentication
  (SPF/DKIM/DMARC), and actual email delivery.
* DNS records and certificate issuance for the domain.
* Instance sizing under real traffic.
* The legacy-data import procedure (§6.6) — documented, never executed.

**Findings to resolve at execution:**
1. **SMTP port 465 (implicit TLS) is not supported** by the current code
   (`smtplib.SMTP` + `starttls()` only) — use 587/STARTTLS. (The README's
   env-var table mentions 465 as an option; if that wording stays, it
   should be reconciled with this code fact — out of scope for CMP-007.)
2. `TRUSTED_PROXY_HOPS` must be confirmed live (§7.7) before rate limiting
   can be trusted across clients.
3. The deployment branch decision: this runbook is prepared on
   `feature/cmp-003-public-auth`; the release branch/merge path is a
   separate decision (this task does not merge).

---

## 10. Restrictions honored

No application code, tests, dependencies, migrations, or databases were
changed. No Render services, databases, Redis instances, SMTP accounts, or
DNS records were created. No push, merge, deployment, or publication. No
secrets were requested, printed, or stored. Existing local data (SQLite
hashes `8c7430a0…` / `bd02878a…`) untouched.

---

## Final recommendation

**NOT READY FOR DEPLOYMENT EXECUTION** — by design: CMP-007 is preparation.
The runbook is complete and evidence-based, but every external prerequisite
is still open:

1. Render account access + create web service (1 instance, Python 3.12,
   health check `/health/ready`) — open.
2. Managed PostgreSQL created + internal URL captured — open.
3. Managed Redis created + URL captured — open.
4. SMTP account provisioned (587/STARTTLS) + sender auth (SPF/DKIM) — open.
5. All §4 environment variables entered (generate `SECRET_KEY`) — open.
6. Custom domain DNS records + certificate issuance — open.
7. §7 live validation checklist (9 items) executed with evidence — open.
8. §8 smoke tests T1–T9 passed — open.

When items 1–8 are done and evidenced, the service is ready for go-live
decision; otherwise it is not.
