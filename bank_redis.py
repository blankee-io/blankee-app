"""
Redis-First Operations for Bank-Link Data

This module provides Redis-first CRUD operations for linked bank data (connections, accounts, transactions).
Data is written to Redis immediately and flushed to MySQL periodically by the Redis manager.
"""

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
    INACTIVITY_TIMEOUT,
    get_table_cache,
    set_table_cache
)
from db_connections import get_db_pool
from log_config import get_logger, log_info, log_error, log_warning, log_exception

logger = get_logger(__name__)


def _get_redis_client():
    """Get Redis client - imported inside function to avoid None at module load time"""
    from redis_manager import _redis_client
    return _redis_client


def _get_from_redis(table: str, user_id: int) -> Optional[List[Dict[str, Any]]]:
    """Get linked bank data from Redis"""
    return get_table_cache(table, user_id)


def _get_all_linked_accounts_raw(user_id: int) -> List[Dict[str, Any]]:
    """
    Get ALL bank accounts for a user, bypassing hydration check and is_active filter.
    
    Used internally by upsert/update/delete operations that need the complete account list.
    Unlike get_linked_accounts(), this:
    1. Reads directly from the Redis key (no hydration check)
    2. Falls back to MySQL WITHOUT is_active=1 filter
    """
    redis_client = _get_redis_client()
    
    # Try Redis key directly (bypasses hydration check)
    if redis_client:
        redis_key = _get_redis_key('linked_accounts', user_id)
        cached = redis_client.get(redis_key)
        if cached:
            return json.loads(cached)
    
    # Fall back to MySQL - get ALL accounts without is_active filter
    try:
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT * FROM linked_accounts WHERE user_id = %s", (user_id,))
            result = list(cursor.fetchall())
            log_info(logger, 'BANK', f"_get_all_linked_accounts_raw: loaded {len(result)} accounts from MySQL for user {user_id}")
            return result
    except Exception as e:
        log_error(logger, 'BANK', f"Error getting all bank accounts from MySQL: {e}")
        return []


def _set_to_redis(table: str, user_id: int, data: List[Dict[str, Any]]) -> bool:
    """Set linked bank data to Redis and mark as dirty (Redis-first write)"""
    log_info(logger, 'BANK', f"Setting {table} for user {user_id} with {len(data)} records (dirty)")
    return set_table_cache(table, user_id, data, mark_dirty=True)


def _set_to_redis_no_dirty(table: str, user_id: int, data: List[Dict[str, Any]]) -> bool:
    """Set linked bank data to Redis WITHOUT marking as dirty (for deletion operations)"""
    log_info(logger, 'BANK', f"Setting {table} for user {user_id} with {len(data)} records (no dirty flag)")
    return set_table_cache(table, user_id, data, mark_dirty=False)


def get_provider_profile(user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    Get user's provider profile from Redis or MySQL.
    
    Returns:
        Profile dict or None
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return None
        user_id = current_user.id
    
    # Try Redis first
    if is_user_hydrated(user_id):
        cached_data = _get_from_redis('linked_provider_profiles', user_id)
        if cached_data and len(cached_data) > 0:
            return cached_data[0]
    
    # Fallback to MySQL
    try:
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT * FROM linked_provider_profiles WHERE user_id = %s",
                (user_id,)
            )
            return cursor.fetchone()
    except Exception as e:
        log_error(logger, 'BANK', f"Error getting provider profile from MySQL: {e}")
        return None


