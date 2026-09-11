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
import loaf_holidays
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
    """A payload with single-valued fields unwrapped and lists left alone.

    A field missing from `multi` is silently truncated to its first element,
    which is not an error anywhere - it just quietly loses days. Anything that
    can legitimately arrive as a list belongs in the tuple below.
    """
    #
    # 'holidays' was missing from here from the day it shipped, and the
    # basket modal sends it as an array of ticked checkboxes - so every
    # holiday after the first was thrown away on save, silently, exactly
    # as the paragraph above warns. A basket set to New Year, Juneteenth
    # and Independence Day kept New Year and quietly accrued through the
    # other two.
    multi = ('weekdays', 'monthly_days', 'accrual_only_weekdays',
             'holidays')
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


def _focus_basket(shelf_id, basket_id):
    """The pool the calendar draws its running balance from.

    The month view shows a whole job now - every pool on it, on one grid - so
    what the caller names is the job. One pool is still focused, because a
    balance is a balance OF something and adding a paid pot to an unpaid one
    gives a number nobody wants.

    A basket_id wins when it is given and belongs to the named job. Otherwise
    the first pool on that job is focused, which is the one the switcher shows
    first. Falling all the way back to _pick_basket covers a caller that names
    neither, and the URL that used to name only a basket.
    """
    if basket_id:
        wanted = loaf_data.get_basket(current_user.id, basket_id) \
            if str(basket_id).isdigit() else None
        if wanted is not None and (
                not shelf_id
                or str(wanted.get('shelf_id') or '') == str(shelf_id)):
            return wanted

    if shelf_id and str(shelf_id).isdigit():
        if loaf_data.get_shelf(current_user.id, shelf_id) is None:
            return None
        on_it = loaf_data.baskets_on(current_user.id, shelf_id,
                                     include_hidden=False)
        return on_it[0] if on_it else None

    return _pick_basket(basket_id)


@loaf.route('/dashboard')
@login_required
def dashboard():
    """One job, one month: what was worked, what was taken, what is left.

    Every pool of hours on the job is drawn on the one grid, colour-coded, so
    a week off shows whether it came out of the paid pot or the unpaid one
    without switching between two calendars to find out. One pool is focused
    at a time for the running balance, because a balance across a paid pot and
    an unpaid one is not a number anybody wants.

    The grid itself is fetched and drawn by /api/month, the way dashboard_m
    builds its calendar - so changing month or swiping is one request rather
    than a page load, and the cell markup exists in one place instead of once
    in Jinja and once in JS.
    """
    today = _today()
    basket = _focus_basket(request.args.get('shelf'), request.args.get('basket'))
    shelf = loaf_data.shelf_of(current_user.id, basket) if basket else None
    return render_template(
        'loaf/dashboard.html',
        baskets=loaf_data.get_baskets(current_user.id, include_hidden=False),
        basket=basket,
        shelf=shelf,
        shelves=loaf_data.get_shelves(current_user.id),
        pools=loaf_data.baskets_on(current_user.id, shelf['id'],
                                   include_hidden=False) if shelf else [],
        today=today,
        # _basket_modal.html carries these on the element for its script to
        # read, so any page including that form has to pass them.
        weekday_prefixes=loaf_data.WEEKDAY_PREFIXES,
        weekday_names=loaf_data.WEEKDAY_NAMES,
        holidays=loaf_holidays.HOLIDAYS,
        **_shell())


@loaf.route('/baskets')
@login_required
def baskets():
    """Manage the pools of hours: what they hold, how they fill, the work week.

    The whole basket is handed to the template so the edit modal can be filled
    from data-* attributes rather than by scraping the table - the one thing
    most worth copying from recurring_i.html.
    """
    # Keyed by id so the template can find each basket's job without a
    # lookup per row. The template falls back to the basket itself for a
    # shelf-less one - the same transitional allowance loaf_data.shelf_of
    # makes, and for the same reason.
    shelves = {s['id']: s for s in loaf_data.get_shelves(current_user.id)}
    return render_template(
        'loaf/baskets.html',
        baskets=loaf_data.get_baskets(current_user.id),
        shelves=shelves,
        today=_today(),
        weekday_prefixes=loaf_data.WEEKDAY_PREFIXES,
        weekday_names=loaf_data.WEEKDAY_NAMES,
        holidays=loaf_holidays.HOLIDAYS,
        # Passed in rather than registered as template filters: they are only
        # wanted on Loaf's pages, and app_template_filter would put them in
        # every Blankee template's namespace too.
        describe_fill=loaf_data.describe_fill,
        describe_carryover=loaf_data.describe_carryover,
        describe_week=loaf_data.describe_week,
        **_shell())


