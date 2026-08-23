"""
Provider registry.

Selection is by env var so swapping vendors is a config change plus one new
module, not an edit to app.py:

    BANK_PROVIDER=null          (default)
    ENRICHMENT_PROVIDER=null    (default)

To add a vendor: write providers/<vendor>.py implementing BankProvider or
EnrichmentProvider from providers/base.py, then register it in the maps below.
"""

import os

from log_config import get_logger, log_info, log_warning
from providers.base import BankProvider, EnrichmentProvider
from providers.null_bank import NullBankProvider
from providers.null_enrichment import NullEnrichmentProvider

logger = get_logger(__name__)

_BANK_PROVIDERS = {
    'null': NullBankProvider,
}

_ENRICHMENT_PROVIDERS = {
    'null': NullEnrichmentProvider,
}

_bank_instance = None
_enrichment_instance = None


def get_bank_provider() -> BankProvider:
    """The configured bank provider (cached). Falls back to null."""
    global _bank_instance
    if _bank_instance is None:
        name = os.getenv('BANK_PROVIDER', 'null').strip().lower() or 'null'
        cls = _BANK_PROVIDERS.get(name)
        if cls is None:
            log_warning(logger, 'BANK',
                        f"Unknown BANK_PROVIDER '{name}'; falling back to null")
            cls = NullBankProvider
        _bank_instance = cls()
        log_info(logger, 'BANK', f"Bank provider: {_bank_instance.name}")
    return _bank_instance


def get_enrichment_provider() -> EnrichmentProvider:
    """The configured enrichment provider (cached). Falls back to null."""
    global _enrichment_instance
    if _enrichment_instance is None:
        name = os.getenv('ENRICHMENT_PROVIDER', 'null').strip().lower() or 'null'
        cls = _ENRICHMENT_PROVIDERS.get(name)
        if cls is None:
            log_warning(logger, 'ENRICHMENT',
                        f"Unknown ENRICHMENT_PROVIDER '{name}'; falling back to null")
            cls = NullEnrichmentProvider
        _enrichment_instance = cls()
        log_info(logger, 'ENRICHMENT', f"Enrichment provider: {_enrichment_instance.name}")
    return _enrichment_instance


def reset_providers():
    """Drop the cached instances (tests / config reload)."""
    global _bank_instance, _enrichment_instance
    _bank_instance = None
    _enrichment_instance = None
