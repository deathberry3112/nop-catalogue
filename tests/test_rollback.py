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
OTHER_RUN = "22222222-2222-2222-2222-222222222222"
STORE_URL = "https://shop.example.com/admin"

#: RUN's window. A claim stamped DURING it belongs to the products this run
#: worked on; one stamped AFTER it was made by something else, which is a
#: distinction build_plan draws and therefore one the fixtures have to be able
#: to express.
BEFORE_RUN = "2026-01-01T00:00:00+00:00"
DURING_RUN = "2026-01-01T00:30:00+00:00"
RUN_ENDED = "2026-01-01T01:00:00+00:00"
AFTER_RUN = "2026-01-02T00:00:00+00:00"


def wire(monkeypatch, *, runs=None, claimed=None, rows=None, available=True):
    """Fake fetch_all so build_plan never needs a live database.

    `run_rows` is still read with WHERE run_id = %s, so rows belong to RUN
    unless a test says otherwise. `created_products` is NOT: the ledger is now
    consulted per SKU for provenance, so the fake reproduces the real
    predicate — this run's claims, plus any claim on a SKU this run touched,
    whichever run made it. Getting that wrong would hide the entire resumed
    case, which is the one thing these tests exist to cover.
    """
    runs_data = runs if runs is not None else [
        {"id": RUN, "status": "succeeded", "target_base_url": STORE_URL,
         "commit": True, "finished_at": RUN_ENDED}]
    claimed_data = [{"run_id": RUN, "created_at": DURING_RUN, **c}
                    for c in (claimed or [])]
    rows_data = [{"run_id": RUN, **r} for r in (rows or [])]

    def fake_fetch_all(sql, params=None):
        sql_l = " ".join(sql.lower().split())
        run_id = params[0] if params else None
        if "from runs" in sql_l:
            return [r for r in runs_data if r["id"] == run_id]
        if "from created_products" in sql_l:
            base_url = params[1] if params and len(params) > 2 else None
            skus = set(params[2]) if params and len(params) > 2 else set()
            return [c for c in claimed_data
                    if c["run_id"] == run_id
                    or (base_url is not None
                        and c.get("base_url") == base_url
                        and c["sku"] in skus)]
        if "from run_rows" in sql_l:
            return [r for r in rows_data if r["run_id"] == run_id]
        return []

    monkeypatch.setattr(rollback, "store_available", lambda: available)
    monkeypatch.setattr(rollback, "fetch_all", fake_fetch_all)


def claim(sku, product_id="100", base_url=STORE_URL, deleted_at=None,
          run_id=None, created_at=None):
    entry = {"sku": sku, "product_id": product_id, "base_url": base_url,
             "product_url": f"{base_url}/Product/Edit/{product_id}",
             "deleted_at": deleted_at}
    if run_id is not None:
        entry["run_id"] = run_id
    if created_at is not None:
        entry["created_at"] = created_at
    return entry


def row(sku, status="added", product_id="100"):
    return {"sku": sku, "status": status, "product_id": product_id}


def origins(plan):
    """{sku: origin} — the provenance tier the dialog groups on."""
    return {p.sku: p.origin for p in plan.products}


# --- the happy path -----------------------------------------------------------

def test_the_plan_is_exactly_what_this_run_claimed(monkeypatch):
    wire(monkeypatch,
         claimed=[claim("A-1", "101"), claim("A-2", "102")],
         rows=[row("A-1", product_id="101"), row("A-2", product_id="102")])
    plan = rollback.build_plan(RUN)
    assert plan.ok
    assert plan.skus == ["A-1", "A-2"]
    assert plan.base_url == STORE_URL


# --- everything the run worked on, labelled by who made it --------------------
#
# The scope used to be "products this run created" and is now "products this run
# worked on". These are the tests that pin the difference: membership widened,
# and each product carries the provenance the narrow set used to imply.

