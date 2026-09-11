"""Undo one run: delete the products that run worked on.

    plan = build_plan(run_id)             # everything this run could undo
    plan = narrow(plan, chosen)           # ...limited to what was selected
    report = execute(page, plan, store=store)

A rollback does not have to be all-or-nothing: `narrow` takes the operator's
selection and refuses anything that is not already in the plan, so a run can be
undone a few products at a time. Each deletion releases its own claim, so the
next plan is simply the remainder — repeat rollbacks need no extra bookkeeping.

THIS MODULE DELETES THINGS FROM A LIVE CATALOGUE. Every design choice below is
about making the blast radius exactly the set the operator was shown, provably,
and making the failure mode "refuse" rather than "delete the wrong product".

WHAT COUNTS AS "THIS RUN WORKED ON IT", which is the whole question:

    run_rows WHERE run_id = ? AND status IN ('added','failed','resumed','skipped')

and nothing else. Membership comes from the run's own rows, so the set is
bounded by what this run actually processed — a product no row of this run
names can never appear here, whatever the ledger says.

Row membership is not the same as ownership, and the two used to be conflated
because the set was narrower: this module once deleted only products the run
had CREATED, derived from `created_products WHERE run_id = ?`. That was widened
deliberately, on the operator's instruction, to everything the run touched.
The ownership question did not go away — it became a per-product fact, and
every product in a plan carries it as `Doomed.origin`:

    created    `created_products` holds a LIVE claim stamped with THIS run_id.
               The product did not exist before this run. Deleting it returns
               the catalogue to where it started — the original, safe case.

    resumed    a live claim exists, stamped with an EARLIER run_id. The
               automation created this product on a previous run and this run
               reopened it to add attributes. Deleting it undoes this run's
               edits AND the earlier run's creation.

    foreign    NO live claim, and the run recorded the row as 'skipped' — which
               is the runner's word for "the client created this product; open
               nothing, change nothing". The run never wrote to it. Deleting it
               destroys catalogue data this automation did not produce and
               cannot recreate. NOTHING here can put it back.

    unclaimed  no live claim, but the run reports having added, resumed or
               failed on it. Either ownership came from the local var/ ledger
               (which records no run_id) or a claim went missing. The product
               is real and this run wrote to it; what cannot be shown is who
               made it.

Callers are expected to put that distinction in front of whoever presses the
button, and `origin` exists for no other reason. This module will delete any of
the four; only the dialog can decide which of them a human meant.

WHAT IS REFUSED, always:

    a run still in progress            its final set is not knowable yet
    a store that is not configured     no record of what the run did = no
                                       defensible answer, so no deletion
    'previewed' / 'pending' rows       a dry run wrote nothing; there is no
                                       change of this run's to undo
    a product already released by an
      earlier rollback of this run     it is gone; say so rather than retry
    a SKU whose live claim was made
      AFTER this run finished          the product standing there now was
                                       created by a later run, so it is that
                                       run's to undo and not this one's
    a product whose SKU on the page
      does not match what we recorded  nopCommerce reuses product ids after a
                                       delete, so a recorded id alone can point
                                       at a stranger's product
    a base_url that is not the run's   never reach into a different store

The only write this module performs is ProductPage.delete(), plus stamping
created_products.deleted_at afterwards for the products that had a claim. It
never edits a product, an attribute, a value, or a combination — there is no
code path here that can.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import library
from app.store.client import fetch_all, store_available
from pages.manage_products import ManageProductsPage
from pages.product_page import ProductPage

log = logging.getLogger("rollback")

#: run_rows statuses that correspond to a product this run worked on and which
#: is therefore in scope. 'added' created it; 'failed' created it and then died
#: configuring it; 'resumed' reopened one an earlier run created; 'skipped' is
#: a product the run found and left untouched because the client owns it.
#: Everything else — previewed, pending, running, blocked — wrote nothing at
#: all, so there is no change of this run's for a rollback to undo.
DELETABLE_STATUSES = {"added", "failed", "resumed", "skipped"}

#: Rows that mean this run put bytes into the catalogue for that product. The
#: difference from DELETABLE_STATUSES is 'skipped', which it did not.
WROTE_STATUSES = {"added", "failed", "resumed"}

# Provenance tiers. See the module docstring — these are the whole reason the
# widened scope is defensible, because they are what lets the dialog ask a
# different question about a product the client made than about one we made.
CREATED = "created"
RESUMED = "resumed"
FOREIGN = "foreign"
UNCLAIMED = "unclaimed"

#: Origins whose product has a live ownership claim to retire after deleting.
CLAIMED_ORIGINS = {CREATED, RESUMED}

#: Origins the automation cannot show it created. Callers should treat these as
#: the dangerous half of a plan; `execute` does not distinguish.
UNOWNED_ORIGINS = {FOREIGN, UNCLAIMED}


def _as_datetime(value) -> datetime | None:
    """A timestamp from either side of the wire, or None if it is not one.

    psycopg2 hands back datetimes; fixtures, JSON round-trips and older rows
    hand back ISO strings. Anything that does not parse is None, which the one
    caller treats as "cannot tell" rather than as a comparison result.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _postdates(claimed_at, run_finished_at) -> bool:
    """Was this claim made after the run finished?

    False whenever that cannot be established — a missing timestamp, an
    unparseable one, or one side naive and the other aware. This decides
    whether a product stays IN a deletion plan, so "don't know" has to mean
    "no exclusion on these grounds", and the SKU/product-id guard in execute()
    remains the check that actually stands between a mistake and a deletion.
    """
    made, ended = _as_datetime(claimed_at), _as_datetime(run_finished_at)
    if made is None or ended is None:
        return False
    if (made.tzinfo is None) != (ended.tzinfo is None):
        return False
    return made > ended


