#!/usr/bin/env python3
"""
Helpers for writing the Playwright automation — the toolbox app/automation/*
imports so the runner reads like the steps a human takes, not like DOM plumbing.

    xlsx -> parser -> main.run -> [runner + library] -> summary -> xlsxCreator.py

main.py owns the browser; this module never launches one. Every function here
takes an already-created `page` (or a Locator) and drives it. That keeps the
launch path in exactly one place and makes each helper testable against any
page you hand it.

What's here, in the order you'll reach for it:

    steps/errors    step(), StepError, retry()
    navigation      goto(), wait_ready()
    interaction     click(), fill(), select(), text_of(), exists(), first_visible()
    nop widgets     open_tab()
    nop feedback    alerts(), validation_errors(), dismiss_alerts()

One convention worth knowing before you use it: anywhere a helper takes
`target`, you can pass a CSS/text selector string, a Playwright Locator, or a
list of candidate selectors — the first one that becomes visible wins (see
first_visible). nop's admin markup shifts between versions, so a list is how you
survive that without an if/else.

WHAT IS NOT HERE, deliberately: the flows. Signing in and driving the product
form live in pages/ as page objects, because those needed selectors verified
against this instance (nopCommerce 3.x) and a generic helper that quietly
matches nothing is worse than no helper at all. This module keeps only what is
version-agnostic enough to be reused.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Sequence

if TYPE_CHECKING:                      # playwright is imported lazily by main.py;
    from playwright.sync_api import Locator, Page   # never import it at load time

log = logging.getLogger("library")

# Default action timeout, ms. Generous enough for the admin's slower AJAX posts
# (attribute-combination generation especially), short enough that a genuinely
# stuck run fails inside a batch window instead of hanging until someone looks.
TIMEOUT = 15_000

# Admin selectors general enough to hold across nop versions. Anything this
# instance renders differently lives in the page object that touches it —
# pages/landing_page.py owns the sign-in markup, pages/product_page.py the
# product form — because those were verified against it and these were not.
SEL: dict[str, Any] = {
    # --- chrome / busy indicators ---------------------------------------
    # nop shows this block during every admin AJAX post; Kendo grids add their
    # own mask. Both must be gone before a click is trustworthy.
    "busy": [".ajax-loading-block-window", ".k-loading-mask", ".k-loading-image"],

    # --- feedback --------------------------------------------------------
    "alert_success":  ".alert-success",
    "alert_error":    ".alert-danger, .alert-error",
    "alert_warning":  ".alert-warning",
    "alert_close":    ".close, [data-dismiss=alert]",
    "validation":     ".validation-summary-errors li, .field-validation-error",

    # --- layout ----------------------------------------------------------
    "tab": ".nav-tabs a",
}


class StepError(Exception):
    """A step failed in a way worth reporting per-row.

    Carries the step name so the runner can put a real message into its
    summary — which is what xlsxCreator turns into the Note column. Raise this
    for "this product could not be added"; let genuinely unexpected exceptions
    propagate so main.py's fatal path catches them.
    """

    def __init__(self, message: str, *, step: str = ""):
        super().__init__(message)
        self.step = step

    def __str__(self) -> str:                       # what lands in the xlsx Note
        base = super().__str__()
        return f"{self.step}: {base}" if self.step else base


# ---------------------------------------------------------------------------
# Steps and retries
# ---------------------------------------------------------------------------

@contextmanager
def step(name: str, *, sku: str = ""):
    """Name a block of work: log it and time it.

    Turns an anonymous timeout deep in a helper into "attributes/add-color
    failed after 15.2s" — the difference between a debuggable run and a shrug.

        with library.step("add colors", sku=sku):
            for colour in row["colors"]:
                library.click(page, SEL["add_attribute"])

    Re-raises as StepError so the runner's per-row except can record it and
    move to the next product.
    """
    label = f"{sku} {name}".strip()
    log.debug("-> %s", label)
    start = time.monotonic()
    try:
        yield
    except StepError:
        raise                                        # already named
    except Exception as exc:
        took = time.monotonic() - start
        log.error("x  %s failed after %.1fs: %s", label, took, exc)
        raise StepError(str(exc), step=name) from exc
    log.debug("<- %s (%.1fs)", label, time.monotonic() - start)


def retry(fn: Callable[[], Any], *, attempts: int = 3, delay: float = 0.5,
          backoff: float = 2.0, on: tuple[type[BaseException], ...] = (Exception,),
          what: str = "") -> Any:
    """Call fn(), retrying transient failures with a growing pause.

    For flake with a real cause you can't fix from here — an admin AJAX post
    that occasionally 500s, a grid that re-renders under you. Do NOT wrap a
    save in this: a retried save can create the product twice. Waiting for an
    element is `expect`'s job, not this one.
    """
    last: BaseException | None = None
    pause = delay
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except on as exc:
            last = exc
            if attempt == attempts:
                break
            log.warning("retry %d/%d%s: %s", attempt, attempts,
                        f" ({what})" if what else "", exc)
            time.sleep(pause)
            pause *= backoff
    raise StepError(f"failed after {attempts} attempts: {last}", step=what)


# ---------------------------------------------------------------------------
# Locating and interacting
# ---------------------------------------------------------------------------

def _candidates(target: "str | Locator | Sequence[str]") -> list:
    if isinstance(target, str):
        return [target]
    if isinstance(target, (list, tuple)):
        return list(target)
    return [target]                                  # already a Locator


def first_visible(page: "Page", target: "str | Locator | Sequence[str]",
                  *, timeout: int = TIMEOUT) -> "Locator":
    """Resolve `target` to the first candidate that is actually visible.

    A single selector is passed straight through. A list is polled in order
    until one shows up — which is how a helper survives nop's markup differing
    between 4.1 and 4.6 without the runner caring which it is.

    Raises StepError (not a bare TimeoutError) naming every selector tried, so
    a broken selector is obvious from the log line alone.
    """
    cands = _candidates(target)
    if len(cands) == 1:
        loc = page.locator(cands[0]).first if isinstance(cands[0], str) else cands[0]
        loc.wait_for(state="visible", timeout=timeout)
        return loc

    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        for cand in cands:
            loc = page.locator(cand).first if isinstance(cand, str) else cand
            try:
                if loc.is_visible():
                    return loc
            except Exception:
                continue                             # detached mid-poll; try the next
        page.wait_for_timeout(100)
    raise StepError("none of these became visible: " +
                    " | ".join(str(c) for c in cands), step="locate")


def click(page: "Page", target: "str | Locator | Sequence[str]", *,
          timeout: int = TIMEOUT, settle: bool = True) -> None:
    """Click, after making sure the click can land.

    Scrolls into view, waits out any admin/Kendo busy overlay first (an overlay
    swallows the click and you get a silent no-op rather than an error), then
    waits for things to settle again so the next call starts from a quiet page.
    """
    if settle:
        wait_ready(page, timeout=timeout)
    loc = first_visible(page, target, timeout=timeout)
    loc.scroll_into_view_if_needed(timeout=timeout)
    loc.click(timeout=timeout)
    if settle:
        wait_ready(page, timeout=timeout)


def fill(page: "Page", target: "str | Locator | Sequence[str]", value: str, *,
         timeout: int = TIMEOUT) -> None:
    """Set an input's value, replacing whatever was there."""
    loc = first_visible(page, target, timeout=timeout)
    loc.scroll_into_view_if_needed(timeout=timeout)
    loc.fill("" if value is None else str(value), timeout=timeout)