def test_a_product_this_run_resumed_is_included_and_marked_as_such(monkeypatch):
    """Resuming means an EARLIER run created it: the claim is live but stamped
    with that run's id. It is in scope — this run edited the product — and it
    is labelled `resumed` so the dialog can say deleting it undoes more than
    this run."""
    wire(monkeypatch,
         claimed=[claim("A-1", run_id=OTHER_RUN, created_at=BEFORE_RUN)],
         rows=[row("A-1", status="resumed")])
    plan = rollback.build_plan(RUN)
    assert plan.skus == ["A-1"]
    assert origins(plan) == {"A-1": rollback.RESUMED}
    assert plan.products[0].owned, "the automation did create it, just earlier"
    assert not plan.unowned


def test_a_skipped_product_is_included_and_marked_as_the_clients(monkeypatch):
    """'skipped' is the runner's word for "this is the client's product" — the
    run found it by SKU and wrote nothing to it. It is in scope by explicit
    instruction, with no ownership claim behind it, so it is labelled `foreign`
    and reported as unowned. Nothing in this project can put one back."""
    wire(monkeypatch, claimed=[], rows=[row("THEIRS", status="skipped")])
    plan = rollback.build_plan(RUN)
    assert plan.skus == ["THEIRS"]
    assert origins(plan) == {"THEIRS": rollback.FOREIGN}
    assert not plan.products[0].owned
    assert [p.sku for p in plan.unowned] == ["THEIRS"]


def test_the_dangerous_half_of_a_plan_is_separable(monkeypatch):
    """The dialog has to ask a different question about the client's products
    than about ours, so the plan has to be able to tell them apart without the
    caller re-deriving it."""
    wire(monkeypatch,
         claimed=[claim("MINE", "101"),
                  claim("EARLIER", "102", run_id=OTHER_RUN,
                        created_at=BEFORE_RUN)],
         rows=[row("MINE", product_id="101"),
               row("EARLIER", status="resumed", product_id="102"),
               row("THEIRS", status="skipped", product_id="103")])
    plan = rollback.build_plan(RUN)
    assert plan.skus == ["EARLIER", "MINE", "THEIRS"]
    assert origins(plan) == {"MINE": rollback.CREATED,
                             "EARLIER": rollback.RESUMED,
                             "THEIRS": rollback.FOREIGN}
    assert plan.origins == {rollback.CREATED: 1, rollback.RESUMED: 1,
                            rollback.FOREIGN: 1}
    assert [p.sku for p in plan.unowned] == ["THEIRS"]
    assert plan.as_dict()["unowned_count"] == 1


def test_a_later_runs_replacement_is_not_this_runs_to_delete(monkeypatch):
    """Same SKU, different product. Delete A-1, let a later run create it
    again, then roll THIS run back: the product standing there was made after
    this run finished, so it belongs to that run's rollback and not this one.

    The old ledger-driven set could not make this mistake — it only ever saw
    claims stamped with this run. The widened one can, so it is checked."""
    wire(monkeypatch,
         claimed=[claim("A-1", run_id=OTHER_RUN, created_at=AFTER_RUN)],
         rows=[row("A-1", status="added")])
    plan = rollback.build_plan(RUN)
    assert plan.skus == []
    assert plan.blocked and "later run" in plan.blocked
    assert any("after this run finished" in x for x in plan.excluded)


def test_an_unparseable_timestamp_does_not_silently_drop_a_product(monkeypatch):
    """"Cannot tell when the claim was made" must not read as "it postdates the
    run" — that would quietly shrink the plan. The SKU check in execute() is
    what stands between a stale record and a wrong deletion, not this."""
    wire(monkeypatch, claimed=[claim("A-1", created_at="not a date")],
         rows=[row("A-1")])
    assert rollback.build_plan(RUN).skus == ["A-1"]


