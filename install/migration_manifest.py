"""
Which SQL files make up the schema, and in what order.

A separate module so that two things can agree on it without either importing
the other. install/migrate.py applies these files; version_info.py reports
whether any of them are outstanding, and it must be able to read the list
without importing migrate.py - which pulls in subprocess, and the web
application deliberately contains no subprocess use at all.

Copying the list into the web tier instead was the obvious alternative and the
wrong one: the two copies would drift, and the first symptom would be an admin
console confidently reporting a schema state that is not true.
"""

import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATIONS_DIR = os.path.join(REPO_ROOT, 'install', 'sql')

# Order matters. schema.sql is the baseline; everything after it is applied on
# top, oldest first.
BASELINE = 'schema.sql'

MIGRATIONS = [
    'add_is_savings_to_categories.sql',
    'add_recurring_mismatches_table.sql',
    'add_recurring_suggestions_table.sql',
    'unify_category_types.sql',
    'widen_decimal_columns.sql',
    'add_instance_settings.sql',
    'add_smtp_verification.sql',
    'add_admin_user.sql',
    'add_totals_remainders_m_fk.sql',
    'add_bucket_confirmation.sql',
    'add_auto_balance.sql',
    'add_email_notification_types.sql',
    'add_autobalance_category.sql',
]

# install/sql/ holds only what a fresh install applies. The rollback scripts and
# the one-off purges that used to sit beside them are gone: they were written
# against particular database states, a new database has never been in any of
# them, and keeping them invited someone to run one.
#
# Anything genuinely needed later belongs here, in MIGRATIONS, in order. A new
# migration must also add its assertions to EXPECTED_TABLES / EXPECTED_COLUMNS /
# EXPECTED_CONSTRAINTS in migrate.py in the same commit - those lists are what
# --verify-only checks, and a migration outside them is one the verification
# silently does not cover.
