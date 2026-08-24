"""
Bank sync support: provider-neutral logic that outlived the vendor removal.

What lives here is the standalone part - pure functions over transaction lists
with no dependency on the Flask app. The sync ORCHESTRATION deliberately stayed
in app.py: it calls update_weekly_totals, update_monthly_totals,
update_daily_ca_totals, update_daily_savings_for_savings_category and several
other app-level routines, so moving it here would mean bank_sync importing app
while app imports bank_sync. Breaking that cycle is a bigger refactor than the
provider removal was, and is not required to make the app provider-free.

Transactions handled here use the normalized shape documented in
providers/base.py, not any vendor's payload.
"""

from typing import Any, Dict, List, Optional

from log_config import get_logger, log_info, log_error, log_warning, log_exception

logger = get_logger(__name__)


def is_compatible_account_type(account_type: str) -> bool:
    """
    True for account types Blankee can model (checking/savings/credit).

    Kept because any replacement aggregator needs the same filter - most return
    loan, investment, and mortgage accounts that Blankee has nowhere to put.
    """
    if not account_type:
        return False
    return str(account_type).upper() in (
        'CHECKING', 'SAVINGS', 'CREDIT_CARD', 'CREDITCARD', 'CREDIT'
    )



def map_transaction_to_entry(transaction: Dict, user_id: int, category_mapping: Dict[str, int]) -> Dict:
    """
    Map a provider transaction to your app's entry format
    
    Args:
        transaction: provider transaction dict
        user_id: Your app's user ID
        category_mapping: Dict mapping provider categories to your category IDs
        
    Returns:
        Entry dict ready for your database
    """
    amount = abs(float(transaction.get('amount', 0)))
    is_expense = float(transaction.get('amount', 0)) < 0
    
    # Map provider category to your app's category
    provider_category = transaction.get('category', 'Other')
    category_id = category_mapping.get(provider_category, category_mapping.get('Other'))
    
    entry = {
        'user_id': user_id,
        'category_id': category_id,
        'date': transaction.get('date'),
        'amount': amount,
        'description': transaction.get('description', ''),
        'merchant_name': transaction.get('merchantName', ''),
        'provider_txn_id': transaction.get('id'),
        'pending': transaction.get('pending', False),
        'is_expense': is_expense
    }
    
    return entry



def get_default_category_mapping() -> Dict[str, str]:
    """
    Get default mapping of provider transaction categories to budget categories
    This can be customized per user in your database
    """
    return {
        # provider category -> Your app category name
        'Food and Drink': 'Groceries',
        'Restaurants': 'Dining Out',
        'Shopping': 'Shopping',
        'Gas': 'Transportation',
        'Transportation': 'Transportation',
        'Bills and Utilities': 'Utilities',
        'Entertainment': 'Entertainment',
        'Travel': 'Travel',
        'Healthcare': 'Healthcare',
        'Personal Care': 'Personal Care',
        'Education': 'Education',
        'Transfer': 'Transfer',
        'Income': 'Income',
        'Other': 'Uncategorized'
    }



