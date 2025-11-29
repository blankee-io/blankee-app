"""
Redis Data Hydration/Dehydration Manager

This module manages automatic Redis caching for user data with the following features:
1. Hydration: Load MySQL data into Redis on user activity
2. Dehydration: Remove user data from Redis after 5 minutes of inactivity
3. Flush: Persist Redis changes back to MySQL every 2 minutes
4. Frontend Refresh: Signal frontend to reload when hydration completes

Architecture:
- Per-user Redis keys following pattern: <table>:v1:<user_id>
- Background threads for automatic dehydration and flush operations
- Efficient bulk queries to minimize database load
- Connection pooling for optimal performance
"""

import logging
import sys
import json
import time
import threading
from datetime import datetime, timedelta, date
from decimal import Decimal
from typing import Dict, List, Optional, Any, Set
from collections import defaultdict
from db_connections import get_db_pool

# Configure logger to output to stderr (which Apache captures)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Add handler if not already present
if not logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# Global state for tracking user activity
_user_activity_lock = threading.Lock()
_user_last_activity: Dict[int, float] = {}  # user_id -> timestamp
_hydrated_users: Set[int] = set()  # users currently hydrated in Redis
_hydrating_users: Set[int] = set()  # users currently being hydrated (in progress)
_redis_client = None
_flush_thread = None
_dehydration_thread = None
_shutdown_event = threading.Event()

# Configuration
INACTIVITY_TIMEOUT = 300  # 5 minutes in seconds
FLUSH_INTERVAL = 15  # 15 seconds (balanced flush interval)
REDIS_KEY_VERSION = "v1"

# Tables to hydrate for each user
USER_TABLES = [
    'income_categories',
    'income_category_groups',
    'expense_categories',
    'expense_category_groups',
    'income_entries',
    'expense_entries',
    'recurring_income',
    'recurring_expense',
    'recurring_c_expense',
    'starting_balance',
    'totals_remainders',
    'totals_remainders_d',
    'totals_remainders_m',
    'savings_entries',
    'credit_accounts',
    'c_expense_categories',
    'c_expense_entries',
    'c_payment_entries',
    'c_a_balances',
    'c_a_balances_d',
    'c_a_balances_m',
    'buds',
    'bud_items',
]


class DecimalEncoder(json.JSONEncoder):
    """Custom JSON encoder to handle Decimal and date/datetime types from MySQL"""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, date):
            return obj.isoformat()
        return super(DecimalEncoder, self).default(obj)


def init_redis_manager(redis_client):
    """
    Initialize the Redis manager with a Redis client and start background threads.
    
    Args:
        redis_client: Redis client instance
    """
    global _redis_client, _flush_thread, _dehydration_thread
    
    _redis_client = redis_client
    
    # Start background threads if not already running
    if _flush_thread is None or not _flush_thread.is_alive():
        _flush_thread = threading.Thread(target=_flush_worker, daemon=True, name="RedisFlushWorker")
        _flush_thread.start()
        logger.info("Redis flush worker thread started")
    
    if _dehydration_thread is None or not _dehydration_thread.is_alive():
        _dehydration_thread = threading.Thread(target=_dehydration_worker, daemon=True, name="RedisDehydrationWorker")
        _dehydration_thread.start()
        logger.info("Redis dehydration worker thread started")


def shutdown_redis_manager():
    """Gracefully shutdown background threads"""
    global _shutdown_event
    logger.info("Shutting down Redis manager...")
    _shutdown_event.set()
    
    if _flush_thread and _flush_thread.is_alive():
        _flush_thread.join(timeout=5)
    if _dehydration_thread and _dehydration_thread.is_alive():
        _dehydration_thread.join(timeout=5)
    
    logger.info("Redis manager shutdown complete")


def track_user_activity(user_id: int):
    """
    Track user activity and trigger hydration if needed.
    Call this function on every user request/interaction.
    
    Args:
        user_id: The ID of the active user
    """
    global _user_last_activity, _hydrated_users
    
    if not _redis_client:
        logger.warning("Redis client not initialized")
        return
    
    current_time = time.time()
    
    with _user_activity_lock:
        _user_last_activity[user_id] = current_time
        
        # Check if user needs hydration and isn't already being hydrated
        if user_id not in _hydrated_users and user_id not in _hydrating_users:
            # Mark as hydrating to prevent duplicate threads
            _hydrating_users.add(user_id)
            logger.info(f"User {user_id} needs hydration, triggering background hydration")
            # Run hydration in background thread to avoid blocking request
            threading.Thread(
                target=_hydrate_user_data,
                args=(user_id,),
                daemon=True,
                name=f"Hydrate-{user_id}"
            ).start()
        elif user_id in _hydrating_users:
            logger.debug(f"User {user_id} hydration already in progress")
        elif user_id in _hydrated_users:
            # User is already hydrated - refresh TTLs to prevent expiration during active use
            # Do this in background to avoid blocking the request
            threading.Thread(
                target=_refresh_user_ttls,
                args=(user_id,),
                daemon=True,
                name=f"RefreshTTL-{user_id}"
            ).start()


def is_user_hydrated(user_id: int) -> bool:
    """
    Check if user's data is currently hydrated in Redis.
    
    Args:
        user_id: The ID of the user
        
    Returns:
        True if user data is in Redis, False otherwise
    """
    with _user_activity_lock:
        return user_id in _hydrated_users


def _get_redis_key(table: str, user_id: int) -> str:
    """Generate Redis key for a table and user"""
    return f"{table}:{REDIS_KEY_VERSION}:{user_id}"


