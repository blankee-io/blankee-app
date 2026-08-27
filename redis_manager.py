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

import json
import time
import threading
from datetime import datetime, timedelta, date
from decimal import Decimal
from typing import Dict, List, Optional, Any, Set
from collections import defaultdict
from db_connections import get_db_pool
from log_config import JsonFormatter, get_logger, log_info, log_error, log_warning, log_exception
import pymysql.cursors

# Configure logger with JSON formatting
logger = get_logger(__name__)

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
REDIS_TTL = 604800  # 7 days in seconds

# Tables to hydrate for each user
USER_TABLES = [
    'income_categories',
    'income_category_groups',
    'expense_categories',
    'expense_category_groups',
    'c_expense_category_groups',
    'income_entries',
    'expense_entries',
    'recurring_income',
    'recurring_expense',
    'recurring_c_expense',
    'recurring_income_buckets',
    'recurring_expense_buckets',
    'recurring_c_expense_buckets',
    'starting_balance',
    'totals_remainders',
    'totals_remainders_d',
    'totals_remainders_m',
    'savings_entries',
    'savings_adjustments',  # Bank balance adjustments for savings
    'credit_accounts',
    'c_expense_categories',
    'c_expense_entries',
    'c_payment_entries',
    'c_a_balances',
    'c_a_balances_d',
    'c_a_balances_m',
    'buds',
    'bud_items',
    # bank-link integration tables
    'linked_provider_profiles',
    'linked_connections',
    'linked_accounts',
    'linked_transactions',
    'category_memory',
    # Additional user tables
    'notifications',
    'password_resets',
    'setup_state',
    'recurring_mismatches',
    'recurring_suggestions',
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


def _coerce(value, default):
    """None-safe default for numeric columns.

    A cached users blob can legitimately carry None for a nullable column. Passing
    that straight into float()/int() raises TypeError, which aborts the ENTIRE users
    flush for that user — so name/email edits silently never reach MySQL. Note this
    differs from dict.get(key, default), which only applies when the key is absent.
    """
    return default if value is None else value


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
        log_info(logger, 'REDIS', "Redis flush worker thread started")
    
    if _dehydration_thread is None or not _dehydration_thread.is_alive():
        _dehydration_thread = threading.Thread(target=_dehydration_worker, daemon=True, name="RedisDehydrationWorker")
        _dehydration_thread.start()
        log_info(logger, 'REDIS', "Redis dehydration worker thread started")


def shutdown_redis_manager():
    """Gracefully shutdown background threads"""
    global _shutdown_event
    log_info(logger, 'REDIS', "Shutting down Redis manager...")
    _shutdown_event.set()
    
    if _flush_thread and _flush_thread.is_alive():
        _flush_thread.join(timeout=5)
    if _dehydration_thread and _dehydration_thread.is_alive():
        _dehydration_thread.join(timeout=5)
    
    log_info(logger, 'REDIS', "Redis manager shutdown complete")


def track_user_activity(user_id: int):
    """
    Track user activity and trigger hydration if needed.
    Call this function on every user request/interaction.
    
    Args:
        user_id: The ID of the active user
    """
    global _user_last_activity, _hydrated_users
    
    if not _redis_client:
        log_warning(logger, 'REDIS', "Redis client not initialized")
        return
    
    current_time = time.time()
    
    with _user_activity_lock:
        _user_last_activity[user_id] = current_time
        
        # Check if user needs hydration and isn't already being hydrated
        if user_id not in _hydrated_users and user_id not in _hydrating_users:
            # Mark as hydrating to prevent duplicate threads
            _hydrating_users.add(user_id)
            log_info(logger, 'REDIS', f"User {user_id} needs hydration, triggering background hydration")
            # Run hydration in background thread to avoid blocking request
            threading.Thread(
                target=_hydrate_user_data,
                args=(user_id,),
                daemon=True,
                name=f"Hydrate-{user_id}"
            ).start()
        elif user_id in _hydrating_users:
            log_info(logger, 'REDIS', f"User {user_id} hydration already in progress")
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


def get_table_cache(table: str, user_id: int):
    """
    Read a user's cached rows for one table out of Redis.

    Returns None when Redis is unavailable, the user is not hydrated, or the key
    is absent - callers treat None as "cache miss, fall back to MySQL".

    This is the single implementation shared by redis_crud and the bank-link
    CRUD modules. It reads the module-global client at CALL time, so it works
    regardless of whether the caller was imported before init_redis_manager().
    """
    if not _redis_client or not is_user_hydrated(user_id):
        return None

    cached = _redis_client.get(_get_redis_key(table, user_id))
    if cached:
        return json.loads(cached)
    return None


def set_table_cache(table: str, user_id: int, data, mark_dirty: bool = True) -> bool:
    """
    Write a user's rows for one table into Redis.

    mark_dirty controls which persistence model the caller is using, and getting
    it wrong is silent either way - so it is always explicit at the call site:

    - mark_dirty=True (Redis-first): Redis is the source of truth and the flush
      worker must carry this table to MySQL. Adds the table to
      dirty_tables:{user_id}. This is what every write path needs.
    - mark_dirty=False: the caller already wrote MySQL itself (redis_crud) or is
      staging a delete that a separate pending-delete key handles. Refreshing the
      cache here must NOT queue a flush.
    """
    if not _redis_client:
        log_error(logger, 'REDIS', f"Redis client not available for {table}")
        return False

    try:
        redis_key = _get_redis_key(table, user_id)
        _redis_client.setex(
            redis_key,
            INACTIVITY_TIMEOUT + 60,
            json.dumps(data, cls=DecimalEncoder)
        )

        if mark_dirty:
            _redis_client.sadd(f"dirty_tables:{user_id}", table)

        return True
    except Exception as e:
        log_exception(logger, 'REDIS', f"Error setting Redis data for {table}: {e}")
        return False


def _hydrate_user_data(user_id: int):
    """
    Hydrate a user's data from MySQL into Redis.
    This runs in a background thread.
    
    Args:
        user_id: The ID of the user to hydrate
    """
    global _hydrated_users, _hydrating_users
    
    start_time = time.time()
    log_info(logger, 'HYDRATION', f"Starting hydration for user {user_id}")
    
    try:
        # Double-check if already hydrated (race condition check)
        with _user_activity_lock:
            if user_id in _hydrated_users:
                log_info(logger, 'REDIS', f"User {user_id} already hydrated, skipping")
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
        
        # Mark user as hydrated and remove from hydrating set
        with _user_activity_lock:
            _hydrated_users.add(user_id)
            _hydrating_users.discard(user_id)
        
        elapsed = time.time() - start_time
        log_info(logger, 'HYDRATION', f"✓ User {user_id} hydrated: {total_rows} total rows across {tables_hydrated} tables in {elapsed:.2f}s")
        
        # Send frontend refresh signal
        _signal_frontend_refresh(user_id)
        
    except Exception as e:
        # Remove from hydrating set on error
        with _user_activity_lock:
            _hydrating_users.discard(user_id)
        log_exception(logger, 'HYDRATION', f"✗ Error hydrating user {user_id}: {e}")


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
                log_info(logger, 'REDIS', f"Hydrated user profile for user {user_id}")
    except Exception as e:
        log_error(logger, 'REDIS', f"Error hydrating user profile for {user_id}: {e}")


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
            elif table == 'recurring_income_buckets':
                # recurring_income_buckets -> income_categories -> users
                query = """
                    SELECT rib.* FROM recurring_income_buckets rib
                    INNER JOIN income_categories ic ON rib.category_id = ic.id
                    WHERE ic.user_id = %s
                """
            elif table == 'recurring_expense_buckets':
                # recurring_expense_buckets -> expense_categories -> users
                query = """
                    SELECT reb.* FROM recurring_expense_buckets reb
                    INNER JOIN expense_categories ec ON reb.category_id = ec.id
                    WHERE ec.user_id = %s
                """
            elif table == 'recurring_c_expense_buckets':
                # recurring_c_expense_buckets -> c_expense_categories -> credit_accounts -> users
                query = """
                    SELECT rceb.* FROM recurring_c_expense_buckets rceb
                    INNER JOIN c_expense_categories cec ON rceb.category_id = cec.id
                    INNER JOIN credit_accounts ca ON cec.account_id = ca.id
                    WHERE ca.user_id = %s
                """
            elif table == 'bud_items':
                # bud_items -> buds -> users. It has no user_id of its own, so
                # the default branch below emitted "WHERE user_id = %s" against
                # a table without that column - a query that failed on every
                # hydration pass.
                query = """
                    SELECT bi.* FROM bud_items bi
                    INNER JOIN buds b ON bi.bud_id = b.id
                    WHERE b.user_id = %s
                """
            elif table == 'credit_accounts':
                # Credit accounts ordered by display_order DESC (highest at top, like categories)
                query = "SELECT * FROM credit_accounts WHERE user_id = %s ORDER BY display_order DESC"
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
                log_info(logger, 'REDIS', f"Hydrated {len(rows)} rows from {table} for user {user_id}")
                return len(rows)
            else:
                # Store empty array to indicate table was checked
                redis_key = _get_redis_key(table, user_id)
                _redis_client.setex(redis_key, INACTIVITY_TIMEOUT + 60, json.dumps([]))
                return 0
                
    except Exception as e:
        log_error(logger, 'REDIS', f"Error hydrating {table} for user {user_id}: {e}")
        return 0


def _refresh_user_ttls(user_id: int):
    """
    Refresh TTLs for all of a user's Redis keys to prevent expiration during active use.
    This is called periodically when an active user makes requests.
    Uses a throttle to avoid excessive Redis operations.
    Also hydrates any missing tables (e.g., if new tables were added to USER_TABLES).
    
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
        
        log_info(logger, 'TTL_REFRESH', f"Refreshing TTLs for user {user_id}")
        
        # Refresh TTL for user profile
        user_key = _get_redis_key('users', user_id)
        if _redis_client.exists(user_key):
            _redis_client.expire(user_key, INACTIVITY_TIMEOUT + 60)
        
        # Refresh TTL for all user tables, hydrate any missing
        refreshed_count = 0
        hydrated_count = 0
        for table in USER_TABLES:
            key = _get_redis_key(table, user_id)
            if _redis_client.exists(key):
                _redis_client.expire(key, INACTIVITY_TIMEOUT + 60)
                refreshed_count += 1
            else:
                # Missing table - hydrate it from MySQL
                rows_count = _hydrate_table(table, user_id)
                if rows_count > 0:
                    hydrated_count += 1
                    log_info(logger, 'TTL_REFRESH', f"Hydrated missing table {table} for user {user_id}: {rows_count} rows")
        
        if hydrated_count > 0:
            log_info(logger, 'TTL_REFRESH', f"✓ Refreshed {refreshed_count} keys, hydrated {hydrated_count} missing tables for user {user_id}")
        else:
            log_info(logger, 'TTL_REFRESH', f"✓ Refreshed {refreshed_count} keys for user {user_id}")
        
    except Exception as e:
        log_error(logger, 'TTL_REFRESH', f"Error refreshing TTLs for user {user_id}: {e}")


def _dehydrate_user_data(user_id: int):
    """
    Remove a user's data from Redis (dehydration).
    Flushes dirty data to MySQL before removing keys.
    
    Args:
        user_id: User ID to dehydrate
    """
    global _hydrated_users
    
    start_time = time.time()
    log_info(logger, 'DEHYDRATION', f"Starting dehydration for user {user_id}")
    
    try:
        # Flush dirty data to MySQL before dehydration
        dirty_tables_key = f"dirty_tables:{user_id}"
        dirty_tables = _redis_client.smembers(dirty_tables_key)
        
        if dirty_tables:
            log_info(logger, 'DEHYDRATION', f"Flushing {len(dirty_tables)} dirty tables for user {user_id} before dehydration")
            
            tables_to_flush = [
                'totals_remainders',
                'totals_remainders_d', 
                'totals_remainders_m',
                'savings_entries',
                'savings_adjustments',  # Bank balance adjustments
                'c_a_balances',
                'c_a_balances_d',
                'c_a_balances_m',
                'income_entries',
                'expense_entries',
                'c_expense_entries',
                'recurring_income',
                'recurring_expense',
                'recurring_c_expense',
                'recurring_income_buckets',  # Bucket state tracking - must flush before dehydration
                'recurring_expense_buckets',
                'recurring_c_expense_buckets',
                'buds',  # Must flush before bud_items to resolve temp IDs
                'bud_items',
                'users',  # User settings (goofy_week_mode, landing_page, etc.)
                'notifications',  # User notifications
                'setup_state',  # Setup wizard temporary state
                'recurring_mismatches',  # provider recurring mismatch detection
                'recurring_suggestions',  # the enrichment provider suggested recurring entries
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
                log_info(logger, 'DEHYDRATION', f"Flushed {flushed_count} rows to MySQL for user {user_id}")
        
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
            for table in ['income_entries', 'expense_entries', 'c_expense_entries', 
                          'recurring_income_buckets', 'recurring_expense_buckets', 'recurring_c_expense_buckets']:
                pending_key = f"pending_deletes:{table}:{user_id}"
                _redis_client.delete(pending_key)
            # Clean up the bank provider-related keys
            
            elapsed = time.time() - start_time
            log_info(logger, 'DEHYDRATION', f"✓ User {user_id} dehydrated: {len(keys_to_delete)} Redis keys deleted in {elapsed:.2f}s")
        else:
            log_info(logger, 'DEHYDRATION', f"User {user_id} had no keys to delete")
        
        # Remove from hydrated set
        with _user_activity_lock:
            _hydrated_users.discard(user_id)
        
    except Exception as e:
        log_exception(logger, 'DEHYDRATION', f"✗ Error dehydrating user {user_id}: {e}")


def _dehydration_worker():
    """
    Background worker that checks for inactive users and dehydrates them.
    Runs continuously until shutdown.
    """
    log_info(logger, 'REDIS', "Dehydration worker started")
    
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
                log_info(logger, 'REDIS', f"User {user_id} inactive for {INACTIVITY_TIMEOUT}s, dehydrating")
                _dehydrate_user_data(user_id)
            
            # Sleep for 30 seconds before next check
            _shutdown_event.wait(30)
            
        except Exception as e:
            log_exception(logger, 'REDIS', f"Error in dehydration worker: {e}")
            _shutdown_event.wait(30)
    
    log_info(logger, 'REDIS', "Dehydration worker stopped")


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
            log_info(logger, 'FLUSH', "No hydrated users to flush")
            return
        
        log_info(logger, 'FLUSH', f"Starting flush for {len(users_to_flush)} hydrated user(s)")
        
        total_flushed = 0
        tables_to_flush = [
            'totals_remainders',
            'totals_remainders_d', 
            'totals_remainders_m',
            'savings_entries',
            'savings_adjustments',  # Bank balance adjustments for savings
            'credit_accounts',  # MUST flush FIRST - other tables depend on this for foreign keys
            'c_a_balances',
            'c_a_balances_d',
            'c_a_balances_m',
            'income_category_groups',  # Groups flush before categories (categories reference groups)
            'expense_category_groups',
            'c_expense_category_groups',  # Depends on expense_category_groups
            'income_categories',  # Category definitions - flush before entries/recurring
            'expense_categories',
            'c_expense_categories',  # Depends on credit_accounts
            'income_entries',
            'expense_entries',
            'c_expense_entries',
            'c_payment_entries',  # Credit account payment entries
            'recurring_income',  # Recurring entry configurations - depend on categories
            'recurring_expense',
            'recurring_c_expense',  # Depends on c_expense_categories
            'recurring_income_buckets',  # Bucket state tracking
            'recurring_expense_buckets',
            'recurring_c_expense_buckets',
            'buds',  # Must flush before bud_items to resolve temp IDs
            'bud_items',
            'users',  # User settings (balance_threshold, starting_savings)
            'notifications',  # User notifications
            'linked_provider_profiles',  # provider session tokens and profile info
            'linked_connections',  # the bank provider bank connections
            'linked_accounts',  # the bank provider bank accounts
            'linked_transactions',  # linked transactions
            'category_memory',  # the bank provider category mappings
            'setup_state',  # Setup wizard temporary state
            'recurring_mismatches',  # provider recurring mismatch detection
            'recurring_suggestions',  # the enrichment provider suggested recurring entries
            # Deletion handlers (must run after updates)
            'linked_connections_deleted',
            'linked_accounts_deleted',
            'linked_transactions_deleted',
        ]
        
        for user_id in users_to_flush:
            user_flushed = 0
            table_stats = {}
            
            # Get dirty tables for this user
            dirty_tables_key = f"dirty_tables:{user_id}"
            dirty_tables_raw = _redis_client.smembers(dirty_tables_key)
            
            # Decode bytes to strings
            dirty_tables = set()
            for dt in dirty_tables_raw:
                if isinstance(dt, bytes):
                    dirty_tables.add(dt.decode('utf-8'))
                else:
                    dirty_tables.add(dt)
            
            if not dirty_tables:
                log_info(logger, 'FLUSH', f"No dirty tables for user {user_id}")
                continue
            
            log_info(logger, 'FLUSH', f"User {user_id} has {len(dirty_tables)} dirty tables: {', '.join(dirty_tables)}")
            
            # Flush only dirty tables
            for table in tables_to_flush:
                # Skip if table is not dirty
                if table not in dirty_tables:
                    continue
                
                log_info(logger, 'FLUSH', f"Processing dirty table: {table} for user {user_id}")
                log_info(logger, 'FLUSH', f"Attempting to flush {table} for user {user_id}")
                flushed_count = _flush_table_to_mysql(table, user_id)
                
                # If flushed_count is -1, skip clearing dirty flag (deferred processing)
                if flushed_count == -1:
                    log_info(logger, 'FLUSH', f"Deferred processing for {table} - not clearing dirty flag")
                    continue
                
                # Always clear the dirty flag after processing to prevent infinite retry loops
                # The table will be marked dirty again if new changes occur
                if flushed_count > 0:
                    table_stats[table] = flushed_count
                    log_info(logger, 'FLUSH', f"Cleared dirty flag for {table} (flushed={flushed_count})")
                else:
                    log_info(logger, 'FLUSH', f"Cleared dirty flag for {table} (no data to flush)")
                
                # Remove from dirty set after processing
                _redis_client.srem(dirty_tables_key, table)
                    
                user_flushed += flushed_count
            
            total_flushed += user_flushed
            if user_flushed > 0:
                stats_str = ", ".join([f"{table}: {count}" for table, count in table_stats.items()])
                log_info(logger, 'FLUSH', f"User {user_id}: {user_flushed} rows ({stats_str})")
        
        elapsed = time.time() - start_time
        if total_flushed > 0:
            log_info(logger, 'FLUSH', f"✓ Flush complete: {len(users_to_flush)} user(s), {total_flushed} rows written to MySQL in {elapsed:.2f}s")
        else:
            log_info(logger, 'FLUSH', f"No dirty data to flush for {len(users_to_flush)} user(s)")
        
    except Exception as e:
        log_exception(logger, 'FLUSH', f"✗ Error in Redis to MySQL flush: {e}")


def _flush_table_to_mysql(table: str, user_id: int):
    """
    Flush a specific table's Redis data back to MySQL.
    
    Args:
        table: Table name
        user_id: User ID
        
    Returns:
        Count of rows flushed
    """
    log_info(logger, 'FLUSH', f"_flush_table_to_mysql called for table={table}, user_id={user_id}")
    
    try:
        # Handle special deletion tables first (they don't have Redis data)
        if table == 'linked_connections_deleted':
            # Handle deletion of linked connections
            log_info(logger, 'FLUSH', f"Processing linked_connections_deleted for user {user_id}")
            
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor()
                
                delete_key = f"linked_connections_to_delete:{user_id}"
                connection_ids = _redis_client.smembers(delete_key)
                
                log_info(logger, 'FLUSH', f"Found {len(connection_ids) if connection_ids else 0} connections to delete")
                
                if not connection_ids:
                    # No connections to delete, but clear the deletion set and return 0
                    # This allows the dirty flag to be removed
                    _redis_client.delete(delete_key)
                    log_info(logger, 'FLUSH', f"No connections to delete, cleared deletion set")
                    return 0
                
                deleted_count = 0
                for conn_id in connection_ids:
                    conn_id_str = conn_id.decode('utf-8') if isinstance(conn_id, bytes) else conn_id
                    
                    # Delete accounts first (foreign key constraint)
                    cursor.execute("""
                        DELETE FROM linked_accounts 
                        WHERE user_id = %s AND connection_id IN (
                            SELECT id FROM linked_connections WHERE connection_id = %s
                        )
                    """, (user_id, conn_id_str))
                    
                    # Delete connection
                    cursor.execute("""
                        DELETE FROM linked_connections 
                        WHERE user_id = %s AND connection_id = %s
                    """, (user_id, conn_id_str))
                    
                    deleted_count += cursor.rowcount
                    log_info(logger, 'FLUSH', f"Deleted linked connection {conn_id_str} for user {user_id}")
                
                conn.commit()
                
                # Clear the deletion set
                _redis_client.delete(delete_key)
                
                # Also clear linked_connections dirty flag since deletes are now processed
                dirty_key = f"dirty_tables:{user_id}"
                _redis_client.srem(dirty_key, 'linked_connections')
                log_info(logger, 'FLUSH', f"Cleared linked_connections dirty flag after delete processing")
                
                cursor.close()
                return deleted_count
        
        elif table == 'linked_accounts_deleted':
            # This is handled by linked_connections_deleted (cascade delete)
            # Just return 0
            return 0
        
        elif table == 'linked_transactions_deleted':
            # This is handled by linked_accounts_deleted (cascade delete via FK)
            # Just return 0
            return 0
        
        # Regular table flush logic
        redis_key = _get_redis_key(table, user_id)
        log_info(logger, 'FLUSH', f"Looking for Redis key: {redis_key}")
        redis_data = _redis_client.get(redis_key)
        
        # Check for pending deletes even if Redis data doesn't exist
        # This handles the case where data was deleted from Redis but pending_deletes still need processing
        pending_key = f"pending_deletes:{table}:{user_id}"
        pending_deletes = _redis_client.smembers(pending_key)
        
        if not redis_data:
            # Even with no Redis data, we may have pending deletes to process
            if pending_deletes:
                log_info(logger, 'FLUSH', f"No Redis data but found {len(pending_deletes)} pending deletes for {table}")
                delete_ids = [int(id_str) for id_str in pending_deletes]
                
                with get_db_pool().get_connection() as conn:
                    cursor = conn.cursor()
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    if table == 'bud_items':
                        # No user_id column; scoped through its parent bud.
                        cursor.execute(f"""
                            DELETE bi FROM bud_items bi
                            INNER JOIN buds b ON bi.bud_id = b.id
                            WHERE bi.id IN ({placeholders}) AND b.user_id = %s
                        """, delete_ids + [user_id])
                    else:
                        cursor.execute(f"""
                            DELETE FROM {table} WHERE id IN ({placeholders}) AND user_id = %s
                        """, delete_ids + [user_id])
                    conn.commit()
                    log_info(logger, 'FLUSH', f"Deleted {len(delete_ids)} {table} from MySQL via pending_deletes (no Redis data)")
                
                # Clear the pending deletes set
                _redis_client.delete(pending_key)
                return len(delete_ids)
            else:
                log_info(logger, 'FLUSH', f"⚠️ No Redis data found for key: {redis_key} - returning 0 (dirty flag will NOT be cleared)")
                return 0
        
        log_info(logger, 'FLUSH', f"Found Redis data for {table}, length: {len(redis_data)} bytes")
        rows = json.loads(redis_data)
        
        # Don't return early when rows is an empty list - we still need to
        # process pending deletions (pending_deletes) for tables like
        # income_entries/expense_entries/c_expense_entries. The per-table
        # handlers below will correctly handle empty `rows` when performing
        # upserts. Keep a debug log for visibility.
        log_info(logger, 'FLUSH', f"Found {len(rows)} rows in Redis for {table}")
        
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
                log_info(logger, 'FLUSH', f"→ {table}: {len(batch_data)} rows")
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
                log_info(logger, 'FLUSH', f"→ savings_entries: {len(batch_data)} rows")
                return len(batch_data)
            
            elif table == 'savings_adjustments':
                # Savings adjustments table (bank balance sync)
                # Allow multiple adjustments per date (no unique constraint on user_id+date)
                
                # First, delete all existing adjustments for this user
                cursor.execute("DELETE FROM savings_adjustments WHERE user_id = %s", (user_id,))
                
                # Then insert all adjustments from Redis
                batch_data = []
                for row in rows:
                    batch_data.append((
                        user_id,
                        row.get('date'),
                        float(row.get('amount', 0)),
                        row.get('description'),
                        row.get('linked_account_id')
                    ))
                
                if batch_data:
                    cursor.executemany("""
                        INSERT INTO savings_adjustments (user_id, date, amount, description, linked_account_id)
                        VALUES (%s, %s, %s, %s, %s)
                    """, batch_data)
                
                conn.commit()
                cursor.close()
                log_info(logger, 'FLUSH', f"→ savings_adjustments: {len(batch_data)} rows")
                return len(batch_data)
                
            elif table in ['c_a_balances', 'c_a_balances_d', 'c_a_balances_m']:
                # Credit account balance tables
                batch_data = []
                skipped_temp_accounts = 0
                
                for row in rows:
                    account_id = row.get('account_id')
                    
                    # Skip rows with temp negative account_id - account hasn't been flushed yet
                    if account_id and int(account_id) < 0:
                        skipped_temp_accounts += 1
                        continue
                    
                    batch_data.append((
                        account_id,
                        row.get('date'),
                        float(row.get('total_expenses', 0)),
                        float(row.get('total_payments', 0)) if 'total_payments' in row else 0.0,
                        float(row.get('balance', 0))
                    ))
                
                if skipped_temp_accounts > 0:
                    log_info(logger, 'FLUSH', f"Skipped {skipped_temp_accounts} {table} rows with temp account_id")
                
                if batch_data:
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
                log_info(logger, 'FLUSH', f"→ {table}: {len(batch_data)} rows")
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
                    log_info(logger, 'FLUSH', f"Deleted {len(delete_ids)} income_entries from MySQL")
                    # Filter out pending deletes from rows to prevent re-upsert
                    delete_ids_set = set(delete_ids)
                    rows = [r for r in rows if r.get('id') not in delete_ids_set]
                    log_info(logger, 'FLUSH', f"Filtered out {len(delete_ids)} pending deletes from income_entries rows")
                
                # Now UPSERT the current state from Redis
                log_info(logger, 'FLUSH_DEBUG', f"Starting income_entries batch preparation for {len(rows)} rows")
                
                # Get valid category IDs to filter out entries for deleted categories
                cursor.execute("SELECT id FROM income_categories WHERE user_id = %s", (user_id,))
                valid_category_ids = {row[0] for row in cursor.fetchall()}
                log_info(logger, 'FLUSH', f"Found {len(valid_category_ids)} valid income categories for user {user_id}")
                
                batch_data = []
                temp_id_entries = []  # Entries with temp negative IDs that need INSERT
                skipped_count = 0
                skipped_temp_category = 0
                redis_needs_update = False  # Track if we need to update Redis with new IDs
                
                for row in rows:
                    entry_id = row.get('id')
                    category_id = row.get('category_id')
                    
                    # Skip entries with temp negative category_id - category hasn't been flushed yet
                    if category_id and category_id < 0:
                        skipped_temp_category += 1
                        continue
                    
                    # Skip entries for categories that don't exist (were deleted)
                    if category_id not in valid_category_ids:
                        skipped_count += 1
                        log_warning(logger, 'FLUSH', f"Skipping entry for deleted category {category_id}, entry_id={row.get('id')}")
                        continue
                    
                    recurring_id = row.get('recurring_id')
                    log_info(logger, 'FLUSH_DEBUG', f"Row recurring_id RAW: {recurring_id}, type: {type(recurring_id)}")
                    if recurring_id is not None:
                        try:
                            recurring_id = int(recurring_id)
                            # Validate it's within MySQL INT range
                            if recurring_id < -2147483648 or recurring_id > 2147483647:
                                log_warning(logger, 'FLUSH', f"recurring_id {recurring_id} out of range, setting to NULL")
                                recurring_id = None
                            else:
                                log_info(logger, 'FLUSH_DEBUG', f"Row recurring_id CONVERTED: {recurring_id}")
                        except (ValueError, TypeError) as e:
                            log_error(logger, 'FLUSH', f"Invalid recurring_id value: {recurring_id}, type: {type(recurring_id)}, row: {row}")
                            recurring_id = None
                    
                    # Handle entries with no ID or temp negative ID - need to INSERT and get real ID
                    if entry_id is None or entry_id < 0:
                        temp_id_entries.append({
                            'temp_id': entry_id,  # Will be None for new entries, negative for temp
                            'category_id': category_id,
                            'date': row.get('date'),
                            'amount': float(row.get('amount', 0)),
                            'recurring_id': recurring_id,
                            'is_bucket': int(row.get('is_bucket', 0)),
                            'original_amount': float(row.get('original_amount')) if row.get('original_amount') is not None else None,
                            'original_date': row.get('original_date'),
                            'processed': int(row.get('processed', 0)),
                            'pending': int(row.get('pending', 0)),
                            'auto_confirmed': int(row.get('auto_confirmed', 0)),
                            'is_auto_adjustment': int(row.get('is_auto_adjustment', 0))
                        })
                        continue
                    
                    batch_data.append((
                        row.get('id'),
                        category_id,
                        row.get('date'),
                        float(row.get('amount', 0)),
                        recurring_id,
                        int(row.get('is_bucket', 0)),
                        float(row.get('original_amount')) if row.get('original_amount') is not None else None,
                        row.get('original_date'),
                        int(row.get('processed', 0)),
                        int(row.get('pending', 0)),
                        int(row.get('auto_confirmed', 0)),
                        int(row.get('is_auto_adjustment', 0))
                    ))
                
                if skipped_count > 0:
                    log_info(logger, 'FLUSH', f"Skipped {skipped_count} entries for deleted categories")
                
                if skipped_temp_category > 0:
                    log_info(logger, 'FLUSH', f"Skipped {skipped_temp_category} income_entries with temp category_ids - will retry next flush")
                
                # Handle entries with no ID or temp negative IDs - INSERT them and update Redis with real IDs
                if temp_id_entries:
                    log_info(logger, 'FLUSH', f"Processing {len(temp_id_entries)} income_entries with no ID or temp negative IDs")
                    id_mapping = {}  # temp_id -> real_id (for negative IDs)
                    none_id_updates = []  # List of (category_id, date, new_id) for None ID entries
                    
                    for entry in temp_id_entries:
                        cursor.execute("""
                            INSERT INTO income_entries (category_id, date, amount, recurring_id, is_bucket, original_amount, original_date, processed, pending, auto_confirmed, is_auto_adjustment)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (
                            entry['category_id'],
                            entry['date'],
                            entry['amount'],
                            entry['recurring_id'],
                            entry['is_bucket'],
                            entry['original_amount'],
                            entry['original_date'],
                            entry['processed'],
                            entry['pending'],
                            entry['auto_confirmed'],
                            entry['is_auto_adjustment']
                        ))
                        new_id = cursor.lastrowid
                        if entry['temp_id'] is None:
                            # Track by category_id and date for None ID entries
                            none_id_updates.append((entry['category_id'], entry['date'], new_id))
                        else:
                            id_mapping[entry['temp_id']] = new_id
                        log_info(logger, 'FLUSH', f"Inserted income_entry with temp_id {entry['temp_id']} -> new real_id {new_id}")
                    
                    # Update Redis entries with new real IDs
                    if id_mapping or none_id_updates:
                        for row in rows:
                            row_id = row.get('id')
                            if row_id is not None and row_id in id_mapping:
                                row['id'] = id_mapping[row_id]
                            elif row_id is None:
                                # Match by category_id and date for None ID entries
                                for cat_id, entry_date, new_id in none_id_updates:
                                    row_date = row.get('date')
                                    if row.get('category_id') == cat_id and (row_date == entry_date or (hasattr(row_date, 'isoformat') and row_date.isoformat() == entry_date)):
                                        row['id'] = new_id
                                        none_id_updates.remove((cat_id, entry_date, new_id))
                                        break
                        redis_needs_update = True
                        log_info(logger, 'FLUSH', f"Updated entries in rows list with real IDs")
                
                if batch_data:
                    # Log the first few rows for debugging
                    for i, data in enumerate(batch_data[:5]):
                        log_info(logger, 'FLUSH_DEBUG', f"income_entries row {i}: id={data[0]}, category={data[1]}, date={data[2]}, amount={data[3]}, recurring_id={data[4]} (type: {type(data[4])}), is_bucket={data[5]}, original_amount={data[6]}, processed={data[7]}, pending={data[8]}, auto_confirmed={data[9]}, is_auto_adjustment={data[10]}")
                    
                    cursor.executemany("""
                        INSERT INTO income_entries (id, category_id, date, amount, recurring_id, is_bucket, original_amount, original_date, processed, pending, auto_confirmed, is_auto_adjustment)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            category_id = VALUES(category_id),
                            amount = VALUES(amount),
                            processed = VALUES(processed),
                            recurring_id = VALUES(recurring_id),
                            is_bucket = VALUES(is_bucket),
                            original_amount = VALUES(original_amount),
                            original_date = VALUES(original_date),
                            pending = VALUES(pending),
                            auto_confirmed = VALUES(auto_confirmed),
                            is_auto_adjustment = VALUES(is_auto_adjustment)
                    """, batch_data)
                
                # Update Redis with real IDs if we inserted temp entries
                if redis_needs_update:
                    redis_key = f"income_entries:v1:{user_id}"
                    _redis_client.setex(redis_key, 604800, json.dumps(rows, cls=DecimalEncoder))
                    log_info(logger, 'FLUSH', f"Updated Redis with real IDs for income_entries")
                
                conn.commit()
                cursor.close()
                
                # Clear pending deletions set after successful flush
                _redis_client.delete(pending_key)
                
                log_info(logger, 'FLUSH', f"→ income_entries: {len(batch_data)} rows")
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
                    log_info(logger, 'FLUSH', f"Deleted {len(delete_ids)} expense_entries from MySQL")
                    # Filter out pending deletes from rows to prevent re-upsert
                    delete_ids_set = set(delete_ids)
                    rows = [r for r in rows if r.get('id') not in delete_ids_set]
                    log_info(logger, 'FLUSH', f"Filtered out {len(delete_ids)} pending deletes from expense_entries rows")
                
                # Now UPSERT the current state from Redis
                # Get valid category IDs to filter out entries for deleted categories
                cursor.execute("SELECT id FROM expense_categories WHERE user_id = %s", (user_id,))
                valid_category_ids = {row[0] for row in cursor.fetchall()}
                log_info(logger, 'FLUSH', f"Found {len(valid_category_ids)} valid expense categories for user {user_id}")
                
                batch_data = []
                temp_id_entries = []  # Entries with temp negative IDs that need INSERT
                skipped_count = 0
                skipped_temp_category = 0
                redis_needs_update = False  # Track if we need to update Redis with new IDs
                
                for row in rows:
                    entry_id = row.get('id')
                    category_id = row.get('category_id')
                    
                    # Skip entries with temp negative category_id - category hasn't been flushed yet
                    if category_id and category_id < 0:
                        skipped_temp_category += 1
                        continue
                    
                    # Skip entries for categories that don't exist (were deleted)
                    if category_id not in valid_category_ids:
                        skipped_count += 1
                        log_warning(logger, 'FLUSH', f"Skipping expense entry for deleted category {category_id}, entry_id={row.get('id')}")
                        continue
                    
                    recurring_id = row.get('recurring_id')
                    if recurring_id is not None:
                        recurring_id = int(recurring_id)
                    
                    # Handle entries with no ID or temp negative ID - need to INSERT and get real ID
                    if entry_id is None or entry_id < 0:
                        temp_id_entries.append({
                            'temp_id': entry_id,  # Will be None for new entries, negative for temp
                            'category_id': category_id,
                            'date': row.get('date'),
                            'amount': float(row.get('amount', 0)),
                            'recurring_id': recurring_id,
                            'is_bucket': int(row.get('is_bucket', 0)),
                            'original_amount': float(row.get('original_amount')) if row.get('original_amount') is not None else None,
                            'original_date': row.get('original_date'),
                            'processed': int(row.get('processed', 0)),
                            'bud_item_id': row.get('bud_item_id'),
                            'pending': int(row.get('pending', 0)),
                            'auto_confirmed': int(row.get('auto_confirmed', 0)),
                            'is_auto_adjustment': int(row.get('is_auto_adjustment', 0))
                        })
                        continue
                    
                    batch_data.append((
                        row.get('id'),
                        row.get('category_id'),
                        row.get('date'),
                        float(row.get('amount', 0)),
                        recurring_id,
                        int(row.get('is_bucket', 0)),
                        float(row.get('original_amount')) if row.get('original_amount') is not None else None,
                        row.get('original_date'),
                        int(row.get('processed', 0)),
                        row.get('bud_item_id'),
                        int(row.get('pending', 0)),
                        int(row.get('auto_confirmed', 0)),
                        int(row.get('is_auto_adjustment', 0))
                    ))
                
                if skipped_count > 0:
                    log_info(logger, 'FLUSH', f"Skipped {skipped_count} expense entries for deleted categories")
                
                if skipped_temp_category > 0:
                    log_info(logger, 'FLUSH', f"Skipped {skipped_temp_category} expense_entries with temp category_ids - will retry next flush")
                
                # Handle entries with no ID or temp negative IDs - INSERT them and update Redis with real IDs
                if temp_id_entries:
                    log_info(logger, 'FLUSH', f"Processing {len(temp_id_entries)} expense_entries with no ID or temp negative IDs")
                    id_mapping = {}  # temp_id -> real_id (for negative IDs)
                    none_id_updates = []  # List of (category_id, date, new_id) for None ID entries
                    
                    for entry in temp_id_entries:
                        cursor.execute("""
                            INSERT INTO expense_entries (category_id, date, amount, recurring_id, is_bucket, original_amount, original_date, processed, bud_item_id, pending, auto_confirmed, is_auto_adjustment)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (
                            entry['category_id'],
                            entry['date'],
                            entry['amount'],
                            entry['recurring_id'],
                            entry['is_bucket'],
                            entry['original_amount'],
                            entry['original_date'],
                            entry['processed'],
                            entry['bud_item_id'],
                            entry['pending'],
                            entry['auto_confirmed'],
                            entry['is_auto_adjustment']
                        ))
                        new_id = cursor.lastrowid
                        if entry['temp_id'] is None:
                            none_id_updates.append((entry['category_id'], entry['date'], new_id))
                        else:
                            id_mapping[entry['temp_id']] = new_id
                        log_info(logger, 'FLUSH', f"Inserted expense_entry with temp_id {entry['temp_id']} -> new real_id {new_id}")
                    
                    # Update Redis entries with new real IDs
                    if id_mapping or none_id_updates:
                        for row in rows:
                            row_id = row.get('id')
                            if row_id is not None and row_id in id_mapping:
                                row['id'] = id_mapping[row_id]
                            elif row_id is None:
                                for cat_id, entry_date, new_id in none_id_updates:
                                    row_date = row.get('date')
                                    if row.get('category_id') == cat_id and (row_date == entry_date or (hasattr(row_date, 'isoformat') and row_date.isoformat() == entry_date)):
                                        row['id'] = new_id
                                        none_id_updates.remove((cat_id, entry_date, new_id))
                                        break
                        redis_needs_update = True
                        log_info(logger, 'FLUSH', f"Updated entries in rows list with real IDs")
                
                if batch_data:
                    cursor.executemany("""
                        INSERT INTO expense_entries (id, category_id, date, amount, recurring_id, is_bucket, original_amount, original_date, processed, bud_item_id, pending, auto_confirmed, is_auto_adjustment)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            category_id = VALUES(category_id),
                            amount = VALUES(amount),
                            processed = VALUES(processed),
                            recurring_id = VALUES(recurring_id),
                            is_bucket = VALUES(is_bucket),
                            original_amount = VALUES(original_amount),
                            original_date = VALUES(original_date),
                            bud_item_id = VALUES(bud_item_id),
                            pending = VALUES(pending),
                            auto_confirmed = VALUES(auto_confirmed),
                            is_auto_adjustment = VALUES(is_auto_adjustment)
                    """, batch_data)
                
                # Update Redis with real IDs if we inserted temp entries
                if redis_needs_update:
                    redis_key = f"expense_entries:v1:{user_id}"
                    _redis_client.setex(redis_key, 604800, json.dumps(rows, cls=DecimalEncoder))
                    log_info(logger, 'FLUSH', f"Updated Redis with real IDs for expense_entries")
                
                conn.commit()
                cursor.close()
                
                # Clear pending deletions set after successful flush
                _redis_client.delete(pending_key)
                
                log_info(logger, 'FLUSH', f"→ expense_entries: {len(batch_data)} rows")
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
                    log_info(logger, 'FLUSH', f"Deleted {len(delete_ids)} c_expense_entries from MySQL")
                    # Filter out pending deletes from rows to prevent re-upsert
                    delete_ids_set = set(delete_ids)
                    rows = [r for r in rows if r.get('id') not in delete_ids_set]
                    log_info(logger, 'FLUSH', f"Filtered out {len(delete_ids)} pending deletes from c_expense_entries rows")
                
                # Now UPSERT the current state from Redis
                # Get valid category IDs to filter out entries for deleted categories
                # c_expense_categories links to credit_accounts via account_id, not directly to users
                cursor.execute("""
                    SELECT cec.id FROM c_expense_categories cec
                    JOIN credit_accounts ca ON cec.account_id = ca.id
                    WHERE ca.user_id = %s
                """, (user_id,))
                valid_category_ids = {row[0] for row in cursor.fetchall()}
                log_info(logger, 'FLUSH', f"Found {len(valid_category_ids)} valid credit expense categories for user {user_id}")
                
                batch_data = []
                temp_id_entries = []  # Entries with temp negative IDs that need INSERT
                skipped_count = 0
                skipped_temp_category = 0
                redis_needs_update = False  # Track if we need to update Redis with new IDs
                
                for row in rows:
                    entry_id = row.get('id')
                    category_id = row.get('category_id')
                    
                    # Skip entries with temp negative category_id - category hasn't been flushed yet
                    if category_id and category_id < 0:
                        skipped_temp_category += 1
                        continue
                    
                    # Skip entries for categories that don't exist (were deleted)
                    if category_id not in valid_category_ids:
                        skipped_count += 1
                        log_warning(logger, 'FLUSH', f"Skipping credit expense entry for deleted category {category_id}, entry_id={row.get('id')}")
                        continue
                    
                    recurring_id = row.get('recurring_id')
                    if recurring_id is not None:
                        recurring_id = int(recurring_id)
                    
                    # Handle entries with no ID or temp negative ID - need to INSERT and get real ID
                    if entry_id is None or entry_id < 0:
                        temp_id_entries.append({
                            'temp_id': entry_id,
                            'category_id': category_id,
                            'date': row.get('date'),
                            'amount': float(row.get('amount', 0)),
                            'recurring_id': recurring_id,
                            'is_bucket': int(row.get('is_bucket', 0)),
                            'original_amount': float(row.get('original_amount')) if row.get('original_amount') is not None else None,
                            'original_date': row.get('original_date'),
                            'processed': int(row.get('processed', 0)),
                            'bud_item_id': row.get('bud_item_id'),
                            'pending': int(row.get('pending', 0)),
                            'auto_confirmed': int(row.get('auto_confirmed', 0)),
                            'is_auto_adjustment': int(row.get('is_auto_adjustment', 0))
                        })
                        continue
                    
                    batch_data.append((
                        row.get('id'),
                        category_id,
                        row.get('date'),
                        float(row.get('amount', 0)),
                        recurring_id,
                        int(row.get('is_bucket', 0)),
                        float(row.get('original_amount')) if row.get('original_amount') is not None else None,
                        row.get('original_date'),
                        int(row.get('processed', 0)),
                        row.get('bud_item_id'),
                        int(row.get('pending', 0)),
                        int(row.get('auto_confirmed', 0)),
                        int(row.get('is_auto_adjustment', 0))
                    ))
                
                if skipped_count > 0:
                    log_info(logger, 'FLUSH', f"Skipped {skipped_count} credit expense entries for deleted categories")
                
                if skipped_temp_category > 0:
                    log_info(logger, 'FLUSH', f"Skipped {skipped_temp_category} c_expense_entries with temp category_ids - will retry next flush")
                
                # Handle entries with no ID or temp negative IDs - INSERT them and update Redis with real IDs
                if temp_id_entries:
                    log_info(logger, 'FLUSH', f"Processing {len(temp_id_entries)} c_expense_entries with no ID or temp negative IDs")
                    id_mapping = {}  # temp_id -> real_id (for negative IDs)
                    none_id_updates = []  # List of (category_id, date, new_id) for None ID entries
                    
                    for entry in temp_id_entries:
                        cursor.execute("""
                            INSERT INTO c_expense_entries (category_id, date, amount, recurring_id, is_bucket, original_amount, original_date, processed, bud_item_id, pending, auto_confirmed, is_auto_adjustment)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (
                            entry['category_id'],
                            entry['date'],
                            entry['amount'],
                            entry['recurring_id'],
                            entry['is_bucket'],
                            entry['original_amount'],
                            entry['original_date'],
                            entry['processed'],
                            entry['bud_item_id'],
                            entry['pending'],
                            entry['auto_confirmed'],
                            entry['is_auto_adjustment']
                        ))
                        new_id = cursor.lastrowid
                        if entry['temp_id'] is None:
                            none_id_updates.append((entry['category_id'], entry['date'], new_id))
                        else:
                            id_mapping[entry['temp_id']] = new_id
                        log_info(logger, 'FLUSH', f"Inserted c_expense_entry with temp_id {entry['temp_id']} -> new real_id {new_id}")
                    
                    # Update Redis entries with new real IDs
                    if id_mapping or none_id_updates:
                        for row in rows:
                            row_id = row.get('id')
                            if row_id is not None and row_id in id_mapping:
                                row['id'] = id_mapping[row_id]
                            elif row_id is None:
                                for cat_id, entry_date, new_id in none_id_updates:
                                    row_date = row.get('date')
                                    if row.get('category_id') == cat_id and (row_date == entry_date or (hasattr(row_date, 'isoformat') and row_date.isoformat() == entry_date)):
                                        row['id'] = new_id
                                        none_id_updates.remove((cat_id, entry_date, new_id))
                                        break
                        redis_needs_update = True
                        log_info(logger, 'FLUSH', f"Updated entries in rows list with real IDs")
                
                if batch_data:
                    cursor.executemany("""
                        INSERT INTO c_expense_entries (id, category_id, date, amount, recurring_id, is_bucket, original_amount, original_date, processed, bud_item_id, pending, auto_confirmed, is_auto_adjustment)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            category_id = VALUES(category_id),
                            amount = VALUES(amount),
                            processed = VALUES(processed),
                            recurring_id = VALUES(recurring_id),
                            is_bucket = VALUES(is_bucket),
                            original_amount = VALUES(original_amount),
                            original_date = VALUES(original_date),
                            bud_item_id = VALUES(bud_item_id),
                            pending = VALUES(pending),
                            auto_confirmed = VALUES(auto_confirmed),
                            is_auto_adjustment = VALUES(is_auto_adjustment)
                    """, batch_data)
                
                # Update Redis with real IDs if we inserted temp entries
                if redis_needs_update:
                    redis_key = f"c_expense_entries:v1:{user_id}"
                    _redis_client.setex(redis_key, 604800, json.dumps(rows, cls=DecimalEncoder))
                    log_info(logger, 'FLUSH', f"Updated Redis with real IDs for c_expense_entries")
                
                conn.commit()
                cursor.close()
                
                # Clear pending deletions set after successful flush
                _redis_client.delete(pending_key)
                
                log_info(logger, 'FLUSH', f"→ c_expense_entries: {len(batch_data)} rows")
                return len(batch_data)
                
            elif table == 'c_payment_entries':
                # Credit account payment entries table
                
                # First, compare Redis IDs with MySQL IDs and delete orphans
                redis_ids = set(int(row.get('id')) for row in rows if row.get('id') and int(row.get('id')) > 0)
                
                # Get all IDs from MySQL for this user's credit accounts
                cursor.execute("""
                    SELECT cpe.id FROM c_payment_entries cpe
                    JOIN credit_accounts ca ON cpe.account_id = ca.id
                    WHERE ca.user_id = %s
                """, (user_id,))
                mysql_ids = set(row[0] for row in cursor.fetchall())
                
                # Delete records that exist in MySQL but not in Redis (orphan detection)
                ids_to_delete = mysql_ids - redis_ids
                if ids_to_delete:
                    placeholders = ','.join(['%s'] * len(ids_to_delete))
                    cursor.execute(f"""
                        DELETE FROM c_payment_entries WHERE id IN ({placeholders})
                    """, list(ids_to_delete))
                    log_info(logger, 'FLUSH', f"Deleted {len(ids_to_delete)} c_payment_entries orphans from MySQL")
                
                # Also handle pending deletions from the set (if any)
                pending_key = f"pending_deletes:c_payment_entries:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    delete_ids = [int(id_str) for id_str in pending_deletes]
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    cursor.execute(f"""
                        DELETE FROM c_payment_entries WHERE id IN ({placeholders})
                    """, delete_ids)
                    log_info(logger, 'FLUSH', f"Deleted {len(delete_ids)} c_payment_entries from pending set")
                    
                    # CRITICAL: Filter out pending deletes from Redis rows to prevent re-upserting
                    delete_ids_set = set(delete_ids)
                    rows = [r for r in rows if int(r.get('id', 0)) not in delete_ids_set]
                    # Save filtered data back to Redis
                    _redis_client.setex(redis_key, REDIS_TTL, json.dumps(rows, cls=DecimalEncoder))
                    log_info(logger, 'FLUSH', f"Filtered {len(delete_ids)} deleted c_payment_entries from Redis")
                    # Clear the pending deletes set
                    _redis_client.delete(pending_key)
                
                # Now UPSERT the current state from Redis
                batch_data = []
                for row in rows:
                    row_id = int(row.get('id', 0))
                    recurring_id = row.get('recurring_id')
                    if recurring_id is not None:
                        recurring_id = int(recurring_id)
                    
                    # Handle negative IDs (new entries not yet in MySQL)
                    if row_id < 0:
                        batch_data.append((
                            None,  # Let MySQL auto-generate ID
                            row.get('account_id'),
                            row.get('date'),
                            float(row.get('amount', 0)),
                            recurring_id,
                            int(row.get('processed', 0)),
                            int(row.get('auto_confirmed', 0))
                        ))
                    else:
                        batch_data.append((
                            row_id,
                            row.get('account_id'),
                            row.get('date'),
                            float(row.get('amount', 0)),
                            recurring_id,
                            int(row.get('processed', 0)),
                            int(row.get('auto_confirmed', 0))
                        ))
                
                if batch_data:
                    cursor.executemany("""
                        INSERT INTO c_payment_entries (id, account_id, date, amount, recurring_id, processed, auto_confirmed)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            amount = VALUES(amount),
                            processed = VALUES(processed),
                            recurring_id = VALUES(recurring_id),
                            auto_confirmed = VALUES(auto_confirmed)
                    """, batch_data)
                
                conn.commit()
                cursor.close()
                
                # Clear pending deletions set after successful flush
                _redis_client.delete(pending_key)
                
                log_info(logger, 'FLUSH', f"→ c_payment_entries: {len(batch_data)} rows")
                return len(batch_data)
                
            elif table == 'recurring_income':
                # Recurring income table
                
                # First, compare Redis IDs with MySQL IDs and delete orphans
                redis_ids = set(int(row.get('id')) for row in rows if row.get('id') and int(row.get('id')) > 0)
                
                # Get all IDs from MySQL for this user
                cursor.execute("SELECT id FROM recurring_income WHERE user_id = %s", (user_id,))
                mysql_ids = set(row[0] for row in cursor.fetchall())
                
                # Delete records that exist in MySQL but not in Redis
                ids_to_delete = mysql_ids - redis_ids
                if ids_to_delete:
                    placeholders = ','.join(['%s'] * len(ids_to_delete))
                    cursor.execute(f"""
                        DELETE FROM recurring_income WHERE id IN ({placeholders}) AND user_id = %s
                    """, list(ids_to_delete) + [user_id])
                    log_info(logger, 'FLUSH', f"Deleted {len(ids_to_delete)} recurring_income from MySQL (removed from Redis)")
                
                # Also handle pending deletions from the set (if any)
                pending_key = f"pending_deletes:recurring_income:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    delete_ids = [int(id_str) for id_str in pending_deletes]
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    cursor.execute(f"""
                        DELETE FROM recurring_income WHERE id IN ({placeholders}) AND user_id = %s
                    """, delete_ids + [user_id])
                    log_info(logger, 'FLUSH', f"Deleted {len(delete_ids)} recurring_income from pending set")
                    
                    # CRITICAL: Filter out pending deletes from Redis rows to prevent re-upserting
                    delete_ids_set = set(delete_ids)
                    rows = [r for r in rows if int(r.get('id', 0)) not in delete_ids_set]
                    # Save filtered data back to Redis
                    _redis_client.setex(redis_key, REDIS_TTL, json.dumps(rows, cls=DecimalEncoder))
                    log_info(logger, 'FLUSH', f"Filtered {len(delete_ids)} deleted recurring_income from Redis")
                    # Clear the pending deletes set
                    _redis_client.delete(pending_key)
                
                # Now UPSERT the current state from Redis
                # Track temp IDs for later resolution
                temp_id_to_row = {}  # temp_id -> row data for resolving after insert
                batch_data = []
                for row in rows:
                    # Skip temporary negative IDs - they'll be handled as INSERTs
                    row_id = int(row.get('id', 0))  # Convert to int for comparison
                    if row_id < 0:
                        # Track this temp ID for later resolution
                        temp_id_to_row[row_id] = row
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
                            row.get('end_date'),
                            int(row.get('wage_bill', 0))
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
                            row.get('end_date'),
                            int(row.get('wage_bill', 0))
                        ))
                
                if batch_data:
                    cursor.executemany("""
                        INSERT INTO recurring_income (id, user_id, category_id, amount, cadence_interval, cadence_unit, 
                                                     weekdays, monthly_days, yearly_day, yearly_month, start_date, end_date, wage_bill)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                            end_date = VALUES(end_date),
                            wage_bill = VALUES(wage_bill)
                    """, batch_data)
                
                conn.commit()
                
                # CRITICAL: Resolve temp IDs to real MySQL IDs
                if temp_id_to_row:
                    log_info(logger, 'FLUSH', f"Resolving {len(temp_id_to_row)} temp recurring_income IDs")
                    
                    # Query MySQL to find newly inserted records by matching category_id + start_date
                    for temp_id, row_data in temp_id_to_row.items():
                        category_id = row_data.get('category_id')
                        start_date = row_data.get('start_date')
                        amount = row_data.get('amount')
                        
                        cursor.execute("""
                            SELECT id FROM recurring_income 
                            WHERE user_id = %s AND category_id = %s AND start_date = %s AND amount = %s
                            ORDER BY id DESC LIMIT 1
                        """, (user_id, category_id, start_date, float(amount) if amount else 0))
                        
                        result = cursor.fetchone()
                        if result:
                            new_mysql_id = result[0]
                            log_info(logger, 'FLUSH', f"Resolved recurring_income temp_id {temp_id} -> MySQL id {new_mysql_id}")
                            
                            # Update income_entries in Redis to use the new ID
                            entries_key = f"income_entries:v1:{user_id}"
                            entries_data = _redis_client.get(entries_key)
                            if entries_data:
                                entries = json.loads(entries_data)
                                updated_count = 0
                                for entry in entries:
                                    if entry.get('recurring_id') == temp_id:
                                        entry['recurring_id'] = new_mysql_id
                                        updated_count += 1
                                if updated_count > 0:
                                    _redis_client.setex(entries_key, 604800, json.dumps(entries))
                                    # Mark income_entries as dirty so they get re-flushed with correct recurring_id
                                    _redis_client.sadd(f"dirty_tables:{user_id}", 'income_entries')
                                    _redis_client.expire(f"dirty_tables:{user_id}", 604800)
                                    log_info(logger, 'FLUSH', f"Updated {updated_count} income_entries with new recurring_id {new_mysql_id}")
                            
                            # Update recurring_income_buckets in Redis to use the new ID
                            buckets_key = f"recurring_income_buckets:v1:{user_id}"
                            buckets_data = _redis_client.get(buckets_key)
                            if buckets_data:
                                buckets = json.loads(buckets_data)
                                bucket_updated_count = 0
                                for bucket in buckets:
                                    if bucket.get('recurring_id') == temp_id:
                                        bucket['recurring_id'] = new_mysql_id
                                        bucket_updated_count += 1
                                if bucket_updated_count > 0:
                                    _redis_client.setex(buckets_key, 604800, json.dumps(buckets))
                                    _redis_client.sadd(f"dirty_tables:{user_id}", 'recurring_income_buckets')
                                    _redis_client.expire(f"dirty_tables:{user_id}", 604800)
                                    log_info(logger, 'FLUSH', f"Updated {bucket_updated_count} recurring_income_buckets with new recurring_id {new_mysql_id}")
                            
                            # Also update the recurring record in Redis with the new ID
                            recurring_key = f"recurring_income:v1:{user_id}"
                            recurring_data = _redis_client.get(recurring_key)
                            if recurring_data:
                                recurring_records = json.loads(recurring_data)
                                for rec in recurring_records:
                                    if rec.get('id') == temp_id:
                                        rec['id'] = new_mysql_id
                                        log_info(logger, 'FLUSH', f"Updated recurring_income record id from {temp_id} to {new_mysql_id}")
                                        break
                                _redis_client.setex(recurring_key, 604800, json.dumps(recurring_records))
                        else:
                            log_warning(logger, 'FLUSH', f"Could not find MySQL id for temp recurring_income {temp_id}")
                
                cursor.close()
                
                # Clear pending deletions set after successful flush
                _redis_client.delete(pending_key)
                
                log_info(logger, 'FLUSH', f"→ recurring_income: {len(batch_data)} rows")
                return len(batch_data)
                
            elif table == 'recurring_expense':
                # Recurring expense table
                
                # First, compare Redis IDs with MySQL IDs and delete orphans
                redis_ids = set(int(row.get('id')) for row in rows if row.get('id') and int(row.get('id')) > 0)
                
                # Get all IDs from MySQL for this user
                cursor.execute("SELECT id FROM recurring_expense WHERE user_id = %s", (user_id,))
                mysql_ids = set(row[0] for row in cursor.fetchall())
                
                # Delete records that exist in MySQL but not in Redis
                ids_to_delete = mysql_ids - redis_ids
                if ids_to_delete:
                    placeholders = ','.join(['%s'] * len(ids_to_delete))
                    cursor.execute(f"""
                        DELETE FROM recurring_expense WHERE id IN ({placeholders}) AND user_id = %s
                    """, list(ids_to_delete) + [user_id])
                    log_info(logger, 'FLUSH', f"Deleted {len(ids_to_delete)} recurring_expense from MySQL (removed from Redis)")
                
                # Also handle pending deletions from the set (if any)
                pending_key = f"pending_deletes:recurring_expense:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    delete_ids = [int(id_str) for id_str in pending_deletes]
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    cursor.execute(f"""
                        DELETE FROM recurring_expense WHERE id IN ({placeholders}) AND user_id = %s
                    """, delete_ids + [user_id])
                    log_info(logger, 'FLUSH', f"Deleted {len(delete_ids)} recurring_expense from pending set")
                    
                    # CRITICAL: Also remove from Redis rows (may have been rehydrated from MySQL)
                    delete_ids_set = set(delete_ids)
                    original_count = len(rows)
                    rows = [r for r in rows if int(r.get('id', 0)) not in delete_ids_set]
                    if len(rows) < original_count:
                        # Save filtered data back to Redis
                        redis_key = _get_redis_key(table, user_id)
                        if rows:
                            _redis_client.setex(redis_key, INACTIVITY_TIMEOUT + 60, json.dumps(rows, cls=DecimalEncoder))
                        else:
                            _redis_client.delete(redis_key)
                        log_info(logger, 'FLUSH', f"Also removed {original_count - len(rows)} pending-delete records from Redis")
                
                # Now UPSERT the current state from Redis
                # Track temp IDs for later resolution
                temp_id_to_row = {}  # temp_id -> row data for resolving after insert
                batch_data = []
                for row in rows:
                    # Skip temporary negative IDs - they'll be handled as INSERTs
                    row_id = int(row.get('id', 0))  # Convert to int for comparison
                    if row_id < 0:
                        # Track this temp ID for later resolution
                        temp_id_to_row[row_id] = row
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
                            row.get('end_date'),
                            int(row.get('wage_bill', 0))
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
                            row.get('end_date'),
                            int(row.get('wage_bill', 0))
                        ))
                
                if batch_data:
                    cursor.executemany("""
                        INSERT INTO recurring_expense (id, user_id, category_id, amount, cadence_interval, cadence_unit, 
                                                      weekdays, monthly_days, yearly_day, yearly_month, start_date, end_date, wage_bill)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                            end_date = VALUES(end_date),
                            wage_bill = VALUES(wage_bill)
                    """, batch_data)
                
                conn.commit()
                
                # CRITICAL: Resolve temp IDs to real MySQL IDs
                if temp_id_to_row:
                    log_info(logger, 'FLUSH', f"Resolving {len(temp_id_to_row)} temp recurring_expense IDs")
                    
                    # Query MySQL to find newly inserted records by matching category_id + start_date
                    for temp_id, row_data in temp_id_to_row.items():
                        category_id = row_data.get('category_id')
                        start_date = row_data.get('start_date')
                        amount = row_data.get('amount')
                        
                        cursor.execute("""
                            SELECT id FROM recurring_expense 
                            WHERE user_id = %s AND category_id = %s AND start_date = %s AND amount = %s
                            ORDER BY id DESC LIMIT 1
                        """, (user_id, category_id, start_date, float(amount) if amount else 0))
                        
                        result = cursor.fetchone()
                        if result:
                            new_mysql_id = result[0]
                            log_info(logger, 'FLUSH', f"Resolved recurring_expense temp_id {temp_id} -> MySQL id {new_mysql_id}")
                            
                            # Update expense_entries in Redis to use the new ID
                            entries_key = f"expense_entries:v1:{user_id}"
                            entries_data = _redis_client.get(entries_key)
                            if entries_data:
                                entries = json.loads(entries_data)
                                updated_count = 0
                                for entry in entries:
                                    if entry.get('recurring_id') == temp_id:
                                        entry['recurring_id'] = new_mysql_id
                                        updated_count += 1
                                if updated_count > 0:
                                    _redis_client.setex(entries_key, 604800, json.dumps(entries))
                                    # Mark expense_entries as dirty so they get re-flushed with correct recurring_id
                                    _redis_client.sadd(f"dirty_tables:{user_id}", 'expense_entries')
                                    _redis_client.expire(f"dirty_tables:{user_id}", 604800)
                                    log_info(logger, 'FLUSH', f"Updated {updated_count} expense_entries with new recurring_id {new_mysql_id}")
                            
                            # Update recurring_expense_buckets in Redis to use the new ID
                            buckets_key = f"recurring_expense_buckets:v1:{user_id}"
                            buckets_data = _redis_client.get(buckets_key)
                            if buckets_data:
                                buckets = json.loads(buckets_data)
                                bucket_updated_count = 0
                                for bucket in buckets:
                                    if bucket.get('recurring_id') == temp_id:
                                        bucket['recurring_id'] = new_mysql_id
                                        bucket_updated_count += 1
                                if bucket_updated_count > 0:
                                    _redis_client.setex(buckets_key, 604800, json.dumps(buckets))
                                    _redis_client.sadd(f"dirty_tables:{user_id}", 'recurring_expense_buckets')
                                    _redis_client.expire(f"dirty_tables:{user_id}", 604800)
                                    log_info(logger, 'FLUSH', f"Updated {bucket_updated_count} recurring_expense_buckets with new recurring_id {new_mysql_id}")
                            
                            # Update c_payment_entries in Redis to use the new recurring_id
                            payments_key = f"c_payment_entries:v1:{user_id}"
                            payments_data = _redis_client.get(payments_key)
                            if payments_data:
                                payments = json.loads(payments_data)
                                payment_updated_count = 0
                                for payment in payments:
                                    if payment.get('recurring_id') == temp_id:
                                        payment['recurring_id'] = new_mysql_id
                                        payment_updated_count += 1
                                if payment_updated_count > 0:
                                    _redis_client.setex(payments_key, 604800, json.dumps(payments))
                                    _redis_client.sadd(f"dirty_tables:{user_id}", 'c_payment_entries')
                                    _redis_client.expire(f"dirty_tables:{user_id}", 604800)
                                    log_info(logger, 'FLUSH', f"Updated {payment_updated_count} c_payment_entries with new recurring_id {new_mysql_id}")
                            
                            # Also update the recurring record in Redis with the new ID
                            recurring_key = f"recurring_expense:v1:{user_id}"
                            recurring_data = _redis_client.get(recurring_key)
                            if recurring_data:
                                recurring_records = json.loads(recurring_data)
                                for rec in recurring_records:
                                    if rec.get('id') == temp_id:
                                        rec['id'] = new_mysql_id
                                        log_info(logger, 'FLUSH', f"Updated recurring_expense record id from {temp_id} to {new_mysql_id}")
                                        break
                                _redis_client.setex(recurring_key, 604800, json.dumps(recurring_records))
                        else:
                            log_warning(logger, 'FLUSH', f"Could not find MySQL id for temp recurring_expense {temp_id}")
                
                cursor.close()
                
                # Clear pending deletions set after successful flush
                _redis_client.delete(pending_key)
                
                log_info(logger, 'FLUSH', f"→ recurring_expense: {len(batch_data)} rows")
                return len(batch_data)
                
            elif table == 'recurring_c_expense':
                # Recurring credit account expense table
                
                # First, compare Redis IDs with MySQL IDs and delete orphans
                redis_ids = set(int(row.get('id')) for row in rows if row.get('id') and int(row.get('id')) > 0)
                
                # Get all IDs from MySQL for this user
                cursor.execute("SELECT id FROM recurring_c_expense WHERE user_id = %s", (user_id,))
                mysql_ids = set(row[0] for row in cursor.fetchall())
                
                # Delete records that exist in MySQL but not in Redis
                ids_to_delete = mysql_ids - redis_ids
                if ids_to_delete:
                    placeholders = ','.join(['%s'] * len(ids_to_delete))
                    cursor.execute(f"""
                        DELETE FROM recurring_c_expense WHERE id IN ({placeholders}) AND user_id = %s
                    """, list(ids_to_delete) + [user_id])
                    log_info(logger, 'FLUSH', f"Deleted {len(ids_to_delete)} recurring_c_expense from MySQL (removed from Redis)")
                
                # Also handle pending deletions from the set (if any)
                pending_key = f"pending_deletes:recurring_c_expense:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    delete_ids = [int(id_str) for id_str in pending_deletes]
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    cursor.execute(f"""
                        DELETE FROM recurring_c_expense WHERE id IN ({placeholders}) AND user_id = %s
                    """, delete_ids + [user_id])
                    log_info(logger, 'FLUSH', f"Deleted {len(delete_ids)} recurring_c_expense from pending set")
                    
                    # CRITICAL: Filter out pending deletes from Redis rows to prevent re-upserting
                    delete_ids_set = set(delete_ids)
                    rows = [r for r in rows if int(r.get('id', 0)) not in delete_ids_set]
                    # Save filtered data back to Redis
                    _redis_client.setex(redis_key, REDIS_TTL, json.dumps(rows, cls=DecimalEncoder))
                    log_info(logger, 'FLUSH', f"Filtered {len(delete_ids)} deleted recurring_c_expense from Redis")
                    # Clear the pending deletes set
                    _redis_client.delete(pending_key)
                
                # Get valid category IDs from MySQL to filter out entries for deleted categories
                cursor.execute("""
                    SELECT cec.id 
                    FROM c_expense_categories cec
                    JOIN credit_accounts ca ON cec.account_id = ca.id
                    WHERE ca.user_id = %s
                """, (user_id,))
                valid_category_ids = set(row[0] for row in cursor.fetchall())
                log_info(logger, 'FLUSH', f"Found {len(valid_category_ids)} valid credit expense categories for user {user_id}")

                # Track rows with negative IDs for later mapping
                temp_id_to_row_idx = {}  # Map temp negative ID to row index
                batch_data = []
                skipped_temp_category = 0
                skipped_deleted_category = 0
                
                for idx, row in enumerate(rows):
                    # Skip temporary negative IDs - they'll be handled as INSERTs
                    row_id = int(row.get('id', 0))  # Convert to int for comparison
                    category_id = row.get('category_id')
                    
                    # Skip rows with temp negative category_id - category hasn't been flushed yet
                    if category_id and int(category_id) < 0:
                        skipped_temp_category += 1
                        continue
                    
                    # Skip rows with deleted category_id - category was deleted
                    if category_id and int(category_id) not in valid_category_ids:
                        skipped_deleted_category += 1
                        log_info(logger, 'FLUSH', f"Skipping recurring_c_expense for deleted category {category_id}, recurring_id={row_id}")
                        continue
                    
                    # Track temp IDs for mapping later
                    if row_id < 0:
                        temp_id_to_row_idx[row_id] = idx
                    
                    if row_id < 0:
                        batch_data.append((
                            None,  # Let MySQL auto-generate
                            user_id,
                            category_id,
                            float(row.get('amount', 0)),
                            row.get('cadence_interval', 1),
                            row.get('cadence_unit', 'days'),
                            row.get('weekdays'),
                            row.get('monthly_days'),
                            row.get('yearly_day'),
                            row.get('yearly_month'),
                            row.get('start_date'),
                            row.get('end_date'),
                            int(row.get('wage_bill', 0))
                        ))
                    else:
                        batch_data.append((
                            row.get('id'),
                            user_id,
                            category_id,
                            float(row.get('amount', 0)),
                            row.get('cadence_interval', 1),
                            row.get('cadence_unit', 'days'),
                            row.get('weekdays'),
                            row.get('monthly_days'),
                            row.get('yearly_day'),
                            row.get('yearly_month'),
                            row.get('start_date'),
                            row.get('end_date'),
                            int(row.get('wage_bill', 0))
                        ))
                
                if skipped_temp_category > 0:
                    log_info(logger, 'FLUSH', f"Skipped {skipped_temp_category} recurring_c_expense with temp category_id")
                
                if skipped_deleted_category > 0:
                    log_info(logger, 'FLUSH', f"Skipped {skipped_deleted_category} recurring_c_expense with deleted category_id")
                
                if batch_data:
                    cursor.executemany("""
                        INSERT INTO recurring_c_expense (id, user_id, category_id, amount, cadence_interval, cadence_unit, 
                                                        weekdays, monthly_days, yearly_day, yearly_month, start_date, end_date, wage_bill)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                            end_date = VALUES(end_date),
                            wage_bill = VALUES(wage_bill)
                    """, batch_data)
                    
                    # If we had temp IDs, fetch the real MySQL IDs and update Redis
                    if temp_id_to_row_idx:
                        # Query all recurring_c_expense for this user to get the ID mappings
                        cursor.execute("""
                            SELECT rce.id, rce.category_id, rce.amount, rce.start_date, rce.end_date
                            FROM recurring_c_expense rce
                            WHERE rce.user_id = %s
                        """, (user_id,))
                        
                        mysql_records = cursor.fetchall()
                        
                        # Match MySQL records to Redis rows by unique attributes
                        for mysql_id, cat_id, amt, start_dt, end_dt in mysql_records:
                            # Find matching row in Redis with negative ID
                            for temp_id, row_idx in temp_id_to_row_idx.items():
                                row = rows[row_idx]
                                # Match by category_id, amount, start_date, end_date
                                if (int(row.get('category_id', 0)) == cat_id and
                                    float(row.get('amount', 0)) == float(amt) and
                                    str(row.get('start_date', '')) == str(start_dt) and
                                    str(row.get('end_date', '')) == str(end_dt)):
                                    
                                    old_id = row.get('id')
                                    rows[row_idx]['id'] = mysql_id
                                    log_info(logger, 'FLUSH', f"Updated recurring_c_expense Redis ID: {old_id} -> {mysql_id}")
                                    break
                        
                        # Write updated rows back to Redis with real IDs
                        redis_key = _get_redis_key(table, user_id)
                        _redis_client.setex(
                            redis_key,
                            INACTIVITY_TIMEOUT + 60,
                            json.dumps(rows, cls=DecimalEncoder)
                        )
                        log_info(logger, 'FLUSH', f"Updated Redis cache for recurring_c_expense with real MySQL IDs")
                        
                        # CRITICAL: Also update c_expense_entries with new recurring_id
                        entries_key = f"c_expense_entries:v1:{user_id}"
                        entries_data = _redis_client.get(entries_key)
                        if entries_data:
                            entries = json.loads(entries_data)
                            entries_updated = False
                            # Build mapping of old temp_id -> new mysql_id
                            for temp_id, row_idx in temp_id_to_row_idx.items():
                                new_id = rows[row_idx].get('id')
                                if new_id and new_id != temp_id:
                                    updated_count = 0
                                    for entry in entries:
                                        if entry.get('recurring_id') == temp_id:
                                            entry['recurring_id'] = new_id
                                            updated_count += 1
                                    if updated_count > 0:
                                        entries_updated = True
                                        log_info(logger, 'FLUSH', f"Updated {updated_count} c_expense_entries recurring_id from {temp_id} to {new_id}")
                            _redis_client.setex(entries_key, 604800, json.dumps(entries))
                            if entries_updated:
                                # Mark c_expense_entries as dirty so they get re-flushed with correct recurring_id
                                _redis_client.sadd(f"dirty_tables:{user_id}", 'c_expense_entries')
                                _redis_client.expire(f"dirty_tables:{user_id}", 604800)
                        
                        # Also update recurring_c_expense_buckets with new recurring_id
                        buckets_key = f"recurring_c_expense_buckets:v1:{user_id}"
                        buckets_data = _redis_client.get(buckets_key)
                        if buckets_data:
                            buckets = json.loads(buckets_data)
                            buckets_updated = False
                            for temp_id, row_idx in temp_id_to_row_idx.items():
                                new_id = rows[row_idx].get('id')
                                if new_id and new_id != temp_id:
                                    bucket_updated_count = 0
                                    for bucket in buckets:
                                        if bucket.get('recurring_id') == temp_id:
                                            bucket['recurring_id'] = new_id
                                            bucket_updated_count += 1
                                    if bucket_updated_count > 0:
                                        buckets_updated = True
                                        log_info(logger, 'FLUSH', f"Updated {bucket_updated_count} recurring_c_expense_buckets recurring_id from {temp_id} to {new_id}")
                            _redis_client.setex(buckets_key, 604800, json.dumps(buckets))
                            if buckets_updated:
                                _redis_client.sadd(f"dirty_tables:{user_id}", 'recurring_c_expense_buckets')
                                _redis_client.expire(f"dirty_tables:{user_id}", 604800)
                
                conn.commit()
                cursor.close()
                
                # Clear pending deletions set after successful flush
                _redis_client.delete(pending_key)
                
                log_info(logger, 'FLUSH', f"→ recurring_c_expense: {len(batch_data)} rows")
                return len(batch_data)
                
            elif table == 'recurring_income_buckets':
                # Recurring income buckets table
                
                # First, compare Redis IDs with MySQL IDs and delete orphans
                redis_ids = set(int(row.get('id')) for row in rows if row.get('id') is not None and int(row.get('id')) > 0)
                
                # Get all IDs from MySQL for this user
                cursor.execute("SELECT id FROM recurring_income_buckets WHERE user_id = %s", (user_id,))
                mysql_ids = set(row[0] for row in cursor.fetchall())
                
                # Delete records that exist in MySQL but not in Redis
                ids_to_delete = mysql_ids - redis_ids
                if ids_to_delete:
                    placeholders = ','.join(['%s'] * len(ids_to_delete))
                    cursor.execute(f"""
                        DELETE FROM recurring_income_buckets WHERE id IN ({placeholders}) AND user_id = %s
                    """, list(ids_to_delete) + [user_id])
                    log_info(logger, 'FLUSH', f"Deleted {len(ids_to_delete)} recurring_income_buckets from MySQL (removed from Redis)")
                
                # Also handle pending deletions from the set (if any)
                pending_key = f"pending_deletes:recurring_income_buckets:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    delete_ids = [int(id_str) for id_str in pending_deletes]
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    cursor.execute(f"""
                        DELETE FROM recurring_income_buckets WHERE id IN ({placeholders}) AND user_id = %s
                    """, delete_ids + [user_id])
                    log_info(logger, 'FLUSH', f"Deleted {len(delete_ids)} recurring_income_buckets from pending set")
                    
                    # CRITICAL: Filter out pending deletes from Redis rows to prevent re-upserting
                    delete_ids_set = set(delete_ids)
                    rows = [r for r in rows if int(r.get('id', 0)) not in delete_ids_set]
                    # Save filtered data back to Redis
                    _redis_client.setex(redis_key, REDIS_TTL, json.dumps(rows, cls=DecimalEncoder))
                    log_info(logger, 'FLUSH', f"Filtered {len(delete_ids)} deleted recurring_income_buckets from Redis")
                    # Clear the pending deletes set
                    _redis_client.delete(pending_key)
                
                # Now UPSERT the current state from Redis
                # Split into updates (positive IDs) and inserts (negative IDs)
                update_data = []
                insert_data = []
                skipped_rows = []
                
                for row in rows:
                    category_id = row.get('category_id')
                    
                    # Skip rows with temporary negative category_id - category hasn't been flushed yet
                    if category_id is not None and int(category_id) < 0:
                        skipped_rows.append(row)
                        continue
                    
                    row_id = row.get('id')
                    if row_id is not None and int(row_id) > 0:
                        # Existing record with real ID - update
                        update_data.append((
                            int(row_id),
                            user_id,
                            int(category_id) if category_id is not None else None,
                            row.get('bucket_date'),
                            float(row.get('amount', 0)),
                            float(row.get('original_amount', 0))
                        ))
                    else:
                        # New record with temp negative ID - insert without ID
                        insert_data.append((
                            user_id,
                            int(category_id) if category_id is not None else None,
                            row.get('bucket_date'),
                            float(row.get('amount', 0)),
                            float(row.get('original_amount', 0))
                        ))
                
                if skipped_rows:
                    log_info(logger, 'FLUSH', f"Skipped {len(skipped_rows)} recurring_income_buckets with temp category_ids - will retry next flush")
                
                if update_data:
                    cursor.executemany("""
                        INSERT INTO recurring_income_buckets (id, user_id, category_id, bucket_date, amount, original_amount)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            amount = VALUES(amount),
                            original_amount = VALUES(original_amount)
                    """, update_data)
                
                if insert_data:
                    cursor.executemany("""
                        INSERT INTO recurring_income_buckets (user_id, category_id, bucket_date, amount, original_amount)
                        VALUES (%s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            amount = VALUES(amount),
                            original_amount = VALUES(original_amount)
                    """, insert_data)
                
                conn.commit()
                cursor.close()
                
                # Clear pending deletions set after successful flush
                _redis_client.delete(pending_key)
                
                total_rows = len(update_data) + len(insert_data)
                log_info(logger, 'FLUSH', f"→ recurring_income_buckets: {total_rows} rows (updates: {len(update_data)}, inserts: {len(insert_data)})")
                return total_rows
                
            elif table == 'recurring_expense_buckets':
                # Recurring expense buckets table
                
                # First, compare Redis IDs with MySQL IDs and delete orphans
                redis_ids = set(int(row.get('id')) for row in rows if row.get('id') is not None and int(row.get('id')) > 0)
                
                # Get all IDs from MySQL for this user
                cursor.execute("SELECT id FROM recurring_expense_buckets WHERE user_id = %s", (user_id,))
                mysql_ids = set(row[0] for row in cursor.fetchall())
                
                # Delete records that exist in MySQL but not in Redis
                ids_to_delete = mysql_ids - redis_ids
                if ids_to_delete:
                    placeholders = ','.join(['%s'] * len(ids_to_delete))
                    cursor.execute(f"""
                        DELETE FROM recurring_expense_buckets WHERE id IN ({placeholders}) AND user_id = %s
                    """, list(ids_to_delete) + [user_id])
                    log_info(logger, 'FLUSH', f"Deleted {len(ids_to_delete)} recurring_expense_buckets from MySQL (removed from Redis)")
                
                # Also handle pending deletions from the set (if any)
                pending_key = f"pending_deletes:recurring_expense_buckets:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    delete_ids = [int(id_str) for id_str in pending_deletes]
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    cursor.execute(f"""
                        DELETE FROM recurring_expense_buckets WHERE id IN ({placeholders}) AND user_id = %s
                    """, delete_ids + [user_id])
                    log_info(logger, 'FLUSH', f"Deleted {len(delete_ids)} recurring_expense_buckets from pending set")
                    
                    # CRITICAL: Filter out pending deletes from Redis rows to prevent re-upserting
                    delete_ids_set = set(delete_ids)
                    rows = [r for r in rows if int(r.get('id', 0)) not in delete_ids_set]
                    # Save filtered data back to Redis
                    _redis_client.setex(redis_key, REDIS_TTL, json.dumps(rows, cls=DecimalEncoder))
                    log_info(logger, 'FLUSH', f"Filtered {len(delete_ids)} deleted recurring_expense_buckets from Redis")
                    # Clear the pending deletes set
                    _redis_client.delete(pending_key)
                
                # Now UPSERT the current state from Redis
                # Split into updates (positive IDs) and inserts (negative IDs)
                update_data = []
                insert_data = []
                skipped_rows = []
                
                for row in rows:
                    category_id = row.get('category_id')
                    
                    # Skip rows with temporary negative category_id - category hasn't been flushed yet
                    if category_id is not None and int(category_id) < 0:
                        skipped_rows.append(row)
                        continue
                    
                    row_id = row.get('id')
                    if row_id is not None and int(row_id) > 0:
                        # Existing record with real ID - update
                        update_data.append((
                            int(row_id),
                            user_id,
                            int(category_id) if category_id is not None else None,
                            row.get('bucket_date'),
                            float(row.get('amount', 0)),
                            float(row.get('original_amount', 0))
                        ))
                    else:
                        # New record with temp negative ID - insert without ID
                        insert_data.append((
                            user_id,
                            int(category_id) if category_id is not None else None,
                            row.get('bucket_date'),
                            float(row.get('amount', 0)),
                            float(row.get('original_amount', 0))
                        ))
                
                if skipped_rows:
                    log_info(logger, 'FLUSH', f"Skipped {len(skipped_rows)} recurring_expense_buckets with temp category_ids - will retry next flush")
                
                if update_data:
                    cursor.executemany("""
                        INSERT INTO recurring_expense_buckets (id, user_id, category_id, bucket_date, amount, original_amount)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            amount = VALUES(amount),
                            original_amount = VALUES(original_amount)
                    """, update_data)
                
                if insert_data:
                    cursor.executemany("""
                        INSERT INTO recurring_expense_buckets (user_id, category_id, bucket_date, amount, original_amount)
                        VALUES (%s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            amount = VALUES(amount),
                            original_amount = VALUES(original_amount)
                    """, insert_data)
                
                conn.commit()
                cursor.close()
                
                # Clear pending deletions set after successful flush
                _redis_client.delete(pending_key)
                
                total_rows = len(update_data) + len(insert_data)
                log_info(logger, 'FLUSH', f"→ recurring_expense_buckets: {total_rows} rows (updates: {len(update_data)}, inserts: {len(insert_data)})")
                return total_rows
                
            elif table == 'recurring_c_expense_buckets':
                # Recurring credit account expense buckets table
                
                # First, compare Redis IDs with MySQL IDs and delete orphans
                redis_ids = set(int(row.get('id')) for row in rows if row.get('id') is not None and int(row.get('id')) > 0)
                
                # Get all IDs from MySQL for this user
                cursor.execute("SELECT id FROM recurring_c_expense_buckets WHERE user_id = %s", (user_id,))
                mysql_ids = set(row[0] for row in cursor.fetchall())
                
                # Delete records that exist in MySQL but not in Redis
                ids_to_delete = mysql_ids - redis_ids
                if ids_to_delete:
                    placeholders = ','.join(['%s'] * len(ids_to_delete))
                    cursor.execute(f"""
                        DELETE FROM recurring_c_expense_buckets WHERE id IN ({placeholders}) AND user_id = %s
                    """, list(ids_to_delete) + [user_id])
                    log_info(logger, 'FLUSH', f"Deleted {len(ids_to_delete)} recurring_c_expense_buckets from MySQL (removed from Redis)")
                
                # Also handle pending deletions from the set (if any)
                pending_key = f"pending_deletes:recurring_c_expense_buckets:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    delete_ids = [int(id_str) for id_str in pending_deletes]
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    cursor.execute(f"""
                        DELETE FROM recurring_c_expense_buckets WHERE id IN ({placeholders}) AND user_id = %s
                    """, delete_ids + [user_id])
                    log_info(logger, 'FLUSH', f"Deleted {len(delete_ids)} recurring_c_expense_buckets from pending set")
                    
                    # CRITICAL: Filter out pending deletes from Redis rows to prevent re-upserting
                    delete_ids_set = set(delete_ids)
                    rows = [r for r in rows if int(r.get('id', 0)) not in delete_ids_set]
                    # Save filtered data back to Redis
                    _redis_client.setex(redis_key, REDIS_TTL, json.dumps(rows, cls=DecimalEncoder))
                    log_info(logger, 'FLUSH', f"Filtered {len(delete_ids)} deleted recurring_c_expense_buckets from Redis")
                    # Clear the pending deletes set
                    _redis_client.delete(pending_key)
                
                # Now UPSERT the current state from Redis
                # Split into updates (positive IDs) and inserts (negative IDs)
                update_data = []
                insert_data = []
                skipped_rows = []
                
                for row in rows:
                    category_id = row.get('category_id')
                    
                    # Skip rows with temporary negative category_id - category hasn't been flushed yet
                    if category_id is not None and int(category_id) < 0:
                        skipped_rows.append(row)
                        continue
                    
                    row_id = row.get('id')
                    if row_id is not None and int(row_id) > 0:
                        # Existing record with real ID - update
                        update_data.append((
                            int(row_id),
                            user_id,
                            row.get('account_id'),
                            int(category_id) if category_id is not None else None,
                            row.get('bucket_date'),
                            float(row.get('amount', 0)),
                            float(row.get('original_amount', 0))
                        ))
                    else:
                        # New record with temp negative ID - insert without ID
                        insert_data.append((
                            user_id,
                            row.get('account_id'),
                            int(category_id) if category_id is not None else None,
                            row.get('bucket_date'),
                            float(row.get('amount', 0)),
                            float(row.get('original_amount', 0))
                        ))
                
                if skipped_rows:
                    log_info(logger, 'FLUSH', f"Skipped {len(skipped_rows)} recurring_c_expense_buckets with temp category_ids - will retry next flush")
                
                if update_data:
                    cursor.executemany("""
                        INSERT INTO recurring_c_expense_buckets (id, user_id, account_id, category_id, bucket_date, amount, original_amount)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            amount = VALUES(amount),
                            original_amount = VALUES(original_amount)
                    """, update_data)
                
                if insert_data:
                    cursor.executemany("""
                        INSERT INTO recurring_c_expense_buckets (user_id, account_id, category_id, bucket_date, amount, original_amount)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            amount = VALUES(amount),
                            original_amount = VALUES(original_amount)
                    """, insert_data)
                
                conn.commit()
                cursor.close()
                
                # Clear pending deletions set after successful flush
                _redis_client.delete(pending_key)
                
                total_rows = len(update_data) + len(insert_data)
                log_info(logger, 'FLUSH', f"→ recurring_c_expense_buckets: {total_rows} rows (updates: {len(update_data)}, inserts: {len(insert_data)})")
                return total_rows
                
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
                    log_info(logger, 'FLUSH', f"Deleted {len(delete_ids)} buds from MySQL")
                
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
                        log_info(logger, 'FLUSH', f"Bud temp ID {old_id} → real ID {new_id}")
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
                    log_info(logger, 'FLUSH', f"Updated {len(temp_id_mappings)} bud temp IDs in Redis")
                    
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
                            log_info(logger, 'FLUSH', f"Updated {updated_count} bud_item bud_id references in Redis")
                
                # Clear pending deletions set after successful flush
                _redis_client.delete(pending_key)
                
                log_info(logger, 'FLUSH', f"→ buds: {len(rows)} rows")
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
                    log_info(logger, 'FLUSH', f"Deleted {len(delete_ids)} bud_items from MySQL")
                
                # Track temp ID to real ID mappings for updating Redis
                temp_id_mappings = {}
                
                # Process bud_items to handle temp IDs
                for row in rows:
                    # Skip if bud_id is negative (temp ID) - parent bud hasn't been flushed yet
                    bud_id_val = row.get('bud_id')
                    if bud_id_val and int(bud_id_val) < 0:
                        log_info(logger, 'FLUSH', f"Skipping bud_item {row.get('id')} - bud_id {bud_id_val} is temp (negative)")
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
                        log_info(logger, 'FLUSH', f"Bud_item temp ID {old_id} → real ID {new_id}")
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
                    log_info(logger, 'FLUSH', f"Updated {len(temp_id_mappings)} bud_item temp IDs in Redis")
                    
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
                                log_info(logger, 'FLUSH', f"Updated {updated_count} bud_item_id references in {table}")
                
                # Clear pending deletions set after successful flush
                _redis_client.delete(pending_key)
                
                log_info(logger, 'FLUSH', f"→ bud_items: {len(rows)} rows")
                return len(rows)
                
            elif table == 'users':
                # Users table - for ALL user settings
                
                if not rows:
                    return 0
                
                # For users, rows is a dict (not a list), since we store a single user object
                user_data = rows if isinstance(rows, dict) else None
                
                if user_data:
                    # Never overwrite identity/password with bad data from cache.
                    # Passwords must be a bcrypt hash string ($2...)
                    cached_username = user_data.get('username')
                    cached_password = user_data.get('password')
                    if not cached_username or not cached_password or not str(cached_password).startswith('$2'):
                        log_warning(logger, 'FLUSH', f"users:v1:{user_id} has invalid username/password payload — skipping flush to protect account integrity")
                        return 0

                    cursor.execute("""
                        UPDATE users
                        SET balance_threshold = %s,
                            starting_savings = %s,
                            password = %s,
                            username = %s,
                            email = %s,
                            mfa_secret = %s,
                            email_notifications = %s,
                            -- COALESCE, not a plain assignment: a cached user
                            -- blob written before this column existed has no key
                            -- for it, and a plain assignment would NULL a saved
                            -- preference on the next flush. NULL here means "the
                            -- cache does not know", while an empty string means
                            -- "nothing is disabled" - see
                            -- notification_kinds.format_disabled.
                            email_notify_disabled = COALESCE(%s, email_notify_disabled),
                            first_name = %s,
                            last_name = %s,
                            goofy_week_mode = %s,
                            landing_page = %s,
                            profile_picture = %s,
                            currency_type = %s,
                            bank_sync_enabled = %s,
                            bank_auto_import = %s,
                            member_since = %s,
                            setup_step = %s,
                            completed_tutorials = %s
                        WHERE id = %s
                    """, (
                        float(_coerce(user_data.get('balance_threshold'), 0)),
                        float(_coerce(user_data.get('starting_savings'), 0)),
                        user_data.get('password'),
                        user_data.get('username'),
                        # Older cached blobs predate `email` being flushed; fall back to
                        # username rather than NULLing the column (they are kept identical).
                        user_data.get('email') or user_data.get('username'),
                        user_data.get('mfa_secret'),
                        int(_coerce(user_data.get('email_notifications'), 0)),
                        user_data.get('email_notify_disabled'),
                        user_data.get('first_name'),
                        user_data.get('last_name'),
                        int(_coerce(user_data.get('goofy_week_mode'), 0)),
                        user_data.get('landing_page', 'dashboard_3m'),
                        user_data.get('profile_picture'),
                        user_data.get('currency_type', 'USD'),
                        int(_coerce(user_data.get('bank_sync_enabled'), 0)),
                        int(_coerce(user_data.get('bank_auto_import'), 1)),
                        user_data.get('member_since'),
                        int(_coerce(user_data.get('setup_step'), 0)),
                        user_data.get('completed_tutorials'),
                        user_id
                    ))
                    
                    conn.commit()
                    cursor.close()
                    log_info(logger, 'FLUSH', f"→ users: Updated user {user_id} settings (including member_since, goofy_week_mode, landing_page, etc.)")
                    return 1
                
                return 0
                
            elif table == 'linked_provider_profiles':
                # provider profiles table
                if not rows or len(rows) == 0:
                    return 0
                
                profile = rows[0]  # Should only be one profile per user
                cursor.execute("""
                    INSERT INTO linked_provider_profiles (user_id, profile_id, session_token, session_expires_at)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        profile_id = VALUES(profile_id),
                        session_token = VALUES(session_token),
                        session_expires_at = VALUES(session_expires_at)
                """, (
                    user_id,
                    profile.get('profile_id'),
                    profile.get('session_token'),
                    profile.get('session_expires_at')
                ))
                
                conn.commit()
                cursor.close()
                log_info(logger, 'FLUSH', f"→ linked_provider_profiles: Updated profile for user {user_id}")
                return 1
                
            elif table == 'linked_connections':
                # Check if there are pending deletes - if so, skip this flush
                # The deletes will be processed by linked_connections_deleted
                delete_key = f"linked_connections_to_delete:{user_id}"
                pending_deletes = _redis_client.smembers(delete_key) if _redis_client else set()
                if pending_deletes:
                    log_info(logger, 'FLUSH', f"linked_connections: Skipping flush - {len(pending_deletes)} pending deletes")
                    # Don't clear the dirty flag - let the delete handler process first
                    return -1  # Return -1 to indicate skip, don't clear dirty flag
                
                # linked connections table
                if not rows:
                    log_info(logger, 'FLUSH', f"linked_connections: No rows in Redis for user {user_id}")
                    return 0
                
                log_info(logger, 'FLUSH', f"linked_connections: Found {len(rows)} rows in Redis for user {user_id}")
                
                batch_data = []
                provider_id_to_row_idx = {}  # Map linked connection_id to row index
                temp_id_to_provider_id = {}  # Map temp ID to linked connection_id
                
                for idx, row in enumerate(rows):
                    batch_data.append((
                        user_id,
                        row.get('connection_id'),
                        row.get('institution_name'),
                        row.get('institution_id'),
                        row.get('status', 'ACTIVE'),
                        row.get('last_synced_at')
                    ))
                    provider_id_to_row_idx[row.get('connection_id')] = idx
                    # Track temp ID if present
                    temp_id = row.get('id')
                    if temp_id and temp_id >= 100000:  # Looks like a temp ID
                        temp_id_to_provider_id[temp_id] = row.get('connection_id')
                
                log_info(logger, 'FLUSH', f"linked_connections: Executing batch insert of {len(batch_data)} rows")
                
                cursor.executemany("""
                    INSERT INTO linked_connections 
                    (user_id, connection_id, institution_name, institution_id, status, last_synced_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        institution_name = VALUES(institution_name),
                        status = VALUES(status),
                        last_synced_at = VALUES(last_synced_at)
                """, batch_data)
                
                rows_affected = cursor.rowcount
                log_info(logger, 'FLUSH', f"linked_connections: MySQL rowcount = {rows_affected}")
                
                # Get the real MySQL IDs and update Redis cache
                cursor.execute(
                    "SELECT id, connection_id FROM linked_connections WHERE user_id = %s",
                    (user_id,)
                )
                
                temp_to_real_id = {}  # Map temp ID to real MySQL ID
                for mysql_id, provider_conn_id in cursor.fetchall():
                    if provider_conn_id in provider_id_to_row_idx:
                        idx = provider_id_to_row_idx[provider_conn_id]
                        old_id = rows[idx].get('id')
                        rows[idx]['id'] = mysql_id  # Update Redis data with real MySQL ID
                        
                        # Track mapping from temp to real ID
                        if old_id and old_id != mysql_id:
                            temp_to_real_id[old_id] = mysql_id
                
                # Update Redis connections with corrected IDs
                redis_key = _get_redis_key(table, user_id)
                _redis_client.setex(
                    redis_key,
                    INACTIVITY_TIMEOUT + 60,
                    json.dumps(rows, cls=DecimalEncoder)
                )
                
                # Update accounts' connection_id fields if they have temp IDs
                if temp_to_real_id:
                    log_info(logger, 'FLUSH', f"linked_connections: Found temp ID mappings: {temp_to_real_id}")
                    accounts_key = _get_redis_key('linked_accounts', user_id)
                    accounts_data = _redis_client.get(accounts_key)
                    
                    if accounts_data:
                        accounts = json.loads(accounts_data)
                        updated = False
                        
                        for account in accounts:
                            old_conn_id = account.get('connection_id')
                            if old_conn_id in temp_to_real_id:
                                account['connection_id'] = temp_to_real_id[old_conn_id]
                                updated = True
                                log_info(logger, 'FLUSH', f"Updated account {account.get('account_id')} connection_id: {old_conn_id} -> {temp_to_real_id[old_conn_id]}")
                        
                        if updated:
                            _redis_client.setex(
                                accounts_key,
                                INACTIVITY_TIMEOUT + 60,
                                json.dumps(accounts, cls=DecimalEncoder)
                            )
                            # Mark accounts as dirty so they get flushed with correct connection_id
                            _redis_client.sadd(f"dirty_tables:{user_id}", 'linked_accounts')
                            log_info(logger, 'FLUSH', f"Updated {len(accounts)} accounts with real connection IDs, marked linked_accounts dirty")
                else:
                    log_info(logger, 'FLUSH', f"linked_connections: No temp ID mappings needed")
                
                conn.commit()
                cursor.close()
                log_info(logger, 'FLUSH', f"→ linked_connections: {len(batch_data)} rows flushed successfully")
                return len(batch_data)
                
            elif table == 'linked_accounts':
                # linked accounts table
                if not rows:
                    log_info(logger, 'FLUSH', f"linked_accounts: No rows in Redis for user {user_id}")
                    return 0
                
                log_info(logger, 'FLUSH', f"linked_accounts: Found {len(rows)} rows in Redis for user {user_id}")
                
                # Get connection mapping from Redis (should have real MySQL IDs after connection flush)
                connections_key = _get_redis_key('linked_connections', user_id)
                connections_data = _redis_client.get(connections_key)
                
                connection_map = {}  # Map from temp ID to real MySQL ID
                if connections_data:
                    connections = json.loads(connections_data)
                    for conn_row in connections:
                        # Map both temp ID (if exists) and real ID to real ID
                        if conn_row.get('id'):
                            connection_map[conn_row.get('id')] = conn_row.get('id')
                
                log_info(logger, 'FLUSH', f"linked_accounts connection_map has {len(connection_map)} entries")
                
                batch_data = []
                updated_rows = []
                skipped_count = 0
                
                for row in rows:
                    # Handle None values for balances
                    current_bal = row.get('current_balance')
                    available_bal = row.get('available_balance')
                    
                    # Resolve connection_id
                    conn_id = row.get('connection_id')
                    mysql_conn_id = connection_map.get(conn_id, conn_id)
                    
                    # Skip if connection doesn't exist yet
                    if mysql_conn_id and mysql_conn_id >= 1000000:
                        log_warning(logger, 'FLUSH', f"Skipping account {row.get('account_id')} - connection not yet flushed (temp ID: {mysql_conn_id})")
                        updated_rows.append(row)  # Keep in Redis unchanged
                        skipped_count += 1
                        continue
                    
                    # Helper to safely convert string numbers to float
                    def to_float(val):
                        if val is None:
                            return None
                        try:
                            return float(val)
                        except (ValueError, TypeError):
                            return None
                    
                    # Helper to convert Unix timestamp (string) to date
                    def to_date(val):
                        if val is None:
                            return None
                        try:
                            from datetime import datetime
                            # Convert Unix timestamp string to date
                            timestamp = int(val)
                            return datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d')
                        except (ValueError, TypeError):
                            return None
                    
                    batch_data.append((
                        user_id,
                        mysql_conn_id,
                        row.get('account_id'),
                        row.get('account_name', 'Account'),
                        row.get('alias'),
                        row.get('account_type', ''),
                        row.get('account_subtype', ''),
                        row.get('mask', ''),
                        float(current_bal) if current_bal is not None else 0.0,
                        float(available_bal) if available_bal is not None else 0.0,
                        int(row.get('is_active')) if row.get('is_active') is not None else None,
                        int(row.get('sync_transactions')) if row.get('sync_transactions') is not None else None,
                        to_float(row.get('interest_rate')),
                        to_float(row.get('origination_principal')),
                        to_date(row.get('origination_date')),
                        to_date(row.get('maturity_date')),
                        row.get('loan_term'),
                        to_date(row.get('last_payment_date')),
                        to_float(row.get('last_payment_amount')),
                        to_date(row.get('next_payment_due_date')),
                        to_float(row.get('minimum_payment_amount')),
                        to_float(row.get('next_payment_minimum_amount')),
                        row.get('payment_frequency'),
                        row.get('account_state')
                    ))
                    
                    # Update row with real connection_id for Redis
                    row_copy = row.copy()
                    row_copy['connection_id'] = mysql_conn_id
                    updated_rows.append(row_copy)
                
                if skipped_count > 0:
                    log_info(logger, 'FLUSH', f"linked_accounts: Skipped {skipped_count} accounts waiting for connection flush")
                
                if not batch_data:
                    log_info(logger, 'FLUSH', f"linked_accounts: No accounts ready to flush (all waiting for connections)")
                    # Still return 0 so dirty flag stays (accounts need to wait for connections)
                    return 0
                
                # Debug: log connection IDs being used
                conn_ids_used = set(item[1] for item in batch_data)
                log_info(logger, 'FLUSH', f"linked_accounts: Flushing {len(batch_data)} accounts with connection_ids: {conn_ids_used}")
                
                cursor.executemany("""
                    INSERT INTO linked_accounts
                    (user_id, connection_id, account_id, account_name, alias, account_type, account_subtype,
                     mask, current_balance, available_balance, is_active, sync_transactions,
                     interest_rate, origination_principal, origination_date, maturity_date, loan_term,
                     last_payment_date, last_payment_amount, next_payment_due_date, minimum_payment_amount,
                     next_payment_minimum_amount, payment_frequency, account_state)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        account_name = VALUES(account_name),
                        alias = VALUES(alias),
                        current_balance = VALUES(current_balance),
                        available_balance = VALUES(available_balance),
                        is_active = VALUES(is_active),
                        sync_transactions = VALUES(sync_transactions),
                        interest_rate = VALUES(interest_rate),
                        origination_principal = VALUES(origination_principal),
                        origination_date = VALUES(origination_date),
                        maturity_date = VALUES(maturity_date),
                        loan_term = VALUES(loan_term),
                        last_payment_date = VALUES(last_payment_date),
                        last_payment_amount = VALUES(last_payment_amount),
                        next_payment_due_date = VALUES(next_payment_due_date),
                        minimum_payment_amount = VALUES(minimum_payment_amount),
                        next_payment_minimum_amount = VALUES(next_payment_minimum_amount),
                        payment_frequency = VALUES(payment_frequency),
                        account_state = VALUES(account_state)
                """, batch_data)
                
                rows_affected = cursor.rowcount
                log_info(logger, 'FLUSH', f"linked_accounts: MySQL rowcount = {rows_affected}")
                
                # Update Redis with corrected connection_ids
                redis_key = _get_redis_key(table, user_id)
                _redis_client.setex(
                    redis_key,
                    INACTIVITY_TIMEOUT + 60,
                    json.dumps(updated_rows, cls=DecimalEncoder)
                )
                
                conn.commit()
                cursor.close()
                log_info(logger, 'FLUSH', f"→ linked_accounts: {len(batch_data)} rows flushed successfully")
                return len(batch_data)
                
            elif table == 'linked_transactions':
                # linked transactions table
                if not rows:
                    return 0
                
                batch_data = []
                for row in rows:
                    batch_data.append((
                        user_id,
                        row.get('account_id'),
                        row.get('transaction_id'),
                        row.get('date'),
                        row.get('description', ''),
                        float(row.get('amount', 0)),
                        row.get('category', ''),
                        row.get('pending', 0),
                        row.get('merchant_name'),
                        row.get('transaction_type'),
                        row.get('imported_to_entry_id'),
                        row.get('imported_entry_type'),
                        row.get('imported_at'),
                        # provider enrichment fields
                        row.get('enrichment_labels'),
                        row.get('enrichment_merchant_id'),
                        row.get('enrichment_logo'),
                        row.get('enrichment_website'),
                        row.get('enrichment_mcc'),
                        row.get('enrichment_location'),
                        row.get('enrichment_location_city'),
                        row.get('enrichment_location_state'),
                        row.get('enrichment_location_country'),
                        row.get('enrichment_recurrence'),
                        row.get('enrichment_recurrence_group_id'),
                        row.get('enrichment_periodicity'),
                        row.get('enrichment_periodicity_days'),
                        row.get('enrichment_avg_amount'),
                        row.get('enrichment_first_payment_date'),
                        row.get('enrichment_last_payment_date'),
                        row.get('enrichment_person'),
                        row.get('enrichment_transaction_type'),
                        row.get('enriched_at'),
                        # Custom category suggestion fields (from direct the enrichment provider API)
                        row.get('custom_category_suggestion'),
                        row.get('custom_category_id'),
                        row.get('custom_category_type'),
                        row.get('custom_category_confidence'),
                        row.get('custom_suggestion_at'),
                        # Finicity metadata
                        row.get('provider_created_date')
                    ))
                
                cursor.executemany("""
                    INSERT INTO linked_transactions
                    (user_id, account_id, transaction_id, date, description, amount, category, pending, merchant_name,
                     transaction_type, imported_to_entry_id, imported_entry_type, imported_at,
                     enrichment_labels, enrichment_merchant_id, enrichment_logo, enrichment_website, enrichment_mcc,
                     enrichment_location, enrichment_location_city, enrichment_location_state, enrichment_location_country,
                     enrichment_recurrence, enrichment_recurrence_group_id, enrichment_periodicity, enrichment_periodicity_days,
                     enrichment_avg_amount, enrichment_first_payment_date, enrichment_last_payment_date,
                     enrichment_person, enrichment_transaction_type, enriched_at,
                     custom_category_suggestion, custom_category_id, custom_category_type, custom_category_confidence, custom_suggestion_at,
                     provider_created_date)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        description = VALUES(description),
                        amount = VALUES(amount),
                        category = VALUES(category),
                        pending = VALUES(pending),
                        merchant_name = VALUES(merchant_name),
                        transaction_type = VALUES(transaction_type),
                        imported_to_entry_id = VALUES(imported_to_entry_id),
                        imported_entry_type = VALUES(imported_entry_type),
                        imported_at = VALUES(imported_at),
                        enrichment_labels = VALUES(enrichment_labels),
                        enrichment_merchant_id = VALUES(enrichment_merchant_id),
                        enrichment_logo = VALUES(enrichment_logo),
                        enrichment_website = VALUES(enrichment_website),
                        enrichment_mcc = VALUES(enrichment_mcc),
                        enrichment_location = VALUES(enrichment_location),
                        enrichment_location_city = VALUES(enrichment_location_city),
                        enrichment_location_state = VALUES(enrichment_location_state),
                        enrichment_location_country = VALUES(enrichment_location_country),
                        enrichment_recurrence = VALUES(enrichment_recurrence),
                        enrichment_recurrence_group_id = VALUES(enrichment_recurrence_group_id),
                        enrichment_periodicity = VALUES(enrichment_periodicity),
                        enrichment_periodicity_days = VALUES(enrichment_periodicity_days),
                        enrichment_avg_amount = VALUES(enrichment_avg_amount),
                        enrichment_first_payment_date = VALUES(enrichment_first_payment_date),
                        enrichment_last_payment_date = VALUES(enrichment_last_payment_date),
                        enrichment_person = VALUES(enrichment_person),
                        enrichment_transaction_type = VALUES(enrichment_transaction_type),
                        enriched_at = VALUES(enriched_at),
                        custom_category_suggestion = VALUES(custom_category_suggestion),
                        custom_category_id = VALUES(custom_category_id),
                        custom_category_type = VALUES(custom_category_type),
                        custom_category_confidence = VALUES(custom_category_confidence),
                        custom_suggestion_at = VALUES(custom_suggestion_at),
                        provider_created_date = VALUES(provider_created_date)
                """, batch_data)
                
                conn.commit()
                cursor.close()
                log_info(logger, 'FLUSH', f"→ linked_transactions: {len(batch_data)} rows")
                return len(batch_data)
                
            elif table == 'category_memory':
                # Category memory: user-confirmed merchant→category mappings
                # Unique key is (user_id, description, category_type)
                if not rows:
                    return 0
                
                batch_data = []
                for row in rows:
                    batch_data.append((
                        user_id,
                        row.get('merchant_id'),
                        row.get('description'),
                        row.get('category_id'),
                        row.get('category_type', 'expense'),
                        row.get('account_id'),
                        row.get('times_confirmed', 1)
                    ))
                
                cursor.executemany("""
                    INSERT INTO category_memory
                    (user_id, merchant_id, description, category_id, category_type, account_id, times_confirmed)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        merchant_id = COALESCE(VALUES(merchant_id), merchant_id),
                        category_id = VALUES(category_id),
                        account_id = VALUES(account_id),
                        times_confirmed = VALUES(times_confirmed)
                """, batch_data)
                
                conn.commit()
                cursor.close()
                log_info(logger, 'FLUSH', f"→ category_memory: {len(batch_data)} rows")
                return len(batch_data)
                
            elif table == 'income_categories':
                # Income categories table
                
                # First, delete any categories marked for deletion
                pending_key = f"pending_deletes:income_categories:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    delete_ids = [int(id_str) for id_str in pending_deletes]
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    cursor.execute(f"""
                        DELETE FROM income_categories WHERE id IN ({placeholders}) AND user_id = %s
                    """, delete_ids + [user_id])
                    deleted_count = cursor.rowcount
                    log_info(logger, 'FLUSH', f"Deleted {deleted_count} income_categories from MySQL (removed from Redis)")
                
                # Track temp ID to real ID mappings
                temp_id_mappings = {}
                
                # Process categories one at a time to get auto-generated IDs for temp IDs
                for row in rows:
                    old_id = row.get('id')
                    is_temp = old_id and int(old_id) < 0
                    
                    if is_temp:
                        # INSERT with NULL id to get auto-generated ID
                        cursor.execute("""
                            INSERT INTO income_categories (id, user_id, name, display_order, group_id,
                                is_recurring, is_auto_adjustment, no_end_date, hidden, is_system, is_savings)
                            VALUES (NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (
                            user_id,
                            row.get('name'),
                            float(row.get('display_order', 0)),
                            row.get('group_id'),
                            int(row.get('is_recurring', 0)),
                            int(row.get('is_auto_adjustment', 0)),
                            int(row.get('no_end_date', 0)),
                            int(row.get('hidden', 0)),
                            int(row.get('is_system', 0)),
                            (1 if row.get('is_savings') else None)
                        ))
                        new_id = cursor.lastrowid
                        temp_id_mappings[int(old_id)] = new_id
                        log_info(logger, 'FLUSH', f"Income category temp ID {old_id} → real ID {new_id}")
                    else:
                        # Regular UPSERT for existing IDs
                        cursor.execute("""
                            INSERT INTO income_categories (id, user_id, name, display_order, group_id,
                                is_recurring, is_auto_adjustment, no_end_date, hidden, is_system, is_savings)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE
                                name = VALUES(name),
                                display_order = VALUES(display_order),
                                group_id = VALUES(group_id),
                                is_recurring = VALUES(is_recurring),
                                is_auto_adjustment = VALUES(is_auto_adjustment),
                                no_end_date = VALUES(no_end_date),
                                hidden = VALUES(hidden),
                                is_system = VALUES(is_system),
                                is_savings = VALUES(is_savings)
                        """, (
                            old_id,
                            user_id,
                            row.get('name'),
                            float(row.get('display_order', 0)),
                            row.get('group_id'),
                            int(row.get('is_recurring', 0)),
                            int(row.get('is_auto_adjustment', 0)),
                            int(row.get('no_end_date', 0)),
                            int(row.get('hidden', 0)),
                            int(row.get('is_system', 0)),
                            (1 if row.get('is_savings') else None)
                        ))
                
                conn.commit()
                
                # Update Redis with new IDs if any temp IDs were replaced
                if temp_id_mappings:
                    for i, row in enumerate(rows):
                        old_id = row.get('id')
                        if old_id and int(old_id) in temp_id_mappings:
                            rows[i]['id'] = temp_id_mappings[int(old_id)]
                    
                    # Save updated categories back to Redis
                    redis_key = _get_redis_key('income_categories', user_id)
                    _redis_client.setex(
                        redis_key,
                        INACTIVITY_TIMEOUT + 60,
                        json.dumps(rows, cls=DecimalEncoder)
                    )
                    log_info(logger, 'FLUSH', f"Updated {len(temp_id_mappings)} income_category temp IDs in Redis")
                    
                    # Also update category_id in income_entries that reference the temp IDs
                    entries_key = _get_redis_key('income_entries', user_id)
                    entries_data = _redis_client.get(entries_key)
                    if entries_data:
                        entries = json.loads(entries_data)
                        entries_updated = 0
                        for entry in entries:
                            old_cat_id = entry.get('category_id')
                            if old_cat_id and int(old_cat_id) in temp_id_mappings:
                                entry['category_id'] = temp_id_mappings[int(old_cat_id)]
                                entries_updated += 1
                        if entries_updated > 0:
                            _redis_client.setex(
                                entries_key,
                                INACTIVITY_TIMEOUT + 60,
                                json.dumps(entries, cls=DecimalEncoder)
                            )
                            # Mark as dirty so entries get flushed in this cycle
                            _redis_client.sadd(f"dirty_tables:{user_id}", 'income_entries')
                            log_info(logger, 'FLUSH', f"Updated {entries_updated} income_entries with new category IDs")
                    
                    # Also update category_id in recurring_income_buckets
                    buckets_key = _get_redis_key('recurring_income_buckets', user_id)
                    buckets_data = _redis_client.get(buckets_key)
                    if buckets_data:
                        buckets = json.loads(buckets_data)
                        buckets_updated = 0
                        for bucket in buckets:
                            old_cat_id = bucket.get('category_id')
                            if old_cat_id and int(old_cat_id) in temp_id_mappings:
                                bucket['category_id'] = temp_id_mappings[int(old_cat_id)]
                                buckets_updated += 1
                        if buckets_updated > 0:
                            _redis_client.setex(
                                buckets_key,
                                INACTIVITY_TIMEOUT + 60,
                                json.dumps(buckets, cls=DecimalEncoder)
                            )
                            # Mark as dirty so buckets get flushed in this cycle
                            _redis_client.sadd(f"dirty_tables:{user_id}", 'recurring_income_buckets')
                            log_info(logger, 'FLUSH', f"Updated {buckets_updated} recurring_income_buckets with new category IDs")
                    
                    # CRITICAL: Also update category_id in recurring_income records
                    recurring_key = _get_redis_key('recurring_income', user_id)
                    recurring_data = _redis_client.get(recurring_key)
                    if recurring_data:
                        recurring_records = json.loads(recurring_data)
                        recurring_updated = 0
                        for rec in recurring_records:
                            old_cat_id = rec.get('category_id')
                            if old_cat_id and int(old_cat_id) in temp_id_mappings:
                                rec['category_id'] = temp_id_mappings[int(old_cat_id)]
                                recurring_updated += 1
                        if recurring_updated > 0:
                            _redis_client.setex(
                                recurring_key,
                                INACTIVITY_TIMEOUT + 60,
                                json.dumps(recurring_records, cls=DecimalEncoder)
                            )
                            # Mark as dirty so recurring_income gets flushed in this cycle
                            _redis_client.sadd(f"dirty_tables:{user_id}", 'recurring_income')
                            log_info(logger, 'FLUSH', f"Updated {recurring_updated} recurring_income records with new category IDs")
                
                # Clear pending deletions
                _redis_client.delete(pending_key)
                
                cursor.close()
                log_info(logger, 'FLUSH', f"→ income_categories: {len(rows)} rows")
                return len(rows)
                
            elif table == 'expense_categories':
                # Expense categories table
                
                # First, delete any categories marked for deletion
                pending_key = f"pending_deletes:expense_categories:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    delete_ids = [int(id_str) for id_str in pending_deletes]
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    cursor.execute(f"""
                        DELETE FROM expense_categories WHERE id IN ({placeholders}) AND user_id = %s
                    """, delete_ids + [user_id])
                    deleted_count = cursor.rowcount
                    log_info(logger, 'FLUSH', f"Deleted {deleted_count} expense_categories from MySQL (removed from Redis)")
                
                # Track temp ID to real ID mappings
                temp_id_mappings = {}
                
                # Process categories one at a time to get auto-generated IDs for temp IDs
                for row in rows:
                    old_id = row.get('id')
                    is_temp = old_id and int(old_id) < 0
                    
                    if is_temp:
                        # INSERT with NULL id to get auto-generated ID
                        cursor.execute("""
                            INSERT INTO expense_categories (id, user_id, name, display_order, group_id,
                                is_recurring, is_auto_adjustment, no_end_date, hidden, is_bud, is_credit_account, credit_account_id, is_system, is_savings)
                            VALUES (NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (
                            user_id,
                            row.get('name'),
                            float(row.get('display_order', 0)),
                            row.get('group_id'),
                            int(row.get('is_recurring', 0)),
                            int(row.get('is_auto_adjustment', 0)),
                            int(row.get('no_end_date', 0)),
                            int(row.get('hidden', 0)),
                            int(row.get('is_bud', 0)),
                            int(row.get('is_credit_account', 0)),
                            row.get('credit_account_id'),
                            int(row.get('is_system', 0)),
                            (1 if row.get('is_savings') else None)
                        ))
                        new_id = cursor.lastrowid
                        temp_id_mappings[int(old_id)] = new_id
                        log_info(logger, 'FLUSH', f"Expense category temp ID {old_id} → real ID {new_id}")
                    else:
                        # Regular UPSERT for existing IDs
                        cursor.execute("""
                            INSERT INTO expense_categories (id, user_id, name, display_order, group_id,
                                is_recurring, is_auto_adjustment, no_end_date, hidden, is_bud, is_credit_account, credit_account_id, is_system, is_savings)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE
                                name = VALUES(name),
                                display_order = VALUES(display_order),
                                group_id = VALUES(group_id),
                                is_recurring = VALUES(is_recurring),
                                is_auto_adjustment = VALUES(is_auto_adjustment),
                                no_end_date = VALUES(no_end_date),
                                hidden = VALUES(hidden),
                                is_bud = VALUES(is_bud),
                                is_credit_account = VALUES(is_credit_account),
                                credit_account_id = VALUES(credit_account_id),
                                is_system = VALUES(is_system),
                                is_savings = VALUES(is_savings)
                        """, (
                            old_id,
                            user_id,
                            row.get('name'),
                            float(row.get('display_order', 0)),
                            row.get('group_id'),
                            int(row.get('is_recurring', 0)),
                            int(row.get('is_auto_adjustment', 0)),
                            int(row.get('no_end_date', 0)),
                            int(row.get('hidden', 0)),
                            int(row.get('is_bud', 0)),
                            int(row.get('is_credit_account', 0)),
                            row.get('credit_account_id'),
                            int(row.get('is_system', 0)),
                            (1 if row.get('is_savings') else None)
                        ))
                
                conn.commit()
                
                # Update Redis with new IDs if any temp IDs were replaced
                if temp_id_mappings:
                    for i, row in enumerate(rows):
                        old_id = row.get('id')
                        if old_id and int(old_id) in temp_id_mappings:
                            rows[i]['id'] = temp_id_mappings[int(old_id)]
                    
                    # Save updated categories back to Redis
                    redis_key = _get_redis_key('expense_categories', user_id)
                    _redis_client.setex(
                        redis_key,
                        INACTIVITY_TIMEOUT + 60,
                        json.dumps(rows, cls=DecimalEncoder)
                    )
                    log_info(logger, 'FLUSH', f"Updated {len(temp_id_mappings)} expense_category temp IDs in Redis")
                    
                    # Also update category_id in expense_entries that reference the temp IDs
                    entries_key = _get_redis_key('expense_entries', user_id)
                    entries_data = _redis_client.get(entries_key)
                    if entries_data:
                        entries = json.loads(entries_data)
                        entries_updated = 0
                        for entry in entries:
                            old_cat_id = entry.get('category_id')
                            if old_cat_id and int(old_cat_id) in temp_id_mappings:
                                entry['category_id'] = temp_id_mappings[int(old_cat_id)]
                                entries_updated += 1
                        if entries_updated > 0:
                            _redis_client.setex(
                                entries_key,
                                INACTIVITY_TIMEOUT + 60,
                                json.dumps(entries, cls=DecimalEncoder)
                            )
                            # Mark as dirty so entries get flushed in this cycle
                            _redis_client.sadd(f"dirty_tables:{user_id}", 'expense_entries')
                            log_info(logger, 'FLUSH', f"Updated {entries_updated} expense_entries with new category IDs")
                    
                    # Also update category_id in recurring_expense_buckets
                    buckets_key = _get_redis_key('recurring_expense_buckets', user_id)
                    buckets_data = _redis_client.get(buckets_key)
                    if buckets_data:
                        buckets = json.loads(buckets_data)
                        buckets_updated = 0
                        for bucket in buckets:
                            old_cat_id = bucket.get('category_id')
                            if old_cat_id and int(old_cat_id) in temp_id_mappings:
                                bucket['category_id'] = temp_id_mappings[int(old_cat_id)]
                                buckets_updated += 1
                        if buckets_updated > 0:
                            _redis_client.setex(
                                buckets_key,
                                INACTIVITY_TIMEOUT + 60,
                                json.dumps(buckets, cls=DecimalEncoder)
                            )
                            # Mark as dirty so buckets get flushed in this cycle
                            _redis_client.sadd(f"dirty_tables:{user_id}", 'recurring_expense_buckets')
                            log_info(logger, 'FLUSH', f"Updated {buckets_updated} recurring_expense_buckets with new category IDs")
                    
                    # CRITICAL: Also update category_id in recurring_expense records
                    recurring_key = _get_redis_key('recurring_expense', user_id)
                    recurring_data = _redis_client.get(recurring_key)
                    if recurring_data:
                        recurring_records = json.loads(recurring_data)
                        recurring_updated = 0
                        for rec in recurring_records:
                            old_cat_id = rec.get('category_id')
                            if old_cat_id and int(old_cat_id) in temp_id_mappings:
                                rec['category_id'] = temp_id_mappings[int(old_cat_id)]
                                recurring_updated += 1
                        if recurring_updated > 0:
                            _redis_client.setex(
                                recurring_key,
                                INACTIVITY_TIMEOUT + 60,
                                json.dumps(recurring_records, cls=DecimalEncoder)
                            )
                            # Mark as dirty so recurring_expense gets flushed in this cycle
                            _redis_client.sadd(f"dirty_tables:{user_id}", 'recurring_expense')
                            log_info(logger, 'FLUSH', f"Updated {recurring_updated} recurring_expense records with new category IDs")
                
                # Clear pending deletions
                _redis_client.delete(pending_key)
                
                cursor.close()
                log_info(logger, 'FLUSH', f"→ expense_categories: {len(rows)} rows")
                return len(rows)
                
            elif table == 'c_expense_categories':
                # Credit account expense categories table
                # Now uses user_id for Redis key (c_expense_categories:v1:{user_id})
                # Categories contain account_id field to identify which account they belong to
                
                # rows variable already contains data from c_expense_categories:v1:{user_id}
                # (fetched by _get_redis_key at the top of this function)
                
                if not rows:
                    # Still need to handle pending deletions even if Redis data is empty
                    pending_key = f"pending_deletes:c_expense_categories:{user_id}"
                    pending_deletes = _redis_client.smembers(pending_key)
                    
                    if pending_deletes:
                        delete_ids = [int(id_str) for id_str in pending_deletes]
                        placeholders = ','.join(['%s'] * len(delete_ids))
                        # Delete categories by ID (they have user association via account_id FK)
                        cursor.execute(f"""
                            DELETE cec FROM c_expense_categories cec
                            INNER JOIN credit_accounts ca ON cec.account_id = ca.id
                            WHERE cec.id IN ({placeholders}) AND ca.user_id = %s
                        """, delete_ids + [user_id])
                        deleted_count = cursor.rowcount
                        log_info(logger, 'FLUSH', f"Deleted {deleted_count} c_expense_categories for user {user_id} from MySQL")
                        conn.commit()
                    
                    _redis_client.delete(pending_key)
                    cursor.close()
                    return 0
                
                # First, delete any categories marked for deletion
                pending_key = f"pending_deletes:c_expense_categories:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    delete_ids = [int(id_str) for id_str in pending_deletes]
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    # Delete categories by ID (they have user association via account_id FK)
                    cursor.execute(f"""
                        DELETE cec FROM c_expense_categories cec
                        INNER JOIN credit_accounts ca ON cec.account_id = ca.id
                        WHERE cec.id IN ({placeholders}) AND ca.user_id = %s
                    """, delete_ids + [user_id])
                    deleted_count = cursor.rowcount
                    log_info(logger, 'FLUSH', f"Deleted {deleted_count} c_expense_categories for user {user_id} from MySQL")
                
                # Track temp ID to real ID mappings
                temp_id_mappings = {}
                
                # Process categories one at a time to get auto-generated IDs for temp IDs
                for row in rows:
                    old_id = row.get('id')
                    is_temp = old_id and int(old_id) < 0
                    
                    if is_temp:
                        # INSERT with NULL id to get auto-generated ID
                        cursor.execute("""
                            INSERT INTO c_expense_categories (id, account_id, name, display_order, group_id,
                                is_recurring, no_end_date, hidden, is_bud, is_interest, is_auto_adjustment, is_system)
                            VALUES (NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (
                            row.get('account_id'),
                            row.get('name'),
                            float(row.get('display_order', 0)),
                            row.get('group_id'),
                            int(row.get('is_recurring', 0)),
                            int(row.get('no_end_date', 0)),
                            int(row.get('hidden', 0)),
                            int(row.get('is_bud', 0)),
                            int(row.get('is_interest', 0)),
                            int(row.get('is_auto_adjustment', 0)),
                            int(row.get('is_system', 0))
                        ))
                        new_id = cursor.lastrowid
                        temp_id_mappings[int(old_id)] = new_id
                        log_info(logger, 'FLUSH', f"C_expense category temp ID {old_id} → real ID {new_id} for user {user_id}")
                    else:
                        # Regular UPSERT for existing IDs
                        cursor.execute("""
                            INSERT INTO c_expense_categories (id, account_id, name, display_order, group_id,
                                is_recurring, no_end_date, hidden, is_bud, is_interest, is_auto_adjustment, is_system)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE
                                name = VALUES(name),
                                display_order = VALUES(display_order),
                                group_id = VALUES(group_id),
                                is_recurring = VALUES(is_recurring),
                                no_end_date = VALUES(no_end_date),
                                hidden = VALUES(hidden),
                                is_bud = VALUES(is_bud),
                                is_interest = VALUES(is_interest),
                                is_auto_adjustment = VALUES(is_auto_adjustment),
                                is_system = VALUES(is_system)
                        """, (
                            old_id,
                            row.get('account_id'),
                            row.get('name'),
                            float(row.get('display_order', 0)),
                            row.get('group_id'),
                            int(row.get('is_recurring', 0)),
                            int(row.get('no_end_date', 0)),
                            int(row.get('hidden', 0)),
                            int(row.get('is_bud', 0)),
                            int(row.get('is_interest', 0)),
                            int(row.get('is_auto_adjustment', 0)),
                            int(row.get('is_system', 0))
                        ))
                
                conn.commit()
                
                # Update Redis with new IDs if any temp IDs were replaced
                if temp_id_mappings:
                    for i, row in enumerate(rows):
                        old_id = row.get('id')
                        if old_id and int(old_id) in temp_id_mappings:
                            rows[i]['id'] = temp_id_mappings[int(old_id)]
                    
                    # Save updated categories back to Redis (using user_id key)
                    _redis_client.setex(
                        redis_key,
                        INACTIVITY_TIMEOUT + 60,
                        json.dumps(rows, cls=DecimalEncoder)
                    )
                    log_info(logger, 'FLUSH', f"Updated {len(temp_id_mappings)} c_expense_category temp IDs in Redis for user {user_id}")
                    
                    # Also update c_expense_entries that reference these temp category IDs
                    entries_redis_key = f"c_expense_entries:v1:{user_id}"
                    cached_entries = _redis_client.get(entries_redis_key)
                    if cached_entries:
                        entries = json.loads(cached_entries)
                        updated_count = 0
                        for entry in entries:
                            old_cat_id = entry.get('category_id')
                            if old_cat_id and int(old_cat_id) in temp_id_mappings:
                                entry['category_id'] = temp_id_mappings[int(old_cat_id)]
                                updated_count += 1
                        
                        if updated_count > 0:
                            _redis_client.setex(
                                entries_redis_key,
                                INACTIVITY_TIMEOUT + 60,
                                json.dumps(entries, cls=DecimalEncoder)
                            )
                            log_info(logger, 'FLUSH', f"Updated {updated_count} c_expense_entries with new category IDs for user {user_id}")
                            # Mark entries as dirty so they get flushed with real IDs
                            _redis_client.sadd(f"dirty_tables:{user_id}", 'c_expense_entries')
                            _redis_client.expire(f"dirty_tables:{user_id}", INACTIVITY_TIMEOUT + 60)
                    
                    # Also update recurring_c_expense_buckets that reference these temp category IDs
                    # Now using user_id key instead of account_id
                    buckets_redis_key = f"recurring_c_expense_buckets:v1:{user_id}"
                    cached_buckets = _redis_client.get(buckets_redis_key)
                    if cached_buckets:
                        buckets = json.loads(cached_buckets)
                        updated_count = 0
                        for bucket in buckets:
                            old_cat_id = bucket.get('category_id')
                            if old_cat_id and int(old_cat_id) in temp_id_mappings:
                                bucket['category_id'] = temp_id_mappings[int(old_cat_id)]
                                updated_count += 1
                        
                        if updated_count > 0:
                            _redis_client.setex(
                                buckets_redis_key,
                                INACTIVITY_TIMEOUT + 60,
                                json.dumps(buckets, cls=DecimalEncoder)
                            )
                            log_info(logger, 'FLUSH', f"Updated {updated_count} recurring_c_expense_buckets with new category IDs for user {user_id}")
                            # Mark buckets as dirty so they get flushed with real IDs
                            _redis_client.sadd(f"dirty_tables:{user_id}", 'recurring_c_expense_buckets')
                            _redis_client.expire(f"dirty_tables:{user_id}", INACTIVITY_TIMEOUT + 60)
                    
                    # Also update category_id in recurring_c_expense records
                    recurring_key = f"recurring_c_expense:v1:{user_id}"
                    recurring_data = _redis_client.get(recurring_key)
                    if recurring_data:
                        recurring_records = json.loads(recurring_data)
                        recurring_updated = 0
                        for rec in recurring_records:
                            old_cat_id = rec.get('category_id')
                            if old_cat_id and int(old_cat_id) in temp_id_mappings:
                                rec['category_id'] = temp_id_mappings[int(old_cat_id)]
                                recurring_updated += 1
                        if recurring_updated > 0:
                            _redis_client.setex(
                                recurring_key,
                                INACTIVITY_TIMEOUT + 60,
                                json.dumps(recurring_records, cls=DecimalEncoder)
                            )
                            # Mark as dirty so recurring_c_expense gets flushed in this cycle
                            _redis_client.sadd(f"dirty_tables:{user_id}", 'recurring_c_expense')
                            _redis_client.expire(f"dirty_tables:{user_id}", INACTIVITY_TIMEOUT + 60)
                            log_info(logger, 'FLUSH', f"Updated {recurring_updated} recurring_c_expense records with new category IDs for user {user_id}")
                
                # Clear pending deletions
                _redis_client.delete(pending_key)
                
                cursor.close()
                log_info(logger, 'FLUSH', f"→ c_expense_categories: {len(rows)} rows for user {user_id}")
                return len(rows)
                
            elif table in ['income_category_groups', 'expense_category_groups']:
                # Category groups tables

                # Handle pending deletes FIRST
                pending_key = f"pending_deletes:{table}:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                if pending_deletes:
                    for gid_bytes in pending_deletes:
                        gid = gid_bytes.decode('utf-8') if isinstance(gid_bytes, bytes) else gid_bytes
                        cursor.execute(f"DELETE FROM {table} WHERE id = %s AND user_id = %s", (gid, user_id))
                    conn.commit()
                    _redis_client.delete(pending_key)
                    log_info(logger, 'FLUSH', f"Deleted {len(pending_deletes)} rows from {table}")

                if not rows:
                    cursor.close()
                    return 0

                # Orphan detection: delete MySQL rows not in Redis
                redis_ids = set(int(r.get('id')) for r in rows if int(r.get('id', 0)) > 0)
                cursor.execute(f"SELECT id FROM {table} WHERE user_id = %s", (user_id,))
                mysql_ids = set(r[0] for r in cursor.fetchall())
                orphan_ids = mysql_ids - redis_ids
                if orphan_ids:
                    for oid in orphan_ids:
                        cursor.execute(f"DELETE FROM {table} WHERE id = %s AND user_id = %s", (oid, user_id))
                    conn.commit()
                    log_info(logger, 'FLUSH', f"Removed {len(orphan_ids)} orphan rows from {table}")
                
                # Track temp ID to real ID mappings
                temp_id_mappings = {}
                
                for row in rows:
                    old_id = row.get('id')
                    is_temp = old_id and int(old_id) < 0
                    
                    if is_temp:
                        cursor.execute(f"""
                            INSERT INTO {table} (id, user_id, name, display_order)
                            VALUES (NULL, %s, %s, %s)
                        """, (
                            user_id,
                            row.get('name'),
                            float(row.get('display_order', 0))
                        ))
                        new_id = cursor.lastrowid
                        temp_id_mappings[int(old_id)] = new_id
                        log_info(logger, 'FLUSH', f"{table} temp ID {old_id} → real ID {new_id}")
                    else:
                        cursor.execute(f"""
                            INSERT INTO {table} (id, user_id, name, display_order)
                            VALUES (%s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE
                                name = VALUES(name),
                                display_order = VALUES(display_order)
                        """, (
                            old_id,
                            user_id,
                            row.get('name'),
                            float(row.get('display_order', 0))
                        ))
                
                conn.commit()
                
                # Update Redis with new IDs if any temp IDs were replaced
                if temp_id_mappings:
                    for i, row in enumerate(rows):
                        old_id = row.get('id')
                        if old_id and int(old_id) in temp_id_mappings:
                            rows[i]['id'] = temp_id_mappings[int(old_id)]
                    
                    redis_key = _get_redis_key(table, user_id)
                    _redis_client.setex(redis_key, INACTIVITY_TIMEOUT + 60, json.dumps(rows, cls=DecimalEncoder))
                    log_info(logger, 'FLUSH', f"Updated {len(temp_id_mappings)} {table} temp IDs in Redis")
                    
                    # Update group_id references in category tables
                    cat_table = 'income_categories' if table == 'income_category_groups' else 'expense_categories'
                    cat_key = _get_redis_key(cat_table, user_id)
                    cat_data = _redis_client.get(cat_key)
                    if cat_data:
                        categories = json.loads(cat_data)
                        cats_updated = 0
                        for cat in categories:
                            old_gid = cat.get('group_id')
                            if old_gid is not None and int(old_gid) in temp_id_mappings:
                                cat['group_id'] = temp_id_mappings[int(old_gid)]
                                cats_updated += 1
                        if cats_updated > 0:
                            _redis_client.setex(cat_key, INACTIVITY_TIMEOUT + 60, json.dumps(categories, cls=DecimalEncoder))
                            _redis_client.sadd(f"dirty_tables:{user_id}", cat_table)
                            log_info(logger, 'FLUSH', f"Updated {cats_updated} {cat_table} group_id refs")
                    
                    # For expense groups: also update source_group_id in c_expense_category_groups
                    if table == 'expense_category_groups':
                        ceg_key = _get_redis_key('c_expense_category_groups', user_id)
                        ceg_data = _redis_client.get(ceg_key)
                        if ceg_data:
                            c_groups_list = json.loads(ceg_data)
                            ceg_updated = 0
                            for cg in c_groups_list:
                                old_src = cg.get('source_group_id')
                                if old_src is not None and int(old_src) in temp_id_mappings:
                                    cg['source_group_id'] = temp_id_mappings[int(old_src)]
                                    ceg_updated += 1
                            if ceg_updated > 0:
                                _redis_client.setex(ceg_key, INACTIVITY_TIMEOUT + 60, json.dumps(c_groups_list, cls=DecimalEncoder))
                                _redis_client.sadd(f"dirty_tables:{user_id}", 'c_expense_category_groups')
                                log_info(logger, 'FLUSH', f"Updated {ceg_updated} c_expense_category_groups source_group_id refs")
                
                cursor.close()
                log_info(logger, 'FLUSH', f"→ {table}: {len(rows)} rows")
                return len(rows)
                
            elif table == 'c_expense_category_groups':
                # Credit account expense category groups table

                # Handle pending deletes FIRST
                pending_key = f"pending_deletes:c_expense_category_groups:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                if pending_deletes:
                    for gid_bytes in pending_deletes:
                        gid = gid_bytes.decode('utf-8') if isinstance(gid_bytes, bytes) else gid_bytes
                        cursor.execute("DELETE FROM c_expense_category_groups WHERE id = %s AND user_id = %s", (gid, user_id))
                    conn.commit()
                    _redis_client.delete(pending_key)
                    log_info(logger, 'FLUSH', f"Deleted {len(pending_deletes)} rows from c_expense_category_groups")

                if not rows:
                    cursor.close()
                    return 0

                # Orphan detection: delete MySQL rows not in Redis
                redis_ids = set(int(r.get('id')) for r in rows if int(r.get('id', 0)) > 0)
                cursor.execute("SELECT id FROM c_expense_category_groups WHERE user_id = %s", (user_id,))
                mysql_ids = set(r[0] for r in cursor.fetchall())
                orphan_ids = mysql_ids - redis_ids
                if orphan_ids:
                    for oid in orphan_ids:
                        cursor.execute("DELETE FROM c_expense_category_groups WHERE id = %s AND user_id = %s", (oid, user_id))
                    conn.commit()
                    log_info(logger, 'FLUSH', f"Removed {len(orphan_ids)} orphan rows from c_expense_category_groups")
                
                # Track temp ID to real ID mappings
                temp_id_mappings = {}
                
                # Pre-filter: verify account_ids exist in credit_accounts (Redis or MySQL)
                # to avoid FK constraint failures from orphaned groups
                valid_account_ids = set()
                ca_key = f"credit_accounts:v1:{user_id}"
                ca_cached = _redis_client.get(ca_key)
                if ca_cached:
                    ca_list = json.loads(ca_cached)
                    valid_account_ids = set(int(a.get('id')) for a in ca_list if a.get('id') is not None)
                if not valid_account_ids:
                    cursor.execute("SELECT id FROM credit_accounts WHERE user_id = %s", (user_id,))
                    valid_account_ids = set(r[0] for r in cursor.fetchall())
                
                # Remove orphaned groups (account_id not in any existing credit account)
                valid_rows = []
                removed_rows = []
                for row in rows:
                    acct_id = row.get('account_id')
                    if acct_id is not None and int(acct_id) in valid_account_ids:
                        valid_rows.append(row)
                    else:
                        removed_rows.append(row)
                
                if removed_rows:
                    log_warning(logger, 'FLUSH', f"Removed {len(removed_rows)} orphaned c_expense_category_groups (invalid account_ids: {[r.get('account_id') for r in removed_rows]})")
                    # Update Redis to remove orphaned groups
                    redis_key = _get_redis_key('c_expense_category_groups', user_id)
                    _redis_client.setex(redis_key, INACTIVITY_TIMEOUT + 60, json.dumps(valid_rows, cls=DecimalEncoder))
                    rows = valid_rows
                
                if not rows:
                    cursor.close()
                    return 0
                
                for row in rows:
                    old_id = row.get('id')
                    is_temp = old_id and int(old_id) < 0
                    
                    if is_temp:
                        cursor.execute("""
                            INSERT INTO c_expense_category_groups (id, account_id, user_id, name, display_order, source_group_id)
                            VALUES (NULL, %s, %s, %s, %s, %s)
                        """, (
                            row.get('account_id'),
                            user_id,
                            row.get('name'),
                            float(row.get('display_order', 0)),
                            row.get('source_group_id')
                        ))
                        new_id = cursor.lastrowid
                        temp_id_mappings[int(old_id)] = new_id
                        log_info(logger, 'FLUSH', f"c_expense_category_groups temp ID {old_id} → real ID {new_id}")
                    else:
                        cursor.execute("""
                            INSERT INTO c_expense_category_groups (id, account_id, user_id, name, display_order, source_group_id)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE
                                name = VALUES(name),
                                display_order = VALUES(display_order),
                                source_group_id = VALUES(source_group_id)
                        """, (
                            old_id,
                            row.get('account_id'),
                            user_id,
                            row.get('name'),
                            float(row.get('display_order', 0)),
                            row.get('source_group_id')
                        ))
                
                conn.commit()
                
                # Update Redis with new IDs if any temp IDs were replaced
                if temp_id_mappings:
                    for i, row in enumerate(rows):
                        old_id = row.get('id')
                        if old_id and int(old_id) in temp_id_mappings:
                            rows[i]['id'] = temp_id_mappings[int(old_id)]
                    
                    redis_key = _get_redis_key('c_expense_category_groups', user_id)
                    _redis_client.setex(redis_key, INACTIVITY_TIMEOUT + 60, json.dumps(rows, cls=DecimalEncoder))
                    log_info(logger, 'FLUSH', f"Updated {len(temp_id_mappings)} c_expense_category_groups temp IDs in Redis")
                    
                    # Update group_id references in c_expense_categories
                    cat_key = _get_redis_key('c_expense_categories', user_id)
                    cat_data = _redis_client.get(cat_key)
                    if cat_data:
                        c_categories = json.loads(cat_data)
                        cats_updated = 0
                        for cat in c_categories:
                            old_gid = cat.get('group_id')
                            if old_gid is not None and int(old_gid) in temp_id_mappings:
                                cat['group_id'] = temp_id_mappings[int(old_gid)]
                                cats_updated += 1
                        if cats_updated > 0:
                            _redis_client.setex(cat_key, INACTIVITY_TIMEOUT + 60, json.dumps(c_categories, cls=DecimalEncoder))
                            _redis_client.sadd(f"dirty_tables:{user_id}", 'c_expense_categories')
                            log_info(logger, 'FLUSH', f"Updated {cats_updated} c_expense_categories group_id refs")
                
                cursor.close()
                log_info(logger, 'FLUSH', f"→ c_expense_category_groups: {len(rows)} rows")
                return len(rows)
                
            elif table == 'credit_accounts':
                # Credit accounts table
                
                # Handle pending deletes FIRST (before checking if rows exist)
                pending_key = f"pending_deletes:credit_accounts:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    for account_id_bytes in pending_deletes:
                        account_id = account_id_bytes.decode('utf-8') if isinstance(account_id_bytes, bytes) else account_id_bytes
                        cursor.execute(
                            "DELETE FROM credit_accounts WHERE id = %s AND user_id = %s",
                            (account_id, user_id)
                        )
                    conn.commit()
                    _redis_client.delete(pending_key)
                    log_info(logger, 'FLUSH', f"Deleted {len(pending_deletes)} credit accounts from MySQL")
                
                # If no rows to insert/update, we're done
                if not rows:
                    cursor.close()
                    return 0
                
                # Track temp ID to real ID mappings
                temp_id_mappings = {}
                
                # Process accounts one at a time to get auto-generated IDs for temp IDs
                for row in rows:
                    old_id = row.get('id')
                    is_temp = old_id and int(old_id) < 0
                    
                    if is_temp:
                        # INSERT with NULL id to get auto-generated ID
                        cursor.execute("""
                            INSERT INTO credit_accounts (id, user_id, name, mask, linked_account_id, interest_rate, starting_balance, is_card, is_line, is_linked, display_order)
                            VALUES (NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (
                            user_id,
                            row.get('name'),
                            row.get('mask'),
                            row.get('linked_account_id'),
                            float(row.get('interest_rate', 0)) if row.get('interest_rate') else None,
                            float(row.get('starting_balance', 0)),
                            int(row.get('is_card', 0)),
                            int(row.get('is_line', 0)),
                            int(row.get('is_linked', 0)),
                            int(row.get('display_order', 0))
                        ))
                        new_id = cursor.lastrowid
                        temp_id_mappings[int(old_id)] = new_id
                        log_info(logger, 'FLUSH', f"Credit account temp ID {old_id} → real ID {new_id}")
                    else:
                        # Regular UPSERT for existing IDs
                        cursor.execute("""
                            INSERT INTO credit_accounts (id, user_id, name, mask, linked_account_id, interest_rate, starting_balance, is_card, is_line, is_linked, display_order)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE
                                name = VALUES(name),
                                mask = VALUES(mask),
                                linked_account_id = VALUES(linked_account_id),
                                interest_rate = VALUES(interest_rate),
                                starting_balance = VALUES(starting_balance),
                                is_card = VALUES(is_card),
                                is_line = VALUES(is_line),
                                is_linked = VALUES(is_linked),
                                display_order = VALUES(display_order)
                        """, (
                            old_id,
                            user_id,
                            row.get('name'),
                            row.get('mask'),
                            row.get('linked_account_id'),
                            float(row.get('interest_rate', 0)) if row.get('interest_rate') else None,
                            float(row.get('starting_balance', 0)),
                            int(row.get('is_card', 0)),
                            int(row.get('is_line', 0)),
                            int(row.get('is_linked', 0)),
                            int(row.get('display_order', 0))
                        ))
                
                conn.commit()
                
                # Update Redis with new IDs if any temp IDs were replaced
                if temp_id_mappings:
                    for i, row in enumerate(rows):
                        old_id = row.get('id')
                        if old_id and int(old_id) in temp_id_mappings:
                            rows[i]['id'] = temp_id_mappings[int(old_id)]
                    
                    # Save updated accounts back to Redis
                    redis_key = f"credit_accounts:v1:{user_id}"
                    _redis_client.setex(
                        redis_key,
                        INACTIVITY_TIMEOUT + 60,
                        json.dumps(rows, cls=DecimalEncoder)
                    )
                    log_info(logger, 'FLUSH', f"Updated {len(temp_id_mappings)} credit_account temp IDs in Redis")
                    
                    # c_expense_categories are now stored at c_expense_categories:v1:{user_id}
                    # We need to update the account_id field WITHIN the categories, not move to different key
                    cat_redis_key = f"c_expense_categories:v1:{user_id}"
                    cached_cats = _redis_client.get(cat_redis_key)
                    if cached_cats:
                        categories = json.loads(cached_cats)
                        updated_count = 0
                        for cat in categories:
                            old_acct_id = cat.get('account_id')
                            if old_acct_id in temp_id_mappings:
                                cat['account_id'] = temp_id_mappings[old_acct_id]
                                updated_count += 1
                        
                        if updated_count > 0:
                            # Save back to same key with updated account_ids
                            _redis_client.setex(
                                cat_redis_key,
                                INACTIVITY_TIMEOUT + 60,
                                json.dumps(categories, cls=DecimalEncoder)
                            )
                            log_info(logger, 'FLUSH', f"Updated account_id in {updated_count} c_expense_categories")
                            # Mark categories as dirty so they get flushed to MySQL
                            _redis_client.sadd(f"dirty_tables:{user_id}", 'c_expense_categories')
                            _redis_client.expire(f"dirty_tables:{user_id}", INACTIVITY_TIMEOUT + 60)
                    
                    # c_expense_category_groups also reference account_id — cascade temp ID resolution
                    ceg_redis_key = f"c_expense_category_groups:v1:{user_id}"
                    cached_groups = _redis_client.get(ceg_redis_key)
                    if cached_groups:
                        groups = json.loads(cached_groups)
                        group_updated_count = 0
                        for grp in groups:
                            old_acct_id = grp.get('account_id')
                            if old_acct_id in temp_id_mappings:
                                grp['account_id'] = temp_id_mappings[old_acct_id]
                                group_updated_count += 1
                        
                        if group_updated_count > 0:
                            _redis_client.setex(
                                ceg_redis_key,
                                INACTIVITY_TIMEOUT + 60,
                                json.dumps(groups, cls=DecimalEncoder)
                            )
                            log_info(logger, 'FLUSH', f"Updated account_id in {group_updated_count} c_expense_category_groups")
                            _redis_client.sadd(f"dirty_tables:{user_id}", 'c_expense_category_groups')
                            _redis_client.expire(f"dirty_tables:{user_id}", INACTIVITY_TIMEOUT + 60)
                    
                    # c_payment_entries are stored at c_payment_entries:v1:{user_id}
                    # We need to update the account_id field WITHIN the entries
                    payment_redis_key = f"c_payment_entries:v1:{user_id}"
                    cached_payments = _redis_client.get(payment_redis_key)
                    if cached_payments:
                        payments = json.loads(cached_payments)
                        payment_updated_count = 0
                        for payment in payments:
                            old_acct_id = payment.get('account_id')
                            if old_acct_id in temp_id_mappings:
                                payment['account_id'] = temp_id_mappings[old_acct_id]
                                payment_updated_count += 1
                        
                        if payment_updated_count > 0:
                            # Save back to same key with updated account_ids
                            _redis_client.setex(
                                payment_redis_key,
                                INACTIVITY_TIMEOUT + 60,
                                json.dumps(payments, cls=DecimalEncoder)
                            )
                            log_info(logger, 'FLUSH', f"Updated account_id in {payment_updated_count} c_payment_entries")
                            # Mark payments as dirty so they get flushed to MySQL
                            _redis_client.sadd(f"dirty_tables:{user_id}", 'c_payment_entries')
                            _redis_client.expire(f"dirty_tables:{user_id}", INACTIVITY_TIMEOUT + 60)
                    
                    # expense_categories: update credit_account_id for payment categories
                    exp_cat_redis_key = f"expense_categories:v1:{user_id}"
                    cached_exp_cats = _redis_client.get(exp_cat_redis_key)
                    if cached_exp_cats:
                        exp_categories = json.loads(cached_exp_cats)
                        exp_cat_updated = 0
                        for cat in exp_categories:
                            old_ca_id = cat.get('credit_account_id')
                            if old_ca_id is not None and int(old_ca_id) in temp_id_mappings:
                                cat['credit_account_id'] = temp_id_mappings[int(old_ca_id)]
                                exp_cat_updated += 1
                        if exp_cat_updated > 0:
                            _redis_client.setex(
                                exp_cat_redis_key,
                                INACTIVITY_TIMEOUT + 60,
                                json.dumps(exp_categories, cls=DecimalEncoder)
                            )
                            log_info(logger, 'FLUSH', f"Updated credit_account_id in {exp_cat_updated} expense_categories")
                            _redis_client.sadd(f"dirty_tables:{user_id}", 'expense_categories')
                            _redis_client.expire(f"dirty_tables:{user_id}", INACTIVITY_TIMEOUT + 60)
                    
                    # c_expense_entries are also stored at c_expense_entries:v1:{user_id}
                    # They reference categories by category_id, not account_id directly
                    # But we still need to check if there are any entries keyed by old temp account_id
                    # and move them to the user_id key
                    for old_account_id, new_account_id in temp_id_mappings.items():
                        old_entries_redis_key = f"c_expense_entries:v1:{old_account_id}"
                        cached_entries = _redis_client.get(old_entries_redis_key)
                        if cached_entries:
                            entries = json.loads(cached_entries)
                            # Merge into user-level key
                            user_entries_key = f"c_expense_entries:v1:{user_id}"
                            user_entries_cached = _redis_client.get(user_entries_key)
                            user_entries = json.loads(user_entries_cached) if user_entries_cached else []
                            user_entries.extend(entries)
                            _redis_client.setex(
                                user_entries_key,
                                INACTIVITY_TIMEOUT + 60,
                                json.dumps(user_entries, cls=DecimalEncoder)
                            )
                            # Delete old key
                            _redis_client.delete(old_entries_redis_key)
                            log_info(logger, 'FLUSH', f"Moved {len(entries)} c_expense_entries from account {old_account_id} to user {user_id}")
                            # Mark entries as dirty
                            _redis_client.sadd(f"dirty_tables:{user_id}", 'c_expense_entries')
                            _redis_client.expire(f"dirty_tables:{user_id}", INACTIVITY_TIMEOUT + 60)
                        
                        # Initialize balance records for new account
                        _initialize_ca_balances_for_account(cursor, conn, new_account_id)
                
                cursor.close()
                log_info(logger, 'FLUSH', f"→ credit_accounts: {len(rows)} rows")
                return len(rows)
                
            elif table == 'starting_balance':
                # Starting balance table
                if not rows:
                    return 0
                
                # Should only be one row per user
                row = rows[0]
                cursor.execute("""
                    INSERT INTO starting_balance (user_id, amount, date)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        amount = VALUES(amount),
                        date = VALUES(date)
                """, (
                    user_id,
                    float(row.get('amount', 0)),
                    row.get('date')
                ))
                
                conn.commit()
                cursor.close()
                log_info(logger, 'FLUSH', f"→ starting_balance: 1 row")
                return 1
            
            elif table == 'setup_state':
                # Setup wizard temporary state — single row per user, JSON blob
                if not rows:
                    # Empty means setup completed — delete from MySQL
                    cursor.execute("DELETE FROM setup_state WHERE user_id = %s", (user_id,))
                    conn.commit()
                    cursor.close()
                    log_info(logger, 'FLUSH', f"→ setup_state: deleted (setup complete)")
                    return 0
                
                row = rows[0]
                state_json = row.get('state')
                if isinstance(state_json, dict):
                    state_json = json.dumps(state_json)
                
                cursor.execute("""
                    INSERT INTO setup_state (user_id, state, updated_at)
                    VALUES (%s, %s, NOW())
                    ON DUPLICATE KEY UPDATE
                        state = VALUES(state),
                        updated_at = NOW()
                """, (
                    user_id,
                    state_json
                ))
                
                conn.commit()
                cursor.close()
                log_info(logger, 'FLUSH', f"→ setup_state: 1 row")
                return 1
            
            elif table == 'notifications':
                # Notifications table
                # First, handle pending deletes
                pending_key = f"pending_deletes:notifications:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    delete_ids = [int(id_str) for id_str in pending_deletes]
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    cursor.execute(f"""
                        DELETE FROM notifications WHERE id IN ({placeholders}) AND user_id = %s
                    """, delete_ids + [user_id])
                    deleted_count = cursor.rowcount
                    log_info(logger, 'FLUSH', f"Deleted {deleted_count} notifications from MySQL (removed from Redis)")
                    _redis_client.delete(pending_key)
                
                if not rows:
                    conn.commit()
                    cursor.close()
                    return 0
                
                # Track temp ID to real ID mappings
                temp_id_mappings = {}
                
                # Process notifications one at a time to handle temp IDs
                for row in rows:
                    old_id = row.get('id')
                    is_temp = old_id and int(old_id) < 0
                    
                    # Convert date string to proper format if needed
                    date_val = row.get('date')
                    if isinstance(date_val, str):
                        # Remove timezone info if present, MySQL will store as local
                        if 'T' in date_val:
                            date_val = date_val.replace('T', ' ').split('.')[0]
                    
                    if is_temp:
                        # INSERT with NULL id to get auto-generated ID
                        cursor.execute("""
                            INSERT INTO notifications (id, user_id, date, message, is_read)
                            VALUES (NULL, %s, %s, %s, %s)
                        """, (
                            user_id,
                            date_val,
                            row.get('message'),
                            int(row.get('is_read', 0))
                        ))
                        new_id = cursor.lastrowid
                        temp_id_mappings[int(old_id)] = new_id
                        log_info(logger, 'FLUSH', f"Notification temp ID {old_id} → real ID {new_id}")
                    else:
                        # Regular UPSERT for existing IDs
                        cursor.execute("""
                            INSERT INTO notifications (id, user_id, date, message, is_read)
                            VALUES (%s, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE
                                date = VALUES(date),
                                message = VALUES(message),
                                is_read = VALUES(is_read)
                        """, (
                            old_id,
                            user_id,
                            date_val,
                            row.get('message'),
                            int(row.get('is_read', 0))
                        ))
                
                conn.commit()
                
                # Update Redis with new IDs if any temp IDs were replaced
                if temp_id_mappings:
                    for i, row in enumerate(rows):
                        old_id = row.get('id')
                        if old_id and int(old_id) in temp_id_mappings:
                            rows[i]['id'] = temp_id_mappings[int(old_id)]
                    
                    # Save updated notifications back to Redis
                    redis_key = _get_redis_key('notifications', user_id)
                    _redis_client.setex(
                        redis_key,
                        INACTIVITY_TIMEOUT + 60,
                        json.dumps(rows, cls=DecimalEncoder)
                    )
                    log_info(logger, 'FLUSH', f"Updated {len(temp_id_mappings)} notification temp IDs in Redis")
                
                cursor.close()
                log_info(logger, 'FLUSH', f"→ notifications: {len(rows)} rows")
                return len(rows)
                
            elif table == 'recurring_mismatches':
                # Recurring mismatches table — tracks the enrichment provider-detected bill/wage changes
                # First, handle pending deletes
                pending_key = f"pending_deletes:recurring_mismatches:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    delete_ids = [int(id_str) for id_str in pending_deletes]
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    cursor.execute(f"""
                        DELETE FROM recurring_mismatches WHERE id IN ({placeholders}) AND user_id = %s
                    """, delete_ids + [user_id])
                    deleted_count = cursor.rowcount
                    log_info(logger, 'FLUSH', f"Deleted {deleted_count} recurring_mismatches from MySQL")
                    _redis_client.delete(pending_key)
                
                if not rows:
                    conn.commit()
                    cursor.close()
                    return 0
                
                # Track temp ID to real ID mappings
                temp_id_mappings = {}
                
                for row in rows:
                    old_id = row.get('id')
                    is_temp = old_id and int(old_id) < 0
                    
                    created_at_val = row.get('created_at')
                    if isinstance(created_at_val, str) and 'T' in created_at_val:
                        created_at_val = created_at_val.replace('T', ' ').split('.')[0]
                    
                    if is_temp:
                        cursor.execute("""
                            INSERT INTO recurring_mismatches (id, user_id, recurring_table, recurring_id, category_id, transaction_id, dismissed, created_at)
                            VALUES (NULL, %s, %s, %s, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE
                                category_id = VALUES(category_id),
                                transaction_id = VALUES(transaction_id),
                                dismissed = VALUES(dismissed),
                                created_at = VALUES(created_at)
                        """, (
                            user_id,
                            row.get('recurring_table'),
                            int(row.get('recurring_id')),
                            int(row.get('category_id')),
                            row.get('transaction_id'),
                            int(row.get('dismissed', 0)),
                            created_at_val
                        ))
                        new_id = cursor.lastrowid
                        if new_id:
                            temp_id_mappings[int(old_id)] = new_id
                            log_info(logger, 'FLUSH', f"recurring_mismatches temp ID {old_id} → real ID {new_id}")
                    else:
                        cursor.execute("""
                            INSERT INTO recurring_mismatches (id, user_id, recurring_table, recurring_id, category_id, transaction_id, dismissed, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE
                                category_id = VALUES(category_id),
                                transaction_id = VALUES(transaction_id),
                                dismissed = VALUES(dismissed),
                                created_at = VALUES(created_at)
                        """, (
                            old_id,
                            user_id,
                            row.get('recurring_table'),
                            int(row.get('recurring_id')),
                            int(row.get('category_id')),
                            row.get('transaction_id'),
                            int(row.get('dismissed', 0)),
                            created_at_val
                        ))
                
                conn.commit()
                
                # Update Redis with real IDs if any temp IDs were replaced
                if temp_id_mappings:
                    for i, row in enumerate(rows):
                        old_id = row.get('id')
                        if old_id and int(old_id) in temp_id_mappings:
                            rows[i]['id'] = temp_id_mappings[int(old_id)]
                    
                    redis_key = _get_redis_key('recurring_mismatches', user_id)
                    _redis_client.setex(
                        redis_key,
                        INACTIVITY_TIMEOUT + 60,
                        json.dumps(rows, cls=DecimalEncoder)
                    )
                    log_info(logger, 'FLUSH', f"Updated {len(temp_id_mappings)} recurring_mismatches temp IDs in Redis")
                
                cursor.close()
                log_info(logger, 'FLUSH', f"→ recurring_mismatches: {len(rows)} rows")
                return len(rows)
            
            elif table == 'recurring_suggestions':
                # Recurring suggestions table — the enrichment provider-detected suggested recurring entries
                # First, handle pending deletes
                pending_key = f"pending_deletes:recurring_suggestions:{user_id}"
                pending_deletes = _redis_client.smembers(pending_key)
                
                if pending_deletes:
                    delete_ids = [int(id_str) for id_str in pending_deletes]
                    placeholders = ','.join(['%s'] * len(delete_ids))
                    cursor.execute(f"""
                        DELETE FROM recurring_suggestions WHERE id IN ({placeholders}) AND user_id = %s
                    """, delete_ids + [user_id])
                    deleted_count = cursor.rowcount
                    log_info(logger, 'FLUSH', f"Deleted {deleted_count} recurring_suggestions from MySQL")
                    _redis_client.delete(pending_key)
                
                if not rows:
                    conn.commit()
                    cursor.close()
                    return 0
                
                # Track temp ID to real ID mappings
                temp_id_mappings = {}
                
                for row in rows:
                    old_id = row.get('id')
                    is_temp = old_id and int(old_id) < 0
                    
                    created_at_val = row.get('created_at')
                    if isinstance(created_at_val, str) and 'T' in created_at_val:
                        created_at_val = created_at_val.replace('T', ' ').split('.')[0]
                    
                    detected_amount = row.get('detected_amount')
                    if detected_amount is not None:
                        detected_amount = float(detected_amount)
                    
                    detected_cadence_interval = row.get('detected_cadence_interval')
                    if detected_cadence_interval is not None:
                        detected_cadence_interval = int(detected_cadence_interval)
                    
                    detected_monthly_day = row.get('detected_monthly_day')
                    if detected_monthly_day is not None:
                        detected_monthly_day = int(detected_monthly_day)
                    
                    if is_temp:
                        cursor.execute("""
                            INSERT INTO recurring_suggestions (id, user_id, suggestion_type, category_id, transaction_id,
                                detected_amount, detected_cadence_interval, detected_cadence_unit, detected_weekday,
                                detected_monthly_day, dismissed, created_at)
                            VALUES (NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE
                                transaction_id = VALUES(transaction_id),
                                detected_amount = VALUES(detected_amount),
                                detected_cadence_interval = VALUES(detected_cadence_interval),
                                detected_cadence_unit = VALUES(detected_cadence_unit),
                                detected_weekday = VALUES(detected_weekday),
                                detected_monthly_day = VALUES(detected_monthly_day),
                                dismissed = VALUES(dismissed),
                                created_at = VALUES(created_at)
                        """, (
                            user_id,
                            row.get('suggestion_type'),
                            int(row.get('category_id')),
                            row.get('transaction_id'),
                            detected_amount,
                            detected_cadence_interval,
                            row.get('detected_cadence_unit'),
                            row.get('detected_weekday'),
                            detected_monthly_day,
                            int(row.get('dismissed', 0)),
                            created_at_val
                        ))
                        new_id = cursor.lastrowid
                        if new_id:
                            temp_id_mappings[int(old_id)] = new_id
                            log_info(logger, 'FLUSH', f"recurring_suggestions temp ID {old_id} → real ID {new_id}")
                    else:
                        cursor.execute("""
                            INSERT INTO recurring_suggestions (id, user_id, suggestion_type, category_id, transaction_id,
                                detected_amount, detected_cadence_interval, detected_cadence_unit, detected_weekday,
                                detected_monthly_day, dismissed, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE
                                transaction_id = VALUES(transaction_id),
                                detected_amount = VALUES(detected_amount),
                                detected_cadence_interval = VALUES(detected_cadence_interval),
                                detected_cadence_unit = VALUES(detected_cadence_unit),
                                detected_weekday = VALUES(detected_weekday),
                                detected_monthly_day = VALUES(detected_monthly_day),
                                dismissed = VALUES(dismissed),
                                created_at = VALUES(created_at)
                        """, (
                            old_id,
                            user_id,
                            row.get('suggestion_type'),
                            int(row.get('category_id')),
                            row.get('transaction_id'),
                            detected_amount,
                            detected_cadence_interval,
                            row.get('detected_cadence_unit'),
                            row.get('detected_weekday'),
                            detected_monthly_day,
                            int(row.get('dismissed', 0)),
                            created_at_val
                        ))
                
                conn.commit()
                
                # Update Redis with real IDs if any temp IDs were replaced
                if temp_id_mappings:
                    for i, row in enumerate(rows):
                        old_id = row.get('id')
                        if old_id and int(old_id) in temp_id_mappings:
                            rows[i]['id'] = temp_id_mappings[int(old_id)]
                    
                    redis_key = _get_redis_key('recurring_suggestions', user_id)
                    _redis_client.setex(
                        redis_key,
                        INACTIVITY_TIMEOUT + 60,
                        json.dumps(rows, cls=DecimalEncoder)
                    )
                    log_info(logger, 'FLUSH', f"Updated {len(temp_id_mappings)} recurring_suggestions temp IDs in Redis")
                
                cursor.close()
                log_info(logger, 'FLUSH', f"→ recurring_suggestions: {len(rows)} rows")
                return len(rows)
            
            else:
                # Table not configured for flushing
                return 0
        
    except Exception as e:
        log_exception(logger, 'FLUSH', f"Error flushing {table} to MySQL for user {user_id}: {e}")
        return 0


def _initialize_ca_balances_for_account(cursor, conn, account_id):
    """
    Initialize balance records for a newly created credit account.
    Creates daily, weekly, and monthly balance records from previous year to 3 years in future.
    
    Args:
        cursor: Database cursor
        conn: Database connection
        account_id: The credit account ID to initialize balances for
    """
    from datetime import date, timedelta
    import calendar as cal
    
    try:
        today = date.today()
        start_year = today.year - 1
        end_year = today.year + 3

        # Daily: every day from Jan 1 of previous year to Dec 31 of year+3
        start_date = date(start_year, 1, 1)
        end_date = date(end_year, 12, 31)

        # Insert daily balances
        current = start_date
        while current <= end_date:
            cursor.execute("""
                INSERT IGNORE INTO c_a_balances_d (account_id, date, total_expenses, balance, total_payments)
                VALUES (%s, %s, 0, 0, 0)
            """, (account_id, current))
            current += timedelta(days=1)
        
        # Weekly: every Friday from Jan 1 of previous year to Dec 31 of year+3
        current = start_date
        # Find first Friday
        while current.weekday() != 4:  # 4 = Friday
            current += timedelta(days=1)
        
        while current <= end_date:
            cursor.execute("""
                INSERT IGNORE INTO c_a_balances (account_id, date, total_expenses, balance, total_payments)
                VALUES (%s, %s, 0, 0, 0)
            """, (account_id, current))
            current += timedelta(weeks=1)
        
        # Monthly: last day of each month
        for year in range(start_year, end_year + 1):
            for month in range(1, 13):
                last_day = date(year, month, cal.monthrange(year, month)[1])
                cursor.execute("""
                    INSERT IGNORE INTO c_a_balances_m (account_id, date, total_expenses, balance, total_payments)
                    VALUES (%s, %s, 0, 0, 0)
                """, (account_id, last_day))
        
        conn.commit()
        log_info(logger, 'FLUSH', f"Initialized balance records for credit account {account_id}")
        
    except Exception as e:
        log_exception(logger, 'FLUSH', f"Error initializing balances for account {account_id}: {e}")


def _recalculate_ca_balances_for_user(user_id):
    """
    Recalculate all CA balances (daily, weekly, monthly) for a user after entries/payments are flushed.
    This ensures balances are up-to-date with the latest MySQL data.
    
    Args:
        user_id: The user ID to recalculate balances for
    """
    try:
        from datetime import date, timedelta
        import calendar as cal
        
        with get_db_pool().get_connection() as conn:
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            
            # Get all credit accounts for this user
            cursor.execute("SELECT id FROM credit_accounts WHERE user_id = %s", (user_id,))
            account_rows = cursor.fetchall()
            
            if not account_rows:
                log_info(logger, 'FLUSH', f"No credit accounts found for user {user_id}, skipping balance recalculation")
                return
            
            # Determine date range to recalculate (from earliest entry to latest balance record)
            # Get the earliest entry date across all accounts
            cursor.execute("""
                SELECT MIN(cee.date) as min_date
                FROM c_expense_entries cee
                JOIN c_expense_categories cec ON cee.category_id = cec.id
                JOIN credit_accounts ca ON cec.account_id = ca.id
                WHERE ca.user_id = %s
            """, (user_id,))
            result = cursor.fetchone()
            start_date = result['min_date'] if result and result['min_date'] else date.today()
            
            # Recalculate each account
            for account_row in account_rows:
                account_id = account_row['id']
                _recalculate_ca_balances_for_account(cursor, conn, account_id, start_date)
            
            cursor.close()
            log_info(logger, 'FLUSH', f"Recalculated CA balances for user {user_id}")
            
    except Exception as e:
        log_exception(logger, 'FLUSH', f"Error recalculating CA balances for user {user_id}: {e}")


def _recalculate_ca_balances_for_account(cursor, conn, account_id, start_date):
    """
    Recalculate balances for a specific credit account from a given start date.
    
    Args:
        cursor: Database cursor
        conn: Database connection
        account_id: The credit account ID
        start_date: Date to start recalculation from
    """
    from datetime import timedelta
    import calendar as cal
    
    try:
        # === DAILY BALANCES ===
        # Get all daily balance dates for this account starting from start_date
        cursor.execute("""
            SELECT date FROM c_a_balances_d
            WHERE account_id = %s AND date >= %s
            ORDER BY date ASC
        """, (account_id, start_date))
        daily_dates = [row['date'] for row in cursor.fetchall()]
        
        if daily_dates:
            # Get previous day's balance
            prev_date = start_date - timedelta(days=1)
            cursor.execute("""
                SELECT balance FROM c_a_balances_d
                WHERE account_id = %s AND date = %s
            """, (account_id, prev_date))
            prev_row = cursor.fetchone()
            running_balance = float(prev_row['balance']) if prev_row and prev_row['balance'] else 0.0
            
            # Get all expenses and payments for this account
            cursor.execute("""
                SELECT cee.date, cee.amount
                FROM c_expense_entries cee
                JOIN c_expense_categories cec ON cee.category_id = cec.id
                WHERE cec.account_id = %s AND cee.date >= %s
                ORDER BY cee.date ASC
            """, (account_id, start_date))
            expenses_by_date = {}
            for row in cursor.fetchall():
                date_key = row['date']
                expenses_by_date[date_key] = expenses_by_date.get(date_key, 0) + float(row['amount'])
            
            cursor.execute("""
                SELECT date, amount
                FROM c_payment_entries
                WHERE account_id = %s AND date >= %s
                ORDER BY date ASC
            """, (account_id, start_date))
            payments_by_date = {}
            for row in cursor.fetchall():
                date_key = row['date']
                payments_by_date[date_key] = payments_by_date.get(date_key, 0) + float(row['amount'])
            
            # Update each date
            updates = []
            for date_val in daily_dates:
                day_expenses = expenses_by_date.get(date_val, 0)
                day_payments = payments_by_date.get(date_val, 0)
                running_balance = running_balance + day_expenses - day_payments
                updates.append((day_expenses, running_balance, day_payments, account_id, date_val))
            
            if updates:
                cursor.executemany("""
                    UPDATE c_a_balances_d
                    SET total_expenses = %s, balance = %s, total_payments = %s
                    WHERE account_id = %s AND date = %s
                """, updates)
        
        # === WEEKLY BALANCES (Saturdays) ===
        cursor.execute("""
            SELECT date FROM c_a_balances
            WHERE account_id = %s AND date >= %s
            ORDER BY date ASC
        """, (account_id, start_date))
        weekly_dates = [row['date'] for row in cursor.fetchall()]
        
        if weekly_dates:
            updates = []
            for date_val in weekly_dates:
                # Sum up daily balances for the week ending on this Saturday
                week_start = date_val - timedelta(days=6)
                cursor.execute("""
                    SELECT SUM(total_expenses) as total_exp, SUM(total_payments) as total_pay
                    FROM c_a_balances_d
                    WHERE account_id = %s AND date BETWEEN %s AND %s
                """, (account_id, week_start, date_val))
                row = cursor.fetchone()
                week_expenses = float(row['total_exp']) if row and row['total_exp'] else 0.0
                week_payments = float(row['total_pay']) if row and row['total_pay'] else 0.0
                
                # Get balance at end of week
                cursor.execute("""
                    SELECT balance FROM c_a_balances_d
                    WHERE account_id = %s AND date = %s
                """, (account_id, date_val))
                row = cursor.fetchone()
                week_balance = float(row['balance']) if row and row['balance'] else 0.0
                
                updates.append((week_expenses, week_balance, week_payments, account_id, date_val))
            
            if updates:
                cursor.executemany("""
                    UPDATE c_a_balances
                    SET total_expenses = %s, balance = %s, total_payments = %s
                    WHERE account_id = %s AND date = %s
                """, updates)
        
        # === MONTHLY BALANCES ===
        cursor.execute("""
            SELECT date FROM c_a_balances_m
            WHERE account_id = %s AND date >= %s
            ORDER BY date ASC
        """, (account_id, start_date))
        monthly_dates = [row['date'] for row in cursor.fetchall()]
        
        if monthly_dates:
            updates = []
            for date_val in monthly_dates:
                # Get the last day of this month
                import calendar
                last_day = calendar.monthrange(date_val.year, date_val.month)[1]
                month_end = date_val.replace(day=last_day)
                
                # Sum up daily balances for the entire month
                cursor.execute("""
                    SELECT SUM(total_expenses) as total_exp, SUM(total_payments) as total_pay
                    FROM c_a_balances_d
                    WHERE account_id = %s AND date BETWEEN %s AND %s
                """, (account_id, date_val, month_end))
                row = cursor.fetchone()
                month_expenses = float(row['total_exp']) if row and row['total_exp'] else 0.0
                month_payments = float(row['total_pay']) if row and row['total_pay'] else 0.0
                
                # Get balance at end of month
                cursor.execute("""
                    SELECT balance FROM c_a_balances_d
                    WHERE account_id = %s AND date = %s
                """, (account_id, month_end))
                row = cursor.fetchone()
                month_balance = float(row['balance']) if row and row['balance'] else 0.0
                
                updates.append((month_expenses, month_balance, month_payments, account_id, date_val))
            
            if updates:
                cursor.executemany("""
                    UPDATE c_a_balances_m
                    SET total_expenses = %s, balance = %s, total_payments = %s
                    WHERE account_id = %s AND date = %s
                """, updates)
        
        conn.commit()
        log_info(logger, 'FLUSH', f"Recalculated balances for account {account_id}")
        
    except Exception as e:
        log_exception(logger, 'FLUSH', f"Error recalculating balances for account {account_id}: {e}")


def flush_dirty_tables_for_user(user_id: int):
    """
    Synchronously flush all dirty tables for a specific user.
    
    This is used when we need immediate MySQL synchronization (e.g., to get real IDs
    or ensure balance records exist before updating them).
    
    Args:
        user_id: User ID to flush
        
    Returns:
        Total number of rows flushed
    """
    try:
        log_info(logger, 'FLUSH', f"Forcing immediate flush for user {user_id}")
        
        tables_to_flush = [
            'totals_remainders',
            'totals_remainders_d', 
            'totals_remainders_m',
            'savings_entries',
            'credit_accounts',  # Must flush FIRST - c_a_balances tables depend on this!
            'c_a_balances',
            'c_a_balances_d',
            'c_a_balances_m',
            'income_categories',  # Category definitions
            'expense_categories',
            'c_expense_categories',
            'income_entries',
            'expense_entries',
            'c_expense_entries',
            'recurring_income',  # Recurring entry configurations
            'recurring_expense',
            'recurring_c_expense',
            'recurring_income_buckets',  # Bucket state tracking
            'recurring_expense_buckets',
            'recurring_c_expense_buckets',
            'buds',  # Must flush before bud_items to resolve temp IDs
            'bud_items',
            'users',  # User settings (balance_threshold, starting_savings)
            'notifications',  # User notifications
            'setup_state',  # Setup wizard temporary state
            'recurring_mismatches',  # provider recurring mismatch detection
            'recurring_suggestions',  # the enrichment provider suggested recurring entries
        ]
        
        # Get dirty tables for this user
        dirty_tables_key = f"dirty_tables:{user_id}"
        dirty_tables_raw = _redis_client.smembers(dirty_tables_key)
        
        # Decode bytes to strings
        dirty_tables = set()
        for dt in dirty_tables_raw:
            if isinstance(dt, bytes):
                dirty_tables.add(dt.decode('utf-8'))
            else:
                dirty_tables.add(dt)
        
        if not dirty_tables:
            log_info(logger, 'FLUSH', f"No dirty tables for user {user_id}")
            return 0
        
        log_info(logger, 'FLUSH', f"User {user_id} has {len(dirty_tables)} dirty tables: {', '.join(dirty_tables)}")
        
        total_flushed = 0
        
        # Flush only dirty tables
        for table in tables_to_flush:
            # Skip if table is not dirty
            if table not in dirty_tables:
                continue
            
            log_info(logger, 'FLUSH', f"Processing dirty table: {table} for user {user_id}")
            flushed_count = _flush_table_to_mysql(table, user_id)
            
            if flushed_count > 0:
                total_flushed += flushed_count
                log_info(logger, 'FLUSH', f"Cleared dirty flag for {table} (flushed={flushed_count})")
            
            # Remove from dirty set after processing
            _redis_client.srem(dirty_tables_key, table)
        
        log_info(logger, 'FLUSH', f"Forced flush complete for user {user_id}: {total_flushed} rows")
        return total_flushed
        
    except Exception as e:
        log_exception(logger, 'FLUSH', f"Error in forced flush for user {user_id}: {e}")
        return 0


def _flush_worker():
    """
    Background worker that periodically flushes Redis data to MySQL.
    Runs continuously until shutdown.
    """
    log_info(logger, 'REDIS', "Flush worker started")
    
    while not _shutdown_event.is_set():
        try:
            _flush_redis_to_mysql()
            
            # Sleep for flush interval
            _shutdown_event.wait(FLUSH_INTERVAL)
            
        except Exception as e:
            log_exception(logger, 'REDIS', f"Error in flush worker: {e}")
            _shutdown_event.wait(FLUSH_INTERVAL)
    
    log_info(logger, 'REDIS', "Flush worker stopped")


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
        
        log_info(logger, 'REDIS', f"Sent refresh signal for user {user_id}")
        
    except Exception as e:
        log_error(logger, 'REDIS', f"Error signaling frontend refresh for user {user_id}: {e}")


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
        log_error(logger, 'REDIS', f"Error getting cached data for {table}, user {user_id}: {e}")
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
        log_info(logger, 'REDIS', f"Set cached data for {table}, user {user_id}: {len(data)} rows")
        
    except Exception as e:
        log_error(logger, 'REDIS', f"Error setting cached data for {table}, user {user_id}: {e}")


def invalidate_user_cache(user_id: int):
    """
    Manually invalidate/dehydrate a user's cache.
    Useful when you want to force a fresh load from MySQL.
    
    Args:
        user_id: User ID
    """
    log_info(logger, 'REDIS', f"Manually invalidating cache for user {user_id}")
    _dehydrate_user_data(user_id)
