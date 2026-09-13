"""
Server-side configuration file, for things that must be settable without a
login.

Exists for exactly one problem today: an administrator who has forgotten their
password on an instance with no email configured. Every in-app recovery route
needs either a session or a working mailbox, so the only remaining authority is
filesystem access to the machine itself - which is the right authority, because
someone who can edit files on the server already controls the deployment.

TWO FILES, and which one a flag lives in is the security model:

  /var/www/budget_env/blankee.conf   owned by the WEB user. Flags the app itself
                                     sets: an update request, the auto-update
                                     toggle. Anything a compromised web process
                                     may as well be allowed to set.
  /etc/blankee/blankee.conf          owned by ROOT. Flags that must mean "a
                                     human on the machine did this": the admin
                                     password reset.

The reset used to live in the first file, which made the docstring on its route
("only someone with access to the machine can set this") false: the web process
owned the file. It now opens only from the root file, and the web file records
when it has been USED - so the app can close the window after a reset, and can
never open one.

Deliberately NOT part of instance_settings.py: that lives in the database, and a
locked-out operator can reach a file far more easily than a table. Deliberately
not an environment variable either - those are read once at import, so setting
one would mean restarting Apache before it took effect, and the flag needs to be
usable by someone who may not know that.

Read on every call rather than cached, so touching the file takes effect on the
next request.

FILE FORMAT - one KEY=VALUE per line, # for comments:

    # /var/www/budget_env/blankee.conf
    RESET_ADMIN_PASSWORD=1

The path defaults to blankee.conf beside the .env and can be overridden with the
BLANKEE_CONFIG environment variable.
"""

import os

from log_config import get_logger, log_info, log_error, log_warning

logger = get_logger(__name__)

_DEFAULT_PATH = '/var/www/budget_env/blankee.conf'
_DEFAULT_ROOT_PATH = '/etc/blankee/blankee.conf'

# What counts as "on". Anything else, including a missing key, is off - the flag
# fails CLOSED, because the failure mode of guessing wrong is an open password
# reset on a live instance.
_TRUTHY = ('1', 'true', 'yes', 'on')

RESET_KEY = 'RESET_ADMIN_PASSWORD'
# Written to the WEB file after a reset, holding the value of RESET_KEY that was
# consumed. The window is open only while the root file's value differs from
# this, so the operator reopens it by writing any NEW value - a date is the
# natural one - rather than by flipping the same 1 again.
RESET_USED_KEY = 'RESET_ADMIN_PASSWORD_USED'

# Self-update. SELF_UPDATE doubles as feature detection, which is why the
# installer only sets it to 1 after `systemctl enable` has actually succeeded:
# it is absent under Docker, absent on an install predating the feature, and 0
# when an operator opts out - and all three should produce the same behaviour,
# which the fail-closed read below already gives.
SELF_UPDATE_KEY = 'SELF_UPDATE'
UPDATE_REQUESTED_KEY = 'UPDATE_REQUESTED'
UPDATE_REQUEST_ID_KEY = 'UPDATE_REQUEST_ID'
AUTO_UPDATE_KEY = 'AUTO_UPDATE'

