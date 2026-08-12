"""Postgres access for the run store.

A small ThreadedConnectionPool — the web layer and Playwright workers share one
process and hit the store from multiple threads. Callers never hold a
connection across Playwright waits; each helper borrows, runs, and returns.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Any, Sequence

from app.config import settings

log = logging.getLogger("store.client")

_pool = None
_pool_lock = threading.Lock()


def store_available() -> bool:
    return settings().store_enabled


def _dsn() -> str:
    url = settings().database_url
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    # Railway (and some other hosts) still emit the postgres:// scheme;
    # psycopg2 accepts both, but normalising keeps tooling consistent.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def _get_pool():
    """Shared pool. Raises if DATABASE_URL is not set — callers should check
    store_available() first; every caller in app/ already does."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        import psycopg2.pool
        dsn = _dsn()
        # Hide credentials in the log: keep host/db, drop userinfo.
        safe = dsn.split("@")[-1] if "@" in dsn else "(local)"
        log.info("Postgres: %s", safe)
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=8, dsn=dsn, connect_timeout=15)
        return _pool


def reset_pool() -> None:
    """Close the pool (tests / after DATABASE_URL changes in-process)."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None


@contextmanager
def connection():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def fetch_all(sql: str, params: Sequence[Any] | None = None) -> list[dict]:
    """Run a SELECT (or anything with a result set) and return dict rows."""
    from psycopg2.extras import RealDictCursor
    with connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]


def execute(sql: str, params: Sequence[Any] | None = None) -> int:
    """Run a write; return rowcount."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount


def healthy() -> tuple[bool, str]:
    """Can we actually reach the database and is the schema applied?

    Called at boot rather than on the first write, so a misconfiguration is a
    startup message instead of a run that silently records nothing.
    """
    if not store_available():
        return False, "not configured"
    try:
        fetch_all("SELECT id FROM runs LIMIT 1")
        return True, "ok"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
