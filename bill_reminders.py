"""
Bills due today: a reminder at the hour the person chose, naming the bills
that fall on that day.

Bills only. An allowance is a budget to spend down over its period and
nothing about it is "due"; a wage is money coming in. What is listed is the
forecast that sits on today and is a bill: a recurring expense set up as a
bill, a card payment (typed or recurring - the category is a card's mirror
either way), and on the cards themselves a subscription set up as a bill or
a projected interest charge.

Read from MySQL, not the cache: at breakfast time the user is not hydrated
and the cache would say nothing is due. The same reasoning as
bucket_confirmation.count_pending_from_db.

The scheduler in bucket_prompt_scheduler calls raise_if_due once per pass
for every user with a timezone; claim_day is what keeps two processes from
both sending (see the migration add_bill_reminders.sql).
"""
from datetime import datetime, time, timedelta

from db_connections import get_db_pool
from log_config import get_logger, log_info, log_warning, log_exception

logger = get_logger(__name__)
TAG = 'BILLS_DUE'
KIND = 'bills_due'
DEFAULT_TIME = time(8, 0)


def _clean_time(value):
    """HH:MM as a time, defaulting to 08:00. MySQL hands TIME back as a timedelta."""
    if isinstance(value, time):
        return value
    if isinstance(value, timedelta):
        total = int(value.total_seconds())
        return time(total // 3600 % 24, total % 3600 // 60)
    if value:
        text = str(value).strip()
        for fmt in ('%H:%M:%S', '%H:%M'):
            try:
                return datetime.strptime(text, fmt).time()
            except ValueError:
                continue
    return DEFAULT_TIME


def _row(row, names):
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    return dict(zip(names, row))


def get_settings(user_id):
    """The user's bill reminder settings, or None if they have never set any."""
    try:
        with get_db_pool().get_cursor() as cursor:
            cursor.execute(
                "SELECT enabled, notify_time, last_sent FROM bill_reminders WHERE user_id = %s",
                (user_id,))
            row = _row(cursor.fetchone(), ('enabled', 'notify_time', 'last_sent'))
    except Exception as e:
        log_exception(logger, TAG, f'Could not read settings for user {user_id}: {e}')
        return None
    if not row:
        return None
    return {'enabled': bool(row.get('enabled')), 'notify_time': _clean_time(row.get('notify_time')),
            'last_sent': row.get('last_sent')}


def notify_at(settings):
    return _clean_time((settings or {}).get('notify_time'))


def save_settings(user_id, enabled, notify_time=None):
    """Store the switch and the time of day. Returns (ok, message)."""
    at = _clean_time(notify_time)
    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            cursor.execute(
                "INSERT INTO bill_reminders (user_id, enabled, notify_time) VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE enabled = VALUES(enabled), notify_time = VALUES(notify_time)",
                (user_id, 1 if enabled else 0, at.strftime('%H:%M:%S')))
    except Exception as e:
        log_exception(logger, TAG, f'Could not save settings for user {user_id}: {e}')
        return False, 'Could not save that.'
    return True, 'Bills due reminder saved.'


def claim_day(user_id, local_date):
    """
    Claim today's reminder for this user. True means this process won it.

    The UPDATE is the concurrency control: moving last_sent to today succeeds
    for exactly one of the processes that wake at the same minute.
    """
    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE bill_reminders SET last_sent = %s "
                " WHERE user_id = %s AND enabled = 1 AND (last_sent IS NULL OR last_sent < %s)",
                (local_date.isoformat(), user_id, local_date.isoformat()))
            return cursor.rowcount == 1
    except Exception as e:
        log_exception(logger, TAG, f'Could not claim the day for user {user_id}: {e}')
        return False


