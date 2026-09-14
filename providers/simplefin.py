"""
SimpleFIN Bridge as the bank provider.

SimpleFIN is unlike the aggregators this interface was written against, and
the differences decide the shape of everything below:

  * The USER has the account with SimpleFIN, not us. They sign up at the
    Bridge, pay SimpleFIN, link their banks there, and create an "App"
    connection that yields a one-time Setup Token. There is no widget, no
    redirect and no callback: the protocol says the user "returns to your app
    with a SimpleFIN Token in their clipboard; provide a location for them to
    paste it". We decode that token, POST once to the claim URL inside it, and
    receive an Access URL with Basic-Auth credentials embedded. That URL is the
    only secret, it is per user, and it lives here - Fernet-encrypted in
    simplefin_credentials, never in Redis and never in a log line.

  * It is PULL only. GET {access_url}/accounts, no webhooks, and the Bridge
    refreshes each bank about once a day. The developer guide expects "24
    requests or fewer per day" per access URL, warns in the response's errlist
    when that is exceeded, and disables the token if it keeps happening. So
    every request goes through one method that counts against a daily ledger
    and refuses at a soft ceiling below the Bridge's, leaving room for
    retries.

  * It gives NO account type. Only a name, a currency and balances. Blankee
    routes on account type (depository vs credit, checking vs savings), so the
    type is the user's to choose at link time; this module only guesses from
    the name to pre-select the choice.

Protocol v2 is requested explicitly (version=2): structured errors in errlist,
and a connections array with the institution name.

Everything here is keyed on Blankee's user_id, as providers/base.py requires.
The provider_ref recorded in linked_provider_profiles is a hash of the access
URL, never the URL.
"""

import base64
import hashlib
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

import httpx

from db_connections import get_db_pool
from log_config import get_logger, log_info, log_error, log_warning, log_exception
from providers.base import BankProvider

logger = get_logger(__name__)

# Where we send people. The create URL is the one the developer guide names;
# it is what the "Connect SimpleFIN" button opens in a new tab.
CREATE_URL = 'https://bridge.simplefin.org/simplefin/create'
SIGNUP_URL = 'https://beta-bridge.simplefin.org/'

# The Bridge's own expectation is 24 a day with "a little leeway". Stopping at
# 20 keeps the leeway for retries after a transient failure rather than
# spending it on a busy day.
DAILY_SOFT_CEILING = 20

# A batch of accounts can take minutes on the Bridge's side; it may answer
# with a redirect after ~80 s that the client must follow to re-attach to the
# running job. Long read timeout, short connect timeout, redirects on.
_TIMEOUT = httpx.Timeout(300.0, connect=15.0)

# The window SimpleFIN allows per request. Callers pass narrower ones; this is
# the clamp.
MAX_WINDOW_DAYS = 90

_CREDIT_WORDS = re.compile(r'\b(credit|card|visa|mastercard|master card|amex|american express|discover)\b', re.I)
_SAVINGS_WORDS = re.compile(r'\b(saving|savings|money market|mma)\b', re.I)
_MASK = re.compile(r'(\d{4})\s*$')


def guess_subtype(account_name: str) -> str:
    """
    checking / savings / credit_card from the account's name.

    A pre-selection for the user, never a decision: SimpleFIN says nothing
    about type, and "Everyday" tells us nothing either. Unknown means
    checking, the most common answer and the safest one to be wrong about
    (a mis-typed savings account still imports; a mis-typed card would route
    purchases as bank withdrawals).
    """
    name = account_name or ''
    if _CREDIT_WORDS.search(name):
        return 'credit_card'
    if _SAVINGS_WORDS.search(name):
        return 'savings'
    return 'checking'


def account_type_for(subtype: str) -> str:
    """The coarse type the rest of the app routes on."""
    return 'CREDIT' if subtype == 'credit_card' else 'DEPOSITORY'


def mask_from_name(account_name: str) -> str:
    """The last four digits if the bank put them in the name, else ''."""
    m = _MASK.search(account_name or '')
    return m.group(1) if m else ''


