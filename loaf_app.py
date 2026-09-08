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
from datetime import date, timedelta

from flask import Blueprint, render_template, request, jsonify, abort
from flask_login import login_required, current_user

import apps_registry
import loaf_data
import loaf_forecast
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


def _today():
    """Today where the user is, not where the server is.

    bucket_confirmation._user_today is the one implementation of this, and it
    lives outside app.py - so Loaf can use it without importing the app that
    imports Loaf. The Docker image runs UTC, where the server's date runs ahead
    of a user's for a good part of their day.
    """
    try:
        import bucket_confirmation
        return bucket_confirmation._user_today(current_user.id)
    except Exception:
        return date.today()


def _pick_basket(wanted):
    """The basket being looked at: the one asked for, or the first there is.

    Scoped through loaf_data, so an id belonging to someone else simply is not
    found rather than being read and refused.
    """
    baskets = loaf_data.get_baskets(current_user.id, include_hidden=False)
    if not baskets:
        return None
    if wanted:
        try:
            return loaf_data.get_basket(current_user.id, int(wanted))
        except (TypeError, ValueError):
            return None
    return baskets[0]


@loaf.route('/dashboard')
@login_required
def dashboard():
    """One basket, one month: what was worked, what was taken, what is left.

    The grid itself is fetched and drawn by /api/month, the way dashboard_m
    builds its calendar - so changing month or swiping is one request rather
    than a page load, and the cell markup exists in one place instead of once
    in Jinja and once in JS.
    """
    today = _today()
    basket = _pick_basket(request.args.get('basket'))
    return render_template(
        'loaf/dashboard.html',
        baskets=loaf_data.get_baskets(current_user.id, include_hidden=False),
        basket=basket,
        today=today,
        # _basket_modal.html carries these on the element for its script to
        # read, so any page including that form has to pass them.
        weekday_prefixes=loaf_data.WEEKDAY_PREFIXES,
        weekday_names=loaf_data.WEEKDAY_NAMES,
        **_shell())


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
        today=_today(),
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

    # "Hours you have now" means now, so the form no longer asks when. Stamped
    # here instead, because the projection replays from this date and a figure
    # entered today against a date from months ago would replay wrongly.
    #
    # Only when nothing was given, though. The form sends nothing; a caller
    # that does say when means it, and overwriting that silently would make the
    # endpoint unable to express a basket that started earlier.
    if not values.get('starting_date'):
        values['starting_date'] = _today().isoformat()

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

    # The starting date moves only when the balance it describes moves. Editing
    # a name would otherwise re-date the figure and shift the whole projection;
    # leaving it alone forever would date a new figure to an old day.
    was = loaf_data.get_basket(current_user.id, basket_id) or {}
    if values.get('starting_date'):
        pass                      # the caller said when; take them at their word
    elif abs(float(values.get('starting_hours') or 0)
             - float(was.get('starting_hours') or 0)) > 0.005:
        values['starting_date'] = _today().isoformat()
    else:
        values.pop('starting_date', None)

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


# ------------------------------------------------- the month, as JSON ----

def _month_payload(user_id, basket, year, month, today):
    """Everything the calendar draws for one basket in one month.

    Built here rather than in the template because the grid is assembled in
    JS - the same division dashboard_m makes, and it means a month change or a
    swipe is one fetch rather than a page load.
    """
    entries = loaf_data.get_entries(user_id)
    baskets = loaf_data.get_baskets(user_id)

    # usage is this basket's; absence is everyone's, because any hour not
    # worked pro-rates the accrual whichever basket it came out of.
    usage = loaf_forecast.usage_by_date(basket, entries)
    absence = loaf_forecast.absence_by_date(baskets, entries)

    viewing = date(int(year), int(month), 1)
    through = loaf_forecast.horizon_for(basket, viewing, today=today)
    result = loaf_forecast.project(basket, usage, absence, through, today=today)

    grid_start, grid_end, first, last = loaf_forecast.grid_bounds(year, month)
    rows = loaf_forecast.month_rows(basket, result, absence, grid_start, grid_end)

    # Which bookings touch each day, so a click can open the one that is
    # already there instead of always adding another.
    mine = int(basket['id'])
    touching = {}
    for entry in entries:
        if int(entry.get('basket_id') or 0) != mine:
            continue
        starts = str(entry.get('starts_at') or '')[:10]
        ends = str(entry.get('ends_at') or '')[:10]
        if not starts:
            continue
        day = date.fromisoformat(starts)
        stop = date.fromisoformat(ends) if ends else day
        while day <= stop:
            touching.setdefault(day.isoformat(), []).append({
                'id': entry.get('id'),
                'starts_at': entry.get('starts_at'),
                'ends_at': entry.get('ends_at'),
                'all_day': int(entry.get('all_day') or 0),
                'hours': entry.get('hours'),
                'hours_overridden': int(entry.get('hours_overridden') or 0),
                'status': entry.get('status'),
                'note': entry.get('note'),
            })
            day += timedelta(days=1)

    out = []
    for row in rows:
        stamp = row['date'].isoformat()
        out.append({
            'date': stamp,
            'day': row['date'].day,
            'weekday': row['date'].weekday(),
            'in_month': first <= row['date'] <= last,
            'is_today': row['date'] == today,
            'non_working': row['non_working'],
            # What the working week says the day is worth. The booking modal
            # seeds its hours box with it, so a whole day off needs no sum.
            'scheduled': row['scheduled'],
            'worked': row['worked'],
            'taken': row['taken'],
            'accrued': row['accrued'],
            'granted': row['granted'],
            'balance': row['balance'],
            'entries': touching.get(stamp, []),
        })

    summary = dict(result['summary'])
    summary.pop('checkpoints', None)
    for key in ('year_end_date', 'lowest_date', 'first_negative'):
        if summary.get(key) is not None:
            summary[key] = summary[key].isoformat()

    return {'rows': out, 'summary': summary,
            'first': first.isoformat(), 'last': last.isoformat()}