# --------------------------------------------------------------- the API ----

def _backfill_year(user_id, basket_id, stated_hours, today):
    """Lay down the accruals a mid-year basket already missed.

    Somebody adopting Loaf in September has been accruing since January and
    has taken leave Loaf never saw. Left alone, the whole year before today is
    blank: no accruals on the calendar, and no way to enter last spring's
    holiday without the balance going wrong.

    So the basket is re-dated to the start of its accrual year, which makes
    every pay date since then land on its own, and one entry reconciles the
    total down to the figure the person actually gave. That entry is a 'prior'
    - it comes off the balance and is invisible to the pro-rate, because it
    carries a year of leave on a single date and the real dates are precisely
    what is not known.

    Returns a note for the caller to pass on, or None when nothing was built.
    Never raises: a basket that cannot be backfilled is still a good basket,
    and is left exactly as it would have been before any of this existed.
    """
    basket = loaf_data.get_basket(user_id, basket_id)
    if basket is None:
        return None

    # Either way of filling counts. A basket that is GRANTED rather than
    # accrued used to fail this guard and keep today as its starting date -
    # so its whole year was outside the projection, and a day booked in it
    # changed nothing while the calendar still drew the booking. An annual
    # grant lands at the year turn, which is exactly what replaying from the
    # year start reproduces.
    accrues = basket.get('accrual_hours') is not None
    granted = basket.get('grant_hours') is not None
    if not accrues and not granted:
        return None

    shelf = loaf_data.shelf_of(user_id, basket)
    if shelf is None:
        return None
    # Only an accrual needs to know when pay lands. A grant does not.
    if accrues and not shelf.get('accrual_anchor_date'):
        return None

    year_start, _ = loaf_forecast.accrual_year_bounds(shelf, today)
    if year_start >= today:
        return None                 # the year began today; nothing to catch up

    # Re-date to the year start and replay from nothing, so the engine - not
    # arithmetic repeated here - decides what the accruals, the grant, the
    # carryover and the ceiling come to. Anything computed by hand would be a
    # second implementation of the walk, drifting the day either changes.
    #
    # The year start itself, not the day before it. Backing off a day did make
    # the year-turn grant land, but it also made a pay date falling ON the
    # year start eligible - and that one pays for the period that ENDED there,
    # which ran entirely in the year before this basket existed. project()
    # recognises a turn on its first day now, so the day before is not needed
    # and would only buy back that phantom accrual.
    loaf_data.update_basket(user_id, basket_id, {
        'starting_hours': 0, 'starting_date': year_start.isoformat()})
    replayed = loaf_data.get_basket(user_id, basket_id)
    # opening_turn: this walk starts the basket at zero on the first day of
    # its leave year, so the turn that opens that year - the carryover, and
    # any annual grant - belongs to it. Nobody else gets that, because a
    # stated starting balance already counts whatever landed that morning.
    result = loaf_forecast.project(shelf, replayed, {}, {}, today, today=today,
                                   opening_turn=True)
    earned = loaf_forecast.balance_on(result, today)

    # What the basket was worth the moment its year opened - the grant, for
    # anything granted. That becomes the stored starting balance, so every
    # later projection reproduces this same series without needing to be told
    # about the opening turn: the grant is inside the opening figure rather
    # than waiting at a boundary the walk starts on and therefore never
    # crosses. Read off the replay, not worked out here, so it stays the
    # engine's answer.
    opening = loaf_forecast.balance_on(result, year_start)

    if not result.get('events'):
        # No pay date has come round yet inside this year. Put the basket back.
        loaf_data.update_basket(user_id, basket_id, {
            'starting_hours': stated_hours,
            'starting_date': today.isoformat()})
        return None

    spent = round(earned - float(stated_hours or 0), 2)
    if spent < -0.005:
        # More hours than the accruals can account for. Refuse rather than
        # invent the difference: it means the rate, the pay dates or the year
        # start is not what the person thinks, and a basket that quietly
        # conjures hours would hide that for months.
        loaf_data.update_basket(user_id, basket_id, {
            'starting_hours': stated_hours,
            'starting_date': today.isoformat()})
        return ('Loaf did not fill in this year, because the accruals since %s '
                'come to %.2f hours and you have more than that. Check the '
                'accrual figure, the pay dates and when your leave year '
                'starts.' % (year_start.strftime('%-d %B'), earned))

    if spent < 0.005:
        # Nothing was spent; the accruals alone fit. The opening balance still
        # has to be stored, or a granted basket would sit at zero all year.
        loaf_data.update_basket(user_id, basket_id, {'starting_hours': opening})
        return None

    loaf_data.update_basket(user_id, basket_id, {'starting_hours': opening})

    loaf_data.create_entry(user_id, {
        'basket_id': basket_id,
        'starts_at': '%s 00:00:00' % today.isoformat(),
        'ends_at': '%s 23:59:00' % today.isoformat(),
        'all_day': 1, 'hours': spent, 'computed_hours': None,
        'hours_overridden': 1, 'status': 'planned', 'direction': 'prior',
        'note': 'Time off already taken this year'})

    return ('Loaf filled in %.2f hours of accruals since %s and recorded %.2f '
            'hours as already taken, which leaves the %.2f you entered. Split '
            'that into your actual days off whenever you like - but delete it '
            'as you go, or the two will both come off.'
            % (earned, year_start.strftime('%-d %B'), spent,
               float(stated_hours or 0)))


