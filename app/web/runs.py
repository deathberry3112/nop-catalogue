"""In-process run manager: start a run, poll it, cancel it.

Playwright's sync API cannot run inside an asyncio event loop, so every run
executes on its own worker THREAD and the API only ever reads the RunStore's
in-memory snapshot. That keeps the HTTP layer responsive while a run holds a
browser open for several minutes. MAX_CONCURRENT is a semaphore around those
threads, not a rewrite of the runner.

This manager is in-process and therefore single-machine. After a restart the
in-memory run list is empty even if Postgres still has rows. Schema columns
`worker_id` and `lease_expires_at` exist; there is no claim loop.
"""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from typing import Any

import main
import xlsxCreator
from app import rollback
from app.config import ROOT, settings
from app.store import RunStore, run_store
from main import ConfigError
from pages.landing_page import LandingPage

log = logging.getLogger("web.runs")

REPORT_DIR = ROOT / "var" / "reports"

#: Concurrent browser sessions against ONE nop instance. Four is a starting
#: point, not a measurement — the admin is the shared resource and it will
#: complain before the worker does. Tune from observed p95 per product.
MAX_CONCURRENT = 4

_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT)


class RunManager:
    """Every run this process knows about, live or finished."""

    def __init__(self):
        self._lock = threading.Lock()
        self._runs: dict[str, RunStore] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cancelled: set[str] = set()

    # --- starting --------------------------------------------------------

    def start(self, rows: list[dict], *, commit: bool, filename: str = "",
              base_url: str = "", email: str = "", password: str = "",
              triggered_by: str = "web") -> str:
        config = settings()
        base_url = base_url or config.admin_base_url
        email = email or config.admin_email
        password = password or config.admin_password

        options = main.RunOptions.from_env(
            commit=commit,
            output_xlsx=None,                 # written by hand below, per run
        )
        credentials = main.Credentials(base_url=base_url, email=email,
                                       password=password)

        store = RunStore.start(rows, base_url=base_url, commit=commit,
                               filename=filename, triggered_by=triggered_by,
                               options={"commit": commit,
                                        "on_existing": options.on_existing})

        with self._lock:
            self._runs[store.run_id] = store

        thread = threading.Thread(
            target=self._execute, name=f"run-{store.run_id[:8]}",
            args=(store, rows, options, credentials), daemon=True)
        with self._lock:
            self._threads[store.run_id] = thread
        thread.start()
        return store.run_id

    def _execute(self, store: RunStore, rows: list[dict],
                 options: main.RunOptions, credentials: main.Credentials) -> None:
        """The worker thread. Owns the whole run and never raises out."""
        acquired = _semaphore.acquire(timeout=1800)
        if not acquired:
            store.finish(error="timed out waiting for a free browser slot")
            return
        try:
            store.event("run starting",
                        data={"rows": len(rows), "commit": options.commit})

            # Fired immediately before the row is opened, so `index` is the
            # same 1-based counter the runner passes to row_started — which is
            # what lets the console hang each row's steps off this one line.
            def progress(done: int, total: int, sku: str) -> None:
                store.event(f"[{done}/{total}] {sku}",
                            data={"kind": "product", "sku": sku,
                                  "index": done, "total": total})

            summary = main.run(rows, options, credentials,
                               progress=progress, store=store)
            self._write_report(store, rows, summary)
            store.finish()

        except main.ConfigError as exc:
            # Refused before the browser opened — the message is safe to show
            # the person who uploaded the file.
            log.warning("run %s refused: %s", store.run_id, exc)
            store.event(str(exc), level="error")
            store.finish(error=str(exc))
        except Exception as exc:
            log.exception("run %s crashed", store.run_id)
            store.event(f"run crashed: {exc}", level="error")
            store.finish(error=f"{type(exc).__name__}: {exc}")
        finally:
            _semaphore.release()

    def _write_report(self, store: RunStore, rows: list[dict],
                      summary: dict) -> None:
        """The re-uploadable .xlsx. Written even for a failed run — a batch
        that died halfway is exactly when you want to know which rows made it."""
        try:
            REPORT_DIR.mkdir(parents=True, exist_ok=True)
            path = REPORT_DIR / f"{store.run_id}.xlsx"
            counts = xlsxCreator.write_report(rows, summary, path)
            store.event("report written", data={"counts": counts})
        except Exception as exc:
            log.warning("could not write the report for %s: %s", store.run_id, exc)
            store.event(f"could not write the report: {exc}", level="warn")

    # --- reading ---------------------------------------------------------

    def get(self, run_id: str) -> RunStore | None:
        with self._lock:
            return self._runs.get(run_id)

    def snapshot(self, run_id: str) -> dict | None:
        """This run as the console polls it, from memory or from the store.

        Memory first, because a LIVE run is only complete there — the mirror is
        written ahead of the database and survives the store being unreachable.
        Falling back to Postgres is what stops a restart from turning a run
        into a 404 while every row of it is still recorded.
        """
        store = self.get(run_id)
        if store is not None:
            return store.snapshot()
        return run_store.load_snapshot(run_id)

    def list(self) -> list[dict]:
        """Newest first, summary only — the run list must not carry every row.

        The in-process runs and the stored ones are one list. Without the
        stored half the console forgets every run on restart, which is exactly
        when somebody needs to find the run that was interrupted — and its
        products are still in the catalogue waiting to be rolled back.
        """
        with self._lock:
            stores = list(self._runs.values())

        summaries = {}
        for run in run_store.recent_runs():
            summaries[run["id"]] = run
        # Memory wins on conflict: a run still executing has progress the
        # database has not caught up with yet.
        for store in stores:
            snap = store.snapshot()
            snap.pop("rows", None)
            snap.pop("events", None)
            summaries[snap["id"]] = snap

        return sorted(summaries.values(),
                      key=lambda s: s.get("started_at") or "", reverse=True)

    def report_path(self, run_id: str) -> Path | None:
        path = REPORT_DIR / f"{run_id}.xlsx"
        return path if path.exists() else None

    # --- rollback --------------------------------------------------------
    #
    # Deleting is deliberately NOT modelled as a run: it must not appear in the
    # run list, must not be resumable, and must never be something a stray
    # retry can repeat. It is one short synchronous operation with a report.

    def rollback_plan(self, run_id: str) -> dict:
        """What a rollback would delete. Computed from the store every time —
        never cached, so the dialog cannot show a stale set."""
        return rollback.build_plan(run_id).as_dict()

    def rollback(self, run_id: str, confirm_skus: list[str],
                 *, base_url: str = "", email: str = "",
                 password: str = "") -> dict:
        """Delete the selected subset of this run's products.

        `confirm_skus` is the operator's selection, and it is a filter, not an
        instruction: the plan is recomputed here from the store, and the
        selection is only allowed to narrow it. A SKU that is no longer
        deletable — already rolled back, claim released, someone else got there
        first — is not in the recomputed plan, so it refuses the whole request
        instead of silently deleting a set nobody approved.

        That is what makes rolling a run back a few products at a time safe,
        including from two dialogs open at once.
        """
        plan = rollback.build_plan(run_id)
        if plan.blocked:
            raise ConfigError(plan.blocked)

        try:
            plan = rollback.narrow(plan, confirm_skus)
        except rollback.SelectionError as exc:
            raise ConfigError(str(exc)) from exc

        config = settings()
        target = base_url or plan.base_url or config.admin_base_url
        if plan.base_url and target.rstrip("/") != plan.base_url.rstrip("/"):
            raise ConfigError(
                f"this run targeted {plan.base_url}; refusing to delete "
                f"against {target}")

        credentials = main.Credentials(
            base_url=target, email=email or config.admin_email,
            password=password or config.admin_password).require()

        options = main.RunOptions.from_env(commit=True, output_xlsx=None)
        store = RunStore(run_id, remote=True)   # for release_product only

        if not _semaphore.acquire(timeout=600):
            raise ConfigError("timed out waiting for a free browser slot")
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.launch(**main.build_launch_kwargs(options))
                context = browser.new_context(
                    **main.build_context_kwargs(credentials.base_url, options))
                page = context.new_page()
                try:
                    LandingPage(page, url=credentials.base_url).login(
                        credentials.email, credentials.password)
                    report = rollback.execute(page, plan, store=store,
                                              base_url=target)
                finally:
                    context.close()
                    browser.close()
        finally:
            _semaphore.release()

        live = self.get(run_id)
        if live:
            live.event(f"rolled back: {len(report['deleted'])} product(s) deleted",
                       level="warn", data=report)
        return report


MANAGER = RunManager()