@loaf.route('/api/month')
@login_required
def api_month():
    today = _today()
    basket = _pick_basket(request.args.get('basket_id'))
    if basket is None:
        return jsonify({'status': 'error', 'message': 'No such basket.'}), 404

    try:
        year = int(request.args.get('year') or today.year)
        month = int(request.args.get('month') or today.month)
        if not 1 <= month <= 12 or not 1970 <= year <= 2999:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'That is not a month.'}), 400

    payload = _month_payload(current_user.id, basket, year, month, today)
    payload['status'] = 'success'
    payload['basket'] = {
        'id': basket['id'], 'name': basket['name'],
        'basket_type': basket['basket_type'],
        'low_balance_hours': basket.get('low_balance_hours'),
    }
    return jsonify(payload)


# --------------------------------------------------------- the bookings ----

def _recompute(basket, values):
    """Fill in what the schedule says a booking costs.

    computed_hours is always what the working week produces. hours follows it
    unless the user typed a figure, which is the whole point of
    hours_overridden - a comparison could not tell an override of 8.00 with
    8.00 from no override at all.
    """
    computed = loaf_forecast.entry_hours(basket, values)
    values['computed_hours'] = computed
    if not int(values.get('hours_overridden') or 0):
        values['hours'] = computed
    return values


@loaf.route('/api/entries', methods=['POST'])
@login_required
def api_create_entry():
    values, error = loaf_data.clean_entry(_flat(_payload()))
    if error:
        return jsonify({'status': 'error', 'message': error}), 400

    basket = loaf_data.get_basket(current_user.id, values['basket_id'])
    if basket is None:
        return jsonify({'status': 'error', 'message': 'No such basket.'}), 404

    values = _recompute(basket, values)
    if not values.get('hours_overridden') and not values['hours']:
        return jsonify({'status': 'error',
                        'message': 'That range does not cover any of your working '
                                   'hours, so it costs nothing. Change the dates, or '
                                   'enter the hours yourself.'}), 400

    entry_id = loaf_data.create_entry(current_user.id, values)
    if entry_id is None:
        return jsonify({'status': 'error', 'message': 'Could not save that.'}), 500
    return jsonify({'status': 'success', 'entry_id': entry_id,
                    'hours': values['hours']})


@loaf.route('/api/entries/<int:entry_id>', methods=['POST'])
@login_required
def api_update_entry(entry_id):
    existing = [e for e in loaf_data.get_entries(current_user.id)
                if int(e.get('id') or 0) == entry_id]
    if not existing:
        return jsonify({'status': 'error', 'message': 'No such booking.'}), 404

    values, error = loaf_data.clean_entry(_flat(_payload()))
    if error:
        return jsonify({'status': 'error', 'message': error}), 400

    basket = loaf_data.get_basket(current_user.id, values['basket_id'])
    if basket is None:
        return jsonify({'status': 'error', 'message': 'No such basket.'}), 404

    values = _recompute(basket, values)
    if not loaf_data.update_entry(current_user.id, entry_id, values):
        return jsonify({'status': 'error', 'message': 'Could not save that.'}), 500
    return jsonify({'status': 'success', 'hours': values['hours']})


@loaf.route('/api/entries/<int:entry_id>/delete', methods=['POST'])
@login_required
def api_delete_entry(entry_id):
    if not any(int(e.get('id') or 0) == entry_id
               for e in loaf_data.get_entries(current_user.id)):
        return jsonify({'status': 'error', 'message': 'No such booking.'}), 404
    if not loaf_data.delete_entry(current_user.id, entry_id):
        return jsonify({'status': 'error', 'message': 'Could not delete that.'}), 500
    return jsonify({'status': 'success'})


