"""
Transaction import from the bank feed.

What the previous vendor's webhook did in a thousand lines of app.py, done
as a pull: ask the provider for the transactions since the last time,
store them, and hand the posted ones to the budget. The whole of "a bank
spoke" lives in this one file - the pull and its rules first, the budget
side (entries, guesses, forecasts, the notification) after.

The rules that are not obvious from the code:

  * Import starts at the moment an account was linked, not before. At link
    time auto_balance.reconcile_to_feed made the app's balance the bank's,
    so everything posted earlier is already inside that figure; importing
    it would count it twice. The bank's own posted epoch is compared with
    the link moment, so a transaction posted that morning, before the link,
    stays out.

  * A transaction the bank still calls pending is stored and nothing more.
    The bank's balance excludes it, so an entry for it would put the app
    and the bank at odds until it posted. When it posts - under the same id
    or a new one - the pending row is replaced, not joined.

  * Savings accounts are never imported. A transfer into savings is the
    checking side's expense, and the savings balance itself comes from the
    feed. Only checking and credit-card transactions become entries.

  * Every pull asks for a little history as well as the new days
    (OVERLAP_DAYS), because a bank can post a transaction dated last week
    today. Rows already stored are updated, not duplicated.

  * A posted transaction becomes an entry AT ONCE, in the category the
    guess chose, marked pending so the person can confirm or change it in
    the same modal that asks about forecasts. Remainders are right the
    moment the bank speaks; the person corrects afterwards rather than
    gating. The guess, in order: the merchant memory, then (when switched
    on) Claude, then a forecast entry of exactly this amount nearby, then
    Uncategorized.

  * A guess that names a specific forecast entry turns THAT entry into the
    real one, at the bank's amount and date - the same thing the evening
    prompt's "came through, different amount" does. A guess that only
    names a category writes a new entry and lets it deplete the category's
    forecast the way a typed entry would.

  * A forecast on a bank-fed table dated today or earlier that nothing
    matched is moved to tomorrow on each pull, exactly as a "No" in the
    evening prompt moves it. The bank now answers for those tables, so the
    prompt stops asking about them; this is what keeps an unmatched
    forecast from being counted as spent. Today's move too: the day's
    remainder then shows what the bank has actually reported, and a bill
    that posts later today is matched to its forecast on tomorrow's pull
    just the same - the guess looks a week either side.

Redis-first, like the rest of the app: linked_transactions is written
through bank_redis and the flush persists it. The user is hydrated first
when they are not - background work reads through the same cache the pages
do, and the cache only exists for a hydrated user.

The app is imported inside functions, never at module level: app imports
this module's neighbours, and auto_balance.py:1031 is the precedent for
breaking that cycle at call time.
"""

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from db_connections import get_db_pool
from log_config import get_logger, log_info, log_error, log_warning, log_exception

logger = get_logger(__name__)

TAG = 'BANK_IMPORT'

# How far back each pull looks past the last posted transaction it knows.
# A bank can post a transaction dated several days ago; a week covers what
# has been seen and costs nothing, since the rows already stored are simply
# updated.
OVERLAP_DAYS = 7

# A posted transaction with a new id replaces a stored pending one when the
# account and the amount match and the pending row's date is within this
# window of the posted date: up to five days before (a card hold that took
# the weekend to settle) or one day after (a bank that dates the pending
# row by the day it was noticed, and the posting by the day it happened).
PENDING_MATCH_BACK_DAYS = 5
PENDING_MATCH_FORWARD_DAYS = 1

# A stored pending row the bank no longer reports is dropped once it is this
# many days old. Younger than that it is given the benefit of the doubt - a
# snapshot taken mid-morning may simply not have it yet.
PENDING_GRACE_DAYS = 3

# The kinds of linked account whose transactions become entries.
IMPORTED_KINDS = ('checking', 'credit')

# Forecast entries this close to a transaction's date are offered as what it
# might be, and searched for an exact amount.
CANDIDATE_DAYS = 7

ENTRY_TABLES = {
    'income': 'income_entries',
    'expense': 'expense_entries',
    'c_expense': 'c_expense_entries',
    'c_payment': 'c_payment_entries',
}

# The entry tables that carry a pending flag - the ones the modal lists.
PENDING_TABLES = ('income_entries', 'expense_entries', 'c_expense_entries')

NOTIFICATION_TYPE = 'pending_transactions'


# ------------------------------------------------------------------ helpers

def _iso(value) -> Optional[str]:
    """A date, however the store handed it back, as 'YYYY-MM-DD'."""
    if value is None or value == '':
        return None
    if isinstance(value, (date, datetime)):
        return value.strftime('%Y-%m-%d')
    return str(value)[:10]


def _cents(value) -> Optional[int]:
    """An amount as whole cents, so equality means what a bank means by it."""
    if value is None or value == '':
        return None
    try:
        return int((Decimal(str(value)) * 100).to_integral_value())
    except Exception:
        return None


def _flag(value) -> int:
    return 1 if value in (1, True, '1', 'true', 'True') else 0


def _parse(iso: str) -> date:
    return datetime.strptime(iso, '%Y-%m-%d').date()


def _user_today(user_id: int) -> date:
    from bucket_confirmation import _user_today as f
    return f(user_id)


# ----------------------------------------------------------------- accounts

def importable_accounts(user_id: int) -> Dict[str, Dict[str, Any]]:
    """
    {account_id: row} for the linked accounts whose transactions are wanted:
    active, set to sync, and of a kind that has somewhere to go (checking or
    credit - see the module docstring on savings).
    """
    from bank_redis import _get_all_linked_accounts_raw, linked_account_kind
    out = {}
    for acc in _get_all_linked_accounts_raw(user_id) or []:
        if not _flag(acc.get('is_active', 1)):
            continue
        if not _flag(acc.get('sync_transactions', 1)):
            continue
        if linked_account_kind(acc) not in IMPORTED_KINDS:
            continue
        aid = acc.get('account_id')
        if aid:
            out[str(aid)] = acc
    return out


def link_moments(user_id: int) -> Dict[str, Tuple[int, str]]:
    """
    {account_id: (epoch, 'YYYY-MM-DD')} - when each linked account row was
    created, which is when it was linked. MySQL's own clock, read as an
    epoch so it compares with the bank's posted epoch on equal terms whatever
    the session time zone is.
    """
    out = {}
    try:
        with get_db_pool().get_cursor() as cursor:
            cursor.execute("SELECT account_id, UNIX_TIMESTAMP(created_at), DATE(created_at) "
                           "FROM linked_accounts WHERE user_id = %s", (user_id,))
            for row in cursor.fetchall():
                aid, epoch, day = (row[0], row[1], row[2]) if not isinstance(row, dict) else tuple(row.values())
                if aid and epoch:
                    out[str(aid)] = (int(epoch), _iso(day))
    except Exception as e:
        log_warning(logger, TAG, f'user {user_id}: could not read link moments: {e}')
    return out


def plan_window(accounts: Dict[str, Dict[str, Any]], stored: List[Dict[str, Any]],
                moments: Dict[str, Tuple[int, str]], today: date) -> Tuple[Optional[str], Dict[str, Tuple[int, str]]]:
    """
    (start date for the request, {account_id: (link epoch, link date)}).

    Per account the start is the later of its link date and OVERLAP_DAYS
    before its last posted transaction; the request is made from the
    earliest of those, because the Bridge answers for all accounts at once.
    An account with no link moment on record (a row the flush has not
    written yet) is treated as linked today.
    """
    floors: Dict[str, Tuple[int, str]] = {}
    starts: List[date] = []
    last_posted: Dict[str, str] = {}
    for t in stored:
        if _flag(t.get('pending')):
            continue
        aid = str(t.get('account_id') or '')
        d = _iso(t.get('date'))
        if aid and d and (aid not in last_posted or d > last_posted[aid]):
            last_posted[aid] = d
    now_epoch = int(datetime.now().timestamp())
    for aid in accounts:
        epoch, link_day = moments.get(aid) or (now_epoch, today.strftime('%Y-%m-%d'))
        floors[aid] = (epoch, link_day)
        start = _parse(link_day)
        if aid in last_posted:
            start = max(start, _parse(last_posted[aid]) - timedelta(days=OVERLAP_DAYS))
        starts.append(min(start, today))
    if not starts:
        return None, floors
    return min(starts).strftime('%Y-%m-%d'), floors


# ---------------------------------------------------------------- normalize

