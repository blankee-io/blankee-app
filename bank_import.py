"""
Transaction import from the bank feed.

What the previous vendor's webhook did in a thousand lines of app.py, done
as a pull: ask the provider for the transactions since the last time,
store them, and hand the posted ones to the budget. This module owns the
first two steps and the rules around them; the budget side (entries,
guesses, forecasts) is added on top and lives here too, so that the whole
of "a bank spoke" is one file rather than one file and a corner of app.py.

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

Redis-first, like the rest of the app: linked_transactions is written
through bank_redis and the flush persists it. The user is hydrated first
when they are not - background work reads through the same cache the pages
do, and the cache only exists for a hydrated user.

The app is imported inside functions, never at module level: app imports
this module's neighbours, and auto_balance.py:1031 is the precedent for
breaking that cycle at call time.
"""

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
    out = []
    for t in fetched:
        aid = str(t.get('account_ref') or '')
        if aid not in accounts:
            continue
        txn_date = _iso(t.get('date'))
        if not txn_date or not t.get('provider_txn_id'):
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
        amount = t.get('amount')
        if amount is None:
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
            'transaction_type': t.get('transaction_type') or ('expense' if float(amount) < 0 else 'income'),
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
    if redis_manager.is_user_hydrated(user_id):
        return
    log_info(logger, TAG, f'user {user_id}: hydrating before the pull')
    redis_manager._hydrate_user_data(user_id)


# -------------------------------------------------------------------- pull

def pull(user_id: int, source: str = 'manual') -> Dict[str, Any]:
    """
    One pull for one user: fetch, normalize, reconcile with what is stored,
    store. Returns what happened, for the button's toast and the daily
    pull's ledger:

        {'ok', 'error', 'message', 'fetched', 'new', 'updated', 'matched',
         'removed', 'pending', 'window_start', 'balances', 'errors'}

    'error' is a short stable code (the provider's, or 'no_accounts');
    'message' is for the person. A provider failure has already been
    recorded in the request ledger by the provider itself.
    """
    from providers import get_bank_provider
    from providers.simplefin import SimpleFINError

    result: Dict[str, Any] = {
        'ok': False, 'error': None, 'message': '', 'source': source,
        'fetched': 0, 'new': 0, 'updated': 0, 'matched': 0, 'removed': 0, 'pending': 0,
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
                              f'{result["pending"]} pending')
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
    if result['updated']:
        parts.append(f"{result['updated']} updated")
    if result['removed']:
        parts.append(f"{result['removed']} pending withdrawn")
    if not parts:
        return 'Nothing new from the bank.'
    return ', '.join(parts) + '.'
