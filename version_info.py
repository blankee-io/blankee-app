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
