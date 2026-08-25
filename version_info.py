"""
What this deployment is running.

Reports the application version, and later (see the Updates panel in the admin
console) whether anything newer exists, whether the installed Python packages
match requirements.txt, and whether any database migration is outstanding.

Three rules hold for everything in here, because this module is read during
template rendering and by root-run tooling:

  * The imports at the top are standard library only. A broken third-party
    package must not be able to take the footer - and therefore every page -
    down with it, so anything else is imported inside the one function that
    needs it.
  * Nothing shells out. The web application contains no subprocess use at all
    and that is worth keeping; a git binary invoked from a request handler is a
    new class of problem for no benefit here.
  * Nothing raises. Every public function returns a safe, obviously-empty value
    on failure and logs it.

The version lives in a plain VERSION file at the repository root rather than in
a git tag or a __version__ constant. A tag would mean either shelling out to
`git describe` or parsing refs, and the Docker image has no git binary at all;
a source tarball with no .git still has to report something. One line in one
file is readable by the app, by the installer, and by a person.
"""

import os
import re
import sys

from log_config import get_logger, log_warning

logger = get_logger(__name__)

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(REPO_ROOT, 'VERSION')

# Semantic version, with an optional release-candidate suffix. Anything else is
# treated as no version at all: a footer showing nothing is tidy, whereas one
# showing "vgarbage" is a bug report waiting to happen.
_VERSION_RE = re.compile(r'^\d+\.\d+\.\d+(-rc\.\d+)?$')

# Read once at import. The version can only change when the code changes, and
# new code does not run until the process is reloaded - which re-runs this line.
# So a value cached here cannot go stale in a way a reload would not already
# fix, and re-reading it per render would charge every page a stat to be told
# the same answer. (Contrast _fontawesome_pro_available() in app.py, which is
# deliberately checked per render precisely because it can change with no code
# change at all.)
_version = None


def read_version():
    """
    The version string, e.g. "1.0.0", or '' if it cannot be determined.

    Returns '' rather than a placeholder so callers can treat it as falsey and
    simply omit the version, which is what the footer does.
    """
    global _version
    if _version is not None:
        return _version

    _version = ''
    try:
        with open(VERSION_FILE, 'r', encoding='utf-8') as f:
            candidate = f.readline().strip()
        if _VERSION_RE.match(candidate):
            _version = candidate
        elif candidate:
            log_warning(logger, 'CONFIG',
                        f'{VERSION_FILE} does not contain a version number',
                        found=candidate[:40])
        else:
            log_warning(logger, 'CONFIG', f'{VERSION_FILE} is empty')
    except FileNotFoundError:
        # Expected in a checkout that predates the file, and on any deployment
        # assembled by hand. Not worth an error: the only consequence is that
        # the footer shows no version.
        log_warning(logger, 'CONFIG', f'No {VERSION_FILE}; no version will be shown')
    except Exception as e:
        log_warning(logger, 'CONFIG', f'Could not read {VERSION_FILE}', error=str(e))

    return _version


# ---------------------------------------------------------------- deployment

def _git_dir():
    """
    The .git directory, or None.

    .git is usually a directory but can be a file containing "gitdir: <path>",
    which is how worktrees and some submodule layouts look. Handling both costs
    three lines and avoids reporting "no commit" on a perfectly normal checkout.
    """
    candidate = os.path.join(REPO_ROOT, '.git')
    if os.path.isdir(candidate):
        return candidate
    if os.path.isfile(candidate):
        with open(candidate, 'r', encoding='utf-8') as f:
            head = f.read(4096).strip()
        if head.startswith('gitdir:'):
            path = head.split(':', 1)[1].strip()
            if not os.path.isabs(path):
                path = os.path.normpath(os.path.join(REPO_ROOT, path))
            return path if os.path.isdir(path) else None
    return None