def update_provider_profile(profile_data: Dict[str, Any], user_id: Optional[int] = None) -> bool:
    """
    Update or create provider profile (Redis-only, MySQL flush happens periodically).
    
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
        cached_data = _get_from_redis('linked_provider_profiles', user_id)
        log_info(logger, 'BANK', f"Got cached_data from Redis for user {user_id}: {cached_data}")
        
        if cached_data is None:
            # Not in Redis yet - load from MySQL if exists
            try:
                with get_db_pool().get_cursor(dictionary=True) as cursor:
                    cursor.execute(
                        "SELECT * FROM linked_provider_profiles WHERE user_id = %s",
                        (user_id,)
                    )
                    existing = cursor.fetchone()
                    if existing:
                        cached_data = [existing]
                        log_info(logger, 'BANK', f"Loaded existing profile from MySQL for user {user_id}")
                    else:
                        cached_data = []
                        log_info(logger, 'BANK', f"No existing profile in MySQL for user {user_id}")
            except Exception as e:
                log_error(logger, 'BANK', f"Error loading profile from MySQL: {e}")
                cached_data = []
        
        if len(cached_data) > 0:
            # Update existing entry
            cached_data[0].update(profile_data)
            cached_data[0]['user_id'] = user_id
            log_info(logger, 'BANK', f"Updated existing profile for user {user_id}")
        else:
            # Add new entry
            cached_data.append({'user_id': user_id, **profile_data})
            log_info(logger, 'BANK', f"Created new profile for user {user_id}")
        
        log_info(logger, 'BANK', f"About to save to Redis: {cached_data}")
        # Save to Redis and mark as dirty
        result = _set_to_redis('linked_provider_profiles', user_id, cached_data)
        log_info(logger, 'BANK', f"Redis save result: {result}")
        
        return True
        
    except Exception as e:
        log_exception(logger, 'BANK', f"Error updating provider profile: {e}")
        return False


def get_linked_connections(user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Get user's linked connections from Redis or MySQL.
    
    Returns:
        List of connection dicts
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return []
        user_id = current_user.id
    
    # Try Redis first
    if is_user_hydrated(user_id):
        cached_data = _get_from_redis('linked_connections', user_id)
        if cached_data is not None:
            return cached_data
    
    # Fallback to MySQL
    try:
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT * FROM linked_connections WHERE user_id = %s ORDER BY last_synced_at DESC",
                (user_id,)
            )
            return cursor.fetchall()
    except Exception as e:
        log_error(logger, 'BANK', f"Error getting linked connections from MySQL: {e}")
        return []


def upsert_linked_connection(connection_data: Dict[str, Any], user_id: Optional[int] = None) -> Optional[int]:
    """
    Insert or update a linked connection (Redis-only, MySQL flush happens periodically).
    
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
        log_error(logger, 'BANK', "connection_id is required")
        return None
    
    try:
        # Get current data from Redis or MySQL
        cached_data = _get_from_redis('linked_connections', user_id)
        
        if cached_data is None:
            # Load from MySQL if not in Redis
            cached_data = get_linked_connections(user_id)
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
        _set_to_redis('linked_connections', user_id, cached_data)
        
        return db_id
        
    except Exception as e:
        log_exception(logger, 'BANK', f"Error upserting linked connection: {e}")
        return None


