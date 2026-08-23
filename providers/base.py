"""
Provider interfaces for bank aggregation and transaction enrichment.

Blankee used to talk to Quiltt (bank aggregation) and Ntropy (enrichment)
directly, with their API shapes threaded through several thousand lines of
app.py. Both integrations were removed; these two interfaces are what replaced
them. A new provider implements one of these classes and nothing outside its
own module needs to know which vendor is behind it.

Read this before writing a provider:

1. Nothing here may leak a vendor's identifiers into Blankee's own vocabulary.
   The mistake the Ntropy integration made was taking the *Quiltt* profile id as
   its account-holder key, which quietly welded the two vendors together and
   meant neither could be replaced alone. Every method below is keyed on
   Blankee's own user_id. A provider that needs its own account-holder id is
   responsible for storing and resolving that itself.

2. fetch_transactions returns the NORMALIZED shape below, not the vendor's
   payload. Translation happens inside the provider, so the importer never
   learns a vendor's field names:

       {
         'provider_txn_id':    str   - vendor's stable transaction id
         'account_ref':        str   - vendor's account id, matches
                                       linked_accounts.account_id
         'amount':             float - signed; negative is money out
         'date':               'YYYY-MM-DD'
         'description':        str
         'merchant_name':      str
         'category':           str   - vendor's own category label, or ''
         'pending':            bool
         'transaction_type':   str
         'provider_created_at': str | None
         'enrichment': {             - {} when nothing enriched it
             'labels':              list[str],
             'merchant_id':         str,
             'recurrence':          str,
             'recurrence_group_id': str,
             'periodicity':         str,
             'periodicity_days':    int | None,
             'avg_amount':          float | None,
             'first_payment_date':  'YYYY-MM-DD' | None,
             'last_payment_date':   'YYYY-MM-DD' | None,
         }
       }

3. Every method must be safe to call when nothing is configured. That is what
   the null providers are for, and it is why the app runs today with no bank
   vendor at all: is_configured() returns False, the fetches return empty, and
   the features built on top of them render empty states instead of breaking.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class BankProvider(ABC):
    """A source of bank connections, accounts, and transactions."""

    name: str = 'base'

    @abstractmethod
    def is_configured(self) -> bool:
        """True when credentials are present and this provider can be used."""

    @abstractmethod
    def connect_widget_config(self, user_id: int) -> Optional[Dict[str, Any]]:
        """
        Everything the frontend needs to open this provider's connect flow, or
        None when there is nothing to open. Templates must treat None as "hide
        the connect button" rather than rendering a dead control.
        """

    @abstractmethod
    def list_connections(self, user_id: int) -> List[Dict[str, Any]]:
        """Institution-level connections for this user."""

    @abstractmethod
    def list_accounts(self, user_id: int) -> List[Dict[str, Any]]:
        """Accounts across all of this user's connections."""

    @abstractmethod
    def fetch_transactions(self, user_id: int, start: Optional[str] = None,
                           end: Optional[str] = None,
                           account_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Transactions in the normalized shape documented at module level."""

    @abstractmethod
    def fetch_account_balances(self, user_id: int) -> List[Dict[str, Any]]:
        """
        Current balances, used by the auto-balance/auto-adjustment engine.
        Each item: {'account_ref', 'account_type', 'current_balance',
                    'available_balance', 'mask'}.
        """

    @abstractmethod
    def disconnect(self, user_id: int, connection_id: str) -> bool:
        """Drop one connection on the provider side."""

    @abstractmethod
    def delete_user(self, user_id: int) -> bool:
        """
        Erase everything this provider holds for the user. Called from account
        deletion, so it must not raise - return False and log instead.
        """

    @abstractmethod
    def verify_webhook(self, headers: Dict[str, str], raw_body: bytes) -> bool:
        """Authenticate an inbound webhook. False rejects the request."""


class EnrichmentProvider(ABC):
    """Adds merchant, category, and recurrence detail to transactions."""

    name: str = 'base'

    @abstractmethod
    def is_configured(self) -> bool:
        """True when credentials are present and this provider can be used."""

    @abstractmethod
    def sync_categories(self, user_id: int) -> bool:
        """Push the user's current category list to the provider."""

    @abstractmethod
    def enrich(self, user_id: int, transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Fill in the 'enrichment' sub-dict on each normalized transaction and
        return the list. Must return the input unchanged rather than dropping
        transactions when it cannot enrich them.
        """

    @abstractmethod
    def suggest_category(self, user_id: int, transaction: Dict[str, Any],
                         account_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Suggest one of the user's own categories for a transaction:
        {'category_id', 'category_type', 'category_name', 'confidence'} or None.
        """

    @abstractmethod
    def recurrence_map(self, user_id: int) -> Dict[str, Any]:
        """
        Recurrence groups keyed by provider_txn_id. Keyed on Blankee's user_id -
        see point 1 at module level.
        """

    @abstractmethod
    def delete_user_data(self, user_id: int) -> bool:
        """
        Erase everything this provider holds for the user. Called from account
        deletion; must not raise.
        """
