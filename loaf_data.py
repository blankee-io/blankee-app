"""Reading and writing Loaf's two tables.

WHY THIS IS MYSQL-FIRST, WHEN CLAUDE.md SECTION 4 SAYS REDIS-FIRST

    Redis-first means the flush worker carries a table to MySQL, and that costs
    a bespoke branch in redis_manager's ~4,000-line _flush_table_to_mysql plus
    the table's name in THREE separate tables_to_flush lists. Miss the third
    and the failure is silent in the worst way available: the write succeeds,
    the dirty flag is cleared regardless, the unknown-table fallback returns 0,
    and the row is gone.

    Blankee needs that machinery because a single balance change rewrites
    thousands of projected entries. Loaf writes a handful of rows a month - a
    basket is created once and edited rarely, and someone books time off a few
    times a year. So every write here goes to MySQL first and then refreshes
    the cache with mark_dirty=False, which is the pattern
    add_income_category_group already uses deliberately ("Insert into MySQL
    first to get a real auto-increment ID (avoids temp-ID race)").

    Both tables are still in redis_manager.USER_TABLES, so reads come out of
    the cache the middleware has already filled. Neither needs a
    _hydrate_table branch, because both carry a direct user_id and the default
    branch covers them.

    THE LANDMINE, STATED PLAINLY: because nothing marks these tables dirty,
    their absence from tables_to_flush is currently harmless. The first
    Redis-first write to either one - anything calling set_table_cache without
    mark_dirty=False - will silently lose data until they are added to all
    three lists and given a flush branch. There is a note beside them in
    USER_TABLES saying so.

WHY EVERY WRITE IS SCOPED BY user_id

    redis_crud.update_entry and delete_entry are `WHERE id = %s` with no
    ownership check. Given an id from a request that is a cross-user write, so
    this module does not use them: its own UPDATE and DELETE carry
    `AND user_id = %s` and report how many rows they actually touched. Only
    create goes through redis_crud, where the data is ours to begin with.

WHY READS ARE NORMALISED

    The two paths disagree about types. Out of Redis a row is JSON: dates are
    'YYYY-MM-DD' strings, decimals are floats, a TIME is '09:00:00'. Straight
    out of MySQL the same row holds date, Decimal and - for a TIME column -
    timedelta objects. Callers that guess wrong break only on a cold cache,
    which is the hardest kind of bug to see. So the MySQL path is normalised to
    look exactly like the Redis path, and nothing downstream has to ask which
    one served it.
"""

import re
from datetime import date, datetime, timedelta
from decimal import Decimal

import redis_manager
from redis_manager import get_table_cache, set_table_cache
from db_connections import get_db_pool
from log_config import get_logger, log_info, log_error, log_exception

logger = get_logger(__name__)


BASKETS = 'loaf_baskets'
ENTRIES = 'loaf_entries'

# 0 = monday, the same numbering auto_balance.WEEKDAY_NUMBERS uses, so the two
# never disagree about which day is which.
WEEKDAY_PREFIXES = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')

# What a caller may set. A request dict is filtered through these rather than
# trusted, so a stray key cannot reach the SQL and id/user_id/created_at cannot
# be written from outside.
BASKET_COLUMNS = (
    'name', 'basket_type', 'display_order', 'hidden',
    'max_balance_hours', 'grant_hours', 'accrual_hours',
    'cadence_interval', 'cadence_unit', 'weekdays', 'monthly_days',
    'yearly_day', 'yearly_month', 'accrual_anchor_date',
    'year_start_month', 'year_start_day',
    'carryover_mode', 'carryover_cap_hours', 'low_balance_hours',
    'starting_hours', 'starting_date', 'accrual_only_weekdays',
) + tuple(
    '%s_%s' % (day, part)
    for day in WEEKDAY_PREFIXES
    for part in ('start', 'end', 'break_minutes')
)

ENTRY_COLUMNS = (
    'basket_id', 'starts_at', 'ends_at', 'all_day',
    'hours', 'computed_hours', 'hours_overridden', 'status', 'direction',
    'note',
)