# ------------------------------------------------------------- the shelves ----

# The fields that describe the job rather than the pool of hours. A payload
# carrying any of them is one that thinks it is setting the working week, the
# pay cadence or the holidays.
SHELF_FIELDS = ('cadence_unit', 'cadence_interval', 'weekdays', 'monthly_days',
                'yearly_day', 'yearly_month', 'accrual_anchor_date',
                'year_start_month', 'year_start_day', 'period_hours',
                'holidays', 'custom_holidays', 'accrual_only_weekdays',
                'uncharged_weekdays') + tuple(
    '%s_%s' % (day, part)
    for day in loaf_data.WEEKDAY_PREFIXES
    for part in ('start', 'end', 'break_minutes'))


def _shelf_error(payload):
    """Whatever is wrong with the job half of a basket payload, or None.

    Checked even when it will not be applied. A basket joining a shelf that
    already exists does not get to redefine when somebody works, but a form
    that sends a thirteenth month or a day ending before it starts is wrong
    whether or not anybody was going to act on it - and answering 200 to it
    tells the caller their setting took when it went nowhere.

    Silence here is the failure mode that matters: the old contract rejected
    these, and quietly dropping them the day the week moved to the shelf would
    be a regression nobody sees until a basket is found with no week at all.
    """
    if not any(field in payload for field in SHELF_FIELDS):
        return None
    _, error = loaf_data.clean_shelf(dict(payload, name=payload.get('name') or 'x'))
    return error


@loaf.route('/api/shelves', methods=['POST'])
@login_required
def api_create_shelf():
    values, error = loaf_data.clean_shelf(_flat(_payload()))
    if error:
        return jsonify({'status': 'error', 'message': error}), 400

    shelf_id = loaf_data.create_shelf(current_user.id, values)
    if shelf_id is None:
        return jsonify({'status': 'error',
                        'message': 'Could not save that job.'}), 500
    return jsonify({'status': 'success', 'shelf_id': shelf_id})


