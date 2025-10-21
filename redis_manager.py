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
FLUSH_INTERVAL = 120  # 2 minutes in seconds
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
    Special case since bud_items are keyed by bud_id, not user_id.
    
    Args:
        user_id: User ID
    
    Returns:
        Total count of bud items hydrated
    """
    try:
        total_items = 0
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            # Get all bud IDs for this user
            cursor.execute("SELECT id FROM buds WHERE user_id = %s", (user_id,))
            bud_ids = [row['id'] for row in cursor.fetchall()]
            
            # Load items for each bud
            for bud_id in bud_ids:
                cursor.execute("SELECT * FROM bud_items WHERE bud_id = %s", (bud_id,))
                items = cursor.fetchall()
                
                redis_key = f"bud_items:{REDIS_KEY_VERSION}:{bud_id}"
                _redis_client.setex(
                    redis_key,
                    INACTIVITY_TIMEOUT + 60,
                    json.dumps(items, cls=DecimalEncoder)
                )
                logger.debug(f"Hydrated {len(items)} items for bud {bud_id}")
                total_items += len(items)
        
        return total_items
                
    except Exception as e:
        logger.error(f"Error hydrating bud_items for user {user_id}: {e}")
        return 0


def _dehydrate_user_data(user_id: int):
    """
    Remove a user's data from Redis (dehydration).
    
    Args:
        user_id: User ID to dehydrate
    """
    global _hydrated_users
    
    start_time = time.time()
    logger.info(f"[DEHYDRATION] Starting dehydration for user {user_id}")
    
    try:
        # Get all keys for this user
        keys_to_delete = []
        
        # User profile
        keys_to_delete.append(_get_redis_key('users', user_id))
        
        # All user tables
        for table in USER_TABLES:
            keys_to_delete.append(_get_redis_key(table, user_id))
        
        # Bud items (need to get bud IDs first)
        buds_key = _get_redis_key('buds', user_id)
        buds_data = _redis_client.get(buds_key)
        bud_count = 0
        if buds_data:
            buds = json.loads(buds_data)
            bud_count = len(buds)
            for bud in buds:
                bud_items_key = f"bud_items:{REDIS_KEY_VERSION}:{bud['id']}"
                keys_to_delete.append(bud_items_key)
        
        # Delete all keys
        if keys_to_delete:
            _redis_client.delete(*keys_to_delete)
            elapsed = time.time() - start_time
            logger.info(f"[DEHYDRATION] ✓ User {user_id} dehydrated: {len(keys_to_delete)} Redis keys deleted (including {bud_count} bud item sets) in {elapsed:.2f}s")
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
    
    This is a simplified implementation. For production, you should:
    1. Track which Redis keys have been modified (dirty tracking)
    2. Only flush modified data
    3. Handle conflicts (optimistic locking with version numbers)
    
    TODO: Implement dirty tracking mechanism
    TODO: Add conflict resolution strategy
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
        
        total_records = 0
        # TODO: For now, we'll just log what would be flushed
        # In production, implement:
        # 1. Check which keys are dirty (modified since last flush)
        # 2. Parse Redis data
        # 3. Compare with MySQL (check last_modified timestamps)
        # 4. Write back changes using UPSERT/INSERT ON DUPLICATE KEY UPDATE
        
        for user_id in users_to_flush:
            # Count records that would be flushed
            user_record_count = 0
            for table in USER_TABLES:
                redis_key = _get_redis_key(table, user_id)
                redis_data = _redis_client.get(redis_key)
                if redis_data:
                    rows = json.loads(redis_data)
                    user_record_count += len(rows)
            
            total_records += user_record_count
            logger.debug(f"[FLUSH] User {user_id}: {user_record_count} records cached (flush not yet implemented)")
            # TODO: Implement actual flush logic per table
            # Example for one table:
            # _flush_table_to_mysql('income_entries', user_id)
        
        elapsed = time.time() - start_time
        logger.info(f"[FLUSH] ⓘ Flush check complete: {len(users_to_flush)} user(s), {total_records} total cached records (actual flush not yet implemented) in {elapsed:.2f}s")
        
    except Exception as e:
        logger.error(f"[FLUSH] ✗ Error in Redis to MySQL flush: {e}", exc_info=True)


def _flush_table_to_mysql(table: str, user_id: int):
    """
    Flush a specific table's Redis data back to MySQL.
    
    This is a template implementation showing the pattern.
    
    Args:
        table: Table name
        user_id: User ID
        
    TODO: Complete implementation for each table type
    TODO: Add optimistic locking to prevent conflicts
    TODO: Batch operations for better performance
    """
    try:
        redis_key = _get_redis_key(table, user_id)
        redis_data = _redis_client.get(redis_key)
        
        if not redis_data:
            return
        
        rows = json.loads(redis_data)
        
        if not rows:
            return
        
        # TODO: For each row, check if it's dirty (modified)
        # TODO: Build efficient UPSERT query
        # TODO: Handle last_modified timestamps
        
        with get_db_pool().get_cursor(commit=True) as cursor:
            for row in rows:
                # Example UPSERT pattern (adjust per table schema)
                # cursor.execute(
                #     f"INSERT INTO {table} (...) VALUES (...) "
                #     "ON DUPLICATE KEY UPDATE ..."
                # )
                pass
        
        logger.debug(f"Flushed {len(rows)} rows from Redis to {table} for user {user_id}")
        
    except Exception as e:
        logger.error(f"Error flushing {table} to MySQL for user {user_id}: {e}")


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
