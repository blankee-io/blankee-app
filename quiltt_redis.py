"""
Redis-First Operations for Quiltt Integration

This module provides Redis-first CRUD operations for Quiltt data (connections, accounts, transactions).
Data is written to Redis immediately and flushed to MySQL periodically by the Redis manager.
"""

import logging
import json
import time
import pymysql.cursors
from typing import Optional, List, Dict, Any
from datetime import datetime
from decimal import Decimal
from flask_login import current_user
from redis_manager import (
    _get_redis_key, 
    REDIS_KEY_VERSION,
    is_user_hydrated,
    DecimalEncoder,
    INACTIVITY_TIMEOUT
)
from db_connections import get_db_pool

logger = logging.getLogger(__name__)


def _get_redis_client():
    """Get Redis client - imported inside function to avoid None at module load time"""
    from redis_manager import _redis_client
    return _redis_client


def _get_from_redis(table: str, user_id: int) -> Optional[List[Dict[str, Any]]]:
    """Get Quiltt data from Redis"""
    redis_client = _get_redis_client()
    if not redis_client or not is_user_hydrated(user_id):
        return None
    
    redis_key = _get_redis_key(table, user_id)
    cached = redis_client.get(redis_key)
    
    if cached:
        return json.loads(cached)
    return None


def _get_all_quiltt_accounts_raw(user_id: int) -> List[Dict[str, Any]]:
    """
    Get ALL quiltt accounts for a user, bypassing hydration check and is_active filter.
    
    Used internally by upsert/update/delete operations that need the complete account list.
    Unlike get_quiltt_accounts(), this:
    1. Reads directly from the Redis key (no hydration check)
    2. Falls back to MySQL WITHOUT is_active=1 filter
    """
    redis_client = _get_redis_client()
    
    # Try Redis key directly (bypasses hydration check)
    if redis_client:
        redis_key = _get_redis_key('quiltt_accounts', user_id)
        cached = redis_client.get(redis_key)
        if cached:
            return json.loads(cached)
    
    # Fall back to MySQL - get ALL accounts without is_active filter
    try:
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT * FROM quiltt_accounts WHERE user_id = %s", (user_id,))
            result = list(cursor.fetchall())
            logger.info(f"_get_all_quiltt_accounts_raw: loaded {len(result)} accounts from MySQL for user {user_id}")
            return result
    except Exception as e:
        logger.error(f"Error getting all quiltt accounts from MySQL: {e}")
        return []


def _set_to_redis(table: str, user_id: int, data: List[Dict[str, Any]]) -> bool:
    """Set Quiltt data to Redis and mark as dirty"""
    redis_client = _get_redis_client()
    if not redis_client:
        logger.error(f"Redis client not available for {table}")
        return False
    
    try:
        redis_key = _get_redis_key(table, user_id)
        logger.info(f"Setting Redis key {redis_key} with {len(data)} records")
        redis_client.setex(
            redis_key,
            INACTIVITY_TIMEOUT + 60,
            json.dumps(data, cls=DecimalEncoder)
        )
        
        # Mark table as dirty for periodic flush to MySQL
        dirty_key = f"dirty_tables:{user_id}"
        redis_client.sadd(dirty_key, table)
        logger.info(f"Marked {table} as dirty for user {user_id}")
        
        return True
    except Exception as e:
        logger.error(f"Error setting Redis data for {table}: {e}", exc_info=True)
        return False


def _set_to_redis_no_dirty(table: str, user_id: int, data: List[Dict[str, Any]]) -> bool:
    """Set Quiltt data to Redis WITHOUT marking as dirty (for deletion operations)"""
    redis_client = _get_redis_client()
    if not redis_client:
        logger.error(f"Redis client not available for {table}")
        return False
    
    try:
        redis_key = _get_redis_key(table, user_id)
        logger.info(f"Setting Redis key {redis_key} with {len(data)} records (no dirty flag)")
        redis_client.setex(
            redis_key,
            INACTIVITY_TIMEOUT + 60,
            json.dumps(data, cls=DecimalEncoder)
        )
        return True
    except Exception as e:
        logger.error(f"Error setting Redis data for {table}: {e}", exc_info=True)
        return False


def get_quiltt_profile(user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    Get user's Quiltt profile from Redis or MySQL.
    
    Returns:
        Profile dict or None
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return None
        user_id = current_user.id
    
    # Try Redis first
    if is_user_hydrated(user_id):
        cached_data = _get_from_redis('quiltt_profiles', user_id)
        if cached_data and len(cached_data) > 0:
            return cached_data[0]
    
    # Fallback to MySQL
    try:
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT * FROM quiltt_profiles WHERE user_id = %s",
                (user_id,)
            )
            return cursor.fetchone()
    except Exception as e:
        logger.error(f"Error getting Quiltt profile from MySQL: {e}")
        return None