def normalize(fetched: List[Dict[str, Any]], accounts: Dict[str, Dict[str, Any]],
              floors: Dict[str, Tuple[int, str]]) -> List[Dict[str, Any]]:
    """
    Provider rows (the normalized shape in providers/base.py) as
    linked_transactions rows, for the accounts wanted, from the link moment
    on. The bank's posted epoch decides when it is known; the date alone when
    it is not.
    """
    from bank_redis import linked_account_kind
    out = []
    for t in fetched:
        aid = str(t.get('account_ref') or '')
        if aid not in accounts:
            continue
        txn_date = _iso(t.get('date'))
        if not txn_date or not t.get('provider_txn_id'):
            continue
        amount = t.get('amount')
        if amount is None:
            continue
        transaction_type = t.get('transaction_type') or ('expense' if float(amount) < 0 else 'income')
        # Money arriving on a card - a payment, a refund - is not imported.
        # The payment reaches the card from the other side: the checking
        # account's feed brings it in as an expense in the card's payment
        # category, and that mirrors onto the card as it does when typed.
        # Importing it here too would count the same payment twice.
        if linked_account_kind(accounts[aid]) == 'credit' and transaction_type != 'expense':
            continue
        floor = floors.get(aid)
        if floor:
            epoch, link_day = floor
            posted_at = t.get('posted_at')
            if posted_at is not None:
                if int(posted_at) < epoch:
                    continue
            elif txn_date < link_day:
                continue
        description = (t.get('description') or '').strip()
        out.append({
            'transaction_id': str(t['provider_txn_id']),
            'account_id': aid,
            'amount': abs(float(amount)),
            'date': txn_date,
            'description': description,
            'merchant_name': (t.get('merchant_name') or '').strip() or description,
            'category': t.get('category') or '',
            'pending': 1 if t.get('pending') else 0,
            'transaction_type': transaction_type,
            'provider_created_date': _iso(t.get('provider_created_at')),
        })
    return out


# --------------------------------------------------------- pending / posted

def reconcile_pending(stored: List[Dict[str, Any]], fetched: List[Dict[str, Any]],
                      window_start: Optional[str], today: date) -> Tuple[List[Dict[str, Any]], List[str], Dict[str, int]]:
    """
    Decide what one pull does to the store.

    Returns (upserts, deletes, counts). Pure: nothing is read or written
    here, so the rule can be exercised with hand-built lists.

      1. A fetched row whose id is stored: update it when amount, date,
         description or pending changed. A pending row that posts under the
         same id simply flips pending here.
      2. A fetched POSTED row with an unknown id: if a stored pending row on
         the same account has the same amount to the cent and a date within
         the match window, that pending row is what this is - delete it and
         record its id on the new row as matched_pending_id. Closest date
         wins when several qualify.
      3. A stored pending row the bank no longer reports, dated inside the
         window asked about and older than the grace period: withdrawn -
         delete it.
      4. Everything else fetched is new.
    """
    counts = {'new': 0, 'updated': 0, 'matched': 0, 'removed': 0, 'unchanged': 0}
    upserts: List[Dict[str, Any]] = []
    deletes: List[str] = []

    stored_by_id = {str(t.get('transaction_id')): t for t in stored}
    fetched_ids = {str(f['transaction_id']) for f in fetched}

    # Pending rows the bank is not reporting under their own id any more are
    # the only candidates for having posted under a new one.
    pool = [t for t in stored
            if _flag(t.get('pending')) and str(t.get('transaction_id')) not in fetched_ids]
    taken = set()

    for f in fetched:
        tid = str(f['transaction_id'])
        current = stored_by_id.get(tid)
        if current is not None:
            changed = (
                _cents(current.get('amount')) != _cents(f['amount'])
                or _iso(current.get('date')) != f['date']
                or (current.get('description') or '') != f['description']
                or _flag(current.get('pending')) != f['pending']
            )
            if changed:
                upserts.append(f)
                counts['updated'] += 1
            else:
                counts['unchanged'] += 1
            continue

        if not f['pending']:
            f_date = _parse(f['date'])
            best = None
            best_gap = None
            for p in pool:
                pid = str(p.get('transaction_id'))
                if pid in taken or str(p.get('account_id')) != f['account_id']:
                    continue
                if _cents(p.get('amount')) != _cents(f['amount']):
                    continue
                p_date = _parse(_iso(p.get('date')))
                gap = (f_date - p_date).days
                if gap < -PENDING_MATCH_FORWARD_DAYS or gap > PENDING_MATCH_BACK_DAYS:
                    continue
                if best is None or abs(gap) < best_gap:
                    best, best_gap = p, abs(gap)
            if best is not None:
                pid = str(best.get('transaction_id'))
                taken.add(pid)
                deletes.append(pid)
                f = dict(f, matched_pending_id=pid)
                counts['matched'] += 1
        upserts.append(f)
        counts['new'] += 1

    if window_start:
        cutoff = (today - timedelta(days=PENDING_GRACE_DAYS)).strftime('%Y-%m-%d')
        for p in pool:
            pid = str(p.get('transaction_id'))
            if pid in taken:
                continue
            d = _iso(p.get('date'))
            if d and window_start <= d <= cutoff:
                deletes.append(pid)
                counts['removed'] += 1

    return upserts, deletes, counts


# ------------------------------------------------------------------- store

def store(user_id: int, upserts: List[Dict[str, Any]], deletes: List[str]) -> None:
    """Apply one pull's decisions to linked_transactions, deletes first."""
    from bank_redis import bulk_upsert_linked_transactions, delete_linked_transactions
    if deletes:
        delete_linked_transactions(deletes, user_id)
    if upserts:
        bulk_upsert_linked_transactions(upserts, user_id)


def _ensure_hydrated(user_id: int) -> None:
    """
    The cache the pages read through only exists for a hydrated user, and
    the flush only writes hydrated users. Hydrate synchronously when needed;
    _hydrate_user_data is the thread's target and is safe to call inline.
    """
    import redis_manager
    if not redis_manager.is_user_hydrated(user_id):
        log_info(logger, TAG, f'user {user_id}: hydrating before the pull')
        redis_manager._hydrate_user_data(user_id)
    # Hydration writes no key for a table with no rows, and add_entry only
    # appends to a key that exists - so a first entry into an empty table
    # would reach MySQL and not the cache the pages read. An empty list is
    # the same thing hydration would have written had it written anything.
    for table in ENTRY_TABLES.values():
        if redis_manager.get_table_cache(table, user_id) is None:
            redis_manager.set_table_cache(table, user_id, [], mark_dirty=False)


# ------------------------------------------------------------- the budget