# ---------------------------------------------------------------- reading ----

def _normalise(row):
    """One row shaped the way it comes out of Redis, whatever produced it.

    See the module docstring: this is what stops a cold cache from being a
    different code path.
    """
    out = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            out[key] = float(value)
        elif isinstance(value, datetime):
            out[key] = value.isoformat()
        elif isinstance(value, date):
            out[key] = value.isoformat()
        elif isinstance(value, timedelta):
            # A TIME column. Same rendering as redis_manager.DecimalEncoder.
            total = int(value.total_seconds())
            sign = '-' if total < 0 else ''
            total = abs(total)
            out[key] = '%s%02d:%02d:%02d' % (sign, total // 3600,
                                             (total % 3600) // 60, total % 60)
        else:
            out[key] = value
    return out


def _from_mysql(table, user_id):
    """Every row of one table for one user, straight from MySQL."""
    if table not in (BASKETS, ENTRIES):
        log_error(logger, 'LOAF', 'Refusing to query unknown table %r' % table)
        return []
    try:
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            cursor.execute(
                'SELECT * FROM %s WHERE user_id = %%s' % table, (user_id,))
            return [_normalise(r) for r in cursor.fetchall()]
    except Exception as e:
        log_exception(logger, 'LOAF',
                      'Error reading %s for user %s: %s' % (table, user_id, e))
        return []


def _read(table, user_id):
    """Cache first, MySQL second.

    get_table_cache returns None for "not hydrated or no key", which is a real
    state and not an error - a cron job runs with nothing hydrated. redis_crud
    has no generic fallback for that (its only one is whitelisted to three
    category tables), which is why this module brings its own.
    """
    cached = get_table_cache(table, user_id)
    if cached is not None:
        return cached
    return _from_mysql(table, user_id)


def _refresh(table, user_id):
    """Point the cache back at MySQL after a write.

    A whole re-read rather than patching the cached array in place. These
    tables hold tens of rows, so the read is free, and it makes cache drift
    impossible instead of merely unlikely - a partial update that misses a
    column is exactly the bug that only shows up hours later.

    mark_dirty=False because MySQL is already current; marking it would queue a
    flush for a table that has no flush branch.
    """
    set_table_cache(table, user_id, _from_mysql(table, user_id), mark_dirty=False)


def get_baskets(user_id, include_hidden=True):
    """A user's baskets, in display order - highest first, like every other
    ordered list in this application."""
    rows = [b for b in _read(BASKETS, user_id)
            if include_hidden or not int(b.get('hidden') or 0)]
    return sorted(rows,
                  key=lambda b: (-float(b.get('display_order') or 0), b.get('id') or 0))


def get_basket(user_id, basket_id):
    """One basket, or None. Scoped by user, so an id from a request cannot
    reach someone else's row."""
    basket_id = int(basket_id)
    for basket in _read(BASKETS, user_id):
        if int(basket.get('id') or 0) == basket_id:
            return basket
    return None


def get_entries(user_id, basket_id=None):
    """A user's bookings, oldest first. All of them when basket_id is None -
    which is what the accrual pro-rate needs, since absence in any basket
    reduces the hours worked that every basket accrues against."""
    rows = _read(ENTRIES, user_id)
    if basket_id is not None:
        basket_id = int(basket_id)
        rows = [e for e in rows if int(e.get('basket_id') or 0) == basket_id]
    return sorted(rows, key=lambda e: (str(e.get('starts_at') or ''), e.get('id') or 0))


def basket_name_exists(user_id, name, exclude_id=None):
    """Whether this user already has a basket by that name.

    Checked here rather than by a UNIQUE index: two baskets called Vacation is
    a user error, not corruption, and the house pattern is a friendly 400 -
    _category_name_exists does the same for categories. exclude_id lets a
    rename keep its own name.
    """
    wanted = (name or '').strip().lower()
    if not wanted:
        return False
    for basket in _read(BASKETS, user_id):
        if exclude_id is not None and int(basket.get('id') or 0) == int(exclude_id):
            continue
        if (basket.get('name') or '').strip().lower() == wanted:
            return True
    return False


# ---------------------------------------------------------------- writing ----

def _filtered(data, allowed):
    """Only the columns a caller is allowed to set, in a stable order."""
    return {k: data[k] for k in allowed if k in data}


def _insert(table, user_id, data, allowed):
    """MySQL first for the real auto-increment id, then refresh the cache."""
    values = _filtered(data, allowed)
    if not values:
        log_error(logger, 'LOAF', 'Refusing to insert into %s with no columns' % table)
        return None

    values['user_id'] = user_id
    columns = list(values.keys())
    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            cursor.execute(
                'INSERT INTO %s (%s) VALUES (%s)'
                % (table, ', '.join(columns), ', '.join(['%s'] * len(columns))),
                tuple(values[c] for c in columns))
            new_id = cursor.lastrowid
    except Exception as e:
        log_exception(logger, 'LOAF', 'Error inserting into %s: %s' % (table, e))
        return None

    _refresh(table, user_id)
    invalidate_projection(user_id)
    log_info(logger, 'LOAF', 'Created %s id=%s for user %s' % (table, new_id, user_id))
    return new_id


def _update(table, user_id, row_id, data, allowed):
    """One row, by id AND user. Returns False when nothing was touched, which
    covers both "no such row" and "not yours" without telling the caller
    which."""
    values = _filtered(data, allowed)
    if not values:
        return False

    columns = list(values.keys())
    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            cursor.execute(
                'UPDATE %s SET %s WHERE id = %%s AND user_id = %%s'
                % (table, ', '.join('%s = %%s' % c for c in columns)),
                tuple(values[c] for c in columns) + (int(row_id), user_id))
            touched = cursor.rowcount
    except Exception as e:
        log_exception(logger, 'LOAF',
                      'Error updating %s id=%s: %s' % (table, row_id, e))
        return False

    # rowcount is 0 for a row that exists but was written its own values back,
    # so absence is confirmed separately rather than inferred from it.
    _refresh(table, user_id)
    invalidate_projection(user_id)
    if touched == 0:
        with get_db_pool().get_cursor() as cursor:
            cursor.execute(
                'SELECT 1 FROM %s WHERE id = %%s AND user_id = %%s' % table,
                (int(row_id), user_id))
            if cursor.fetchone() is None:
                return False
    return True


def _delete(table, user_id, row_id):
    """One row, by id AND user."""
    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            cursor.execute(
                'DELETE FROM %s WHERE id = %%s AND user_id = %%s' % table,
                (int(row_id), user_id))
            gone = cursor.rowcount
    except Exception as e:
        log_exception(logger, 'LOAF',
                      'Error deleting %s id=%s: %s' % (table, row_id, e))
        return False

    _refresh(table, user_id)
    invalidate_projection(user_id)
    return gone > 0


def create_basket(user_id, data):
    return _insert(BASKETS, user_id, data, BASKET_COLUMNS)


def update_basket(user_id, basket_id, data):
    return _update(BASKETS, user_id, basket_id, data, BASKET_COLUMNS)


def delete_basket(user_id, basket_id):
    """A basket and everything booked against it.

    MySQL cascades the entries; Redis does not, so the entries cache is
    refreshed too. Without that second refresh the calendar keeps drawing
    bookings against a basket that no longer exists until the key expires.
    """
    gone = _delete(BASKETS, user_id, basket_id)
    _refresh(ENTRIES, user_id)
    return gone


def create_entry(user_id, data):
    return _insert(ENTRIES, user_id, data, ENTRY_COLUMNS)


def update_entry(user_id, entry_id, data):
    return _update(ENTRIES, user_id, entry_id, data, ENTRY_COLUMNS)


def delete_entry(user_id, entry_id):
    return _delete(ENTRIES, user_id, entry_id)


def set_basket_order(user_id, order):
    """Re-order baskets from a list of (basket_id, display_order) pairs.

    One statement per row inside a single commit: the list is a handful long,
    and a CASE expression here would be harder to read than the loop for no
    measurable gain. Every row is still scoped by user.
    """
    pairs = [(float(o), int(i)) for i, o in order]
    if not pairs:
        return True
    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            for display_order, basket_id in pairs:
                cursor.execute(
                    'UPDATE loaf_baskets SET display_order = %s '
                    'WHERE id = %s AND user_id = %s',
                    (display_order, basket_id, user_id))
    except Exception as e:
        log_exception(logger, 'LOAF', 'Error reordering baskets: %s' % e)
        return False

    _refresh(BASKETS, user_id)
    return True


# ------------------------------------------------------------- the schedule ----

_TIME_RE = re.compile(r'^(-?)(\d+):(\d{2})(?::(\d{2}))?$')


def _minutes(value):
    """A stored TIME as minutes from midnight, or None.

    Minutes because every hours calculation downstream is arithmetic on
    minutes, and doing the parse once here keeps the engine free of string
    handling. Accepts what both read paths produce - 'HH:MM:SS' from Redis or
    from _normalise - and a timedelta, in case a caller hands over a raw row.
    """
    if value is None or value == '':
        return None
    if isinstance(value, timedelta):
        return int(value.total_seconds()) // 60
    match = _TIME_RE.match(str(value).strip())
    if not match:
        return None
    sign, hours, minutes, _seconds = match.groups()
    total = int(hours) * 60 + int(minutes)
    return -total if sign else total


def schedule_for(basket, weekday):
    """What this basket's week looks like on one weekday.

    weekday is 0=monday..6=sunday. Returns None for a day not worked - a NULL
    start - so a caller can skip it without inspecting the parts. Otherwise
    {'start', 'end', 'break_minutes'}, all in minutes.

    A day whose end is not after its start is treated as not worked. That is
    the overnight shift the plan says is out of scope, and returning None is
    the honest answer: better a day that costs nothing than a negative one
    quietly subtracted from a balance.
    """
    try:
        prefix = WEEKDAY_PREFIXES[int(weekday)]
    except (IndexError, TypeError, ValueError):
        return None

    start = _minutes(basket.get('%s_start' % prefix))
    end = _minutes(basket.get('%s_end' % prefix))
    if start is None or end is None or end <= start:
        return None

    try:
        pause = int(basket.get('%s_break_minutes' % prefix) or 0)
    except (TypeError, ValueError):
        pause = 0

    return {'start': start, 'end': end, 'break_minutes': max(0, pause)}


def scheduled_minutes(basket, weekday):
    """The minutes actually worked on one weekday, breaks removed. 0 if the day
    is not worked."""
    day = schedule_for(basket, weekday)
    if not day:
        return 0
    return max(0, (day['end'] - day['start']) - day['break_minutes'])


def attends(basket, weekday):
    """Is this a weekday the person is actually at work?

    Not the same question as scheduled_minutes, and deliberately not folded
    into it. That one answers what the EMPLOYER counts, which is what the
    accrual is pro-rated against, and for a day like this it has to keep
    saying eight hours. This one answers whether anybody is there, which is
    what decides whether booking the day costs anything.

    They differ only for a compressed week counted as a standard one - four
    ten-hour days accrued as five eights. Everywhere else accrual_only_weekdays is
    empty and this is True for every day the schedule covers, so the two
    questions have the same answer and nothing changes.

    True for an unrecognised weekday: the caller has already asked
    schedule_for, and a day off is a day off without this saying so as well.
    """
    try:
        name = WEEKDAY_NAMES[int(weekday)]
    except (IndexError, TypeError, ValueError):
        return True
    listed = str(basket.get('accrual_only_weekdays') or '').split(',')
    return name not in listed


# ------------------------------------------------------------ the projection ----

PROJECTION_KEY = 'loaf_projection:%s:{user_id}' % redis_manager.REDIS_KEY_VERSION


def invalidate_projection(user_id):
    """Drop a user's cached projection.

    Loaf does not store a balance per day - its balance moves only on accrual
    dates and days off, so the series is computed on demand and cached under
    one key. Any change to a basket or an entry makes that series wrong, so
    every write path above calls this.

    redis_manager's client is read at call time rather than imported, for the
    reason get_table_cache gives for doing the same: it works regardless of
    import order, and it avoids this module opening a second connection or
    importing app.py, which imports this.
    """
    client = getattr(redis_manager, '_redis_client', None)
    if not client:
        return False
    try:
        client.delete(PROJECTION_KEY.format(user_id=user_id))
        return True
    except Exception as e:
        log_exception(logger, 'LOAF',
                      'Could not invalidate projection for %s: %s' % (user_id, e))
        return False


# ------------------------------------------------- turning a form into rows ----
#
# Coercion lives here rather than beside the routes because this module already
# owns the column list, and two places that both believe they know the columns
# is how one of them ends up wrong.

CADENCE_UNITS = ('days', 'weeks', 'months', 'years')
BASKET_TYPES = ('pto', 'uto')
CARRYOVER_MODES = ('reset', 'all', 'capped')
ENTRY_STATUSES = ('planned', 'taken', 'cancelled')

# Which way an entry moves the balance. 'use' is time off and is everything
# this table held before; 'accrue' is a credit - hours handed over, recorded
# on the day they arrived. An accrual is never costed against the working
# week, so it is always one date and always the figure the person typed.
ENTRY_DIRECTIONS = ('use', 'accrue')

# The same lowercase names the recurring forms emit and auto_balance validates,
# so a cadence written here is one bucket_utils recognises. It skips a name it
# does not know in silence, which is a cadence that never fires.
WEEKDAY_NAMES = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday',
                 'saturday', 'sunday')

