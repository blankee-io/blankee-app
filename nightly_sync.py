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
    """Get a direct MySQL connection with buffered cursor support"""
    return mysql.connector.connect(
        host='localhost',
        user='ms_admin',
        password='dune6MEANTIME.ching_reek',
        database='budget',
        buffered=True  # Prevent unread result errors
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
        SELECT u.id, u.first_name, u.email, qp.profile_id, qp.session_token, qp.session_expires_at,
               u.goofy_week_mode
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
    """Get user's active Quiltt accounts with connection info"""
    cursor.execute("""
        SELECT qa.account_id, qa.account_name, qa.account_type, qa.current_balance, 
               qa.account_subtype, qa.mask, qa.created_at, qc.last_synced_at
        FROM quiltt_accounts qa
        JOIN quiltt_connections qc ON qa.connection_id = qc.id
        WHERE qa.user_id = %s AND qa.is_active = 1 AND qa.sync_transactions = 1
    """, (user_id,))
    return cursor.fetchall()


def sync_transactions_for_account(cursor, conn, user_id, account_id, session_token, last_synced_at, account_created_at):
    """Sync new transactions for a specific account.
    
    Args:
        last_synced_at: Last time this connection synced
        account_created_at: When the account was first connected to Blankee
        
    Transactions are synced from MAX(last_synced_at, account_created_at) to today.
    We never pull transactions from before the account was connected.
    """
    try:
        # Determine date range
        end_date = date.today()
        
        # Convert account_created_at to date if it's a datetime
        if account_created_at:
            created_date = account_created_at.date() if isinstance(account_created_at, datetime) else account_created_at
        else:
            created_date = end_date  # If no created_at, only sync today
        
        # Determine start date: MAX(last_synced_at, created_at)
        if last_synced_at:
            synced_date = last_synced_at.date() if isinstance(last_synced_at, datetime) else last_synced_at
            # Never sync before the account was created
            start_date = max(synced_date, created_date)
        else:
            # First sync - start from when account was created
            start_date = created_date
        
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
    1. Delete any existing auto-adjustment entries for yesterday (clean slate)
    2. Calculate the "natural" remainder (without auto-adjustments)
    3. Create ONE adjustment entry for the difference
    
    Note: Uses yesterday's date because nightly sync runs after midnight,
    and we're reconciling the previous day's ending balance.
    """
    try:
        # Use yesterday since nightly sync runs after midnight
        yesterday = date.today() - timedelta(days=1)
        target_date_str = yesterday.strftime('%Y-%m-%d')
        
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
        
        # Step 1: Delete existing auto-adjustment entries for target date
        cursor.execute("""
            DELETE FROM income_entries WHERE category_id = %s AND date = %s
        """, (income_cat_id, target_date_str))
        cursor.execute("""
            DELETE FROM expense_entries WHERE category_id = %s AND date = %s
        """, (expense_cat_id, target_date_str))
        conn.commit()
        
        # Step 2: Get day before target's remainder (starting point)
        day_before = yesterday - timedelta(days=1)
        cursor.execute("""
            SELECT remainder FROM totals_remainders_d
            WHERE user_id = %s AND date = %s
        """, (user_id, day_before.strftime('%Y-%m-%d')))
        row = cursor.fetchone()
        last_day_remainder = float(row[0]) if row else 0.0
        
        # Step 3: Calculate target date's natural income (excluding auto-adjustment)
        cursor.execute("""
            SELECT COALESCE(SUM(ie.amount), 0)
            FROM income_entries ie
            JOIN income_categories ic ON ie.category_id = ic.id
            WHERE ic.user_id = %s AND ie.date = %s AND ic.is_auto_adjustment = 0
        """, (user_id, target_date_str))
        row = cursor.fetchone()
        target_income = float(row[0]) if row else 0.0
        
        # Step 4: Calculate target date's natural expenses (excluding auto-adjustment)
        cursor.execute("""
            SELECT COALESCE(SUM(ee.amount), 0)
            FROM expense_entries ee
            JOIN expense_categories ec ON ee.category_id = ec.id
            WHERE ec.user_id = %s AND ee.date = %s AND ec.is_auto_adjustment = 0
        """, (user_id, target_date_str))
        row = cursor.fetchone()
        target_expenses = float(row[0]) if row else 0.0
        
        # Step 5: Calculate natural remainder
        natural_remainder = last_day_remainder + target_income - target_expenses
        
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
            """, (income_cat_id, target_date_str, abs(diff)))
            entry_type = 'income'
        else:
            cursor.execute("""
                INSERT INTO expense_entries (category_id, date, amount, processed)
                VALUES (%s, %s, %s, 1)
            """, (expense_cat_id, target_date_str, abs(diff)))
            entry_type = 'expense'
        
        conn.commit()
        
        # Clear Redis cache to prevent stale data from being flushed back
        # This is more reliable than marking dirty when we've updated MySQL directly
        if is_user_hydrated(user_id):
            income_key = get_redis_key('income_entries', user_id)
            expense_key = get_redis_key('expense_entries', user_id)
            redis_client.delete(income_key)
            redis_client.delete(expense_key)
            logger.debug(f"User {user_id}: Cleared Redis cache for income/expense entries")
        
        return True, f"Created {entry_type} adjustment of ${abs(diff):.2f}"
        
    except Exception as e:
        logger.error(f"User {user_id}: Error creating auto-adjustment: {e}")
        return False, str(e)


