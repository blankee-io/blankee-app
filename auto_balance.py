"""
Reconciling the app against a real bank balance, on a cadence.

An installation with no bank feed has nothing that corrects drift. Every
forecast the user confirms by hand is a chance to be slightly wrong - a wage
that arrived for a different amount, a bill paid on a card instead, a cash spend
never recorded - and nothing notices. Over months the app's balance and the
bank's separate, quietly.

This closes that loop. On a cadence the user chooses they are asked what their
balance actually is, and the difference is written as one Uncategorized entry:
income when they have more than the app thinks, expense when less.

Pending bucket confirmations are auto-confirmed FIRST, before the balance is
read - not because it moves the number, but because it settles what the number
means.

Confirming is balance-neutral: a bucket already counts toward the balance, and
answering Yes keeps its amount, so the figure is identical either way. What
changes is the state behind it. Leave the buckets outstanding and the app's
balance is a forecast; the correction then measures a real bank balance against
a prediction, and the moment the user later answers Skip on one of those buckets
the balance moves again - out from under the correction that was just written to
make the two agree. Deciding them first means there is nothing left that can
shift afterwards.

The stored totals are recalculated between confirming and measuring anyway.
remainder is stored per day rather than derived on render, so nothing guarantees
it reflects the confirmations, and it also makes sure today has a row at all -
app_balance has nothing to return otherwise.

Deliberately cash only. A credit card balance is a debt, not a position, and
netting the two would ask the user a question with no single right answer.

Redis-first for entry data, as everywhere: the correction goes into Redis and is
marked dirty for the flush worker. The settings row is MySQL only, because
next_due is a cross-process claim and Redis is not where that belongs.
"""

import calendar
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import redis_manager
from db_connections import get_db_pool
from log_config import get_logger, log_info, log_warning, log_exception

logger = get_logger(__name__)

CADENCE_UNITS = ('days', 'weeks', 'months', 'years')

# The names the settings widget emits, mapped to date.weekday(). Lowercase
# because that is what recurring_income stores, and the two are the same widget.
WEEKDAY_NUMBERS = {
    'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
    'friday': 4, 'saturday': 5, 'sunday': 6,
}

# How far _next_due will scan for a qualifying day. Comfortably past the worst
# real case (every 12 months on the 31st) and short enough that a nonsensical
# setting fails fast instead of spinning.
_SCAN_LIMIT = 800

# The widget's sentinel for "whichever day ends the month". Stored as written and
# resolved per month when the calendar is walked - the 30th and the 31st are the
# same day in April and different in May.
LAST_DAY = 'Last Day'

# The category every correction lands in. Never a credit-account category - see
# the module docstring.
UNCATEGORIZED = 'Uncategorized'

# Differences below this are treated as agreement. Rounding on the user's side
# and ours will not always agree to the cent, and writing a 1p correction every
# month is noise that looks like a bug.
TOLERANCE = Decimal('0.01')


# ----------------------------------------------------------------- settings

def _row_to_settings(row):
    if not row:
        return None
    keys = ('id', 'user_id', 'enabled', 'cadence_interval', 'cadence_unit',
            'weekdays', 'monthly_days', 'notify_time', 'anchor_date',
            'next_due', 'pending_date', 'last_balanced', 'last_adjustment')
    if isinstance(row, dict):
        return {k: row.get(k) for k in keys}
    return dict(zip(keys, row[:len(keys)]))


def get_settings(user_id):
    """The user's auto-balance settings, or None if they have never set any."""
    try:
        with get_db_pool().get_cursor() as cursor:
            cursor.execute(
                "SELECT id, user_id, enabled, cadence_interval, cadence_unit, "
                "       weekdays, monthly_days, notify_time, anchor_date, "
                "       next_due, pending_date, last_balanced, last_adjustment "
                "  FROM autobalance_settings WHERE user_id = %s", (user_id,))
            return _row_to_settings(cursor.fetchone())
    except Exception as e:
        log_exception(logger, 'AUTOBALANCE',
                      f"Could not read settings for user {user_id}: {e}")
        return None