class SelectionError(ValueError):
    """The operator's selection is not a subset of the current plan.

    Always means nothing was deleted: narrow() runs before any page is opened,
    so a bad selection is refused whole rather than applied in part.
    """


@dataclass(frozen=True)
class Doomed:
    """One product the rollback would delete."""

    sku: str
    product_id: str | None
    product_url: str | None
    row_status: str | None          # what run_rows says happened to it
    origin: str = CREATED           # who made it — see the module docstring

    @property
    def owned(self) -> bool:
        """Can the automation show it created this product?"""
        return self.origin in CLAIMED_ORIGINS

    def as_dict(self) -> dict:
        return {"sku": self.sku, "product_id": self.product_id,
                "product_url": self.product_url, "row_status": self.row_status,
                "origin": self.origin, "owned": self.owned}


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

    @property
    def origins(self) -> dict[str, int]:
        """How many products of each provenance. The dialog leads with this."""
        counts: dict[str, int] = {}
        for p in self.products:
            counts[p.origin] = counts.get(p.origin, 0) + 1
        return counts

    @property
    def unowned(self) -> list[Doomed]:
        """The products the automation cannot show it created."""
        return [p for p in self.products if not p.owned]

    def as_dict(self) -> dict:
        return {"run_id": self.run_id, "base_url": self.base_url,
                "products": [p.as_dict() for p in self.products],
                "excluded": self.excluded, "blocked": self.blocked,
                "count": len(self.products), "origins": self.origins,
                "unowned_count": len(self.unowned)}

    @property
    def skus(self) -> list[str]:
        return sorted(p.sku for p in self.products)


