"""
End-of-day confirmation of forecast entries.

A bucket is a forecast: any entry dated today or later is written is_bucket=1 and
counts toward the user's totals, so future weeks show projected spending. A real
entry dated in the past depletes the matching bucket, so the forecast is consumed
rather than counted twice.

Nothing resolved a bucket whose day simply passed. The only code that turned one
into a real entry lived inside _sync_bank_transactions_for_user, which has no
callers, ran only when a bank sync had already imported transactions, and covered
credit expenses alone - so an unconfirmed forecast stayed in the totals forever.

This module is the answer to "did it actually happen?", asked once each evening:

    came_through          the forecast was right      -> becomes a real entry
    came_through_amount   right, wrong figure         -> real entry, new amount
    defer                 not yet, ask me tomorrow    -> moves to tomorrow
    skip                  it is not going to happen   -> entry and record removed

Nothing else removes anything. There is no sweeper and no automatic expiry: an
unanswered bucket keeps being asked about until the user answers it, and the
evening scheduler only sends the notification - it never touches entry or bucket
data. A bucket disappears in exactly two cases, both of them an answer the user
gave: skip, or a defer that lands on a date this category already has a bucket
for, where the pushed one is dropped instead of duplicating it.

Redis-first throughout. Every write goes to Redis and marks the table dirty for
the flush worker. The old conversion wrote to MySQL directly, which is a bug in
this architecture - the flush worker compares Redis against MySQL and reverts
anything MySQL knows that Redis does not.
"""

from datetime import date, timedelta
from decimal import Decimal
import json

import redis_manager
from db_connections import get_db_pool
from recurring_bucket_manager import get_bucket_table_for_entry_table
from log_config import get_logger, log_info, log_error, log_warning, log_exception

logger = get_logger(__name__)

# The three entry tables that can hold buckets, and the category table each one
# joins to for a display name.
ENTRY_TABLES = {
    'income_entries': 'income_categories',
    'expense_entries': 'expense_categories',
    'c_expense_entries': 'c_expense_categories',
}

ACTIONS = ('came_through', 'came_through_amount', 'defer', 'skip')



# ------------------------------------------------------------------ reading

def _entries(table, user_id):
    """A user's rows for one entry table, or None on a cache miss."""
    return redis_manager.get_table_cache(table, user_id)


def _categories(table, user_id):
    """{category_id: name} for the category table behind an entry table."""
    cat_table = ENTRY_TABLES.get(table)
    rows = redis_manager.get_table_cache(cat_table, user_id) or []
    return {int(r['id']): r.get('name', '') for r in rows if r.get('id') is not None}


def _account_names(table, user_id):
    """
    {category_id: account name} for credit expenses, empty for the others.

    A credit category belongs to a card rather than to the user directly, and
    "Groceries" on one card is a different thing from "Groceries" on another -
    so the row has to be able to say which.
    """
    if table != 'c_expense_entries':
        return {}
    cats = redis_manager.get_table_cache('c_expense_categories', user_id) or []
    accounts = redis_manager.get_table_cache('credit_accounts', user_id) or []
    by_id = {int(a['id']): a.get('name', '') for a in accounts if a.get('id') is not None}
    out = {}
    for c in cats:
        if c.get('id') is None:
            continue
        out[int(c['id'])] = by_id.get(int(c.get('account_id') or 0), '')
    return out


def _wage_bill_map(table, user_id):
    """
    {category_id: wage_bill} from the recurring template behind each category.

    A bucket with no recurring template - one created by typing an entry dated
    today - has no wage_bill, and reads as 0 (Allowance).
    """
    recurring_table = {
        'income_entries': 'recurring_income',
        'expense_entries': 'recurring_expense',
        'c_expense_entries': 'recurring_c_expense',
    }.get(table)
    rows = redis_manager.get_table_cache(recurring_table, user_id) or []
    out = {}
    for r in rows:
        cid = r.get('category_id')
        if cid is not None:
            out[int(cid)] = int(r.get('wage_bill', 0) or 0)
    return out


