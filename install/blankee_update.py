#!/usr/bin/env python3
"""
Apply an update when the admin console asks for one.

Run by the blankee-update systemd service every minute. Exits immediately and
writes nothing unless UPDATE_REQUESTED=1 is set in blankee.conf, so the ordinary
case costs one small file read.

    blankee_update.py                 honour the flag; no-op if it is not set
    blankee_update.py --force         run regardless (for an operator at a shell)
    blankee_update.py --dry-run       preflight and fetch, change nothing
    blankee_update.py --mark-aborted  stamp an unfinished run as failed

WHY PYTHON AND NOT A SHELL SCRIPT, unlike install.sh: this script runs
`git reset --hard`, which rewrites the file it is executing from. bash reads a
script incrementally as it runs, so replacing the file underneath it can make it
resume in the middle of a different command. CPython reads and compiles the whole
file before executing a line, so its own source changing is harmless. That is
also why the systemd unit points at the copy in the repository rather than an
installed copy: there is no second copy to go stale.

WHY IT IMPORTS NOTHING FROM THE REPOSITORY: it is about to replace that tree.
Importing server_config to read a flag would mean the helper could change between
two calls to it, and a failed update could leave the updater unable to report why.
So the fifteen lines of KEY=VALUE parsing are duplicated here on purpose.

The privilege boundary: the web process (www-data) can only set a flag in a file
it owns. It never chooses a ref, a branch, a remote or a path - if it could, it
would be choosing what gets checked out and run. This script takes nothing from
that file except "yes" and an opaque id. In particular it does NOT take the
location of the signing key from there: that comes from the unit's own
environment, and the file it names must be root's - see verify_release().

WHAT IT UPDATES TO: the newest release TAG on the branch, signed by the pinned
key, not the tip of the branch - see resolve_release(). Development happens on
that branch in the open, so its tip is whatever was merged last; the tag is the
maintainer saying a particular commit is a release.

WHO THIS RUNS AS. Either root, or the `blankee` service user the installer
creates. Nine of the twelve steps need only ownership of the tree, the venv and
the WSGI file, and the service user has exactly that. The three that touch the
system - re-applying permissions, rewriting the units, refreshing the Apache
directives - and the Apache restart the reload falls back to, go through
helper(): in-process when this is root, and otherwise as a request file that a
root-owned .path unit picks up and answers, each helper allowed to write one
directory. That is the same shape as the web-to-updater hand-off, one level up.
"""

import argparse
import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
import re
import subprocess
import sys
import tempfile
import time

APP_DIR = os.environ.get('BLANKEE_APP_DIR', '/opt/blankee')
CONFIG_DIR = os.environ.get('BLANKEE_CONFIG_DIR', '/var/www/budget_env')
CONFIG_FILE = os.environ.get('BLANKEE_CONFIG', os.path.join(CONFIG_DIR, 'blankee.conf'))
VENV_DIR = os.environ.get('BLANKEE_VENV', os.path.join(CONFIG_DIR, 'venv'))
WSGI_FILE = os.environ.get('BLANKEE_WSGI', os.path.join(CONFIG_DIR, 'blankee.wsgi'))
DB_CONF = os.environ.get('BLANKEE_DB_CONF', '/etc/blankee/db.conf')
STATUS_FILE = os.environ.get('BLANKEE_UPDATE_STATUS',
                             os.path.join(CONFIG_DIR, 'update-status.json'))
LOCK_FILE = os.path.join(CONFIG_DIR, '.update.lock')

# The public key releases are signed with, in ssh allowed-signers format. From
# the unit's environment or this default - NEVER from blankee.conf. That file is
# owned by the web user, and a web process that can name the trust anchor can
# blank the name (no verification) or point it at a key it wrote itself. The
# installer pins this file once and neither it nor the updater ever rewrites it.
SIGNERS_FILE = os.environ.get('BLANKEE_SIGNERS', '/etc/blankee/allowed_signers')

# Where a non-root updater leaves requests for the root helpers, and where the
# helpers leave their answers. A tmpfiles.d entry keeps it 0770 root:blankee.
HELPER_DIR = os.environ.get('BLANKEE_HELPER_DIR', '/run/blankee-update')

# The service user. Anything else that is not root is refused in main().
SERVICE_USER = 'blankee'

BRANCH = 'main'
# What a release tag looks like. Anchored on a digit so a branch-shaped name
# like `verify-me` cannot be mistaken for one, and listed with --sort=-v:refname
# so 1.10.0 sorts above 1.9.0 rather than below it.
RELEASE_TAG_GLOB = 'v[0-9]*'
STATUS_SCHEMA = 1
STALE_AFTER = 15 * 60

# A config file is a few hundred bytes. Capping the read before parsing means a
# web process that has been made to write a huge file cannot turn this into a
# memory or disk problem on the host.
MAX_CONFIG_BYTES = 64 * 1024

TRUTHY = ('1', 'true', 'yes', 'on')


# The file mod_wsgi sends the application's own stderr to. Writing here as well
# as to the journal puts an update in the log an operator already reads, instead
# of in a second place they have to know about. Overridable for a layout that is
# not Debian's; on a container there is no Apache and the path will not exist.
# 1.4.0 moved the logs to /var/log/blankee; the unit passes the path in, and
# this default is for a shell run.
APP_LOG = os.environ.get('BLANKEE_APP_LOG', '/var/log/blankee/blankee_error.log')


