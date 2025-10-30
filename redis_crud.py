"""
Redis-First CRUD Operations

This module provides CRUD operations that write to Redis immediately
and queue MySQL updates for background processing.

For migration from MySQL-only routes to Redis-first architecture.
"""

import logging
import json
from typing import Optional, List, Dict, Any, Union
from datetime import date, datetime
from decimal import Decimal
from flask_login import current_user
from redis_manager import (
    _redis_client, 
    _get_redis_key, 
    REDIS_KEY_VERSION,
    is_user_hydrated,
    DecimalEncoder
)
from db_connections import get_db_pool

logger = logging.getLogger(__name__)


class RedisDecoder(json.JSONDecoder):
    """Decode JSON with support for date/datetime strings"""
    def __init__(self, *args, **kwargs):
        super().__init__(object_hook=self.object_hook, *args, **kwargs)
    
    def object_hook(self, obj):
        for key, value in obj.items():
            if isinstance(value, str):
                # Try to parse ISO date strings
                try:
                    if 'T' in value or len(value) == 10:
                        obj[key] = datetime.fromisoformat(value).date()
                except (ValueError, AttributeError):
                    pass
        return obj


def _get_from_redis(table: str, user_id: int) -> Optional[List[Dict[str, Any]]]:
    """Get data from Redis"""
    if not _redis_client or not is_user_hydrated(user_id):
        return None
    
    redis_key = _get_redis_key(table, user_id)
    cached = _redis_client.get(redis_key)
    
    if cached:
        return json.loads(cached)
    return None


def _set_to_redis(table: str, user_id: int, data: List[Dict[str, Any]]) -> bool:
    """Set data to Redis"""
    if not _redis_client:
        return False
    
    try:
        redis_key = _get_redis_key(table, user_id)
        from redis_manager import INACTIVITY_TIMEOUT
        _redis_client.setex(
            redis_key,
            INACTIVITY_TIMEOUT + 60,
            json.dumps(data, cls=DecimalEncoder)
        )
        return True
    except Exception as e:
        logger.error(f"Error setting Redis data for {table}: {e}")
        return False


def add_entry(table: str, data: Dict[str, Any], user_id: Optional[int] = None) -> Optional[int]:
    """
    Add a new entry to a table (Redis + MySQL).
    
    Args:
        table: Table name (e.g., 'income_entries', 'expense_entries')
        data: Dictionary of column values
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        New entry ID or None if failed
        
    Example:
        entry_id = add_entry('income_entries', {
            'category_id': 5,
            'date': date.today(),
            'amount': Decimal('100.50'),
            'processed': 0
        })
    """
    if user_id is None:
        if not current_user.is_authenticated:
            logger.error("Cannot add entry: user not authenticated")
            return None
        user_id = current_user.id
    
    try:
        # Insert to MySQL first to get auto-increment ID
        with get_db_pool().get_cursor(commit=True, dictionary=True) as cursor:
            columns = ', '.join(data.keys())
            placeholders = ', '.join(['%s'] * len(data))
            query = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"
            
            cursor.execute(query, tuple(data.values()))
            new_id = cursor.lastrowid
            
            logger.debug(f"Inserted into {table}: ID={new_id}")
        
        # Update Redis if user is hydrated
        if is_user_hydrated(user_id):
            cached_data = _get_from_redis(table, user_id)
            if cached_data is not None:
                # Add new entry to cached data
                new_entry = {**data, 'id': new_id}
                cached_data.append(new_entry)
                _set_to_redis(table, user_id, cached_data)
                logger.debug(f"Updated Redis cache for {table}")
        
        return new_id
        
    except Exception as e:
        logger.error(f"Error adding entry to {table}: {e}", exc_info=True)
        return None


def update_entry(table: str, entry_id: int, data: Dict[str, Any], user_id: Optional[int] = None) -> bool:
    """
    Update an existing entry (Redis + MySQL).
    
    Args:
        table: Table name
        entry_id: ID of entry to update
        data: Dictionary of columns to update
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        True if successful
        
    Example:
        success = update_entry('income_entries', 123, {
            'amount': Decimal('150.00'),
            'date': date(2025, 10, 21)
        })
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return False
        user_id = current_user.id
    
    try:
        # Update MySQL
        with get_db_pool().get_cursor(commit=True) as cursor:
            set_clause = ', '.join([f"{col} = %s" for col in data.keys()])
            query = f"UPDATE {table} SET {set_clause} WHERE id = %s"
            
            cursor.execute(query, (*data.values(), entry_id))
            affected = cursor.rowcount
            
            logger.debug(f"Updated {table} ID={entry_id}: {affected} row(s)")
        
        # Update Redis if user is hydrated
        if is_user_hydrated(user_id):
            cached_data = _get_from_redis(table, user_id)
            if cached_data is not None:
                # Find and update entry in cache
                for i, entry in enumerate(cached_data):
                    if entry.get('id') == entry_id:
                        cached_data[i].update(data)
                        break
                _set_to_redis(table, user_id, cached_data)
                logger.debug(f"Updated Redis cache for {table}")
        
        return True
        
    except Exception as e:
        logger.error(f"Error updating {table} ID={entry_id}: {e}", exc_info=True)
        return False


def delete_entry(table: str, entry_id: int, user_id: Optional[int] = None) -> bool:
    """
    Delete an entry (Redis + MySQL).
    
    Args:
        table: Table name
        entry_id: ID of entry to delete
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        True if successful
        
    Example:
        success = delete_entry('income_entries', 123)
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return False
        user_id = current_user.id
    
    try:
        # Delete from MySQL
        with get_db_pool().get_cursor(commit=True) as cursor:
            query = f"DELETE FROM {table} WHERE id = %s"
            cursor.execute(query, (entry_id,))
            affected = cursor.rowcount
            
            logger.debug(f"Deleted from {table} ID={entry_id}: {affected} row(s)")
        
        # Update Redis if user is hydrated
        if is_user_hydrated(user_id):
            cached_data = _get_from_redis(table, user_id)
            if cached_data is not None:
                # Remove entry from cache
                cached_data = [e for e in cached_data if e.get('id') != entry_id]
                _set_to_redis(table, user_id, cached_data)
                logger.debug(f"Updated Redis cache for {table}")
        
        return True
        
    except Exception as e:
        logger.error(f"Error deleting {table} ID={entry_id}: {e}", exc_info=True)
        return False


