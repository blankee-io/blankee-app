"""
Ntropy API Integration

This module handles direct communication with the Ntropy API for:
- Syncing user's custom categories to Ntropy
- Creating/managing Ntropy account holders
- Category suggestion lookups

Ntropy enriches bank transactions with category labels. By syncing user's 
custom categories, Ntropy will return exact category names that match the 
user's budget categories.
"""

import os
import logging
import requests
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# Ntropy API configuration
NTROPY_API_BASE = "https://api.ntropy.com/v3"
NTROPY_API_KEY = os.environ.get('NTROPY_API_KEY')


def _get_api_headers() -> Dict[str, str]:
    """Get headers for Ntropy API requests"""
    if not NTROPY_API_KEY:
        raise ValueError("NTROPY_API_KEY environment variable not set")
    return {
        "X-API-KEY": NTROPY_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }


def _get_categories_from_redis(table_name: str, user_id: int) -> Optional[List[Dict]]:
    """
    Get categories from Redis cache.
    
    Args:
        table_name: 'income_categories', 'expense_categories', or 'c_expense_categories'
        user_id: User ID
    
    Returns:
        List of category dictionaries, or None if not in Redis
    """
    import redis
    import json
    
    try:
        r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
        redis_key = f"{table_name}:v1:{user_id}"
        cached = r.get(redis_key)
        return json.loads(cached) if cached else None
    except Exception as e:
        logger.error(f"Error getting categories from {table_name} in Redis: {e}")
        return None


def _get_user_categories(user_id: int) -> Dict[str, List[str]]:
    """
    Get all categories for a user, organized for Ntropy.
    
    Returns:
        {
            "incoming": ["Wages", "Variable", ...],
            "outgoing": ["Housing", "Utilities", "Groceries", ...]
        }
    """
    incoming = []
    outgoing = []
    
    # Get income categories -> incoming
    income_cats = _get_categories_from_redis('income_categories', user_id)
    if income_cats:
        for cat in income_cats:
            name = cat.get('name')
            if name and name not in incoming:
                incoming.append(name)
    
    # Get expense categories -> outgoing
    expense_cats = _get_categories_from_redis('expense_categories', user_id)
    if expense_cats:
        for cat in expense_cats:
            name = cat.get('name')
            if name and name not in outgoing:
                outgoing.append(name)
    
    # Get credit expense categories -> outgoing (merged)
    c_expense_cats = _get_categories_from_redis('c_expense_categories', user_id)
    if c_expense_cats:
        for cat in c_expense_cats:
            name = cat.get('name')
            if name and name not in outgoing:
                outgoing.append(name)
    
    # Always include Uncategorized as catch-all
    if "Uncategorized" not in incoming:
        incoming.append("Uncategorized")
    if "Uncategorized" not in outgoing:
        outgoing.append("Uncategorized")
    
    return {
        "incoming": incoming,
        "outgoing": outgoing
    }


def sync_user_categories_to_ntropy(user_id: int) -> bool:
    """
    Sync all of a user's categories to Ntropy.
    
    This creates/updates a custom category set in Ntropy with the user's
    exact category names. When Ntropy enriches transactions, it will
    return these exact names instead of generic labels.
    
    Args:
        user_id: The Blankee user ID
        
    Returns:
        True if successful, False otherwise
    """
    try:
        if not NTROPY_API_KEY:
            logger.error("NTROPY_API_KEY not configured - skipping category sync")
            return False
        
        # Get user's categories organized for Ntropy
        categories = _get_user_categories(user_id)
        
        logger.info(f"Syncing categories to Ntropy for user {user_id}: "
                   f"{len(categories['incoming'])} incoming, {len(categories['outgoing'])} outgoing")
        
        # Create/update custom category set
        category_set_id = f"blankee_user_{user_id}"
        url = f"{NTROPY_API_BASE}/categories/{category_set_id}"
        
        response = requests.post(
            url,
            headers=_get_api_headers(),
            json=categories
        )
        
        if response.status_code in (200, 201):
            logger.info(f"Successfully synced categories to Ntropy for user {user_id}")
        else:
            logger.error(f"Failed to sync categories to Ntropy: {response.status_code} - {response.text}")
            return False
        
        # Create/update account holder with this category set
        account_holder_created = _ensure_account_holder(user_id, category_set_id)
        
        return account_holder_created
        
    except Exception as e:
        logger.error(f"Error syncing categories to Ntropy for user {user_id}: {e}", exc_info=True)
        return False


def _ensure_account_holder(user_id: int, category_set_id: str) -> bool:
    """
    Ensure an Ntropy account holder exists for this user.
    
    Args:
        user_id: The Blankee user ID
        category_set_id: The Ntropy category set ID to associate
        
    Returns:
        True if successful, False otherwise
    """
    try:
        account_holder_id = f"blankee_user_{user_id}"
        url = f"{NTROPY_API_BASE}/account_holders"
        
        payload = {
            "id": account_holder_id,
            "type": "consumer",
            "category_id": category_set_id
        }
        
        response = requests.post(
            url,
            headers=_get_api_headers(),
            json=payload
        )
        
        # 201 = created, 200 = already exists (updated)
        if response.status_code in (200, 201):
            logger.info(f"Ntropy account holder ready for user {user_id}")
            return True
        elif response.status_code == 409:
            # Account holder already exists - this is fine
            logger.info(f"Ntropy account holder already exists for user {user_id}")
            return True
        elif response.status_code == 400 and 'already exists' in response.text:
            # Ntropy returns 400 instead of 409 for "already exists"
            logger.info(f"Ntropy account holder already exists for user {user_id}")
            return True
        else:
            logger.error(f"Failed to create Ntropy account holder: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        logger.error(f"Error creating Ntropy account holder for user {user_id}: {e}", exc_info=True)
        return False


def get_ntropy_category_set(user_id: int) -> Optional[Dict[str, List[str]]]:
    """
    Retrieve the current category set for a user from Ntropy.
    
    Useful for debugging/verification.
    
    Args:
        user_id: The Blankee user ID
        
    Returns:
        Category set dict or None if not found
    """
    try:
        if not NTROPY_API_KEY:
            return None
            
        category_set_id = f"blankee_user_{user_id}"
        url = f"{NTROPY_API_BASE}/categories/{category_set_id}"
        
        response = requests.get(url, headers=_get_api_headers())
        
        if response.status_code == 200:
            return response.json()
        else:
            logger.warning(f"Could not fetch Ntropy categories for user {user_id}: {response.status_code}")
            return None
            
    except Exception as e:
        logger.error(f"Error fetching Ntropy categories: {e}", exc_info=True)
        return None


def delete_ntropy_user_data(user_id: int) -> bool:
    """
    Delete a user's data from Ntropy (for account deletion).
    
    Args:
        user_id: The Blankee user ID
        
    Returns:
        True if successful, False otherwise
    """
    try:
        if not NTROPY_API_KEY:
            return False
            
        category_set_id = f"blankee_user_{user_id}"
        
        # Delete category set
        url = f"{NTROPY_API_BASE}/categories/{category_set_id}/reset"
        response = requests.post(url, headers=_get_api_headers())
        
        if response.status_code in (200, 204, 404):
            logger.info(f"Deleted Ntropy category set for user {user_id}")
        else:
            logger.warning(f"Could not delete Ntropy category set: {response.status_code}")
        
        # Note: Account holders cannot be deleted via API per Ntropy docs
        # They will just become orphaned which is fine
        
        return True
        
    except Exception as e:
        logger.error(f"Error deleting Ntropy data for user {user_id}: {e}", exc_info=True)
        return False
