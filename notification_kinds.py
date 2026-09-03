"""
The kinds of notification this app sends, and which of them reach a user by
email.

One list, in one place, because three things need to agree about it: the sender
deciding whether to email, the settings page listing the switches, and whoever
adds the next kind. When that list lived only in the settings template, a kind
added in the code had no switch and quietly emailed everyone.

Every kind is on unless the user has turned it off, and the opt-outs are what is
stored - see install/sql/add_email_notification_types.sql for why round that way.
The general Email Notifications switch still gates all of them.
"""

from log_config import get_logger, log_warning

logger = get_logger(__name__)

# key, label, and what it covers. The description is the settings page's info
# tip, so it says what arrives rather than restating the label.
KINDS = (
    ('entries_to_confirm', 'Entries to confirm',
     'The evening reminder listing forecast entries waiting for you to say '
     'whether they happened.'),
    ('balance_reminder', 'Balance reminder',
     'The reminder, on the cadence you set, asking whether you want to '
     'reconcile against your real bank balance.'),
    ('low_balance', 'Projected shortfall',
     'Sent when your projected remainder falls below zero on some future date, '
     'so a shortfall shows up before it arrives.'),
    ('allowance_spent', 'Allowance spent',
     'Sent when a recurring allowance is used up, or when income for a category '
     'comes in higher than forecast.'),
    ('pending_transactions', 'Pending transactions',
     'Sent when transactions synced from a bank account are waiting to be given '
     'a category.'),
    ('bundles', 'Bundles',
     'Sent when one of your bundles is activated or deactivated.'),
    ('account', 'Account changes',
     'Sent when your password is reset or your email address is changed. '
     'Turning this off means those changes are not announced by email.'),
)

KIND_KEYS = tuple(k for k, _label, _desc in KINDS)


def parse_disabled(value):
    """The set of kinds a stored opt-out string turns off."""
    if not value:
        return set()
    return {part.strip() for part in str(value).split(',')
            if part.strip() in KIND_KEYS}


def format_disabled(kinds):
    """
    The stored form of a set of opt-outs. Empty string when there are none.

    Empty rather than NULL, and that distinction carries weight: the flush worker
    writes this column with COALESCE, so NULL there means "this cached blob does
    not know about the column" and leaves whatever is stored alone. If "nothing
    disabled" were also NULL, a user turning every type back on could never
    clear their opt-outs.
    """
    kept = [k for k in KIND_KEYS if k in set(kinds or ())]
    return ','.join(kept) if kept else ''


def emails_enabled_for(user, kind):
    """
    Whether this user wants `kind` by email.

    A kind that is not on the list is treated as wanted. That is not laxness: it
    is what keeps a newly added kind switched on for everyone, rather than off
    for every account whose preferences were saved before it existed.

    An unknown kind is also treated as wanted, and logged - the alternative is a
    typo at a call site silently suppressing a notification nobody notices is
    missing.
    """
    if not kind:
        return True
    if kind not in KIND_KEYS:
        log_warning(logger, 'NOTIFICATION',
                    f"Unknown notification kind {kind!r}; emailing anyway")
        return True
    disabled = parse_disabled((user or {}).get('email_notify_disabled'))
    return kind not in disabled
