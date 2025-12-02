"""
Redis-First Operations for Quiltt Integration

This module provides Redis-first CRUD operations for Quiltt data (connections, accounts, transactions).
Data is written to Redis immediately and flushed to MySQL periodically by the Redis manager.
"""

import logging
import json
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
                # Update existing account
                cached_data[i].update(account_data)
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
        
        # Find and update the account
        found = False
        for i, acc in enumerate(cached_data):
            if acc.get('account_id') == account_id:
                cached_data[i][field] = value
                found = True
                break
        
        if not found:
            return False
        
        # Save to Redis and mark as dirty
        _set_to_redis('quiltt_accounts', user_id, cached_data)
        
        return True
        
    except Exception as e:
        logger.error(f"Error updating Quiltt account field: {e}", exc_info=True)
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
    if user_id is None:
        if not current_user.is_authenticated:
            return False
        user_id = current_user.id
    
    try:
        redis_client = _get_redis_client()
        if not redis_client:
            logger.warning("Redis client not available for delete, falling back to direct MySQL")
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
        connections = get_quiltt_connections(user_id)
        conn_db_id = None
        for conn in connections:
            if conn.get('connection_id') == connection_id:
                conn_db_id = conn.get('id')
                break
        
        # Remove connection from Redis
        cached_connections = _get_from_redis('quiltt_connections', user_id)
        if cached_connections is None:
            cached_connections = connections
        
        cached_connections = [c for c in cached_connections if c.get('connection_id') != connection_id]
        _set_to_redis('quiltt_connections', user_id, cached_connections)
        
        # Remove associated accounts from Redis
        if conn_db_id:
            cached_accounts = _get_from_redis('quiltt_accounts', user_id)
            if cached_accounts is None:
                cached_accounts = get_quiltt_accounts(user_id)
            
            if cached_accounts:
                cached_accounts = [a for a in cached_accounts if a.get('connection_id') != conn_db_id]
                _set_to_redis('quiltt_accounts', user_id, cached_accounts)
        
        # Mark both tables as dirty for deletion flush
        dirty_key = f"dirty_tables:{user_id}"
        redis_client.sadd(dirty_key, 'quiltt_connections_deleted')
        redis_client.sadd(dirty_key, 'quiltt_accounts_deleted')
        
        # Store the connection_id to delete
        delete_key = f"quiltt_connections_to_delete:{user_id}"
        redis_client.sadd(delete_key, connection_id)
        redis_client.expire(delete_key, 300)  # Expire in 5 minutes
        
        logger.info(f"Marked Quiltt connection {connection_id} for deletion (user {user_id})")
        return True
        
    except Exception as e:
        logger.error(f"Error deleting Quiltt connection: {e}", exc_info=True)
        return False
