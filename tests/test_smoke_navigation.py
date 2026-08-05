#!/usr/bin/env python3
"""Smoke test: launch a real browser and load the admin URL.

No login, no writes, no assumptions about what the page contains beyond it
being the admin's front door. This exists to answer one question before
anything else is debugged: does Playwright launch Chromium here, and does the
configured host answer?

    pytest -m live tests/test_smoke_navigation.py -s
    NOP_HEADED=1 NOP_SLOWMO=300 pytest -m live tests/test_smoke_navigation.py -s

The target comes from the `admin_credentials` fixture, which resolves
ADMIN_BASE_URL from the environment, then .env — never from os.environ here, so
a forgotten export cannot quietly point this at a different host.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.live
def test_admin_url_loads(live_page, admin_credentials):
    """Navigate to ADMIN_BASE_URL and prove a real page came back."""
    url = admin_credentials.base_url
    print(f"\nADMIN_BASE_URL: {url}")

    # Absolute, not "/": the point is to hit the URL as configured, and a "/"
    # navigation against base_url would strip the /admin path off it.
    response = live_page.goto(url, wait_until="domcontentloaded", timeout=30_000)

    assert response is not None, f"no response from {url}"
    assert response.status < 400, f"{url} returned HTTP {response.status}"

    print(f"HTTP {response.status}  {live_page.url}")
    print(f"title: {live_page.title()!r}")