# Written verbatim when the file does not exist. The installer calls
# ensure_config_file() so there is one template, at one path, with no copy in a
# shell script to drift from it. The app itself can no longer create the file -
# CONFIG_DIR is 750, so the web user cannot add entries to that directory - but
# it can still rewrite the file in place, which is all any flag needs.
_TEMPLATE = """# Blankee server configuration
#
# Created automatically. Read on every request, so an edit here takes effect on
# the next page load - no restart, no reload.
#
# ---------------------------------------------------------------------------
# RESET_ADMIN_PASSWORD - has moved
# ---------------------------------------------------------------------------
# The administrator password reset is opened from /etc/blankee/blankee.conf,
# which is owned by root, and NOT from here. This file is owned by the web
# server, and a flag that can hand out the administrator password must not be
# settable by the web server. Setting RESET_ADMIN_PASSWORD in this file does
# nothing.
#
# RESET_ADMIN_PASSWORD_USED is written here by the application after a reset:
# it records the root file's value that was consumed, which is how the window
# closes itself. Leave it alone.
RESET_ADMIN_PASSWORD_USED=

# ---------------------------------------------------------------------------
# SELF_UPDATE
# ---------------------------------------------------------------------------
# Whether the admin console may apply updates. Set to 1 by install/install.sh,
# and only once the blankee-update systemd timer is actually enabled - so it
# answers "can this instance update itself" rather than "was it asked to".
#
# Set it to 0 to take the button away and be told the commands instead. The
# units stay installed either way; nothing is uninstalled by turning this off.
SELF_UPDATE=0

# ---------------------------------------------------------------------------
# UPDATE_REQUESTED
# ---------------------------------------------------------------------------
# Set to 1 by the admin console to ask for an update. A root-owned systemd
# service checks this every minute, and clears it before doing any work - so a
# request is consumed exactly once even if the update then fails.
#
# Editing it here is a supported way to update from a shell:
#     sudo sed -i 's/^UPDATE_REQUESTED=0/UPDATE_REQUESTED=1/' this-file
# though `sudo systemctl start blankee-update` is more direct.
#
# UPDATE_REQUEST_ID is written alongside it, and exists so the console can tell
# "the updater finished my request" from "I am reading last week's result".
UPDATE_REQUESTED=0
UPDATE_REQUEST_ID=

# ---------------------------------------------------------------------------
# AUTO_UPDATE
# ---------------------------------------------------------------------------
# Apply updates without being asked. When this is 1, a systemd timer checks
# once a day, at midnight local time, and installs anything newer.
#
# Off by default, and worth a moment's thought before turning on: it means code
# from the internet is installed and run on this machine while nobody is
# watching. It also means a release with a problem reaches this instance before
# anybody has had a chance to notice. The trade is that security fixes arrive
# without you having to remember.
#
# Nothing else changes: the same updater runs, refuses a modified working tree,
# verifies the schema first, and stops at the first failure rather than pressing
# on. Results are in `journalctl -u blankee-update`.
AUTO_UPDATE=0
"""

# The root-owned file. Created by install.sh as root; the application never
# writes it, and cannot.
_ROOT_TEMPLATE = """# Blankee server configuration - root-owned.
#
# The flags in this file must mean "a person with access to this machine did
# this", which is why it lives here and not beside .env: the file beside .env
# is owned by the web server, and nothing in here may be settable by it.
#
# ---------------------------------------------------------------------------
# RESET_ADMIN_PASSWORD
# ---------------------------------------------------------------------------
# Recovery for an administrator who has forgotten their password on an instance
# with no working email configuration. Every other route needs either a session
# or a mailbox; this one needs the ability to edit THIS file, which is the same
# authority as running the server.
#
# Set it to any value other than 0 - today's date is a good one - and reload the
# site: the administrator recovery page becomes the landing page. Set a new
# password there and the window closes by itself: the application records the
# value it consumed, and the window stays closed until this line holds a value
# it has not seen. To reopen it later, write a NEW value here, not the same one.
#
# WHILE THIS IS OPEN, ANYONE WHO CAN REACH THE SITE CAN SET THE ADMINISTRATOR
# PASSWORD. There is no second factor - the flag IS the authorization. Turn it
# on when you are ready to use it, not in advance, and check the log line
# saying it closed afterwards.
RESET_ADMIN_PASSWORD=0
"""


def root_config_path():
    """The root-owned file. Overridable so a test can point somewhere else."""
    return os.getenv('BLANKEE_ROOT_CONFIG', _DEFAULT_ROOT_PATH)