# --- what must never be in the set --------------------------------------------

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
    opposite facts and were reading identically.

    Selective rollback makes this the ORDINARY end state — delete a few at a
    time and the last dialog you open is this one — so it says plainly that
    they were deleted rather than reporting a bookkeeping anomaly.
    """
    wire(monkeypatch,
         claimed=[claim("A-1", deleted_at="2026-01-01T00:00:00Z")],
         rows=[row("A-1", status="added")])
    plan = rollback.build_plan(RUN)
    assert plan.blocked and "already been deleted" in plan.blocked
    assert plan.excluded, "the reason must survive into the dialog"
    assert not any("cannot prove" in x for x in plan.excluded), \
        "a product we deleted ourselves is not an unprovable claim"


def test_a_claim_against_a_different_store_is_never_deleted_here(monkeypatch):
    """Two stores can hold the same SKU and they are not the same product. A
    claim from elsewhere confers nothing here, and with no row of this run
    naming it there is nothing to delete."""
    elsewhere = "https://other.example.com/admin"
    wire(monkeypatch,
         claimed=[claim("A-1"), claim("ELSEWHERE", base_url=elsewhere)],
         rows=[row("A-1")])
    plan = rollback.build_plan(RUN)
    assert plan.skus == ["A-1"]
    assert any("other.example.com" in x for x in plan.excluded)


def test_an_offsite_claim_confers_no_ownership_on_this_store(monkeypatch):
    """The run worked on the SKU here, but the only claim is against another
    store. That is not proof we made this store's copy, so it is in scope as
    `unclaimed` rather than being passed off as ours."""
    elsewhere = "https://other.example.com/admin"
    wire(monkeypatch,
         claimed=[claim("BOTH", base_url=elsewhere)],
         rows=[row("BOTH")])
    plan = rollback.build_plan(RUN)
    assert origins(plan) == {"BOTH": rollback.UNCLAIMED}
    assert not plan.products[0].owned


def test_another_runs_products_are_not_in_this_plan(monkeypatch):
    """Membership is this run's rows. A product a different run created and
    this one never touched has its own rollback and must not appear here — the
    ledger holding a live claim on it is not enough to pull it in."""
    wire(monkeypatch,
         claimed=[claim("MINE"),
                  claim("THEIRS", run_id=OTHER_RUN, created_at=BEFORE_RUN)],
         rows=[row("MINE")])
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

def test_an_added_row_without_a_claim_is_offered_but_not_called_ours(monkeypatch):
    """The two records disagree: the run says it added the product, the ledger
    has no live claim. Under the widened scope the product is in scope anyway —
    this run wrote to it — but it is `unclaimed`, not `created`, because the
    one record that could show we made it does not. It is the ordinary shape
    for a run whose ownership came from the local var/ ledger, which carries no
    run_id at all."""
    wire(monkeypatch, claimed=[], rows=[row("A-1", status="added")])
    plan = rollback.build_plan(RUN)
    assert plan.skus == ["A-1"]
    assert origins(plan) == {"A-1": rollback.UNCLAIMED}
    assert not plan.products[0].owned


def test_a_claim_that_went_missing_is_not_reported_as_a_creation(monkeypatch):
    """The distinction that matters in the dialog: `created` means the ledger
    vouches for it, and nothing else may borrow that word."""
    wire(monkeypatch, claimed=[], rows=[row("A-1", status="failed")])
    plan = rollback.build_plan(RUN)
    assert plan.products[0].origin != rollback.CREATED


# --- the shape the API relies on ----------------------------------------------

def test_the_plan_serialises_for_the_confirmation_dialog(monkeypatch):
    wire(monkeypatch, claimed=[claim("A-1", "101")], rows=[row("A-1", product_id="101")])
    data = rollback.build_plan(RUN).as_dict()
    assert data["count"] == 1
    assert data["products"][0]["sku"] == "A-1"
    assert data["products"][0]["product_id"] == "101"
    assert data["blocked"] is None


def test_the_dialog_is_told_provenance_per_product_and_in_total(monkeypatch):
    """The dialog cannot group products it cannot tell apart, and it leads with
    the count of the ones nothing can put back."""
    wire(monkeypatch,
         claimed=[claim("MINE", "101")],
         rows=[row("MINE", product_id="101"),
               row("THEIRS", status="skipped", product_id="103")])
    data = rollback.build_plan(RUN).as_dict()
    assert {p["sku"]: (p["origin"], p["owned"]) for p in data["products"]} == {
        "MINE": (rollback.CREATED, True),
        "THEIRS": (rollback.FOREIGN, False)}
    assert data["origins"] == {rollback.CREATED: 1, rollback.FOREIGN: 1}
    assert data["unowned_count"] == 1


def test_a_skipped_products_page_is_still_addressable(monkeypatch):
    """A 'skipped' row records the URL it found but never a product id — the
    runner never opened the page. The plan has to carry what there is, because
    the dialog links to it and execute() falls back to the SKU check."""
    found = f"{STORE_URL}/Product/Edit/900"
    wire(monkeypatch, claimed=[],
         rows=[{"sku": "THEIRS", "status": "skipped", "product_id": None,
                "product_url": found}])
    doomed = rollback.build_plan(RUN).products[0]
    assert doomed.product_url == found
    assert doomed.product_id is None


def test_skus_are_sorted_so_the_confirmation_check_is_order_independent(monkeypatch):
    wire(monkeypatch, claimed=[claim("B"), claim("A")], rows=[row("A"), row("B")])
    assert rollback.build_plan(RUN).skus == ["A", "B"]


# --- selective rollback: narrow() ---------------------------------------------
#
# The confirmation used to demand an exact match — "the list you sent must equal
# the list I computed". Subsets replaced that, and these pin down the property
# it existed for: a selection may only ever narrow the plan.

def plan_of(monkeypatch, skus):
    wire(monkeypatch, claimed=[claim(s) for s in skus],
         rows=[row(s) for s in skus])
    return rollback.build_plan(RUN)


def test_a_selection_narrows_the_plan_to_what_was_chosen(monkeypatch):
    plan = plan_of(monkeypatch, ["A-1", "A-2", "A-3"])
    assert rollback.narrow(plan, ["A-1", "A-3"]).skus == ["A-1", "A-3"]


def test_narrowing_keeps_the_records_rather_than_rebuilding_them(monkeypatch):
    """execute() re-verifies product_id against the page before clicking, so
    the narrowed plan must carry the same Doomed objects build_plan proved."""
    plan = plan_of(monkeypatch, ["A-1", "A-2"])
    chosen = rollback.narrow(plan, ["A-2"]).products
    assert chosen == [p for p in plan.products if p.sku == "A-2"]


def test_a_sku_outside_the_plan_refuses_the_whole_request(monkeypatch):
    """The one that matters: nothing sent here can widen the scope, and an
    unrecognised SKU stops the deletion rather than joining it."""
    plan = plan_of(monkeypatch, ["A-1", "A-2"])
    with pytest.raises(rollback.SelectionError, match="no longer"):
        rollback.narrow(plan, ["A-1", "SOMEBODY-ELSES"])


def test_a_stale_dialog_deletes_nothing_rather_than_part(monkeypatch):
    """Two operators, one run. The first deleted A-2; the second still has it
    on screen. The second must not get a quiet partial deletion of the rest."""
    plan = plan_of(monkeypatch, ["A-1", "A-3"])       # A-2 already released
    with pytest.raises(rollback.SelectionError):
        rollback.narrow(plan, ["A-1", "A-2", "A-3"])


def test_an_empty_selection_is_refused(monkeypatch):
    plan = plan_of(monkeypatch, ["A-1"])
    with pytest.raises(rollback.SelectionError, match="nothing was selected"):
        rollback.narrow(plan, [])


def test_blank_entries_are_not_a_selection(monkeypatch):
    plan = plan_of(monkeypatch, ["A-1"])
    with pytest.raises(rollback.SelectionError, match="nothing was selected"):
        rollback.narrow(plan, ["", "   "])


def test_selecting_everything_is_just_the_widest_subset(monkeypatch):
    """The old all-or-nothing path still works, unchanged, out of order."""
    plan = plan_of(monkeypatch, ["A-1", "A-2"])
    assert rollback.narrow(plan, ["A-2", "A-1"]).skus == ["A-1", "A-2"]


def test_narrowing_does_not_mutate_the_plan_it_was_given(monkeypatch):
    """build_plan's result is the authoritative set. narrow returns a view of
    it and must not quietly shrink the thing it was checked against."""
    plan = plan_of(monkeypatch, ["A-1", "A-2"])
    rollback.narrow(plan, ["A-1"])
    assert plan.skus == ["A-1", "A-2"]


# --- delete a few now, a few later --------------------------------------------

def test_a_partly_rolled_back_run_offers_only_the_remainder(monkeypatch):
    """What makes repeat rollbacks need no bookkeeping: each delete releases
    its own claim, so the next plan is simply what is left."""
    wire(monkeypatch,
         claimed=[claim("A-1", deleted_at="2026-01-01T00:00:00Z"),
                  claim("A-2"), claim("A-3")],
         rows=[row("A-1"), row("A-2"), row("A-3")])
    plan = rollback.build_plan(RUN)
    assert plan.skus == ["A-2", "A-3"]
    assert any("already deleted" in x for x in plan.excluded), \
        "the dialog has to answer 'where did the first one go?'"


# --- execute(): the claim it releases, and the one it cannot ------------------
#
# A deletion has two halves: the click, and stamping created_products so the
# ledger stops claiming a product that is gone. Only the first half applies to a
# product the automation never claimed, and conflating them would have the
# ledger recording releases of claims that never existed.

class FakeStore:
    """Records release_product calls and nothing else."""

    def __init__(self):
        self.released = []

    def release_product(self, base_url, sku):
        self.released.append(sku)


class FakePage:
    """Enough page for execute(): a SKU input and a goto that does nothing."""

    def __init__(self, sku_on_page):
        self._sku = sku_on_page
        self.url = f"{STORE_URL}/Product/Edit/1"

    def goto(self, url):
        self.url = url

    def locator(self, _selector):
        page = self

        class _Input:
            def input_value(self):
                return page._sku

        return _Input()


def wire_pages(monkeypatch, *, present=True, deleted_ok=True):
    """Stand in for the two page objects execute() drives.

    `present` is whether find_by_sku resolves before the delete; after a
    successful delete it must resolve to None, which is the check execute()
    uses to confirm the click landed.
    """
    state = {"gone": not present, "deletes": []}

    class FakeProducts:
        def __init__(self, page):
            pass

        def find_by_sku(self, sku):
            if state["gone"]:
                return None
            return f"{STORE_URL}/Product/Edit/1"

    class FakeProduct:
        product_id = None

        def __init__(self, page):
            pass

        def delete(self):
            state["deletes"].append(True)
            if deleted_ok:
                state["gone"] = True

    monkeypatch.setattr(rollback, "ManageProductsPage", FakeProducts)
    monkeypatch.setattr(rollback, "ProductPage", FakeProduct)
    monkeypatch.setattr(rollback.ProductPage, "SEL", {"sku": "#sku"},
                        raising=False)
    return state


def doomed(sku, origin):
    return rollback.Doomed(sku=sku, product_id=None,
                           product_url=f"{STORE_URL}/Product/Edit/1",
                           row_status="added", origin=origin)


def test_deleting_ours_retires_the_ownership_claim(monkeypatch):
    """Without the release the claim outlives the product, and a SKU the client
    later creates for themselves looks like ours."""
    wire_pages(monkeypatch)
    store = FakeStore()
    plan = rollback.Plan(run_id=RUN, base_url=STORE_URL,
                         products=[doomed("A-1", rollback.CREATED)])
    report = rollback.execute(FakePage("A-1"), plan, store=store)
    assert report["deleted"] == ["A-1"]
    assert store.released == ["A-1"]


def test_deleting_the_clients_product_releases_no_claim(monkeypatch):
    """There is no claim on it to retire. The UPDATE would match nothing
    anyway; not issuing it keeps "released a claim" out of the record for a
    product this automation never claimed."""
    wire_pages(monkeypatch)
    store = FakeStore()
    plan = rollback.Plan(run_id=RUN, base_url=STORE_URL,
                         products=[doomed("THEIRS", rollback.FOREIGN)])
    report = rollback.execute(FakePage("THEIRS"), plan, store=store)
    assert report["deleted"] == ["THEIRS"]
    assert store.released == []


def test_the_report_names_what_nothing_can_put_back(monkeypatch):
    """"3 deleted" does not tell the operator what they just did when one of
    the three was the client's. The irreversible half gets its own line."""
    wire_pages(monkeypatch)
    plan = rollback.Plan(run_id=RUN, base_url=STORE_URL,
                         products=[doomed("MINE", rollback.CREATED),
                                   doomed("THEIRS", rollback.FOREIGN)])
    # One page, one SKU: the guard refuses whichever product it is not showing,
    # so drive them separately and merge what each reported.
    mine = rollback.execute(FakePage("MINE"), rollback.narrow(plan, ["MINE"]))
    wire_pages(monkeypatch)
    theirs = rollback.execute(FakePage("THEIRS"),
                              rollback.narrow(plan, ["THEIRS"]))
    assert mine["unowned_deleted"] == []
    assert theirs["unowned_deleted"] == ["THEIRS"]