def _app_log(message, level):
    """
    One line in the shape the log viewer parses: an Apache-style prefix with a
    JSON object at end of line (its reader takes the last {...} on the line and
    ignores whatever came before).

    Appends only to a file that already exists - creating it would have root
    guessing an owner and mode for a file Apache manages, and would litter hosts
    that have no Apache at all. Never raises: logging is not worth failing an
    update over.
    """
    try:
        if not os.path.exists(APP_LOG):
            return
        t = time.time()
        us = int((t % 1) * 1000000)
        prefix = '[%s.%06d %s] [blankee-update] [pid %d]' % (
            time.strftime('%a %b %d %H:%M:%S', time.localtime(t)), us,
            time.strftime('%Y', time.localtime(t)), os.getpid())
        entry = {
            'timestamp': '%s.%03dZ' % (time.strftime('%Y-%m-%dT%H:%M:%S',
                                                     time.gmtime(t)), us // 1000),
            'level': level,
            'logger': 'blankee_update',
            'module': 'blankee_update',
            'function': 'say',
            'line': 0,
            'tag': 'UPDATE',
            'endpoint': None,
            'user_id': None,
            'request_id': _status.get('request_id'),
            'message': message,
        }
        with open(APP_LOG, 'a', encoding='utf-8') as f:
            print(prefix, json.dumps(entry, ensure_ascii=False), file=f)
    except Exception:
        pass


def say(message, level='INFO'):
    """
    To stdout, which systemd captures - journalctl -u blankee-update is the log -
    and to the application log, so an update is visible from either.
    """
    print(message, flush=True)
    _app_log(message, level)


# ---------------------------------------------------------------- the flag

def read_flag():
    """
    (requested, request_id, auto), read defensively.

    This file is written by the web process, so it is treated as hostile input
    however it is meant to be used:

      O_NOFOLLOW  refuses to follow a symlink. The web user cannot currently
                  replace this file - the installer keeps CONFIG_DIR at 750, so
                  it cannot unlink entries there - but this does not depend on
                  that remaining true.
      fstat       must be a regular file, owned by root or by the web user.
      size cap    read a bounded amount, before parsing.
      few keys    three keys are recognised, and each flag must be 0 or 1.
                  Everything else in the file is ignored.
    """
    try:
        fd = os.open(CONFIG_FILE, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        if e.errno in (errno.ENOENT, errno.ELOOP):
            return (False, None, False)
        say(f'  cannot open {CONFIG_FILE}: {e}')
        return (False, None, False)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            say(f'  {CONFIG_FILE} is not a regular file; ignoring it')
            return (False, None, False)
        raw = os.read(fd, MAX_CONFIG_BYTES)
    finally:
        os.close(fd)

    requested, request_id, auto = False, None, False
    for line in raw.decode('utf-8', 'replace').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key, value = key.strip().upper(), value.strip()
        if key == 'UPDATE_REQUESTED':
            if value not in ('0', '1') and value.lower() not in TRUTHY:
                say(f'  UPDATE_REQUESTED is not 0 or 1; treating as off')
                continue
            requested = value.lower() in TRUTHY
        elif key == 'AUTO_UPDATE':
            if value.lower() in TRUTHY:
                auto = True
        elif key == 'UPDATE_REQUEST_ID':
            # Opaque, and only ever echoed back into the status file. Bounded
            # and stripped of anything that is not plausibly an id.
            cleaned = ''.join(c for c in value if c.isalnum() or c in '-_')[:64]
            request_id = cleaned or None
    return (requested, request_id, auto)


def clear_flag():
    """Set UPDATE_REQUESTED=0 in place, before any work, so a crash cannot loop."""
    try:
        with open(CONFIG_FILE, 'r+', encoding='utf-8') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            lines = f.readlines()
            out, seen = [], False
            for line in lines:
                stripped = line.strip()
                if (not stripped.startswith('#') and '=' in stripped
                        and stripped.partition('=')[0].strip().upper() == 'UPDATE_REQUESTED'):
                    out.append('UPDATE_REQUESTED=0\n')
                    seen = True
                else:
                    out.append(line)
            if not seen:
                out.append('UPDATE_REQUESTED=0\n')
            f.seek(0)
            f.writelines(out)
            f.truncate()
            f.flush()
            os.fsync(f.fileno())
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return True
    except Exception as e:
        say(f'  could not clear UPDATE_REQUESTED: {e}')
        return False


# ---------------------------------------------------------------- status

_status = {}


def status_init(request_id, forced):
    global _status
    _status = {
        'schema': STATUS_SCHEMA,
        'request_id': request_id,
        'forced': bool(forced),
        'phase': 'starting',
        'ok': None,
        'started_at': now(),
        'updated_at': now(),
        'finished_at': None,
        'from': {}, 'to': {},
        'steps': [],
        'message': 'Starting.',
        'detail': '',
        'recovery': [],
        'log_command': 'journalctl -u blankee-update -n 200 --no-pager',
        'updater_pid': os.getpid(),
    }
    status_write()


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def status_write():
    """
    Atomically, because the web process reads this file and a half-written one
    would show up as a parse failure rather than as progress.

    Root owns it; the web user's group may read it. 640 rather than 644 - it
    carries paths and git output, which is nobody else's business.
    """
    _status['updated_at'] = now()
    try:
        directory = os.path.dirname(STATUS_FILE)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix='.update-status-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(_status, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o640)
            # Readable by the web user's group either way. As root the file is
            # handed to root:www-data as before; as the service user it stays
            # blankee-owned and only the group is set, which is all the web
            # tier needs to read it.
            try:
                import grp
                gid = grp.getgrnam('www-data').gr_gid
                os.chown(tmp, 0 if os.geteuid() == 0 else -1, gid)
            except Exception:
                pass
            os.replace(tmp, STATUS_FILE)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    except Exception as e:
        say(f'  could not write {STATUS_FILE}: {e}')


def step(name, message):
    """Record a step as started. Written before the work, so a crash names it."""
    _status['phase'] = name
    _status['message'] = message
    _status['steps'].append({'name': name, 'ok': None, 'at': now(), 'detail': ''})
    status_write()
    say(f'==> {message}')


def step_done(detail='', ok=True):
    if _status['steps']:
        _status['steps'][-1]['ok'] = ok
        _status['steps'][-1]['detail'] = detail[-2000:]
    status_write()


def finish_ok(message):
    _status.update(phase='done', ok=True, message=message, finished_at=now())
    status_write()
    say(f'==> {message}')
    return 0


def finish_failed(message, detail='', recovery=None):
    if _status['steps'] and _status['steps'][-1]['ok'] is None:
        _status['steps'][-1]['ok'] = False
        _status['steps'][-1]['detail'] = detail[-2000:]
    _status.update(phase='failed', ok=False, message=message,
                   detail=detail[-4000:], finished_at=now(),
                   recovery=recovery or [])
    status_write()
    say(f'FAILED: {message}', 'ERROR')
    if detail:
        say(detail[-2000:], 'ERROR')
    return 1


# ---------------------------------------------------------------- helpers

def run(argv, cwd=None, env=None, timeout=600):
    """(returncode, combined_output). Everything is echoed to the journal."""
    child_env = dict(os.environ)
    child_env.update({
        'GIT_TERMINAL_PROMPT': '0',
        'GIT_ASKPASS': '/bin/true',
        'GIT_SSH_COMMAND': 'ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new',
        'DEBIAN_FRONTEND': 'noninteractive',
    })
    if env:
        child_env.update(env)
    say('    $ ' + ' '.join(argv))
    try:
        proc = subprocess.run(argv, cwd=cwd, env=child_env, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True)
    except subprocess.TimeoutExpired:
        return (124, f'timed out after {timeout}s')
    except Exception as e:
        return (127, str(e))
    output = (proc.stdout or '').strip()
    for line in output.splitlines()[-40:]:
        say('      ' + line)
    return (proc.returncode, output)


_git_global = None


def git_env():
    """
    Environment for git: our own "global" config and no system one.

    The tree belongs to the service user, and an operator at a root shell still
    runs this; git refuses that mismatch ("dubious ownership") unless the
    directory is named in safe.directory - and it honours that key ONLY from
    the global or system scope, never from -c or the repository's own config.
    So a one-line global config is written to a private temp file and handed
    to git through GIT_CONFIG_GLOBAL. Trusting this one directory is safe ONLY
    because owned_safely() has already applied a stricter rule than git's -
    every ancestor trusted, none of it web-writable - and nothing here runs git
    before that check. GIT_CONFIG_NOSYSTEM keeps /etc/gitconfig out of it too.
    """
    global _git_global
    if _git_global is None:
        fd, path = tempfile.mkstemp(prefix='blankee-git-', suffix='.gitconfig')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write('[safe]' + chr(10) + chr(9) + 'directory = ' + APP_DIR + chr(10))
        _git_global = path
    env = dict(os.environ)
    env['GIT_CONFIG_GLOBAL'] = _git_global
    env['GIT_CONFIG_NOSYSTEM'] = '1'
    return env


def git(*args, timeout=600):
    argv = ['git',
            # A repository whose hooks or config the web user could write would
            # be code execution for whoever runs git in it. Ownership is checked
            # in the preflight; this makes the hooks inert regardless.
            '-c', 'core.hooksPath=/dev/null',
            '-c', 'core.fsmonitor=false',
            '-C', APP_DIR] + list(args)
    return run(argv, env=git_env(), timeout=timeout)


def read_kv(path, keys):
    """Selected keys from a KEY=VALUE file. Never executes anything."""
    found = {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f.read(MAX_CONFIG_BYTES).splitlines():
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, value = line.partition('=')
                key = key.strip()
                if key in keys:
                    found[key] = value.strip()
    except Exception as e:
        say(f'  could not read {path}: {e}')
    return found


def web_ids():
    """(uid, gid) of the web user, or (None, None) where there is none."""
    try:
        import pwd
        entry = pwd.getpwnam('www-data')
        return (entry.pw_uid, entry.pw_gid)
    except Exception:
        return (None, None)


def service_uid():
    """uid of the service user, or None before the installer has created it."""
    try:
        import pwd
        return pwd.getpwnam(SERVICE_USER).pw_uid
    except Exception:
        return None


def owned_safely(path):
    """
    True when path and every directory above it are owned by root, by the
    service user or by the user running this, and none of them can be written
    by the web user.

    This used to demand root all the way up, and the reason was never root as
    such: it was that a .git the web user could write is code execution for
    whoever runs git in it, through hooks or a rewritten config. The service
    user owning the tree keeps that guarantee - what must not own it, or be able
    to write it, is www-data. So that is what is checked. Never work around a
    failure here with safe.directory; fix the ownership.
    """
    trusted = {0, os.geteuid()}
    if service_uid() is not None:
        trusted.add(service_uid())
    web_uid, web_gid = web_ids()
    current = os.path.abspath(path)
    while True:
        try:
            info = os.stat(current)
        except OSError as e:
            return (False, f'{current} ({e})')
        if info.st_uid not in trusted:
            return (False, f'{current} is owned by uid {info.st_uid}')
        if web_uid is not None and info.st_uid == web_uid:
            return (False, f'{current} is owned by the web user')
        if info.st_mode & stat.S_IWOTH:
            return (False, f'{current} is world-writable')
        if (info.st_mode & stat.S_IWGRP) and web_gid is not None and info.st_gid == web_gid:
            return (False, f'{current} is writable by the web group')
        parent = os.path.dirname(current)
        if parent == current:
            return (True, None)
        current = parent


def file_hash(path):
    try:
        with open(path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return None


# ---------------------------------------------------------------- the update

def read_version_file():
    try:
        with open(os.path.join(APP_DIR, 'VERSION'), 'r', encoding='utf-8') as f:
            return f.readline().strip() or None
    except Exception:
        return None


def credentials_path():
    """
    Where the DB credentials are this run.

    Root reads /etc/blankee/db.conf directly. The service user cannot - the file
    stays root-only on disk - so the unit hands it over with LoadCredential=,
    which places a copy under $CREDENTIALS_DIRECTORY readable by this process
    alone for exactly as long as it runs. Either way the source is the same
    root-written file, and never .env.
    """
    cred_dir = os.environ.get('CREDENTIALS_DIRECTORY')
    if cred_dir:
        handed = os.path.join(cred_dir, 'db.conf')
        if os.path.isfile(handed):
            return handed
    return DB_CONF


def db_env():
    """DB_* from the root-only credential file, never from .env."""
    wanted = ('DB_HOST', 'DB_USER', 'DB_PASSWORD', 'DB_NAME')
    found = read_kv(credentials_path(), wanted)
    missing = [k for k in wanted if not found.get(k)]
    return (found, missing)


def wait_for_site(seconds=60):
    """
    Poll the local site until it answers. (ok, last_status).

    urllib rather than curl, so that "I could not check" never depends on a
    package being installed.
    """
    import urllib.error
    import urllib.request

    # The port comes from the unit (the installer reads it off the vhost it
    # wrote), because .env is the web user's and a non-root updater cannot
    # read it; the .env read is kept for a shell run with no unit behind it,
    # and 18420 is the installer's default when neither says.
    port, host = '18420', None
    app_url = ''
    if os.environ.get('BLANKEE_HTTP_PORT'):
        port = os.environ['BLANKEE_HTTP_PORT']
    else:
        env = read_kv(os.path.join(CONFIG_DIR, '.env'), ('APP_URL',))
        app_url = env.get('APP_URL', '')
    if '://' in app_url:
        rest = app_url.split('://', 1)[1].split('/', 1)[0]
        if ':' in rest:
            host, port = rest.rsplit(':', 1)
        else:
            host = rest
    url = f'http://127.0.0.1:{port}/register'

    deadline = time.time() + seconds
    last = 'no response'
    while time.time() < deadline:
        request = urllib.request.Request(url, method='GET')
        if host:
            request.add_header('Host', host)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return (True, response.status)
        except urllib.error.HTTPError as e:
            # A redirect or a 404 still means the application answered.
            if e.code < 500:
                return (True, e.code)
            last = e.code
        except Exception as e:
            last = str(e)[:80]
        time.sleep(2)
    return (False, last)


def helper(name, verb='run', timeout=300):
    """
    Run one of the three things that still need root: `permissions`, `units`,
    `apache` (verb `run` refreshes the directives; `restart` restarts Apache).

    As root, exactly what this script always did: install.sh in-process. As the
    service user it cannot, and polkit is not assumed, so it asks the way the
    web tier asks it - a request file, watched by a root-owned .path unit that
    starts blankee-update-<name>.service. That unit runs install.sh --helper
    <name> under ProtectSystem=strict with one directory writable, reads the
    verb, deletes the request, and leaves <name>.result: the exit code on the
    first line, the output after it. (returncode, output), like run().

    Polled rather than waited on with inotify: the helper is a separate process
    under systemd's control, and a timeout is the honest outcome when it never
    appears - which is what an un-enabled .path unit looks like from here.
    """
    if os.geteuid() == 0:
        if name == 'apache' and verb == 'restart':
            rc, out = run(['apache2ctl', 'configtest'], timeout=60)
            if rc != 0:
                return (rc, out)
            return run(['systemctl', 'restart', 'apache2'], timeout=180)
        mode = {'permissions': '--permissions-only', 'units': '--units-only',
                'apache': '--apache-conf'}[name]
        return run(['bash', os.path.join(APP_DIR, 'install', 'install.sh'), mode],
                   timeout=timeout)

    request = os.path.join(HELPER_DIR, name + '.request')
    result = os.path.join(HELPER_DIR, name + '.result')
    try:
        os.unlink(result)
    except OSError:
        pass
    try:
        fd, tmp = tempfile.mkstemp(dir=HELPER_DIR, prefix='.' + name + '-')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(verb + chr(10))
        # Two of the helpers run as root with an EMPTY capability set, which
        # means root bound by file modes like anyone else: a 660 file owned by
        # the service user is unreadable to them. 664 inside a directory only
        # root and the service user can enter gives away nothing.
        os.chmod(tmp, 0o664)
        os.replace(tmp, request)
    except Exception as e:
        return (127, f'could not ask the {name} helper: {e}')
    say(f'    -> asked blankee-update-{name} ({verb})')

    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.isfile(result):
            time.sleep(0.2)     # let the helper finish its rename
            try:
                with open(result, 'r', encoding='utf-8') as f:
                    text = f.read()
                os.unlink(result)
            except Exception as e:
                return (127, f'could not read the {name} helper result: {e}')
            first, _, rest = text.partition(chr(10))
            try:
                rc = int(first.strip())
            except ValueError:
                rc = 127
            for line in rest.strip().splitlines()[-40:]:
                say('      ' + line)
            return (rc, rest.strip())
        time.sleep(1)
    return (124, f'the {name} helper did not answer within {timeout}s; '
                 f'is blankee-update-{name}.path enabled?')


def reload_app():
    """
    Pick up the new code without restarting Apache.

    mod_wsgi runs this application in daemon mode, where touching the WSGI
    script file restarts the daemon process group and re-imports everything. No
    listening socket closes and no connection is reset, so the console's own
    polling survives it - which a systemctl restart would not.

    Returns (ok, detail), escalating only if the touch does not take.
    """
    detail = []
    try:
        os.utime(WSGI_FILE, None)
        detail.append(f'touched {WSGI_FILE}')
    except Exception as e:
        detail.append(f'could not touch {WSGI_FILE}: {e}')

    ok, code = wait_for_site()
    if ok:
        detail.append(f'site answered {code}')
        return (True, '; '.join(detail))

    detail.append(f'no answer after the touch (last: {code})')
    # configtest then restart, through the apache helper so it needs no root
    # here. A failed configtest comes back as the helper's exit code: the code
    # on disk is new and the running process is old, a genuinely mixed state.
    rc, out = helper('apache', verb='restart', timeout=240)
    detail.append(f'restart rc={rc}')
    if rc != 0:
        return (False, '; '.join(detail))
    ok, code = wait_for_site()
    detail.append(f'after restart: {code}')
    return (ok, '; '.join(detail))


AVAILABLE_FILE = os.environ.get('BLANKEE_UPDATE_AVAILABLE',
                                os.path.join(CONFIG_DIR, 'update-available.json'))


def write_available(record):
    """
    Record whether an update is waiting, for the application to read.

    Separate from the status file on purpose: that one describes a run that
    happened, this one describes the world. Conflating them would mean a
    successful update erasing the knowledge that a newer one exists.
    """
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(AVAILABLE_FILE),
                                   prefix='.update-available-')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(record, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, AVAILABLE_FILE)
    except Exception as e:
        say(f'  could not write {AVAILABLE_FILE}: {e}')


def _signers_status():
    """
    Whether the pinned key file can be trusted, without deciding what to do
    about it: ('ok', None), ('absent', why) when no key is pinned at all, or
    ('bad', why) when the file exists but is not a trust anchor.

    Split out of verify_release because working out which tag to follow needs
    the same answer before any of the step machinery is running, and a check
    that can only fail a run cannot be reused by one that only reports.
    """
    try:
        fd = os.open(SIGNERS_FILE, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        if e.errno == errno.ENOENT:
            return 'absent', f'no signing key pinned at {SIGNERS_FILE}'
        if e.errno == errno.ELOOP:
            # Only root can put a symlink there, and the installer never does;
            # whatever it points at is not the pinned file. Not "absent".
            return 'bad', f'{SIGNERS_FILE} is a symlink.'
        return 'bad', f'Could not open {SIGNERS_FILE}: {e}'
    try:
        info = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(info.st_mode):
        return 'bad', f'{SIGNERS_FILE} is not a regular file.'
    if info.st_uid != 0 or (info.st_mode & 0o022):
        return 'bad', (f'{SIGNERS_FILE} must be owned by root and writable by root '
                       f'alone (it is uid {info.st_uid}, mode '
                       f'{oct(info.st_mode & 0o777)}). A key file anyone else can '
                       f'write is no trust anchor.')
    return 'ok', None


def tag_signed(tag):
    """
    Whether an annotated tag verifies against the pinned key. Quiet on purpose:
    resolve_release tries candidates in turn, and one that does not verify is a
    tag to skip rather than a failure to report.
    """
    rc, _out = git('-c', 'gpg.ssh.allowedSignersFile=' + SIGNERS_FILE,
                   'verify-tag', '--raw', tag, timeout=60)
    return rc == 0


def resolve_release(require_signature=True):
    """
    (tag, commit) of the newest release an installation should be on, or
    (None, None) when the remote carries none.

    A release is a TAG, not the tip of the branch. Development happens on the
    release branch in the open, so a commit landing there is somebody's change
    being accepted - and following the tip would turn every merge into a release
    on every installation the moment it was pushed. The tag is where the
    maintainer says "this one", and the signature on it is what makes that
    statement checkable by a machine nobody is watching.

    Three things disqualify a candidate, cheapest check first: a pre-release
    name, because an -rc is opt-in and never handed to everyone; a commit that
    is not an ancestor of the release branch, because a tag pushed onto an
    unmerged branch is not a release however well signed; and a signature that
    does not verify against the pinned key.
    """
    rc, out = git('tag', '--list', RELEASE_TAG_GLOB, '--sort=-v:refname')
    if rc != 0:
        return None, None
    for tag in (line.strip() for line in out.splitlines()):
        if not tag or '-' in tag:
            continue
        rc, sha = git('rev-parse', tag + '^{commit}')
        if rc != 0:
            continue
        sha = sha.strip().splitlines()[-1].strip()
        rc, _out = git('merge-base', '--is-ancestor', sha, f'origin/{BRANCH}')
        if rc != 0:
            say(f'  ignoring {tag}: it is not on origin/{BRANCH}')
            continue
        if require_signature and not tag_signed(tag):
            say(f'  ignoring {tag}: not signed by a trusted key')
            continue
        return tag, sha
    return None, None


def check_only():
    """
    Fetch and compare, changing nothing else.

    What the nightly timer does when automatic updates are off: an operator who
    has not opted into unattended updates should still be told that one exists.
    Nothing is installed, nothing is reloaded, and the working tree is not
    touched - `git fetch` only writes to .git.
    """
    # Tags as well as the branch: a release is a tag now (see resolve_release),
    # and --prune-tags drops a local tag the remote no longer has, so a tag
    # planted locally cannot win a comparison.
    rc, out = git('fetch', '--prune', '--prune-tags', '--tags', 'origin', BRANCH, timeout=300)
    if rc != 0:
        say(f'  could not reach the remote: {out[-200:]}')
        return 1

    state, why = _signers_status()
    if state == 'bad':
        say(f'  {why}')
        return 1
    if state == 'absent':
        say(f'  {why}; reading the newest tag unverified')
    tag, target = resolve_release(require_signature=(state == 'ok'))

    rc, current = git('rev-parse', 'HEAD')
    current = current.strip().splitlines()[-1].strip()

    if not target:
        say('  the remote carries no signed release tag yet')
        write_available({
            'available': False,
            'checked_at': now(),
            'from_commit': current, 'from_short': current[:7],
            'from_version': read_version_file(),
            'to_commit': None, 'to_short': None, 'to_tag': None, 'to_version': None,
        })
        return 0

    # Already on it, or past it: an installation someone has moved forward by
    # hand is not offered a way backwards.
    rc, _out = git('merge-base', '--is-ancestor', target, current)
    available = target != current and rc != 0

    version = None
    rc, out = git('show', f'{tag}:VERSION')
    if rc == 0 and out.strip():
        candidate = out.strip().splitlines()[-1].strip()
        if re.fullmatch(r'\d+\.\d+\.\d+(-rc\.\d+)?', candidate):
            version = candidate

    write_available({
        'available': available,
        'checked_at': now(),
        'from_commit': current, 'from_short': current[:7],
        'from_version': read_version_file(),
        'to_commit': target if available else None,
        'to_short': target[:7] if available else None,
        'to_tag': tag if available else None,
        'to_version': version if available else None,
    })
    say(f'  {"an update is available: " + tag if available else "already up to date"}')
    return 0


def verify_release(tag):
    """
    Refuse a release tag not signed by the pinned key. None, or a failure.

    Nothing about a git fetch says who wrote what it fetched. HTTPS proves the
    server is github.com and no more; whoever can push to the release branch can
    run code on every installation that updates, because this script runs
    install.sh out of the checkout it just made. The control for that is a
    signature, and it is only a control if the KEY cannot be replaced by the
    same push - which is why the key file is pinned by the installer, never
    rewritten, and why its LOCATION comes from the unit's environment rather
    than from a file the web user owns.

    The TAG is what gets verified, not the commit it points at. Commits on the
    branch are signed by whoever pushed them - a contributor, or GitHub itself
    on a squash merge - and none of those signatures says "this is a release".
    The tag is the maintainer's own statement, made with the key installations
    pin.

    The key file must itself be root's and not group- or world-writable. A
    signers file the web user could rewrite is the same hole by another door -
    checked in _signers_status, which the tag resolution above uses too.

    No key pinned at all (an installation upgraded in place from before signing
    existed): this run proceeds unverified and says so, and the units step
    seeds the key from install/allowed_signers, so the NEXT run verifies. That
    converges every installation without a human on any of them; refusing here
    instead would strand exactly the installations that most need to update.
    """
    state, why = _signers_status()
    if state == 'absent':
        say(f'  {why}; this update is unverified and will seed one for the next')
        return None
    if state == 'bad':
        return finish_failed(f'{why} Refusing to update unverified.', '',
                             [f'ls -l {SIGNERS_FILE}'])

    step('verify', f'Verifying the signature on {tag}')
    rc, out = git('-c', 'gpg.ssh.allowedSignersFile=' + SIGNERS_FILE,
                  'verify-tag', '--raw', tag, timeout=60)
    if rc != 0:
        return finish_failed(
            f'The signature on {tag} did not verify. Nothing was '
            'checked out and the deployment is unchanged. Either the release '
            'is not signed by a trusted key, or it is not what it claims to '
            'be.', out,
            [f'cd {APP_DIR}', f'git verify-tag {tag}'])
    step_done('signed by a trusted key')
    return None


def preflight(creds, missing):
    """Everything that must be true before anything is changed. None, or a failure."""
    problems = []
    if not os.path.isdir(os.path.join(APP_DIR, '.git')):
        problems.append(f'{APP_DIR} is not a git checkout')
    ok, offender = owned_safely(os.path.join(APP_DIR, '.git'))
    if not ok:
        # A .git the web user owns or can write is code execution for whoever
        # runs git in it, through hooks or a rewritten config. Never work around
        # this with safe.directory - see owned_safely.
        problems.append(f'{offender}; re-run install/install.sh')
    if missing:
        problems.append(f'{DB_CONF} is missing {", ".join(missing)}')
    try:
        free = shutil.disk_usage(APP_DIR).free // (1024 * 1024)
        if free < 500:
            problems.append(f'only {free}MB free on {APP_DIR}')
    except Exception as e:
        problems.append(f'could not check free space: {e}')

    if problems:
        return finish_failed('The deployment is not in a state to update.',
                             '; '.join(problems),
                             [f'cd {APP_DIR}', 'sudo ./install/install.sh'])

    rc, dirty = git('status', '--porcelain')
    if rc != 0:
        return finish_failed('Could not read the git status.', dirty,
                             [f'cd {APP_DIR}', 'git status'])
    if dirty.strip():
        # git reset --hard would delete these without asking. There is
        # deliberately no override, and none is offered in the web interface.
        return finish_failed(
            'There are local modifications, so nothing was changed.', dirty,
            [f'cd {APP_DIR}', 'git status',
             '# commit, stash or discard them, then try again'])
    step_done('working tree is clean')
    return None


def do_update(dry_run):
    creds, missing = db_env()

    step('preflight', 'Checking the deployment')
    failure = preflight(creds, missing)
    if failure is not None:
        return failure

    step('schema-check', 'Verifying the current schema before changing anything')
    rc, out = run([os.path.join(VENV_DIR, 'bin', 'python'),
                   os.path.join(APP_DIR, 'install', 'migrate.py'), '--verify-only'],
                  cwd=APP_DIR, env=creds, timeout=300)
    if rc != 0:
        # Migrating on top of a schema that is already wrong turns one problem
        # into two, and the second is harder to see.
        return finish_failed(
            'The current schema does not verify, so no update was applied.', out,
            [f'cd {APP_DIR}',
             f'sudo {VENV_DIR}/bin/python install/migrate.py --verify-only'])
    step_done((out.strip().splitlines() or [''])[-1])

    step('fetch', f'Fetching the releases on origin/{BRANCH}')
    rc, out = git('fetch', '--prune', '--prune-tags', '--tags', 'origin', BRANCH, timeout=300)
    if rc != 0:
        return finish_failed('Could not reach the remote.', out,
                             [f'cd {APP_DIR}', f'sudo git fetch --tags origin {BRANCH}'])
    state, why = _signers_status()
    if state == 'bad':
        return finish_failed(f'{why} Refusing to update unverified.', '',
                             [f'ls -l {SIGNERS_FILE}'])
    tag, target = resolve_release(require_signature=(state == 'ok'))
    rc, current = git('rev-parse', 'HEAD')
    current = current.strip().splitlines()[-1].strip()
    if not target:
        return finish_ok(f'origin/{BRANCH} carries no signed release tag; nothing to do.')
    _status['from'] = {'commit': current, 'short': current[:7],
                       'version': read_version_file()}
    _status['to'] = {'commit': target, 'short': target[:7], 'tag': tag, 'version': None}
    step_done(f'the newest release is {tag} ({target[:7]}), '
              f'this deployment is {current[:7]}')

    # Ancestor, not equality: an installation that has been moved ahead of the
    # newest tag by hand stays where it is rather than being reset backwards.
    rc, _out = git('merge-base', '--is-ancestor', target, current)
    if target == current or rc == 0:
        return finish_ok(f'Already at {tag}; nothing to do.')
    if dry_run:
        return finish_ok(f'Dry run: would update {current[:7]} to {tag} ({target[:7]}).')

    requirements_before = file_hash(os.path.join(APP_DIR, 'requirements.txt'))

    failure = verify_release(tag)
    if failure is not None:
        return failure

    # Land whatever is still only in Redis before the code changes underneath it.
    #
    # Redis outlives the reload, so this is not about the restart. It is about the
    # migration that may follow - rows written under the old shape are better in
    # MySQL before the shape changes - and about the machine being rolled back or
    # rebooted after a bad update, when the unflushed part is the part nobody can
    # reconstruct. Before the checkout on purpose: the code doing the flushing is
    # then the code that wrote the data.
    #
    # Not fatal. Anything it misses stays in Redis for the normal worker, so a
    # failed flush is worth saying out loud and not worth refusing an update over.
    step('flush', 'Saving pending changes to the database')
    rc, out = run([os.path.join(VENV_DIR, 'bin', 'python'),
                   os.path.join(APP_DIR, 'install', 'flush_pending.py')],
                  cwd=APP_DIR, env=creds, timeout=600)
    if rc == 0:
        step_done((out.strip().splitlines() or [''])[-1])
    else:
        step_done(f'could not flush pending changes (rc={rc}); continuing: '
                  f'{out[-200:]}', ok=False)

    step('checkout', f'Moving to {tag}')
    # No `git clean`: it deletes untracked-but-not-ignored files, and the
    # preflight has already refused a dirty tree, so it could only do harm.
    rc, out = git('reset', '--hard', target)
    if rc != 0:
        return finish_failed('Could not check out the new commit.', out)
    _status['to']['version'] = read_version_file()
    step_done(f"{_status['from'].get('version')} to {_status['to'].get('version')}")

    step('permissions', 'Re-applying ownership and modes')
    # git creates new files with root's umask, so on a host with a restrictive
    # one every added file is unreadable by www-data and the site 500s the moment
    # it reloads. The installer owns these rules; calling it keeps one copy.
    rc, out = helper('permissions', timeout=300)
    if rc != 0:
        return finish_failed('Could not re-apply permissions.', out,
                             [f'sudo {APP_DIR}/install/install.sh --permissions-only'])
    step_done()

    step('units', 'Refreshing the updater units')
    # A release can change a unit file or add one, and until this ran the new
    # file just sat in the repository while the old one stayed installed. That is
    # how 1.1.0 shipped an automatic-update toggle whose nightly timer nobody had
    # installed. Not fatal if it fails: the code is already updated and the site
    # still works, it is the next update that would be affected.
    rc, out = helper('units', timeout=180)
    if rc == 0:
        step_done()
    else:
        step_done(f'could not refresh the units (rc={rc}); '
                  f'run install.sh to fix: {out[-200:]}', ok=False)

    step('apache', 'Refreshing the application Apache directives')
    # A release can need an Apache directive - 1.9.0 needed a Cache-Control
    # header on /static - and before this step there was no way for one to reach
    # an installation that updates through the admin console. The vhost is not
    # touched: it holds ServerName, TLS and anything the operator added, and the
    # updater knows none of that. Only the file install.sh owns is rewritten, and
    # it reverts itself if Apache rejects the result.
    #
    # Not fatal. The code is already updated and the site still works without it;
    # what is lost is a header, not the application.
    rc, out = helper('apache', timeout=180)
    if rc == 0:
        step_done()
    else:
        step_done(f'could not refresh the Apache directives (rc={rc}); '
                  f'run install.sh to fix: {out[-200:]}', ok=False)

    if file_hash(os.path.join(APP_DIR, 'requirements.txt')) != requirements_before:
        step('dependencies', 'Installing changed dependencies')
        rc, out = run([os.path.join(VENV_DIR, 'bin', 'pip'), 'install', '--no-input',
                       '-r', os.path.join(APP_DIR, 'requirements.txt')], timeout=1800)
        if rc != 0:
            return finish_failed(
                'Dependencies could not be installed. The code is updated but the '
                'application was not reloaded, so it is still serving the previous '
                'version.', out,
                [f'cd {APP_DIR}',
                 f'sudo {VENV_DIR}/bin/pip install -r requirements.txt',
                 'sudo systemctl start blankee-update'])
        step_done('requirements.txt changed')
    else:
        step('dependencies', 'Dependencies unchanged, skipping')
        step_done('requirements.txt is identical')

    step('migrate', 'Applying migrations')
    rc, out = run([os.path.join(VENV_DIR, 'bin', 'python'),
                   os.path.join(APP_DIR, 'install', 'migrate.py')],
                  cwd=APP_DIR, env=creds, timeout=1800)
    if rc != 0:
        return finish_failed(
            'Migrations failed. The application was not reloaded, so it is still '
            'serving the previous version.', out,
            [f'cd {APP_DIR}',
             f'sudo {VENV_DIR}/bin/python install/migrate.py',
             'sudo systemctl start blankee-update'])
    step_done((out.strip().splitlines() or [''])[-1])
    step('reload', 'Reloading the application')
    ok, detail = reload_app()
    step_done(detail, ok=ok)
    if not ok:
        return finish_failed(
            f'Updated to {target[:7]}, but the application did not come back. The '
            f'new code is on disk and the old process may still be serving.', detail,
            ['sudo apache2ctl configtest',
             'sudo systemctl restart apache2',
             'sudo tail -50 /var/log/apache2/blankee_error.log'])

    version = _status['to'].get('version') or target[:7]
    # Whatever was waiting has just been installed. Clearing this here rather
    # than waiting for the next nightly check means the console stops offering
    # an update the moment it has been taken.
    write_available({'available': False, 'checked_at': now(),
                     'from_commit': target, 'from_short': target[:7],
                     'from_version': version,
                     'to_commit': None, 'to_short': None, 'to_version': None})
    return finish_ok(f'Updated to {version} ({target[:7]}) and reloaded.')


# ---------------------------------------------------------------- entry point

def mark_aborted():
    """
    ExecStopPost: stamp an unfinished run as failed.

    Without this, a run killed by TimeoutStartSec or the OOM killer leaves the
    status file saying "migrating" forever, and the console spins on it.
    """
    try:
        with open(STATUS_FILE, 'r', encoding='utf-8') as f:
            existing = json.load(f)
    except Exception:
        return 0
    if existing.get('finished_at'):
        return 0
    global _status
    _status = existing
    return finish_failed('The updater stopped before finishing.',
                         'Killed or timed out. The journal has the detail.',
                         ['journalctl -u blankee-update -n 200 --no-pager'])


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--force', action='store_true',
                        help='update even if no request flag is set')
    parser.add_argument('--dry-run', action='store_true',
                        help='check and fetch, change nothing')
    parser.add_argument('--auto', action='store_true',
                        help='run only if AUTO_UPDATE is on (used by the daily timer)')
    parser.add_argument('--mark-aborted', action='store_true',
                        help='stamp an unfinished run as failed (used by systemd)')
    args = parser.parse_args()

    if args.mark_aborted:
        return mark_aborted()

    if os.geteuid() != 0:
        # The service user is the intended one; root is for an operator at a
        # shell. Anyone else - the web user above all - would be replacing
        # code it is not meant to be able to touch.
        try:
            import pwd
            who = pwd.getpwuid(os.geteuid()).pw_name
        except Exception:
            who = str(os.geteuid())
        if who != SERVICE_USER:
            say(f'This must run as root or as the {SERVICE_USER} service user, not {who}.')
            return 2

    # Not the flag, and not systemd's own serialisation: this covers a manual run
    # racing the timer.
    try:
        lock = open(LOCK_FILE, 'w')
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        say('Another update is already running; leaving it alone.')
        return 0
    except Exception as e:
        say(f'Could not take the lock at {LOCK_FILE}: {e}')
        return 1

    try:
        requested, request_id, auto = read_flag()

        if args.auto:
            # The daily timer. With automatic updates off it still checks, and
            # records the answer for the console to show - an operator who has
            # not opted into unattended updates should still be told that one
            # exists. Nothing is installed on that path.
            if not auto:
                say('Nightly check (automatic updates are off).')
                return check_only()
            say('Automatic update (AUTO_UPDATE is on).')
            request_id = f'auto-{int(time.time())}'
            # A request from the console is still honoured; it just gets folded
            # into this run rather than repeating it a minute later.
            if requested:
                clear_flag()
        elif requested:
            say(f'Update requested (id {request_id}).')
            # Cleared before any work, so a crash cannot make it repeat.
            clear_flag()
        elif args.force:
            say('Forced update (no request flag).')
        else:
            # The ordinary case, once a minute, and it must stay cheap and
            # silent. Do not "fix" this into logging something: it would fill
            # the journal with a message meaning nothing happened.
            return 0

        status_init(request_id, forced=args.force and not requested and not args.auto)
        return do_update(args.dry_run)
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