def _fernet():
    # instance_settings owns the key handling (and its None-on-missing rule).
    from instance_settings import _fernet as f
    return f()


def _decimal(value) -> Optional[float]:
    if value is None or value == '':
        return None
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def _epoch_int(epoch) -> Optional[int]:
    """The bank's epoch as an int, or None for anything that is not one."""
    try:
        epoch = int(epoch)
    except (TypeError, ValueError):
        return None
    return epoch if epoch > 0 else None


def _epoch_to_date(epoch) -> Optional[str]:
    epoch = _epoch_int(epoch)
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime('%Y-%m-%d')


class SimpleFINError(Exception):
    """A failure the user can act on. `code` is short and stable; `message` is
    for them."""

    def __init__(self, code: str, message: str, http_status: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class SimpleFINBankProvider(BankProvider):
    name = 'simplefin'

    # ------------------------------------------------------------- readiness

    def is_configured(self) -> bool:
        # Selected means usable: credentials are per user, checked per call.
        return True

    def _row(self, user_id: int) -> Optional[Dict[str, Any]]:
        try:
            with get_db_pool().get_cursor(dictionary=True) as cursor:
                cursor.execute(
                    "SELECT user_id, access_url_encrypted, claimed_at, last_pull_at, "
                    "last_pull_ok, last_error_code, last_error_msg, pulls_day, "
                    "pulls_today, quota_warned_at "
                    "FROM simplefin_credentials WHERE user_id = %s", (user_id,))
                return cursor.fetchone()
        except Exception as e:
            log_warning(logger, 'SIMPLEFIN', f'Could not read simplefin_credentials for user {user_id}: {e}')
            return None

    def has_credentials(self, user_id: int) -> bool:
        row = self._row(user_id)
        return bool(row and row.get('access_url_encrypted'))

    def _access_url(self, user_id: int) -> Optional[str]:
        row = self._row(user_id)
        token = (row or {}).get('access_url_encrypted')
        if not token:
            return None
        f = _fernet()
        if f is None:
            log_error(logger, 'SIMPLEFIN',
                      'An access URL is stored but SETTINGS_ENCRYPTION_KEY is missing or invalid; '
                      'the bank connection is unusable until the key is restored.')
            return None
        try:
            return f.decrypt(token.encode('utf-8')).decode('utf-8')
        except Exception as e:
            log_error(logger, 'SIMPLEFIN',
                      f'Stored access URL could not be decrypted ({type(e).__name__}); '
                      f'the encryption key may have changed.')
            return None

    def status(self, user_id: int) -> Dict[str, Any]:
        """What the pages show: connected, quota used, last pull, last error."""
        row = self._row(user_id) or {}
        today = date.today()
        pulls_today = int(row.get('pulls_today') or 0) if row.get('pulls_day') == today else 0
        code = row.get('last_error_code') or ''
        chosen = False
        try:
            from bank_redis import _get_all_linked_accounts_raw
            chosen = any(int(a.get('is_active', 1) or 0) == 1 for a in (_get_all_linked_accounts_raw(user_id) or []))
        except Exception:
            pass
        return {
            'connected': bool(row.get('access_url_encrypted')),
            # Connected but nothing chosen: the pages load the account list
            # by themselves so the person can pick up where they left off.
            'accounts_chosen': chosen,
            'claimed_at': row.get('claimed_at').isoformat() if row.get('claimed_at') else None,
            'last_pull_at': row.get('last_pull_at').isoformat() if row.get('last_pull_at') else None,
            'last_pull_ok': row.get('last_pull_ok'),
            'last_error_code': code,
            'last_error_msg': row.get('last_error_msg') or '',
            'pulls_today': pulls_today,
            'daily_ceiling': DAILY_SOFT_CEILING,
            # The access URL itself was refused: only a fresh Setup Token fixes
            # that, and the page has to say so rather than offer "Sync now".
            'needs_new_token': code in ('gen.auth', 'http.403'),
            'encryption_available': _fernet() is not None,
        }

    def connect_widget_config(self, user_id: int) -> Optional[Dict[str, Any]]:
        st = self.status(user_id)
        return {
            'mode': 'setup_token',
            'provider': self.name,
            'create_url': CREATE_URL,
            'signup_url': SIGNUP_URL,
            **st,
        }

    # ---------------------------------------------------------------- claim

    @staticmethod
    def decode_setup_token(token: str) -> str:
        """The claim URL inside a Setup Token, or a SimpleFINError."""
        raw = (token or '').strip()
        # People paste with surrounding quotes, or the token wrapped by an
        # email client; strip what cannot be part of base64.
        raw = raw.strip('"\'').replace('\n', '').replace('\r', '').replace(' ', '')
        if not raw:
            raise SimpleFINError('empty', 'Paste the Setup Token from SimpleFIN first.')
        try:
            padded = raw + '=' * (-len(raw) % 4)
            url = base64.b64decode(padded, validate=False).decode('utf-8').strip()
        except Exception:
            raise SimpleFINError('invalid', 'That does not look like a SimpleFIN Setup Token. '
                                            'Copy the whole token from the Bridge and paste it again.')
        if not url.lower().startswith('https://') or '/claim/' not in url:
            raise SimpleFINError('invalid', 'That does not look like a SimpleFIN Setup Token. '
                                            'Copy the whole token from the Bridge and paste it again.')
        return url

    def claim_setup_token(self, user_id: int, token: str) -> Tuple[bool, str]:
        """
        Turn a Setup Token into stored credentials. Returns (ok, message).

        One POST, once: the Bridge answers 403 to a second claim of the same
        token, which is the whole security model of the token - it is spent
        the moment it is used.
        """
        f = _fernet()
        if f is None:
            return (False, 'The server has no SETTINGS_ENCRYPTION_KEY, so the bank connection '
                           'cannot be stored. Ask the administrator to set one.')
        try:
            claim_url = self.decode_setup_token(token)
        except SimpleFINError as e:
            return (False, e.message)

        try:
            with httpx.Client(timeout=httpx.Timeout(30.0, connect=15.0), follow_redirects=True) as client:
                resp = client.post(claim_url, content=b'')
        except httpx.HTTPError as e:
            log_warning(logger, 'SIMPLEFIN', f'Claim request failed for user {user_id}: {type(e).__name__}')
            return (False, 'Could not reach SimpleFIN. Check the connection and try again.')

        if resp.status_code == 403:
            return (False, 'SimpleFIN refused that token: it has already been used or has expired. '
                           'Each Setup Token works once - create a new one on the Bridge and paste it here.')
        if resp.status_code == 402:
            return (False, 'SimpleFIN says a subscription is required. Subscribe on the Bridge, '
                           'then create a new Setup Token.')
        if resp.status_code != 200:
            log_warning(logger, 'SIMPLEFIN', f'Claim returned HTTP {resp.status_code} for user {user_id}')
            return (False, f'SimpleFIN answered with an unexpected status ({resp.status_code}). Try again later.')

        access_url = resp.text.strip()
        if not access_url.lower().startswith('https://') or '@' not in access_url:
            log_warning(logger, 'SIMPLEFIN', f'Claim returned something that is not an access URL for user {user_id}')
            return (False, 'SimpleFIN answered, but not with an access URL. Try a new Setup Token.')

        encrypted = f.encrypt(access_url.encode('utf-8')).decode('utf-8')
        provider_ref = hashlib.sha256(access_url.encode('utf-8')).hexdigest()
        try:
            with get_db_pool().get_cursor(commit=True) as cursor:
                cursor.execute(
                    "INSERT INTO simplefin_credentials "
                    "(user_id, access_url_encrypted, claimed_at, last_error_code, last_error_msg, "
                    " pulls_day, pulls_today) "
                    "VALUES (%s, %s, NOW(), NULL, NULL, CURDATE(), 0) "
                    "ON DUPLICATE KEY UPDATE access_url_encrypted = VALUES(access_url_encrypted), "
                    " claimed_at = NOW(), last_error_code = NULL, last_error_msg = NULL",
                    (user_id, encrypted))
        except Exception as e:
            log_error(logger, 'SIMPLEFIN', f'Could not store credentials for user {user_id}: {e}')
            return (False, 'The token was accepted but could not be stored. Try again.')

        # The profile row, in MySQL at once and in the Redis-first cache: the
        # last-connection cleanup in bank_redis reads MySQL, and a disconnect
        # seconds after connecting must find the row there, not wait for the
        # periodic flush.
        metadata = json.dumps({'host': httpx.URL(access_url).host})
        try:
            with get_db_pool().get_cursor(commit=True) as cursor:
                cursor.execute(
                    "INSERT INTO linked_provider_profiles (user_id, provider, provider_ref, metadata) "
                    "VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE provider = VALUES(provider), "
                    "provider_ref = VALUES(provider_ref), metadata = VALUES(metadata)",
                    (user_id, self.name, provider_ref, metadata))
        except Exception as e:
            log_warning(logger, 'SIMPLEFIN', f'Could not record provider profile row for user {user_id}: {e}')
        try:
            from bank_redis import update_provider_profile
            update_provider_profile({'provider': self.name, 'provider_ref': provider_ref, 'metadata': metadata}, user_id)
        except Exception as e:
            # The credential is stored; the cache copy is bookkeeping.
            log_warning(logger, 'SIMPLEFIN', f'Could not cache provider profile for user {user_id}: {e}')

        log_info(logger, 'SIMPLEFIN', f'Access URL claimed and stored for user {user_id}')
        return (True, 'Connected to SimpleFIN.')

    # --------------------------------------------------------------- the call

    def _ledger(self, user_id: int, ok: bool, error_code: Optional[str] = None,
                error_msg: Optional[str] = None, counted: bool = True):
        """Record one request against today's budget and its outcome."""
        try:
            with get_db_pool().get_cursor(commit=True) as cursor:
                cursor.execute(
                    "UPDATE simplefin_credentials SET "
                    "  pulls_today = IF(pulls_day = CURDATE(), pulls_today, 0) + %s, "
                    "  pulls_day = CURDATE(), "
                    "  last_pull_at = NOW(), last_pull_ok = %s, "
                    "  last_error_code = %s, last_error_msg = %s "
                    "WHERE user_id = %s",
                    (1 if counted else 0, 1 if ok else 0, error_code, (error_msg or '')[:500], user_id))
        except Exception as e:
            log_warning(logger, 'SIMPLEFIN', f'Could not update the request ledger for user {user_id}: {e}')

    def _get(self, user_id: int, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        GET /accounts with the user's credentials. Returns the parsed body.

        Raises SimpleFINError for anything the caller (and the user) should
        hear about. Never logs the access URL.
        """
        row = self._row(user_id)
        if not row or not row.get('access_url_encrypted'):
            raise SimpleFINError('no_credentials', 'No SimpleFIN connection yet. Paste a Setup Token first.')
        pulls_today = int(row.get('pulls_today') or 0) if row.get('pulls_day') == date.today() else 0
        if pulls_today >= DAILY_SOFT_CEILING:
            raise SimpleFINError('quota', f'SimpleFIN allows about 24 pulls a day and {pulls_today} have been '
                                          f'used today. Try again tomorrow.')
        access_url = self._access_url(user_id)
        if not access_url:
            raise SimpleFINError('undecryptable', 'The stored SimpleFIN connection cannot be read on this '
                                                  'server (encryption key problem). Paste a new Setup Token.')

        url = httpx.URL(access_url.rstrip('/') + '/accounts')
        auth = (url.username or '', url.password or '')
        bare = url.copy_with(username=None, password=None)
        query = {'version': '2', **{k: v for k, v in params.items() if v is not None}}
        try:
            with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, auth=auth) as client:
                resp = client.get(bare, params=query)
        except httpx.HTTPError as e:
            self._ledger(user_id, ok=False, error_code='network', error_msg=type(e).__name__)
            raise SimpleFINError('network', 'Could not reach SimpleFIN. Try again in a few minutes.')

        if resp.status_code == 403:
            self._ledger(user_id, ok=False, error_code='http.403', error_msg='access URL refused')
            raise SimpleFINError('http.403', 'SimpleFIN no longer accepts this connection. Create a new '
                                             'Setup Token on the Bridge and paste it under "Replace token".', 403)
        if resp.status_code == 402:
            self._ledger(user_id, ok=False, error_code='http.402', error_msg='payment required')
            raise SimpleFINError('http.402', 'SimpleFIN says the subscription has lapsed. Renew it on the '
                                             'Bridge, then try again.', 402)
        if resp.status_code != 200:
            self._ledger(user_id, ok=False, error_code=f'http.{resp.status_code}', error_msg=resp.text[:200])
            raise SimpleFINError('http', f'SimpleFIN answered with status {resp.status_code}. Try again later.',
                                 resp.status_code)
        try:
            body = resp.json()
        except ValueError:
            self._ledger(user_id, ok=False, error_code='badjson', error_msg='response was not JSON')
            raise SimpleFINError('badjson', 'SimpleFIN sent an unreadable answer. Try again later.')

        errors = body.get('errlist') or body.get('errors') or []
        # v1 servers send errors as plain strings; v2 as objects. Normalise.
        norm = []
        for err in errors:
            if isinstance(err, dict):
                norm.append({'code': str(err.get('code') or 'gen.'), 'msg': str(err.get('msg') or ''),
                             'conn_id': err.get('conn_id'), 'account_id': err.get('account_id')})
            else:
                norm.append({'code': 'gen.', 'msg': str(err), 'conn_id': None, 'account_id': None})
        body['errlist'] = norm

        fatal = next((e for e in norm if e['code'] == 'gen.auth'), None)
        if fatal:
            self._ledger(user_id, ok=False, error_code='gen.auth', error_msg=fatal['msg'])
            raise SimpleFINError('gen.auth', 'SimpleFIN no longer accepts this connection. Create a new Setup '
                                             'Token on the Bridge and paste it under "Replace token".')
        # Anything else is per-connection or per-account and is reported
        # alongside the data; the caller decides what to do with it.
        worst = norm[0] if norm else None
        self._ledger(user_id, ok=not worst, error_code=worst['code'] if worst else None,
                     error_msg=worst['msg'] if worst else None)
        if any(('rate' in e['msg'].lower() or 'too many' in e['msg'].lower()) for e in norm):
            try:
                with get_db_pool().get_cursor(commit=True) as cursor:
                    cursor.execute("UPDATE simplefin_credentials SET quota_warned_at = NOW() WHERE user_id = %s",
                                   (user_id,))
            except Exception:
                pass
        return body

    # ------------------------------------------------------------ overview

    def fetch_overview(self, user_id: int) -> Dict[str, Any]:
        """
        Connections and accounts with balances, no transactions (balances-only
        is one request and the cheapest thing the Bridge does). What the link
        screen shows, and what the bank page refreshes from.
        """
        body = self._get(user_id, {'balances-only': '1'})
        connections, accounts = self._parse_accounts(body)
        return {'connections': connections, 'accounts': accounts,
                'errors': body.get('errlist') or []}

    @staticmethod
    def _parse_accounts(body: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        (connections, accounts) from one /accounts body, in the shapes the
        pages use. The same body carries transactions when they were asked
        for, so an overview and a transaction pull parse their accounts here
        alike.
        """
        connections = {}
        for c in body.get('connections') or []:
            cid = str(c.get('conn_id') or '')
            if not cid:
                continue
            connections[cid] = {
                'connection_id': cid,
                'institution_name': c.get('name') or c.get('org_url') or 'Bank',
                'institution_id': c.get('org_id') or '',
                'status': 'ACTIVE',
            }
        accounts = []
        for a in body.get('accounts') or []:
            aid = str(a.get('id') or '')
            if not aid:
                continue
            cid = str(a.get('conn_id') or '')
            if cid and cid not in connections:
                # A v1 server, or an account whose connection was not listed:
                # still show it, under a connection named after the org.
                org = a.get('org') or {}
                connections[cid] = {
                    'connection_id': cid,
                    'institution_name': org.get('name') or org.get('domain') or 'Bank',
                    'institution_id': org.get('sfin-url') or '',
                    'status': 'ACTIVE',
                }
            elif not cid:
                org = a.get('org') or {}
                cid = 'org:' + (org.get('domain') or org.get('name') or 'unknown')
                connections.setdefault(cid, {
                    'connection_id': cid,
                    'institution_name': org.get('name') or org.get('domain') or 'Bank',
                    'institution_id': org.get('sfin-url') or '',
                    'status': 'ACTIVE',
                })
            name = a.get('name') or 'Account'
            accounts.append({
                'account_id': aid,
                'connection_id': cid,
                'account_name': name,
                'currency': a.get('currency') or 'USD',
                'current_balance': _decimal(a.get('balance')),
                'available_balance': _decimal(a.get('available-balance')),
                'balance_date': _epoch_to_date(a.get('balance-date')),
                'guessed_subtype': guess_subtype(name),
                'mask': mask_from_name(name),
            })
        # Per-connection / per-account errors travel with the data.
        for err in body.get('errlist') or []:
            if err.get('conn_id') and err['conn_id'] in connections:
                connections[err['conn_id']]['status'] = 'ERROR_REPAIRABLE' if err['code'] == 'con.auth' else 'ERROR'
                connections[err['conn_id']]['error_msg'] = err['msg']
        return list(connections.values()), accounts

    def list_connections(self, user_id: int) -> List[Dict[str, Any]]:
        try:
            return self.fetch_overview(user_id)['connections']
        except SimpleFINError as e:
            log_warning(logger, 'SIMPLEFIN', f'list_connections for user {user_id}: {e.code}')
            return []

    def list_accounts(self, user_id: int) -> List[Dict[str, Any]]:
        try:
            return self.fetch_overview(user_id)['accounts']
        except SimpleFINError as e:
            log_warning(logger, 'SIMPLEFIN', f'list_accounts for user {user_id}: {e.code}')
            return []

    # ----------------------------------------------------------- transactions

    def fetch_transactions(self, user_id: int, start: Optional[str] = None,
                           end: Optional[str] = None,
                           account_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Transactions in the normalized shape from providers/base.py."""
        return self.fetch_transactions_and_balances(user_id, start, end, account_id)['transactions']

    def fetch_transactions_and_balances(self, user_id: int, start: Optional[str] = None,
                                        end: Optional[str] = None,
                                        account_id: Optional[str] = None) -> Dict[str, Any]:
        """
        One request, two answers: the transactions in the window, and the
        accounts with their balances as of the same moment. The Bridge sends
        both in one body, the daily pull needs both, and a request is a
        twentieth of the day's budget - asking twice would be paying twice.

        {'transactions': [normalized, plus 'posted_at' epoch when the bank
        gave one], 'connections': [...], 'accounts': [...] as fetch_overview
        returns them, 'errors': errlist}. Windows are clamped to SimpleFIN's
        90 days; pending transactions are requested and flagged.
        """
        try:
            start_d = datetime.strptime(start, '%Y-%m-%d').date() if start else None
            end_d = datetime.strptime(end, '%Y-%m-%d').date() if end else date.today()
        except ValueError:
            raise SimpleFINError('badwindow', 'Dates must be YYYY-MM-DD.')
        if start_d and (end_d - start_d).days > MAX_WINDOW_DAYS:
            start_d = date.fromordinal(end_d.toordinal() - MAX_WINDOW_DAYS)
        params: Dict[str, Any] = {'pending': '1'}
        if start_d:
            params['start-date'] = str(int(datetime(start_d.year, start_d.month, start_d.day,
                                                    tzinfo=timezone.utc).timestamp()))
        # end-date is exclusive on the Bridge; ask through the end of the day.
        params['end-date'] = str(int(datetime(end_d.year, end_d.month, end_d.day,
                                              tzinfo=timezone.utc).timestamp()) + 86400)
        if account_id:
            params['account'] = account_id
        body = self._get(user_id, params)
        out: List[Dict[str, Any]] = []
        for a in body.get('accounts') or []:
            aid = str(a.get('id') or '')
            for t in a.get('transactions') or []:
                amount = _decimal(t.get('amount'))
                if amount is None:
                    continue
                pending = bool(t.get('pending')) or not t.get('posted')
                posted = _epoch_to_date(t.get('posted'))
                transacted = _epoch_to_date(t.get('transacted_at'))
                txn_date = posted or transacted
                if not txn_date:
                    continue
                out.append({
                    'provider_txn_id': str(t.get('id')),
                    'account_ref': aid,
                    'amount': amount,                       # SimpleFIN: positive = deposit
                    'date': txn_date,
                    'description': (t.get('description') or '').strip(),
                    'merchant_name': (t.get('payee') or '').strip(),
                    'category': '',
                    'pending': pending,
                    'transaction_type': 'expense' if amount < 0 else 'income',
                    'provider_created_at': transacted or posted,
                    'posted_at': _epoch_int(t.get('posted')),
                    'enrichment': {},
                })
        connections, accounts = self._parse_accounts(body)
        return {'transactions': out, 'connections': connections, 'accounts': accounts,
                'errors': body.get('errlist') or []}

    def fetch_account_balances(self, user_id: int) -> List[Dict[str, Any]]:
        try:
            overview = self.fetch_overview(user_id)
        except SimpleFINError as e:
            log_warning(logger, 'SIMPLEFIN', f'fetch_account_balances for user {user_id}: {e.code}')
            return []
        # Type and mask are the user's classification, stored on the linked
        # account; the provider does not know them.
        try:
            from bank_redis import get_linked_accounts
            stored = {a.get('account_id'): a for a in get_linked_accounts(user_id)}
        except Exception:
            stored = {}
        out = []
        for a in overview['accounts']:
            s = stored.get(a['account_id']) or {}
            out.append({
                'account_ref': a['account_id'],
                'account_type': s.get('account_type') or account_type_for(a['guessed_subtype']),
                'account_subtype': s.get('account_subtype') or a['guessed_subtype'],
                'current_balance': a['current_balance'],
                'available_balance': a['available_balance'],
                'mask': s.get('mask') or a['mask'],
            })
        return out

    # ----------------------------------------------------------- lifecycle

    def disconnect(self, user_id: int, connection_id: str) -> bool:
        """
        Forget one connection locally. SimpleFIN has no API for removing a
        connection; the user does that on the Bridge, and the page says so.
        """
        try:
            from bank_redis import delete_linked_connection
            return bool(delete_linked_connection(connection_id, user_id))
        except Exception as e:
            log_exception(logger, 'SIMPLEFIN', f'disconnect failed for user {user_id}: {e}')
            return False

    def delete_user(self, user_id: int) -> bool:
        try:
            with get_db_pool().get_cursor(commit=True) as cursor:
                cursor.execute("DELETE FROM simplefin_credentials WHERE user_id = %s", (user_id,))
            log_info(logger, 'SIMPLEFIN', f'Credentials removed for user {user_id}')
            return True
        except Exception as e:
            log_error(logger, 'SIMPLEFIN', f'Could not remove credentials for user {user_id}: {e}')
            return False

    def forget_credentials(self, user_id: int) -> bool:
        """Drop the access URL but keep nothing else - used by "disconnect all"."""
        return self.delete_user(user_id)

    def verify_webhook(self, headers: Dict[str, str], raw_body: bytes) -> bool:
        # SimpleFIN is pull-based; there is no legitimate sender.
        return False
