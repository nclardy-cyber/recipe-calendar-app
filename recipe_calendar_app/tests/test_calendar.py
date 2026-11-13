"""Calendar date calculations, navigation, and route responses.

All tests use the temporary-database fixtures from conftest.py.
"""
import re
from datetime import datetime

# Grid cells: class="day-cell <cls> [clickable]" then optional attributes
# (onclick on clickable cells) before the ">", followed by the day-number div.
DAY_CELL_RE = re.compile(
    r'class="day-cell ((?:current-month|prev-month|next-month)(?: clickable)?)"'
    r'[^>]*>\s*<div class="day-number">(\d{2})</div>', re.S)
# Calendar nav URLs appear in document order: prev, next (expression output,
# so & is autoescaped)
CAL_URL_RE = re.compile(r"window\.location\.href='(\?month=\d{2}&amp;year=\d{4})'")
# Day-view nav URLs appear in document order: prev day, next day
DAY_URL_RE = re.compile(r"window\.location\.href='(/day/\d+\?month=\d{2}&amp;year=\d{4})'")


def _grid(html):
    """List of (cls, day) tuples in document order."""
    return [(cls, int(d)) for cls, d in DAY_CELL_RE.findall(html)]


def _current_days(html):
    return [d for cls, d in _grid(html) if cls.split()[0] == 'current-month']


# --- Month length (leap years included) ------------------------------------

def test_february_leap_year_2024_has_29_days(client):
    html = client.get('/calendar?month=02&year=2024').get_data(as_text=True)
    assert _current_days(html) == list(range(1, 30))


def test_february_common_year_2025_has_28_days(client):
    html = client.get('/calendar?month=02&year=2025').get_data(as_text=True)
    assert _current_days(html) == list(range(1, 29))


def test_april_has_30_days(client):
    html = client.get('/calendar?month=04&year=2026').get_data(as_text=True)
    assert _current_days(html) == list(range(1, 31))


def test_january_has_31_days(client):
    html = client.get('/calendar?month=01&year=2026').get_data(as_text=True)
    assert _current_days(html) == list(range(1, 32))


def test_december_has_31_days(client):
    html = client.get('/calendar?month=12&year=2026').get_data(as_text=True)
    assert _current_days(html) == list(range(1, 32))


def test_every_month_length(client):
    from calendar import monthrange
    for m in range(1, 13):
        html = client.get(f'/calendar?month={m:02d}&year=2024').get_data(as_text=True)
        assert _current_days(html) == list(range(1, monthrange(2024, m)[1] + 1)), m


# --- Adjacent-month cells ----------------------------------------------------

def test_days_29_to_31_not_replaced_by_next_month(client):
    """Old bug: days_in_month was always 28, so Jan 29-31 rendered as next-month."""
    html = client.get('/calendar?month=01&year=2026').get_data(as_text=True)
    grid = _grid(html)
    current = [d for cls, d in grid if cls.split()[0] == 'current-month']
    assert {29, 30, 31} <= set(current)
    next_cells = [d for cls, d in grid if cls.split()[0] == 'next-month']
    # Trailing cells must be 1, 2, 3, ... of the next month
    assert next_cells == list(range(1, len(next_cells) + 1))


def test_leading_prev_month_cells_are_real_dates(client):
    """Old bug: every leading cell was hardcoded to day 28."""
    # March 1, 2026 is a Sunday -> leading cells are Feb 23..28
    html = client.get('/calendar?month=03&year=2026').get_data(as_text=True)
    grid = _grid(html)
    prev = [d for cls, d in grid if cls.split()[0] == 'prev-month']
    assert prev == list(range(23, 29))
    # January 2026: Jan 1 is a Thursday -> leading cells are Dec 29..31, 2025
    html = client.get('/calendar?month=01&year=2026').get_data(as_text=True)
    grid = _grid(html)
    prev = [d for cls, d in grid if cls.split()[0] == 'prev-month']
    assert prev == [29, 30, 31]