def bulk_add_entries(table: str, entries: List[Dict[str, Any]], user_id: Optional[int] = None) -> List[int]:
    """
    Bulk add entries (more efficient than individual adds).
    
    Args:
        table: Table name
        entries: List of entry dictionaries
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        List of new entry IDs
        
    Example:
        ids = bulk_add_entries('income_entries', [
            {'category_id': 5, 'date': date.today(), 'amount': 100},
            {'category_id': 6, 'date': date.today(), 'amount': 200}
        ])
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return []
        user_id = current_user.id
    
    if not entries:
        return []
    
    new_ids = []
    
    try:
        # Bulk insert to MySQL
        with get_db_pool().get_cursor(commit=True, dictionary=True) as cursor:
            columns = ', '.join(entries[0].keys())
            placeholders = ', '.join(['%s'] * len(entries[0]))
            query = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"
            
            for entry in entries:
                cursor.execute(query, tuple(entry.values()))
                new_ids.append(cursor.lastrowid)
            
            logger.debug(f"Bulk inserted {len(entries)} entries into {table}")
        
        # Update Redis if user is hydrated
        if is_user_hydrated(user_id):
            cached_data = _get_from_redis(table, user_id)
            if cached_data is not None:
                # Add all new entries
                for i, entry in enumerate(entries):
                    new_entry = {**entry, 'id': new_ids[i]}
                    cached_data.append(new_entry)
                _set_to_redis(table, user_id, cached_data)
                logger.debug(f"Updated Redis cache for {table} with {len(entries)} entries")
        
        return new_ids
        
    except Exception as e:
        logger.error(f"Error bulk adding to {table}: {e}", exc_info=True)
        return new_ids


def bulk_update_entries(table: str, updates: List[Dict[str, Any]], user_id: Optional[int] = None) -> bool:
    """
    Bulk update entries (each dict must include 'id' key).
    
    Args:
        table: Table name
        updates: List of dicts with 'id' and columns to update
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        True if successful
        
    Example:
        success = bulk_update_entries('income_entries', [
            {'id': 1, 'amount': Decimal('100')},
            {'id': 2, 'amount': Decimal('200')}
        ])
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return False
        user_id = current_user.id
    
    if not updates:
        return True
    
    try:
        # Update MySQL - use batch update for better performance
        with get_db_pool().get_cursor(commit=True) as cursor:
            # Group updates by columns being updated for efficient batching
            updates_by_columns = {}
            for update_data in updates:
                update = dict(update_data)
                entry_id = update.pop('id')
                
                if not update:
                    continue
                
                # Create a key from sorted column names
                col_key = tuple(sorted(update.keys()))
                if col_key not in updates_by_columns:
                    updates_by_columns[col_key] = []
                updates_by_columns[col_key].append((entry_id, update))
            
            # Execute batched updates for each column set
            for columns, batch in updates_by_columns.items():
                if not batch:
                    continue
                
                # For single column updates, use CASE statement for efficiency
                if len(columns) == 1:
                    col = columns[0]
                    ids = [item[0] for item in batch]
                    
                    # Build CASE statement for batch update
                    case_parts = []
                    params = []
                    for entry_id, update_dict in batch:
                        case_parts.append("WHEN id = %s THEN %s")
                        params.extend([entry_id, update_dict[col]])
                    
                    case_stmt = " ".join(case_parts)
                    params.extend(ids)
                    
                    query = f"""
                        UPDATE {table}
                        SET {col} = CASE {case_stmt} END
                        WHERE id IN ({','.join(['%s'] * len(ids))})
                    """
                    cursor.execute(query, params)
                else:
                    # For multiple columns, fall back to individual updates
                    for entry_id, update_dict in batch:
                        set_clause = ', '.join([f"{col} = %s" for col in update_dict.keys()])
                        query = f"UPDATE {table} SET {set_clause} WHERE id = %s"
                        cursor.execute(query, (*update_dict.values(), entry_id))
            
            logger.debug(f"Bulk updated {len(updates)} entries in {table}")
        
        # Update Redis if user is hydrated
        if is_user_hydrated(user_id):
            cached_data = _get_from_redis(table, user_id)
            if cached_data is not None:
                # Update entries in cache
                update_map = {u['id']: u for u in updates}
                for i, entry in enumerate(cached_data):
                    entry_id = entry.get('id')
                    if entry_id in update_map:
                        # Update only the fields that are in the update dict (excluding 'id')
                        update_fields = {k: v for k, v in update_map[entry_id].items() if k != 'id'}
                        cached_data[i].update(update_fields)
                _set_to_redis(table, user_id, cached_data)
                logger.debug(f"Updated Redis cache for {table}")
        
        return True
        
    except Exception as e:
        logger.error(f"Error bulk updating {table}: {e}", exc_info=True)
        return False


