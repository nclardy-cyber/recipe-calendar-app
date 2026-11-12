"""
Flask Calendar Web App
Simple calendar view with month navigation
"""

from flask import Flask, render_template, request, redirect, url_for, session, flash
# from flask_sqlalchemy import SQLAlchemy  # Disabled for now
from datetime import datetime, timedelta
import os

app = Flask(__name__)
app.secret_key = os.urandom(24)  # Secure random secret key for sessions
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///calendar.db'
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

# db = SQLAlchemy(app)  # Disabled for now

# Calendar Model  # Disabled for now
# class Recipe(db.Model):
#     id = db.Column(db.Integer, primary_key=True)
#     title = db.Column(db.String(100), nullable=False)
#     image = db.Column(db.String(200))
#     date = db.Column(db.Date)
#     description = db.Column(db.Text)

# User Model for Authentication  # Disabled for now
# class User(db.Model):
#     id = db.Column(db.Integer, primary_key=True)
#     email = db.Column(db.String(120), unique=True, nullable=False)
#     password = db.Column(db.String(120), unique=True, nullable=False)

# Initialize database  # Disabled for now
# with app.app_context():
#     db.create_all()
#     print("Database initialized!")

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
    return redirect(url_for('calendar'))


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
        month = str(now.month).zfill(2)
        year = str(now.year)
    else:
        month = str(month).zfill(2)
        year = str(year)
    
    # Calculate days in current month
    try:
        last_day = datetime(int(year), int(month), 28).strftime('%Y-%m-%d')
        last_day = datetime.strptime(last_day, '%Y-%m-%d')
        days_in_month = last_day.day
    except:
        days_in_month = 30
    
    # Calculate first day of month (0=Monday, 6=Sunday)
    first_day = datetime(int(year), int(month), 1).strftime('%Y-%m-%d')
    first_day = datetime.strptime(first_day, '%Y-%m-%d')
    start_day = first_day.weekday()
    
    # Generate calendar grid
    calendar_days = []
    for i in range(42):  # 6 weeks max
        day_num = i - start_day + 1
        if day_num <= 0:
            # Previous month
            prev_month = int(month) - 1
            if prev_month < 1:
                prev_month = 12
                prev_year = int(year) - 1
            else:
                prev_year = int(year)
            prev_month = str(prev_month).zfill(2)
            try:
                day_date = datetime(prev_year, prev_month, 28).strftime('%Y-%m-%d')
                day_date = datetime.strptime(day_date, '%Y-%m-%d')
                day_num = day_date.day
            except:
                day_num = 28
            calendar_days.append({
                'day': day_num,
                'day_str': str(day_num).zfill(2),
                'is_current_month': False,
                'day_class': 'prev-month'
            })
        elif day_num > days_in_month:
            # Next month
            next_month = int(month) + 1
            if next_month > 12:
                next_month = 1
                next_year = int(year) + 1
            else:
                next_year = int(year)
            next_month = str(next_month).zfill(2)
            try:
                day_date = datetime(next_year, next_month, 28).strftime('%Y-%m-%d')
                day_date = datetime.strptime(day_date, '%Y-%m-%d')
                day_num = day_date.day
            except:
                day_num = 28
            calendar_days.append({
                'day': day_num,
                'day_str': str(day_num).zfill(2),
                'is_current_month': False,
                'day_class': 'next-month'
            })
        else:
            # Current month
            day_date = datetime(int(year), int(month), day_num)
            # Convert to date object for consistent query
            day_date_date = day_date.date()
            recipes = Recipe.query.filter_by(date=day_date_date).all()
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
    
    # Get navigation buttons
    prev_month = int(month) - 1
    next_month = int(month) + 1
    prev_year = int(year)
    next_year = int(year)
    
    if prev_month < 1:
        prev_month = 12
        prev_year -= 1
    else:
        prev_month = str(prev_month).zfill(2)
    
    if next_month > 12:
        next_month = 1
        next_year += 1
    else:
        next_month = str(next_month).zfill(2)
    
    prev_month_url = f'?month={prev_month}&year={prev_year}'
    next_month_url = f'?month={next_month}&year={next_year}'
    
    # Month name for display
    from calendar import month_name
    month_name_display = month_name[int(month)]
    
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
        recipes = Recipe.query.filter_by(date=date).all()
        return {
            'date': date,
            'recipes': [{'id': r.id, 'title': r.title, 'image': r.image, 'description': r.description} for r in recipes]
        }
    return {'recipes': []}

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
@app.route('/day/<int:day_num>')
def day_view(day_num):
    """Show detailed view for a specific day"""
    month = request.args.get('month')
    year = request.args.get('year')
    search_query = request.args.get('search', '').strip()  # Capture search query
    
    # Get days in this month for navigation
    try:
        last_day = datetime(int(year), int(month), 28)
        days_in_month = last_day.day
    except:
        days_in_month = 30
    
    if month is None or year is None:
        now = datetime.now()
        month = str(now.month).zfill(2)
        year = str(now.year)
    else:
        month = str(month).zfill(2)
        year = str(year)
    
    # Calculate the date
    try:
        day_date = datetime(int(year), int(month), int(day_num))
        day_date = day_date.date()
        day_date_str = str(day_date)
    except:
        day_date_str = str(datetime.now().date())
    
    # Get recipes for this day
    recipes = Recipe.query.filter_by(date=day_date_str).all()
    
    # Get navigation links
    from calendar import month_name
    month_name_display = month_name[int(month)]
    
    prev_month = int(month) - 1
    next_month = int(month) + 1
    prev_year = int(year)
    next_year = int(year)
    
    if prev_month < 1:
        prev_month = 12
        prev_year -= 1
    else:
        prev_month = str(prev_month).zfill(2)
    
    if next_month > 12:
        next_month = 1
        next_year += 1
    else:
        next_month = str(next_month).zfill(2)
    
    prev_day_url = f'/day/{day_num-1}?month={prev_month}&year={prev_year}' if day_num > 1 else f'/day/1?month={prev_month}&year={prev_year}'
    next_day_url = f'/day/{day_num+1}?month={next_month}&year={next_year}' if day_num < days_in_month else f'/day/1?month={next_month}&year={next_year}'
    
    # Build back to calendar URL
    calendar_url = f'/calendar?month={month}&year={year}'
    
    return render_template('day_view.html',
                         day_num=day_num,
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
@app.route('/search')
def search():
    """Search recipes across multiple websites"""
    query = request.args.get('q', '').strip()
    source = request.args.get('source', '').strip()
    
    results = []
    
    if not query:
        # Show search form
        sources = [
            ('all', 'All Sources', 'https://www.allrecipes.com/search?q={}'.format(query)),
            ('foodnetwork', 'Food Network', 'https://www.foodnetwork.com/recipes/search?q={}'.format(query)),
            ('recipetineats', 'Recipe Tin Eats', 'https://www.recipetineats.com/search?q={}'.format(query))
        ]
        return render_template('search.html', query=query, sources=sources, results=results)
    
    # Search each source
    if source == 'all' or not source:
        # Search all sources
        for src, name, base_url in sources:
            search_url = base_url.format(query)
            try:
                from hermes_tools import web_extract
                page = web_extract([search_url])
                results.append({
                    'source': name,
                    'url': search_url,
                    'content': page[0].get('content', '') if page else ''
                })
            except Exception as e:
                results.append({
                    'source': name,
                    'url': search_url,
                    'content': f'Error: {str(e)}'
                })
    
    else:
        # Search specific source
        search_map = {
            'allrecipes': 'https://www.allrecipes.com/search?q={}',
            'foodnetwork': 'https://www.foodnetwork.com/recipes/search?q={}',
            'recipetineats': 'https://www.recipetineats.com/search?q={}'
        }
        
        if source in search_map:
            search_url = search_map[source].format(query)
            try:
                from hermes_tools import web_extract
                page = web_extract([search_url])
                results.append({
                    'source': search_map[source].split('q={}')[0].split('=')[1],
                    'url': search_url,
                    'content': page[0].get('content', '') if page else ''
                })
            except Exception as e:
                results.append({
                    'source': search_map[source].split('q={}')[0].split('=')[1],
                    'url': search_url,
                    'content': f'Error: {str(e)}'
                })
    
    return render_template('search.html', query=query, sources=sources, results=results)


# Start the development server
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
