"""
Blankee push relay.

Apple only delivers a push to an app from a key tied to the account that
signed the app, so a self-hosted Blankee server cannot reach the phones of its
own users by itself: the key is the app maker's, and handing it out would let
anyone who held it push anything to everyone. This relay holds that one key,
and forwards a nudge on a server's behalf.

It is built to know as little as possible:

- The phone registers its own device token here, with a secret it made up.
  The relay stores the secret's hash, never the secret.
- A server asks for a push with the token, the same secret (the phone gave it
  to the server too), and the id of the notification. If the secret matches,
  Apple is told to wake the phone with that id and nothing else. No text, no
  amounts, no server address ever pass through here.
- The phone's notification service extension then fetches the notification
  from the person's own server, using its own credentials, and fills in the
  alert before it is shown.

So a stranger cannot push to a phone without both its token and its secret,
the relay cannot read anyone's finances, and losing the relay's database
costs nothing that the next app launch does not put back.

Settings, all from the environment (see install.sh):
    APNS_KEY_PATH, APNS_KEY_ID, APNS_TEAM_ID, APNS_TOPIC   the app maker's key
    RELAY_DB           SQLite file (default /var/lib/blankee-relay/relay.db)
    RELAY_HOURLY_LIMIT pushes per token per hour (default 60)
"""

import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time

import httpx
import jwt
from flask import Flask, jsonify, request

app = Flask(__name__)

DB_PATH = os.environ.get('RELAY_DB', '/var/lib/blankee-relay/relay.db')
HOURLY_LIMIT = int(os.environ.get('RELAY_HOURLY_LIMIT', '60'))
TOKEN_RE = re.compile(r'^[0-9a-f]{64}$')
ENVIRONMENTS = ('sandbox', 'production')
HOSTS = {'sandbox': 'api.sandbox.push.apple.com', 'production': 'api.push.apple.com'}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