def update_quiltt_profile(profile_data: Dict[str, Any], user_id: Optional[int] = None) -> bool:
    """
    Update or create Quiltt profile (Redis-only, MySQL flush happens periodically).
    
    Args:
        profile_data: Dict with profile fields (profile_id, session_token, session_expires_at, etc.)
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        True if successful
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return False
        user_id = current_user.id
    
    try:
        # Write to Redis only - flush worker will sync to MySQL
        cached_data = _get_from_redis('quiltt_profiles', user_id)
        logger.info(f"Got cached_data from Redis for user {user_id}: {cached_data}")
        
        if cached_data is None:
            # Not in Redis yet - load from MySQL if exists
            try:
                with get_db_pool().get_cursor(dictionary=True) as cursor:
                    cursor.execute(
                        "SELECT * FROM quiltt_profiles WHERE user_id = %s",
                        (user_id,)
                    )
                    existing = cursor.fetchone()
                    if existing:
                        cached_data = [existing]
                        logger.info(f"Loaded existing profile from MySQL for user {user_id}")
                    else:
                        cached_data = []
                        logger.info(f"No existing profile in MySQL for user {user_id}")
            except Exception as e:
                logger.error(f"Error loading profile from MySQL: {e}")
                cached_data = []
        
        if len(cached_data) > 0:
            # Update existing entry
            cached_data[0].update(profile_data)
            cached_data[0]['user_id'] = user_id
            logger.info(f"Updated existing profile for user {user_id}")
        else:
            # Add new entry
            cached_data.append({'user_id': user_id, **profile_data})
            logger.info(f"Created new profile for user {user_id}")
        
        logger.info(f"About to save to Redis: {cached_data}")
        # Save to Redis and mark as dirty
        result = _set_to_redis('quiltt_profiles', user_id, cached_data)
        logger.info(f"Redis save result: {result}")
        
        return True
        
    except Exception as e:
        logger.error(f"Error updating Quiltt profile: {e}", exc_info=True)
        return False


def get_quiltt_connections(user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Get user's Quiltt connections from Redis or MySQL.
    
    Returns:
        List of connection dicts
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return []
        user_id = current_user.id
    
    # Try Redis first
    if is_user_hydrated(user_id):
        cached_data = _get_from_redis('quiltt_connections', user_id)
        if cached_data is not None:
            return cached_data
    
    # Fallback to MySQL
    try:
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT * FROM quiltt_connections WHERE user_id = %s ORDER BY last_synced_at DESC",
                (user_id,)
            )
            return cursor.fetchall()
    except Exception as e:
        logger.error(f"Error getting Quiltt connections from MySQL: {e}")
        return []


def upsert_quiltt_connection(connection_data: Dict[str, Any], user_id: Optional[int] = None) -> Optional[int]:
    """
    Insert or update a Quiltt connection (Redis-only, MySQL flush happens periodically).
    
    Args:
        connection_data: Dict with connection fields (connection_id, institution_name, status, etc.)
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        Connection database ID or None
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return None
        user_id = current_user.id
    
    connection_id = connection_data.get('connection_id')
    if not connection_id:
        logger.error("connection_id is required")
        return None
    
    try:
        # Get current data from Redis or MySQL
        cached_data = _get_from_redis('quiltt_connections', user_id)
        
        if cached_data is None:
            # Load from MySQL if not in Redis
            cached_data = get_quiltt_connections(user_id)
            if cached_data is None:
                cached_data = []
        
        # Ensure cached_data is a list, not a tuple
        if not isinstance(cached_data, list):
            cached_data = list(cached_data) if cached_data else []
        
        # Find existing or generate new ID
        db_id = None
        found = False
        for i, conn in enumerate(cached_data):
            if conn.get('connection_id') == connection_id:
                cached_data[i].update(connection_data)
                cached_data[i]['last_synced_at'] = datetime.now()
                db_id = cached_data[i].get('id')
                found = True
                break
        
        if not found:
            # Generate temporary ID for new connection (will be replaced by MySQL auto-increment on flush)
            import time
            temp_id = int(time.time() * 1000) % 1000000  # Use timestamp as temp ID
            db_id = temp_id
            new_conn = {
                'id': db_id,
                'user_id': user_id,
                'last_synced_at': datetime.now(),
                **connection_data
            }
            cached_data.append(new_conn)
        
        # Save to Redis and mark as dirty
        _set_to_redis('quiltt_connections', user_id, cached_data)
        
        return db_id
        
    except Exception as e:
        logger.error(f"Error upserting Quiltt connection: {e}", exc_info=True)
        return None


