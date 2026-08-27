#!/usr/bin/env python3
"""
pytest fixtures for the nopCommerce automation.

ONE browser, ONE context, ONE page for the entire session. Every test that
touches the admin shares the same tab:

    _playwright -> browser -> live_page
       (all session-scoped, all torn down in reverse at the end of the run)

There are no pytest command-line flags here on purpose. The automation is
triggered from the front end with a RunOptions object, not argv, so the tests
build the same object the web layer does — the browser under test is configured
by exactly the code path that configures it in production. Turn knobs with the
same NOP_* environment variables the server uses:

    pytest                                   # unit tests only; no browser
    NOP_HEADED=1 NOP_SLOWMO=300 pytest -m live

Sharing one page is a deliberate trade, not a shortcut. It buys a single login
and a realistic session, and it costs test isolation: tests run in file/definition
order and state leaks forward. The _page_hygiene fixture below clears alerts
after every test, but a test that needs a known starting point should navigate
there itself (library.goto) rather than assume the previous test left it there.

Browser tests are marked `live` and skip unless ADMIN_EMAIL / ADMIN_PASSWORD
are set. Everything else runs with no browser and no credentials.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import library
import main

REPO_ROOT = Path(__file__).resolve().parent

# The page currently handed out by the `live_page` fixture, or None if no test has
# needed a browser yet. _page_hygiene uses this to tell "a page exists and may
# be dirty" from "no browser was ever launched" — asking pytest for the fixture
# instead would *create* the page (and launch Chromium) just to clean it up.
_live_page = None


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: drives a real nopCommerce admin; skipped unless "
        "ADMIN_EMAIL / ADMIN_PASSWORD are set",
    )
    config.addinivalue_line(
        "markers",
        "cleanup: DELETES products from the catalogue. Never runs unless you "
        "ask for it by name: pytest -m 'live and cleanup'",
    )


# ---------------------------------------------------------------------------
# settings + data — no browser, available to every test
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def options() -> main.RunOptions:
    """The same object the front end hands run(). Tests never commit."""
    return main.RunOptions.from_env(commit=False)


# --- settings that come from a file, not the environment --------------------
#
# Nothing in the app loads .env — main.Credentials.from_env() reads os.environ
# and falls back to a *different* default host when ADMIN_BASE_URL is unset, so
# a test that forgot to export it would quietly drive the wrong site. These
# resolve env -> .env instead, so the browser tests drive the admin the repo is
# actually configured for.
#
# Deliberately NOT pushed into os.environ: the `credentials` fixture above and
# the unit tests must keep seeing the real environment and nothing else.

def _env_file_value(name: str, filename: str) -> str | None:
    """`name`'s value in `filename`, or None. Five lines, not python-dotenv:
    KEY=value, '#' comments, blanks. Enough for the three keys we ship."""
    path = REPO_ROOT / filename
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip() or None
    return None


def resolve_setting(name: str) -> str | None:
    for value in (os.environ.get(name), _env_file_value(name, ".env")):
        if value:
            return value
    return None


@pytest.fixture(scope="session")
def admin_credentials() -> main.Credentials:
    """Credentials for the browser tests, resolved from the env files above."""
    url = resolve_setting("ADMIN_BASE_URL")
    if not url:
        pytest.fail("ADMIN_BASE_URL is not set in the environment or .env")
    return main.Credentials(
        base_url=url,
        email=resolve_setting("ADMIN_EMAIL") or "",
        password=resolve_setting("ADMIN_PASSWORD") or "",
    )


class CreatedLedger:
    """A record of the products this automation created, on disk.

    The delete test deletes what is in here and nothing else. That is the whole
    point of it: several payload SKUs already exist in the catalogue as products
    nobody here made (CP-30-C100V is product 388, with attributes and
    combinations already set up), and "delete the payload SKUs" would destroy
    them. Creating a product is one click; un-deleting one is a restore from
    backup.

    Survives between runs so you can add today and clean up tomorrow, and it is
    under var/ which is gitignored.
    """

    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def record(self, sku: str, url: str) -> None:
        data = self._read()
        data[sku] = url
        self._write(data)

    def forget(self, sku: str) -> None:
        data = self._read()
        data.pop(sku, None)
        self._write(data)

    def all(self) -> dict:
        return self._read()

    def __contains__(self, sku: str) -> bool:
        return sku in self._read()


@pytest.fixture(scope="session")
def created_ledger() -> CreatedLedger:
    return CreatedLedger(REPO_ROOT / "var" / "created-products.json")


@pytest.fixture(scope="session")
def report_path() -> Path:
    """Where the run writes its .xlsx report.

    Defaults to xlsxCreator's own DEFAULT_OUTPUT at the repo root, which is
    where `python xlsxCreator.py` puts it too — one report, one place, whether
    it came from a run or was regenerated by hand. Override with
    NOP_OUTPUT_XLSX to keep a run's report separate.
    """
    override = os.environ.get("NOP_OUTPUT_XLSX")
    return Path(override) if override else REPO_ROOT / "report.xlsx"


@pytest.fixture(scope="session")
def rows() -> list[dict]:
    """The checked-in parsed_rows.json fixture, validated through main.load_rows().
    Production uploads go through parse_workbook, not this file.

    Two knobs narrow it, for when you want a two-minute run rather than a
    ten-minute one:

        NOP_ROW_SKUS=CP-30-C100,CP-V-200    just these, in payload order
        NOP_ROW_LIMIT=2                     just the first N

    Both narrow the payload itself rather than skipping rows mid-run, so the
    .xlsx report covers exactly what was attempted. A report listing five
    untouched rows as Pending would read like a run that died, not one that was
    deliberately small.
    """
    payload = main.load_rows(main.DEFAULT_PAYLOAD)

    wanted = [s.strip() for s in os.environ.get("NOP_ROW_SKUS", "").split(",") if s.strip()]
    if wanted:
        by_sku = {row["sku"]: row for row in payload}
        unknown = [s for s in wanted if s not in by_sku]
        if unknown:
            pytest.fail(f"NOP_ROW_SKUS names rows that are not in the payload: {unknown}")
        payload = [row for row in payload if row["sku"] in set(wanted)]

    limit = _env_int("NOP_ROW_LIMIT", 0)
    return payload[:limit] if limit > 0 else payload


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# the shared browser stack — nothing here runs until a test asks for `live_page`
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def _playwright():
    from playwright.sync_api import sync_playwright   # lazy, exactly as main.py does
    with sync_playwright() as pw:
        yield pw


@pytest.fixture(scope="session")
def browser(_playwright, options):
    b = _playwright.chromium.launch(**main.build_launch_kwargs(options))
    yield b
    b.close()


@pytest.fixture(scope="session")
def _live_selected(request):
    """Skip unless the run explicitly asked for live tests.

    A bare `pytest` is documented above as browser-free, and this is what
    enforces it — not a credentials check, which would pass for anyone whose
    .env is filled in, i.e. everyone who can run these at all. Requested first
    by live_page so the skip happens before Chromium is launched, not after.
    """
    if "live" not in (request.config.getoption("-m") or ""):
        pytest.skip("drives a real browser — select it with `pytest -m live`")


@pytest.fixture(scope="session")
def live_page(_live_selected, browser, options, admin_credentials):
    """THE page, bound to the resolved ADMIN_BASE_URL.

    Session-scoped on purpose: every test shares this one tab. That is a
    deliberate trade — it buys a single login and a realistic session, and it
    costs isolation, so state leaks forward between tests by design.
    """
    global _live_page
    ctx = browser.new_context(**main.build_context_kwargs(admin_credentials.base_url, options))
    p = ctx.new_page()
    _live_page = p
    yield p
    _live_page = None
    ctx.close()


# ---------------------------------------------------------------------------
# per-test hygiene on the shared page
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _page_hygiene(request):
    """Runs after every test. Clears alerts so the next test doesn't inherit a
    notification bar sitting over the save button (see library.dismiss_alerts)."""
    yield
    page = _live_page
    if page is None:
        return                                   # no test has needed a browser
    try:
        library.dismiss_alerts(page)
    except Exception:
        pass
