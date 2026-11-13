# CMP-008 — Infrastructure Readiness

**Service:** CalendarMealPlanner.com — Flask recipe calendar app on Render
**Prepared:** 2026-11-13
**Evidence base:** commit `3ee597f` on branch `feature/cmp-003-public-auth`
(ancestors `16d069f` = CMP-006A, `c3e820b` = CMP-006B-DOCFIX, and
`3ee597f` itself = CMP-007).

> **Preparation / audit only.** This document audits prerequisites and
> records exact setup steps. **No external resource was provisioned,
> modified, deployed, or configured.** No Render service, database, Redis
> instance, SMTP account, or DNS record was created or changed. No secrets
> were requested, read, printed, or stored. Every fact that requires live
> access is explicitly marked **NOT VERIFIED** or **PENDING VERIFICATION**.
>
> This document is the only change made to the repository in this task.

---

## 1. Preflight evidence

All checks were run on this machine, in the repository only. No git
destructive operations were performed (no reset, stash, rebase, or force).

| Check | Expected | Observed | Result |
|---|---|---|---|
| Project path | `C:\Users\jo1ni\Documents\GitHub\recipe-calendar-app\` | `/c/Users/jo1ni/Documents/GitHub/recipe-calendar-app` | PASS |
| Branch | `feature/cmp-003-public-auth` | `feature/cmp-003-public-auth` | PASS |
| HEAD | `3ee597f` | `3ee597f69254df656c8362300570539b932e1c65` (short `3ee597f`) | PASS |
| Working tree | clean | `nothing to commit, working tree clean` | PASS |
| Ancestor `16d069f` (CMP-006A) | ancestor of HEAD | `git merge-base --is-ancestor` → true | PASS |
| Ancestor `c3e820b` (CMP-006B-DOCFIX) | ancestor of HEAD | `git merge-base --is-ancestor` → true | PASS |
| Ancestor `3ee597f` (CMP-007) | ancestor of HEAD (it is HEAD) | `git merge-base --is-ancestor` → true | PASS |
| Runbook present | `CMP-007-DEPLOYMENT-RUNBOOK.md` in root | present (34,893 bytes, tracked) | PASS |
| README present | `recipe_calendar_app/README.md` | present (9,269 bytes, tracked) | PASS |
| Migrations doc present | `recipe_calendar_app/MIGRATIONS.md` | present (8,721 bytes, tracked) | PASS |
| Tracked file count | — | 40 files (excluding `.venv-test/` and caches) | — |
| No `render.yaml` / `render.json` | dashboard-only Render config | confirmed absent from tracked files | PASS |

Preflight result: **PASS**. The branch, HEAD, tree, and ancestor chain all
match the task's expectations, so work proceeds. No external system was
touched.

---

## 2. Render settings and resource checklist

All Render service settings live in the **Render dashboard** (there is no
`render.yaml` in the repository). The repository contributes only build/start
artifacts. Each item below is **PENDING CREATION** — none has been created.

### 2.1 Web service

| Setting | Value | Where | Status |
|---|---|---|---|
| Type | Web Service | Dashboard | PENDING CREATION |
| Root directory | `recipe_calendar_app` | Dashboard | PENDING CREATION |
| Runtime | Python **3.12** (explicitly pinned; hard minimum ≥ 3.11) | Dashboard | PENDING CREATION |
| Build command | `pip install -r requirements.txt` | Dashboard | PENDING CREATION |
| Start command | Procfile `web:` line (auto-detected): `python -m flask db upgrade && gunicorn -c gunicorn.conf.py --bind 0.0.0.0:$PORT app:app` | Repository `recipe_calendar_app/Procfile` | VERIFIED (repo) |
| Worker policy | 2 sync workers, 60 s timeout, 60 s graceful timeout | Repository `recipe_calendar_app/gunicorn.conf.py` | VERIFIED (repo) |
| Health check | HTTP `GET /health/ready` | Dashboard | PENDING CREATION |
| Instance count | **exactly 1** | Dashboard | PENDING CREATION |
| Autoscaling | **disabled** | Dashboard | PENDING CREATION |
| Zero-downtime deploys | on (default) | Dashboard | PENDING CREATION |
| Environment variables | §3 | Dashboard | PENDING CREATION |

**Runtime justification (verified from repository):** the transitive
SQLAlchemy dependency resolves to `2.1.8`, whose `METADATA` declares
`Requires-Python: >=3.11` (observed in
`.venv-test/Lib/site-packages/sqlalchemy-2.1.8.dist-info/METADATA`). A
runtime older than 3.11 cannot install the pinned dependency set, so the
build fails. Python 3.12 is the recommended pin.

**Single-instance requirement (blocking, verified from code):** every
instance runs `flask db upgrade` at startup (Procfile), and **no migration
concurrency lock exists** in the Alembic chain. Two instances starting
together can race on the same migrations. The service must run exactly one
instance with autoscaling off until migration execution is moved to a
separately controlled, single-run operation (e.g. a one-off Render job that
runs before any web instance starts). **Do not scale out on the current
start command.**

### 2.2 Managed PostgreSQL

| Setting | Value | Status |
|---|---|---|
| Instance | Render managed PostgreSQL, **empty** (no schema), same region as the web service | PENDING CREATION |
| Connection string | **internal** URL (same-region; the external string is rejected by Render's network) for `DATABASE_URL` | PENDING VERIFICATION (read from dashboard at creation) |
| Automated backups | enable/keep provider automated backups **in addition to** the manual `pg_dump` procedure in §5 | PENDING CREATION |

The migration chain owns the schema. The first deploy's `flask db upgrade`
creates `user`, `recipe`, and `alembic_version` at head `0003`. Production
never runs `db.create_all()`.

### 2.3 Managed Redis

| Setting | Value | Status |
|---|---|---|
| Instance | Render managed Redis, same region as the web service | PENDING CREATION |
| Connection URL | `redis://` or `rediss://`; prefer the internal/TLS URL Render provides, for `REDIS_URL` | PENDING VERIFICATION (read from dashboard at creation) |