def get_entries(table: str, filters: Optional[Dict[str, Any]] = None, user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Get entries with optional filtering (Redis-first).
    
    Args:
        table: Table name
        filters: Optional dict of column: value filters
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        List of matching entries
        
    Example:
        # Get all entries
        entries = get_entries('income_entries')
        
        # Get entries with filters
        entries = get_entries('income_entries', {'category_id': 5, 'processed': 0})
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return []
        user_id = current_user.id
    
    # Try Redis first
    if is_user_hydrated(user_id):
        cached_data = _get_from_redis(table, user_id)
        if cached_data is not None:
            # Apply filters if provided
            if filters:
                result = []
                for entry in cached_data:
                    match = all(entry.get(k) == v for k, v in filters.items())
                    if match:
                        result.append(entry)
                return result
            return cached_data
    
    # Fallback to MySQL - need to filter by user_id through category tables
    try:
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            # Determine category table based on entry table
            if table == 'income_entries':
                category_table = 'income_categories'
                query = f"""
                    SELECT e.* FROM {table} e
                    JOIN {category_table} c ON e.category_id = c.id
                    WHERE c.user_id = %s
                """
                params = [user_id]
            elif table == 'expense_entries':
                category_table = 'expense_categories'
                query = f"""
                    SELECT e.* FROM {table} e
                    JOIN {category_table} c ON e.category_id = c.id
                    WHERE c.user_id = %s
                """
                params = [user_id]
            elif table == 'c_expense_entries':
                query = f"""
                    SELECT e.* FROM {table} e
                    JOIN c_expense_categories c ON e.category_id = c.id
                    JOIN credit_accounts a ON c.account_id = a.id
                    WHERE a.user_id = %s
                """
                params = [user_id]
            else:
                # For other tables, just query directly (no user filtering)
                query = f"SELECT * FROM {table} WHERE 1=1"
                params = []
            
            # Add filters
            if filters:
                for col, val in filters.items():
                    query += f" AND e.{col} = %s"
                    params.append(val)
            
            cursor.execute(query, tuple(params))
            return cursor.fetchall()
    except Exception as e:
        logger.error(f"Error getting entries from {table}: {e}", exc_info=True)
        return []


# Convenience functions for common operations

def add_income_entry(category_id: int, date_val: date, amount: Decimal, recurring_id: Optional[int] = None) -> Optional[int]:
    """Add income entry"""
    return add_entry('income_entries', {
        'category_id': category_id,
        'date': date_val,
        'amount': amount,
        'recurring_id': recurring_id,
        'processed': 0
    })


def add_expense_entry(category_id: int, date_val: date, amount: Decimal, recurring_id: Optional[int] = None, bud_item_id: Optional[int] = None) -> Optional[int]:
    """Add expense entry"""
    return add_entry('expense_entries', {
        'category_id': category_id,
        'date': date_val,
        'amount': amount,
        'recurring_id': recurring_id,
        'bud_item_id': bud_item_id,
        'processed': 0
    })


def update_user_profile(updates: Dict[str, Any], user_id: Optional[int] = None) -> bool:
    """
    Update user profile data.
    
    Args:
        updates: Dict of columns to update
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        True if successful
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return False
        user_id = current_user.id
    
    try:
        # Update MySQL
        with get_db_pool().get_cursor(commit=True) as cursor:
            set_clause = ', '.join([f"{col} = %s" for col in updates.keys()])
            query = f"UPDATE users SET {set_clause} WHERE id = %s"
            cursor.execute(query, (*updates.values(), user_id))
        
        # Update Redis if user is hydrated
        if is_user_hydrated(user_id):
            if _redis_client:
                redis_key = f"users:{REDIS_KEY_VERSION}:{user_id}"
                cached = _redis_client.get(redis_key)
                if cached:
                    user_data = json.loads(cached)
                    user_data.update(updates)
                    from redis_manager import INACTIVITY_TIMEOUT
                    _redis_client.setex(
                        redis_key,
                        INACTIVITY_TIMEOUT + 60,
                        json.dumps(user_data, cls=DecimalEncoder)
                    )
        
        return True
        
    except Exception as e:
        logger.error(f"Error updating user profile: {e}", exc_info=True)
        return False
