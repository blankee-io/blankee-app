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
        # Spaces collapsed within a line; the line breaks of a message written
        # as a list (bills due today) are kept - an alert body shows them.
        clean_body = '\n'.join(' '.join(line.split()) for line in clean_body.splitlines()
                               if line.strip())

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


def push_to_user(user_id, body, title='Blankee', badge=None, url=None, action=None,
                 notification_id=None):
    """Send one alert to every device this user has registered.

    This is the push half of a notification, and it is the only way a
    notification should reach a device. It used to live inline in
    add_notification, which meant the two other senders - the allowance-spent
    notification in bucket_utils and the evening reminder in the scheduler -
    each had to remember to push as well, and one of them did not: an
    allowance running out arrived by email and never on the phone.

    Best effort throughout. A device that has gone (APNs says the token is bad
    or unregistered) is dropped from device_tokens so it is not tried again;
    anything else is logged and skipped, because a push that fails must never
    lose the in-app notification or the email that go with it.

    `url` is a relative app path the phone opens when the alert is tapped;
    `action` names something the app does instead (the bucket prompt). Both
    ride along as custom keys. Returns how many devices were sent to.

    Two ways out. With APNS_ settings of its own - the app maker's server, or
    an installation that signs its own build - the server talks to Apple
    directly and the alert carries its text. Otherwise it asks the relay
    (relay/README.md), which carries only `notification_id`; the phone fetches
    the text itself. A notification with no row (the evening reminder raised
    before its row exists) still gets a nudge, and the phone shows its generic
    text for it.
    """
    direct = apns_enabled()
    relay = (os.environ.get('PUSH_RELAY_URL') or '').strip().rstrip('/')
    if not direct and not relay:
        return 0

    from db_connections import get_db_pool
    try:
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT device_token, relay_secret FROM device_tokens WHERE user_id = %s", (user_id,))
            rows = cursor.fetchall() or []
    except Exception as exc:
        log_warning(logger, 'PUSH', f"Could not load device tokens for user {user_id}: {exc}")
        return 0

    devices = [(r['device_token'], r.get('relay_secret')) for r in rows if r.get('device_token')]
    if not devices:
        return 0
    if not direct:
        return _push_via_relay(relay, user_id, devices, notification_id,
                               'reminder' if action else 'notification')
    tokens = [t for t, _ in devices]

    custom = {}
    if url:
        custom['url'] = url
    if action:
        custom['action'] = action

    sent = 0
    for token in tokens:
        result = send_apns_notification(token, title, body, badge, 'default', custom or None)
        if result.get('sent'):
            sent += 1
            continue
        if result.get('reason') == 'invalid_token':
            try:
                with get_db_pool().get_cursor(commit=True) as cursor:
                    cursor.execute(
                        "DELETE FROM device_tokens WHERE device_token = %s AND user_id = %s",
                        (token, user_id))
                log_info(logger, 'PUSH', f"Dropped a dead device token for user {user_id}",
                         apns_reason=result.get('apns_reason'))
            except Exception as exc:
                log_warning(logger, 'PUSH', f"Could not drop a dead device token for user {user_id}: {exc}")
        else:
            log_warning(logger, 'PUSH', f"Push to user {user_id} not sent",
                        reason=result.get('reason'), status=result.get('status'),
                        apns_reason=result.get('apns_reason'), error=result.get('error'))
    if sent:
        # Success is logged too, so "no PUSH lines" reads as "nothing was
        # attempted" rather than leaving delivery and silence looking alike.
        log_info(logger, 'PUSH', f"Pushed to {sent} device(s) for user {user_id}")
    return sent


def _forget_device(user_id, token, why):
    from db_connections import get_db_pool
    try:
        with get_db_pool().get_cursor(commit=True) as cursor:
            cursor.execute(
                "DELETE FROM device_tokens WHERE device_token = %s AND user_id = %s",
                (token, user_id))
        log_info(logger, 'PUSH', f"Dropped a dead device token for user {user_id}", reason=why)
    except Exception as exc:
        log_warning(logger, 'PUSH', f"Could not drop a dead device token for user {user_id}: {exc}")


def _push_via_relay(relay, user_id, devices, notification_id, kind):
    """The relay half of push_to_user. A device the phone never registered
    with the relay (no secret stored) is skipped, not an error: an older app,
    or one that has not signed in since the relay existed."""
    sent = 0
    for token, secret in devices:
        if not secret:
            continue
        try:
            response = httpx.post(f"{relay}/v1/push",
                                  json={'token': token, 'secret': secret,
                                        'id': int(notification_id or 0), 'kind': kind},
                                  timeout=httpx.Timeout(10.0, connect=5.0))
        except Exception as exc:
            log_warning(logger, 'PUSH', f"Relay unreachable for user {user_id}: {exc}")
            return sent
        if response.status_code == 200:
            sent += 1
        elif response.status_code == 410:
            _forget_device(user_id, token, 'relay says the device is gone')
        else:
            detail = None
            try:
                detail = response.json()
            except Exception:
                pass
            log_warning(logger, 'PUSH', f"Relay refused a push for user {user_id}",
                        status=response.status_code, detail=detail)
    if sent:
        log_info(logger, 'PUSH', f"Pushed to {sent} device(s) for user {user_id} via the relay")
    return sent


# ---------------------------------------------------------------- the badge
# A new notification sets the number on the app's icon as it arrives (its
# push carries it, or the phone's extension fetches it). Anything else that
# changes how many are unread - one read or deleted in a browser, all marked
# read, a standing notification the app withdrew - used to leave the icon
# wrong until the app was next opened. badge_changed sends the new number on
# its own: silently, to every device, through the relay's /v1/badge or
# straight to Apple. Asked for on 2026-11-16.