def _hydrate_user_data(user_id: int):
    """
    Hydrate a user's data from MySQL into Redis.
    This runs in a background thread.
    
    Args:
        user_id: The ID of the user to hydrate
    """
    global _hydrated_users, _hydrating_users
    
    start_time = time.time()
    logger.info(f"[HYDRATION] Starting hydration for user {user_id}")
    
    try:
        # Double-check if already hydrated (race condition check)
        with _user_activity_lock:
            if user_id in _hydrated_users:
                logger.debug(f"User {user_id} already hydrated, skipping")
                _hydrating_users.discard(user_id)
                return
        
        total_rows = 0
        tables_hydrated = 0
        
        # Load user's basic info
        _hydrate_user_profile(user_id)
        tables_hydrated += 1
        
        # Load all user tables
        for table in USER_TABLES:
            rows_count = _hydrate_table(table, user_id)
            total_rows += rows_count
            tables_hydrated += 1
        
        # Load bud_items (special case - uses bud_id not user_id)
        bud_items_count = _hydrate_bud_items(user_id)
        total_rows += bud_items_count
        
        # Mark user as hydrated and remove from hydrating set
        with _user_activity_lock:
            _hydrated_users.add(user_id)
            _hydrating_users.discard(user_id)
        
        elapsed = time.time() - start_time
        logger.info(f"[HYDRATION] ✓ User {user_id} hydrated: {total_rows} total rows across {tables_hydrated} tables in {elapsed:.2f}s")
        
        # Send frontend refresh signal
        _signal_frontend_refresh(user_id)
        
    except Exception as e:
        # Remove from hydrating set on error
        with _user_activity_lock:
            _hydrating_users.discard(user_id)
        logger.error(f"[HYDRATION] ✗ Error hydrating user {user_id}: {e}", exc_info=True)


def _hydrate_user_profile(user_id: int):
    """Hydrate user profile data"""
    try:
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT * FROM users WHERE id = %s",
                (user_id,)
            )
            user_data = cursor.fetchone()
            
            if user_data:
                redis_key = _get_redis_key('users', user_id)
                _redis_client.setex(
                    redis_key,
                    INACTIVITY_TIMEOUT + 60,  # Slightly longer TTL
                    json.dumps(user_data, cls=DecimalEncoder)
                )
                logger.debug(f"Hydrated user profile for user {user_id}")
    except Exception as e:
        logger.error(f"Error hydrating user profile for {user_id}: {e}")


def _hydrate_table(table: str, user_id: int):
    """
    Hydrate a specific table's data for a user.
    
    Args:
        table: Table name
        user_id: User ID
    """
    try:
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            # Build query based on table structure
            # Different tables have different relationships to users
            
            if table == 'income_entries':
                # income_entries -> income_categories -> users
                query = """
                    SELECT ie.* FROM income_entries ie
                    INNER JOIN income_categories ic ON ie.category_id = ic.id
                    WHERE ic.user_id = %s
                """
            elif table == 'expense_entries':
                # expense_entries -> expense_categories -> users
                query = """
                    SELECT ee.* FROM expense_entries ee
                    INNER JOIN expense_categories ec ON ee.category_id = ec.id
                    WHERE ec.user_id = %s
                """
            elif table == 'c_expense_entries':
                # c_expense_entries -> c_expense_categories -> credit_accounts -> users
                query = """
                    SELECT ce.* FROM c_expense_entries ce
                    INNER JOIN c_expense_categories cec ON ce.category_id = cec.id
                    INNER JOIN credit_accounts ca ON cec.account_id = ca.id
                    WHERE ca.user_id = %s
                """
            elif table in ['c_a_balances', 'c_a_balances_d', 'c_a_balances_m', 'c_payment_entries']:
                # These tables join directly to credit_accounts
                query = f"""
                    SELECT t.* FROM {table} t
                    INNER JOIN credit_accounts ca ON t.account_id = ca.id
                    WHERE ca.user_id = %s
                """
            elif table in ['c_expense_categories']:
                # c_expense_categories -> credit_accounts -> users
                query = f"""
                    SELECT cec.* FROM {table} cec
                    INNER JOIN credit_accounts ca ON cec.account_id = ca.id
                    WHERE ca.user_id = %s
                """
            else:
                # Default: table has direct user_id column
                query = f"SELECT * FROM {table} WHERE user_id = %s"
            
            cursor.execute(query, (user_id,))
            rows = cursor.fetchall()
            
            if rows:
                redis_key = _get_redis_key(table, user_id)
                # Store as JSON array
                _redis_client.setex(
                    redis_key,
                    INACTIVITY_TIMEOUT + 60,
                    json.dumps(rows, cls=DecimalEncoder)
                )
                logger.debug(f"Hydrated {len(rows)} rows from {table} for user {user_id}")
                return len(rows)
            else:
                # Store empty array to indicate table was checked
                redis_key = _get_redis_key(table, user_id)
                _redis_client.setex(redis_key, INACTIVITY_TIMEOUT + 60, json.dumps([]))
                return 0
                
    except Exception as e:
        logger.error(f"Error hydrating {table} for user {user_id}: {e}")
        return 0


def _hydrate_bud_items(user_id: int):
    """
    Hydrate bud_items for all of a user's buds.
    Stores all bud_items for a user in a single Redis key.
    
    Args:
        user_id: User ID
    
    Returns:
        Total count of bud items hydrated
    """
    try:
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            # Get all bud_items for all buds belonging to this user
            cursor.execute("""
                SELECT bi.* FROM bud_items bi
                INNER JOIN buds b ON bi.bud_id = b.id
                WHERE b.user_id = %s
            """, (user_id,))
            items = cursor.fetchall()
            
            # Store all bud_items for this user in one key
            redis_key = f"bud_items:{REDIS_KEY_VERSION}:{user_id}"
            _redis_client.setex(
                redis_key,
                INACTIVITY_TIMEOUT + 60,
                json.dumps(items, cls=DecimalEncoder)
            )
            logger.debug(f"Hydrated {len(items)} bud_items for user {user_id}")
            return len(items)
                
    except Exception as e:
        logger.error(f"Error hydrating bud_items for user {user_id}: {e}")
        return 0


