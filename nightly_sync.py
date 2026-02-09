#!/usr/bin/env python3
"""
Nightly Balance Sync and Auto-Adjust
Runs via cron at midnight to sync bank balances and import new transactions for all users.

Cron entry (add to server):
    0 0 * * * cd /var/www/html/budget && /usr/bin/python3 nightly_sync.py >> /var/log/apache2/nightly_sync.log 2>&1

What it does:
1. Gets all users with quiltt_enabled = 1
2. For each user:
   a. Refresh session token if needed
   b. Fetch latest account balances from Quiltt
   c. Sync new transactions (since last_synced_at)
   d. Run auto-balance for checking accounts
   e. Update savings balance from bank
   f. Recalculate totals_remainders_d
3. Logs results

This replaces the need for users to manually sync or rely on webhooks for daily balance updates.
"""

import os
import sys
import json
import logging
from datetime import datetime, timedelta, date
from decimal import Decimal

# Load environment variables from .env file (check multiple locations)
from dotenv import load_dotenv

# Try dev server path first, then prod path
env_paths = ['/var/www/budget_env/.env', '/var/www/blankee/.env']
for env_path in env_paths:
    if os.path.exists(env_path):
        load_dotenv(env_path)
        break

import mysql.connector
import redis

# Configure logging to file
LOG_FILE = '/var/log/apache2/nightly_sync.log'
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()  # Also print to console when run manually
    ]
)
logger = logging.getLogger(__name__)

# Import after dotenv is loaded
from quiltt_utils import QuilttClient

# Redis configuration
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_DB = int(os.getenv('REDIS_DB', 0))
REDIS_KEY_VERSION = 'v1'
REDIS_TTL = 604800  # 7 days