def _clean_weekdays(value):
    """A comma-separated weekday list, keeping only names the widget can emit."""
    if not value:
        return None
    if isinstance(value, str):
        value = value.split(',')
    kept = [str(v).strip().lower() for v in value
            if str(v).strip().lower() in WEEKDAY_NUMBERS]
    return ','.join(kept) if kept else None


def _clean_monthly_days(value):
    """
    A comma-separated day-of-month list: 1-31 and the literal LAST_DAY.

    LAST_DAY is carried through rather than resolved here because which day it
    means depends on the month being tested, and that is not known until
    _next_due walks the calendar. The recurring income widget offers it, and
    this is the same widget.
    """
    if not value:
        return None
    if isinstance(value, str):
        value = value.split(',')
    numbers, last = set(), False
    for v in value:
        text = str(v).strip()
        if text.lower() == LAST_DAY.lower():
            last = True
            continue
        try:
            day = int(text)
        except (TypeError, ValueError):
            continue
        if 1 <= day <= 31:
            numbers.add(day)
    parts = [str(d) for d in sorted(numbers)]
    if last:
        parts.append(LAST_DAY)
    return ','.join(parts) if parts else None


def _clean_time(value):
    """
    HH:MM as a time, defaulting to 20:00.

    Defaulting rather than refusing: a missing time is the browser not sending
    one, and the whole setting is useless if that rejects the save.
    """
    if isinstance(value, time):
        return value
    if isinstance(value, timedelta):
        # MySQL hands TIME back as a timedelta.
        total = int(value.total_seconds())
        return time(total // 3600 % 24, total % 3600 // 60)
    if value:
        text = str(value).strip()
        for fmt in ('%H:%M:%S', '%H:%M'):
            try:
                return datetime.strptime(text, fmt).time()
            except ValueError:
                continue
    return time(20, 0)


def save_settings(user_id, enabled, cadence_interval, cadence_unit,
                  weekdays=None, monthly_days=None, notify_time=None,
                  anchor_date=None):
    """
    Store the cadence, the days and the time of day.

    Returns (ok, message). Validates rather than trusting: these arrive from a
    browser, and a bad cadence_unit would make _next_due return the anchor
    forever, prompting every time the scheduler wakes.
    """
    if cadence_unit not in CADENCE_UNITS:
        return False, 'Choose how often to balance.'
    try:
        cadence_interval = int(cadence_interval)
    except (TypeError, ValueError):
        return False, 'That interval is not a number.'
    if cadence_interval < 1 or cadence_interval > 365:
        return False, 'The interval has to be between 1 and 365.'

    enabled = 1 if enabled else 0
    days_of_week = _clean_weekdays(weekdays) if cadence_unit == 'weeks' else None
    days_of_month = _clean_monthly_days(monthly_days) if cadence_unit == 'months' else None
    at = _clean_time(notify_time)

    anchor = anchor_date or date.today()
    if isinstance(anchor, str):
        try:
            anchor = date.fromisoformat(anchor[:10])
        except ValueError:
            anchor = date.today()

    # Today counts when the cadence includes it and the time has not gone by:
    # choosing every 1 day at 8pm in the afternoon should notify this evening.
    # Otherwise the first notification is the next occurrence after today, so
    # enabling a monthly check does not fire the moment it is saved.
    next_due = None
    if enabled:
        now = _user_now(user_id)
        today = now.date()
        if (_occurs_on(today, cadence_interval, cadence_unit,
                       days_of_week, days_of_month, anchor)
                and now.time() < at):
            next_due = today
        else:
            next_due = _next_due(today, cadence_interval, cadence_unit,
                                 days_of_week, days_of_month, anchor)

    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO autobalance_settings "
                "  (user_id, enabled, cadence_interval, cadence_unit, "
                "   weekdays, monthly_days, notify_time, anchor_date, next_due) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "  enabled = VALUES(enabled), "
                "  cadence_interval = VALUES(cadence_interval), "
                "  cadence_unit = VALUES(cadence_unit), "
                "  weekdays = VALUES(weekdays), "
                "  monthly_days = VALUES(monthly_days), "
                "  notify_time = VALUES(notify_time), "
                "  anchor_date = VALUES(anchor_date), "
                "  next_due = VALUES(next_due)",
                (user_id, enabled, cadence_interval, cadence_unit,
                 days_of_week, days_of_month, at.strftime('%H:%M:%S'),
                 anchor.isoformat(), next_due.isoformat() if next_due else None))
    except Exception as e:
        log_exception(logger, 'AUTOBALANCE',
                      f"Could not save settings for user {user_id}: {e}")
        return False, 'Could not save that.'

    log_info(logger, 'AUTOBALANCE',
             f"User {user_id} balance notification enabled={enabled} "
             f"every {cadence_interval} {cadence_unit} at {at}, "
             f"weekdays={days_of_week}, monthly_days={days_of_month}, "
             f"next due {next_due}")
    return True, 'Saved.'