LAST_DAY = 'Last Day'


def _opt_number(value, cast=float):
    """A nullable number from a form field.

    An empty HTML number input arrives as '' and must become NULL, not 0 - the
    difference between "no ceiling" and "a ceiling of nothing", and between
    "carry everything" and "carry none of it". Returns (value, ok).
    """
    if value is None:
        return None, True
    text = str(value).strip()
    if text == '':
        return None, True
    try:
        return cast(text), True
    except (TypeError, ValueError):
        return None, False


def _opt_time(value):
    """A nullable TIME from an input type=time, which sends HH:MM."""
    if value is None:
        return None, True
    text = str(value).strip()
    if text == '':
        return None, True
    parts = text.split(':')
    if len(parts) not in (2, 3):
        return None, False
    try:
        hour, minute = int(parts[0]), int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
    except (TypeError, ValueError):
        return None, False
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        return None, False
    return '%02d:%02d:%02d' % (hour, minute, second), True


def _opt_date(value):
    if value is None:
        return None, True
    text = str(value).strip()
    if text == '':
        return None, True
    try:
        date.fromisoformat(text[:10])
    except ValueError:
        return None, False
    return text[:10], True


def _weekday_list(value):
    """Whichever recognised weekday names were sent, in week order."""
    if isinstance(value, (list, tuple)):
        sent = [str(v).strip().lower() for v in value]
    else:
        sent = [p.strip().lower() for p in str(value or '').split(',')]
    keep = [d for d in WEEKDAY_NAMES if d in sent]
    return ','.join(keep) if keep else None


