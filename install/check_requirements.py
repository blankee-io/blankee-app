#!/usr/bin/env python3
"""
Check that every module the code imports is declared in requirements.txt.

Run before committing:

    python3 install/check_requirements.py

Exits non-zero and names the offenders, with the files that import them.

WHY THIS EXISTS: httpx was imported by push_notifications.py and never declared,
so a fresh virtualenv could not import the application at all - while the
development machine, which happened to have httpx installed, showed nothing
wrong. PyJWT was the same story. Both were found by a clean install failing,
which is the most expensive possible place to find them.

The admin console reports the same thing at runtime, but it can only see what a
worker process has already imported, and much of this codebase imports inside
request handlers. This walks the source with ast, so it sees every import
whether or not it has run.

Deliberately stdlib-only and subprocess-free, so it works anywhere the
application does.
"""

import argparse
import ast
import importlib.metadata as md
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIREMENTS = os.path.join(REPO_ROOT, 'requirements.txt')

# Directories that are not this application's source.
SKIP_DIRS = {'venv', '.git', '__pycache__', 'node_modules', 'static', 'templates',
             'optimization', 'checklists', 'integrations', 'docs'}

# Installed for reasons requirements.txt is not responsible for.
IGNORE = {'pip', 'setuptools', 'wheel', 'pkg-resources', 'gunicorn', 'mod-wsgi'}

# Modules provided by this repository rather than by a package.
LOCAL = {os.path.splitext(n)[0] for n in os.listdir(REPO_ROOT) if n.endswith('.py')}
LOCAL |= {d for d in os.listdir(REPO_ROOT)
          if os.path.isdir(os.path.join(REPO_ROOT, d))
          and os.path.exists(os.path.join(REPO_ROOT, d, '__init__.py'))}
LOCAL |= {'migration_manifest', 'check_requirements', 'migrate', 'build_fa_fallback'}


def normalize(name):
    return re.sub(r'[-_.]+', '-', name).strip().lower()


def python_files():
    for base, dirs, names in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]
        for name in names:
            if name.endswith('.py') and not name.startswith('._'):
                yield os.path.join(base, name)


def imports_in(path):
    """Top-level module names imported by one file."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=path)
    except Exception as e:
        print(f'  could not parse {os.path.relpath(path, REPO_ROOT)}: {e}', file=sys.stderr)
        return set()

    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import, which is always local.
            if node.level == 0 and node.module:
                found.add(node.module.split('.')[0])
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--list', action='store_true',
                        help='list every third-party module found and where it came from')
    args = parser.parse_args()

    pinned, unpinned = {}, []
    with open(REQUIREMENTS, 'r', encoding='utf-8') as f:
        for raw in f:
            line = raw.split('#', 1)[0].strip()
            if not line:
                continue
            m = re.match(r'^([A-Za-z0-9._-]+)\s*==\s*([^\s;]+)$', line)
            if m:
                pinned[normalize(m.group(1))] = m.group(2)
            else:
                unpinned.append(line)

    # Shares version_info's implementation rather than calling
    # packages_distributions() directly - see the docstring there for why that
    # function is not sufficient on Python 3.10.
    sys.path.insert(0, REPO_ROOT)
    from version_info import top_level_map, declared_closure
    top_level = top_level_map()
    closure = declared_closure(pinned)
    stdlib = set(getattr(sys, 'stdlib_module_names', ()))

    # module -> files that import it
    where = {}
    for path in python_files():
        rel = os.path.relpath(path, REPO_ROOT).replace(os.sep, '/')
        for mod in imports_in(path):
            where.setdefault(mod, set()).add(rel)

    # Two severities, and the distinction is the point of this script.
    #
    #   problems   not accounted for at all. A fresh virtualenv cannot import
    #              the application. This is the httpx bug.
    #   indirect   works, but only because something else happens to depend on
    #              it - so it breaks on the upgrade that drops that dependency,
    #              with no warning. Flask is imported directly by app.py; that
    #              it would also arrive via Flask-Login is not a reason to leave
    #              it undeclared.
    problems, indirect, listed = [], [], []
    for mod in sorted(where):
        if mod in stdlib or mod in LOCAL or mod.startswith('_'):
            continue
        dists = [normalize(d) for d in top_level.get(mod, [])]
        if not dists:
            # Not installed here at all: either nobody declared it, or this
            # environment simply does not have it. Both want a look.
            problems.append((mod, None, sorted(where[mod])))
            continue
        listed.append((mod, dists))
        if any(d in IGNORE for d in dists) or any(d in pinned for d in dists):
            continue
        if any(d in closure for d in dists):
            indirect.append((mod, dists, sorted(where[mod])))
        else:
            problems.append((mod, dists, sorted(where[mod])))

    print(f'{len(pinned)} pinned requirement(s), {len(closure)} with dependencies')
    print(f'{len(where)} distinct top-level import(s) across the source')
    if unpinned:
        print(f'\nnot pinned with == ({len(unpinned)}):')
        for line in unpinned:
            print(f'  {line}')

    if args.list:
        print('\nthird-party modules in use:')
        for mod, dists in listed:
            print(f'  {mod:22} <- {", ".join(dists)}')

    if indirect:
        print(f'\n{len(indirect)} import(s) satisfied only indirectly - pin these directly:')
        for mod, dists, files in indirect:
            print(f'  {mod}  (arrives via another requirement: {", ".join(dists)})')
            for rel in files[:3]:
                print(f'      imported by {rel}')

    if problems:
        print(f'\n{len(problems)} import(s) not accounted for by requirements.txt:')
        for mod, dists, files in problems:
            provider = f'provided by {", ".join(dists)}' if dists else 'not installed here'
            print(f'  {mod}  ({provider})')
            for rel in files[:4]:
                print(f'      imported by {rel}')
        print('\nAdd the distribution to requirements.txt with an == pin, or add it to')
        print('IGNORE in this script if requirements.txt is genuinely not responsible.')
        return 1

    if indirect:
        # Not a failure: the application runs. Reported so it can be fixed
        # before the upgrade that removes whatever is carrying it.
        print('\nok, with warnings: everything imports, but the above are not pinned')
        return 0

    print('\nok: every third-party import is declared and pinned directly')
    return 0


if __name__ == '__main__':
    sys.exit(main())