def deployed_commit():
    """
    The commit this deployment is running, as
    {'sha', 'short', 'ref', 'branch', 'source'}, or {} if it cannot be told.

    Read on every call and deliberately not cached. This is the one value that
    can prove a reload actually happened: after an update the new process reads
    the new SHA, and a value cached at import would either report the new commit
    from a process still running the old code, or report the old one forever if
    the reload silently failed.

    Parsed out of .git rather than by running git, because the web application
    contains no subprocess use and this is not worth being the first. Note that
    a fresh `git clone` produces a *packed* repository: refs/heads/main exists
    only in packed-refs until something writes a loose ref. On a server that is
    the normal case, not an edge case.
    """
    env_sha = os.environ.get('BLANKEE_COMMIT', '').strip()
    if re.fullmatch(r'[0-9a-f]{40}', env_sha):
        return {'sha': env_sha, 'short': env_sha[:7], 'ref': None,
                'branch': None, 'source': 'env'}

    try:
        git_dir = _git_dir()
        if not git_dir:
            return {}

        with open(os.path.join(git_dir, 'HEAD'), 'r', encoding='utf-8') as f:
            head = f.read(4096).strip()

        # Detached HEAD: the file is the SHA itself.
        if re.fullmatch(r'[0-9a-f]{40}', head):
            return {'sha': head, 'short': head[:7], 'ref': None,
                    'branch': None, 'source': 'git'}

        if not head.startswith('ref:'):
            return {}
        ref = head.split(':', 1)[1].strip()
        branch = ref.rsplit('/', 1)[-1] if ref.startswith('refs/heads/') else None

        # Loose ref first, then packed-refs.
        loose = os.path.join(git_dir, *ref.split('/'))
        if os.path.isfile(loose):
            with open(loose, 'r', encoding='utf-8') as f:
                sha = f.read(64).strip()
        else:
            sha = ''
            packed = os.path.join(git_dir, 'packed-refs')
            if os.path.isfile(packed):
                with open(packed, 'r', encoding='utf-8') as f:
                    for line in f:
                        if line.startswith(('#', '^')):
                            continue
                        parts = line.split()
                        if len(parts) == 2 and parts[1] == ref:
                            sha = parts[0].strip()
                            break

        if not re.fullmatch(r'[0-9a-f]{40}', sha):
            return {}
        return {'sha': sha, 'short': sha[:7], 'ref': ref,
                'branch': branch, 'source': 'git'}
    except Exception as e:
        log_warning(logger, 'CONFIG', 'Could not read the deployed commit', error=str(e))
        return {}


def remote_slug():
    """
    The origin remote as {'host', 'owner', 'repo', 'url'}, or {}.

    .git/config is scanned with a regex rather than read with configparser.
    Git's config format permits things configparser raises on - valueless keys,
    unquoted subsection names - and the failure would surface as "this
    deployment has no remote", which is both wrong and confusing.
    """
    try:
        git_dir = _git_dir()
        if not git_dir:
            return {}
        path = os.path.join(git_dir, 'config')
        if not os.path.isfile(path):
            return {}
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read(65536)

        block = re.search(
            r'\[remote\s+"origin"\](.*?)(?=^\[|\Z)', text, re.S | re.M)
        if not block:
            return {}
        found = re.search(r'^\s*url\s*=\s*(\S+)', block.group(1), re.M)
        if not found:
            return {}
        url = found.group(1).strip()

        # git@host:owner/repo.git | ssh://git@host/owner/repo | https://host/owner/repo
        m = re.match(r'^(?:https?://|ssh://)?(?:[^@/]+@)?([^/:]+)[/:]+'
                     r'([^/]+)/([^/]+?)(?:\.git)?/?$', url)
        if not m:
            return {'host': None, 'owner': None, 'repo': None, 'url': url}
        return {'host': m.group(1).lower(), 'owner': m.group(2),
                'repo': m.group(3), 'url': url}
    except Exception as e:
        log_warning(logger, 'CONFIG', 'Could not read the git remote', error=str(e))
        return {}


AUTO_TIMER_UNIT = '/etc/systemd/system/blankee-update-auto.timer'


