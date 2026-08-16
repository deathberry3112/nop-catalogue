#!/usr/bin/env python3
"""Reading runs back out of the store — app/store/run_store.py.

RunStore's in-memory mirror only lives as long as the process. These cover the
read side that lets a run outlive it, and the property that actually matters:
the front end must not be able to tell whether a run came from memory or from
Postgres, because it polls one endpoint for both.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.store import run_store


RUN = "324f7343-5fb5-4df1-bd41-5ffd2d3e4487"
ROW = "aaaaaaaa-1111-2222-3333-444444444444"
STEP = "bbbbbbbb-1111-2222-3333-444444444444"
WHEN = datetime(2026, 8, 14, 1, 9, 25, tzinfo=timezone.utc)


def wire(monkeypatch, *, runs=None, rows=None, steps=None, events=None,
         cost=0, available=True, explode=False):
    """Fake fetch_all, dispatching on which table the query names."""
    def fake_fetch_all(sql, params=None):
        if explode:
            raise RuntimeError("connection refused")
        low = " ".join(sql.lower().split())
        if "from runs" in low:
            return runs if runs is not None else [run_record()]
        if "from run_rows" in low:
            return rows or []
        if "from row_steps" in low:
            return steps or []
        if "from events" in low:
            return events or []
        if "from llm_inferences" in low:
            return [{"total": cost}]
        return []

    monkeypatch.setattr(run_store, "store_available", lambda: available)
    monkeypatch.setattr(run_store, "fetch_all", fake_fetch_all)


def run_record(**over):
    return {"id": RUN, "status": "partial", "source_filename": "batch.xlsx",
            "commit": True, "target_base_url": "https://shop.example.com/admin",
            "total_rows": 2, "counts": {"added": 1, "failed": 1},
            "error": None, "started_at": WHEN, "finished_at": WHEN, **over}


def row_record(**over):
    return {"id": ROW, "sku": "A-1", "source_sheet": "Sheet1", "row_index": 1,
            "status": "failed", "product_url": "https://shop/Product/Edit/1",
            "product_id": "1", "error": "boom", "error_class": "unknown",
            "colour_tiers": {"map": 3}, "started_at": WHEN, "finished_at": WHEN,
            "duration_ms": 1200, **over}


# --- the shape contract -------------------------------------------------------

def test_a_stored_run_looks_exactly_like_a_live_one(monkeypatch):
    """The one that matters. The console polls a single endpoint for both, so a
    reassembled run carrying a different shape would break the screen only for
    runs old enough to have left memory — the worst kind of bug to find."""
    live = run_store.RunStore(RUN, remote=False).snapshot()

    wire(monkeypatch, rows=[row_record()])
    stored = run_store.load_snapshot(RUN)

    assert set(stored) == set(live), "snapshot keys drifted apart"


def test_a_stored_row_carries_what_the_console_reads(monkeypatch):
    wire(monkeypatch, rows=[row_record()])
    row = run_store.load_snapshot(RUN)["rows"][0]
    # Exactly the fields renderRows/renderLog reach for.
    for field in ("id", "sku", "status", "row_index", "product_url", "error",
                  "colour_tiers", "steps"):
        assert field in row, f"{field} missing from a stored row"


def test_timestamps_come_back_as_strings(monkeypatch):
    """The live mirror holds ISO strings. Handing the front end datetimes here
    would serialise differently and only for restored runs."""
    wire(monkeypatch, rows=[row_record()])
    snap = run_store.load_snapshot(RUN)
    assert isinstance(snap["started_at"], str)
    assert isinstance(snap["rows"][0]["started_at"], str)


# --- assembling ---------------------------------------------------------------

def test_steps_are_attached_to_their_own_row(monkeypatch):
    wire(monkeypatch, rows=[row_record()],
         steps=[{"id": STEP, "row_id": ROW, "name": "save", "status": "ok",
                 "attempt": 1, "error_class": None, "error": None,
                 "detail": {}, "started_at": WHEN, "duration_ms": 90}])
    row = run_store.load_snapshot(RUN)["rows"][0]
    assert [s["name"] for s in row["steps"]] == ["save"]


def test_colour_tiers_are_summed_across_rows(monkeypatch):
    wire(monkeypatch, rows=[row_record(colour_tiers={"map": 3}),
                            row_record(id="other", sku="A-2",
                                       colour_tiers={"map": 2, "llm": 1})])
    assert run_store.load_snapshot(RUN)["colour_tiers"] == {"map": 5, "llm": 1}


def test_completed_counts_only_finished_rows(monkeypatch):
    wire(monkeypatch, rows=[row_record(status="added"),
                            row_record(id="b", sku="A-2", status="running"),
                            row_record(id="c", sku="A-3", status="pending")])
    assert run_store.load_snapshot(RUN)["completed"] == 1


def test_the_llm_cost_is_a_float_not_a_decimal(monkeypatch):
    """It is summed by Postgres as numeric; the UI calls .toFixed() on it."""
    wire(monkeypatch, rows=[row_record()], cost=0.0042)
    assert run_store.load_snapshot(RUN)["llm_cost_usd"] == pytest.approx(0.0042)


# --- refusing, quietly --------------------------------------------------------

def test_an_unknown_run_is_none_not_an_empty_run(monkeypatch):
    """The caller turns this into a 404 — the same answer it gave when memory
    was the only place a run could live."""
    wire(monkeypatch, runs=[])
    assert run_store.load_snapshot(RUN) is None


def test_no_store_means_nothing_to_read(monkeypatch):
    wire(monkeypatch, available=False)
    assert run_store.recent_runs() == []
    assert run_store.load_snapshot(RUN) is None


def test_an_unreachable_store_degrades_rather_than_raising(monkeypatch):
    """Same rule as every write in this module: telemetry must never break the
    thing it observes. A console that 500s because Postgres blinked is worse
    than one showing only the runs this process is holding."""
    wire(monkeypatch, explode=True)
    assert run_store.recent_runs() == []
    assert run_store.load_snapshot(RUN) is None


# --- the list -----------------------------------------------------------------

def test_the_run_list_carries_no_rows_or_events(monkeypatch):
    """A summary list that dragged every row of every run along would grow
    without bound, and the screen does not show them."""
    wire(monkeypatch)
    summary = run_store.recent_runs()[0]
    assert "rows" not in summary and "events" not in summary


def test_the_run_list_reports_progress_from_the_counts(monkeypatch):
    wire(monkeypatch, runs=[run_record(
        total_rows=10, counts={"added": 4, "failed": 2, "running": 1})])
    assert run_store.recent_runs()[0]["completed"] == 6
