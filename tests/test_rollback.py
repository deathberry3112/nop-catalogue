#!/usr/bin/env python3
"""Guard rails on the rollback — app/rollback.py.

This is the only code in the project that deletes from a live catalogue, so
these tests are almost entirely about what it REFUSES to do. The happy path is
one test; the rest pin down the ways a rollback could reach a product it has no
business touching.
"""

from __future__ import annotations

import pytest

from app import rollback


RUN = "11111111-1111-1111-1111-111111111111"
STORE_URL = "https://shop.example.com/admin"


def wire(monkeypatch, *, runs=None, claimed=None, rows=None, available=True):
    """Fake fetch_all so build_plan never needs a live database.

    Both child tables are queried with WHERE run_id = %s, so the fixtures
    belong to RUN unless a test deliberately says otherwise.
    """
    runs_data = runs if runs is not None else [
        {"id": RUN, "status": "succeeded", "target_base_url": STORE_URL,
         "commit": True}]
    claimed_data = [{"run_id": RUN, **c} for c in (claimed or [])]
    rows_data = [{"run_id": RUN, **r} for r in (rows or [])]

    def fake_fetch_all(sql, params=None):
        sql_l = " ".join(sql.lower().split())
        run_id = params[0] if params else None
        if "from runs" in sql_l:
            return [r for r in runs_data if r["id"] == run_id]
        if "from created_products" in sql_l:
            out = [r for r in claimed_data if r["run_id"] == run_id]
            if "deleted_at is null" in sql_l:
                out = [r for r in out if r.get("deleted_at") is None]
            return out
        if "from run_rows" in sql_l:
            return [r for r in rows_data if r["run_id"] == run_id]
        return []

    monkeypatch.setattr(rollback, "store_available", lambda: available)
    monkeypatch.setattr(rollback, "fetch_all", fake_fetch_all)


def claim(sku, product_id="100", base_url=STORE_URL, deleted_at=None):
    return {"sku": sku, "product_id": product_id, "base_url": base_url,
            "product_url": f"{base_url}/Product/Edit/{product_id}",
            "deleted_at": deleted_at}


def row(sku, status="added", product_id="100"):
    return {"sku": sku, "status": status, "product_id": product_id}


# --- the happy path -----------------------------------------------------------

def test_the_plan_is_exactly_what_this_run_claimed(monkeypatch):
    wire(monkeypatch,
         claimed=[claim("A-1", "101"), claim("A-2", "102")],
         rows=[row("A-1", product_id="101"), row("A-2", product_id="102")])
    plan = rollback.build_plan(RUN)
    assert plan.ok
    assert plan.skus == ["A-1", "A-2"]
    assert plan.base_url == STORE_URL


# --- what must never be in the set --------------------------------------------

def test_a_product_this_run_only_resumed_is_never_included(monkeypatch):
    """Resuming means an earlier run created it. Deleting it here would undo
    somebody else's work under the banner of undoing this run."""
    wire(monkeypatch, claimed=[claim("A-1")],
         rows=[row("A-1", status="resumed")])
    plan = rollback.build_plan(RUN)
    assert plan.skus == []
    assert any("resumed" in x for x in plan.excluded)


def test_a_skipped_product_is_never_included(monkeypatch):
    """'skipped' is the runner's word for "this is the client's product".
    It is the single most important exclusion in the file."""
    wire(monkeypatch, claimed=[claim("THEIRS")],
         rows=[row("THEIRS", status="skipped")])
    plan = rollback.build_plan(RUN)
    assert plan.skus == []
    assert any("skipped" in x for x in plan.excluded)


def test_a_previewed_row_is_never_included(monkeypatch):
    wire(monkeypatch, claimed=[claim("A-1")], rows=[row("A-1", status="previewed")])
    assert rollback.build_plan(RUN).skus == []


def test_a_release_claim_is_not_reclaimed(monkeypatch):
    """deleted_at set means it is already gone; a second rollback must not try
    to delete it again."""
    wire(monkeypatch, claimed=[claim("A-1", deleted_at="2026-01-01T00:00:00Z")],
         rows=[row("A-1")])
    plan = rollback.build_plan(RUN)
    assert plan.skus == []
    assert plan.blocked


def test_a_recreated_sku_is_deletable_again(monkeypatch):
    """The bug this exists to stop: roll back a SKU, create it again, and the
    reused claim row keeps its old tombstone unless claim_product clears it —
    leaving a live product that nothing believes it owns. Here the claim is
    live again, so the rollback can see it."""
    wire(monkeypatch, claimed=[claim("A-1", deleted_at=None)], rows=[row("A-1")])
    assert rollback.build_plan(RUN).skus == ["A-1"]


def test_already_deleted_is_not_reported_as_never_created(monkeypatch):
    """"created nothing" and "created things that are already gone" are
    opposite facts and were reading identically."""
    wire(monkeypatch,
         claimed=[claim("A-1", deleted_at="2026-01-01T00:00:00Z")],
         rows=[row("A-1", status="added")])
    plan = rollback.build_plan(RUN)
    assert plan.blocked and "no longer claimed" in plan.blocked
    assert plan.excluded, "the reason must survive into the dialog"


