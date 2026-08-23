"""
Instance-wide settings, currently just the outbound SMTP configuration.

Deliberately depends only on db_connections and the standard library - never on
app - so email_utils can import it without a circular import.

Two invariants worth keeping:

  * get_smtp_config() is the ONLY place that reads the configuration. There is
    no environment fallback by design: mail is configured in the UI or not at
    all. If a source is ever added, it goes in that one function.

  * The plaintext password never leaves this module except inside the dict
    get_smtp_config() returns, which is consumed directly by smtplib. It is
    never rendered into a template or returned from a route - see
    get_smtp_config_for_display().
"""

import hashlib
import os

from db_connections import get_db_pool
from log_config import get_logger, log_info, log_error, log_warning

logger = get_logger(__name__)

# The only default. See get_smtp_config for why the port has one and nothing
# else does.
_DEFAULT_PORT = 587

# Emailed codes are real TOTP, derived from a stored secret and the clock, so
# the code itself is never written down. A 300s step with a one-step window
# means a code works for 5-10 minutes depending on where in the step it landed:
# long enough for mail to arrive, short enough to be worth little if seen.
_CODE_INTERVAL = 300
_CODE_WINDOW = 1

# Six digits is guessable in ~10^6 tries, so attempts against one secret are
# capped. Exhausting them requires saving again, which mints a new secret.
_MAX_CODE_ATTEMPTS = 5


def _fernet():
    """
    Build a Fernet from SETTINGS_ENCRYPTION_KEY, or None if it is unusable.

    Returning None rather than raising is deliberate. A missing key blocks
    saving a NEW password, but it must not take down an instance that is already
    working - so callers handle None instead of the import failing.
    """
    key = os.getenv('SETTINGS_ENCRYPTION_KEY', '').strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode('utf-8'))
    except Exception as e:
        log_error(logger, 'SETTINGS',
                  f'SETTINGS_ENCRYPTION_KEY is set but unusable ({type(e).__name__}); '
                  f'expected a urlsafe base64 32-byte key from Fernet.generate_key()')
        return None


def encryption_available():
    """True when a usable encryption key is configured, so saving can proceed."""
    return _fernet() is not None


def _read_row():
    """The settings row, or None. Never raises - a missing table reads as None."""
    try:
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT smtp_server, smtp_port, smtp_username, "
                "smtp_password_encrypted, from_email, use_tls, "
                "verified_at, verified_fingerprint, verification_secret, "
                "verification_sent_at, verification_attempts "
                "FROM instance_settings WHERE id = 1"
            )
            return cursor.fetchone()
    except Exception as e:
        # Most likely the migration has not been run yet. Returning None reads
        # as "not configured", so mail is skipped and logged rather than raising
        # inside whatever request happened to trigger a notification.
        log_warning(logger, 'SETTINGS',
                    f'Could not read instance_settings ({e}); treating email as unconfigured')
        return None


def get_smtp_config():
    """
    The SMTP configuration, read from the settings row and nowhere else.

    There is deliberately no environment fallback. Mail is configured in the UI
    or not at all, which means one place to look when it is not working, and no
    situation where the app quietly sends through a mailbox nobody on this
    instance chose. The consequence is that email does nothing until somebody
    fills the form in - 'configured' below is how callers check.

    Port is the one convenience default: 587 when blank, because that is the
    near-universal STARTTLS submission port and requiring it to be typed buys
    nothing. Everything else must be entered explicitly.
    """
    row = _read_row() or {}

    server = (row.get('smtp_server') or '').strip()
    username = (row.get('smtp_username') or '').strip()
    from_email = (row.get('from_email') or '').strip()
    port = int(row['smtp_port']) if row.get('smtp_port') else _DEFAULT_PORT
    use_tls = bool(row['use_tls']) if row.get('use_tls') is not None else True

    password = ''
    token = row.get('smtp_password_encrypted')
    if token:
        f = _fernet()
        if f is None:
            log_error(logger, 'SETTINGS',
                      'A password is stored but SETTINGS_ENCRYPTION_KEY is missing or invalid, '
                      'so it cannot be decrypted. Email is disabled until the key is restored.')
        else:
            try:
                password = f.decrypt(token.encode('utf-8')).decode('utf-8')
            except Exception as e:
                log_error(logger, 'SETTINGS',
                          f'Stored SMTP password could not be decrypted ({type(e).__name__}); '
                          f'the encryption key may have changed. Email is disabled.')

    return {
        'server': server,
        'port': port,
        'username': username,
        'password': password,
        'from_email': from_email,
        'use_tls': use_tls,
        # Everything needed to actually send. Callers should check this rather
        # than testing fields individually.
        'configured': bool(server and username and password and from_email),
    }


