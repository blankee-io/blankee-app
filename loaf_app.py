"""Loaf - the time off planner. Everything it serves lives under /loaf.

A blueprint rather than routes in app.py, because Loaf will have as many pages
as Blankee has and they should not be interleaved with them. It brings three
things worth having:

    /loaf/...   one prefix, declared once. A page adds a route and gets the
                prefix for free rather than repeating it and eventually
                mistyping it.

    loaf.*      one endpoint namespace. apps_registry attributes a request by
                the part before the dot, so a page added here is recognised as
                Loaf's without being listed anywhere.

    one gate    the check below runs for every route in the blueprint,
                including the ones not written yet. Guarding each route
                separately is a guard someone forgets on the twentieth page.

Templates live in templates/loaf/ for the same reason the routes live here.

Data access goes through loaf_data, which is MySQL-first and scopes every write
by user - see its docstring. Nothing in here talks to Redis or MySQL directly.
"""

import json

from flask import Blueprint, render_template, request, jsonify, abort
from flask_login import login_required, current_user

import apps_registry
import loaf_data
from db_connections import get_db_pool
from log_config import get_logger, log_exception

logger = get_logger(__name__)

loaf = Blueprint('loaf', __name__, url_prefix='/loaf')


@loaf.before_request
def _require_enabled():
    """Nothing in Loaf answers unless this instance has Loaf switched on.

    On the blueprint rather than on each route: hiding the menu entry is not
    access control, and a per-route check is one somebody eventually leaves off
    a new page. This one covers pages that do not exist yet.
    """
    if not apps_registry.is_enabled('loaf'):
        abort(404)


def _shell():
    """The context nav.html needs that is not injected for it.

    nav_first_name, app_version and current_app all arrive by context
    processor, but profile_picture is passed per route in this codebase - so
    Loaf's pages passed nothing and showed the default avatar even to someone
    who had uploaded one. The profile picture is one of exactly two things the
    apps share, so getting it wrong was visible on every Loaf page.

    Redis first with a MySQL fallback, the same order every Blankee route uses.
    """
    picture = None
    try:
        import redis_manager
        client = getattr(redis_manager, '_redis_client', None)
        if client:
            cached = client.get('users:v1:%s' % current_user.id)
            if cached:
                picture = (json.loads(cached) or {}).get('profile_picture')
    except Exception:
        picture = None

    if picture is None:
        try:
            with get_db_pool().get_cursor(dictionary=True) as cursor:
                cursor.execute(
                    'SELECT profile_picture FROM users WHERE id = %s',
                    (current_user.id,))
                row = cursor.fetchone()
                picture = row.get('profile_picture') if row else None
        except Exception as e:
            log_exception(logger, 'LOAF', 'Could not read profile picture: %s' % e)

    return {'profile_picture': picture}


def _payload():
    """The request body, whether it arrived as JSON or as a form.

    get_json(silent=True) rather than force: a form post is a legitimate way to
    reach these endpoints and should not raise on the way in.
    """
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        return body
    return request.form.to_dict(flat=False) if request.form else {}


def _one(payload, key, default=None):
    """A single value out of a payload that may hold lists.

    request.form.to_dict(flat=False) gives every field as a list so the
    multi-valued ones (weekdays, monthly_days) survive; the rest have to be
    unwrapped. A JSON body needs neither.
    """
    value = payload.get(key, default)
    if isinstance(value, list):
        return value[0] if value else default
    return value


def _flat(payload):
    """A payload with single-valued fields unwrapped and lists left alone."""
    multi = ('weekdays', 'monthly_days')
    out = {}
    for key, value in payload.items():
        out[key] = value if key in multi else _one(payload, key)
    return out


# ------------------------------------------------------------------ pages ----

@loaf.route('/')
@login_required
def home():
    """Whatever Loaf opens on - its own dashboard."""
    return dashboard()


@loaf.route('/dashboard')
@login_required
def dashboard():
    """The month view for one basket. Still the placeholder."""
    return render_template('loaf/dashboard.html', **_shell())


