"""
Raises the "entries to confirm" reminder, on each user's own cadence and in
their own timezone.

It used to raise two: a fixed 20:00 prompt about forecast entries, and a
reminder on a chosen cadence about the balance. They were the same evening's
question asked twice, so the fixed one is gone and the cadence carries both -
entries waiting to be confirmed, and a balance to check. The claim table
(bucket_prompts) and _is_due stay: the bank pull scheduler borrows the latter.

A daemon thread, in the shape of the flush and dehydration workers it sits
beside: wake on an interval, do a little work, exit promptly on shutdown.

Two things about it are not obvious and are worth stating before the code.

**It must not assume it is the only one running.** The Debian install serves with
one mod_wsgi daemon process, but the Docker image runs `gunicorn --workers 2`, so
this thread exists in two processes and both wake at the same time. Each claims a
user's turn with auto_balance.claim_due - a conditional UPDATE that advances the
cadence, which exactly one caller wins; only the winner sends anything. A
check-then-act - "is this user due?" followed by a write - races between the
two, and the symptom is a duplicate push and email, once a day, only in Docker.

**A missing timezone is a reason to stay quiet, not to guess.** A user whose zone
we do not know is skipped. Defaulting to the server's zone would fire the prompt
at the wrong hour - lunchtime for someone far enough east - and a prompt at the
wrong time is worse than no prompt, because the user cannot tell it is wrong.
"""

import threading
from datetime import datetime, timezone as _timezone

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception

import redis_manager
from db_connections import get_db_pool
from log_config import get_logger, log_info, log_error, log_warning, log_exception

logger = get_logger(__name__)

_thread = None
_shutdown = threading.Event()

# The defaults _is_due answers with. Nothing here uses them any more - the
# reminder runs at the hour each user chose - but the bank pull scheduler
# passes its own hour and window through the same function.
PROMPT_HOUR = 20
WINDOW_MINUTES = 30

# How often to look. Five minutes is fine: the reminder fires on the first
# pass after the user's hour, and claim_due stops a second pass repeating it.
CHECK_INTERVAL = 300

# The type the fixed evening prompt used to write to the notifications table.
# Kept so clear_prompt can still remove any such row an older release left.
NOTIFICATION_TYPE = 'bucket_prompt'


def start():
    """Start the scheduler thread if it is not already running."""
    global _thread
    if ZoneInfo is None:
        log_warning(logger, 'BUCKET_PROMPT',
                    "zoneinfo unavailable; the entries-to-confirm reminder is disabled")
        return
    if _thread is not None and _thread.is_alive():
        return
    _shutdown.clear()
    _thread = threading.Thread(target=_worker, daemon=True, name="BucketPromptScheduler")
    _thread.start()
    log_info(logger, 'BUCKET_PROMPT', "Bucket prompt scheduler started")


def stop():
    _shutdown.set()


def _worker():
    while not _shutdown.is_set():
        try:
            run_once()
        except Exception as e:
            # Never let one bad pass kill the thread - it only gets one chance a
            # day per user, so a crash here is a whole day of silence.
            log_exception(logger, 'BUCKET_PROMPT', f"Scheduler pass failed: {e}")
        _shutdown.wait(CHECK_INTERVAL)