@loaf.route('/api/shelves/<int:shelf_id>', methods=['POST'])
@login_required
def api_update_shelf(shelf_id):
    if loaf_data.get_shelf(current_user.id, shelf_id) is None:
        return jsonify({'status': 'error', 'message': 'No such job.'}), 404

    payload = _flat(_payload())
    values, error = loaf_data.clean_shelf(payload)
    if error:
        return jsonify({'status': 'error', 'message': error}), 400

    # The same rule api_update_basket documents at length, for the fields that
    # moved here with the working week: clean_shelf reads "never sent" as "not
    # set", and a form that does not show a control sends nothing. Without
    # this, saving a name change would wipe a figure the projection is still
    # using.
    #
    #   accrual_only_weekdays   only offered once the counted-week column is
    #                       switched on from the console, so an ordinary save
    #                       never mentions it. Wiping it would put the
    #                       Thursdays back and start spending them again.
    #   period_hours        lives in Advanced and only appears once the
    #                       accrual is per hour worked. Wiping it silently
    #                       swaps the employer's flat denominator back for a
    #                       walked one, which drifts a few hours a year.
    #
    # A payload that DOES send one, empty or not, is still obeyed - so both
    # stay clearable on purpose.
    # uncharged_weekdays joins them: the Free column is on screen for
    # everybody, so the current form always sends the key - but a browser still
    # holding the previous release's script does not, and during an update
    # window that is exactly a payload that would silently clear it.
    for absent_means_keep in ('accrual_only_weekdays', 'uncharged_weekdays',
                              'period_hours'):
        if absent_means_keep not in payload:
            values.pop(absent_means_keep, None)

    # source_basket_id is provenance written once by the migration. Nothing on
    # a form may move it, and clean_shelf never produces it, but popping it is
    # cheap insurance against a future caller that does.
    values.pop('source_basket_id', None)

    if not loaf_data.update_shelf(current_user.id, shelf_id, values):
        return jsonify({'status': 'error',
                        'message': 'Could not save that job.'}), 500
    return jsonify({'status': 'success'})


@loaf.route('/api/shelves/<int:shelf_id>/delete', methods=['POST'])
@login_required
def api_delete_shelf(shelf_id):
    """A job, and everything on it.

    The count goes back so the caller can say what it is about to destroy.
    Deleting a job takes every pool of hours on it and every booking against
    those, which is a good deal more than the word "delete" implies on its
    own, and the confirmation ought to say so.
    """
    if loaf_data.get_shelf(current_user.id, shelf_id) is None:
        return jsonify({'status': 'error', 'message': 'No such job.'}), 404

    losing = len(loaf_data.baskets_on(current_user.id, shelf_id))
    if not loaf_data.delete_shelf(current_user.id, shelf_id):
        return jsonify({'status': 'error',
                        'message': 'Could not delete that job.'}), 500
    return jsonify({'status': 'success', 'baskets_deleted': losing})