def ensure_root_config_file():
    """
    Create the root-owned file, with everything off, if it is not there.

    For the installer, running as root. Never overwrites: the value in it is the
    operator's. Mode 644 - it holds no secret, and the web tier must be able to
    READ it to know whether the window is open; only writing is root's.
    """
    path = root_config_path()
    if os.path.exists(path):
        return
    with open(path, 'w', encoding='utf-8') as f:
        f.write(_ROOT_TEMPLATE)
    os.chmod(path, 0o644)


def _read_root():
    """
    The root-owned file as a dict, or {} if absent or unreadable.

    No lock: root writes it by hand and rarely, and the reader is only ever the
    web tier. A torn read of a hand edit can at worst see the old value for one
    request.
    """
    path = root_config_path()
    if not os.path.exists(path):
        return {}
    try:
        out = {}
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, value = line.partition('=')
                out[key.strip().upper()] = value.strip()
        return out
    except Exception as e:
        log_error(logger, 'CONFIG', f'Could not read {path}: {e}')
        return {}


# One creation attempt per process. Retrying on every request would mean a
# failed write logging on every request too.
_ensure_attempted = False


def ensure_config_file():
    """
    Create the config file, with everything off, if it is not there.

    Removes the setup step this used to need. The failure case is deliberately
    not fatal: the file being absent means every flag reads as off, which is the
    correct default, so a read-only directory costs nothing until somebody
    actually wants to use a flag - and the log line says exactly what to run.
    """
    global _ensure_attempted
    if _ensure_attempted:
        return
    _ensure_attempted = True

    path = config_path()
    if os.path.exists(path):
        return

    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(_TEMPLATE)
        # 660: the web server owns it and sets the flags; the updater, a member
        # of its group, clears UPDATE_REQUESTED in place once it has taken a
        # request. Nobody else reads it - it holds no secret today, but a file
        # the machine acts on is not something to leave world-readable either.
        os.chmod(path, 0o660)
        log_info(logger, 'CONFIG', f'Created {path}')
    except Exception as e:
        # Not an error, and the expected outcome when the web user runs this:
        # CONFIG_DIR is deliberately not writable by www-data, so creating the file
        # is the installer's job. Nothing is broken until a flag is wanted, and
        # every flag reads as off until then.
        log_warning(logger, 'CONFIG',
                    f'No config file at {path} and it could not be created ({e}). '
                    f'All flags read as off. Re-run install/install.sh to create it, '
                    f'or by hand: '
                    f'sudo install -o www-data -g www-data -m 640 /dev/null {path}')


def config_path():
    """Where the file lives. Overridable so a test can point somewhere else."""
    return os.getenv('BLANKEE_CONFIG', _DEFAULT_PATH)


