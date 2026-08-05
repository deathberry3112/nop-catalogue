#!/usr/bin/env python3
"""Page object for the admin's front door — the sign-in screen.

An unauthenticated request to ADMIN_BASE_URL never lands on the dashboard: the
site bounces it to /login?ReturnUrl=%2fadmin. So this is the page every run
starts on, and getting past it is the first thing any flow does.

    from pages.landing_page import LandingPage

    LandingPage(page).login(credentials.email, credentials.password)

Selectors live in SEL and nowhere else — when the theme changes, this dict is
the only edit. The values marked `# verified` were checked against the live
CSP Configurator instance; the rest are candidate lists so a theme difference
degrades into "try the next one" instead of a failed run.

Actions go through library (fill/click/wait_ready) rather than raw Playwright
so this page gets the same overlay handling and the same StepError reporting as
the rest of the automation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import library

if TYPE_CHECKING:
    from playwright.sync_api import Page

log = logging.getLogger("pages.landing")


class LandingPage:
    """The login screen at the admin root."""

    SEL = {
        "email":    "#Email",                       # verified
        "password": "#Password",                    # verified
        # The theme renders "Log in" as one of these; ordered most specific
        # first so a page with several submit buttons still picks the right one.
        "submit": [
            "input.login-button",
            "button.login-button",
            "form button[type=submit]",
            "form input[type=submit]",
            "button[type=submit]",
            "input[type=submit]",
        ],
        # Admin chrome that only exists once you are through. Verified against
        # this instance, which is nopCommerce 3.x: a Kendo menu bar across the
        # top, no sidebar — so anything written for 4.x's AdminLTE sidebar
        # matches nothing here.
        # #admin-menu is the stronger of the two: the storefront has a header
        # with a logged-in block too, but only the admin has this menu.
        "signed_in": ["#admin-menu", ".login-info"],    # verified
        "login_info": ".login-info",                    # verified: "Logged in as: <email>"
        "errors": ".validation-summary-errors li, .field-validation-error, .message-error li",
    }

    def __init__(self, page: "Page", *, url: str | None = None,
                 timeout: int = library.TIMEOUT):
        self.page = page
        self.url = url
        self.timeout = timeout

    # --- navigation ------------------------------------------------------

    def open(self) -> "LandingPage":
        """Go to the admin root.

        Pass the full admin URL as `url`. Without it this falls back to "/",
        which against a base_url of https://host/admin resolves to https://host/
        — the storefront, not the admin. You still get a login form there and
        the credentials still work, so the failure is silent: you end up signed
        in to the wrong side of the site with no admin chrome anywhere.

        ready=False because the login page has none of the AJAX chrome that
        wait_ready() watches for.
        """
        library.goto(self.page, self.url or "/", ready=False)
        return self

    # --- state -----------------------------------------------------------

    def has_login_form(self) -> bool:
        return library.exists(self.page, self.SEL["email"])

    def is_signed_in(self) -> bool:
        """True when the admin chrome is on screen.

        A positive check, not `not has_login_form()`: the login form is also
        absent while the next page is still parsing, and on any error page. Only
        the admin menu means we are actually in.
        """
        return library.exists(self.page, self.SEL["signed_in"])

    def signed_in_as(self) -> str:
        """The email in the header's "Logged in as" block, or "" if not signed
        in. Proof of *who* the session belongs to, not just that one exists."""
        if not library.exists(self.page, self.SEL["login_info"]):
            return ""
        text = self.page.locator(self.SEL["login_info"]).first.inner_text()
        # "Logged in as: someone@example.com , Logout?"
        after = text.split(":", 1)[-1]
        return after.split(",")[0].strip()

    def errors(self) -> list[str]:
        """Validation text currently on the page ("Wrong email", "The credentials
        provided are incorrect"). Empty when the form is merely unsubmitted.

        Never raises: this is called from login()'s failure path, where a second
        exception would bury the one worth reading. A page that has crashed or
        been closed simply has no messages to report.
        """
        try:
            out = self.page.locator(self.SEL["errors"]).all_inner_texts()
        except Exception:
            return []
        return [t.strip() for t in out if t and t.strip()]

    # --- the flow --------------------------------------------------------

    def login(self, email: str, password: str, *, timeout: int | None = None) -> "LandingPage":
        """Sign in, and don't return until the admin actually accepted it.

        A no-op when the session is already authenticated, so it is safe to call
        at the top of a batch and again after a suspected session timeout.

        Raises library.StepError naming the admin's own validation message when
        the credentials are rejected — a bare TimeoutError on #Email would tell
        you nothing about why.
        """
        timeout = timeout or self.timeout

        self.open()
        if self.is_signed_in():
            log.debug("already signed in")
            return self

        if not email or not password:
            raise library.StepError("email and password are required", step="login")

        library.fill(self.page, self.SEL["email"], email, timeout=timeout)
        library.fill(self.page, self.SEL["password"], password, timeout=timeout)
        library.click(self.page, self.SEL["submit"], timeout=timeout, settle=False)

        # Wait for the admin menu, not for #Email to disappear. The field goes
        # the instant the old document does, which is well before the dashboard
        # exists — a caller that acted on that would find an empty body. This
        # returns only when the destination page is actually there.
        try:
            library.first_visible(self.page, self.SEL["signed_in"], timeout=timeout)
        except Exception:
            raise library.StepError(f"login failed: {self._why()}", step="login") from None

        library.wait_ready(self.page, timeout=timeout)
        log.info("Signed in as %s", email)          # never log the password
        return self

    def _why(self) -> str:
        """Best available explanation for a rejected login, for the StepError
        message. Every lookup here is best-effort — see errors()."""
        detail = "; ".join(self.errors())
        if not detail:
            try:
                detail = "; ".join(m for _, m in library.alerts(self.page))
            except Exception:
                detail = ""
        return detail or "still on the login form, no message given"
