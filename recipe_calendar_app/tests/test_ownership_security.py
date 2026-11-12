"""CMP-003C: recipe ownership and application security tests.

Covers the security hardening added to the recipe API:

* guests (unauthenticated) get 401 on every recipe endpoint - no listing,
  no reads, no writes
* authenticated users only ever see / read / write rows they own
* another user's recipe id, a legacy (unowned) recipe id and a
  non-existent id all produce the identical 404 (no existence leak)
* legacy rows (user_id IS NULL) are invisible to every account
* API errors are JSON, not HTML
* production mode: secure state-changing requests require a same-origin
  Referer (Flask-WTF SSL-strict CSRF)
* the clean-samples maintenance CLI refuses to run in production and in
  development only deletes rows whose title contains 'Sample'
"""
from datetime import date

from helpers import (DEFAULT_PASSWORD, csrf_wrapped, get_csrf_token, login,
                     make_user)

DATE_A = '2026-03-15'
DATE_B = '2026-03-16'

ALICE = 'owner-a@example.com'
BOB = 'owner-b@example.com'


# --- helpers ------------------------------------------------------------------

def _user_id(app_module, email):
    """Primary key of an account (ORM access inside an app context)."""
    with app_module.app.app_context():
        return app_module.User.query.filter_by(email=email).first().id


def _make_recipe(app_module, owner_id, title, when=DATE_A):
    """Insert a recipe owned by ``owner_id`` and return its id."""
    with app_module.app.app_context():
        recipe = app_module.Recipe(
            title=title, date=date.fromisoformat(when), user_id=owner_id)
        app_module.db.session.add(recipe)
        app_module.db.session.commit()
        return recipe.id


def _get_recipe(app_module, recipe_id):
    with app_module.app.app_context():
        return app_module.db.session.get(app_module.Recipe, recipe_id)


def _recipe_count(app_module):
    with app_module.app.app_context():
        return app_module.Recipe.query.count()


def _json(client, url, method='get', **kwargs):
    """Run ``method`` against ``url``; return (status, parsed body).

    Dispatches through the client's native verb methods so a CSRF-wrapped
    client (see ``helpers.csrf_wrapped``) injects its token.
    """
    resp = getattr(client, method.lower())(url, **kwargs)
    return resp.status_code, (resp.get_json(silent=True) if resp.is_json
                              else resp.get_data(as_text=True))


def _two_logged_in(app_module, client):
    """Log user A in via ``client`` and user B in via a second client.

    Returns the second (CSRF-wrapped) client for user B.
    """
    make_user(app_module, ALICE)
    make_user(app_module, BOB)
    login(client, ALICE)
    b_client = csrf_wrapped(app_module.app.test_client())
    login(b_client, BOB)
    return b_client


# --- guest (unauthenticated) access -------------------------------------------

def test_guest_recipe_list_requires_auth(raw_client):
    status, body = _json(raw_client, '/api/recipes')
    assert status == 401
    assert body == {'error': 'authentication required'}


def test_guest_recipe_create_requires_auth(client):
    # Wrapped client: valid CSRF token, but no session -> view-level 401.
    status, body = _json(client, '/api/recipes', method='post',
                         json={'title': 'Nope', 'date': DATE_A})
    assert status == 401
    assert body == {'error': 'authentication required'}


def test_guest_recipe_read_requires_auth(raw_client):
    status, body = _json(raw_client, '/api/recipes/1')
    assert status == 401
    assert body == {'error': 'authentication required'}


def test_guest_recipe_update_requires_auth(client):
    status, body = _json(client, '/api/recipes/1', method='put',
                         json={'title': 'Hijack'})
    assert status == 401
    assert body == {'error': 'authentication required'}


def test_guest_recipe_delete_requires_auth(client):
    status, body = _json(client, '/api/recipes/1', method='delete')
    assert status == 401
    assert body == {'error': 'authentication required'}


# --- ownership isolation between two users ------------------------------------

def test_users_see_only_their_own_recipes(app_module, client):
    b_client = _two_logged_in(app_module, client)
    aid = _user_id(app_module, ALICE)
    bid = _user_id(app_module, BOB)
    for i in (1, 2):
        _make_recipe(app_module, aid, f'Alice recipe {i}')
    _make_recipe(app_module, bid, 'Bob recipe')

    alice = client.get(f'/api/recipes?date={DATE_A}')
    bob = b_client.get(f'/api/recipes?date={DATE_A}')
    alice_titles = {r['title'] for r in alice.get_json()['recipes']}
    bob_titles = {r['title'] for r in bob.get_json()['recipes']}
    assert alice_titles == {'Alice recipe 1', 'Alice recipe 2'}
    assert bob_titles == {'Bob recipe'}