The limiter runs with `in_memory_fallback_enabled=False` and
`swallow_errors=False` (verified, `app.py` lines 625–635): while Redis is
unreachable, rate-limited endpoints **fail closed with 500** rather than
degrading to per-process in-memory counters (which would multiply every
limit by the worker count). Unlimited endpoints keep working.

### 2.4 HTTPS and custom domain

| Setting | Value | Status |
|---|---|---|
| Custom domain | `calendarmealplanner.com` (and `www.` variant if wanted) | PENDING CREATION |
| TLS termination | Render provisions the certificate | PENDING CREATION |
| HTTPS enforcement | force HTTPS / redirect `http→https` ON | PENDING CREATION (current state NOT VERIFIED) |
| DNS records | at the registrar, outside Render — values in §4 | PENDING VERIFICATION |

---

## 3. Environment-variable requirements

The production startup gate (`_validate_production_environment`, `app.py`
lines 82–148) plus `_resolve_secret_key` (lines 55–79) validate these at
import time and **refuse to start**, listing every missing or invalid value
in one message. Values are entered **only** in the Render dashboard's
environment settings for the web service; `.env*` is git-ignored and no
secret is ever committed, logged, requested, or shared.

| Variable | Required | Format / validation | Purpose | Source (operator supplies at execution) |
|---|---|---|---|---|
| `FLASK_ENV` | yes | `production` | Production mode: activates every gate, `Secure` cookies, HTTPS URL scheme. Anything not explicitly `development`/`dev` already counts as production, but set it explicitly. | Fixed literal |
| `SECRET_KEY` | yes | ≥ 32 chars; never the dev fallback key | Flask session signing | Generated once, locally: `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Pasted to dashboard; never committed/logged/shared. |
| `DATABASE_URL` | yes | `postgres://`, `postgresql://`, or `postgresql+psycopg://` URL | App database | Render Postgres **internal** connection string (dashboard). An unset value would silently fall back to a local SQLite file that Render discards on every deploy. |
| `REDIS_URL` | yes | `redis://` or `rediss://` scheme | Shared rate-limit storage | Render Redis internal/TLS URL (dashboard). |
| `SMTP_HOST` | yes | non-empty | Mail server for verification/reset email | SMTP provider dashboard. |
| `SMTP_PORT` | validated (default `587`) | integer 1–65535; **invalid values raise `RuntimeError` at import** | SMTP port. Use **587 + STARTTLS** — the code uses `smtplib.SMTP` + `starttls()` and never `SMTP_SSL`, so **port 465 implicit TLS is not supported** (see §6.2). | SMTP provider docs. |
| `SMTP_USER` | yes | non-empty | SMTP account | SMTP provider dashboard; never logged. |
| `SMTP_PASSWORD` | yes | non-empty | SMTP account password/app password | SMTP provider dashboard; never logged; prefer an app-specific password if offered. |
| `APP_BASE_URL` | yes | must start with `https://` | Base URL for emailed verification/reset links | `https://calendarmealplanner.com` (final form confirmed at execution). |
| `TRUSTED_PROXY_HOPS` | optional | non-negative integer; **invalid (non-integer or negative) prevents startup** | Trusted proxy hops for client-IP resolution in rate limiting. Unset: **1** in production, **0** in development. | Leave unset initially (assumes one trusted hop = Render's LB). **Render's actual forwarding behavior is NOT VERIFIED** — validate with §7 T7 / CMP-007 7.7 before relying on it; set explicitly if it differs. |

Optional/tunable (safe defaults; set only if needed):

| Variable | Default | Note |
|---|---|---|
| `SMTP_FROM` | `CalendarMealPlanner <no-reply@calendarmealplanner.com>` (`app.py` 250–251) | Override only if the provider requires a different sender (then SPF/DKIM for that address must be in place, §6.2). |
| `SMTP_USE_TLS` | `true` | Keep `true` for STARTTLS on 587. |
| `VERIFICATION_TTL_MINUTES` | 60 | Verification-link validity. |
| `PASSWORD_RESET_TTL_MINUTES` | 30 | Reset-link validity. |

**Never set in production:** `FLASK_DEBUG` (would enable the Werkzeug
debugger in development only; it must not exist in the production service).
`PORT` is supplied by Render and bound directly by the start command.

### Secret handling rules

* Enter values **only** in the Render dashboard's environment settings
  (Render masks secret values in the UI and API).
* Never commit credentials (`.env*` is git-ignored). Do not add env files or
  paste real values into this document, tickets, or chat.
* If a secret is ever exposed, rotate it. A new `SECRET_KEY` invalidates all
  existing sessions — plan for that.
* Keep a private, operator-only record of which providers hold which values
  (names, not values) so rotation can be performed under incident pressure.

---

## 4. DNS requirements

DNS is set at the **registrar, outside Render, by the operator**. No DNS
record is created or changed in this task. Record **values are PENDING
VERIFICATION** — the exact values are taken from the Render dashboard (for
the domain records) and the SMTP provider's dashboard (for sender
authentication) at execution time.