def select(page: "Page", target: "str | Locator | Sequence[str]", value: str, *,
           by: str = "label", timeout: int = TIMEOUT) -> None:
    """Choose an option in a plain <select> by visible label (default) or value.

    For a Kendo dropdown — a styled <span> over a hidden input — this won't
    work; click the widget and pick from its popup list instead, as
    ProductPage._pick_kendo does.
    """
    loc = first_visible(page, target, timeout=timeout)
    loc.select_option(label=value if by == "label" else None,
                      value=value if by == "value" else None,
                      timeout=timeout)


def text_of(page: "Page", target: "str | Locator | Sequence[str]", *,
            timeout: int = TIMEOUT) -> str:
    return (first_visible(page, target, timeout=timeout).inner_text() or "").strip()


def exists(page: "Page", target: "str | Locator | Sequence[str]") -> bool:
    """Is it in the DOM right now? No waiting — use for branching, not gating."""
    for cand in _candidates(target):
        loc = page.locator(cand) if isinstance(cand, str) else cand
        try:
            if loc.count() > 0:
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Navigation and waiting
# ---------------------------------------------------------------------------

def goto(page: "Page", path: str, *, wait_until: str = "domcontentloaded",
         timeout: int = 30_000, ready: bool = True) -> None:
    """Navigate. `path` is relative — main.py sets base_url on the context, so
    "/Product/List" resolves against the admin root and nothing here hardcodes
    a host.

    domcontentloaded rather than networkidle on purpose: the admin keeps
    background requests going, so networkidle can wait forever on a page that
    has been usable for seconds. wait_ready() below is the real gate.
    """
    page.goto(path, wait_until=wait_until, timeout=timeout)
    if ready:
        wait_ready(page, timeout=timeout)


