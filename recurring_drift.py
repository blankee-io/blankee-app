"""
A bill that has settled into a new habit.

A recurring bill or wage is a promise about two things: how much, and when.
Every confirmation is a chance to hold the promise up against what happened.
One miss is noise - a public holiday, a one-off credit, a typo at the bank.
The same miss twice running is a habit, and the person is asked whether the
plan should follow it.

Bills and wages only (wage_bill=1). An allowance is spent down over its
period, so its confirmed figure differs by design, and a variable income is
variable by name; neither has a promise to hold anything against.

What counts as a miss, and as the same miss twice:

- amount: any difference, to the cent. The two occurrences have to agree with
  each other to the cent as well - 54.99 then 55.10 is two different misses.
- date: more than SLACK_DAYS from the nearest day the template would have put
  it on, with the two shifts within SLACK_DAYS of each other. A weekend or
  holiday slip of a day or two never counts, and neither does the 4th followed
  by the 9th.

The check runs where a confirmation happens - the evening prompt and the bank
modal, which between them see every occurrence, since nothing is confirmed
without the person - and compares this occurrence with the previous confirmed
one in the same category. Nothing is recorded per occurrence: the entries
already hold what came through and when, and the template says what should
have.

What is recorded is the habit, once it has been seen twice: a
recurring_mismatches row, one per recurring link, which is also what the
badge on the recurring pages reads. Saying no marks it dismissed with the
habit kept on it, so the same habit is not asked about again; a different
one takes the row over and un-dismisses it. Saying yes schedules the change
from the next due date through the same mechanism as a raise or a price
change, so nothing already recorded moves.

A cadence in days ("every 14 days") has no day-of-month or weekday to move,
and its phase is its start date, which a scheduled change cannot shift
separately from where it takes effect. A date habit on one of those is left
alone; an amount habit is still offered.
"""

import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

import redis_manager
from bucket_utils import recurring_occurrence_dates
from log_config import get_logger, log_info, log_warning, log_exception

logger = get_logger(__name__)
TAG = 'DRIFT'

# A slip this size or smaller is a weekend or a bank holiday, not a habit.
SLACK_DAYS = 2

RECURRING_TABLE = {
    'income_entries': 'recurring_income',
    'expense_entries': 'recurring_expense',
    'c_expense_entries': 'recurring_c_expense',
}
KIND = {
    'income_entries': 'income',
    'expense_entries': 'expense',
    'c_expense_entries': 'c_expense',
}
CATEGORY_TABLE = {
    'recurring_income': 'income_categories',
    'recurring_expense': 'expense_categories',
    'recurring_c_expense': 'c_expense_categories',
}
ENTRY_TABLE = {v: k for k, v in RECURRING_TABLE.items()}
WEEKDAY_NAMES = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']


# ---------------------------------------------------------------- small helpers

def _as_date(value):
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _iso(value):
    d = _as_date(value)
    return d.isoformat() if d else None


def _money(value):
    try:
        return Decimal(str(value if value not in (None, '') else 0)).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal('0.00')


def _flag(value):
    return str(value).strip().lower() in ('1', 'true')


def _ordinal(n):
    n = int(n)
    if 11 <= (n % 100) <= 13:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f'{n}{suffix}'


def _split(value):
    return [p.strip() for p in str(value or '').split(',') if p.strip()]


def _user_today(user_id):
    from bucket_confirmation import _user_today
    return _user_today(user_id)


# ---------------------------------------------------------------- the template

def _span_days(link):
    """One period of the link, in days, generously - a window to look in."""
    n = max(1, int(link.get('cadence_interval') or 1))
    return {'days': n, 'weeks': 7 * n, 'months': 31 * n,
            'years': 366 * n}.get(link.get('cadence_unit'), 31 * n)


def _links(user_id, recurring_table, category_id):
    rows = redis_manager.get_table_cache(recurring_table, user_id) or []
    out = [r for r in rows if int(r.get('category_id') or 0) == int(category_id)]
    out.sort(key=lambda r: _as_date(r.get('start_date')) or date.min)
    return out


def link_for(user_id, recurring_table, category_id, on):
    """The chain link in force on `on`: the last one starting on or before it."""
    chosen = None
    for r in _links(user_id, recurring_table, category_id):
        start = _as_date(r.get('start_date'))
        if start is None or start <= on:
            chosen = r
    return chosen


def _link_by_id(user_id, recurring_table, recurring_id):
    for r in redis_manager.get_table_cache(recurring_table, user_id) or []:
        if str(r.get('id')) == str(recurring_id):
            return r
    return None