def bills_due(user_id, on_date):
    """
    The bills sitting on `on_date`: [{'name', 'amount', 'account'}], the
    budget's first and then each card's, each group by name.

    A forecast is a bill when its category is a recurring expense set up as
    one, or a card's payment category; on a card, a recurring charge set up
    as a bill, or the projected interest. Allowances, wages, variable income
    and bundles are not bills and are left out.
    """
    day = on_date.isoformat()
    out = []
    try:
        with get_db_pool().get_cursor() as cursor:
            cursor.execute(
                "SELECT c.name, e.amount FROM expense_entries e "
                "  JOIN expense_categories c ON c.id = e.category_id "
                " WHERE c.user_id = %s AND e.is_bucket = 1 AND e.amount > 0 AND e.date = %s "
                "   AND (c.is_credit_account = 1 OR EXISTS ("
                "        SELECT 1 FROM recurring_expense r WHERE r.category_id = c.id AND r.wage_bill = 1)) "
                " ORDER BY c.name", (user_id, day))
            for row in cursor.fetchall() or []:
                row = _row(row, ('name', 'amount'))
                out.append({'name': row['name'], 'amount': float(row['amount']), 'account': None})
            cursor.execute(
                "SELECT c.name, e.amount, a.name AS account FROM c_expense_entries e "
                "  JOIN c_expense_categories c ON c.id = e.category_id "
                "  JOIN credit_accounts a ON a.id = c.account_id "
                " WHERE a.user_id = %s AND e.is_bucket = 1 AND e.amount > 0 AND e.date = %s "
                "   AND (c.is_interest = 1 OR EXISTS ("
                "        SELECT 1 FROM recurring_c_expense r WHERE r.category_id = c.id AND r.wage_bill = 1)) "
                " ORDER BY a.name, c.name", (user_id, day))
            for row in cursor.fetchall() or []:
                row = _row(row, ('name', 'amount', 'account'))
                out.append({'name': row['name'], 'amount': float(row['amount']), 'account': row['account']})
    except Exception as e:
        log_exception(logger, TAG, f'Could not list the bills due for user {user_id}: {e}')
        return []
    return out


def _money(amount):
    return f'${amount:,.2f}'


def message(items, day=None):
    """
    A list, one bill to a line:

        Due today:
        • Rent $1,650.00
        • Gym $45.00 on Coastal Visa
        Open the day

    The line breaks are real newlines in the stored text. Every place a
    notification is shown keeps them: the notifications page turns them into
    <br> (notification_html), the email's HTML part likewise, and the push and
    the iOS extension's fetch keep them as the alert's own line breaks. The
    link goes to the day view, which is where the bills sit; the push takes its
    deep link from the first link in the message.
    """
    lines = ['Due today:']
    for it in items:
        text = f"\u2022 {it['name']} {_money(it['amount'])}"
        if it.get('account'):
            text += f" on {it['account']}"
        lines.append(text)
    href = '/dashboard_d' + (f'?date={day.isoformat()}' if day else '')
    lines.append(f'<a href="{href}">Open the day</a>')
    return '\n'.join(lines)


def raise_if_due(user_id, local_now):
    """
    Send today's reminder if it is on, the hour has come and nothing has
    gone out yet today. Returns True when one was sent.

    The day is claimed before the bills are counted, so a day with nothing
    due is a day with nothing sent - not a day that keeps being checked
    every five minutes until something turns up at an odd hour.
    """
    settings = get_settings(user_id)
    if not settings or not settings.get('enabled'):
        return False
    if local_now.time() < notify_at(settings):
        return False
    local_date = local_now.date()
    if settings.get('last_sent') and settings['last_sent'] >= local_date:
        return False
    if not claim_day(user_id, local_date):
        return False
    items = bills_due(user_id, local_date)
    if not items:
        log_info(logger, TAG, f'user {user_id}: nothing due on {local_date}')
        return False
    try:
        from app import add_notification
        add_notification(user_id, message(items, local_date), kind=KIND, notification_type=KIND)
    except Exception as e:
        log_warning(logger, TAG, f'user {user_id}: could not send the reminder: {e}')
        return False
    log_info(logger, TAG, f'user {user_id}: {len(items)} bill(s) due on {local_date}')
    return True
