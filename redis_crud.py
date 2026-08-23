"""
Redis-First CRUD Operations

This module provides CRUD operations that write to Redis immediately
and queue MySQL updates for background processing.

For migration from MySQL-only routes to Redis-first architecture.
"""

import json
import time
import pymysql.cursors
from typing import Optional, List, Dict, Any, Union
from datetime import date, datetime
from decimal import Decimal
from flask_login import current_user
from redis_manager import (
    is_user_hydrated,
    get_table_cache,
    set_table_cache,
    INACTIVITY_TIMEOUT
)
from db_connections import get_db_pool
from log_config import get_logger, log_info, log_error, log_warning, log_exception

logger = get_logger(__name__)


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
    return get_table_cache(table, user_id)


def _get_categories_from_redis(table_name: str, user_id: int) -> Optional[List[Dict[str, Any]]]:
    """
    Categories from the Redis cache, or None when the user is not hydrated.

    A thin alias over _get_from_redis, kept because the call sites read as a
    deliberate pair - Redis first, then _get_categories_from_mysql - and that
    pairing is easier to follow than a bare _get_from_redis next to a MySQL
    fallback that does something table-specific.
    """
    return _get_from_redis(table_name, user_id)


def _get_categories_from_mysql(table_name: str, user_id: int) -> Optional[List[Dict[str, Any]]]:
    """
    Categories straight from MySQL, bypassing Redis entirely.

    Needed because the callers also run from cron (the nightly sync), where no
    request has hydrated the user and _get_categories_from_redis returns None
    for a perfectly valid account.

    c_expense_categories is keyed by account_id rather than user_id, so
    ownership has to travel through credit_accounts - the same asymmetry that
    the entry tables have.

    table_name is checked against a whitelist rather than interpolated on
    trust. It only ever arrives from a literal today, but this string reaches
    an f-string in a query, and that is not a property worth relying on
    callers to preserve.
    """
    if table_name not in ('income_categories', 'expense_categories', 'c_expense_categories'):
        log_error(logger, 'CATEGORY', f'Refusing to query unknown category table {table_name!r}')
        return None

    try:
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            if table_name == 'c_expense_categories':
                cursor.execute(
                    "SELECT cec.id, cec.name FROM c_expense_categories cec "
                    "JOIN credit_accounts ca ON cec.account_id = ca.id "
                    "WHERE ca.user_id = %s",
                    (user_id,)
                )
            else:
                cursor.execute(
                    f"SELECT id, name FROM {table_name} WHERE user_id = %s",
                    (user_id,)
                )
            rows = cursor.fetchall()
        return rows or None
    except Exception as e:
        log_error(logger, 'CATEGORY',
                  f'Error reading {table_name} from MySQL for user {user_id}: {e}')
        return None


