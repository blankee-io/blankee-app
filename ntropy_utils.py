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


def _get_categories_from_mysql(table_name: str, user_id: int) -> Optional[List[Dict]]:
    """
    Fallback: get categories directly from MySQL when Redis doesn't have them.
    Used by cron scripts (nightly_sync) when users aren't hydrated in Redis.
    
    Args:
        table_name: 'income_categories', 'expense_categories', or 'c_expense_categories'
        user_id: User ID
    
    Returns:
        List of category dicts with at least 'id' and 'name', or None on error
    """
    try:
        from dotenv import load_dotenv
        load_dotenv('/var/www/budget_env/.env')
        import mysql.connector
        
        conn = mysql.connector.connect(
            host=os.environ.get('DB_HOST', 'localhost'),
            user=os.environ.get('DB_USER'),
            password=os.environ.get('DB_PASSWORD'),
            database=os.environ.get('DB_NAME', 'budget')
        )
        cursor = conn.cursor(dictionary=True)
        
        if table_name == 'c_expense_categories':
            cursor.execute("""
                SELECT cec.id, cec.name FROM c_expense_categories cec
                JOIN credit_accounts ca ON cec.account_id = ca.id
                WHERE ca.user_id = %s
            """, (user_id,))
        else:
            cursor.execute(f"SELECT id, name FROM {table_name} WHERE user_id = %s", (user_id,))
        
        categories = cursor.fetchall()
        cursor.close()
        conn.close()
        return categories if categories else None
    except Exception as e:
        logger.error(f"Error getting categories from {table_name} in MySQL: {e}")
        return None


def _get_user_categories(user_id: int) -> Dict[str, List[str]]:
    """
    Get all categories for a user, organized for Ntropy.
    
    Note: "Uncategorized" and "Starting Balance" are NOT sent to Ntropy.
    This ensures Ntropy only suggests these as fallbacks.
    
    Returns:
        {
            "incoming": ["Wages", "Variable", ...],
            "outgoing": ["Housing", "Utilities", "Groceries", ...]
        }
    """
    incoming = []
    outgoing = []
    
    # Categories to exclude from Ntropy
    excluded_names = {'uncategorized', 'starting balance'}
    
    # Get income categories -> incoming (exclude Uncategorized, Starting Balance)
    income_cats = _get_categories_from_redis('income_categories', user_id)
    if income_cats:
        for cat in income_cats:
            name = cat.get('name')
            if name and name not in incoming and name.lower() not in excluded_names:
                incoming.append(name)
    
    # Get expense categories -> outgoing (exclude Uncategorized, Starting Balance)
    expense_cats = _get_categories_from_redis('expense_categories', user_id)
    if expense_cats:
        for cat in expense_cats:
            name = cat.get('name')
            if name and name not in outgoing and name.lower() not in excluded_names:
                outgoing.append(name)
    
    # Get credit expense categories -> outgoing (merged, exclude Uncategorized, Starting Balance)
    c_expense_cats = _get_categories_from_redis('c_expense_categories', user_id)
    if c_expense_cats:
        for cat in c_expense_cats:
            name = cat.get('name')
            if name and name not in outgoing and name.lower() not in excluded_names:
                outgoing.append(name)
    
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