BADGE_DELAY = 2.0      # seconds: a burst of changes sends one number
_badge_timers = {}
_badge_lock = threading.Lock()


def badge_changed(user_id):
    """
    The person's unread count may have changed: send it to their phones in
    BADGE_DELAY seconds, on a thread of its own, the count read when it goes.
    Call it after the change is committed. Never raises, never waits, and
    does nothing on a server with no way to push.
    """
    try:
        if not apns_enabled() and not (os.environ.get('PUSH_RELAY_URL') or '').strip():
            return
        with _badge_lock:
            pending = _badge_timers.pop(user_id, None)
            if pending is not None:
                pending.cancel()
            timer = threading.Timer(BADGE_DELAY, _send_badge_now, args=(user_id,))
            timer.daemon = True
            _badge_timers[user_id] = timer
            timer.start()
    except Exception as exc:
        log_warning(logger, 'PUSH', f"Could not queue a badge update for user {user_id}: {exc}")


def unread_count(user_id):
    """Unread notifications - the same count the phone's extension is given
    (/api/widget/notification), so the two never disagree."""
    from db_connections import get_db_pool
    with get_db_pool().get_cursor(dictionary=True) as cursor:
        cursor.execute("SELECT COUNT(*) AS total FROM notifications WHERE user_id = %s AND is_read = 0",
                       (user_id,))
        return int((cursor.fetchone() or {}).get('total') or 0)


def _send_badge_now(user_id):
    with _badge_lock:
        if _badge_timers.get(user_id) is not None and not _badge_timers[user_id].is_alive():
            _badge_timers.pop(user_id, None)
    try:
        from db_connections import get_db_pool
        count = min(unread_count(user_id), 9999)
        with get_db_pool().get_cursor(dictionary=True) as cursor:
            cursor.execute("SELECT device_token, relay_secret FROM device_tokens WHERE user_id = %s",
                           (user_id,))
            devices = [(r['device_token'], r.get('relay_secret')) for r in (cursor.fetchall() or [])
                       if r.get('device_token')]
    except Exception as exc:
        log_warning(logger, 'PUSH', f"Could not read the badge for user {user_id}: {exc}")
        return 0
    if not devices:
        return 0

    sent = 0
    if apns_enabled():
        for token, _ in devices:
            result = send_apns_badge(token, count)
            if result.get('sent'):
                sent += 1
            elif result.get('reason') == 'invalid_token':
                _forget_device(user_id, token, 'APNs says the device is gone')
            else:
                log_warning(logger, 'PUSH', f"Badge for user {user_id} not sent", reason=result.get('reason'),
                            status=result.get('status'), apns_reason=result.get('apns_reason'))
    else:
        relay = (os.environ.get('PUSH_RELAY_URL') or '').strip().rstrip('/')
        for token, secret in devices:
            if not secret:
                continue
            try:
                response = httpx.post(f"{relay}/v1/badge", json={'token': token, 'secret': secret, 'count': count},
                                      timeout=httpx.Timeout(10.0, connect=5.0))
            except Exception as exc:
                log_warning(logger, 'PUSH', f"Relay unreachable for user {user_id}'s badge: {exc}")
                break
            if response.status_code == 200:
                sent += 1
            elif response.status_code == 410:
                _forget_device(user_id, token, 'relay says the device is gone')
            else:
                # 404 from a relay older than /v1/badge too: the icon simply
                # waits for the app, as it did before.
                log_warning(logger, 'PUSH', f"Relay refused a badge for user {user_id}",
                            status=response.status_code)
    if sent:
        log_info(logger, 'PUSH', f"Badge {count} sent to {sent} device(s) for user {user_id}")
    return sent


def send_apns_badge(device_token, count):
    """A badge-only push straight to Apple: no alert, no sound. Returns the
    same shape as send_apns_notification."""
    if not _init_config():
        return {"sent": False, "reason": "apns_not_configured"}
    try:
        token = _get_jwt()
    except Exception as exc:
        return {"sent": False, "reason": "apns_jwt_error", "error": str(exc)}
    headers = {
        "authorization": f"bearer {token}",
        "apns-topic": _apns_topic,
        "apns-push-type": "alert",
        "apns-priority": "10",
        "apns-collapse-id": "badge",
    }
    try:
        response = _http_client.post(f"https://{_apns_host}/3/device/{device_token}", headers=headers,
                                     content=json.dumps({"aps": {"badge": int(count)}}))
        if response.status_code == 200:
            return {"sent": True}
        apns_reason = None
        try:
            apns_reason = response.json().get("reason")
        except Exception:
            pass
        if response.status_code == 410 or apns_reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"):
            return {"sent": False, "reason": "invalid_token", "status": response.status_code, "apns_reason": apns_reason}
        return {"sent": False, "reason": "apns_error", "status": response.status_code, "apns_reason": apns_reason}
    except Exception as exc:
        return {"sent": False, "reason": "apns_exception", "error": str(exc)}


def deep_link_from(message):
    """The relative app path a notification's first link points at, if any.

    Notification messages carry their link as HTML; the phone cannot follow
    that from an alert, so the path travels as its own key. Only a relative
    path is accepted - an absolute URL in a message must never become
    somewhere the app is sent.
    """
    import re
    match = re.search(r'href=["\']([^"\']+)["\']', message or '')
    if match:
        candidate = match.group(1).strip()
        if candidate.startswith('/'):
            return candidate
    return '/notifications'