def auto_timer_installed():
    """
    Whether the nightly timer unit actually exists.

    Setting AUTO_UPDATE only records an intention; something has to be running to
    act on it. Updating does not install systemd units on its own before 1.1.1,
    so an instance can have the toggle and no timer - which would silently never
    update. Cheap to check, and the answer is worth showing.
    """
    try:
        return os.path.exists(AUTO_TIMER_UNIT)
    except Exception:
        return False


def install_kind():
    """'docker', 'bare-metal' or 'unknown'."""
    try:
        if os.environ.get('BLANKEE_CONTAINER', '').strip() == '1':
            return 'docker'
        # For images built before BLANKEE_CONTAINER existed. Deliberately not
        # /proc/1/cgroup, which reports differently under Podman, LXC and
        # cgroup v2 and gets this wrong in both directions.
        if os.path.exists('/.dockerenv'):
            return 'docker'
        return 'bare-metal' if os.path.isdir('/etc/apache2') else 'unknown'
    except Exception:
        return 'unknown'


# ---------------------------------------------------------------- dependencies

REQUIREMENTS_FILE = os.path.join(REPO_ROOT, 'requirements.txt')

# Distributions that are legitimately installed without appearing in
# requirements.txt. gunicorn is the one that matters: the Dockerfile installs it
# deliberately, outside requirements.txt, because only the container serves with
# it - so without this every container would report a false alarm.
_UNDECLARED_IGNORE = frozenset({'pip', 'setuptools', 'wheel', 'pkg-resources',
                                'gunicorn', 'mod-wsgi'})


def _normalize(name):
    """PEP 503 name normalisation. 'Flask-Bcrypt' and 'flask_bcrypt' are one package."""
    return re.sub(r'[-_.]+', '-', name).strip().lower()


def _requirements():
    """{normalized_name: pinned_version} plus a list of lines that are not '=='."""
    pinned, unpinned = {}, []
    with open(REQUIREMENTS_FILE, 'r', encoding='utf-8') as f:
        for raw in f:
            line = raw.split('#', 1)[0].strip()
            if not line:
                continue
            m = re.match(r'^([A-Za-z0-9._-]+)\s*==\s*([^\s;]+)$', line)
            if m:
                pinned[_normalize(m.group(1))] = m.group(2)
            else:
                unpinned.append(line)
    return pinned, unpinned


def declared_closure(pinned_names):
    """
    Every distribution the pinned requirements legitimately pull in.

    Requirements gated behind an "extra" are skipped. They are not installed
    unless somebody asks for the extra by name, so counting them would excuse a
    genuinely missing declaration - which is exactly what happened with PyJWT:
    redis declares `pyjwt>=2.9.0; extra == 'jwt'`, we install plain redis, and a
    closure that honoured that marker reported the undeclared import as fine.

    Other markers (python_version and the like) are kept: those describe real
    conditional dependencies.
    """
    import importlib.metadata as md

    closure, queue = set(pinned_names), list(pinned_names)
    while queue:
        current = queue.pop()
        try:
            requirements = md.requires(current) or []
        except Exception:
            # A name that cannot be resolved contributes nothing. Erring towards
            # a larger closure is the safer direction for everything except
            # extras, which is why those are filtered above.
            continue
        for req in requirements:
            marker = req.partition(';')[2]
            if 'extra ==' in marker.replace('"', "'"):
                continue
            dep = _normalize(re.split(r'[\s\[<>=!;(]', req, 1)[0])
            if dep and dep not in closure:
                closure.add(dep)
                queue.append(dep)
    return closure


