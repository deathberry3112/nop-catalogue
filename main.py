#!/usr/bin/env python3
"""
Entry point for the nopCommerce product-upload automation.

This is triggered from the web console: the user uploads an .xlsx, the web
layer parses and validates it into rows, and then calls run(). run() does not
parse argv — every setting arrives as a RunOptions object:

    app (xlsx upload) -> rows -> main.run() -> Playwright -> summary
                                            -> xlsxCreator -> report.xlsx

    from main import run, RunOptions
    summary = run(rows, RunOptions(commit=True, output_xlsx="report.xlsx"))

It does three things:
  1. filters the rows it must not touch (already added, or carrying errors)
  2. launches Chromium with the right arguments and builds one context
  3. hands a ready `page` to the automation and returns what came back

All browser/launch configuration lives here. The automation modules never
launch a browser of their own — they receive a page and drive it.

Each row is the dict the parser emits: sku, product_name, spec_value,
color_attr, colors, swatches, size_attr, sizes, generate_combinations,
allow_out_of_stock, combinations, and (optionally) an `errors` list and a
`status` (carried in from a re-uploaded report; rows already "Added" are
skipped so a resumed batch never adds a product twice).

Failure is signalled by exception, never by exit code — a worker process
calling run() needs to catch something, not inspect sys.exit:

    ConfigError    nothing ran: bad input, missing credentials, refused commit
    Exception      the automation itself blew up mid-batch

Running this file directly (`python main.py`) is a convenience for local
development only: it runs DEFAULT_PAYLOAD in dry-run and prints the tally.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlparse

# playwright is imported lazily inside run(), AFTER ensure_dependencies() has
# had a chance to install it. Importing it at module load would crash before we
# could offer to install it.

# ---------------------------------------------------------------------------
# INTEGRATION POINT — the automation entry point run() drives.
#
# run_batch(page, rows, *, base_url, email, password, commit, on_existing,
#           progress) -> summary dict
#
# It must accept an already-created `page`, log in, loop the rows, and return a
# summary. It must NOT call sync_playwright() or launch a browser.
#
# Production is app.automation.runner.run_batch. resolve_runner() imports that
# and falls back to _stub_run_batch only if the import fails.
# ---------------------------------------------------------------------------

log = logging.getLogger("launch")

ROOT = Path(__file__).parent

# Checked-in fixture for `python main.py` and tests. Uploads go through the parser.
DEFAULT_PAYLOAD = ROOT / "parsed_rows.json"

# Row `status` values that mean "already handled — don't add again". A row picks
# one up when the user re-uploads a previous run's report (xlsxCreator writes the
# Status column; the app carries it into each row's `status`). Kept in sync with
# xlsxCreator.DONE_STATUSES.
DONE_STATUSES = {"added", "done", "success", "complete", "skipped",
                 "configured", "added + configured"}


class ConfigError(Exception):
    """The run was refused before the browser opened. Nothing was touched, and
    the message is safe to show the user who uploaded the file."""


# ---------------------------------------------------------------------------
# Run settings
# ---------------------------------------------------------------------------

def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class RunOptions:
    """Everything a run needs beyond the rows themselves.

    The web layer sets `commit` and `output_xlsx` per request; the rest are
    deployment knobs that come from the environment (see from_env) so they can
    be changed on the server without touching the request payload.
    """

    # --- per-request ---------------------------------------------------
    commit: bool = False               # False = fill everything, save nothing
    on_existing: str = "skip"          # "skip" | "fail" when the SKU exists
    output_xlsx: str | None = None     # write the re-uploadable report here

    # --- deployment / debugging ----------------------------------------
    headed: bool = False               # show the browser (never on a server)
    slowmo: int = 0                    # ms between actions; --headed debugging

    def __post_init__(self):
        if self.on_existing not in {"skip", "fail"}:
            raise ConfigError(f"on_existing must be 'skip' or 'fail', got {self.on_existing!r}")

    @classmethod
    def from_env(cls, **overrides) -> "RunOptions":
        """Deployment knobs from NOP_* environment variables, then overrides."""
        base = cls(
            headed=_env_flag("NOP_HEADED", cls.headed),
            slowmo=_env_int("NOP_SLOWMO", cls.slowmo),
        )
        return replace(base, **overrides) if overrides else base


@dataclass(frozen=True)
class Credentials:
    """Which admin to drive, and who to sign in as.

    The web layer builds this per request (form fields, else ADMIN_*). from_env()
    is the CLI / omitted-argument path. It is a parameter, not a module-level
    lookup, so a caller can pin a host without changing process env.

    `password` is repr=False so it cannot surface in a traceback, a log line, or
    a debugger frame dump. Keep it that way: this object gets passed around, and
    the summary it ends up next to is serialised back to the browser.
    """

    base_url: str
    email: str
    password: str = field(repr=False, default="")

    def __post_init__(self):
        # Anything but http(s) — file://, javascript: — is not a site, it is an
        # attack. The URL may come from env or from a request body.
        scheme = urlparse(self.base_url).scheme.lower()
        if scheme not in {"http", "https"}:
            raise ConfigError(f"Admin URL must be http(s), got {self.base_url!r}")

    @classmethod
    def from_env(cls) -> "Credentials":
        return cls(
            base_url=os.environ.get("ADMIN_BASE_URL", "https://propper.csp-1.com/Admin"),
            email=os.environ.get("ADMIN_EMAIL", ""),
            password=os.environ.get("ADMIN_PASSWORD", ""),
        )

    def require(self) -> "Credentials":
        """Raise unless we can actually sign in. Separate from __post_init__ so
        a caller can construct Credentials before email/password are filled."""
        if not self.email or not self.password:
            raise ConfigError("Admin email and password are required.")
        return self


def resolve_runner():
    """Return the automation callable: the real app.automation.runner.run_batch
    if importable, otherwise the built-in stub. Logs which one is active."""
    try:
        from app.automation.runner import run_batch
    except ImportError:
        log.warning("app.automation.runner not found — using the built-in STUB "
                    "runner (navigates only, writes nothing).")
        return _stub_run_batch
    log.info("Using app.automation.runner.run_batch")
    return run_batch


def _stub_run_batch(page, rows, *, base_url, email, password, commit,
                    on_existing, progress):
    """Fallback if app.automation.runner.run_batch cannot be imported.

    Navigates only: no login, no writes. `commit` is ignored. Production never
    takes this path while the runner module is present.
    """
    start = time.monotonic()
    log.info("STUB runner: %d row(s), base_url=%s (no login, no writes)",
             len(rows), base_url)

    results = []
    for i, row in enumerate(rows, 1):
        sku = row.get("sku")
        progress(i, len(rows), sku)
        try:
            # base_url is set on the context, so "/" hits the admin root.
            page.goto("/", wait_until="domcontentloaded")
            results.append({"sku": sku, "error": None})
        except Exception as exc:  # record, don't crash — tests the error path too
            log.error("stub: %s failed: %s", sku, exc)
            results.append({"sku": sku, "error": str(exc)})

    failed = sum(1 for r in results if r["error"])
    return {
        "succeeded": len(results) - failed,
        "failed": failed,
        "created": 0,
        "duration_seconds": round(time.monotonic() - start, 1),
        "results": results,
    }


# ---------------------------------------------------------------------------
# Browser launch arguments. Each earns its place; not a copy-pasted grab bag.
# ---------------------------------------------------------------------------
CHROMIUM_ARGS = [
    # Containers usually can't create the user namespaces Chromium's sandbox
    # needs, so it crashes on start without this. Safe here ONLY because we
    # visit one admin site we control — never drop the sandbox on a browser
    # that opens arbitrary/user-supplied URLs.
    "--no-sandbox",
    "--disable-setuid-sandbox",

    # Docker gives /dev/shm just 64 MB by default; Chromium writes render data
    # there and dies on big pages (your attribute-combination grids qualify).
    # This routes it to /tmp instead. Belt-and-braces with `shm_size: 1gb` in
    # compose — keep both, they cover different failure modes.
    "--disable-dev-shm-usage",

    # No GPU on a headless server; asking for one just logs errors.
    "--disable-gpu",

    # Quieter, more deterministic runs in a server environment.
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--no-first-run",
    "--no-default-browser-check",
]


def build_launch_kwargs(options: RunOptions) -> dict:
    kw = {
        "headless": not options.headed,
        "args": CHROMIUM_ARGS,
        "timeout": 30_000,          # fail fast if Chromium won't start
    }
    if options.slowmo:
        kw["slow_mo"] = options.slowmo
    return kw


def build_context_kwargs(base_url: str, options: RunOptions) -> dict:
    kw = {
        "base_url": base_url,
        "viewport": {"width": 1600, "height": 1000},
        "accept_downloads": True,
        "locale": "en-US",
    }
    return kw


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------

def _already_done(row: dict) -> bool:
    return str(row.get("status", "")).strip().lower() in DONE_STATUSES


def load_rows(source: str | Path) -> list[dict]:
    """Read parsed JSON from a path and normalise to a list of row dicts.

    Accepts either the parser's `{... "rows": [...]}` report or a bare rows
    array. The web layer normally hands run() the list directly and never comes
    through here; this is for the checked-in payload and for tests.
    """
    raw = Path(source).read_text(encoding="utf-8")
    return rows_from(json.loads(raw))


def rows_from(data) -> list[dict]:
    """Validate an already-decoded payload into a list of rows (parser envelope
    or a bare list)."""
    if isinstance(data, dict):
        rows = data.get("rows")
        if rows is None:
            raise ValueError("JSON object has no 'rows' key")
    elif isinstance(data, list):
        rows = data
    else:
        raise ValueError("JSON must be an object with 'rows', or a list of rows")

    if not isinstance(rows, list):
        raise ValueError("'rows' is not a list")
    for i, r in enumerate(rows):
        if not isinstance(r, dict) or not r.get("sku"):
            raise ValueError(f"row {i} is missing an 'sku'")
    return rows


# ---------------------------------------------------------------------------
# Dependencies. A server installs these at build time; call this once at boot
# rather than per request — resolving the Chromium path starts a driver process.
# ---------------------------------------------------------------------------

REQUIRED_PACKAGES = ["playwright"]


def ensure_dependencies(auto_install: bool = False) -> None:
    """Make sure Playwright and its Chromium browser are available.

    Without auto_install this only *checks*, raising ConfigError with the exact
    command to fix it. With auto_install it pip-installs the package and
    downloads Chromium, then returns.
    """
    missing = [p for p in REQUIRED_PACKAGES if importlib.util.find_spec(p) is None]
    if missing:
        if not auto_install:
            raise ConfigError(
                f"Missing Python package(s): {', '.join(missing)}. "
                f"fix: {sys.executable} -m pip install {' '.join(missing)}")
        log.info("Installing package(s): %s", ", ".join(missing))
        subprocess.run([sys.executable, "-m", "pip", "install", *missing], check=True)
        importlib.invalidate_caches()

    # Package present — now confirm the Chromium binary itself is installed.
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            chromium = Path(pw.chromium.executable_path)
        chromium_ok = chromium.exists()
    except Exception as exc:                       # driver/browser not resolvable
        log.debug("Could not resolve Chromium path: %s", exc)
        chromium_ok = False

    if chromium_ok:
        log.debug("Chromium present at %s", chromium)
        return
    if not auto_install:
        raise ConfigError(
            "Playwright is installed but its Chromium browser is not. "
            f"fix: {sys.executable} -m playwright install chromium")
    log.info("Downloading Chromium for Playwright...")
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)


# ---------------------------------------------------------------------------
# run() — what the web layer calls
# ---------------------------------------------------------------------------

def partition(rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Split rows into (runnable, done, blocked).

    `done` — already added on an earlier run, so a resumed batch never adds a
    product twice. `blocked` — the parser left validation errors on the row.
    """
    done = [r for r in rows if _already_done(r)]
    pending = [r for r in rows if not _already_done(r)]
    runnable = [r for r in pending if not r.get("errors")]
    blocked = [r for r in pending if r.get("errors")]
    return runnable, done, blocked