| # | Record | Type | Name | Value | Purpose | Status |
|---|---|---|---|---|---|---|
| D1 | App domain (apex) | `A` (or CNAME if the registrar flattens) | `calendarmealplanner.com` | Render-provided IP / target (shown in dashboard after the domain is attached) | Route the app to Render | PENDING VERIFICATION |
| D2 | App domain (www, optional) | `CNAME` | `www.calendarmealplanner.com` | Render-provided target | Route `www` to Render | PENDING VERIFICATION |
| D3 | SPF | `TXT` | `calendarmealplanner.com` | `v=spf1 include:<smtp-provider> ~all` (provider-specific include) | Authorize the SMTP provider to send as this domain | PENDING VERIFICATION (provider-specific) |
| D4 | DKIM | `TXT` | `<selector>._domainkey.calendarmealplanner.com` | Provider DKIM public key | Cryptographically sign mail from this domain | PENDING VERIFICATION (provider-specific) |
| D5 | DMARC (recommended) | `TXT` | `_dmarc.calendarmealplanner.com` | `v=DMARC1; p=none; rua=mailto:<report>` (policy tightened after monitoring) | Monitor/report sender-authentication failures | PENDING VERIFICATION |

**Render TLS steps (execution order, no action taken now):**
1. Attach `calendarmealplanner.com` in the web service's dashboard custom-domain settings.
2. Render provisions the TLS certificate for the domain.
3. Read the DNS record values Render shows (typically an A record for the apex; a CNAME for `www`).
4. Create those records at the registrar (D1/D2). Wait for propagation and certificate issuance.
5. Confirm HTTPS-only behavior per §7 T9 (valid cert chain; `http` redirected/refused per the chosen enforcement).

The app's emailed links and `PREFERRED_URL_SCHEME` are `https` in production
(verified, `app.py` lines 221–224), so a working certificate is required
before any email flow is trusted.

---