def test_user_cannot_read_others_recipe(app_module, client):
    b_client = _two_logged_in(app_module, client)
    a_id = _make_recipe(app_module, _user_id(app_module, ALICE), 'Secret')
    _make_recipe(app_module, _user_id(app_module, BOB), 'Bobs')

    status, body = _json(b_client, f'/api/recipes/{a_id}')
    assert status == 404
    assert body == {'error': 'not found'}
    # And the owner still gets it.
    status, body = _json(client, f'/api/recipes/{a_id}')
    assert status == 200
    assert body['title'] == 'Secret'


def test_user_cannot_update_others_recipe(app_module, client):
    b_client = _two_logged_in(app_module, client)
    a_id = _make_recipe(app_module, _user_id(app_module, ALICE), 'Original')
    _make_recipe(app_module, _user_id(app_module, BOB), 'Bobs')

    status, body = _json(b_client, f'/api/recipes/{a_id}', method='put',
                         json={'title': 'TAMPERED'})
    assert status == 404
    assert body == {'error': 'not found'}
    assert _get_recipe(app_module, a_id).title == 'Original'


def test_user_cannot_delete_others_recipe(app_module, client):
    b_client = _two_logged_in(app_module, client)
    a_id = _make_recipe(app_module, _user_id(app_module, ALICE), 'Precious')
    _make_recipe(app_module, _user_id(app_module, BOB), 'Bobs')

    status, body = _json(b_client, f'/api/recipes/{a_id}', method='delete')
    assert status == 404
    assert body == {'error': 'not found'}
    assert _get_recipe(app_module, a_id) is not None


def test_cross_user_404_indistinguishable_from_missing(app_module, client):
    b_client = _two_logged_in(app_module, client)
    a_id = _make_recipe(app_module, _user_id(app_module, ALICE), 'Secret')
    _make_recipe(app_module, _user_id(app_module, BOB), 'Bobs')

    other = b_client.get(f'/api/recipes/{a_id}')
    missing = b_client.get('/api/recipes/999999')
    assert other.status_code == missing.status_code == 404
    assert other.get_data(as_text=True) == missing.get_data(as_text=True)


def test_legacy_unowned_recipes_invisible(app_module, client):
    make_user(app_module, ALICE)
    login(client, ALICE)
    with app_module.app.app_context():
        legacy = app_module.Recipe(title='Legacy shared',
                                   date=date.fromisoformat(DATE_A),
                                   user_id=None)
        app_module.db.session.add(legacy)
        app_module.db.session.commit()
        legacy_id = legacy.id

    # Not in the owner's date listing...
    listing = client.get(f'/api/recipes?date={DATE_A}').get_json()['recipes']
    assert all(r['id'] != legacy_id for r in listing)
    # ...and not fetchable by id either.
    status, body = _json(client, f'/api/recipes/{legacy_id}')
    assert status == 404
    assert body == {'error': 'not found'}


def test_created_recipe_owned_by_creator(app_module, client):
    make_user(app_module, ALICE)
    login(client, ALICE)
    aid = _user_id(app_module, ALICE)

    status, body = _json(client, '/api/recipes', method='post',
                         json={'title': 'Mine', 'date': DATE_A})
    assert status == 201
    assert body['owner'] == ALICE
    assert _get_recipe(app_module, body['id']).user_id == aid


def test_own_recipe_crud_roundtrip(app_module, client):
    make_user(app_module, ALICE)
    login(client, ALICE)

    status, created = _json(client, '/api/recipes', method='post',
                            json={'title': 'Pancakes', 'date': DATE_A,
                                  'description': 'classic'})
    assert status == 201
    rid = created['id']

    status, body = _json(client, f'/api/recipes/{rid}')
    assert status == 200
    assert body['title'] == 'Pancakes'

    status, body = _json(client, f'/api/recipes/{rid}', method='put',
                         json={'title': 'Fluffy pancakes'})
    assert status == 200
    assert body['title'] == 'Fluffy pancakes'

    status, body = _json(client, f'/api/recipes/{rid}', method='delete')
    assert status == 200
    assert body['deleted'] is True
    assert _get_recipe(app_module, rid) is None


