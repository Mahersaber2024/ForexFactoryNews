"""
Shared news fetching for the bot: ONE upstream request serves every user.

Before, every user who pressed a news button made their own HTTP request to the API (up to 75 s, 3 tries).
Now the answer for (api server, endpoint, impacts) is cached for a few seconds and fetched by a single caller
(single-flight): when 10,000 users ask at once, one of them fetches and the others reuse the result.
If the API is down, a slightly old answer is served instead of an error.

Pure Python + requests, no Telegram imports (easy to test). All functions are thread-safe and blocking:
call them with asyncio.to_thread.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

TTL = 30.0               # seconds a fetched answer is reused
STALE_MAX = 900.0        # seconds an old answer may still be served when the API is failing
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 25.0
MAX_ENTRIES = 256

_session = requests.Session()
_adapter = HTTPAdapter(pool_connections=8, pool_maxsize=64, max_retries=0)
_session.mount("http://", _adapter)
_session.mount("https://", _adapter)

_cache: dict[tuple, tuple[float, list]] = {}      # key -> (fetched_at monotonic, rows)
_key_locks: dict[tuple, threading.Lock] = {}
_guard = threading.Lock()


def _lock_for(key: tuple) -> threading.Lock:
    with _guard:
        lock = _key_locks.get(key)
        if lock is None:
            lock = _key_locks[key] = threading.Lock()
        return lock


def _prune(now: float) -> None:
    with _guard:
        if len(_cache) <= MAX_ENTRIES:
            return
        for k in [k for k, (t, _) in _cache.items() if now - t > STALE_MAX]:
            _cache.pop(k, None)
            _key_locks.pop(k, None)
        while len(_cache) > MAX_ENTRIES:            # still too many: drop the oldest
            oldest = min(_cache, key=lambda k: _cache[k][0])
            _cache.pop(oldest, None)
            _key_locks.pop(oldest, None)


def fetch(api_base: str, endpoint: str, impacts: list[str],
          post: Callable[[list], list] | None = None) -> list[dict]:
    """Rows for that endpoint from the shared cache (or fetched now). The returned list and its dicts are SHARED:
    never modify them, copy first (dict(row)). `post` runs once on fresh data, before it is cached."""
    wanted = ",".join(sorted({i.lower() for i in (impacts or ["High"])}))
    key = (api_base, endpoint, wanted)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < TTL:
        return hit[1]
    with _lock_for(key):
        hit = _cache.get(key)                       # someone else may have fetched while we waited
        now = time.monotonic()
        if hit and now - hit[0] < TTL:
            return hit[1]
        try:
            response = _session.get(f"{api_base}/{endpoint}", params={"impact": wanted},
                                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            response.raise_for_status()
            rows = response.json()
            if post is not None:
                rows = post(rows)
            if not isinstance(rows, list):
                raise ValueError("news API did not return a list")
        except Exception as exc:
            if hit and now - hit[0] < STALE_MAX:
                logger.warning("News API failed (%s): serving a %.0f s old answer for '%s'", exc, now - hit[0], endpoint)
                return hit[1]
            raise
        _cache[key] = (time.monotonic(), rows)
    _prune(time.monotonic())
    return rows


def clear() -> None:
    """Forget everything (e.g. after the admin switches API server)."""
    with _guard:
        _cache.clear()