# --------------------------------------------------------- the summary ----

def _weekly_series(basket, result, first, last):
    """Balance once a week across a span - what the little charts plot.

    Weekly rather than daily because the balance only moves on accrual dates
    and days off, so a daily series would repeat the same figure six times out
    of seven and make the chart no more truthful. A year is 53 points, which
    fits without the scrolling machinery Blankee's charts need for 3,650.
    """
    points = []
    when = first
    while when <= last:
        points.append({'date': when.isoformat(),
                       'balance': loaf_forecast.balance_on(result, when)})
        when += timedelta(days=7)
    if points and points[-1]['date'] != last.isoformat():
        points.append({'date': last.isoformat(),
                       'balance': loaf_forecast.balance_on(result, last)})
    return points


def _basket_summary(basket, entries, baskets, today, extra=None):
    """One basket's figures, and optionally the same again with a proposed
    booking folded in.

    extra is an unsaved entry. It is added to BOTH usage and absence, because a
    booking costs its own basket the hours and costs every basket the accrual
    those hours would have earned - which is the whole reason Test Time Off
    cannot be answered by subtracting a number.
    """
    usage = loaf_forecast.usage_by_date(basket, entries)
    absence = loaf_forecast.absence_by_date(baskets, entries)

    if extra is not None:
        split = loaf_forecast._entry_split(basket, extra)
        for when, hours in split.items():
            usage[when] = round(usage.get(when, 0.0) + hours, 2)
            absence[when] = round(absence.get(when, 0.0) + hours, 2)

    year_start, year_end = loaf_forecast.accrual_year_bounds(basket, today)
    result = loaf_forecast.project(basket, usage, absence, year_end, today=today)

    summary = dict(result['summary'])
    summary.pop('checkpoints', None)
    for key in ('year_end_date', 'lowest_date', 'first_negative'):
        if summary.get(key) is not None:
            summary[key] = summary[key].isoformat()

    summary.update({
        'id': basket['id'],
        'name': basket['name'],
        'basket_type': basket['basket_type'],
        'max_balance_hours': basket.get('max_balance_hours'),
        'year_start': year_start.isoformat(),
        'year_end': year_end.isoformat(),
        'series': _weekly_series(basket, result, year_start, year_end),
    })
    return summary


@loaf.route('/summary')
@login_required
def summary():
    """Every basket at once: what is left, what has gone, and where it is going."""
    return render_template(
        'loaf/summary.html',
        baskets=loaf_data.get_baskets(current_user.id, include_hidden=False),
        today=_today(),
        **_shell())


@loaf.route('/api/summary')
@login_required
def api_summary():
    today = _today()
    baskets = loaf_data.get_baskets(current_user.id, include_hidden=False)
    entries = loaf_data.get_entries(current_user.id)
    return jsonify({
        'status': 'success',
        'today': today.isoformat(),
        'baskets': [_basket_summary(b, entries, baskets, today) for b in baskets],
    })


@loaf.route('/api/test-time-off', methods=['POST'])
@login_required
def api_test_time_off():
    """What a proposed booking would do, without saving anything.

    Nothing is written. The proposed entry is folded into a copy of the figures
    and the walk is run twice - as things stand, and with it - so the answer
    accounts for the accrual those hours would have earned as well as for the
    hours themselves. Subtracting the cost from the balance would miss half of
    it, and would miss it in the reassuring direction.
    """
    values, error = loaf_data.clean_entry(_flat(_payload()))
    if error:
        return jsonify({'status': 'error', 'message': error}), 400

    basket = loaf_data.get_basket(current_user.id, values['basket_id'])
    if basket is None:
        return jsonify({'status': 'error', 'message': 'No such basket.'}), 404

    today = _today()
    baskets = loaf_data.get_baskets(current_user.id, include_hidden=False)
    entries = loaf_data.get_entries(current_user.id)

    values = _recompute(basket, values)
    cost = values['hours']
    if not cost:
        return jsonify({'status': 'error',
                        'message': 'That range does not cover any of your working '
                                   'hours, so it would cost nothing.'}), 400

    now = _basket_summary(basket, entries, baskets, today)
    then = _basket_summary(basket, entries, baskets, today, extra=values)

    return jsonify({
        'status': 'success',
        'basket': {'id': basket['id'], 'name': basket['name'],
                   'low_balance_hours': basket.get('low_balance_hours')},
        'cost': cost,
        'now': now,
        'then': then,
        # The two numbers worth saying out loud, and they are not the same
        # question: whether it ever goes negative, and how much slack is left
        # at the worst moment.
        'goes_negative': then.get('first_negative'),
        'lowest': then.get('lowest'),
        'lowest_date': then.get('lowest_date'),
        'accrual_lost': round((now.get('accrued_total') or 0)
                              - (then.get('accrued_total') or 0), 2),
    })