def pending_buckets(user_id, on_date=None):
    """
    Every unresolved bucket dated on or before `on_date`, newest first.

    Returns (items, total). Every outstanding entry is returned - there is no
    cap. A limit here would decide for the user which of their own entries they
    are allowed to see, and the count in the nav would stop matching the list the
    prompt shows.

    Returns (items, total) where total is
    how many there really were, so the caller can say what it is not showing.

    Buckets dated in the future are deliberately absent: they are forecasts that
    have not come due, and asking about them is asking the user to predict.

    "Future" is measured on the user's calendar, not the server's. A server in
    UTC is already on tomorrow's date for most of the Americas' evening, and this
    listed tomorrow's entries as due for anyone west of it.
    """
    on_date = on_date or _user_today(user_id)
    items = []

    for table in ENTRY_TABLES:
        entries = _entries(table, user_id)
        if not entries:
            continue
        names = _categories(table, user_id)
        accounts = _account_names(table, user_id)
        wage_bill = _wage_bill_map(table, user_id)

        for e in entries:
            if e.get('is_bucket') != 1:
                continue
            try:
                amount = float(e.get('amount') or 0)
            except (TypeError, ValueError):
                continue
            if amount <= 0:
                continue

            e_date = e.get('date')
            if isinstance(e_date, str):
                try:
                    e_date = date.fromisoformat(e_date)
                except ValueError:
                    continue
            if e_date is None or e_date > on_date:
                continue

            # How far this has been pushed. original_date is stamped on the
            # first defer and never moved again, so the gap between it and where
            # the entry sits now is the number of days it has been carried
            # forward - no separate counter to keep in step.
            days_pushed = 0
            orig = e.get('original_date')
            if orig:
                if isinstance(orig, str):
                    try:
                        orig = date.fromisoformat(orig)
                    except ValueError:
                        orig = None
                if orig:
                    # From where the entry now sits, not from today: the number
                    # means how many days it has been carried forward, so a
                    # bucket due the 26th and deferred to the 27th is 1. This
                    # matches app.py's _bucket_days_pushed, which the dashboard
                    # badge uses - two definitions would disagree by a day and
                    # look like a bug.
                    days_pushed = max(0, (e_date - orig).days)

            cid = int(e.get('category_id')) if e.get('category_id') is not None else None
            items.append({
                'entry_id': e.get('id'),
                'table': table,
                'category_id': cid,
                'category_name': names.get(cid, 'Unknown category'),
                'account_name': accounts.get(cid, ''),
                'date': e_date.isoformat(),
                'amount': amount,
                'original_amount': float(e.get('original_amount') or amount),
                'wage_bill': wage_bill.get(cid, 0),
                'days_overdue': (on_date - e_date).days,
                'days_pushed': days_pushed,
            })

    items.sort(key=lambda i: (i['date'], i['category_name']), reverse=True)
    return items, len(items)


def pending_overdue_count(user_id):
    """
    How many pending buckets are already late - dated before the user's today.

    Kept apart from the day's total because the two are asked about at different
    times. Today's entries wait for tonight's notification; these were asked
    about on the day they fell due and never answered, so nothing is gained by
    hiding them again each midnight.

    Expressed as pending_buckets() bounded to yesterday rather than by filtering
    its result, so the date arithmetic lives in one place rather than being
    repeated by every caller that wants to know.
    """
    _, total = pending_buckets(user_id, on_date=_user_today(user_id) - timedelta(days=1))
    return total


