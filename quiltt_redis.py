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
                            LOWER(account_name) LIKE '%checking%' 
                            OR LOWER(account_name) LIKE '%savings%'
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
                            LOWER(account_name) LIKE '%checking%' 
                            OR LOWER(account_name) LIKE '%savings%'
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
        # Get current data from Redis or MySQL
        cached_data = _get_from_redis('quiltt_accounts', user_id)
        
        if cached_data is None:
            # Load from MySQL if not in Redis
            cached_data = get_quiltt_accounts(user_id)
            if cached_data is None:
                cached_data = []
        
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
                    # Don't overwrite user settings or liability data with None
                    if value is None and key in ('is_active', 'sync_transactions', *liability_fields):
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
        # Update in Redis only
        cached_data = _get_from_redis('quiltt_accounts', user_id)
        
        if cached_data is None:
            # Load from MySQL if not in Redis
            cached_data = get_quiltt_accounts(user_id)
            if cached_data is None:
                return False
        
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
            # Update in Redis only
            cached_data = _get_from_redis('quiltt_accounts', user_id)
            
            if cached_data is None:
                # Load from MySQL if not in Redis
                cached_data = get_quiltt_accounts(user_id)
                if cached_data is None:
                    return False
            
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
                logger.warning(f"Account {account_id} not found in cached data for user {user_id}")
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
            cached_accounts = _get_from_redis('quiltt_accounts', user_id)
            if cached_accounts is None:
                cached_accounts = get_quiltt_accounts(user_id)
            
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


def get_user_id_by_profile_id(profile_id: str) -> Optional[int]:
    """
    Look up user_id from a Quiltt profile_id.
    Checks MySQL since we need to find the user from an incoming webhook.
    
    Args:
        profile_id: Quiltt profile ID string
        
    Returns:
        user_id or None if not found
    """
    try:
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT user_id FROM quiltt_profiles WHERE profile_id = %s",
                (profile_id,)
            )
            result = cursor.fetchone()
            return result['user_id'] if result else None
    except Exception as e:
        logger.error(f"Error looking up user_id for profile {profile_id}: {e}")
        return None


def get_quiltt_webhook_events(user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Get Quiltt webhook events from Redis or MySQL.
    
    Args:
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        List of webhook event dicts
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return []
        user_id = current_user.id
    
    # Try Redis first
    if is_user_hydrated(user_id):
        cached_data = _get_from_redis('quiltt_webhook_events', user_id)
        if cached_data is not None:
            return cached_data
    
    # Fallback to MySQL - get events for this user's profile
    try:
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            cursor.execute("""
                SELECT qwe.* FROM quiltt_webhook_events qwe
                INNER JOIN quiltt_profiles qp ON qwe.profile_id = qp.profile_id
                WHERE qp.user_id = %s
                ORDER BY qwe.created_at DESC
            """, (user_id,))
            return cursor.fetchall() or []
    except Exception as e:
        logger.error(f"Error getting Quiltt webhook events from MySQL: {e}")
        return []


def upsert_quiltt_webhook_event(event_data: Dict[str, Any], user_id: int) -> bool:
    """
    Insert or update a Quiltt webhook event (Redis-first).
    
    Args:
        event_data: Dict with event fields (event_id, event_type, profile_id, connection_id, payload)
        user_id: User ID to store under
        
    Returns:
        True if successful
    """
    try:
        # Get existing events from Redis or MySQL
        cached_data = _get_from_redis('quiltt_webhook_events', user_id)
        
        if cached_data is None:
            # Not in Redis - load from MySQL
            cached_data = get_quiltt_webhook_events(user_id)
        
        if not isinstance(cached_data, list):
            cached_data = []
        
        event_id = event_data.get('event_id')
        
        # Check if event already exists
        existing_idx = None
        for i, evt in enumerate(cached_data):
            if evt.get('event_id') == event_id:
                existing_idx = i
                break
        
        if existing_idx is not None:
            # Update existing
            cached_data[existing_idx].update(event_data)
        else:
            # Add new - generate temp ID
            next_id = max((e.get('id', 0) for e in cached_data), default=0) + 1
            if next_id < 1000000000:
                next_id = 1000000000 + len(cached_data)
            event_data['id'] = next_id
            event_data['created_at'] = datetime.now().isoformat()
            cached_data.append(event_data)
        
        # Save to Redis
        return _set_to_redis('quiltt_webhook_events', user_id, cached_data)
        
    except Exception as e:
        logger.error(f"Error upserting Quiltt webhook event: {e}", exc_info=True)
        return False


def delete_quiltt_webhook_events_for_connection(connection_id: str, user_id: int, is_last_connection: bool = False) -> bool:
    """
    Delete all webhook events for a specific connection (Redis-first).
    If is_last_connection is True, also deletes events with NULL connection_id.
    
    Args:
        connection_id: Quiltt connection ID
        user_id: User ID
        is_last_connection: If True, also delete events with NULL connection_id
        
    Returns:
        True if successful
    """
    try:
        cached_data = _get_from_redis('quiltt_webhook_events', user_id)
        
        if cached_data is None:
            cached_data = get_quiltt_webhook_events(user_id)
        
        if not isinstance(cached_data, list):
            cached_data = []
        
        # Get user's profile_id for NULL event deletion
        profile_id = None
        if is_last_connection:
            profile = get_quiltt_profile(user_id)
            if profile:
                profile_id = profile.get('profile_id')
        
        # Filter out events for this connection
        original_count = len(cached_data)
        
        if is_last_connection:
            # Remove events for this connection AND events with NULL connection_id
            cached_data = [evt for evt in cached_data 
                          if evt.get('connection_id') != connection_id 
                          and evt.get('connection_id') is not None]
        else:
            # Only remove events for this specific connection
            cached_data = [evt for evt in cached_data if evt.get('connection_id') != connection_id]
        
        deleted_count = original_count - len(cached_data)
        
        if deleted_count > 0:
            logger.info(f"Deleted {deleted_count} webhook events for connection {connection_id} (is_last={is_last_connection})")
        
        # Always save to Redis (even if empty) and mark pending deletes for MySQL
        _set_to_redis('quiltt_webhook_events', user_id, cached_data)
        
        # Always add to pending deletes for MySQL cleanup
        redis_client = _get_redis_client()
        if redis_client:
            pending_key = f"pending_webhook_deletes:{user_id}"
            redis_client.sadd(pending_key, connection_id)
            if is_last_connection and profile_id:
                # Store profile_id so we can delete NULL events even after profile is deleted
                redis_client.sadd(pending_key, f'__NULL__:{profile_id}')
            redis_client.expire(pending_key, INACTIVITY_TIMEOUT + 60)
            
            # Mark table as dirty so flush worker will process the pending delete
            dirty_key = f"dirty_tables:{user_id}"
            redis_client.sadd(dirty_key, 'quiltt_webhook_events')
            redis_client.expire(dirty_key, INACTIVITY_TIMEOUT + 60)
        
        return True
        
    except Exception as e:
        logger.error(f"Error deleting webhook events for connection: {e}", exc_info=True)
        return False

