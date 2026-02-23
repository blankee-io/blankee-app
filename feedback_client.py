import json
import logging
import os
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 6
_FIDER_CACHE_TTL = 7 * 24 * 60 * 60  # 7 days


class FiderClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            response = self.session.request(method, url, timeout=_DEFAULT_TIMEOUT, **kwargs)
            return response
        except Exception as exc:
            logger.error(f"[FIDER] Request error {method} {url}: {exc}")
            raise

    def ensure_user(self, name: str, email: str, reference: str, redis_client=None) -> int:
        """Create-or-get a Fider user and cache the id in Redis."""
        cache_key = f"fider_user_id:{reference}"
        cached_id = None
        if redis_client:
            try:
                cached_val = redis_client.get(cache_key)
                if cached_val:
                    cached_id = int(cached_val)
            except Exception:
                pass
        if cached_id:
            return cached_id

        payload = {"name": name, "email": email, "reference": reference}
        resp = self._request("POST", "/api/v1/users", json=payload)
        resp.raise_for_status()
        user_id = resp.json().get("id")
        if not user_id:
            raise RuntimeError("Fider create user returned no id")

        if redis_client:
            try:
                redis_client.setex(cache_key, _FIDER_CACHE_TTL, user_id)
            except Exception:
                pass

        return int(user_id)

    def list_posts(self, params: Dict[str, Any], user_id: Optional[int] = None) -> Dict[str, Any]:
        headers = {"X-Fider-UserID": str(user_id)} if user_id else None
        resp = self._request("GET", "/api/v1/posts", params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def get_post(self, number: int, user_id: Optional[int] = None) -> Dict[str, Any]:
        headers = {"X-Fider-UserID": str(user_id)} if user_id else None
        resp = self._request("GET", f"/api/v1/posts/{number}", headers=headers)
        resp.raise_for_status()
        return resp.json()

    def create_post(self, user_id: int, title: str, description: str = "") -> Dict[str, Any]:
        headers = {"X-Fider-UserID": str(user_id)}
        resp = self._request("POST", "/api/v1/posts", json={"title": title, "description": description}, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def vote(self, user_id: int, number: int):
        headers = {"X-Fider-UserID": str(user_id)}
        resp = self._request("POST", f"/api/v1/posts/{number}/votes", headers=headers)
        resp.raise_for_status()
        return {}

    def unvote(self, user_id: int, number: int):
        headers = {"X-Fider-UserID": str(user_id)}
        resp = self._request("DELETE", f"/api/v1/posts/{number}/votes", headers=headers)
        resp.raise_for_status()
        return {}

    def list_comments(self, number: int) -> Dict[str, Any]:
        resp = self._request("GET", f"/api/v1/posts/{number}/comments")
        resp.raise_for_status()
        return resp.json()

    def add_comment(self, user_id: int, number: int, content: str) -> Dict[str, Any]:
        headers = {"X-Fider-UserID": str(user_id)}
        resp = self._request("POST", f"/api/v1/posts/{number}/comments", headers=headers, json={"content": content})
        resp.raise_for_status()
        return resp.json()

    def list_tags(self) -> Dict[str, Any]:
        resp = self._request("GET", "/api/v1/tags")
        resp.raise_for_status()
        return resp.json()

    def create_tag(self, name: str, color: str = "2AAAA8", is_public: bool = True) -> Optional[str]:
        payload = {"name": name, "color": color, "isPublic": is_public}
        resp = self._request("POST", "/api/v1/tags", json=payload)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("slug")
        # If tag already exists, fallback to list and match
        try:
            tags = self.list_tags()
            for tag in tags:
                if tag.get("name") == name:
                    return tag.get("slug")
        except Exception:
            pass
        return None

    def tag_post(self, number: int, slug: str):
        resp = self._request("POST", f"/api/v1/posts/{number}/tags/{slug}")
        resp.raise_for_status()
        return {}


_client: Optional[FiderClient] = None

def get_fider_client() -> FiderClient:
    global _client
    if _client:
        return _client

    base_url = os.environ.get("FIDER_BASE_URL", "https://blankeeio.fider.io")
    api_key = os.environ.get("FIDER_ADMIN_KEY")
    if not api_key:
        raise RuntimeError("FIDER_ADMIN_KEY is required for Fider integration")
    _client = FiderClient(base_url, api_key)
    return _client