def _occurrences(link, start, end):
    """
    The days the link lands on between start and end. Walked from the link's
    own start date, not from `start`: a cadence of every second week is
    phased on where it began, and a walk from an arbitrary day would land on
    the wrong weeks.
    """
    walk_from = _as_date(link.get('start_date')) or start
    if walk_from > end:
        return []
    try:
        dates = recurring_occurrence_dates(
            link.get('cadence_interval'), link.get('cadence_unit'), walk_from, end,
            weekdays=_split(link.get('weekdays')),
            monthly_days=_split(link.get('monthly_days') or link.get('monthly_day')),
            yearly_day=link.get('yearly_day'), yearly_month=link.get('yearly_month'))
    except Exception as e:
        log_warning(logger, TAG, f"could not walk recurring {link.get('id')}: {e}")
        return []
    return [d for d in dates if d >= start]


def _expected(link, on):
    """The template's day nearest to `on`, or None if it has none nearby."""
    span = _span_days(link)
    dates = _occurrences(link, on - timedelta(days=span), on + timedelta(days=span))
    if not dates:
        return None
    return min(dates, key=lambda d: (abs((d - on).days), d))


def _miss(link, on, amount):
    """
    How one occurrence differs from its template. 'shift' is days from the
    nearest template day, None when within the slack; 'amount' is what came
    through, None when it is the template's figure.
    """
    expected = _expected(link, on)
    if expected is None:
        return None
    shift = (on - expected).days
    planned, paid = _money(link.get('amount')), _money(amount)
    return {
        'expected': expected,
        'shift': shift if abs(shift) > SLACK_DAYS else None,
        'amount': paid if paid != planned else None,
    }


def _shared_habit(now, before):
    """
    What the two misses have in common - each dimension on its own. A bill
    that has landed on the 13th twice has a day habit whatever its amounts
    did, and one that has been 54.99 twice has an amount habit whether or
    not it was on time. Returns {'amount', 'shift'}, each None where the two
    do not agree; the caller asks when either is set.
    """
    amount = now['amount'] if (now['amount'] is not None and before['amount'] == now['amount']) else None
    shift = (now['shift'] if (now['shift'] is not None and before['shift'] is not None
                              and abs(before['shift'] - now['shift']) <= SLACK_DAYS) else None)
    return {'amount': amount, 'shift': shift}


def _previous(user_id, table, category_id, before, exclude_id):
    """The most recent confirmed entry in the category dated before `before`."""
    best, best_on = None, None
    for e in redis_manager.get_table_cache(table, user_id) or []:
        if int(e.get('category_id') or 0) != int(category_id):
            continue
        if exclude_id is not None and str(e.get('id')) == str(exclude_id):
            continue
        if _flag(e.get('is_bucket')) or _flag(e.get('pending')) or _flag(e.get('is_auto_adjustment')):
            continue
        on = _as_date(e.get('date'))
        if on is None or on >= before:
            continue
        if best is None or on > best_on:
            best, best_on = e, on
    return best


# ---------------------------------------------------------------- the record

def _recorded_habit(row):
    amount = row.get('detected_amount')
    shift = row.get('detected_shift')
    return {
        'amount': _money(amount) if amount not in (None, '') else None,
        'shift': int(shift) if shift not in (None, '') else None,
    }


def _same_recorded(row, habit):
    seen = _recorded_habit(row)
    if (habit['amount'] is None) != (seen['amount'] is None) or (habit['shift'] is None) != (seen['shift'] is None):
        return False
    if habit['amount'] is not None and habit['amount'] != seen['amount']:
        return False
    if habit['shift'] is not None and abs(habit['shift'] - seen['shift']) > SLACK_DAYS:
        return False
    return True


def observe(user_id, table, category_id, on, amount, entry_id=None, transaction_id=None):
    """
    One occurrence has just been confirmed. Returns the question to put to
    the person - see describe() - when this and the previous occurrence miss
    the template the same way, or None. Never raises: a confirmation must
    not fail because the check did.
    """
    try:
        return _observe(user_id, table, category_id, on, amount, entry_id, transaction_id)
    except Exception as e:
        log_exception(logger, TAG, f"user {user_id}: drift check failed (non-blocking): {e}")
        return None