def _refresh_user_ttls(user_id: int):
    """
    Refresh TTLs for all of a user's Redis keys to prevent expiration during active use.
    This is called periodically when an active user makes requests.
    Uses a throttle to avoid excessive Redis operations.
    
    Args:
        user_id: User ID
    """
    try:
        # Throttle: Only refresh if the last refresh was more than 2 minutes ago
        throttle_key = f"ttl_refresh:{user_id}"
        if _redis_client.exists(throttle_key):
            # Already refreshed recently, skip
            return
        
        # Set throttle flag (2 minute TTL)
        _redis_client.setex(throttle_key, 120, '1')
        
        logger.debug(f"[TTL REFRESH] Refreshing TTLs for user {user_id}")
        
        # Refresh TTL for user profile
        user_key = _get_redis_key('users', user_id)
        if _redis_client.exists(user_key):
            _redis_client.expire(user_key, INACTIVITY_TIMEOUT + 60)
        
        # Refresh TTL for all user tables
        refreshed_count = 0
        for table in USER_TABLES:
            key = _get_redis_key(table, user_id)
            if _redis_client.exists(key):
                _redis_client.expire(key, INACTIVITY_TIMEOUT + 60)
                refreshed_count += 1
        
        # Refresh TTL for bud_items (stored by user_id)
        bud_items_key = f"bud_items:{REDIS_KEY_VERSION}:{user_id}"
        if _redis_client.exists(bud_items_key):
            _redis_client.expire(bud_items_key, INACTIVITY_TIMEOUT + 60)
            refreshed_count += 1
        
        logger.debug(f"[TTL REFRESH] ✓ Refreshed {refreshed_count} keys for user {user_id}")
        
    except Exception as e:
        logger.error(f"[TTL REFRESH] Error refreshing TTLs for user {user_id}: {e}")


def _dehydrate_user_data(user_id: int):
    """
    Remove a user's data from Redis (dehydration).
    Flushes dirty data to MySQL before removing keys.
    
    Args:
        user_id: User ID to dehydrate
    """
    global _hydrated_users
    
    start_time = time.time()
    logger.info(f"[DEHYDRATION] Starting dehydration for user {user_id}")
    
    try:
        # Flush dirty data to MySQL before dehydration
        dirty_tables_key = f"dirty_tables:{user_id}"
        dirty_tables = _redis_client.smembers(dirty_tables_key)
        
        if dirty_tables:
            logger.info(f"[DEHYDRATION] Flushing {len(dirty_tables)} dirty tables for user {user_id} before dehydration")
            
            tables_to_flush = [
                'totals_remainders',
                'totals_remainders_d', 
                'totals_remainders_m',
                'savings_entries',
                'c_a_balances',
                'c_a_balances_d',
                'c_a_balances_m',
                'income_entries',
                'expense_entries',
                'c_expense_entries',
                'buds',  # Must flush before bud_items to resolve temp IDs
                'bud_items'
            ]
            
            flushed_count = 0
            for table in tables_to_flush:
                if table in dirty_tables:
                    count = _flush_table_to_mysql(table, user_id)
                    if count > 0:
                        flushed_count += count
                        # Remove from dirty set after successful flush
                        _redis_client.srem(dirty_tables_key, table)
            
            if flushed_count > 0:
                logger.info(f"[DEHYDRATION] Flushed {flushed_count} rows to MySQL for user {user_id}")
        
        # Get all keys for this user
        keys_to_delete = []
        
        # User profile
        keys_to_delete.append(_get_redis_key('users', user_id))
        
        # All user tables
        for table in USER_TABLES:
            keys_to_delete.append(_get_redis_key(table, user_id))
        
        # Bud items (stored by user_id)
        bud_items_key = f"bud_items:{REDIS_KEY_VERSION}:{user_id}"
        keys_to_delete.append(bud_items_key)
        
        # Delete all keys
        if keys_to_delete:
            _redis_client.delete(*keys_to_delete)
            # Also clean up dirty_tables and pending_deletes keys
            _redis_client.delete(dirty_tables_key)
            for table in ['income_entries', 'expense_entries', 'c_expense_entries']:
                pending_key = f"pending_deletes:{table}:{user_id}"
                _redis_client.delete(pending_key)
            
            elapsed = time.time() - start_time
            logger.info(f"[DEHYDRATION] ✓ User {user_id} dehydrated: {len(keys_to_delete)} Redis keys deleted in {elapsed:.2f}s")
        else:
            logger.info(f"[DEHYDRATION] User {user_id} had no keys to delete")
        
        # Remove from hydrated set
        with _user_activity_lock:
            _hydrated_users.discard(user_id)
        
    except Exception as e:
        logger.error(f"[DEHYDRATION] ✗ Error dehydrating user {user_id}: {e}", exc_info=True)


def _dehydration_worker():
    """
    Background worker that checks for inactive users and dehydrates them.
    Runs continuously until shutdown.
    """
    logger.info("Dehydration worker started")
    
    while not _shutdown_event.is_set():
        try:
            current_time = time.time()
            users_to_dehydrate = []
            
            # Find inactive users
            with _user_activity_lock:
                for user_id, last_activity in list(_user_last_activity.items()):
                    if current_time - last_activity > INACTIVITY_TIMEOUT:
                        if user_id in _hydrated_users:
                            users_to_dehydrate.append(user_id)
                        # Clean up tracking
                        del _user_last_activity[user_id]
            
            # Dehydrate inactive users
            for user_id in users_to_dehydrate:
                logger.info(f"User {user_id} inactive for {INACTIVITY_TIMEOUT}s, dehydrating")
                _dehydrate_user_data(user_id)
            
            # Sleep for 30 seconds before next check
            _shutdown_event.wait(30)
            
        except Exception as e:
            logger.error(f"Error in dehydration worker: {e}", exc_info=True)
            _shutdown_event.wait(30)
    
    logger.info("Dehydration worker stopped")