@loaf.route('/baskets')
@login_required
def baskets():
    """Manage the pools of hours: what they hold, how they fill, the work week.

    The whole basket is handed to the template so the edit modal can be filled
    from data-* attributes rather than by scraping the table - the one thing
    most worth copying from recurring_i.html.
    """
    return render_template(
        'loaf/baskets.html',
        baskets=loaf_data.get_baskets(current_user.id),
        weekday_prefixes=loaf_data.WEEKDAY_PREFIXES,
        weekday_names=loaf_data.WEEKDAY_NAMES,
        # Passed in rather than registered as template filters: they are only
        # wanted on Loaf's pages, and app_template_filter would put them in
        # every Blankee template's namespace too.
        describe_fill=loaf_data.describe_fill,
        describe_carryover=loaf_data.describe_carryover,
        **_shell())


# --------------------------------------------------------------- the API ----

@loaf.route('/api/baskets', methods=['POST'])
@login_required
def api_create_basket():
    values, error = loaf_data.clean_basket(_flat(_payload()))
    if error:
        return jsonify({'status': 'error', 'message': error}), 400

    if loaf_data.basket_name_exists(current_user.id, values['name']):
        return jsonify({'status': 'error',
                        'message': 'You already have a basket called "%s".'
                                   % values['name']}), 400

    # New baskets go to the top, the way a new category does. Half a step above
    # the current highest, so the fractional ordering keeps working without
    # renumbering anything.
    existing = loaf_data.get_baskets(current_user.id)
    highest = max([float(b.get('display_order') or 0) for b in existing] or [0.0])
    values['display_order'] = highest + 1.0

    basket_id = loaf_data.create_basket(current_user.id, values)
    if basket_id is None:
        return jsonify({'status': 'error',
                        'message': 'Could not save that basket.'}), 500

    return jsonify({'status': 'success', 'basket_id': basket_id})


@loaf.route('/api/baskets/<int:basket_id>', methods=['POST'])
@login_required
def api_update_basket(basket_id):
    if loaf_data.get_basket(current_user.id, basket_id) is None:
        return jsonify({'status': 'error', 'message': 'No such basket.'}), 404

    values, error = loaf_data.clean_basket(_flat(_payload()))
    if error:
        return jsonify({'status': 'error', 'message': error}), 400

    if loaf_data.basket_name_exists(current_user.id, values['name'],
                                    exclude_id=basket_id):
        return jsonify({'status': 'error',
                        'message': 'You already have a basket called "%s".'
                                   % values['name']}), 400

    # display_order is not in the form. Left out of the update so a save does
    # not quietly undo a drag-reorder.
    values.pop('display_order', None)

    if not loaf_data.update_basket(current_user.id, basket_id, values):
        return jsonify({'status': 'error',
                        'message': 'Could not save that basket.'}), 500
    return jsonify({'status': 'success'})


@loaf.route('/api/baskets/<int:basket_id>/delete', methods=['POST'])
@login_required
def api_delete_basket(basket_id):
    if loaf_data.get_basket(current_user.id, basket_id) is None:
        return jsonify({'status': 'error', 'message': 'No such basket.'}), 404
    if not loaf_data.delete_basket(current_user.id, basket_id):
        return jsonify({'status': 'error',
                        'message': 'Could not delete that basket.'}), 500
    return jsonify({'status': 'success'})


@loaf.route('/api/basket-order', methods=['POST'])
@login_required
def api_basket_order():
    """New order for the basket list, top first.

    The client sends ids in the order they now appear and the server assigns
    the numbers, rather than the client inventing them. One less thing that can
    drift, and it means the fractional scheme stays the server's business.
    """
    payload = _payload()
    ids = payload.get('basket_ids') or payload.get('basket_ids[]') or []
    if not isinstance(ids, list):
        ids = [ids]

    try:
        ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'Bad ordering.'}), 400

    mine = {int(b['id']) for b in loaf_data.get_baskets(current_user.id)}
    if not ids or set(ids) - mine:
        return jsonify({'status': 'error',
                        'message': 'That ordering does not match your baskets.'}), 400

    # Descending, because the list is read display_order DESC.
    order = [(basket_id, float(len(ids) - position))
             for position, basket_id in enumerate(ids)]
    if not loaf_data.set_basket_order(current_user.id, order):
        return jsonify({'status': 'error',
                        'message': 'Could not save the new order.'}), 500
    return jsonify({'status': 'success'})