# Initialize clients
redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)
quiltt_client = QuilttClient()


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal types"""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if hasattr(obj, 'isoformat'):
            return obj.isoformat()
        return super().default(obj)


def get_db_connection():
    """Get a direct MySQL connection"""
    return mysql.connector.connect(
        host='localhost',
        user='ms_admin',
        password='dune6MEANTIME.ching_reek',
        database='budget'
    )


def is_user_hydrated(user_id):
    """Check if user has data in Redis"""
    try:
        key = f"income_entries:{REDIS_KEY_VERSION}:{user_id}"
        return redis_client.exists(key) > 0
    except Exception as e:
        logger.warning(f"Error checking hydration for user {user_id}: {e}")
        return False


def get_redis_key(table, user_id):
    """Get Redis key for a table"""
    return f"{table}:{REDIS_KEY_VERSION}:{user_id}"


def mark_dirty(user_id, table_name):
    """Mark a table as dirty for flush"""
    try:
        dirty_key = f"dirty_tables:{user_id}"
        redis_client.sadd(dirty_key, table_name)
        redis_client.expire(dirty_key, REDIS_TTL)
    except Exception as e:
        logger.warning(f"Error marking dirty {table_name} for user {user_id}: {e}")


def get_quiltt_enabled_users(cursor):
    """Get all users with Quiltt enabled and their profile info"""
    cursor.execute("""
        SELECT u.id, u.first_name, u.email, qp.profile_id, qp.session_token, qp.session_expires_at
        FROM users u
        JOIN quiltt_profiles qp ON u.id = qp.user_id
        WHERE u.quiltt_enabled = 1
    """)
    return cursor.fetchall()


def refresh_session_if_needed(cursor, conn, user_id, profile_id, session_token, session_expires_at):
    """Refresh Quiltt session token if expired or expiring soon"""
    now = datetime.now()
    
    # Check if token is expired or expires in next hour
    if session_expires_at and isinstance(session_expires_at, datetime):
        if session_expires_at > now + timedelta(hours=1):
            # Token still valid
            return session_token
    
    logger.info(f"User {user_id}: Refreshing expired session token")
    
    # Refresh token
    result = quiltt_client.refresh_session_token(profile_id)
    if not result or not result.get('token'):
        logger.error(f"User {user_id}: Failed to refresh session token")
        return None
    
    new_token = result['token']
    new_expires = result.get('expiresAt')
    
    # Parse expiration if string
    if new_expires and isinstance(new_expires, str):
        try:
            new_expires = datetime.fromisoformat(new_expires.replace('Z', '+00:00'))
        except:
            new_expires = now + timedelta(hours=1)
    
    # Update in database
    cursor.execute("""
        UPDATE quiltt_profiles
        SET session_token = %s, session_expires_at = %s
        WHERE user_id = %s
    """, (new_token, new_expires, user_id))
    conn.commit()
    
    # Update Redis if hydrated
    if is_user_hydrated(user_id):
        try:
            redis_key = get_redis_key('quiltt_profiles', user_id)
            cached = redis_client.get(redis_key)
            if cached:
                profiles = json.loads(cached)
                if profiles:
                    profiles[0]['session_token'] = new_token
                    profiles[0]['session_expires_at'] = new_expires.isoformat() if new_expires else None
                    redis_client.setex(redis_key, REDIS_TTL, json.dumps(profiles, cls=DecimalEncoder))
        except Exception as e:
            logger.warning(f"User {user_id}: Error updating Redis profile: {e}")
    
    logger.info(f"User {user_id}: Session token refreshed")
    return new_token


def fetch_and_update_balances(cursor, conn, user_id, session_token):
    """Fetch latest account balances from Quiltt and update locally"""
    try:
        profile_data = quiltt_client.get_profile(session_token)
        if not profile_data:
            logger.warning(f"User {user_id}: Failed to fetch profile from Quiltt")
            return []
        
        updated_accounts = []
        
        for connection in profile_data.get('connections', []):
            if not connection:
                continue
            
            for account in connection.get('accounts', []):
                if not account:
                    continue
                
                account_id = account.get('id', '')
                account_name = account.get('name', 'Account')
                account_type = account.get('kind', '').upper()
                
                # Get balance
                balance = account.get('balance') or {}
                current_balance = balance.get('current', 0) if isinstance(balance, dict) else 0
                available_balance = balance.get('available', 0) if isinstance(balance, dict) else 0
                current_balance = abs(current_balance) if current_balance else 0
                available_balance = abs(available_balance) if available_balance else 0
                
                # Update in database
                cursor.execute("""
                    UPDATE quiltt_accounts
                    SET current_balance = %s, available_balance = %s, last_modified = NOW()
                    WHERE user_id = %s AND account_id = %s
                """, (current_balance, available_balance, user_id, account_id))
                
                if cursor.rowcount > 0:
                    updated_accounts.append({
                        'account_id': account_id,
                        'account_name': account_name,
                        'account_type': account_type,
                        'current_balance': current_balance
                    })
        
        conn.commit()
        
        # Update Redis if hydrated
        if is_user_hydrated(user_id) and updated_accounts:
            try:
                redis_key = get_redis_key('quiltt_accounts', user_id)
                cached = redis_client.get(redis_key)
                if cached:
                    accounts = json.loads(cached)
                    for acc in accounts:
                        for updated in updated_accounts:
                            if acc.get('account_id') == updated['account_id']:
                                acc['current_balance'] = updated['current_balance']
                                break
                    redis_client.setex(redis_key, REDIS_TTL, json.dumps(accounts, cls=DecimalEncoder))
                    mark_dirty(user_id, 'quiltt_accounts')
            except Exception as e:
                logger.warning(f"User {user_id}: Error updating Redis accounts: {e}")
        
        return updated_accounts
        
    except Exception as e:
        logger.error(f"User {user_id}: Error fetching balances: {e}")
        return []


def get_active_accounts(cursor, user_id):
    """Get user's active Quiltt accounts"""
    cursor.execute("""
        SELECT account_id, account_name, account_type, current_balance, sync_transactions, mask
        FROM quiltt_accounts
        WHERE user_id = %s AND is_active = 1 AND sync_transactions = 1
    """, (user_id,))
    return cursor.fetchall()


