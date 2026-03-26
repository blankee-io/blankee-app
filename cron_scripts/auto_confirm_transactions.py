#!/usr/bin/env python3
"""
Auto-Confirm Pending Transactions
Runs via cron at midnight to auto-categorize pending transactions using Ntropy suggestions.

Cron entry (add to server):
    0 0 * * * cd /var/www/html/budget && /usr/bin/python3 auto_confirm_transactions.py >> /var/log/apache2/auto_confirm.log 2>&1

What it does:
1. Gets all users with pending transactions (pending=1)
2. For each pending entry, looks up the Ntropy suggestion from quiltt_transactions
3. If a valid suggestion exists (not Uncategorized):
   - Updates category_id to suggested category
   - Sets pending=0, auto_confirmed=1
   - Handles bucket reduction for recurring categories
4. If no valid suggestion, confirms to Uncategorized with auto_confirmed=1
5. Updates Redis (if user hydrated) and marks dirty for MySQL flush
"""

import os
import sys
import json
import logging
from datetime import datetime, timedelta
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
LOG_FILE = '/var/log/apache2/auto_confirm.log'
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

# Redis configuration
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_DB = int(os.getenv('REDIS_DB', 0))
REDIS_KEY_VERSION = 'v1'
REDIS_TTL = 604800  # 7 days

# Initialize Redis client
redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)


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
        database=os.getenv('DB_NAME', 'budget')
    )


def is_user_hydrated(user_id):
    """Check if user has data in Redis"""
    try:
        key = f"income_entries:{REDIS_KEY_VERSION}:{user_id}"
        return redis_client.exists(key) > 0
    except Exception as e:
        logger.warning(f"Error checking hydration for user {user_id}: {e}")
        return False


