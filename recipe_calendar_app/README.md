# Recipe Calendar App

A Flask-based recipe calendar web application.

## Deployment

This app is configured for deployment on Render.com

### Build Command
`pip install -r requirements.txt`

### Start Command
`gunicorn app:app --bind 0.0.0.0 --workers 3`

> The `0.0.0.0` bind is only for Render's platform router. Running
> `python app.py` locally binds to `127.0.0.1` (loopback) only. Gunicorn does
> not run the Werkzeug dev server, and `app.debug` stays `False` unless
> `FLASK_DEBUG=1` is set explicitly in development — so the Werkzeug debugger
> can never be enabled in production.

### Environment Variables
- `SECRET_KEY`: **required in production.** Flask session signing key.
  Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
  If it is missing while `FLASK_ENV=production`, the app refuses to start.
  In development a fixed fallback key is used instead (with a logged warning).
  The key is never logged.
- `DATABASE_URL`: Postgres connection string for Render, e.g.
  `postgres://user:pass@host:5432/dbname`. Not needed locally. Local
  development uses SQLite at `instance/calendar.db` (created automatically on
  first run).
- `FLASK_ENV`: set to `production` on Render (enables the required-SECRET_KEY
  check).
- `FLASK_DEBUG`: optional, development only. `1` enables the Werkzeug
  debugger when running `python app.py` locally. Never set in production.

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

Tests run against a temporary SQLite database in a temp directory — they never
touch `db/calendar.db` or `instance/calendar.db`.