_db_lock = threading.Lock()


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                token        TEXT PRIMARY KEY,
                secret_hash  TEXT NOT NULL,
                environment  TEXT NOT NULL,
                created_at   INTEGER NOT NULL,
                last_push_at INTEGER,
                window_start INTEGER NOT NULL DEFAULT 0,
                window_count INTEGER NOT NULL DEFAULT 0
            )
        """)


def _hash(secret):
    return hashlib.sha256(secret.encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# APNs
# ---------------------------------------------------------------------------

_apns = {'client': None, 'key': None, 'jwt': None, 'jwt_at': 0.0}
_apns_lock = threading.Lock()
_JWT_REFRESH_AFTER = 50 * 60


def _apns_ready():
    if _apns['client'] is not None:
        return True
    path, key_id, team, topic = (os.environ.get(k) for k in
                                 ('APNS_KEY_PATH', 'APNS_KEY_ID', 'APNS_TEAM_ID', 'APNS_TOPIC'))
    if not all([path, key_id, team, topic]):
        return False
    with open(path) as f:
        _apns['key'] = f.read()
    _apns['client'] = httpx.Client(http2=True, timeout=httpx.Timeout(10.0, connect=10.0))
    return True


def _bearer():
    now = time.time()
    with _apns_lock:
        if not _apns['jwt'] or now - _apns['jwt_at'] > _JWT_REFRESH_AFTER:
            token = jwt.encode({'iss': os.environ['APNS_TEAM_ID'], 'iat': int(now)},
                               _apns['key'], algorithm='ES256',
                               headers={'kid': os.environ['APNS_KEY_ID'], 'alg': 'ES256'})
            _apns['jwt'] = token.decode('utf-8') if isinstance(token, bytes) else token
            _apns['jwt_at'] = now
        return _apns['jwt']


def _send(token, environment, notification_id, kind):
    """One push. Returns (status, apns_reason)."""
    payload = {
        'aps': {
            # What the phone shows if its extension cannot reach the server.
            # The extension replaces this with the real text on the way in.
            'alert': {'title': 'Blankee', 'body': 'You have a new notification.'},
            'mutable-content': 1,
            'sound': 'default',
        },
        'nid': notification_id,
        # 'notification' (fetch nid from the server) or 'reminder' (the
        # evening nudge, which has no row to fetch; the phone knows its text).
        'kind': kind,
    }
    headers = {
        'authorization': f'bearer {_bearer()}',
        'apns-topic': os.environ['APNS_TOPIC'],
        'apns-push-type': 'alert',
    }
    response = _apns['client'].post(f'https://{HOSTS[environment]}/3/device/{token}',
                                    headers=headers, content=json.dumps(payload))
    reason = None
    if response.status_code != 200:
        try:
            reason = response.json().get('reason')
        except Exception:
            pass
    return response.status_code, reason


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get('/v1/health')
def health():
    return jsonify({'ok': True, 'apns': _apns_ready()})


@app.post('/v1/register')
def register():
    """The phone, registering its own token with a secret of its choosing.

    Re-registering a known token replaces its secret. That is what a
    reinstalled app needs - it has lost its old secret - and it is safe
    because the token itself is the thing nobody else has.
    """
    data = request.get_json(silent=True) or {}
    token = str(data.get('token') or '').strip().lower()
    secret = str(data.get('secret') or '')
    environment = str(data.get('environment') or 'production').strip().lower()
    if not TOKEN_RE.match(token):
        return jsonify({'error': 'bad_token'}), 400
    if len(secret) < 32 or len(secret) > 256:
        return jsonify({'error': 'bad_secret'}), 400
    if environment not in ENVIRONMENTS:
        return jsonify({'error': 'bad_environment'}), 400
    with _db_lock, _db() as conn:
        conn.execute("""
            INSERT INTO devices (token, secret_hash, environment, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(token) DO UPDATE SET secret_hash = excluded.secret_hash,
                                            environment = excluded.environment
        """, (token, _hash(secret), environment, int(time.time())))
    return '', 204


@app.post('/v1/push')
def push():
    """A server, asking for a nudge on behalf of one of its users' phones."""
    data = request.get_json(silent=True) or {}
    token = str(data.get('token') or '').strip().lower()
    secret = str(data.get('secret') or '')
    notification_id = data.get('id')
    if not TOKEN_RE.match(token) or not secret:
        return jsonify({'error': 'bad_request'}), 400
    try:
        notification_id = int(notification_id)
    except (TypeError, ValueError):
        return jsonify({'error': 'bad_request'}), 400
    kind = str(data.get('kind') or 'notification')
    if kind not in ('notification', 'reminder'):
        return jsonify({'error': 'bad_request'}), 400

    now = int(time.time())
    with _db_lock, _db() as conn:
        row = conn.execute("SELECT * FROM devices WHERE token = ?", (token,)).fetchone()
        if row is None:
            return jsonify({'error': 'unknown_token'}), 404
        if not hmac.compare_digest(row['secret_hash'], _hash(secret)):
            return jsonify({'error': 'bad_secret'}), 401
        # A sliding hour, so one device cannot be used as a buzzer.
        window_start, count = row['window_start'], row['window_count']
        if now - window_start >= 3600:
            window_start, count = now, 0
        if count >= HOURLY_LIMIT:
            return jsonify({'error': 'rate_limited'}), 429
        conn.execute("UPDATE devices SET window_start = ?, window_count = ?, last_push_at = ? WHERE token = ?",
                     (window_start, count + 1, now, token))
        environment = row['environment']

    # Checked after the secret, not before: a relay with no key yet should
    # still tell a caller with the wrong secret exactly that.
    if not _apns_ready():
        return jsonify({'error': 'relay_not_configured'}), 503
    try:
        status, reason = _send(token, environment, notification_id, kind)
    except Exception as exc:
        return jsonify({'error': 'apns_unreachable', 'detail': str(exc)}), 502

    if status == 200:
        return jsonify({'sent': True})
    if status in (400, 410) or reason in ('BadDeviceToken', 'Unregistered', 'DeviceTokenNotForTopic'):
        # The device is gone, or this token belongs to the other environment.
        # Forgotten here, and the server is told so it can forget it too.
        with _db_lock, _db() as conn:
            conn.execute("DELETE FROM devices WHERE token = ?", (token,))
        return jsonify({'sent': False, 'error': 'invalid_token', 'apns_reason': reason}), 410
    return jsonify({'sent': False, 'error': 'apns_error', 'status': status, 'apns_reason': reason}), 502


init_db()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('RELAY_PORT', '8100')))