def route(user_id: int, row: Dict[str, Any], account: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Where a posted transaction goes: {'entry_type', 'table',
    'credit_account_id'}. None when it has nowhere to go yet - a card with
    no Blankee account behind it - in which case the row stays unimported
    and is tried again on the next pull.
    """
    from bank_redis import linked_account_kind, get_credit_account_for_linked_account
    kind = linked_account_kind(account)
    outflow = (row.get('transaction_type') == 'expense')
    if kind == 'checking':
        entry_type = 'expense' if outflow else 'income'
        return {'entry_type': entry_type, 'table': ENTRY_TABLES[entry_type], 'credit_account_id': None}
    if kind == 'credit':
        card = get_credit_account_for_linked_account(user_id, str(row.get('account_id')))
        if not card or card.get('id') is None:
            log_warning(logger, TAG, f"user {user_id}: no Blankee card behind linked account "
                                     f"{row.get('account_id')}; transaction {row.get('transaction_id')} waits")
            return None
        # Money in on a card is a payment towards it; money out is a purchase.
        entry_type = 'c_expense' if outflow else 'c_payment'
        return {'entry_type': entry_type, 'table': ENTRY_TABLES[entry_type], 'credit_account_id': int(card['id'])}
    return None


def _category_names(user_id: int, table: str) -> Dict[int, str]:
    from bucket_confirmation import _categories
    return _categories(table, user_id)


def _card_category_ids(user_id: int, credit_account_id: Optional[int]) -> Optional[set]:
    """The c_expense category ids of one card, or None when no card is meant."""
    if credit_account_id is None:
        return None
    import redis_manager
    cats = redis_manager.get_table_cache('c_expense_categories', user_id) or []
    out = set()
    for c in cats:
        try:
            if int(c.get('account_id') or 0) == int(credit_account_id) and c.get('id') is not None:
                out.add(int(c['id']))
        except (TypeError, ValueError):
            continue
    return out


def candidates(user_id: int, table: str, txn_date: str,
               credit_account_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Forecast entries in `table` dated within CANDIDATE_DAYS of the
    transaction, nearest first:
        [{entry_id, category_id, category_name, forecast_amount, forecast_date, gap}]
    For a card, only that card's categories. What the amount guess searches,
    and what the modal offers as "this might be that".
    """
    import redis_manager
    entries = redis_manager.get_table_cache(table, user_id) or []
    names = _category_names(user_id, table)
    allowed = _card_category_ids(user_id, credit_account_id) if table == 'c_expense_entries' else None
    when = _parse(txn_date)
    out = []
    for e in entries:
        if _flag(e.get('is_bucket')) != 1:
            continue
        try:
            amount = float(e.get('amount') or 0)
            cid = int(e.get('category_id'))
        except (TypeError, ValueError):
            continue
        if amount <= 0 or (allowed is not None and cid not in allowed):
            continue
        d = _iso(e.get('date'))
        if not d:
            continue
        gap = abs((_parse(d) - when).days)
        if gap > CANDIDATE_DAYS:
            continue
        try:
            original = float(e.get('original_amount') or 0) or amount
        except (TypeError, ValueError):
            original = amount
        out.append({'entry_id': e.get('id'), 'category_id': cid, 'category_name': names.get(cid, ''),
                    'forecast_amount': amount, 'forecast_date': d, 'gap': gap, 'original_amount': original})
    out.sort(key=lambda c: (c['gap'], c['forecast_date']))
    return out


def _canonical(user_id: int, entry_type: str, category_id: Optional[int]) -> Optional[int]:
    """The user-level category id behind a table-level one (a card's mirror -> the expense category)."""
    if category_id is None:
        return None
    if entry_type != 'c_expense':
        return int(category_id)
    try:
        from app import _canonical_expense_category_id
        return _canonical_expense_category_id(user_id, category_id)
    except Exception:
        return None


RECORD_DAYS = 5


def _record_pick(user_id: int, row: Dict[str, Any], plan: Dict[str, Any],
                 taken: set) -> Optional[Dict[str, Any]]:
    """
    An entry the person already recorded that this transaction is: same
    table (a card's own categories for a card), not a forecast, not one the
    bank brought in, the same amount to the cent, dated within RECORD_DAYS
    of the transaction. The nearest wins. Such a transaction is that entry
    posting, not a new one - a bill confirmed by hand on the 14th and posted
    by the bank on the 16th is one bill. `taken` holds entry ids already
    claimed by another row of this pull.
    """
    import redis_manager
    table, card = plan['table'], plan['credit_account_id']
    when = _iso(row.get('date'))
    if not when:
        return None
    try:
        amount = round(float(row.get('amount') or 0), 2)
    except (TypeError, ValueError):
        return None
    allowed = _card_category_ids(user_id, card) if table == 'c_expense_entries' else None
    day = _parse(when)
    best = None
    for e in redis_manager.get_table_cache(table, user_id) or []:
        if e.get('id') is None or str(e['id']) in taken:
            continue
        if _flag(e.get('is_bucket')) == 1 or _flag(e.get('pending')) == 1 or _flag(e.get('is_auto_adjustment')) == 1:
            continue
        try:
            cid = int(e.get('category_id'))
            if allowed is not None and cid not in allowed:
                continue
            if abs(round(float(e.get('amount') or 0), 2) - amount) > 0.005:
                continue
            d = _iso(e.get('date'))
            gap = abs((_parse(d) - day).days) if d else None
        except (TypeError, ValueError):
            continue
        if gap is None or gap > RECORD_DAYS:
            continue
        if best is None or gap < best[0]:
            best = (gap, e)
    return best[1] if best else None


def absorb_record(user_id: int, row: Dict[str, Any], plan: Dict[str, Any],
                  record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    The transaction is `record` posting: the entry moves to the bank's day
    (the amount already agrees), waits in the modal like any bank row, and
    the linked row points at it. Nothing is depleted - the record did that
    when it was made - so there is nothing to hand back if the category is
    changed later.
    """
    from redis_crud import update_entry
    entry_type, table = plan['entry_type'], plan['table']
    when = _iso(row['date'])
    old_day = _iso(record.get('date'))
    amount = float(record.get('amount') or 0)
    cid = int(record['category_id'])
    if not update_entry(table, record['id'], {'date': when, 'pending': 1, 'auto_confirmed': 0}, user_id):
        return None
    if old_day and old_day != when and entry_type == 'expense':
        # A payment towards a card moves with its expense (see app._shift_payment_in_redis).
        try:
            import redis_manager
            from app import _shift_payment_in_redis
            cat = next((c for c in (redis_manager.get_table_cache('expense_categories', user_id) or [])
                        if c.get('id') is not None and int(c['id']) == cid), None)
            if cat and _flag(cat.get('is_credit_account')) and cat.get('credit_account_id'):
                _shift_payment_in_redis(user_id, int(cat['credit_account_id']), old_day, when, amount, amount)
        except Exception as e:
            log_warning(logger, TAG, f'user {user_id}: could not move the card payment with the record: {e}')
    names = _category_names(user_id, table)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return {'transaction_id': row['transaction_id'], 'imported_entry_type': entry_type, 'imported_at': now,
            'imported_to_entry_id': int(record['id']), 'depleted_bucket': None,
            'custom_category_suggestion': names.get(cid), 'custom_category_id': _canonical(user_id, entry_type, cid),
            'custom_category_type': 'incoming' if entry_type == 'income' else 'outgoing',
            'custom_category_confidence': 'record', 'custom_suggestion_at': now}


def _memory_pick(user_id: int, row: Dict[str, Any], plan: Dict[str, Any]) -> Optional[int]:
    """The canonical category the merchant memory holds for this row, or None."""
    from redis_crud import lookup_category_memory
    entry_type, card = plan['entry_type'], plan['credit_account_id']
    direction = 'incoming' if entry_type == 'income' else 'outgoing'
    account_type = 'CREDIT' if card is not None else 'DEPOSITORY'
    seen = set()
    for key in (row.get('description'), row.get('merchant_name')):
        if not key or key in seen:
            continue
        seen.add(key)
        m = lookup_category_memory(user_id, description=key, category_type=direction, account_type=account_type)
        if m and m.get('category_id'):
            return int(m['category_id'])
    return None


def _category_options(user_id: int, table: str, credit_account_id: Optional[int]) -> List[Dict[str, Any]]:
    """
    [{'id', 'name', <icon flags>}] a row on `table` may be sorted into: the
    direction's categories, or a card's own. Hidden categories, interest
    charges, bundles and the starting balance are nobody's to choose - the
    same set every category picker in the app offers. The flags are what
    the picker draws its icons from.
    """
    import redis_manager
    from bucket_confirmation import ENTRY_TABLES as CATEGORY_TABLES
    cats = redis_manager.get_table_cache(CATEGORY_TABLES[table], user_id) or []
    allowed = _card_category_ids(user_id, credit_account_id) if table == 'c_expense_entries' else None
    out = []
    for c in cats:
        try:
            cid = int(c['id'])
        except (TypeError, ValueError, KeyError):
            continue
        if _flag(c.get('hidden')) or _flag(c.get('is_interest')) or _flag(c.get('is_bundle')):
            continue
        if (c.get('name') or '') == 'Starting Balance':
            continue
        if allowed is not None and cid not in allowed:
            continue
        out.append({'id': cid, 'name': c.get('name') or '',
                    'is_recurring': _flag(c.get('is_recurring')),
                    'is_credit_account': _flag(c.get('is_credit_account')),
                    'is_savings': _flag(c.get('is_savings')),
                    'is_auto_adjustment': _flag(c.get('is_auto_adjustment'))})
    return out


def _claude_picks(user_id: int, todo: List[Tuple[Dict[str, Any], Dict[str, Any], Optional[int]]]) -> Dict[str, Dict[str, Any]]:
    """
    Claude's answer for every row the memory had none for, one request per
    pull (per set of categories - a card's rows are asked against the
    card's categories). {transaction_id: {'category_id', 'canonical_id',
    'name', 'confidence'}}. Empty when the feature is off for this user.

    What leaves the server: each row's description, amount and direction,
    and the names of the categories. Nothing else.
    """
    from providers import get_enrichment_provider
    provider = get_enrichment_provider()
    if getattr(provider, 'name', '') != 'claude' or not hasattr(provider, 'suggest_categories_batch'):
        return {}
    asks = [(row, plan) for row, plan, mem, rec in todo if mem is None and rec is None and plan['entry_type'] != 'c_payment']
    if not asks:
        return {}
    try:
        if not provider.is_active(user_id):
            log_info(logger, TAG, f'user {user_id}: AI categorisation is off; {len(asks)} row(s) left to the other guesses')
            return {}
    except Exception as e:
        log_warning(logger, TAG, f'user {user_id}: could not tell whether AI is on: {e}')
        return {}

    groups: Dict[Tuple[str, Optional[int]], List[Tuple[Dict[str, Any], Dict[str, Any]]]] = {}
    for row, plan in asks:
        key = ('card', plan['credit_account_id']) if plan['table'] == 'c_expense_entries' else ('cash', None)
        groups.setdefault(key, []).append((row, plan))

    out: Dict[str, Dict[str, Any]] = {}
    for (kind, card_id), members in groups.items():
        if kind == 'card':
            options = {'outgoing': _category_options(user_id, 'c_expense_entries', card_id), 'incoming': []}
        else:
            options = {'outgoing': _category_options(user_id, 'expense_entries', None),
                       'incoming': _category_options(user_id, 'income_entries', None)}
        # Uncategorized is where a row goes when nobody has an answer, so it is
        # not an answer to offer. Listed, Claude picked it - with high
        # confidence - for anything the person's categories did not cover,
        # and the row then said "Claude's guess" for what was really no guess.
        options = {d: [o for o in lst if (o.get('name') or '').strip().lower() != 'uncategorized']
                   for d, lst in options.items()}
        items = []
        for i, (row, plan) in enumerate(members):
            items.append({'i': i,
                          'direction': 'incoming' if plan['entry_type'] == 'income' else 'outgoing',
                          'amount': float(row.get('amount') or 0),
                          'description': row.get('description') or row.get('merchant_name') or ''})
        try:
            answers = provider.suggest_categories_batch(user_id, items, options) or {}
        except Exception as e:
            log_exception(logger, TAG, f'user {user_id}: Claude categorisation failed: {e}')
            continue
        for i, a in answers.items():
            try:
                row, plan = members[int(i)]
            except (IndexError, ValueError, TypeError):
                continue
            out[str(row.get('transaction_id'))] = {
                'category_id': int(a['category_id']),
                'canonical_id': _canonical(user_id, plan['entry_type'], int(a['category_id'])),
                'name': a.get('category_name') or '',
                'confidence': a.get('confidence') or 'medium',
            }
    return out


def guess(user_id: int, row: Dict[str, Any], plan: Dict[str, Any], cands: List[Dict[str, Any]],
          mem: Optional[int] = None, ai: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    What a posted transaction is, and why:
        {'category_id', 'canonical_id', 'name', 'confidence', 'forecast'}
    category_id is what the entry table takes (a card's own category for a
    card); canonical_id the user-level one for the memory columns; forecast
    the candidate this IS when the guess named one, else None.

    In order: the merchant memory (mem, looked up beforehand); Claude's
    answer (ai, asked beforehand in one batch); a forecast nearby of exactly
    this amount; Uncategorized, with no confidence at all.

    A category with a forecast nearby makes that forecast the entry - but
    only when the forecast is a bill, a wage or a one-off, something one
    payment settles. An allowance is money for a period, and one purchase
    does not fulfil it; that guess writes an entry and lets it deplete the
    allowance the way a typed one would.
    """
    from redis_crud import resolve_suggestion_for_entry, get_uncategorized_category_id
    from bucket_confirmation import _wage_bill_map
    entry_type, table, card = plan['entry_type'], plan['table'], plan['credit_account_id']
    names = _category_names(user_id, table)
    settled_in_one = _wage_bill_map(table, user_id)

    def fulfilled(cid: int) -> Optional[Dict[str, Any]]:
        if not settled_in_one.get(cid, 1):
            return None
        return next((c for c in cands if c['category_id'] == cid), None)

    if mem is not None:
        cid = resolve_suggestion_for_entry(user_id, entry_type, card, mem)
        if cid:
            return {'category_id': int(cid), 'canonical_id': mem, 'name': names.get(int(cid), ''),
                    'confidence': 'memory', 'forecast': fulfilled(int(cid))}

    if ai and ai.get('category_id') in names:
        cid = int(ai['category_id'])
        return {'category_id': cid, 'canonical_id': ai.get('canonical_id'), 'name': names.get(cid, ''),
                'confidence': ai.get('confidence') or 'medium', 'forecast': fulfilled(cid)}

    cents = _cents(row.get('amount'))
    for c in cands:
        if _cents(c['forecast_amount']) == cents and settled_in_one.get(c['category_id'], 1):
            return {'category_id': c['category_id'], 'canonical_id': _canonical(user_id, entry_type, c['category_id']),
                    'name': c['category_name'], 'confidence': 'amount', 'forecast': c}

    unc = get_uncategorized_category_id(user_id, entry_type, account_id=card)
    return {'category_id': int(unc) if unc else None, 'canonical_id': _canonical(user_id, entry_type, unc),
            'name': names.get(int(unc), 'Uncategorized') if unc else 'Uncategorized',
            'confidence': None, 'forecast': None}


def _snapshot(table: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Enough of a forecast entry to put it back later."""
    return {
        'table': table,
        'entry_id': entry.get('id'),
        'category_id': entry.get('category_id'),
        'date': _iso(entry.get('date')),
        'original_date': _iso(entry.get('original_date')),
        'amount': float(entry.get('amount') or 0),
        'original_amount': (float(entry['original_amount']) if entry.get('original_amount') is not None else None),
        'recurring_id': entry.get('recurring_id'),
    }


def apply_guess(user_id: int, row: Dict[str, Any], plan: Dict[str, Any],
                g: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Write the entry the guess describes and say how the linked row should
    record it. None when nothing could be written (the row stays unimported
    and is tried again).

    A card payment is written confirmed: there is no category to ask about.
    Everything else is written pending, for the modal.
    """
    from redis_crud import add_entry, update_entry
    entry_type, table, card = plan['entry_type'], plan['table'], plan['credit_account_id']
    amount = float(row['amount'])
    when = _iso(row['date'])
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    mark = {'transaction_id': row['transaction_id'], 'imported_entry_type': entry_type, 'imported_at': now}

    if entry_type == 'c_payment':
        eid = add_entry(table, {'account_id': card, 'date': when, 'amount': amount, 'recurring_id': None,
                                'processed': 1, 'auto_confirmed': 0, 'is_auto_adjustment': 0}, user_id)
        if not eid:
            return None
        return dict(mark, imported_to_entry_id=int(eid))

    if g.get('category_id') is None:
        log_warning(logger, TAG, f"user {user_id}: no category at all for {table}; transaction "
                                 f"{row.get('transaction_id')} waits")
        return None
    cid = int(g['category_id'])
    direction = 'incoming' if entry_type == 'income' else 'outgoing'
    suggestion = {
        'custom_category_suggestion': g.get('name'),
        'custom_category_id': g.get('canonical_id'),
        'custom_category_type': direction,
        'custom_category_confidence': g.get('confidence'),
        'custom_suggestion_at': now if g.get('confidence') else None,
    }

    forecast = g.get('forecast')
    if forecast and forecast.get('entry_id') is not None:
        # The forecast becomes the record, at the bank's figure and on the
        # bank's day - what the evening prompt's "came through, different
        # amount" does, then dated when the money actually moved.
        import redis_manager
        from bucket_confirmation import resolve
        entries = redis_manager.get_table_cache(table, user_id) or []
        target = next((e for e in entries if str(e.get('id')) == str(forecast['entry_id'])), None)
        if target is None:
            return None
        snapshot = _snapshot(table, target)
        ok, msg, change = resolve(user_id, table, forecast['entry_id'], 'came_through_amount', amount)
        if not ok or change is None:
            log_warning(logger, TAG, f"user {user_id}: forecast {forecast['entry_id']} could not be taken: {msg}")
            return None
        update_entry(table, forecast['entry_id'], {'date': when, 'original_date': None, 'pending': 1,
                                                   'auto_confirmed': 0}, user_id)
        _add_mirror_payment(user_id, entry_type, cid, when, amount)
        return dict(mark, imported_to_entry_id=int(forecast['entry_id']), depleted_bucket=snapshot, **suggestion)

    # A category, not a particular forecast: a new entry, depleting the
    # category's forecast the way a typed entry does (a bill's due
    # occurrence in full, an allowance by the amount).
    from bucket_utils import find_next_bucket_for_category, process_manual_entry_with_bucket
    snapshot = None
    try:
        bucket = find_next_bucket_for_category(table, cid, user_id)
        if bucket:
            snapshot = _snapshot(table, bucket)
    except Exception as e:
        log_warning(logger, TAG, f'user {user_id}: could not look ahead at the forecast for {table}/{cid}: {e}')
    data = {'category_id': cid, 'date': when, 'original_date': None, 'amount': amount, 'recurring_id': None,
            'is_bucket': 0, 'original_amount': None, 'processed': 1, 'auto_confirmed': 0,
            'is_auto_adjustment': 0, 'pending': 1}
    if table != 'income_entries':
        data['bundle_item_id'] = None
    eid = add_entry(table, data, user_id)
    if not eid:
        return None
    try:
        process_manual_entry_with_bucket(table, cid, when, Decimal(str(amount)), user_id)
    except Exception as e:
        log_exception(logger, TAG, f'user {user_id}: depleting the forecast for {table}/{cid} failed: {e}')
    _add_mirror_payment(user_id, entry_type, cid, when, amount)
    return dict(mark, imported_to_entry_id=int(eid), depleted_bucket=snapshot, **suggestion)


def _add_mirror_payment(user_id: int, entry_type: str, category_id: int, when: str, amount: float) -> None:
    """
    An expense in a card's mirror category ("Payment to Visa") is a payment
    towards that card, and the app records the c_payment when one is typed.
    The same here, linked card or not: the card's own feed does not bring
    payments in (see normalize), so this is the only place the payment
    reaches the card.
    """
    if entry_type != 'expense':
        return
    import redis_manager
    cat = next((c for c in (redis_manager.get_table_cache('expense_categories', user_id) or [])
                if c.get('id') is not None and int(c['id']) == int(category_id)), None)
    if not cat or not _flag(cat.get('is_credit_account')) or not cat.get('credit_account_id'):
        return
    account_id = int(cat['credit_account_id'])
    try:
        from app import _update_payment_entry_in_redis, _get_entries_from_redis
        existing = next((p for p in (_get_entries_from_redis('c_payment_entries', user_id) or [])
                         if str(p.get('account_id')) == str(account_id) and str(p.get('date'))[:10] == when), None)
        total = float(amount) + (float(existing.get('amount') or 0) if existing else 0.0)
        _update_payment_entry_in_redis(user_id, account_id, when, total)
    except Exception as e:
        log_warning(logger, TAG, f'user {user_id}: could not record the card payment for category {category_id}: {e}')


def create_entries(user_id: int) -> Dict[str, Any]:
    """
    Every posted, unimported transaction becomes an entry. Returns
    {'imported', 'skipped', 'cards', 'earliest'} - cards says whether a
    card's figures moved, earliest is the first day the totals changed on.
    """
    from bank_redis import get_linked_transactions, bulk_upsert_linked_transactions
    accounts = importable_accounts(user_id)
    stored = list(get_linked_transactions(user_id) or [])
    updates: List[Dict[str, Any]] = []
    earliest = None
    cards = False
    skipped = 0
    # Two passes: what each row is and what the memory says, then one
    # request to Claude for the rows the memory had nothing for, then the
    # writing. Asking per row would be a request per transaction.
    todo: List[Tuple[Dict[str, Any], Dict[str, Any], Optional[int], Optional[Dict[str, Any]]]] = []
    taken: set = {str(r.get('imported_to_entry_id')) for r in stored if r.get('imported_to_entry_id')}
    for row in stored:
        if _flag(row.get('pending')) or row.get('imported_to_entry_id'):
            continue
        acc = accounts.get(str(row.get('account_id')))
        if not acc:
            continue
        plan = route(user_id, row, acc)
        if not plan:
            skipped += 1
            continue
        # First: is this an entry the person already recorded? Then no guess
        # is needed and nothing new is written.
        rec = None if plan['entry_type'] == 'c_payment' else _record_pick(user_id, row, plan, taken)
        if rec is not None:
            taken.add(str(rec['id']))
        mem = None if (rec is not None or plan['entry_type'] == 'c_payment') else _memory_pick(user_id, row, plan)
        todo.append((row, plan, mem, rec))
    picks = _claude_picks(user_id, todo)
    for row, plan, mem, rec in todo:
        when = _iso(row.get('date'))
        if rec is not None:
            update = absorb_record(user_id, dict(row, date=when), plan, rec)
        else:
            if plan['entry_type'] == 'c_payment':
                g: Dict[str, Any] = {}
            else:
                g = guess(user_id, row, plan, candidates(user_id, plan['table'], when, plan['credit_account_id']),
                          mem=mem, ai=picks.get(str(row.get('transaction_id'))))
            update = apply_guess(user_id, dict(row, date=when), plan, g)
        if not update:
            skipped += 1
            continue
        updates.append(update)
        cards = cards or plan['credit_account_id'] is not None
        earliest = when if earliest is None or when < earliest else earliest
    if updates:
        bulk_upsert_linked_transactions(updates, user_id)
    return {'imported': len(updates), 'skipped': skipped, 'cards': cards, 'earliest': earliest}


def last_sync_date(user_id: int) -> Optional[str]:
    """
    The last day the bank has spoken for: the date of the latest posted
    transaction it has reported. None before the first one.

    Not the day of the last pull. A pull at six in the morning that brings
    back transactions dated yesterday has heard nothing about today, and
    locking today on the strength of it took the day away from the person
    while the bank had not spoken for it. The bank's own dates are the only
    days it has vouched for.
    """
    try:
        from bank_redis import get_last_linked_transaction_date
        return get_last_linked_transaction_date(user_id)
    except Exception as e:
        log_warning(logger, TAG, f'user {user_id}: could not read the last sync date: {e}')
        return None


def locked_day(user_id: int, entry_type: str, category_id, when) -> bool:
    """
    Whether a day on this account is the bank's rather than the person's:
    any day up to the last sync, on the current account when a checking
    account is linked, or on a card that is. Those days are filled from
    the feed; an entry typed there would be counted again when the bank
    posts it. Days since the last sync are still the person's - the bank
    has not spoken for them yet - and so are the forecasts ahead.
    """
    try:
        d = _iso(when)
        synced = last_sync_date(user_id)
        if not d or not synced or d > synced:
            return False
        from bank_redis import get_user_linked_account_flags
        flags = get_user_linked_account_flags(user_id) or {}
        if entry_type in ('income', 'expense'):
            return bool(flags.get('has_checking'))
        if entry_type == 'ca':
            linked = {int(i) for i in (flags.get('linked_credit_ids') or [])}
            if not linked or category_id is None:
                return False
            import redis_manager
            for c in redis_manager.get_table_cache('c_expense_categories', user_id) or []:
                if c.get('id') is not None and int(c['id']) == int(category_id):
                    return int(c.get('account_id') or 0) in linked
        return False
    except Exception as e:
        log_warning(logger, TAG, f'user {user_id}: could not tell whether {when} is locked: {e}')
        return False


LOCKED_MESSAGE = 'Days up to the last bank sync on a linked account come from the bank; the feed has filled them in.'


def fed_tables(user_id: int) -> Dict[str, Optional[set]]:
    """
    {table: category ids, or None for all of them} for the entry tables a
    bank feed now answers for: income and expense when a checking account
    is linked, and a linked card's own categories.
    """
    from bank_redis import get_user_linked_account_flags
    flags = get_user_linked_account_flags(user_id)
    out: Dict[str, Optional[set]] = {}
    if flags.get('has_checking'):
        out['income_entries'] = None
        out['expense_entries'] = None
    allowed = set()
    for cid in flags.get('linked_credit_ids') or []:
        allowed |= _card_category_ids(user_id, int(cid)) or set()
    if allowed:
        out['c_expense_entries'] = allowed
    return out


def defer_unmatched(user_id: int) -> Tuple[int, Optional[str]]:
    """
    Move every forecast on a bank-fed table dated today or earlier that
    nothing matched to tomorrow - exactly what "No, ask me tomorrow" does
    in the evening prompt, and through the same code. Returns (moved,
    earliest date any of them sat on), the latter so the totals can be
    recomputed from there.
    """
    import redis_manager
    from bucket_confirmation import resolve
    today = _user_today(user_id).isoformat()
    moved = 0
    earliest = None
    for table, allowed in fed_tables(user_id).items():
        entries = redis_manager.get_table_cache(table, user_id) or []
        due = []
        for e in entries:
            if _flag(e.get('is_bucket')) != 1:
                continue
            try:
                amount = float(e.get('amount') or 0)
                cid = int(e.get('category_id'))
            except (TypeError, ValueError):
                continue
            if amount <= 0 or (allowed is not None and cid not in allowed):
                continue
            d = _iso(e.get('date'))
            if d and d <= today:
                due.append((e.get('id'), d))
        for eid, d in due:
            ok, msg, change = resolve(user_id, table, eid, 'defer')
            if ok and change:
                moved += 1
                earliest = d if earliest is None or d < earliest else earliest
    return moved, earliest


def recalc(user_id: int, since: Optional[str], cards: bool) -> None:
    """The totals, from the first day that changed; the cards' too when one moved."""
    from app import app, _recalc_totals_remainders, _recalc_ca_daily_balance
    start = _parse(since) if since else None
    with app.app_context():
        _recalc_totals_remainders(user_id, start)
        if cards:
            _recalc_ca_daily_balance(user_id, start)


# ---------------------------------------------------------------- the modal

def _source(confidence: Optional[str]) -> Optional[str]:
    """Where a guess came from, for the row's small tag."""
    return {'memory': 'memory', 'amount': 'amount', 'record': 'record',
            'high': 'claude', 'medium': 'claude', 'low': 'claude'}.get(confidence or '')


def _choices(user_id: int, table: str, txn_date: str, credit_account_id: Optional[int]) -> List[Dict[str, Any]]:
    """
    What the modal offers for one bank row: the direction's categories (a
    card's own, for a card), each carrying the forecast it would consume
    when one sits within CANDIDATE_DAYS - so the list reads as "this might
    be that" rather than as bare names.
    """
    nearby: Dict[int, Dict[str, Any]] = {}
    for c in candidates(user_id, table, txn_date, credit_account_id):
        nearby.setdefault(c['category_id'], {'amount': c['forecast_amount'], 'date': c['forecast_date'],
                                             'original': c.get('original_amount') or c['forecast_amount']})
    out = [dict(o, category_id=o['id'], forecast=nearby.get(o['id']))
           for o in _category_options(user_id, table, credit_account_id)]
    out.sort(key=lambda o: o['name'].lower())
    return out


def pending_bank_items(user_id: int) -> List[Dict[str, Any]]:
    """
    The imported entries still waiting for the person, as the modal shows
    them, newest first. Each carries the guess and where it came from, the
    forecast it consumed when it consumed one, and the choices on offer.
    """
    import redis_manager
    from bank_redis import get_linked_transactions, _get_all_linked_accounts_raw
    linked: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for t in get_linked_transactions(user_id) or []:
        if t.get('imported_to_entry_id') and t.get('imported_entry_type'):
            linked[(str(t['imported_entry_type']), str(t['imported_to_entry_id']))] = t
    accounts = {str(a.get('account_id')): a for a in (_get_all_linked_accounts_raw(user_id) or [])}
    cards = {int(a['id']): a.get('name') or '' for a in (redis_manager.get_table_cache('credit_accounts', user_id) or [])
             if a.get('id') is not None}
    card_of = {}
    for c in redis_manager.get_table_cache('c_expense_categories', user_id) or []:
        if c.get('id') is not None:
            card_of[int(c['id'])] = int(c.get('account_id') or 0)
    items = []
    for entry_type, table in ENTRY_TABLES.items():
        if table not in PENDING_TABLES:
            continue
        names = _category_names(user_id, table)
        for e in redis_manager.get_table_cache(table, user_id) or []:
            if not _flag(e.get('pending')):
                continue
            t = linked.get((entry_type, str(e.get('id'))))
            if not t:
                continue
            acc = accounts.get(str(t.get('account_id'))) or {}
            cid = int(e['category_id']) if e.get('category_id') is not None else None
            card_id = card_of.get(cid) if table == 'c_expense_entries' else None
            snap = t.get('depleted_bucket')
            if isinstance(snap, str):
                try:
                    snap = json.loads(snap)
                except ValueError:
                    snap = None
            when = _iso(e.get('date'))
            items.append({
                'kind': 'bank',
                'entry_id': e.get('id'),
                'table': table,
                'entry_type': entry_type,
                'transaction_id': str(t.get('transaction_id')),
                'description': t.get('description') or '',
                'merchant_name': t.get('merchant_name') or '',
                'account_name': acc.get('alias') or acc.get('account_name') or '',
                'card_name': cards.get(card_id, '') if card_id else '',
                'credit_account_id': card_id,
                'date': when,
                'amount': float(e.get('amount') or 0),
                'category_id': cid,
                'category_name': names.get(cid, ''),
                'source': _source(t.get('custom_category_confidence')),
                'forecast': ({'amount': snap.get('amount'), 'date': snap.get('date')} if snap else None),
                'choices': _choices(user_id, table, when, card_id),
            })
    items.sort(key=lambda i: (i['date'] or '', i['description']), reverse=True)
    return items


def _restore_forecast(user_id: int, table: str, entry_type: str, snap: Dict[str, Any],
                      txn_amount: float, this_entry_id: str) -> Optional[Dict[str, Any]]:
    """
    Put back the forecast a guess consumed, because the person said the
    transaction was something else.

    Still there (an allowance partly spent): the amount goes back into it.
    Gone (a bill settled in full, or the forecast that became this very
    entry): re-created where it was - on its own date when that is still
    ahead, or tomorrow when it had come due, as "No, ask me tomorrow" would
    have left it. A guess consumes a category's next forecast wherever it
    sits (next month's bill, next year's renewal), and putting that back
    on tomorrow made a bill due in October ask for money next week. Its
    record gets the amount back either way. Returns the row as the page
    needs to draw it, 'added' saying whether it is new.
    """
    import redis_manager
    from redis_crud import add_entry
    from bucket_utils import restore_bucket_for_category_change
    from recurring_bucket_manager import get_bucket_table_for_entry_table
    cid = snap.get('category_id')
    if cid is None:
        return None
    bucket_table = get_bucket_table_for_entry_table(table)
    forecast_amount = float(snap.get('amount') or 0)
    origin = snap.get('original_date') or snap.get('date')

    entries = redis_manager.get_table_cache(table, user_id) or []
    live = None
    if snap.get('entry_id') is not None and str(snap['entry_id']) != str(this_entry_id):
        live = next((e for e in entries if str(e.get('id')) == str(snap['entry_id']) and _flag(e.get('is_bucket'))), None)
    if live is not None:
        back = float(live.get('amount') or 0) + txn_amount
        if forecast_amount:
            back = min(back, forecast_amount)
        live['amount'] = back
        redis_manager.set_table_cache(table, user_id, entries, mark_dirty=True)
        try:
            restore_bucket_for_category_change(bucket_table, cid, origin, txn_amount, user_id, entry_type)
        except Exception as e:
            log_warning(logger, TAG, f'user {user_id}: could not restore the record for {table}/{cid}: {e}')
        return {'table': table, 'entry_id': live.get('id'), 'category_id': int(cid), 'date': _iso(live.get('date')),
                'amount': back, 'is_bucket': 1, 'processed': 0, 'removed': False,
                'original_date': _iso(live.get('original_date')), 'added': False}

    tomorrow = (_user_today(user_id) + timedelta(days=1)).isoformat()
    back_on = max(tomorrow, _iso(snap.get('date')) or tomorrow)
    data = {'category_id': int(cid), 'date': back_on, 'original_date': origin, 'amount': forecast_amount,
            'recurring_id': snap.get('recurring_id'), 'is_bucket': 1,
            'original_amount': snap.get('original_amount') or forecast_amount,
            'processed': 0, 'auto_confirmed': 0, 'is_auto_adjustment': 0, 'pending': 0}
    if table != 'income_entries':
        data['bundle_item_id'] = None
    eid = add_entry(table, data, user_id)
    if not eid:
        return None
    try:
        restore_bucket_for_category_change(bucket_table, cid, origin, forecast_amount, user_id, entry_type)
    except Exception as e:
        log_warning(logger, TAG, f'user {user_id}: could not restore the record for {table}/{cid}: {e}')
    return {'table': table, 'entry_id': int(eid), 'category_id': int(cid), 'date': back_on,
            'amount': forecast_amount, 'is_bucket': 1, 'processed': 0, 'removed': False,
            'original_date': origin, 'original_amount': data['original_amount'], 'added': True}


def confirm(user_id: int, transaction_id: str, entry_id: int, entry_type: str,
            category_id: int) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    The person's answer for one bank row: the guessed category kept, or the
    entry moved to another.

    Moving it undoes the guess first - the forecast it consumed comes back
    (see _restore_forecast) - then the entry goes into the new category and
    consumes that one's forecast, exactly as the guess did. Either way the
    choice is remembered for the merchant. Returns (ok, message, change);
    change carries 'restored' when a forecast came back, so the page can
    draw it.
    """
    import redis_manager
    from redis_crud import upsert_category_memory
    from bank_redis import get_linked_transactions, bulk_upsert_linked_transactions
    from bucket_utils import find_next_bucket_for_category, process_manual_entry_with_bucket

    table = ENTRY_TABLES.get(entry_type)
    if table not in PENDING_TABLES:
        return False, 'Unknown entry type.', None
    entries = redis_manager.get_table_cache(table, user_id)
    if entries is None:
        # Not loaded is not a refusal. The prompt can arrive here straight
        # after a request that dropped the cache - adding a recurring entry
        # forces a flush and leaves the tables to be read back - so they are
        # read back now, as a page load would.
        _ensure_hydrated(user_id)
        entries = redis_manager.get_table_cache(table, user_id)
    if entries is None:
        return False, 'Your data is not loaded yet. Try again in a moment.', None
    target = next((e for e in entries if str(e.get('id')) == str(entry_id)), None)
    if target is None:
        return False, 'That entry is no longer there.', None
    names = _category_names(user_id, table)
    new_cid = int(category_id)
    if new_cid not in names:
        return False, 'That category does not exist.', None
    row = next((t for t in (get_linked_transactions(user_id) or [])
                if str(t.get('transaction_id')) == str(transaction_id)), None)
    old_cid = int(target['category_id']) if target.get('category_id') is not None else None
    when = _iso(target.get('date'))
    amount = float(target.get('amount') or 0)
    moved = new_cid != old_cid

    target['pending'] = 0
    target['auto_confirmed'] = 0
    target['processed'] = 1
    if moved:
        target['category_id'] = new_cid
    redis_manager.set_table_cache(table, user_id, entries, mark_dirty=True)

    restored = None
    row_update: Dict[str, Any] = {}
    if moved:
        snap = (row or {}).get('depleted_bucket')
        if isinstance(snap, str):
            try:
                snap = json.loads(snap)
            except ValueError:
                snap = None
        if snap:
            restored = _restore_forecast(user_id, table, entry_type, snap, amount, str(entry_id))
        new_snap = None
        try:
            bucket = find_next_bucket_for_category(table, new_cid, user_id)
            if bucket:
                new_snap = _snapshot(table, bucket)
        except Exception as e:
            log_warning(logger, TAG, f'user {user_id}: could not look ahead at the forecast for {table}/{new_cid}: {e}')
        try:
            process_manual_entry_with_bucket(table, new_cid, when, Decimal(str(amount)), user_id)
        except Exception as e:
            log_exception(logger, TAG, f'user {user_id}: depleting the forecast for {table}/{new_cid} failed: {e}')
        _add_mirror_payment(user_id, entry_type, new_cid, when, amount)
        row_update['depleted_bucket'] = new_snap

    direction = 'incoming' if entry_type == 'income' else 'outgoing'
    canonical = _canonical(user_id, entry_type, new_cid)
    # Uncategorized is the absence of an answer, not one to remember.
    if row and canonical and (names.get(new_cid) or '').lower() != 'uncategorized':
        for key in {row.get('description'), row.get('merchant_name')} - {None, ''}:
            try:
                upsert_category_memory(user_id, merchant_id=None, description=key,
                                       category_id=canonical, category_type=direction)
            except Exception as e:
                log_warning(logger, TAG, f'user {user_id}: could not remember the category for {key!r}: {e}')
    if row:
        row_update.update({'transaction_id': str(transaction_id), 'custom_category_suggestion': names.get(new_cid),
                           'custom_category_id': canonical, 'custom_category_type': direction})
        bulk_upsert_linked_transactions([row_update], user_id)

    try:
        recalc(user_id, when, cards=(table == 'c_expense_entries'))
    except Exception as e:
        log_exception(logger, TAG, f'user {user_id}: recalculation after confirming failed: {e}')

    change = {'table': table, 'entry_id': target.get('id'), 'category_id': new_cid, 'action': 'categorise',
              'removed': False, 'date': when, 'amount': amount, 'is_bucket': 0, 'processed': 1,
              'original_date': None, 'restored': restored}
    return True, 'Recorded.', change


# ---------------------------------------------------------------- balances

def record_balances(user_id: int, balances: List[Dict[str, Any]]) -> int:
    """
    What the bank said each linked account holds at pull time, onto the
    linked_accounts rows - the bank page shows it, and the reconcile that
    runs when the modal is completed reuses it rather than asking again.
    """
    from bank_redis import update_linked_account_fields, _get_all_linked_accounts_raw
    active = {str(a.get('account_id')): a for a in (_get_all_linked_accounts_raw(user_id) or [])
              if _flag(a.get('is_active', 1))}
    written = 0
    for b in balances or []:
        aid = str(b.get('account_id') or '')
        if aid not in active or b.get('current_balance') is None:
            continue
        fields = {'current_balance': b['current_balance']}
        if b.get('available_balance') is not None:
            fields['available_balance'] = b['available_balance']
        try:
            if update_linked_account_fields(aid, fields, user_id):
                written += 1
        except Exception as e:
            log_warning(logger, TAG, f'user {user_id}: could not record the balance of {aid}: {e}')
    return written


def feed_as_of_yesterday(user_id: int, balances: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], str, List[str]]:
    """
    The bank's balances turned into what the app should show for the end of
    the user's YESTERDAY: today's balance less whatever posted today. As of
    yesterday because today's forecasts are still open; as of the end of it
    because the bank's figure is a moment, not a day.

    Returns ({'checking', 'savings', 'cards': {credit_account_id: owed}},
    yesterday, [account ids skipped for a stale balance date]).
    """
    from bank_redis import (_get_all_linked_accounts_raw, linked_account_kind,
                            get_credit_account_for_linked_account, get_linked_transactions)
    yesterday = (_user_today(user_id) - timedelta(days=1)).isoformat()
    active = {str(a.get('account_id')): a for a in (_get_all_linked_accounts_raw(user_id) or [])
              if _flag(a.get('is_active', 1))}

    # Signed movements posted after yesterday, per account.
    moved: Dict[str, float] = {}
    for t in get_linked_transactions(user_id) or []:
        if _flag(t.get('pending')):
            continue
        d = _iso(t.get('date'))
        if not d or d <= yesterday:
            continue
        aid = str(t.get('account_id') or '')
        signed = float(t.get('amount') or 0) * (1 if t.get('transaction_type') == 'income' else -1)
        moved[aid] = moved.get(aid, 0.0) + signed

    feed: Dict[str, Any] = {'checking': None, 'savings': None, 'cards': {}}
    stale: List[str] = []
    for b in balances or []:
        aid = str(b.get('account_id') or '')
        acc = active.get(aid)
        if not acc or b.get('current_balance') is None:
            continue
        if b.get('balance_date') and b['balance_date'] < yesterday:
            stale.append(aid)
            continue
        as_of = float(b['current_balance']) - moved.get(aid, 0.0)
        kind = linked_account_kind(acc)
        if kind == 'checking':
            feed['checking'] = as_of
        elif kind == 'savings':
            feed['savings'] = as_of
        elif kind == 'credit':
            card = get_credit_account_for_linked_account(user_id, aid)
            if card and card.get('id') is not None:
                # A card's balance from the bank is what is owed, negative.
                feed['cards'][int(card['id'])] = abs(as_of)
    return feed, yesterday, stale


def reconcile(user_id: int, balances: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Bring the app's balances to the bank's, as of the user's yesterday, with
    the corrections auto_balance writes for a typed figure. Nothing is
    confirmed on the way: a forecast the feed answers for has been matched
    or moved on already, and one it does not answer for is the evening
    prompt's. None when there was nothing to compare.
    """
    import auto_balance
    feed, yesterday, stale = feed_as_of_yesterday(user_id, balances)
    if feed['checking'] is None and feed['savings'] is None and not feed['cards']:
        if stale:
            log_info(logger, TAG, f'user {user_id}: balances not reconciled, the bank\'s figures are older than {yesterday}')
        return None
    from app import app
    with app.app_context():
        result = auto_balance.reconcile_to_feed(user_id, checking=feed['checking'], savings=feed['savings'],
                                                cards=feed['cards'], on_date=_parse(yesterday),
                                                confirm_buckets=False)
    result['as_of'] = yesterday
    result['stale'] = stale
    return result


def reconcile_summary(result: Optional[Dict[str, Any]]) -> str:
    """One clause for a toast."""
    if not result:
        return ''
    parts = []
    chk = result.get('checking') or {}
    if chk.get('error'):
        return f"Balances not matched: {chk['error']}"
    for label, r in (('checking', chk), ('savings', result.get('savings') or {})):
        if r and r.get('entry_written'):
            parts.append(f"{label} corrected by {abs(float(r.get('difference') or 0)):.2f}")
    cards = [c for c in (result.get('cards') or []) if c.get('entry_written')]
    if cards:
        parts.append(f"{len(cards)} card{'s' if len(cards) != 1 else ''} corrected")
    if not parts:
        return 'Balances match the bank.'
    return 'Balances matched to the bank: ' + ', '.join(parts) + '.'


# ------------------------------------------------------------- notification

def _count_pending_db(user_id: int) -> int:
    queries = (
        "SELECT COUNT(*) FROM income_entries e JOIN income_categories c ON c.id = e.category_id "
        " WHERE c.user_id = %s AND e.pending = 1",
        "SELECT COUNT(*) FROM expense_entries e JOIN expense_categories c ON c.id = e.category_id "
        " WHERE c.user_id = %s AND e.pending = 1",
        "SELECT COUNT(*) FROM c_expense_entries e JOIN c_expense_categories c ON c.id = e.category_id "
        "  JOIN credit_accounts a ON a.id = c.account_id WHERE a.user_id = %s AND e.pending = 1",
    )
    total = 0
    try:
        with get_db_pool().get_cursor() as cursor:
            for sql in queries:
                cursor.execute(sql, (user_id,))
                row = cursor.fetchone()
                if row:
                    total += int((row[0] if not isinstance(row, dict) else list(row.values())[0]) or 0)
    except Exception as e:
        log_exception(logger, TAG, f'user {user_id}: could not count pending entries: {e}')
    return total


def count_pending(user_id: int) -> int:
    """How many imported entries still wait for the person - Redis when hydrated, MySQL otherwise."""
    import redis_manager
    if not redis_manager.is_user_hydrated(user_id):
        return _count_pending_db(user_id)
    total = 0
    for table in PENDING_TABLES:
        rows = redis_manager.get_table_cache(table, user_id)
        if rows is None:
            return _count_pending_db(user_id)
        total += sum(1 for e in rows if _flag(e.get('pending')))
    return total


def _forget_notifications_cache(user_id: int) -> None:
    """Every notification writer must do this, or the badge counts one the page does not show."""
    import redis_manager
    try:
        if redis_manager._redis_client:
            redis_manager._redis_client.delete(f'notifications:v1:{user_id}')
    except Exception:
        pass


def _delete_notification(user_id: int) -> int:
    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            cursor.execute("DELETE FROM notifications WHERE user_id = %s AND type = %s", (user_id, NOTIFICATION_TYPE))
            n = cursor.rowcount
    except Exception as e:
        log_error(logger, TAG, f'user {user_id}: could not clear the pending notification: {e}')
        return 0
    if n:
        _forget_notifications_cache(user_id)
    return n


def notify(user_id: int) -> int:
    """
    One notification, replaced rather than added to, saying how many
    imported entries wait. Found again by notifications.type, not by its
    wording. The link is the dashboard: the modal opens there by itself,
    and the push deep-links wherever the message's href points.
    """
    total = count_pending(user_id)
    _delete_notification(user_id)
    if total <= 0:
        return 0
    from app import add_notification
    one = total == 1
    message = (f'{total} bank transaction{"" if one else "s"} {"was" if one else "were"} added with a guessed '
               f'category. <a href="/dashboard">Check {"it" if one else "them"}</a>.')
    add_notification(user_id, message, kind=NOTIFICATION_TYPE, notification_type=NOTIFICATION_TYPE)
    return total


def clear_notification_if_none(user_id: int) -> bool:
    """Drop the notification once nothing waits. True when one was dropped."""
    if count_pending(user_id) > 0:
        return False
    return _delete_notification(user_id) > 0


# -------------------------------------------------------------------- pull

def pull(user_id: int, source: str = 'manual') -> Dict[str, Any]:
    """
    One pull for one user: fetch, normalize, reconcile with what is stored,
    store; then the posted rows into the budget, unmatched forecasts moved
    on, the totals recomputed, the notification refreshed. Returns what
    happened, for the button's toast and the daily pull's ledger:

        {'ok', 'error', 'message', 'fetched', 'new', 'updated', 'matched',
         'removed', 'pending', 'imported', 'skipped', 'deferred',
         'window_start', 'balances', 'errors'}

    'error' is a short stable code (the provider's, or 'no_accounts');
    'message' is for the person. A provider failure has already been
    recorded in the request ledger by the provider itself.
    """
    from providers import get_bank_provider
    from providers.simplefin import SimpleFINError

    result: Dict[str, Any] = {
        'ok': False, 'error': None, 'message': '', 'source': source,
        'fetched': 0, 'new': 0, 'updated': 0, 'matched': 0, 'removed': 0, 'pending': 0,
        'imported': 0, 'skipped': 0, 'deferred': 0, 'reconciled': None,
        'window_start': None, 'balances': [], 'errors': [],
    }
    provider = get_bank_provider()
    if getattr(provider, 'name', 'null') != 'simplefin':
        result.update(error='no_provider', message='No bank provider is configured.')
        return result

    status = provider.status(user_id)
    if not status.get('connected'):
        result.update(error='no_credentials', message='No bank connection yet.')
        return result
    if status.get('needs_new_token'):
        result.update(error=status.get('last_error_code') or 'gen.auth',
                      message='SimpleFIN no longer accepts this connection. Paste a new Setup Token first.')
        return result

    accounts = importable_accounts(user_id)
    if not accounts:
        result.update(error='no_accounts', message='No checking or credit-card account is set to import.')
        return result

    try:
        _ensure_hydrated(user_id)
        from bank_redis import get_linked_transactions, update_last_linked_transaction_date
        import redis_manager

        stored = list(get_linked_transactions(user_id) or [])
        today = date.today()
        window_start, floors = plan_window(accounts, stored, link_moments(user_id), today)
        result['window_start'] = window_start

        try:
            fetched = provider.fetch_transactions_and_balances(user_id, start=window_start,
                                                               end=today.strftime('%Y-%m-%d'))
        except SimpleFINError as e:
            log_warning(logger, TAG, f'user {user_id}: pull refused: {e.code}')
            result.update(error=e.code, message=e.message)
            return result

        result['balances'] = fetched.get('accounts') or []
        result['errors'] = fetched.get('errors') or []
        raw = fetched.get('transactions') or []
        result['fetched'] = len(raw)

        rows = normalize(raw, accounts, floors)
        upserts, deletes, counts = reconcile_pending(stored, rows, window_start, today)
        store(user_id, upserts, deletes)
        result.update(counts)
        result['pending'] = sum(1 for r in rows if r['pending'])
        update_last_linked_transaction_date(user_id)

        # Into the budget. The store is flushed first so an entry created
        # below can never outlive the transaction row it points at.
        redis_manager.flush_dirty_tables_for_user(user_id)
        made = create_entries(user_id)
        moved, moved_from = defer_unmatched(user_id)
        result.update(imported=made['imported'], skipped=made['skipped'], deferred=moved)
        if made['imported'] or moved:
            since = min(d for d in (made['earliest'], moved_from) if d) if (made['earliest'] or moved_from) else None
            try:
                # Cards too when a forecast moved: a card's bucket deferred
                # off yesterday changes yesterday's card balance, and the
                # reconcile below reads that balance. Left stale, it was
                # measured against a figure that still held the bucket, and
                # the difference was written as a correction.
                recalc(user_id, since, made['cards'] or bool(moved))
            except Exception as e:
                log_exception(logger, TAG, f'user {user_id}: recalculation after the pull failed: {e}')
        if made['imported']:
            try:
                notify(user_id)
            except Exception as e:
                log_warning(logger, TAG, f'user {user_id}: notification after the pull: {e}')

        # The bank's balances: recorded, then matched. Every pull, so the
        # remainders are right the moment the bank speaks.
        record_balances(user_id, result['balances'])
        try:
            result['reconciled'] = reconcile(user_id, result['balances'])
        except Exception as e:
            log_exception(logger, TAG, f'user {user_id}: reconcile after the pull failed: {e}')
            result['reconciled'] = None

        try:
            redis_manager.flush_dirty_tables_for_user(user_id)
        except Exception as e:
            log_warning(logger, TAG, f'user {user_id}: post-pull flush: {e}')
        try:
            from app import _bump_data_version
            _bump_data_version(user_id)
        except Exception:
            pass

        result['ok'] = True
        result['message'] = _summary(result)
        log_info(logger, TAG, f'user {user_id} ({source}): window from {window_start}, '
                              f'{result["fetched"]} fetched, {counts["new"]} new, {counts["updated"]} updated, '
                              f'{counts["matched"]} matched, {counts["removed"]} removed, '
                              f'{result["pending"]} pending; {made["imported"]} into the budget, '
                              f'{made["skipped"]} waiting, {moved} forecast(s) moved on')
        return result
    except Exception as e:
        log_exception(logger, TAG, f'user {user_id}: pull failed: {e}')
        result.update(error='internal', message='The pull failed on this server. Try again later.')
        return result


def _summary(result: Dict[str, Any]) -> str:
    """One line for a toast."""
    parts = []
    if result['new']:
        parts.append(f"{result['new']} new transaction{'s' if result['new'] != 1 else ''}")
    if result['imported']:
        parts.append(f"{result['imported']} added to the budget")
    if result['updated']:
        parts.append(f"{result['updated']} updated")
    if result['removed']:
        parts.append(f"{result['removed']} pending withdrawn")
    if not parts:
        return 'Nothing new from the bank.'
    return ', '.join(parts) + '.'