def get_quiltt_accounts(user_id: Optional[int] = None, connection_db_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Get user's Quiltt accounts from Redis or MySQL.
    
    Args:
        user_id: User ID (defaults to current_user.id)
        connection_db_id: Optional filter by connection database ID
        
    Returns:
        List of account dicts
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return []
        user_id = current_user.id
    
    # Try Redis first
    if is_user_hydrated(user_id):
        cached_data = _get_from_redis('quiltt_accounts', user_id)
        if cached_data is not None:
            # Filter by connection if specified
            if connection_db_id is not None:
                cached_data = [a for a in cached_data if a.get('connection_id') == connection_db_id]
            
            # Filter to only include relevant account types (DEPOSITORY for checking/savings, CREDIT)
            filtered_accounts = []
            for account in cached_data:
                account_type = account.get('account_type', '').upper()
                account_name_lower = account.get('account_name', '').lower()
                
                # Include DEPOSITORY accounts with 'checking' or 'savings' in name
                if account_type == 'DEPOSITORY':
                    if 'checking' in account_name_lower or 'savings' in account_name_lower:
                        filtered_accounts.append(account)
                # Include all CREDIT accounts
                elif account_type == 'CREDIT':
                    filtered_accounts.append(account)
            
            return filtered_accounts
    
    # Fallback to MySQL
    try:
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            if connection_db_id is not None:
                cursor.execute("""
                    SELECT * FROM quiltt_accounts 
                    WHERE user_id = %s AND connection_id = %s AND is_active = 1
                    AND (
                        (account_type = 'CREDIT')
                        OR (account_type = 'DEPOSITORY' AND (
                            LOWER(account_name) LIKE '%%checking%%' 
                            OR LOWER(account_name) LIKE '%%savings%%'
                        ))
                    )
                """, (user_id, connection_db_id))
            else:
                cursor.execute("""
                    SELECT * FROM quiltt_accounts 
                    WHERE user_id = %s AND is_active = 1
                    AND (
                        (account_type = 'CREDIT')
                        OR (account_type = 'DEPOSITORY' AND (
                            LOWER(account_name) LIKE '%%checking%%' 
                            OR LOWER(account_name) LIKE '%%savings%%'
                        ))
                    )
                """, (user_id,))
            return cursor.fetchall()
    except Exception as e:
        logger.error(f"Error getting Quiltt accounts from MySQL: {e}")
        return []


def upsert_quiltt_account(account_data: Dict[str, Any], user_id: Optional[int] = None) -> Optional[int]:
    """
    Insert or update a Quiltt account (Redis-only, MySQL flush happens periodically).
    
    Args:
        account_data: Dict with account fields (account_id, connection_id, account_name, etc.)
                      Note: connection_id should be the MySQL ID from quiltt_connections.id
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        Account database ID or None
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return None
        user_id = current_user.id
    
    account_id = account_data.get('account_id')
    mysql_connection_id = account_data.get('connection_id')  # MySQL ID from quiltt_connections
    if not account_id:
        logger.error("account_id is required")
        return None
    
    if not mysql_connection_id:
        logger.error("connection_id is required")
        return None
    
    try:
        # Get ALL quiltt accounts (bypasses hydration check and is_active filter)
        # This is critical: update-account may have written is_active=1 to Redis,
        # but if user isn't hydrated, _get_from_redis would return None and
        # get_quiltt_accounts filters by is_active=1 in MySQL (stale data).
        cached_data = _get_all_quiltt_accounts_raw(user_id)
        
        # Ensure cached_data is a list, not a tuple
        if not isinstance(cached_data, list):
            cached_data = list(cached_data) if cached_data else []
        
        # Find existing or generate new ID
        db_id = None
        found = False
        for i, acc in enumerate(cached_data):
            if acc.get('account_id') == account_id:
                # Update existing account - only update non-None fields to preserve user settings
                liability_fields = {
                    'interest_rate', 'origination_principal', 'origination_date', 'maturity_date',
                    'loan_term', 'last_payment_date', 'last_payment_amount', 'next_payment_due_date',
                    'minimum_payment_amount', 'next_payment_minimum_amount', 'payment_frequency', 'account_state'
                }
                for key, value in account_data.items():
                    # Don't overwrite user settings, alias, or liability data with None
                    if value is None and key in ('is_active', 'sync_transactions', 'alias', *liability_fields):
                        continue
                    # Never overwrite user-set alias from sync data
                    if key == 'alias' and cached_data[i].get('alias') and value is None:
                        continue
                    cached_data[i][key] = value
                db_id = cached_data[i].get('id')
                found = True
                break
        
        if not found:
            # Generate temporary ID for new account
            import time
            temp_id = int(time.time() * 1000) % 1000000
            db_id = temp_id
            new_acc = {
                'id': db_id,
                'user_id': user_id,
                **account_data
            }
            cached_data.append(new_acc)
        
        # Save to Redis and mark as dirty
        _set_to_redis('quiltt_accounts', user_id, cached_data)
        
        return db_id
        
    except Exception as e:
        logger.error(f"Error upserting Quiltt account: {e}", exc_info=True)
        return None


def update_quiltt_account_field(account_id: str, field: str, value: Any, user_id: Optional[int] = None) -> bool:
    """
    Update a single field in a Quiltt account (Redis-only, MySQL flush happens periodically).
    
    Args:
        account_id: Quiltt account ID
        field: Field name to update
        value: New value
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        True if successful
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return False
        user_id = current_user.id
    
    try:
        # Get ALL quiltt accounts (bypasses hydration check and is_active filter)
        cached_data = _get_all_quiltt_accounts_raw(user_id)
        
        # Ensure cached_data is a list, not a tuple
        if not isinstance(cached_data, list):
            cached_data = list(cached_data) if cached_data else []
        
        # Find and update the account
        found = False
        for i, acc in enumerate(cached_data):
            if acc.get('account_id') == account_id:
                cached_data[i][field] = value
                found = True
                break
        
        if not found:
            logger.warning(f"Account {account_id} not found in cached data for user {user_id}")
            return False
        
        # Save to Redis and mark as dirty
        _set_to_redis('quiltt_accounts', user_id, cached_data)
        
        logger.info(f"Successfully updated {field}={value} for account {account_id}")
        return True
        
    except Exception as e:
        logger.error(f"Error updating Quiltt account field: {e}", exc_info=True)
        return False


def update_quiltt_account_fields(account_id: str, fields: dict, user_id: Optional[int] = None) -> bool:
    """
    Update multiple fields in a Quiltt account atomically (Redis-only, MySQL flush happens periodically).
    This prevents race conditions when multiple fields need to be updated together.
    
    Args:
        account_id: Quiltt account ID
        fields: Dictionary of field names and values to update (e.g. {'is_active': 1, 'sync_transactions': 1})
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        True if successful
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return False
        user_id = current_user.id
    
    try:
        redis_client = _get_redis_client()
        
        # Get Redis lock key for this user's accounts
        lock_key = f"lock:quiltt_accounts:{user_id}"
        
        # Try to acquire lock for up to 5 seconds
        lock_acquired = False
        for attempt in range(50):  # 50 attempts x 100ms = 5 seconds max
            if redis_client.set(lock_key, '1', nx=True, ex=10):  # Lock expires in 10 seconds
                lock_acquired = True
                break
            time.sleep(0.1)  # Wait 100ms between attempts
        
        if not lock_acquired:
            logger.error(f"Failed to acquire lock for user {user_id} accounts")
            return False
        
        try:
            # Get ALL quiltt accounts (bypasses hydration check and is_active filter)
            cached_data = _get_all_quiltt_accounts_raw(user_id)
            
            # Ensure cached_data is a list, not a tuple
            if not isinstance(cached_data, list):
                cached_data = list(cached_data) if cached_data else []
            
            # Find and update the account
            found = False
            for i, acc in enumerate(cached_data):
                if acc.get('account_id') == account_id:
                    # Update all fields atomically
                    for field, value in fields.items():
                        cached_data[i][field] = value
                    found = True
                    break
            
            if not found:
                logger.warning(f"Account {account_id} not found in cached data for user {user_id} ({len(cached_data)} accounts checked)")
                return False
            
            # Save to Redis and mark as dirty
            _set_to_redis('quiltt_accounts', user_id, cached_data)
            
            logger.info(f"Successfully updated {len(fields)} fields for account {account_id}: {fields}")
            return True
            
        finally:
            # Always release the lock
            redis_client.delete(lock_key)
        
    except Exception as e:
        logger.error(f"Error updating Quiltt account fields: {e}", exc_info=True)
        return False


def delete_quiltt_connection(connection_id: str, user_id: Optional[int] = None) -> bool:
    """
    Delete a Quiltt connection and all its accounts (Redis-first, MySQL flush happens periodically).
    
    Args:
        connection_id: Quiltt connection ID
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        True if successful
    """
    # Use print for debugging since logger may not be configured
    import sys
    def debug_log(msg):
        print(f"[DELETE_CONN] {msg}", file=sys.stderr, flush=True)
    
    debug_log(f"delete_quiltt_connection called for connection_id={connection_id}, user_id={user_id}")
    
    if user_id is None:
        if not current_user.is_authenticated:
            debug_log("user_id is None and no authenticated user")
            return False
        user_id = current_user.id
    
    debug_log(f"Processing delete for user {user_id}")
    
    try:
        redis_client = _get_redis_client()
        debug_log(f"Got redis_client: {redis_client is not None}")
        if not redis_client:
            debug_log("Redis client not available for delete, falling back to direct MySQL")
            # Fallback to direct MySQL deletion
            from db_connections import get_db_pool
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor()
                # Delete accounts first (foreign key constraint)
                cursor.execute("""
                    DELETE FROM quiltt_accounts 
                    WHERE user_id = %s AND connection_id IN (
                        SELECT id FROM quiltt_connections WHERE connection_id = %s
                    )
                """, (user_id, connection_id))
                # Delete connection
                cursor.execute("""
                    DELETE FROM quiltt_connections 
                    WHERE user_id = %s AND connection_id = %s
                """, (user_id, connection_id))
                conn.commit()
                cursor.close()
            return True
        
        # Get connection's db_id before deleting
        debug_log(f"Getting connections for user {user_id}")
        connections = get_quiltt_connections(user_id)
        debug_log(f"Got {len(connections)} connections")
        conn_db_id = None
        for conn in connections:
            if conn.get('connection_id') == connection_id:
                conn_db_id = conn.get('id')
                break
        
        debug_log(f"Found conn_db_id={conn_db_id} for connection_id={connection_id}")
        
        # Remove connection from Redis (without marking dirty - deletion is handled separately)
        cached_connections = _get_from_redis('quiltt_connections', user_id)
        debug_log(f"Got {len(cached_connections) if cached_connections else 0} cached connections from Redis")
        if cached_connections is None:
            cached_connections = connections
        
        before_count = len(cached_connections)
        cached_connections = [c for c in cached_connections if c.get('connection_id') != connection_id]
        after_count = len(cached_connections)
        debug_log(f"Filtered connections: {before_count} -> {after_count}")
        
        debug_log(f"About to call _set_to_redis_no_dirty for quiltt_connections")
        result = _set_to_redis_no_dirty('quiltt_connections', user_id, cached_connections)
        debug_log(f"_set_to_redis_no_dirty returned: {result}")
        
        debug_log(f"After deletion, user {user_id} has {len(cached_connections)} connection(s) remaining")
        
        # Get account_ids to delete their transactions
        account_ids_to_delete = []
        if conn_db_id:
            # Use _get_all_quiltt_accounts_raw to bypass hydration check
            cached_accounts = _get_all_quiltt_accounts_raw(user_id)
            
            if cached_accounts:
                # Collect account_ids before removing accounts
                account_ids_to_delete = [
                    a.get('account_id') for a in cached_accounts 
                    if a.get('connection_id') == conn_db_id and a.get('account_id')
                ]
                # Remove accounts (without marking dirty - deletion is handled separately)
                cached_accounts = [a for a in cached_accounts if a.get('connection_id') != conn_db_id]
                _set_to_redis_no_dirty('quiltt_accounts', user_id, cached_accounts)
        
        # Remove associated transactions from Redis (cascade delete)
        if account_ids_to_delete:
            cached_transactions = _get_from_redis('quiltt_transactions', user_id)
            if cached_transactions is None:
                cached_transactions = get_quiltt_transactions(user_id)
            
            if cached_transactions:
                # Filter out transactions for deleted accounts
                cached_transactions = [
                    t for t in cached_transactions 
                    if t.get('account_id') not in account_ids_to_delete
                ]
                _set_to_redis_no_dirty('quiltt_transactions', user_id, cached_transactions)
        
        # Mark tables as dirty for deletion flush
        dirty_key = f"dirty_tables:{user_id}"
        redis_client.sadd(dirty_key, 'quiltt_connections_deleted')
        redis_client.sadd(dirty_key, 'quiltt_accounts_deleted')
        redis_client.sadd(dirty_key, 'quiltt_transactions_deleted')
        
        # Store the connection_id to delete
        delete_key = f"quiltt_connections_to_delete:{user_id}"
        redis_client.sadd(delete_key, connection_id)
        redis_client.expire(delete_key, 300)  # Expire in 5 minutes
        
        logger.info(f"Marked Quiltt connection {connection_id} for deletion (user {user_id})")
        
        # Check if this was the last connection - if so, delete the Quiltt profile
        logger.info(f"Checking if last connection: len(cached_connections) = {len(cached_connections)}")
        if len(cached_connections) == 0:
            logger.info(f"Last connection deleted for user {user_id}, deleting Quiltt profile")
            try:
                from quiltt_utils import QuilttClient
                from db_connections import get_db_pool
                import pymysql
                
                # Get profile_id from database
                with get_db_pool().get_connection() as conn:
                    cursor = conn.cursor(pymysql.cursors.DictCursor)
                    cursor.execute("SELECT profile_id FROM quiltt_profiles WHERE user_id = %s", (user_id,))
                    profile = cursor.fetchone()
                    cursor.close()
                    
                if profile and profile.get('profile_id'):
                    quiltt_client = QuilttClient()
                    success = quiltt_client.delete_profile(profile['profile_id'])
                    if success:
                        logger.info(f"Successfully deleted Quiltt profile {profile['profile_id']} for user {user_id}")
                        
                        # Delete quiltt_profiles record from database
                        with get_db_pool().get_connection() as conn:
                            cursor = conn.cursor()
                            cursor.execute("DELETE FROM quiltt_profiles WHERE user_id = %s", (user_id,))
                            conn.commit()
                            cursor.close()
                        
                        # Delete quiltt_profiles from Redis cache
                        profiles_key = f"quiltt_profiles:v1:{user_id}"
                        redis_client.delete(profiles_key)
                        
                        logger.info(f"Deleted quiltt_profiles record and Redis cache for user {user_id}")
                    else:
                        logger.warning(f"Failed to delete Quiltt profile {profile['profile_id']} for user {user_id}")
                else:
                    logger.warning(f"No Quiltt profile found in database for user {user_id}")
            except Exception as e:
                logger.error(f"Error deleting Quiltt profile: {e}", exc_info=True)
        else:
            logger.info(f"User {user_id} still has {len(cached_connections)} connection(s), not deleting profile")
        
        return True
        
    except Exception as e:
        logger.error(f"Error deleting Quiltt connection: {e}", exc_info=True)
        return False


def get_quiltt_transactions(user_id: Optional[int] = None, account_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Get Quiltt transactions from Redis (or MySQL if not cached).
    
    Args:
        user_id: User ID (defaults to current_user.id)
        account_id: Optional Quiltt account_id to filter by
        
    Returns:
        List of transaction dicts
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return []
        user_id = current_user.id
    
    try:
        # Try Redis first
        cached_data = _get_from_redis('quiltt_transactions', user_id)
        
        if cached_data is None:
            # Fallback to MySQL
            from db_connections import get_db_pool
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute("""
                    SELECT * FROM quiltt_transactions 
                    WHERE user_id = %s
                    ORDER BY date DESC, created_at DESC
                """, (user_id,))
                cached_data = cursor.fetchall()
                cursor.close()
            
            # Cache in Redis
            if cached_data:
                _set_to_redis('quiltt_transactions', user_id, list(cached_data))
        
        # Filter by account_id if provided
        if account_id and cached_data:
            cached_data = [t for t in cached_data if t.get('account_id') == account_id]
        
        return cached_data or []
        
    except Exception as e:
        logger.error(f"Error getting Quiltt transactions: {e}", exc_info=True)
        return []


def upsert_quiltt_transaction(transaction_data: Dict[str, Any], user_id: Optional[int] = None) -> Optional[int]:
    """
    Insert or update a Quiltt transaction in Redis (MySQL flush happens periodically).
    
    Args:
        transaction_data: Dict with transaction fields (transaction_id, account_id, amount, date, etc.)
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        Transaction database ID or None
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return None
        user_id = current_user.id
    
    transaction_id = transaction_data.get('transaction_id')
    if not transaction_id:
        logger.error("transaction_id is required")
        return None
    
    try:
        # Get current data from Redis or MySQL
        cached_data = _get_from_redis('quiltt_transactions', user_id)
        
        if cached_data is None:
            # Load from MySQL if not in Redis
            cached_data = get_quiltt_transactions(user_id)
            if cached_data is None:
                cached_data = []
        
        # Ensure cached_data is a list
        if not isinstance(cached_data, list):
            cached_data = list(cached_data) if cached_data else []
        
        # Find existing or generate new ID
        db_id = None
        found = False
        for i, txn in enumerate(cached_data):
            if txn.get('transaction_id') == transaction_id:
                # Update existing transaction
                logger.info(f"BEFORE update: {transaction_id} has ntropy_enriched_at={cached_data[i].get('ntropy_enriched_at')}")
                logger.info(f"NEW DATA: ntropy_enriched_at={transaction_data.get('ntropy_enriched_at')}")
                cached_data[i].update(transaction_data)
                db_id = cached_data[i].get('id')
                found = True
                logger.info(f"AFTER update: {transaction_id} has ntropy_enriched_at={cached_data[i].get('ntropy_enriched_at')}")
                break
        
        if not found:
            # Generate temporary ID for new transaction
            import time
            temp_id = int(time.time() * 1000) % 1000000
            db_id = temp_id
            new_txn = {
                'id': db_id,
                'user_id': user_id,
                **transaction_data
            }
            cached_data.append(new_txn)
        
        # Save to Redis and mark as dirty
        _set_to_redis('quiltt_transactions', user_id, cached_data)
        
        return db_id
        
    except Exception as e:
        logger.error(f"Error upserting Quiltt transaction: {e}", exc_info=True)
        return None


def delete_quiltt_accounts_by_ids(account_ids: List[str], user_id: Optional[int] = None) -> bool:
    """
    Delete specific Quiltt accounts by their account_id strings from Redis and MySQL.
    Used when canceling account selection for an existing connection - only removes
    the newly-added accounts, not the entire connection.
    
    Args:
        account_ids: List of Quiltt account_id strings to delete
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        True if successful, False otherwise
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return False
        user_id = current_user.id
    
    if not account_ids:
        return True
    
    account_ids_set = set(account_ids)
    
    try:
        # Remove accounts from Redis
        cached_accounts = _get_all_quiltt_accounts_raw(user_id)
        if cached_accounts:
            if not isinstance(cached_accounts, list):
                cached_accounts = list(cached_accounts)
            
            original_count = len(cached_accounts)
            cached_accounts = [a for a in cached_accounts if a.get('account_id') not in account_ids_set]
            removed_count = original_count - len(cached_accounts)
            
            if removed_count > 0:
                _set_to_redis('quiltt_accounts', user_id, cached_accounts)
                logger.info(f"Removed {removed_count} accounts from Redis for user {user_id}")
        
        # Remove transactions for these accounts from Redis
        cached_transactions = _get_from_redis('quiltt_transactions', user_id)
        if cached_transactions is None:
            cached_transactions = get_quiltt_transactions(user_id)
        if cached_transactions:
            if not isinstance(cached_transactions, list):
                cached_transactions = list(cached_transactions)
            cached_transactions = [t for t in cached_transactions if t.get('account_id') not in account_ids_set]
            _set_to_redis('quiltt_transactions', user_id, cached_transactions)
        
        # Also delete from MySQL directly (they may have been flushed already)
        try:
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor()
                placeholders = ', '.join(['%s'] * len(account_ids))
                
                # Delete transactions first
                cursor.execute(f"""
                    DELETE FROM quiltt_transactions 
                    WHERE user_id = %s AND account_id IN ({placeholders})
                """, [user_id] + list(account_ids))
                
                # Delete accounts
                cursor.execute(f"""
                    DELETE FROM quiltt_accounts 
                    WHERE user_id = %s AND account_id IN ({placeholders})
                """, [user_id] + list(account_ids))
                
                conn.commit()
                cursor.close()
                logger.info(f"Deleted accounts {account_ids} from MySQL for user {user_id}")
        except Exception as db_error:
            logger.error(f"Error deleting accounts from MySQL: {db_error}")
        
        return True
        
    except Exception as e:
        logger.error(f"Error deleting accounts by IDs: {e}", exc_info=True)
        return False


def delete_quiltt_transactions_for_account(account_id: str, user_id: Optional[int] = None) -> bool:
    """
    Delete all transactions for a specific account from Redis and MySQL.
    
    Args:
        account_id: The Quiltt account_id (string like 'acct_xxx')
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        True if successful, False otherwise
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return False
        user_id = current_user.id
    
    try:
        # Get current transactions from Redis or MySQL
        cached_data = _get_from_redis('quiltt_transactions', user_id)
        
        if cached_data is None:
            # Load from MySQL if not in Redis
            cached_data = get_quiltt_transactions(user_id)
            if cached_data is None:
                cached_data = []
        
        # Ensure cached_data is a list
        if not isinstance(cached_data, list):
            cached_data = list(cached_data) if cached_data else []
        
        # Filter out transactions for this account
        original_count = len(cached_data)
        cached_data = [txn for txn in cached_data if txn.get('account_id') != account_id]
        deleted_count = original_count - len(cached_data)
        
        if deleted_count > 0:
            logger.info(f"Deleted {deleted_count} transactions for account {account_id}")
            
            # Save filtered data back to Redis
            _set_to_redis('quiltt_transactions', user_id, cached_data)
            
            # Also delete from MySQL directly
            try:
                with get_db_pool().get_connection() as conn:
                    cursor = conn.cursor()
                    
                    # Delete from expense_entries first (if imported)
                    cursor.execute("""
                        DELETE ee FROM expense_entries ee
                        INNER JOIN quiltt_transactions qt ON ee.id = qt.imported_to_entry_id
                        WHERE qt.user_id = %s AND qt.account_id = %s
                    """, (user_id, account_id))
                    
                    # Delete from quiltt_transactions
                    cursor.execute("""
                        DELETE FROM quiltt_transactions 
                        WHERE user_id = %s AND account_id = %s
                    """, (user_id, account_id))
                    
                    conn.commit()
                    cursor.close()
                    
                    logger.info(f"Deleted transactions from MySQL for account {account_id}")
            except Exception as db_error:
                logger.error(f"Error deleting from MySQL: {db_error}")
                # Continue anyway - Redis update is primary
        
        return True
        
    except Exception as e:
        logger.error(f"Error deleting transactions for account: {e}", exc_info=True)
        return False


def get_uncategorized_category_id(user_id: int, entry_type: str, account_id: int = None) -> Optional[int]:
    """
    Get the "Uncategorized" category ID for a user.
    
    Args:
        user_id: User ID
        entry_type: One of 'income', 'expense', 'c_expense', 'c_payment'
        account_id: Required for c_expense (the credit account ID in Blankee)
        
    Returns:
        Category ID for "Uncategorized" or None if not found
    """
    try:
        if entry_type == 'income':
            # Get income categories from Redis
            redis_key = f"income_categories:v1:{user_id}"
            redis_client = _get_redis_client()
            cached = redis_client.get(redis_key) if redis_client else None
            
            if cached:
                categories = json.loads(cached)
            else:
                # Fallback to MySQL
                from db_connections import get_db_pool
                with get_db_pool().get_connection() as conn:
                    cursor = conn.cursor(pymysql.cursors.DictCursor)
                    cursor.execute("SELECT * FROM income_categories WHERE user_id = %s", (user_id,))
                    categories = cursor.fetchall()
                    cursor.close()
            
            # Find Uncategorized
            for cat in categories:
                if cat.get('name') == 'Uncategorized':
                    return cat.get('id')
            
            logger.warning(f"Uncategorized income category not found for user {user_id}")
            return None
            
        elif entry_type == 'expense':
            # Get expense categories from Redis
            redis_key = f"expense_categories:v1:{user_id}"
            redis_client = _get_redis_client()
            cached = redis_client.get(redis_key) if redis_client else None
            
            if cached:
                categories = json.loads(cached)
            else:
                # Fallback to MySQL
                from db_connections import get_db_pool
                with get_db_pool().get_connection() as conn:
                    cursor = conn.cursor(pymysql.cursors.DictCursor)
                    cursor.execute("SELECT * FROM expense_categories WHERE user_id = %s", (user_id,))
                    categories = cursor.fetchall()
                    cursor.close()
            
            # Find Uncategorized
            for cat in categories:
                if cat.get('name') == 'Uncategorized':
                    return cat.get('id')
            
            logger.warning(f"Uncategorized expense category not found for user {user_id}")
            return None
            
        elif entry_type == 'c_expense':
            if not account_id:
                logger.error("account_id is required for c_expense entry type")
                return None
            
            # Get c_expense_categories from Redis (keyed by user_id, filter by account_id)
            redis_key = f"c_expense_categories:v1:{user_id}"
            redis_client = _get_redis_client()
            cached = redis_client.get(redis_key) if redis_client else None
            
            if cached:
                categories = json.loads(cached)
            else:
                # Fallback to MySQL
                from db_connections import get_db_pool
                with get_db_pool().get_connection() as conn:
                    cursor = conn.cursor(pymysql.cursors.DictCursor)
                    cursor.execute("SELECT * FROM c_expense_categories WHERE account_id = %s", (account_id,))
                    categories = cursor.fetchall()
                    cursor.close()
            
            # Find Uncategorized for this specific account
            for cat in categories:
                if cat.get('name') == 'Uncategorized' and cat.get('account_id') == account_id:
                    return cat.get('id')
            
            logger.warning(f"Uncategorized c_expense category not found for account {account_id}")
            return None
            
        elif entry_type == 'c_payment':
            # c_payment_entries don't have categories - they are tied directly to credit accounts
            # Return the account_id itself as it's used in the c_payment_entries table
            if not account_id:
                logger.error("account_id is required for c_payment entry type")
                return None
            return account_id
            
        else:
            logger.error(f"Unknown entry type: {entry_type}")
            return None
            
    except Exception as e:
        logger.error(f"Error getting Uncategorized category: {e}", exc_info=True)
        return None


def get_blankee_credit_account_for_quiltt_account(user_id: int, quiltt_account_id: str) -> Optional[Dict]:
    """
    Find the Blankee credit account that corresponds to a Quiltt account.
    
    Strategy:
    1. First, check for direct quiltt_account_id match (most reliable)
    2. Fallback to mask matching (last 4 digits)
    
    Args:
        user_id: User ID
        quiltt_account_id: The Quiltt account_id (e.g., 'acct_xxx')
        
    Returns:
        Dict with credit account info or None if not found
    """
    try:
        # Get credit accounts from Redis or MySQL
        redis_key = f"credit_accounts:v1:{user_id}"
        redis_client = _get_redis_client()
        cached = redis_client.get(redis_key) if redis_client else None
        
        if cached:
            credit_accounts = json.loads(cached)
        else:
            # Fallback to MySQL
            from db_connections import get_db_pool
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute("SELECT * FROM credit_accounts WHERE user_id = %s", (user_id,))
                credit_accounts = cursor.fetchall()
                cursor.close()
        
        # Strategy 1: Direct quiltt_account_id match (most reliable)
        for ca in credit_accounts:
            if ca.get('quiltt_account_id') == quiltt_account_id:
                logger.info(f"Found credit account {ca.get('id')} via direct quiltt_account_id match")
                return ca
        
        # Strategy 2: Fallback to mask matching
        # First, get the Quiltt account to find its mask
        quiltt_accounts = get_quiltt_accounts(user_id)
        quiltt_account = None
        for qa in quiltt_accounts:
            if qa.get('account_id') == quiltt_account_id:
                quiltt_account = qa
                break
        
        if not quiltt_account:
            logger.warning(f"Quiltt account {quiltt_account_id} not found for user {user_id}")
            return None
        
        mask = quiltt_account.get('mask')
        if not mask:
            logger.warning(f"Quiltt account {quiltt_account_id} has no mask")
            return None
        
        # Find credit account with matching mask
        # Quiltt mask format may be "XXXX-XXXX-XXXX-7691" while Blankee stores just "7691"
        # Extract last 4 digits for comparison
        quiltt_last4 = mask[-4:] if mask and len(mask) >= 4 else mask
        
        for ca in credit_accounts:
            ca_mask = ca.get('mask')
            if not ca_mask:
                continue
            # Compare last 4 digits
            ca_last4 = ca_mask[-4:] if len(ca_mask) >= 4 else ca_mask
            if ca_last4 == quiltt_last4:
                logger.info(f"Found credit account {ca.get('id')} via mask match (last4: {ca_last4})")
                return ca
        
        logger.warning(f"No Blankee credit account found with mask ending in {quiltt_last4} for user {user_id}")
        return None
        
    except Exception as e:
        logger.error(f"Error finding Blankee credit account: {e}", exc_info=True)
        return None


# ============================================================================
# QUILTT ENTRY LOCKING HELPERS
# ============================================================================

def get_user_quiltt_account_flags(user_id: int) -> Dict[str, Any]:
    """
    Determine what Quiltt account types a user has connected.
    Used for entry locking - entries linked to bank accounts cannot be edited.
    
    Args:
        user_id: User ID
        
    Returns:
        Dict with:
        - has_checking: bool - True if user has a checking account connected
        - has_savings: bool - True if user has a savings account connected  
        - quiltt_credit_ids: List[int] - Blankee credit_accounts.id where is_quiltt=1
        - savings_income_category_id: int|None - ID of "Savings" income category
        - savings_expense_category_id: int|None - ID of "Savings" expense category
    """
    result = {
        'has_checking': False,
        'has_savings': False,
        'quiltt_credit_ids': [],
        'savings_income_category_id': None,
        'savings_expense_category_id': None
    }
    
    try:
        # Get all quiltt accounts for user
        quiltt_accounts = get_quiltt_accounts(user_id)
        
        for account in quiltt_accounts:
            account_type = account.get('account_type', '').upper()
            account_name = account.get('account_name', '').lower()
            
            if account_type == 'DEPOSITORY':
                if 'checking' in account_name:
                    result['has_checking'] = True
                if 'savings' in account_name:
                    result['has_savings'] = True
            # CREDIT accounts are handled separately via credit_accounts table
        
        # Get Quiltt-linked credit accounts from credit_accounts table
        result['quiltt_credit_ids'] = get_quiltt_credit_account_ids(user_id)
        
        # Get Savings category IDs
        savings_ids = get_savings_category_ids(user_id)
        result['savings_income_category_id'] = savings_ids.get('income_savings_id')
        result['savings_expense_category_id'] = savings_ids.get('expense_savings_id')
        
        logger.info(f"Quiltt account flags for user {user_id}: {result}")
        return result
        
    except Exception as e:
        logger.error(f"Error getting Quiltt account flags for user {user_id}: {e}", exc_info=True)
        return result


def get_quiltt_credit_account_ids(user_id: int) -> List[int]:
    """
    Get list of Blankee credit_accounts.id where is_quiltt=1.
    These credit accounts are linked to bank and their entries should be locked.
    
    Args:
        user_id: User ID
        
    Returns:
        List of credit_accounts.id that are Quiltt-linked
    """
    try:
        redis_client = _get_redis_client()
        redis_key = f"credit_accounts:v1:{user_id}"
        cached = redis_client.get(redis_key) if redis_client else None
        
        if cached:
            credit_accounts = json.loads(cached)
        else:
            # Fallback to MySQL
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute("SELECT id, is_quiltt FROM credit_accounts WHERE user_id = %s", (user_id,))
                credit_accounts = cursor.fetchall()
                cursor.close()
        
        # Return IDs where is_quiltt = 1
        quiltt_ids = []
        for ca in credit_accounts:
            if ca.get('is_quiltt') == 1 or ca.get('is_quiltt') == True:
                quiltt_ids.append(ca.get('id'))
        
        return quiltt_ids
        
    except Exception as e:
        logger.error(f"Error getting Quiltt credit account IDs for user {user_id}: {e}", exc_info=True)
        return []


def get_savings_category_ids(user_id: int) -> Dict[str, Optional[int]]:
    """
    Get the category IDs for "Savings" in income_categories and expense_categories.
    Used for locking only the Savings category when user has Quiltt savings account.
    
    Args:
        user_id: User ID
        
    Returns:
        Dict with income_savings_id and expense_savings_id (or None if not found)
    """
    result = {
        'income_savings_id': None,
        'expense_savings_id': None
    }
    
    try:
        redis_client = _get_redis_client()
        
        # Check income_categories
        income_key = f"income_categories:v1:{user_id}"
        income_cached = redis_client.get(income_key) if redis_client else None
        
        if income_cached:
            income_categories = json.loads(income_cached)
            for cat in income_categories:
                if cat.get('name', '').lower() == 'savings':
                    result['income_savings_id'] = cat.get('id')
                    break
        else:
            # Fallback to MySQL for income
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute(
                    "SELECT id FROM income_categories WHERE user_id = %s AND LOWER(name) = 'savings' LIMIT 1",
                    (user_id,)
                )
                row = cursor.fetchone()
                if row:
                    result['income_savings_id'] = row['id']
                cursor.close()
        
        # Check expense_categories
        expense_key = f"expense_categories:v1:{user_id}"
        expense_cached = redis_client.get(expense_key) if redis_client else None
        
        if expense_cached:
            expense_categories = json.loads(expense_cached)
            for cat in expense_categories:
                if cat.get('name', '').lower() == 'savings':
                    result['expense_savings_id'] = cat.get('id')
                    break
        else:
            # Fallback to MySQL for expense
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute(
                    "SELECT id FROM expense_categories WHERE user_id = %s AND LOWER(name) = 'savings' LIMIT 1",
                    (user_id,)
                )
                row = cursor.fetchone()
                if row:
                    result['expense_savings_id'] = row['id']
                cursor.close()
        
        return result
        
    except Exception as e:
        logger.error(f"Error getting Savings category IDs for user {user_id}: {e}", exc_info=True)
        return result