def _set_to_redis(table: str, user_id: int, data: List[Dict[str, Any]]) -> bool:
    """
    Refresh this table's Redis cache to match MySQL.

    mark_dirty=False is deliberate: every write path in this module inserts or
    updates MySQL itself first and only then refreshes the cache, so MySQL is
    already current and queueing a flush would be redundant work.

    A Redis-first write - one where nothing has touched MySQL yet - must call
    redis_manager.set_table_cache(..., mark_dirty=True) instead, or the write
    lives only in Redis until the TTL evicts it.
    """
    return set_table_cache(table, user_id, data, mark_dirty=False)


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
            log_error(logger, 'REDIS', "Cannot add entry: user not authenticated")
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
            
            log_info(logger, 'REDIS', f"Inserted into {table}: ID={new_id}")
        
        # Update Redis if user is hydrated
        if is_user_hydrated(user_id):
            cached_data = _get_from_redis(table, user_id)
            if cached_data is not None:
                # Add new entry to cached data
                new_entry = {**data, 'id': new_id}
                cached_data.append(new_entry)
                _set_to_redis(table, user_id, cached_data)
                log_info(logger, 'REDIS', f"Updated Redis cache for {table}")
        
        return new_id
        
    except Exception as e:
        log_exception(logger, 'REDIS', f"Error adding entry to {table}: {e}")
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
            
            log_info(logger, 'REDIS', f"Updated {table} ID={entry_id}: {affected} row(s)")
        
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
                log_info(logger, 'REDIS', f"Updated Redis cache for {table}")
        
        return True
        
    except Exception as e:
        log_exception(logger, 'REDIS', f"Error updating {table} ID={entry_id}: {e}")
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
            
            log_info(logger, 'REDIS', f"Deleted from {table} ID={entry_id}: {affected} row(s)")
        
        # Update Redis if user is hydrated
        if is_user_hydrated(user_id):
            cached_data = _get_from_redis(table, user_id)
            if cached_data is not None:
                # Remove entry from cache
                cached_data = [e for e in cached_data if e.get('id') != entry_id]
                _set_to_redis(table, user_id, cached_data)
                log_info(logger, 'REDIS', f"Updated Redis cache for {table}")
        
        return True
        
    except Exception as e:
        log_exception(logger, 'REDIS', f"Error deleting {table} ID={entry_id}: {e}")
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
            
            log_info(logger, 'REDIS', f"Bulk inserted {len(entries)} entries into {table}")
        
        # Update Redis if user is hydrated
        if is_user_hydrated(user_id):
            cached_data = _get_from_redis(table, user_id)
            if cached_data is not None:
                # Add all new entries
                for i, entry in enumerate(entries):
                    new_entry = {**entry, 'id': new_ids[i]}
                    cached_data.append(new_entry)
                _set_to_redis(table, user_id, cached_data)
                log_info(logger, 'REDIS', f"Updated Redis cache for {table} with {len(entries)} entries")
        
        return new_ids
        
    except Exception as e:
        log_exception(logger, 'REDIS', f"Error bulk adding to {table}: {e}")
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
        # Update MySQL
        with get_db_pool().get_cursor(commit=True) as cursor:
            for update_data in updates:
                # Make a copy to avoid modifying the original
                update = dict(update_data)
                entry_id = update.pop('id')
                
                if not update:
                    continue
                    
                set_clause = ', '.join([f"{col} = %s" for col in update.keys()])
                query = f"UPDATE {table} SET {set_clause} WHERE id = %s"
                cursor.execute(query, (*update.values(), entry_id))
            
            log_info(logger, 'REDIS', f"Bulk updated {len(updates)} entries in {table}")
        
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
                log_info(logger, 'REDIS', f"Updated Redis cache for {table}")
        
        return True
        
    except Exception as e:
        log_exception(logger, 'REDIS', f"Error bulk updating {table}: {e}")
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
        log_exception(logger, 'REDIS', f"Error getting entries from {table}: {e}")
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
        # NOTE: the users key holds a single dict, not a row list - the cache
        # helpers are shape-agnostic, so the same pair works here.
        user_data = get_table_cache('users', user_id)
        if user_data:
            user_data.update(updates)
            set_table_cache('users', user_id, user_data, mark_dirty=False)

        return True
        
    except Exception as e:
        log_exception(logger, 'REDIS', f"Error updating user profile: {e}")
        return False


# =============================================================================
# Provider-neutral helpers
#
# These moved here from the old vendor modules when the bank-aggregation and
# enrichment integrations were removed. Nothing in them is provider-specific:
# they are category lookups, merchant->category memory, and the recurring
# mismatch/suggestion detection store, all of which outlive any one provider.
#
# NOTE these are Redis-FIRST: they write Redis and mark the table dirty so the
# flush worker carries it to MySQL. That is why they call _set_redis_first()
# and NOT this module's _set_to_redis(), which is Redis-second.
# =============================================================================


def _get_redis_client():
    """Get Redis client at call time (it is None until init_redis_manager runs)."""
    from redis_manager import _redis_client
    return _redis_client


def _set_redis_first(table: str, user_id: int, data) -> bool:
    """Redis-first write: cache it and queue the flush to MySQL."""
    return set_table_cache(table, user_id, data, mark_dirty=True)


def _set_redis_first_no_dirty(table: str, user_id: int, data) -> bool:
    """Redis-first write with the flush suppressed (deletion staging)."""
    return set_table_cache(table, user_id, data, mark_dirty=False)


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
            
            log_warning(logger, 'BANK', f"Uncategorized income category not found for user {user_id}")
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
            
            log_warning(logger, 'BANK', f"Uncategorized expense category not found for user {user_id}")
            return None
            
        elif entry_type == 'c_expense':
            if not account_id:
                log_error(logger, 'BANK', "account_id is required for c_expense entry type")
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
            
            log_warning(logger, 'BANK', f"Uncategorized c_expense category not found for account {account_id}")
            return None
            
        elif entry_type == 'c_payment':
            # c_payment_entries don't have categories - they are tied directly to credit accounts
            # Return the account_id itself as it's used in the c_payment_entries table
            if not account_id:
                log_error(logger, 'BANK', "account_id is required for c_payment entry type")
                return None
            return account_id
            
        else:
            log_error(logger, 'BANK', f"Unknown entry type: {entry_type}")
            return None
            
    except Exception as e:
        log_exception(logger, 'BANK', f"Error getting Uncategorized category: {e}")
        return None