## 5. Database backup, migration, and recovery

### 5.1 Creating the managed PostgreSQL database

Create an **empty** database via the Render dashboard (no schema, no sample
data). The app and its migration chain own the schema; do not create tables
manually and do not run `db.create_all()` against it.

### 5.2 Internal connection URL

Use the Postgres instance's **internal** connection string for `DATABASE_URL`
(same-region services). The external string is rejected by Render's network
for same-region services. The exact value is read from the dashboard at
creation time — it is never stored in this document.

### 5.3 Backing up and verifying before any risky operation

Before **any** operation that can change data (a migration against a database
that already contains data, a manual downgrade, a data import):

```bash
pg_dump "$DATABASE_URL" -f /backups/calendar-<date>-<reason>.sql
sha256sum /backups/calendar-<date>-<reason>.sql   # record the hash
```

**Verify the backup before relying on it:** restore it into a throwaway
Postgres database and compare row counts:

```sql
SELECT count(*) FROM "user";
SELECT count(*) FROM recipe;
SELECT count(*) FROM recipe WHERE user_id IS NULL;
```

(`user` is a PostgreSQL reserved word and must be quoted in raw SQL.) For the
first-ever deploy the database is empty, so the pre-migration backup is
trivially an empty dump — still take it, so the procedure is identical every
time.

### 5.4 Running migrations once, via the existing startup process (to `0003`)

* **Do not run `flask db upgrade` manually in production.** The start command
  already runs it before gunicorn, on the single instance (§2.1). With one
  instance, each deploy's start is a single, serialized migration run.
* **Migration to Alembic `0003` (head):** empty Postgres → the first deploy
  creates the complete schema (`user`, `recipe`, `alembic_version` at head
  `0003`). The chain is `0001` (user table + `recipe.user_id` FK) → `0002`
  (four token columns) → `0003` (recipe table on a fresh DB); head = `0003`.
* Later deploys apply only pending revisions (a no-op when current).
* A failed upgrade stops the `&&` chain: the instance never binds, the health
  check fails, Render cancels the deploy, and the previous version keeps
  serving. A half-migrated database can never serve traffic because of the
  readiness backstop (§5.5).

### 5.5 Verifying migration head and readiness

* `GET /health/ready` must return **200** `{"status": "ok", "database": "ok",
  "schema": "ok"}`. In production this asserts: database reachable **and**
  `alembic_version.version_num` equals the committed chain head (`0003`)
  (verified, `app.py` lines 1426–1476, 1520–1537).
* Optionally confirm directly (operator, read-only):
  `psql "$DATABASE_URL" -c "SELECT version_num FROM alembic_version;"` must
  show `0003`.
* While not ready, every non-health route returns **503** (coarse body, no
  details) — by design.

### 5.6 Recovery if migration or startup fails

Symptoms and actions (in order):

1. **Deploy cancelled, previous version serving** (expected, safe state):
   read the Render deploy logs for the `flask db upgrade` error.
   * *Invalid `DATABASE_URL` / connection refused* → fix the env var or the
     Postgres instance (paused? wrong region?); redeploy.
   * *Migration error on an existing database* → the database is at most
     partially migrated and is not serving traffic (readiness backstop).
     Take a `pg_dump` **now**, then repair per the specific error (restore
     from the verified dump if in doubt), and redeploy.
2. **Instance starts but `/health/ready` is 503 `{"database":
   "unavailable"}`** → Postgres unreachable from the service: check the
   internal URL, Postgres running/paused, same-region.
3. **`/health/ready` is 503 `{"schema": "not ready"}`** → `alembic_version`
   is not at head. Take a `pg_dump`, restore from the verified pre-migration
   dump, and let the next deploy re-run the migration cleanly.
4. **Startup refused at import** (env-var gate): the log lists every missing
   or invalid variable in one message — fix all of them, redeploy.
5. **Redis down** (app is up, rate-limited endpoints 500): restore/point
   `REDIS_URL` at a healthy instance; the limiter recovers on the next
   successful Redis connection.

At no point is user traffic served against an unmigrated or unreachable
database (readiness backstop), so recovery time does not equal data-risk time.

### 5.7 Application rollback vs. database rollback (hard rule)

* **Rolling back the application is not rolling back the database.**
  Reverting or redeploying an older application version does **not** require
  a database downgrade: `flask db upgrade` only moves the schema forward, and
  an older app version keeps working against a newer schema as long as it only
  uses existing columns.