def _observe(user_id, table, category_id, on, amount, entry_id, transaction_id):
    recurring_table = RECURRING_TABLE.get(table)
    on = _as_date(on)
    if recurring_table is None or on is None or category_id is None:
        return None
    link = link_for(user_id, recurring_table, category_id, on)
    if link is None or not _flag(link.get('wage_bill')):
        return None
    now = _miss(link, on, amount)
    if now is None or (now['shift'] is None and now['amount'] is None):
        return None

    prev = _previous(user_id, table, category_id, on, entry_id)
    if prev is None:
        return None
    prev_on = _as_date(prev.get('date'))
    # Older than two periods back is not the previous occurrence, it is history.
    if prev_on < on - timedelta(days=2 * _span_days(link)):
        return None
    prev_link = link_for(user_id, recurring_table, category_id, prev_on)
    if prev_link is None or not _flag(prev_link.get('wage_bill')):
        return None
    before = _miss(prev_link, prev_on, prev.get('amount'))
    if before is None:
        return None
    habit = _shared_habit(now, before)
    if habit['amount'] is None and habit['shift'] is None:
        return None

    from redis_crud import get_recurring_mismatches, upsert_recurring_mismatch
    for row in get_recurring_mismatches(user_id, dismissed=True) or []:
        if (row.get('recurring_table') == recurring_table
                and int(row.get('category_id') or 0) == int(category_id)
                and _flag(row.get('dismissed')) and _same_recorded(row, habit)):
            # Asked, and answered no. Not again for this one.
            return None

    record = {
        'recurring_table': recurring_table,
        'recurring_id': link.get('id'),
        'category_id': int(category_id),
        'transaction_id': transaction_id,
        'entry_id': entry_id,
        'detected_amount': float(habit['amount']) if habit['amount'] is not None else None,
        'detected_shift': habit['shift'],
        'expected_date': now['expected'].isoformat(),
        'observed_date': on.isoformat(),
    }
    mismatch_id = upsert_recurring_mismatch(record, user_id)
    if mismatch_id is None:
        return None
    log_info(logger, TAG, f"user {user_id}: {recurring_table} {link.get('id')} has come through "
                          f"the same new way twice (amount {record['detected_amount']}, "
                          f"shift {record['detected_shift']})")
    return describe(user_id, dict(record, id=mismatch_id, dismissed=0))


# ---------------------------------------------------------------- the question

def _day_text(unit, on):
    """The day as the sentence says it - after "on": "the 4th", "Mondays", "4 Oct"."""
    if on is None:
        return None
    if unit == 'months':
        return f'the {_ordinal(on.day)}'
    if unit == 'weeks':
        return f'{WEEKDAY_NAMES[on.weekday()].capitalize()}s'
    if unit == 'years':
        return f'{on.day} {on.strftime("%b")}'
    return None


def _proposal(user_id, link, row):
    """
    The link that Yes would schedule: the template with the habit applied,
    and the day it takes effect. None when there is nothing to offer.
    """
    unit = link.get('cadence_unit')
    interval = max(1, int(link.get('cadence_interval') or 1))
    expected, observed = _as_date(row.get('expected_date')), _as_date(row.get('observed_date'))
    seen = _recorded_habit(row)
    shift = seen['shift'] if expected and observed else None
    amount = seen['amount'] if seen['amount'] is not None else _money(link.get('amount'))

    weekdays = [w.lower() for w in _split(link.get('weekdays'))]
    monthly_days = _split(link.get('monthly_days') or link.get('monthly_day'))
    yearly_day, yearly_month = link.get('yearly_day'), link.get('yearly_month')
    day_changed = False

    if shift is not None:
        if unit == 'months':
            last = expected.day == calendar.monthrange(expected.year, expected.month)[1]
            new_day = str(observed.day)
            for i, d in enumerate(monthly_days):
                if d == str(expected.day) or (last and d.lower() == 'last day'):
                    monthly_days[i] = new_day
                    break
            else:
                monthly_days = [new_day]
            monthly_days = list(dict.fromkeys(monthly_days))
            day_changed = True
        elif unit == 'weeks':
            old, new = WEEKDAY_NAMES[expected.weekday()], WEEKDAY_NAMES[observed.weekday()]
            weekdays = [new if w == old else w for w in weekdays] or [new]
            weekdays = list(dict.fromkeys(weekdays))
            day_changed = True
        elif unit == 'years':
            yearly_day, yearly_month = observed.day, observed.month
            day_changed = True
        # 'days': nothing to move - see the module docstring.

    if seen['amount'] is None and not day_changed:
        return None

    today = _user_today(user_id)
    upcoming = _occurrences(link, today + timedelta(days=1),
                            today + timedelta(days=2 * _span_days(link) + 400))
    if not upcoming:
        return None
    effective = upcoming[0]
    # A habit of arriving early has to take effect before the old day, or the
    # first occurrence on the new day is skipped.
    if day_changed and shift < 0:
        effective = effective + timedelta(days=shift)
    if effective <= today:
        effective = today + timedelta(days=1)

    return {
        'amount': amount,
        'cadence_interval': interval,
        'cadence_unit': unit,
        'weekdays': weekdays,
        'monthly_days': monthly_days,
        'yearly_day': yearly_day,
        'yearly_month': yearly_month,
        'effective': effective,
        'current_day': _day_text(unit, expected) if day_changed else None,
        'proposed_day': _day_text(unit, observed) if day_changed else None,
    }


