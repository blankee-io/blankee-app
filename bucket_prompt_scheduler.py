"""
Raises the end-of-day bucket prompt at 20:00 in each user's own timezone.

A daemon thread, in the shape of the flush and dehydration workers it sits
beside: wake on an interval, do a little work, exit promptly on shutdown.

Two things about it are not obvious and are worth stating before the code.

**It must not assume it is the only one running.** The Debian install serves with
one mod_wsgi daemon process, but the Docker image runs `gunicorn --workers 2`, so
this thread exists in two processes and both wake at the same time. Each claims a
user's evening with an INSERT that either wins or does nothing; only the winner
sends anything. A check-then-act - "has this user been prompted today?" followed
by a write - races between the two, and the symptom is a duplicate notification
and a duplicate push, once a day, only in Docker.

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

# The local hour at which the prompt goes out.
PROMPT_HOUR = 20

# How often to look. Five minutes is fine: the window below is wider than the
# interval, so a user cannot be stepped over.
CHECK_INTERVAL = 300

# A user is due when their local time is between PROMPT_HOUR:00 and this many
# minutes later. Wider than CHECK_INTERVAL so a slow pass cannot skip anyone,
# and the daily claim stops the overlap from prompting twice.
WINDOW_MINUTES = 30

# Marks the evening prompt in the notifications table, so the next one can
# find and replace it. A stable key rather than matching the message text,
# which changes whenever the wording does.
NOTIFICATION_TYPE = 'bucket_prompt'


def start():
    """Start the scheduler thread if it is not already running."""
    global _thread
    if ZoneInfo is None:
        log_warning(logger, 'BUCKET_PROMPT',
                    "zoneinfo unavailable; end-of-day bucket prompts are disabled")
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


def _is_due(tz_name, now_utc):
    """(due, local_date) for a user in `tz_name` right now."""
    try:
        local = now_utc.astimezone(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        # A zone the browser reported that this machine does not know. Log once
        # per pass rather than silently dropping the user forever.
        log_warning(logger, 'BUCKET_PROMPT', f"Unknown timezone {tz_name!r}; skipping user")
        return False, None
    if local.hour != PROMPT_HOUR:
        return False, local.date()
    return local.minute < WINDOW_MINUTES, local.date()


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

        # The balance notification rides the same pass, but not the same clock:
        # its time of day is the user's choice, so it needs the local time
        # rather than the 20:00 window the bucket prompt uses. Sharing the walk
        # is still worth it - the timezone handling is the fiddly part and a
        # second thread doing it again is a second thing to keep in step.
        local_now = _local_now(tz_name, now_utc)
        if local_now is not None:
            try:
                if _raise_balance_prompt(user_id, local_now):
                    raised += 1
            except Exception as e:
                log_exception(logger, 'AUTOBALANCE',
                              f"Failed to raise the balance prompt for user {user_id}: {e}")

        due, local_date = _is_due(tz_name, now_utc)
        if not due:
            continue

        if not _claim(user_id, local_date):
            continue
        try:
            if _raise_prompt(user_id, local_date):
                raised += 1
        except Exception as e:
            log_exception(logger, 'BUCKET_PROMPT',
                          f"Failed to raise prompt for user {user_id}: {e}")

    return raised


def _local_now(tz_name, now_utc):
    """The user's own wall clock, or None if their zone is unknown here."""
    try:
        return now_utc.astimezone(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return None


def _raise_balance_prompt(user_id, local_now):
    """
    Notify a user that it is time to balance, if their cadence says so.

    No notifications row, unlike the evening bucket prompt. This one is a nudge
    to open the app, and the modal is driven by
    autobalance_settings.pending_date instead - so it cannot leave a stale
    'balance your account' line sitting in the list after the user has done it.

    Email as well as push, because the point is to reach someone who is not
    looking at the app.
    """
    import auto_balance

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

    # Nothing to ask about means nothing to send. Checked before the claim so
    # the day is not consumed - if they unlink an account tomorrow, the reminder
    # should come round normally rather than having been silently used up.
    if not auto_balance.anything_to_reconcile(user_id, local_date):
        return False

    if not auto_balance.claim_due(user_id, local_date):
        return False

    pending = auto_balance.pending_bucket_count(user_id, local_date)
    if pending:
        body = (f"Time to check your balance. {pending} "
                f"{'entry' if pending == 1 else 'entries'} will be confirmed.")
    else:
        body = "Time to check your balance."

    _push_balance(user_id, body)
    _email_balance(user_id, body)
    log_info(logger, 'AUTOBALANCE', f"Balance prompt raised for user {user_id}")
    return True


def _push_balance(user_id, body):
    """Best-effort APNs nudge. A failure must not lose the in-app prompt."""
    try:
        from push_notifications import apns_enabled, send_apns_notification
        if not apns_enabled():
            return False
    except Exception:
        return False

    sent = False
    try:
        with get_db_pool().get_cursor() as cursor:
            cursor.execute(
                "SELECT device_token FROM device_tokens WHERE user_id = %s", (user_id,))
            tokens = [r[0] if not isinstance(r, dict) else r['device_token']
                      for r in (cursor.fetchall() or [])]
        for token in tokens:
            try:
                send_apns_notification(token, 'Blankee', body, None, 'default',
                                       {'action': 'autobalance'})
                sent = True
            except Exception as e:
                log_warning(logger, 'AUTOBALANCE',
                            f"Push failed for user {user_id}: {e}")
    except Exception as e:
        log_warning(logger, 'AUTOBALANCE',
                    f"Could not push to user {user_id}: {e}")
    return sent


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
            kind='balance_reminder')
    except Exception as e:
        log_warning(logger, 'AUTOBALANCE',
                    f"Could not email user {user_id}: {e}")
        return False