* **Never run `flask db downgrade` automatically as part of an application
  rollback.**
* Database downgrades are a separate, manual operation: take a backup, verify
  it, and explicitly review the specific migration's downgrade risks before
  running it. Some downgrades **destroy user data**:
  * `0002`'s downgrade drops the four token columns (invalidating pending
    verification/reset tokens).
  * `0001`'s downgrade removes `recipe.user_id` and the `user` table (all
    accounts gone).
  * `0003`'s downgrade is a deliberate no-op precisely so recipe rows survive.

---

## 6. Redis / SMTP validation

Both are **live tests** — configuration in the dashboard is not proof. All
items below are **NOT VERIFIED** until executed against a live service.

### 6.1 Redis

| # | Check | How | Pass evidence | Status |
|---|---|---|---|---|
| R1 | Redis connectivity | Trigger a rate-limited action; confirm Redis is the store | No 500 "fail closed" on rate-limited endpoints; dashboard/`redis-cli` shows the limiter keys | NOT VERIFIED |
| R2 | Cross-client rate limits hold through Redis | Two different source IPs each hit `POST /login` with bad credentials | Client A: 5/15 min then 429; client B unaffected (separate bucket) — proves XFF-based identity resolution works through Render's LB | NOT VERIFIED |
| R3 | Redis outage fail-closed | (only if an outage is induced) | rate-limited endpoints return 500, unlimited endpoints keep working | NOT VERIFIED (not to be induced in production) |

### 6.2 SMTP

**Transport requirement (verified from code):** the app uses
`smtplib.SMTP(host, port, timeout=20)` then `smtp.starttls()` when
`SMTP_USE_TLS` is true (default) (`app.py` lines 459–464). It **never uses
`SMTP_SSL`**, so **implicit TLS (port 465) is not supported**. **STARTTLS on
port 587 is the required production mode.**

| # | Check | How | Pass evidence | Status |
|---|---|---|---|---|
| S1 | SMTP TLS/STARTTLS | `openssl s_client -starttls smtp -connect <smtp-host>:587` from a host with egress to the provider; then a real test signup | STARTTLS handshake succeeds; no auth errors in service logs | NOT VERIFIED |
| S2 | Verification-email delivery | Create a test account; check the real inbox (and spam); confirm the link host is `https://calendarmealplanner.com` | Email arrives with a working HTTPS link; sender/SPF/DKIM accepted | NOT VERIFIED |
| S3 | Reset-email delivery | Request a password reset for the test account; confirm email + working link | Same as S2 | NOT VERIFIED |
| S4 | Sender authentication (SPF/DKIM/DMARC) | After §4 records (D3/D4/D5) are live, send a test and check an email-auth checker | SPF + DKIM pass for the sending domain; DMARC policy in effect | NOT VERIFIED |

**README inconsistency (flagged; code NOT changed, per task boundary):**
`recipe_calendar_app/README.md` line 74 documents `SMTP_PORT` as *"Use
`587` for STARTTLS or `465` for implicit TLS."* That 465/implicit-TLS wording
contradicts the code, which cannot perform implicit TLS (`smtplib.SMTP` +
`starttls()` only). The port **validation** gate accepts any integer 1–65535
(including 465) — `test_config_security.py` pins that validation contract —
but setting `SMTP_PORT=465` would still fail at the transport layer because
the code never opens an `SMTP_SSL` connection. **Action for a later task (out
of scope here):** reconcile the README wording with the code fact. This task
documents the discrepancy and changes no code.

---

## 7. CMP-007 T1–T9 smoke tests

These are the ordered launch smoke tests (CMP-007 §8). They run **after** §6
passes, in order. Each has a go/no-go gate. All are **PENDING EXECUTION** —
none has been run against a live system.