def build_plan(run_id: str) -> Plan:
    """What rolling this run back would actually delete.

    Reads the database, never the in-memory run manager: the record outlives
    the process, so a rollback works after a restart, and the answer does not
    depend on which worker happens to be serving the request.

    Driven by `run_rows`, with `created_products` consulted per SKU for
    provenance. It used to be the other way round — the ledger chose the set
    and the rows cross-checked it — which is why a product the run only
    resumed or skipped could not appear: the ledger had no entry under this
    run's id. Rows are now the membership test, and the ledger answers a
    different question, "who made this", recorded on each product as `origin`.
    """
    plan = Plan(run_id=run_id)

    if not store_available():
        plan.blocked = (
            "the run store is not configured, so there is no record of what "
            "this run did — refusing to guess")
        return plan

    try:
        runs = fetch_all(
            """
            SELECT id, status, target_base_url, commit, finished_at
              FROM runs WHERE id = %s
            """,
            (run_id,),
        )
        rows = fetch_all(
            """
            SELECT sku, status, product_id, product_url
              FROM run_rows WHERE run_id = %s
            """,
            (run_id,),
        )
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
            f"this run is still {run.get('status')} — what it did is not final "
            f"yet. Wait for it to finish, then roll it back")
        return plan

    # Ownership claims for the SKUs this run touched, whichever run made them,
    # PLUS every claim stamped with this run — the latter so a creation whose
    # row somehow went missing is still accounted for rather than silently
    # dropped. Released claims are read too, not filtered out in SQL: they are
    # the difference between "this run made nothing" and "you already deleted
    # these", which are opposite facts, and with selective rollback a
    # half-released run is the normal state rather than an edge case.
    skus = sorted({r["sku"] for r in rows})
    try:
        if skus:
            claimed = fetch_all(
                """
                SELECT sku, product_id, product_url, base_url, run_id,
                       created_at, deleted_at
                  FROM created_products
                 WHERE run_id = %s OR (base_url = %s AND sku = ANY(%s))
                """,
                (run_id, plan.base_url, skus),
            )
        else:
            claimed = fetch_all(
                """
                SELECT sku, product_id, product_url, base_url, run_id,
                       created_at, deleted_at
                  FROM created_products WHERE run_id = %s
                """,
                (run_id,),
            )
    except Exception as exc:
        plan.blocked = f"could not read the ownership ledger: {exc}"
        return plan

    def _same_store(entry) -> bool:
        """A claim against a different nop instance says nothing about this
        one. Two stores can hold the same SKU and they are not the same
        product, so a claim from elsewhere must not confer ownership here."""
        theirs, ours = entry.get("base_url"), plan.base_url
        return not (theirs and ours) or theirs.rstrip("/") == ours.rstrip("/")

    onsite = [c for c in claimed if _same_store(c)]
    offsite = [c for c in claimed if not _same_store(c)]
    live = {c["sku"]: c for c in onsite if not c.get("deleted_at")}
    released = {c["sku"]: c for c in onsite if c.get("deleted_at")}

    # Why each row was left out, counted rather than re-derived from the
    # message strings later. "Nothing to delete" has several causes that read
    # identically in an empty list and mean different things to whoever opened
    # the dialog, and the final message picks between them from these.
    wrote_nothing = already_gone = postdated = 0

    for row in sorted(rows, key=lambda r: r["sku"]):
        sku = row["sku"]
        status = row.get("status")

        if status not in DELETABLE_STATUSES:
            wrote_nothing += 1
            plan.excluded.append(
                f"{sku} — this run recorded it as {status!r}; it wrote nothing "
                f"to the catalogue for this row, so there is nothing of this "
                f"run's to undo")
            continue

        claim = live.get(sku)
        gone = released.get(sku)

        # Already deleted, by an earlier rollback of this run or of the run
        # that created it. Only applies where the automation held the claim: a
        # 'skipped' row's tombstone is from an older life of that SKU
        # (owned_skus() filters deleted_at, which is exactly why the run
        # skipped it), so the product standing there now is the client's and is
        # still in scope.
        if claim is None and gone is not None and status in WROTE_STATUSES:
            already_gone += 1
            plan.excluded.append(
                f"{sku} — already deleted by an earlier rollback; nothing left "
                f"to do")
            continue

        # The SKU is the same; the product need not be. A claim made after this
        # run finished means a LATER run created what is standing there now —
        # the thing this row saw is gone. Deleting it would undo the later
        # run's work in this run's name, which is the mistake the old
        # ledger-driven set could not make and the widened one can. It is also
        # how "run X added it, then it was deleted, then run Z added it again"
        # ends up looking from here.
        if claim is not None and _postdates(claim.get("created_at"),
                                            run.get("finished_at")):
            postdated += 1
            plan.excluded.append(
                f"{sku} — the product under this SKU was created after this "
                f"run finished, so it is not the one this run worked on; "
                f"roll back the run that made it instead")
            continue

        if claim is not None:
            origin = CREATED if str(claim.get("run_id")) == str(run_id) \
                else RESUMED
            # The ROW's id first, then the claim's. Both describe the same
            # product, but the row's was read off the live edit page when this
            # run last touched it, while a `resumed` product's claim was
            # stamped by whichever earlier run created it — which can be weeks
            # of catalogue churn ago. execute() refuses on a mismatch, so
            # preferring the staler of two records buys nothing but false
            # refusals. Neither source is authoritative enough to delete on:
            # the SKU re-read in execute() is what actually gates the click.
            product_id = row.get("product_id") or claim.get("product_id")
            product_url = row.get("product_url") or claim.get("product_url")
        else:
            # No claim we can stand behind. 'skipped' is the run telling us
            # whose it is; anything else is a bookkeeping gap, and the two read
            # very differently to whoever is about to delete it.
            origin = FOREIGN if status == "skipped" else UNCLAIMED
            product_id = row.get("product_id")
            product_url = row.get("product_url")

        plan.products.append(Doomed(
            sku=sku, product_id=product_id, product_url=product_url,
            row_status=status, origin=origin))

    # A claim stamped with this run whose SKU is not in run_rows at all. Should
    # be unreachable — claim_product() runs inside a row — but this function
    # decides what gets deleted, so an unaccounted creation is named rather
    # than dropped.
    seen_rows = {r["sku"] for r in rows}
    for entry in sorted((c for c in onsite
                         if str(c.get("run_id")) == str(run_id)
                         and not c.get("deleted_at")
                         and c["sku"] not in seen_rows),
                        key=lambda e: e["sku"]):
        plan.products.append(Doomed(
            sku=entry["sku"], product_id=entry.get("product_id"),
            product_url=entry.get("product_url"), row_status=None,
            origin=CREATED))

    in_plan = {p.sku for p in plan.products}
    for entry in sorted(offsite, key=lambda e: e["sku"]):
        if entry["sku"] in in_plan:
            continue
        plan.excluded.append(
            f"{entry['sku']} — claimed against {entry['base_url']}, but this "
            f"run targeted {plan.base_url}; left alone")

    if not plan.products and not plan.blocked:
        # Say WHICH kind of nothing. "worked on nothing", "already deleted",
        # "only previewed" and "somebody has since replaced these" look
        # identical in an empty list and mean completely different things to
        # whoever is standing in front of the dialog.
        reasons = wrote_nothing + already_gone + postdated

        def only(n: int) -> bool:
            """This reason accounts for every row that was left out."""
            return bool(n) and n == reasons

        if not rows:
            plan.blocked = (
                "this run has no rows on record, so there is nothing to undo")
        elif only(already_gone):
            plan.blocked = (
                "every product this run worked on has already been deleted — "
                "there is nothing left to undo")
        elif only(wrote_nothing):
            plan.blocked = (
                "this run wrote nothing to the catalogue — every row was "
                "previewed or never ran — so there is nothing to undo")
        elif only(postdated):
            plan.blocked = (
                "every product this run worked on has since been replaced by a "
                "later run — roll those runs back instead of this one")
        elif plan.excluded:
            plan.blocked = (
                "nothing here can be deleted. See below for the reason against "
                "each one.")
        else:
            plan.blocked = (
                "this run worked on no products, so there is nothing to undo")

    plan.products.sort(key=lambda p: p.sku)
    return plan


