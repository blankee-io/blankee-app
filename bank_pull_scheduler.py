"""
Pulls each user's bank once a day, at PULL_HOUR:PULL_MINUTE in their own
timezone.

The shape of bucket_prompt_scheduler, and for the same reasons - read its
docstring for the two that matter: it must not assume it is the only one
running (the Docker image runs two workers, and each claims a user's day
with an INSERT that either wins or does nothing), and a user whose timezone
is unknown is skipped rather than guessed at.

Its own thread rather than a job on the bucket prompt's walk. A pull is a
network request that the Bridge can take minutes to answer; the 20:00 walk
has to stay quick, and a slow bank should not delay anyone's prompt.

Ten to midnight, local: the day is over, so what posted today comes in
while it is still today, today's forecasts that nothing matched are moved
on before the day closes, and the balances are compared as of yesterday,
when nothing was still open. (It used to run at six in the morning; the
overnight postings the bank makes after midnight now arrive a day later,
and the day's own transactions a day earlier.)
"""

import threading
from datetime import datetime, timezone as _timezone

from db_connections import get_db_pool
from log_config import get_logger, log_info, log_warning, log_exception
from bucket_prompt_scheduler import ZoneInfo, _is_due

logger = get_logger(__name__)

TAG = 'BANK_PULL'

_thread = None
_shutdown = threading.Event()

# The local time the pull runs at: 06:00, so the night's postings are in
# the budget before the day starts. It was 23:50 for a while, to catch the
# day's own postings; in practice the bank posts overnight, and a morning pull
# sees the same rows a day earlier from the person's point of view.
PULL_HOUR = 6
PULL_MINUTE = 0

# How often to look, and how wide the window is - wider than the interval,
# so a slow pass cannot step over anyone; the daily claim stops the overlap
# from pulling twice.
CHECK_INTERVAL = 300
WINDOW_MINUTES = 10


def start():
    """Start the scheduler thread if it is not already running."""
    global _thread
    if ZoneInfo is None:
        log_warning(logger, TAG, "zoneinfo unavailable; the daily bank pull is disabled")
        return
    if _thread is not None and _thread.is_alive():
        return
    _shutdown.clear()
    _thread = threading.Thread(target=_worker, daemon=True, name="BankPullScheduler")
    _thread.start()
    log_info(logger, TAG, "Bank pull scheduler started")


def stop():
    _shutdown.set()


def _worker():
    while not _shutdown.is_set():
        try:
            run_once()
        except Exception as e:
            # One bad pass must not take the thread with it: a user gets one
            # pull a day, and a crash here is a day without one for everybody.
            log_exception(logger, TAG, f"Scheduler pass failed: {e}")
        _shutdown.wait(CHECK_INTERVAL)


def run_once(now_utc=None):
    """
    One pass. Returns how many pulls were made.

    Separate from the loop so it can be called directly in testing, with the
    instant it should think it is.
    """
    now_utc = now_utc or datetime.now(_timezone.utc)
    pulled = 0

    try:
        with get_db_pool().get_cursor() as cursor:
            cursor.execute(
                "SELECT id, timezone FROM users "
                " WHERE timezone IS NOT NULL AND timezone <> ''")
            users = cursor.fetchall() or []
    except Exception as e:
        log_exception(logger, TAG, f"Could not list users: {e}")
        return 0

    for row in users:
        user_id = row[0] if not isinstance(row, dict) else row['id']
        tz_name = row[1] if not isinstance(row, dict) else row['timezone']

        due, local_date = _is_due(tz_name, now_utc, hour=PULL_HOUR, window=WINDOW_MINUTES,
                                  minute=PULL_MINUTE)
        if not due:
            continue
        # Nothing to pull for: no connection, no accounts chosen, a token the
        # bank refuses, or the day's requests spent. Checked before the claim,
        # so the day is not consumed - connect at noon and tomorrow's pull
        # comes round normally.
        if not _wanted(user_id):
            log_info(logger, TAG, f"User {user_id} is due but has nothing to pull for today")
            continue
        if not _claim(user_id, local_date):
            # Said out loud, because a silent skip reads as a scheduler that
            # never woke. The night the pull moved from 06:00 to 23:50, the
            # morning run had already claimed that date, so the evening one
            # was skipped without a word and looked like a missed pull.
            log_info(logger, TAG, f"User {user_id}: {local_date} already pulled or being pulled; skipping")
            continue

        import bank_import
        try:
            result = bank_import.pull(user_id, 'daily')
        except Exception as e:
            log_exception(logger, TAG, f"Pull failed for user {user_id}: {e}")
            result = {'ok': False, 'error': 'exception', 'message': str(e)}
        _record_outcome(user_id, local_date, result)
        if result.get('ok'):
            pulled += 1
            log_info(logger, TAG, f"Pulled for user {user_id}: {result.get('message')}")
        else:
            log_warning(logger, TAG, f"Pull for user {user_id} did not complete: "
                                     f"{result.get('error')}: {result.get('message')}")

    return pulled


def _wanted(user_id):
    """Whether a pull for this user would do anything today."""
    try:
        from providers import get_bank_provider
        from providers.simplefin import DAILY_SOFT_CEILING
        provider = get_bank_provider()
        if getattr(provider, 'name', '') != 'simplefin':
            return False
        st = provider.status(user_id)
        return bool(st.get('connected') and st.get('accounts_chosen')
                    and not st.get('needs_new_token')
                    and int(st.get('pulls_today') or 0) < DAILY_SOFT_CEILING)
    except Exception as e:
        log_warning(logger, TAG, f"Could not read the bank status for user {user_id}: {e}")
        return False


def _claim(user_id, local_date):
    """
    Claim this user's day, atomically. INSERT IGNORE against the UNIQUE
    (user_id, pull_date) key: one caller gets rowcount 1, the rest get 0.
    commit=True, or the claim is no claim at all.
    """
    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT IGNORE INTO bank_pulls (user_id, pull_date) VALUES (%s, %s)",
                (user_id, local_date.isoformat()))
            return cursor.rowcount == 1
    except Exception as e:
        log_exception(logger, TAG, f"Claim failed for user {user_id}: {e}")
        return False


def _record_outcome(user_id, local_date, result):
    """What the pull did, on the claim row, for diagnosing a quiet morning."""
    reconciled = result.get('reconciled') or {}
    matched = bool(reconciled) and not (reconciled.get('checking') or {}).get('error')
    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE bank_pulls SET finished_at = NOW(), ok = %s, fetched = %s, imported = %s, "
                " reconciled = %s, error_code = %s, error_msg = %s "
                " WHERE user_id = %s AND pull_date = %s",
                (1 if result.get('ok') else 0, int(result.get('fetched') or 0), int(result.get('imported') or 0),
                 1 if matched else 0, (result.get('error') or None) if not result.get('ok') else None,
                 (result.get('message') or '')[:500] if not result.get('ok') else None,
                 user_id, local_date.isoformat()))
    except Exception as e:
        log_warning(logger, TAG, f"Could not record the pull outcome for user {user_id}: {e}")