| # | Test | Summary | Key expectation | Status |
|---|---|---|---|---|
| T1 | Health and readiness | `GET /health`, `GET /health/ready` (and over HTTPS on the domain) | `/health` → 200 `{"status":"ok"}` (no DB); `/health/ready` → 200 `{"status":"ok","database":"ok","schema":"ok"}`; stable | PENDING EXECUTION |
| T2 | Registration | `POST /signup` fresh email + valid password | success; `user` row `status='pending_verification'`; verification email sent; duplicate rejected; signup limit 3/h per client | PENDING EXECUTION |
| T3 | Email verification | open the emailed verification link (must be `https://`) | account verified; login works; token single-use; expired/unknown token rejected without revealing existence | PENDING EXECUTION |
| T4 | Login, logout, reset | login; logout; request reset; use link; set new password | session works then gone; old password no longer works; new one does; reset limit 3/h; generic "if an account exists" message | PENDING EXECUTION |
| T5 | **Two-account isolation** | accounts A and B (both verified); A adds a recipe (form + `POST /api/recipes`); B tries read/update/delete/list; guest hits the API | A sees only A's; B's list only B's; B's cross-id ops return **404** (identical to missing — no 403/leak); guest gets 401 (JSON); A's recipe on A's date, absent from B's. **Failure = stop-everything data-privacy failure** | PENDING EXECUTION |
| T6 | CSRF + unauthenticated | state-changing form from a different origin / without token; `POST /api/recipes` with cookies but no `X-CSRFToken`; protected page logged out | 400 CSRF rejection, no state change; 400 no state change; redirect to login, no data. **Failure = stop-everything security failure** | PENDING EXECUTION |
| T7 | Cross-client rate limiting | two distinct client IPs hammer `POST /login` with bad credentials | each client independently limited (5/15 min) with 429 after exhaustion; other unaffected; generic 429 message. Also confirms §6.1 R2 / CMP-007 7.7 (XFF identity) | PENDING EXECUTION |
| T8 | Persistence across restart | note A's recipe count; restart the service (and one deploy cycle); re-check | all recipes/accounts intact (state in Postgres, not ephemeral FS); `/health/ready` back to 200. **Failure = critical stop** | PENDING EXECUTION |
| T9 | HTTPS, cookies, domain | inspect login response headers on the domain; cert chain; `Set-Cookie`; `http://` behavior; emailed link host | valid cert for `calendarmealplanner.com`; session cookie `HttpOnly; SameSite=Lax; Secure`; remember-me `Max-Age=43200` only when requested; `http` redirected/rejected per enforcement | PENDING EXECUTION |

**Go/no-go rule:** all of T1–T9 pass **and** the §6 live items (R1–R2, S1–S4)
have evidence. Any single failure blocks launch; failures in T5/T6/T8 are
stop-everything, not just stop-this-test.

In-repo test coverage already exercises the *logic* behind T2–T7 against local
SQLite / an embedded PostgreSQL / a local Redis / a local `aiosmtpd`
(`tests/test_auth.py`, `test_ownership_security.py`, `test_rate_limits.py`,
`test_proxy_trust.py`, `test_production_hardening.py`, `test_migrations.py`,
`test_health_production.py`). The T1–T9 smoke tests are the **live production
equivalents**; passing the unit suite does not substitute for them.

---

## 8. Blockers, risks, dependencies, and Go/No-Go

### 8.1 Risks

| # | Risk | Mitigation | Status |
|---|---|---|---|
| RISK-1 | **Single-instance requirement.** No migration concurrency lock; multiple instances or autoscaling can race `flask db upgrade` at startup. | Keep exactly 1 instance, autoscaling off, until migrations move to a separately controlled single-run operation. | Open (design constraint) |
| RISK-2 | **App rollback must never auto-downgrade the DB.** An automatic `flask db downgrade` on rollback can destroy user data (`0001`/`0002` downgrades drop accounts/tokens). | Rollback = redeploy older app only; downgrades are a separate manual, backup-first operation (§5.7). | Open (operational rule) |
| RISK-3 | **Backup/recovery.** A bad migration or manual edit against a populated DB can lose data. | `pg_dump` + `sha256` + verified restore before any data-changing operation (§5.3, §5.6). | Open (procedure) |
| RISK-4 | **`TRUSTED_PROXY_HOPS` unverified.** Render's actual proxy-hop count is unknown; a wrong value breaks cross-client rate-limit identity (all clients share one bucket, or a client can impersonate another). | Verify live before relying on the default of 1 (T7 / CMP-007 7.7); set `TRUSTED_PROXY_HOPS` explicitly if it differs. | Open (live check) |
| RISK-5 | **Live Redis, SMTP, TLS, DNS, and health checks unverified.** Configuration in the dashboard is not proof; each can fail only when exercised. | Run the §6 live checklist and T1–T9 with evidence before go-live. | Open (live checks) |
| RISK-6 | **Two-user registration, login, calendar isolation, and recipe ownership.** A cross-account visibility/mutation leak is a data-privacy failure. | T5 (two-account isolation) + T4 (login/verification) are stop-everything gates; in-repo `test_ownership_security.py` already pins the logic. | Open (live check) |
| RISK-7 | **SMTP 465/implicit-TLS mismatch.** README advertises 465; code supports 587/STARTTLS only. An operator who follows the README and sets 465 will see transport failure. | Documented (§6.2); reconcile the README in a later task (out of scope here). | Flagged (doc fix pending) |
| RISK-8 | **Instance sizing under real traffic unknown.** | Smallest comfortable plan first; revisit only with live load evidence. | Open (live check) |

