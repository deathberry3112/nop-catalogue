"""The Supabase client, created once.

Uses the SERVICE key, which bypasses row-level security. That is correct for a
trusted backend worker and wrong for anything a browser can reach — the web
layer must never hand this key, or a client built from it, to the front end.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from app.config import settings

log = logging.getLogger("store.client")


def store_available() -> bool:
    return settings().store_enabled


@lru_cache(maxsize=1)
def get_client():
    """The shared client. Raises if Supabase is not configured — callers should
    check store_available() first; every caller in app/ already does."""
    config = settings()
    if not config.store_enabled:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY are not set")
    from supabase import create_client
    log.info("Supabase: %s", config.supabase_url)
    return create_client(config.supabase_url, config.supabase_service_key)


def healthy() -> tuple[bool, str]:
    """Can we actually reach the database and is the schema applied?

    Called at boot rather than on the first write, so a misconfiguration is a
    startup message instead of a run that silently records nothing.
    """
    if not store_available():
        return False, "not configured"
    try:
        get_client().table("runs").select("id").limit(1).execute()
        return True, "ok"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
