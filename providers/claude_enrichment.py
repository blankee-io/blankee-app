"""
Claude (Anthropic) as the enrichment provider.

Phase 1 of the bank work: configuration, verification and gating only. The
enrichment itself - batching imported transactions through the Messages API
and answering suggest_category - arrives with the transaction import; until
then enrich() returns its input untouched and suggest_category() returns None,
exactly as the null provider does. What is real now:

  * a per-user API key, Fernet-encrypted in user_ai_settings (not Redis-first:
    a credential must not sit in a 7-day cache - the same rule as
    instance_settings), plus the model to use;
  * a Test that makes one tiny Messages call and, on success, records a
    fingerprint of key+model - so changing either un-verifies as arithmetic,
    the way instance_settings.is_verified() works;
  * the gate. AI categorization is on for a user only when ALL of these hold:
    the user switched it on, the key+model are verified, and the user has at
    least one active linked bank account. The last condition is Adrian's
    rule "no LLM without a bank": disconnecting the last bank makes the
    feature inert without touching the key.

Keyed on Blankee's user_id throughout, as providers/base.py requires.
"""

import hashlib
from typing import Any, Dict, List, Optional, Tuple

import httpx

from db_connections import get_db_pool
from log_config import get_logger, log_info, log_error, log_warning
from providers.base import EnrichmentProvider

logger = get_logger(__name__)

API_URL = 'https://api.anthropic.com/v1/messages'
API_VERSION = '2023-06-01'

# What the model select offers. The first is the default: cheapest, and more
# than enough to sort "STARBUCKS #1234" into Coffee.
MODELS = [
    ('claude-haiku-4-5-20251001', 'Claude Haiku 4.5 - fastest and cheapest (recommended)'),
    ('claude-sonnet-5', 'Claude Sonnet 5 - more capable, about twice the cost'),
]
DEFAULT_MODEL = MODELS[0][0]
_MODEL_IDS = {m for m, _ in MODELS}

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _fernet():
    from instance_settings import _fernet as f
    return f()


def _fingerprint(api_key: str, model: str) -> str:
    """
    Hash of what a verification applied to. Includes the key, unlike the SMTP
    fingerprint: here the key IS the thing being tested, there is no other
    transport, and it is already stored encrypted beside this.
    """
    return hashlib.sha256(f'{model}|{api_key}'.encode('utf-8')).hexdigest()


def _read_row(user_id: int) -> Optional[Dict[str, Any]]:
    try:
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT user_id, api_key_encrypted, model, verified_fingerprint, verified_at, "
                "last_error, created_at, last_modified FROM user_ai_settings WHERE user_id = %s",
                (user_id,))
            return cursor.fetchone()
    except Exception as e:
        log_warning(logger, 'AI', f'Could not read user_ai_settings for user {user_id}: {e}')
        return None


def _api_key(row: Optional[Dict[str, Any]]) -> str:
    token = (row or {}).get('api_key_encrypted')
    if not token:
        return ''
    f = _fernet()
    if f is None:
        log_error(logger, 'AI', 'An API key is stored but SETTINGS_ENCRYPTION_KEY is missing or invalid.')
        return ''
    try:
        return f.decrypt(token.encode('utf-8')).decode('utf-8')
    except Exception as e:
        log_error(logger, 'AI', f'Stored API key could not be decrypted ({type(e).__name__}).')
        return ''


def has_active_linked_account(user_id: int) -> bool:
    """
    The bank half of the gate.

    Reads the Redis copy directly (falling back to MySQL) rather than the
    hydration-gated reader: right after linking - which is exactly when the
    wizard moves on to this step - the rows are in Redis and not yet flushed,
    and the gated reader would answer "no bank" for the next fifteen seconds.
    """
    try:
        from bank_redis import _get_all_linked_accounts_raw, linked_account_kind
        return any(int(a.get('is_active', 1) or 0) == 1 and linked_account_kind(a)
                   for a in (_get_all_linked_accounts_raw(user_id) or []))
    except Exception as e:
        log_warning(logger, 'AI', f'Could not determine linked accounts for user {user_id}: {e}')
        return False