def _read():
    """
    The file as a dict, or {} if it is absent or unreadable.

    A missing file is the normal case - most instances never need one - so it is
    not worth a log line. An unreadable one is, because that is a permissions
    problem someone should hear about.
    """
    path = config_path()
    if not os.path.exists(path):
        return {}
    try:
        out = {}
        with _locked(path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, value = line.partition('=')
                out[key.strip().upper()] = value.strip()
        return out
    except Exception as e:
        log_error(logger, 'CONFIG', f'Could not read {path}: {e}')
        return {}


def _reset_value():
    """The root file's RESET_ADMIN_PASSWORD, or '' when off."""
    value = _read_root().get(RESET_KEY, '').strip()
    if value.lower() in ('', '0', 'false', 'no', 'off'):
        return ''
    return value


def admin_password_reset_enabled():
    """
    Whether the out-of-band admin password reset is currently open.

    Open only when the ROOT-owned file names a value, and the web-owned file
    has not recorded that same value as already used. Both halves matter: the
    first is what makes opening it need a person on the machine, the second is
    what lets the application close it again after a reset without being able
    to write the root file. The asymmetry is the point - the web process can
    shut this door and can never open it.

    While this is true, anyone who can reach the site can set the administrator
    password. That is the intended trade - the operator opened the window
    deliberately - but it is why consume_admin_password_reset() exists and why
    the window should be closed the moment it has been used.
    """
    value = _reset_value()
    if not value:
        return False
    return _read().get(RESET_USED_KEY, '').strip() != value


# fcntl is POSIX-only. The application only ever runs on Linux, but importing
# this module on a developer's Windows box should not explode - without the lock
# the behaviour is exactly what it was before locking existed.
try:
    import fcntl
except ImportError:
    fcntl = None


class _locked:
    """
    Open the config file with an flock held for the duration.

    The lock, not atomic replacement, is what makes this file safe to share.
    An atomic write means a temp file plus os.replace(), and replace() needs
    write permission on the *directory* - which www-data deliberately does not
    have, because that permission is also the right to swap out the virtualenv
    and the WSGI entry point sitting beside this file. So the app rewrites the
    file in place, and every reader and writer takes a lock instead.

    Readers take a shared lock so they cannot observe a half-written file;
    writers take an exclusive one so two threads cannot interleave. Root-run
    tooling that reads this file must take the same shared lock.

    A platform without fcntl, or a file that cannot be locked, degrades to no
    lock rather than failing: an unlocked read is what this code did for its
    whole life until now, and refusing to read would break the password-reset
    recovery path this file exists to serve.
    """

    def __init__(self, path, mode, exclusive=False):
        self.path, self.mode, self.exclusive = path, mode, exclusive
        self.f = None

    def __enter__(self):
        self.f = open(self.path, self.mode, encoding='utf-8')
        if fcntl is not None:
            try:
                fcntl.flock(self.f.fileno(),
                            fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH)
            except OSError as e:
                log_warning(logger, 'CONFIG',
                            f'Could not lock {self.path} ({e}); proceeding unlocked')
        return self.f

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.exclusive and exc_type is None:
                self.f.flush()
                os.fsync(self.f.fileno())
        finally:
            if fcntl is not None:
                try:
                    fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            self.f.close()
        return False


def _set_keys(mapping):
    """
    Set one or more KEY=VALUE pairs in place. Returns (ok, error_message).

    In place, on a single file handle, under an exclusive lock: seek to the
    start, write, truncate. Comments and key order survive, which matters
    because this file is documentation as much as configuration.

    A key that is not present is appended rather than silently dropped - the
    caller asked for a value to be set, and a file that predates the key must
    still end up with it.
    """
    path = config_path()
    if not os.path.exists(path):
        return (False, f'{path} does not exist')

    want = {k.upper(): v for k, v in mapping.items()}
    try:
        with _locked(path, 'r+', exclusive=True) as f:
            lines = f.readlines()

            out, seen = [], set()
            for line in lines:
                stripped = line.strip()
                if not stripped.startswith('#') and '=' in stripped:
                    key = stripped.partition('=')[0].strip().upper()
                    if key in want:
                        out.append(f'{key}={want[key]}\n')
                        seen.add(key)
                        continue
                out.append(line)

            for key, value in want.items():
                if key not in seen:
                    if out and not out[-1].endswith('\n'):
                        out.append('\n')
                    out.append(f'{key}={value}\n')

            f.seek(0)
            f.writelines(out)
            f.truncate()
        return (True, None)
    except Exception as e:
        return (False, str(e))


def consume_admin_password_reset():
    """
    Close the window. Returns (ok, message).

    Called immediately after a successful reset so the flag cannot be left on by
    accident - a forgotten flag is a standing takeover of the instance.

    Rewrites the file in place rather than deleting it, so the operator's own
    comments survive and the file remains as documentation of the mechanism.
    In-place rewriting also needs no write permission on the directory, which is
    what makes it possible at all: the installer keeps /var/www/budget_env at 750
    precisely so the web user cannot create or unlink entries there. See _locked
    for why a lock rather than an atomic replace.

    A failure here is reported to the caller rather than swallowed: the reset has
    already happened, and the one thing the operator must know is that the door
    is still open.
    """
    path = config_path()
    value = _reset_value()
    if not value or not os.path.exists(path):
        return (True, None)

    # Recorded in the WEB file, which is the one this process may write. The
    # legacy key is set to 0 as well, so an operator reading the old file is not
    # misled by a stale 1 left there from before the move.
    ok, error = _set_keys({RESET_USED_KEY: value, RESET_KEY: '0'})
    if ok:
        log_info(logger, 'CONFIG',
                 f'{RESET_KEY} closed after use ({root_config_path()} value recorded '
                 f'in {path})')
        return (True, None)

    log_error(logger, 'CONFIG',
              f'Password reset succeeded but the window could NOT be closed: '
              f'{RESET_USED_KEY} could not be written to {path} ({error}). '
              f'It is still open - set {RESET_KEY}=0 in {root_config_path()} by hand.')
    return (False, f'The password was changed, but the reset window could not be '
                   f'closed automatically. Set {RESET_KEY}=0 in {root_config_path()} '
                   f'now - until you do, anyone reaching this site can change the '
                   f'administrator password.')


def self_update_enabled():
    """
    Whether this instance can apply its own updates.

    Fails closed, like every other flag here. A missing file, a missing key and
    an unreadable file all mean "no", which is the right answer: the console then
    shows the commands to run instead of a button that would write a flag nothing
    is watching.
    """
    return _read().get(SELF_UPDATE_KEY, '').lower() in _TRUTHY


def update_requested():
    """(requested, request_id). The privileged updater's entry point."""
    values = _read()
    return (values.get(UPDATE_REQUESTED_KEY, '').lower() in _TRUTHY,
            values.get(UPDATE_REQUEST_ID_KEY, '').strip() or None)


def request_update():
    """
    Ask for an update. Returns (ok, request_id, error).

    Both keys are written in one pass. Two separate writes could leave the
    updater looking at a fresh UPDATE_REQUESTED beside the previous request id,
    and it would then report the wrong run as finished.

    The id is a timestamp and eight random hex characters. It is opaque and
    carries no instruction - deliberately, because this file is the one channel
    from the web process to a root process, and the moment it carries something
    root acts on (a ref, a branch, a path) the web user chooses what root
    checks out.
    """
    import secrets
    import time

    request_id = f'{int(time.time())}-{secrets.token_hex(4)}'
    ok, error = _set_keys({UPDATE_REQUESTED_KEY: '1',
                           UPDATE_REQUEST_ID_KEY: request_id})
    if not ok:
        log_error(logger, 'UPDATE', f'Could not request an update in {config_path()}: {error}')
        return (False, None, error)
    log_info(logger, 'UPDATE', f'Update requested', request_id=request_id)
    return (True, request_id, None)


def clear_update_request():
    """
    Clear the request. Called by the privileged updater before it starts work,
    so a crash cannot make the request repeat forever.

    The request id is deliberately left in place as a record of what was last
    asked for.
    """
    ok, error = _set_keys({UPDATE_REQUESTED_KEY: '0'})
    if not ok:
        log_error(logger, 'UPDATE', f'Could not clear the update request: {error}')
    return (ok, error)


def auto_update_enabled():
    """Whether updates are applied without being asked. Fails closed."""
    return _read().get(AUTO_UPDATE_KEY, '').lower() in _TRUTHY


def set_auto_update(on):
    """Turn automatic updates on or off. Returns (ok, error)."""
    ok, error = _set_keys({AUTO_UPDATE_KEY: '1' if on else '0'})
    if ok:
        log_info(logger, 'UPDATE',
                 f'Automatic updates turned {"on" if on else "off"}')
    else:
        log_error(logger, 'UPDATE', f'Could not change {AUTO_UPDATE_KEY}: {error}')
    return (ok, error)