def prompt_raised_today(user_id):
    """
    Whether this user's prompt has already gone out for their local today.

    bucket_prompts carries one row per user per local date, written when the
    scheduler raises the notification - so its presence is the record of the
    notification having happened, and no second source is needed.

    The page uses it to decide whether to open anything. Entries fall due at
    midnight, and a modal appearing the moment a date rolls over is asking about
    a day that has not happened yet; waiting for the notification means the
    question arrives when the user chose to be asked.

    False on any error, which errs toward not interrupting.
    """
    try:
        with get_db_pool().get_cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM bucket_prompts "
                " WHERE user_id = %s AND prompt_date = %s LIMIT 1",
                (user_id, _user_today(user_id).isoformat()))
            return cursor.fetchone() is not None
    except Exception as e:
        log_warning(logger, 'BUCKET_CONFIRM',
                    f"Could not tell whether user {user_id} has been prompted: {e}")
        return False


def count_pending_from_db(user_id, on_date=None):
    """
    How many unresolved buckets a user has, read straight from MySQL.

    The scheduler needs this and cannot use pending_buckets(): that reads Redis,
    and Redis only holds users who are currently hydrated. At 20:00 most users
    are not - they have not touched the app in hours - so the cached read would
    return nothing and no prompt would ever go out for exactly the people who
    most need reminding.

    Reading MySQL here is also the cheaper choice: hydrating every user every
    evening just to count rows would pull the whole database into Redis nightly.

    The interactive route still reads Redis, because by then the user is in the
    app and hydrated.

    Defaults to the user's own date for the same reason pending_buckets does,
    though the scheduler always passes one explicitly.
    """
    on_date = on_date or _user_today(user_id)
    total = 0

    # One row per bucket. This counted distinct categories while a sweeper
    # collapsed each category to its newest due bucket; now that nothing is
    # removed unasked, every due bucket is listed, and counting categories would
    # promise fewer than the prompt goes on to show.
    #
    # Credit expense categories hang off an account, not a user - they have
    # account_id where the other two have user_id - so that one needs an extra
    # hop through credit_accounts.
    queries = (
        ("SELECT COUNT(*) FROM income_entries e "
         "  JOIN income_categories c ON c.id = e.category_id "
         " WHERE c.user_id = %s AND e.is_bucket = 1 AND e.amount > 0 AND e.date <= %s"),
        ("SELECT COUNT(*) FROM expense_entries e "
         "  JOIN expense_categories c ON c.id = e.category_id "
         " WHERE c.user_id = %s AND e.is_bucket = 1 AND e.amount > 0 AND e.date <= %s"),
        ("SELECT COUNT(*) FROM c_expense_entries e "
         "  JOIN c_expense_categories c ON c.id = e.category_id "
         "  JOIN credit_accounts a ON a.id = c.account_id "
         " WHERE a.user_id = %s AND e.is_bucket = 1 AND e.amount > 0 AND e.date <= %s"),
    )

    try:
        with get_db_pool().get_cursor() as cursor:
            for sql in queries:
                cursor.execute(sql, (user_id, on_date.isoformat()))
                row = cursor.fetchone()
                if not row:
                    continue
                value = row[0] if not isinstance(row, dict) else list(row.values())[0]
                total += int(value or 0)
    except Exception as e:
        log_exception(logger, 'BUCKET_CONFIRM',
                      f"Could not count pending buckets for user {user_id}: {e}")
        return 0
    return total


def _user_today(user_id):
    """
    Today's date where the user is.

    The browser reports its zone on every visit, so this is the same "today" the
    user sees on their own clock. Falls back to the server's date when the zone
    is unknown or unrecognised - a day out is better than an exception, and the
    only consequence is that "not yet" lands a few hours early or late.
    """
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime, timezone as _tz
        with get_db_pool().get_cursor() as cursor:
            cursor.execute("SELECT timezone FROM users WHERE id = %s", (user_id,))
            row = cursor.fetchone()
        tz_name = (row[0] if not isinstance(row, dict) else row.get('timezone')) if row else None
        if tz_name:
            return datetime.now(_tz.utc).astimezone(ZoneInfo(tz_name)).date()
    except Exception:
        pass
    return date.today()