### 8.2 Blockers (each blocks go-live until cleared)

1. Render account access + web service creation (1 instance, Python 3.12,
   health check `/health/ready`) — open.
2. Managed PostgreSQL created + internal URL captured — open.
3. Managed Redis created + URL captured — open.
4. SMTP account provisioned (587/STARTTLS) + sender auth (SPF/DKIM) — open.
5. All §3 environment variables entered (generate `SECRET_KEY`) — open.
6. Custom-domain DNS records + certificate issuance — open.
7. §6 live validation (R1–R2, S1–S4) executed with evidence — open.
8. T1–T9 smoke tests passed with evidence — open.

### 8.3 Dependencies

* Render account with web-service, Postgres, and Redis creation permissions.
* An SMTP/transactional-email provider account (587/STARTTLS capable).
* Registrar access for `calendarmealplanner.com` (DNS).
* A live, reachable Postgres in the same region as the web service before the
  first deploy (the migration run targets it).
* A live Redis before rate-limited endpoints can be exercised.
* The domain attached + certificate issued before T3/T4/T9 (email links and
  HTTPS cookies) can pass.

### 8.4 Go/No-Go recommendation

**NO-GO for deployment execution** — by design. CMP-008 is preparation and
audit. The repository is ready (verified build/start artifacts, env-var gate,
health/readiness semantics, migration chain and its downgrade risks,
ownership/visibility rules, CSRF/session policy, rate-limit values, SMTP
STARTTLS-only transport, single-instance rationale, and a comprehensive local
test suite), but **every external prerequisite in §8.2 is still open** and no
live validation has been performed.

The service becomes **GO** only when blockers 1–8 are done **and evidenced**
(§6 live checks + T1–T9 all passing). Until then it is **NO-GO**. No
provisioning or deployment is authorized by this document.

---

## 9. Verified facts versus items requiring live access

### 9.1 Verified (repository inspection at `3ee597f`, this machine only)

* **Build/start artifacts:** Procfile start command; `gunicorn.conf.py`
  (2 sync workers, 60 s timeouts); `requirements.txt` (all deps pinned:
  Flask 3.1.3, Werkzeug 3.1.9, gunicorn 27.0.1, Flask-SQLAlchemy 3.1.1,
  Flask-Login 0.6.3, Flask-Migrate 4.1.0, Flask-Limiter 4.1.1, Flask-WTF
  1.3.0, alembic 1.20.1, psycopg[binary] 3.3.9, redis 5.2.1).
* **Python constraint:** transitive SQLAlchemy resolves to 2.1.8 with
  `Requires-Python >= 3.11` → runtime must be ≥ 3.11; 3.12 recommended.
* **Production env-var gate:** `_validate_production_environment` (app.py
  82–148) + `_resolve_secret_key` (55–79) refuse to start on any missing/invalid
  value, listing all problems in one message.
* **`TRUSTED_PROXY_HOPS`:** optional non-negative int; default 1 (production) /
  0 (development); invalid value raises `RuntimeError` at import (app.py
  545–558, called at module load line 642).
* **Health semantics:** `/health` = liveness (no DB, app.py 1509–1517);
  `/health/ready` = readiness — production: DB reachable **and**
  `alembic_version` == chain head `0003` (app.py 1426–1476, 1520–1537).
* **Readiness backstop:** production serves no non-health route until ready
  (503, registered before CSRF) — app.py 166–185.
* **Migration chain:** `0001` → `0002` → `0003`, head `0003`; schema-only;
  never rewrites existing recipe rows; `0003` downgrade is a deliberate no-op
  (migrations/versions/0001–0003, MIGRATIONS.md).