def _user_now(user_id):
    """
    The user's own wall clock.

    Same fallback as bucket_confirmation._user_today: the server's clock when the
    zone is unknown, because being a few hours out is better than an exception,
    and the only consequence is that the first notification lands early or late
    by that much.
    """
    try:
        from zoneinfo import ZoneInfo
        from datetime import timezone as _tz
        with get_db_pool().get_cursor() as cursor:
            cursor.execute("SELECT timezone FROM users WHERE id = %s", (user_id,))
            row = cursor.fetchone()
        tz_name = (row[0] if not isinstance(row, dict) else row.get('timezone')) if row else None
        if tz_name:
            return datetime.now(_tz.utc).astimezone(ZoneInfo(tz_name))
    except Exception:
        pass
    return datetime.now()


def _occurs_on(day, interval, unit, weekdays=None, monthly_days=None, anchor=None):
    """
    Whether `day` is one of this cadence's occurrences.

    _next_due answers "when is the one after this", which is the wrong question
    when the user has just set a cadence that includes today: picking every
    1 day at 8pm at three in the afternoon should notify this evening, not
    tomorrow. This says whether a given day qualifies at all, so the caller can
    decide between today and the one after.
    """
    anchor = anchor or day
    if day < anchor:
        return False

    if unit == 'days':
        return (day - anchor).days % max(1, interval) == 0

    if unit == 'weeks':
        anchor_week = anchor - timedelta(days=anchor.weekday())
        day_week = day - timedelta(days=day.weekday())
        aligned = ((day_week - anchor_week).days // 7) % max(1, interval) == 0
        if not aligned:
            return False
        if weekdays:
            wanted = {WEEKDAY_NUMBERS[d] for d in weekdays.split(',')
                      if d in WEEKDAY_NUMBERS}
            return day.weekday() in wanted if wanted else day == anchor
        return day.weekday() == anchor.weekday()

    if unit in ('months', 'years'):
        step = max(1, interval) * (12 if unit == 'years' else 1)
        months_apart = (day.year - anchor.year) * 12 + day.month - anchor.month
        if months_apart < 0 or months_apart % step != 0:
            return False
        if unit == 'months' and monthly_days:
            parts = [d.strip() for d in monthly_days.split(',') if d.strip()]
            wanted = {int(d) for d in parts if d.isdigit()}
            want_last = any(d.lower() == LAST_DAY.lower() for d in parts)
            is_last = day.day == calendar.monthrange(day.year, day.month)[1]
            return day.day in wanted or (want_last and is_last)
        # No list: the anchor's day of the month, clamped for a short month.
        return day.day == min(anchor.day,
                              calendar.monthrange(day.year, day.month)[1])

    return False


def _add_months(d, months):
    """d shifted by whole months, clamped to the end of a short month."""
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _next_due(from_date, interval, unit, weekdays=None, monthly_days=None,
              anchor=None):
    """
    The next occurrence strictly after from_date.

    Mirrors the recurring-entry cadence because the settings UI is that same
    widget: an interval, a unit, and for weeks or months a list of days.

    With no list the anchor supplies the day - stepping by months from a 15th
    lands on the 15th, clamped so a monthly check anchored on the 31st falls on
    the 28th in February rather than overflowing into March.

    With a list, the interval still gates which weeks or months qualify, counted
    from the anchor. Without that gate "every 2 weeks on Monday" would land on
    every Monday, which is a different cadence entirely.

    Scanning forward day by day rather than computing directly: the qualifying
    set is small, the bound below is generous, and the arithmetic for "the third
    qualifying month, on whichever of these days comes first" is where a closed
    form would go wrong quietly.
    """
    anchor = anchor or from_date

    if unit == 'days':
        return from_date + timedelta(days=interval)

    if unit == 'weeks' and not weekdays:
        return from_date + timedelta(weeks=interval)

    if unit == 'months' and not monthly_days:
        return _add_months(from_date, interval)

    if unit == 'years':
        return _add_months(from_date, 12 * interval)

    if unit == 'weeks':
        wanted = {WEEKDAY_NUMBERS[d] for d in weekdays.split(',')
                  if d in WEEKDAY_NUMBERS}
        if not wanted:
            return from_date + timedelta(weeks=interval)
        # Week alignment is measured from the anchor's own week, so "every 2
        # weeks" means every second week of that series rather than of the year.
        anchor_week = anchor - timedelta(days=anchor.weekday())
        for step in range(1, _SCAN_LIMIT):
            candidate = from_date + timedelta(days=step)
            if candidate.weekday() not in wanted:
                continue
            candidate_week = candidate - timedelta(days=candidate.weekday())
            if ((candidate_week - anchor_week).days // 7) % interval == 0:
                return candidate
        return from_date + timedelta(weeks=interval)

    # months, with specific days
    parts = [d.strip() for d in monthly_days.split(',') if d.strip()]
    wanted = {int(d) for d in parts if d.isdigit()}
    want_last = any(d.lower() == LAST_DAY.lower() for d in parts)
    if not wanted and not want_last:
        return _add_months(from_date, interval)
    for step in range(1, _SCAN_LIMIT):
        candidate = from_date + timedelta(days=step)
        is_last = candidate.day == calendar.monthrange(candidate.year, candidate.month)[1]
        if candidate.day not in wanted and not (want_last and is_last):
            continue
        months_apart = ((candidate.year - anchor.year) * 12
                        + candidate.month - anchor.month)
        if months_apart % interval == 0:
            return candidate
    return _add_months(from_date, interval)


def notify_at(settings):
    """The local time of day this user should be notified."""
    return _clean_time((settings or {}).get('notify_time'))


# ------------------------------------------------------------------ raising

def claim_due(user_id, local_date):
    """
    Claim this user's turn, advancing the cadence in the same statement.

    True means this process won it and should notify. The UPDATE is the
    concurrency control: two gunicorn workers both wake, both run this, and only
    one gets rowcount = 1. Advancing next_due here rather than after the user
    responds is what makes "skip until next time" the default - ignoring the
    prompt costs nothing and it comes round again on schedule.
    """
    settings = get_settings(user_id)
    if not settings or not settings.get('enabled'):
        return False

    following = _next_due(local_date,
                          int(settings.get('cadence_interval') or 1),
                          settings.get('cadence_unit') or 'months',
                          settings.get('weekdays'),
                          settings.get('monthly_days'),
                          settings.get('anchor_date') or local_date)
    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE autobalance_settings "
                "   SET next_due = %s, pending_date = %s "
                " WHERE user_id = %s AND enabled = 1 AND next_due IS NOT NULL "
                "   AND next_due <= %s",
                (following.isoformat(), local_date.isoformat(),
                 user_id, local_date.isoformat()))
            won = cursor.rowcount == 1
    except Exception as e:
        log_exception(logger, 'AUTOBALANCE',
                      f"Could not claim the balance prompt for user {user_id}: {e}")
        return False

    if won:
        log_info(logger, 'AUTOBALANCE',
                 f"Balance prompt due for user {user_id} on {local_date}; "
                 f"next due {following}")
    return won


def clear_pending(user_id):
    """Nothing is waiting any more - the user balanced, or skipped."""
    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE autobalance_settings SET pending_date = NULL "
                " WHERE user_id = %s", (user_id,))
        return True
    except Exception as e:
        log_exception(logger, 'AUTOBALANCE',
                      f"Could not clear the pending balance for user {user_id}: {e}")
        return False


