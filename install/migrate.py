#!/usr/bin/env python3
"""
Bring a database up to date. Used by both install paths and safe to re-run.

WHY THIS EXISTS RATHER THAN "just load schema.sql": install/sql/schema.sql is a
dump from a point in the past. It is missing nine later migrations - is_savings,
the recurring_* tables, the category_type unification, the decimal widening,
instance_settings, the SMTP verification columns, users.is_admin, and the
totals_remainders_m foreign key. Loading it alone produces a database the
application cannot actually run on: provision_user() writes is_savings on its
first call.

WHY IT SHELLS OUT TO THE mysql CLIENT: schema.sql is a mysqldump containing
DELIMITER, which is a client directive rather than server SQL. Splitting
statements in Python and feeding them to a driver breaks on it. The client
already knows how.

IDEMPOTENCE: applied files are recorded in a schema_migrations table, so a
second run does nothing. On a database that predates that table - an existing
deployment - every file is attempted once with --force, which continues past
"already exists" errors, and the verification at the end is what actually
decides whether the schema is correct. That check is the point: it asserts the
end state rather than trusting that the steps ran.

    python3 install/migrate.py                 # uses DB_* from the environment
    python3 install/migrate.py --verify-only   # check, change nothing
"""

import argparse
import os
import re
import subprocess
import sys

# The file list lives in migration_manifest.py so that version_info.py can read
# it without importing this module, which would pull subprocess into the web
# application. This import works because running `python3 install/migrate.py`
# puts install/ at the front of sys.path.
from migration_manifest import BASELINE, MIGRATIONS, MIGRATIONS_DIR, REPO_ROOT

# What must be true when this finishes. Checked against the live schema, so a
# migration that silently did nothing is caught here rather than by a 500 later.
EXPECTED_TABLES = (
    'users', 'income_categories', 'expense_categories', 'income_entries',
    'expense_entries', 'credit_accounts', 'totals_remainders',
    'totals_remainders_d', 'totals_remainders_m', 'savings_entries',
    'notifications', 'setup_state', 'password_resets', 'instance_settings',
    'recurring_mismatches', 'recurring_suggestions', 'linked_accounts',
)
EXPECTED_COLUMNS = (
    ('users', 'is_admin'),
    ('users', 'member_since'),
    ('users', 'setup_step'),
    ('income_categories', 'is_savings'),
    ('expense_categories', 'is_savings'),
    ('instance_settings', 'smtp_password_encrypted'),
    ('instance_settings', 'verified_at'),
    ('instance_settings', 'verification_secret'),
)
EXPECTED_CONSTRAINTS = (
    ('totals_remainders_m', 'totals_remainders_m_ibfk_1'),
)


def db_config():
    """DB settings from the environment, the same names the app itself reads."""
    missing = [k for k in ('DB_HOST', 'DB_USER', 'DB_PASSWORD', 'DB_NAME')
               if not os.environ.get(k)]
    if missing:
        sys.exit(f'Missing environment variables: {", ".join(missing)}')
    return {
        'host': os.environ['DB_HOST'],
        'user': os.environ['DB_USER'],
        'password': os.environ['DB_PASSWORD'],
        'name': os.environ['DB_NAME'],
    }


