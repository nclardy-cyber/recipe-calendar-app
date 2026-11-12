"""
Flask Calendar Web App
Simple calendar view with month navigation
"""

from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin
from flask_migrate import Migrate
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta, date
from calendar import monthrange, month_name
import logging
import os

logger = logging.getLogger(__name__)

# --- Security configuration --------------------------------------------------
# SECRET_KEY is read from the environment. It is NEVER generated randomly on
# startup (that invalidates all sessions on every restart) and is never logged.
# Production: a missing SECRET_KEY aborts startup. Development: a fixed,
# clearly-marked fallback key is used so local sessions survive restarts.
DEV_FALLBACK_SECRET_KEY = 'dev-only-insecure-secret-key-change-me'
_PRODUCTION_ENVS = {'production', 'prod'}


def _is_production() -> bool:
    return os.environ.get('FLASK_ENV', '').strip().lower() in _PRODUCTION_ENVS


def _resolve_secret_key() -> str:
    key = os.environ.get('SECRET_KEY', '').strip()
    if key:
        return key
    if _is_production():
        raise RuntimeError(
            'SECRET_KEY environment variable is required in production. '
            'Generate one with: '
            'python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    logger.warning(
        'SECRET_KEY not set - using the insecure development fallback key. '
        'Set SECRET_KEY before deploying.'
    )
    return DEV_FALLBACK_SECRET_KEY


app = Flask(__name__)
app.secret_key = _resolve_secret_key()
# Debug mode is OFF by default, so the Werkzeug debugger never runs. Only the
# local dev server may enable it via FLASK_DEBUG=1; gunicorn does not run the
# Werkzeug dev server, so the debugger cannot start under it.
app.debug = os.environ.get('FLASK_DEBUG', '').strip().lower() in {'1', 'true', 'yes'}
app.config['DEBUG'] = app.debug

# Use Render's Postgres when DATABASE_URL is set; fall back to local SQLite
_db_url = os.environ.get('DATABASE_URL', 'sqlite:///calendar.db')
if _db_url.startswith('postgres://'):
    _db_url = 'postgresql+psycopg://' + _db_url[len('postgres://'):]
elif _db_url.startswith('postgresql://'):
    _db_url = 'postgresql+psycopg://' + _db_url[len('postgresql://'):]
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_CHANGES'] = False
@app.route('/login', methods=['GET', 'POST'])
def login():
    """Handle user login"""
    if request.method == 'POST':
        # Simple validation without database for now
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        
        if not email or not password:
            flash('Please provide both email and password', 'error')
            return redirect(url_for('login'))
        
        # For now, just show a welcome message
        flash(f'Welcome, {email}! Login successful!', 'success')
        return redirect(url_for('calendar'))
    
    # GET: Show login form
    next_url = request.args.get('next')
    email = request.args.get('email', '')
    return render_template('login.html', email=email, next=next_url)


# Signup Route
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    """Handle user signup"""
    if request.method == 'POST':
        # Simple validation without database for now
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        
        if not email or not password:
            flash('Please provide both email and password', 'error')
            return redirect(url_for('signup'))
        
        # For now, just show a success message
        flash(f'Account created for {email}! Please log in.', 'success')
        return redirect(url_for('login'))
    
    # GET: Show signup form
    return render_template('signup.html')


# Logout Route
@app.route('/logout')
def logout():
    """Handle user logout"""
    session.pop('user_id', None)
    session.pop('email', None)
    session.pop('user_id_str', None)
    flash('You have been logged out', 'info')
    return redirect(url_for('login'))

app.config['SQLALCHEMY_TRACK_CHANGES'] = False

db = SQLAlchemy(app)
migrate = Migrate(app, db)


def normalize_email(email) -> str:
    """Normalize an email address for storage and lookup.

    Strips surrounding whitespace and lowercases, so ' User@Example.COM '
    and 'user@example.com' resolve to the same account.
    """
    return (email or '').strip().lower()