def _monthly_day_list(value):
    """Day-of-month entries: 1-31, or the literal Last Day.

    Order is preserved and duplicates dropped. Anything unrecognised is
    discarded rather than rejected, matching _clean_monthly_days.
    """
    if isinstance(value, (list, tuple)):
        sent = [str(v).strip() for v in value]
    else:
        sent = [p.strip() for p in str(value or '').split(',')]
    keep = []
    for item in sent:
        if item.lower() == LAST_DAY.lower():
            candidate = LAST_DAY
        else:
            try:
                number = int(item)
            except (TypeError, ValueError):
                continue
            if not 1 <= number <= 31:
                continue
            candidate = str(number)
        if candidate not in keep:
            keep.append(candidate)
    return ','.join(keep) if keep else None


def clean_basket(payload):
    """A basket form as column values, or an error to show the user.

    Returns (values, error). error is a sentence fit for a toast; when it is
    None, values is safe to hand to create_basket or update_basket.
    """
    values = {}

    name = str(payload.get('name') or '').strip()
    if not name:
        return None, 'A basket needs a name.'
    if len(name) > 255:
        return None, 'That name is too long.'
    values['name'] = name

    kind = str(payload.get('basket_type') or 'pto').strip().lower()
    if kind not in BASKET_TYPES:
        return None, 'A basket is either PTO or UTO.'
    values['basket_type'] = kind

    unit = str(payload.get('cadence_unit') or 'weeks').strip().lower()
    if unit not in CADENCE_UNITS:
        return None, 'That pay cadence is not one Loaf understands.'
    values['cadence_unit'] = unit

    interval, ok = _opt_number(payload.get('cadence_interval'), int)
    if not ok or (interval is not None and interval < 1):
        return None, 'Pay has to land every one period or more.'
    values['cadence_interval'] = interval or 1

    mode = str(payload.get('carryover_mode') or 'reset').strip().lower()
    if mode not in CARRYOVER_MODES:
        return None, 'That carryover setting is not one Loaf understands.'
    values['carryover_mode'] = mode

    # Nullable figures. Empty means "not set", which is a real answer for every
    # one of these - see _opt_number.
    for field, label in (('max_balance_hours', 'The maximum balance'),
                         ('grant_hours', 'The granted hours'),
                         ('accrual_hours', 'The accrual'),
                         ('carryover_cap_hours', 'The carryover cap'),
                         ('low_balance_hours', 'The low-balance warning')):
        number, ok = _opt_number(payload.get(field))
        if not ok:
            return None, '%s has to be a number.' % label
        if number is not None and number < 0:
            return None, '%s cannot be negative.' % label
        values[field] = number

    starting, ok = _opt_number(payload.get('starting_hours'))
    if not ok:
        return None, 'The starting balance has to be a number.'
    values['starting_hours'] = 0.0 if starting is None else starting

    for field, label in (('accrual_anchor_date', 'The first pay date'),
                         ('starting_date', 'The starting date')):
        when, ok = _opt_date(payload.get(field))
        if not ok:
            return None, '%s is not a date Loaf can read.' % label
        values[field] = when

    month, ok = _opt_number(payload.get('year_start_month'), int)
    if not ok or (month is not None and not 1 <= month <= 12):
        return None, 'The month the year starts in has to be 1 to 12.'
    values['year_start_month'] = month or 1

    day, ok = _opt_number(payload.get('year_start_day'), int)
    if not ok or (day is not None and not 1 <= day <= 31):
        return None, 'The day the year starts on has to be 1 to 31.'
    values['year_start_day'] = day or 1

    yearly_day, ok = _opt_number(payload.get('yearly_day'), int)
    if not ok or (yearly_day is not None and not 1 <= yearly_day <= 31):
        return None, 'That day of the year is not one Loaf can use.'
    values['yearly_day'] = yearly_day

    yearly_month, ok = _opt_number(payload.get('yearly_month'), int)
    if not ok or (yearly_month is not None and not 1 <= yearly_month <= 12):
        return None, 'That month of the year is not one Loaf can use.'
    values['yearly_month'] = yearly_month

    values['weekdays'] = _weekday_list(payload.get('weekdays'))

    # Counted for accrual, never attended - see attends(). Same parser and
    # same storage shape as `weekdays` directly above, and a completely
    # different subject: that one is when the pay lands.
    values['accrual_only_weekdays'] = _weekday_list(payload.get('accrual_only_weekdays'))
    values['monthly_days'] = _monthly_day_list(payload.get('monthly_days'))
    values['hidden'] = 1 if str(payload.get('hidden') or '') in ('1', 'true', 'on') else 0

    # The working week. A day with no start is a day not worked, which is how
    # the form says "I do not work Sundays" - it clears the two time inputs.
    for index, prefix in enumerate(WEEKDAY_PREFIXES):
        for part in ('start', 'end'):
            field = '%s_%s' % (prefix, part)
            when, ok = _opt_time(payload.get(field))
            if not ok:
                return None, 'The %s time for %s is not a time.' % (
                    part, WEEKDAY_NAMES[index].capitalize())
            values[field] = when
        pause, ok = _opt_number(payload.get('%s_break_minutes' % prefix), int)
        if not ok or (pause is not None and pause < 0):
            return None, 'The break on %s has to be a whole number of minutes.' % (
                WEEKDAY_NAMES[index].capitalize())
        values['%s_break_minutes' % prefix] = pause or 0

    # A day whose end is not after its start would be silently treated as not
    # worked by the engine. Said out loud here instead, while someone is
    # looking at the form.
    for index, prefix in enumerate(WEEKDAY_PREFIXES):
        start = _minutes(values['%s_start' % prefix])
        end = _minutes(values['%s_end' % prefix])
        if (start is None) != (end is None):
            return None, ('%s needs both a start and an end, or neither.'
                          % WEEKDAY_NAMES[index].capitalize())
        if start is not None and end is not None and end <= start:
            return None, ('%s finishes before it starts. Overnight shifts are not '
                          'supported yet.' % WEEKDAY_NAMES[index].capitalize())

    return values, None