def mysql(cfg, sql=None, stdin_text=None, force=False):
    """
    Run SQL through the mysql client. Returns (returncode, stdout, stderr).

    The password goes in via MYSQL_PWD rather than -p on the command line, where
    it would be visible to anyone running ps.
    """
    # --no-defaults must come first, and matters more than it looks: a password
    # in /root/.my.cnf takes precedence over MYSQL_PWD, so on a machine that has
    # one this connects as the wrong identity and fails with "Access denied" for
    # credentials that are perfectly correct. An installer cannot depend on
    # whatever option files the operator happens to have.
    cmd = ['mysql', '--no-defaults', '-h', cfg['host'], '-u', cfg['user'],
           '--batch', '--skip-column-names']
    if force:
        cmd.append('--force')
    cmd.append(cfg['name'])
    if sql:
        cmd.extend(['-e', sql])

    env = dict(os.environ, MYSQL_PWD=cfg['password'])
    proc = subprocess.run(cmd, input=stdin_text, env=env,
                          capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def query(cfg, sql):
    """One-column query as a list of strings."""
    rc, out, err = mysql(cfg, sql=sql)
    if rc != 0:
        sys.exit(f'Query failed: {err}')
    return [line for line in out.splitlines() if line]


def read_migration(name):
    """
    A migration file's SQL, with table encryption stripped unless asked for.

    schema.sql declares ENCRYPTION='Y' on 37 tables. That needs a keyring
    component, which a default MySQL 8 does not have - so a fresh install fails
    at the first CREATE TABLE with "Can't find master key from keyring". Set
    MYSQL_TABLE_ENCRYPTION=1 if the server is configured for it.
    """
    path = os.path.join(MIGRATIONS_DIR, name)
    with open(path, 'r', encoding='utf-8') as f:
        sql = f.read()
    if os.environ.get('MYSQL_TABLE_ENCRYPTION') != '1':
        sql = re.sub(r"\s*ENCRYPTION='Y'", '', sql)
    return sql


def ensure_tracking_table(cfg):
    rc, _, err = mysql(cfg, sql="""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename varchar(255) NOT NULL,
            applied_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (filename)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    if rc != 0:
        sys.exit(f'Could not create schema_migrations: {err}')


def applied(cfg):
    return set(query(cfg, "SELECT filename FROM schema_migrations"))


def record(cfg, name):
    mysql(cfg, sql="INSERT IGNORE INTO schema_migrations (filename) VALUES "
                   f"('{name}')")


def apply_file(cfg, name):
    """
    Apply one file with --force, so statements that are already in place do not
    abort the rest. Whether it worked is settled by verify(), not by this.
    """
    sql = read_migration(name)
    rc, _, err = mysql(cfg, stdin_text=sql, force=True)
    noise = [line for line in err.splitlines()
             if line and 'Using a password' not in line]
    return rc, noise


def table_exists(cfg, table):
    rows = query(cfg, "SELECT COUNT(*) FROM information_schema.TABLES "
                      f"WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '{table}'")
    return bool(rows) and rows[0] != '0'


def verify(cfg):
    """Assert the end state. Returns a list of problems."""
    problems = []

    for table in EXPECTED_TABLES:
        if not table_exists(cfg, table):
            problems.append(f'missing table: {table}')

    for table, column in EXPECTED_COLUMNS:
        rows = query(cfg, "SELECT COUNT(*) FROM information_schema.COLUMNS "
                          f"WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '{table}' "
                          f"AND COLUMN_NAME = '{column}'")
        if not rows or rows[0] == '0':
            problems.append(f'missing column: {table}.{column}')

    for table, constraint in EXPECTED_CONSTRAINTS:
        rows = query(cfg, "SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS "
                          f"WHERE CONSTRAINT_SCHEMA = DATABASE() "
                          f"AND CONSTRAINT_NAME = '{constraint}'")
        if not rows or rows[0] == '0':
            problems.append(f'missing constraint: {table}.{constraint}')

    # The decimal widening is a column type change, so its presence cannot be
    # inferred from a name.
    rows = query(cfg, "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
                      "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'income_entries' "
                      "AND COLUMN_NAME = 'amount'")
    if rows and 'decimal(15,2)' not in rows[0]:
        problems.append(f'income_entries.amount is {rows[0]}, expected decimal(15,2)')

    # Every user-scoped table should cascade, or deleting an account leaves
    # orphans - the bug that cost 5,580 rows before it was found.
    rows = query(cfg, """
        SELECT c.TABLE_NAME FROM information_schema.COLUMNS c
        WHERE c.TABLE_SCHEMA = DATABASE() AND c.COLUMN_NAME = 'user_id'
          AND c.TABLE_NAME NOT LIKE 'quiltt_purge_backup%'
          AND c.TABLE_NAME NOT IN (
              SELECT k.TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE k
              JOIN information_schema.REFERENTIAL_CONSTRAINTS r
                ON r.CONSTRAINT_NAME = k.CONSTRAINT_NAME
               AND r.CONSTRAINT_SCHEMA = k.TABLE_SCHEMA
              WHERE k.TABLE_SCHEMA = DATABASE() AND k.REFERENCED_TABLE_NAME = 'users'
                AND k.COLUMN_NAME = 'user_id' AND r.DELETE_RULE = 'CASCADE')
    """)
    for table in rows:
        problems.append(f'{table} has user_id but no cascading foreign key to users')

    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-only', action='store_true',
                        help='check the schema and change nothing')
    args = parser.parse_args()

    cfg = db_config()
    print(f"database: {cfg['name']} on {cfg['host']} as {cfg['user']}")

    rc, _, err = mysql(cfg, sql='SELECT 1')
    if rc != 0:
        sys.exit(f'Cannot connect: {err}')

    if not args.verify_only:
        ensure_tracking_table(cfg)
        done = applied(cfg)

        if not table_exists(cfg, 'users'):
            print(f'  applying baseline {BASELINE}')
            rc, noise = apply_file(cfg, BASELINE)
            if noise:
                for line in noise[:5]:
                    print(f'      {line}')
            record(cfg, BASELINE)
        elif BASELINE not in done:
            print(f'  baseline {BASELINE}: schema already present, recording as applied')
            record(cfg, BASELINE)

        for name in MIGRATIONS:
            if name in done:
                print(f'  {name}: already applied')
                continue
            path = os.path.join(MIGRATIONS_DIR, name)
            if not os.path.exists(path):
                print(f'  {name}: MISSING FROM THE REPO - skipped')
                continue
            rc, noise = apply_file(cfg, name)
            already = [n for n in noise if 'Duplicate' in n or 'already exists' in n]
            if already:
                print(f'  {name}: partly in place already ({len(already)} statement(s) skipped)')
            else:
                print(f'  {name}: applied')
            record(cfg, name)

    print()
    print('verifying the schema...')
    problems = verify(cfg)
    if problems:
        print(f'  {len(problems)} problem(s):')
        for p in problems:
            print(f'    - {p}')
        sys.exit(1)

    print(f'  ok: {len(EXPECTED_TABLES)} tables, {len(EXPECTED_COLUMNS)} columns, '
          f'{len(EXPECTED_CONSTRAINTS)} constraint(s), all user tables cascade')
    return 0


if __name__ == '__main__':
    sys.exit(main())
