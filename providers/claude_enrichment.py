"""
Claude (Anthropic) as the enrichment provider.

Two halves. The first is configuration, verification and gating; the second
is the work itself - a batch of imported transactions goes through the
Messages API once per pull and comes back sorted into the person's own
categories, by name, which is then checked against the list that was sent
(a name that is not on it is dropped, never invented). What is sent, and
all that is sent: each transaction's description, amount and direction, and
the names of the person's categories. enrich() still returns its input
untouched - there is no per-transaction enrichment beyond the category.

The first half:

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
import json
import time
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

_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# Transactions per request. A day's pull is a handful; a first pull after a
# quiet spell can be more, and forty lines is well inside what a small model
# answers accurately in one go.
BATCH_SIZE = 40

# The instruction. The category names follow it in the same system prompt,
# so the whole of it is the same for every batch of one user's.
SYSTEM_PROMPT = (
    "You sort a person's bank transactions into their own budget categories.\n"
    "Below are the names of their categories for money going out and for money coming in. "
    "Then, one per line, the transactions to sort: index | direction | amount | description. "
    "Bank descriptions are terse - abbreviated merchant names, reference numbers, city names.\n\n"
    "Answer with a JSON array and nothing else, one object per transaction you are reasonably "
    "sure about: {\"i\": <index>, \"category\": \"<name exactly as listed>\", "
    "\"confidence\": \"high\" | \"medium\" | \"low\"}. Use the names exactly as given, from the "
    "list for the transaction's direction. Leave out any transaction you are not reasonably "
    "sure about. Never invent a category."
)

CONFIDENCES = ('high', 'medium', 'low')


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


def _parse_answer(text: str) -> List[Dict[str, Any]]:
    """
    The JSON array out of an answer, forgiving the wrapping a model adds -
    a code fence, a sentence before it. Anything that is not a list of
    objects is nothing at all.
    """
    raw = (text or '').strip()
    if '```' in raw:
        inner = raw.split('```')
        raw = max(inner, key=len).strip()
        if raw.lower().startswith('json'):
            raw = raw[4:].strip()
    start, end = raw.find('['), raw.rfind(']')
    if start < 0 or end <= start:
        return []
    try:
        parsed = json.loads(raw[start:end + 1])
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return [p for p in parsed if isinstance(p, dict)]


def _record_error(user_id: int, message: str) -> None:
    """The panel shows this. The fingerprint stays: a failed batch is not an unverified key."""
    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            cursor.execute("UPDATE user_ai_settings SET last_error = %s WHERE user_id = %s",
                           ((message or '')[:255], user_id))
    except Exception as e:
        log_warning(logger, 'AI', f'Could not record the error for user {user_id}: {e}')


def _category_options(user_id: int) -> Dict[str, List[Dict[str, Any]]]:
    """The user's own categories by direction, for a one-off suggest_category call."""
    out: Dict[str, List[Dict[str, Any]]] = {'outgoing': [], 'incoming': []}
    tables = {'outgoing': 'expense_categories', 'incoming': 'income_categories'}
    for direction, table in tables.items():
        rows = None
        try:
            import redis_manager
            rows = redis_manager.get_table_cache(table, user_id)
        except Exception:
            rows = None
        if rows is None:
            try:
                with get_db_pool().get_cursor(dictionary=True) as cursor:
                    cursor.execute(f"SELECT id, name, hidden FROM {table} WHERE user_id = %s", (user_id,))
                    rows = cursor.fetchall()
            except Exception as e:
                log_warning(logger, 'AI', f'Could not read {table} for user {user_id}: {e}')
                rows = []
        for r in rows or []:
            if r.get('id') is None or int(r.get('hidden') or 0):
                continue
            out[direction].append({'id': int(r['id']), 'name': r.get('name') or ''})
    return out


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
        ok, text, _status = ClaudeEnrichmentProvider._messages(
            api_key, model, None, 'Reply with the single word OK.', max_tokens=8)
        if ok:
            return (True, 'The key works. AI categorization can be turned on.')
        return (False, text)

    @staticmethod
    def _messages(api_key: str, model: str, system: Optional[str], user_text: str,
                  max_tokens: int = 4000) -> Tuple[bool, str, Optional[int]]:
        """
        One Messages request. Returns (ok, text, status): the answer's text
        when ok, a sentence for the person when not.

        Raw HTTP rather than the SDK, as the key test has been since it was
        written - one endpoint, one shape, and no dependency to ship. Rate
        limits and overloads get one retry after a pause; anything else is
        reported as it is.
        """
        payload: Dict[str, Any] = {
            'model': model,
            'max_tokens': max_tokens,
            'messages': [{'role': 'user', 'content': user_text}],
        }
        if system:
            payload['system'] = system
        # Sorting is not thinking work. Haiku takes no effort setting.
        if not model.startswith('claude-haiku'):
            payload['output_config'] = {'effort': 'low'}
        headers = {'x-api-key': api_key, 'anthropic-version': API_VERSION, 'content-type': 'application/json'}
        resp = None
        for attempt in (1, 2):
            try:
                with httpx.Client(timeout=_TIMEOUT) as client:
                    resp = client.post(API_URL, json=payload, headers=headers)
            except httpx.HTTPError as e:
                if attempt == 1:
                    time.sleep(2)
                    continue
                return (False, f'Could not reach Anthropic ({type(e).__name__}). Try again.', None)
            if resp.status_code in (429, 529, 503) and attempt == 1:
                time.sleep(2)
                continue
            break
        if resp is None:
            return (False, 'Could not reach Anthropic. Try again.', None)
        if resp.status_code == 200:
            try:
                body = resp.json()
            except ValueError:
                return (False, 'Anthropic sent an unreadable answer.', 200)
            if body.get('stop_reason') == 'refusal':
                return (False, 'Anthropic declined to answer this request.', 200)
            text = ''.join((b.get('text') or '') for b in (body.get('content') or []) if b.get('type') == 'text')
            return (True, text, 200)
        detail = ''
        try:
            detail = (resp.json().get('error') or {}).get('message') or ''
        except ValueError:
            pass
        if resp.status_code == 401:
            return (False, 'Anthropic rejected the key. Check it was copied whole, or create a new one.', 401)
        if resp.status_code == 403:
            return (False, 'Anthropic refused the request for this key. Check the key\'s permissions on platform.claude.com.', 403)
        if resp.status_code == 404 or 'model' in detail.lower():
            return (False, f'That model is not available to this key ({detail or "not found"}). Try the other model.', resp.status_code)
        if resp.status_code == 400 and 'credit' in detail.lower():
            return (False, 'The key works but the account has no credit. Add billing on platform.claude.com.', 400)
        if resp.status_code == 429:
            return (False, 'Anthropic is rate-limiting this key right now. Try again in a minute.', 429)
        if resp.status_code in (529, 503):
            return (False, 'Anthropic is overloaded at the moment. Try again shortly.', resp.status_code)
        return (False, f'Anthropic answered with status {resp.status_code}{": " + detail if detail else ""}.', resp.status_code)

    # ------------------------------------------------------- the sorting

    def suggest_categories_batch(self, user_id: int, items: List[Dict[str, Any]],
                                 options: Dict[str, List[Dict[str, Any]]]) -> Dict[int, Dict[str, Any]]:
        """
        Sort many transactions in one request (BATCH_SIZE per request).

        items:   [{'i', 'direction': 'outgoing'|'incoming', 'amount', 'description'}]
        options: {'outgoing': [{'id', 'name'}], 'incoming': [{'id', 'name'}]} -
                 the names the answer may use, and the ids they stand for.

        Returns {i: {'category_id', 'category_type', 'category_name',
        'confidence'}} for the transactions answered with a listed name; the
        rest are simply absent. Nothing here when the gate is closed. A
        failed request is logged and shown on the panel, and the batches
        answered so far are kept.
        """
        if not items or not self.is_active(user_id):
            return {}
        cfg = self.get_config(user_id)
        if not cfg['api_key'] or not cfg['verified']:
            return {}
        by_name = {d: {(o.get('name') or '').strip().lower(): o for o in (options.get(d) or []) if o.get('name')}
                   for d in ('outgoing', 'incoming')}
        system = (SYSTEM_PROMPT
                  + '\n\nCategories for money going out:\n'
                  + '\n'.join('- ' + o['name'] for o in (options.get('outgoing') or []))
                  + '\n\nCategories for money coming in:\n'
                  + '\n'.join('- ' + o['name'] for o in (options.get('incoming') or [])))
        out: Dict[int, Dict[str, Any]] = {}
        for start in range(0, len(items), BATCH_SIZE):
            chunk = items[start:start + BATCH_SIZE]
            by_index = {int(it['i']): it for it in chunk}
            lines = []
            for it in chunk:
                direction = 'in' if it.get('direction') == 'incoming' else 'out'
                description = (it.get('description') or '').replace('\n', ' ').strip()[:120]
                lines.append(f"{int(it['i'])} | {direction} | {float(it.get('amount') or 0):.2f} | {description}")
            ok, answer, status = self._messages(cfg['api_key'], cfg['model'], system, '\n'.join(lines))
            if not ok:
                log_warning(logger, 'AI', f'user {user_id}: categorisation request failed ({status}): {answer}')
                _record_error(user_id, answer)
                return out
            for parsed in _parse_answer(answer):
                try:
                    i = int(parsed.get('i'))
                except (TypeError, ValueError):
                    continue
                item = by_index.get(i)
                if item is None or i in out:
                    # Not asked about, or already answered: the first answer stands.
                    continue
                name = str(parsed.get('category') or '').strip().lower()
                option = by_name.get(item.get('direction') or 'outgoing', {}).get(name)
                if not option:
                    continue
                confidence = str(parsed.get('confidence') or 'medium').strip().lower()
                if confidence not in CONFIDENCES:
                    confidence = 'medium'
                out[i] = {'category_id': int(option['id']), 'category_type': item.get('direction'),
                          'category_name': option['name'], 'confidence': confidence}
        log_info(logger, 'AI', f'user {user_id}: {len(out)} of {len(items)} transaction(s) sorted by Claude')
        return out

    # ------------------------------------------------------- the contract

    def sync_categories(self, user_id: int) -> bool:
        # Categories travel with each request; nothing to push.
        return False

    def enrich(self, user_id: int, transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # The category is the enrichment, and the importer asks for it in
        # batches through suggest_categories_batch. Returning the input,
        # never an empty list - see the null provider for why.
        return transactions

    def suggest_category(self, user_id: int, transaction: Dict[str, Any],
                         account_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """One transaction, against the user's own categories. The batch of one."""
        direction = 'incoming' if transaction.get('transaction_type') == 'income' else 'outgoing'
        item = {'i': 0, 'direction': direction, 'amount': abs(float(transaction.get('amount') or 0)),
                'description': transaction.get('description') or transaction.get('merchant_name') or ''}
        return self.suggest_categories_batch(user_id, [item], _category_options(user_id)).get(0)

    def recurrence_map(self, user_id: int) -> Dict[str, Any]:
        return {}

    def delete_user_data(self, user_id: int) -> bool:
        ok, _ = self.clear_key(user_id)
        return ok
