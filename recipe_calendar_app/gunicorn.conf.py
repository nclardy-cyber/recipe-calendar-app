"""Gunicorn configuration for production (Render deployment, CMP-005C).

Loaded explicitly from the Procfile start command
(`gunicorn -c gunicorn.conf.py --bind 0.0.0.0:$PORT app:app`) so the
worker policy is identical everywhere the app is served. Plain,
importable values - tests load this file to verify the settings.
"""

# sync workers: the app is request-driven with a per-request database
# session; 2 processes give headroom on a Render small instance without
# the complexity of async workers.
workers = 2
worker_class = 'sync'

# Render health checks / zero-downtime deploys: give in-flight requests
# time to finish and the old workers time to drain before the process
# exits on a new deployment or a container stop.
graceful_timeout = 60
timeout = 60

# The Flask app configures its own structured logging; gunicorn logs the
# process/service level.
loglevel = 'info'

# No `bind` here on purpose: the start command passes
# --bind 0.0.0.0:$PORT, where $PORT is assigned by Render.
