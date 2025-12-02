"""
Quiltt.io API Integration Utilities
Handles authentication, API calls, and data transformation for Quiltt
"""

import os
import requests
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any

# Set up logging
logger = logging.getLogger(__name__)

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
            logger.warning("QUILTT_API_KEY not set in environment variables")
        if not self.environment_id:
            logger.warning("QUILTT_ENVIRONMENT_ID not set in environment variables")
        
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
            logger.error("Quiltt credentials not configured. Set QUILTT_API_KEY and QUILTT_ENVIRONMENT_ID")
            return None
            
        try:
            # Quiltt Auth API endpoint for issuing session tokens
            url = 'https://auth.quiltt.io/v1/users/sessions'
            
            # When creating a new profile, omit userId
            # Quiltt will generate a Profile ID which we'll store
            payload = {}
            
            if metadata:
                payload['metadata'] = metadata
            
            logger.info(f"Creating new Quiltt profile session token for app user {user_id}")
            logger.info(f"Request payload: {payload}")
            
            response = requests.post(
                url,
                json=payload,
                headers=self._get_headers(),
                timeout=10
            )
            
            logger.info(f"Response status: {response.status_code}")
            logger.info(f"Response body: {response.text[:500]}")
            
            response.raise_for_status()
            
            data = response.json()
            logger.info(f"Created Quiltt session token - Profile ID: {data.get('userId')}")
            
            return {
                'token': data.get('token'),
                'profileId': data.get('userId'),  # Quiltt returns userId as the Profile ID
                'expiresAt': data.get('expiresAt')
            }
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Error creating Quiltt session token: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response status: {e.response.status_code}")
                logger.error(f"Response body: {e.response.text}")
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
            logger.error("Quiltt credentials not configured")
            return None
            
        try:
            url = 'https://auth.quiltt.io/v1/users/sessions'
            
            # For existing profiles, provide the Quiltt Profile ID
            payload = {
                'userId': quiltt_profile_id
            }
            
            if metadata:
                payload['metadata'] = metadata
            
            logger.info(f"Refreshing session token for Quiltt Profile: {quiltt_profile_id}")
            
            response = requests.post(
                url,
                json=payload,
                headers=self._get_headers(),
                timeout=10
            )
            
            logger.info(f"Response status: {response.status_code}")
            
            response.raise_for_status()
            
            data = response.json()
            logger.info(f"Refreshed session token for Profile: {quiltt_profile_id}")
            
            return {
                'token': data.get('token'),
                'profileId': data.get('userId'),
                'expiresAt': data.get('expiresAt')
            }
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Error refreshing session token: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response body: {e.response.text}")
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
            logger.info(f"Successfully updated profile email")
            return True
        else:
            logger.warning(f"Failed to update profile email")
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
            
            logger.info("Revoked Quiltt session token")
            return True
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Error revoking Quiltt session token: {e}")
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
            logger.debug(f"GraphQL response (first 500 chars): {response_text[:500]}")
            
            try:
                data = response.json()
            except ValueError as json_err:
                logger.error(f"JSON decode error: {json_err}")
                logger.error(f"Response content: {response_text[:1000]}")
                return None
            
            if 'errors' in data:
                logger.error(f"GraphQL errors: {data['errors']}")
                return None
                
            return data.get('data')
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Error executing GraphQL query: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response status: {e.response.status_code}")
                logger.error(f"Response body: {e.response.text[:1000]}")
            return None
    
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
        query = """
        query GetTransactions($accountId: ID, $startDate: ISO8601Date, $endDate: ISO8601Date, $first: Int) {
            profile {
                transactions(accountId: $accountId, startDate: $startDate, endDate: $endDate, first: $first) {
                    nodes {
                        id
                        accountId
                        amount
                        date
                        description
                        pending
                        category
                        merchantName
                        transactionType
                    }
                }
            }
        }
        """
        
        variables = {
            'first': limit
        }
        
        if account_id:
            variables['accountId'] = account_id
        if start_date:
            variables['startDate'] = start_date
        if end_date:
            variables['endDate'] = end_date
        
        result = self.query_graphql(session_token, query, variables)
        
        if result and 'profile' in result and 'transactions' in result['profile']:
            return result['profile']['transactions']['nodes']
        
        return None
    
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
        'Other': 'Auto Adjustments'
    }
