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
from datetime import datetime, timedelta, date, timezone
from decimal import Decimal

# Add parent directory to path so we can import app modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
REDIS_TTL = 604800  # 7 days - only for non-hydrated users
INACTIVITY_TIMEOUT = 300  # 5 minutes - for hydrated users (matches redis_manager.py)

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
    """Get a direct MySQL connection using .env config"""
    return mysql.connector.connect(
        host=os.getenv('DB_HOST', 'localhost'),
        user=os.getenv('DB_USER', 'ms_admin'),
        password=os.getenv('DB_PASSWORD', ''),
        database=os.getenv('DB_NAME', 'budget'),
        buffered=True  # Prevent unread result errors
    )


def is_user_hydrated(user_id):
    """Check if user has data in Redis (user profile exists)"""
    try:
        key = f"users:{REDIS_KEY_VERSION}:{user_id}"
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
        redis_client.expire(dirty_key, INACTIVITY_TIMEOUT + 60)
    except Exception as e:
        logger.warning(f"Error marking dirty {table_name} for user {user_id}: {e}")


def get_remainder_from_redis(table_name, user_id, target_date, field_name='remainder'):
    """Get remainder/balance from Redis for a specific date. Returns None if not found."""
    try:
        key = f"{table_name}:{REDIS_KEY_VERSION}:{user_id}"
        cached = redis_client.get(key)
        if cached:
            data = json.loads(cached)
            target_str = target_date.isoformat() if hasattr(target_date, 'isoformat') else str(target_date)
            for item in data:
                item_date = str(item.get('date', ''))[:10]
                if item_date == target_str[:10]:
                    return float(item.get(field_name, 0))
    except Exception:
        pass
    return None


def get_ca_balance_from_redis(table_name, user_id, account_id, target_date):
    """Get credit account balance from Redis for a specific date and account. Returns None if not found."""
    try:
        key = f"{table_name}:{REDIS_KEY_VERSION}:{user_id}"
        cached = redis_client.get(key)
        if cached:
            data = json.loads(cached)
            target_str = target_date.isoformat() if hasattr(target_date, 'isoformat') else str(target_date)
            for item in data:
                item_date = str(item.get('date', ''))[:10]
                if item_date == target_str[:10] and int(item.get('account_id', 0)) == account_id:
                    return float(item.get('balance', 0))
    except Exception:
        pass
    return None


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


def refresh_session_if_needed(cursor, conn, user_id, profile_id, session_token, session_expires_at, force=False):
    """Refresh Quiltt session token if expired or expiring soon"""
    now = datetime.now()
    
    # Check if token is expired or expires in next hour (skip check if force=True)
    if not force and session_expires_at and isinstance(session_expires_at, datetime):
        if session_expires_at > now + timedelta(hours=1):
            # Token still valid
            return session_token
    
    logger.info(f"User {user_id}: Refreshing expired session token")
    
    # Refresh token
    result = quiltt_client.refresh_session_token(profile_id, metadata={'user_id': str(user_id)})
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
                    redis_client.setex(redis_key, INACTIVITY_TIMEOUT + 60, json.dumps(profiles, cls=DecimalEncoder))
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
                
                updated_accounts.append({
                    'account_id': account_id,
                    'account_name': account_name,
                    'account_type': account_type,
                    'current_balance': current_balance,
                    'available_balance': available_balance
                })
        
        # Update storage based on hydration status
        if is_user_hydrated(user_id) and updated_accounts:
            # REDIS-FIRST: Update Redis only, mark dirty for flush
            try:
                redis_key = get_redis_key('quiltt_accounts', user_id)
                cached = redis_client.get(redis_key)
                if cached:
                    accounts = json.loads(cached)
                    for acc in accounts:
                        for updated in updated_accounts:
                            if acc.get('account_id') == updated['account_id']:
                                acc['current_balance'] = updated['current_balance']
                                acc['available_balance'] = updated['available_balance']
                                break
                    redis_client.setex(redis_key, INACTIVITY_TIMEOUT + 60, json.dumps(accounts, cls=DecimalEncoder))
                    mark_dirty(user_id, 'quiltt_accounts')
            except Exception as e:
                logger.warning(f"User {user_id}: Error updating Redis accounts: {e}")
        else:
            # MYSQL-ONLY: User not hydrated, update MySQL directly
            for updated in updated_accounts:
                cursor.execute("""
                    UPDATE quiltt_accounts
                    SET current_balance = %s, available_balance = %s, last_modified = NOW()
                    WHERE user_id = %s AND account_id = %s
                """, (updated['current_balance'], updated['available_balance'], user_id, updated['account_id']))
            conn.commit()
        
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


def create_pending_transactions_notification(cursor, conn, user_id, imported_count):
    """
    Create a notification for pending transactions waiting for categorization.
    
    Args:
        cursor: MySQL cursor
        conn: MySQL connection
        user_id: User ID
        imported_count: Number of transactions imported this run
    """
    if imported_count <= 0:
        return
    
    # Get total count of pending transactions
    total_pending = 0
    
    # Check Redis first if user is hydrated
    if is_user_hydrated(user_id):
        try:
            # Count pending income entries
            income_key = f"income_entries:{REDIS_KEY_VERSION}:{user_id}"
            cached = redis_client.get(income_key)
            if cached:
                entries = json.loads(cached)
                total_pending += sum(1 for e in entries if e.get('pending') == 1)
            
            # Count pending expense entries
            expense_key = f"expense_entries:{REDIS_KEY_VERSION}:{user_id}"
            cached = redis_client.get(expense_key)
            if cached:
                entries = json.loads(cached)
                total_pending += sum(1 for e in entries if e.get('pending') == 1)
            
            # Count pending c_expense entries
            c_expense_key = f"c_expense_entries:{REDIS_KEY_VERSION}:{user_id}"
            cached = redis_client.get(c_expense_key)
            if cached:
                entries = json.loads(cached)
                total_pending += sum(1 for e in entries if e.get('pending') == 1)
        except Exception as e:
            logger.error(f"User {user_id}: Error counting pending from Redis: {e}")
            total_pending = imported_count
    else:
        # Count from MySQL
        cursor.execute("""
            SELECT COUNT(*) FROM income_entries ie
            JOIN income_categories ic ON ie.category_id = ic.id
            WHERE ic.user_id = %s AND ie.pending = 1
        """, (user_id,))
        total_pending += cursor.fetchone()[0]
        
        cursor.execute("""
            SELECT COUNT(*) FROM expense_entries ee
            JOIN expense_categories ec ON ee.category_id = ec.id
            WHERE ec.user_id = %s AND ee.pending = 1
        """, (user_id,))
        total_pending += cursor.fetchone()[0]
        
        cursor.execute("""
            SELECT COUNT(*) FROM c_expense_entries cee
            JOIN c_expense_categories cec ON cee.category_id = cec.id
            JOIN credit_accounts ca ON cec.account_id = ca.id
            WHERE ca.user_id = %s AND cee.pending = 1
        """, (user_id,))
        total_pending += cursor.fetchone()[0]
    
    if total_pending <= 0:
        return
    
    # Create notification message
    txn_word = "transaction" if total_pending == 1 else "transactions"
    need_word = "needs" if total_pending == 1 else "need"
    message = f'You have {total_pending} pending {txn_word} synced from your bank accounts that {need_word} to be categorized. <a href="/pending-transactions">Click here to review</a>.'
    
    import time
    
    if is_user_hydrated(user_id):
        # REDIS-FIRST: Update Redis only, mark dirty for flush
        try:
            redis_key = get_redis_key('notifications', user_id)
            cached = redis_client.get(redis_key)
            notifications = json.loads(cached) if cached else []
            
            # Remove old pending transaction notifications from Redis
            notifications = [n for n in notifications 
                           if 'pending transaction' not in n.get('message', '') 
                           or 'synced from your bank' not in n.get('message', '')]
            
            # Generate temp ID for new notification
            temp_id = -(int(time.time() * 1000) % 1000000000)
            
            # Add new notification
            new_notification = {
                'id': temp_id,
                'user_id': user_id,
                'date': datetime.now().isoformat(),
                'message': message,
                'is_read': 0
            }
            notifications.append(new_notification)
            
            # Save back to Redis with INACTIVITY_TIMEOUT (user is hydrated)
            redis_client.setex(redis_key, INACTIVITY_TIMEOUT + 60, json.dumps(notifications, cls=DecimalEncoder))
            mark_dirty(user_id, 'notifications')
            logger.info(f"User {user_id}: Added pending transactions notification to Redis")
        except Exception as e:
            logger.warning(f"User {user_id}: Failed to add notification to Redis: {e}")
    else:
        # MYSQL-ONLY: User not hydrated, write directly to MySQL
        # Delete any existing pending transaction notifications
        cursor.execute("""
            DELETE FROM notifications
            WHERE user_id = %s
            AND message LIKE %s
        """, (user_id, '%pending transaction%synced from your bank%'))
        deleted = cursor.rowcount
        if deleted > 0:
            logger.info(f"User {user_id}: Deleted {deleted} old pending transaction notification(s)")
        
        # Insert new notification
        cursor.execute("""
            INSERT INTO notifications (user_id, message, is_read)
            VALUES (%s, %s, 0)
        """, (user_id, message))
        conn.commit()
    
    logger.info(f"User {user_id}: Created pending transactions notification ({total_pending} pending)")


