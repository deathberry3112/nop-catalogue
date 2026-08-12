"""The run store — runs, rows, steps, events, in Postgres.

    from app.store import RunStore
    store = RunStore.start(rows, base_url=..., commit=..., filename=...)
    store.run_id                      # the session id everything hangs off
"""

from app.store.client import fetch_all, store_available
from app.store.run_store import RunStore

__all__ = ["RunStore", "fetch_all", "store_available"]