def user_opted_in(user_id: int) -> bool:
    """users.ai_categorization, Redis-first like every other user setting."""
    try:
        from redis_manager import _redis_client
        import json
        if _redis_client is not None:
            cached = _redis_client.get(f'users:v1:{user_id}')
            if cached:
                return int(json.loads(cached).get('ai_categorization') or 0) == 1
    except Exception:
        pass
    try:
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT ai_categorization FROM users WHERE id = %s", (user_id,))
            row = cursor.fetchone()
            return bool(row and int(row.get('ai_categorization') or 0) == 1)
    except Exception:
        return False


class ClaudeEnrichmentProvider(EnrichmentProvider):
    name = 'claude'

    # ------------------------------------------------------------ config

    def is_configured(self) -> bool:
        return True

    def get_config(self, user_id: int) -> Dict[str, Any]:
        """The decrypted configuration. Only for making calls; never rendered."""
        row = _read_row(user_id) or {}
        key = _api_key(row)
        model = (row.get('model') or DEFAULT_MODEL).strip()
        verified = bool(key and row.get('verified_fingerprint')
                        and row['verified_fingerprint'] == _fingerprint(key, model))
        return {'api_key': key, 'model': model, 'verified': verified,
                'key_stored': bool(row.get('api_key_encrypted'))}

    def is_verified(self, user_id: int) -> bool:
        return self.get_config(user_id)['verified']

    def is_active(self, user_id: int) -> bool:
        """The gate: opted in, verified, and a bank is linked."""
        return user_opted_in(user_id) and self.is_verified(user_id) and has_active_linked_account(user_id)

    def get_display(self, user_id: int) -> Dict[str, Any]:
        """What the settings panel needs - no key, only whether one is stored."""
        row = _read_row(user_id) or {}
        cfg = self.get_config(user_id)
        linked = has_active_linked_account(user_id)
        enabled = user_opted_in(user_id)
        return {
            'key_stored': cfg['key_stored'],
            'model': cfg['model'],
            'models': [{'id': m, 'label': l} for m, l in MODELS],
            'verified': cfg['verified'],
            'verified_at': row.get('verified_at').isoformat() if row.get('verified_at') else None,
            'last_error': row.get('last_error') or '',
            'bank_linked': linked,
            'enabled': enabled,
            'effective': enabled and cfg['verified'] and linked,
            'encryption_available': _fernet() is not None,
        }

    def save_config(self, user_id: int, api_key: Optional[str], model: Optional[str]) -> Tuple[bool, str]:
        """
        Store key and/or model. A blank key means "keep the stored one" - the
        field is never prefilled. Changing either clears verification.
        """
        model = (model or DEFAULT_MODEL).strip()
        if model not in _MODEL_IDS:
            return (False, 'Choose one of the listed models.')
        api_key = (api_key or '').strip()
        encrypted = None
        if api_key:
            if not api_key.startswith('sk-ant-'):
                return (False, 'That does not look like an Anthropic API key (they start with sk-ant-).')
            f = _fernet()
            if f is None:
                return (False, 'The server has no SETTINGS_ENCRYPTION_KEY, so the key cannot be stored. '
                               'Ask the administrator to set one.')
            encrypted = f.encrypt(api_key.encode('utf-8')).decode('utf-8')
        row = _read_row(user_id) or {}
        if not encrypted and not row.get('api_key_encrypted'):
            return (False, 'Paste your Anthropic API key first.')
        try:
            with get_db_pool().get_cursor(commit=True) as cursor:
                if encrypted:
                    cursor.execute(
                        "INSERT INTO user_ai_settings (user_id, api_key_encrypted, model) VALUES (%s, %s, %s) "
                        "ON DUPLICATE KEY UPDATE api_key_encrypted = VALUES(api_key_encrypted), "
                        " model = VALUES(model), verified_fingerprint = NULL, verified_at = NULL, last_error = NULL",
                        (user_id, encrypted, model))
                else:
                    # Model only. The fingerprint covers the model, so a change
                    # un-verifies by arithmetic; nothing to clear explicitly.
                    cursor.execute("UPDATE user_ai_settings SET model = %s WHERE user_id = %s", (model, user_id))
        except Exception as e:
            log_error(logger, 'AI', f'Could not save AI settings for user {user_id}: {e}')
            return (False, 'Could not save the settings.')
        log_info(logger, 'AI', f'AI settings saved for user {user_id} (model={model}, key_changed={bool(encrypted)})')
        return (True, 'Saved. Now test the key.')

    def clear_key(self, user_id: int) -> Tuple[bool, str]:
        try:
            with get_db_pool().get_cursor(commit=True) as cursor:
                cursor.execute("DELETE FROM user_ai_settings WHERE user_id = %s", (user_id,))
        except Exception as e:
            log_error(logger, 'AI', f'Could not clear AI settings for user {user_id}: {e}')
            return (False, 'Could not remove the key.')
        return (True, 'Key removed. AI categorization is off.')

    def test_config(self, user_id: int) -> Tuple[bool, str]:
        """
        One minimal Messages call. Success records the fingerprint; failure
        records the reason where the panel can show it.
        """
        cfg = self.get_config(user_id)
        if not cfg['api_key']:
            return (False, 'Save an API key first.')
        ok, message = self._probe(cfg['api_key'], cfg['model'])
        try:
            with get_db_pool().get_cursor(commit=True) as cursor:
                if ok:
                    cursor.execute(
                        "UPDATE user_ai_settings SET verified_fingerprint = %s, verified_at = NOW(), "
                        "last_error = NULL WHERE user_id = %s",
                        (_fingerprint(cfg['api_key'], cfg['model']), user_id))
                else:
                    cursor.execute(
                        "UPDATE user_ai_settings SET verified_fingerprint = NULL, verified_at = NULL, "
                        "last_error = %s WHERE user_id = %s", (message[:255], user_id))
        except Exception as e:
            log_error(logger, 'AI', f'Could not record the test result for user {user_id}: {e}')
        log_info(logger, 'AI', f'API key test for user {user_id}: {"ok" if ok else "failed"}')
        return (ok, message)

    @staticmethod
    def _probe(api_key: str, model: str) -> Tuple[bool, str]:
        payload = {
            'model': model,
            'max_tokens': 8,
            'messages': [{'role': 'user', 'content': 'Reply with the single word OK.'}],
        }
        headers = {'x-api-key': api_key, 'anthropic-version': API_VERSION, 'content-type': 'application/json'}
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                resp = client.post(API_URL, json=payload, headers=headers)
        except httpx.HTTPError as e:
            return (False, f'Could not reach Anthropic ({type(e).__name__}). Try again.')
        if resp.status_code == 200:
            return (True, 'The key works. AI categorization can be turned on.')
        detail = ''
        try:
            detail = (resp.json().get('error') or {}).get('message') or ''
        except ValueError:
            pass
        if resp.status_code == 401:
            return (False, 'Anthropic rejected the key. Check it was copied whole, or create a new one.')
        if resp.status_code == 403:
            return (False, 'Anthropic refused the request for this key. Check the key\'s permissions on platform.claude.com.')
        if resp.status_code == 404 or 'model' in detail.lower():
            return (False, f'That model is not available to this key ({detail or "not found"}). Try the other model.')
        if resp.status_code == 400 and 'credit' in detail.lower():
            return (False, 'The key works but the account has no credit. Add billing on platform.claude.com.')
        if resp.status_code == 429:
            return (False, 'Anthropic is rate-limiting this key right now. Try again in a minute.')
        if resp.status_code in (529, 503):
            return (False, 'Anthropic is overloaded at the moment. Try again shortly.')
        return (False, f'Anthropic answered with status {resp.status_code}{": " + detail if detail else ""}.')

    # ----------------------------------------------- the contract (phase 2)

    def sync_categories(self, user_id: int) -> bool:
        # Categories travel with each enrichment request; nothing to push.
        return False

    def enrich(self, user_id: int, transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # Phase 2. Returning the input, never an empty list - see the null
        # provider for why.
        return transactions

    def suggest_category(self, user_id: int, transaction: Dict[str, Any],
                         account_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
        # Phase 2.
        return None

    def recurrence_map(self, user_id: int) -> Dict[str, Any]:
        return {}

    def delete_user_data(self, user_id: int) -> bool:
        ok, _ = self.clear_key(user_id)
        return ok
