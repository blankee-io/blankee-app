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

Cash, savings and each credit card are reconciled separately, and each
correction is contained to the thing it corrects:

  cash     an Uncategorized income or expense entry dated today
  savings  a savings_adjustments row - the same mechanism as "Initial savings
           balance" - because a Savings category entry would move money out of
           the cash balance as well, and cash is being reconciled on its own
  cards    a signed entry in that card's Uncategorized category, not a payment.
           A payment is created from an expense against the cash balance, so it
           would move two figures when the user has already stated both.

Nothing is netted across them. A card balance is a debt and a savings balance
is not spendable cash; asking for one number covering all three would be
asking a question with no single right answer.

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

# Where a correction lands: the same place the app has always put automatic
# balance adjustments.
#
# _webhook_autobalance has reconciled against bank balances since long before
# this feature existed, and it never used a category of its own - a checking
# adjustment goes to Uncategorized, a credit adjustment to that card's
# Uncategorized, and savings to a savings_adjustments row with no category at
# all. Doing anything else here would give the app two conventions for one idea.
#
# Matched by name rather than by the is_auto_adjustment flag the bank path
# scans for. That flag is on more than one income category - Savings carries it
# too - so selecting by it is only deterministic by luck of row order.
CORRECTION_CATEGORY = 'Uncategorized'

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
            'next_due', 'pending_date', 'last_balanced', 'last_adjustment',
            'income_category_id', 'expense_category_id')
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
                "       next_due, pending_date, last_balanced, last_adjustment, "
                "       income_category_id, expense_category_id "
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

    anchor = anchor_date or _user_today(user_id)
    if isinstance(anchor, str):
        try:
            anchor = date.fromisoformat(anchor[:10])
        except ValueError:
            anchor = _user_today(user_id)

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