def enrich_transaction_with_custom_categories(
    user_id: int,
    transaction_id: str,
    description: str,
    amount: float,
    date: str,
    entry_type: str,  # 'incoming' or 'outgoing'
    currency: str = 'USD'
) -> Optional[Dict[str, Any]]:
    """
    Enrich a transaction using Ntropy with the user's custom categories.
    
    This calls Ntropy directly (not through Quiltt) to get enrichment
    using our custom category set for this user.
    
    Args:
        user_id: The Blankee user ID (used to look up account_holder_id)
        transaction_id: Unique transaction identifier
        description: Transaction description from bank
        amount: Transaction amount (absolute value)
        date: Transaction date in YYYY-MM-DD format
        entry_type: 'incoming' or 'outgoing'
        currency: Currency code (default: USD)
        
    Returns:
        Enrichment response with categories using custom labels, or None on error
        Response includes: entities, categories, location
    """
    try:
        if not NTROPY_API_KEY:
            logger.warning("NTROPY_API_KEY not set, skipping enrichment")
            return None
            
        account_holder_id = f"blankee_user_{user_id}"
        url = f"{NTROPY_API_BASE}/transactions"
        
        payload = {
            "id": transaction_id,
            "description": description,
            "date": date,
            "amount": abs(amount),  # Ntropy expects positive amount
            "entry_type": entry_type,
            "currency": currency,
            "account_holder_id": account_holder_id
        }
        
        response = requests.post(url, headers=_get_api_headers(), json=payload)
        
        if response.status_code == 200:
            result = response.json()
            logger.info(f"Ntropy enrichment for txn {transaction_id}: categories={result.get('categories')}")
            return result
        else:
            logger.warning(f"Ntropy enrichment failed for txn {transaction_id}: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        logger.error(f"Error enriching transaction {transaction_id}: {e}", exc_info=True)
        return None


def suggest_category_for_transaction(
    user_id: int,
    transaction: Dict[str, Any],
    account_type: str  # 'DEPOSITORY' or 'CREDIT'
) -> Dict[str, Any]:
    """
    Get category suggestion for a transaction.
    
    Calls Ntropy to enrich the transaction with user's custom categories,
    then maps the returned category to the appropriate category_id.
    
    Args:
        user_id: The Blankee user ID
        transaction: Quiltt transaction dict with: id, description, amount, date, transaction_type
        account_type: 'DEPOSITORY' or 'CREDIT' (determines which category table to use)
        
    Returns:
        Dict with:
            - suggested_category: Category name from Ntropy
            - suggested_category_id: Matching category ID, or None if no match
            - category_type: 'income', 'expense', 'c_expense', or 'c_payment'
            - confidence: 'high' if exact match, 'low' if Uncategorized fallback
    """
    try:
        amount = float(transaction.get('amount', 0))
        
        # Use transaction_type field if available (most reliable)
        # Otherwise fall back to amount-based inference
        txn_type = transaction.get('transaction_type', '').lower()
        
        if txn_type == 'expense':
            entry_type = 'outgoing'
        elif txn_type == 'income':
            entry_type = 'incoming'
        else:
            # Fallback: infer from account type and amount
            if account_type == 'CREDIT':
                entry_type = 'outgoing' if amount > 0 else 'incoming'
            else:
                entry_type = 'incoming' if amount > 0 else 'outgoing'
        
        # Call Ntropy for enrichment with custom categories
        enrichment = enrich_transaction_with_custom_categories(
            user_id=user_id,
            transaction_id=transaction.get('id', transaction.get('transaction_id', '')),
            description=transaction.get('description', ''),
            amount=amount,
            date=transaction.get('date', ''),
            entry_type=entry_type
        )
        
        # Extract suggested category from enrichment
        suggested_category = None
        if enrichment and enrichment.get('categories'):
            suggested_category = enrichment['categories'].get('general')
        
        # Determine which table to look up based on entry_type (already calculated correctly)
        if account_type == 'CREDIT':
            if entry_type == 'incoming':
                # Payment to credit card
                category_type = 'c_payment'
                suggested_category_id = None  # Payments don't have categories
            else:
                # Charge to credit card (outgoing/expense)
                category_type = 'c_expense'
                suggested_category_id = _find_category_id(
                    user_id, suggested_category, 'c_expense_categories'
                ) if suggested_category else None
        else:  # DEPOSITORY
            if entry_type == 'incoming':
                category_type = 'income'
                suggested_category_id = _find_category_id(
                    user_id, suggested_category, 'income_categories'
                ) if suggested_category else None
            else:
                category_type = 'expense'
                suggested_category_id = _find_category_id(
                    user_id, suggested_category, 'expense_categories'
                ) if suggested_category else None
        
        # Determine confidence
        if suggested_category_id:
            confidence = 'high'
        elif suggested_category:
            confidence = 'medium'  # Ntropy gave category but no match in user's list
        else:
            confidence = 'low'
        
        return {
            'suggested_category': suggested_category or 'Uncategorized',
            'suggested_category_id': suggested_category_id,
            'category_type': category_type,
            'confidence': confidence
        }
        
    except Exception as e:
        logger.error(f"Error suggesting category for transaction: {e}", exc_info=True)
        return {
            'suggested_category': 'Uncategorized',
            'suggested_category_id': None,
            'category_type': 'expense' if account_type == 'DEPOSITORY' else 'c_expense',
            'confidence': 'low'
        }


def _find_category_id(user_id: int, category_name: str, table_name: str) -> Optional[int]:
    """
    Find a category ID by name in the user's categories.
    
    Args:
        user_id: User ID
        category_name: Category name to match (case-insensitive)
        table_name: 'income_categories', 'expense_categories', or 'c_expense_categories'
        
    Returns:
        Category ID if found, None otherwise
    """
    if not category_name:
        return None
        
    categories = _get_categories_from_redis(table_name, user_id)
    
    # MySQL fallback if Redis doesn't have the data (e.g. cron scripts at midnight)
    if not categories:
        categories = _get_categories_from_mysql(table_name, user_id)
    
    if not categories:
        return None
    
    # Case-insensitive match
    category_name_lower = category_name.lower()
    for cat in categories:
        if cat.get('name', '').lower() == category_name_lower:
            return cat.get('id')
    
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