def _flush_redis_to_mysql():
    """
    Flush dirty Redis data back to MySQL.
    
    Flushes totals/remainders/balances tables from Redis to MySQL.
    """
    start_time = time.time()
    
    try:
        # Get all currently hydrated users
        with _user_activity_lock:
            users_to_flush = list(_hydrated_users)
        
        if not users_to_flush:
            logger.debug("[FLUSH] No hydrated users to flush")
            return
        
        logger.info(f"[FLUSH] Starting flush for {len(users_to_flush)} hydrated user(s)")
        
        total_flushed = 0
        tables_to_flush = [
            'totals_remainders',
            'totals_remainders_d', 
            'totals_remainders_m',
            'savings_entries',
            'c_a_balances',
            'c_a_balances_d',
            'c_a_balances_m',
            'income_entries',
            'expense_entries',
            'c_expense_entries',
            'buds',  # Must flush before bud_items to resolve temp IDs
            'bud_items',
            'users'  # User settings (balance_threshold, starting_savings)
        ]
        
        for user_id in users_to_flush:
            user_flushed = 0
            table_stats = {}
            
            # Get dirty tables for this user
            dirty_tables_key = f"dirty_tables:{user_id}"
            dirty_tables = _redis_client.smembers(dirty_tables_key)
            
            if not dirty_tables:
                logger.debug(f"[FLUSH] No dirty tables for user {user_id}")
                continue
            
            logger.debug(f"[FLUSH] User {user_id} has {len(dirty_tables)} dirty tables: {', '.join(dirty_tables)}")
            
            # Flush only dirty tables
            for table in tables_to_flush:
                # Skip if table is not dirty
                if table not in dirty_tables:
                    continue
                    
                logger.debug(f"[FLUSH] Attempting to flush {table} for user {user_id}")
                flushed_count = _flush_table_to_mysql(table, user_id)
                if flushed_count > 0:
                    table_stats[table] = flushed_count
                    # Remove from dirty set after successful flush
                    _redis_client.srem(dirty_tables_key, table)
                user_flushed += flushed_count
            
            total_flushed += user_flushed
            if user_flushed > 0:
                stats_str = ", ".join([f"{table}: {count}" for table, count in table_stats.items()])
                logger.info(f"[FLUSH] User {user_id}: {user_flushed} rows ({stats_str})")
        
        elapsed = time.time() - start_time
        if total_flushed > 0:
            logger.info(f"[FLUSH] ✓ Flush complete: {len(users_to_flush)} user(s), {total_flushed} rows written to MySQL in {elapsed:.2f}s")
        else:
            logger.debug(f"[FLUSH] No dirty data to flush for {len(users_to_flush)} user(s)")
        
    except Exception as e:
        logger.error(f"[FLUSH] ✗ Error in Redis to MySQL flush: {e}", exc_info=True)