def get_smtp_config_for_display():
    """
    The configuration as the form needs it: every value the user saved, and no
    password.

    Since there is no environment fallback, these are the same values
    get_smtp_config returns - so a field is populated if and only if somebody
    saved it. password_stored_in_db is the only way to describe the password,
    because the value itself must never reach the browser.
    """
    cfg = get_smtp_config()
    row = _read_row() or {}
    return {
        'smtp_server': cfg['server'],
        'smtp_port': (row.get('smtp_port') or ''),
        'smtp_username': cfg['username'],
        'from_email': cfg['from_email'],
        'use_tls': cfg['use_tls'],
        'password_stored_in_db': bool(row.get('smtp_password_encrypted')),
        'configured': cfg['configured'],
        'encryption_available': encryption_available(),
        'verification': get_verification_state(),
    }


def _fingerprint(cfg):
    """
    A hash of the transport that a verification applies to.

    Deliberately excludes the password. Hashing a credential to store it next to
    the credential is a worse problem than the one it solves, and a password
    change is already visible at save time - see save_smtp_config.
    """
    raw = "|".join([
        cfg["server"],
        str(cfg["port"]),
        cfg["username"],
        cfg["from_email"],
        "1" if cfg["use_tls"] else "0",
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_verified():
    """
    True when the CURRENT configuration is the one that confirmed a code.

    Note what is absent: there is no code anywhere that clears a "verified" flag
    when settings change. The stored fingerprint is recomputed and compared on
    every call, so editing the server or the address makes the instance
    unverified as a matter of arithmetic, rather than as a side effect somebody
    has to remember to trigger.
    """
    row = _read_row() or {}
    if not row.get("verified_at") or not row.get("verified_fingerprint"):
        return False
    cfg = get_smtp_config()
    if not cfg["configured"]:
        return False
    return row["verified_fingerprint"] == _fingerprint(cfg)


def get_verification_state():
    """What the settings page needs in order to describe verification."""
    row = _read_row() or {}
    attempts = int(row.get("verification_attempts") or 0)
    verified = is_verified()
    return {
        "verified": verified,
        "verified_at": row.get("verified_at"),
        # A secret exists but the config is not verified: either a code is in
        # flight, or the settings were edited after verifying.
        "pending": bool(row.get("verification_secret")) and not verified,
        "attempts_left": max(0, _MAX_CODE_ATTEMPTS - attempts),
        "sent_at": row.get("verification_sent_at"),
    }


def start_verification():
    """
    Mint a fresh secret and return the code to email. Returns (code, error).

    The caller sends the mail, and that is the whole point: the code can only
    arrive if the settings just saved actually work, so delivery IS the test.
    """
    cfg = get_smtp_config()
    if not cfg["configured"]:
        return (None, "Fill in the address, username, password and server first.")

    try:
        import pyotp
    except ImportError:
        log_error(logger, "SETTINGS", "pyotp is not installed; cannot generate a code.")
        return (None, "The server is missing the pyotp library.")

    secret = pyotp.random_base32()
    code = pyotp.TOTP(secret, interval=_CODE_INTERVAL).now()

    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE instance_settings SET verification_secret = %s, "
                "verification_sent_at = NOW(), verification_attempts = 0 "
                "WHERE id = 1", (secret,)
            )
    except Exception as e:
        log_error(logger, "SETTINGS", f"Could not store the verification secret: {e}")
        return (None, "Could not start verification.")

    log_info(logger, "SETTINGS", f"Verification code generated for {cfg['from_email']}")
    return (code, None)


def confirm_verification(code):
    """
    Check a submitted code. Returns (ok, message).

    The attempt is counted BEFORE the comparison, so a crash between the two
    cannot hand out a free guess.
    """
    code = (code or "").strip().replace(" ", "")
    if not code:
        return (False, "Enter the code from the email.")

    row = _read_row() or {}
    secret = row.get("verification_secret")
    if not secret:
        return (False, "No code is outstanding. Save your settings to send one.")

    attempts = int(row.get("verification_attempts") or 0)
    if attempts >= _MAX_CODE_ATTEMPTS:
        return (False, "Too many incorrect attempts. Save your settings again to send a new code.")

    try:
        import pyotp
    except ImportError:
        return (False, "The server is missing the pyotp library.")

    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            cursor.execute("UPDATE instance_settings SET verification_attempts = "
                           "verification_attempts + 1 WHERE id = 1")
    except Exception as e:
        log_error(logger, "SETTINGS", f"Could not record a verification attempt: {e}")
        return (False, "Could not check the code.")

    if not pyotp.TOTP(secret, interval=_CODE_INTERVAL).verify(code, valid_window=_CODE_WINDOW):
        left = max(0, _MAX_CODE_ATTEMPTS - (attempts + 1))
        log_warning(logger, "SETTINGS", f"Incorrect SMTP verification code; {left} attempt(s) left")
        if left == 0:
            return (False, "Incorrect code, and no attempts left. Save your settings "
                           "again to send a new one.")
        return (False, f"That code is incorrect or has expired. {left} attempt(s) left.")

    cfg = get_smtp_config()
    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE instance_settings SET verified_at = NOW(), "
                "verified_fingerprint = %s, verification_secret = NULL, "
                "verification_attempts = 0 WHERE id = 1",
                (_fingerprint(cfg),)
            )
    except Exception as e:
        log_error(logger, "SETTINGS", f"Could not record verification: {e}")
        return (False, "The code was correct but the result could not be saved.")

    log_info(logger, "SETTINGS", f"Email delivery verified for {cfg['from_email']}")
    return (True, "Email verified. Notifications can now be turned on.")