def test_the_sku_guard_still_refuses_a_product_it_did_not_expect(monkeypatch):
    """For a product with no claimed product id — every 'skipped' one — this is
    the ONLY identity check there is, so it carries the whole weight."""
    wire_pages(monkeypatch)
    store = FakeStore()
    plan = rollback.Plan(run_id=RUN, base_url=STORE_URL,
                         products=[doomed("THEIRS", rollback.FOREIGN)])
    report = rollback.execute(FakePage("SOMEONE-ELSE"), plan, store=store)
    assert report["deleted"] == []
    assert report["refused"] and "SOMEONE-ELSE" in report["refused"][0]["why"]
    assert store.released == []


def test_narrowing_keeps_provenance(monkeypatch):
    """execute() decides whether to release a claim from `origin`, so a
    narrowed plan that lost it would start retiring claims it never had."""
    wire(monkeypatch, claimed=[], rows=[row("THEIRS", status="skipped")])
    plan = rollback.build_plan(RUN)
    assert rollback.narrow(plan, ["THEIRS"]).products[0].origin == \
        rollback.FOREIGN


def test_the_freshest_product_id_wins(monkeypatch):
    """execute() refuses when the recorded id disagrees with the page, so the
    plan must carry the most recently observed one. For a `resumed` product the
    row's id was read off the live page during THIS run; the claim's was
    stamped whenever the earlier run created it. Preferring the claim bought
    nothing but false refusals."""
    wire(monkeypatch,
         claimed=[claim("A-1", "642", run_id=OTHER_RUN, created_at=BEFORE_RUN)],
         rows=[row("A-1", status="resumed", product_id="650")])
    doomed = rollback.build_plan(RUN).products[0]
    assert doomed.origin == rollback.RESUMED
    assert doomed.product_id == "650"


def test_the_claim_still_fills_in_when_the_row_recorded_nothing(monkeypatch):
    """Rows from before product_id was recorded have none. The claim is then
    the only id there is, and dropping it would disarm the id guard entirely."""
    wire(monkeypatch, claimed=[claim("A-1", "642")],
         rows=[{"sku": "A-1", "status": "added", "product_id": None}])
    assert rollback.build_plan(RUN).products[0].product_id == "642"
