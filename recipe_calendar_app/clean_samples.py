import os

# Local development helper: explicit development opt-in (since CMP-006A
# production is the safe default and an unset FLASK_ENV would refuse to
# start without a full production configuration).
os.environ.setdefault('FLASK_ENV', 'development')

from app import app, db, Recipe

with app.app_context():
    samples = Recipe.query.filter(Recipe.title.contains('Sample')).all()
    for s in samples:
        db.session.delete(s)
    db.session.commit()
    print(f'Removed {len(samples)} sample recipes!')