# --- Month navigation ---------------------------------------------------------

def test_december_to_january_navigation(client):
    html = client.get('/calendar?month=12&year=2026').get_data(as_text=True)
    urls = CAL_URL_RE.findall(html)
    assert urls == ['?month=11&amp;year=2026', '?month=01&amp;year=2027']


def test_january_to_december_navigation(client):
    html = client.get('/calendar?month=01&year=2027').get_data(as_text=True)
    urls = CAL_URL_RE.findall(html)
    assert urls == ['?month=12&amp;year=2026', '?month=02&amp;year=2027']


def test_default_month_is_current(client):
    now = datetime.now()
    html = client.get('/calendar').get_data(as_text=True)
    # The month title (header h1) must reflect the current month
    assert f'<h1 id="month-title">{now.strftime("%B")} {now.year}</h1>' in html
    assert _current_days(html), 'current month must render its days'


def test_invalid_query_params_fall_back_to_current_month(client):
    now = datetime.now()
    title = f'<h1 id="month-title">{now.strftime("%B")} {now.year}</h1>'
    for q in ('?month=99&year=2026', '?month=abc&year=xyz', '?month=0&year=0'):
        html = client.get(f'/calendar{q}').get_data(as_text=True)
        assert title in html, q


# --- Day view ------------------------------------------------------------------

def test_day_view_defaults_to_current_month(client):
    now = datetime.now()
    resp = client.get('/day/15')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # Form action uses a literal '&' (template literal, not autoescaped)
    assert f'/day/15?month={now.month:02d}&year={now.year}' in html


def test_day_view_feb_29_leap_year(client):
    html = client.get('/day/29?month=02&year=2024').get_data(as_text=True)
    assert 'February 29, 2024' in html


def test_day_view_feb_29_nonleap_clamps_to_last_day(client):
    """Old behavior fell back to *today* on invalid day; now it clamps."""
    resp = client.get('/day/29?month=02&year=2025')
    assert resp.status_code == 200
    assert 'February 28, 2025' in resp.get_data(as_text=True)


def test_day_view_last_day_next_wraps_to_next_month(client):
    html = client.get('/day/31?month=01&year=2026').get_data(as_text=True)
    urls = DAY_URL_RE.findall(html)
    assert urls[1] == '/day/1?month=02&amp;year=2026'


def test_day_view_first_day_prev_wraps_to_prev_month(client):
    html = client.get('/day/1?month=03&year=2026').get_data(as_text=True)
    urls = DAY_URL_RE.findall(html)
    assert urls[0] == '/day/28?month=02&amp;year=2026'


def test_day_view_mid_month_stays_in_same_month(client):
    """Old bug: same-month prev/next day links pointed into the prev/next month."""
    html = client.get('/day/15?month=11&year=2026').get_data(as_text=True)
    urls = DAY_URL_RE.findall(html)
    assert urls[0] == '/day/14?month=11&amp;year=2026'
    assert urls[1] == '/day/16?month=11&amp;year=2026'


def test_day_view_december_first_wraps_year_backwards(client):
    html = client.get('/day/1?month=01&year=2026').get_data(as_text=True)
    urls = DAY_URL_RE.findall(html)
    assert urls[0] == '/day/31?month=12&amp;year=2025'


# --- Route responses -------------------------------------------------------------

def test_root_returns_calendar(client):
    resp = client.get('/')
    assert resp.status_code == 200
    assert 'day-cell' in resp.get_data(as_text=True)


def test_calendar_route(client):
    assert client.get('/calendar?month=06&year=2026').status_code == 200


def test_login_signup_logout_routes(client):
    assert client.get('/login').status_code == 200
    assert client.get('/signup').status_code == 200
    # Login POST redirects into the app
    resp = client.post('/login', data={'email': 'a@b.com', 'password': 'x'},
                       follow_redirects=False)
    assert resp.status_code == 302
    assert client.get('/logout').status_code == 302


