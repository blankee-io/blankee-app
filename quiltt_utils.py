"""
Quiltt.io API Integration Utilities
Handles authentication, API calls, and data transformation for Quiltt
"""

import os
import requests
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from log_config import get_logger, log_info, log_error, log_warning, log_exception

# Set up logging
logger = get_logger(__name__)

class QuilttClient:
    """Client for interacting with Quiltt API"""
    
    def __init__(self):
        # Load from environment variables
        self.api_key = os.getenv('QUILTT_API_KEY', '')
        self.environment_id = os.getenv('QUILTT_ENVIRONMENT_ID', '')
        self.base_url = 'https://api.quiltt.io/v1'
        self.graphql_url = f'{self.base_url}/graphql'
        
        # Validate credentials
        if not self.api_key:
            log_warning(logger, 'QUILTT', "QUILTT_API_KEY not set in environment variables")
        if not self.environment_id:
            log_warning(logger, 'QUILTT', "QUILTT_ENVIRONMENT_ID not set in environment variables")
        
    def _get_headers(self, session_token: Optional[str] = None) -> Dict[str, str]:
        """Get headers for API requests"""
        headers = {
            'Content-Type': 'application/json',
        }
        
        if session_token:
            # User-specific requests use session token
            headers['Authorization'] = f'Bearer {session_token}'
        else:
            # Platform-level requests use API key
            headers['Authorization'] = f'Bearer {self.api_key}'
            
        return headers
    
    def create_session_token(self, user_id: int, metadata: Optional[Dict] = None) -> Optional[Dict]:
        """
        Create a session token for a user to authenticate with Quiltt Connector
        
        Args:
            user_id: Your application's user ID
            metadata: Optional metadata to attach to the Quiltt profile
            
        Returns:
            Dict with 'token' and 'profileId' or None on error
        """
        # Check if credentials are configured
        if not self.api_key or not self.environment_id:
            log_error(logger, 'QUILTT', "Quiltt credentials not configured. Set QUILTT_API_KEY and QUILTT_ENVIRONMENT_ID")
            return None
            
        try:
            # Quiltt Auth API endpoint for issuing session tokens
            url = 'https://auth.quiltt.io/v1/users/sessions'
            
            # When creating a new profile, omit userId
            # Quiltt will generate a Profile ID which we'll store
            payload = {}
            
            if metadata:
                payload['metadata'] = metadata
            
            log_info(logger, 'QUILTT', f"Creating new Quiltt profile session token for app user {user_id}")
            log_info(logger, 'QUILTT', f"Request payload: {payload}")
            
            response = requests.post(
                url,
                json=payload,
                headers=self._get_headers(),
                timeout=10
            )
            
            log_info(logger, 'QUILTT', f"Response status: {response.status_code}")
            log_info(logger, 'QUILTT', f"Response body: {response.text[:500]}")
            
            response.raise_for_status()
            
            data = response.json()
            log_info(logger, 'QUILTT', f"Created Quiltt session token - Profile ID: {data.get('userId')}")
            
            return {
                'token': data.get('token'),
                'profileId': data.get('userId'),  # Quiltt returns userId as the Profile ID
                'expiresAt': data.get('expiresAt')
            }
            
        except requests.exceptions.RequestException as e:
            log_error(logger, 'QUILTT', f"Error creating Quiltt session token: {e}")
            if hasattr(e, 'response') and e.response is not None:
                log_error(logger, 'QUILTT', f"Response status: {e.response.status_code}")
                log_error(logger, 'QUILTT', f"Response body: {e.response.text}")
            return None
    
    def refresh_session_token(self, quiltt_profile_id: str, metadata: Optional[Dict] = None) -> Optional[Dict]:
        """
        Refresh session token for an existing Quiltt profile
        
        Args:
            quiltt_profile_id: The Quiltt Profile ID (starts with p_)
            metadata: Optional metadata to update
            
        Returns:
            Dict with 'token' and 'profileId' or None on error
        """
        if not self.api_key or not self.environment_id:
            log_error(logger, 'QUILTT', "Quiltt credentials not configured")
            return None
            
        try:
            url = 'https://auth.quiltt.io/v1/users/sessions'
            
            # For existing profiles, provide the Quiltt Profile ID
            payload = {
                'userId': quiltt_profile_id
            }
            
            if metadata:
                payload['metadata'] = metadata
            
            log_info(logger, 'QUILTT', f"Refreshing session token for Quiltt Profile: {quiltt_profile_id}")
            
            response = requests.post(
                url,
                json=payload,
                headers=self._get_headers(),
                timeout=10
            )
            
            log_info(logger, 'QUILTT', f"Response status: {response.status_code}")
            
            response.raise_for_status()
            
            data = response.json()
            log_info(logger, 'QUILTT', f"Refreshed session token for Profile: {quiltt_profile_id}")
            
            return {
                'token': data.get('token'),
                'profileId': data.get('userId'),
                'expiresAt': data.get('expiresAt')
            }
            
        except requests.exceptions.RequestException as e:
            log_error(logger, 'QUILTT', f"Error refreshing session token: {e}")
            if hasattr(e, 'response') and e.response is not None:
                log_error(logger, 'QUILTT', f"Response body: {e.response.text}")
            return None
    
    def update_profile_email(self, session_token: str, email: str) -> bool:
        """
        Update the user's profile with their email address
        This pre-fills the email in Quiltt Connector so users don't have to enter it
        
        Args:
            session_token: User's session token
            email: Email address to set
            
        Returns:
            True if successful, False otherwise
        """
        mutation = """
        mutation UpdateProfile($email: String!) {
            profileUpdate(input: {email: $email}) {
                record {
                    id
                    email
                }
            }
        }
        """
        
        variables = {'email': email}
        result = self.query_graphql(session_token, mutation, variables)
        
        if result and 'profileUpdate' in result:
            log_info(logger, 'QUILTT', f"Successfully updated profile email")
            return True
        else:
            log_warning(logger, 'QUILTT', f"Failed to update profile email")
            return False
    
    def revoke_session_token(self, session_token: str) -> bool:
        """Revoke a session token"""
        try:
            url = f'{self.base_url}/users/sessions/revoke'
            
            response = requests.post(
                url,
                headers=self._get_headers(session_token),
                timeout=10
            )
            response.raise_for_status()
            
            log_info(logger, 'QUILTT', "Revoked Quiltt session token")
            return True
            
        except requests.exceptions.RequestException as e:
            log_error(logger, 'QUILTT', f"Error revoking Quiltt session token: {e}")
            return False
    
    def query_graphql(self, session_token: str, query: str, variables: Optional[Dict] = None) -> Optional[Dict]:
        """
        Execute a GraphQL query against Quiltt API
        
        Args:
            session_token: User's session token
            query: GraphQL query string
            variables: Optional query variables
            
        Returns:
            Query result dict or None on error
        """
        try:
            payload = {'query': query}
            if variables:
                payload['variables'] = variables
            
            response = requests.post(
                self.graphql_url,
                json=payload,
                headers=self._get_headers(session_token),
                timeout=30
            )
            response.raise_for_status()
            
            # Log raw response for debugging
            response_text = response.text
            log_info(logger, 'QUILTT', f"GraphQL response (first 500 chars): {response_text[:500]}")
            
            try:
                data = response.json()
            except ValueError as json_err:
                log_error(logger, 'QUILTT', f"JSON decode error: {json_err}")
                log_error(logger, 'QUILTT', f"Response content: {response_text[:1000]}")
                return None
            
            if 'errors' in data:
                log_error(logger, 'QUILTT', f"GraphQL errors: {data['errors']}")
                return None
                
            return data.get('data')
            
        except requests.exceptions.RequestException as e:
            log_error(logger, 'QUILTT', f"Error executing GraphQL query: {e}")
            if hasattr(e, 'response') and e.response is not None:
                log_error(logger, 'QUILTT', f"Response status: {e.response.status_code}")
                log_error(logger, 'QUILTT', f"Response body: {e.response.text[:1000]}")
            return None
    
    def get_connection(self, session_token: str, connection_id: str) -> Optional[Dict]:
        """
        Get a specific connection's details including its current status
        
        Args:
            session_token: User's session token
            connection_id: The connection ID to fetch
            
        Returns:
            Connection dict with status or None on error
        """
        query = """
        query GetConnection($id: ID!) {
            connection(id: $id) {
                id
                status
                institution {
                    id
                    name
                }
                at
            }
        }
        """
        
        variables = {'id': connection_id}
        result = self.query_graphql(session_token, query, variables)
        return result.get('connection') if result else None
    
    def get_profile(self, session_token: str) -> Optional[Dict]:
        """Get user's Quiltt connections and accounts"""
        query = """
        query GetConnections {
            connections {
                id
                institution {
                    id
                    name
                }
                status
                at
                accounts {
                    id
                    name
                    kind
                    mask
                    balance {
                        current
                        available
                    }
                    currencyCode
                    remoteData {
                        finicity {
                            account {
                                response {
                                    detail {
                                        interestRate
                                        originalInterestRate
                                        creditAvailableAmount
                                        creditMaxAmount
                                        currentBalance
                                        lastPaymentAmount
                                        lastPaymentDate
                                        nextPaymentDate
                                        termOfMl
                                        openDate
                                        maturityDate
                                        initialMlAmount
                                        currentLoanBalance
                                        escrowBalance
                                        payoffAmount
                                        ytdPrincipalPaid
                                        ytdInterestPaid
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        """
        
        result = self.query_graphql(session_token, query)
        # Return in a format compatible with existing code - wrap connections in dict
        return {'connections': result.get('connections', [])} if result else None
    
    def get_transactions(self, session_token: str, account_id: Optional[str] = None, 
                        start_date: Optional[str] = None, end_date: Optional[str] = None,
                        limit: int = 100) -> Optional[List[Dict]]:
        """
        Get transactions for a user
        
        Args:
            session_token: User's session token
            account_id: Optional account ID to filter by
            start_date: Optional start date (YYYY-MM-DD)
            end_date: Optional end date (YYYY-MM-DD)
            limit: Maximum number of transactions to return
            
        Returns:
            List of transaction dicts or None on error
        """
        log_info(logger, 'QUILTT', f"Fetching transactions: account_id={account_id}, start={start_date}, end={end_date}, limit={limit}")
        
        # Build filter for account and date range
        filter_parts = []
        if account_id:
            filter_parts.append(f'accountIds: ["{account_id}"]')
        if start_date:
            filter_parts.append(f'date_gte: "{start_date}"')
        if end_date:
            filter_parts.append(f'date_lte: "{end_date}"')
        
        filter_str = ', '.join(filter_parts) if filter_parts else ''
        filter_arg = f'filter: {{ {filter_str} }}' if filter_str else ''
        
        query = f"""
        query GetTransactions {{
            transactions({filter_arg}, first: {limit}, sort: DATE_DESC) {{
                nodes {{
                    id
                    account {{
                        id
                    }}
                    amount
                    date
                    description
                    status
                    entryType
                    kind
                }}
            }}
        }}
        """
        
        log_info(logger, 'QUILTT', f"GraphQL query: {query}")
        result = self.query_graphql(session_token, query, None)
        
        if result and 'transactions' in result and 'nodes' in result['transactions']:
            transactions = result['transactions']['nodes']
            log_info(logger, 'QUILTT', f"Retrieved {len(transactions)} transactions from Quiltt API")
            if transactions:
                log_info(logger, 'QUILTT', f"First transaction sample: {transactions[0]}")
            return transactions
        else:
            log_warning(logger, 'QUILTT', f"No transactions found in response. Result structure: {result}")
        
        return None
    
    def get_transactions_with_ntropy(self, session_token: str, account_ids: Optional[List[str]] = None,
                                     start_date: Optional[str] = None, end_date: Optional[str] = None,
                                     limit: int = 1000) -> Optional[List[Dict]]:
        """
        Get transactions with Ntropy enrichment data for category recommendations
        
        Args:
            session_token: User's session token
            account_ids: Optional list of account IDs to filter by
            start_date: Optional start date (YYYY-MM-DD)
            end_date: Optional end date (YYYY-MM-DD)
            limit: Maximum number of transactions to return (default 500)
            
        Returns:
            List of transaction dicts with Ntropy data or None on error
        """
        log_info(logger, 'QUILTT', f"Fetching transactions with Ntropy enrichment: accounts={account_ids}, start={start_date}, end={end_date}, limit={limit}")
        
        # Build filter with account IDs and date range
        filter_parts = []
        
        # Filter by account IDs if provided (using accountIds field from TransactionFilter)
        if account_ids:
            # Format as GraphQL array of quoted strings
            account_ids_str = ', '.join([f'"{acc_id}"' for acc_id in account_ids])
            filter_parts.append(f'accountIds: [{account_ids_str}]')
            log_info(logger, 'QUILTT', f"Filtering by account IDs: {account_ids_str}")
        else:
            log_warning(logger, 'QUILTT', "No account IDs provided - will fetch all user transactions")
        
        if start_date:
            filter_parts.append(f'date_gte: "{start_date}"')
        if end_date:
            filter_parts.append(f'date_lte: "{end_date}"')
        
        filter_str = ', '.join(filter_parts) if filter_parts else ''
        filter_arg = f'filter: {{ {filter_str} }}' if filter_str else ''
        
        # Fetch all transactions using pagination
        all_transactions = []
        has_next_page = True
        after_cursor = None
        page_count = 0
        max_pages = 20  # Safety limit to prevent infinite loops
        
        while has_next_page and page_count < max_pages:
            page_count += 1
            after_arg = f', after: "{after_cursor}"' if after_cursor else ''
            
            query = f"""
            query GetTransactionsWithNtropy {{
                transactions({filter_arg}, first: 100{after_arg}, sort: DATE_DESC) {{
                    pageInfo {{
                        hasNextPage
                        endCursor
                    }}
                    nodes {{
                        id
                        account {{
                            id
                            type
                        }}
                        amount
                        date
                        description
                        status
                        entryType
                        kind
                        remoteData {{
                            ntropy {{
                                enrichment {{
                                    id
                                    timestamp
                                    response {{
                                        categories {{
                                            general
                                            accounting
                                        }}
                                        entities {{
                                            counterparty {{
                                                id
                                                name
                                                logo
                                                mccs
                                                website
                                                type
                                            }}
                                            intermediaries {{
                                                id
                                                name
                                                logo
                                                mccs
                                                website
                                            }}
                                        }}
                                        location
                                        id
                                        createdAt
                                    }}
                                }}
                            }}
                            finicity {{
                                transaction {{
                                    response {{
                                        createdDate
                                        categorization {{
                                            bestRepresentation
                                            normalizedPayeeName
                                            category
                                            city
                                            state
                                            country
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    }}
                }}
            }}
            """
            
            log_info(logger, 'NTROPY', f"Fetching page {page_count} of transactions...")
            log_info(logger, 'NTROPY', f"Filter: {filter_arg}")
            result = self.query_graphql(session_token, query, None)
            
            if not result:
                log_warning(logger, 'NTROPY', f"Page {page_count}: GraphQL returned None")
                break
            
            if 'transactions' not in result:
                log_warning(logger, 'NTROPY', f"Page {page_count}: No 'transactions' key in result. Keys: {result.keys() if result else 'None'}")
                log_warning(logger, 'NTROPY', f"Result: {str(result)[:500]}")
                break
            
            transactions_data = result['transactions']
            page_transactions = transactions_data.get('nodes', [])
            all_transactions.extend(page_transactions)
            
            # Check pagination
            page_info = transactions_data.get('pageInfo', {})
            has_next_page = page_info.get('hasNextPage', False)
            after_cursor = page_info.get('endCursor')
            
            log_info(logger, 'NTROPY', f"Page {page_count}: {len(page_transactions)} transactions, hasNextPage={has_next_page}, total so far={len(all_transactions)}")
            
            # Stop if we've reached the limit
            if len(all_transactions) >= limit:
                log_info(logger, 'NTROPY', f"Reached limit of {limit} transactions")
                all_transactions = all_transactions[:limit]
                break
        
        if all_transactions:
            log_info(logger, 'NTROPY', f"Total transactions fetched: {len(all_transactions)}")
            
            # Log account type breakdown for visibility
            type_counts = {}
            for txn in all_transactions:
                acct = txn.get('account', {})
                atype = acct.get('type', 'UNKNOWN').upper()
                type_counts[atype] = type_counts.get(atype, 0) + 1
            log_info(logger, 'NTROPY', f"Account type breakdown: {type_counts}")
            
            # Log first transaction as sample
            sample = all_transactions[0]
            log_info(logger, 'QUILTT', f"Sample transaction: {sample.get('description')} - Amount: {sample.get('amount')}")
            
            return all_transactions
        else:
            log_info(logger, 'NTROPY', "No transactions found in any page")
        
        return None
    
    def analyze_transactions_for_categories(self, transactions: List[Dict]) -> Dict:
        """
        Analyze transactions with Ntropy data to generate category recommendations
        
        Args:
            transactions: List of transactions with Ntropy enrichment data
            
        Returns:
            Dict with 'income' and 'expense' category recommendations
        """
        from datetime import datetime, timedelta
        from statistics import median
        from collections import defaultdict
        
        log_info(logger, 'QUILTT', f"Analyzing {len(transactions)} transactions for category recommendations")
        
        if not transactions or len(transactions) == 0:
            log_warning(logger, 'QUILTT', "No transactions to analyze - returning fallback")
            return {'fallback': True, 'income': [], 'expense': []}
        
        # Group transactions by category
        income_categories = defaultdict(list)
        expense_categories = defaultdict(list)
        
        for txn in transactions:
            # Extract category from Ntropy enrichment (Quiltt schema: entities.counterparty, categories.general)
            remote_data = txn.get('remoteData', {})
            ntropy_data = remote_data.get('ntropy', {})
            enrichment = ntropy_data.get('enrichment', {})
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
                log_info(logger, 'QUILTT', f"Skipping transaction - no category or merchant: {txn.get('description')}")
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
                log_info(logger, 'QUILTT', f"Skipping internal banking operation: {category_name}")
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
        
        log_info(logger, 'QUILTT', f"Found {len(income_categories)} income categories, {len(expense_categories)} expense categories")
        
        # Analyze each category for recurring patterns
        income_recommendations = []
        expense_recommendations = []
        
        for category_name, txns in income_categories.items():
            rec = self._analyze_category_pattern(category_name, txns, is_income=True)
            if rec:
                income_recommendations.append(rec)
        
        for category_name, txns in expense_categories.items():
            rec = self._analyze_category_pattern(category_name, txns, is_income=False)
            if rec:
                expense_recommendations.append(rec)
        
        # Sort by importance (recurring first, then by amount)
        income_recommendations.sort(key=lambda x: (not x.get('is_recurring', False), -x.get('amount', 0)))
        expense_recommendations.sort(key=lambda x: (not x.get('is_recurring', False), -x.get('amount', 0)))
        
        # Limit to top categories
        income_recommendations = income_recommendations[:10]
        expense_recommendations = expense_recommendations[:15]
        
        log_info(logger, 'QUILTT', f"Generated {len(income_recommendations)} income recommendations, {len(expense_recommendations)} expense recommendations")
        
        return {
            'fallback': False,
            'income': income_recommendations,
            'expense': expense_recommendations
        }
    
    def _analyze_category_pattern(self, category_name: str, transactions: List[Dict], is_income: bool) -> Optional[Dict]:
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
    
    def disconnect_connection(self, session_token: str, connection_id: str) -> bool:
        """Disconnect a financial institution connection"""
        mutation = """
        mutation DisconnectConnection($id: ID!) {
            connectionDisconnect(input: {id: $id}) {
                record {
                    id
                    status
                }
            }
        }
        """
        
        variables = {'id': connection_id}
        result = self.query_graphql(session_token, mutation, variables)
        
        return result is not None
    
    def delete_profile(self, profile_id: str) -> bool:
        """
        Permanently delete a Quiltt Profile and all its associated data.
        
        This is a Platform API call that requires the API key (not a session token).
        It permanently deletes:
        - Profile information (name, email, phone, etc.)
        - All Connections and their credentials
        - All Accounts and their balances
        - All Transactions and transaction data
        - All Identities and identity verification data
        - Custom metadata associated with the Profile
        
        Note: This process can take up to 15 minutes to complete on Quiltt's side.
        
        Args:
            profile_id: The Quiltt Profile ID (e.g., 'p_1hyoxpVVFib1HngGwKAzIr')
            
        Returns:
            True if deletion request was successful, False otherwise
        """
        if not self.api_key:
            log_error(logger, 'QUILTT', "Quiltt API key not configured - cannot delete profile")
            return False
            
        if not profile_id:
            log_warning(logger, 'QUILTT', "No profile_id provided for deletion")
            return False
            
        try:
            url = f'{self.base_url}/profiles/{profile_id}'
            
            response = requests.delete(
                url,
                headers=self._get_headers(),  # Uses API key for Platform API
                timeout=30
            )
            
            # 204 No Content means successful deletion
            if response.status_code == 204:
                log_info(logger, 'QUILTT', f"Successfully requested deletion of Quiltt profile: {profile_id}")
                return True
            elif response.status_code == 404:
                # Profile doesn't exist - consider this a success
                log_warning(logger, 'QUILTT', f"Quiltt profile not found (may already be deleted): {profile_id}")
                return True
            else:
                log_error(logger, 'QUILTT', f"Failed to delete Quiltt profile. Status: {response.status_code}, Body: {response.text}")
                return False
                
        except requests.exceptions.RequestException as e:
            log_error(logger, 'QUILTT', f"Error deleting Quiltt profile {profile_id}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                log_error(logger, 'QUILTT', f"Response status: {e.response.status_code}")
                log_error(logger, 'QUILTT', f"Response body: {e.response.text}")
            return False


def map_quiltt_transaction_to_entry(transaction: Dict, user_id: int, category_mapping: Dict[str, int]) -> Dict:
    """
    Map a Quiltt transaction to your app's entry format
    
    Args:
        transaction: Quiltt transaction dict
        user_id: Your app's user ID
        category_mapping: Dict mapping Quiltt categories to your category IDs
        
    Returns:
        Entry dict ready for your database
    """
    amount = abs(float(transaction.get('amount', 0)))
    is_expense = float(transaction.get('amount', 0)) < 0
    
    # Map Quiltt category to your app's category
    quiltt_category = transaction.get('category', 'Other')
    category_id = category_mapping.get(quiltt_category, category_mapping.get('Other'))
    
    entry = {
        'user_id': user_id,
        'category_id': category_id,
        'date': transaction.get('date'),
        'amount': amount,
        'description': transaction.get('description', ''),
        'merchant_name': transaction.get('merchantName', ''),
        'quiltt_transaction_id': transaction.get('id'),
        'pending': transaction.get('pending', False),
        'is_expense': is_expense
    }
    
    return entry


def get_default_category_mapping() -> Dict[str, str]:
    """
    Get default mapping of Quiltt transaction categories to budget categories
    This can be customized per user in your database
    """
    return {
        # Quiltt category -> Your app category name
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