@loaf.route('/api/baskets', methods=['POST'])
@login_required
def api_create_basket():
    payload = _flat(_payload())
    values, error = loaf_data.clean_basket(payload)
    if error:
        return jsonify({'status': 'error', 'message': error}), 400

    error = _shelf_error(payload)
    if error:
        return jsonify({'status': 'error', 'message': error}), 400


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
    #
    # Whether we stamped it also decides whether the year behind it gets
    # filled in below: a caller that named a date has already said where this
    # basket begins, and moving it would overrule them.
    stamped_today = not values.get('starting_date')
    if stamped_today:
        values['starting_date'] = _today().isoformat()

    # Every pool of hours belongs to a job. A caller that named one is taken
    # at their word; otherwise it joins the only one there is, and if there is
    # not one yet it gets one built FROM THIS FORM.
    #
    # That last part is what keeps "add a basket" a single step. The form has
    # always carried the working week, the holidays and the pay cadence
    # alongside the pool's own settings, and for somebody's first basket those
    # answers are the job. Ignoring them and creating a blank shelf would mean
    # a first basket that costs nothing to book, which is the same as broken.
    #
    # A basket joining a shelf that already exists does NOT get to rewrite it.
    # Adding a second pool of hours is not the moment to redefine when you
    # work, and silently reconfiguring the job from a form the person thought
    # was about holiday entitlement is exactly the sort of thing that is only
    # noticed months later.
    if not values.get('shelf_id'):
        shelves = loaf_data.get_shelves(current_user.id)
        if len(shelves) == 1:
            values['shelf_id'] = shelves[0]['id']
        elif not shelves:
            made, shelf_error = loaf_data.clean_shelf(payload)
            if shelf_error:
                return jsonify({'status': 'error', 'message': shelf_error}), 400
            values['shelf_id'] = loaf_data.create_shelf(current_user.id, made)
        else:
            return jsonify({'status': 'error',
                            'message': 'Say which job this basket is for.'}), 400
    elif loaf_data.get_shelf(current_user.id, values['shelf_id']) is None:
        return jsonify({'status': 'error', 'message': 'No such shelf.'}), 404

    # Named after the shelf it just landed on, when nobody named it. The clash
    # check follows rather than precedes, because two unnamed PTO baskets on
    # one shelf would both want to be called "Acme Corp PTO" and the second
    # has to be told so.
    if not values.get('name'):
        values['name'] = loaf_data.default_basket_name(
            loaf_data.get_shelf(current_user.id, values['shelf_id']),
            values.get('basket_type'))

    if loaf_data.basket_name_exists(current_user.id, values['name']):
        return jsonify({'status': 'error',
                        'message': 'You already have a basket called "%s".'
                                   % values['name']}), 400

    basket_id = loaf_data.create_basket(current_user.id, values)
    if basket_id is None:
        return jsonify({'status': 'error',
                        'message': 'Could not save that basket.'}), 500

    # A basket started part-way through its leave year has a year behind it
    # that Loaf knows nothing about. Fill it in - see _backfill_year. Only on
    # create: afterwards these are ordinary rows, and rebuilding them would
    # overwrite whatever the person has since done with them.
    note = None
    if stamped_today:
        note = _backfill_year(current_user.id, basket_id,
                              values.get('starting_hours'), _today())

    return jsonify({'status': 'success', 'basket_id': basket_id,
                    'note': note})