def get_savings_category_ids(user_id: int) -> Dict[str, Optional[int]]:
    """
    Get the category IDs for "Savings" in income_categories and expense_categories.
    Used for locking only the Savings category when user has the bank provider savings account.
    
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
                if int(cat.get('is_savings') or 0) == 1:
                    result['income_savings_id'] = cat.get('id')
                    break
        else:
            # Fallback to MySQL for income
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute(
                    "SELECT id FROM income_categories WHERE user_id = %s AND is_savings = 1 LIMIT 1",
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
                if int(cat.get('is_savings') or 0) == 1:
                    result['expense_savings_id'] = cat.get('id')
                    break
        else:
            # Fallback to MySQL for expense
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute(
                    "SELECT id FROM expense_categories WHERE user_id = %s AND is_savings = 1 LIMIT 1",
                    (user_id,)
                )
                row = cursor.fetchone()
                if row:
                    result['expense_savings_id'] = row['id']
                cursor.close()
        
        return result
        
    except Exception as e:
        log_exception(logger, 'BANK', f"Error getting Savings category IDs for user {user_id}: {e}")
        return result


def upsert_category_memory(user_id, merchant_id, description, category_id, category_type):
    """
    Save or update a user's category choice for a merchant/description.
    Called when user confirms a pending transaction.

    Description is the primary match key (always present).
    merchant_id is supplementary (stored for faster lookup when available).

    category_type is 'outgoing' or 'incoming' (unified across debit/credit).
    category_id is the canonical id (expense_categories.id for 'outgoing',
    income_categories.id for 'incoming').
    """
    if not description:
        return

    redis_key = f"category_memory:v1:{user_id}"
    try:
        cached = _get_from_redis('category_memory', user_id)
        if cached is None:
            cached = []

        # Match by description + category_type (the unique key)
        found = False
        for mapping in cached:
            if mapping.get('description') == description and mapping.get('category_type') == category_type:
                mapping['category_id'] = category_id
                mapping['account_id'] = None
                mapping['times_confirmed'] = mapping.get('times_confirmed', 1) + 1
                # Update merchant_id if we now have one
                if merchant_id and not mapping.get('merchant_id'):
                    mapping['merchant_id'] = merchant_id
                found = True
                break

        if not found:
            new_id = -(len(cached) + 1)  # Temp negative ID for Redis-only rows
            cached.append({
                'id': new_id,
                'user_id': user_id,
                'merchant_id': merchant_id,
                'description': description,
                'category_id': category_id,
                'category_type': category_type,
                'account_id': None,
                'times_confirmed': 1
            })

        _set_redis_first('category_memory', user_id, cached)

        # Mark dirty for MySQL flush
        redis_client = _get_redis_client()
        if redis_client:
            dirty_key = f"dirty_tables:{user_id}"
            redis_client.sadd(dirty_key, 'category_memory')
            redis_client.expire(dirty_key, INACTIVITY_TIMEOUT)
    except Exception as e:
        log_error(logger, 'BANK', f"Error upserting category memory for user {user_id}: {e}")


def lookup_category_memory(user_id, merchant_id=None, description=None, category_type=None, account_type=None):
    """
    Look up a user's remembered category for a merchant/description.
    Returns dict with category_id, category_type, account_id or None.

    category_type is 'outgoing' or 'incoming' (unified across debit/credit).
    The returned category_id is the canonical id (expense_categories.id for
    outgoing, income_categories.id for incoming) -- per-account c_expense
    resolution happens at apply time via resolve_suggestion_for_entry().

    Lookup order:
      1. Match by merchant_id + category_type (best)
      2. Match by description + category_type (fallback)

    Suppression rules (defensive -- mirrored from the old enrichment module):
      - Savings categories (is_savings=1) never returned as suggestions.
      - Credit-payment mirror categories (is_credit_account=1 on expense_categories)
        never returned when account_type='CREDIT'.
    """
    if not merchant_id and not description:
        return None

    def _resolve_cat_row(match):
        """Look up the full category row for a memory match so we can inspect flags."""
        cat_id = match.get('category_id')
        cat_type = match.get('category_type')
        # Map both new (outgoing/incoming) and legacy (income/expense/c_expense)
        # category_type values to the right Redis cache.
        table_map = {
            'outgoing': 'expense_categories',
            'incoming': 'income_categories',
            # Legacy values -- still readable until migration runs.
            'income': 'income_categories',
            'expense': 'expense_categories',
            'c_expense': 'c_expense_categories',
        }
        table = table_map.get(cat_type)
        if not cat_id or not table:
            return None
        cats = _get_from_redis(table, user_id)
        if not cats:
            return None
        for c in cats:
            try:
                if int(c.get('id', 0)) == int(cat_id):
                    return c
            except (TypeError, ValueError):
                continue
        return None

    def _suppress(match):
        """Apply savings + credit-payment suppression rules."""
        cat = _resolve_cat_row(match)
        if not cat:
            return False
        try:
            if int(cat.get('is_savings') or 0) == 1:
                return True
            if str(account_type or '').upper() == 'CREDIT' and int(cat.get('is_credit_account') or 0) == 1:
                return True
        except (TypeError, ValueError):
            return False
        return False

    try:
        cached = _get_from_redis('category_memory', user_id)
        if cached is None:
            # Fallback to MySQL
            from db_connections import get_db_pool
            with get_db_pool().get_connection() as conn:
                cursor = conn.cursor(pymysql.cursors.DictCursor)
                cursor.execute(
                    "SELECT * FROM category_memory WHERE user_id = %s",
                    (user_id,)
                )
                cached = cursor.fetchall()
                cursor.close()
            if cached:
                _set_redis_first('category_memory', user_id, list(cached))
            else:
                return None

        # 1. Try merchant_id match
        if merchant_id:
            for m in cached:
                if m.get('merchant_id') == merchant_id:
                    if category_type is None or m.get('category_type') == category_type:
                        result = {
                            'category_id': m.get('category_id'),
                            'category_type': m.get('category_type'),
                            'account_id': m.get('account_id'),
                            'times_confirmed': m.get('times_confirmed', 1)
                        }
                        if not _suppress(result):
                            return result

        # 2. Fallback: description match
        if description:
            for m in cached:
                if m.get('description') == description:
                    if category_type is None or m.get('category_type') == category_type:
                        result = {
                            'category_id': m.get('category_id'),
                            'category_type': m.get('category_type'),
                            'account_id': m.get('account_id'),
                            'times_confirmed': m.get('times_confirmed', 1)
                        }
                        if not _suppress(result):
                            return result

        return None
    except Exception as e:
        log_error(logger, 'BANK', f"Error looking up category memory for user {user_id}: {e}")
        return None


def get_recurring_mismatches(user_id=None, dismissed=False):
    """
    Get recurring mismatches for a user from Redis or MySQL.
    
    Args:
        user_id: User ID (defaults to current_user.id)
        dismissed: If False (default), only return non-dismissed mismatches
        
    Returns:
        List of mismatch dicts
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return []
        user_id = current_user.id
    
    # Try Redis first
    if is_user_hydrated(user_id):
        cached_data = _get_from_redis('recurring_mismatches', user_id)
        if cached_data is not None:
            if not dismissed:
                return [m for m in cached_data if not int(m.get('dismissed', 0))]
            return cached_data
    
    # Fallback to MySQL
    try:
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            if dismissed:
                cursor.execute("SELECT * FROM recurring_mismatches WHERE user_id = %s", (user_id,))
            else:
                cursor.execute("SELECT * FROM recurring_mismatches WHERE user_id = %s AND dismissed = 0", (user_id,))
            return cursor.fetchall()
    except Exception as e:
        log_error(logger, 'MISMATCH', f"Error getting recurring mismatches from MySQL: {e}", user_id=user_id)
        return []


