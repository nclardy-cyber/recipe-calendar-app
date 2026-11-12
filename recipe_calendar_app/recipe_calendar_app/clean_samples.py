from app import app, db, Recipe

with app.app_context():
    samples = Recipe.query.filter(Recipe.title.contains('Sample')).all()
    for s in samples:
        db.session.delete(s)
    db.session.commit()
    print(f'Removed {len(samples)} sample recipes!')