def sync_transactions_for_account(cursor, conn, user_id, account_id, session_token, last_synced_at):
    """Sync new transactions for a specific account"""
    try:
        # Determine date range
        end_date = date.today()
        if last_synced_at:
            start_date = last_synced_at.date() if isinstance(last_synced_at, datetime) else last_synced_at
        else:
            # First sync - go back 30 days
            start_date = end_date - timedelta(days=30)
        
        # Format dates
        start_str = start_date.strftime('%Y-%m-%d')
        end_str = end_date.strftime('%Y-%m-%d')
        
        logger.info(f"User {user_id}: Syncing transactions for account {account_id} from {start_str} to {end_str}")
        
        # Fetch transactions from Quiltt
        transactions = quiltt_client.get_transactions_with_ntropy(
            session_token=session_token,
            account_ids=[account_id],
            start_date=start_str,
            end_date=end_str,
            limit=500
        )
        
        if not transactions:
            return 0, 0
        
        # Get existing transaction IDs to avoid duplicates
        cursor.execute("""
            SELECT transaction_id FROM quiltt_transactions
            WHERE user_id = %s AND account_id = %s
        """, (user_id, account_id))
        existing_txn_ids = {row[0] for row in cursor.fetchall()}
        
        new_count = 0
        imported_count = 0
        
        for txn in transactions:
            txn_id = txn.get('id')
            if not txn_id or txn_id in existing_txn_ids:
                continue
            
            # Store transaction
            amount = abs(float(txn.get('amount', 0)))
            txn_date = txn.get('date')
            description = txn.get('description', '')
            merchant_name = txn.get('merchantName')
            category = txn.get('category')
            txn_type = txn.get('transactionType', txn.get('type', '')).lower()
            pending = 1 if txn.get('pending') else 0
            
            # Extract Ntropy data if present
            ntropy_data = {}
            remote_data = txn.get('remoteData', {})
            if remote_data:
                ntropy = remote_data.get('ntropy', {})
                enrichment = ntropy.get('enrichment', {})
                response = enrichment.get('response', {})
                
                labels = response.get('labels')
                if labels:
                    ntropy_data['ntropy_labels'] = json.dumps(labels)
                
                merchant = response.get('merchant')
                if merchant and isinstance(merchant, dict):
                    ntropy_data['ntropy_merchant_id'] = merchant.get('id')
                    ntropy_data['ntropy_logo'] = merchant.get('logo')
                    ntropy_data['ntropy_website'] = merchant.get('website')
                
                ntropy_data['ntropy_recurrence'] = response.get('recurrence')
                if ntropy_data.get('ntropy_recurrence'):
                    ntropy_data['ntropy_enriched_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # Insert transaction
            cursor.execute("""
                INSERT INTO quiltt_transactions
                (user_id, account_id, transaction_id, amount, date, description, merchant_name, 
                 category, transaction_type, pending, ntropy_labels, ntropy_merchant_id, 
                 ntropy_logo, ntropy_website, ntropy_recurrence, ntropy_enriched_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    description = VALUES(description),
                    amount = VALUES(amount),
                    pending = VALUES(pending)
            """, (
                user_id, account_id, txn_id, amount, txn_date, description, merchant_name,
                category, txn_type, pending,
                ntropy_data.get('ntropy_labels'),
                ntropy_data.get('ntropy_merchant_id'),
                ntropy_data.get('ntropy_logo'),
                ntropy_data.get('ntropy_website'),
                ntropy_data.get('ntropy_recurrence'),
                ntropy_data.get('ntropy_enriched_at')
            ))
            new_count += 1
            
            # Auto-import new transactions as pending entries
            # Skip if this is a pending bank transaction
            if not pending:
                imported = auto_import_transaction(cursor, conn, user_id, account_id, txn, txn_type)
                if imported:
                    imported_count += 1
        
        conn.commit()
        
        # Update connection last_synced_at
        cursor.execute("""
            UPDATE quiltt_connections qc
            JOIN quiltt_accounts qa ON qc.id = qa.connection_id
            SET qc.last_synced_at = NOW()
            WHERE qa.user_id = %s AND qa.account_id = %s
        """, (user_id, account_id))
        conn.commit()
        
        return new_count, imported_count
        
    except Exception as e:
        logger.error(f"User {user_id}: Error syncing transactions for {account_id}: {e}")
        return 0, 0


def auto_import_transaction(cursor, conn, user_id, account_id, txn, txn_type):
    """Auto-import a transaction as a pending entry"""
    try:
        txn_id = txn.get('id')
        amount = abs(float(txn.get('amount', 0)))
        txn_date = txn.get('date')
        
        # Determine entry type based on account type and transaction direction
        cursor.execute("""
            SELECT account_type FROM quiltt_accounts
            WHERE user_id = %s AND account_id = %s
        """, (user_id, account_id))
        acc_row = cursor.fetchone()
        account_type = acc_row[0].lower() if acc_row else 'depository'
        
        # Determine which table to insert into
        if account_type == 'credit':
            if txn_type == 'income' or 'payment' in (txn.get('description') or '').lower():
                # Payment to credit card
                entry_type = 'c_payment'
            else:
                # Expense on credit card
                entry_type = 'c_expense'
        else:
            # Checking/savings account
            if txn_type == 'income':
                entry_type = 'income'
            else:
                entry_type = 'expense'
        
        # Get Uncategorized category
        if entry_type == 'income':
            cursor.execute("""
                SELECT id FROM income_categories
                WHERE user_id = %s AND LOWER(name) = 'uncategorized'
                LIMIT 1
            """, (user_id,))
        elif entry_type == 'expense':
            cursor.execute("""
                SELECT id FROM expense_categories
                WHERE user_id = %s AND LOWER(name) = 'uncategorized'
                LIMIT 1
            """, (user_id,))
        elif entry_type == 'c_expense':
            # Get the Blankee credit account that matches this Quiltt account
            cursor.execute("""
                SELECT ca.id FROM credit_accounts ca
                JOIN quiltt_accounts qa ON ca.quiltt_account_id = qa.account_id
                WHERE qa.user_id = %s AND qa.account_id = %s
            """, (user_id, account_id))
            ca_row = cursor.fetchone()
            if not ca_row:
                return False
            blankee_account_id = ca_row[0]
            
            cursor.execute("""
                SELECT id FROM c_expense_categories
                WHERE account_id = %s AND LOWER(name) = 'uncategorized'
                LIMIT 1
            """, (blankee_account_id,))
        elif entry_type == 'c_payment':
            # Credit card payments don't need categorization
            return False
        
        cat_row = cursor.fetchone()
        if not cat_row:
            return False
        category_id = cat_row[0]
        
        # Insert entry with pending=1, auto_confirmed=0 (user must review)
        if entry_type == 'income':
            cursor.execute("""
                INSERT INTO income_entries (category_id, date, amount, pending, auto_confirmed, processed)
                VALUES (%s, %s, %s, 1, 0, 0)
            """, (category_id, txn_date, amount))
            entry_id = cursor.lastrowid
            table_name = 'income_entries'
        elif entry_type == 'expense':
            cursor.execute("""
                INSERT INTO expense_entries (category_id, date, amount, pending, auto_confirmed, processed)
                VALUES (%s, %s, %s, 1, 0, 0)
            """, (category_id, txn_date, amount))
            entry_id = cursor.lastrowid
            table_name = 'expense_entries'
        elif entry_type == 'c_expense':
            cursor.execute("""
                INSERT INTO c_expense_entries (category_id, date, amount, pending, auto_confirmed, processed)
                VALUES (%s, %s, %s, 1, 0, 0)
            """, (category_id, txn_date, amount))
            entry_id = cursor.lastrowid
            table_name = 'c_expense_entries'
        
        # Update quiltt_transaction with entry reference
        cursor.execute("""
            UPDATE quiltt_transactions
            SET imported_to_entry_id = %s, imported_entry_type = %s, imported_at = NOW()
            WHERE user_id = %s AND transaction_id = %s
        """, (entry_id, entry_type, user_id, txn_id))
        
        conn.commit()
        
        # Update Redis if hydrated
        if is_user_hydrated(user_id):
            mark_dirty(user_id, table_name)
        
        return True
        
    except Exception as e:
        logger.warning(f"User {user_id}: Error auto-importing transaction: {e}")
        return False


def create_auto_adjustment(cursor, conn, user_id, bank_balance, account_name):
    """
    Create auto-adjustment entry to match bank balance.
    
    Logic:
    1. Delete any existing auto-adjustment entries for today (clean slate)
    2. Calculate the "natural" remainder (without auto-adjustments)
    3. Create ONE adjustment entry for the difference
    """
    try:
        today = date.today()
        today_str = today.strftime('%Y-%m-%d')
        
        # Get auto-adjustment category IDs
        cursor.execute("""
            SELECT id FROM income_categories
            WHERE user_id = %s AND is_auto_adjustment = 1
            LIMIT 1
        """, (user_id,))
        income_cat_row = cursor.fetchone()
        income_cat_id = income_cat_row[0] if income_cat_row else None
        
        cursor.execute("""
            SELECT id FROM expense_categories
            WHERE user_id = %s AND is_auto_adjustment = 1
            LIMIT 1
        """, (user_id,))
        expense_cat_row = cursor.fetchone()
        expense_cat_id = expense_cat_row[0] if expense_cat_row else None
        
        if not income_cat_id or not expense_cat_id:
            logger.warning(f"User {user_id}: Missing auto-adjustment category")
            return False, "No auto-adjustment category"
        
        # Step 1: Delete existing auto-adjustment entries for today
        cursor.execute("""
            DELETE FROM income_entries WHERE category_id = %s AND date = %s
        """, (income_cat_id, today_str))
        cursor.execute("""
            DELETE FROM expense_entries WHERE category_id = %s AND date = %s
        """, (expense_cat_id, today_str))
        conn.commit()
        
        # Step 2: Get yesterday's remainder (starting point)
        yesterday = today - timedelta(days=1)
        cursor.execute("""
            SELECT remainder FROM totals_remainders_d
            WHERE user_id = %s AND date = %s
        """, (user_id, yesterday.strftime('%Y-%m-%d')))
        row = cursor.fetchone()
        last_day_remainder = float(row[0]) if row else 0.0
        
        # Step 3: Calculate today's natural income (excluding auto-adjustment)
        cursor.execute("""
            SELECT COALESCE(SUM(ie.amount), 0)
            FROM income_entries ie
            JOIN income_categories ic ON ie.category_id = ic.id
            WHERE ic.user_id = %s AND ie.date = %s AND ic.is_auto_adjustment = 0
        """, (user_id, today_str))
        row = cursor.fetchone()
        today_income = float(row[0]) if row else 0.0
        
        # Step 4: Calculate today's natural expenses (excluding auto-adjustment)
        cursor.execute("""
            SELECT COALESCE(SUM(ee.amount), 0)
            FROM expense_entries ee
            JOIN expense_categories ec ON ee.category_id = ec.id
            WHERE ec.user_id = %s AND ee.date = %s AND ec.is_auto_adjustment = 0
        """, (user_id, today_str))
        row = cursor.fetchone()
        today_expenses = float(row[0]) if row else 0.0
        
        # Step 5: Calculate natural remainder
        natural_remainder = last_day_remainder + today_income - today_expenses
        
        # Step 6: Determine needed adjustment
        diff = float(bank_balance) - natural_remainder
        
        logger.info(f"User {user_id}: Bank=${bank_balance:.2f}, Natural=${natural_remainder:.2f}, Diff=${diff:.2f}")
        
        if abs(diff) < 0.01:
            return True, "Already balanced"
        
        # Step 7: Create single adjustment entry
        if diff > 0:
            cursor.execute("""
                INSERT INTO income_entries (category_id, date, amount, processed)
                VALUES (%s, %s, %s, 1)
            """, (income_cat_id, today_str, abs(diff)))
            entry_type = 'income'
        else:
            cursor.execute("""
                INSERT INTO expense_entries (category_id, date, amount, processed)
                VALUES (%s, %s, %s, 1)
            """, (expense_cat_id, today_str, abs(diff)))
            entry_type = 'expense'
        
        conn.commit()
        
        # Mark dirty for Redis
        if is_user_hydrated(user_id):
            mark_dirty(user_id, 'income_entries')
            mark_dirty(user_id, 'expense_entries')
        
        return True, f"Created {entry_type} adjustment of ${abs(diff):.2f}"
        
    except Exception as e:
        logger.error(f"User {user_id}: Error creating auto-adjustment: {e}")
        return False, str(e)


def update_savings_balance(cursor, conn, user_id, bank_balance, account_id=None):
    """Update savings balance from bank"""
    try:
        today = date.today()
        today_str = today.strftime('%Y-%m-%d')
        
        # Check for existing savings adjustment today
        cursor.execute("""
            SELECT id FROM savings_adjustments
            WHERE user_id = %s AND date = %s
        """, (user_id, today_str))
        existing = cursor.fetchone()
        
        if existing:
            cursor.execute("""
                UPDATE savings_adjustments
                SET amount = %s, quiltt_account_id = %s
                WHERE id = %s
            """, (bank_balance, account_id, existing[0]))
        else:
            cursor.execute("""
                INSERT INTO savings_adjustments (user_id, date, amount, description, quiltt_account_id)
                VALUES (%s, %s, %s, %s, %s)
            """, (user_id, today_str, bank_balance, 'Nightly sync from bank', account_id))
        
        conn.commit()
        
        if is_user_hydrated(user_id):
            mark_dirty(user_id, 'savings_adjustments')
        
        return True, f"Updated savings to ${bank_balance:.2f}"
        
    except Exception as e:
        logger.error(f"User {user_id}: Error updating savings: {e}")
        return False, str(e)


def recalculate_daily_totals(cursor, conn, user_id, start_date=None):
    """
    Recalculate totals_remainders_d for a user.
    This is a standalone version that doesn't require Flask context.
    """
    try:
        if start_date is None:
            start_date = date.today() - timedelta(days=7)  # Recalc last week by default
        
        # Get user's goofy_week_mode
        cursor.execute("SELECT goofy_week_mode FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        goofy_week_mode = bool(row[0]) if row else False
        
        # Get all dates that need recalculation
        cursor.execute("""
            SELECT date FROM totals_remainders_d
            WHERE user_id = %s AND date >= %s
            ORDER BY date ASC
        """, (user_id, start_date))
        all_dates = [row[0] for row in cursor.fetchall()]
        
        if not all_dates:
            logger.info(f"User {user_id}: No dates to recalculate")
            return True
        
        # Get previous day's remainder
        prev_date = start_date - timedelta(days=1)
        cursor.execute("""
            SELECT remainder FROM totals_remainders_d
            WHERE user_id = %s AND date = %s
        """, (user_id, prev_date))
        prev_row = cursor.fetchone()
        last_day_remainder = float(prev_row[0]) if prev_row else 0.0
        
        # Get all income entries
        cursor.execute("""
            SELECT ie.date, ie.amount FROM income_entries ie
            JOIN income_categories ic ON ie.category_id = ic.id
            WHERE ic.user_id = %s AND ie.date >= %s
        """, (user_id, start_date))
        income_by_date = {}
        for row in cursor.fetchall():
            entry_date = row[0]
            income_by_date[entry_date] = income_by_date.get(entry_date, 0) + float(row[1] or 0)
        
        # Get all expense entries
        cursor.execute("""
            SELECT ee.date, ee.amount FROM expense_entries ee
            JOIN expense_categories ec ON ee.category_id = ec.id
            WHERE ec.user_id = %s AND ee.date >= %s
        """, (user_id, start_date))
        expense_by_date = {}
        for row in cursor.fetchall():
            entry_date = row[0]
            expense_by_date[entry_date] = expense_by_date.get(entry_date, 0) + float(row[1] or 0)
        
        # Update each day
        updates = []
        for current_date in all_dates:
            total_income = income_by_date.get(current_date, 0) + last_day_remainder
            total_expenses = expense_by_date.get(current_date, 0)
            remainder = total_income - total_expenses
            
            updates.append((total_income, total_expenses, remainder, last_day_remainder, user_id, current_date))
            last_day_remainder = remainder
        
        # Batch update MySQL
        cursor.executemany("""
            UPDATE totals_remainders_d
            SET total_income = %s, total_expenses = %s, remainder = %s, last_day_remainder = %s
            WHERE user_id = %s AND date = %s
        """, updates)
        conn.commit()
        
        logger.info(f"User {user_id}: Recalculated {len(updates)} days of totals")
        
        # Update Redis if hydrated
        if is_user_hydrated(user_id):
            try:
                redis_key = get_redis_key('totals_remainders_d', user_id)
                cached = redis_client.get(redis_key)
                if cached:
                    totals = json.loads(cached)
                    # Update the cached data
                    totals_by_date = {t['date']: t for t in totals}
                    for (total_income, total_expenses, remainder, last_day_rem, uid, d) in updates:
                        date_str = d.isoformat() if hasattr(d, 'isoformat') else str(d)
                        if date_str in totals_by_date:
                            totals_by_date[date_str]['total_income'] = total_income
                            totals_by_date[date_str]['total_expenses'] = total_expenses
                            totals_by_date[date_str]['remainder'] = remainder
                            totals_by_date[date_str]['last_day_remainder'] = last_day_rem
                    redis_client.setex(redis_key, REDIS_TTL, json.dumps(list(totals_by_date.values()), cls=DecimalEncoder))
                    mark_dirty(user_id, 'totals_remainders_d')
            except Exception as e:
                logger.warning(f"User {user_id}: Error updating Redis totals: {e}")
        
        return True
        
    except Exception as e:
        logger.error(f"User {user_id}: Error recalculating totals: {e}")
        return False


def recalculate_savings_entries(cursor, conn, user_id, start_date=None):
    """
    Recalculate savings_entries based on remainder and adjustments.
    """
    try:
        if start_date is None:
            start_date = date.today() - timedelta(days=7)
        
        # Get user's starting_savings
        cursor.execute("SELECT starting_savings FROM users WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        starting_savings = float(row[0]) if row and row[0] else 0.0
        
        # Get savings adjustments
        cursor.execute("""
            SELECT date, amount FROM savings_adjustments
            WHERE user_id = %s AND date >= %s
            ORDER BY date ASC
        """, (user_id, start_date))
        adjustments_by_date = {row[0]: float(row[1]) for row in cursor.fetchall()}
        
        # Get daily remainders
        cursor.execute("""
            SELECT date, remainder FROM totals_remainders_d
            WHERE user_id = %s AND date >= %s
            ORDER BY date ASC
        """, (user_id, start_date))
        
        updates = []
        for row in cursor.fetchall():
            current_date = row[0]
            remainder = float(row[1]) if row[1] else 0.0
            
            # If there's an adjustment for this date, use that instead of calculated
            if current_date in adjustments_by_date:
                savings_amount = adjustments_by_date[current_date]
            else:
                # Savings = starting_savings + remainder
                savings_amount = starting_savings + remainder
            
            updates.append((savings_amount, user_id, current_date))
        
        # Update savings entries
        for (amount, uid, d) in updates:
            cursor.execute("""
                INSERT INTO savings_entries (user_id, date, amount, processed)
                VALUES (%s, %s, %s, 1)
                ON DUPLICATE KEY UPDATE amount = VALUES(amount)
            """, (uid, d, amount))
        
        conn.commit()
        logger.info(f"User {user_id}: Updated {len(updates)} savings entries")
        
        # Mark Redis dirty
        if is_user_hydrated(user_id):
            mark_dirty(user_id, 'savings_entries')
        
        return True
        
    except Exception as e:
        logger.error(f"User {user_id}: Error recalculating savings: {e}")
        return False


def process_user(cursor, conn, user_row):
    """Process a single user's nightly sync"""
    user_id = user_row[0]
    first_name = user_row[1]
    email = user_row[2]
    profile_id = user_row[3]
    session_token = user_row[4]
    session_expires = user_row[5]
    
    logger.info(f"Processing user {user_id} ({first_name or email})")
    
    result = {
        'user_id': user_id,
        'balances_updated': 0,
        'transactions_synced': 0,
        'transactions_imported': 0,
        'adjustments': []
    }
    
    # 1. Refresh session token if needed
    session_token = refresh_session_if_needed(cursor, conn, user_id, profile_id, session_token, session_expires)
    if not session_token:
        logger.error(f"User {user_id}: Could not get valid session token")
        return result
    
    # 2. Fetch and update account balances
    updated_accounts = fetch_and_update_balances(cursor, conn, user_id, session_token)
    result['balances_updated'] = len(updated_accounts)
    logger.info(f"User {user_id}: Updated {len(updated_accounts)} account balances")
    
    # 3. Get active accounts and sync transactions
    active_accounts = get_active_accounts(cursor, user_id)
    
    for acc in active_accounts:
        account_id = acc[0]
        account_name = acc[1]
        account_type = (acc[2] or '').lower()
        current_balance = acc[3]
        mask = acc[5]
        
        # Get last synced date
        cursor.execute("""
            SELECT qc.last_synced_at
            FROM quiltt_connections qc
            JOIN quiltt_accounts qa ON qc.id = qa.connection_id
            WHERE qa.user_id = %s AND qa.account_id = %s
        """, (user_id, account_id))
        sync_row = cursor.fetchone()
        last_synced = sync_row[0] if sync_row else None
        
        # Sync transactions
        new_txns, imported_txns = sync_transactions_for_account(
            cursor, conn, user_id, account_id, session_token, last_synced
        )
        result['transactions_synced'] += new_txns
        result['transactions_imported'] += imported_txns
        
        # 4. Auto-adjust based on account type
        if account_type == 'depository' and current_balance:
            account_name_lower = account_name.lower()
            
            if 'checking' in account_name_lower:
                # Checking account - create auto-adjustment
                success, msg = create_auto_adjustment(cursor, conn, user_id, current_balance, account_name)
                result['adjustments'].append({
                    'account': account_name,
                    'type': 'checking',
                    'success': success,
                    'message': msg
                })
                logger.info(f"User {user_id}: Checking adjustment - {msg}")
                
            elif 'savings' in account_name_lower:
                # Savings account - update savings balance
                success, msg = update_savings_balance(cursor, conn, user_id, current_balance, account_id)
                result['adjustments'].append({
                    'account': account_name,
                    'type': 'savings',
                    'success': success,
                    'message': msg
                })
                logger.info(f"User {user_id}: Savings update - {msg}")
    
    # 5. Recalculate totals and savings if any adjustments were made
    if result['adjustments']:
        # Only recalculate today since adjustments are for today
        recalc_start = date.today()
        recalculate_daily_totals(cursor, conn, user_id, recalc_start)
        recalculate_savings_entries(cursor, conn, user_id, recalc_start)
    
    return result


def main():
    """Main entry point"""
    logger.info("=" * 70)
    logger.info("Starting Nightly Balance Sync")
    logger.info(f"Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 70)
    
    start_time = datetime.now()
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get all Quiltt-enabled users
        users = get_quiltt_enabled_users(cursor)
        logger.info(f"Found {len(users)} users with Quiltt enabled")
        
        total_balances = 0
        total_txns_synced = 0
        total_txns_imported = 0
        total_adjustments = 0
        
        for user_row in users:
            try:
                result = process_user(cursor, conn, user_row)
                total_balances += result['balances_updated']
                total_txns_synced += result['transactions_synced']
                total_txns_imported += result['transactions_imported']
                total_adjustments += len(result['adjustments'])
            except Exception as e:
                logger.error(f"Error processing user {user_row[0]}: {e}")
                continue
        
        cursor.close()
        conn.close()
        
        elapsed = datetime.now() - start_time
        logger.info("-" * 70)
        logger.info(f"Nightly sync complete!")
        logger.info(f"Users processed: {len(users)}")
        logger.info(f"Account balances updated: {total_balances}")
        logger.info(f"Transactions synced: {total_txns_synced}")
        logger.info(f"Transactions imported: {total_txns_imported}")
        logger.info(f"Auto-adjustments made: {total_adjustments}")
        logger.info(f"Time elapsed: {elapsed}")
        logger.info("=" * 70)
        
    except Exception as e:
        logger.error(f"Fatal error in nightly sync: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