def upsert_recurring_mismatch(mismatch_data, user_id=None):
    """
    Insert or update a recurring mismatch record (Redis-first).
    Uses (recurring_table, recurring_id) as the unique key.
    Always overwrites with latest — un-dismisses if previously dismissed.
    
    Args:
        mismatch_data: Dict with: recurring_table, recurring_id, category_id, transaction_id
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        Mismatch ID or None
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return None
        user_id = current_user.id
    
    recurring_table = mismatch_data.get('recurring_table')
    recurring_id = mismatch_data.get('recurring_id')
    if not recurring_table or not recurring_id:
        log_error(logger, 'MISMATCH', "recurring_table and recurring_id are required")
        return None
    
    try:
        # Get current data from Redis or MySQL
        cached_data = _get_from_redis('recurring_mismatches', user_id)
        if cached_data is None:
            cached_data = get_recurring_mismatches(user_id, dismissed=True)
            if cached_data is None:
                cached_data = []
        
        if not isinstance(cached_data, list):
            cached_data = list(cached_data) if cached_data else []
        
        # Find existing by unique key (recurring_table + recurring_id)
        db_id = None
        found = False
        for i, m in enumerate(cached_data):
            if m.get('recurring_table') == recurring_table and int(m.get('recurring_id', 0)) == int(recurring_id):
                # Update existing — always overwrite with latest, un-dismiss
                cached_data[i]['category_id'] = mismatch_data.get('category_id', m.get('category_id'))
                cached_data[i]['transaction_id'] = mismatch_data.get('transaction_id')
                cached_data[i]['dismissed'] = 0
                cached_data[i]['created_at'] = datetime.now().isoformat()
                db_id = cached_data[i].get('id')
                found = True
                log_info(logger, 'MISMATCH', 'Updated existing mismatch', recurring_table=recurring_table, recurring_id=recurring_id, user_id=user_id)
                break
        
        if not found:
            # Generate temp negative ID
            temp_id = -int(time.time() * 1000) % 1000000
            if temp_id > 0:
                temp_id = -temp_id
            db_id = temp_id
            new_mismatch = {
                'id': db_id,
                'user_id': user_id,
                'recurring_table': recurring_table,
                'recurring_id': int(recurring_id),
                'category_id': int(mismatch_data.get('category_id', 0)),
                'transaction_id': mismatch_data.get('transaction_id'),
                'dismissed': 0,
                'created_at': datetime.now().isoformat()
            }
            cached_data.append(new_mismatch)
            log_info(logger, 'MISMATCH', 'Created new mismatch', recurring_table=recurring_table, recurring_id=recurring_id, user_id=user_id)
        
        # Save to Redis and mark dirty
        _set_redis_first('recurring_mismatches', user_id, cached_data)
        
        return db_id
        
    except Exception as e:
        log_exception(logger, 'MISMATCH', f"Error upserting recurring mismatch: {e}", user_id=user_id)
        return None


def dismiss_recurring_mismatch(mismatch_id, user_id=None):
    """
    Dismiss a recurring mismatch by ID.
    
    Args:
        mismatch_id: The mismatch record ID
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        True if successful
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return False
        user_id = current_user.id
    
    try:
        cached_data = _get_from_redis('recurring_mismatches', user_id)
        if cached_data is None:
            cached_data = get_recurring_mismatches(user_id, dismissed=True)
            if cached_data is None:
                return False
        
        if not isinstance(cached_data, list):
            cached_data = list(cached_data) if cached_data else []
        
        found = False
        for i, m in enumerate(cached_data):
            if int(m.get('id', 0)) == int(mismatch_id):
                cached_data[i]['dismissed'] = 1
                found = True
                break
        
        if not found:
            log_warning(logger, 'MISMATCH', f"Mismatch ID {mismatch_id} not found for dismiss", user_id=user_id)
            return False
        
        _set_redis_first('recurring_mismatches', user_id, cached_data)
        log_info(logger, 'MISMATCH', 'Mismatch dismissed', mismatch_id=mismatch_id, user_id=user_id)
        return True
        
    except Exception as e:
        log_exception(logger, 'MISMATCH', f"Error dismissing mismatch: {e}", user_id=user_id)
        return False