def narrow(plan: Plan, skus) -> Plan:
    """`plan`, limited to the products the operator actually selected.

    Selective rollback replaced an exact-match confirmation ("the list you sent
    must equal the list I computed") with a subset check, and the property that
    check existed for has to survive the change. It does, because `plan` is
    recomputed from the store immediately before this runs: a SKU the operator
    was shown but which is no longer deletable — someone else rolled it back
    meanwhile, or its claim was released — is simply not in `plan`, so it lands
    in `unknown` and the WHOLE operation is refused rather than partly applied.

    Nothing passed in here can widen the scope. The result is always a subset
    of what build_plan derived from this run's rows, so an unrecognised SKU
    stops the deletion; it never joins it. This is also the check that keeps
    the widened scope bounded by one run: a SKU from a DIFFERENT run's rows is
    as unknown here as an invented one.
    """
    wanted = {str(sku).strip() for sku in skus if str(sku).strip()}
    if not wanted:
        raise SelectionError(
            "nothing was selected, so there is nothing to delete")

    unknown = sorted(wanted - set(plan.skus))
    if unknown:
        raise SelectionError(
            f"these are no longer in this run's deletable set: {unknown}. "
            f"The catalogue moved since the dialog opened — nothing was "
            f"deleted. Re-open it and check the list again")

    return Plan(run_id=plan.run_id, base_url=plan.base_url,
                products=[p for p in plan.products if p.sku in wanted],
                excluded=list(plan.excluded), blocked=plan.blocked)