def _flush_table_to_mysql(table: str, user_id: int):
    """
    Flush a specific table's Redis data back to MySQL.
    
    Args:
        table: Table name
        user_id: User ID
        
    Returns:
        Count of rows flushed
    """
    try:
        redis_key = _get_redis_key(table, user_id)
        redis_data = _redis_client.get(redis_key)
        
        if not redis_data:
            logger.debug(f"[FLUSH] No Redis data found for key: {redis_key}")
            return 0
        
        rows = json.loads(redis_data)
        
        # Don't return early when rows is an empty list - we still need to
        # process pending deletions (pending_deletes) for tables like
        # income_entries/expense_entries/c_expense_entries. The per-table
        # handlers below will correctly handle empty `rows` when performing
        # upserts. Keep a debug log for visibility.
        logger.debug(f"[FLUSH] Found {len(rows)} rows in Redis for {table}")
        
        # Build UPSERT query based on table type
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor()
            
            if table in ['totals_remainders', 'totals_remainders_d', 'totals_remainders_m']:
                # Totals/remainders tables
                last_field = {
                    'totals_remainders': 'last_week_remainder',
                    'totals_remainders_d': 'last_day_remainder',
                    'totals_remainders_m': 'last_month_remainder'
                }[table]
                
                # Prepare batch data
                batch_data = []
                for row in rows:
                    batch_data.append((
                        user_id,
                        row.get('date'),
                        float(row.get('total_income', 0)),
                        float(row.get('total_expenses', 0)),
                        float(row.get('remainder', 0)),
                        float(row.get(last_field, 0))
                    ))
                
                # Execute batch upsert
                cursor.executemany(f"""
                    INSERT INTO {table} (user_id, date, total_income, total_expenses, remainder, {last_field})
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        total_income = VALUES(total_income),
                        total_expenses = VALUES(total_expenses),
                        remainder = VALUES(remainder),
                        {last_field} = VALUES({last_field})
                """, batch_data)
                
                conn.commit()
                cursor.close()
                logger.debug(f"[FLUSH] → {table}: {len(batch_data)} rows")
                return len(batch_data)
                
            elif table == 'savings_entries':
                # Savings entries table
                batch_data = []
                for row in rows:
                    batch_data.append((
                        user_id,
                        row.get('date'),
                        float(row.get('amount', 0))
                    ))
                
                cursor.executemany("""
                    INSERT INTO savings_entries (user_id, date, amount)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE amount = VALUES(amount)
                """, batch_data)
                
                conn.commit()
                cursor.close()
                logger.debug(f"[FLUSH] → savings_entries: {len(batch_data)} rows")
                return len(batch_data)
                
            elif table in ['c_a_balances', 'c_a_balances_d', 'c_a_balances_m']:
                # Credit account balance tables
                batch_data = []
                for row in rows:
                    batch_data.append((
                        row.get('account_id'),
                        row.get('date'),
                        float(row.get('total_expenses', 0)),
                        float(row.get('total_payments', 0)) if 'total_payments' in row else 0.0,
                        float(row.get('balance', 0))
                    ))
                
                cursor.executemany(f"""
                    INSERT INTO {table} (account_id, date, total_expenses, total_payments, balance)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        total_expenses = VALUES(total_expenses),
                        total_payments = VALUES(total_payments),
                        balance = VALUES(balance)
                """, batch_data)
                
                conn.commit()
                cursor.close()
                logger.debug(f"[FLUSH] → {table}: {len(batch_data)} rows")
                return len(batch_data)
                
            elif table == 'income_entries':
                # Income entries table
                
                # First, delete any entries marked for deletion
                pending_key = f"pending_deletes:income_entries:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    delete_ids = [int(id_str) for id_str in pending_deletes]
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    cursor.execute(f"""
                        DELETE FROM income_entries WHERE id IN ({placeholders})
                    """, delete_ids)
                    logger.info(f"[FLUSH] Deleted {len(delete_ids)} income_entries from MySQL")
                
                # Now UPSERT the current state from Redis
                batch_data = []
                for row in rows:
                    batch_data.append((
                        row.get('id'),
                        row.get('category_id'),
                        row.get('date'),
                        float(row.get('amount', 0)),
                        row.get('recurring_id'),
                        int(row.get('processed', 0))
                    ))
                
                if batch_data:
                    cursor.executemany("""
                        INSERT INTO income_entries (id, category_id, date, amount, recurring_id, processed)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            amount = VALUES(amount),
                            processed = VALUES(processed),
                            recurring_id = VALUES(recurring_id)
                    """, batch_data)
                
                conn.commit()
                cursor.close()
                
                # Clear pending deletions set after successful flush
                _redis_client.delete(pending_key)
                
                logger.debug(f"[FLUSH] → income_entries: {len(batch_data)} rows")
                return len(batch_data)
                
            elif table == 'expense_entries':
                # Expense entries table
                
                # First, delete any entries marked for deletion
                pending_key = f"pending_deletes:expense_entries:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    delete_ids = [int(id_str) for id_str in pending_deletes]
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    cursor.execute(f"""
                        DELETE FROM expense_entries WHERE id IN ({placeholders})
                    """, delete_ids)
                    logger.info(f"[FLUSH] Deleted {len(delete_ids)} expense_entries from MySQL")
                
                # Now UPSERT the current state from Redis
                batch_data = []
                for row in rows:
                    batch_data.append((
                        row.get('id'),
                        row.get('category_id'),
                        row.get('date'),
                        float(row.get('amount', 0)),
                        row.get('recurring_id'),
                        int(row.get('processed', 0)),
                        row.get('bud_item_id')
                    ))
                
                if batch_data:
                    cursor.executemany("""
                        INSERT INTO expense_entries (id, category_id, date, amount, recurring_id, processed, bud_item_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            amount = VALUES(amount),
                            processed = VALUES(processed),
                            recurring_id = VALUES(recurring_id),
                            bud_item_id = VALUES(bud_item_id)
                    """, batch_data)
                
                conn.commit()
                cursor.close()
                
                # Clear pending deletions set after successful flush
                _redis_client.delete(pending_key)
                
                logger.debug(f"[FLUSH] → expense_entries: {len(batch_data)} rows")
                return len(batch_data)
                
            elif table == 'c_expense_entries':
                # Credit account expense entries table
                
                # First, delete any entries marked for deletion
                pending_key = f"pending_deletes:c_expense_entries:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    delete_ids = [int(id_str) for id_str in pending_deletes]
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    cursor.execute(f"""
                        DELETE FROM c_expense_entries WHERE id IN ({placeholders})
                    """, delete_ids)
                    logger.info(f"[FLUSH] Deleted {len(delete_ids)} c_expense_entries from MySQL")
                
                # Now UPSERT the current state from Redis
                batch_data = []
                for row in rows:
                    batch_data.append((
                        row.get('id'),
                        row.get('category_id'),
                        row.get('date'),
                        float(row.get('amount', 0)),
                        row.get('recurring_id'),
                        int(row.get('processed', 0)),
                        row.get('bud_item_id')
                    ))
                
                if batch_data:
                    cursor.executemany("""
                        INSERT INTO c_expense_entries (id, category_id, date, amount, recurring_id, processed, bud_item_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            amount = VALUES(amount),
                            processed = VALUES(processed),
                            recurring_id = VALUES(recurring_id),
                            bud_item_id = VALUES(bud_item_id)
                    """, batch_data)
                
                conn.commit()
                cursor.close()
                
                # Clear pending deletions set after successful flush
                _redis_client.delete(pending_key)
                
                logger.debug(f"[FLUSH] → c_expense_entries: {len(batch_data)} rows")
                return len(batch_data)
                
            elif table == 'c_payment_entries':
                # Credit account payment entries table
                
                # First, delete any entries marked for deletion
                pending_key = f"pending_deletes:c_payment_entries:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    delete_ids = [int(id_str) for id_str in pending_deletes]
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    cursor.execute(f"""
                        DELETE FROM c_payment_entries WHERE id IN ({placeholders})
                    """, delete_ids)
                    logger.info(f"[FLUSH] Deleted {len(delete_ids)} c_payment_entries from MySQL")
                
                # Now UPSERT the current state from Redis
                batch_data = []
                for row in rows:
                    batch_data.append((
                        row.get('id'),
                        row.get('account_id'),
                        row.get('date'),
                        float(row.get('amount', 0)),
                        row.get('recurring_id'),
                        int(row.get('processed', 0))
                    ))
                
                if batch_data:
                    cursor.executemany("""
                        INSERT INTO c_payment_entries (id, account_id, date, amount, recurring_id, processed)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            amount = VALUES(amount),
                            processed = VALUES(processed),
                            recurring_id = VALUES(recurring_id)
                    """, batch_data)
                
                conn.commit()
                cursor.close()
                
                # Clear pending deletions set after successful flush
                _redis_client.delete(pending_key)
                
                logger.debug(f"[FLUSH] → c_payment_entries: {len(batch_data)} rows")
                return len(batch_data)
                
            elif table == 'recurring_income':
                # Recurring income table
                
                # First, delete any records marked for deletion
                pending_key = f"pending_deletes:recurring_income:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    delete_ids = [int(id_str) for id_str in pending_deletes]
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    cursor.execute(f"""
                        DELETE FROM recurring_income WHERE id IN ({placeholders}) AND user_id = %s
                    """, delete_ids + [user_id])
                    logger.info(f"[FLUSH] Deleted {len(delete_ids)} recurring_income from MySQL")
                
                # Now UPSERT the current state from Redis
                batch_data = []
                for row in rows:
                    # Skip temporary negative IDs - they'll be handled as INSERTs
                    if row.get('id', 0) < 0:
                        batch_data.append((
                            None,  # Let MySQL auto-generate
                            user_id,
                            row.get('category_id'),
                            float(row.get('amount', 0)),
                            row.get('cadence_interval', 1),
                            row.get('cadence_unit', 'days'),
                            row.get('weekdays'),
                            row.get('monthly_days'),
                            row.get('yearly_day'),
                            row.get('yearly_month'),
                            row.get('start_date'),
                            row.get('end_date')
                        ))
                    else:
                        batch_data.append((
                            row.get('id'),
                            user_id,
                            row.get('category_id'),
                            float(row.get('amount', 0)),
                            row.get('cadence_interval', 1),
                            row.get('cadence_unit', 'days'),
                            row.get('weekdays'),
                            row.get('monthly_days'),
                            row.get('yearly_day'),
                            row.get('yearly_month'),
                            row.get('start_date'),
                            row.get('end_date')
                        ))
                
                if batch_data:
                    cursor.executemany("""
                        INSERT INTO recurring_income (id, user_id, category_id, amount, cadence_interval, cadence_unit, 
                                                     weekdays, monthly_days, yearly_day, yearly_month, start_date, end_date)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            category_id = VALUES(category_id),
                            amount = VALUES(amount),
                            cadence_interval = VALUES(cadence_interval),
                            cadence_unit = VALUES(cadence_unit),
                            weekdays = VALUES(weekdays),
                            monthly_days = VALUES(monthly_days),
                            yearly_day = VALUES(yearly_day),
                            yearly_month = VALUES(yearly_month),
                            start_date = VALUES(start_date),
                            end_date = VALUES(end_date)
                    """, batch_data)
                
                conn.commit()
                cursor.close()
                
                # Clear pending deletions set after successful flush
                _redis_client.delete(pending_key)
                
                logger.debug(f"[FLUSH] → recurring_income: {len(batch_data)} rows")
                return len(batch_data)
                
            elif table == 'recurring_expense':
                # Recurring expense table
                
                # First, delete any records marked for deletion
                pending_key = f"pending_deletes:recurring_expense:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    delete_ids = [int(id_str) for id_str in pending_deletes]
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    cursor.execute(f"""
                        DELETE FROM recurring_expense WHERE id IN ({placeholders}) AND user_id = %s
                    """, delete_ids + [user_id])
                    logger.info(f"[FLUSH] Deleted {len(delete_ids)} recurring_expense from MySQL")
                
                # Now UPSERT the current state from Redis
                batch_data = []
                for row in rows:
                    # Skip temporary negative IDs - they'll be handled as INSERTs
                    if row.get('id', 0) < 0:
                        batch_data.append((
                            None,  # Let MySQL auto-generate
                            user_id,
                            row.get('category_id'),
                            float(row.get('amount', 0)),
                            row.get('cadence_interval', 1),
                            row.get('cadence_unit', 'days'),
                            row.get('weekdays'),
                            row.get('monthly_days'),
                            row.get('yearly_day'),
                            row.get('yearly_month'),
                            row.get('start_date'),
                            row.get('end_date')
                        ))
                    else:
                        batch_data.append((
                            row.get('id'),
                            user_id,
                            row.get('category_id'),
                            float(row.get('amount', 0)),
                            row.get('cadence_interval', 1),
                            row.get('cadence_unit', 'days'),
                            row.get('weekdays'),
                            row.get('monthly_days'),
                            row.get('yearly_day'),
                            row.get('yearly_month'),
                            row.get('start_date'),
                            row.get('end_date')
                        ))
                
                if batch_data:
                    cursor.executemany("""
                        INSERT INTO recurring_expense (id, user_id, category_id, amount, cadence_interval, cadence_unit, 
                                                      weekdays, monthly_days, yearly_day, yearly_month, start_date, end_date)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            category_id = VALUES(category_id),
                            amount = VALUES(amount),
                            cadence_interval = VALUES(cadence_interval),
                            cadence_unit = VALUES(cadence_unit),
                            weekdays = VALUES(weekdays),
                            monthly_days = VALUES(monthly_days),
                            yearly_day = VALUES(yearly_day),
                            yearly_month = VALUES(yearly_month),
                            start_date = VALUES(start_date),
                            end_date = VALUES(end_date)
                    """, batch_data)
                
                conn.commit()
                cursor.close()
                
                # Clear pending deletions set after successful flush
                _redis_client.delete(pending_key)
                
                logger.debug(f"[FLUSH] → recurring_expense: {len(batch_data)} rows")
                return len(batch_data)
                
            elif table == 'recurring_c_expense':
                # Recurring credit account expense table
                
                # First, delete any records marked for deletion
                pending_key = f"pending_deletes:recurring_c_expense:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    delete_ids = [int(id_str) for id_str in pending_deletes]
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    cursor.execute(f"""
                        DELETE FROM recurring_c_expense WHERE id IN ({placeholders}) AND user_id = %s
                    """, delete_ids + [user_id])
                    logger.info(f"[FLUSH] Deleted {len(delete_ids)} recurring_c_expense from MySQL")
                
                # Now UPSERT the current state from Redis
                batch_data = []
                for row in rows:
                    # Skip temporary negative IDs - they'll be handled as INSERTs
                    if row.get('id', 0) < 0:
                        batch_data.append((
                            None,  # Let MySQL auto-generate
                            user_id,
                            row.get('category_id'),
                            float(row.get('amount', 0)),
                            row.get('cadence_interval', 1),
                            row.get('cadence_unit', 'days'),
                            row.get('weekdays'),
                            row.get('monthly_days'),
                            row.get('yearly_day'),
                            row.get('yearly_month'),
                            row.get('start_date'),
                            row.get('end_date')
                        ))
                    else:
                        batch_data.append((
                            row.get('id'),
                            user_id,
                            row.get('category_id'),
                            float(row.get('amount', 0)),
                            row.get('cadence_interval', 1),
                            row.get('cadence_unit', 'days'),
                            row.get('weekdays'),
                            row.get('monthly_days'),
                            row.get('yearly_day'),
                            row.get('yearly_month'),
                            row.get('start_date'),
                            row.get('end_date')
                        ))
                
                if batch_data:
                    cursor.executemany("""
                        INSERT INTO recurring_c_expense (id, user_id, category_id, amount, cadence_interval, cadence_unit, 
                                                        weekdays, monthly_days, yearly_day, yearly_month, start_date, end_date)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            category_id = VALUES(category_id),
                            amount = VALUES(amount),
                            cadence_interval = VALUES(cadence_interval),
                            cadence_unit = VALUES(cadence_unit),
                            weekdays = VALUES(weekdays),
                            monthly_days = VALUES(monthly_days),
                            yearly_day = VALUES(yearly_day),
                            yearly_month = VALUES(yearly_month),
                            start_date = VALUES(start_date),
                            end_date = VALUES(end_date)
                    """, batch_data)
                
                conn.commit()
                cursor.close()
                
                # Clear pending deletions set after successful flush
                _redis_client.delete(pending_key)
                
                logger.debug(f"[FLUSH] → recurring_c_expense: {len(batch_data)} rows")
                return len(batch_data)
                
            elif table == 'buds':
                # Buds table
                
                # First, delete any buds marked for deletion
                pending_key = f"pending_deletes:buds:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    delete_ids = [int(id_str) for id_str in pending_deletes]
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    cursor.execute(f"""
                        DELETE FROM buds WHERE id IN ({placeholders}) AND user_id = %s
                    """, delete_ids + [user_id])
                    logger.info(f"[FLUSH] Deleted {len(delete_ids)} buds from MySQL")
                
                # Track temp ID to real ID mappings for updating bud_items
                temp_id_mappings = {}
                
                # Process buds one at a time to get auto-generated IDs for temp IDs
                for row in rows:
                    old_id = row.get('id')
                    is_temp = old_id and int(old_id) < 0
                    
                    if is_temp:
                        # INSERT with NULL id to get auto-generated ID
                        cursor.execute("""
                            INSERT INTO buds (id, user_id, name, expense_category_id, active, created_at)
                            VALUES (NULL, %s, %s, %s, %s, %s)
                        """, (
                            user_id,
                            row.get('name'),
                            row.get('expense_category_id'),
                            int(row.get('active', 0)),
                            row.get('created_at')
                        ))
                        new_id = cursor.lastrowid
                        temp_id_mappings[int(old_id)] = new_id
                        logger.info(f"[FLUSH] Bud temp ID {old_id} → real ID {new_id}")
                    else:
                        # Regular UPSERT for existing IDs
                        cursor.execute("""
                            INSERT INTO buds (id, user_id, name, expense_category_id, active, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE
                                name = VALUES(name),
                                expense_category_id = VALUES(expense_category_id),
                                active = VALUES(active)
                        """, (
                            old_id,
                            user_id,
                            row.get('name'),
                            row.get('expense_category_id'),
                            int(row.get('active', 0)),
                            row.get('created_at')
                        ))
                
                conn.commit()
                cursor.close()
                
                # Update Redis buds cache with new IDs
                if temp_id_mappings:
                    for i, row in enumerate(rows):
                        old_id = row.get('id')
                        if old_id and int(old_id) in temp_id_mappings:
                            rows[i]['id'] = temp_id_mappings[int(old_id)]
                    
                    # Save updated buds back to Redis
                    redis_key = _get_redis_key('buds', user_id)
                    _redis_client.setex(
                        redis_key,
                        INACTIVITY_TIMEOUT + 60,
                        json.dumps(rows, cls=DecimalEncoder)
                    )
                    logger.info(f"[FLUSH] Updated {len(temp_id_mappings)} bud temp IDs in Redis")
                    
                    # Update bud_items in Redis with new bud_ids
                    bud_items_key = _get_redis_key('bud_items', user_id)
                    bud_items_data = _redis_client.get(bud_items_key)
                    if bud_items_data:
                        bud_items = json.loads(bud_items_data)
                        updated_count = 0
                        for item in bud_items:
                            old_bud_id = item.get('bud_id')
                            if old_bud_id and int(old_bud_id) in temp_id_mappings:
                                item['bud_id'] = temp_id_mappings[int(old_bud_id)]
                                updated_count += 1
                        
                        if updated_count > 0:
                            _redis_client.setex(
                                bud_items_key,
                                INACTIVITY_TIMEOUT + 60,
                                json.dumps(bud_items, cls=DecimalEncoder)
                            )
                            logger.info(f"[FLUSH] Updated {updated_count} bud_item bud_id references in Redis")
                
                # Clear pending deletions set after successful flush
                _redis_client.delete(pending_key)
                
                logger.debug(f"[FLUSH] → buds: {len(rows)} rows")
                return len(rows)
                
            elif table == 'bud_items':
                # Bud items table
                
                # First, delete any bud_items marked for deletion
                pending_key = f"pending_deletes:bud_items:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    delete_ids = [int(id_str) for id_str in pending_deletes]
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    cursor.execute(f"""
                        DELETE FROM bud_items WHERE id IN ({placeholders})
                    """, delete_ids)
                    logger.info(f"[FLUSH] Deleted {len(delete_ids)} bud_items from MySQL")
                
                # Track temp ID to real ID mappings for updating Redis
                temp_id_mappings = {}
                
                # Process bud_items to handle temp IDs
                for row in rows:
                    # Skip if bud_id is negative (temp ID) - parent bud hasn't been flushed yet
                    bud_id_val = row.get('bud_id')
                    if bud_id_val and int(bud_id_val) < 0:
                        logger.debug(f"[FLUSH] Skipping bud_item {row.get('id')} - bud_id {bud_id_val} is temp (negative)")
                        continue
                    
                    old_id = row.get('id')
                    is_temp = old_id and int(old_id) < 0
                    
                    if is_temp:
                        # INSERT with NULL id to get auto-generated ID
                        cursor.execute("""
                            INSERT INTO bud_items (id, bud_id, account, name, value, date, description)
                            VALUES (NULL, %s, %s, %s, %s, %s, %s)
                        """, (
                            bud_id_val,
                            row.get('account'),
                            row.get('name'),
                            float(row.get('value', 0)),
                            row.get('date'),
                            row.get('description')
                        ))
                        new_id = cursor.lastrowid
                        temp_id_mappings[int(old_id)] = new_id
                        logger.info(f"[FLUSH] Bud_item temp ID {old_id} → real ID {new_id}")
                    else:
                        # Regular UPSERT for existing IDs
                        cursor.execute("""
                            INSERT INTO bud_items (id, bud_id, account, name, value, date, description)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE
                                bud_id = VALUES(bud_id),
                                account = VALUES(account),
                                name = VALUES(name),
                                value = VALUES(value),
                                date = VALUES(date),
                                description = VALUES(description)
                        """, (
                            old_id,
                            bud_id_val,
                            row.get('account'),
                            row.get('name'),
                            float(row.get('value', 0)),
                            row.get('date'),
                            row.get('description')
                        ))
                
                conn.commit()
                cursor.close()
                
                # Update Redis bud_items cache with new IDs
                if temp_id_mappings:
                    for i, row in enumerate(rows):
                        old_id = row.get('id')
                        if old_id and int(old_id) in temp_id_mappings:
                            rows[i]['id'] = temp_id_mappings[int(old_id)]
                    
                    # Save updated bud_items back to Redis
                    redis_key = _get_redis_key('bud_items', user_id)
                    _redis_client.setex(
                        redis_key,
                        INACTIVITY_TIMEOUT + 60,
                        json.dumps(rows, cls=DecimalEncoder)
                    )
                    logger.info(f"[FLUSH] Updated {len(temp_id_mappings)} bud_item temp IDs in Redis")
                    
                    # Update expense_entries and c_expense_entries with new bud_item_ids
                    for table in ['expense_entries', 'c_expense_entries']:
                        entries_key = _get_redis_key(table, user_id)
                        entries_data = _redis_client.get(entries_key)
                        if entries_data:
                            entries = json.loads(entries_data)
                            updated_count = 0
                            for entry in entries:
                                old_bud_item_id = entry.get('bud_item_id')
                                if old_bud_item_id and int(old_bud_item_id) in temp_id_mappings:
                                    entry['bud_item_id'] = temp_id_mappings[int(old_bud_item_id)]
                                    updated_count += 1
                            
                            if updated_count > 0:
                                _redis_client.setex(
                                    entries_key,
                                    INACTIVITY_TIMEOUT + 60,
                                    json.dumps(entries, cls=DecimalEncoder)
                                )
                                logger.info(f"[FLUSH] Updated {updated_count} bud_item_id references in {table}")
                
                # Clear pending deletions set after successful flush
                _redis_client.delete(pending_key)
                
                logger.debug(f"[FLUSH] → bud_items: {len(rows)} rows")
                return len(rows)
                
            elif table == 'users':
                # Users table - for user settings like balance_threshold, starting_savings
                
                if not rows:
                    return 0
                
                # For users, rows is a dict (not a list), since we store a single user object
                user_data = rows if isinstance(rows, dict) else None
                
                if user_data:
                    cursor.execute("""
                        UPDATE users
                        SET balance_threshold = %s,
                            starting_savings = %s,
                            password = %s,
                            username = %s,
                            mfa_secret = %s,
                            email_notifications = %s
                        WHERE id = %s
                    """, (
                        float(user_data.get('balance_threshold', 0)),
                        float(user_data.get('starting_savings', 0)),
                        user_data.get('password'),
                        user_data.get('username'),
                        user_data.get('mfa_secret'),
                        int(user_data.get('email_notifications', 0)),
                        user_id
                    ))
                    
                    conn.commit()
                    cursor.close()
                    logger.debug(f"[FLUSH] → users: Updated user {user_id} settings")
                    return 1
                
                return 0
                
            else:
                # Table not configured for flushing
                return 0
        
    except Exception as e:
        logger.error(f"[FLUSH] Error flushing {table} to MySQL for user {user_id}: {e}", exc_info=True)
        return 0