def delete_recurring_mismatch(recurring_table, recurring_id, user_id=None):
    """
    Delete a recurring mismatch by its unique key (recurring_table + recurring_id).
    Used for self-healing when values now match.
    
    Args:
        recurring_table: 'recurring_income', 'recurring_expense', or 'recurring_c_expense'
        recurring_id: The recurring entry ID
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        True if a record was deleted
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return False
        user_id = current_user.id
    
    try:
        redis_client = _get_redis_client()
        cached_data = _get_from_redis('recurring_mismatches', user_id)
        if cached_data is None:
            cached_data = get_recurring_mismatches(user_id, dismissed=True)
            if cached_data is None:
                return False
        
        if not isinstance(cached_data, list):
            cached_data = list(cached_data) if cached_data else []
        
        # Find and remove the matching record
        removed_id = None
        new_data = []
        for m in cached_data:
            if m.get('recurring_table') == recurring_table and int(m.get('recurring_id', 0)) == int(recurring_id):
                removed_id = m.get('id')
            else:
                new_data.append(m)
        
        if removed_id is None:
            return False
        
        # Save updated list (without marking dirty — deletion handled separately)
        _set_redis_first_no_dirty('recurring_mismatches', user_id, new_data)
        
        # Mark for pending delete in MySQL
        if redis_client and removed_id and int(removed_id) > 0:
            pending_key = f"pending_deletes:recurring_mismatches:{user_id}"
            redis_client.sadd(pending_key, str(removed_id))
            redis_client.expire(pending_key, 604800)
            dirty_key = f"dirty_tables:{user_id}"
            redis_client.sadd(dirty_key, 'recurring_mismatches')
        elif redis_client:
            # Temp ID (negative) — just remove from Redis, nothing in MySQL to delete
            _set_redis_first('recurring_mismatches', user_id, new_data)
        
        log_info(logger, 'MISMATCH', 'Mismatch deleted (self-healing)', recurring_table=recurring_table, recurring_id=recurring_id, user_id=user_id)
        return True
        
    except Exception as e:
        log_exception(logger, 'MISMATCH', f"Error deleting mismatch: {e}", user_id=user_id)
        return False


def get_recurring_suggestions(user_id=None, dismissed=False):
    """
    Get recurring suggestions for a user from Redis or MySQL.
    
    Args:
        user_id: User ID (defaults to current_user.id)
        dismissed: If False (default), only return non-dismissed suggestions
        
    Returns:
        List of suggestion dicts
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return []
        user_id = current_user.id
    
    # Try Redis first
    if is_user_hydrated(user_id):
        cached_data = _get_from_redis('recurring_suggestions', user_id)
        if cached_data is not None:
            if not dismissed:
                return [s for s in cached_data if not int(s.get('dismissed', 0))]
            return cached_data
    
    # Fallback to MySQL
    try:
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            if dismissed:
                cursor.execute("SELECT * FROM recurring_suggestions WHERE user_id = %s", (user_id,))
            else:
                cursor.execute("SELECT * FROM recurring_suggestions WHERE user_id = %s AND dismissed = 0", (user_id,))
            return cursor.fetchall()
    except Exception as e:
        log_error(logger, 'SUGGEST_REC', f"Error getting recurring suggestions from MySQL: {e}", user_id=user_id)
        return []


