"""RunStore — everything one automation run records about itself.

    store = RunStore.start(rows, base_url=..., commit=..., filename=...)
    row_id = store.row_started(row)
    with store.step(row_id, "create"): ...
    store.row_finished(row_id, status="added", product_url=...)
    store.finish()

THE RULE THIS MODULE IS BUILT ON: telemetry must never break the automation.
Every write is wrapped in _safe(). A dropped log line costs observability; an
exception thrown from a logging call costs a half-added product in a live
catalogue. So Postgres being down, misconfigured, or entirely absent degrades
this class to an in-memory recorder and the run proceeds unchanged.

That is also why the in-memory mirror is not a debug convenience: it is what
the web layer polls for live progress, so the UI works before Postgres is
configured and keeps working if it goes away mid-run.
"""

from __future__ import annotations

import hashlib
import logging
import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from psycopg2.extras import Json

from app.errors import classify
from app.store.client import execute, fetch_all, store_available

log = logging.getLogger("store.run")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class RunStore:
    """One run's records, mirrored in memory and (when configured) Postgres."""

    def __init__(self, run_id: str, *, remote: bool):
        self.run_id = run_id
        self.remote = remote
        self._lock = threading.Lock()
        self._started = time.monotonic()

        # The in-memory mirror the UI polls. Kept complete enough that the
        # front end never needs to know whether Postgres is configured.
        self.state: dict[str, Any] = {
            "id": run_id,
            "status": "queued",
            "created_at": _now(),
            "started_at": None,
            "finished_at": None,
            "total_rows": 0,
            "counts": {},
            "rows": {},          # row_id -> row state
            "order": [],         # row_ids, in payload order
            "events": [],
            "error": None,
            "llm_cost_usd": 0.0,
            "colour_tiers": {},
        }

    # --- construction ----------------------------------------------------

    @classmethod
    def start(cls, rows: list[dict], *, base_url: str, commit: bool,
              filename: str = "", triggered_by: str = "web",
              options: dict | None = None) -> "RunStore":
        run_id = str(uuid.uuid4())
        store = cls(run_id, remote=store_available())

        digest = hashlib.sha256(
            repr([r.get("sku") for r in rows]).encode()).hexdigest()

        store.state.update(status="running", started_at=_now(),
                           total_rows=len(rows), source_filename=filename,
                           target_base_url=base_url, commit=commit)

        started = _now()
        store._safe(lambda: execute(
            """
            INSERT INTO runs (
              id, status, triggered_by, source_filename, source_sha256,
              target_base_url, commit, options, worker_id, total_rows, started_at
            ) VALUES (
              %s, 'running', %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (run_id, triggered_by, filename or None, digest, base_url, commit,
             Json(options or {}), _worker_id(), len(rows), started),
        ))

        log.info("run %s started — %d row(s), commit=%s", run_id, len(rows), commit)
        return store

    # --- safety net ------------------------------------------------------

    def _safe(self, fn, *, what: str = "write") -> Any:
        """Run a remote write, swallowing anything it throws.

        Deliberately broad: the failure modes here are network, auth, schema
        drift and library-internal errors, and the correct response to all of
        them is identical — log it, keep automating.
        """
        if not self.remote:
            return None
        try:
            return fn()
        except Exception as exc:
            log.warning("store %s failed (continuing): %s", what, exc)
            return None

    def event(self, message: str, *, level: str = "info", row_id: str | None = None,
              step_id: str | None = None, data: dict | None = None) -> None:
        entry = {"level": level, "message": message, "at": _now(),
                 "row_id": row_id, "data": data or {}}
        with self._lock:
            self.state["events"].append(entry)
            del self.state["events"][:-500]      # the UI only ever shows the tail
        self._safe(lambda: execute(
            """
            INSERT INTO events (run_id, row_id, step_id, level, message, data)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (self.run_id, row_id, step_id, level, message[:2000], Json(data or {})),
        ), what="event")

    # --- rows ------------------------------------------------------------

    def row_started(self, row: dict, index: int = 0) -> str:
        row_id = str(uuid.uuid4())
        state = {
            "id": row_id, "sku": row.get("sku"), "status": "running",
            "source_sheet": row.get("source_sheet"), "row_index": index,
            "product_url": None, "error": None, "error_class": None,
            "started_at": _now(), "finished_at": None, "duration_ms": None,
            "steps": [], "colour_tiers": {},
        }
        with self._lock:
            self.state["rows"][row_id] = state
            self.state["order"].append(row_id)

        started = _now()
        self._safe(lambda: execute(
            """
            INSERT INTO run_rows (
              id, run_id, sku, source_sheet, row_index, status, payload, started_at
            ) VALUES (%s, %s, %s, %s, %s, 'running', %s, %s)
            """,
            (row_id, self.run_id, row.get("sku"), row.get("source_sheet"),
             index, Json(row), started),
        ), what="row insert")
        return row_id

    def row_finished(self, row_id: str, *, status: str, error: str | None = None,
                     error_class: str | None = None, product_url: str | None = None,
                     product_id: str | None = None,
                     colour_tiers: dict | None = None) -> None:
        with self._lock:
            state = self.state["rows"].get(row_id, {})
            state.update(status=status, error=error, error_class=error_class,
                         product_url=product_url, finished_at=_now(),
                         colour_tiers=colour_tiers or {})
            if colour_tiers:
                for tier, count in colour_tiers.items():
                    totals = self.state["colour_tiers"]
                    totals[tier] = totals.get(tier, 0) + count

        finished = _now()
        err = (error or None) and error[:2000]
        tiers = colour_tiers or {}
        self._safe(lambda: execute(
            """
            UPDATE run_rows SET
              status = %s, error = %s, error_class = %s,
              product_url = %s, product_id = %s, finished_at = %s,
              colour_tiers = %s
            WHERE id = %s
            """,
            (status, err, error_class, product_url, product_id, finished,
             Json(tiers), row_id),
        ), what="row update")

    # --- steps -----------------------------------------------------------

    @contextmanager
    def step(self, row_id: str, name: str, *, attempt: int = 1):
        """Time one action and record how it ended.

        Re-raises on failure — this records what happened, it does not decide
        what to do about it. Retry policy lives in app.errors, applied by the
        runner, so there is one place that knows the rules.
        """
        step_id = str(uuid.uuid4())
        started = time.monotonic()
        started_at = _now()          # wall clock, so the UI can interleave
        detail: dict[str, Any] = {}  # steps and events on one timeline
        try:
            yield detail
        except BaseException as exc:
            classified = classify(exc)
            self._finish_step(step_id, row_id, name, "failed", started,
                              started_at, attempt, detail,
                              classified.error_class.value, str(exc))
            raise
        else:
            self._finish_step(step_id, row_id, name, "ok", started, started_at,
                              attempt, detail)

    def _finish_step(self, step_id, row_id, name, status, started, started_at,
                     attempt, detail, error_class=None, error=None) -> None:
        duration_ms = int((time.monotonic() - started) * 1000)
        record = {"id": step_id, "name": name, "status": status,
                  "attempt": attempt, "duration_ms": duration_ms,
                  "error_class": error_class, "error": error, "detail": detail,
                  "started_at": started_at}
        with self._lock:
            row = self.state["rows"].get(row_id)
            if row is not None:
                row["steps"].append(record)

        err = (error or None) and error[:2000]
        self._safe(lambda: execute(
            """
            INSERT INTO row_steps (
              id, run_id, row_id, name, status, attempt, error_class, error,
              detail, started_at, duration_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (step_id, self.run_id, row_id, name, status, attempt,
             error_class, err, Json(detail), started_at, duration_ms),
        ), what="step insert")

    # --- colour resolution audit -----------------------------------------

    def record_inference(self, resolution, row_id: str | None = None) -> None:
        """Audit one tier-3 call: what went in, what came back, and — the field
        that actually matters — whether it survived verification."""
        result = getattr(resolution, "llm_result", None)
        if result is None:
            return
        with self._lock:
            self.state["llm_cost_usd"] = round(
                self.state["llm_cost_usd"] + result.cost_usd, 6)

        self._safe(lambda: execute(
            """
            INSERT INTO llm_inferences (
              run_id, row_id, scope, model, input, output, accepted,
              reject_reason, prompt_tokens, completion_tokens, cost_usd, latency_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (self.run_id, row_id, resolution.scope, result.model or "unknown",
             Json({"colours": list(result.assignments)}),
             Json({"assignments": result.assignments,
                   "confidence": result.confidence,
                   "reasoning": result.reasoning}),
             bool(resolution.verified), resolution.error,
             result.prompt_tokens, result.completion_tokens,
             result.cost_usd, result.latency_ms),
        ), what="llm audit")

    # --- created products (the concurrency-safe ledger) -------------------

    def claim_product(self, base_url: str, sku: str, product_url: str,
                      product_id: str | None = None) -> None:
        """Record that this run created a product.

        Replaces var/created-products.json. The unique key on (base_url, sku)
        is what makes two concurrent workers safe: the second insert conflicts
        instead of both believing they own the product.
        """
        self._safe(lambda: execute(
            """
            INSERT INTO created_products (
              base_url, sku, product_url, product_id, run_id, deleted_at
            ) VALUES (%s, %s, %s, %s, %s, NULL)
            ON CONFLICT (base_url, sku) DO UPDATE SET
              product_url = EXCLUDED.product_url,
              product_id  = EXCLUDED.product_id,
              run_id      = EXCLUDED.run_id,
              -- Clearing this is not tidiness. The row is reused per (base_url,
              -- sku), so a SKU that was rolled back and then created again
              -- inherits the old tombstone unless it is explicitly cleared — and
              -- a claim that says "deleted" while the product exists is invisible
              -- to owned_skus(), which silently disarms the ownership guard, and
              -- to the rollback, which then reports it created nothing.
              deleted_at  = NULL
            """,
            (base_url, sku, product_url, product_id, self.run_id),
        ), what="claim")

    def release_product(self, base_url: str, sku: str) -> None:
        """Retire an ownership claim — the product is gone from the catalogue.

        Soft, not a delete: the row stays as the record that this automation
        once created that SKU, and owned_skus() already filters on deleted_at
        being null. Stamping it is what stops a claim outliving its product,
        which would let a later run treat a SKU the client has since created
        for themselves as ours and edit it.
        """
        when = _now()
        self._safe(lambda: execute(
            """
            UPDATE created_products
               SET deleted_at = %s
             WHERE base_url = %s AND sku = %s AND deleted_at IS NULL
            """,
            (when, base_url, sku),
        ), what="release")

    def owned_skus(self, base_url: str) -> set[str]:
        """SKUs this automation created on that store, across all runs."""
        rows = self._safe(lambda: fetch_all(
            """
            SELECT sku FROM created_products
             WHERE base_url = %s AND deleted_at IS NULL
            """,
            (base_url,),
        ), what="owned")
        return {r["sku"] for r in (rows or [])}

    # --- finishing -------------------------------------------------------

    def finish(self, *, error: str | None = None) -> dict:
        with self._lock:
            counts: dict[str, int] = {}
            for row in self.state["rows"].values():
                counts[row["status"]] = counts.get(row["status"], 0) + 1

            failed = counts.get("failed", 0)
            done = counts.get("added", 0) + counts.get("resumed", 0)
            if error:
                status = "failed"
            elif failed and done:
                status = "partial"
            elif failed:
                status = "failed"
            else:
                status = "succeeded"

            duration_ms = int((time.monotonic() - self._started) * 1000)
            self.state.update(status=status, counts=counts, error=error,
                              finished_at=_now(), duration_ms=duration_ms)
            snapshot = dict(self.state)

        finished = _now()
        self._safe(lambda: execute(
            """
            UPDATE runs SET
              status = %s, counts = %s, error = %s,
              finished_at = %s, duration_ms = %s
            WHERE id = %s
            """,
            (status, Json(counts), error, finished, duration_ms, self.run_id),
        ), what="run finish")

        log.info("run %s %s in %dms — %s", self.run_id, status, duration_ms, counts)
        return snapshot

    # --- what the UI reads -----------------------------------------------

    def snapshot(self) -> dict:
        """A JSON-safe view for the front end. Rows in payload order, because
        a progress list that reorders itself as things finish is unreadable."""
        with self._lock:
            rows = [self.state["rows"][rid] for rid in self.state["order"]
                    if rid in self.state["rows"]]
            done = sum(1 for r in rows if r["status"] not in {"running", "pending"})
            return {
                "id": self.run_id,
                "status": self.state["status"],
                "source_filename": self.state.get("source_filename", ""),
                "commit": self.state.get("commit", False),
                "target_base_url": self.state.get("target_base_url", ""),
                "total_rows": self.state["total_rows"],
                "completed": done,
                "counts": self.state["counts"],
                "colour_tiers": self.state["colour_tiers"],
                "llm_cost_usd": self.state["llm_cost_usd"],
                "error": self.state["error"],
                "started_at": self.state["started_at"],
                "finished_at": self.state["finished_at"],
                "remote": self.remote,
                "rows": rows,
                "events": self.state["events"][-80:],
            }


# ---------------------------------------------------------------------------
# Reading runs back out.
#
# RunStore is the WRITE side and its in-memory mirror only lives as long as the
# process. These read the same records back from Postgres, in the shape the web
# layer already polls, so a run survives a restart instead of vanishing from
# the console while every row of it is still in the database.
#
# The shape is RunStore.snapshot()'s, deliberately: the front end must not be
# able to tell whether a run is live in memory or reassembled from the store.
# ---------------------------------------------------------------------------

def _iso(value: Any) -> Any:
    """Timestamps as ISO strings, matching what the in-memory mirror holds."""
    return value.isoformat() if isinstance(value, datetime) else value


def _summary(run: dict, *, completed: int = 0) -> dict:
    return {
        "id": str(run["id"]),
        "status": run.get("status"),
        "source_filename": run.get("source_filename") or "",
        "commit": bool(run.get("commit")),
        "target_base_url": run.get("target_base_url") or "",
        "total_rows": run.get("total_rows") or 0,
        "completed": completed,
        "counts": run.get("counts") or {},
        "colour_tiers": {},
        "llm_cost_usd": 0.0,
        "error": run.get("error"),
        "started_at": _iso(run.get("started_at")),
        "finished_at": _iso(run.get("finished_at")),
        "remote": True,
    }


def recent_runs(limit: int = 50) -> list[dict]:
    """Run summaries from the store, newest first — no rows, no events.

    Summary only, like RunManager.list(): a run list that carried every row of
    every run would grow without bound and is not what the screen shows.
    """
    if not store_available():
        return []
    try:
        runs = fetch_all(
            """
            SELECT id, status, source_filename, commit, target_base_url,
                   total_rows, counts, error, started_at, finished_at
              FROM runs
             ORDER BY created_at DESC
             LIMIT %s
            """,
            (limit,),
        )
    except Exception as exc:
        log.warning("could not list runs from the store: %s", exc)
        return []

    out = []
    for run in runs:
        counts = run.get("counts") or {}
        # 'completed' drives the progress bar. Every terminal status counts;
        # summing them is cheaper than a second query per run.
        done = sum(n for status, n in counts.items()
                   if status not in {"running", "pending"})
        out.append(_summary(run, completed=done))
    return out


def load_snapshot(run_id: str) -> dict | None:
    """One run, reassembled from the store, in the shape the console polls.

    Returns None when the run is not there — the caller turns that into a 404,
    the same answer it gave when memory was the only place a run could live.
    """
    if not store_available():
        return None
    try:
        runs = fetch_all(
            """
            SELECT id, status, source_filename, commit, target_base_url,
                   total_rows, counts, error, started_at, finished_at
              FROM runs WHERE id = %s
            """,
            (run_id,),
        )
        if not runs:
            return None
        rows = fetch_all(
            """
            SELECT id, sku, source_sheet, row_index, status, product_url,
                   product_id, error, error_class, colour_tiers,
                   started_at, finished_at, duration_ms
              FROM run_rows WHERE run_id = %s ORDER BY row_index, sku
            """,
            (run_id,),
        )
        steps = fetch_all(
            """
            SELECT id, row_id, name, status, attempt, error_class, error,
                   detail, started_at, duration_ms
              FROM row_steps WHERE run_id = %s ORDER BY started_at
            """,
            (run_id,),
        )
        # The tail, to match the live mirror. The console shows recent history,
        # not the whole narrative, and a long run's event log is large.
        events = fetch_all(
            """
            SELECT * FROM (
              SELECT row_id, level, message, data, at
                FROM events WHERE run_id = %s ORDER BY at DESC LIMIT 80
            ) recent ORDER BY at
            """,
            (run_id,),
        )
        cost = fetch_all(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM llm_inferences "
            "WHERE run_id = %s",
            (run_id,),
        )
    except Exception as exc:
        log.warning("could not load run %s from the store: %s", run_id, exc)
        return None

    by_row: dict[str, list[dict]] = {}
    for step in steps:
        by_row.setdefault(str(step["row_id"]), []).append({
            "id": str(step["id"]), "name": step["name"], "status": step["status"],
            "attempt": step["attempt"], "duration_ms": step["duration_ms"],
            "error_class": step["error_class"], "error": step["error"],
            "detail": step["detail"] or {}, "started_at": _iso(step["started_at"]),
        })

    tiers: dict[str, int] = {}
    shaped = []
    for row in rows:
        row_id = str(row["id"])
        row_tiers = row.get("colour_tiers") or {}
        for tier, n in row_tiers.items():
            tiers[tier] = tiers.get(tier, 0) + n
        shaped.append({
            "id": row_id, "sku": row["sku"], "status": row["status"],
            "source_sheet": row.get("source_sheet"),
            "row_index": row.get("row_index") or 0,
            "product_url": row.get("product_url"),
            "product_id": row.get("product_id"),
            "error": row.get("error"), "error_class": row.get("error_class"),
            "started_at": _iso(row.get("started_at")),
            "finished_at": _iso(row.get("finished_at")),
            "duration_ms": row.get("duration_ms"),
            "steps": by_row.get(row_id, []),
            "colour_tiers": row_tiers,
        })

    snapshot = _summary(runs[0])
    snapshot.update(
        completed=sum(1 for r in shaped
                      if r["status"] not in {"running", "pending"}),
        colour_tiers=tiers,
        llm_cost_usd=float(cost[0]["total"] or 0) if cost else 0.0,
        rows=shaped,
        events=[{"level": e["level"], "message": e["message"],
                 "at": _iso(e["at"]),
                 "row_id": str(e["row_id"]) if e["row_id"] else None,
                 "data": e["data"] or {}} for e in events],
    )
    return snapshot