class User(db.Model, UserMixin):
    """Public multi-user account.

    - ``email`` is unique and always stored in normalized form.
    - ``password_hash`` holds a salted Werkzeug password hash (scrypt by
      default); plaintext passwords are never stored.
    - ``created_at`` is the account creation timestamp (DB-side default).
    - ``email_verified`` / ``status`` track verification and lifecycle.
      Valid ``status`` values: 'active', 'pending_verification', 'disabled'.
    """
    __tablename__ = 'user'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False,
                           server_default=db.func.now())
    email_verified = db.Column(db.Boolean, nullable=False,
                               server_default=db.false())
    status = db.Column(db.String(20), nullable=False,
                       server_default='active', default='active')

    def __init__(self, email=None, **kwargs):
        if email is not None:
            email = normalize_email(email)
        super().__init__(email=email, **kwargs)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


# Calendar Model
class Recipe(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    image = db.Column(db.String(200))
    date = db.Column(db.Date)
    description = db.Column(db.Text)
    # Nullable ownership: NULL means a legacy/shared recipe that belongs to
    # no account. Legacy rows must NEVER be assigned to an arbitrary user.
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    user = db.relationship('User')


# Flask-Login wiring (foundation; endpoint rework lands in CMP-004)
login_manager = LoginManager(app)
login_manager.login_view = 'login'


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# Initialize database (idempotent, safe on every boot)
try:
    with app.app_context():
        db.create_all()
        print("Database initialized!")
except Exception as _db_exc:  # don't block boot if DB is briefly unavailable
    print(f"WARNING: could not initialize database: {_db_exc}")

# Sample recipes (can be removed or customized)  # Disabled for now
# sample_recipes = [
#     {
#         'title': 'Sample Recipe 1',
#         'image': 'https://picsum.photos/300/200?random=1',
#         'date': '2026-11-05',
#         'description': 'A sample recipe description for November 5th, 2026.'
#     },
#     {
#         'title': 'Sample Recipe 2',
#         'image': 'https://picsum.photos/300/200?random=2',
#         'date': '2026-11-10',
#         'description': 'A sample recipe description for November 10th, 2026.'
#     }
# ]

# with app.app_context():
#     if sample_recipes:
#         from datetime import datetime
#         for recipe_data in sample_recipes:
#             new_recipe = Recipe(
#                 title=recipe_data['title'],
#                 image=recipe_data['image'],
#                 date=datetime.strptime(recipe_data['date'], '%Y-%m-%d').date(),
#                 description=recipe_data.get('description', '')
#             )
#             db.session.add(new_recipe)
#         db.session.commit()
#         print(f"Added {len(sample_recipes)} sample recipes to database!")

# Create sample users for testing (with unique passwords)  # Disabled for now
# with app.app_context():
#     test_users = [
#         User(email='test@example.com', password='password123'),
#         User(email='admin@example.com', password='admin123')
#     ]
#     existing_emails = [User.query.filter_by(email=u.email).first() for u in test_users]
#     for user, existing in zip(test_users, existing_emails):
#         if not existing:
#             db.session.add(user)
#     db.session.commit()
#     print("Created test users for demo!")

# Calendar View - main landing page
@app.route('/clean-samples')
def clean_samples():
    """Remove all sample recipes and show calendar without them."""
    from app import app, db, Recipe
    
    with app.app_context():
        samples = Recipe.query.filter(Recipe.title.contains('Sample')).all()
        for sample in samples:
            db.session.delete(sample)
        db.session.commit()
        print(f"Removed {len(samples)} sample recipes!")
        return f"Removed {len(samples)} sample recipes. <a href='/'>Go back to calendar</a>"

# Calendar View - main landing page
@app.route('/')
def calendar():
    """
    Display calendar for the current month.
    Supports month/year navigation via query parameters.
    """
    # Serve the calendar directly. (Was: redirect(url_for('calendar')),
    # which resolves to this same route -> infinite redirect loop.)
    return calendar_view()


# Calendar View - main route
@app.route('/calendar')
def calendar_view():
    """
    Display calendar for the current month.
    Supports month/year navigation via query parameters.
    """
    # Get month and year from query params, default to current
    month = request.args.get('month')
    year = request.args.get('year')

    if month is None or year is None:
        now = datetime.now()
        month_i, year_i = now.month, now.year
    else:
        try:
            month_i, year_i = int(month), int(year)
        except ValueError:
            now = datetime.now()
            month_i, year_i = now.month, now.year
    if not (1 <= month_i <= 12 and 1 <= year_i <= 9999):
        now = datetime.now()
        month_i, year_i = now.month, now.year
    month = str(month_i).zfill(2)
    year = str(year_i)

    # Days in the displayed month - monthrange handles leap years (Feb 2024 = 29)
    days_in_month = monthrange(year_i, month_i)[1]

    # First weekday of the month (0=Monday, 6=Sunday) and the adjacent months
    start_day = date(year_i, month_i, 1).weekday()
    if month_i == 1:
        prev_month_i, prev_year_i = 12, year_i - 1
    else:
        prev_month_i, prev_year_i = month_i - 1, year_i
    if month_i == 12:
        next_month_i, next_year_i = 1, year_i + 1
    else:
        next_month_i, next_year_i = month_i + 1, year_i
    prev_month_days = monthrange(prev_year_i, prev_month_i)[1]

    # Generate calendar grid
    calendar_days = []
    for i in range(42):  # 6 weeks max
        day_num = i - start_day + 1
        if day_num <= 0:
            # Leading cells: real dates from the previous month (e.g. 30, 31)
            d = prev_month_days + day_num  # day_num is negative
            calendar_days.append({
                'day': d,
                'day_str': str(d).zfill(2),
                'is_current_month': False,
                'day_class': 'prev-month'
            })
        elif day_num > days_in_month:
            # Trailing cells: real dates from the next month (1, 2, 3, ...)
            d = day_num - days_in_month
            calendar_days.append({
                'day': d,
                'day_str': str(d).zfill(2),
                'is_current_month': False,
                'day_class': 'next-month'
            })
        else:
            # Current month - always a valid current-month date
            day_date = date(year_i, month_i, day_num)
            recipes = Recipe.query.filter_by(date=day_date).all()
            # Serialize recipes for template
            calendar_days.append({
                'day': day_num,
                'day_str': str(day_num).zfill(2),
                'is_current_month': True,
                'day_class': 'current-month',
                'recipes': [
                    {
                        'id': r.id,
                        'title': r.title,
                        'image': r.image,
                        'description': r.description
                    }
                    for r in recipes
                ]
            })

    # Navigation URLs (year boundaries wrap correctly)
    prev_month_url = (f'?month=12&year={year_i - 1}' if month_i == 1
                      else f'?month={str(month_i - 1).zfill(2)}&year={year_i}')
    next_month_url = (f'?month=01&year={year_i + 1}' if month_i == 12
                      else f'?month={str(month_i + 1).zfill(2)}&year={year_i}')

    # Month name for display
    month_name_display = month_name[month_i]
    
    return render_template('calendar.html',
                         days=calendar_days,
                         month=month,
                         year=year,
                         prev_month_url=prev_month_url,
                         next_month_url=next_month_url,
                         month_name=month_name_display,
                         days_in_month=days_in_month,
                         user_email=session.get('email', 'Guest'))

# Recipe API endpoints
@app.route('/api/recipes', methods=['GET'])
def get_recipes():
    """Get recipes for a specific date"""
    date = request.args.get('date')
    if date:
        try:
            date_obj = datetime.strptime(date, '%Y-%m-%d').date()
        except ValueError:
            return {'date': date, 'recipes': []}
        recipes = Recipe.query.filter_by(date=date_obj).all()
        return {
            'date': date,
            'recipes': [{'id': r.id, 'title': r.title, 'image': r.image, 'description': r.description} for r in recipes]
        }
    return {'recipes': []}

@app.route('/api/recipes', methods=['POST'])
def create_recipe():
    """Create a recipe from JSON or form data."""
    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = request.form.to_dict()
    title = (data.get('title') or '').strip()
    date_str = (data.get('date') or '').strip()
    if not title or not date_str:
        return {'error': 'title and date (YYYY-MM-DD) are required'}, 400
    try:
        recipe_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return {'error': 'date must be formatted YYYY-MM-DD'}, 400
    recipe = Recipe(
        title=title[:100],
        image=(data.get('image') or '').strip() or None,
        date=recipe_date,
        description=(data.get('description') or '').strip(),
    )
    db.session.add(recipe)
    db.session.commit()
    return {'id': recipe.id, 'title': recipe.title, 'date': str(recipe_date)}, 201

@app.route('/api/recipes/<int:id>', methods=['GET', 'PUT', 'DELETE'])
def recipe_api(id):
    """CRUD API for recipes"""
    recipe = Recipe.query.get_or_404(id)
    
    if request.method == 'GET':
        return {
            'id': recipe.id,
            'title': recipe.title,
            'image': recipe.image,
            'description': recipe.description,
            'date': str(recipe.date)
        }
    
    elif request.method == 'PUT':
        data = request.get_json()
        recipe.title = data.get('title', recipe.title)
        recipe.image = data.get('image', recipe.image)
        recipe.description = data.get('description', recipe.description)
        recipe.date = datetime.strptime(data.get('date'), '%Y-%m-%d').date() if data.get('date') else recipe.date
        db.session.commit()
        return {'id': recipe.id, 'title': recipe.title, 'image': recipe.image, 'description': recipe.description, 'date': str(recipe.date)}
    
    elif request.method == 'DELETE':
        db.session.delete(recipe)
        db.session.commit()
        return {'id': recipe.id, 'title': recipe.title, 'deleted': True}
    
    return recipe

# Day View - show all recipes for a specific day
@app.route('/day/<int:day_num>', methods=['GET', 'POST'])
def day_view(day_num):
    """Show detailed view for a specific day"""
    month = request.args.get('month')
    year = request.args.get('year')
    search_query = request.args.get('search', '').strip()  # Capture search query

    # Resolve month/year FIRST (default to current month), then compute length
    if month is None or year is None:
        now = datetime.now()
        month_i, year_i = now.month, now.year
    else:
        try:
            month_i, year_i = int(month), int(year)
        except ValueError:
            now = datetime.now()
            month_i, year_i = now.month, now.year
    if not (1 <= month_i <= 12 and 1 <= year_i <= 9999):
        now = datetime.now()
        month_i, year_i = now.month, now.year
    month = str(month_i).zfill(2)
    year = str(year_i)

    # Days in this month (correct for leap years and short months)
    days_in_month = monthrange(year_i, month_i)[1]

    # Clamp the requested day into the month so dates and navigation agree
    day_num = max(1, min(day_num, days_in_month))

    # Calculate the date
    day_date = date(year_i, month_i, day_num)
    day_date_str = str(day_date)
    
    # Handle recipe submission (form POST from the day page)
    if request.method == 'POST':
        title = (request.form.get('title') or '').strip()
        if not title:
            flash('Title is required', 'error')
        else:
            recipe = Recipe(
                title=title[:100],
                image=(request.form.get('image') or '').strip() or None,
                date=day_date,
                description=(request.form.get('description') or '').strip(),
            )
            db.session.add(recipe)
            db.session.commit()
            flash(f'Added "{title}"', 'success')
        return redirect(f'/day/{day_num}?month={month}&year={year}')

    # Get recipes for this day (serialize for the template)
    _rows = Recipe.query.filter_by(date=day_date).all()
    recipes = [
        {'id': r.id, 'title': r.title, 'image': r.image, 'description': r.description}
        for r in _rows
    ]
    
    # Get navigation links
    month_name_display = month_name[month_i]

    if month_i == 1:
        prev_month_i, prev_year_i = 12, year_i - 1
    else:
        prev_month_i, prev_year_i = month_i - 1, year_i
    if month_i == 12:
        next_month_i, next_year_i = 1, year_i + 1
    else:
        next_month_i, next_year_i = month_i + 1, year_i
    prev_month_days = monthrange(prev_year_i, prev_month_i)[1]

    # Prev/next day stay in the same month; only wrap across month boundaries
    if day_num > 1:
        prev_day_url = f'/day/{day_num - 1}?month={month}&year={year}'
    else:
        prev_day_url = (f'/day/{prev_month_days}?month={str(prev_month_i).zfill(2)}'
                        f'&year={prev_year_i}')
    if day_num < days_in_month:
        next_day_url = f'/day/{day_num + 1}?month={month}&year={year}'
    else:
        next_day_url = f'/day/1?month={str(next_month_i).zfill(2)}&year={next_year_i}'
    
    # Build back to calendar URL
    calendar_url = f'/calendar?month={month}&year={year}'
    
    return render_template('day_view.html',
                         day_num=day_num,
                         month=month,
                         year=year,
                         day_date=day_date_str,
                         day_date_display=day_date.strftime('%A, %B %d, %Y'),
                         recipes=recipes,
                         prev_day_url=prev_day_url,
                         next_day_url=next_day_url,
                         month_name_display=month_name_display,
                         year_display=day_date.strftime('%Y'),
                         days_in_month=days_in_month,
                         search_query=search_query,
                         calendar_url=calendar_url)

# Recipe API endpoints

# Search Routes - Search across three recipe websites
import re as _re
import requests
from urllib.parse import quote_plus

SEARCH_SOURCES = [
    {
        'key': 'allrecipes',
        'name': 'AllRecipes',
        'url_template': 'https://www.allrecipes.com/search?q={}',
        'scrape': False,  # JS-rendered page + bot protection: link-only
    },
    {
        'key': 'foodnetwork',
        'name': 'Food Network',
        'url_template': 'https://www.foodnetwork.com/search/{}',
        'scrape': False,  # blocks server requests: link-only
    },
    {
        'key': 'recipetineats',
        'name': 'Recipe Tin Eats',
        'url_template': 'https://www.recipetineats.com/search/{}',
        'scrape': True,
    },
]

_SEARCH_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}