def upsert_recurring_suggestion(suggestion_data, user_id=None):
    """
    Insert or update a recurring suggestion record (Redis-first).
    Uses (suggestion_type, category_id) as the unique key.
    Always overwrites with latest — un-dismisses if previously dismissed.
    
    Args:
        suggestion_data: Dict with: suggestion_type, category_id, transaction_id,
                         detected_amount, detected_cadence_interval, detected_cadence_unit,
                         detected_weekday, detected_monthly_day
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        Suggestion ID or None
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return None
        user_id = current_user.id
    
    suggestion_type = suggestion_data.get('suggestion_type')
    category_id = suggestion_data.get('category_id')
    if not suggestion_type or not category_id:
        log_error(logger, 'SUGGEST_REC', "suggestion_type and category_id are required")
        return None
    
    try:
        # Get current data from Redis or MySQL
        cached_data = _get_from_redis('recurring_suggestions', user_id)
        if cached_data is None:
            cached_data = get_recurring_suggestions(user_id, dismissed=True)
            if cached_data is None:
                cached_data = []
        
        if not isinstance(cached_data, list):
            cached_data = list(cached_data) if cached_data else []
        
        # Find existing by unique key (suggestion_type + category_id)
        db_id = None
        found = False
        for i, s in enumerate(cached_data):
            if s.get('suggestion_type') == suggestion_type and int(s.get('category_id', 0)) == int(category_id):
                # Update existing — always overwrite with latest, un-dismiss
                cached_data[i]['transaction_id'] = suggestion_data.get('transaction_id')
                cached_data[i]['detected_amount'] = suggestion_data.get('detected_amount')
                cached_data[i]['detected_cadence_interval'] = suggestion_data.get('detected_cadence_interval')
                cached_data[i]['detected_cadence_unit'] = suggestion_data.get('detected_cadence_unit')
                cached_data[i]['detected_weekday'] = suggestion_data.get('detected_weekday')
                cached_data[i]['detected_monthly_day'] = suggestion_data.get('detected_monthly_day')
                cached_data[i]['dismissed'] = 0
                cached_data[i]['created_at'] = datetime.now().isoformat()
                db_id = cached_data[i].get('id')
                found = True
                log_info(logger, 'SUGGEST_REC', 'Updated existing suggestion', suggestion_type=suggestion_type, category_id=category_id, user_id=user_id)
                break
        
        if not found:
            # Generate temp negative ID
            temp_id = -int(time.time() * 1000) % 1000000
            if temp_id > 0:
                temp_id = -temp_id
            db_id = temp_id
            new_suggestion = {
                'id': db_id,
                'user_id': user_id,
                'suggestion_type': suggestion_type,
                'category_id': int(category_id),
                'transaction_id': suggestion_data.get('transaction_id'),
                'detected_amount': suggestion_data.get('detected_amount'),
                'detected_cadence_interval': suggestion_data.get('detected_cadence_interval'),
                'detected_cadence_unit': suggestion_data.get('detected_cadence_unit'),
                'detected_weekday': suggestion_data.get('detected_weekday'),
                'detected_monthly_day': suggestion_data.get('detected_monthly_day'),
                'dismissed': 0,
                'created_at': datetime.now().isoformat()
            }
            cached_data.append(new_suggestion)
            log_info(logger, 'SUGGEST_REC', 'Created new suggestion', suggestion_type=suggestion_type, category_id=category_id, user_id=user_id)
        
        # Save to Redis and mark dirty
        _set_redis_first('recurring_suggestions', user_id, cached_data)
        
        return db_id
        
    except Exception as e:
        log_exception(logger, 'SUGGEST_REC', f"Error upserting recurring suggestion: {e}", user_id=user_id)
        return None


def dismiss_recurring_suggestion(suggestion_id, user_id=None):
    """
    Dismiss a recurring suggestion by ID.
    
    Args:
        suggestion_id: The suggestion record ID
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        True if successful
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return False
        user_id = current_user.id
    
    try:
        cached_data = _get_from_redis('recurring_suggestions', user_id)
        if cached_data is None:
            cached_data = get_recurring_suggestions(user_id, dismissed=True)
            if cached_data is None:
                return False
        
        if not isinstance(cached_data, list):
            cached_data = list(cached_data) if cached_data else []
        
        found = False
        for i, s in enumerate(cached_data):
            if int(s.get('id', 0)) == int(suggestion_id):
                cached_data[i]['dismissed'] = 1
                found = True
                break
        
        if not found:
            log_warning(logger, 'SUGGEST_REC', f"Suggestion ID {suggestion_id} not found for dismiss", user_id=user_id)
            return False
        
        _set_redis_first('recurring_suggestions', user_id, cached_data)
        log_info(logger, 'SUGGEST_REC', 'Suggestion dismissed', suggestion_id=suggestion_id, user_id=user_id)
        return True
        
    except Exception as e:
        log_exception(logger, 'SUGGEST_REC', f"Error dismissing suggestion: {e}", user_id=user_id)
        return False


