#!/usr/bin/env python3
"""
The front end triggers a run with no argv — RunOptions and the guardrails in
run() are the whole contract now, so they're worth pinning down.

Every guardrail here fires BEFORE playwright is imported or a browser opens, so
these tests need no credentials. One exception, marked in its docstring:
test_run_takes_credentials_from_the_caller_over_the_environment stubs the runner
but still lets run() launch a real (headless) browser, because the point is that
run() wires the caller's values through.
"""

from __future__ import annotations

import pytest

import main


def _row(sku="SKU-1", **over) -> dict:
    base = {"sku": sku, "product_name": sku, "errors": []}
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    """A developer with ADMIN_* exported must not get different results."""
    for var in ("ADMIN_EMAIL", "ADMIN_PASSWORD", "NOP_HEADED", "NOP_SLOWMO"):
        monkeypatch.delenv(var, raising=False)


# --- RunOptions ------------------------------------------------------------

def test_defaults_are_safe():
    """A run triggered with no options must not write to the catalogue."""
    o = main.RunOptions()
    assert o.commit is False
    assert o.on_existing == "skip"
    assert o.headed is False


def test_bad_on_existing_is_rejected_at_construction():
    with pytest.raises(main.ConfigError, match="on_existing"):
        main.RunOptions(on_existing="explode")


def test_options_are_frozen():
    """Nothing mid-run may quietly flip commit on."""
    o = main.RunOptions()
    with pytest.raises(Exception):
        o.commit = True


def test_from_env_reads_deployment_knobs(monkeypatch):
    monkeypatch.setenv("NOP_HEADED", "1")
    monkeypatch.setenv("NOP_SLOWMO", "250")

    o = main.RunOptions.from_env()
    assert (o.headed, o.slowmo) == (True, 250)


def test_from_env_ignores_a_junk_slowmo(monkeypatch):
    """A bad env var must not take the whole worker down at import time."""
    monkeypatch.setenv("NOP_SLOWMO", "fast please")
    assert main.RunOptions.from_env().slowmo == 0


def test_from_env_overrides_beat_the_environment(monkeypatch):
    """Per-request settings come from the caller, not the server's env."""
    monkeypatch.setenv("NOP_HEADED", "1")
    o = main.RunOptions.from_env(commit=True, headed=False)
    assert o.commit is True
    assert o.headed is False


# --- launch kwargs ---------------------------------------------------------

def test_headless_unless_headed():
    assert main.build_launch_kwargs(main.RunOptions())["headless"] is True
    assert main.build_launch_kwargs(main.RunOptions(headed=True))["headless"] is False


def test_slowmo_is_omitted_when_zero():
    """Passing slow_mo=0 is not the same as not passing it."""
    assert "slow_mo" not in main.build_launch_kwargs(main.RunOptions())
    assert main.build_launch_kwargs(main.RunOptions(slowmo=50))["slow_mo"] == 50


def test_context_carries_base_url():
    """library.login() does goto(page, "/") and takes no base_url of its own, so
    login works only if the context has it."""
    kw = main.build_context_kwargs("https://example.test/Admin", main.RunOptions())
    assert kw["base_url"] == "https://example.test/Admin"


def test_sandbox_flags_are_present():
    """Dropped from CHROMIUM_ARGS, Chromium won't start in a container."""
    assert "--no-sandbox" in main.build_launch_kwargs(main.RunOptions())["args"]
    assert "--disable-dev-shm-usage" in main.build_launch_kwargs(main.RunOptions())["args"]


# --- partition -------------------------------------------------------------

def test_partition_splits_runnable_done_and_blocked():
    rows = [
        _row("ok"),
        _row("already", status="Added"),
        _row("bad", errors=["no colors"]),
    ]
    runnable, done, blocked = main.partition(rows)
    assert [r["sku"] for r in runnable] == ["ok"]
    assert [r["sku"] for r in done] == ["already"]
    assert [r["sku"] for r in blocked] == ["bad"]


@pytest.mark.parametrize("status", sorted(main.DONE_STATUSES))
def test_every_done_status_is_treated_as_done(status):
    """A re-uploaded report must never add a product a second time."""
    _, done, _ = main.partition([_row(status=status.upper())])
    assert len(done) == 1


