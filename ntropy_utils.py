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
import requests
from typing import List, Dict, Any, Optional
from log_config import get_logger, log_info, log_error, log_warning, log_exception

logger = get_logger(__name__)

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
        log_error(logger, 'NTROPY', f"Error getting categories from {table_name} in Redis: {e}")
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
        log_error(logger, 'NTROPY', f"Error getting categories from {table_name} in MySQL: {e}")
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
            log_error(logger, 'NTROPY', "NTROPY_API_KEY not configured - skipping category sync")
            return False
        
        # Get user's categories organized for Ntropy
        categories = _get_user_categories(user_id)
        
        log_info(logger, 'NTROPY', f"Syncing categories to Ntropy for user {user_id}: " f"{len(categories['incoming'])} incoming, {len(categories['outgoing'])} outgoing")
        
        # Create/update custom category set
        category_set_id = f"blankee_user_{user_id}"
        url = f"{NTROPY_API_BASE}/categories/{category_set_id}"
        
        response = requests.post(
            url,
            headers=_get_api_headers(),
            json=categories
        )
        
        if response.status_code in (200, 201):
            log_info(logger, 'NTROPY', f"Successfully synced categories to Ntropy for user {user_id}")
        else:
            log_error(logger, 'NTROPY', f"Failed to sync categories to Ntropy: {response.status_code} - {response.text}")
            return False
        
        # Create/update account holder with this category set
        account_holder_created = _ensure_account_holder(user_id, category_set_id)
        
        return account_holder_created
        
    except Exception as e:
        log_exception(logger, 'NTROPY', f"Error syncing categories to Ntropy for user {user_id}: {e}")
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
            log_info(logger, 'NTROPY', f"Ntropy account holder ready for user {user_id}")
            return True
        elif response.status_code == 409:
            # Account holder already exists - this is fine
            log_info(logger, 'NTROPY', f"Ntropy account holder already exists for user {user_id}")
            return True
        elif response.status_code == 400 and 'already exists' in response.text:
            # Ntropy returns 400 instead of 409 for "already exists"
            log_info(logger, 'NTROPY', f"Ntropy account holder already exists for user {user_id}")
            return True
        else:
            log_error(logger, 'NTROPY', f"Failed to create Ntropy account holder: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        log_exception(logger, 'NTROPY', f"Error creating Ntropy account holder for user {user_id}: {e}")
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
            log_warning(logger, 'NTROPY', f"Could not fetch Ntropy categories for user {user_id}: {response.status_code}")
            return None
            
    except Exception as e:
        log_exception(logger, 'NTROPY', f"Error fetching Ntropy categories: {e}")
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
            log_warning(logger, 'NTROPY', "NTROPY_API_KEY not set, skipping enrichment")
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
            log_info(logger, 'NTROPY', f"Ntropy enrichment for txn {transaction_id}: categories={result.get('categories')}")
            return result
        else:
            log_warning(logger, 'NTROPY', f"Ntropy enrichment failed for txn {transaction_id}: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        log_exception(logger, 'NTROPY', f"Error enriching transaction {transaction_id}: {e}")
        return None


def suggest_category_for_transaction(
    user_id: int,
    transaction: Dict[str, Any],
    account_type: str,  # 'DEPOSITORY' or 'CREDIT'
) -> Dict[str, Any]:
    """
    Get category suggestion for a transaction.

    Calls Ntropy to enrich the transaction with user's custom categories,
    then maps the returned category to the canonical category_id
    (expense_categories.id or income_categories.id).

    Per-account c_expense_categories.id resolution is deferred to apply time
    (auto-confirm / confirm_transaction) via resolve_suggestion_for_entry().

    Args:
        user_id: The Blankee user ID
        transaction: Quiltt transaction dict with: id, description, amount, date, transaction_type
        account_type: 'DEPOSITORY' or 'CREDIT' (used for direction detection only)

    Returns:
        Dict with:
            - suggested_category: Category name from Ntropy
            - suggested_category_id: Canonical category ID (expense_categories.id
              for outgoing, income_categories.id for incoming), or None.
              Always None for CREDIT incoming (payments to credit cards have no category).
            - category_type: 'outgoing' or 'incoming'
            - confidence: 'high' if exact match, 'medium' if Ntropy gave a name
              that didn't match any user category, 'low' otherwise.
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

        # For DEPOSITORY (debit/checking) expenses, the user-facing 'Interest Charge'
        # category is hidden — credit interest tracking lives on c_expense per credit
        # account. Remap the Ntropy suggestion to None so it falls back to 'Uncategorized'.
        if (
            account_type != 'CREDIT'
            and entry_type != 'incoming'
            and suggested_category
            and suggested_category.strip().lower() == 'interest charge'
        ):
            suggested_category = None
        
        # Extract entity/merchant data from enrichment
        # Check counterparty first, fall back to first intermediary (e.g. Venmo)
        merchant_id = None
        merchant_name = None
        merchant_website = None
        merchant_logo = None
        entities = enrichment.get('entities', {}) if enrichment else {}
        if entities:
            counterparty = entities.get('counterparty')
            if counterparty and isinstance(counterparty, dict):
                merchant_id = counterparty.get('id')
                merchant_name = counterparty.get('name')
                merchant_website = counterparty.get('website')
                merchant_logo = counterparty.get('logo')
            if not merchant_id:
                intermediaries = entities.get('intermediaries', [])
                if intermediaries and isinstance(intermediaries, list) and len(intermediaries) > 0:
                    intermediary = intermediaries[0]
                    if isinstance(intermediary, dict):
                        merchant_id = intermediary.get('id')
                        merchant_name = intermediary.get('name')
                        merchant_website = intermediary.get('website')
                        merchant_logo = intermediary.get('logo')
        
        # Resolve to CANONICAL category id (expense_categories / income_categories).
        # Per-account c_expense_categories.id is resolved at apply time, not here.
        suggested_category_id = None
        resolved_cat = None
        category_type = entry_type  # 'outgoing' or 'incoming'

        if account_type == 'CREDIT' and entry_type == 'incoming':
            # Payment received on a credit card -- no category, no memory.
            suggested_category_id = None
        elif suggested_category:
            if entry_type == 'incoming':
                suggested_category_id, resolved_cat = _resolve_category_with_flags(
                    user_id, suggested_category, 'income_categories'
                )
            else:  # 'outgoing'
                suggested_category_id, resolved_cat = _resolve_category_with_flags(
                    user_id, suggested_category, 'expense_categories'
                )

        # Suppress savings + (CREDIT-only) credit-payment mirror suggestions.
        # Falls back to no category match → caller surfaces 'Uncategorized'.
        if _should_suppress_suggestion(resolved_cat, account_type):
            log_info(
                logger, 'NTROPY',
                f"Suppressed suggestion '{suggested_category}' for user {user_id} "
                f"(account_type={account_type}, is_savings={resolved_cat.get('is_savings')}, "
                f"is_credit_account={resolved_cat.get('is_credit_account')})"
            )
            suggested_category = None
            suggested_category_id = None
            resolved_cat = None

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
            'confidence': confidence,
            'ntropy_merchant_id': merchant_id,
            'ntropy_merchant_name': merchant_name,
            'ntropy_website': merchant_website,
            'ntropy_logo': merchant_logo,
        }
        
    except Exception as e:
        log_exception(logger, 'NTROPY', f"Error suggesting category for transaction: {e}")
        return {
            'suggested_category': 'Uncategorized',
            'suggested_category_id': None,
            'category_type': 'outgoing',
            'confidence': 'low'
        }


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
            from quiltt_redis import get_uncategorized_category_id
            return get_uncategorized_category_id(user_id, 'c_expense', account_id=account_id)
        except Exception as e:
            log_warning(logger, 'NTROPY',
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


def get_recurring_groups(profile_id: str) -> Optional[List[Dict[str, Any]]]:
    """
    Fetch recurring transaction groups from Ntropy for a Quiltt profile.
    
    Quiltt submits all transactions under the profile_id as account_holder_id.
    This endpoint analyzes those transactions and returns detected recurring patterns.
    
    Args:
        profile_id: The Quiltt profile ID (e.g., 'p_132UrS5UDu66vSa2QYUvpE')
        
    Returns:
        List of recurring group dicts, each containing:
            - id: Group UUID
            - counterparty: {id, name, website, logo, ...}
            - periodicity: 'monthly', 'bi-weekly', 'weekly', 'other'
            - periodicity_in_days: float
            - average_amount: float
            - start_date, end_date: date strings
            - transaction_ids: list of Quiltt transaction IDs
            - entry_type: 'incoming' or 'outgoing'
        Or None on error.
    """
    try:
        if not NTROPY_API_KEY:
            log_warning(logger, 'NTROPY', "NTROPY_API_KEY not set, skipping recurring groups")
            return None
        
        url = f"{NTROPY_API_BASE}/account_holders/{profile_id}/recurring_groups"
        response = requests.post(url, headers=_get_api_headers())
        
        if response.status_code == 200:
            groups = response.json()
            log_info(logger, 'NTROPY', f"Ntropy recurring groups for {profile_id}: {len(groups)} groups found")
            return groups
        else:
            log_warning(logger, 'NTROPY', f"Ntropy recurring_groups failed for {profile_id}: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        log_exception(logger, 'NTROPY', f"Error fetching recurring groups for {profile_id}: {e}")
        return None


def build_recurrence_map(recurring_groups: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Build a mapping from transaction_id → recurrence data from recurring groups.
    
    Args:
        recurring_groups: List of recurring group dicts from get_recurring_groups()
        
    Returns:
        Dict mapping transaction_id to recurrence fields:
            ntropy_recurrence, ntropy_recurrence_group_id, ntropy_periodicity,
            ntropy_periodicity_days, ntropy_avg_amount, ntropy_first_payment_date,
            ntropy_latest_payment_date, ntropy_merchant_id, ntropy_logo, ntropy_website
    """
    txn_map = {}
    for group in recurring_groups:
        group_id = group.get('id')
        counterparty = group.get('counterparty') or {}
        
        recurrence_data = {
            'ntropy_recurrence': 'recurring',
            'ntropy_recurrence_group_id': group_id,
            'ntropy_periodicity': group.get('periodicity'),
            'ntropy_periodicity_days': group.get('periodicity_in_days'),
            'ntropy_avg_amount': group.get('average_amount'),
            'ntropy_first_payment_date': group.get('start_date'),
            'ntropy_latest_payment_date': group.get('end_date'),
            'ntropy_merchant_id': counterparty.get('id'),
            'ntropy_logo': counterparty.get('logo'),
            'ntropy_website': counterparty.get('website'),
        }
        
        for txn_id in group.get('transaction_ids', []):
            txn_map[txn_id] = recurrence_data
    
    return txn_map


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
            log_info(logger, 'NTROPY', f"Deleted Ntropy category set for user {user_id}")
        else:
            log_warning(logger, 'NTROPY', f"Could not delete Ntropy category set: {response.status_code}")
        
        # Note: Account holders cannot be deleted via API per Ntropy docs
        # They will just become orphaned which is fine
        
        return True
        
    except Exception as e:
        log_exception(logger, 'NTROPY', f"Error deleting Ntropy data for user {user_id}: {e}")
        return False