def _extract_recipetineats(html_text):
    """Extract (title, url) pairs from a Recipe Tin Eats search results page."""
    pattern = _re.compile(
        r'<article[^>]*aria-label="([^"]+)"[^>]*>.*?'
        r'href="(https://www\.recipetineats\.com/[^"]+)"',
        _re.S,
    )
    seen = set()
    items = []
    for title, url in pattern.findall(html_text):
        if url in seen:
            continue
        seen.add(url)
        items.append({'title': title, 'url': url})
    return items


@app.route('/search')
def search():
    """Search recipes across multiple websites."""
    query = request.args.get('q', '').strip()
    source = request.args.get('source', '').strip()

    results = []

    if not query:
        # Show search form
        return render_template('search.html', query=query, source=source, results=results)

    encoded_query = quote_plus(query)

    if not source or source == 'all':
        selected = SEARCH_SOURCES
    else:
        selected = [s for s in SEARCH_SOURCES if s['key'] == source] or list(SEARCH_SOURCES)

    for src in selected:
        search_url = src['url_template'].format(encoded_query)
        items = []
        message = ''
        if src['scrape']:
            try:
                resp = requests.get(search_url, headers=_SEARCH_HEADERS, timeout=15)
                resp.raise_for_status()
                items = _extract_recipetineats(resp.text)[:10]
                if not items:
                    message = 'No recipes matched this query on this site.'
            except Exception as exc:
                message = 'Could not fetch results ({}). Use the link below to search directly.'.format(exc.__class__.__name__)
        else:
            message = 'This site blocks automated access, so use the link below to browse its results.'
        results.append({
            'source': src['name'],
            'url': search_url,
            'items': items,
            'message': message,
        })

    return render_template('search.html', query=query, source=source, results=results)



# Start the development server
if __name__ == '__main__':
    # Local development only: loopback bind (never LAN/public), debug OFF by
    # default. Enable the dev server debugger explicitly with FLASK_DEBUG=1.
    app.run(host=os.environ.get('FLASK_RUN_HOST', '127.0.0.1'),
            port=int(os.environ.get('PORT', 5000)),
            debug=app.debug)
