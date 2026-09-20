#!/usr/bin/env python3
"""
Check the migration list against the files, before a database is involved.

A new migration has to be registered in three places in the same commit: the
SQL file in install/sql/, an entry appended to MIGRATIONS in
install/migration_manifest.py, and its assertions in EXPECTED_TABLES /
EXPECTED_COLUMNS in install/migrate.py. docs/RELEASING.md and the manifest both
say so in prose, and prose is what somebody's first contribution misses - "a
migration outside them is one the verification silently does not cover".

`migrate.py --verify-only` already checks a live schema, which is the right
check in the wrong place for this: it needs a database, so it cannot run on a
pull request, and it answers "is this server correct?" rather than "is this
commit complete?". This script reads files only, imports nothing that touches
the network or a socket, and finishes in milliseconds.

    python3 install/check_migrations.py            report and exit non-zero on a problem
    python3 install/check_migrations.py --list     also list what it parsed

What it refuses to guess: SQL is parsed with regular expressions, which is
enough for the plain DDL these migrations are and nothing more. A statement it
cannot read confidently is counted and ignored rather than reported as a
problem, because a check that cries wolf is a check somebody switches off.
"""

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from migration_manifest import BASELINE, MIGRATIONS, MIGRATIONS_DIR  # noqa: E402

# Words that can follow ADD in an ALTER TABLE and are not a column name.
_NOT_A_COLUMN = {
    'column', 'constraint', 'index', 'key', 'unique', 'primary', 'foreign',
    'fulltext', 'spatial', 'check', 'partition',
}

_CREATE_TABLE = re.compile(
    r'\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?[`"]?(\w+)[`"]?', re.I)
_DROP_TABLE = re.compile(
    r'\bdrop\s+table\s+(?:if\s+exists\s+)?[`"]?(\w+)[`"]?', re.I)
_ALTER_TABLE = re.compile(r'\balter\s+table\s+[`"]?(\w+)[`"]?', re.I)
_RENAME_TABLE = re.compile(
    r'\brename\s+table\s+[`"]?(\w+)[`"]?\s+to\s+[`"]?(\w+)[`"]?', re.I)
# A temporary table lives for the length of the connection; it is scaffolding a
# data migration builds, never schema anybody should assert about.
_TEMPORARY = re.compile(r'\b(create|drop)\s+temporary\s+table\b', re.I)
_ADD_SOMETHING = re.compile(r'\badd\s+(?:column\s+)?[`"]?(\w+)[`"]?', re.I)
_DROP_COLUMN = re.compile(r'\bdrop\s+(?:column\s+)?[`"]?(\w+)[`"]?', re.I)


def statements(sql):
    """
    The statements in one file, comments stripped. None when the file uses
    DELIMITER - a stored procedure is beyond what this reads, and saying so is
    better than misreading it.

    The split walks the text rather than calling str.split(';'): these
    migrations carry COMMENT strings that contain semicolons, and splitting
    naively cut an ALTER TABLE in half and lost the columns after the cut.
    """
    if re.search(r'^\s*delimiter\b', sql, re.I | re.M):
        return None
    lines = [l for l in sql.splitlines() if not l.lstrip().startswith('--')]
    body = re.sub(r'/\*.*?\*/', ' ', '\n'.join(lines), flags=re.S)
    out, current, quote = [], [], None
    for ch in body:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"', '`'):
            quote = ch
            current.append(ch)
            continue
        if ch == ';':
            out.append(''.join(current))
            current = []
            continue
        current.append(ch)
    out.append(''.join(current))
    return [s.strip() for s in out if s.strip()]