def top_level_map():
    """
    {top_level_module_name: [distribution names]} for everything installed.

    importlib.metadata.packages_distributions() exists for this, and on
    Python 3.10 it is not enough: it reads top_level.txt, which modern wheels
    built with flit or hatchling no longer ship. Flask 3.x, httpx, redis and
    werkzeug all return nothing from it, so relying on it alone reports half the
    installed packages as unaccounted for - which is worse than not checking,
    because a report full of false alarms gets ignored.

    So: take top_level.txt where it exists, and otherwise derive the names from
    the distribution's own file list.
    """
    import importlib.metadata as md

    mapping = {}

    def add(module, dist_name):
        if module and not module.startswith('_'):
            mapping.setdefault(module, [])
            if dist_name not in mapping[module]:
                mapping[module].append(dist_name)

    for dist in md.distributions():
        name = (dist.metadata or {}).get('Name')
        if not name:
            continue
        declared = dist.read_text('top_level.txt')
        if declared:
            for line in declared.splitlines():
                add(line.strip(), name)
            continue
        for path in (dist.files or []):
            parts = str(path).replace(os.sep, '/').split('/')
            head = parts[0]
            if head.endswith(('.dist-info', '.egg-info', '.data')) or head in ('..',):
                continue
            if len(parts) > 1:
                add(head, name)              # a package directory
            elif head.endswith('.py'):
                add(head[:-3], name)         # a single-module distribution
    return mapping


def dependency_report():
    """
    Whether the installed packages match requirements.txt.

    Every requirement in this project is pinned with '==', so comparison is
    string equality and no version-parsing library is needed.

    'undeclared' is the bucket that earns this function its place: a module that
    has been imported but is not accounted for by requirements.txt or by the
    dependency closure of something in it. That is exactly the shape of the httpx
    bug - imported by push_notifications.py, never declared, so a fresh
    virtualenv could not import the application at all while the dev machine,
    which happened to have it, showed nothing wrong.

    It is a lower bound, and honestly so: it can only see what *this worker
    process* has already imported, and several imports in this codebase happen
    inside request handlers. install/check_requirements.py is the complete
    answer, and runs before a commit rather than on an admin page.
    """
    out = {'ok': True, 'pinned_count': 0, 'missing': [], 'mismatched': [],
           'undeclared': [], 'unpinned': [], 'error': None}
    try:
        import importlib.metadata as md

        pinned, unpinned = _requirements()
        out['pinned_count'] = len(pinned)
        out['unpinned'] = unpinned

        installed = {}
        for dist in md.distributions():
            name = (dist.metadata or {}).get('Name')
            if name:
                installed[_normalize(name)] = dist.version

        for name, want in sorted(pinned.items()):
            have = installed.get(name)
            if have is None:
                out['missing'].append({'name': name, 'required': want})
            elif have != want:
                out['mismatched'].append({'name': name, 'required': want,
                                          'installed': have})

        closure = declared_closure(pinned)

        top_level = top_level_map()
        imported = {m for m in list(sys.modules) if m and '.' not in m}
        stdlib = getattr(sys, 'stdlib_module_names', frozenset())
        for mod in sorted(imported - set(stdlib)):
            for dist_name in top_level.get(mod, []):
                norm = _normalize(dist_name)
                if norm in closure or norm in _UNDECLARED_IGNORE:
                    continue
                entry = {'name': norm, 'installed': installed.get(norm), 'module': mod}
                if entry not in out['undeclared']:
                    out['undeclared'].append(entry)

        out['ok'] = not (out['missing'] or out['mismatched'] or out['undeclared'])
    except Exception as e:
        out['error'] = str(e)
        out['ok'] = False
        log_warning(logger, 'CONFIG', 'Could not compare installed packages', error=str(e))
    return out


# ---------------------------------------------------------------- schema