def _flush_worker():
    """
    Background worker that periodically flushes Redis data to MySQL.
    Runs continuously until shutdown.
    """
    logger.info("Flush worker started")
    
    while not _shutdown_event.is_set():
        try:
            _flush_redis_to_mysql()
            
            # Sleep for flush interval
            _shutdown_event.wait(FLUSH_INTERVAL)
            
        except Exception as e:
            logger.error(f"Error in flush worker: {e}", exc_info=True)
            _shutdown_event.wait(FLUSH_INTERVAL)
    
    logger.info("Flush worker stopped")


def _signal_frontend_refresh(user_id: int):
    """
    Signal the frontend that data has been hydrated and page should refresh.
    
    This uses a Redis pub/sub pattern. The frontend should subscribe to
    user-specific channels and listen for refresh signals.
    
    Args:
        user_id: User ID to signal
        
    TODO: Integrate with frontend WebSocket or Server-Sent Events
    TODO: Alternative: Use Redis pub/sub with frontend polling
    """
    try:
        channel = f"user:{user_id}:refresh"
        message = json.dumps({
            'type': 'hydration_complete',
            'timestamp': datetime.now().isoformat(),
            'user_id': user_id
        })
        
        _redis_client.publish(channel, message)
        
        # Also set a flag key that frontend can poll
        flag_key = f"user:{user_id}:refresh_needed"
        _redis_client.setex(flag_key, 10, '1')  # 10 second TTL
        
        logger.info(f"Sent refresh signal for user {user_id}")
        
    except Exception as e:
        logger.error(f"Error signaling frontend refresh for user {user_id}: {e}")


