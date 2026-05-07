import os
import collections

# Compatibility shim for Python 3.10+ where Iterable/Mapping moved to collections.abc
try:
    from collections.abc import Iterable, Mapping, MutableSet, MutableMapping
    if not hasattr(collections, "Iterable"):
        collections.Iterable = Iterable
    if not hasattr(collections, "Mapping"):
        collections.Mapping = Mapping
    if not hasattr(collections, "MutableSet"):
        collections.MutableSet = MutableSet
    if not hasattr(collections, "MutableMapping"):
        collections.MutableMapping = MutableMapping
except Exception:
    pass

from apns2.client import APNsClient
from apns2.credentials import TokenCredentials
from apns2.errors import BadDeviceToken, Unregistered
from apns2.payload import Payload
from log_config import get_logger, log_info, log_error, log_warning, log_exception

logger = get_logger(__name__)

_apns_client = None
_apns_topic = None
_apns_sandbox = False
# change the above line to 'False' if you want to default to production environment for APNs #

def _get_client():
    """Lazily initialize and cache the APNs client."""
    global _apns_client, _apns_topic, _apns_sandbox

    if _apns_client:
        return _apns_client

    key_path = os.getenv("APNS_KEY_PATH")
    key_id = os.getenv("APNS_KEY_ID")
    team_id = os.getenv("APNS_TEAM_ID")
    topic = os.getenv("APNS_TOPIC")
    use_sandbox = os.getenv("APNS_USE_SANDBOX", "false").lower() == "true"

    if not all([key_path, key_id, team_id, topic]):
        log_info(logger, 'PUSH', "APNs not configured; missing required environment variables")
        return None

    try:
        credentials = TokenCredentials(
            auth_key_path=key_path,
            auth_key_id=key_id,
            team_id=team_id
        )
        _apns_client = APNsClient(
            credentials=credentials,
            use_sandbox=use_sandbox,
            use_alternative_port=False
        )
        _apns_topic = topic
        _apns_sandbox = use_sandbox
        log_info(logger, 'PUSH', "APNs client initialized", sandbox=use_sandbox)
    except Exception as exc:
        log_exception(logger, 'PUSH', "Failed to initialize APNs client", error=str(exc))
        _apns_client = None

    return _apns_client


def apns_enabled():
    """Return True if APNs configuration is present and client can be created."""
    return _get_client() is not None


def send_apns_notification(device_token, title, body, badge=None, sound="default", custom=None):
    """Send a push notification via APNs.
    
    Automatically strips HTML tags from body and adds mutable-content flag
    for iOS Notification Service Extension processing.

    Returns a dict with keys:
    - sent: bool
    - reason/status/apns_reason/error when not sent
    """
    client = _get_client()
    if not client or not _apns_topic:
        return {"sent": False, "reason": "apns_not_configured"}
    
    # Strip HTML tags from body for cleaner notifications
    import re
    clean_body = body
    if body:
        # Remove HTML tags
        clean_body = re.sub(r'<[^>]+>', '', body)
        # Decode HTML entities
        clean_body = clean_body.replace('&nbsp;', ' ')
        clean_body = clean_body.replace('&amp;', '&')
        clean_body = clean_body.replace('&lt;', '<')
        clean_body = clean_body.replace('&gt;', '>')
        clean_body = clean_body.replace('&quot;', '"')
        clean_body = clean_body.replace('&#39;', "'")
        clean_body = clean_body.replace('&apos;', "'")
        # Remove extra whitespace
        clean_body = ' '.join(clean_body.split()).strip()

    payload = Payload(
        alert={"title": title, "body": clean_body},
        badge=badge,
        sound=sound,
        custom=custom or {},
        mutable_content=True  # Enable iOS Notification Service Extension
    )

    try:
        response = client.send_notification(device_token, payload, topic=_apns_topic)
        status = getattr(response, "status", None)
        reason = getattr(response, "reason", None)

        if status and status != 200:
            if status in (400, 410) or reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"):
                return {"sent": False, "reason": "invalid_token", "status": status, "apns_reason": reason}
            return {"sent": False, "reason": "apns_error", "status": status, "apns_reason": reason}

        return {"sent": True}
    except (Unregistered, BadDeviceToken):
        return {"sent": False, "reason": "invalid_token"}
    except Exception as exc:
        log_warning(logger, 'PUSH', "APNs send failed", error=str(exc))
        return {"sent": False, "reason": "apns_exception", "error": str(exc)}