def execute(page, plan: Plan, *, store=None, base_url: str = "") -> dict:
    """Delete exactly the products in `plan`. Returns a per-SKU report.

    Every product is re-verified on its own edit page immediately before the
    click. That check is not paranoia about our own bookkeeping — nopCommerce
    reuses product ids after a delete, so a recorded id can genuinely resolve
    to a product somebody else created since. For a product the automation
    never created it is the ONLY identity check available, because there is no
    claimed product id to compare against, so it carries the whole weight.

    Never raises for one product's sake: a refusal or a failure is recorded and
    the rest continue, because a rollback that stops half-way and cannot say
    where it stopped is the worst outcome available.
    """
    if plan.blocked:
        raise library.StepError(f"rollback refused: {plan.blocked}", step="rollback")

    target = base_url or plan.base_url
    products = ManageProductsPage(page)
    allowed = {p.sku: p for p in plan.products}

    # Said once, loudly, before anything opens. The dialog already made the
    # operator tick these individually, but the server log is the only record
    # that survives the browser tab, and "who authorised deleting the client's
    # products" is a question that gets asked afterwards.
    if plan.unowned:
        log.warning("rollback %s: %d of %d products were NOT created by this "
                    "automation and are being deleted anyway: %s",
                    plan.run_id, len(plan.unowned), len(plan.products),
                    ", ".join(sorted(p.sku for p in plan.unowned)))

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
                _release(store, target, doomed)
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
                                f"this run recorded {sku} as product "
                                f"{doomed.product_id}, but it is now product "
                                f"{on_page_id} — refusing to delete a "
                                f"different product"})
                continue

            if sku not in allowed:                      # unreachable by design
                refused.append({"sku": sku, "why": "not in the approved set"})
                continue

            product.delete()

            if products.find_by_sku(sku) is not None:
                failed.append({"sku": sku, "why": "still in the catalogue after "
                                                  "the delete was submitted"})
                continue

            _release(store, target, doomed)
            deleted.append(sku)
            log.warning("rollback: deleted %s (product %s, origin %s)",
                        sku, on_page_id, doomed.origin)

        except Exception as exc:
            failed.append({"sku": sku, "why": f"{type(exc).__name__}: {exc}"})
            log.exception("rollback: %s failed", sku)

    report = {"run_id": plan.run_id, "deleted": deleted,
              "already_gone": already_gone, "refused": refused, "failed": failed,
              "requested": plan.skus,
              # Called out separately because it is the half of the outcome
              # nobody can undo, and a report that buries it in a flat list of
              # SKUs does not tell the operator what they just did.
              "unowned_deleted": sorted(
                  sku for sku in deleted if not allowed[sku].owned)}
    log.warning("rollback %s: %d deleted, %d already gone, %d refused, %d failed",
                plan.run_id, len(deleted), len(already_gone), len(refused),
                len(failed))
    return report


def _release(store, base_url: str, doomed: Doomed) -> None:
    """Retire the ownership claim so the ledger stops saying we own it.

    Without this the claim outlives the product, and a SKU the client later
    creates for themselves would look like ours — which would let a future run
    edit their product. That is the exact guarantee the ownership record
    exists to provide, so releasing is part of the delete, not a tidy-up.

    A product with no claim has nothing to retire. The UPDATE would match no
    rows anyway; skipping it keeps "released a claim" out of the log for
    products this automation never claimed.
    """
    if store is None or not doomed.owned:
        return
    try:
        store.release_product(base_url, doomed.sku)
    except Exception as exc:                 # never let bookkeeping undo a delete
        log.warning("could not release the claim on %s: %s", doomed.sku, exc)
