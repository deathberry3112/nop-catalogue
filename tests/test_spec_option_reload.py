#!/usr/bin/env python3
"""The AJAX reload that put the wrong specification on a product.

Choosing a specification attribute reloads the option <select> over AJAX.
library.wait_ready cannot see that request — it watches for a visible busy
overlay and this one raises none — so everything after it races the response.
Lose that race and the list is replaced *after* an option has been picked,
the selection resets to entry one, and Add posts that instead.

That is not hypothetical twice over: a hard hat was filed as "HighVisVest"
during development, and CP-20-105ECO was filed as "HighVisVest" in a real run.

These need a browser but NOT an admin, so they run in the normal suite: the
whole failure reproduces against a plain <select> and a setTimeout.
"""

from __future__ import annotations

import pytest

from pages.product_page import ProductPage

OPTIONS = ["HighVisVest", "HardHat", "ZingSafetyGlasses", "ZingHighViz"]
SELECT_ID = "AddSpecificationAttributeModel_SpecificationAttributeOptionId"


def _markup(delay_ms: int | None = 600, options: list[str] | None = None) -> str:
    """A select whose options are replaced by IDENTICAL ones after `delay_ms`,
    with the selection reset — exactly what the admin's reload does."""
    opts = "".join(f"<option>{o}</option>" for o in (options or OPTIONS))
    swap = "" if delay_ms is None else f"""
      <script>
        setTimeout(() => {{
          const s = document.getElementById('{SELECT_ID}');
          s.innerHTML = `{opts}`;
          s.selectedIndex = 0;
        }}, {delay_ms});
      </script>"""
    return f'<select id="{SELECT_ID}">{opts}</select>{swap}'


@pytest.fixture(scope="module")
def blank_page(_playwright):
    """A browser with no admin behind it — these tests need neither."""
    browser = _playwright.chromium.launch()
    page = browser.new_page()
    yield page
    browser.close()


@pytest.fixture()
def product(blank_page):
    return ProductPage(blank_page)


def test_comparing_the_content_cannot_see_the_reload(product, blank_page):
    """Why the obvious fix does not work, pinned so nobody re-introduces it:
    the reload serves the same options, so the list reads identically before
    and after while the nodes beneath it are swapped."""
    blank_page.set_content(_markup())
    before = product._spec_option_labels()
    blank_page.wait_for_timeout(900)
    assert product._spec_option_labels() == before      # blind


def test_marking_the_nodes_detects_the_reload(product, blank_page):
    blank_page.set_content(_markup())
    assert product._await_option_reload(timeout_ms=2000) is True


def test_no_reload_times_out_rather_than_hanging(product, blank_page):
    blank_page.set_content(_markup(delay_ms=None))
    assert product._await_option_reload(timeout_ms=400) is False


def test_a_late_reload_resets_the_selection_to_the_first_entry(product, blank_page):
    """The failure itself. Without a re-check, Add posts 'HighVisVest'."""
    blank_page.set_content(_markup())
    chosen = product._select_spec_option("ZingSafetyGlasses")
    assert chosen == "ZingSafetyGlasses"

    blank_page.wait_for_timeout(900)                    # the response lands
    assert product._selected_spec_option() == "HighVisVest"
    assert product._selected_spec_option() != chosen    # what the caller catches


def test_reselecting_after_the_reload_sticks(product, blank_page):
    """The recovery: once the list has settled, selecting again holds."""
    blank_page.set_content(_markup())
    blank_page.wait_for_timeout(900)
    assert product._select_spec_option("ZingSafetyGlasses") == "ZingSafetyGlasses"
    blank_page.wait_for_timeout(300)
    assert product._selected_spec_option() == "ZingSafetyGlasses"


def test_the_workbook_spelling_still_resolves_through_all_of_this(product, blank_page):
    blank_page.set_content(_markup(delay_ms=None))
    assert product._select_spec_option("zingsafetyglasses") == "ZingSafetyGlasses"