* **Migrations run at instance start** before gunicorn; failure stops the
  `&&` chain → Render cancels the deploy, previous version keeps serving.
* **Session cookies:** `HttpOnly`, `SameSite=Lax`, `Secure` in production;
  remember-me = 12 h persistent (`Max-Age=43200`); `SESSION_PROTECTION=basic`
  (app.py 216–220).
* **CSRF:** on all state-changing requests; session-bound, 1 h TTL; JSON API
  uses `X-CSRFToken` (app.py 187–191).
* **Rate limits (POST only, Redis-backed in production, fail-closed 500 if
  Redis down):** `/signup` 3/h (660), `/login` 5/15 min (713),
  `/resend-verification` 3/h (784), `/reset-password` 3/h (831); limiter
  config (596–641).
* **Recipe visibility:** guests 401 on the API (1102–1103, 1121–1122,
  1154–1155); a user sees only `user_id == own id`; a cross-user, legacy
  (`user_id IS NULL`), or missing id all return the **identical 404**
  (`owned_recipe_query`/`get_owned_recipe_or_404`, 374–396).
* **Recipe titles:** create paths truncate to 100 (1136); `PUT` rejects >100
  with 400 (1176–1177).
* **SMTP transport:** `smtplib.SMTP` + `starttls()` only; `SMTP_SSL` never
  used → **port 465 implicit TLS unsupported**, 587/STARTTLS is the supported
  mode (459–464). `SMTP_FROM` default `CalendarMealPlanner
  <no-reply@calendarmealplanner.com>` (250–251).
* **Single-instance rationale:** no migration concurrency lock (Procfile +
  MIGRATIONS.md).
* **No `render.yaml`/`render.json`:** Render configuration is dashboard-only.
* **Local test suite:** comprehensive coverage of auth, ownership isolation,
  rate limits, proxy trust, production hardening, migrations (SQLite +
  embedded PostgreSQL), and health/readiness — all runnable without external
  systems.

### 9.2 NOT VERIFIED / PENDING (no live system touched)

* **All Render account state:** no services, Postgres, Redis, domains, or
  certificates exist or were checked for this deployment.
* **Render's actual proxy/forwarding behavior** vs. the `TRUSTED_PROXY_HOPS=1`
  default (RISK-4 / T7 / CMP-007 7.7).
* **SMTP provider connectivity**, STARTTLS handshake, sender authentication
  (SPF/DKIM/DMARC), and actual email delivery (S1–S4).
* **DNS records and certificate issuance** for the domain (D1–D5, §4).
* **Instance sizing under real traffic** (RISK-8).
* **Live Redis connectivity and cross-client rate-limit behavior** (R1–R2).
* **The T1–T9 launch smoke tests** (all pending execution).

### 9.3 Findings to resolve at execution

1. **SMTP 465 mismatch** — README line 74 advertises 465/implicit TLS; the
   code supports 587/STARTTLS only. Use 587. Reconcile the README in a later
   task (out of scope here; no code changed).
2. **`TRUSTED_PROXY_HOPS`** — confirm live (T7 / CMP-007 7.7) before relying
   on rate limiting across clients; set explicitly if Render's hop count
   differs.
3. **Deployment branch decision** — this readiness audit is prepared on
   `feature/cmp-003-public-auth`; the release branch/merge path is a separate
   decision (this task does not merge).

---

## 10. Boundaries honored (CMP-008 strict scope)

* No resource creation or modification (no Render service, database, Redis,
  SMTP account, or DNS record).
* No deployment, no production migration, no DNS change, no public release.
* No secrets accessed, requested, printed, or stored.
* No code, dependency, database, or migration changes.
* No push and no merge.
* **Only the new readiness document (`CMP-008-INFRASTRUCTURE-READINESS.md`)
  was created/committed.** No other file was modified. The working tree
  contains only this new file.

---

## Final recommendation

**NO-GO for deployment execution** — by design: CMP-008 is preparation and
audit. The repository is verified and ready; the infrastructure plan is
complete and evidence-based, but every external prerequisite in §8.2 remains
open and no live validation has been performed. When blockers 1–8 are done
and evidenced (the §6 live checks and T1–T9 all passing with recorded
evidence), the service is ready for a go-live decision; otherwise it stays
**NO-GO**.

Await explicit authorization before any provisioning or deployment.
