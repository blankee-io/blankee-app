#!/usr/bin/env python3
"""
Apply an update, as root, when the admin console asks for one.

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
would be choosing what root checks out and runs. This script takes nothing from
that file except "yes" and an opaque id.
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

BRANCH = 'main'
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
APP_LOG = os.environ.get('BLANKEE_APP_LOG', '/var/log/apache2/blankee_error.log')


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
            try:
                import grp
                os.chown(tmp, 0, grp.getgrnam('www-data').gr_gid)
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


def git(*args, timeout=600):
    argv = ['git',
            # A repository whose hooks or config the web user could write would
            # be root code execution. Ownership is checked in the preflight; this
            # makes the hooks inert regardless.
            '-c', 'core.hooksPath=/dev/null',
            '-c', 'core.fsmonitor=false',
            '-C', APP_DIR] + list(args)
    return run(argv, timeout=timeout)


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


def owned_by_root(path):
    """True when path and every directory above it are owned by root."""
    current = os.path.abspath(path)
    while True:
        try:
            if os.stat(current).st_uid != 0:
                return (False, current)
        except OSError as e:
            return (False, f'{current} ({e})')
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


def db_env():
    """DB_* from the root-only credential file, never from .env."""
    wanted = ('DB_HOST', 'DB_USER', 'DB_PASSWORD', 'DB_NAME')
    found = read_kv(DB_CONF, wanted)
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

    env = read_kv(os.path.join(CONFIG_DIR, '.env'), ('APP_URL',))
    app_url = env.get('APP_URL', '')
    port, host = '18420', None
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
    rc, out = run(['apache2ctl', 'configtest'], timeout=60)
    detail.append(f'configtest rc={rc}')
    if rc != 0:
        # The code on disk is new and the running process is old. Templates and
        # static files are read from disk, so this is a genuinely mixed state.
        return (False, '; '.join(detail))

    rc, out = run(['systemctl', 'restart', 'apache2'], timeout=180)
    detail.append(f'restart rc={rc}')
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


def check_only():
    """
    Fetch and compare, changing nothing else.

    What the nightly timer does when automatic updates are off: an operator who
    has not opted into unattended updates should still be told that one exists.
    Nothing is installed, nothing is reloaded, and the working tree is not
    touched - `git fetch` only writes to .git.
    """
    rc, out = git('fetch', '--prune', 'origin', BRANCH, timeout=300)
    if rc != 0:
        say(f'  could not reach the remote: {out[-200:]}')
        return 1

    rc, target = git('rev-parse', f'origin/{BRANCH}')
    if rc != 0:
        return 1
    target = target.strip().splitlines()[-1].strip()
    rc, current = git('rev-parse', 'HEAD')
    current = current.strip().splitlines()[-1].strip()

    version = None
    rc, out = git('show', f'origin/{BRANCH}:VERSION')
    if rc == 0 and out.strip():
        candidate = out.strip().splitlines()[-1].strip()
        if re.fullmatch(r'\d+\.\d+\.\d+(-rc\.\d+)?', candidate):
            version = candidate

    available = target != current
    write_available({
        'available': available,
        'checked_at': now(),
        'from_commit': current, 'from_short': current[:7],
        'from_version': read_version_file(),
        'to_commit': target if available else None,
        'to_short': target[:7] if available else None,
        'to_version': version if available else None,
    })
    say(f'  {"an update is available: " + target[:7] if available else "already up to date"}')
    return 0


def preflight(creds, missing):
    """Everything that must be true before anything is changed. None, or a failure."""
    problems = []
    if not os.path.isdir(os.path.join(APP_DIR, '.git')):
        problems.append(f'{APP_DIR} is not a git checkout')
    ok, offender = owned_by_root(os.path.join(APP_DIR, '.git'))
    if not ok:
        # An install predating the root-owned tree has a www-data-owned .git,
        # where root running git executes the web user's code through hooks or a
        # rewritten config. Never work around this with safe.directory.
        problems.append(f'{offender} is not owned by root; re-run install/install.sh')
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

    step('fetch', f'Fetching origin/{BRANCH}')
    rc, out = git('fetch', '--prune', 'origin', BRANCH, timeout=300)
    if rc != 0:
        return finish_failed('Could not reach the remote.', out,
                             [f'cd {APP_DIR}', f'sudo git fetch origin {BRANCH}'])
    rc, target = git('rev-parse', f'origin/{BRANCH}')
    if rc != 0:
        return finish_failed(f'Could not resolve origin/{BRANCH}.', target)
    target = target.strip().splitlines()[-1].strip()
    rc, current = git('rev-parse', 'HEAD')
    current = current.strip().splitlines()[-1].strip()
    _status['from'] = {'commit': current, 'short': current[:7],
                       'version': read_version_file()}
    _status['to'] = {'commit': target, 'short': target[:7], 'version': None}
    step_done(f'origin/{BRANCH} is {target[:7]}, this deployment is {current[:7]}')

    if target == current:
        return finish_ok('Already at the latest commit; nothing to do.')
    if dry_run:
        return finish_ok(f'Dry run: would update {current[:7]} to {target[:7]}.')

    requirements_before = file_hash(os.path.join(APP_DIR, 'requirements.txt'))

    step('checkout', f'Moving to {target[:7]}')
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
    rc, out = run(['bash', os.path.join(APP_DIR, 'install', 'install.sh'),
                   '--permissions-only'], timeout=300)
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
    rc, out = run(['bash', os.path.join(APP_DIR, 'install', 'install.sh'),
                   '--units-only'], timeout=120)
    if rc == 0:
        step_done()
    else:
        step_done(f'could not refresh the units (rc={rc}); '
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
        say('This must run as root: it replaces root-owned code and reloads the '
            'web server.')
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