# ------------------------------------------------------------------ writing

def _save_entries(table, user_id, entries):
    return redis_manager.set_table_cache(table, user_id, entries, mark_dirty=True)


def _record_key(bucket_table, user_id):
    return f"{bucket_table}:v1:{user_id}"


def _load_records(bucket_table, user_id):
    """Bucket records straight from Redis, matching how this module's peers read them."""
    if not redis_manager._redis_client:
        return None
    raw = redis_manager._redis_client.get(_record_key(bucket_table, user_id))
    return json.loads(raw) if raw else None


def _save_records(bucket_table, user_id, records):
    if not redis_manager._redis_client:
        return False
    redis_manager._redis_client.setex(
        _record_key(bucket_table, user_id), 604800, json.dumps(records))
    redis_manager._redis_client.sadd(f"dirty_tables:{user_id}", bucket_table)
    return True


def _apply_to_record(table, user_id, category_id, bucket_date, mutate):
    """
    Find the bucket record paired with an entry and hand it to `mutate`.

    `mutate(record, records)` returns True to keep the list, or False to drop
    that record. Every entry change has to be mirrored here: the same bucket
    lives in two stores and nothing in the database keeps them agreeing.
    """
    bucket_table = get_bucket_table_for_entry_table(table)
    if not bucket_table:
        return False
    records = _load_records(bucket_table, user_id)
    if records is None:
        return False

    kept, touched = [], False
    for r in records:
        same_cat = int(r.get('category_id', -1)) == int(category_id)
        same_date = str(r.get('bucket_date', ''))[:10] == bucket_date
        if same_cat and same_date:
            touched = True
            if mutate(r, records) is False:
                continue
        kept.append(r)

    if touched:
        _save_records(bucket_table, user_id, kept)
    else:
        log_warning(logger, 'BUCKET_CONFIRM',
                    f"No bucket record for category {category_id} on {bucket_date} "
                    f"in {bucket_table} - entry updated without its record")
    return touched


