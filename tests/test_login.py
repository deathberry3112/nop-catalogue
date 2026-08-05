#!/usr/bin/env python3
"""Does LandingPage.login() actually get through the admin's front door?

The one thing every other browser test depends on, so it is worth its own file.

    pytest -m live tests/test_login.py -s
    NOP_HEADED=1 NOP_SLOWMO=300 pytest -m live tests/test_login.py -s

Credentials and the target host come from the `admin_credentials` fixture
(environment -> .env). Nothing here reads os.environ itself and nothing prints
the password.
"""

from __future__ import annotations

import pytest

import library
from pages.landing_page import LandingPage


@pytest.mark.live
def test_login_reaches_the_admin(live_page, admin_credentials):
    """Sign in with real credentials and prove we are past the login form."""
    if not admin_credentials.email or not admin_credentials.password:
        pytest.skip("ADMIN_EMAIL / ADMIN_PASSWORD not set")

    # Start signed out whatever ran before. The page is session-scoped and
    # shared, so by the time this runs another test may already have logged in
    # — and a login test that silently skips the login proves nothing.
    live_page.context.clear_cookies()

    landing = LandingPage(live_page, url=admin_credentials.base_url)
    landing.open()
    assert landing.has_login_form(), (
        f"expected the login form at {live_page.url}, found none — "
        "the admin URL is wrong, or the session survived clear_cookies()"
    )

    landing.login(admin_credentials.email, admin_credentials.password)

    # Success means we are standing on the dashboard — four independent facts,
    # because any one of them alone has a way of being true on a failed login.
    assert not landing.has_login_form(), "login form is still on screen after submit"
    assert "/login" not in live_page.url.lower(), f"still on the login page: {live_page.url}"

    # The ReturnUrl has to have put us inside the admin. Signing in at the
    # storefront root succeeds just as happily and looks fine until you go
    # looking for a menu that isn't there.
    assert "/admin" in live_page.url.lower(), (
        f"signed in, but landed outside the admin: {live_page.url}"
    )
    assert live_page.locator(LandingPage.SEL["signed_in"][0]).first.is_visible(), (
        "no admin menu — signed in, but not on an admin page"
    )
    assert landing.signed_in_as() == admin_credentials.email, (
        f"header says {landing.signed_in_as()!r}, expected {admin_credentials.email!r}"
    )
    assert "Dashboard" in live_page.title(), (
        f"expected the dashboard, page is {live_page.title()!r}"
    )

    print(f"\nlanded on: {live_page.url}")
    print(f"title:     {live_page.title()!r}")
    print(f"header:    Logged in as {landing.signed_in_as()}")


@pytest.mark.live
def test_login_rejects_a_bad_password(live_page, admin_credentials):
    """The failure path reports the admin's own message rather than timing out.

    Runs after the success test on the shared page, so it signs out first —
    login() is a no-op against an authenticated session and would otherwise
    "pass" without testing anything.
    """
    if not admin_credentials.email:
        pytest.skip("ADMIN_EMAIL not set")

    live_page.context.clear_cookies()

    landing = LandingPage(live_page, url=admin_credentials.base_url)
    with pytest.raises(library.StepError) as excinfo:
        landing.login(admin_credentials.email, "definitely-not-the-password",
                      timeout=8_000)

    assert "login failed" in str(excinfo.value)
    print(f"\nrejected as expected: {excinfo.value}")
