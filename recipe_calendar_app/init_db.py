"""
Database initialization script - run this once to create the database and tables
"""

import os

# Local development helper: explicit development opt-in (since CMP-006A
# production is the safe default and an unset FLASK_ENV would refuse to
# start without a full production configuration).
os.environ.setdefault('FLASK_ENV', 'development')

from app import app, db, Recipe

with app.app_context():
    db.create_all()
    print("Database created successfully!")
    print("Tables created:")
    print("  - recipe")
    print("\nDatabase file: db/calendar.db")
    print("\nYou can now add sample recipes or access the calendar at http://localhost:5000")
