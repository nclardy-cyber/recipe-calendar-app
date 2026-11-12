"""
Database initialization script - run this once to create the database and tables
"""

from app import app, db, Recipe

with app.app_context():
    db.create_all()
    print("Database created successfully!")
    print("Tables created:")
    print("  - recipe")
    print("\nDatabase file: db/calendar.db")
    print("\nYou can now add sample recipes or access the calendar at http://localhost:5000")
