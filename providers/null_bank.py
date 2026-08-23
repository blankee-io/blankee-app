"""
The no-bank-provider provider.

This is what Blankee runs on between aggregation vendors. Every method answers
"nothing", which is a valid answer everywhere it is called: no connections, no
accounts, no transactions to import, no balances to auto-adjust against, and no
lock date - so the entry-locking engine leaves every cell editable.
"""

from typing import Any, Dict, List, Optional

from log_config import get_logger, log_info
from providers.base import BankProvider

logger = get_logger(__name__)


class NullBankProvider(BankProvider):
    """Answers 'nothing' to everything, without raising."""

    name = 'null'

    def is_configured(self) -> bool:
        return False

    def connect_widget_config(self, user_id: int) -> Optional[Dict[str, Any]]:
        return None

    def list_connections(self, user_id: int) -> List[Dict[str, Any]]:
        return []

    def list_accounts(self, user_id: int) -> List[Dict[str, Any]]:
        return []

    def fetch_transactions(self, user_id: int, start: Optional[str] = None,
                           end: Optional[str] = None,
                           account_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return []

    def fetch_account_balances(self, user_id: int) -> List[Dict[str, Any]]:
        return []

    def disconnect(self, user_id: int, connection_id: str) -> bool:
        return False

    def delete_user(self, user_id: int) -> bool:
        # Nothing held anywhere, so deletion trivially succeeded.
        log_info(logger, 'BANK', f"No bank provider configured; nothing to delete for user {user_id}")
        return True

    def verify_webhook(self, headers: Dict[str, str], raw_body: bytes) -> bool:
        # No provider means no legitimate webhook sender.
        return False