def get_redis_entries(table_name, user_id):
    """Get entries from Redis"""
    try:
        key = f"{table_name}:{REDIS_KEY_VERSION}:{user_id}"
        cached = redis_client.get(key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        logger.warning(f"Error getting Redis entries for {table_name}:{user_id}: {e}")
    return None


def save_redis_entries(table_name, user_id, entries):
    """Save entries to Redis and mark dirty"""
    try:
        key = f"{table_name}:{REDIS_KEY_VERSION}:{user_id}"
        redis_client.setex(key, REDIS_TTL, json.dumps(entries, cls=DecimalEncoder))
        
        # Mark table as dirty
        dirty_key = f"dirty_tables:{user_id}"
        redis_client.sadd(dirty_key, table_name)
        redis_client.expire(dirty_key, REDIS_TTL)
        return True
    except Exception as e:
        logger.error(f"Error saving Redis entries for {table_name}:{user_id}: {e}")
        return False


def get_recurring_info(cursor, recurring_table, user_id, category_id):
    """Get recurring info for a category (including wage_bill flag)"""
    try:
        cursor.execute(f"""
            SELECT id, amount, cadence_interval, cadence_unit, start_date, end_date, weekdays, monthly_days, wage_bill
            FROM {recurring_table}
            WHERE user_id = %s AND category_id = %s
            LIMIT 1
        """, (user_id, category_id))
        row = cursor.fetchone()
        if row:
            return {
                'id': row[0],
                'amount': float(row[1]) if row[1] else 0,
                'cadence_interval': row[2],
                'cadence_unit': row[3],
                'start_date': row[4],
                'end_date': row[5],
                'weekdays': row[6],
                'monthly_days': row[7],
                'wage_bill': int(row[8]) if row[8] is not None else 0
            }
    except Exception as e:
        logger.warning(f"Error getting recurring info: {e}")
    return None


def reduce_bucket_for_entry(cursor, conn, bucket_table, user_id, category_id, entry_date, amount, wage_bill=0, is_hydrated=False, entry_table=None):
    """
    Reduce bucket for a recurring category entry.
    
    Matches manual entry behavior from bucket_utils.process_manual_entry_with_bucket():
    - Finds the NEXT bucket (earliest with date >= today), not exact date match
    - wage_bill=1 (Wage/Bill): Removes entire bucket amount
    - wage_bill=0 (Variable/Allowance): Subtracts only the entry amount
    - Updates both the bucket ENTRY (in the entries table) and the bucket RECORD (in the recurring_*_buckets table)
    - Works via Redis if user is hydrated, otherwise MySQL
    """
    from datetime import date as date_type
    today = date_type.today()
    
    # Parse entry_date
    if isinstance(entry_date, str):
        entry_date = date_type.fromisoformat(str(entry_date)[:10])
    elif hasattr(entry_date, 'date'):
        entry_date = entry_date.date()
    
    # Only reduce bucket if entry_date <= today (future entries don't deplete)
    if entry_date > today:
        logger.info(f"Entry date {entry_date} is future, skipping bucket reduction")
        return False
    
    # --- STEP 1: Find the next bucket entry (is_bucket=1, date >= today) ---
    bucket_entry = None
    
    if is_hydrated and entry_table:
        # Find next bucket from Redis
        redis_entries = get_redis_entries(entry_table, user_id)
        if redis_entries:
            future_buckets = []
            for e in redis_entries:
                if (e.get('category_id') == int(category_id) and
                    e.get('is_bucket') == 1 and
                    float(e.get('amount', 0)) > 0):
                    bdate = str(e.get('date', ''))[:10]
                    if bdate >= str(today):
                        future_buckets.append(e)
            if future_buckets:
                future_buckets.sort(key=lambda x: str(x.get('date', '')))
                bucket_entry = future_buckets[0]
                logger.info(f"Found next bucket from Redis: id={bucket_entry.get('id')}, date={bucket_entry.get('date')}, amount={bucket_entry.get('amount')}")
    
    if not bucket_entry and entry_table:
        # Find next bucket from MySQL
        try:
            cursor.execute(f"""
                SELECT id, amount, date FROM {entry_table}
                WHERE category_id = %s AND is_bucket = 1 AND amount > 0 AND date >= %s
                ORDER BY date ASC LIMIT 1
            """, (category_id, today))
            row = cursor.fetchone()
            if row:
                bucket_entry = {'id': row[0], 'amount': float(row[1]), 'date': row[2]}
                logger.info(f"Found next bucket from MySQL: id={row[0]}, date={row[2]}, amount={row[1]}")
        except Exception as e:
            logger.error(f"Error finding next bucket from MySQL: {e}")
    
    if not bucket_entry:
        logger.info(f"No future bucket found for category {category_id}")
        return False
    
    bucket_id = bucket_entry['id']
    bucket_amount = float(bucket_entry.get('amount', 0))
    bucket_date = bucket_entry.get('date')
    if isinstance(bucket_date, str):
        bucket_date = date_type.fromisoformat(str(bucket_date)[:10])
    
    # --- STEP 2: Calculate subtraction amount based on wage_bill ---
    if wage_bill:
        subtract_amount = bucket_amount  # Remove entire bucket
        logger.info(f"Wage/Bill mode: subtracting full bucket amount {bucket_amount}")
    else:
        subtract_amount = abs(amount)  # Subtract only entry amount
        logger.info(f"Variable/Allowance mode: subtracting entry amount {subtract_amount}")
    
    # --- STEP 3: Update bucket ENTRY (in entries table) ---
    try:
        if is_hydrated and entry_table:
            # Update in Redis
            redis_entries = get_redis_entries(entry_table, user_id)
            if redis_entries:
                for e in redis_entries:
                    if e.get('id') == bucket_id and e.get('is_bucket') == 1:
                        new_amount = float(e.get('amount', 0)) - subtract_amount
                        if new_amount <= 0:
                            # Remove the bucket entry
                            redis_entries = [x for x in redis_entries if not (x.get('id') == bucket_id and x.get('is_bucket') == 1)]
                            # Mark for MySQL deletion
                            try:
                                redis_client.sadd(f"pending_deletes:{entry_table}:{user_id}", str(bucket_id))
                                redis_client.expire(f"pending_deletes:{entry_table}:{user_id}", REDIS_TTL)
                            except Exception:
                                pass
                            logger.info(f"Bucket entry {bucket_id} removed from Redis (amount went to {new_amount})")
                        else:
                            e['amount'] = new_amount
                            logger.info(f"Bucket entry {bucket_id} reduced to {new_amount} in Redis")
                        break
                save_redis_entries(entry_table, user_id, redis_entries)
        
        # Also update MySQL directly
        new_mysql_amount = bucket_amount - subtract_amount
        if new_mysql_amount <= 0:
            cursor.execute(f"DELETE FROM {entry_table} WHERE id = %s AND is_bucket = 1", (bucket_id,))
        else:
            cursor.execute(f"UPDATE {entry_table} SET amount = %s WHERE id = %s AND is_bucket = 1", (new_mysql_amount, bucket_id))
        conn.commit()
    except Exception as e:
        logger.error(f"Error updating bucket entry: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    
    # --- STEP 4: Update bucket RECORD (in recurring_*_buckets table) ---
    try:
        bucket_date_str = str(bucket_date)[:10] if bucket_date else str(entry_date)[:10]
        
        # Find the bucket record
        cursor.execute(f"""
            SELECT id, amount FROM {bucket_table}
            WHERE user_id = %s AND category_id = %s AND bucket_date = %s
        """, (user_id, category_id, bucket_date_str))
        record = cursor.fetchone()
        
        if record:
            record_id, record_amount = record
            if wage_bill:
                new_record_amount = 0  # Full removal
            else:
                new_record_amount = float(record_amount) - abs(amount)
            
            cursor.execute(f"""
                UPDATE {bucket_table} SET amount = %s WHERE id = %s
            """, (new_record_amount, record_id))
            conn.commit()
            logger.info(f"Bucket record {record_id} updated to {new_record_amount}")
            
            # Update Redis bucket record if hydrated
            if is_hydrated:
                redis_key = f"{bucket_table}:{REDIS_KEY_VERSION}:{user_id}"
                try:
                    cached = redis_client.get(redis_key)
                    if cached:
                        records = json.loads(cached)
                        for r in records:
                            if r.get('id') == record_id:
                                r['amount'] = new_record_amount
                                break
                        redis_client.setex(redis_key, REDIS_TTL, json.dumps(records, cls=DecimalEncoder))
                        redis_client.sadd(f"dirty_tables:{user_id}", bucket_table)
                        redis_client.expire(f"dirty_tables:{user_id}", REDIS_TTL)
                except Exception as re:
                    logger.warning(f"Error updating Redis bucket record: {re}")
        else:
            logger.info(f"No bucket record found for category {category_id} on {bucket_date_str}")
    except Exception as e:
        logger.error(f"Error updating bucket record: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    
    return True


def get_pending_entries_for_user(cursor, user_id):
    """Get all pending entries for a user across all entry tables"""
    pending = []
    
    # Entry table configs: (table_name, entry_type, category_table, recurring_table, bucket_table)
    configs = [
        ('income_entries', 'income', 'income_categories', 'recurring_income', 'recurring_income_buckets'),
        ('expense_entries', 'expense', 'expense_categories', 'recurring_expense', 'recurring_expense_buckets'),
        ('c_expense_entries', 'c_expense', 'c_expense_categories', 'recurring_c_expense', 'recurring_c_expense_buckets'),
    ]
    
    for table_name, entry_type, cat_table, rec_table, bucket_table in configs:
        try:
            if entry_type == 'c_expense':
                # c_expense_entries requires join through c_expense_categories -> credit_accounts
                cursor.execute("""
                    SELECT cee.id, cee.category_id, cee.date, cee.amount
                    FROM c_expense_entries cee
                    JOIN c_expense_categories cec ON cee.category_id = cec.id
                    JOIN credit_accounts ca ON cec.account_id = ca.id
                    WHERE ca.user_id = %s AND cee.pending = 1
                """, (user_id,))
            else:
                # income_entries and expense_entries join directly
                cursor.execute(f"""
                    SELECT e.id, e.category_id, e.date, e.amount
                    FROM {table_name} e
                    JOIN {cat_table} c ON e.category_id = c.id
                    WHERE c.user_id = %s AND e.pending = 1
                """, (user_id,))
            
            for row in cursor.fetchall():
                pending.append({
                    'entry_id': row[0],
                    'category_id': row[1],
                    'date': row[2],
                    'amount': float(row[3]) if row[3] else 0,
                    'table_name': table_name,
                    'entry_type': entry_type,
                    'recurring_table': rec_table,
                    'bucket_table': bucket_table
                })
        except Exception as e:
            logger.error(f"Error getting pending entries from {table_name}: {e}")
    
    return pending


def get_ntropy_suggestion(cursor, user_id, entry_id, entry_type):
    """Get Ntropy suggestion for an entry from quiltt_transactions"""
    try:
        cursor.execute("""
            SELECT custom_category_id, custom_category_suggestion, custom_category_type
            FROM quiltt_transactions
            WHERE user_id = %s AND imported_to_entry_id = %s AND imported_entry_type = %s
        """, (user_id, entry_id, entry_type))
        row = cursor.fetchone()
        if row and row[0]:  # Has custom_category_id
            return {
                'category_id': row[0],
                'category_name': row[1],
                'category_type': row[2]
            }
    except Exception as e:
        logger.warning(f"Error getting Ntropy suggestion: {e}")
    return None


def get_uncategorized_category_id(cursor, user_id, entry_type):
    """Get the Uncategorized category ID for a user/type"""
    try:
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
            cursor.execute("""
                SELECT cec.id FROM c_expense_categories cec
                JOIN credit_accounts ca ON cec.account_id = ca.id
                WHERE ca.user_id = %s AND LOWER(cec.name) = 'uncategorized'
                LIMIT 1
            """, (user_id,))
        
        row = cursor.fetchone()
        if row:
            return row[0]
    except Exception as e:
        logger.warning(f"Error getting uncategorized category: {e}")
    return None


def auto_confirm_entry(cursor, conn, user_id, entry, new_category_id, is_hydrated):
    """Auto-confirm a single entry"""
    table_name = entry['table_name']
    entry_id = entry['entry_id']
    entry_date = entry['date']
    entry_amount = entry['amount']
    recurring_table = entry['recurring_table']
    bucket_table = entry['bucket_table']
    
    # Update in MySQL
    cursor.execute(f"""
        UPDATE {table_name}
        SET category_id = %s, pending = 0, auto_confirmed = 1
        WHERE id = %s
    """, (new_category_id, entry_id))
    conn.commit()
    
    # If user is hydrated, also update Redis
    if is_hydrated:
        entries = get_redis_entries(table_name, user_id)
        if entries:
            for e in entries:
                if e.get('id') == entry_id:
                    e['category_id'] = new_category_id
                    e['pending'] = 0
                    e['auto_confirmed'] = 1
                    break
            save_redis_entries(table_name, user_id, entries)
    
    # Check if new category is recurring and reduce bucket
    recurring_info = get_recurring_info(cursor, recurring_table, user_id, new_category_id)
    if recurring_info:
        wage_bill = recurring_info.get('wage_bill', 0)
        reduce_bucket_for_entry(
            cursor, conn, bucket_table, user_id, new_category_id,
            entry_date, entry_amount,
            wage_bill=wage_bill,
            is_hydrated=is_hydrated,
            entry_table=table_name
        )
    
    return True


def process_user(cursor, conn, user_id):
    """Process all pending entries for a user"""
    is_hydrated = is_user_hydrated(user_id)
    pending_entries = get_pending_entries_for_user(cursor, user_id)
    
    if not pending_entries:
        return 0, 0
    
    confirmed_count = 0
    fallback_count = 0
    
    for entry in pending_entries:
        entry_id = entry['entry_id']
        entry_type = entry['entry_type']
        current_category = entry['category_id']
        
        # Get Ntropy suggestion
        suggestion = get_ntropy_suggestion(cursor, user_id, entry_id, entry_type)
        
        if suggestion and suggestion['category_id']:
            new_category_id = suggestion['category_id']
            logger.info(f"Auto-confirming entry {entry_id} ({entry_type}) to suggested category {new_category_id} ({suggestion['category_name']})")
        else:
            # Fall back to Uncategorized (keep current or get Uncategorized ID)
            uncategorized_id = get_uncategorized_category_id(cursor, user_id, entry_type)
            if uncategorized_id:
                new_category_id = uncategorized_id
                logger.info(f"Auto-confirming entry {entry_id} ({entry_type}) to Uncategorized (no suggestion)")
                fallback_count += 1
            else:
                # No Uncategorized category, skip this entry
                logger.warning(f"Skipping entry {entry_id} - no Uncategorized category found")
                continue
        
        try:
            auto_confirm_entry(cursor, conn, user_id, entry, new_category_id, is_hydrated)
            confirmed_count += 1
        except Exception as e:
            logger.error(f"Error auto-confirming entry {entry_id}: {e}")
    
    return confirmed_count, fallback_count


def get_users_with_pending_entries(cursor):
    """Get list of user IDs that have pending entries"""
    user_ids = set()
    
    # Check each entry table for pending entries
    # income_entries and expense_entries: join through categories table to get user_id
    # c_expense_entries: join through c_expense_categories -> credit_accounts
    
    try:
        cursor.execute("""
            SELECT DISTINCT c.user_id
            FROM income_entries e
            JOIN income_categories c ON e.category_id = c.id
            WHERE e.pending = 1
        """)
        for row in cursor.fetchall():
            user_ids.add(row[0])
    except Exception as e:
        logger.error(f"Error getting users from income_entries: {e}")
    
    try:
        cursor.execute("""
            SELECT DISTINCT c.user_id
            FROM expense_entries e
            JOIN expense_categories c ON e.category_id = c.id
            WHERE e.pending = 1
        """)
        for row in cursor.fetchall():
            user_ids.add(row[0])
    except Exception as e:
        logger.error(f"Error getting users from expense_entries: {e}")
    
    try:
        cursor.execute("""
            SELECT DISTINCT ca.user_id
            FROM c_expense_entries e
            JOIN c_expense_categories cec ON e.category_id = cec.id
            JOIN credit_accounts ca ON cec.account_id = ca.id
            WHERE e.pending = 1
        """)
        for row in cursor.fetchall():
            user_ids.add(row[0])
    except Exception as e:
        logger.error(f"Error getting users from c_expense_entries: {e}")
    
    return list(user_ids)


def main():
    """Main entry point"""
    logger.info("=" * 60)
    logger.info("Starting Auto-Confirm Pending Transactions")
    logger.info("=" * 60)
    
    start_time = datetime.now()
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get users with pending entries
        user_ids = get_users_with_pending_entries(cursor)
        logger.info(f"Found {len(user_ids)} users with pending entries")
        
        total_confirmed = 0
        total_fallback = 0
        
        for user_id in user_ids:
            logger.info(f"Processing user {user_id}...")
            confirmed, fallback = process_user(cursor, conn, user_id)
            total_confirmed += confirmed
            total_fallback += fallback
            logger.info(f"User {user_id}: {confirmed} entries confirmed ({fallback} fallback to Uncategorized)")
        
        cursor.close()
        conn.close()
        
        elapsed = datetime.now() - start_time
        logger.info("-" * 60)
        logger.info(f"Auto-confirm complete!")
        logger.info(f"Total entries confirmed: {total_confirmed}")
        logger.info(f"Fallback to Uncategorized: {total_fallback}")
        logger.info(f"Time elapsed: {elapsed}")
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error(f"Fatal error in auto-confirm: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