def clean_entry(payload):
    """A booking form as column values, or an error to show the user.

    The form collects a start date, an end date and - unless the whole day is
    being taken - a time for each. Composed into the two datetimes the column
    pair holds, rather than asking the browser for a datetime-local, which
    renders as two different controls depending on the browser and cannot be
    left half-filled.

    hours is NOT set here. It depends on the basket's working week, so the
    caller computes it with loaf_forecast.entry_hours once ownership of the
    basket has been established. Returns (values, error).
    """
    values = {}

    basket_id, ok = _opt_number(payload.get('basket_id'), int)
    if not ok or not basket_id:
        return None, 'Pick a basket for these hours.'
    values['basket_id'] = basket_id

    all_day = str(payload.get('all_day') or '') in ('1', 'true', 'on')
    values['all_day'] = 1 if all_day else 0

    start_date, ok = _opt_date(payload.get('start_date'))
    if not ok or start_date is None:
        return None, 'When does the time off start?'
    # An end date left blank means a single day, which is the common case and
    # not worth making someone type twice.
    end_date, ok = _opt_date(payload.get('end_date'))
    if not ok:
        return None, 'That end date is not one Loaf can read.'
    if end_date is None:
        end_date = start_date

    if all_day:
        # 23:59 rather than the next midnight: the range is inclusive of the
        # end date, and 00:00 the following day would pull in a day nobody
        # asked for. all_day makes the engine substitute the scheduled day
        # anyway, so the times only have to bracket it.
        start_time, end_time = '00:00:00', '23:59:00'
    else:
        start_time, ok = _opt_time(payload.get('start_time'))
        if not ok:
            return None, 'That start time is not a time.'
        end_time, ok = _opt_time(payload.get('end_time'))
        if not ok:
            return None, 'That end time is not a time.'
        if start_time is None or end_time is None:
            return None, ('Give a start and an end time, or tick whole days.')

    values['starts_at'] = '%s %s' % (start_date, start_time)
    values['ends_at'] = '%s %s' % (end_date, end_time)

    if values['ends_at'] <= values['starts_at']:
        # String comparison is safe on ISO datetimes, and is what the rest of
        # this codebase does with dates out of Redis.
        return None, 'That time off ends before it starts.'

    status = str(payload.get('status') or 'planned').strip().lower()
    if status not in ENTRY_STATUSES:
        return None, 'That status is not one Loaf understands.'
    values['status'] = status

    direction = str(payload.get('direction') or 'use').strip().lower()
    if direction not in ENTRY_DIRECTIONS:
        return None, 'That is not something Loaf can do with hours.'
    values['direction'] = direction

    note = str(payload.get('note') or '').strip()
    if len(note) > 255:
        return None, 'That note is too long.'
    values['note'] = note or None

    # An override is only an override when a figure was actually typed. A blank
    # box means "work it out", which is the default and not a zero.
    typed, ok = _opt_number(payload.get('hours'))
    if not ok:
        return None, 'Those hours are not a number.'
    if typed is not None and typed < 0:
        return None, 'Hours cannot be negative.'
    overridden = str(payload.get('hours_overridden') or '') in ('1', 'true', 'on')
    if overridden and typed is None:
        overridden = False
    values['hours_overridden'] = 1 if overridden else 0
    if overridden:
        values['hours'] = typed

    # A credit has no shape to work out. There is no range to intersect with a
    # working week and no schedule that knows how big it should be - somebody
    # was handed some hours, and the only source for the figure is them. So it
    # is always its own override, and always the one date it arrived on.
    if direction == 'accrue':
        if typed is None or typed <= 0:
            return None, 'How many hours were added?'
        values['hours'] = typed
        values['hours_overridden'] = 1
        values['all_day'] = 1
        values['ends_at'] = '%s 23:59:00' % start_date
        values['starts_at'] = '%s 00:00:00' % start_date

    return values, None