# Convenience functions for application use

def get_cached_data(table: str, user_id: int) -> Optional[List[Dict]]:
    """
    Get cached data from Redis for a specific table and user.
    
    Args:
        table: Table name
        user_id: User ID
        
    Returns:
        List of dictionaries (rows) or None if not in cache
    """
    if not _redis_client:
        return None
    
    try:
        redis_key = _get_redis_key(table, user_id)
        data = _redis_client.get(redis_key)
        
        if data:
            return json.loads(data)
        return None
        
    except Exception as e:
        logger.error(f"Error getting cached data for {table}, user {user_id}: {e}")
        return None


def set_cached_data(table: str, user_id: int, data: List[Dict], ttl: int = INACTIVITY_TIMEOUT + 60):
    """
    Set cached data in Redis for a specific table and user.
    
    Args:
        table: Table name
        user_id: User ID
        data: List of dictionaries (rows) to cache
        ttl: Time to live in seconds
    """
    if not _redis_client:
        return
    
    try:
        redis_key = _get_redis_key(table, user_id)
        _redis_client.setex(
            redis_key,
            ttl,
            json.dumps(data, cls=DecimalEncoder)
        )
        logger.debug(f"Set cached data for {table}, user {user_id}: {len(data)} rows")
        
    except Exception as e:
        logger.error(f"Error setting cached data for {table}, user {user_id}: {e}")


def invalidate_user_cache(user_id: int):
    """
    Manually invalidate/dehydrate a user's cache.
    Useful when you want to force a fresh load from MySQL.
    
    Args:
        user_id: User ID
    """
    logger.info(f"Manually invalidating cache for user {user_id}")
    _dehydrate_user_data(user_id)