@loaf.route('/api/baskets/<int:basket_id>', methods=['POST'])
@login_required
def api_update_basket(basket_id):
    if loaf_data.get_basket(current_user.id, basket_id) is None:
        return jsonify({'status': 'error', 'message': 'No such basket.'}), 404

    payload = _flat(_payload())
    values, error = loaf_data.clean_basket(payload)
    if error:
        return jsonify({'status': 'error', 'message': error}), 400

    error = _shelf_error(payload)
    if error:
        return jsonify({'status': 'error', 'message': error}), 400

    # Clearing the name puts the default back rather than saving an empty
    # one, which is the same promise the create path makes.
    if not values.get('name'):
        was_on = loaf_data.get_basket(current_user.id, basket_id) or {}
        values['name'] = loaf_data.default_basket_name(
            loaf_data.shelf_of(current_user.id, was_on),
            values.get('basket_type'))

    if loaf_data.basket_name_exists(current_user.id, values['name'],
                                    exclude_id=basket_id):
        return jsonify({'status': 'error',
                        'message': 'You already have a basket called "%s".'
                                   % values['name']}), 400

    # display_order is not in the form. Left out of the update so a save does
    # not quietly undo a drag-reorder.
    values.pop('display_order', None)

    # Two columns the form does not always ask about, and for both of them
    # clean_basket reads "never sent" as "not set" - so without this, saving a
    # name change would wipe a figure the projection is still using.
    #
    #   max_balance_hours   came off the form because the accrual already says
    #                       how fast a basket fills. The walk still clamps to
    #                       it, so wiping one quietly raises every future
    #                       balance from that day on.
    #
    # accrual_only_weekdays used to be here too and has moved to the shelf,
    # where api_update_shelf guards it the same way - it is the job's week
    # that knows which days are counted but not attended, not the pool's.
    #
    # A payload that does not mention a field leaves it alone, the same
    # treatment starting_date gets below. One that DOES send it, empty or not,
    # is still obeyed - so both stay reachable and clearable on purpose.
    for absent_means_keep in ('max_balance_hours',):
        if absent_means_keep not in payload:
            values.pop(absent_means_keep, None)

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

    # A basket that is the ONLY pool on its job is still, to the person
    # looking at it, just "my basket" - so a form carrying the working week
    # edits the week, exactly as it did before shelves existed. The moment a
    # second pool joins that job the week stops being this basket's to change
    # and the job gets edited on its own.
    #
    # Detected rather than declared, because the alternative is a hidden field
    # saying "and by the way also write the shelf", which is the kind of flag
    # that survives long after the reason for it is gone.
    shelf = loaf_data.shelf_of(current_user.id, was)
    alone = shelf and len(loaf_data.baskets_on(current_user.id,
                                               shelf['id'])) == 1
    if alone and any(k in payload for k in ('mon_start', 'cadence_unit',
                                            'holidays', 'period_hours')):
        shelf_values, shelf_error = loaf_data.clean_shelf(payload)
        if shelf_error:
            return jsonify({'status': 'error', 'message': shelf_error}), 400
        for absent_means_keep in ('accrual_only_weekdays',
                                  'uncharged_weekdays', 'period_hours'):
            if absent_means_keep not in payload:
                shelf_values.pop(absent_means_keep, None)
        shelf_values.pop('source_basket_id', None)
        loaf_data.update_shelf(current_user.id, shelf['id'], shelf_values)

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

    # usage is this pool's; absence is every pool ON THIS SHELF, because an
    # hour not worked pro-rates the accrual whichever pot it came out of - and
    # scoped to the shelf, because an hour not worked at another job does not.
    shelf = loaf_data.shelf_of(user_id, basket)
    baskets = loaf_data.baskets_on(user_id, shelf['id'], include_hidden=False) \
        if shelf else [basket]
    if not any(int(b['id']) == int(basket['id']) for b in baskets):
        baskets = [basket] + baskets    # a hidden pool can still be the focus

    absence = loaf_forecast.absence_by_date(shelf, baskets, entries)

    # Every pool's split, not just the focused one. The calendar colours a day
    # by which pot the hours came out of, so it needs all of them - and the
    # focused pool's is in here too rather than being worked out twice.
    taken_by = {int(b['id']): loaf_forecast.usage_by_date(shelf, b, entries)
                for b in baskets}
    usage = taken_by[int(basket['id'])]
    credits = loaf_forecast.credit_by_date(basket, entries)

    viewing = date(int(year), int(month), 1)
    through = loaf_forecast.horizon_for(shelf, viewing, today=today)
    result = loaf_forecast.project(shelf, basket, usage, absence, through,
                                   today=today, credits=credits)

    grid_start, grid_end, first, last = loaf_forecast.grid_bounds(year, month)
    rows = loaf_forecast.month_rows(shelf, basket, result, absence,
                                    grid_start, grid_end, taken_by=taken_by)

    # Which bookings touch each day, so a click can open the one that is
    # already there instead of always adding another.
    #
    # Every pool on the job, not just the focused one: the calendar shows them
    # all, so clicking a day somebody booked against UTO has to open THAT
    # booking rather than silently starting a new PTO one on top of it.
    on_shelf = {int(b['id']) for b in baskets}
    touching = {}
    for entry in entries:
        if int(entry.get('basket_id') or 0) not in on_shelf:
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
                'basket_id': int(entry.get('basket_id') or 0),
                'starts_at': entry.get('starts_at'),
                'ends_at': entry.get('ends_at'),
                'all_day': int(entry.get('all_day') or 0),
                'hours': entry.get('hours'),
                'hours_overridden': int(entry.get('hours_overridden') or 0),
                'status': entry.get('status'),
                'direction': entry.get('direction') or 'use',
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
            'closed': row['closed'],
            'unattended': row['unattended'],
            # What the working week says the day is worth. The booking modal
            # seeds its hours box with it, so a whole day off needs no sum.
            'scheduled': row['scheduled'],
            'worked': row['worked'],
            'taken': row['taken'],
            # Keyed by basket id as a string, because JSON object keys always
            # are and a client comparing 3 to "3" would find nothing.
            'taken_by': {str(k): v for k, v in row['taken_by'].items()},
            'accrued': row['accrued'],
            'granted': row['granted'],
            'credited': row['credited'],
            'balance': row['balance'],
            'entries': touching.get(stamp, []),
        })

    summary = dict(result['summary'])
    summary.pop('checkpoints', None)
    for key in ('year_end_date', 'lowest_date', 'first_negative'):
        if summary.get(key) is not None:
            summary[key] = summary[key].isoformat()

    # The pools on this job, in the order the switcher shows them, each with
    # what it is worth today. This is what the chips above the calendar read.
    pools = []
    for other in baskets:
        if int(other['id']) == int(basket['id']):
            balance = result['summary'].get('balance_today')
        else:
            walked = loaf_forecast.project(
                shelf, other,
                taken_by.get(int(other['id']), {}), absence, through,
                today=today,
                credits=loaf_forecast.credit_by_date(other, entries))
            balance = walked['summary'].get('balance_today')
        pools.append({
            'id': int(other['id']),
            'name': other.get('name'),
            'basket_type': other.get('basket_type') or 'pto',
            'low_balance_hours': other.get('low_balance_hours'),
            'balance_today': balance,
            'focused': int(other['id']) == int(basket['id']),
        })

    return {'rows': out, 'summary': summary, 'pools': pools,
            'shelf': {'id': shelf['id'], 'name': shelf.get('name')}
                     if shelf else None,
            'first': first.isoformat(), 'last': last.isoformat()}


