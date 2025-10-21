"""
Redis Cache Helper Utilities

Convenience functions for working with Redis-cached data in route handlers.
"""

import logging
from typing import Optional, List, Dict, Any, Callable
from flask_login import current_user
from redis_manager import get_cached_data, set_cached_data, is_user_hydrated
from db_connections import get_db_pool

logger = logging.getLogger(__name__)


def get_user_data(table: str, 
                  fallback_query: Optional[Callable] = None,
                  force_mysql: bool = False) -> List[Dict[str, Any]]:
    """
    Get user data from Redis cache or MySQL fallback.
    
    This is the main function to use in route handlers for fetching data.
    It automatically tries Redis first, then falls back to MySQL if needed.
    
    Args:
        table: Table name (e.g., 'income_entries')
        fallback_query: Optional function that queries MySQL
                       If None, uses default query pattern
        force_mysql: If True, skip Redis and query MySQL directly
        
    Returns:
        List of dictionaries (rows)
        
    Example:
        # Simple usage with default query
        income = get_user_data('income_entries')
        
        # Custom query
        def custom_query():
            with get_db_pool().get_cursor(dictionary=True) as cursor:
                cursor.execute(
                    "SELECT * FROM income_entries WHERE user_id = %s AND date > %s",
                    (current_user.id, start_date)
                )
                return cursor.fetchall()
        
        income = get_user_data('income_entries', custom_query)
    """
    
    if not current_user.is_authenticated:
        return []
    
    user_id = current_user.id
    
    # Try Redis first (unless forced to MySQL)
    if not force_mysql and is_user_hydrated(user_id):
        cached = get_cached_data(table, user_id)
        if cached is not None:
            logger.debug(f"Cache HIT for {table}, user {user_id}: {len(cached)} rows")
            return cached
    
    # Cache miss or forced MySQL - query database
    logger.debug(f"Cache MISS for {table}, user {user_id}, querying MySQL")
    
    if fallback_query:
        data = fallback_query()
    else:
        # Default query pattern
        data = _default_table_query(table, user_id)
    
    # Cache the result if user is hydrated (helps with partial data)
    if is_user_hydrated(user_id):
        set_cached_data(table, user_id, data)
    
    return data


def _default_table_query(table: str, user_id: int) -> List[Dict[str, Any]]:
    """
    Default query pattern for user tables.
    
    Args:
        table: Table name
        user_id: User ID
        
    Returns:
        List of dictionaries
    """
    with get_db_pool().get_cursor(dictionary=True) as cursor:
        # Most tables have user_id column
        query = f"SELECT * FROM {table} WHERE user_id = %s"
        
        # Special handling for credit account-related tables
        if table.startswith('c_') and table not in ('credit_accounts',):
            query = f"""
                SELECT t.* FROM {table} t
                INNER JOIN credit_accounts ca ON t.account_id = ca.id
                WHERE ca.user_id = %s
            """
        
        cursor.execute(query, (user_id,))
        return cursor.fetchall()


def get_bud_items(bud_id: int, force_mysql: bool = False) -> List[Dict[str, Any]]:
    """
    Get bud items (special case since they're keyed by bud_id, not user_id).
    
    Args:
        bud_id: Bud ID
        force_mysql: If True, skip Redis and query MySQL
        
    Returns:
        List of dictionaries
    """
    if not current_user.is_authenticated:
        return []
    
    user_id = current_user.id
    
    # Try Redis first
    if not force_mysql and is_user_hydrated(user_id):
        from redis_manager import _redis_client, REDIS_KEY_VERSION
        if _redis_client:
            redis_key = f"bud_items:{REDIS_KEY_VERSION}:{bud_id}"
            import json
            cached = _redis_client.get(redis_key)
            if cached:
                return json.loads(cached)
    
    # Query MySQL
    with get_db_pool().get_cursor(dictionary=True) as cursor:
        cursor.execute("SELECT * FROM bud_items WHERE bud_id = %s", (bud_id,))
        return cursor.fetchall()


