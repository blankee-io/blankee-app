import os
import json
import time
import threading

import httpx
import jwt
from log_config import get_logger, log_info, log_error, log_warning, log_exception

logger = get_logger(__name__)

# ---- Module state (lazily initialized) ----
_lock = threading.Lock()
_http_client = None          # httpx.Client (HTTP/2)
_apns_topic = None
_apns_host = None            # 'api.push.apple.com' or 'api.sandbox.push.apple.com'
_apns_key_text = None        # contents of the .p8 file
_apns_key_id = None
_apns_team_id = None
_jwt_token = None
_jwt_token_issued_at = 0.0
_JWT_REFRESH_AFTER = 50 * 60  # refresh JWT every 50 min (Apple max 60, min reuse 20)


def _init_config():
    """Read env, load .p8 key, build httpx HTTP/2 client. Returns True on success."""
    global _http_client, _apns_topic, _apns_host
    global _apns_key_text, _apns_key_id, _apns_team_id

    if _http_client is not None:
        return True

    key_path = os.getenv("APNS_KEY_PATH")
    key_id = os.getenv("APNS_KEY_ID")
    team_id = os.getenv("APNS_TEAM_ID")
    topic = os.getenv("APNS_TOPIC")
    use_sandbox = os.getenv("APNS_USE_SANDBOX", "false").lower() == "true"

    if not all([key_path, key_id, team_id, topic]):
        log_info(logger, 'PUSH', "APNs not configured; missing required environment variables")
        return False

    try:
        with open(key_path, "r") as f:
            _apns_key_text = f.read()
        _apns_key_id = key_id
        _apns_team_id = team_id
        _apns_topic = topic
        _apns_host = "api.sandbox.push.apple.com" if use_sandbox else "api.push.apple.com"
        # http2=True is required by APNs
        _http_client = httpx.Client(
            http2=True,
            timeout=httpx.Timeout(10.0, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=4),
        )
        log_info(logger, 'PUSH', "APNs client initialized", sandbox=use_sandbox, host=_apns_host)
        return True
    except Exception as exc:
        log_exception(logger, 'PUSH', "Failed to initialize APNs client", error=str(exc))
        _http_client = None
        return False


def _get_jwt():
    """Return a cached JWT, refreshing if older than ~50 min. Thread-safe."""
    global _jwt_token, _jwt_token_issued_at
    now = time.time()
    if _jwt_token and (now - _jwt_token_issued_at) < _JWT_REFRESH_AFTER:
        return _jwt_token
    with _lock:
        # re-check after acquiring lock
        now = time.time()
        if _jwt_token and (now - _jwt_token_issued_at) < _JWT_REFRESH_AFTER:
            return _jwt_token
        token = jwt.encode(
            payload={"iss": _apns_team_id, "iat": int(now)},
            key=_apns_key_text,
            algorithm="ES256",
            headers={"kid": _apns_key_id, "alg": "ES256"},
        )
        # PyJWT 1.x returns bytes, 2.x returns str
        if isinstance(token, bytes):
            token = token.decode("utf-8")
        _jwt_token = token
        _jwt_token_issued_at = now
        return _jwt_token


def apns_enabled():
    """Return True if APNs configuration is present and client can be created."""
    return _init_config()


def send_apns_notification(device_token, title, body, badge=None, sound="default", custom=None):
    """Send a push notification via APNs (HTTP/2 + JWT bearer auth).

    Returns a dict with keys:
    - sent: bool
    - reason/status/apns_reason/error when not sent
    """
    if not _init_config():
        return {"sent": False, "reason": "apns_not_configured"}

    # Strip HTML tags from body for cleaner notifications
    import re
    clean_body = body or ""
    if clean_body:
        clean_body = re.sub(r'<[^>]+>', '', clean_body)
        clean_body = (clean_body
            .replace('&nbsp;', ' ')
            .replace('&amp;', '&')
            .replace('&lt;', '<')
            .replace('&gt;', '>')
            .replace('&quot;', '"')
            .replace('&#39;', "'")
            .replace('&apos;', "'"))
        clean_body = ' '.join(clean_body.split()).strip()

    aps = {
        "alert": {"title": title, "body": clean_body},
        "mutable-content": 1,  # enables iOS Notification Service Extension
    }
    if sound is not None:
        aps["sound"] = sound
    if badge is not None:
        aps["badge"] = badge

    payload = {"aps": aps}
    if custom:
        # merge custom keys at top level (APNs convention)
        for k, v in custom.items():
            if k != "aps":
                payload[k] = v

    try:
        token = _get_jwt()
    except Exception as exc:
        log_exception(logger, 'PUSH', "Failed to build APNs JWT", error=str(exc))
        return {"sent": False, "reason": "apns_jwt_error", "error": str(exc)}

    url = f"https://{_apns_host}/3/device/{device_token}"
    headers = {
        "authorization": f"bearer {token}",
        "apns-topic": _apns_topic,
        "apns-push-type": "alert",
    }

    try:
        response = _http_client.post(url, headers=headers, content=json.dumps(payload))
        status = response.status_code
        if status == 200:
            return {"sent": True}

        # parse APNs error body { "reason": "..." }
        apns_reason = None
        try:
            data = response.json()
            apns_reason = data.get("reason")
        except Exception:
            pass

        if status in (400, 410) or apns_reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"):
            return {"sent": False, "reason": "invalid_token", "status": status, "apns_reason": apns_reason}
        return {"sent": False, "reason": "apns_error", "status": status, "apns_reason": apns_reason}
    except Exception as exc:
        log_warning(logger, 'PUSH', "APNs send failed", error=str(exc))
        return {"sent": False, "reason": "apns_exception", "error": str(exc)}