def delete_recurring_suggestion(suggestion_type, category_id, user_id=None):
    """
    Delete a recurring suggestion by its unique key (suggestion_type + category_id).
    Used for self-healing when user creates a recurring entry for the category.
    
    Args:
        suggestion_type: 'recurring_income', 'recurring_expense', or 'recurring_c_expense'
        category_id: The category ID
        user_id: User ID (defaults to current_user.id)
        
    Returns:
        True if a record was deleted
    """
    if user_id is None:
        if not current_user.is_authenticated:
            return False
        user_id = current_user.id
    
    try:
        redis_client = _get_redis_client()
        cached_data = _get_from_redis('recurring_suggestions', user_id)
        if cached_data is None:
            cached_data = get_recurring_suggestions(user_id, dismissed=True)
            if cached_data is None:
                return False
        
        if not isinstance(cached_data, list):
            cached_data = list(cached_data) if cached_data else []
        
        # Find and remove the matching record
        removed_id = None
        new_data = []
        for s in cached_data:
            if s.get('suggestion_type') == suggestion_type and int(s.get('category_id', 0)) == int(category_id):
                removed_id = s.get('id')
            else:
                new_data.append(s)
        
        if removed_id is None:
            return False
        
        # Save updated list (without marking dirty — deletion handled separately)
        _set_redis_first_no_dirty('recurring_suggestions', user_id, new_data)
        
        # Mark for pending delete in MySQL
        if redis_client and removed_id and int(removed_id) > 0:
            pending_key = f"pending_deletes:recurring_suggestions:{user_id}"
            redis_client.sadd(pending_key, str(removed_id))
            redis_client.expire(pending_key, 604800)
            dirty_key = f"dirty_tables:{user_id}"
            redis_client.sadd(dirty_key, 'recurring_suggestions')
        elif redis_client:
            # Temp ID (negative) — just remove from Redis, nothing in MySQL to delete
            _set_redis_first('recurring_suggestions', user_id, new_data)
        
        log_info(logger, 'SUGGEST_REC', 'Suggestion deleted (self-healing)', suggestion_type=suggestion_type, category_id=category_id, user_id=user_id)
        return True
        
    except Exception as e:
        log_exception(logger, 'SUGGEST_REC', f"Error deleting suggestion: {e}", user_id=user_id)
        return False


# --- suggestion -> per-account category resolution (was the enrichment module) ---
# Pure category-id mapping; it does no enrichment-provider API work, and the
# surviving confirm-transaction routes depend on it.