# ------------------------------------------------------------- describing ----

_MONTH_NAMES = ('January', 'February', 'March', 'April', 'May', 'June', 'July',
                'August', 'September', 'October', 'November', 'December')


def _ordinal(number):
    number = int(number)
    if 10 <= number % 100 <= 20:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(number % 10, 'th')
    return '%d%s' % (number, suffix)


def describe_cadence(basket):
    """When pay lands, in a sentence fragment.

    The period itself comes from bucket_utils._format_cadence_string, which is
    what the recurring pages already use - so "every 2 weeks" is worded the
    same in both apps. Only the day detail is added here, because that part
    reads differently for a pay date than for a bill.
    """
    from bucket_utils import _format_cadence_string

    unit = str(basket.get('cadence_unit') or 'weeks')
    interval = int(_as_int(basket.get('cadence_interval'), 1))
    period = _format_cadence_string(unit, interval)

    if unit == 'weeks':
        days = [d.capitalize() for d in str(basket.get('weekdays') or '').split(',') if d]
        if days:
            return 'every %s on %s' % (period, ', '.join(days))
    elif unit == 'months':
        parts = [p.strip() for p in str(basket.get('monthly_days') or '').split(',') if p.strip()]
        if parts:
            shown = [p if p.lower() == LAST_DAY.lower() else _ordinal(p) for p in parts]
            return 'every %s on the %s' % (period, ', '.join(shown))
    elif unit == 'years':
        day, month = basket.get('yearly_day'), basket.get('yearly_month')
        if day and month:
            try:
                return 'every %s on %s %s' % (
                    period, _MONTH_NAMES[int(month) - 1], _ordinal(day))
            except (IndexError, TypeError, ValueError):
                pass

    return 'every %s' % period


def _as_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def describe_fill(basket):
    """How a basket fills, in a sentence fragment, or None if it does not."""
    accrual = basket.get('accrual_hours')
    grant = basket.get('grant_hours')
    parts = []
    if accrual is not None:
        parts.append('%.2f h %s' % (float(accrual), describe_cadence(basket)))
    if grant is not None:
        parts.append('%.2f h granted each year' % float(grant))
    return ', then '.join(parts) if parts else None


def describe_carryover(basket):
    """What happens to the balance when the year turns."""
    mode = str(basket.get('carryover_mode') or 'reset')
    if mode == 'all':
        return 'carries over'
    if mode == 'capped':
        cap = basket.get('carryover_cap_hours')
        # A capped basket with no cap is resolved as zero by the engine, so it
        # is described as what it does rather than as what it was set to.
        if cap is None:
            return 'resets (no cap set)'
        return 'carries up to %.2f h' % float(cap)
    return 'resets'