def test_search_form_renders_without_query(client):
    resp = client.get('/search')
    assert resp.status_code == 200


def test_api_recipe_roundtrip(app_module, client):
    from helpers import make_user, login
    make_user(app_module, 'roundtrip@example.com')
    login(client, 'roundtrip@example.com')
    # Create
    resp = client.post('/api/recipes', json={'title': 'Test Soup', 'date': '2026-04-05'})
    assert resp.status_code == 201
    recipe_id = resp.get_json()['id']
    # Read by date
    got = client.get('/api/recipes?date=2026-04-05').get_json()
    assert got['recipes'][0]['title'] == 'Test Soup'
    # Read by id
    got = client.get(f'/api/recipes/{recipe_id}').get_json()
    assert got['title'] == 'Test Soup'
    # Update
    resp = client.put(f'/api/recipes/{recipe_id}',
                      json={'title': 'Updated Soup', 'date': '2026-04-06'})
    assert resp.status_code == 200
    assert resp.get_json()['date'] == '2026-04-06'
    # Calendar page shows it on the right day
    html = client.get('/calendar?month=04&year=2026').get_data(as_text=True)
    assert 'Updated Soup' in html
    # Delete
    resp = client.delete(f'/api/recipes/{recipe_id}')
    assert resp.status_code == 200
    assert client.get(f'/api/recipes/{recipe_id}').status_code == 404


def test_api_recipe_validation(app_module, client):
    from helpers import make_user, login
    make_user(app_module, 'validation@example.com')
    login(client, 'validation@example.com')
    assert client.post('/api/recipes', json={'date': '2026-04-05'}).status_code == 400
    assert client.post('/api/recipes', json={'title': 'X', 'date': 'nope'}).status_code == 400
    assert client.get('/api/recipes?date=garbage').get_json() == {'date': 'garbage', 'recipes': []}


# --- PUT title length limit (CMP-006A) ----------------------------------------
# The recipe title column is String(100). PostgreSQL would reject an over-long
# title with a database error (500) and SQLite would silently store it - the
# API must reject it with 400 before it reaches the database.

def test_api_put_title_over_100_chars_rejected(app_module, client):
    from helpers import make_user, login
    make_user(app_module, 'putlimit@example.com')
    login(client, 'putlimit@example.com')
    recipe_id = client.post('/api/recipes',
                            json={'title': 'Seed', 'date': '2026-04-05'}
                            ).get_json()['id']
    resp = client.put(f'/api/recipes/{recipe_id}',
                      json={'title': 'x' * 101})
    assert resp.status_code == 400
    assert '100 characters' in resp.get_json()['error']
    # The title on the database is unchanged.
    assert client.get(f'/api/recipes/{recipe_id}').get_json()['title'] == 'Seed'


def test_api_put_title_exactly_100_chars_accepted(app_module, client):
    from helpers import make_user, login
    make_user(app_module, 'putlimit100@example.com')
    login(client, 'putlimit100@example.com')
    recipe_id = client.post('/api/recipes',
                            json={'title': 'Seed', 'date': '2026-04-05'}
                            ).get_json()['id']
    title = 'y' * 100
    resp = client.put(f'/api/recipes/{recipe_id}', json={'title': title})
    assert resp.status_code == 200
    assert client.get(f'/api/recipes/{recipe_id}').get_json()['title'] == title


def test_api_put_blank_title_rejected(app_module, client):
    from helpers import make_user, login
    make_user(app_module, 'putblank@example.com')
    login(client, 'putblank@example.com')
    recipe_id = client.post('/api/recipes',
                            json={'title': 'Seed', 'date': '2026-04-05'}
                            ).get_json()['id']
    assert client.put(f'/api/recipes/{recipe_id}',
                      json={'title': '   '}).status_code == 400
    assert client.get(f'/api/recipes/{recipe_id}').get_json()['title'] == 'Seed'
