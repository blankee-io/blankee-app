"""
Server-side configuration file, for things that must be settable without a
login.

Exists for exactly one problem today: an administrator who has forgotten their
password on an instance with no email configured. Every in-app recovery route
needs either a session or a working mailbox, so the only remaining authority is
filesystem access to the machine itself - which is the right authority, because
someone who can edit files on the server already controls the deployment.

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

# What counts as "on". Anything else, including a missing key, is off - the flag
# fails CLOSED, because the failure mode of guessing wrong is an open password
# reset on a live instance.
_TRUTHY = ('1', 'true', 'yes', 'on')

RESET_KEY = 'RESET_ADMIN_PASSWORD'

# Written verbatim when the file does not exist. The app creating its own config
# is why there is no install step: there is one file, at one path, and no
# template lying around for someone to edit by mistake.
_TEMPLATE = """# Blankee server configuration
#
# Created automatically. Read on every request, so an edit here takes effect on
# the next page load - no restart, no reload.
#
# ---------------------------------------------------------------------------
# RESET_ADMIN_PASSWORD
# ---------------------------------------------------------------------------
# Recovery for an administrator who has forgotten their password on an instance
# with no working email configuration. Every other route needs either a session
# or a mailbox; this one needs the ability to edit this file, which is the same
# authority as running the server.
#
# Set it to 1 and reload the site: the administrator recovery page becomes the
# landing page. Set a new password there and this flag returns to 0 by itself.
#
# WHILE THIS IS 1, ANYONE WHO CAN REACH THE SITE CAN SET THE ADMINISTRATOR
# PASSWORD. There is no second factor - the flag IS the authorization. Turn it
# on when you are ready to use it, not in advance, and check that it went back
# to 0 afterwards.
RESET_ADMIN_PASSWORD=0
"""

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
        # 640: the web server reads and writes it, nobody else reads it. It holds
        # no secret today, but a config file that can enable a password reset is
        # not something to leave world-readable either.
        os.chmod(path, 0o640)
        log_info(logger, 'CONFIG', f'Created {path}')
    except Exception as e:
        # Not an error: this is the expected outcome on a deployment whose config
        # directory is root-owned, and nothing is broken until a flag is wanted.
        log_warning(logger, 'CONFIG',
                    f'No config file at {path} and it could not be created ({e}). '
                    f'All flags read as off. To create it: '
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


def admin_password_reset_enabled():
    """
    Whether the out-of-band admin password reset is currently open.

    While this is true, anyone who can reach the site can set the administrator
    password. That is the intended trade - the operator opened the window
    deliberately - but it is why consume_admin_password_reset() exists and why
    the window should be closed the moment it has been used.
    """
    return _read().get(RESET_KEY, '').lower() in _TRUTHY


def consume_admin_password_reset():
    """
    Close the window. Returns (ok, message).

    Called immediately after a successful reset so the flag cannot be left on by
    accident - a forgotten flag is a standing takeover of the instance.

    Rewrites the file in place rather than deleting it, so the operator's own
    comments survive and the file remains as documentation of the mechanism.
    In-place rewriting also needs no write permission on the directory, which
    matters because /var/www/budget_env is usually root-owned.

    A failure here is reported to the caller rather than swallowed: the reset has
    already happened, and the one thing the operator must know is that the door
    is still open.
    """
    path = config_path()
    if not os.path.exists(path):
        return (True, None)

    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        out, found = [], False
        for line in lines:
            stripped = line.strip()
            if (not stripped.startswith('#') and '=' in stripped
                    and stripped.partition('=')[0].strip().upper() == RESET_KEY):
                out.append(f'{RESET_KEY}=0\n')
                found = True
            else:
                out.append(line)
        if not found:
            out.append(f'{RESET_KEY}=0\n')

        with open(path, 'w', encoding='utf-8') as f:
            f.writelines(out)

        log_info(logger, 'CONFIG', f'{RESET_KEY} switched off in {path} after use')
        return (True, None)
    except Exception as e:
        log_error(logger, 'CONFIG',
                  f'Password reset succeeded but {RESET_KEY} could NOT be switched off '
                  f'in {path} ({e}). It is still open - clear it by hand.')
        return (False, f'The password was changed, but {RESET_KEY} could not be turned '
                       f'off automatically. Set it to 0 in {path} now - until you do, '
                       f'anyone reaching this site can change the administrator password.')