def update_savings_balance(cursor, conn, user_id, bank_balance, account_id=None):
    """
    Update YESTERDAY's savings balance from bank.
    Only updates yesterday's savings_entries to match the bank balance.
    Today and future dates remain as projected from budget data.
    Follows Redis-first architecture.
    
    Note: Uses yesterday's date because nightly sync runs after midnight,
    and we're recording the previous day's ending balance.
    """
    try:
        # Use yesterday since nightly sync runs after midnight
        yesterday = date.today() - timedelta(days=1)
        target_date_str = yesterday.strftime('%Y-%m-%d')
        
        # Store the adjustment record (for auditing/history)
        cursor.execute("""
            SELECT id FROM savings_adjustments
            WHERE user_id = %s AND date = %s
        """, (user_id, target_date_str))
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
            """, (user_id, target_date_str, bank_balance, 'Nightly sync from bank', account_id))
        
        conn.commit()
        
        # Update target date's savings entry directly
        if is_user_hydrated(user_id):
            # Redis-first: Update Redis
            try:
                redis_key = get_redis_key('savings_entries', user_id)
                cached = redis_client.get(redis_key)
                if cached:
                    savings_list = json.loads(cached)
                    savings_by_date = {s['date']: s for s in savings_list}
                    
                    # Update only target date's entry
                    if target_date_str in savings_by_date:
                        savings_by_date[target_date_str]['amount'] = float(bank_balance)
                    else:
                        savings_by_date[target_date_str] = {
                            'user_id': user_id,
                            'date': target_date_str,
                            'amount': float(bank_balance),
                            'processed': 1
                        }
                    
                    savings_list = list(savings_by_date.values())
                    redis_client.setex(redis_key, REDIS_TTL, json.dumps(savings_list, cls=DecimalEncoder))
                    mark_dirty(user_id, 'savings_entries')
                    mark_dirty(user_id, 'savings_adjustments')
            except Exception as e:
                logger.warning(f"User {user_id}: Redis update failed: {e}")
                # Fall back to MySQL
                cursor.execute("""
                    INSERT INTO savings_entries (user_id, date, amount, processed)
                    VALUES (%s, %s, %s, 1)
                    ON DUPLICATE KEY UPDATE amount = VALUES(amount)
                """, (user_id, target_date_str, bank_balance))
                conn.commit()
        else:
            # MySQL direct
            cursor.execute("""
                INSERT INTO savings_entries (user_id, date, amount, processed)
                VALUES (%s, %s, %s, 1)
                ON DUPLICATE KEY UPDATE amount = VALUES(amount)
            """, (user_id, target_date_str, bank_balance))
            conn.commit()
        
        return True, f"Updated savings to ${bank_balance:.2f}"
        
    except Exception as e:
        logger.error(f"User {user_id}: Error updating savings: {e}")
        return False, str(e)


# ============================================================================
# BUCKET ENTRY CLEANUP
# Remove expired bucket entries from the previous day
# ============================================================================

def cleanup_expired_bucket_entries(cursor, conn, user_id):
    """
    Remove bucket entries (is_bucket=1) from yesterday.
    
    Bucket entries are placeholders for expected recurring income/expenses.
    Once the day passes, these should be removed so they don't affect calculations.
    
    The nightly sync runs at 00:05, so we delete entries for yesterday.
    
    This directly updates MySQL, and also updates Redis if user is hydrated.
    
    Args:
        cursor: MySQL cursor
        conn: MySQL connection
        user_id: User ID
        
    Returns:
        dict with counts of deleted entries per table
    """
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    
    tables = [
        ('income_entries', 'income_categories'),
        ('expense_entries', 'expense_categories'),
        ('c_expense_entries', 'c_expense_categories')
    ]
    
    results = {}
    
    for entry_table, cat_table in tables:
        try:
            # First, get IDs of bucket entries to delete (for Redis cleanup)
            if entry_table == 'c_expense_entries':
                # Credit account categories link via account_id
                cursor.execute(f"""
                    SELECT e.id FROM {entry_table} e
                    JOIN {cat_table} c ON e.category_id = c.id
                    JOIN credit_accounts ca ON c.account_id = ca.id
                    WHERE ca.user_id = %s AND e.date = %s AND e.is_bucket = 1
                """, (user_id, yesterday))
            else:
                cursor.execute(f"""
                    SELECT e.id FROM {entry_table} e
                    JOIN {cat_table} c ON e.category_id = c.id
                    WHERE c.user_id = %s AND e.date = %s AND e.is_bucket = 1
                """, (user_id, yesterday))
            
            entry_ids = [row[0] for row in cursor.fetchall()]
            
            if not entry_ids:
                results[entry_table] = 0
                continue
            
            # Delete from MySQL
            placeholders = ','.join(['%s'] * len(entry_ids))
            cursor.execute(f"""
                DELETE FROM {entry_table} WHERE id IN ({placeholders})
            """, entry_ids)
            conn.commit()
            
            deleted_count = cursor.rowcount
            results[entry_table] = deleted_count
            
            # Update Redis if user is hydrated
            if is_user_hydrated(user_id):
                try:
                    redis_key = get_redis_key(entry_table, user_id)
                    redis_data = redis_client.get(redis_key)
                    
                    if redis_data:
                        entries_list = json.loads(redis_data)
                        entry_ids_set = set(entry_ids)
                        
                        # Filter out deleted entries
                        entries_list = [e for e in entries_list if e.get('id') not in entry_ids_set]
                        
                        # Save back to Redis
                        redis_client.setex(redis_key, REDIS_TTL, json.dumps(entries_list, cls=DecimalEncoder))
                        mark_dirty(user_id, entry_table)
                        
                except Exception as redis_err:
                    logger.warning(f"User {user_id}: Redis cleanup error for {entry_table}: {redis_err}")
            
            if deleted_count > 0:
                logger.info(f"User {user_id}: Deleted {deleted_count} expired bucket entries from {entry_table} for {yesterday}")
                
        except Exception as e:
            logger.error(f"User {user_id}: Error cleaning bucket entries from {entry_table}: {e}")
            results[entry_table] = 0
    
    return results


# ============================================================================
# RECALCULATION FUNCTIONS
# These recalculate totals/balances after auto-adjustments are made
# ============================================================================

def recalculate_daily_totals(cursor, conn, user_id, start_date, date_to_remainder, goofy_week_mode=False):
    """
    Recalculate totals_remainders_d from start_date forward.
    Updates both Redis (if user is hydrated) and MySQL.
    
    Args:
        cursor: MySQL cursor
        conn: MySQL connection
        user_id: User ID
        start_date: Date to start recalculation from
        date_to_remainder: Dict to store date->remainder mappings for subsequent calculations
        goofy_week_mode: Whether user uses Friday-start weeks
    """
    try:
        # Get all dates that need recalculation
        cursor.execute("""
            SELECT date FROM totals_remainders_d
            WHERE user_id = %s AND date >= %s
            ORDER BY date ASC
        """, (user_id, start_date))
        all_dates = [row[0] for row in cursor.fetchall()]
        
        if not all_dates:
            logger.debug(f"User {user_id}: No daily totals to recalculate from {start_date}")
            return

        # Get previous day's remainder
        prev_date = start_date - timedelta(days=1)
        cursor.execute("""
            SELECT remainder FROM totals_remainders_d
            WHERE user_id = %s AND date = %s
        """, (user_id, prev_date))
        row = cursor.fetchone()
        last_day_remainder = float(row[0]) if row and row[0] is not None else 0.0

        # Get all income entries (from Redis or MySQL)
        income_entries = get_entries_from_redis_or_mysql(cursor, 'income_entries', user_id)
        expense_entries = get_entries_from_redis_or_mysql(cursor, 'expense_entries', user_id)
        
        # Aggregate by date
        income_by_date = {}
        for entry in income_entries:
            entry_date = entry.get('date')
            if isinstance(entry_date, str):
                entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
            if entry_date >= start_date:
                income_by_date[entry_date] = income_by_date.get(entry_date, 0) + float(entry.get('amount', 0))
        
        expense_by_date = {}
        for entry in expense_entries:
            entry_date = entry.get('date')
            if isinstance(entry_date, str):
                entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
            if entry_date >= start_date:
                expense_by_date[entry_date] = expense_by_date.get(entry_date, 0) + float(entry.get('amount', 0))

        # Calculate and update each day
        updates = []
        for current_date in all_dates:
            total_income = income_by_date.get(current_date, 0) + last_day_remainder
            total_expenses = expense_by_date.get(current_date, 0)
            remainder = total_income - total_expenses
            
            updates.append({
                'date': current_date,
                'total_income': float(total_income),
                'total_expenses': float(total_expenses),
                'remainder': float(remainder),
                'last_day_remainder': float(last_day_remainder)
            })
            
            last_day_remainder = remainder
            date_to_remainder[current_date] = remainder

        # Update MySQL
        for update in updates:
            cursor.execute("""
                UPDATE totals_remainders_d
                SET total_income = %s, total_expenses = %s, remainder = %s, last_day_remainder = %s
                WHERE user_id = %s AND date = %s
            """, (update['total_income'], update['total_expenses'], update['remainder'], 
                  update['last_day_remainder'], user_id, update['date']))
        
        conn.commit()
        
        # Update Redis if user is hydrated
        if is_user_hydrated(user_id):
            update_totals_remainders_in_redis('totals_remainders_d', user_id, updates)
        
        logger.info(f"User {user_id}: Recalculated {len(updates)} daily totals from {start_date}")
        
    except Exception as e:
        logger.error(f"User {user_id}: Error recalculating daily totals: {e}")


def recalculate_weekly_totals(cursor, conn, user_id, start_date, date_to_remainder, goofy_week_mode=False):
    """
    Recalculate totals_remainders (weekly) from start_date forward.
    """
    try:
        # Get all week dates (Fridays in normal mode, or Saturdays in goofy mode)
        cursor.execute("""
            SELECT date FROM totals_remainders
            WHERE user_id = %s AND date >= %s
            ORDER BY date ASC
        """, (user_id, start_date))
        all_week_dates = [row[0] for row in cursor.fetchall()]
        
        if not all_week_dates:
            logger.debug(f"User {user_id}: No weekly totals to recalculate from {start_date}")
            return

        def get_week_range(week_date):
            if goofy_week_mode:
                week_start = week_date
                week_end = week_start + timedelta(days=6)
            else:
                week_end = week_date
                week_start = week_end - timedelta(days=6)
            return week_start, week_end

        # Get entries
        income_entries = get_entries_from_redis_or_mysql(cursor, 'income_entries', user_id)
        expense_entries = get_entries_from_redis_or_mysql(cursor, 'expense_entries', user_id)
        
        # Calculate date range
        earliest_week = min(all_week_dates)
        latest_week = max(all_week_dates)
        earliest_start, _ = get_week_range(earliest_week)
        _, latest_end = get_week_range(latest_week)
        
        # Aggregate by date
        income_by_date = {}
        for entry in income_entries:
            entry_date = entry.get('date')
            if isinstance(entry_date, str):
                entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
            if earliest_start <= entry_date <= latest_end:
                income_by_date[entry_date] = income_by_date.get(entry_date, 0) + float(entry.get('amount', 0))
        
        expense_by_date = {}
        for entry in expense_entries:
            entry_date = entry.get('date')
            if isinstance(entry_date, str):
                entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
            if earliest_start <= entry_date <= latest_end:
                expense_by_date[entry_date] = expense_by_date.get(entry_date, 0) + float(entry.get('amount', 0))

        # Calculate each week
        updates = []
        for week_date in all_week_dates:
            week_start, week_end = get_week_range(week_date)

            total_income = 0
            current_date = week_start
            while current_date <= week_end:
                total_income += income_by_date.get(current_date, 0)
                current_date += timedelta(days=1)
            
            total_expenses = 0
            current_date = week_start
            while current_date <= week_end:
                total_expenses += expense_by_date.get(current_date, 0)
                current_date += timedelta(days=1)

            prev_week_date = week_date - timedelta(days=7)
            # Try to get from date_to_remainder first, otherwise fetch from MySQL
            if prev_week_date in date_to_remainder:
                last_week_remainder = date_to_remainder[prev_week_date]
            else:
                # Fetch from MySQL - this handles the case where prev week wasn't recalculated
                cursor.execute("""
                    SELECT remainder FROM totals_remainders
                    WHERE user_id = %s AND date = %s
                """, (user_id, prev_week_date))
                row = cursor.fetchone()
                last_week_remainder = float(row[0]) if row and row[0] is not None else 0.0

            total_income_with_remainder = total_income + float(last_week_remainder)
            week_remainder = total_income_with_remainder - total_expenses

            updates.append({
                'date': week_date,
                'total_income': float(total_income_with_remainder),
                'total_expenses': float(total_expenses),
                'remainder': float(week_remainder),
                'last_week_remainder': float(last_week_remainder)
            })

            date_to_remainder[week_date] = week_remainder

        # Update MySQL
        for update in updates:
            cursor.execute("""
                UPDATE totals_remainders
                SET total_income = %s, total_expenses = %s, remainder = %s, last_week_remainder = %s
                WHERE user_id = %s AND date = %s
            """, (update['total_income'], update['total_expenses'], update['remainder'],
                  update['last_week_remainder'], user_id, update['date']))
        
        conn.commit()
        
        # Update Redis if user is hydrated
        if is_user_hydrated(user_id):
            update_totals_remainders_in_redis('totals_remainders', user_id, updates)
        
        logger.info(f"User {user_id}: Recalculated {len(updates)} weekly totals from {start_date}")
        
    except Exception as e:
        logger.error(f"User {user_id}: Error recalculating weekly totals: {e}")


def recalculate_monthly_totals(cursor, conn, user_id, start_date, date_to_remainder):
    """
    Recalculate totals_remainders_m (monthly) from start_date forward.
    """
    import calendar
    
    try:
        start_of_month = date(start_date.year, start_date.month, 1)
        
        cursor.execute("""
            SELECT MIN(date), MAX(date) FROM totals_remainders_d
            WHERE user_id = %s AND date >= %s
        """, (user_id, start_of_month))
        date_range = cursor.fetchone()
        
        if not date_range or not date_range[0]:
            return
            
        min_date, max_date = date_range[0], date_range[1]
        
        # Build list of months
        months_data = []
        current_year, current_month = min_date.year, min_date.month
        end_year, end_month = max_date.year, max_date.month
        
        while (current_year < end_year) or (current_year == end_year and current_month <= end_month):
            last_day = date(current_year, current_month, calendar.monthrange(current_year, current_month)[1])
            first_day = date(current_year, current_month, 1)
            
            months_data.append({
                'year': current_year,
                'month': current_month,
                'first_day': first_day,
                'last_day': last_day
            })
            
            if current_month == 12:
                current_month = 1
                current_year += 1
            else:
                current_month += 1
        
        months_data = [m for m in months_data if m['first_day'] >= start_of_month]
        
        if not months_data:
            return
        
        # Get entries
        income_entries = get_entries_from_redis_or_mysql(cursor, 'income_entries', user_id)
        expense_entries = get_entries_from_redis_or_mysql(cursor, 'expense_entries', user_id)
        
        first_day = months_data[0]['first_day']
        last_day = months_data[-1]['last_day']
        
        # Aggregate by month
        income_by_month = {}
        for entry in income_entries:
            entry_date = entry.get('date')
            if isinstance(entry_date, str):
                entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
            if first_day <= entry_date <= last_day:
                key = (entry_date.year, entry_date.month)
                income_by_month[key] = income_by_month.get(key, 0) + float(entry.get('amount', 0))
        
        expense_by_month = {}
        for entry in expense_entries:
            entry_date = entry.get('date')
            if isinstance(entry_date, str):
                entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
            if first_day <= entry_date <= last_day:
                key = (entry_date.year, entry_date.month)
                expense_by_month[key] = expense_by_month.get(key, 0) + float(entry.get('amount', 0))

        # Calculate each month
        updates = []
        for month_info in months_data:
            year = month_info['year']
            month = month_info['month']
            last_day_of_month = month_info['last_day']
            
            total_income = income_by_month.get((year, month), 0.0)
            total_expenses = expense_by_month.get((year, month), 0.0)
            
            prev_month = (month - 1) or 12
            prev_year = year if month > 1 else year - 1
            prev_last_day = date(prev_year, prev_month, calendar.monthrange(prev_year, prev_month)[1])
            
            last_month_remainder = date_to_remainder.get(prev_last_day, 0.0)
            
            # If not in date_to_remainder, try to get from DB
            if prev_last_day not in date_to_remainder:
                cursor.execute("""
                    SELECT remainder FROM totals_remainders_m
                    WHERE user_id = %s AND date = %s
                """, (user_id, prev_last_day))
                prev_row = cursor.fetchone()
                if prev_row and prev_row[0] is not None:
                    last_month_remainder = float(prev_row[0])

            total_income_with_remainder = total_income + last_month_remainder
            remainder = total_income_with_remainder - total_expenses
            
            updates.append({
                'date': last_day_of_month,
                'total_income': float(total_income_with_remainder),
                'total_expenses': float(total_expenses),
                'remainder': float(remainder),
                'last_month_remainder': float(last_month_remainder)
            })
            
            date_to_remainder[last_day_of_month] = remainder

        # Update MySQL
        for update in updates:
            cursor.execute("""
                UPDATE totals_remainders_m
                SET total_income = %s, total_expenses = %s, remainder = %s, last_month_remainder = %s
                WHERE user_id = %s AND date = %s
            """, (update['total_income'], update['total_expenses'], update['remainder'],
                  update['last_month_remainder'], user_id, update['date']))
        
        conn.commit()
        
        # Update Redis if user is hydrated
        if is_user_hydrated(user_id):
            update_totals_remainders_in_redis('totals_remainders_m', user_id, updates)
        
        logger.info(f"User {user_id}: Recalculated {len(updates)} monthly totals from {start_date}")
        
    except Exception as e:
        logger.error(f"User {user_id}: Error recalculating monthly totals: {e}")


def recalculate_savings(cursor, conn, user_id, start_date):
    """
    Recalculate savings_entries from start_date forward.
    """
    try:
        # Get member_since and starting_savings
        cursor.execute("SELECT member_since, starting_savings FROM users WHERE id = %s", (user_id,))
        user_row = cursor.fetchone()
        if user_row and user_row[0]:
            member_since = user_row[0]
            starting_savings = float(user_row[1]) if user_row[1] is not None else 0.0
        else:
            member_since = start_date
            starting_savings = 0.0

        # Get savings category IDs
        cursor.execute("""
            SELECT 'income' as type, id FROM income_categories 
            WHERE user_id = %s AND name = 'Savings'
            UNION ALL
            SELECT 'expense' as type, id FROM expense_categories 
            WHERE user_id = %s AND name = 'Savings'
        """, (user_id, user_id))
        
        income_savings_id = None
        expense_savings_id = None
        
        for row in cursor.fetchall():
            if row[0] == 'income':
                income_savings_id = row[1]
            else:
                expense_savings_id = row[1]

        if not income_savings_id and not expense_savings_id:
            return

        # Get all dates
        cursor.execute("""
            SELECT date FROM totals_remainders_d
            WHERE user_id = %s AND date >= %s
            ORDER BY date ASC
        """, (user_id, start_date))
        all_dates = [row[0] for row in cursor.fetchall()]
        
        if not all_dates:
            return

        # Get previous day's savings
        prev_date = start_date - timedelta(days=1)
        cursor.execute("""
            SELECT amount FROM savings_entries
            WHERE user_id = %s AND date = %s
        """, (user_id, prev_date))
        prev_row = cursor.fetchone()
        last_savings = float(prev_row[0]) if prev_row and prev_row[0] is not None else 0.0

        # Get entries
        income_entries = get_entries_from_redis_or_mysql(cursor, 'income_entries', user_id)
        expense_entries = get_entries_from_redis_or_mysql(cursor, 'expense_entries', user_id)
        
        # Get savings adjustments
        cursor.execute("""
            SELECT date, SUM(amount) as total_amount
            FROM savings_adjustments
            WHERE user_id = %s
            GROUP BY date
        """, (user_id,))
        adjustment_by_date = {}
        for row in cursor.fetchall():
            adj_date = row[0]
            if isinstance(adj_date, str):
                adj_date = datetime.strptime(adj_date, '%Y-%m-%d').date()
            adjustment_by_date[adj_date] = float(row[1])
        
        # Aggregate by date
        min_date = min(all_dates)
        max_date = max(all_dates)
        
        income_by_date = {}
        if income_savings_id:
            for entry in income_entries:
                if int(entry.get('category_id', 0)) == int(income_savings_id):
                    entry_date = entry.get('date')
                    if isinstance(entry_date, str):
                        entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
                    if min_date <= entry_date <= max_date:
                        income_by_date[entry_date] = income_by_date.get(entry_date, 0.0) + float(entry.get('amount', 0))
        
        expense_by_date = {}
        if expense_savings_id:
            for entry in expense_entries:
                if int(entry.get('category_id', 0)) == int(expense_savings_id):
                    entry_date = entry.get('date')
                    if isinstance(entry_date, str):
                        entry_date = datetime.strptime(entry_date, '%Y-%m-%d').date()
                    if min_date <= entry_date <= max_date:
                        expense_by_date[entry_date] = expense_by_date.get(entry_date, 0.0) + float(entry.get('amount', 0))

        # Calculate and update
        updates = []
        for current_date in all_dates:
            total_income = income_by_date.get(current_date, 0.0)
            total_expenses = expense_by_date.get(current_date, 0.0)
            
            # Check if there's a bank balance adjustment for this date
            # Adjustments OVERRIDE the calculated savings (they represent actual bank balance)
            if current_date in adjustment_by_date:
                # Use the bank balance as the savings value
                savings = adjustment_by_date[current_date]
            else:
                # Calculate normally from previous day
                savings = last_savings + total_expenses - total_income
            
            updates.append({
                'date': current_date,
                'amount': float(savings)
            })
            last_savings = savings

        # Update MySQL
        for update in updates:
            cursor.execute("""
                INSERT INTO savings_entries (user_id, date, amount, processed)
                VALUES (%s, %s, %s, 1)
                ON DUPLICATE KEY UPDATE amount = %s, processed = 1
            """, (user_id, update['date'], update['amount'], update['amount']))
        
        conn.commit()
        
        # Update Redis if user is hydrated
        if is_user_hydrated(user_id):
            set_savings_entries_to_redis(user_id, updates)
        
        logger.info(f"User {user_id}: Recalculated {len(updates)} savings entries from {start_date}")
        
    except Exception as e:
        logger.error(f"User {user_id}: Error recalculating savings: {e}")


def recalculate_ca_daily_balances(cursor, conn, user_id, start_date):
    """
    Recalculate c_a_balances_d (daily credit account balances) from start_date forward.
    """
    try:
        # Get all credit accounts for this user
        cursor.execute("SELECT id FROM credit_accounts WHERE user_id = %s", (user_id,))
        account_ids = [row[0] for row in cursor.fetchall()]
        if not account_ids:
            return

        all_redis_updates = []

        for account_id in account_ids:
            # Get date range
            cursor.execute("""
                SELECT MIN(date) as min_date, MAX(date) as max_date FROM c_a_balances_d
                WHERE account_id = %s AND date >= %s
            """, (account_id, start_date))
            date_range = cursor.fetchone()
            
            if not date_range or not date_range[0]:
                continue
                
            min_date, max_date = date_range[0], date_range[1]
            
            # Get all dates
            cursor.execute("""
                SELECT date FROM c_a_balances_d
                WHERE account_id = %s AND date BETWEEN %s AND %s
                ORDER BY date ASC
            """, (account_id, min_date, max_date))
            all_dates = [row[0] for row in cursor.fetchall()]
            
            if not all_dates:
                continue

            # Get previous balance
            prev_date = min_date - timedelta(days=1)
            cursor.execute("""
                SELECT balance FROM c_a_balances_d
                WHERE account_id = %s AND date = %s
            """, (account_id, prev_date))
            prev_row = cursor.fetchone()
            last_day_balance = float(prev_row[0]) if prev_row and prev_row[0] is not None else 0.0

            # Get expenses for this account
            cursor.execute("""
                SELECT cee.date, SUM(cee.amount) as total
                FROM c_expense_entries cee
                JOIN c_expense_categories cec ON cee.category_id = cec.id
                WHERE cec.account_id = %s AND cee.date BETWEEN %s AND %s
                GROUP BY cee.date
            """, (account_id, min_date, max_date))
            expense_by_date = {row[0]: float(row[1]) for row in cursor.fetchall()}

            # Get payments for this account
            cursor.execute("""
                SELECT date, SUM(amount) as total
                FROM c_payment_entries
                WHERE account_id = %s AND date BETWEEN %s AND %s
                GROUP BY date
            """, (account_id, min_date, max_date))
            payments_by_date = {row[0]: float(row[1]) for row in cursor.fetchall()}

            # Calculate each day
            updates = []
            for current_date in all_dates:
                total_expenses = expense_by_date.get(current_date, 0.0)
                total_payments = payments_by_date.get(current_date, 0.0)
                balance = last_day_balance + total_expenses - total_payments
                
                updates.append({
                    'account_id': account_id,
                    'date': current_date,
                    'total_expenses': float(total_expenses),
                    'total_payments': float(total_payments),
                    'balance': float(balance)
                })
                
                last_day_balance = balance

            # Update MySQL
            for update in updates:
                cursor.execute("""
                    UPDATE c_a_balances_d
                    SET total_expenses = %s, total_payments = %s, balance = %s
                    WHERE account_id = %s AND date = %s
                """, (update['total_expenses'], update['total_payments'], update['balance'],
                      update['account_id'], update['date']))
            
            all_redis_updates.extend(updates)
        
        conn.commit()
        
        # Update Redis if user is hydrated
        if is_user_hydrated(user_id) and all_redis_updates:
            set_ca_balances_to_redis('c_a_balances_d', user_id, all_redis_updates)
        
        logger.info(f"User {user_id}: Recalculated {len(all_redis_updates)} daily CA balances from {start_date}")
        
    except Exception as e:
        logger.error(f"User {user_id}: Error recalculating daily CA balances: {e}")


def recalculate_ca_weekly_balances(cursor, conn, user_id, start_date, goofy_week_mode=False):
    """
    Recalculate c_a_balances (weekly credit account balances) from start_date forward.
    """
    try:
        cursor.execute("SELECT id FROM credit_accounts WHERE user_id = %s", (user_id,))
        account_ids = [row[0] for row in cursor.fetchall()]
        if not account_ids:
            return

        def get_week_range(week_date):
            if goofy_week_mode:
                week_start = week_date
                week_end = week_start + timedelta(days=6)
            else:
                week_end = week_date
                week_start = week_end - timedelta(days=6)
            return week_start, week_end

        all_redis_updates = []

        for account_id in account_ids:
            cursor.execute("""
                SELECT date FROM c_a_balances
                WHERE account_id = %s AND date >= %s
                ORDER BY date ASC
            """, (account_id, start_date))
            all_week_dates = [row[0] for row in cursor.fetchall()]
            
            if not all_week_dates:
                continue
            
            earliest_week = min(all_week_dates)
            latest_week = max(all_week_dates)
            earliest_start, _ = get_week_range(earliest_week)
            _, latest_end = get_week_range(latest_week)

            # Get expenses
            cursor.execute("""
                SELECT cee.date, SUM(cee.amount) as total
                FROM c_expense_entries cee
                JOIN c_expense_categories cec ON cee.category_id = cec.id
                WHERE cec.account_id = %s AND cee.date BETWEEN %s AND %s
                GROUP BY cee.date
            """, (account_id, earliest_start, latest_end))
            expenses_by_date = {row[0]: float(row[1]) for row in cursor.fetchall()}

            # Get payments
            cursor.execute("""
                SELECT date, SUM(amount) as total
                FROM c_payment_entries
                WHERE account_id = %s AND date BETWEEN %s AND %s
                GROUP BY date
            """, (account_id, earliest_start, latest_end))
            payments_by_date = {row[0]: float(row[1]) for row in cursor.fetchall()}

            # Get previous week balance
            prev_week = earliest_week - timedelta(days=7)
            cursor.execute("""
                SELECT balance FROM c_a_balances
                WHERE account_id = %s AND date = %s
            """, (account_id, prev_week))
            prev_row = cursor.fetchone()
            last_week_balance = float(prev_row[0]) if prev_row and prev_row[0] is not None else 0.0

            updates = []
            for week_date in all_week_dates:
                week_start, week_end = get_week_range(week_date)
                
                total_expenses = 0
                total_payments = 0
                current_date = week_start
                while current_date <= week_end:
                    total_expenses += expenses_by_date.get(current_date, 0)
                    total_payments += payments_by_date.get(current_date, 0)
                    current_date += timedelta(days=1)
                
                balance = last_week_balance + total_expenses - total_payments
                
                updates.append({
                    'account_id': account_id,
                    'date': week_date,
                    'total_expenses': float(total_expenses),
                    'total_payments': float(total_payments),
                    'balance': float(balance)
                })
                
                last_week_balance = balance

            # Update MySQL
            for update in updates:
                cursor.execute("""
                    UPDATE c_a_balances
                    SET total_expenses = %s, total_payments = %s, balance = %s
                    WHERE account_id = %s AND date = %s
                """, (update['total_expenses'], update['total_payments'], update['balance'],
                      update['account_id'], update['date']))
            
            all_redis_updates.extend(updates)
        
        conn.commit()
        
        if is_user_hydrated(user_id) and all_redis_updates:
            set_ca_balances_to_redis('c_a_balances', user_id, all_redis_updates)
        
        logger.info(f"User {user_id}: Recalculated {len(all_redis_updates)} weekly CA balances from {start_date}")
        
    except Exception as e:
        logger.error(f"User {user_id}: Error recalculating weekly CA balances: {e}")


def recalculate_ca_monthly_balances(cursor, conn, user_id, start_date):
    """
    Recalculate c_a_balances_m (monthly credit account balances) from start_date forward.
    """
    import calendar
    
    try:
        cursor.execute("SELECT id FROM credit_accounts WHERE user_id = %s", (user_id,))
        account_ids = [row[0] for row in cursor.fetchall()]
        if not account_ids:
            return

        all_redis_updates = []

        for account_id in account_ids:
            cursor.execute("""
                SELECT date FROM c_a_balances_m
                WHERE account_id = %s AND date >= %s
                ORDER BY date ASC
            """, (account_id, start_date))
            all_month_dates = [row[0] for row in cursor.fetchall()]
            
            if not all_month_dates:
                continue

            # Get expenses for entire date range
            min_date = min(all_month_dates).replace(day=1)
            max_date = max(all_month_dates)
            
            cursor.execute("""
                SELECT cee.date, SUM(cee.amount) as total
                FROM c_expense_entries cee
                JOIN c_expense_categories cec ON cee.category_id = cec.id
                WHERE cec.account_id = %s AND cee.date BETWEEN %s AND %s
                GROUP BY cee.date
            """, (account_id, min_date, max_date))
            expenses_by_date = {row[0]: float(row[1]) for row in cursor.fetchall()}

            cursor.execute("""
                SELECT date, SUM(amount) as total
                FROM c_payment_entries
                WHERE account_id = %s AND date BETWEEN %s AND %s
                GROUP BY date
            """, (account_id, min_date, max_date))
            payments_by_date = {row[0]: float(row[1]) for row in cursor.fetchall()}

            # Get previous month balance
            first_month_date = min(all_month_dates)
            prev_year = first_month_date.year if first_month_date.month > 1 else first_month_date.year - 1
            prev_month = (first_month_date.month - 1) or 12
            prev_last_day = date(prev_year, prev_month, calendar.monthrange(prev_year, prev_month)[1])
            
            cursor.execute("""
                SELECT balance FROM c_a_balances_m
                WHERE account_id = %s AND date = %s
            """, (account_id, prev_last_day))
            prev_row = cursor.fetchone()
            last_month_balance = float(prev_row[0]) if prev_row and prev_row[0] is not None else 0.0

            updates = []
            for month_date in all_month_dates:
                # Calculate month range
                month_start = month_date.replace(day=1)
                month_end = month_date
                
                total_expenses = 0
                total_payments = 0
                current_date = month_start
                while current_date <= month_end:
                    total_expenses += expenses_by_date.get(current_date, 0)
                    total_payments += payments_by_date.get(current_date, 0)
                    current_date += timedelta(days=1)
                
                balance = last_month_balance + total_expenses - total_payments
                
                updates.append({
                    'account_id': account_id,
                    'date': month_date,
                    'total_expenses': float(total_expenses),
                    'total_payments': float(total_payments),
                    'balance': float(balance)
                })
                
                last_month_balance = balance

            # Update MySQL
            for update in updates:
                cursor.execute("""
                    UPDATE c_a_balances_m
                    SET total_expenses = %s, total_payments = %s, balance = %s
                    WHERE account_id = %s AND date = %s
                """, (update['total_expenses'], update['total_payments'], update['balance'],
                      update['account_id'], update['date']))
            
            all_redis_updates.extend(updates)
        
        conn.commit()
        
        if is_user_hydrated(user_id) and all_redis_updates:
            set_ca_balances_to_redis('c_a_balances_m', user_id, all_redis_updates)
        
        logger.info(f"User {user_id}: Recalculated {len(all_redis_updates)} monthly CA balances from {start_date}")
        
    except Exception as e:
        logger.error(f"User {user_id}: Error recalculating monthly CA balances: {e}")


# ============================================================================
# HELPER FUNCTIONS FOR RECALCULATION
# ============================================================================

def get_entries_from_redis_or_mysql(cursor, table_name, user_id):
    """
    Get entries from Redis if user is hydrated, otherwise from MySQL.
    """
    if is_user_hydrated(user_id):
        key = f"{table_name}:{REDIS_KEY_VERSION}:{user_id}"
        try:
            data = redis_client.get(key)
            if data:
                return json.loads(data)
        except Exception:
            pass
    
    # Fallback to MySQL
    if table_name == 'income_entries':
        cursor.execute("""
            SELECT ie.id, ie.category_id, ie.date, ie.amount, ie.recurring_id, 
                   ie.is_bucket, ie.original_amount, ie.processed
            FROM income_entries ie
            JOIN income_categories ic ON ie.category_id = ic.id
            WHERE ic.user_id = %s
        """, (user_id,))
    elif table_name == 'expense_entries':
        cursor.execute("""
            SELECT ee.id, ee.category_id, ee.date, ee.amount, ee.recurring_id,
                   ee.is_bucket, ee.original_amount, ee.processed, ee.bud_item_id
            FROM expense_entries ee
            JOIN expense_categories ec ON ee.category_id = ec.id
            WHERE ec.user_id = %s
        """, (user_id,))
    else:
        return []
    
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def update_totals_remainders_in_redis(table_name, user_id, updates):
    """
    Update totals_remainders in Redis.
    """
    try:
        key = f"{table_name}:{REDIS_KEY_VERSION}:{user_id}"
        
        # Get existing data
        existing_data = redis_client.get(key)
        if existing_data:
            data = json.loads(existing_data)
        else:
            data = []
        
        # Convert to dict by date for easy lookup
        data_by_date = {}
        for item in data:
            item_date = item.get('date')
            if isinstance(item_date, str):
                item_date = datetime.strptime(item_date[:10], '%Y-%m-%d').date()
            data_by_date[str(item_date)] = item
        
        # Update with new data
        for update in updates:
            update_date = update.get('date')
            if hasattr(update_date, 'isoformat'):
                update_date = update_date.isoformat()
            data_by_date[update_date] = update
        
        # Convert back to list
        updated_data = list(data_by_date.values())
        
        redis_client.setex(key, REDIS_TTL, json.dumps(updated_data, cls=DecimalEncoder))
        
        # Mark as dirty
        dirty_key = f"dirty_tables:{user_id}"
        redis_client.sadd(dirty_key, table_name)
        redis_client.expire(dirty_key, REDIS_TTL)
        
    except Exception as e:
        logger.warning(f"Error updating {table_name} in Redis for user {user_id}: {e}")


def set_savings_entries_to_redis(user_id, updates):
    """
    Set savings_entries in Redis.
    """
    try:
        key = f"savings_entries:{REDIS_KEY_VERSION}:{user_id}"
        
        # Get existing data
        existing_data = redis_client.get(key)
        if existing_data:
            data = json.loads(existing_data)
        else:
            data = []
        
        # Convert to dict by date
        data_by_date = {}
        for item in data:
            item_date = item.get('date')
            if isinstance(item_date, str):
                item_date = datetime.strptime(item_date[:10], '%Y-%m-%d').date()
            data_by_date[str(item_date)] = item
        
        # Update with new data
        for update in updates:
            update_date = update.get('date')
            if hasattr(update_date, 'isoformat'):
                update_date = update_date.isoformat()
            data_by_date[update_date] = {'date': update_date, 'amount': update['amount'], 'user_id': user_id}
        
        updated_data = list(data_by_date.values())
        
        redis_client.setex(key, REDIS_TTL, json.dumps(updated_data, cls=DecimalEncoder))
        
        dirty_key = f"dirty_tables:{user_id}"
        redis_client.sadd(dirty_key, 'savings_entries')
        redis_client.expire(dirty_key, REDIS_TTL)
        
    except Exception as e:
        logger.warning(f"Error setting savings_entries in Redis for user {user_id}: {e}")


def set_ca_balances_to_redis(table_name, user_id, updates):
    """
    Set credit account balances in Redis.
    """
    try:
        key = f"{table_name}:{REDIS_KEY_VERSION}:{user_id}"
        
        # Get existing data
        existing_data = redis_client.get(key)
        if existing_data:
            data = json.loads(existing_data)
        else:
            data = []
        
        # Convert to dict by (account_id, date) for lookup
        data_by_key = {}
        for item in data:
            account_id = item.get('account_id')
            item_date = item.get('date')
            if isinstance(item_date, str):
                item_date = datetime.strptime(item_date[:10], '%Y-%m-%d').date()
            data_by_key[(account_id, str(item_date))] = item
        
        # Update with new data
        for update in updates:
            account_id = update.get('account_id')
            update_date = update.get('date')
            if hasattr(update_date, 'isoformat'):
                update_date = update_date.isoformat()
            data_by_key[(account_id, update_date)] = update
        
        updated_data = list(data_by_key.values())
        
        redis_client.setex(key, REDIS_TTL, json.dumps(updated_data, cls=DecimalEncoder))
        
        dirty_key = f"dirty_tables:{user_id}"
        redis_client.sadd(dirty_key, table_name)
        redis_client.expire(dirty_key, REDIS_TTL)
        
    except Exception as e:
        logger.warning(f"Error setting {table_name} in Redis for user {user_id}: {e}")


def create_credit_account_auto_adjustment(cursor, conn, user_id, quiltt_account_id, bank_balance, account_mask, account_name):
    """
    Create an auto-adjustment entry for a credit account to match the bank balance.
    Similar to app.py's _create_credit_account_auto_adjustment but for standalone use.
    
    Logic:
    1. Delete any existing auto-adjustment entries for yesterday (clean slate)
    2. Calculate the "natural" balance (without auto-adjustments)
    3. Create ONE adjustment entry for the difference
    
    Note: Uses yesterday's date because nightly sync runs after midnight,
    and we're reconciling the previous day's ending balance.
    
    Args:
        cursor: DB cursor
        conn: DB connection
        user_id: User ID
        quiltt_account_id: Quiltt account ID
        bank_balance: Current balance from the bank (positive = amount owed)
        account_mask: Account mask for fallback matching
        account_name: Account name for logging
        
    Returns: (success: bool, message: str)
    """
    try:
        # Use yesterday since nightly sync runs after midnight
        yesterday = date.today() - timedelta(days=1)
        target_date_str = yesterday.strftime('%Y-%m-%d')
        day_before = yesterday - timedelta(days=1)
        day_before_str = day_before.strftime('%Y-%m-%d')
        
        # Find the credit account by quiltt_account_id
        cursor.execute("""
            SELECT id, starting_balance FROM credit_accounts
            WHERE user_id = %s AND quiltt_account_id = %s
        """, (user_id, quiltt_account_id))
        ca_row = cursor.fetchone()
        
        if not ca_row:
            # Fallback: try by mask
            cursor.execute("""
                SELECT id, starting_balance FROM credit_accounts
                WHERE user_id = %s AND mask = %s
            """, (user_id, account_mask))
            ca_row = cursor.fetchone()
        
        if not ca_row:
            return False, f"No credit account found for {account_name}"
        
        account_id = ca_row[0]
        starting_balance = float(ca_row[1] or 0)
        
        # Find auto_adjustment category for this account (or create it)
        cursor.execute("""
            SELECT id FROM c_expense_categories
            WHERE account_id = %s AND is_auto_adjustment = 1
        """, (account_id,))
        cat_row = cursor.fetchone()
        
        if not cat_row:
            # Create Uncategorized category with auto_adjustment flag
            cursor.execute("""
                INSERT INTO c_expense_categories (account_id, name, display_order, is_auto_adjustment)
                VALUES (%s, 'Uncategorized', 0, 1)
            """, (account_id,))
            conn.commit()
            auto_adj_category_id = cursor.lastrowid
            logger.info(f"User {user_id}: Created Uncategorized category {auto_adj_category_id} for account {account_id}")
        else:
            auto_adj_category_id = cat_row[0]
        
        # Step 1: Delete existing auto-adjustment entries for target date
        # Delete expenses from auto-adjustment category
        cursor.execute("""
            DELETE FROM c_expense_entries 
            WHERE category_id = %s AND date = %s
        """, (auto_adj_category_id, target_date_str))
        
        # Delete payments for this account for target date (payments can also be auto-adjustments)
        # We need to be careful here - only delete "adjustment" payments, not real payments
        # For now, we'll track by checking if there's a payment that matches our adjustment pattern
        # Actually, let's use a different approach: delete ALL target date's entries and recalculate fresh
        # This is safer and matches how checking works
        cursor.execute("""
            DELETE FROM c_payment_entries 
            WHERE account_id = %s AND date = %s AND recurring_id IS NULL
        """, (account_id, target_date_str))
        conn.commit()
        
        # Also remove from Redis if hydrated
        if is_user_hydrated(user_id):
            try:
                # Remove target date's auto-adjustment expenses from Redis
                redis_key = get_redis_key('c_expense_entries', user_id)
                cached = redis_client.get(redis_key)
                if cached:
                    entries = json.loads(cached)
                    entries = [e for e in entries if not (
                        e.get('category_id') == auto_adj_category_id and 
                        e.get('date') == target_date_str
                    )]
                    redis_client.setex(redis_key, REDIS_TTL, json.dumps(entries, cls=DecimalEncoder))
                
                # Remove target date's non-recurring payments from Redis
                redis_key = get_redis_key('c_payment_entries', user_id)
                cached = redis_client.get(redis_key)
                if cached:
                    entries = json.loads(cached)
                    entries = [e for e in entries if not (
                        e.get('account_id') == account_id and 
                        e.get('date') == target_date_str and
                        e.get('recurring_id') is None
                    )]
                    redis_client.setex(redis_key, REDIS_TTL, json.dumps(entries, cls=DecimalEncoder))
            except Exception as e:
                logger.warning(f"User {user_id}: Redis cleanup error: {e}")
        
        # Step 2: Get day before target's balance (starting point for natural calculation)
        cursor.execute("""
            SELECT balance FROM c_a_balances_d
            WHERE account_id = %s AND date = %s
        """, (account_id, day_before_str))
        result = cursor.fetchone()
        day_before_balance = float(result[0]) if result else starting_balance
        
        # Step 3: Calculate target date's natural expenses (excluding auto-adjustment category)
        cursor.execute("""
            SELECT COALESCE(SUM(cee.amount), 0)
            FROM c_expense_entries cee
            JOIN c_expense_categories cec ON cee.category_id = cec.id
            WHERE cec.account_id = %s AND cee.date = %s AND cec.is_auto_adjustment = 0
        """, (account_id, target_date_str))
        row = cursor.fetchone()
        target_expenses = float(row[0]) if row else 0.0
        
        # Step 4: Calculate target date's natural payments
        cursor.execute("""
            SELECT COALESCE(SUM(amount), 0)
            FROM c_payment_entries
            WHERE account_id = %s AND date = %s
        """, (account_id, target_date_str))
        row = cursor.fetchone()
        target_payments = float(row[0]) if row else 0.0
        
        # Step 5: Calculate natural balance
        # Balance = day before's balance + target date's expenses - target date's payments
        natural_balance = day_before_balance + target_expenses - target_payments
        
        # Step 6: Determine needed adjustment
        bank_balance_float = abs(float(bank_balance))  # Bank balance should be positive (amount owed)
        diff = bank_balance_float - natural_balance
        
        logger.info(f"User {user_id}: {account_name} - Yesterday=${day_before_balance:.2f}, Natural=${natural_balance:.2f}, Bank=${bank_balance_float:.2f}, Diff=${diff:.2f}")
        
        # Skip if difference is negligible
        if abs(diff) < 0.01:
            return True, f"No adjustment needed - balance already matches (${bank_balance_float:.2f})"
        
        adjustment_amount = abs(diff)
        
        # Step 7: Create single adjustment entry
        if diff > 0:
            # Balance needs to go UP - create expense entry
            if is_user_hydrated(user_id):
                try:
                    redis_key = get_redis_key('c_expense_entries', user_id)
                    cached = redis_client.get(redis_key)
                    entries = json.loads(cached) if cached else []
                    
                    existing_ids = [int(e.get('id', 0)) for e in entries if e.get('id')]
                    min_id = min(existing_ids) if existing_ids else 0
                    temp_id = min_id - 1 if min_id <= 0 else -1
                    
                    entries.append({
                        'id': temp_id,
                        'category_id': auto_adj_category_id,
                        'date': target_date_str,
                        'amount': float(adjustment_amount),
                        'recurring_id': None,
                        'is_bucket': 0,
                        'original_amount': None,
                        'processed': 1,
                        'bud_item_id': None
                    })
                    
                    redis_client.setex(redis_key, REDIS_TTL, json.dumps(entries, cls=DecimalEncoder))
                    mark_dirty(user_id, 'c_expense_entries')
                except Exception as e:
                    logger.warning(f"User {user_id}: Redis error, falling back to MySQL: {e}")
                    cursor.execute("""
                        INSERT INTO c_expense_entries (category_id, date, amount, processed)
                        VALUES (%s, %s, %s, 1)
                    """, (auto_adj_category_id, target_date_str, adjustment_amount))
                    conn.commit()
            else:
                cursor.execute("""
                    INSERT INTO c_expense_entries (category_id, date, amount, processed)
                    VALUES (%s, %s, %s, 1)
                """, (auto_adj_category_id, target_date_str, adjustment_amount))
                conn.commit()
            
            return True, f"Created expense adjustment +${adjustment_amount:.2f}"
        
        else:
            # Balance needs to go DOWN - create payment entry
            if is_user_hydrated(user_id):
                try:
                    redis_key = get_redis_key('c_payment_entries', user_id)
                    cached = redis_client.get(redis_key)
                    entries = json.loads(cached) if cached else []
                    
                    existing_ids = [int(e.get('id', 0)) for e in entries if e.get('id')]
                    min_id = min(existing_ids) if existing_ids else 0
                    temp_id = min_id - 1 if min_id <= 0 else -1
                    
                    entries.append({
                        'id': temp_id,
                        'account_id': account_id,
                        'date': target_date_str,
                        'amount': float(adjustment_amount),
                        'recurring_id': None,
                        'processed': 1
                    })
                    
                    redis_client.setex(redis_key, REDIS_TTL, json.dumps(entries, cls=DecimalEncoder))
                    mark_dirty(user_id, 'c_payment_entries')
                except Exception as e:
                    logger.warning(f"User {user_id}: Redis error, falling back to MySQL: {e}")
                    cursor.execute("""
                        INSERT INTO c_payment_entries (account_id, date, amount, processed)
                        VALUES (%s, %s, %s, 1)
                    """, (account_id, target_date_str, adjustment_amount))
                    conn.commit()
            else:
                cursor.execute("""
                    INSERT INTO c_payment_entries (account_id, date, amount, processed)
                    VALUES (%s, %s, %s, 1)
                """, (account_id, target_date_str, adjustment_amount))
                conn.commit()
            
            return True, f"Created payment adjustment -${adjustment_amount:.2f}"
        
    except Exception as e:
        logger.error(f"User {user_id}: Error creating credit account auto-adjustment: {e}")
        return False, str(e)


def process_user(cursor, conn, user_row):
    """
    Process a single user's nightly sync.
    
    Flow:
    1. Refresh session token if needed
    2. Pull balances for ALL accounts
    3. Sync transactions since last sync (never before account was first connected)
    4. Clean up expired bucket entries from yesterday
    5. Run auto-balance for Checking, Savings, and Credit accounts
    6. Recalculate all totals/remainders/balances
    """
    user_id = user_row[0]
    first_name = user_row[1]
    email = user_row[2]
    profile_id = user_row[3]
    session_token = user_row[4]
    session_expires = user_row[5]
    goofy_week_mode = bool(user_row[6]) if len(user_row) > 6 else False
    
    logger.info(f"Processing user {user_id} ({first_name or email})")
    
    result = {
        'user_id': user_id,
        'balances_updated': 0,
        'transactions_synced': 0,
        'transactions_imported': 0,
        'adjustments': []
    }
    
    # =========================================================================
    # STEP 1: Refresh session token if needed
    # =========================================================================
    session_token = refresh_session_if_needed(cursor, conn, user_id, profile_id, session_token, session_expires)
    if not session_token:
        logger.error(f"User {user_id}: Could not get valid session token")
        return result
    
    # =========================================================================
    # STEP 2: Fetch and update account balances for ALL accounts
    # =========================================================================
    updated_accounts = fetch_and_update_balances(cursor, conn, user_id, session_token)
    result['balances_updated'] = len(updated_accounts)
    logger.info(f"User {user_id}: Updated {len(updated_accounts)} account balances")
    
    # =========================================================================
    # STEP 3: Sync transactions for ALL accounts
    # =========================================================================
    active_accounts = get_active_accounts(cursor, user_id)
    
    # Build account info dict for later use in auto-adjust
    accounts_info = {}
    
    for acc in active_accounts:
        account_id = acc[0]
        account_name = acc[1]
        account_type = (acc[2] or '').lower()
        current_balance = acc[3]
        account_subtype = (acc[4] or '').lower()
        mask = acc[5]
        account_created_at = acc[6]  # When account was first connected
        last_synced_at = acc[7]      # Last sync time from connection
        
        accounts_info[account_id] = {
            'name': account_name,
            'type': account_type,
            'subtype': account_subtype,
            'balance': current_balance,
            'mask': mask
        }
        
        # Sync transactions for this account
        # Pass both last_synced_at and account_created_at
        new_txns, imported_txns = sync_transactions_for_account(
            cursor, conn, user_id, account_id, session_token, last_synced_at, account_created_at
        )
        result['transactions_synced'] += new_txns
        result['transactions_imported'] += imported_txns
    
    # =========================================================================
    # STEP 4: Clean up expired bucket entries from yesterday
    # (Do this BEFORE auto-balance so remainder calculations are accurate)
    # =========================================================================
    bucket_cleanup = cleanup_expired_bucket_entries(cursor, conn, user_id)
    total_buckets_deleted = sum(bucket_cleanup.values())
    if total_buckets_deleted > 0:
        logger.info(f"User {user_id}: Cleaned up {total_buckets_deleted} expired bucket entries")
    
    # =========================================================================
    # STEP 5: Run auto-balance for Checking, Savings, and Credit accounts
    # =========================================================================
    for account_id, info in accounts_info.items():
        account_name = info['name']
        account_type = info['type']
        account_subtype = info['subtype']
        current_balance = info['balance']
        mask = info['mask']
        
        if not current_balance:
            continue
        
        # DEPOSITORY accounts (Checking/Savings)
        if account_type == 'depository':
            account_name_lower = account_name.lower()
            
            if 'checking' in account_name_lower or account_subtype == 'checking':
                # Checking account - create auto-adjustment entry
                success, msg = create_auto_adjustment(cursor, conn, user_id, current_balance, account_name)
                result['adjustments'].append({
                    'account': account_name,
                    'type': 'checking',
                    'success': success,
                    'message': msg
                })
                logger.info(f"User {user_id}: Checking adjustment - {msg}")
                
            elif 'savings' in account_name_lower or account_subtype == 'savings':
                # Savings account - update savings balance
                success, msg = update_savings_balance(cursor, conn, user_id, current_balance, account_id)
                result['adjustments'].append({
                    'account': account_name,
                    'type': 'savings',
                    'success': success,
                    'message': msg
                })
                logger.info(f"User {user_id}: Savings update - {msg}")
        
        # CREDIT accounts
        elif account_type == 'credit':
            # Credit card/line - create credit account auto-adjustment
            success, msg = create_credit_account_auto_adjustment(
                cursor, conn, user_id, account_id, current_balance, mask, account_name
            )
            result['adjustments'].append({
                'account': account_name,
                'type': 'credit',
                'success': success,
                'message': msg
            })
            logger.info(f"User {user_id}: Credit account adjustment - {msg}")
    
    # =========================================================================
    # STEP 6: Recalculate all totals/remainders/balances
    # =========================================================================
    # Start from yesterday since that's where we made adjustments
    yesterday = date.today() - timedelta(days=1)
    
    # Track remainders across calculations
    date_to_remainder = {}
    
    # Regular budget calculations
    logger.info(f"User {user_id}: Starting recalculation from {yesterday}")
    
    recalculate_daily_totals(cursor, conn, user_id, yesterday, date_to_remainder, goofy_week_mode)
    recalculate_weekly_totals(cursor, conn, user_id, yesterday, date_to_remainder, goofy_week_mode)
    recalculate_monthly_totals(cursor, conn, user_id, yesterday, date_to_remainder)
    recalculate_savings(cursor, conn, user_id, yesterday)
    
    # Credit account calculations
    recalculate_ca_daily_balances(cursor, conn, user_id, yesterday)
    recalculate_ca_weekly_balances(cursor, conn, user_id, yesterday, goofy_week_mode)
    recalculate_ca_monthly_balances(cursor, conn, user_id, yesterday)
    
    # Clear Redis caches to ensure UI gets fresh data from MySQL
    # This is critical because we've updated MySQL directly
    if is_user_hydrated(user_id):
        keys_to_clear = [
            get_redis_key('totals_remainders_d', user_id),
            get_redis_key('totals_remainders', user_id),
            get_redis_key('totals_remainders_m', user_id),
            get_redis_key('savings_entries', user_id),
            get_redis_key('savings_adjustments', user_id),
            get_redis_key('c_a_balances_d', user_id),
            get_redis_key('c_a_balances', user_id),
            get_redis_key('c_a_balances_m', user_id),
        ]
        for key in keys_to_clear:
            redis_client.delete(key)
        logger.debug(f"User {user_id}: Cleared Redis caches after recalculations")
    
    logger.info(f"User {user_id}: Completed all recalculations")
    
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