def build_recurrence_map(recurring_groups: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Build a mapping from transaction_id -> recurrence data from recurring groups.
    
    Args:
        recurring_groups: List of recurring group dicts from get_recurring_groups()
        
    Returns:
        Dict mapping transaction_id to recurrence fields:
            enrichment_recurrence, enrichment_recurrence_group_id, enrichment_periodicity,
            enrichment_periodicity_days, enrichment_avg_amount, enrichment_first_payment_date,
            enrichment_last_payment_date, enrichment_merchant_id
    """
    txn_map = {}
    for group in recurring_groups:
        group_id = group.get('id')
        counterparty = group.get('counterparty') or {}
        
        recurrence_data = {
            'enrichment_recurrence': 'recurring',
            'enrichment_recurrence_group_id': group_id,
            'enrichment_periodicity': group.get('periodicity'),
            'enrichment_periodicity_days': group.get('periodicity_in_days'),
            'enrichment_avg_amount': group.get('average_amount'),
            'enrichment_first_payment_date': group.get('start_date'),
            'enrichment_last_payment_date': group.get('end_date'),
            'enrichment_merchant_id': counterparty.get('id'),
        }
        
        for txn_id in group.get('transaction_ids', []):
            txn_map[txn_id] = recurrence_data
    
    return txn_map



def analyze_transactions_for_categories(transactions: List[Dict]) -> Dict:
    """
    Analyze transactions with enrichment data to generate category recommendations
    
    Args:
        transactions: List of transactions with enrichment data
        
    Returns:
        Dict with 'income' and 'expense' category recommendations
    """
    from datetime import datetime, timedelta
    from statistics import median
    from collections import defaultdict
    
    log_info(logger, 'BANK', f"Analyzing {len(transactions)} transactions for category recommendations")
    
    if not transactions or len(transactions) == 0:
        log_warning(logger, 'BANK', "No transactions to analyze - returning fallback")
        return {'fallback': True, 'income': [], 'expense': []}
    
    # Group transactions by category
    income_categories = defaultdict(list)
    expense_categories = defaultdict(list)
    
    for txn in transactions:
        # Extract category from the provider's enrichment payload
        remote_data = txn.get('remoteData', {})
        enrichment_data = remote_data.get('enrichment', {})
        enrichment = enrichment_data.get('enrichment', {})
        response = enrichment.get('response', {})
        
        categories = response.get('categories', {})
        general_category = categories.get('general', '') if isinstance(categories, dict) else ''
        
        entities = response.get('entities', {})
        counterparty = entities.get('counterparty', {}) if isinstance(entities, dict) else {}
        merchant = counterparty.get('name', '') if isinstance(counterparty, dict) else ''
        
        # Use general category label, or counterparty name, or skip
        category_name = None
        if general_category:
            category_name = general_category
        elif merchant:
            category_name = merchant
        
        if not category_name:
            log_info(logger, 'BANK', f"Skipping transaction - no category or merchant: {txn.get('description')}")
            continue
        
        # Filter out internal banking operations that aren't useful budget categories
        skip_categories = {
            'inter-account transfer', 'intra-account transfer', 'internal transfer',
            'bank adjustment', 'banking fee', 'bank fee', 'account fee',
            'bank withdrawal', 'atm withdrawal', 'cash withdrawal',
            'overdraft', 'overdraft fee', 'account maintenance',
            'interest charge', 'interest earned',
            'dividend', 'capital gains', 'wire transfer fee'
        }
        
        if category_name.lower() in skip_categories:
            log_info(logger, 'BANK', f"Skipping internal banking operation: {category_name}")
            continue
        
        # Get transaction details
        amount = abs(float(txn.get('amount', 0)))
        date_str = txn.get('date')
        entry_type = txn.get('entryType', '').upper()
        
        # Determine if income or expense based on entryType
        # CREDIT = inflow (income), DEBIT = outflow (expense)
        is_income = (entry_type == 'CREDIT')
        
        if amount > 0 and date_str:
            transaction_data = {
                'amount': amount,
                'date': datetime.strptime(date_str, '%Y-%m-%d'),
                'merchant': merchant or ''
            }
            
            if is_income:
                income_categories[category_name].append(transaction_data)
            else:
                expense_categories[category_name].append(transaction_data)
    
    log_info(logger, 'BANK', f"Found {len(income_categories)} income categories, {len(expense_categories)} expense categories")
    
    # Analyze each category for recurring patterns
    income_recommendations = []
    expense_recommendations = []
    
    for category_name, txns in income_categories.items():
        rec = _analyze_category_pattern(category_name, txns, is_income=True)
        if rec:
            income_recommendations.append(rec)
    
    for category_name, txns in expense_categories.items():
        rec = _analyze_category_pattern(category_name, txns, is_income=False)
        if rec:
            expense_recommendations.append(rec)
    
    # Sort by importance (recurring first, then by amount)
    income_recommendations.sort(key=lambda x: (not x.get('is_recurring', False), -x.get('amount', 0)))
    expense_recommendations.sort(key=lambda x: (not x.get('is_recurring', False), -x.get('amount', 0)))
    
    # Limit to top categories
    income_recommendations = income_recommendations[:10]
    expense_recommendations = expense_recommendations[:15]
    
    log_info(logger, 'BANK', f"Generated {len(income_recommendations)} income recommendations, {len(expense_recommendations)} expense recommendations")
    
    return {
        'fallback': False,
        'income': income_recommendations,
        'expense': expense_recommendations
    }



def _analyze_category_pattern(category_name: str, transactions: List[Dict], is_income: bool) -> Optional[Dict]:
    """
    Analyze a single category's transactions to detect recurring patterns
    
    Args:
        category_name: Name of the category
        transactions: List of transactions in this category
        is_income: Whether this is an income category
        
    Returns:
        Category recommendation dict or None
    """
    from datetime import datetime, timedelta
    from statistics import median
    
    if len(transactions) == 0:
        return None
    
    # Sort by date
    transactions.sort(key=lambda x: x['date'])
    
    amounts = [t['amount'] for t in transactions]
    median_amount = median(amounts)
    
    # Detect recurring pattern (need at least 3 occurrences)
    is_recurring = False
    cadence_unit = None
    cadence_interval = None
    weekdays = None
    monthly_days = None
    
    if len(transactions) >= 3:
        # Calculate intervals between transactions (in days)
        intervals = []
        for i in range(1, len(transactions)):
            diff = (transactions[i]['date'] - transactions[i-1]['date']).days
            if diff > 0:
                intervals.append(diff)
        
        if intervals:
            median_interval = median(intervals)
            
            # Detect cadence type
            if 6 <= median_interval <= 8:  # Weekly (allow some variance)
                is_recurring = True
                cadence_unit = 'weeks'
                cadence_interval = 1
                # Get weekday from most recent transaction
                weekday = transactions[-1]['date'].strftime('%A')
                weekdays = weekday
                
            elif 13 <= median_interval <= 15:  # Biweekly
                is_recurring = True
                cadence_unit = 'weeks'
                cadence_interval = 2
                weekday = transactions[-1]['date'].strftime('%A')
                weekdays = weekday
                
            elif 28 <= median_interval <= 33:  # Monthly
                is_recurring = True
                cadence_unit = 'months'
                cadence_interval = 1
                # Get common days of month
                days_of_month = list(set([t['date'].day for t in transactions[-3:]]))
                monthly_days = ','.join(str(d) for d in sorted(days_of_month))
                
            elif 60 <= median_interval <= 65:  # Bimonthly
                is_recurring = True
                cadence_unit = 'months'
                cadence_interval = 2
                days_of_month = list(set([t['date'].day for t in transactions[-3:]]))
                monthly_days = ','.join(str(d) for d in sorted(days_of_month))
                
            elif 350 <= median_interval <= 370:  # Yearly
                is_recurring = True
                cadence_unit = 'years'
                cadence_interval = 1
    
    recommendation = {
        'name': category_name,
        'is_recurring': is_recurring,
        'amount': round(median_amount, 2),
        'transaction_count': len(transactions)
    }
    
    if is_recurring:
        recommendation['cadence_unit'] = cadence_unit
        recommendation['cadence_interval'] = cadence_interval
        if weekdays:
            recommendation['weekdays'] = weekdays
        if monthly_days:
            recommendation['monthly_days'] = monthly_days
    
    return recommendation
