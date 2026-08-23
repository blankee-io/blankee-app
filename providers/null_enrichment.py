"""
The no-enrichment-provider provider.

Transactions pass through untouched. Suggestions come back empty, which the
pending-transaction UI already handles - it just shows no AI suggestion badge.
"""

from typing import Any, Dict, List, Optional

from log_config import get_logger, log_info
from providers.base import EnrichmentProvider

logger = get_logger(__name__)


class NullEnrichmentProvider(EnrichmentProvider):
    """Passes transactions through unchanged, without raising."""

    name = 'null'

    def is_configured(self) -> bool:
        return False

    def sync_categories(self, user_id: int) -> bool:
        return False

    def enrich(self, user_id: int, transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # Returns the input, NOT an empty list - dropping transactions here
        # would silently lose imports rather than merely leaving them unenriched.
        return transactions

    def suggest_category(self, user_id: int, transaction: Dict[str, Any],
                         account_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
        return None

    def recurrence_map(self, user_id: int) -> Dict[str, Any]:
        return {}

    def delete_user_data(self, user_id: int) -> bool:
        log_info(logger, 'ENRICHMENT', f"No enrichment provider configured; nothing to delete for user {user_id}")
        return True