@loaf.route('/api/month')
@login_required
def api_month():
    today = _today()
    basket = _focus_basket(request.args.get('shelf_id'),
                           request.args.get('basket_id'))
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

def _recompute(shelf, values):
    """Fill in what the job's schedule says a booking costs.

    computed_hours is always what the working week produces. hours follows it
    unless the user typed a figure, which is the whole point of
    hours_overridden - a comparison could not tell an override of 8.00 with
    8.00 from no override at all.

    A credit skips all of it. There is no range to cost: clean_entry has
    already fixed the date and taken the figure from the person, and the
    working week has no opinion about hours it did not produce.
    """
    if str(values.get('direction') or 'use') in loaf_data.FLAT_DIRECTIONS:
        values['computed_hours'] = None
        return values

    computed = loaf_forecast.entry_hours(shelf, values)
    values['computed_hours'] = computed
    if not int(values.get('hours_overridden') or 0):
        values['hours'] = computed
    return values


def _unworkable(shelf, values):
    """The message for time off booked where no hours are worked, or None.

    Checked ahead of the override, unlike the plain zero-cost guard below it,
    and that is the difference: typing a figure used to force a booking onto
    any day at all, including one the schedule says is empty. It cannot now.
    A day you are not at work is not a day you can take off.

    Only the range as a whole has to miss - a Monday-to-Friday booking is
    fine, and the days inside it that are not worked simply cost nothing.
    """
    if str(values.get('direction') or 'use') in loaf_data.FLAT_DIRECTIONS:
        return None                 # neither is booked against the week

    if loaf_forecast.entry_hours(shelf, values) > 0:
        return None

    return ('You do not work any of those hours, so there is no time off to '
            'take. Pick a day your working week covers.')


def _overbooked(user_id, shelf, values, editing=None):
    """The message for a day booked past what it holds, or None.

    A day can carry bookings from as many pools as you like - a morning of
    paid leave and the afternoon unpaid is an ordinary thing - so nothing
    stops a second one going on. What stops is the total: you cannot take
    more hours off a day than were ever going to be worked.

    Counted across every pool on the SHELF, because they share one working
    week. Another job's day off says nothing about the hours available here.
    """
    if str(values.get('direction') or 'use') in loaf_data.FLAT_DIRECTIONS:
        return None                 # hours handed over, not hours taken

    if shelf is None:
        return None

    on_shelf = loaf_data.baskets_on(user_id, shelf['id'])
    clash = loaf_forecast.booked_beyond(
        shelf, on_shelf, loaf_data.get_entries(user_id), values,
        ignoring=editing)
    if clash is None:
        return None

    day, already, capacity = clash
    when = day.strftime('%A %d %B')
    if capacity <= 0:
        return ('You do not work on %s, so there is no time off to take '
                'there.' % when)
    return ('%s only has %.2f hours to give and %.2f of them are already '
            'booked. Take less, or free some up first.'
            % (when, capacity, already))


