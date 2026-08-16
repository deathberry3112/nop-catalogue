"""The run store — runs, rows, steps, events, in Postgres.

    from app.store import RunStore
    store = RunStore.start(rows, base_url=..., commit=..., filename=...)
    store.run_id                      # the session id everything hangs off

Reading them back, for runs this process did not execute:

    from app.store import run_store
    run_store.recent_runs()           # summaries, newest first
    run_store.load_snapshot(run_id)   # one run, in the shape the console polls
"""

from app.store import run_store
from app.store.client import fetch_all, store_available
from app.store.run_store import RunStore

__all__ = ["RunStore", "run_store", "fetch_all", "store_available"]