def parse(sql):
    """
    (tables, columns, unreadable) a file creates: table names, (table, column)
    pairs, and how many statements were skipped.
    """
    tables, columns = set(), set()
    dropped_tables, dropped_columns = set(), set()
    parsed = statements(sql)
    if parsed is None:
        return set(), set(), None
    unreadable = 0
    for stmt in parsed:
        if _TEMPORARY.search(stmt):
            continue
        renamed = _RENAME_TABLE.search(stmt)
        if renamed:
            # The old name is gone and the new one is what must be asserted.
            old_name, new_name = renamed.group(1), renamed.group(2)
            dropped_tables.add(old_name)
            tables.add(new_name)
            columns = {(t, c) for (t, c) in columns if t != old_name}
            continue
        created = _CREATE_TABLE.search(stmt)
        if created:
            tables.add(created.group(1))
            continue
        gone = _DROP_TABLE.search(stmt)
        if gone:
            dropped_tables.add(gone.group(1))
            continue
        altered = _ALTER_TABLE.search(stmt)
        if not altered:
            # INSERT, UPDATE, SET, a data fix: nothing to assert about.
            if re.match(r'\s*(insert|update|delete|set|use|start|commit|'
                        r'rollback|create\s+(index|unique|fulltext))\b', stmt, re.I):
                continue
            unreadable += 1
            continue
        table = altered.group(1)
        for match in _ADD_SOMETHING.finditer(stmt):
            name = match.group(1)
            if name.lower() in _NOT_A_COLUMN:
                continue
            columns.add((table, name))
        for match in _DROP_COLUMN.finditer(stmt):
            name = match.group(1)
            if name.lower() in _NOT_A_COLUMN:
                continue
            dropped_columns.add((table, name))
    # Only what survives the list is worth asserting: a column added in one
    # migration and dropped in a later one is not in the schema any more, and
    # demanding an assertion for it would be demanding a false one.
    tables -= dropped_tables
    columns = {(t, c) for (t, c) in columns
               if t not in dropped_tables and (t, c) not in dropped_columns}
    return tables, columns, unreadable


def expected():
    """The assertions migrate.py makes, read without importing it."""
    source = open(os.path.join(HERE, 'migrate.py'), encoding='utf-8').read()

    def tuple_after(name):
        start = source.index(f'{name} = (')
        end = source.index('\n)', start)
        return source[start:end]

    tables = set(re.findall(r"'(\w+)'", tuple_after('EXPECTED_TABLES')))
    columns = set(re.findall(r"\(\s*'(\w+)'\s*,\s*'(\w+)'\s*\)",
                             tuple_after('EXPECTED_COLUMNS')))
    return tables, columns


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--list', action='store_true',
                    help='also print what was parsed out of each migration')
    args = ap.parse_args()

    problems = []
    # macOS writes an AppleDouble sibling for every file on a network share;
    # ._name.sql is metadata, not a migration nobody registered.
    on_disk = {f for f in os.listdir(MIGRATIONS_DIR)
               if f.endswith('.sql') and not f.startswith('.')}

    # 1. The manifest and the directory agree.
    for name in MIGRATIONS:
        if name not in on_disk:
            problems.append(f'{name} is in MIGRATIONS but not in install/sql/')
    seen = set()
    for name in MIGRATIONS:
        if name in seen:
            problems.append(f'{name} appears in MIGRATIONS more than once')
        seen.add(name)
    for name in sorted(on_disk - set(MIGRATIONS) - {BASELINE}):
        problems.append(f'install/sql/{name} is not in MIGRATIONS, so it is never applied')

    # 2. What the migrations build is what migrate.py checks for.
    want_tables, want_columns = expected()
    skipped = []
    for name in MIGRATIONS:
        path = os.path.join(MIGRATIONS_DIR, name)
        if not os.path.exists(path):
            continue
        tables, columns, unreadable = parse(open(path, encoding='utf-8').read())
        if unreadable is None:
            skipped.append(f'{name} (uses DELIMITER)')
            continue
        if unreadable:
            skipped.append(f'{name} ({unreadable} statement(s) not read)')
        if args.list:
            print(f'{name}: tables={sorted(tables) or "-"} '
                  f'columns={sorted(columns) or "-"}')
        for table in sorted(tables - want_tables):
            problems.append(f'{name} creates {table}, which is not in '
                            f'EXPECTED_TABLES in migrate.py')
        for table, column in sorted(columns - want_columns):
            if table in want_tables and (table in tables):
                # A column of a table this same migration creates is covered by
                # the table's own assertion.
                continue
            problems.append(f'{name} adds {table}.{column}, which is not in '
                            f'EXPECTED_COLUMNS in migrate.py')

    if skipped:
        print('not fully read (by design):')
        for note in skipped:
            print(f'  {note}')
    if problems:
        print(f'\n{len(problems)} problem(s):')
        for problem in problems:
            print(f'  {problem}')
        print('\nSee docs/RELEASING.md: a migration is three edits in one commit - '
              'the SQL file, MIGRATIONS in install/migration_manifest.py, and the '
              'EXPECTED_* assertions in install/migrate.py.')
        return 1
    print(f'{len(MIGRATIONS)} migration(s) listed, all present, all asserted.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