def get_aggregated_totals(view: str = 'weekly', 
                          start_date=None, 
                          end_date=None) -> List[Dict[str, Any]]:
    """
    Get aggregated totals/remainders for different time views.
    
    Args:
        view: 'weekly', 'daily', or 'monthly'
        start_date: Optional start date filter
        end_date: Optional end date filter
        
    Returns:
        List of total/remainder records
    """
    if not current_user.is_authenticated:
        return []
    
    user_id = current_user.id
    
    # Map view to table
    table_map = {
        'weekly': 'totals_remainders',
        'daily': 'totals_remainders_d',
        'monthly': 'totals_remainders_m'
    }
    
    table = table_map.get(view, 'totals_remainders')
    
    # If no date filters, use standard get_user_data
    if not start_date and not end_date:
        return get_user_data(table)
    
    # Custom query with date filters
    def query_with_dates():
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            query = f"SELECT * FROM {table} WHERE user_id = %s"
            params = [user_id]
            
            if start_date:
                query += " AND date >= %s"
                params.append(start_date)
            
            if end_date:
                query += " AND date <= %s"
                params.append(end_date)
            
            query += " ORDER BY date"
            
            cursor.execute(query, tuple(params))
            return cursor.fetchall()
    
    return get_user_data(table, query_with_dates)


# Example usage patterns for common queries

def get_income_categories(include_hidden: bool = False) -> List[Dict[str, Any]]:
    """Get income categories for current user"""
    if include_hidden:
        return get_user_data('income_categories')
    else:
        def query_visible():
            with get_db_pool().get_cursor(dictionary=True) as cursor:
                cursor.execute(
                    "SELECT * FROM income_categories WHERE user_id = %s AND hidden = 0 ORDER BY display_order",
                    (current_user.id,)
                )
                return cursor.fetchall()
        return get_user_data('income_categories', query_visible)


def get_expense_categories(include_hidden: bool = False) -> List[Dict[str, Any]]:
    """Get expense categories for current user"""
    if include_hidden:
        return get_user_data('expense_categories')
    else:
        def query_visible():
            with get_db_pool().get_cursor(dictionary=True) as cursor:
                cursor.execute(
                    "SELECT * FROM expense_categories WHERE user_id = %s AND hidden = 0 ORDER BY display_order",
                    (current_user.id,)
                )
                return cursor.fetchall()
        return get_user_data('expense_categories', query_visible)


def get_recent_entries(entry_type: str = 'income', limit: int = 100) -> List[Dict[str, Any]]:
    """
    Get recent income or expense entries.
    
    Args:
        entry_type: 'income' or 'expense'
        limit: Maximum number of entries
    """
    table = f"{entry_type}_entries"
    
    def query_recent():
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            cursor.execute(
                f"SELECT * FROM {table} WHERE category_id IN "
                f"(SELECT id FROM {entry_type}_categories WHERE user_id = %s) "
                f"ORDER BY date DESC LIMIT %s",
                (current_user.id, limit)
            )
            return cursor.fetchall()
    
    return get_user_data(table, query_recent)


def get_user_profile() -> Optional[Dict[str, Any]]:
    """Get current user's profile data"""
    if not current_user.is_authenticated:
        return None
    
    user_id = current_user.id
    
    # Try Redis first
    if is_user_hydrated(user_id):
        from redis_manager import _redis_client, REDIS_KEY_VERSION
        if _redis_client:
            import json
            redis_key = f"users:{REDIS_KEY_VERSION}:{user_id}"
            cached = _redis_client.get(redis_key)
            if cached:
                return json.loads(cached)
    
    # Query MySQL
    with get_db_pool().get_cursor(dictionary=True) as cursor:
        cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        return cursor.fetchone()


def invalidate_and_refresh_cache():
    """
    Invalidate current user's cache and trigger fresh hydration.
    Useful after bulk updates or data imports.
    """
    from redis_manager import invalidate_user_cache, track_user_activity
    
    if current_user.is_authenticated:
        user_id = current_user.id
        invalidate_user_cache(user_id)
        # Trigger immediate re-hydration
        track_user_activity(user_id)
        logger.info(f"Cache invalidated and refresh triggered for user {user_id}")