def sync_transactions_for_account(cursor, conn, user_id, account_id, session_token, last_synced_at, account_created_at):
    """Sync new transactions for a specific account.
    
    Uses a rolling 14-day lookback window to catch late-appearing transactions.
    Banks may post a transaction on day X but it may not appear in Quiltt until
    day X+3. The 14-day window ensures we pick up these stragglers.
    Duplicate detection via transaction_id prevents re-importing.
    
    We never pull transactions from before the account was connected.
    
    Note: Since this script runs after midnight, we only sync up to yesterday to ensure
    we're getting finalized transactions, not pending ones from the current day.
    """
    try:
        # Determine date range - rolling 14-day lookback, up to yesterday
        yesterday = date.today() - timedelta(days=1)
        end_date = yesterday
        lookback_start = date.today() - timedelta(days=14)
        
        # Convert account_created_at to date if it's a datetime
        if account_created_at:
            created_date = account_created_at.date() if isinstance(account_created_at, datetime) else account_created_at
        else:
            created_date = end_date  # If no created_at, only sync yesterday
        
        # Start date: MAX(14-day lookback, account_created_at)
        # Never sync before the account was connected
        start_date = max(lookback_start, created_date)
        
        # Skip if start date is after end date (nothing to sync)
        if start_date > end_date:
            logger.info(f"User {user_id}: Skipping account {account_id} - already synced past {end_date}")
            return 0, 0, None, set()
        
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
            return 0, 0, None, set()
        
        # Get existing transaction IDs to avoid duplicates (check both MySQL and Redis)
        existing_txn_ids = set()
        
        # Check MySQL
        cursor.execute("""
            SELECT transaction_id FROM quiltt_transactions
            WHERE user_id = %s AND account_id = %s
        """, (user_id, account_id))
        existing_txn_ids.update(row[0] for row in cursor.fetchall())
        
        # Also check Redis in case transactions were added but not yet flushed
        redis_key = f"quiltt_transactions:v1:{user_id}"
        redis_data = redis_client.get(redis_key)
        if redis_data:
            try:
                redis_txns = json.loads(redis_data)
                existing_txn_ids.update(
                    t.get('transaction_id', '') for t in redis_txns
                    if t.get('account_id') == account_id
                )
            except (json.JSONDecodeError, TypeError):
                pass
        
        new_count = 0
        imported_count = 0
        earliest_date = None  # Track earliest imported transaction date
        imported_dates = set()  # Track ALL dates with newly imported transactions
        
        for txn in transactions:
            txn_id = txn.get('id')
            if not txn_id or txn_id in existing_txn_ids:
                continue
            
            # Store transaction
            raw_amount = float(txn.get('amount', 0))
            amount = abs(raw_amount)
            txn_date = txn.get('date')
            # Use Quiltt's entryType field: CREDIT = inflow (income), DEBIT = outflow (expense)
            entry_type_raw = txn.get('entryType', '').upper()
            is_expense = (entry_type_raw != 'CREDIT')  # DEBIT or empty = expense
            
            # Track earliest date and all imported dates for recalculation
            if txn_date:
                try:
                    txn_date_obj = datetime.strptime(txn_date, '%Y-%m-%d').date() if isinstance(txn_date, str) else txn_date
                    imported_dates.add(txn_date_obj)
                    if earliest_date is None or txn_date_obj < earliest_date:
                        earliest_date = txn_date_obj
                except (ValueError, TypeError):
                    pass
            description = txn.get('description', '')
            category = txn.get('category')
            # Derive txn_type from entryType for auto-import routing
            txn_type = 'income' if entry_type_raw == 'CREDIT' else 'expense'
            pending = 1 if txn.get('pending') else 0
            
            # Extract Ntropy data if present
            ntropy_data = {}
            finicity_created_date = None
            remote_data = txn.get('remoteData', {})
            if remote_data:
                ntropy = remote_data.get('ntropy', {})
                enrichment = ntropy.get('enrichment', {})
                response = enrichment.get('response', {})
                
                # Extract categories (Quiltt schema: categories.general)
                categories = response.get('categories')
                if categories and isinstance(categories, dict):
                    general = categories.get('general')
                    if general:
                        ntropy_data['ntropy_labels'] = json.dumps([general])
                
                # Extract counterparty/merchant info (Quiltt schema: entities.counterparty)
                entities = response.get('entities')
                if entities and isinstance(entities, dict):
                    counterparty = entities.get('counterparty')
                    if counterparty and isinstance(counterparty, dict):
                        ntropy_data['ntropy_merchant_id'] = counterparty.get('id')
                        ntropy_data['ntropy_merchant_name'] = counterparty.get('name')
                        ntropy_data['ntropy_logo'] = counterparty.get('logo')
                        ntropy_data['ntropy_website'] = counterparty.get('website')
                        ntropy_data['ntropy_transaction_type'] = counterparty.get('type')
                        mccs = counterparty.get('mccs')
                        if mccs:
                            ntropy_data['ntropy_mcc'] = json.dumps(mccs)
                
                # Extract location info (Quiltt schema: location is RemoteDataNtropyLocation object)
                location = response.get('location')
                if location and isinstance(location, dict):
                    raw_address = location.get('rawAddress')
                    if raw_address:
                        ntropy_data['ntropy_location'] = raw_address
                    structured = location.get('structured')
                    if structured and isinstance(structured, dict):
                        ntropy_data['ntropy_location_city'] = structured.get('city')
                        ntropy_data['ntropy_location_state'] = structured.get('state')
                        ntropy_data['ntropy_location_country'] = structured.get('country')
                elif location and isinstance(location, str):
                    # Fallback: treat as plain string if API returns scalar
                    ntropy_data['ntropy_location'] = location
                
                if any(v for k, v in ntropy_data.items() if k != 'ntropy_enriched_at'):
                    ntropy_data['ntropy_enriched_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                
                # Extract Finicity data (createdDate + categorization fallback)
                finicity = remote_data.get('finicity', {})
                if finicity:
                    fin_txn = finicity.get('transaction', {})
                    if fin_txn:
                        fin_response = fin_txn.get('response', {})
                        if fin_response:
                            epoch = fin_response.get('createdDate')
                            if epoch:
                                try:
                                    finicity_created_date = datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                                except (ValueError, TypeError, OSError):
                                    finicity_created_date = None
                            # Use Finicity categorization as fallback for merchant/category
                            categorization = fin_response.get('categorization', {})
                            if categorization and isinstance(categorization, dict):
                                if not ntropy_data.get('ntropy_merchant_name'):
                                    payee = categorization.get('normalizedPayeeName')
                                    if payee:
                                        ntropy_data['ntropy_merchant_name'] = payee
                                if not ntropy_data.get('ntropy_labels'):
                                    fin_category = categorization.get('category')
                                    if fin_category:
                                        ntropy_data['ntropy_labels'] = json.dumps([fin_category])
                                if not ntropy_data.get('ntropy_location_city'):
                                    ntropy_data['ntropy_location_city'] = categorization.get('city') or None
                                    ntropy_data['ntropy_location_state'] = categorization.get('state') or None
                                    ntropy_data['ntropy_location_country'] = categorization.get('country') or None
            
            # Build transaction object
            new_quiltt_txn = {
                'user_id': user_id,
                'account_id': account_id,
                'transaction_id': txn_id,
                'amount': str(amount),
                'date': txn_date,
                'description': description,
                'merchant_name': ntropy_data.get('ntropy_merchant_name') or description,
                'category': category,
                'transaction_type': txn_type,
                'pending': pending,
                'imported_to_entry_id': None,
                'imported_entry_type': None,
                'imported_at': None,
                'ntropy_labels': ntropy_data.get('ntropy_labels'),
                'ntropy_merchant_id': ntropy_data.get('ntropy_merchant_id'),
                'ntropy_logo': ntropy_data.get('ntropy_logo'),
                'ntropy_website': ntropy_data.get('ntropy_website'),
                'ntropy_recurrence': ntropy_data.get('ntropy_recurrence'),
                'ntropy_enriched_at': ntropy_data.get('ntropy_enriched_at'),
                'finicity_created_date': finicity_created_date
            }
            
            # --- CUSTOM CATEGORY SUGGESTION (Ntropy) ---
            # Get AI category suggestion using user's own categories
            if not pending:
                try:
                    from ntropy_utils import suggest_category_for_transaction
                    
                    # Determine account type for category lookup
                    cursor.execute("""
                        SELECT account_type FROM quiltt_accounts
                        WHERE user_id = %s AND account_id = %s
                    """, (user_id, account_id))
                    acct_row = cursor.fetchone()
                    acct_type = (acct_row[0] or '').upper() if acct_row else 'DEPOSITORY'
                    ntropy_account_type = 'CREDIT' if acct_type == 'CREDIT' else 'DEPOSITORY'
                    
                    suggestion = suggest_category_for_transaction(
                        user_id=user_id,
                        transaction={
                            'id': txn_id,
                            'transaction_id': txn_id,
                            'description': description,
                            'amount': amount,
                            'date': txn_date,
                            'transaction_type': 'expense' if is_expense else 'income'
                        },
                        account_type=ntropy_account_type
                    )
                    if suggestion and suggestion.get('suggested_category'):
                        new_quiltt_txn['custom_category_suggestion'] = suggestion.get('suggested_category')
                        new_quiltt_txn['custom_category_id'] = suggestion.get('suggested_category_id')
                        new_quiltt_txn['custom_category_type'] = suggestion.get('category_type')
                        new_quiltt_txn['custom_category_confidence'] = suggestion.get('confidence')
                        new_quiltt_txn['custom_suggestion_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        logger.info(f"User {user_id}: Ntropy suggestion for {txn_id}: {suggestion.get('suggested_category')} (confidence={suggestion.get('confidence')})")
                except Exception as suggest_err:
                    logger.warning(f"User {user_id}: Failed to get category suggestion for {txn_id}: {suggest_err}")
            # --- END CUSTOM CATEGORY SUGGESTION ---
            
            # Store transaction based on hydration status
            if is_user_hydrated(user_id):
                # REDIS-FIRST: Add to Redis only, mark dirty for flush
                try:
                    redis_key = get_redis_key('quiltt_transactions', user_id)
                    cached_txns = redis_client.get(redis_key)
                    quiltt_txns = json.loads(cached_txns) if cached_txns else []
                    quiltt_txns.append(new_quiltt_txn)
                    redis_client.setex(redis_key, INACTIVITY_TIMEOUT + 60, json.dumps(quiltt_txns, cls=DecimalEncoder))
                    mark_dirty(user_id, 'quiltt_transactions')
                except Exception as e:
                    logger.warning(f"User {user_id}: Error adding quiltt_transaction to Redis: {e}")
            else:
                # MYSQL-ONLY: User not hydrated, insert directly
                cursor.execute("""
                    INSERT INTO quiltt_transactions
                    (user_id, account_id, transaction_id, amount, date, description, merchant_name, 
                     category, transaction_type, pending, ntropy_labels, ntropy_merchant_id, 
                     ntropy_logo, ntropy_website, ntropy_recurrence, ntropy_enriched_at,
                     custom_category_suggestion, custom_category_id, custom_category_type,
                     custom_category_confidence, custom_suggestion_at, finicity_created_date)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        description = VALUES(description),
                        amount = VALUES(amount),
                        pending = VALUES(pending),
                        finicity_created_date = VALUES(finicity_created_date)
                """, (
                    user_id, account_id, txn_id, amount, txn_date, description, merchant_name,
                    category, txn_type, pending,
                    ntropy_data.get('ntropy_labels'),
                    ntropy_data.get('ntropy_merchant_id'),
                    ntropy_data.get('ntropy_logo'),
                    ntropy_data.get('ntropy_website'),
                    ntropy_data.get('ntropy_recurrence'),
                    ntropy_data.get('ntropy_enriched_at'),
                    new_quiltt_txn.get('custom_category_suggestion'),
                    new_quiltt_txn.get('custom_category_id'),
                    new_quiltt_txn.get('custom_category_type'),
                    new_quiltt_txn.get('custom_category_confidence'),
                    new_quiltt_txn.get('custom_suggestion_at'),
                    finicity_created_date
                ))
            
            new_count += 1
            
            # Auto-import new transactions as pending entries
            # Skip if this is a pending bank transaction
            if not pending:
                imported = auto_import_transaction(cursor, conn, user_id, account_id, txn, txn_type)
                if imported:
                    imported_count += 1
        
        conn.commit()
        
        # Update connection last_synced_at based on hydration status
        if is_user_hydrated(user_id):
            # REDIS-FIRST: Update Redis only, mark dirty
            try:
                redis_key = get_redis_key('quiltt_connections', user_id)
                cached = redis_client.get(redis_key)
                if cached:
                    connections = json.loads(cached)
                    # Find connection for this account
                    for conn_obj in connections:
                        # We need to check if this connection owns this account
                        # Connection has connection_id, accounts link via connection_id
                        conn_obj['last_synced_at'] = datetime.now().isoformat()
                    redis_client.setex(redis_key, INACTIVITY_TIMEOUT + 60, json.dumps(connections, cls=DecimalEncoder))
                    mark_dirty(user_id, 'quiltt_connections')
            except Exception as e:
                logger.warning(f"User {user_id}: Error updating quiltt_connections in Redis: {e}")
        else:
            # MYSQL-ONLY: Update directly
            cursor.execute("""
                UPDATE quiltt_connections qc
                JOIN quiltt_accounts qa ON qc.id = qa.connection_id
                SET qc.last_synced_at = NOW()
                WHERE qa.user_id = %s AND qa.account_id = %s
            """, (user_id, account_id))
            conn.commit()
        
        return new_count, imported_count, earliest_date, imported_dates
        
    except Exception as e:
        logger.error(f"User {user_id}: Error syncing transactions for {account_id}: {e}")
        return 0, 0, None, set()


def auto_import_transaction(cursor, conn, user_id, account_id, txn, txn_type):
    """Auto-import a transaction as a pending entry.
    
    Follows Redis-first architecture:
    - If user is hydrated: append entry to Redis list + mark dirty (flush worker syncs to MySQL)
    - If user is NOT hydrated: insert directly to MySQL (no Redis to be inconsistent with)
    """
    try:
        import time
        
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
        
        # Use txn_type derived from Quiltt's entryType (CREDIT=income, DEBIT=expense)
        is_income = (txn_type == 'income')
        
        # Determine which table to insert into
        blankee_account_id = None
        if account_type == 'credit':
            if is_income:
                entry_type = 'c_payment'
            else:
                entry_type = 'c_expense'
        else:
            if is_income:
                entry_type = 'income'
            else:
                entry_type = 'expense'
        
        # Get the appropriate category / account ID
        if entry_type == 'income':
            cursor.execute("""
                SELECT id FROM income_categories
                WHERE user_id = %s AND LOWER(name) = 'uncategorized'
                LIMIT 1
            """, (user_id,))
            cat_row = cursor.fetchone()
            if not cat_row:
                return False
            category_id = cat_row[0]
            table_name = 'income_entries'
            
        elif entry_type == 'expense':
            cursor.execute("""
                SELECT id FROM expense_categories
                WHERE user_id = %s AND LOWER(name) = 'uncategorized'
                LIMIT 1
            """, (user_id,))
            cat_row = cursor.fetchone()
            if not cat_row:
                return False
            category_id = cat_row[0]
            table_name = 'expense_entries'
            
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
            cat_row = cursor.fetchone()
            if not cat_row:
                return False
            category_id = cat_row[0]
            table_name = 'c_expense_entries'
            
        elif entry_type == 'c_payment':
            # Get the Blankee credit account for the payment
            cursor.execute("""
                SELECT ca.id FROM credit_accounts ca
                JOIN quiltt_accounts qa ON ca.quiltt_account_id = qa.account_id
                WHERE qa.user_id = %s AND qa.account_id = %s
            """, (user_id, account_id))
            ca_row = cursor.fetchone()
            if not ca_row:
                return False
            blankee_account_id = ca_row[0]
            category_id = None  # Payments don't have categories
            table_name = 'c_payment_entries'
        
        # Check if user is hydrated - determines update strategy
        user_hydrated = is_user_hydrated(user_id)
        
        if user_hydrated:
            # ====== REDIS-FIRST PATH ======
            # Append entry to Redis list, mark dirty, flush worker syncs to MySQL
            temp_id = int(time.time() * 1000) % 1000000
            
            if entry_type == 'income':
                new_entry = {
                    'id': temp_id,
                    'category_id': category_id,
                    'date': txn_date,
                    'amount': str(amount),
                    'recurring_id': None,
                    'is_bucket': 0,
                    'original_amount': None,
                    'processed': 1,
                    'pending': 1,
                    'auto_confirmed': 0
                }
            elif entry_type == 'expense':
                new_entry = {
                    'id': temp_id,
                    'category_id': category_id,
                    'date': txn_date,
                    'amount': str(amount),
                    'recurring_id': None,
                    'is_bucket': 0,
                    'original_amount': None,
                    'processed': 1,
                    'pending': 1,
                    'auto_confirmed': 0,
                    'bud_item_id': None
                }
            elif entry_type == 'c_expense':
                new_entry = {
                    'id': temp_id,
                    'category_id': category_id,
                    'date': txn_date,
                    'amount': str(amount),
                    'recurring_id': None,
                    'is_bucket': 0,
                    'original_amount': None,
                    'processed': 1,
                    'pending': 1,
                    'auto_confirmed': 0,
                    'bud_item_id': None
                }
            elif entry_type == 'c_payment':
                new_entry = {
                    'id': temp_id,
                    'account_id': blankee_account_id,
                    'date': txn_date,
                    'amount': str(amount),
                    'recurring_id': None,
                    'processed': 1
                }
            
            # Append to Redis list
            redis_key = get_redis_key(table_name, user_id)
            cached = redis_client.get(redis_key)
            entries = json.loads(cached) if cached else []
            entries.append(new_entry)
            redis_client.setex(redis_key, INACTIVITY_TIMEOUT + 60, json.dumps(entries, cls=DecimalEncoder))
            
            # DEBUG: Verify pending was written correctly
            logger.info(f"User {user_id}: Added entry to Redis - id={temp_id}, pending={new_entry.get('pending')}, total entries now={len(entries)}")
            
            # Mark dirty for flush worker to sync to MySQL
            mark_dirty(user_id, table_name)
            
            entry_id = temp_id
            logger.debug(f"User {user_id}: Added {entry_type} entry {temp_id} to Redis for txn {txn_id}")
            
        else:
            # ====== MYSQL-ONLY PATH ======
            # User not hydrated, insert directly to MySQL
            if entry_type == 'income':
                cursor.execute("""
                    INSERT INTO income_entries (category_id, date, amount, pending, auto_confirmed, processed)
                    VALUES (%s, %s, %s, 1, 0, 1)
                """, (category_id, txn_date, amount))
                entry_id = cursor.lastrowid
            elif entry_type == 'expense':
                cursor.execute("""
                    INSERT INTO expense_entries (category_id, date, amount, pending, auto_confirmed, processed)
                    VALUES (%s, %s, %s, 1, 0, 1)
                """, (category_id, txn_date, amount))
                entry_id = cursor.lastrowid
            elif entry_type == 'c_expense':
                cursor.execute("""
                    INSERT INTO c_expense_entries (category_id, date, amount, pending, auto_confirmed, processed)
                    VALUES (%s, %s, %s, 1, 0, 1)
                """, (category_id, txn_date, amount))
                entry_id = cursor.lastrowid
            elif entry_type == 'c_payment':
                cursor.execute("""
                    INSERT INTO c_payment_entries (account_id, date, amount, processed)
                    VALUES (%s, %s, %s, 1)
                """, (blankee_account_id, txn_date, amount))
                entry_id = cursor.lastrowid
            
            conn.commit()
            logger.debug(f"User {user_id}: Inserted {entry_type} entry {entry_id} to MySQL for txn {txn_id}")
            
            # Also update quiltt_transaction with entry reference in MySQL (only if not hydrated)
            cursor.execute("""
                UPDATE quiltt_transactions
                SET imported_to_entry_id = %s, imported_entry_type = %s, imported_at = NOW()
                WHERE user_id = %s AND transaction_id = %s
            """, (entry_id, entry_type, user_id, txn_id))
            conn.commit()
        
        # Update quiltt_transactions in Redis if user is hydrated
        if user_hydrated:
            try:
                txn_redis_key = get_redis_key('quiltt_transactions', user_id)
                cached_txns = redis_client.get(txn_redis_key)
                quiltt_txns = json.loads(cached_txns) if cached_txns else []
                
                # Find and update the transaction
                txn_id_val = txn.get('id')
                found = False
                for qt in quiltt_txns:
                    if qt.get('transaction_id') == txn_id_val:
                        qt['imported_to_entry_id'] = entry_id
                        qt['imported_entry_type'] = entry_type
                        qt['imported_at'] = datetime.now().isoformat()
                        found = True
                        break
                
                if not found:
                    # Transaction wasn't in Redis yet, add it
                    new_quiltt_txn = {
                        'user_id': user_id,
                        'account_id': account_id,
                        'transaction_id': txn_id_val,
                        'amount': str(amount),
                        'date': txn_date,
                        'description': txn.get('description', ''),
                        'merchant_name': txn.get('merchantName'),
                        'category': txn.get('category'),
                        'transaction_type': txn_type,
                        'pending': 0,
                        'imported_to_entry_id': entry_id,
                        'imported_entry_type': entry_type,
                        'imported_at': datetime.now().isoformat()
                    }
                    quiltt_txns.append(new_quiltt_txn)
                
                redis_client.setex(txn_redis_key, INACTIVITY_TIMEOUT + 60, json.dumps(quiltt_txns, cls=DecimalEncoder))
                mark_dirty(user_id, 'quiltt_transactions')
                logger.debug(f"User {user_id}: Updated quiltt_transactions in Redis for txn {txn_id_val}")
            except Exception as e:
                logger.warning(f"User {user_id}: Error updating quiltt_transactions in Redis: {e}")
        
        return True
        
    except Exception as e:
        logger.warning(f"User {user_id}: Error auto-importing transaction: {e}")
        return False


def cleanup_stale_checking_adjustments(cursor, conn, user_id, affected_dates):
    """
    Remove auto-adjustment entries (income/expense) on dates where late bank
    transactions were imported.  Removing them before the first recalculation
    lets the remainder chain rebuild cleanly so that yesterday's single new
    adjustment absorbs the full cumulative delta vs the bank.

    Args:
        cursor: DB cursor
        conn: DB connection
        user_id: User ID
        affected_dates: set of date objects that received new transactions
    """
    if not affected_dates:
        return
    
    yesterday = date.today() - timedelta(days=1)
    # Only clean up dates BEFORE yesterday — yesterday gets a fresh adjustment
    dates_to_clean = [d for d in affected_dates if d < yesterday]
    if not dates_to_clean:
        return
    
    date_strings = [d.strftime('%Y-%m-%d') for d in dates_to_clean]
    
    # Find auto-adjustment category IDs
    income_cat_id = None
    expense_cat_id = None
    
    user_hydrated = is_user_hydrated(user_id)
    
    if user_hydrated:
        inc_cats = get_entries_from_redis_or_mysql(cursor, 'income_categories', user_id)
        exp_cats = get_entries_from_redis_or_mysql(cursor, 'expense_categories', user_id)
        for c in (inc_cats or []):
            if c.get('is_auto_adjustment'):
                income_cat_id = int(c['id'])
                break
        for c in (exp_cats or []):
            if c.get('is_auto_adjustment'):
                expense_cat_id = int(c['id'])
                break
    else:
        cursor.execute("SELECT id FROM income_categories WHERE user_id = %s AND is_auto_adjustment = 1 LIMIT 1", (user_id,))
        row = cursor.fetchone()
        income_cat_id = row[0] if row else None
        cursor.execute("SELECT id FROM expense_categories WHERE user_id = %s AND is_auto_adjustment = 1 LIMIT 1", (user_id,))
        row = cursor.fetchone()
        expense_cat_id = row[0] if row else None
    
    if not income_cat_id and not expense_cat_id:
        return
    
    removed_count = 0
    
    if user_hydrated:
        # REDIS-FIRST: filter out auto-adjustment entries on affected dates
        # IMPORTANT: Only remove entries tagged with is_auto_adjustment=1
        # to avoid deleting imported pending transactions on the same category+date
        for table, cat_id in [('income_entries', income_cat_id), ('expense_entries', expense_cat_id)]:
            if not cat_id:
                continue
            redis_key = get_redis_key(table, user_id)
            cached = redis_client.get(redis_key)
            if not cached:
                continue
            entries = json.loads(cached)
            original_len = len(entries)
            entries = [
                e for e in entries
                if not (
                    int(e.get('category_id', 0)) == cat_id and
                    str(e.get('date', ''))[:10] in date_strings and
                    e.get('is_auto_adjustment')
                )
            ]
            removed = original_len - len(entries)
            if removed > 0:
                redis_client.setex(redis_key, INACTIVITY_TIMEOUT + 60, json.dumps(entries, cls=DecimalEncoder))
                mark_dirty(user_id, table)
                removed_count += removed
    else:
        # MYSQL-ONLY: delete directly (only auto-adjustment entries)
        for table, cat_id in [('income_entries', income_cat_id), ('expense_entries', expense_cat_id)]:
            if not cat_id:
                continue
            placeholders = ','.join(['%s'] * len(date_strings))
            cursor.execute(f"""
                DELETE FROM {table}
                WHERE category_id = %s AND date IN ({placeholders}) AND is_auto_adjustment = 1
            """, [cat_id] + date_strings)
            removed_count += cursor.rowcount
        conn.commit()
    
    if removed_count > 0:
        logger.info(f"User {user_id}: Removed {removed_count} stale checking auto-adjustment(s) on dates: {', '.join(date_strings)}")


def cleanup_stale_ca_adjustments(cursor, conn, user_id, quiltt_account_id, affected_dates, account_name):
    """
    Remove auto-adjustment entries for a credit account on dates where late
    bank transactions were imported.  Same logic as checking cleanup but
    targets c_expense_entries with is_auto_adjustment categories.

    Args:
        cursor: DB cursor
        conn: DB connection
        user_id: User ID
        quiltt_account_id: Quiltt account ID string
        affected_dates: set of date objects that received new transactions
        account_name: Account name for logging
    """
    if not affected_dates:
        return
    
    yesterday = date.today() - timedelta(days=1)
    dates_to_clean = [d for d in affected_dates if d < yesterday]
    if not dates_to_clean:
        return
    
    date_strings = [d.strftime('%Y-%m-%d') for d in dates_to_clean]
    
    # Find the credit account and its auto-adjustment category
    all_credit_accounts = get_entries_from_redis_or_mysql(cursor, 'credit_accounts', user_id)
    ca_match = None
    for ca in (all_credit_accounts or []):
        if ca.get('quiltt_account_id') == quiltt_account_id:
            ca_match = ca
            break
    
    if not ca_match:
        return
    
    account_id = int(ca_match['id'])
    
    c_expense_cats = get_entries_from_redis_or_mysql(cursor, 'c_expense_categories', user_id)
    auto_adj_cat_id = None
    for c in (c_expense_cats or []):
        if int(c.get('account_id', 0)) == account_id and c.get('is_auto_adjustment'):
            auto_adj_cat_id = int(c['id'])
            break
    
    if not auto_adj_cat_id:
        return
    
    removed_count = 0
    user_hydrated = is_user_hydrated(user_id)
    
    if user_hydrated:
        redis_key = get_redis_key('c_expense_entries', user_id)
        cached = redis_client.get(redis_key)
        if cached:
            entries = json.loads(cached)
            original_len = len(entries)
            entries = [
                e for e in entries
                if not (
                    int(e.get('category_id', 0)) == auto_adj_cat_id and
                    str(e.get('date', ''))[:10] in date_strings and
                    e.get('is_auto_adjustment')
                )
            ]
            removed = original_len - len(entries)
            if removed > 0:
                redis_client.setex(redis_key, INACTIVITY_TIMEOUT + 60, json.dumps(entries, cls=DecimalEncoder))
                mark_dirty(user_id, 'c_expense_entries')
                removed_count = removed
    else:
        placeholders = ','.join(['%s'] * len(date_strings))
        cursor.execute(f"""
            DELETE FROM c_expense_entries
            WHERE category_id = %s AND date IN ({placeholders}) AND is_auto_adjustment = 1
        """, [auto_adj_cat_id] + date_strings)
        removed_count = cursor.rowcount
        conn.commit()
    
    if removed_count > 0:
        logger.info(f"User {user_id}: Removed {removed_count} stale {account_name} auto-adjustment(s) on dates: {', '.join(date_strings)}")


def create_auto_adjustment(cursor, conn, user_id, bank_balance, account_name, target_date=None):
    """
    Create auto-adjustment entry to match bank balance.
    
    Simple delta approach:
    1. Get target date's CURRENT remainder from totals_remainders_d
       (this already includes any previous auto-adjustments)
    2. Compare to bank balance
    3. If different, add ONE new entry for just the delta
    
    No deleting old entries. The remainder already reflects everything.
    Each night we just add the incremental difference.
    
    IMPORTANT: If user is hydrated, we update Redis first and mark dirty so the
    flush worker syncs to MySQL. We NEVER delete Redis keys, as that causes
    TTL refresh to re-hydrate stale data which then gets flushed back.
    
    Args:
        target_date: Date to autobalance (date object or YYYY-MM-DD string). Defaults to yesterday.
    """
    try:
        if target_date is not None:
            if isinstance(target_date, str):
                yesterday = date.fromisoformat(target_date[:10])
            else:
                yesterday = target_date
        else:
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
        
        # Step 1: Get yesterday's CURRENT remainder (includes all previous adjustments)
        user_hydrated = is_user_hydrated(user_id)
        current_remainder = None
        
        if user_hydrated:
            totals_key = get_redis_key('totals_remainders_d', user_id)
            totals_cached = redis_client.get(totals_key)
            if totals_cached:
                totals_list = json.loads(totals_cached)
                for t in totals_list:
                    if str(t.get('date', ''))[:10] == target_date_str:
                        current_remainder = float(t.get('remainder', 0))
                        break
        
        if current_remainder is None:
            # Fall back to MySQL
            cursor.execute("""
                SELECT remainder FROM totals_remainders_d
                WHERE user_id = %s AND date = %s
            """, (user_id, target_date_str))
            row = cursor.fetchone()
            current_remainder = float(row[0]) if row and row[0] is not None else None
        
        if current_remainder is None:
            logger.warning(f"User {user_id}: No remainder found for {target_date_str}, skipping auto-adjustment")
            return False, "No remainder for target date"
        
        # Step 2: Compare to bank balance
        bank_float = float(bank_balance)
        diff = bank_float - current_remainder
        
        logger.info(f"User {user_id}: {account_name} - Remainder=${current_remainder:.2f}, Bank=${bank_float:.2f}, Delta=${diff:.2f}")
        
        # Step 3: Skip if already balanced
        if abs(diff) < 0.01:
            logger.info(f"User {user_id}: {account_name} - Already balanced, no adjustment needed")
            return True, "Already balanced"
        
        # Step 4: Add ONE entry for just the delta
        entry_type = 'income' if diff > 0 else 'expense'
        adj_amount = abs(diff)
        
        if user_hydrated:
            # ====== REDIS-FIRST PATH ======
            import time
            temp_id = -(int(time.time() * 1000) % 1000000000)
            
            new_entry = {
                'id': temp_id,
                'date': target_date_str,
                'amount': adj_amount,
                'processed': 1,
                'recurring_id': None,
                'is_bucket': 0,
                'original_amount': None,
                'pending': 0,
                'auto_confirmed': 0,
                'is_auto_adjustment': 1
            }
            
            if diff > 0:
                new_entry['category_id'] = income_cat_id
                table = 'income_entries'
            else:
                new_entry['category_id'] = expense_cat_id
                new_entry['bud_item_id'] = None
                table = 'expense_entries'
            
            # Append to Redis list
            redis_key = get_redis_key(table, user_id)
            cached = redis_client.get(redis_key)
            entries = json.loads(cached) if cached else []
            entries.append(new_entry)
            redis_client.setex(redis_key, INACTIVITY_TIMEOUT + 60, json.dumps(entries, cls=DecimalEncoder))
            
            # Mark dirty for flush
            mark_dirty(user_id, table)
            
            logger.info(f"User {user_id}: Added {entry_type} delta adjustment of ${adj_amount:.2f} to Redis")
        
        else:
            # ====== MYSQL-ONLY PATH ======
            if diff > 0:
                cursor.execute("""
                    INSERT INTO income_entries (category_id, date, amount, processed, is_auto_adjustment)
                    VALUES (%s, %s, %s, 1, 1)
                """, (income_cat_id, target_date_str, adj_amount))
            else:
                cursor.execute("""
                    INSERT INTO expense_entries (category_id, date, amount, processed, is_auto_adjustment)
                    VALUES (%s, %s, %s, 1, 1)
                """, (expense_cat_id, target_date_str, adj_amount))
            
            conn.commit()
            logger.info(f"User {user_id}: Added {entry_type} delta adjustment of ${adj_amount:.2f} to MySQL")
        
        return True, f"Created {entry_type} adjustment of ${adj_amount:.2f}"
        
    except Exception as e:
        logger.error(f"User {user_id}: Error creating auto-adjustment: {e}")
        return False, str(e)


def update_savings_balance(cursor, conn, user_id, bank_balance, account_id=None, target_date=None):
    """
    Update target date's savings to match bank balance by storing a DELTA adjustment.
    
    Delta approach (matches checking account auto-adjustment pattern):
    1. Get target date's current savings from savings_entries (post first recalc,
       which already includes any previous adjustment)
    2. Get existing adjustment for target date (if any)
    3. base_savings = current_savings - existing_adjustment
    4. new_delta = bank_balance - base_savings
    5. Store new_delta in savings_adjustments
    
    The second recalculation then picks up the delta and adds it:
      savings = last_savings + expenses - income + adjustment_delta
    
    This is idempotent — re-runs produce the same delta.
    
    Args:
        target_date: Date to adjust (date object or YYYY-MM-DD string). Defaults to yesterday.
    """
    try:
        import time
        
        if target_date is not None:
            if isinstance(target_date, str):
                yesterday = date.fromisoformat(target_date[:10])
            else:
                yesterday = target_date
        else:
            yesterday = date.today() - timedelta(days=1)
        target_date_str = yesterday.strftime('%Y-%m-%d')
        
        # Step 1: Get yesterday's current savings (after first recalculation)
        current_savings = None
        user_hydrated = is_user_hydrated(user_id)
        
        if user_hydrated:
            savings_key = get_redis_key('savings_entries', user_id)
            cached = redis_client.get(savings_key)
            if cached:
                savings_list = json.loads(cached)
                for s in savings_list:
                    if str(s.get('date', ''))[:10] == target_date_str:
                        current_savings = float(s.get('amount', 0))
                        break
        
        if current_savings is None:
            cursor.execute("""
                SELECT amount FROM savings_entries
                WHERE user_id = %s AND date = %s
            """, (user_id, target_date_str))
            row = cursor.fetchone()
            current_savings = float(row[0]) if row and row[0] is not None else 0.0
        
        # Step 2: Get existing adjustment for yesterday (if any)
        existing_adjustment = 0.0
        existing_adj_record = None
        
        if user_hydrated:
            adj_key = get_redis_key('savings_adjustments', user_id)
            adj_cached = redis_client.get(adj_key)
            adjustments = json.loads(adj_cached) if adj_cached else []
        else:
            adjustments = []
        
        for adj in adjustments:
            if str(adj.get('date', ''))[:10] == target_date_str:
                existing_adjustment = float(adj.get('amount', 0))
                existing_adj_record = adj
                break
        
        if not user_hydrated and existing_adj_record is None:
            cursor.execute("""
                SELECT id, amount FROM savings_adjustments
                WHERE user_id = %s AND date = %s
            """, (user_id, target_date_str))
            row = cursor.fetchone()
            if row:
                existing_adjustment = float(row[1]) if row[1] is not None else 0.0
        
        # Step 3: Calculate base savings (without current adjustment)
        base_savings = current_savings - existing_adjustment
        
        # Step 4: Calculate delta needed
        bank_float = float(bank_balance)
        delta = bank_float - base_savings
        
        logger.info(f"User {user_id}: Savings - Current=${current_savings:.2f}, Base=${base_savings:.2f}, Bank=${bank_float:.2f}, Delta=${delta:.2f}")
        
        # Step 5: Skip if already balanced
        if abs(delta) < 0.01:
            logger.info(f"User {user_id}: Savings already balanced, no adjustment needed")
            return True, "Already balanced"
        
        # Step 6: Store the delta in savings_adjustments
        if user_hydrated:
            # ====== REDIS-FIRST PATH ======
            try:
                if existing_adj_record:
                    existing_adj_record['amount'] = delta
                    existing_adj_record['quiltt_account_id'] = account_id
                else:
                    temp_id = -(int(time.time() * 1000) % 1000000000)
                    adjustments.append({
                        'id': temp_id,
                        'user_id': user_id,
                        'date': target_date_str,
                        'amount': delta,
                        'description': 'Nightly sync from bank',
                        'quiltt_account_id': account_id
                    })
                
                adj_key = get_redis_key('savings_adjustments', user_id)
                redis_client.setex(adj_key, INACTIVITY_TIMEOUT + 60, json.dumps(adjustments, cls=DecimalEncoder))
                mark_dirty(user_id, 'savings_adjustments')
                
                logger.info(f"User {user_id}: Stored savings delta adjustment of ${delta:.2f} in Redis")
                    
            except Exception as e:
                logger.warning(f"User {user_id}: Redis update failed: {e}")
        else:
            # ====== MYSQL-ONLY PATH ======
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
                """, (delta, account_id, existing[0]))
            else:
                cursor.execute("""
                    INSERT INTO savings_adjustments (user_id, date, amount, description, quiltt_account_id)
                    VALUES (%s, %s, %s, %s, %s)
                """, (user_id, target_date_str, delta, 'Nightly sync from bank', account_id))
            
            conn.commit()
            logger.info(f"User {user_id}: Stored savings delta adjustment of ${delta:.2f} in MySQL")
        
        return True, f"Savings adjustment delta: ${delta:.2f}"
        
    except Exception as e:
        logger.error(f"User {user_id}: Error updating savings balance: {e}")
        return False, str(e)
        
        return True, f"Updated savings to ${bank_balance:.2f}"
        
    except Exception as e:
        logger.error(f"User {user_id}: Error updating savings: {e}")
        return False, str(e)


# ============================================================================
# BUCKET ENTRY CLEANUP
# Remove expired bucket entries from the previous day
# ============================================================================

def cleanup_expired_bucket_entries(cursor, conn, user_id, target_date=None):
    """
    Handle bucket entries (is_bucket=1) from the target date.
    
    Bucket entries are placeholders for expected recurring income/expenses.
    Once the day passes, these need to be handled:
    
    - For Quiltt-linked accounts: DELETE the bucket entry (real transaction comes from bank sync)
    - For non-Quiltt accounts: CONVERT to regular entry (set is_bucket=0)
    
    Uses Redis-first architecture: reads entries from Redis if hydrated, then
    modifies both Redis and MySQL. If not hydrated, reads/writes MySQL directly.
    
    Args:
        cursor: MySQL cursor
        conn: MySQL connection
        user_id: User ID
        target_date: Date to process (date object or YYYY-MM-DD string). Defaults to yesterday.
        
    Returns:
        dict with counts of processed entries per table
    """
    if target_date is not None:
        yesterday = str(target_date)[:10]
    else:
        yesterday = (date.today() - timedelta(days=1)).isoformat()
    
    results = {}
    user_hydrated = is_user_hydrated(user_id)
    
    # -------------------------------------------------------------------------
    # Check if user has any Quiltt-linked depository accounts (for income/expense)
    # Use quiltt_accounts from Redis if hydrated
    # -------------------------------------------------------------------------
    has_quiltt_depository = False
    if user_hydrated:
        try:
            qa_key = get_redis_key('quiltt_accounts', user_id)
            qa_cached = redis_client.get(qa_key)
            if qa_cached:
                qa_list = json.loads(qa_cached)
                has_quiltt_depository = any(
                    qa.get('is_active') and str(qa.get('account_type', '')).upper() == 'DEPOSITORY'
                    for qa in qa_list
                )
        except Exception:
            pass
    
    if not has_quiltt_depository:
        cursor.execute("""
            SELECT COUNT(*) FROM quiltt_accounts qa
            JOIN quiltt_connections qc ON qa.connection_id = qc.id
            WHERE qa.user_id = %s AND qa.is_active = 1 AND qa.account_type = 'DEPOSITORY'
        """, (user_id,))
        has_quiltt_depository = cursor.fetchone()[0] > 0
    
    # -------------------------------------------------------------------------
    # Process income_entries (Redis-first)
    # -------------------------------------------------------------------------
    try:
        all_income = get_entries_from_redis_or_mysql(cursor, 'income_entries', user_id)
        entry_ids = [e.get('id') for e in all_income 
                     if e.get('is_bucket') and str(e.get('date', ''))[:10] <= yesterday]
        
        if entry_ids:
            if has_quiltt_depository:
                action = 'deleted'
            else:
                action = 'converted'
            
            # REDIS-FIRST: If hydrated, update Redis only (flush worker syncs to MySQL)
            if user_hydrated:
                _update_redis_after_bucket_cleanup(user_id, 'income_entries', entry_ids, has_quiltt_depository)
            else:
                # MYSQL-ONLY: User not hydrated
                placeholders = ','.join(['%s'] * len(entry_ids))
                if has_quiltt_depository:
                    cursor.execute(f"DELETE FROM income_entries WHERE id IN ({placeholders})", entry_ids)
                else:
                    cursor.execute(f"UPDATE income_entries SET is_bucket = 0 WHERE id IN ({placeholders})", entry_ids)
                conn.commit()
            
            results['income_entries'] = len(entry_ids)
            if results['income_entries'] > 0:
                logger.info(f"User {user_id}: {action.capitalize()} {results['income_entries']} bucket entries from income_entries for {yesterday}")
        else:
            results['income_entries'] = 0
            
    except Exception as e:
        logger.error(f"User {user_id}: Error processing bucket entries from income_entries: {e}")
        results['income_entries'] = 0
    
    # -------------------------------------------------------------------------
    # Process expense_entries (Redis-first)
    # -------------------------------------------------------------------------
    try:
        all_expense = get_entries_from_redis_or_mysql(cursor, 'expense_entries', user_id)
        entry_ids = [e.get('id') for e in all_expense
                     if e.get('is_bucket') and str(e.get('date', ''))[:10] <= yesterday]
        
        if entry_ids:
            if has_quiltt_depository:
                action = 'deleted'
            else:
                action = 'converted'
            
            # REDIS-FIRST: If hydrated, update Redis only (flush worker syncs to MySQL)
            if user_hydrated:
                _update_redis_after_bucket_cleanup(user_id, 'expense_entries', entry_ids, has_quiltt_depository)
            else:
                # MYSQL-ONLY: User not hydrated
                placeholders = ','.join(['%s'] * len(entry_ids))
                if has_quiltt_depository:
                    cursor.execute(f"DELETE FROM expense_entries WHERE id IN ({placeholders})", entry_ids)
                else:
                    cursor.execute(f"UPDATE expense_entries SET is_bucket = 0 WHERE id IN ({placeholders})", entry_ids)
                conn.commit()
            
            results['expense_entries'] = len(entry_ids)
            if results['expense_entries'] > 0:
                logger.info(f"User {user_id}: {action.capitalize()} {results['expense_entries']} bucket entries from expense_entries for {yesterday}")
        else:
            results['expense_entries'] = 0
            
    except Exception as e:
        logger.error(f"User {user_id}: Error processing bucket entries from expense_entries: {e}")
        results['expense_entries'] = 0
    
    # -------------------------------------------------------------------------
    # Process c_expense_entries (credit accounts) - Redis-first
    # Need to check per-account if it's Quiltt-linked
    # -------------------------------------------------------------------------
    try:
        all_c_expenses = get_entries_from_redis_or_mysql(cursor, 'c_expense_entries', user_id)
        all_c_cats = get_entries_from_redis_or_mysql(cursor, 'c_expense_categories', user_id)
        all_credit_accts = get_entries_from_redis_or_mysql(cursor, 'credit_accounts', user_id)
        
        # Build lookups: category_id -> account_id, account_id -> is_quiltt
        cat_to_account = {int(c['id']): int(c['account_id']) for c in all_c_cats} if all_c_cats else {}
        acct_is_quiltt = {int(a['id']): bool(a.get('is_quiltt', 0)) for a in all_credit_accts} if all_credit_accts else {}
        
        quiltt_entry_ids = []
        non_quiltt_entry_ids = []
        
        for entry in all_c_expenses:
            if entry.get('is_bucket') and str(entry.get('date', ''))[:10] <= yesterday:
                entry_id = entry.get('id')
                cat_id = int(entry.get('category_id', 0))
                account_id = cat_to_account.get(cat_id, 0)
                if acct_is_quiltt.get(account_id, False):
                    quiltt_entry_ids.append(entry_id)
                else:
                    non_quiltt_entry_ids.append(entry_id)
        
        total_processed = 0
        
        # DELETE Quiltt-linked bucket entries
        if quiltt_entry_ids:
            # REDIS-FIRST: If hydrated, update Redis only (flush worker syncs to MySQL)
            if user_hydrated:
                _update_redis_after_bucket_cleanup(user_id, 'c_expense_entries', quiltt_entry_ids, True)
            else:
                # MYSQL-ONLY: User not hydrated
                placeholders = ','.join(['%s'] * len(quiltt_entry_ids))
                cursor.execute(f"DELETE FROM c_expense_entries WHERE id IN ({placeholders})", quiltt_entry_ids)
                conn.commit()
            total_processed += len(quiltt_entry_ids)
            logger.info(f"User {user_id}: Deleted {len(quiltt_entry_ids)} bucket entries from c_expense_entries (Quiltt-linked) for {yesterday}")
        
        # CONVERT non-Quiltt bucket entries
        if non_quiltt_entry_ids:
            # REDIS-FIRST: If hydrated, update Redis only (flush worker syncs to MySQL)
            if user_hydrated:
                _update_redis_after_bucket_cleanup(user_id, 'c_expense_entries', non_quiltt_entry_ids, False)
            else:
                # MYSQL-ONLY: User not hydrated
                placeholders = ','.join(['%s'] * len(non_quiltt_entry_ids))
                cursor.execute(f"UPDATE c_expense_entries SET is_bucket = 0 WHERE id IN ({placeholders})", non_quiltt_entry_ids)
                conn.commit()
            total_processed += len(non_quiltt_entry_ids)
            logger.info(f"User {user_id}: Converted {len(non_quiltt_entry_ids)} bucket entries from c_expense_entries (non-Quiltt) for {yesterday}")
        
        results['c_expense_entries'] = total_processed
            
    except Exception as e:
        logger.error(f"User {user_id}: Error processing bucket entries from c_expense_entries: {e}")
        results['c_expense_entries'] = 0
    
    return results


def _update_redis_after_bucket_cleanup(user_id, entry_table, entry_ids, delete_entries):
    """
    Update Redis after bucket entry cleanup.
    
    Args:
        user_id: User ID
        entry_table: Table name (income_entries, expense_entries, c_expense_entries)
        entry_ids: List of entry IDs that were processed
        delete_entries: True to remove entries, False to set is_bucket=0
    """
    try:
        redis_key = get_redis_key(entry_table, user_id)
        redis_data = redis_client.get(redis_key)
        
        if redis_data:
            entries_list = json.loads(redis_data)
            entry_ids_set = set(entry_ids)
            
            if delete_entries:
                # Remove entries from list
                entries_list = [e for e in entries_list if e.get('id') not in entry_ids_set]
            else:
                # Set is_bucket=0 for these entries
                for entry in entries_list:
                    if entry.get('id') in entry_ids_set:
                        entry['is_bucket'] = 0
            
            # Save back to Redis
            redis_client.setex(redis_key, INACTIVITY_TIMEOUT + 60, json.dumps(entries_list, cls=DecimalEncoder))
            mark_dirty(user_id, entry_table)
            
    except Exception as redis_err:
        logger.warning(f"User {user_id}: Redis cleanup error for {entry_table}: {redis_err}")


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

        # Get previous day's remainder (Redis-first)
        prev_date = start_date - timedelta(days=1)
        last_day_remainder = None
        
        if is_user_hydrated(user_id):
            last_day_remainder = get_remainder_from_redis('totals_remainders_d', user_id, prev_date)
        
        # Fallback to MySQL if not found in Redis
        if last_day_remainder is None:
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

        # REDIS-FIRST: If hydrated, update Redis only (flush worker syncs to MySQL)
        if is_user_hydrated(user_id):
            update_totals_remainders_in_redis('totals_remainders_d', user_id, updates)
        else:
            # MYSQL-ONLY: User not hydrated, update MySQL directly
            for update in updates:
                cursor.execute("""
                    UPDATE totals_remainders_d
                    SET total_income = %s, total_expenses = %s, remainder = %s, last_day_remainder = %s
                    WHERE user_id = %s AND date = %s
                """, (update['total_income'], update['total_expenses'], update['remainder'], 
                      update['last_day_remainder'], user_id, update['date']))
            conn.commit()
        
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

            # In goofy mode, store on Thursday (week_end) instead of Friday (week_start)
            store_date = week_end if goofy_week_mode else week_date

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

            prev_store_date = store_date - timedelta(days=7)
            # Try to get from date_to_remainder first (already calculated this run)
            if prev_store_date in date_to_remainder:
                last_week_remainder = date_to_remainder[prev_store_date]
            else:
                # REDIS-FIRST: Check Redis before MySQL
                last_week_remainder = None
                if is_user_hydrated(user_id):
                    last_week_remainder = get_remainder_from_redis('totals_remainders', user_id, prev_store_date)
                
                # Fallback to MySQL if not in Redis
                if last_week_remainder is None:
                    cursor.execute("""
                        SELECT remainder FROM totals_remainders
                        WHERE user_id = %s AND date = %s
                    """, (user_id, prev_store_date))
                    row = cursor.fetchone()
                    last_week_remainder = float(row[0]) if row and row[0] is not None else 0.0

            total_income_with_remainder = total_income + float(last_week_remainder)
            week_remainder = total_income_with_remainder - total_expenses

            updates.append({
                'date': store_date,
                'total_income': float(total_income_with_remainder),
                'total_expenses': float(total_expenses),
                'remainder': float(week_remainder),
                'last_week_remainder': float(last_week_remainder)
            })

            date_to_remainder[store_date] = week_remainder

        # REDIS-FIRST: If hydrated, update Redis only (flush worker syncs to MySQL)
        if is_user_hydrated(user_id):
            update_totals_remainders_in_redis('totals_remainders', user_id, updates)
        else:
            # MYSQL-ONLY: User not hydrated, update MySQL directly
            for update in updates:
                cursor.execute("""
                    UPDATE totals_remainders
                    SET total_income = %s, total_expenses = %s, remainder = %s, last_week_remainder = %s
                    WHERE user_id = %s AND date = %s
                """, (update['total_income'], update['total_expenses'], update['remainder'],
                      update['last_week_remainder'], user_id, update['date']))
            conn.commit()
        
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
            
            last_month_remainder = date_to_remainder.get(prev_last_day, None)
            
            # If not in date_to_remainder, try Redis first, then MySQL
            if last_month_remainder is None:
                # REDIS-FIRST: Check Redis before MySQL
                if is_user_hydrated(user_id):
                    last_month_remainder = get_remainder_from_redis('totals_remainders_m', user_id, prev_last_day)
                
                # Fallback to MySQL if not in Redis
                if last_month_remainder is None:
                    cursor.execute("""
                        SELECT remainder FROM totals_remainders_m
                        WHERE user_id = %s AND date = %s
                    """, (user_id, prev_last_day))
                    prev_row = cursor.fetchone()
                    last_month_remainder = float(prev_row[0]) if prev_row and prev_row[0] is not None else 0.0

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

        # REDIS-FIRST: If hydrated, update Redis only (flush worker syncs to MySQL)
        if is_user_hydrated(user_id):
            update_totals_remainders_in_redis('totals_remainders_m', user_id, updates)
        else:
            # MYSQL-ONLY: User not hydrated, update MySQL directly
            for update in updates:
                cursor.execute("""
                    UPDATE totals_remainders_m
                    SET total_income = %s, total_expenses = %s, remainder = %s, last_month_remainder = %s
                    WHERE user_id = %s AND date = %s
                """, (update['total_income'], update['total_expenses'], update['remainder'],
                      update['last_month_remainder'], user_id, update['date']))
            conn.commit()
        
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

        # Get previous day's savings (REDIS-FIRST)
        prev_date = start_date - timedelta(days=1)
        last_savings = None
        
        if is_user_hydrated(user_id):
            last_savings = get_remainder_from_redis('savings_entries', user_id, prev_date, field_name='amount')
        
        # Fallback to MySQL if not in Redis
        if last_savings is None:
            cursor.execute("""
                SELECT amount FROM savings_entries
                WHERE user_id = %s AND date = %s
            """, (user_id, prev_date))
            prev_row = cursor.fetchone()
            last_savings = float(prev_row[0]) if prev_row and prev_row[0] is not None else 0.0

        # Get entries
        income_entries = get_entries_from_redis_or_mysql(cursor, 'income_entries', user_id)
        expense_entries = get_entries_from_redis_or_mysql(cursor, 'expense_entries', user_id)
        
        # Get savings adjustments (Redis-first)
        all_adjustments = get_entries_from_redis_or_mysql(cursor, 'savings_adjustments', user_id)
        adjustment_by_date = {}
        for adj in all_adjustments:
            adj_date = adj.get('date')
            if isinstance(adj_date, str):
                adj_date = datetime.strptime(adj_date[:10], '%Y-%m-%d').date()
            adj_amount = float(adj.get('amount', 0))
            adjustment_by_date[adj_date] = adjustment_by_date.get(adj_date, 0) + adj_amount
        
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
            
            # Check if there's an adjustment for this date (from profile setup or bank sync)
            adjustment_delta = adjustment_by_date.get(current_date, 0.0)

            # Calculate savings: previous + expenses - income + any adjustment
            savings = last_savings + total_expenses - total_income + adjustment_delta
            
            updates.append({
                'date': current_date,
                'amount': float(savings)
            })
            last_savings = savings

        # REDIS-FIRST: If hydrated, update Redis only (flush worker syncs to MySQL)
        if is_user_hydrated(user_id):
            set_savings_entries_to_redis(user_id, updates)
        else:
            # MYSQL-ONLY: User not hydrated, update MySQL directly
            for update in updates:
                cursor.execute("""
                    INSERT INTO savings_entries (user_id, date, amount, processed)
                    VALUES (%s, %s, %s, 1)
                    ON DUPLICATE KEY UPDATE amount = %s, processed = 1
                """, (user_id, update['date'], update['amount'], update['amount']))
            conn.commit()
        
        logger.info(f"User {user_id}: Recalculated {len(updates)} savings entries from {start_date}")
        
    except Exception as e:
        logger.error(f"User {user_id}: Error recalculating savings: {e}")


def recalculate_ca_daily_balances(cursor, conn, user_id, start_date):
    """
    Recalculate c_a_balances_d (daily credit account balances) from start_date forward.
    Uses Redis-first for c_expense_entries and c_payment_entries.
    """
    try:
        # Get all credit accounts from Redis or MySQL
        credit_accounts = get_entries_from_redis_or_mysql(cursor, 'credit_accounts', user_id)
        if not credit_accounts:
            return
        account_ids = [int(ca['id']) for ca in credit_accounts]

        # Get all c_expense_entries and c_payment_entries from Redis or MySQL
        all_c_expenses = get_entries_from_redis_or_mysql(cursor, 'c_expense_entries', user_id)
        all_c_payments = get_entries_from_redis_or_mysql(cursor, 'c_payment_entries', user_id)

        # We also need c_expense_categories to map category_id -> account_id
        # Build lookup from the entries + categories
        c_expense_cats = get_entries_from_redis_or_mysql(cursor, 'c_expense_categories', user_id)
        cat_to_account = {int(c['id']): int(c['account_id']) for c in c_expense_cats} if c_expense_cats else {}

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

            # Get previous balance (REDIS-FIRST)
            prev_date = min_date - timedelta(days=1)
            last_day_balance = None
            
            if is_user_hydrated(user_id):
                last_day_balance = get_ca_balance_from_redis('c_a_balances_d', user_id, account_id, prev_date)
            
            # Fallback to MySQL if not in Redis
            if last_day_balance is None:
                cursor.execute("""
                    SELECT balance FROM c_a_balances_d
                    WHERE account_id = %s AND date = %s
                """, (account_id, prev_date))
                prev_row = cursor.fetchone()
                last_day_balance = float(prev_row[0]) if prev_row and prev_row[0] is not None else 0.0

            # Aggregate expenses by date for this account (from Redis/MySQL data)
            expense_by_date = {}
            for entry in all_c_expenses:
                entry_cat_id = int(entry.get('category_id', 0))
                if cat_to_account.get(entry_cat_id) == account_id:
                    entry_date = entry.get('date')
                    if isinstance(entry_date, str):
                        entry_date = datetime.strptime(entry_date[:10], '%Y-%m-%d').date()
                    if min_date <= entry_date <= max_date:
                        expense_by_date[entry_date] = expense_by_date.get(entry_date, 0) + float(entry.get('amount', 0))

            # Aggregate payments by date for this account
            payments_by_date = {}
            for entry in all_c_payments:
                if int(entry.get('account_id', 0)) == account_id:
                    entry_date = entry.get('date')
                    if isinstance(entry_date, str):
                        entry_date = datetime.strptime(entry_date[:10], '%Y-%m-%d').date()
                    if min_date <= entry_date <= max_date:
                        payments_by_date[entry_date] = payments_by_date.get(entry_date, 0) + float(entry.get('amount', 0))

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

            # Collect all updates for this account (don't update MySQL yet)
            all_redis_updates.extend(updates)

        # REDIS-FIRST: If hydrated, update Redis only (flush worker syncs to MySQL)
        if is_user_hydrated(user_id):
            if all_redis_updates:
                set_ca_balances_to_redis('c_a_balances_d', user_id, all_redis_updates)
        else:
            # MYSQL-ONLY: User not hydrated, update MySQL directly
            for update in all_redis_updates:
                cursor.execute("""
                    UPDATE c_a_balances_d
                    SET total_expenses = %s, total_payments = %s, balance = %s
                    WHERE account_id = %s AND date = %s
                """, (update['total_expenses'], update['total_payments'], update['balance'],
                      update['account_id'], update['date']))
            conn.commit()
        
        logger.info(f"User {user_id}: Recalculated {len(all_redis_updates)} daily CA balances from {start_date}")
        
    except Exception as e:
        logger.error(f"User {user_id}: Error recalculating daily CA balances: {e}")


def recalculate_ca_weekly_balances(cursor, conn, user_id, start_date, goofy_week_mode=False):
    """
    Recalculate c_a_balances (weekly credit account balances) from start_date forward.
    Uses Redis-first for c_expense_entries and c_payment_entries.
    """
    try:
        credit_accounts = get_entries_from_redis_or_mysql(cursor, 'credit_accounts', user_id)
        if not credit_accounts:
            return
        account_ids = [int(ca['id']) for ca in credit_accounts]

        def get_week_range(week_date):
            if goofy_week_mode:
                week_start = week_date
                week_end = week_start + timedelta(days=6)
            else:
                week_end = week_date
                week_start = week_end - timedelta(days=6)
            return week_start, week_end

        # Get all entries from Redis or MySQL once
        all_c_expenses = get_entries_from_redis_or_mysql(cursor, 'c_expense_entries', user_id)
        all_c_payments = get_entries_from_redis_or_mysql(cursor, 'c_payment_entries', user_id)
        c_expense_cats = get_entries_from_redis_or_mysql(cursor, 'c_expense_categories', user_id)
        cat_to_account = {int(c['id']): int(c['account_id']) for c in c_expense_cats} if c_expense_cats else {}

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

            # Aggregate expenses by date for this account from Redis/MySQL data
            expenses_by_date = {}
            for entry in all_c_expenses:
                entry_cat_id = int(entry.get('category_id', 0))
                if cat_to_account.get(entry_cat_id) == account_id:
                    entry_date = entry.get('date')
                    if isinstance(entry_date, str):
                        entry_date = datetime.strptime(entry_date[:10], '%Y-%m-%d').date()
                    if earliest_start <= entry_date <= latest_end:
                        expenses_by_date[entry_date] = expenses_by_date.get(entry_date, 0) + float(entry.get('amount', 0))

            # Aggregate payments by date for this account
            payments_by_date = {}
            for entry in all_c_payments:
                if int(entry.get('account_id', 0)) == account_id:
                    entry_date = entry.get('date')
                    if isinstance(entry_date, str):
                        entry_date = datetime.strptime(entry_date[:10], '%Y-%m-%d').date()
                    if earliest_start <= entry_date <= latest_end:
                        payments_by_date[entry_date] = payments_by_date.get(entry_date, 0) + float(entry.get('amount', 0))

            # Get previous week balance (REDIS-FIRST)
            # In goofy mode, stored data is on Thursday (week_end), so look up previous Thursday
            earliest_ws, earliest_we = get_week_range(earliest_week)
            prev_store_date = (earliest_we if goofy_week_mode else earliest_week) - timedelta(days=7)
            last_week_balance = None
            
            if is_user_hydrated(user_id):
                last_week_balance = get_ca_balance_from_redis('c_a_balances', user_id, account_id, prev_store_date)
            
            # Fallback to MySQL if not in Redis
            if last_week_balance is None:
                cursor.execute("""
                    SELECT balance FROM c_a_balances
                    WHERE account_id = %s AND date = %s
                """, (account_id, prev_store_date))
                prev_row = cursor.fetchone()
                last_week_balance = float(prev_row[0]) if prev_row and prev_row[0] is not None else 0.0

            updates = []
            for week_date in all_week_dates:
                week_start, week_end = get_week_range(week_date)

                # In goofy mode, store on Thursday (week_end) instead of Friday (week_start)
                store_date = week_end if goofy_week_mode else week_date
                
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
                    'date': store_date,
                    'total_expenses': float(total_expenses),
                    'total_payments': float(total_payments),
                    'balance': float(balance)
                })
                
                last_week_balance = balance

            # Collect all updates for this account (don't update MySQL yet)
            all_redis_updates.extend(updates)

        # REDIS-FIRST: If hydrated, update Redis only (flush worker syncs to MySQL)
        if is_user_hydrated(user_id):
            if all_redis_updates:
                set_ca_balances_to_redis('c_a_balances', user_id, all_redis_updates)
        else:
            # MYSQL-ONLY: User not hydrated, update MySQL directly
            for update in all_redis_updates:
                cursor.execute("""
                    UPDATE c_a_balances
                    SET total_expenses = %s, total_payments = %s, balance = %s
                    WHERE account_id = %s AND date = %s
                """, (update['total_expenses'], update['total_payments'], update['balance'],
                      update['account_id'], update['date']))
            conn.commit()
        
        logger.info(f"User {user_id}: Recalculated {len(all_redis_updates)} weekly CA balances from {start_date}")
        
    except Exception as e:
        logger.error(f"User {user_id}: Error recalculating weekly CA balances: {e}")


def recalculate_ca_monthly_balances(cursor, conn, user_id, start_date):
    """
    Recalculate c_a_balances_m (monthly credit account balances) from start_date forward.
    Uses Redis-first for c_expense_entries and c_payment_entries.
    """
    import calendar
    
    try:
        credit_accounts = get_entries_from_redis_or_mysql(cursor, 'credit_accounts', user_id)
        if not credit_accounts:
            return
        account_ids = [int(ca['id']) for ca in credit_accounts]

        # Get all entries from Redis or MySQL once
        all_c_expenses = get_entries_from_redis_or_mysql(cursor, 'c_expense_entries', user_id)
        all_c_payments = get_entries_from_redis_or_mysql(cursor, 'c_payment_entries', user_id)
        c_expense_cats = get_entries_from_redis_or_mysql(cursor, 'c_expense_categories', user_id)
        cat_to_account = {int(c['id']): int(c['account_id']) for c in c_expense_cats} if c_expense_cats else {}

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

            min_date = min(all_month_dates).replace(day=1)
            max_date = max(all_month_dates)

            # Aggregate expenses by date for this account from Redis/MySQL data
            expenses_by_date = {}
            for entry in all_c_expenses:
                entry_cat_id = int(entry.get('category_id', 0))
                if cat_to_account.get(entry_cat_id) == account_id:
                    entry_date = entry.get('date')
                    if isinstance(entry_date, str):
                        entry_date = datetime.strptime(entry_date[:10], '%Y-%m-%d').date()
                    if min_date <= entry_date <= max_date:
                        expenses_by_date[entry_date] = expenses_by_date.get(entry_date, 0) + float(entry.get('amount', 0))

            # Aggregate payments by date for this account
            payments_by_date = {}
            for entry in all_c_payments:
                if int(entry.get('account_id', 0)) == account_id:
                    entry_date = entry.get('date')
                    if isinstance(entry_date, str):
                        entry_date = datetime.strptime(entry_date[:10], '%Y-%m-%d').date()
                    if min_date <= entry_date <= max_date:
                        payments_by_date[entry_date] = payments_by_date.get(entry_date, 0) + float(entry.get('amount', 0))

            # Get previous month balance (REDIS-FIRST)
            first_month_date = min(all_month_dates)
            prev_year = first_month_date.year if first_month_date.month > 1 else first_month_date.year - 1
            prev_month = (first_month_date.month - 1) or 12
            prev_last_day = date(prev_year, prev_month, calendar.monthrange(prev_year, prev_month)[1])
            
            last_month_balance = None
            if is_user_hydrated(user_id):
                last_month_balance = get_ca_balance_from_redis('c_a_balances_m', user_id, account_id, prev_last_day)
            
            # Fallback to MySQL if not in Redis
            if last_month_balance is None:
                cursor.execute("""
                    SELECT balance FROM c_a_balances_m
                    WHERE account_id = %s AND date = %s
                """, (account_id, prev_last_day))
                prev_row = cursor.fetchone()
                last_month_balance = float(prev_row[0]) if prev_row and prev_row[0] is not None else 0.0

            updates = []
            for month_date in all_month_dates:
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

            # Collect all updates for this account (don't update MySQL yet)
            all_redis_updates.extend(updates)

        # REDIS-FIRST: If hydrated, update Redis only (flush worker syncs to MySQL)
        if is_user_hydrated(user_id):
            if all_redis_updates:
                set_ca_balances_to_redis('c_a_balances_m', user_id, all_redis_updates)
        else:
            # MYSQL-ONLY: User not hydrated, update MySQL directly
            for update in all_redis_updates:
                cursor.execute("""
                    UPDATE c_a_balances_m
                    SET total_expenses = %s, total_payments = %s, balance = %s
                    WHERE account_id = %s AND date = %s
                """, (update['total_expenses'], update['total_payments'], update['balance'],
                      update['account_id'], update['date']))
            conn.commit()
        
        logger.info(f"User {user_id}: Recalculated {len(all_redis_updates)} monthly CA balances from {start_date}")
        
    except Exception as e:
        logger.error(f"User {user_id}: Error recalculating monthly CA balances: {e}")


# ============================================================================
# HELPER FUNCTIONS FOR RECALCULATION
# ============================================================================

def get_entries_from_redis_or_mysql(cursor, table_name, user_id):
    """
    Get entries from Redis if user is hydrated, otherwise from MySQL.
    Supports: income_entries, expense_entries, c_expense_entries, c_payment_entries,
              savings_adjustments, credit_accounts
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
    elif table_name == 'c_expense_entries':
        cursor.execute("""
            SELECT cee.id, cee.category_id, cee.date, cee.amount, cee.recurring_id,
                   cee.is_bucket, cee.original_amount, cee.processed, cee.bud_item_id
            FROM c_expense_entries cee
            JOIN c_expense_categories cec ON cee.category_id = cec.id
            JOIN credit_accounts ca ON cec.account_id = ca.id
            WHERE ca.user_id = %s
        """, (user_id,))
    elif table_name == 'c_payment_entries':
        cursor.execute("""
            SELECT cpe.id, cpe.account_id, cpe.date, cpe.amount, cpe.recurring_id, cpe.processed
            FROM c_payment_entries cpe
            JOIN credit_accounts ca ON cpe.account_id = ca.id
            WHERE ca.user_id = %s
        """, (user_id,))
    elif table_name == 'savings_adjustments':
        cursor.execute("""
            SELECT id, user_id, date, amount, description, quiltt_account_id
            FROM savings_adjustments
            WHERE user_id = %s
        """, (user_id,))
    elif table_name == 'c_expense_categories':
        cursor.execute("""
            SELECT cec.id, cec.account_id, cec.name, cec.display_order, cec.group_id,
                   cec.is_recurring, cec.no_end_date, cec.hidden, cec.is_bud,
                   cec.is_interest, cec.is_auto_adjustment
            FROM c_expense_categories cec
            JOIN credit_accounts ca ON cec.account_id = ca.id
            WHERE ca.user_id = %s
        """, (user_id,))
    elif table_name == 'credit_accounts':
        cursor.execute("""
            SELECT id, user_id, name, mask, quiltt_account_id, interest_rate,
                   starting_balance, is_card, is_line, is_quiltt
            FROM credit_accounts
            WHERE user_id = %s
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
        
        redis_client.setex(key, INACTIVITY_TIMEOUT + 60, json.dumps(updated_data, cls=DecimalEncoder))
        
        # Mark as dirty
        dirty_key = f"dirty_tables:{user_id}"
        redis_client.sadd(dirty_key, table_name)
        redis_client.expire(dirty_key, INACTIVITY_TIMEOUT + 60)
        
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
        
        redis_client.setex(key, INACTIVITY_TIMEOUT + 60, json.dumps(updated_data, cls=DecimalEncoder))
        
        dirty_key = f"dirty_tables:{user_id}"
        redis_client.sadd(dirty_key, 'savings_entries')
        redis_client.expire(dirty_key, INACTIVITY_TIMEOUT + 60)
        
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
        
        redis_client.setex(key, INACTIVITY_TIMEOUT + 60, json.dumps(updated_data, cls=DecimalEncoder))
        
        dirty_key = f"dirty_tables:{user_id}"
        redis_client.sadd(dirty_key, table_name)
        redis_client.expire(dirty_key, INACTIVITY_TIMEOUT + 60)
        
    except Exception as e:
        logger.warning(f"Error setting {table_name} in Redis for user {user_id}: {e}")


def create_credit_account_auto_adjustment(cursor, conn, user_id, quiltt_account_id, bank_balance, account_mask, account_name, target_date=None):
    """
    Create an auto-adjustment entry for a credit account to match the bank balance.
    Similar to app.py's _create_credit_account_auto_adjustment but for standalone use.
    
    Logic:
    1. Delete any existing auto-adjustment entries for target date (clean slate)
    2. Calculate the "natural" balance (without auto-adjustments)
    3. Create ONE adjustment entry for the difference
    
    Args:
        cursor: DB cursor
        conn: DB connection
        user_id: User ID
        quiltt_account_id: Quiltt account ID
        bank_balance: Current balance from the bank (positive = amount owed)
        account_mask: Account mask for fallback matching
        account_name: Account name for logging
        target_date: Date to adjust (date object or YYYY-MM-DD string). Defaults to yesterday.
        
    Returns: (success: bool, message: str)
    """
    try:
        if target_date is not None:
            if isinstance(target_date, str):
                yesterday = date.fromisoformat(target_date[:10])
            else:
                yesterday = target_date
        else:
            yesterday = date.today() - timedelta(days=1)
        target_date_str = yesterday.strftime('%Y-%m-%d')
        day_before = yesterday - timedelta(days=1)
        day_before_str = day_before.strftime('%Y-%m-%d')
        
        # Find the credit account by quiltt_account_id (Redis-first)
        all_credit_accounts = get_entries_from_redis_or_mysql(cursor, 'credit_accounts', user_id)
        ca_match = None
        for ca in all_credit_accounts:
            if ca.get('quiltt_account_id') == quiltt_account_id:
                ca_match = ca
                break
        
        if not ca_match:
            # Fallback: try by mask
            for ca in all_credit_accounts:
                if ca.get('mask') == account_mask:
                    ca_match = ca
                    break
        
        if not ca_match:
            return False, f"No credit account found for {account_name}"
        
        account_id = int(ca_match['id'])
        starting_balance = float(ca_match.get('starting_balance') or 0)
        
        # Find auto_adjustment category for this account (Redis-first)
        all_c_cats = get_entries_from_redis_or_mysql(cursor, 'c_expense_categories', user_id)
        auto_adj_category_id = None
        for cat in all_c_cats:
            if int(cat.get('account_id', 0)) == account_id and cat.get('is_auto_adjustment'):
                auto_adj_category_id = int(cat['id'])
                break
        
        if not auto_adj_category_id:
            # Create Uncategorized category with auto_adjustment flag
            user_hydrated = is_user_hydrated(user_id)
            if user_hydrated:
                # REDIS-FIRST: Add category to Redis with temp ID
                try:
                    redis_key = get_redis_key('c_expense_categories', user_id)
                    cached = redis_client.get(redis_key)
                    cats = json.loads(cached) if cached else []
                    
                    # Generate temp ID
                    temp_id = -(int(time.time() * 1000) % 1000000000)
                    
                    cats.append({
                        'id': temp_id,
                        'account_id': account_id,
                        'name': 'Uncategorized',
                        'display_order': 0,
                        'group_id': None,
                        'is_recurring': 0,
                        'no_end_date': 0,
                        'hidden': 0,
                        'is_bud': 0,
                        'is_interest': 0,
                        'is_auto_adjustment': 1
                    })
                    
                    redis_client.setex(redis_key, INACTIVITY_TIMEOUT + 60, json.dumps(cats, cls=DecimalEncoder))
                    mark_dirty(user_id, 'c_expense_categories')
                    auto_adj_category_id = temp_id
                    logger.info(f"User {user_id}: Created Uncategorized category (temp ID {temp_id}) for account {account_id} in Redis")
                except Exception as e:
                    logger.warning(f"User {user_id}: Redis error creating category, falling back to MySQL: {e}")
                    cursor.execute("""
                        INSERT INTO c_expense_categories (account_id, name, display_order, is_auto_adjustment)
                        VALUES (%s, 'Uncategorized', 0, 1)
                    """, (account_id,))
                    conn.commit()
                    auto_adj_category_id = cursor.lastrowid
            else:
                # MYSQL-ONLY: User not hydrated
                cursor.execute("""
                    INSERT INTO c_expense_categories (account_id, name, display_order, is_auto_adjustment)
                    VALUES (%s, 'Uncategorized', 0, 1)
                """, (account_id,))
                conn.commit()
                auto_adj_category_id = cursor.lastrowid
                logger.info(f"User {user_id}: Created Uncategorized category {auto_adj_category_id} for account {account_id}")
        
        # Step 1: Delete existing auto-adjustment entries for target date
        # REDIS-FIRST: If hydrated, delete from Redis only + mark dirty (flush worker handles MySQL)
        user_hydrated = is_user_hydrated(user_id)
        
        if user_hydrated:
            try:
                # Remove target date's auto-adjustment expenses from Redis
                redis_key = get_redis_key('c_expense_entries', user_id)
                cached = redis_client.get(redis_key)
                if cached:
                    entries = json.loads(cached)
                    # Track which IDs we're deleting for pending_deletes
                    deleted_expense_ids = [e.get('id') for e in entries if (
                        int(e.get('category_id', 0)) == auto_adj_category_id and 
                        str(e.get('date', ''))[:10] == target_date_str and
                        e.get('is_auto_adjustment') and
                        e.get('id') is not None and int(e.get('id', 0)) > 0  # Only real IDs
                    )]
                    entries = [e for e in entries if not (
                        int(e.get('category_id', 0)) == auto_adj_category_id and 
                        str(e.get('date', ''))[:10] == target_date_str and
                        e.get('is_auto_adjustment')
                    )]
                    redis_client.setex(redis_key, INACTIVITY_TIMEOUT + 60, json.dumps(entries, cls=DecimalEncoder))
                    
                    # Mark pending deletes for real IDs
                    if deleted_expense_ids:
                        pending_key = f"pending_deletes:c_expense_entries:{user_id}"
                        for del_id in deleted_expense_ids:
                            redis_client.sadd(pending_key, str(del_id))
                        redis_client.expire(pending_key, INACTIVITY_TIMEOUT + 60)
                    
                    mark_dirty(user_id, 'c_expense_entries')
                
                # Remove target date's non-recurring payments from Redis
                redis_key = get_redis_key('c_payment_entries', user_id)
                cached = redis_client.get(redis_key)
                if cached:
                    entries = json.loads(cached)
                    # Track which IDs we're deleting
                    deleted_payment_ids = [e.get('id') for e in entries if (
                        int(e.get('account_id', 0)) == account_id and 
                        str(e.get('date', ''))[:10] == target_date_str and
                        e.get('recurring_id') is None and
                        e.get('id') is not None and int(e.get('id', 0)) > 0
                    )]
                    entries = [e for e in entries if not (
                        int(e.get('account_id', 0)) == account_id and 
                        str(e.get('date', ''))[:10] == target_date_str and
                        e.get('recurring_id') is None
                    )]
                    redis_client.setex(redis_key, INACTIVITY_TIMEOUT + 60, json.dumps(entries, cls=DecimalEncoder))
                    
                    if deleted_payment_ids:
                        pending_key = f"pending_deletes:c_payment_entries:{user_id}"
                        for del_id in deleted_payment_ids:
                            redis_client.sadd(pending_key, str(del_id))
                        redis_client.expire(pending_key, INACTIVITY_TIMEOUT + 60)
                    
                    mark_dirty(user_id, 'c_payment_entries')
            except Exception as e:
                logger.warning(f"User {user_id}: Redis delete error, falling back to MySQL: {e}")
                cursor.execute("""
                    DELETE FROM c_expense_entries 
                    WHERE category_id = %s AND date = %s AND is_auto_adjustment = 1
                """, (auto_adj_category_id, target_date_str))
                cursor.execute("""
                    DELETE FROM c_payment_entries 
                    WHERE account_id = %s AND date = %s AND recurring_id IS NULL
                """, (account_id, target_date_str))
                conn.commit()
        else:
            # MYSQL-ONLY: User not hydrated
            cursor.execute("""
                DELETE FROM c_expense_entries 
                WHERE category_id = %s AND date = %s AND is_auto_adjustment = 1
            """, (auto_adj_category_id, target_date_str))
            cursor.execute("""
                DELETE FROM c_payment_entries 
                WHERE account_id = %s AND date = %s AND recurring_id IS NULL
            """, (account_id, target_date_str))
            conn.commit()
        
        # Step 2: Get day before target's balance from Redis or MySQL
        day_before_balance = None
        
        if user_hydrated:
            try:
                bal_key = get_redis_key('c_a_balances_d', user_id)
                bal_cached = redis_client.get(bal_key)
                if bal_cached:
                    bal_list = json.loads(bal_cached)
                    for b in bal_list:
                        if int(b.get('account_id', 0)) == account_id and str(b.get('date', ''))[:10] == day_before_str:
                            day_before_balance = float(b.get('balance', 0))
                            break
            except Exception:
                pass
        
        if day_before_balance is None:
            cursor.execute("""
                SELECT balance FROM c_a_balances_d
                WHERE account_id = %s AND date = %s
            """, (account_id, day_before_str))
            result = cursor.fetchone()
            day_before_balance = float(result[0]) if result else starting_balance
        
        # Step 3: Calculate target date's natural expenses (excluding auto-adjustment category)
        # Use Redis-first data
        all_c_expenses = get_entries_from_redis_or_mysql(cursor, 'c_expense_entries', user_id)
        c_expense_cats = get_entries_from_redis_or_mysql(cursor, 'c_expense_categories', user_id)
        cat_to_account = {int(c['id']): int(c['account_id']) for c in c_expense_cats} if c_expense_cats else {}
        auto_adj_cat_ids = set(int(c['id']) for c in c_expense_cats if c.get('is_auto_adjustment')) if c_expense_cats else set()
        
        target_expenses = 0.0
        for entry in all_c_expenses:
            entry_cat_id = int(entry.get('category_id', 0))
            entry_date = str(entry.get('date', ''))[:10]
            if (cat_to_account.get(entry_cat_id) == account_id and 
                entry_date == target_date_str and
                entry_cat_id not in auto_adj_cat_ids):
                target_expenses += float(entry.get('amount', 0))
        
        # Step 4: Calculate target date's natural payments
        all_c_payments = get_entries_from_redis_or_mysql(cursor, 'c_payment_entries', user_id)
        target_payments = 0.0
        for entry in all_c_payments:
            if (int(entry.get('account_id', 0)) == account_id and 
                str(entry.get('date', ''))[:10] == target_date_str):
                target_payments += float(entry.get('amount', 0))
        
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
                        'bud_item_id': None,
                        'is_auto_adjustment': 1
                    })
                    
                    redis_client.setex(redis_key, INACTIVITY_TIMEOUT + 60, json.dumps(entries, cls=DecimalEncoder))
                    mark_dirty(user_id, 'c_expense_entries')
                except Exception as e:
                    logger.warning(f"User {user_id}: Redis error, falling back to MySQL: {e}")
                    cursor.execute("""
                        INSERT INTO c_expense_entries (category_id, date, amount, processed, is_auto_adjustment)
                        VALUES (%s, %s, %s, 1, 1)
                    """, (auto_adj_category_id, target_date_str, adjustment_amount))
                    conn.commit()
            else:
                cursor.execute("""
                    INSERT INTO c_expense_entries (category_id, date, amount, processed, is_auto_adjustment)
                    VALUES (%s, %s, %s, 1, 1)
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
                    
                    redis_client.setex(redis_key, INACTIVITY_TIMEOUT + 60, json.dumps(entries, cls=DecimalEncoder))
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

    # Fetch goofy_week_mode: Redis first if hydrated, then MySQL fallback
    goofy_week_mode = None
    if is_user_hydrated(user_id):
        try:
            cached = redis_client.get(f"users:{REDIS_KEY_VERSION}:{user_id}")
            if cached:
                user_data = json.loads(cached)
                if 'goofy_week_mode' in user_data:
                    goofy_week_mode = bool(int(user_data['goofy_week_mode']))
        except Exception:
            pass
    if goofy_week_mode is None:
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
    
    # If balance fetch failed (likely 401 / stale token), force refresh and retry
    if not updated_accounts:
        logger.info(f"User {user_id}: Balance fetch failed, force-refreshing session token")
        session_token = refresh_session_if_needed(cursor, conn, user_id, profile_id, session_token, session_expires, force=True)
        if session_token:
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
            'mask': mask,
            'imported_dates': set()  # Will be populated by sync
        }
        
        # Sync transactions for this account
        # Pass both last_synced_at and account_created_at
        new_txns, imported_txns, earliest_txn_date, txn_dates = sync_transactions_for_account(
            cursor, conn, user_id, account_id, session_token, last_synced_at, account_created_at
        )
        result['transactions_synced'] += new_txns
        result['transactions_imported'] += imported_txns
        accounts_info[account_id]['imported_dates'] = txn_dates
        
        # Track the earliest transaction date across all accounts
        if earliest_txn_date:
            if 'earliest_transaction_date' not in result or earliest_txn_date < result['earliest_transaction_date']:
                result['earliest_transaction_date'] = earliest_txn_date
    
    # --- UPDATE RECURRENCE DATA FROM NTROPY ---
    if result['transactions_synced'] > 0 and profile_id:
        try:
            from ntropy_utils import get_recurring_groups, build_recurrence_map
            from quiltt_redis import update_transaction_recurrence
            groups = get_recurring_groups(profile_id)
            if groups:
                recurrence_map = build_recurrence_map(groups)
                rec_updated = update_transaction_recurrence(recurrence_map, user_id)
                logger.info(f"User {user_id}: Updated {rec_updated} transactions with recurrence data")
        except Exception as rec_err:
            logger.warning(f"User {user_id}: Error updating recurrence: {rec_err}")
    
    # Update cached last transaction date for locking/marker
    try:
        cursor.execute("SELECT MAX(date) FROM quiltt_transactions WHERE user_id = %s", (user_id,))
        max_row = cursor.fetchone()
        if max_row and max_row[0]:
            last_txn_date_str = max_row[0].strftime('%Y-%m-%d') if hasattr(max_row[0], 'strftime') else str(max_row[0])
            cache_key = f"quiltt_last_txn_date:v1:{user_id}"
            redis_client.setex(cache_key, INACTIVITY_TIMEOUT, last_txn_date_str)
            result['last_transaction_date'] = last_txn_date_str
            logger.info(f"User {user_id}: Last transaction date: {last_txn_date_str}")
    except Exception as e:
        logger.warning(f"User {user_id}: Failed to cache last txn date: {e}")
    
    # =========================================================================
    # STEP 4: Clean up expired bucket entries
    # Use last synced transaction date if available (capped at yesterday)
    # =========================================================================
    last_txn_date = result.get('last_transaction_date')
    if last_txn_date:
        last_txn_date_obj = date.fromisoformat(last_txn_date[:10])
        if last_txn_date_obj > yesterday:
            last_txn_date_obj = yesterday
    else:
        last_txn_date_obj = None  # functions will default to yesterday
    
    bucket_cleanup = cleanup_expired_bucket_entries(cursor, conn, user_id, target_date=last_txn_date_obj)
    total_buckets_deleted = sum(bucket_cleanup.values())
    if total_buckets_deleted > 0:
        logger.info(f"User {user_id}: Cleaned up {total_buckets_deleted} expired bucket entries")
    
    # =========================================================================
    # STEP 4.5: Create notification for pending transactions
    # =========================================================================
    if result['transactions_imported'] > 0:
        create_pending_transactions_notification(cursor, conn, user_id, result['transactions_imported'])
    
    # =========================================================================
    # STEP 4.75: Clean up stale auto-adjustments on dates with new transactions
    # If a late bank transaction lands on a date that already had an auto-
    # adjustment, the old adjustment is now wrong.  Remove it so the first
    # recalculation rebuilds clean remainders, and yesterday's fresh
    # adjustment absorbs the full cumulative delta vs the bank.
    # =========================================================================
    # Collect all imported dates for checking/depository accounts
    checking_imported_dates = set()
    for acc_id, info in accounts_info.items():
        if info['type'] == 'depository':
            acc_name_lower = info['name'].lower()
            if 'checking' in acc_name_lower or info['subtype'] == 'checking':
                checking_imported_dates |= info.get('imported_dates', set())
    
    if checking_imported_dates:
        cleanup_stale_checking_adjustments(cursor, conn, user_id, checking_imported_dates)
    
    # Clean up stale credit account adjustments per account
    for acc_id, info in accounts_info.items():
        if info['type'] == 'credit' and info.get('imported_dates'):
            cleanup_stale_ca_adjustments(
                cursor, conn, user_id, acc_id, info['imported_dates'], info['name']
            )
    
    # =========================================================================
    # STEP 5: FIRST recalculation - get accurate totals including new transactions
    # (Must happen BEFORE auto-balance so we compare accurate remainder to bank)
    # =========================================================================
    yesterday = date.today() - timedelta(days=1)
    earliest_txn = result.get('earliest_transaction_date')
    
    if earliest_txn and earliest_txn < yesterday:
        recalc_start_date = earliest_txn
        logger.info(f"User {user_id}: Transactions imported from {earliest_txn}, recalculating from there")
    else:
        recalc_start_date = yesterday
    
    # Track remainders across calculations
    date_to_remainder = {}
    
    # Regular budget calculations
    logger.info(f"User {user_id}: Starting recalculation from {recalc_start_date}")
    
    recalculate_daily_totals(cursor, conn, user_id, recalc_start_date, date_to_remainder, goofy_week_mode)
    recalculate_weekly_totals(cursor, conn, user_id, recalc_start_date, date_to_remainder, goofy_week_mode)
    recalculate_monthly_totals(cursor, conn, user_id, recalc_start_date, date_to_remainder)
    recalculate_savings(cursor, conn, user_id, recalc_start_date)
    
    # Credit account calculations
    recalculate_ca_daily_balances(cursor, conn, user_id, recalc_start_date)
    recalculate_ca_weekly_balances(cursor, conn, user_id, recalc_start_date, goofy_week_mode)
    recalculate_ca_monthly_balances(cursor, conn, user_id, recalc_start_date)
    
    logger.info(f"User {user_id}: Completed first recalculation (pre-adjustment)")
    
    # =========================================================================
    # STEP 6: Run auto-balance for Checking, Savings, and Credit accounts
    # (Now we have accurate remainders to compare against bank balances)
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
                success, msg = create_auto_adjustment(cursor, conn, user_id, current_balance, account_name, target_date=last_txn_date_obj)
                result['adjustments'].append({
                    'account': account_name,
                    'type': 'checking',
                    'success': success,
                    'message': msg
                })
                logger.info(f"User {user_id}: Checking adjustment - {msg}")
                
            elif 'savings' in account_name_lower or account_subtype == 'savings':
                # Savings account - update savings balance
                success, msg = update_savings_balance(cursor, conn, user_id, current_balance, account_id, target_date=last_txn_date_obj)
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
                cursor, conn, user_id, account_id, current_balance, mask, account_name, target_date=last_txn_date_obj
            )
            result['adjustments'].append({
                'account': account_name,
                'type': 'credit',
                'success': success,
                'message': msg
            })
            logger.info(f"User {user_id}: Credit account adjustment - {msg}")
    
    # =========================================================================
    # STEP 7: SECOND recalculation - incorporate auto-adjustments into totals
    # =========================================================================
    # Recalculate from the adjustment date (last_txn_date or yesterday)
    adjustment_date = last_txn_date_obj if last_txn_date_obj else yesterday
    date_to_remainder = {}
    
    recalculate_daily_totals(cursor, conn, user_id, adjustment_date, date_to_remainder, goofy_week_mode)
    recalculate_weekly_totals(cursor, conn, user_id, adjustment_date, date_to_remainder, goofy_week_mode)
    recalculate_monthly_totals(cursor, conn, user_id, adjustment_date, date_to_remainder)
    recalculate_savings(cursor, conn, user_id, adjustment_date)
    
    # Credit account calculations
    recalculate_ca_daily_balances(cursor, conn, user_id, adjustment_date)
    recalculate_ca_weekly_balances(cursor, conn, user_id, adjustment_date, goofy_week_mode)
    recalculate_ca_monthly_balances(cursor, conn, user_id, adjustment_date)
    
    logger.info(f"User {user_id}: Completed final recalculation (post-adjustment, from {adjustment_date})")
    
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