def _is_due(tz_name, now_utc, hour=PROMPT_HOUR, window=WINDOW_MINUTES, minute=0):
    """
    (due, local_date) for a user in `tz_name` right now: local time is
    within `window` minutes from hour:minute.

    hour, minute and window are parameters so the bank pull scheduler,
    which runs the same walk at a different time, asks the same question
    the same way.
    """
    try:
        local = now_utc.astimezone(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        # A zone the browser reported that this machine does not know. Log once
        # per pass rather than silently dropping the user forever.
        log_warning(logger, 'BUCKET_PROMPT', f"Unknown timezone {tz_name!r}; skipping user")
        return False, None
    if local.hour != hour:
        return False, local.date()
    return minute <= local.minute < minute + window, local.date()


def run_once(now_utc=None):
    """
    One pass. Returns how many prompts were raised.

    Separated from the loop so it can be called directly in testing without
    waiting out an interval.
    """
    now_utc = now_utc or datetime.now(_timezone.utc)
    raised = 0

    try:
        with get_db_pool().get_cursor() as cursor:
            cursor.execute(
                "SELECT id, timezone FROM users "
                " WHERE timezone IS NOT NULL AND timezone <> ''")
            users = cursor.fetchall() or []
    except Exception as e:
        log_exception(logger, 'BUCKET_PROMPT', f"Could not list users: {e}")
        return 0

    for row in users:
        user_id = row[0] if not isinstance(row, dict) else row['id']
        tz_name = row[1] if not isinstance(row, dict) else row['timezone']

        # The user's own wall clock: the time of day is their choice.
        local_now = _local_now(tz_name, now_utc)
        if local_now is None:
            continue
        try:
            if _raise_balance_prompt(user_id, local_now):
                raised += 1
        except Exception as e:
            log_exception(logger, 'AUTOBALANCE',
                          f"Failed to raise the reminder for user {user_id}: {e}")

    return raised


def _local_now(tz_name, now_utc):
    """The user's own wall clock, or None if their zone is unknown here."""
    try:
        return now_utc.astimezone(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return None


def _raise_balance_prompt(user_id, local_now):
    """
    The "entries to confirm" reminder, when the user's cadence says so:
    entries waiting to be confirmed, a balance to check, or both.

    No notifications row. This is a nudge to open the app; the modals are
    driven by what is actually outstanding (the entries list, and
    autobalance_settings.pending_date for the balance) - so it cannot leave
    a stale line in the list after the user has dealt with it.

    Email as well as push, because the point is to reach someone who is not
    looking at the app.
    """
    import auto_balance
    import bucket_confirmation

    local_date = local_now.date()
    settings = auto_balance.get_settings(user_id)
    if not settings or not settings.get('enabled') or not settings.get('next_due'):
        return False

    # Not yet today's turn, or today's turn but not yet the hour they chose.
    # A due date already in the past fires on this pass whatever the time: the
    # user has been waiting, and holding it back until their hour comes round
    # again would add another day to that.
    due_date = settings['next_due']
    if hasattr(due_date, 'date'):
        due_date = due_date.date()
    if local_date < due_date:
        return False
    if local_date == due_date and local_now.time() < auto_balance.notify_at(settings):
        return False

    # What there is to ask about. Counted from MySQL, not the cache: at this
    # hour the user is almost certainly not hydrated, and a cached read would
    # say zero for exactly the people who most need reminding. The bank's
    # rows count too - they wait in the same modal.
    pending = bucket_confirmation.count_pending_from_db(user_id, on_date=local_date)
    try:
        import bank_import
        pending += bank_import._count_pending_db(user_id)
    except Exception as e:
        log_warning(logger, 'AUTOBALANCE', f"Could not count the bank's rows for user {user_id}: {e}")
    balance = auto_balance.anything_to_reconcile(user_id, local_date)

    # Nothing to ask about means nothing to send. Checked before the claim so
    # the day is not consumed - if something turns up tomorrow, the reminder
    # should come round normally rather than having been silently used up.
    if not pending and not balance:
        return False

    if not auto_balance.claim_due(user_id, local_date):
        return False
    # claim_due set pending_date, which the dashboard's balance modal reads -
    # an open page should see the prompt without waiting for a reload.
    try:
        from app import _bump_data_version
        _bump_data_version(user_id)
    except Exception:
        pass

    if pending and balance:
        body = (f"{pending} {'entry' if pending == 1 else 'entries'} to confirm, "
                f"then your balance to check.")
    elif pending:
        body = f"{pending} {'entry' if pending == 1 else 'entries'} waiting to be confirmed."
    else:
        body = "Time to check your balance."

    _push_balance(user_id, body, 'bucket_prompt' if pending else 'autobalance')
    _email_balance(user_id, body)
    log_info(logger, 'AUTOBALANCE', f"Reminder raised for user {user_id}: {body}")
    return True


def _push_balance(user_id, body, action='autobalance'):
    """Best-effort APNs nudge. A failure must not lose the in-app prompt."""
    try:
        from push_notifications import push_to_user
        return push_to_user(user_id, body, action=action) > 0
    except Exception as e:
        log_warning(logger, 'AUTOBALANCE',
                    f"Could not push to user {user_id}: {e}")
        return False


def _email_balance(user_id, body):
    """
    Best-effort email.

    Hands off to send_notification_email_for_user rather than checking the
    opt-in and choosing a recipient here: that helper exists precisely because
    those two rules were duplicated in app.py and bucket_utils.py once already,
    and the recipient rule is the one most likely to change.
    """
    try:
        with get_db_pool().get_cursor() as cursor:
            cursor.execute(
                "SELECT id, email, first_name, email_notifications, "
                "       email_notify_disabled "
                "  FROM users WHERE id = %s", (user_id,))
            row = cursor.fetchone()
        if not row:
            return False
        user = row if isinstance(row, dict) else {
            'id': row[0], 'email': row[1], 'first_name': row[2],
            'email_notifications': row[3], 'email_notify_disabled': row[4]}

        from email_utils import send_notification_email_for_user
        # The time printed in the email is the reader's, not the server's.
        import auto_balance
        return send_notification_email_for_user(
            user, body, auto_balance._user_now(user_id),
            kind='entries_to_confirm')
    except Exception as e:
        log_warning(logger, 'AUTOBALANCE',
                    f"Could not email user {user_id}: {e}")
        return False


def clear_prompt(user_id):
    """
    Remove the prompt notification, because there is nothing left to confirm.

    Raised once an evening with a count, it goes stale the moment the user
    answers the last one: a notification reading "you have 12 entries to
    confirm" when there are none is worse than a duplicate, and tapping it opens
    an empty prompt.

    Only the empty case is handled here. Rewriting the number on every single
    answer would mean a write per tap, and the count is refreshed by the next
    evening run anyway - whereas zero is the one value the user can see is wrong.

    Returns how many rows went, so the caller can log or ignore it.
    """
    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            cursor.execute(
                "DELETE FROM notifications WHERE user_id = %s AND type = %s",
                (user_id, NOTIFICATION_TYPE))
            removed = cursor.rowcount
    except Exception as e:
        log_exception(logger, 'BUCKET_PROMPT',
                      f"Could not clear the prompt notification for user {user_id}: {e}")
        return 0

    if removed:
        # Same reason as when one is raised: the list is cached, and this wrote
        # straight past it.
        try:
            if redis_manager._redis_client:
                redis_manager._redis_client.delete(f"notifications:v1:{user_id}")
        except Exception as cache_err:
            log_warning(logger, 'BUCKET_PROMPT',
                        f"Could not clear the notifications cache for user {user_id}: {cache_err}")
    return removed