def test_a_claim_against_a_different_store_is_excluded(monkeypatch):
    wire(monkeypatch,
         claimed=[claim("A-1"), claim("ELSEWHERE", base_url="https://other.example.com/admin")],
         rows=[row("A-1"), row("ELSEWHERE")])
    plan = rollback.build_plan(RUN)
    assert plan.skus == ["A-1"]
    assert any("other.example.com" in x for x in plan.excluded)


def test_another_runs_products_are_not_in_this_plan(monkeypatch):
    """The filter is run_id. A product created by a different run has its own
    rollback and must not appear in this one."""
    other = "22222222-2222-2222-2222-222222222222"
    runs = [{"id": RUN, "status": "succeeded", "target_base_url": STORE_URL,
             "commit": True}]
    claimed = [dict(claim("MINE"), run_id=RUN),
               dict(claim("THEIRS"), run_id=other)]
    rows = [dict(row("MINE"), run_id=RUN)]

    def fake_fetch_all(sql, params=None):
        sql_l = " ".join(sql.lower().split())
        run_id = params[0] if params else None
        if "from runs" in sql_l:
            return [r for r in runs if r["id"] == run_id]
        if "from created_products" in sql_l:
            out = [r for r in claimed if r["run_id"] == run_id]
            if "deleted_at is null" in sql_l:
                out = [r for r in out if r.get("deleted_at") is None]
            return out
        if "from run_rows" in sql_l:
            return [r for r in rows if r["run_id"] == run_id]
        return []

    monkeypatch.setattr(rollback, "store_available", lambda: True)
    monkeypatch.setattr(rollback, "fetch_all", fake_fetch_all)
    assert rollback.build_plan(RUN).skus == ["MINE"]


# --- a failed row that still left a product behind -----------------------------

def test_a_failed_row_that_created_a_product_is_included(monkeypatch):
    """The runner claims a product the instant it saves. A row that died later
    left a real product; leaving it is how a 'rollback' quietly orphans one."""
    wire(monkeypatch, claimed=[claim("A-1")], rows=[row("A-1", status="failed")])
    plan = rollback.build_plan(RUN)
    assert plan.skus == ["A-1"]
    assert plan.products[0].row_status == "failed"


# --- refusing the whole operation ---------------------------------------------

def test_a_running_run_cannot_be_rolled_back(monkeypatch):
    wire(monkeypatch,
         runs=[{"id": RUN, "status": "running", "target_base_url": STORE_URL}],
         claimed=[claim("A-1")], rows=[row("A-1")])
    plan = rollback.build_plan(RUN)
    assert plan.blocked and "still running" in plan.blocked
    assert not plan.ok


def test_no_store_means_no_deletion(monkeypatch):
    """Without the record there is no defensible answer to "what did this run
    create?" — so nothing is deleted, rather than something being guessed."""
    wire(monkeypatch, available=False)
    plan = rollback.build_plan(RUN)
    assert plan.blocked and "not configured" in plan.blocked


def test_an_unknown_run_is_refused(monkeypatch):
    wire(monkeypatch, runs=[])
    assert rollback.build_plan(RUN).blocked


def test_a_run_that_created_nothing_is_refused(monkeypatch):
    wire(monkeypatch, claimed=[], rows=[row("A-1", status="previewed")])
    plan = rollback.build_plan(RUN)
    assert not plan.ok
    assert "nothing to undo" in plan.blocked


def test_execute_refuses_a_blocked_plan():
    """The plan carries its own refusal; execute must honour it even if a
    caller ignored `blocked` and passed it in anyway."""
    plan = rollback.Plan(run_id=RUN, blocked="nope")
    with pytest.raises(Exception) as exc:
        rollback.execute(object(), plan)
    assert "refused" in str(exc.value)


# --- an added row with no ownership claim --------------------------------------

def test_an_added_row_without_a_claim_is_reported_not_deleted(monkeypatch):
    """The two records disagree. created_products is authoritative, so the
    product is left alone — but silently narrowing the scope would be worse
    than saying so."""
    wire(monkeypatch, claimed=[], rows=[row("A-1", status="added")])
    plan = rollback.build_plan(RUN)
    assert plan.skus == []
    assert any("no ownership claim" in x for x in plan.excluded)


# --- the shape the API relies on ----------------------------------------------

def test_the_plan_serialises_for_the_confirmation_dialog(monkeypatch):
    wire(monkeypatch, claimed=[claim("A-1", "101")], rows=[row("A-1", product_id="101")])
    data = rollback.build_plan(RUN).as_dict()
    assert data["count"] == 1
    assert data["products"][0]["sku"] == "A-1"
    assert data["products"][0]["product_id"] == "101"
    assert data["blocked"] is None


def test_skus_are_sorted_so_the_confirmation_check_is_order_independent(monkeypatch):
    wire(monkeypatch, claimed=[claim("B"), claim("A")], rows=[row("A"), row("B")])
    assert rollback.build_plan(RUN).skus == ["A", "B"]