def resolve_suggestion_for_entry(user_id: int, entry_type: str, account_id: Optional[int],
                                  canonical_category_id: Optional[int]) -> Optional[int]:
    """
    Translate a canonical category_id (expense_categories.id / income_categories.id)
    to the actual category_id that should be written to the entry table.

    For income_entries / expense_entries: the canonical id IS the entry's category_id
        -> returned as-is.

    For c_expense_entries: looks up the canonical expense_categories.name, finds the
        matching c_expense_categories row scoped to `account_id`. If no match exists
        (orphaned mirror), falls back to that account's Uncategorized category.

    Args:
        user_id: Blankee user ID.
        entry_type: 'income' | 'expense' | 'c_expense'.
        account_id: For c_expense entries, the credit_accounts.id the entry belongs to.
        canonical_category_id: expense_categories.id (outgoing) or income_categories.id
            (incoming). May be None.

    Returns:
        The category_id to write to the entry's category_id column, or None if
        nothing could be resolved.
    """
    if not canonical_category_id:
        return None

    if entry_type in ('income', 'expense'):
        return canonical_category_id

    if entry_type == 'c_expense':
        if not account_id:
            return None
        # Look up the canonical expense category's name.
        expense_cats = (_get_categories_from_redis('expense_categories', user_id)
                        or _get_categories_from_mysql('expense_categories', user_id)
                        or [])
        canonical_name = None
        for cat in expense_cats:
            try:
                if int(cat.get('id', 0)) == int(canonical_category_id):
                    canonical_name = cat.get('name')
                    break
            except (TypeError, ValueError):
                continue
        if not canonical_name:
            return None

        # Find matching c_expense category for this account.
        c_cats = (_get_categories_from_redis('c_expense_categories', user_id)
                  or _get_categories_from_mysql('c_expense_categories', user_id)
                  or [])
        canonical_name_lower = canonical_name.lower()
        for cat in c_cats:
            try:
                if (int(cat.get('account_id', 0)) == int(account_id)
                        and cat.get('name', '').lower() == canonical_name_lower):
                    return cat.get('id')
            except (TypeError, ValueError):
                continue

        # Fall back to that account's Uncategorized.
        try:
            # (moved into this module by the the bank provider removal - local call)
            return get_uncategorized_category_id(user_id, 'c_expense', account_id=account_id)
        except Exception as e:
            log_warning(logger, 'ENRICHMENT',
                f"resolve_suggestion_for_entry: Uncategorized lookup failed for user {user_id}, account {account_id}: {e}")
            return None

    return None


def _find_category_id(user_id: int, category_name: str, table_name: str, account_id: int = None) -> Optional[int]:
    """
    Find a category ID by name in the user's categories.
    
    Args:
        user_id: User ID
        category_name: Category name to match (case-insensitive)
        table_name: 'income_categories', 'expense_categories', or 'c_expense_categories'
        account_id: For c_expense_categories, only match categories belonging to this credit account
        
    Returns:
        Category ID if found, None otherwise
    """
    cat_id, _ = _resolve_category_with_flags(user_id, category_name, table_name, account_id)
    return cat_id


def _resolve_category_with_flags(user_id: int, category_name: str, table_name: str, account_id: int = None):
    """
    Same lookup as _find_category_id but also returns the full category dict so callers
    can inspect flags (is_savings, is_credit_account, etc.) for suppression decisions.

    Returns:
        (category_id, category_dict) or (None, None) if not found.
    """
    if not category_name:
        return None, None

    categories = _get_categories_from_redis(table_name, user_id)
    if not categories:
        categories = _get_categories_from_mysql(table_name, user_id)
    if not categories:
        return None, None

    category_name_lower = category_name.lower()
    for cat in categories:
        if cat.get('name', '').lower() == category_name_lower:
            if table_name == 'c_expense_categories' and account_id is not None:
                if int(cat.get('account_id', 0)) != int(account_id):
                    continue
            return cat.get('id'), cat

    return None, None


def _should_suppress_suggestion(cat: Optional[dict], account_type: Optional[str]) -> bool:
    """
    Decide whether a resolved category should be suppressed as a suggestion.

    Rules:
      - Savings categories (is_savings=1) are always suppressed: savings has its
        own dedicated flow and shouldn't be auto-suggested from bank txns.
      - Credit-payment mirror categories (is_credit_account=1, only present on
        expense_categories) are suppressed when the underlying account is CREDIT,
        because a charge to a credit card cannot be categorized as a payment-from-
        checking mirror. They remain valid suggestions for DEPOSITORY accounts.
    """
    if not cat:
        return False
    try:
        if int(cat.get('is_savings') or 0) == 1:
            return True
        if str(account_type or '').upper() == 'CREDIT' and int(cat.get('is_credit_account') or 0) == 1:
            return True
    except (TypeError, ValueError):
        return False
    return False

