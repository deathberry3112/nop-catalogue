"""Undo one run: delete the products that run created, and nothing else.

    plan = build_plan(run_id)          # what would be deleted, and why
    report = execute(page, plan, store=store)

THIS MODULE DELETES THINGS FROM A LIVE CATALOGUE. Every design choice below is
about making the blast radius exactly one run's creations, provably, and making
the failure mode "refuse" rather than "delete the wrong product".

WHAT COUNTS AS "THIS RUN CREATED IT", which is the whole question:

    created_products WHERE run_id = ? AND deleted_at IS NULL

and nothing else. That table is written by RunStore.claim_product(), which the
runner calls in exactly one place — immediately after a *new* product's first
successful save. A product the run merely resumed, skipped, or previewed never
reaches that line, so it can never appear here. It is the only record in the
system that means "this run brought this product into existence", and deriving
the set any other way would be weaker:

    run_rows.status == 'added'   agrees with it, and is cross-checked below,
                                 but is a report of what happened rather than a
                                 claim of ownership
    the local var/ ledger        has no run_id at all — it cannot answer this
    "the SKUs in the payload"    catastrophic: the payload lists SKUs that
                                 already existed as the client's own products,
                                 which is the exact thing the runner refuses to
                                 touch on the way in

A row whose status is 'failed' IS included when it was claimed. The runner
claims a product the moment it saves, so a row that died later — half-way
through attributes, say — left a real product behind. That orphan is precisely
what a rollback exists to clear up, and excluding it would leave the catalogue
dirtier than the rollback claims to have left it.

WHAT IS REFUSED, always:

    a run still in progress            its final set is not knowable yet
    a store that is not configured     no record of what was created = no
                                       defensible answer, so no deletion
    'skipped' rows                     somebody else's product, by definition
    a product whose SKU on the page
      does not match what we recorded  nopCommerce reuses product ids after a
                                       delete, so a recorded id alone can point
                                       at a stranger's product
    a base_url that is not the run's   never reach into a different store

The only write this module performs is ProductPage.delete(), plus stamping
created_products.deleted_at afterwards. It never edits a product, an attribute,
a value, or a combination — there is no code path here that can.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import library
from app.store.client import get_client, store_available
from pages.manage_products import ManageProductsPage
from pages.product_page import ProductPage

log = logging.getLogger("rollback")

#: run_rows statuses that may correspond to a product this run created.
#: 'added' is the normal case; 'failed' covers a row that saved the product and
#: then died configuring it. Everything else — resumed, skipped, previewed,
#: pending, running, blocked — must never be deleted by a rollback.
DELETABLE_STATUSES = {"added", "failed"}


@dataclass(frozen=True)
class Doomed:
    """One product the rollback would delete."""

    sku: str
    product_id: str | None
    product_url: str | None
    row_status: str | None          # what run_rows says happened to it

    def as_dict(self) -> dict:
        return {"sku": self.sku, "product_id": self.product_id,
                "product_url": self.product_url, "row_status": self.row_status}


@dataclass
class Plan:
    """Exactly what a rollback would do, computed server-side.

    `blocked` non-empty means the whole operation is refused and `products`
    must be ignored. `excluded` lists what was deliberately left alone, with
    the reason — a rollback that silently narrows its own scope is worse than
    one that explains itself.
    """

    run_id: str
    base_url: str = ""
    products: list[Doomed] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    blocked: str | None = None

    @property
    def ok(self) -> bool:
        return self.blocked is None and bool(self.products)

    def as_dict(self) -> dict:
        return {"run_id": self.run_id, "base_url": self.base_url,
                "products": [p.as_dict() for p in self.products],
                "excluded": self.excluded, "blocked": self.blocked,
                "count": len(self.products)}

    @property
    def skus(self) -> list[str]:
        return sorted(p.sku for p in self.products)


def build_plan(run_id: str) -> Plan:
    """What deleting this run's products would actually delete.

    Reads the database, never the in-memory run manager: the record outlives
    the process, so a rollback works after a restart, and the answer does not
    depend on which worker happens to be serving the request.
    """
    plan = Plan(run_id=run_id)

    if not store_available():
        plan.blocked = (
            "the run store is not configured, so there is no record of which "
            "products this run created — refusing to guess")
        return plan

    try:
        client = get_client()
        runs = client.table("runs").select(
            "id,status,target_base_url,commit").eq("id", run_id).execute().data
        claimed = client.table("created_products").select(
            "sku,product_id,product_url,base_url,deleted_at").eq(
            "run_id", run_id).is_("deleted_at", "null").execute().data
        rows = client.table("run_rows").select(
            "sku,status,product_id").eq("run_id", run_id).execute().data
    except Exception as exc:
        plan.blocked = f"could not read the run record: {exc}"
        return plan

    if not runs:
        plan.blocked = f"no run {run_id} in the store"
        return plan

    run = runs[0]
    plan.base_url = run.get("target_base_url") or ""

    if run.get("status") in {"running", "claimed", "queued"}:
        plan.blocked = (
            f"this run is still {run.get('status')} — what it created is not "
            f"final yet. Wait for it to finish, then roll it back")
        return plan

    by_sku = {r["sku"]: r for r in rows}

    for entry in claimed:
        sku = entry["sku"]
        row = by_sku.get(sku)
        status = (row or {}).get("status")

        # Belt and braces. claim_product() is only reachable on the create
        # path, so a skipped row can't be here — but "can't" is doing a lot of
        # work in a function that deletes things, so check anyway.
        if status is not None and status not in DELETABLE_STATUSES:
            plan.excluded.append(
                f"{sku} — this run recorded it as {status!r}, not something it "
                f"created; left alone")
            continue

        if entry.get("base_url") and plan.base_url and \
                entry["base_url"].rstrip("/") != plan.base_url.rstrip("/"):
            plan.excluded.append(
                f"{sku} — claimed against {entry['base_url']}, but this run "
                f"targeted {plan.base_url}; left alone")
            continue

        plan.products.append(Doomed(
            sku=sku, product_id=entry.get("product_id"),
            product_url=entry.get("product_url"), row_status=status))

    # Cross-check against the other record of what happened. A disagreement
    # does not block — created_products is the authoritative claim — but it is
    # worth putting in front of whoever is about to press the button.
    added = {r["sku"] for r in rows if r.get("status") == "added"}
    claimed_skus = {p.sku for p in plan.products}
    for sku in sorted(added - claimed_skus):
        plan.excluded.append(
            f"{sku} — the run reported it as added but there is no ownership "
            f"claim for it, so this cannot prove it created it; left alone")

    if not plan.products and not plan.blocked:
        # Say WHICH kind of nothing. "created nothing" and "created things that
        # are already gone" look identical here and mean opposite things to
        # whoever is standing in front of the dialog.
        if plan.excluded:
            plan.blocked = (
                "nothing here can be deleted — this run's products are no "
                "longer claimed by it. See below for each one.")
        else:
            plan.blocked = (
                "this run created no products, so there is nothing to undo")

    plan.products.sort(key=lambda p: p.sku)
    return plan


def execute(page, plan: Plan, *, store=None, base_url: str = "") -> dict:
    """Delete exactly the products in `plan`. Returns a per-SKU report.

    Every product is re-verified on its own edit page immediately before the
    click. That check is not paranoia about our own bookkeeping — nopCommerce
    reuses product ids after a delete, so a recorded id can genuinely resolve
    to a product somebody else created since.

    Never raises for one product's sake: a refusal or a failure is recorded and
    the rest continue, because a rollback that stops half-way and cannot say
    where it stopped is the worst outcome available.
    """
    if plan.blocked:
        raise library.StepError(f"rollback refused: {plan.blocked}", step="rollback")

    target = base_url or plan.base_url
    products = ManageProductsPage(page)
    allowed = {p.sku: p for p in plan.products}

    deleted: list[str] = []
    already_gone: list[str] = []
    refused: list[dict] = []
    failed: list[dict] = []

    for doomed in plan.products:
        sku = doomed.sku
        try:
            url = products.find_by_sku(sku)
            if not url:
                already_gone.append(sku)
                _release(store, target, sku)
                log.info("rollback: %s was already gone", sku)
                continue

            page.goto(url)
            product = ProductPage(page)

            # --- the guard, immediately before the irreversible click -------
            on_page_sku = page.locator(ProductPage.SEL["sku"]).input_value()
            if on_page_sku != sku:
                refused.append({"sku": sku, "why":
                                f"the edit page at {url} shows SKU "
                                f"{on_page_sku!r} — refusing to delete it"})
                continue

            on_page_id = product.product_id
            if doomed.product_id and on_page_id and on_page_id != doomed.product_id:
                refused.append({"sku": sku, "why":
                                f"this run created product {doomed.product_id}, "
                                f"but {sku} is now product {on_page_id} — "
                                f"refusing to delete a different product"})
                continue

            if sku not in allowed:                      # unreachable by design
                refused.append({"sku": sku, "why": "not in the approved set"})
                continue

            product.delete()

            if products.find_by_sku(sku) is not None:
                failed.append({"sku": sku, "why": "still in the catalogue after "
                                                  "the delete was submitted"})
                continue

            _release(store, target, sku)
            deleted.append(sku)
            log.warning("rollback: deleted %s (product %s)", sku, on_page_id)

        except Exception as exc:
            failed.append({"sku": sku, "why": f"{type(exc).__name__}: {exc}"})
            log.exception("rollback: %s failed", sku)

    report = {"run_id": plan.run_id, "deleted": deleted,
              "already_gone": already_gone, "refused": refused, "failed": failed,
              "requested": plan.skus}
    log.warning("rollback %s: %d deleted, %d already gone, %d refused, %d failed",
                plan.run_id, len(deleted), len(already_gone), len(refused),
                len(failed))
    return report


def _release(store, base_url: str, sku: str) -> None:
    """Retire the ownership claim so the ledger stops saying we own it.

    Without this the claim outlives the product, and a SKU the client later
    creates for themselves would look like ours — which would let a future run
    edit their product. That is the exact guarantee the ownership record
    exists to provide, so releasing is part of the delete, not a tidy-up.
    """
    if store is None:
        return
    try:
        store.release_product(base_url, sku)
    except Exception as exc:                 # never let bookkeeping undo a delete
        log.warning("could not release the claim on %s: %s", sku, exc)
