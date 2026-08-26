"""
Reader for Blankee's own log files, for the admin log viewer at /admin/logs.

The application logs to stderr, so each line reaching the log is an Apache
wrapper around a JSON payload written by log_config.py:

    [Sun Mar 29 22:51:01.037126 2026] [wsgi:error] [pid ...] {"timestamp": ...}

The self-updater writes here too, tagged UPDATE, with its own prefix. Nothing
depends on the prefix: the payload is the last {...} on the line, and that is
what is read.

No Flask, no database, no application imports. This is a file reader, and
keeping it that way is what lets it be tested without standing anything up.
"""
import glob
import json
import os
import re

# Everything after the last brace-delimited object on the line is ignored, and
# so is everything before it. Apache's prefix has changed shape between versions
# and carries an optional [remote ...] field; matching the payload rather than
# the prefix means none of that matters.
_JSON_RE = re.compile(r'\{.*\}\s*$')

# An 8 digit date is how a rotated file names its day - blankee_app_20260329.log
# is written by cron_scripts/rotate_blankee_logs.sh at 23:59.
_DATE_RE = re.compile(r'(\d{8})')

# Where install.sh puts Blankee's logs. Overridable because dev and prod predate
# that layout and still keep them under /var/log/apache2.
LOG_DIR = os.environ.get('BLANKEE_LOG_DIR', '/var/log/blankee')
LOG_CURRENT = os.environ.get('BLANKEE_LOG_CURRENT', 'blankee_error.log')
LOG_ARCHIVES = os.environ.get('BLANKEE_LOG_ARCHIVES', 'blankee_app_*.log')

# Bounds, and they are load-bearing rather than defensive decoration. Archives
# are kept for 180 days, and reading every one of them at 50,000 lines each is
# 9,000,000 lines parsed to render a single page. A year-old instance would time
# out on its own log viewer, which is exactly when someone needs it most.
MAX_LINES_PER_FILE = 50000
MAX_ARCHIVE_FILES = 14


# ------------------------------------------------------------------ discovery

def list_log_files(log_dir, current_file, archive_pattern):
    """
    Available log files, newest first:

        [{"path": ..., "name": ..., "size_bytes": ..., "date_label": "current",
          "mtime": ...}, ...]

    date_label is "current" for the live file and the YYYYMMDD from the name for
    a rotated one.
    """
    files = []

    current_path = os.path.join(log_dir, current_file)
    if os.path.isfile(current_path):
        st = os.stat(current_path)
        files.append({
            'path': current_path,
            'name': current_file,
            'size_bytes': st.st_size,
            'date_label': 'current',
            'mtime': st.st_mtime,
        })

    for path in glob.glob(os.path.join(log_dir, archive_pattern)):
        name = os.path.basename(path)
        try:
            st = os.stat(path)
        except OSError:
            # Rotation runs at 23:59 and can unlink a file between the glob and
            # the stat. One missing archive is not worth failing the page for.
            continue
        m = _DATE_RE.search(name)
        files.append({
            'path': path,
            'name': name,
            'size_bytes': st.st_size,
            'date_label': m.group(1) if m else name,
            'mtime': st.st_mtime,
        })

    files.sort(key=lambda f: f['mtime'], reverse=True)
    return files


def dir_status():
    """
    ('ok' | 'missing' | 'unreadable', explanation) for the configured log
    directory.

    A viewer that renders an empty table when it simply cannot see the files is
    indistinguishable from one reporting that nothing has been logged, and the
    two want completely different responses from whoever is looking.
    """
    if not os.path.isdir(LOG_DIR):
        return 'missing', f'{LOG_DIR} does not exist'
    if not os.access(LOG_DIR, os.R_OK | os.X_OK):
        return 'unreadable', f'{LOG_DIR} is not readable by the web server user'
    return 'ok', ''


# ------------------------------------------------------------------- parsing

def _parse_line(raw_line):
    """The JSON payload of one line, or None if there isn't one."""
    m = _JSON_RE.search(raw_line)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return None


def parse_log_file(path, filters=None, max_lines=MAX_LINES_PER_FILE):
    """Entries from one file that match every filter. Missing file reads as []."""
    if not os.path.isfile(path):
        return []

    entries = []
    try:
        with open(path, 'r', errors='replace') as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                entry = _parse_line(line)
                if entry is None:
                    continue
                if filters and not _matches(entry, filters):
                    continue
                entries.append(entry)
    except OSError:
        # Unreadable is reported by dir_status(), not by a traceback here.
        return []
    return entries


def _matches(entry, filters):
    level = filters.get('level')
    if level and level != 'ALL' and entry.get('level') != level:
        return False

    tag = filters.get('tag')
    if tag and tag != 'ALL' and entry.get('tag') != tag:
        return False

    user_id = filters.get('user_id')
    if user_id is not None and user_id != '':
        try:
            if entry.get('user_id') != int(user_id):
                return False
        except (ValueError, TypeError):
            return False

    endpoint = filters.get('endpoint')
    if endpoint and endpoint not in (entry.get('endpoint') or ''):
        return False

    request_id = filters.get('request_id')
    if request_id and entry.get('request_id') != request_id:
        return False

    search = filters.get('search')
    if search:
        needle = search.lower()
        msg = (entry.get('message') or '').lower()
        extra = json.dumps(entry.get('extra') or {}).lower()
        if needle not in msg and needle not in extra:
            return False

    date_from = filters.get('date_from')
    if date_from:
        ts = entry.get('timestamp', '')
        if ts and ts < date_from:
            return False

    date_to = filters.get('date_to')
    if date_to:
        ts = entry.get('timestamp', '')
        # Inclusive: compare the date part so the whole of date_to counts.
        if ts and ts[:10] > date_to:
            return False

    return True


# -------------------------------------------------------------- what a page loads

def load(filters=None, scope='current', day=None):
    """
    (entries, files, scanned) for one request, newest first.

    scope:
      'current' - the live file only. The default, and the only one whose cost
                  does not grow with how long the instance has been running.
      'day'     - one rotated file, chosen by its YYYYMMDD label.
      'all'     - the live file plus the newest MAX_ARCHIVE_FILES archives.

    `day` names a label, never a path. The file is looked up in what discovery
    already found rather than built by joining a string onto LOG_DIR, so a
    crafted value selects nothing instead of escaping the directory.
    """
    files = list_log_files(LOG_DIR, LOG_CURRENT, LOG_ARCHIVES)

    if scope == 'day' and day:
        chosen = [f for f in files if f['date_label'] == day]
    elif scope == 'all':
        current = [f for f in files if f['date_label'] == 'current']
        archives = [f for f in files if f['date_label'] != 'current']
        chosen = current + archives[:MAX_ARCHIVE_FILES]
    else:
        chosen = [f for f in files if f['date_label'] == 'current']

    entries = []
    for f in chosen:
        entries.extend(parse_log_file(f['path'], filters))
    entries.sort(key=lambda e: e.get('timestamp', ''), reverse=True)
    return entries, files, chosen


def available_tags(entries):
    """Tags present in what was actually loaded.

    Deliberately not every tag in every archive: that meant re-reading all 180
    files on each page load purely to populate a dropdown.
    """
    return sorted({e.get('tag') for e in entries if e.get('tag')})


def available_levels(entries):
    return sorted({e.get('level') for e in entries if e.get('level')})


def paginate(entries, page, per_page):
    """(page_entries, total, total_pages), with page clamped into range."""
    total = len(entries)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    return entries[start:start + per_page], total, total_pages