def wait_ready(page: "Page", *, timeout: int = TIMEOUT) -> None:
    """Wait until no busy overlay is up — the admin's AJAX block and Kendo's
    loading mask. Cheap, so callers use it liberally before and after actions."""
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        if not any(_any_visible(page, sel) for sel in SEL["busy"]):
            return
        page.wait_for_timeout(100)
    log.debug("wait_ready: overlay still up after %dms — continuing anyway", timeout)


def _any_visible(page: "Page", selector: str) -> bool:
    try:
        return page.locator(selector).first.is_visible()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# nopCommerce admin widgets
# ---------------------------------------------------------------------------

def open_tab(page: "Page", name: str, *, timeout: int = TIMEOUT) -> None:
    """Click a product-edit tab by its visible name ("Attributes", "Inventory").
    A no-op if it's already the active tab."""
    tab = page.locator(SEL["tab"]).filter(has_text=name).first
    tab.wait_for(state="visible", timeout=timeout)
    if "active" in (tab.get_attribute("class") or ""):
        return
    tab.click(timeout=timeout)
    wait_ready(page, timeout=timeout)


# ---------------------------------------------------------------------------
# Reading what the admin tells you
# ---------------------------------------------------------------------------

def alerts(page: "Page") -> list[tuple[str, str]]:
    """Every visible admin notification as (kind, message), kind in
    success|error|warning. Read before dismissing, obviously."""
    out: list[tuple[str, str]] = []
    for kind, sel in (("success", SEL["alert_success"]),
                      ("error", SEL["alert_error"]),
                      ("warning", SEL["alert_warning"])):
        loc = page.locator(sel)
        for i in range(loc.count()):
            item = loc.nth(i)
            try:
                if item.is_visible():
                    out.append((kind, _clean_alert(item.inner_text())))
            except Exception:
                continue
    return out


def _clean_alert(text: str | None) -> str:
    """Drop the dismiss button's glyph from an alert's text.

    inner_text() includes the nested close button, so a message arrives as
    "The product has been added. ×" — and that string is what ends up in the
    report's Note column. Trim it there rather than at every call site.

    Only the &times; glyph nop actually renders is stripped; stripping a plain
    "x" too would eat the last letter of a message that legitimately ends in one.
    """
    return (text or "").strip().rstrip("×").strip()


def validation_errors(page: "Page") -> list[str]:
    """Client/server field-validation messages on the current form."""
    loc = page.locator(SEL["validation"])
    msgs = []
    for i in range(loc.count()):
        try:
            text = (loc.nth(i).inner_text() or "").strip()
        except Exception:
            continue
        if text:
            msgs.append(text)
    return msgs


def dismiss_alerts(page: "Page") -> None:
    """Close open notifications.

    Not cosmetic: a stacked alert bar pushes the page down and can cover the
    save button, so a later click lands on the alert instead. Call it after
    reading a result and before the next action.
    """
    close = page.locator(SEL["alert_close"])
    for _ in range(close.count()):
        try:
            close.first.click(timeout=1_000)
        except Exception:
            break
