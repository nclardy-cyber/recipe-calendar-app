# Recipe Calendar App

A Flask-based recipe calendar web application.

## Deployment

This app is configured for deployment on Render.com

### Build Command
`pip install -r requirements.txt`

### Start Command
`gunicorn app:app --bind 0.0.0.0 --workers 3`

### Environment Variables
- `DATABASE_URL`: SQLite database path (default: `./instance/app.db`)
- `SECRET_KEY`: Flask secret key (generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`)