@loaf.route('/api/entries', methods=['POST'])
@login_required
def api_create_entry():
    values, error = loaf_data.clean_entry(_flat(_payload()))
    if error:
        return jsonify({'status': 'error', 'message': error}), 400

    basket = loaf_data.get_basket(current_user.id, values['basket_id'])
    if basket is None:
        return jsonify({'status': 'error', 'message': 'No such basket.'}), 404

    shelf = loaf_data.shelf_of(current_user.id, basket)
    blocked = _unworkable(shelf, values) \
        or _overbooked(current_user.id, shelf, values)
    if blocked:
        return jsonify({'status': 'error', 'message': blocked}), 400

    values = _recompute(shelf, values)

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

    # The update path had no zero-cost guard at all, so re-saving a booking
    # onto a day with no hours quietly wrote 0 and kept the row. It gets the
    # same refusal as the create path.
    shelf = loaf_data.shelf_of(current_user.id, basket)
    # Not against itself: re-saving an eight-hour day on an eight-hour
    # schedule is not an overbooking.
    blocked = _unworkable(shelf, values) \
        or _overbooked(current_user.id, shelf, values, editing=entry_id)
    if blocked:
        return jsonify({'status': 'error', 'message': blocked}), 400

    values = _recompute(shelf, values)
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


def _basket_summary(user_id, basket, entries, baskets, today, extra=None):
    """One basket's figures, and optionally the same again with a proposed
    booking folded in.

    extra is an unsaved entry. It is added to BOTH usage and absence, because a
    booking costs its own basket the hours and costs every pool ON ITS SHELF
    the accrual those hours would have earned - which is the whole reason Test
    Time Off cannot be answered by subtracting a number.

    Takes user_id because the shelf has to be looked up, and `baskets` cannot
    stand in for that: the caller passes every basket the person has, and the
    pro-rate needs only the ones on this one's job.
    """
    shelf = loaf_data.shelf_of(user_id, basket)
    on_shelf = loaf_data.baskets_on(user_id, shelf['id']) if shelf else [basket]
    usage = loaf_forecast.usage_by_date(shelf, basket, entries)
    absence = loaf_forecast.absence_by_date(shelf, on_shelf, entries)
    credits = loaf_forecast.credit_by_date(basket, entries)

    # Test Time Off only ever tries taking hours, never being given them, so
    # `extra` is a use and goes into both usage and absence as before.
    if extra is not None:
        split = loaf_forecast._entry_split(shelf, extra)
        for when, hours in split.items():
            usage[when] = round(usage.get(when, 0.0) + hours, 2)
            absence[when] = round(absence.get(when, 0.0) + hours, 2)

    year_start, year_end = loaf_forecast.accrual_year_bounds(shelf, today)
    result = loaf_forecast.project(shelf, basket, usage, absence, year_end,
                                   today=today,
                                   credits=credits)

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
        'baskets': [_basket_summary(current_user.id, b, entries, baskets, today)
                    for b in baskets],
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
    shelf = loaf_data.shelf_of(current_user.id, basket)
    baskets = loaf_data.baskets_on(current_user.id, shelf['id'],
                                   include_hidden=False) if shelf else [basket]
    entries = loaf_data.get_entries(current_user.id)

    blocked = _unworkable(shelf, values) \
        or _overbooked(current_user.id, shelf, values)
    if blocked:
        return jsonify({'status': 'error', 'message': blocked}), 400

    values = _recompute(shelf, values)
    cost = values['hours']

    now = _basket_summary(current_user.id, basket, entries, baskets, today)
    then = _basket_summary(current_user.id, basket, entries, baskets, today,
                           extra=values)

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