def _user_today(user_id):
    """The user's own date. Their clock, dated - not the server's."""
    return _user_now(user_id).date()


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

    The user's date, not the server's: on a UTC server this would otherwise read
    tomorrow's row for anyone whose evening falls after midnight UTC, and report
    a balance that includes a day they have not had yet.
    """
    on_date = on_date or _user_now(user_id).date()
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


def reconcilable(user_id):
    """
    Which balances this user can usefully state by hand.

    Only the ones no bank feed already covers. Where a feed exists it is the
    authority, and asking the user to type a figure that a sync will overwrite is
    worse than not asking: they would be correcting a number that is about to be
    replaced, and the correction entry would survive.

    Per account type, not per user, because the three are independent - a
    checking feed says nothing about whether the savings balance is being kept up
    to date, and a linked card says nothing about the others.

    Returns {'cash': bool, 'savings': bool, 'cards': set of unlinked account ids}.
    On failure everything is reconcilable: the feature not working is a smaller
    problem than a balance nobody can correct.
    """
    try:
        from bank_redis import get_user_linked_account_flags
        flags = get_user_linked_account_flags(user_id) or {}
        linked = {int(i) for i in (flags.get('linked_credit_ids') or [])}
        return {
            'cash': not flags.get('has_checking'),
            'savings': not flags.get('has_savings'),
            'linked_cards': linked,
        }
    except Exception as e:
        log_warning(logger, 'AUTOBALANCE',
                    f"Could not read bank flags for user {user_id}; offering "
                    f"every balance: {e}")
        return {'cash': True, 'savings': True, 'linked_cards': set()}


def anything_to_reconcile(user_id, on_date=None):
    """
    Whether there is any balance worth asking this user about.

    False when a feed covers the current account and the savings account and
    every card - there is nothing left they could usefully state, so the reminder
    is not sent and the modal does not open. A prompt that opens with no rows in
    it is worse than no prompt.
    """
    allowed = reconcilable(user_id)
    if allowed['cash'] or allowed['savings']:
        return True
    return any(card['account_id'] not in allowed['linked_cards']
               for card in card_balances(user_id, on_date))


def savings_balance(user_id, on_date=None):
    """
    What the app thinks the savings balance is at the end of on_date.

    savings_entries.amount is the running balance for that day, not that day's
    movement - it is computed as previous + transfers in - transfers out +
    adjustments, and stored per day the same way remainder is.

    Returns Decimal, or None when there is no row for the date.
    """
    on_date = on_date or _user_now(user_id).date()
    try:
        with get_db_pool().get_cursor() as cursor:
            cursor.execute(
                "SELECT amount FROM savings_entries "
                " WHERE user_id = %s AND date = %s", (user_id, on_date.isoformat()))
            row = cursor.fetchone()
    except Exception as e:
        log_exception(logger, 'AUTOBALANCE',
                      f"Could not read the savings balance for user {user_id}: {e}")
        return None
    if not row:
        return None
    value = row['amount'] if isinstance(row, dict) else row[0]
    return Decimal(str(value or 0))


def card_balances(user_id, on_date=None):
    """
    Each credit card and what the app thinks its balance is at the end of on_date.

    c_a_balances_d is keyed on account_id rather than user_id, so this joins
    through credit_accounts. balance there is computed as previous +
    total_expenses - total_payments, stored per day per card.

    A card with no row for the date is still listed, with None for its balance:
    the user can still say what the card actually holds, and a card quietly
    missing from the list looks like the feature not working.
    """
    on_date = on_date or _user_now(user_id).date()
    cards = []
    try:
        with get_db_pool().get_cursor() as cursor:
            cursor.execute(
                "SELECT a.id, a.name, b.balance "
                "  FROM credit_accounts a "
                "  LEFT JOIN c_a_balances_d b "
                "    ON b.account_id = a.id AND b.date = %s "
                " WHERE a.user_id = %s "
                " ORDER BY a.display_order, a.id",
                (on_date.isoformat(), user_id))
            for row in cursor.fetchall() or []:
                if isinstance(row, dict):
                    account_id, name, balance = row['id'], row['name'], row['balance']
                else:
                    account_id, name, balance = row[0], row[1], row[2]
                cards.append({
                    'account_id': int(account_id),
                    'name': name,
                    'balance': float(balance) if balance is not None else None,
                })
    except Exception as e:
        log_exception(logger, 'AUTOBALANCE',
                      f"Could not read card balances for user {user_id}: {e}")
    return cards


def _card_uncategorized_id(user_id, account_id):
    """
    A card's Uncategorized category.

    c_expense_categories hang off an account rather than a user, so every card
    has its own. Matched by name like the cash one, and None rather than a guess:
    a correction landing in a category the user did not expect is worse than one
    that does not happen and says so.
    """
    rows = redis_manager.get_table_cache('c_expense_categories', user_id) or []
    for row in rows:
        if (int(row.get('account_id') or 0) == int(account_id)
                and str(row.get('name', '')).strip().lower() == CORRECTION_CATEGORY.lower()):
            return int(row.get('id'))
    return None


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
        if str(row.get('name', '')).strip().lower() == CORRECTION_CATEGORY.lower():
            return int(row.get('id'))
    return None


def user_categories(table, user_id):
    """The user's categories for an entry table, Redis first.

    Same source _uncategorized_id reads, so the two cannot disagree about what
    exists.
    """
    cat_table = ('income_categories' if table == 'income_entries'
                 else 'expense_categories')
    rows = redis_manager.get_table_cache(cat_table, user_id)
    if rows is not None:
        return rows
    try:
        with get_db_pool().get_cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM {cat_table} WHERE user_id = %s", (user_id,))
            return list(cursor.fetchall() or [])
    except Exception as e:
        log_warning(logger, 'AUTOBALANCE',
                    f"Could not read {cat_table} for user {user_id}: {e}")
        return []


def _category_name(table, user_id, category_id):
    """The name of the category a correction went to, for the message.

    The user chose where corrections land, so the confirmation has to say where
    it actually went - "Uncategorized" was hardcoded in the browser and would
    now be a lie for anyone who changed it.
    """
    for row in user_categories(table, user_id):
        if int(row.get('id') or 0) == int(category_id):
            return row.get('name') or CORRECTION_CATEGORY
    return CORRECTION_CATEGORY


def can_hold_a_correction(row):
    """Is this a category a balance correction may be pointed at?

    System categories are excluded, which is four things at once:

      Uncategorized     the default already, offered as "(default)" rather
                        than twice
      Savings           reconciled on its own, through savings_adjustments
                        with no category at all - a correction here would be
                        counted against the savings figure as well
      Starting Balance  the anchor the whole projection is measured from
      Interest Charge   a card mechanism, not somewhere cash goes

    Credit-account payment categories go too: a cash correction does not belong
    against a card.

    Used by the picker AND by save_correction_categories. The picker is a
    convenience; the check on save is the guarantee, because a request does not
    have to come from the picker.
    """
    return (not int(row.get('is_system') or 0)
            and not int(row.get('is_credit_account') or 0))


def correction_category_id(table, user_id, settings=None):
    """Where a correction in this direction should land.

    The user's choice if they made one and it still exists, and Uncategorized
    otherwise. "Still exists" is checked rather than trusted: the foreign key
    is ON DELETE SET NULL, so a deleted category clears the setting - but a
    category can also stop belonging to this user's list between the setting
    being written and being read, and a correction landing somewhere unexpected
    is the one outcome this whole feature is meant to avoid.

    Returns None when there is no Uncategorized either, which apply() reports
    rather than guessing at another category.
    """
    settings = settings if settings is not None else (get_settings(user_id) or {})
    key = ('income_category_id' if table == 'income_entries'
           else 'expense_category_id')
    chosen = settings.get(key)

    if chosen:
        chosen = int(chosen)
        for row in user_categories(table, user_id):
            if int(row.get('id') or 0) == chosen:
                return chosen
        log_warning(logger, 'AUTOBALANCE',
                    f"User {user_id} chose category {chosen} for {table}, but it "
                    f"is not in their categories; using {CORRECTION_CATEGORY}")

    return _uncategorized_id(table, user_id)


def save_correction_categories(user_id, income_category_id, expense_category_id):
    """Store where corrections should land. None or 0 means Uncategorized.

    Separate from save_settings because the two are separate decisions: Balance
    now works whether or not the scheduled reminder is on, so choosing a target
    must not require sending - and so must not risk rewriting - the cadence and
    its next_due.

    Returns (ok, message). Validates ownership rather than trusting: these
    arrive from a browser, and an id belonging to another user would silently
    write this user's corrections into someone else's category.
    """
    resolved = {}
    for table, value, label in (
            ('income_entries', income_category_id, 'income'),
            ('expense_entries', expense_category_id, 'expense')):
        if value in (None, '', 0, '0'):
            resolved[table] = None
            continue
        try:
            value = int(value)
        except (TypeError, ValueError):
            return False, f'That {label} category is not valid.'
        row = next((r for r in user_categories(table, user_id)
                    if int(r.get('id') or 0) == value), None)
        if row is None:
            return False, f'That {label} category does not exist.'
        if not can_hold_a_correction(row):
            return False, (f"Corrections cannot go to '{row.get('name')}' - "
                           f"it is one Blankee manages itself.")
        resolved[table] = value

    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            # INSERT ... ON DUPLICATE KEY so a user who has never opened the
            # cadence settings can still choose a target. The defaults on the
            # other columns are what a fresh row gets, and enabled is 0, so this
            # does not switch the reminder on as a side effect.
            cursor.execute(
                "INSERT INTO autobalance_settings "
                "  (user_id, income_category_id, expense_category_id) "
                "VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "  income_category_id = VALUES(income_category_id), "
                "  expense_category_id = VALUES(expense_category_id)",
                (user_id, resolved['income_entries'], resolved['expense_entries']))
    except Exception as e:
        log_exception(logger, 'AUTOBALANCE',
                      f"Could not save correction categories for user {user_id}: {e}")
        return False, 'Could not save that.'

    log_info(logger, 'AUTOBALANCE',
             f"User {user_id} correction categories: "
             f"income={resolved['income_entries']}, "
             f"expense={resolved['expense_entries']}")
    return True, 'Saved.'


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
    items, outstanding = bucket_confirmation.pending_buckets(user_id, on_date=on_date)

    # Bounded by how many there were, not by a fixed number: every one of them
    # should be answered, and a fixed bound would quietly leave a backlog behind
    # on exactly the account that most needed clearing. Each success removes one,
    # so that many passes is enough, and the +1 is there to notice a stall rather
    # than to allow extra work.
    attempts = outstanding + 1
    last_id = None
    while items and attempts > 0:
        attempts -= 1
        item = items[0]
        # resolve() said it worked and the entry is still at the head of the
        # list. Something is wrong, and going round again would spin forever.
        if item['entry_id'] == last_id:
            log_warning(logger, 'AUTOBALANCE',
                        f"Entry {item['entry_id']} for user {user_id} survived "
                        f"being confirmed; stopping to avoid looping")
            break
        last_id = item['entry_id']
        ok, _msg, _change = bucket_confirmation.resolve(
            user_id, item['table'], item['entry_id'], 'came_through')
        if not ok:
            log_warning(logger, 'AUTOBALANCE',
                        f"Could not auto-confirm entry {item['entry_id']} "
                        f"for user {user_id}; stopping")
            break
        confirmed += 1
        # Re-read: resolve() rewrites the user's whole entry list, so answering
        # from a list captured up front would have each answer undo the last.
        items, _ = bucket_confirmation.pending_buckets(user_id, on_date=on_date)
    if confirmed:
        log_info(logger, 'AUTOBALANCE',
                 f"Auto-confirmed {confirmed} entr(ies) for user {user_id}")
    return confirmed


def _correct_savings(user_id, actual, on_date):
    """
    Move the savings balance to `actual` with an adjustment row.

    savings_adjustments, not an entry in the Savings category. A Savings expense
    is a transfer - it moves money out of the cash balance and into savings - and
    the user is stating their cash balance separately in the same breath, so a
    transfer here would correct savings by moving cash that was already correct.
    An adjustment is the mechanism "Initial savings balance" uses, and it touches
    nothing else.

    Returns (ok, difference) with difference as a Decimal, zero when in balance.
    """
    from app import (_get_savings_adjustments_from_redis,
                     _set_savings_adjustments_to_redis)

    current = savings_balance(user_id, on_date)
    if current is None:
        return False, None

    difference = actual - current
    if abs(difference) < TOLERANCE:
        return True, Decimal('0')

    rows = _get_savings_adjustments_from_redis(user_id)
    if rows is None:
        rows = []
    rows.append({
        'user_id': user_id,
        'date': on_date.isoformat(),
        'amount': float(difference),
        'description': 'Autobalance',
        'linked_account_id': None,
    })
    _set_savings_adjustments_to_redis(user_id, rows)

    log_info(logger, 'AUTOBALANCE',
             f"User {user_id} savings corrected by {difference} "
             f"(app {current}, actual {actual})")
    return True, difference


def _correct_card(user_id, account_id, actual, on_date):
    """
    Move one card's balance to `actual` with a signed entry in its Uncategorized.

    Not a payment, even when the balance needs to come down. A payment is created
    from an expense against the cash balance, so recording one would move the
    card and the cash together - and the cash figure has just been stated by the
    user, so moving it would undo that.

    A negative entry is therefore how a card balance comes down: the card's
    balance is previous + expenses - payments, so an expense of -20 says plainly
    "twenty pounds of spending was recorded here that did not happen". That reads
    oddly in a list of expenses, and it is still the honest entry - the
    alternative writes a payment that never left the current account.

    Returns (ok, difference).
    """
    from app import _update_entry_in_redis

    current = None
    for card in card_balances(user_id, on_date):
        if card['account_id'] == int(account_id):
            current = (Decimal(str(card['balance']))
                       if card['balance'] is not None else None)
            break
    if current is None:
        return False, None

    difference = actual - current
    if abs(difference) < TOLERANCE:
        return True, Decimal('0')

    category_id = _card_uncategorized_id(user_id, account_id)
    if category_id is None:
        log_warning(logger, 'AUTOBALANCE',
                    f"No {CORRECTION_CATEGORY} category on card {account_id} for "
                    f"user {user_id}; nothing written")
        return False, None

    _update_entry_in_redis('c_expense_entries', user_id, category_id,
                           on_date.isoformat(), float(difference), processed=1)

    log_info(logger, 'AUTOBALANCE',
             f"User {user_id} card {account_id} corrected by {difference} "
             f"(app {current}, actual {actual})")
    return True, difference


def apply(user_id, actual_balance, on_date=None, actual_savings=None,
          actual_cards=None):
    """
    Reconcile the app against a real balance.

    Confirms every outstanding bucket, recalculates the stored totals, reads
    what the app now thinks the balance is, and writes the difference as one
    Uncategorized entry dated today.

    actual_savings and actual_cards are optional: the savings balance the user
    reports, and {account_id: balance} for whichever cards they filled in. Each
    is corrected independently and none of them touches the cash figure, so the
    order among them does not matter - only that the confirmations and the
    recalculation happen first.

    Returns (ok, result) where result carries what happened, so the caller can
    say it rather than guess: confirmed, app_balance, actual, difference,
    direction, entry written or not, and the same for savings and each card.
    """
    from app import _update_entry_in_redis, save_totals_remainders_d

    # The user's date throughout, so the correction is dated the day they are
    # actually having and measured against that day's stored figures.
    on_date = on_date or _user_now(user_id).date()

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
        # Savings and the cards are corrected here as well. Returning early
        # because the cash figure agreed would silently skip them, and the two
        # have nothing to do with each other.
        _correct_the_rest(user_id, on_date, actual_savings, actual_cards, result)
        log_info(logger, 'AUTOBALANCE',
                 f"User {user_id} cash balanced with no adjustment "
                 f"(confirmed {confirmed})")
        return True, result

    # 4. More than the app expected is income it never saw; less is spending.
    if difference > 0:
        table, direction = 'income_entries', 'income'
    else:
        table, direction = 'expense_entries', 'expense'
    amount = abs(difference)

    category_id = correction_category_id(table, user_id)
    if category_id is None:
        log_warning(logger, 'AUTOBALANCE',
                    f"No {CORRECTION_CATEGORY} category in {table} for user "
                    f"{user_id}; nothing written")
        return False, dict(result,
                           error=f'No {CORRECTION_CATEGORY} category to put the '
                                 f'difference in.')

    # Dated today and Paid: it is money that has already moved, which is the
    # whole premise - the bank balance is the evidence.
    _update_entry_in_redis(table, user_id, category_id, on_date.isoformat(),
                           float(amount), processed=1)

    # And it depletes a bucket in that category, exactly as the same entry typed
    # by hand would. A correction is the user saying this money moved and they
    # had not recorded it, so it has to behave like the record they did not make
    # - otherwise the spending counts once as the correction and again as the
    # forecast it was actually part of.
    #
    # This did not matter while corrections always went to Uncategorized, which
    # has no buckets. It matters now that they can be pointed at a real
    # category, which may well be a recurring one.
    #
    # cadence_info is left out so process_manual_entry_with_bucket looks the
    # wage_bill up itself - passing 0 here would silently treat every category
    # as an allowance.
    try:
        from bucket_utils import process_manual_entry_with_bucket
        process_manual_entry_with_bucket(table, category_id, on_date,
                                         float(amount), user_id)
    except Exception as e:
        # The correction is already written and is the point of the operation.
        # A bucket left undepleted is visible and fixable; losing the correction
        # would put the app back out of step with the bank by exactly the amount
        # we just measured.
        log_exception(logger, 'AUTOBALANCE',
                      f"Correction written but bucket depletion failed for user "
                      f"{user_id}: {e}")

    result['entry_written'] = True
    result['direction'] = direction
    result['category_id'] = category_id
    result['category_name'] = _category_name(table, user_id, category_id)
    _record_balanced(user_id, difference)
    clear_pending(user_id)
    _correct_the_rest(user_id, on_date, actual_savings, actual_cards, result)

    log_info(logger, 'AUTOBALANCE',
             f"User {user_id} balanced: app {current}, actual {actual}, "
             f"{direction} adjustment {amount} (confirmed {confirmed})")
    return True, result


def _correct_the_rest(user_id, on_date, actual_savings, actual_cards, result):
    """
    Apply the savings and card corrections and record what they did.

    Separate from apply() only because both of its exit paths need it: the one
    where the cash figure needed correcting and the one where it did not.

    Card balances are recalculated at the end rather than per card - it walks
    every account, so doing it once for a user with four cards is three fewer
    passes over the same data.
    """
    from app import save_ca_daily_balance, save_totals_remainders_d

    allowed = reconcilable(user_id)

    # Checked here and not only in the route: the page decides what to show, this
    # decides what may be written. A stale page - one open since before an account
    # was linked - would otherwise post a figure for a balance the feed now owns.
    if actual_savings is not None and not allowed['savings']:
        log_info(logger, 'AUTOBALANCE',
                 f"Ignoring a savings figure for user {user_id}: a savings "
                 f"account is synced")
        actual_savings = None

    if actual_savings is not None:
        try:
            ok, diff = _correct_savings(user_id, Decimal(str(actual_savings)), on_date)
            result['savings'] = {
                'ok': ok,
                'difference': float(diff) if diff is not None else None,
                'entry_written': bool(ok and diff and abs(diff) >= TOLERANCE),
            }
        except Exception as e:
            log_exception(logger, 'AUTOBALANCE',
                          f"Could not correct savings for user {user_id}: {e}")
            result['savings'] = {'ok': False, 'difference': None,
                                 'entry_written': False}

    if actual_cards:
        result['cards'] = []
        touched = False
        for account_id, actual in actual_cards.items():
            if actual is None or actual == '':
                continue
            if int(account_id) in allowed['linked_cards']:
                log_info(logger, 'AUTOBALANCE',
                         f"Ignoring a balance for card {account_id}: it is linked "
                         f"to a bank account")
                continue
            try:
                ok, diff = _correct_card(user_id, account_id,
                                         Decimal(str(actual)), on_date)
            except Exception as e:
                log_exception(logger, 'AUTOBALANCE',
                              f"Could not correct card {account_id} for user "
                              f"{user_id}: {e}")
                ok, diff = False, None
            written = bool(ok and diff and abs(diff) >= TOLERANCE)
            touched = touched or written
            result['cards'].append({
                'account_id': int(account_id),
                'ok': ok,
                'difference': float(diff) if diff is not None else None,
                'entry_written': written,
            })
        if touched:
            # The card's daily balance is stored per day, so an entry alone does
            # not move it - the same reason the cash correction recalculates
            # totals.
            try:
                save_ca_daily_balance()
            except Exception as e:
                log_exception(logger, 'AUTOBALANCE',
                              f"Could not recalculate card balances: {e}")

    if result.get('savings', {}).get('entry_written'):
        # savings_entries is a stored running balance, rebuilt by the same pass
        # that rebuilds the remainders.
        try:
            save_totals_remainders_d()
        except Exception as e:
            log_exception(logger, 'AUTOBALANCE',
                          f"Could not recalculate savings: {e}")


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
                (_user_today(user_id).isoformat(), str(difference), user_id))
    except Exception as e:
        log_warning(logger, 'AUTOBALANCE',
                    f"Could not record the balance for user {user_id}: {e}")