def get_linked_accounts(user_id: Optional[int] = None, connection_db_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Get user's linked accounts from Redis or MySQL.
    
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
        cached_data = _get_from_redis('linked_accounts', user_id)
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
                    SELECT * FROM linked_accounts 
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
                    SELECT * FROM linked_accounts 
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
        log_error(logger, 'BANK', f"Error getting linked accounts from MySQL: {e}")
        return []


def upsert_linked_account(account_data: Dict[str, Any], user_id: Optional[int] = None) -> Optional[int]:
    """
    Insert or update a linked account (Redis-only, MySQL flush happens periodically).
    
    Args:
        account_data: Dict with account fields (account_id, connection_id, account_name, etc.)
                      Note: connection_id should be the MySQL ID from linked_connections.id
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        Account database ID or None
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return None
        user_id = current_user.id
    
    account_id = account_data.get('account_id')
    mysql_connection_id = account_data.get('connection_id')  # MySQL ID from linked_connections
    if not account_id:
        log_error(logger, 'BANK', "account_id is required")
        return None
    
    if not mysql_connection_id:
        log_error(logger, 'BANK', "connection_id is required")
        return None
    
    try:
        # Get ALL bank accounts (bypasses hydration check and is_active filter)
        # This is critical: update-account may have written is_active=1 to Redis,
        # but if user isn't hydrated, _get_from_redis would return None and
        # get_linked_accounts filters by is_active=1 in MySQL (stale data).
        cached_data = _get_all_linked_accounts_raw(user_id)
        
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
        _set_to_redis('linked_accounts', user_id, cached_data)
        
        return db_id
        
    except Exception as e:
        log_exception(logger, 'BANK', f"Error upserting linked account: {e}")
        return None


def update_linked_account_field(account_id: str, field: str, value: Any, user_id: Optional[int] = None) -> bool:
    """
    Update a single field in a linked account (Redis-only, MySQL flush happens periodically).
    
    Args:
        account_id: linked account ID
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
        # Get ALL bank accounts (bypasses hydration check and is_active filter)
        cached_data = _get_all_linked_accounts_raw(user_id)
        
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
            log_warning(logger, 'BANK', f"Account {account_id} not found in cached data for user {user_id}")
            return False
        
        # Save to Redis and mark as dirty
        _set_to_redis('linked_accounts', user_id, cached_data)
        
        log_info(logger, 'BANK', f"Successfully updated {field}={value} for account {account_id}")
        return True
        
    except Exception as e:
        log_exception(logger, 'BANK', f"Error updating linked account field: {e}")
        return False


def update_linked_account_fields(account_id: str, fields: dict, user_id: Optional[int] = None) -> bool:
    """
    Update multiple fields in a linked account atomically (Redis-only, MySQL flush happens periodically).
    This prevents race conditions when multiple fields need to be updated together.
    
    Args:
        account_id: linked account ID
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
        lock_key = f"lock:linked_accounts:{user_id}"
        
        # Try to acquire lock for up to 5 seconds
        lock_acquired = False
        for attempt in range(50):  # 50 attempts x 100ms = 5 seconds max
            if redis_client.set(lock_key, '1', nx=True, ex=10):  # Lock expires in 10 seconds
                lock_acquired = True
                break
            time.sleep(0.1)  # Wait 100ms between attempts
        
        if not lock_acquired:
            log_error(logger, 'BANK', f"Failed to acquire lock for user {user_id} accounts")
            return False
        
        try:
            # Get ALL bank accounts (bypasses hydration check and is_active filter)
            cached_data = _get_all_linked_accounts_raw(user_id)
            
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
                log_warning(logger, 'BANK', f"Account {account_id} not found in cached data for user {user_id} ({len(cached_data)} accounts checked)")
                return False
            
            # Save to Redis and mark as dirty
            _set_to_redis('linked_accounts', user_id, cached_data)
            
            log_info(logger, 'BANK', f"Successfully updated {len(fields)} fields for account {account_id}: {fields}")
            return True
            
        finally:
            # Always release the lock
            redis_client.delete(lock_key)
        
    except Exception as e:
        log_exception(logger, 'BANK', f"Error updating linked account fields: {e}")
        return False


def delete_linked_connection(connection_id: str, user_id: Optional[int] = None) -> bool:
    """
    Delete a linked connection and all its accounts (Redis-first, MySQL flush happens periodically).
    
    Args:
        connection_id: linked connection ID
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        True if successful
    """
    log_info(logger, 'DELETE_CONN', f"delete_linked_connection called for connection_id={connection_id}, user_id={user_id}")
    
    if user_id is None:
        if not current_user.is_authenticated:
            log_info(logger, 'DELETE_CONN', "user_id is None and no authenticated user")
            return False
        user_id = current_user.id
    
    log_info(logger, 'DELETE_CONN', f"Processing delete for user {user_id}")
    
    try:
        redis_client = _get_redis_client()
        log_info(logger, 'DELETE_CONN', f"Got redis_client: {redis_client is not None}")
        if not redis_client:
            log_info(logger, 'DELETE_CONN', "Redis client not available for delete, falling back to direct MySQL")
            # Fallback to direct MySQL deletion
            from db_connections import get_db_pool
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor()
                # Delete accounts first (foreign key constraint)
                cursor.execute("""
                    DELETE FROM linked_accounts 
                    WHERE user_id = %s AND connection_id IN (
                        SELECT id FROM linked_connections WHERE connection_id = %s
                    )
                """, (user_id, connection_id))
                # Delete connection
                cursor.execute("""
                    DELETE FROM linked_connections 
                    WHERE user_id = %s AND connection_id = %s
                """, (user_id, connection_id))
                conn.commit()
                cursor.close()
            return True
        
        # Get connection's db_id before deleting
        log_info(logger, 'DELETE_CONN', f"Getting connections for user {user_id}")
        connections = get_linked_connections(user_id)
        log_info(logger, 'DELETE_CONN', f"Got {len(connections)} connections")
        conn_db_id = None
        for conn in connections:
            if conn.get('connection_id') == connection_id:
                conn_db_id = conn.get('id')
                break
        
        log_info(logger, 'DELETE_CONN', f"Found conn_db_id={conn_db_id} for connection_id={connection_id}")
        
        # Remove connection from Redis (without marking dirty - deletion is handled separately)
        cached_connections = _get_from_redis('linked_connections', user_id)
        log_info(logger, 'DELETE_CONN', f"Got {len(cached_connections) if cached_connections else 0} cached connections from Redis")
        if cached_connections is None:
            cached_connections = connections
        
        before_count = len(cached_connections)
        cached_connections = [c for c in cached_connections if c.get('connection_id') != connection_id]
        after_count = len(cached_connections)
        log_info(logger, 'DELETE_CONN', f"Filtered connections: {before_count} -> {after_count}")
        
        log_info(logger, 'DELETE_CONN', f"About to call _set_to_redis_no_dirty for linked_connections")
        result = _set_to_redis_no_dirty('linked_connections', user_id, cached_connections)
        log_info(logger, 'DELETE_CONN', f"_set_to_redis_no_dirty returned: {result}")
        
        log_info(logger, 'DELETE_CONN', f"After deletion, user {user_id} has {len(cached_connections)} connection(s) remaining")
        
        # Get account_ids to delete their transactions
        account_ids_to_delete = []
        if conn_db_id:
            # Use _get_all_linked_accounts_raw to bypass hydration check
            cached_accounts = _get_all_linked_accounts_raw(user_id)
            
            if cached_accounts:
                # Collect account_ids before removing accounts
                account_ids_to_delete = [
                    a.get('account_id') for a in cached_accounts 
                    if a.get('connection_id') == conn_db_id and a.get('account_id')
                ]
                # Remove accounts (without marking dirty - deletion is handled separately)
                cached_accounts = [a for a in cached_accounts if a.get('connection_id') != conn_db_id]
                _set_to_redis_no_dirty('linked_accounts', user_id, cached_accounts)
        
        # Remove associated transactions from Redis (cascade delete)
        if account_ids_to_delete:
            cached_transactions = _get_from_redis('linked_transactions', user_id)
            if cached_transactions is None:
                cached_transactions = get_linked_transactions(user_id)
            
            if cached_transactions:
                # Filter out transactions for deleted accounts
                cached_transactions = [
                    t for t in cached_transactions 
                    if t.get('account_id') not in account_ids_to_delete
                ]
                _set_to_redis_no_dirty('linked_transactions', user_id, cached_transactions)
        
        # Mark tables as dirty for deletion flush
        dirty_key = f"dirty_tables:{user_id}"
        redis_client.sadd(dirty_key, 'linked_connections_deleted')
        redis_client.sadd(dirty_key, 'linked_accounts_deleted')
        redis_client.sadd(dirty_key, 'linked_transactions_deleted')
        
        # Store the connection_id to delete
        delete_key = f"linked_connections_to_delete:{user_id}"
        redis_client.sadd(delete_key, connection_id)
        redis_client.expire(delete_key, 300)  # Expire in 5 minutes
        
        log_info(logger, 'BANK', f"Marked linked connection {connection_id} for deletion (user {user_id})")
        
        # Check if this was the last connection - if so, delete the provider profile
        log_info(logger, 'BANK', f"Checking if last connection: len(cached_connections) = {len(cached_connections)}")
        if len(cached_connections) == 0:
            log_info(logger, 'BANK', f"Last connection deleted for user {user_id}, deleting provider profile")
            try:
                from db_connections import get_db_pool
                from providers import get_bank_provider
                import pymysql
                
                # Get provider_ref from database
                with get_db_pool().get_connection() as conn:
                    cursor = conn.cursor(pymysql.cursors.DictCursor)
                    cursor.execute("SELECT provider_ref FROM linked_provider_profiles WHERE user_id = %s", (user_id,))
                    profile = cursor.fetchone()
                    cursor.close()
                    
                if profile and profile.get('provider_ref'):
                    success = get_bank_provider().delete_user(user_id)
                    if success:
                        log_info(logger, 'BANK', f"Successfully deleted provider profile {profile['provider_ref']} for user {user_id}")
                        
                        # Delete linked_provider_profiles record from database
                        with get_db_pool().get_connection() as conn:
                            cursor = conn.cursor()
                            cursor.execute("DELETE FROM linked_provider_profiles WHERE user_id = %s", (user_id,))
                            conn.commit()
                            cursor.close()
                        
                        # Delete linked_provider_profiles from Redis cache
                        profiles_key = f"linked_provider_profiles:v1:{user_id}"
                        redis_client.delete(profiles_key)
                        
                        log_info(logger, 'BANK', f"Deleted linked_provider_profiles record and Redis cache for user {user_id}")
                    else:
                        log_warning(logger, 'BANK', f"Failed to delete provider profile {profile['provider_ref']} for user {user_id}")
                else:
                    log_warning(logger, 'BANK', f"No provider profile found in database for user {user_id}")
            except Exception as e:
                log_exception(logger, 'BANK', f"Error deleting provider profile: {e}")
        else:
            log_info(logger, 'BANK', f"User {user_id} still has {len(cached_connections)} connection(s), not deleting profile")
        
        return True
        
    except Exception as e:
        log_exception(logger, 'BANK', f"Error deleting linked connection: {e}")
        return False


def get_linked_transactions(user_id: Optional[int] = None, account_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Get linked transactions from Redis (or MySQL if not cached).
    
    Args:
        user_id: User ID (defaults to current_user.id)
        account_id: Optional linked account_id to filter by
        
    Returns:
        List of transaction dicts
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return []
        user_id = current_user.id
    
    try:
        # Try Redis first
        cached_data = _get_from_redis('linked_transactions', user_id)
        
        if cached_data is None:
            # Fallback to MySQL
            from db_connections import get_db_pool
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute("""
                    SELECT * FROM linked_transactions 
                    WHERE user_id = %s
                    ORDER BY date DESC, created_at DESC
                """, (user_id,))
                cached_data = cursor.fetchall()
                cursor.close()
            
            # Cache in Redis
            if cached_data:
                _set_to_redis('linked_transactions', user_id, list(cached_data))
        
        # Filter by account_id if provided
        if account_id and cached_data:
            cached_data = [t for t in cached_data if t.get('account_id') == account_id]
        
        return cached_data or []
        
    except Exception as e:
        log_exception(logger, 'BANK', f"Error getting linked transactions: {e}")
        return []


def upsert_linked_transaction(transaction_data: Dict[str, Any], user_id: Optional[int] = None) -> Optional[int]:
    """
    Insert or update a linked transaction in Redis (MySQL flush happens periodically).
    
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
        log_error(logger, 'BANK', "transaction_id is required")
        return None
    
    try:
        # Get current data from Redis or MySQL
        cached_data = _get_from_redis('linked_transactions', user_id)
        
        if cached_data is None:
            # Load from MySQL if not in Redis
            cached_data = get_linked_transactions(user_id)
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
                log_info(logger, 'BANK', f"BEFORE update: {transaction_id} has enriched_at={cached_data[i].get('enriched_at')}")
                log_info(logger, 'BANK', f"NEW DATA: enriched_at={transaction_data.get('enriched_at')}")
                cached_data[i].update(transaction_data)
                db_id = cached_data[i].get('id')
                found = True
                log_info(logger, 'BANK', f"AFTER update: {transaction_id} has enriched_at={cached_data[i].get('enriched_at')}")
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
        _set_to_redis('linked_transactions', user_id, cached_data)
        
        return db_id
        
    except Exception as e:
        log_exception(logger, 'BANK', f"Error upserting linked transaction: {e}")
        return None


def update_transaction_recurrence(recurrence_map: dict, user_id: int) -> int:
    """
    Batch-update recurrence fields on linked_transactions in Redis.
    
    Args:
        recurrence_map: Dict mapping transaction_id → recurrence data dict
                        (from bank_sync.build_recurrence_map)
        user_id: User ID
        
    Returns:
        Number of transactions updated
    """
    if not recurrence_map:
        return 0
    
    try:
        cached_data = _get_from_redis('linked_transactions', user_id)
        
        if cached_data is None:
            cached_data = get_linked_transactions(user_id)
            if cached_data is None:
                return 0
        
        if not isinstance(cached_data, list):
            cached_data = list(cached_data) if cached_data else []
        
        updated = 0
        recurring_txn_ids = set(recurrence_map.keys())
        
        for txn in cached_data:
            txn_id = txn.get('transaction_id')
            if txn_id in recurring_txn_ids:
                txn.update(recurrence_map[txn_id])
                updated += 1
            elif txn.get('enrichment_recurrence') != 'recurring':
                # Mark non-recurring transactions (only if not already set to recurring
                # by a previous call — avoids overwriting if groups API is stale)
                txn['enrichment_recurrence'] = 'one off'
        
        if updated > 0:
            _set_to_redis('linked_transactions', user_id, cached_data)
            log_info(logger, 'BANK', f"Updated recurrence for {updated} transactions for user {user_id}")
        
        return updated
        
    except Exception as e:
        log_exception(logger, 'BANK', f"Error updating transaction recurrence for user {user_id}: {e}")
        return 0


def delete_linked_accounts_by_ids(account_ids: List[str], user_id: Optional[int] = None) -> bool:
    """
    Delete specific linked accounts by their account_id strings from Redis and MySQL.
    Used when canceling account selection for an existing connection - only removes
    the newly-added accounts, not the entire connection.
    
    Args:
        account_ids: List of linked account_id strings to delete
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
        cached_accounts = _get_all_linked_accounts_raw(user_id)
        if cached_accounts:
            if not isinstance(cached_accounts, list):
                cached_accounts = list(cached_accounts)
            
            original_count = len(cached_accounts)
            cached_accounts = [a for a in cached_accounts if a.get('account_id') not in account_ids_set]
            removed_count = original_count - len(cached_accounts)
            
            if removed_count > 0:
                _set_to_redis('linked_accounts', user_id, cached_accounts)
                log_info(logger, 'BANK', f"Removed {removed_count} accounts from Redis for user {user_id}")
        
        # Remove transactions for these accounts from Redis
        cached_transactions = _get_from_redis('linked_transactions', user_id)
        if cached_transactions is None:
            cached_transactions = get_linked_transactions(user_id)
        if cached_transactions:
            if not isinstance(cached_transactions, list):
                cached_transactions = list(cached_transactions)
            cached_transactions = [t for t in cached_transactions if t.get('account_id') not in account_ids_set]
            _set_to_redis('linked_transactions', user_id, cached_transactions)
        
        # Also delete from MySQL directly (they may have been flushed already)
        try:
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor()
                placeholders = ', '.join(['%s'] * len(account_ids))
                
                # Delete transactions first
                cursor.execute(f"""
                    DELETE FROM linked_transactions 
                    WHERE user_id = %s AND account_id IN ({placeholders})
                """, [user_id] + list(account_ids))
                
                # Delete accounts
                cursor.execute(f"""
                    DELETE FROM linked_accounts 
                    WHERE user_id = %s AND account_id IN ({placeholders})
                """, [user_id] + list(account_ids))
                
                conn.commit()
                cursor.close()
                log_info(logger, 'BANK', f"Deleted accounts {account_ids} from MySQL for user {user_id}")
        except Exception as db_error:
            log_error(logger, 'BANK', f"Error deleting accounts from MySQL: {db_error}")
        
        return True
        
    except Exception as e:
        log_exception(logger, 'BANK', f"Error deleting accounts by IDs: {e}")
        return False


def delete_linked_transactions_for_account(account_id: str, user_id: Optional[int] = None) -> bool:
    """
    Delete all transactions for a specific account from Redis and MySQL.
    
    Args:
        account_id: The linked account_id (string like 'acct_xxx')
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
        cached_data = _get_from_redis('linked_transactions', user_id)
        
        if cached_data is None:
            # Load from MySQL if not in Redis
            cached_data = get_linked_transactions(user_id)
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
            log_info(logger, 'BANK', f"Deleted {deleted_count} transactions for account {account_id}")
            
            # Save filtered data back to Redis
            _set_to_redis('linked_transactions', user_id, cached_data)
            
            # Also delete from MySQL directly
            try:
                with get_db_pool().get_connection() as conn:
                    cursor = conn.cursor()
                    
                    # Delete from expense_entries first (if imported)
                    cursor.execute("""
                        DELETE ee FROM expense_entries ee
                        INNER JOIN linked_transactions qt ON ee.id = qt.imported_to_entry_id
                        WHERE qt.user_id = %s AND qt.account_id = %s
                    """, (user_id, account_id))
                    
                    # Delete from linked_transactions
                    cursor.execute("""
                        DELETE FROM linked_transactions 
                        WHERE user_id = %s AND account_id = %s
                    """, (user_id, account_id))
                    
                    conn.commit()
                    cursor.close()
                    
                    log_info(logger, 'BANK', f"Deleted transactions from MySQL for account {account_id}")
            except Exception as db_error:
                log_error(logger, 'BANK', f"Error deleting from MySQL: {db_error}")
                # Continue anyway - Redis update is primary
        
        return True
        
    except Exception as e:
        log_exception(logger, 'BANK', f"Error deleting transactions for account: {e}")
        return False


def get_credit_account_for_linked_account(user_id: int, linked_account_id: str) -> Optional[Dict]:
    """
    Find the Blankee credit account that corresponds to a linked account.
    
    Strategy:
    1. First, check for direct linked_account_id match (most reliable)
    2. Fallback to mask matching (last 4 digits)
    
    Args:
        user_id: User ID
        linked_account_id: The linked account_id (e.g., 'acct_xxx')
        
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
        
        # Strategy 1: Direct linked_account_id match (most reliable)
        for ca in credit_accounts:
            if ca.get('linked_account_id') == linked_account_id:
                log_info(logger, 'BANK', f"Found credit account {ca.get('id')} via direct linked_account_id match")
                return ca
        
        # Strategy 2: Fallback to mask matching
        # First, get the linked account to find its mask
        linked_accounts = get_linked_accounts(user_id)
        linked_account = None
        for qa in linked_accounts:
            if qa.get('account_id') == linked_account_id:
                linked_account = qa
                break
        
        if not linked_account:
            log_warning(logger, 'BANK', f"linked account {linked_account_id} not found for user {user_id}")
            return None
        
        mask = linked_account.get('mask')
        if not mask:
            log_warning(logger, 'BANK', f"linked account {linked_account_id} has no mask")
            return None
        
        # Find credit account with matching mask
        # the bank provider mask format may be "XXXX-XXXX-XXXX-7691" while Blankee stores just "7691"
        # Extract last 4 digits for comparison
        linked_last4 = mask[-4:] if mask and len(mask) >= 4 else mask
        
        for ca in credit_accounts:
            ca_mask = ca.get('mask')
            if not ca_mask:
                continue
            # Compare last 4 digits
            ca_last4 = ca_mask[-4:] if len(ca_mask) >= 4 else ca_mask
            if ca_last4 == linked_last4:
                log_info(logger, 'BANK', f"Found credit account {ca.get('id')} via mask match (last4: {ca_last4})")
                return ca
        
        log_warning(logger, 'BANK', f"No Blankee credit account found with mask ending in {linked_last4} for user {user_id}")
        return None
        
    except Exception as e:
        log_exception(logger, 'BANK', f"Error finding Blankee credit account: {e}")
        return None


# ============================================================================
# BANK ENTRY LOCKING HELPERS
# ============================================================================

def get_user_linked_account_flags(user_id: int) -> Dict[str, Any]:
    """
    Determine what linked account types a user has connected.
    Used for entry locking - entries linked to bank accounts cannot be edited.
    
    Args:
        user_id: User ID
        
    Returns:
        Dict with:
        - has_checking: bool - True if user has a checking account connected
        - has_savings: bool - True if user has a savings account connected  
        - linked_credit_ids: List[int] - Blankee credit_accounts.id where is_linked=1
        - savings_income_category_id: int|None - ID of "Savings" income category
        - savings_expense_category_id: int|None - ID of "Savings" expense category
    """
    result = {
        'has_checking': False,
        'has_savings': False,
        'linked_credit_ids': [],
        'savings_income_category_id': None,
        'savings_expense_category_id': None
    }
    
    try:
        # Get all bank accounts for user
        linked_accounts = get_linked_accounts(user_id)
        
        for account in linked_accounts:
            account_type = account.get('account_type', '').upper()
            account_name = account.get('account_name', '').lower()
            
            if account_type == 'DEPOSITORY':
                if 'checking' in account_name:
                    result['has_checking'] = True
                if 'savings' in account_name:
                    result['has_savings'] = True
            # CREDIT accounts are handled separately via credit_accounts table
        
        # Get bank-linked credit accounts from credit_accounts table
        result['linked_credit_ids'] = get_linked_credit_account_ids(user_id)
        
        # Get Savings category IDs. Imported here rather than at module scope:
        # redis_crud does not import bank_redis today, but keeping the edge
        # inside the function means adding that import later cannot turn this
        # into an import cycle.
        from redis_crud import get_savings_category_ids
        savings_ids = get_savings_category_ids(user_id)
        result['savings_income_category_id'] = savings_ids.get('income_savings_id')
        result['savings_expense_category_id'] = savings_ids.get('expense_savings_id')
        
        log_info(logger, 'BANK', f"bank account flags for user {user_id}: {result}")
        return result
        
    except Exception as e:
        log_exception(logger, 'BANK', f"Error getting bank account flags for user {user_id}: {e}")
        return result


def get_linked_credit_account_ids(user_id: int) -> List[int]:
    """
    Get list of Blankee credit_accounts.id where is_linked=1.
    These credit accounts are linked to bank and their entries should be locked.
    
    Args:
        user_id: User ID
        
    Returns:
        List of credit_accounts.id that are bank-linked
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
                cursor.execute("SELECT id, is_linked FROM credit_accounts WHERE user_id = %s", (user_id,))
                credit_accounts = cursor.fetchall()
                cursor.close()
        
        # Return IDs where is_linked = 1
        linked_ids = []
        for ca in credit_accounts:
            if ca.get('is_linked') == 1 or ca.get('is_linked') == True:
                linked_ids.append(ca.get('id'))
        
        return linked_ids
        
    except Exception as e:
        log_exception(logger, 'BANK', f"Error getting the bank provider credit account IDs for user {user_id}: {e}")
        return []






def get_last_linked_transaction_date(user_id: int) -> Optional[str]:
    """
    Get the date of the most recent synced transaction across all linked connections.
    Used for entry locking and dashboard sync markers.
    
    Checks dedicated cache key first, then computes from linked_transactions.
    
    Args:
        user_id: User ID
        
    Returns:
        Date string 'YYYY-MM-DD' or None if no transactions
    """
    redis_client = _get_redis_client()
    cache_key = f"bank_last_txn_date:v1:{user_id}"
    
    # Check dedicated cache key first
    if redis_client:
        try:
            cached = redis_client.get(cache_key)
            if cached:
                return cached if isinstance(cached, str) else cached.decode('utf-8')
        except Exception as e:
            log_warning(logger, 'BANK', f"Error reading bank_last_txn_date cache for user {user_id}: {e}")
    
    # Compute from linked_transactions in Redis
    last_date = None
    if redis_client:
        try:
            txn_key = _get_redis_key('linked_transactions', user_id)
            txn_data = redis_client.get(txn_key)
            if txn_data:
                transactions = json.loads(txn_data)
                for txn in transactions:
                    # Skip pending transactions — only POSTED txns should advance the marker
                    pending_val = txn.get('pending')
                    if pending_val in (1, True, '1', 'true', 'True'):
                        continue
                    txn_date = txn.get('date')
                    if txn_date and (last_date is None or txn_date > last_date):
                        last_date = txn_date
        except Exception as e:
            log_warning(logger, 'BANK', f"Error computing last txn date from Redis for user {user_id}: {e}")
    
    # MySQL fallback
    if last_date is None:
        try:
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT MAX(date) FROM linked_transactions WHERE user_id = %s AND pending = 0", (user_id,))
                row = cursor.fetchone()
                cursor.close()
                if row and row[0]:
                    last_date = row[0].strftime('%Y-%m-%d') if hasattr(row[0], 'strftime') else str(row[0])
        except Exception as e:
            log_error(logger, 'BANK', f"Error fetching last txn date from MySQL for user {user_id}: {e}")
    
    # Fallback: if no transactions at all, use earliest connection created_at date.
    # This locks entries from the day the bank was connected even before any sync.
    if last_date is None:
        try:
            connections = get_linked_connections(user_id)
            earliest = None
            for conn in connections:
                created = conn.get('created_at')
                if created:
                    date_str_val = created.strftime('%Y-%m-%d') if hasattr(created, 'strftime') else str(created)[:10]
                    if earliest is None or date_str_val < earliest:
                        earliest = date_str_val
            if earliest:
                last_date = earliest
        except Exception as e:
            log_error(logger, 'BANK', f"Error fetching connection created_at fallback for user {user_id}: {e}")
    
    # Cache the result
    if last_date and redis_client:
        try:
            redis_client.setex(cache_key, INACTIVITY_TIMEOUT, last_date)
        except Exception:
            pass
    
    return last_date


def update_last_linked_transaction_date(user_id: int, date_str: str = None):
    """
    Recompute and cache the last synced transaction date for a user.
    Call after syncing transactions.
    
    Args:
        user_id: User ID
        date_str: Optional date string to set directly (skips recompute)
    """
    redis_client = _get_redis_client()
    cache_key = f"bank_last_txn_date:v1:{user_id}"
    
    if date_str:
        # Set directly if provided
        if redis_client:
            try:
                redis_client.setex(cache_key, INACTIVITY_TIMEOUT, date_str)
            except Exception as e:
                log_warning(logger, 'BANK', f"Error caching bank_last_txn_date for user {user_id}: {e}")
        return
    
    # Invalidate cache so get_last_linked_transaction_date() recomputes
    if redis_client:
        try:
            redis_client.delete(cache_key)
        except Exception:
            pass
    
    # Trigger recompute
    get_last_linked_transaction_date(user_id)


# ============================================================================
# CATEGORY MEMORY — user-confirmed merchant→category mappings
# ============================================================================


# ============================================================
# Recurring Mismatches CRUD (Redis-first)
# ============================================================


# ─── Recurring Suggestions (Suggested Recurring Categories) ───────────────────