# ------------------------------------------------------------------ balance

def app_balance(user_id, on_date=None):
    """
    What the app thinks the balance is at the end of on_date.

    totals_remainders_d.remainder is the running balance for that day. It is
    stored rather than derived, so whatever last recalculated it decides this
    number - which is why apply() recalculates before reading it.

    Returns Decimal or None when there is no row for the date, which is not an
    error: a user with no entries at all has no rows.
    """
    on_date = on_date or date.today()
    try:
        with get_db_pool().get_cursor() as cursor:
            cursor.execute(
                "SELECT remainder FROM totals_remainders_d "
                " WHERE user_id = %s AND date = %s", (user_id, on_date.isoformat()))
            row = cursor.fetchone()
    except Exception as e:
        log_exception(logger, 'AUTOBALANCE',
                      f"Could not read the balance for user {user_id}: {e}")
        return None
    if not row:
        return None
    value = row['remainder'] if isinstance(row, dict) else row[0]
    return Decimal(str(value or 0))


def _uncategorized_id(table, user_id):
    """
    The user's Uncategorized category for an entry table.

    Matched by name because that is what it is: a system category every user
    gets at signup. Returns None rather than guessing at another category - a
    correction landing somewhere the user did not expect is worse than one that
    does not happen and says so.
    """
    cat_table = ('income_categories' if table == 'income_entries'
                 else 'expense_categories')
    rows = redis_manager.get_table_cache(cat_table, user_id) or []
    for row in rows:
        if str(row.get('name', '')).strip().lower() == UNCATEGORIZED.lower():
            return int(row.get('id'))
    return None