def _manifest():
    """
    BASELINE, MIGRATIONS and MIGRATIONS_DIR from install/migration_manifest.py.

    Loaded by path rather than imported, because install/ is not a package and
    putting it on sys.path would expose migrate.py to an ordinary `import` -
    which would pull subprocess into the web application.
    """
    import importlib.util

    path = os.path.join(REPO_ROOT, 'install', 'migration_manifest.py')
    spec = importlib.util.spec_from_file_location('blankee_migration_manifest', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def migration_report():
    """
    Whether every migration in the manifest has been applied.

    Four ways this can be interesting, and they mean different things:

      pending       in the manifest, the file is present, not recorded as
                    applied. An update will apply it.
      missing_files in the manifest but absent from install/sql/. A broken
                    checkout; migrate.py would skip it silently.
      unlisted      a .sql file in install/sql/ the manifest does not name. A
                    developer added a file and forgot the list, so nothing will
                    ever apply it.
      unknown       recorded as applied but no such file. The database is ahead
                    of the code - usually a downgrade.

    Only the first two are failures. The other two are warnings about the
    repository rather than about this deployment.
    """
    out = {'ok': True, 'applied_count': 0, 'pending': [], 'missing_files': [],
           'unlisted': [], 'unknown': [], 'error': None}
    try:
        manifest = _manifest()
        expected = [manifest.BASELINE] + list(manifest.MIGRATIONS)
        sql_dir = manifest.MIGRATIONS_DIR

        on_disk = set()
        if os.path.isdir(sql_dir):
            on_disk = {n for n in os.listdir(sql_dir) if n.endswith('.sql')}

        from db_connections import get_db_pool
        with get_db_pool().get_cursor() as cursor:
            cursor.execute("SELECT filename FROM schema_migrations")
            applied = {row[0] for row in cursor.fetchall()}

        out['applied_count'] = len(applied)
        for name in expected:
            if name not in on_disk:
                out['missing_files'].append(name)
            elif name not in applied:
                out['pending'].append(name)
        out['unlisted'] = sorted(on_disk - set(expected))
        out['unknown'] = sorted(applied - set(expected))
        out['ok'] = not (out['pending'] or out['missing_files'])
    except Exception as e:
        message = str(e)
        # 1146 is "table doesn't exist". That is the state of every deployment
        # predating the tracking table, and the answer is a specific instruction
        # rather than a stack trace.
        if '1146' in message or 'schema_migrations' in message:
            out['error'] = ('schema_migrations does not exist yet. Run '
                            'install/migrate.py once to create it.')
        else:
            out['error'] = message
        out['ok'] = False
        log_warning(logger, 'CONFIG', 'Could not read migration state', error=message)
    return out


# ---------------------------------------------------------------- the remote

# Where the privileged updater records what it did. Defined here because the app
# only ever reads it, and this is the module that reads it.
STATUS_FILE = os.environ.get('BLANKEE_UPDATE_STATUS',
                             '/var/www/budget_env/update-status.json')

_GITHUB_HOSTS = ('github.com', 'www.github.com')

AVAILABLE_FILE = os.environ.get('BLANKEE_UPDATE_AVAILABLE',
                                '/var/www/budget_env/update-available.json')


def update_available():
    """
    What the nightly check last found, or None.

    Written by the privileged updater, read here. Deliberately separate from the
    run status: that describes an update that happened, this describes one that
    is waiting. A successful run clears it, so the two cannot disagree for long.

    Returns None rather than a falsey dict when nothing is waiting, so callers
    can write `{% if update_notice %}` without reasoning about the shape.
    """
    try:
        if not os.path.exists(AVAILABLE_FILE):
            return None
        import json
        with open(AVAILABLE_FILE, 'r', encoding='utf-8') as f:
            record = json.load(f)
        if not record.get('available'):
            return None
        return record
    except Exception as e:
        log_warning(logger, 'UPDATE', 'Could not read the update-available file',
                    error=str(e))
        return None


def read_run_status():
    """The updater's last run, or None if it has never run."""
    try:
        if not os.path.exists(STATUS_FILE):
            return None
        import json
        with open(STATUS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        log_warning(logger, 'CONFIG', 'Could not read the update status file', error=str(e))
        return None


def fetch_remote_state(slug, local_sha):
    """
    What the remote's main branch looks like. The only function here that uses
    the network, and it is called only when an administrator presses the button.

    Two independent signals, because neither endpoint is reachable everywhere:

      raw.githubusercontent.com/.../VERSION  no auth, no rate limit, and it
          works on networks where the API does not. Gives the human answer,
          "1.0.0 -> 1.1.0".
      the compare API                        says how many commits main has that
          this deployment does not, which is what actually decides whether an
          update exists - the target is the branch tip, so a commit that did not
          touch VERSION still counts.

    Failure is reported as failure. Answering "up to date" when the check could
    not look is worse than not offering a check at all.
    """
    out = {'checked': True, 'up_to_date': None, 'behind_by': None, 'status': None,
           'latest_sha': None, 'latest_short': None, 'latest_version': None,
           'commits': [], 'signals': [], 'error': None}

    host = (slug or {}).get('host')
    owner, repo = (slug or {}).get('owner'), (slug or {}).get('repo')
    if not owner or not repo:
        out['error'] = 'This deployment has no git remote, so there is nothing to compare against.'
        return out
    if host not in _GITHUB_HOSTS:
        out['error'] = (f'The remote is {host}, not github.com, so the latest commit '
                        f'cannot be read from here. Updating still works.')
        return out

    # Constructing the client is inside the try, not just the import. httpx pulls
    # in httpcore, which imports h2 for HTTP/2 - and an old h2/hyperframe raises
    # AttributeError there rather than ImportError, so importing httpx succeeds
    # and building a client explodes. That happened on a real deployment. If it
    # escaped this function it would take the whole report down, and the
    # dependency check - which is what diagnoses it - would be the first
    # casualty.
    try:
        import httpx
        # Same client shape as push_notifications.py: an explicit timeout, so a
        # blocked or slow network cannot hang the request waiting on this.
        client = httpx.Client(
            timeout=httpx.Timeout(10.0, connect=10.0),
            follow_redirects=True,
            headers={'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28',
                     'User-Agent': f'blankee/{read_version() or "unknown"}'})
    except Exception as e:
        detail = f'{type(e).__name__}: {e}'
        # One cause accounts for essentially all of these, and it has an exact
        # fix, so say it rather than making somebody search for it. An old
        # h2/hyperframe - pulled in by the hyper pin that requirements.txt no
        # longer has - raises AttributeError inside httpcore's HTTP/2 import.
        # pip does not remove what requirements.txt stopped naming, so an
        # upgraded install still has them.
        if 'MutableSet' in detail or 'hyperframe' in detail or 'h2' in detail:
            out['error'] = (
                'The HTTP client could not start because an old h2/hyperframe is '
                'installed. They came from dependencies this application no longer '
                'uses, and pip leaves them behind. On the server: '
                'pip uninstall -y apns2 hyper hyperframe h2')
        else:
            out['error'] = (f'The HTTP client could not be started, so the check could '
                            f'not run ({detail}). install/check_requirements.py usually '
                            f'says why.')
        log_warning(logger, 'UPDATE', 'Could not build an HTTP client', error=detail)
        return out
    errors = []
    not_found = False
    try:
        # Signal 1: the published VERSION.
        try:
            r = client.get(f'https://raw.githubusercontent.com/{owner}/{repo}/main/VERSION')
            if r.status_code == 200:
                candidate = r.text.strip().splitlines()[0].strip() if r.text.strip() else ''
                if _VERSION_RE.match(candidate):
                    out['latest_version'] = candidate
                    out['signals'].append('VERSION')
            elif r.status_code == 404:
                # Either this branch has no VERSION file, or the repository is
                # not visible to an unauthenticated request. Which one it is
                # becomes clear once the compare result is in.
                not_found = True
            else:
                errors.append(f'VERSION lookup returned HTTP {r.status_code}')
        except Exception as e:
            errors.append(f'VERSION lookup failed ({e})')

        # Signal 2: how far behind main this commit is.
        if local_sha:
            try:
                r = client.get(f'https://api.github.com/repos/{owner}/{repo}'
                               f'/compare/{local_sha}...main')
                if r.status_code == 200:
                    data = r.json()
                    # base is the deployed commit and head is main, so ahead_by
                    # is "commits main has that this deployment does not". Not
                    # behind_by, which counts the other direction - the single
                    # easiest thing here to get backwards.
                    out['behind_by'] = data.get('ahead_by')
                    out['status'] = data.get('status')
                    out['up_to_date'] = data.get('status') == 'identical'
                    head = (data.get('commits') or [])[-1:] or []
                    if head:
                        out['latest_sha'] = head[-1].get('sha')
                    for entry in (data.get('commits') or [])[-10:]:
                        message = (entry.get('commit') or {}).get('message') or ''
                        out['commits'].append({
                            'short': (entry.get('sha') or '')[:7],
                            'subject': message.splitlines()[0][:100] if message else '',
                            'date': ((entry.get('commit') or {}).get('committer') or {}).get('date')})
                    out['signals'].append('compare')
                elif r.status_code in (403, 429):
                    reset = r.headers.get('x-ratelimit-reset')
                    remaining = r.headers.get('x-ratelimit-remaining')
                    errors.append(
                        f'GitHub rate limit reached (remaining {remaining}, resets at '
                        f'{reset}). Unauthenticated requests are limited per source '
                        f'address, so a shared connection reaches it sooner.')
                elif r.status_code == 404:
                    # Either the commit is unknown to GitHub - a local build - or
                    # the whole repository is invisible to us. Both give a 404.
                    not_found = True
                    errors.append('GitHub does not know this commit, so the distance '
                                  'from main could not be measured.')
                else:
                    errors.append(f'compare returned HTTP {r.status_code}')
            except Exception as e:
                errors.append(f'compare failed ({e})')
    finally:
        try:
            client.close()
        except Exception:
            pass

    if not out['signals']:
        if not_found:
            # Everything 404s when the repository is private, which is the usual
            # reason: an unauthenticated request cannot see it at all. Saying
            # that is more use than repeating two 404s.
            out['error'] = (
                f'Could not read {owner}/{repo} from GitHub. Both the version file and '
                f'the commit came back "not found", which is what a private repository '
                f'looks like to an unauthenticated request - update checks work on a '
                f'deployment cloned from a public repository.')
        else:
            out['error'] = ' '.join(errors) or 'Nothing could be reached.'
    elif out['up_to_date'] is None and out['latest_version']:
        # No compare, but a VERSION - fall back to comparing versions.
        out['up_to_date'] = out['latest_version'] == read_version()
        if errors:
            out['error'] = ' '.join(errors)
    elif errors:
        out['error'] = ' '.join(errors)
    return out


# ---------------------------------------------------------------- the whole picture

def update_state(check_remote=False):
    """
    Everything the Updates panel shows, in one dict.

    The single blob every route and the template consume, so the page cannot
    show one thing while a handler believes another - the same reason
    get_smtp_config_for_display() exists for email delivery.

    check_remote is False by default and stays False for the initial page render
    and for status polling: an administrator opening the console must not cause
    an outbound request, and nothing here runs on a schedule.
    """
    commit = deployed_commit()
    slug = remote_slug()

    code = {'checked': False, 'up_to_date': None, 'behind_by': None, 'status': None,
            'remote': f"{slug.get('owner')}/{slug.get('repo')}" if slug.get('owner') else None,
            'remote_host': slug.get('host'), 'latest_sha': None, 'latest_short': None,
            'latest_version': None, 'commits': [], 'signals': [], 'error': None}
    if check_remote:
        code.update(fetch_remote_state(slug, commit.get('sha')))
        code['remote'] = f"{slug.get('owner')}/{slug.get('repo')}" if slug.get('owner') else None
        code['remote_host'] = slug.get('host')
        if code.get('latest_sha'):
            code['latest_short'] = code['latest_sha'][:7]

    return {
        'version': read_version(),
        'commit': commit,
        'install': {'kind': install_kind(), 'app_dir': REPO_ROOT,
                    'auto_timer': auto_timer_installed()},
        'code': code,
        'dependencies': dependency_report(),
        'schema': migration_report(),
        'run': read_run_status(),
        'available': update_available(),
    }
