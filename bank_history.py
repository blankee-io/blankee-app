"""
The last three months of bank transactions, kept aside for Suggested recurring.

The import starts at the moment each account was linked and never goes
earlier (bank_import's docstring says why: the balance at link time already
holds everything before it). That leaves a newly linked bank with a few days
of history, and a monthly bill needs three months to be recognised. So the
history is read separately and kept here, for spotting patterns only - none
of it ever becomes an entry or a linked_transactions row.

What came before the link never changes, so it is read once: in the
background, the first time Suggested recurring is opened, and again only
when an account is added or the key has expired (TTL_SECONDS - by then the
stored feed covers the same months by itself). The daily pull is untouched.

One read is two requests of SPAN_DAYS each. SimpleFIN allows 90 days per
request but warns past 45, and a warning is recorded as the request's
outcome - the bank settings would show the connection as failing.

Stored in Redis only, under bank_history:v1:{user_id}: it is a copy of what
the bank holds and can be read again, so it has no MySQL table and no flush.
"""
import json
import threading
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from log_config import get_logger, log_info, log_warning, log_exception

logger = get_logger(__name__)
TAG = 'BANK_HISTORY'

# Two requests: 87 days in windows of 43. The Bridge counts a window to the
# end of its last day, so 43 asks for 44 - inside the 45 it recommends.
HISTORY_DAYS = 87
SPAN_DAYS = 43
TTL_SECONDS = 120 * 86400
LOCK_SECONDS = 600


def _key(user_id: int) -> str:
    return f'bank_history:v1:{user_id}'


def _lock_key(user_id: int) -> str:
    return f'bank_history_fetching:{user_id}'


def _redis():
    # Read at call time: redis_manager sets its client after import.
    from redis_manager import _redis_client
    return _redis_client


def windows(today: date):
    """[(start, end), ...] covering HISTORY_DAYS, none longer than SPAN_DAYS."""
    out, end = [], today
    first = today - timedelta(days=HISTORY_DAYS)
    while end >= first:
        start = max(first, end - timedelta(days=SPAN_DAYS))
        out.append((start.isoformat(), end.isoformat()))
        end = start - timedelta(days=1)
    return out


def read(user_id: int) -> Optional[Dict[str, Any]]:
    """{'fetched_at': epoch, 'rows': [...]} or None when nothing is kept."""
    try:
        raw = _redis().get(_key(user_id))
        return json.loads(raw) if raw else None
    except Exception as e:
        log_warning(logger, TAG, f'user {user_id}: could not read the kept history: {e}')
        return None


def rows(user_id: int) -> List[Dict[str, Any]]:
    kept = read(user_id)
    return list((kept or {}).get('rows') or [])


def needed(user_id: int, accounts) -> bool:
    """True when nothing is kept, or an account has been added since it was read."""
    kept = read(user_id)
    if not kept:
        return True
    if kept.get('failed'):
        return False
    return bool(set(str(a) for a in accounts) - set(kept.get('accounts') or []))


def fetching(user_id: int) -> bool:
    try:
        return bool(_redis().exists(_lock_key(user_id)))
    except Exception:
        return False


def keep(user_id: int, fetched: List[Dict[str, Any]], accounts: Dict[str, Dict[str, Any]]) -> int:
    """
    Store the posted rows of a provider answer, shaped as linked_transactions
    rows by bank_import.normalize - every account the import wants, with no
    link-date floor. Returns how many were kept.
    """
    from bank_import import normalize
    kept = [r for r in normalize(fetched, accounts, {}) if not r.get('pending')]
    body = {'fetched_at': time.time(), 'rows': kept, 'accounts': sorted(str(a) for a in accounts)}
    try:
        _redis().set(_key(user_id), json.dumps(body), ex=TTL_SECONDS)
    except Exception as e:
        log_exception(logger, TAG, f'user {user_id}: could not keep the history: {e}')
        return 0
    log_info(logger, TAG, f'user {user_id}: kept {len(kept)} posted transactions from the last {HISTORY_DAYS} days')
    return len(kept)


def mark_failed(user_id: int) -> None:
    """Remember a failed read for a few hours, so the page stops waiting and
    does not ask the bank again on every visit."""
    body = {'fetched_at': time.time(), 'rows': [], 'failed': True}
    try:
        _redis().set(_key(user_id), json.dumps(body), ex=3 * 3600)
    except Exception:
        pass


def fetch_now(user_id: int) -> bool:
    """Read the window, in SPAN_DAYS requests, and keep it. True when it worked."""
    from bank_import import importable_accounts
    from providers import get_bank_provider
    provider = get_bank_provider()
    if getattr(provider, 'name', 'null') != 'simplefin':
        return False
    accounts = importable_accounts(user_id)
    if not accounts:
        return False
    fetched = []
    for start, end in windows(date.today()):
        try:
            answer = provider.fetch_transactions_and_balances(user_id, start=start, end=end)
        except Exception as e:
            log_warning(logger, TAG, f'user {user_id}: history read refused: {getattr(e, "code", e)}')
            mark_failed(user_id)
            return False
        fetched.extend(answer.get('transactions') or [])
    keep(user_id, fetched, accounts)
    return True


def fetch_in_background(user_id: int, app=None) -> bool:
    """
    Start fetch_now in a thread unless one is already running. Bumps the
    data version when it lands - the waiting page reloads on it - on success
    and on failure alike. Returns True when a fetch was started.
    """
    try:
        if not _redis().set(_lock_key(user_id), '1', nx=True, ex=LOCK_SECONDS):
            return False
    except Exception:
        return False

    def run():
        try:
            if app is not None:
                with app.app_context():
                    fetch_now(user_id)
            else:
                fetch_now(user_id)
        except Exception as e:
            log_exception(logger, TAG, f'user {user_id}: history fetch failed: {e}')
            mark_failed(user_id)
        finally:
            try:
                _redis().delete(_lock_key(user_id))
            except Exception:
                pass
            try:
                from app import _bump_data_version
                _bump_data_version(user_id)
            except Exception as e:
                log_warning(logger, TAG, f'user {user_id}: could not bump the data version: {e}')

    threading.Thread(target=run, name=f'bank-history-{user_id}', daemon=True).start()
    return True