def pending_bucket_count(user_id, on_date=None):
    """How many confirmations auto-balance would answer Yes to."""
    import bucket_confirmation
    try:
        _, total = bucket_confirmation.pending_buckets(user_id, on_date=on_date)
        return total
    except Exception as e:
        log_warning(logger, 'AUTOBALANCE',
                    f"Could not count pending buckets for user {user_id}: {e}")
        return 0


def confirm_pending_buckets(user_id, on_date=None):
    """
    Answer Yes to every outstanding confirmation.

    Runs before the balance is read, for the reason in the module docstring: not
    to change the figure, which it does not, but so that nothing is left
    outstanding that could later be skipped and move the balance after the
    correction has been written.

    Sequential, and it re-reads the list each time: resolve() rewrites the whole
    entry list for the user, so answering from a list captured up front would
    have each answer overwrite the one before it.

    Returns how many were confirmed.
    """
    import bucket_confirmation
    confirmed = 0
    for _ in range(bucket_confirmation.MAX_PROMPT_ITEMS + 1):
        items, _total = bucket_confirmation.pending_buckets(user_id, on_date=on_date)
        if not items:
            break
        item = items[0]
        ok, _msg, _change = bucket_confirmation.resolve(
            user_id, item['table'], item['entry_id'], 'came_through')
        if not ok:
            log_warning(logger, 'AUTOBALANCE',
                        f"Could not auto-confirm entry {item['entry_id']} "
                        f"for user {user_id}; stopping")
            break
        confirmed += 1
    if confirmed:
        log_info(logger, 'AUTOBALANCE',
                 f"Auto-confirmed {confirmed} entr(ies) for user {user_id}")
    return confirmed