def test_done_wins_over_errors():
    """An already-added row is not re-run just because it also has errors."""
    _, done, blocked = main.partition([_row(status="Added", errors=["stale"])])
    assert len(done) == 1 and not blocked


# --- run() guardrails: all raise before a browser opens --------------------

def test_run_refuses_an_empty_batch():
    with pytest.raises(main.ConfigError, match="No runnable rows"):
        main.run([_row(status="Added")])


def test_run_refuses_to_commit_a_partial_batch():
    """Half a catalogue is expensive to unwind; blocked rows abort the commit."""
    rows = [_row("ok"), _row("bad", errors=["no colors"])]
    with pytest.raises(main.ConfigError, match="Commit refused"):
        main.run(rows, main.RunOptions(commit=True))


def test_blocked_rows_do_not_stop_a_dry_run():
    """Same batch, no commit: it should get past the guardrail and fail later on
    the missing credentials instead."""
    rows = [_row("ok"), _row("bad", errors=["no colors"])]
    with pytest.raises(main.ConfigError, match="password are required"):
        main.run(rows, main.RunOptions(commit=False))


def test_run_refuses_without_credentials():
    with pytest.raises(main.ConfigError, match="password are required"):
        main.run([_row()])


def test_run_takes_credentials_from_the_caller_over_the_environment(monkeypatch):
    """Caller-supplied Credentials beat ADMIN_* env vars."""
    monkeypatch.setenv("ADMIN_EMAIL", "env@example.test")
    monkeypatch.setenv("ADMIN_PASSWORD", "env-secret")
    seen = {}

    def fake_run_batch(page, rows, **kw):
        seen.update(kw)
        return {"succeeded": 0, "failed": 0, "created": 0, "results": []}

    monkeypatch.setattr(main, "resolve_runner", lambda: fake_run_batch)
    creds = main.Credentials("https://form.example.test/Admin", "form@example.test", "form-secret")
    main.run([_row()], main.RunOptions(), creds)

    assert seen["email"] == "form@example.test"
    assert seen["base_url"] == "https://form.example.test/Admin"


# --- Credentials -----------------------------------------------------------

def test_credentials_come_from_the_environment_today(monkeypatch):
    monkeypatch.setenv("ADMIN_BASE_URL", "https://shop.example.test/Admin")
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2")

    c = main.Credentials.from_env()
    assert (c.base_url, c.email, c.password) == (
        "https://shop.example.test/Admin", "admin@example.test", "hunter2")


def test_password_never_appears_in_repr():
    """This object sits next to the summary that gets serialised back to the
    browser, and shows up in any traceback that includes the frame."""
    c = main.Credentials("https://example.test/Admin", "admin@example.test", "hunter2")
    assert "hunter2" not in repr(c)
    assert "admin@example.test" in repr(c)      # identifying the run is fine


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "javascript:alert(1)",
    "ftp://example.test",
    "example.test/Admin",                        # no scheme at all
])
def test_non_http_admin_urls_are_rejected(url):
    """base_url must be http(s), whether it came from env or a request."""
    with pytest.raises(main.ConfigError, match="http"):
        main.Credentials(url, "admin@example.test", "hunter2")


def test_partial_credentials_construct_but_do_not_pass_require():
    """Incomplete Credentials may be constructed; require() is the gate."""
    c = main.Credentials("https://example.test/Admin", "admin@example.test", "")
    with pytest.raises(main.ConfigError, match="password are required"):
        c.require()


def test_require_returns_self_so_it_can_be_chained():
    c = main.Credentials("https://example.test/Admin", "a@b.test", "pw")
    assert c.require() is c


# --- payload -------------------------------------------------------------

def test_rows_from_accepts_the_parser_report():
    assert len(main.rows_from({"total_rows": 1, "rows": [_row()]})) == 1


def test_rows_from_accepts_a_bare_list():
    assert len(main.rows_from([_row()])) == 1


def test_rows_from_rejects_a_missing_sku():
    with pytest.raises(ValueError, match="missing an 'sku'"):
        main.rows_from([_row(sku="")])


def test_rows_from_rejects_a_bare_object():
    with pytest.raises(ValueError, match="no 'rows' key"):
        main.rows_from({"total_rows": 3})