def test_create_recipe_validation(app_module, client):
    make_user(app_module, ALICE)
    login(client, ALICE)

    status, body = _json(client, '/api/recipes', method='post',
                         json={'date': DATE_A})
    assert status == 400
    assert 'required' in body['error']

    status, body = _json(client, '/api/recipes', method='post',
                         json={'title': 'X', 'date': '15/03/2026'})
    assert status == 400
    assert 'YYYY-MM-DD' in body['error']


# --- API error responses -------------------------------------------------------

def test_api_404_and_405_are_json(app_module, client):
    make_user(app_module, ALICE)
    login(client, ALICE)
    a_id = _make_recipe(app_module, _user_id(app_module, ALICE), 'Existing')

    not_found = client.get('/api/recipes/999999')
    assert not_found.status_code == 404
    assert not_found.is_json
    assert not_found.get_json() == {'error': 'not found'}
    assert '<html' not in not_found.get_data(as_text=True).lower()

    bad_method = client.post(f'/api/recipes/{a_id}')
    assert bad_method.status_code == 405
    assert bad_method.is_json
    assert bad_method.get_json() == {'error': 'method not allowed'}


# --- production: SSL-strict referrer enforcement --------------------------------

def test_production_missing_referrer_rejected(prod_app):
    with prod_app.app.test_client() as raw:
        token = get_csrf_token(raw)
        resp = raw.post('/signup', data={
            'email': 'ref@example.com', 'password': DEFAULT_PASSWORD,
            'csrf_token': token})
        assert resp.status_code == 400
        assert 'referrer header is missing' in resp.get_data(as_text=True)


def test_production_cross_origin_referrer_rejected(prod_app):
    with prod_app.app.test_client() as raw:
        token = get_csrf_token(raw)
        resp = raw.post('/signup', data={
            'email': 'ref@example.com', 'password': DEFAULT_PASSWORD,
            'csrf_token': token},
            headers={'Referer': 'https://evil.example.com/'})
        assert resp.status_code == 400
        assert 'referrer' in resp.get_data(as_text=True).lower()


def test_production_same_origin_referrer_accepted(prod_app):
    with prod_app.app.test_client() as raw:
        token = get_csrf_token(raw)
        resp = raw.post('/signup', data={
            'email': 'ref@example.com', 'password': DEFAULT_PASSWORD,
            'csrf_token': token},
            headers={'Referer': 'https://localhost/'})
        assert resp.status_code == 302
    with prod_app.app.app_context():
        user = prod_app.User.query.filter_by(email='ref@example.com').first()
    assert user is not None


# --- clean-samples maintenance CLI ----------------------------------------------

def test_clean_samples_refused_in_production(prod_app):
    make_user(prod_app, ALICE)
    _make_recipe(prod_app, _user_id(prod_app, ALICE), 'Sample Pancakes')
    assert _recipe_count(prod_app) == 1

    runner = prod_app.app.test_cli_runner()
    result = runner.invoke(prod_app.clean_samples_command, ['--yes'])
    assert result.exit_code == 2
    assert 'Refusing to run in production' in result.output
    assert _recipe_count(prod_app) == 1  # nothing deleted


def test_clean_samples_dev_deletes_only_sample_titles(app_module):
    make_user(app_module, ALICE)
    aid = _user_id(app_module, ALICE)
    _make_recipe(app_module, aid, 'Sample Pancakes')
    _make_recipe(app_module, aid, 'Grandma Pancakes', when=DATE_B)

    runner = app_module.app.test_cli_runner()
    result = runner.invoke(app_module.clean_samples_command, ['--yes'])
    assert result.exit_code == 0
    assert 'Deleted 1 sample recipe(s).' in result.output

    with app_module.app.app_context():
        titles = {r.title for r in app_module.Recipe.query.all()}
    assert titles == {'Grandma Pancakes'}

    # Second run: nothing left to do.
    result = runner.invoke(app_module.clean_samples_command, ['--yes'])
    assert result.exit_code == 0
    assert 'nothing to do' in result.output


def test_clean_samples_prompt_default_aborts(app_module):
    make_user(app_module, ALICE)
    _make_recipe(app_module, _user_id(app_module, ALICE), 'Sample Pancakes')

    runner = app_module.app.test_cli_runner()
    result = runner.invoke(app_module.clean_samples_command, [], input='\n')
    assert result.exit_code == 0
    assert 'Aborted' in result.output
    assert _recipe_count(app_module) == 1