def _claim(user_id, local_date):
    """
    Claim this user's evening, atomically.

    INSERT IGNORE against the UNIQUE (user_id, prompt_date) key: exactly one
    caller gets rowcount 1, everyone else gets 0 and stops. This is the whole
    defence against the two gunicorn workers both waking at 20:00.
    """
    try:
        # commit=True is required: get_cursor defaults to commit=False, and an
        # uncommitted claim is no claim at all - every worker would win.
        with get_db_pool().get_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT IGNORE INTO bucket_prompts (user_id, prompt_date) VALUES (%s, %s)",
                (user_id, local_date.isoformat()))
            return cursor.rowcount == 1
    except Exception as e:
        log_exception(logger, 'BUCKET_PROMPT', f"Claim failed for user {user_id}: {e}")
        return False


def _raise_prompt(user_id, local_date):
    """Create the notification, and push it if push is configured."""
    import bucket_confirmation

    # Counted from MySQL, not from the Redis cache: at 20:00 the user is almost
    # certainly not hydrated, and a cached read would report zero for exactly the
    # people who have not opened the app and most need the reminder.
    total = bucket_confirmation.count_pending_from_db(user_id, on_date=local_date)
    if not total:
        # Nothing to confirm. The claim stays, so the day is not retried - which
        # is correct: they had no buckets due, and that will not change tonight.
        _record_outcome(user_id, local_date, 0, False)
        return False

    noun = 'entry' if total == 1 else 'entries'
    message = f"You have {total} {noun} to confirm."

    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            # Exactly one of these exists at any time. Today's prompt already
            # counts every outstanding date, so an older one says nothing today's
            # does not - it is the same fact with a staler number.
            #
            # Read ones go too, not just unread. They were kept at first on the
            # grounds that removing something the user had already been through
            # was worse than a duplicate; it is not. A read prompt saying "3
            # entries" sitting under an unread one saying "11" is two answers to
            # one question, and the older is wrong.
            #
            # DELETE then INSERT inside one transaction, so no reader ever sees
            # zero of them or two.
            cursor.execute(
                "DELETE FROM notifications WHERE user_id = %s AND type = %s",
                (user_id, NOTIFICATION_TYPE))
            superseded = cursor.rowcount
            cursor.execute(
                "INSERT INTO notifications (user_id, message, type) VALUES (%s, %s, %s)",
                (user_id, message, NOTIFICATION_TYPE))
        if superseded:
            log_info(logger, 'BUCKET_PROMPT',
                     f"Replaced {superseded} earlier prompt(s) for user {user_id}")

        # The notifications list is cached in Redis, and this wrote straight past
        # it. Without dropping the key the user's next page load reads the old
        # cached list: the prompt exists, the badge counts it, and the
        # notifications page does not show it. Every other notification writer
        # does the same thing after inserting - see _create_notification.
        try:
            if redis_manager._redis_client:
                redis_manager._redis_client.delete(f"notifications:v1:{user_id}")
        except Exception as cache_err:
            log_warning(logger, 'BUCKET_PROMPT',
                        f"Could not clear the notifications cache for user {user_id}: {cache_err}")
    except Exception as e:
        log_exception(logger, 'BUCKET_PROMPT', f"Could not write notification: {e}")

    pushed = _push(user_id, total, noun)
    _record_outcome(user_id, local_date, total, pushed)
    log_info(logger, 'BUCKET_PROMPT',
             f"Raised prompt for user {user_id}: {total} bucket(s), pushed={pushed}")
    return True


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


def _push(user_id, total, noun):
    """
    Best-effort APNs nudge. Never fatal.

    The in-app prompt is the feature; the push is a reminder to go and look. A
    stale device token must not cost the user their prompt.
    """
    try:
        from push_notifications import apns_enabled, send_apns_notification
        if not apns_enabled():
            return False
    except Exception:
        return False

    sent = False
    try:
        with get_db_pool().get_cursor() as cursor:
            cursor.execute(
                "SELECT device_token FROM device_tokens WHERE user_id = %s", (user_id,))
            tokens = [row[0] if not isinstance(row, dict) else row['device_token']
                      for row in (cursor.fetchall() or [])]
        for token in tokens:
            try:
                send_apns_notification(
                    token,
                    title="Confirm today's entries",
                    body=f"{total} {noun} waiting for you.",
                    badge=total,
                    custom={'action': 'bucket_prompt'})
                sent = True
            except Exception as e:
                log_warning(logger, 'BUCKET_PROMPT',
                            f"Push to one device failed for user {user_id}: {e}")
    except Exception as e:
        log_warning(logger, 'BUCKET_PROMPT', f"Push step failed for user {user_id}: {e}")
    return sent


def _record_outcome(user_id, local_date, count, pushed):
    """What the prompt covered, for diagnosing a quiet evening."""
    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE bucket_prompts SET bucket_count = %s, pushed = %s "
                " WHERE user_id = %s AND prompt_date = %s",
                (count, 1 if pushed else 0, user_id, local_date.isoformat()))
    except Exception as e:
        log_warning(logger, 'BUCKET_PROMPT', f"Could not record prompt outcome: {e}")