def _category_name(user_id, recurring_table, category_id):
    for c in redis_manager.get_table_cache(CATEGORY_TABLE[recurring_table], user_id) or []:
        if str(c.get('id')) == str(category_id):
            return c.get('name') or ''
    return ''


def describe(user_id, row):
    """
    What the page needs to ask the question: the two figures, the two days,
    and when the change would start. None when the row no longer describes
    anything - the link it points at is gone, say.
    """
    recurring_table = row.get('recurring_table')
    if recurring_table not in ENTRY_TABLE:
        return None
    link = _link_by_id(user_id, recurring_table, row.get('recurring_id'))
    if link is None:
        return None
    proposal = _proposal(user_id, link, row)
    if proposal is None:
        return None
    seen = _recorded_habit(row)
    return {
        'id': row.get('id'),
        'recurring_table': recurring_table,
        'recurring_id': link.get('id'),
        'category_id': int(row.get('category_id') or 0),
        'category_name': _category_name(user_id, recurring_table, row.get('category_id')),
        'kind': KIND[ENTRY_TABLE[recurring_table]],
        'current_amount': float(_money(link.get('amount'))),
        'detected_amount': float(seen['amount']) if seen['amount'] is not None else None,
        'current_day': proposal['current_day'],
        'proposed_day': proposal['proposed_day'],
        'effective_date': proposal['effective'].isoformat(),
        'effective_text': proposal['effective'].strftime('%b %d, %Y'),
        'created_at': str(row.get('created_at') or '')[:19],
    }


def find_row(user_id, recurring_table, recurring_id):
    """
    The row for one recurring link. Looked up by that pair rather than by the
    row's id, which is a negative placeholder until the flush worker gives it
    a real one - a question asked and answered inside fifteen seconds would
    otherwise name a row that no longer exists.
    """
    from redis_crud import get_recurring_mismatches
    return next((m for m in (get_recurring_mismatches(user_id, dismissed=True) or [])
                 if m.get('recurring_table') == recurring_table
                 and str(m.get('recurring_id')) == str(recurring_id)), None)


def dismiss(user_id, recurring_table, recurring_id):
    """No: keep the habit on the row, marked dismissed, so it is not asked again."""
    from redis_crud import dismiss_recurring_mismatch
    row = find_row(user_id, recurring_table, recurring_id)
    if row is None:
        return False, 'That one is no longer there.'
    if not dismiss_recurring_mismatch(row['id'], user_id):
        return False, 'Could not save that.'
    return True, 'Left as it is.'


def apply(user_id, recurring_table, recurring_id):
    """
    Yes: schedule the habit as a change from the next due date, then put the
    row away. Returns (ok, message, result).
    """
    from redis_crud import dismiss_recurring_mismatch
    from app import _schedule_recurring_change, _chain_link_is_open_ended

    row = find_row(user_id, recurring_table, recurring_id)
    if row is None:
        return False, 'That one is no longer there.', None
    link = _link_by_id(user_id, recurring_table, row.get('recurring_id'))
    if link is None:
        dismiss_recurring_mismatch(row['id'], user_id)
        return False, 'That recurring entry has changed since; nothing to apply.', None
    proposal = _proposal(user_id, link, row)
    if proposal is None:
        dismiss_recurring_mismatch(row['id'], user_id)
        return False, 'There is nothing left to change.', None

    status, payload = _schedule_recurring_change(
        user_id, KIND[ENTRY_TABLE[recurring_table]], int(row['category_id']),
        proposal['effective'], float(proposal['amount']),
        proposal['cadence_interval'], proposal['cadence_unit'],
        end_date=None, no_end_date=1 if _chain_link_is_open_ended(link) else 0,
        weekdays=proposal['weekdays'], monthly_days=proposal['monthly_days'],
        yearly_day=proposal['yearly_day'], yearly_month=proposal['yearly_month'])
    if status != 200:
        return False, payload.get('message') or 'Could not schedule the change.', None
    dismiss_recurring_mismatch(row['id'], user_id)
    when = proposal['effective'].strftime('%b %d, %Y')
    log_info(logger, TAG, f"user {user_id}: {recurring_table} {link.get('id')} follows its new "
                          f"habit from {proposal['effective'].isoformat()}")
    return True, f'Changed from {when}.', {
        'recurring_id': payload.get('recurring_id'),
        'amount': float(proposal['amount']),
        'proposed_day': proposal['proposed_day'],
        'effective_date': proposal['effective'].isoformat(),
    }