def apply(user_id, actual_balance, on_date=None):
    """
    Reconcile the app against a real balance.

    Confirms every outstanding bucket, recalculates the stored totals, reads
    what the app now thinks the balance is, and writes the difference as one
    Uncategorized entry dated today.

    Returns (ok, result) where result carries what happened, so the caller can
    say it rather than guess: confirmed, app_balance, actual, difference,
    direction, entry written or not.
    """
    from app import _update_entry_in_redis, save_totals_remainders_d

    on_date = on_date or date.today()

    try:
        actual = Decimal(str(actual_balance))
    except Exception:
        return False, {'error': 'That balance is not a number.'}

    # 1. Settle the outstanding confirmations before anything is measured. This
    #    does not move the balance - a bucket already counts and Yes keeps its
    #    amount - but it leaves nothing that a later Skip could shift.
    confirmed = confirm_pending_buckets(user_id, on_date=on_date)

    # 2. remainder is stored rather than derived, so nothing guarantees it
    #    reflects the confirmations above - and a user with no row for today has
    #    no balance for app_balance to return at all.
    try:
        save_totals_remainders_d()
    except Exception as e:
        log_exception(logger, 'AUTOBALANCE',
                      f"Could not recalculate totals for user {user_id}: {e}")
        return False, {'error': 'Could not recalculate your totals. Nothing was changed.'}

    # 3. What the app thinks now.
    current = app_balance(user_id, on_date)
    if current is None:
        return False, {'error': "There is no balance recorded for today yet.",
                       'confirmed': confirmed}

    difference = actual - current
    result = {
        'confirmed': confirmed,
        'app_balance': float(current),
        'actual': float(actual),
        'difference': float(difference),
        'entry_written': False,
        'direction': None,
    }

    if abs(difference) < TOLERANCE:
        _record_balanced(user_id, Decimal('0'))
        clear_pending(user_id)
        log_info(logger, 'AUTOBALANCE',
                 f"User {user_id} balanced with no adjustment "
                 f"(confirmed {confirmed})")
        return True, result

    # 4. More than the app expected is income it never saw; less is spending.
    if difference > 0:
        table, direction = 'income_entries', 'income'
    else:
        table, direction = 'expense_entries', 'expense'
    amount = abs(difference)

    category_id = _uncategorized_id(table, user_id)
    if category_id is None:
        log_warning(logger, 'AUTOBALANCE',
                    f"No {UNCATEGORIZED} category in {table} for user {user_id}; "
                    f"nothing written")
        return False, dict(result,
                           error=f'No {UNCATEGORIZED} category to put the '
                                 f'difference in.')

    # Dated today and Paid: it is money that has already moved, which is the
    # whole premise - the bank balance is the evidence.
    _update_entry_in_redis(table, user_id, category_id, on_date.isoformat(),
                           float(amount), processed=1)

    result['entry_written'] = True
    result['direction'] = direction
    result['category_id'] = category_id
    _record_balanced(user_id, difference)
    clear_pending(user_id)

    log_info(logger, 'AUTOBALANCE',
             f"User {user_id} balanced: app {current}, actual {actual}, "
             f"{direction} adjustment {amount} (confirmed {confirmed})")
    return True, result


def _record_balanced(user_id, difference):
    """
    Remember when and by how much, so a drift that keeps going one way shows up.

    A correction is a plug: it makes the figures agree without saying why they
    disagreed. If the same sign appears every period, something upstream is
    wrong - a recurring amount, a missing category - and this column is the only
    place that would reveal it.
    """
    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE autobalance_settings "
                "   SET last_balanced = %s, last_adjustment = %s "
                " WHERE user_id = %s",
                (date.today().isoformat(), str(difference), user_id))
    except Exception as e:
        log_warning(logger, 'AUTOBALANCE',
                    f"Could not record the balance for user {user_id}: {e}")
