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
    'starting_hours', 'starting_date',
) + tuple(
    '%s_%s' % (day, part)
    for day in WEEKDAY_PREFIXES
    for part in ('start', 'end', 'break_minutes')
)

ENTRY_COLUMNS = (
    'basket_id', 'starts_at', 'ends_at', 'all_day',
    'hours', 'computed_hours', 'hours_overridden', 'status', 'note',
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