def resolve(user_id, table, entry_id, action, amount=None):
    """
    Apply one of the four answers to one bucket.

    Returns (ok, message, change) where change describes what the entry now
    looks like, or None when there was nothing to change. The browser needs it:
    it holds the same entries and has to show the result without reloading, and
    working the outcome out for itself would mean duplicating the rules above -
    including the collision case, where a defer removes the entry instead of
    moving it. The server already knows; it may as well say.

    Refuses an unknown table or action rather than guessing - these arrive from
    a browser.
    """
    if table not in ENTRY_TABLES:
        return False, 'Unknown entry type.', None
    if action not in ACTIONS:
        return False, 'Unknown action.', None

    entries = _entries(table, user_id)
    if entries is None:
        return False, 'Your data is not loaded yet. Try again in a moment.', None

    target = None
    for e in entries:
        if str(e.get('id')) == str(entry_id) and e.get('is_bucket') == 1:
            target = e
            break
    if target is None:
        # Already answered on another device since the prompt was built.
        # Not an error worth showing. No change to report - this process does not
        # know what the other one did, and guessing would be worse than the row
        # simply staying as it is until the page is next drawn.
        return True, 'That one has already been dealt with.', None

    category_id = int(target.get('category_id'))
    bucket_date = str(target.get('date'))[:10]

    def _state(entry=None, removed=False):
        """What the browser should do to its copy of this entry."""
        change = {
            'table': table,
            'entry_id': target.get('id'),
            'category_id': category_id,
            'action': action,
            'removed': removed,
        }
        if not removed and entry is not None:
            change['date'] = str(entry.get('date'))[:10]
            change['amount'] = float(entry.get('amount') or 0)
            change['is_bucket'] = int(entry.get('is_bucket') or 0)
            change['processed'] = int(entry.get('processed') or 0)
            change['original_date'] = (str(entry.get('original_date'))[:10]
                                       if entry.get('original_date') else None)
        return change

    # Yes, in either form, consumes the bucket completely - Wage, Bill, Allowance
    # or Variable alike. The forecast row becomes the record and the bucket record
    # is deleted, so there is no remainder left to spend and nothing to ask about
    # again. Allowance part-depletion is what manual entries during the period are
    # for; the evening question is whether this occurrence happened, not how much
    # of an allowance is left.
    #
    # original_amount is dropped with it. It is the forecast figure, and a real
    # entry does not carry one - _update_entry_in_redis passes None for anything
    # that is not a bucket - so clearing it leaves a confirmed entry
    # indistinguishable from one the user typed.
    if action == 'came_through':
        target['is_bucket'] = 0
        target['original_amount'] = None
        target['processed'] = 1
        _apply_to_record(table, user_id, category_id, bucket_date,
                         lambda r, rs: False)
        _save_entries(table, user_id, entries)
        return True, 'Recorded.', _state(target)

    if action == 'came_through_amount':
        try:
            new_amount = float(Decimal(str(amount)))
        except Exception:
            return False, 'That amount is not a number.', None
        if new_amount <= 0:
            return False, 'Enter an amount greater than zero.', None
        target['is_bucket'] = 0
        target['amount'] = new_amount
        target['original_amount'] = None
        target['processed'] = 1
        _apply_to_record(table, user_id, category_id, bucket_date,
                         lambda r, rs: False)
        _save_entries(table, user_id, entries)
        return True, 'Recorded.', _state(target)

    if action == 'defer':
        # Tomorrow where the user is, not the day after the bucket's own date.
        # An entry three days overdue that moved to bucket_date + 1 would still
        # be two days overdue and would ask again immediately - "not yet" has to
        # mean "ask me tomorrow", which is only true relative to today.
        tomorrow = (_user_today(user_id) + timedelta(days=1)).isoformat()

        # If tomorrow already holds a bucket for this category, this one has
        # caught up with its own next occurrence, and moving it would put two
        # buckets for one category on one date. That does not work: bucket
        # records are keyed on (category_id, bucket_date), so the pair would
        # share a single record and every later update to either would fight
        # over it. The one being pushed is the one that goes - it went unanswered
        # for its whole run, so treating it as not having happened is the
        # inference that does not invent spending.
        #
        # This is the only automatic removal left in the system, and it happens
        # only because the user pressed No on this specific entry.
        collision = None
        for other in entries:
            if other is target or other.get('is_bucket') != 1:
                continue
            if str(other.get('date'))[:10] != tomorrow:
                continue
            try:
                if int(other.get('category_id')) != category_id:
                    continue
            except (TypeError, ValueError):
                continue
            collision = other
            break

        if collision is not None:
            entries.remove(target)
            _apply_to_record(table, user_id, category_id, bucket_date,
                             lambda r, rs: False)
            _save_entries(table, user_id, entries)
            log_info(logger, 'BUCKET_CONFIRM',
                     f"Deferred bucket {entry_id} in {table} met the existing "
                     f"bucket for category {category_id} on {tomorrow} - removed")
            return (True, "Tomorrow already has this one, so it was merged in.",
                    _state(removed=True))

        # original_date is the anchor: it remembers where this forecast started,
        # however many times it is deferred.
        if not target.get('original_date'):
            target['original_date'] = bucket_date
        target['date'] = tomorrow

        def move(r, rs):
            r['bucket_date'] = tomorrow
            return True

        _apply_to_record(table, user_id, category_id, bucket_date, move)
        _save_entries(table, user_id, entries)
        return True, 'Moved to tomorrow.', _state(target)

    # skip
    entries.remove(target)
    _apply_to_record(table, user_id, category_id, bucket_date, lambda r, rs: False)
    _save_entries(table, user_id, entries)
    return True, 'Removed.', _state(removed=True)
