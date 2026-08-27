"""run_batch — the automation proper.

main.resolve_runner imports this. If the import fails it falls back to a stub;
production uses this path.

    run_batch(page, rows, *, base_url, email, password, commit, on_existing,
              progress, store=None) -> summary

CONTRACT (main.py's): take an already-created `page`, log in, loop the rows,
return a summary. Never launch a browser — main.py owns that, so there is
exactly one place that configures Chromium.

Per row, the flow and its guard:

    look the SKU up exactly
      exists, not ours   -> skip, touch nothing        <- the guard
      exists, ours       -> resume, finish configuring it
      new                -> create, then configure

The guard matters more than it looks: several payload SKUs already exist as
products nobody here made, and "add the payload" without it would edit a
client's live catalogue. Ownership comes from the store when Postgres is
configured (shared between concurrent workers) and the local ledger otherwise.

Every write step is idempotent — adding an attribute that exists returns it,
generating combinations that exist returns them — which is what makes both
resume and retry safe.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable

import combination_skus
import library
from app import configurator as app_configurator
from app.colour import resolve as resolve_colours
from app.colour import swatches_for_row, swatches_for_values
from app.errors import ErrorClass, classify
from pages.configurator_page import ConfiguratorPage
from pages.landing_page import LandingPage
from pages.manage_products import ManageProductsPage
from pages.product_page import ProductPage

log = logging.getLogger("automation.runner")


class _NullStore:
    """Stands in when no store was supplied, so the runner never branches on
    whether telemetry exists. Every method is a no-op with the right shape."""

    run_id = None

    def row_started(self, row, index=0): return None
    def row_finished(self, *a, **k): pass
    def event(self, *a, **k): pass
    def record_inference(self, *a, **k): pass
    def claim_product(self, *a, **k): pass
    def owned_skus(self, base_url): return set()

    def step(self, row_id, name, attempt=1):
        from contextlib import nullcontext
        return nullcontext({})


def _retrying(store, row_id: str | None, name: str,
              action: Callable[[], Any], *, detail: dict | None = None,
              detail_from: Callable[[Any], dict] | None = None) -> Any:
    """Run one step, retrying only what is worth retrying.

    The taxonomy decides. A transient DOM error — Kendo rebinding the grid
    under an open editor, which is common and expected — is retried up to five
    times. An unresolvable colour is not retried at all: five attempts spend
    five minutes arriving at the identical answer. That distinction is the
    entire reason app.errors exists.

    `detail` is recorded on the step BEFORE the action runs and `detail_from`
    turns its return value into more of the same, which is what row_steps.detail
    has always been for. The order matters: a step that dies still carries what
    it set out to do, so a half-configured product explains itself instead of
    going quiet at the moment it becomes interesting.
    """
    attempt = 1
    while True:
        try:
            with store.step(row_id, name, attempt=attempt) as recorded:
                if detail:
                    recorded.update(detail)
                result = action()
                if detail_from:
                    _record(recorded, detail_from, result, name)
                return result
        except Exception as exc:
            classified = classify(exc)
            if not classified.retryable or attempt >= classified.max_attempts:
                raise
            log.warning("%s failed (%s), attempt %d/%d: %s", name,
                        classified.error_class.value, attempt,
                        classified.max_attempts, exc)
            store.event(f"retrying {name} after {classified.error_class.value}",
                        level="warn", row_id=row_id,
                        data={"attempt": attempt, "error": str(exc)[:500]})
            time.sleep(0.5 * attempt)
            attempt += 1


def _record(recorded: dict, detail_from: Callable[[Any], dict], result: Any,
            name: str) -> None:
    """Fold the result into the step's detail, never at the step's expense.

    Reporting is downstream of the work. A step that did its job in the admin
    has succeeded whatever this function makes of the return value, so a bug in
    here is a log line, not a failed product and not a retry of an action that
    already ran.
    """
    try:
        recorded.update(detail_from(result))
    except Exception as exc:                # pragma: no cover - defensive
        log.warning("could not record the detail of step %s: %s", name, exc)


def mismatch_message(report: dict, row: dict) -> str:
    """Say what to DO about a combination grid that does not match the payload.

    The two halves of the report mean completely different things, and the old
    message printed both as raw dicts and left the reader to work out which:

      unmatched  the catalogue generated combinations the workbook never
                 described. Almost always ONE stray attribute VALUE left on the
                 product by an earlier run — the grid rows are just its cross
                 product, so naming the value is worth far more than listing
                 them. Nothing in this codebase ever deletes an attribute
                 value, which is why it survives every retry and re-run until a
                 human removes it. That is the sentence people actually need.

      missing    the workbook described combinations nopCommerce did not
                 generate. The attribute set is incomplete, not polluted — the
                 opposite problem, and a different fix.

    The real case: a row whose size attribute column repeated "Color" filed
    "OS" as a ninth colour, and the run afterwards reported
    `unmatched rows: [{'Color': 'OS', 'Size': 'OS'}]` — true, and nearly
    useless.
    """
    known: dict[str, set[str]] = {}
    for attribute in attributes_of(row):
        known.setdefault(attribute["name"], set()).update(
            str(v).casefold() for v in attribute["values"])

    unmatched = report.get("unmatched") or []
    missing = list(report.get("missing") or [])
    parts: list[str] = []

    if unmatched:
        stray: dict[str, set[str]] = {}
        for combination in unmatched:
            for attribute, value in (combination or {}).items():
                if str(value).casefold() in known.get(attribute, set()):
                    continue
                stray.setdefault(attribute, set()).add(str(value))
        if stray:
            named = "; ".join(f"{attribute} = "
                              + ", ".join(repr(v) for v in sorted(values))
                              for attribute, values in sorted(stray.items()))
            parts.append(
                f"this product carries attribute value(s) the workbook does not "
                f"list — {named}. That is usually left over from an earlier run, "
                f"and nothing here deletes an attribute value, so it will "
                f"survive re-running. Remove it on the product's Product "
                f"attributes tab, delete the product's existing combinations, "
                f"then run this row again")
        else:
            parts.append(
                f"{len(unmatched)} generated combination(s) match nothing in the "
                f"workbook: {unmatched}")

    if missing:
        parts.append(
            f"{len(missing)} workbook entry(ies) had no combination generated "
            f"for them: {missing}. The product's attribute values are missing "
            f"something the workbook expects")

    return "combinations do not line up — " + ". Also, ".join(parts)


def attributes_of(row: dict) -> list[dict]:
    """The attributes this row configures, in sheet order.

    A row parsed from a workbook carries them outright. One loaded from JSON —
    a checked-in payload, an old report — predates general attributes and speaks
    colours and sizes, so the same shape is derived from those. Both come out
    in the order combination_skus.attribute_names() keys on, which is what makes
    the combination lookup match.
    """
    attributes = [a for a in (row.get("attributes") or []) if a.get("values")]
    if attributes:
        return attributes

    derived: list[dict] = []
    if row.get("colors"):
        derived.append({"name": row.get("color_attr") or "Color",
                        "values": list(row["colors"]),
                        "swatches": list(row.get("swatches") or []),
                        "is_colour": True})
    if row.get("sizes"):
        derived.append({"name": row.get("size_attr") or "Size",
                        "values": list(row["sizes"]), "swatches": [],
                        "is_colour": False})
    return derived


def _resolve_swatches(product, attribute_id: str, row: dict, attribute: dict
                      ) -> tuple[dict, list[str], dict]:
    """({value: swatch id}, ["Value — why not"], what to record about it).

    Driven by the sheet's "<Attribute> Swatch" column; see app.colour.swatch. An
    attribute that has no such column gets no swatches and says nothing about it
    — the third element comes back empty, and the console prints nothing rather
    than a line saying nothing happened.

    Never raises, and that is the policy this encodes: a swatch is decoration on
    a product that is otherwise correct, so failing to find one must not fail the
    row. What could not be settled comes back named, with the reason, so the run
    reports it rather than leaving somebody to notice blanks on the storefront.
    """
    if not attribute.get("swatches"):
        return {}, [], {}
    options = product.swatch_options(attribute_id)
    if not options:
        # The sheet asked and this admin has no swatch field at all — stock
        # nopCommerce. Silence here reads as "no swatches were wanted", which
        # is the opposite of what happened, so the step says so once.
        return {}, [], {"swatches": [], "unavailable": True}
    swatches, problems, outcomes = swatches_for_values(
        attribute["values"], attribute["swatches"], options,
        scope=row.get("source_sheet"))
    return swatches, problems, ({"swatches": outcomes} if outcomes else {})


#: How a colour's swatch ended, once the admin has had its say. The first three
#: are what the value editor reports back; "planned" survives only on a step
#: that died before it could say, and "skipped" is decided before it ever runs.
SWATCH_STATES = ("set", "kept", "missed", "skipped", "planned")


def _swatch_states(seed: dict, report: dict) -> dict:
    """Fold what the admin accepted back into what the sheet asked for.

    `report` is ProductPage.add_attribute_values' return: which values it set a
    swatch on and which already carried one. That distinction is the reason this
    exists — a colour we set and a colour a human set look identical on the
    storefront and mean opposite things about who chose it, and until now both
    were reported the same way, which is to say not at all.
    """
    entries = seed.get("swatches") or []
    if not entries:
        return {}
    report = report or {}
    was_set = {str(k): str(v) for k, v in (report.get("set") or {}).items()}
    kept = {str(k): str(v) for k, v in (report.get("kept") or {}).items()}

    out, counts = [], {}
    for entry in entries:
        entry = dict(entry)
        colour = entry.get("colour")
        if colour in was_set:
            entry["state"] = "set"
        elif colour in kept:
            # What is on the value now, which is not always what we resolved:
            # an existing swatch is never overwritten, so say which one won.
            current = kept[colour]
            if current != (entry.get("swatch_id") or ""):
                entry["swatch_label"] = None
                entry["note"] = (f"this colour already had swatch {current} "
                                 f"on it, so this run left it as it was")
            entry["swatch_id"] = current
            entry["state"] = "kept"
        elif entry.get("swatch_id"):
            # Resolved, and the editor reports neither setting nor keeping it.
            entry["state"] = "missed"
            entry["note"] = (f"this colour should have got swatch "
                             f"{entry['swatch_id']}, but the admin never confirmed "
                             f"it — worth checking on the product")
        else:
            entry["state"] = "skipped"
        counts[entry["state"]] = counts.get(entry["state"], 0) + 1
        out.append(entry)
    # Counted in SWATCH_STATES order, not first-seen order: this lands in the
    # database and gets read back next to yesterday's run.
    return {"swatches": out,
            "counts": {k: counts[k] for k in SWATCH_STATES if k in counts}}


def _configure_attributes(page, products, product, row, store, row_id) -> dict:
    """Colours, sizes, combinations and their SKUs for one row.

    Each stage re-opens the product first. The attribute value editor and the
    combination generator both navigate away from the edit page, so carrying on
    from wherever the last stage finished means clicking a tab that is no longer
    on screen.
    """
    tiers: dict[str, int] = {}
    attributes = attributes_of(row)
    if not attributes:
        return tiers

    # EVERY ATTRIBUTE THE SHEET DECLARES, in its own order — Color and Size for a
    # vest, Sign Material and Sign Size for a sign board. Nothing here knows what
    # any of them mean; add_attribute maps whatever the admin already offers
    # under that name.
    for index, attribute in enumerate(attributes):
        name = attribute["name"]
        if index:
            # The value editor navigates away from the edit page, so each
            # attribute after the first has to re-open the product.
            page.goto(products.find_by_sku(row["sku"]))

        attribute_id = _retrying(
            store, row_id, f"attribute_{index + 1}",
            lambda: product.add_attribute(name),
            detail={"attribute": name})

        # Swatches, for the attributes that ask for them. Resolved against the
        # dropdown the admin is actually offering, so nothing here can name a
        # swatch that does not exist. A value that cannot be resolved
        # confidently is reported and left unset — never guessed. A missing
        # swatch is cosmetic; the wrong one is a lie about the product, and it
        # would be invisible until a customer saw it.
        swatches, unresolved, detail = _resolve_swatches(
            product, attribute_id, row, attribute)
        if unresolved:
            log.warning("%s: no swatch for %s", row["sku"], "; ".join(unresolved))

        # Recorded on the step that does the work rather than as its own event:
        # this is what happened to each value, so it belongs beside the action
        # it describes, and one entry per value would otherwise push the run's
        # narrative out of the event tail the console reads.
        _retrying(store, row_id, f"values_{index + 1}",
                  lambda: product.add_attribute_values(
                      attribute_id, attribute["values"], swatches=swatches),
                  detail={"attribute": name, **detail},
                  detail_from=lambda report: _swatch_states(detail, report))

    if not row.get("generate_combinations"):
        return tiers

    # Resolve the colour codes BEFORE generating anything. If the colours
    # cannot be matched to the payload's child SKUs there is no point creating
    # combinations we would then be unable to name — and the failure is the
    # client's data, not the browser's, so it should cost one second, not one
    # page load.
    #
    # Only where the product HAS a colour. The tiers exist to recover which
    # colour a child SKU's code stands for; a sign board's child SKUs encode
    # nothing of the sort, and the sheet states every pairing outright, so
    # combination_map reads it and no tier runs.
    codes = None
    if row.get("colors"):
        resolution = resolve_colours(row)
        store.record_inference(resolution, row_id)
        tiers = resolution.tier_counts
        if resolution.used_llm:
            store.event("colour codes resolved by model", row_id=row_id,
                        data={"tiers": tiers, "codes": resolution.codes})
        if not resolution.ok:
            raise combination_skus.CombinationError(
                resolution.error or "colours could not be resolved")
        codes = resolution.codes

    wanted = combination_skus.combination_map(row, codes)

    page.goto(products.find_by_sku(row["sku"]))
    _retrying(store, row_id, "generate_combinations",
              lambda: product.generate_combinations())

    def apply() -> dict:
        report = product.apply_combination_skus(
            wanted,
            attributes=combination_skus.attribute_names(row),
            allow_out_of_stock=bool(row.get("allow_out_of_stock")),
        )
        if report["unmatched"] or report["missing"]:
            raise library.StepError(mismatch_message(report, row),
                                    step="combinations")
        return report

    _retrying(store, row_id, "combination_skus", apply)
    return tiers


def _create_and_claim(page, products, row, store, row_id, *,
                      base_url: str, owned: set) -> tuple[Any, tuple[str, str | None]]:
    """Create the product `row` names, and record that this run made it.

    The claim is the point of doing this in one place. `created_products` is the
    only record that means "this run brought this product into existence" — it
    is what the ownership guard reads on the next run and the ONLY thing
    rollback will delete — so a product created by any path has to be claimed by
    the same path that created it. A second creation site that forgot to claim
    would leave a product nothing could clean up and the next run would treat as
    the client's own.

    Returns (the ProductPage, the (url, id) pair the caller records as `made`).
    """
    product = _retrying(store, row_id, "create", lambda: products.add_new())

    def save() -> None:
        product.fill_basics(row).save()
        if not product.is_saved():
            raise library.StepError(
                f"saved but no product id: {page.url}", step="save")

    _retrying(store, row_id, "save", save)
    store.claim_product(base_url, row["sku"], page.url, product.product_id)
    _record_local(row["sku"], page.url)
    owned.add(row["sku"])
    return product, (page.url, product.product_id)


def _run_configurator(page, products, row, store, row_id,
                      existing: str | None, *, commit: bool,
                      created: bool = False) -> dict:
    """One phase-2 row: put the workbook's locations and areas on the product.

    THE OWNERSHIP GUARD DOES NOT APPLY HERE, and that is a deliberate exception
    worth being explicit about. Everywhere else a SKU this automation did not
    create is left untouched, because the harm being prevented is bringing a
    product into a client's catalogue — or editing one — that nobody asked for.
    Phase 2 is the opposite case by construction: the workbook names products
    that already exist precisely so their configurator can be filled in, and
    every product in the shipped sheet is one the client made themselves.
    Applying the guard would skip all of them and do nothing.

    What keeps that safe is that this path is additive and narrow. It adds
    locations and areas; it never edits a product field, an attribute, a price
    or a combination, and there is no code path here that can. A location that
    already exists under a different Picario reference is refused rather than
    repointed, and an area already on a spot is left as it is.

    A SKU THAT IS NOT IN THE CATALOGUE IS CREATED, by run_batch, before this
    runs — `created` says whether that happened. The product it makes is a bare
    one: the configurator sheet carries a name and a SKU and nothing else, so
    that is all it can fill, and phase 1 remains the only place a product gets
    its specification, attributes and combinations. What this buys is that a
    configurator row no longer needs phase 1 to have run first; what it costs is
    that a typo'd SKU becomes a new product rather than an error, which is why
    the product is claimed on creation and so can be rolled back like any other.
    """
    sku = row["sku"]

    if not commit:
        if not existing:
            # Nothing to open: the configurator tab lives on a product, so a
            # product that does not exist yet cannot have its references or
            # thumbnails checked. Saying that is better than implying the
            # rehearsal covered them.
            store.event(f"{sku} does not exist — would be created, then configured",
                        row_id=row_id)
            store.row_finished(row_id, status="previewed")
            return {"sku": sku, "kind": "configurator", "error": None,
                    "status": "Pending", "url": None,
                    "detail": {"would_create": True,
                               "locations": [l["name"] for l in row["locations"]],
                               "checked": "nothing yet — the product does not exist"}}

        page.goto(existing)
        detail = _preview_configurator(page, products, row, store, row_id)
        store.row_finished(row_id, status="previewed", product_url=existing)
        return {"sku": sku, "kind": "configurator", "error": None,
                "status": "Pending", "url": existing, "detail": detail}

    page.goto(existing)
    detail = _configure_areas(page, products, row, store, row_id)
    detail["created_product"] = created
    store.event(
        f"{'created and configured' if created else 'configured'} "
        f"{len(detail['locations'])} location(s): {detail['added']} area(s) "
        f"added, {detail['kept']} already set", row_id=row_id, data=detail)
    store.row_finished(row_id, status="added" if created else "resumed",
                       product_url=existing)
    return {"sku": sku, "kind": "configurator", "error": None,
            "status": "Configured", "url": existing, "detail": detail}


def _configure_areas(page, products, row, store, row_id) -> dict:
    """The configurator locations and areas for one phase-2 row.

    Each location is created (or found) and then filled with its areas, one
    location at a time, because the Picario scene that names a location's areas
    only arrives once that location has been saved.
    """
    configurator = ConfiguratorPage(page).open()
    done: list[dict] = []
    problems: list[str] = []
    swapped: list[dict] = []      # locations that needed an alternative reference

    for location in row.get("locations") or []:
        name, reference = location["name"], location["reference_id"]
        wanted = [(area["picario_name"], area["thumbnail"])
                  for area in location["areas"]]
        spots = tuple(area["picario_name"] for area in location["areas"])
        alternatives = tuple(app_configurator.reference_alternatives(reference))
        try:
            location_id, used = _retrying(
                store, row_id, "configurator_location",
                lambda: configurator.ensure_location(
                    name, reference, spots=spots, alternatives=alternatives),
                detail={"location": name, "reference_id": reference,
                        "alternatives": list(alternatives)})

            if used != reference:
                # Never silent. The run worked, but the workbook is wrong, and
                # an alternative that quietly keeps working is a sheet nobody
                # ever fixes.
                swapped.append({"location": name, "sheet": reference, "used": used})
                log.warning("%s: %s used %r, not the workbook's %r",
                            row["sku"], name, used, reference)
                store.event(
                    f"location {name!r}: the workbook's Picario RefrenceId "
                    f"{reference!r} has no {list(spots)}, so this run used "
                    f"{used!r}, which does — correct the sheet",
                    level="warn", row_id=row_id,
                    data={"location": name, "sheet": reference, "used": used})

            report = _retrying(
                store, row_id, "configurator_areas",
                lambda: configurator.ensure_areas(location_id, wanted),
                detail={"location": name, "areas": [w[0] for w in wanted]})
        except Exception as exc:
            # ONE BAD LOCATION MUST NOT COST THE OTHER THREE. Locations are
            # independent — a different Picario scene, its own spots — so a
            # workbook that names one wrong scene should still configure the
            # rest and then say exactly which one it could not do. The row
            # still fails at the end; it just fails having done the work it
            # could, which is the same bargain run_batch already strikes when
            # one row of a batch fails.
            #
            # Only for DETERMINISTIC failures. A dead session or a timeout is
            # not this location's problem, it is the run's, and carrying on
            # would just collect the identical error three more times.
            if classify(exc).retryable:
                raise
            log.error("%s: location %r failed: %s", row["sku"], name, exc)
            store.event(f"location {name!r} could not be configured: {exc}",
                        level="error", row_id=row_id,
                        data={"location": name, "reference_id": reference})
            problems.append(f"{name} — {exc}")
            continue
        done.append({"location": name, "location_id": location_id, **report})

    result = {"locations": done,
              "added": sum(len(d["added"]) for d in done),
              "kept": sum(len(d["kept"]) for d in done),
              "failed_locations": problems,
              "reference_swaps": swapped}

    if problems:
        # Recorded before raising, so a partly-configured product explains
        # itself instead of going quiet at the moment it becomes interesting.
        store.event(f"configured {len(done)} of {len(done) + len(problems)} "
                    f"location(s)", level="warn", row_id=row_id, data=result)
        raise library.StepError(
            f"{len(problems)} of {len(done) + len(problems)} location(s) could "
            f"not be configured ({'; '.join(problems)}). The other "
            f"{len(done)} were done.", step="configurator")

    return result


def _preview_configurator(page, products, row, store, row_id) -> dict:
    """A dry run of one configurator row: everything except creating anything.

    It checks, in the order the things actually go wrong:

      * every Picario reference the row names, against Picario itself
      * every thumbnail, against what this store offers
      * any location that already exists under a different reference

    What it CANNOT check is the Picario Names, and that limit is worth stating
    plainly rather than papering over: a scene only lists its areas once a
    location referencing it has been saved. So a preview proves the references
    and the thumbnails are real, and stops there.
    """
    configurator = ConfiguratorPage(page).open()
    existing = configurator.locations()
    offered = set(configurator.thumbnails())
    unknown: list[str] = []

    for location in row.get("locations") or []:
        for area in location["areas"]:
            if area["thumbnail"] not in offered:
                unknown.append(f"{area['thumbnail']} (row {area['source_row']})")

    if unknown:
        raise library.StepError(
            f"this store has no thumbnail(s) {unknown} — check the Thumbnail "
            f"Name column against the admin's list", step="configurator")

    for location in row.get("locations") or []:
        name, reference = location["name"], location["reference_id"]
        if name in existing:
            store.event(f"location {name!r} already exists — would be reused",
                        row_id=row_id)
            continue
        with store.step(row_id, "validate_reference") as recorded:
            recorded.update({"location": name, "reference_id": reference})
            configurator.validate_reference(name, reference)

    return {"locations": [l["name"] for l in row.get("locations") or []],
            "checked": "references and thumbnails only"}


def _preview_row(page, products, row, store, row_id, existing: str | None) -> dict:
    """A dry run of one row: everything except the irreversible click.

    This is a real rehearsal, not a different code path with the writes removed.
    It catches, in order of how often they actually bite:

      * colours that cannot be matched to the payload's child SKUs — the most
        common data problem, and it costs one second here instead of one page
        load and a half-configured product
      * a SKU that already belongs to somebody else's product
      * a create form that rejects the name or SKU

    What it CANNOT check is anything requiring a saved product: nopCommerce
    will not show the attribute tabs until the product exists. So a preview
    verifies the data and the form, and stops. Saying so plainly matters more
    than implying a completeness we cannot deliver.
    """
    tiers: dict[str, int] = {}

    if row.get("colors") and row.get("generate_combinations"):
        with store.step(row_id, "resolve_colours"):
            resolution = resolve_colours(row)
            store.record_inference(resolution, row_id)
            tiers = resolution.tier_counts
            if not resolution.ok:
                raise combination_skus.CombinationError(
                    resolution.error or "colours could not be resolved")

    if existing:
        store.event(f"{row['sku']} already exists — would be skipped or resumed",
                    row_id=row_id, data={"url": existing})
        return tiers

    with store.step(row_id, "fill_form"):
        product = products.add_new()
        product.fill_basics(row)
        problems = library.validation_errors(page)
        if problems:
            raise library.StepError("the form rejected this row: "
                                    + "; ".join(problems), step="fill_form")
    store.event(f"{row['sku']} would be created", row_id=row_id)
    return tiers


def run_batch(page, rows, *, base_url: str, email: str, password: str,
              commit: bool, on_existing: str,
              progress, store=None) -> dict:
    """Drive `rows` through the admin and report what happened."""
    store = store or _NullStore()
    started = time.monotonic()

    LandingPage(page, url=base_url).login(email, password)
    store.event("signed in", data={"base_url": base_url})

    products = ManageProductsPage(page)
    owned = store.owned_skus(base_url) | _local_ledger_skus()
    results: list[dict] = []
    created = 0

    for index, row in enumerate(rows, 1):
        sku = row["sku"]
        progress(index, len(rows), sku)
        row_id = store.row_started(row, index)
        row_started = time.monotonic()
        tiers: dict[str, int] = {}
        # Set only on the CREATE path, so it means exactly what the rollback
        # means by ownership. A row that dies after this is set left a real,
        # half-configured product behind — recording it here is what lets the
        # run screen say so instead of reporting a bare 'failed'.
        made: tuple[str, str | None] | None = None

        try:
            existing = _retrying(store, row_id, "lookup",
                                 lambda: products.find_by_sku(sku))

            if row.get("kind") == "configurator":
                # Phase 2 attaches to a product; if the sheet names one that is
                # not here yet, make it. Deliberately BEFORE the ownership guard
                # below and never subject to it — see _run_configurator.
                if commit and not existing:
                    _, made = _create_and_claim(
                        page, products, row, store, row_id,
                        base_url=base_url, owned=owned)
                    created += 1
                    existing = made[0]
                    store.event(f"{sku} did not exist — created it to configure",
                                row_id=row_id, data={"url": existing})
                results.append(_run_configurator(
                    page, products, row, store, row_id, existing,
                    commit=commit, created=made is not None))
                continue

            if existing and sku not in owned:
                # Somebody else's product. Open nothing, change nothing.
                if on_existing == "fail":
                    raise library.StepError(
                        f"{sku} already exists at {existing} and was not created "
                        f"by this automation", step="lookup")
                store.row_finished(row_id, status="skipped", product_url=existing,
                                   error="already in the catalogue and not created "
                                         "by this automation — left untouched")
                results.append({"sku": sku, "error": None, "status": "Skipped",
                                "url": existing})
                continue

            if not commit:
                # Dry run. Everything that can be checked without writing gets
                # checked; the one irreversible click is what we stop short of.
                tiers = _preview_row(page, products, row, store, row_id, existing)
                store.row_finished(row_id, status="previewed",
                                   product_url=existing, colour_tiers=tiers)
                results.append({"sku": sku, "error": None, "status": "Pending",
                                "url": existing})
                continue

            if existing:
                page.goto(existing)
                product = ProductPage(page)
                outcome = "resumed"
            else:
                product, made = _create_and_claim(
                    page, products, row, store, row_id,
                    base_url=base_url, owned=owned)
                created += 1
                outcome = "added"

            if row.get("spec_value"):
                _retrying(store, row_id, "specification",
                          lambda: product.add_specification(row["spec_value"]))

            tiers = _configure_attributes(page, products, product, row, store, row_id)

            url = products.find_by_sku(sku) or page.url
            store.row_finished(row_id, status=outcome, product_url=url,
                               product_id=product.product_id, colour_tiers=tiers)
            results.append({"sku": sku, "error": None, "url": url,
                            "status": "Added" if outcome == "added" else "Added"})

        except Exception as exc:
            classified = classify(exc)
            log.error("%s failed (%s): %s", sku, classified.error_class.value, exc)
            # `made` carries the product this row created before it died, so a
            # failed row points at the orphan it left rather than at nothing.
            store.row_finished(row_id, status="failed", error=str(exc),
                               error_class=classified.error_class.value,
                               product_url=made[0] if made else None,
                               product_id=made[1] if made else None,
                               colour_tiers=tiers)
            results.append({"sku": sku, "error": str(exc),
                            "error_class": classified.error_class.value})
        finally:
            log.info("%s took %.1fs", sku, time.monotonic() - row_started)

    failed = sum(1 for r in results if r.get("error"))
    return {
        "succeeded": len(results) - failed,
        "failed": failed,
        "created": created,
        "duration_seconds": round(time.monotonic() - started, 1),
        "results": results,
    }


# ---------------------------------------------------------------------------
# The local ledger. Superseded by created_products in Postgres, kept as the
# fallback for a run with no store — but NOT safe for concurrent workers: it is
# an unlocked read-modify-write, so two processes lose each other's entries.
# Configure DATABASE_URL (and apply schema) before running anything concurrently.
# ---------------------------------------------------------------------------

_LEDGER = Path("var/created-products.json")


def _local_ledger_skus() -> set[str]:
    import json
    if not _LEDGER.exists():
        return set()
    try:
        return set(json.loads(_LEDGER.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def _record_local(sku: str, url: str) -> None:
    import json
    try:
        data = {}
        if _LEDGER.exists():
            data = json.loads(_LEDGER.read_text(encoding="utf-8"))
        data[sku] = url
        _LEDGER.parent.mkdir(parents=True, exist_ok=True)
        _LEDGER.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except (OSError, ValueError) as exc:
        log.warning("could not update the local ledger: %s", exc)