def disable_delivery():
    """
    Turn email delivery off by discarding the verification.

    Deliberately not a separate "enabled" flag. With one piece of state there is
    no way for "enabled" and "proven to work" to drift apart, and no possibility
    of an enabled instance whose proof belongs to some older configuration. The
    cost is that re-enabling sends a fresh code, which is a fair price for
    having switched it off.

    The settings themselves are untouched, so re-enabling means one click and
    one code - not retyping the credential.
    """
    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE instance_settings SET verified_at = NULL, "
                "verified_fingerprint = NULL, verification_secret = NULL, "
                "verification_attempts = 0 WHERE id = 1"
            )
        log_info(logger, 'SETTINGS', 'Email delivery disabled')
        return (True, 'Email delivery is off. Notifications will not be sent.')
    except Exception as e:
        log_error(logger, 'SETTINGS', f'Could not disable email delivery: {e}')
        return (False, 'Could not turn email delivery off.')


def save_smtp_config(smtp_server, smtp_port, smtp_username, from_email,
                     use_tls=True, smtp_password=None):
    """
    Upsert the settings row.

    The visible fields overwrite directly, including with NULL. That is correct
    because the form prefills them from whatever was saved before, so what the
    user submits is what they see - and clearing a field is a deliberate request
    to unset it, which turns email off until it is filled in again.

    The password is the exception, because it is the one field never prefilled:
    blank there means "keep the stored one", not "clear it". Otherwise every
    save of an unrelated field would wipe the credential.

    Returns (ok, message). Refuses to store a password without an encryption
    key rather than writing plaintext.
    """
    encrypt_token = None
    if smtp_password:
        f = _fernet()
        if f is None:
            return (False,
                    'Cannot store the password: SETTINGS_ENCRYPTION_KEY is not set on the server. '
                    'Generate one with Fernet.generate_key() and add it to the server environment.')
        try:
            encrypt_token = f.encrypt(smtp_password.encode('utf-8')).decode('utf-8')
        except Exception as e:
            log_error(logger, 'SETTINGS', f'Failed to encrypt SMTP password: {e}')
            return (False, 'Could not encrypt the password.')

    try:
        port = int(smtp_port) if smtp_port else None
    except (TypeError, ValueError):
        return (False, 'Port must be a number.')

    base_cols = "(id, smtp_server, smtp_port, smtp_username, from_email, use_tls)"
    base_vals = (smtp_server or None, port, smtp_username or None,
                 from_email or None, 1 if use_tls else 0)

    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            if encrypt_token is None:
                # Leave whatever password is already stored in place.
                cursor.execute(
                    "INSERT INTO instance_settings " + base_cols + " "
                    "VALUES (1, %s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE "
                    "  smtp_server = VALUES(smtp_server), "
                    "  smtp_port = VALUES(smtp_port), "
                    "  smtp_username = VALUES(smtp_username), "
                    "  from_email = VALUES(from_email), "
                    "  use_tls = VALUES(use_tls)",
                    base_vals
                )
            else:
                cursor.execute(
                    "INSERT INTO instance_settings "
                    "(id, smtp_server, smtp_port, smtp_username, "
                    " smtp_password_encrypted, from_email, use_tls) "
                    "VALUES (1, %s, %s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE "
                    "  smtp_server = VALUES(smtp_server), "
                    "  smtp_port = VALUES(smtp_port), "
                    "  smtp_username = VALUES(smtp_username), "
                    "  smtp_password_encrypted = VALUES(smtp_password_encrypted), "
                    "  from_email = VALUES(from_email), "
                    "  use_tls = VALUES(use_tls), "
                    # The password is the one field the fingerprint cannot
                    # cover, so this is where a password change has to drop
                    # verification explicitly.
                    "  verified_at = NULL, "
                    "  verified_fingerprint = NULL",
                    (smtp_server or None, port, smtp_username or None,
                     encrypt_token, from_email or None, 1 if use_tls else 0)
                )
        log_info(logger, 'SETTINGS',
                 f'SMTP settings saved (server={smtp_server}, password_changed={bool(encrypt_token)})')
        return (True, 'Settings saved.')
    except Exception as e:
        log_error(logger, 'SETTINGS', f'Failed to save SMTP settings: {e}')
        return (False, 'Could not save settings.')


def get_notification_recipient():
    """
    Where notification emails go, or None if nowhere.

    Returns None until the address has confirmed a code. An unverified address
    is one nobody has shown can receive mail, and notifications are exactly the
    traffic that fails silently - so this refuses rather than firing into the
    dark. get_verification_state() is what the UI uses to explain why.

    Still the seam for per-user destinations: callers delegate the decision
    here, so that change stays local to this function.
    """
    if not is_verified():
        return None
    return get_smtp_config().get('from_email') or None