def run(rows: list[dict], options: RunOptions | None = None,
        credentials: Credentials | None = None, *, progress=None,
        store=None) -> dict:
    """Drive the automation over `rows` and return the run summary.

    This is what the web layer calls once it has parsed the uploaded workbook:

        run(rows, RunOptions(commit=True), Credentials(base_url, email, password))

    It never reads argv, never prompts, and never calls sys.exit. Credentials
    default to the environment when the caller omits them (CLI / tests).

    `store` is an optional app.store.RunStore. When given, the runner records
    every row, step and failure against it; when omitted the run behaves
    exactly as it always has. Telemetry is never allowed to be a hard
    dependency of adding products.

    Raises ConfigError if the run is refused before the browser opens.
    """
    options = options or RunOptions.from_env()
    creds = credentials or Credentials.from_env()
    progress = progress or (lambda done, total, sku: log.info("[%d/%d] %s", done, total, sku))

    runnable, done, blocked = partition(rows)
    for r in done:
        log.info("skipping %s: already %s", r.get("sku"), r.get("status"))
    for r in blocked:
        log.warning("skipping %s: %s", r.get("sku"), "; ".join(r["errors"]))

    if not runnable:
        raise ConfigError("No runnable rows: every row is already done or carries errors.")

    # A production catalogue is expensive to unwind; never commit a partial batch.
    if options.commit and blocked:
        raise ConfigError(
            f"Commit refused: {len(blocked)} row(s) carry validation errors. "
            "Fix the workbook and re-upload, or run without commit.")

    creds.require()

    from playwright.sync_api import sync_playwright   # lazy: see module docstring
    run_batch = resolve_runner()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**build_launch_kwargs(options))
        context = browser.new_context(**build_context_kwargs(creds.base_url, options))

        page = context.new_page()
        extra = {"store": store} if store is not None else {}
        try:
            summary = run_batch(
                page,
                runnable,
                base_url=creds.base_url,
                email=creds.email,
                password=creds.password,
                commit=options.commit,
                on_existing=options.on_existing,
                progress=progress,
                **extra,
            )
        finally:
            context.close()
            browser.close()

    # Counts the runner can't know: it only ever saw the runnable rows.
    summary["skipped"] = len(done)
    summary["blocked"] = len(blocked)
    summary["total_rows"] = len(rows)

    log.info("Done in %ss — %d ok, %d failed, %d created, %d skipped, %d blocked",
             summary.get("duration_seconds", "?"), summary.get("succeeded", 0),
             summary.get("failed", 0), summary.get("created", 0),
             summary["skipped"], summary["blocked"])
    for r in summary.get("results", []):
        if r.get("error"):
            log.error("  FAILED %s: %s", r["sku"], r["error"])

    # The report covers ALL rows (done, blocked, and just-run) so Status
    # reflects the whole batch, not only what the runner touched.
    if options.output_xlsx:
        try:
            import xlsxCreator
            counts = xlsxCreator.write_report(rows, summary, options.output_xlsx)
            summary["report"] = {"path": str(options.output_xlsx), "counts": counts}
            log.info("Report %s — %s", options.output_xlsx,
                     ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
        except Exception:
            log.exception("Could not write xlsx report to %s", options.output_xlsx)

    return summary


def run_payload(source: str | Path = DEFAULT_PAYLOAD,
                options: RunOptions | None = None, **kwargs) -> dict:
    """run() against a parsed-rows JSON file. Used by `python main.py` and tests;
    the web layer passes rows from parse_workbook instead."""
    return run(load_rows(source), options, **kwargs)


if __name__ == "__main__":
    # Local development only — the web layer calls run() directly.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        ensure_dependencies()
        summary = run_payload(options=RunOptions.from_env(output_xlsx="report.xlsx"))
    except ConfigError as exc:
        log.error("%s", exc)
        sys.exit(2)
    sys.exit(1 if summary.get("failed") else 0)
