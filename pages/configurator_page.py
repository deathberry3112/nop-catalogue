#!/usr/bin/env python3
"""Page object for the product's "Configurator Areas" tab.

    configurator = ConfiguratorPage(page).open()
    location_id = configurator.ensure_location("Front", "G347LA-Front.pfs")
    report = configurator.ensure_areas(location_id, [
        ("K-chest-upper-left-vest-without-sleeves",
         "/thumbnails/K-chest-upper-left-vest-without-sleeves.png")])

NOT STOCK NOPCOMMERCE. This tab is the Aperture.CSPConfigurator plugin, and it
is a Knockout app rather than a Kendo grid, so none of the widget helpers the
rest of pages/ leans on apply here. Its content is fetched on demand from
/Admin/Widget/Plugins/ConfiguratorAdminProductEdit/ExtensionTab/{productId},
which is why the tab has to be CLICKED before anything below exists — loading
the product page is not enough.

THE MODEL, two deep:

    Location {Id, LocationName: "Front", ImageName: "G347LA-Front.pfs"}
      └─ Area {Id, LocationId, ImageIndex: 1, ImageName: "/thumbnails/x.png",
               ZoomIncrement: 10}

`Area.ImageIndex` IS THE WHOLE REASON THIS CLASS EXISTS. The workbook names an
area in words ("K-chest-upper-left-vest-without-sleeves") and the plugin stores
an integer. The integer is not derivable — it is an id Picario assigns to a spot
within one scene, and it arrives only when the location is saved:

    SaveLocation -> {"Scene": {"SceneId": "G55FF-Front.pfs", "SceneAreas": [
                        {"Id": 0, "Name": "Full Color"},
                        {"Id": 1, "Name": "K-chest-upper-left-vest-without-sleeves"},
                        {"Id": 2, "Name": "F-abdomen-right-vest-without-sleeves"}]}}

So the name is matched against that list and nothing else. A name the scene does
not offer raises, naming what the scene DOES offer — it is never approximated,
because area 1 and area 2 are both real spots on the garment and putting the
logo on the wrong one is invisible until a customer orders it. Same rule, same
reason, as the colour swatches in app/colour/swatch.py.

READS GO THROUGH THE VIEWMODEL, WRITES GO THROUGH THE UI. `productAreaViewModel`
is a global the plugin leaves on the page, and reading it with ko.mapping.toJS
is simply a more honest way of asking "what does this page currently hold" than
scraping a table that Knockout rebinds underneath you. Every write is a real
click or a real select: the Picario reference is validated by the admin as it is
typed, and the save buttons do not exist until it passes.

Verified against nopCommerce 3.x on zinggreenproductsdev.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import library

if TYPE_CHECKING:
    from playwright.sync_api import Page

log = logging.getLogger("pages.configurator")

#: What a new area gets when the sheet does not say. The plugin's own default.
DEFAULT_ZOOM_INCREMENT = 10


class ConfiguratorPage:
    """The Configurator Areas tab of one product."""

    TAB_LABEL = "Configurator Areas"

    SEL = {
        "container":     "#productAreasContainer",
        # The Knockout bindings are the stable identifiers here: this markup has
        # almost no ids, and the two <select>s in an area row are told apart by
        # what they are bound to, not by position.
        "add_location":   "button[data-bind*='click: addLocation']",
        "remove_location": "button[data-bind*='click: removeLocation']",
        "location_select": "select[data-bind*='LocationName']",
        "location_name":   "input[name=locationName]",
        "location_image":  "input[name=locationImage]",
        "save_location":   "button[data-bind*='saveLocation']",
        "cancel_location": "button[data-bind*='cancelAddingLocation']",
        "areas_table":     "table.adminContent",
        "area_rows":       "table.adminContent tbody tr",
        "add_area":        "#btnAdd",
        "save_areas":      "button[data-bind*='saveAreas']",
        "picario_select":  "select[data-bind*='SceneAreas']",
        "thumbnail_select": "select[data-bind*='Thumbnails']",
        "zoom":            "input[data-bind*='ZoomIncrement']",
        "message":         "div[data-bind*='$root.message()']",
        "error":           "div[data-bind*='errorMessage']",
    }

    def __init__(self, page: "Page", *, timeout: int = library.TIMEOUT):
        self.page = page
        self.timeout = timeout

    # --- getting to the tab ----------------------------------------------

    def open(self) -> "ConfiguratorPage":
        """Click the tab and wait for the plugin to finish booting.

        Waiting on the container alone is not enough: the markup arrives before
        Knockout has applied its bindings, and a click landing in that gap hits
        a button whose handler does not exist yet. `productAreaViewModel` is the
        global the plugin assigns last, so waiting for it is waiting for the
        page to actually be usable.
        """
        library.click(self.page, [
            f"ul.k-tabstrip-items a.k-link:text-is('{self.TAB_LABEL}')",
            f"a.k-link:text-is('{self.TAB_LABEL}')",
            f"[role=tab]:has-text('{self.TAB_LABEL}')",
            f"text='{self.TAB_LABEL}'",
        ], timeout=self.timeout, settle=False)

        self.page.locator(self.SEL["container"]).wait_for(
            state="visible", timeout=self.timeout)
        try:
            self.page.wait_for_function(
                "() => typeof productAreaViewModel !== 'undefined'"
                " && typeof ko !== 'undefined'",
                timeout=self.timeout)
        except Exception:
            raise library.StepError(
                "the Configurator Areas tab never finished loading — is the "
                "CSPConfigurator plugin enabled on this store?",
                step="configurator") from None
        return self

    # --- reading ----------------------------------------------------------

    def state(self) -> dict:
        """The plugin's viewmodel as plain data."""
        return self.page.evaluate("() => ko.mapping.toJS(productAreaViewModel)")

    def locations(self) -> dict[str, int]:
        """{location name: id} for the locations this product already has."""
        return {str(loc["LocationName"]): int(loc["Id"])
                for loc in (self.state().get("Locations") or [])}

    def thumbnails(self) -> list[str]:
        """Every thumbnail this store offers, as the dropdown lists them."""
        return list(self.state().get("Thumbnails") or [])

    def scene_areas(self, location_id: int) -> dict[str, int]:
        """{Picario Name: ImageIndex} for the scene behind `location_id`.

        This is the lookup table the whole module turns on, and it comes from
        Picario by way of the admin — never from the workbook, and never from
        the name itself.
        """
        state = self.state()
        scene_id = next((loc.get("ImageName") for loc in (state.get("Locations") or [])
                         if int(loc["Id"]) == int(location_id)), None)
        if scene_id is None:
            raise library.StepError(
                f"no location {location_id} on this product", step="configurator")
        for scene in state.get("Scenes") or []:
            if scene.get("SceneId") == scene_id:
                return {str(a["Name"]): int(a["Id"])
                        for a in (scene.get("SceneAreas") or [])}
        raise library.StepError(
            f"the admin has no Picario scene loaded for {scene_id!r} — the "
            f"location exists but its scene did not come back",
            step="configurator")

    def areas(self, location_id: int) -> list[dict]:
        """The areas already saved against `location_id`."""
        return [a for a in (self.state().get("Areas") or [])
                if int(a.get("LocationId") or 0) == int(location_id)]

    def message(self) -> str:
        """Whatever the plugin is currently telling the user."""
        parts = []
        for key in ("error", "message"):
            try:
                text = (self.page.locator(self.SEL[key]).first.inner_text() or "").strip()
            except Exception:
                text = ""
            if text:
                parts.append(text)
        return " ".join(parts)

    # --- locations --------------------------------------------------------

    def ensure_location(self, name: str, reference_id: str, *,
                        spots: tuple[str, ...] = (),
                        alternatives: tuple[str, ...] = ()) -> tuple[int, str]:
        """(location id, the reference actually used) for this product's `name`
        location, creating it if absent.

        Idempotent, which is what lets a half-finished run be run again: a
        location already there is returned untouched rather than added twice.

        `spots` and `alternatives` are both optional and both off by default, so
        a caller passing neither behaves exactly as this always did — try the
        written reference, keep whatever comes back.

        WHEN THEY ARE GIVEN, THIS VERIFIES BEFORE IT SETTLES. A Picario scene
        only lists its spots once a location referencing it has been saved, so
        "is this the right scene?" cannot be asked any other way: the location is
        created, the scene read, and the spots the workbook named checked against
        it. If they are all there, done. If they are not, the next candidate
        from `alternatives` is tried the same way, and the location just created
        is removed first so nothing accumulates.

        A candidate is only ever accepted on evidence — it must contain EVERY
        spot the workbook named for this location. That is what makes trying a
        second spelling safe: the alternative scene is a different asset, not a
        renaming, and the only thing entitling us to prefer it is that it holds
        the spots the sheet asked for and the written one does not.

        Nothing that carries areas is ever removed. An empty location holds no
        configuration, so replacing it loses nothing; one with areas is somebody's
        work, and if it disagrees with the workbook this raises and says so.
        """
        candidates = [reference_id, *alternatives]
        attempts: list[tuple[str, list[str], list[str]]] = []

        # A location that is already here settles this before anything is
        # created. It counts as a match against ANY candidate, not just the
        # written one — otherwise a row that needed the alternative last time
        # would come back a conflict on every later run, because the product
        # would say "F WE.pfs" while the sheet still says "F WE .pfs". The run
        # that swapped is reported; it must not also be unrepeatable.
        settled = self._reuse_existing(name, candidates, spots)
        if settled is not None:
            return settled

        for index, candidate in enumerate(candidates):
            location_id = self.add_location(name, candidate)

            if not spots:
                return location_id, candidate

            offered = self.scene_areas(location_id)
            missing = [spot for spot in spots if spot not in offered]
            if not missing:
                if index:
                    # What the WRITTEN reference lacked, not this one's empty
                    # missing list — the first attempt is the one worth naming.
                    log.warning(
                        "location %r: the workbook's reference %r has no %s; "
                        "using %r, which does", name, reference_id,
                        attempts[0][2], candidate)
                return location_id, candidate

            attempts.append((candidate, sorted(offered), missing))

            # Wrong scene. Drop it only while it is still empty — and stop
            # entirely if it is not, because then it is not ours to remove.
            if self.areas(location_id):
                break
            if index < len(candidates) - 1:
                self.remove_location(location_id)

        detail = "; ".join(
            f"{ref!r} offers {offered} (missing {missing})"
            for ref, offered, missing in attempts)
        raise library.StepError(
            f"no Picario scene for location {name!r} contains the spot(s) the "
            f"workbook names — tried {detail}. Either the Picario RefrenceId or "
            f"the Picario Name column is wrong for this row",
            step="configurator")

    def _reuse_existing(self, name: str, candidates: list[str],
                        spots: tuple[str, ...]) -> tuple[int, str] | None:
        """Settle this location from what the product already has, or None if
        there is nothing usable and it should be created.

        Three outcomes, and the difference between them is whether anything
        would be LOST:

          reference matches a candidate, scene has the spots  -> reuse it
          empty location, wrong scene or missing spots        -> remove it and
                                                                 let the caller
                                                                 create; an empty
                                                                 location holds no
                                                                 configuration, so
                                                                 nothing is lost
          location with areas that disagrees with the sheet   -> raise. That is
                                                                 somebody's work,
                                                                 and re-pointing
                                                                 it would move
                                                                 every area on it
        """
        key = name.strip().casefold()
        location = next(
            (l for l in (self.state().get("Locations") or [])
             if str(l["LocationName"]).strip().casefold() == key), None)
        if location is None:
            return None

        location_id = int(location["Id"])
        current = str(location.get("ImageName") or "").strip()
        wanted = [c.strip() for c in candidates]
        has_areas = bool(self.areas(location_id))

        if current in wanted:
            if not spots:
                log.info("location %r already exists (%s)", name, location_id)
                return location_id, current
            offered = self.scene_areas(location_id)
            missing = [spot for spot in spots if spot not in offered]
            if not missing:
                log.info("location %r already exists (%s)", name, location_id)
                return location_id, current
            if has_areas:
                raise library.StepError(
                    f"location {name!r} already exists under {current!r}, whose "
                    f"Picario scene has no {missing} — it offers "
                    f"{sorted(offered)}. It already carries areas, so this run "
                    f"will not re-point it", step="configurator")
        elif has_areas:
            raise library.StepError(
                f"location {name!r} already exists on this product with "
                f"Picario reference {current!r}, but the workbook says "
                f"{candidates[0]!r} — remove the location in the admin if the "
                f"workbook is right", step="configurator")

        # Empty, and not usable as it stands. Debris from an attempt that died
        # after creating it; keeping it would block this row on every run.
        log.warning("replacing empty location %r (%s)", name, current)
        self.remove_location(location_id)
        return None

    def add_location(self, name: str, reference_id: str) -> int:
        """Create one location, and return the id the admin gave it.

        The Save button is not on the page until the Picario reference has been
        checked and accepted — the plugin posts the reference as it loses focus
        and only then renders the button. So waiting for that button IS waiting
        for validation, and its absence is a rejected reference rather than a
        slow page, which is why the timeout reads the plugin's own message.
        """
        pane = self.page.locator(self.SEL["container"])
        library.click(self.page, pane.locator(self.SEL["add_location"]),
                      timeout=self.timeout, settle=False)

        pane.locator(self.SEL["location_name"]).fill(name, timeout=self.timeout)
        reference = pane.locator(self.SEL["location_image"])
        reference.fill(reference_id, timeout=self.timeout)
        # The binding is `value:`, which fires on change — not per keystroke —
        # so the validation request does not start until the field is left.
        reference.blur()

        save = pane.locator(self.SEL["save_location"])
        try:
            save.wait_for(state="visible", timeout=self.timeout)
        except Exception:
            raise library.StepError(
                f"the admin would not accept Picario reference {reference_id!r} "
                f"for location {name!r}: {self.message() or 'no reason given'}",
                step="configurator") from None

        save.click(timeout=self.timeout)
        try:
            self.page.wait_for_function(
                "name => ko.mapping.toJS(productAreaViewModel).Locations"
                ".some(l => l.LocationName === name)",
                arg=name, timeout=self.timeout)
        except Exception:
            raise library.StepError(
                f"location {name!r} was submitted but never came back: "
                f"{self.message() or 'no reason given'}",
                step="configurator") from None

        location_id = self.locations()[name]
        log.info("added location %r -> %s (%s)", name, location_id, reference_id)
        self.select_location(location_id)
        return location_id

    def validate_reference(self, name: str, reference_id: str) -> None:
        """Ask the admin whether it would accept this Picario reference, and
        then throw the answer away. Raises if it would not.

        A real rehearsal rather than a weaker check with the write removed: it
        fills the same form a commit fills and lets the plugin post the
        reference to Picario exactly as it always does. The only thing it does
        differently is press Cancel instead of Save, so nothing is created.

        This is most of what a configurator preview can honestly offer. What it
        cannot check is the Picario Names, because the scene's list of areas
        only comes back attached to a SAVED location — there is no way to see
        which spots a scene has without first creating the location that names
        it.
        """
        pane = self.page.locator(self.SEL["container"])
        library.click(self.page, pane.locator(self.SEL["add_location"]),
                      timeout=self.timeout, settle=False)
        pane.locator(self.SEL["location_name"]).fill(name, timeout=self.timeout)
        reference = pane.locator(self.SEL["location_image"])
        reference.fill(reference_id, timeout=self.timeout)
        reference.blur()

        try:
            pane.locator(self.SEL["save_location"]).wait_for(
                state="visible", timeout=self.timeout)
        except Exception:
            raise library.StepError(
                f"the admin would not accept Picario reference {reference_id!r} "
                f"for location {name!r}: {self.message() or 'no reason given'}",
                step="configurator") from None
        finally:
            # Always, including on the failure path: an abandoned half-filled
            # form would still be open when the next location is rehearsed.
            try:
                pane.locator(self.SEL["cancel_location"]).click(timeout=self.timeout)
            except Exception:
                log.warning("could not cancel the add-location form")

    def select_location(self, location_id: int) -> "ConfiguratorPage":
        """Make `location_id` the location the area table is editing.

        Always done explicitly, never left to the save. The plugin sets its
        current location from a field its own SaveLocation response does not
        contain, so after a save the selection is whatever the <select> settled
        on — usually right, and not something to build on.
        """
        self.page.locator(self.SEL["container"]).locator(
            self.SEL["location_select"]).select_option(str(location_id),
                                                       timeout=self.timeout)
        # Read off the observable, NOT through ko.mapping.toJS: mapping returns
        # only the keys it mapped from the server's data, and the plugin adds
        # currentLocation (and saving, and message) by hand afterwards. They are
        # invisible to toJS, which is why state() cannot answer this.
        self.page.wait_for_function(
            "id => productAreaViewModel.currentLocation() == id",
            arg=str(location_id), timeout=self.timeout)
        return self

    def remove_location(self, location_id: int) -> None:
        """DELETE `location_id` and every area on it. Irreversible.

        NEVER CALLED BY A RUN, and that is deliberate: run_batch adds, it does
        not destroy, so the only way to lose a location is for somebody to ask
        for it explicitly. The plugin cannot re-point a location at a different
        Picario scene — it offers add and remove and nothing else — so this is
        what "the workbook disagrees with what is already there" costs, and it
        should cost a decision rather than happen inside a batch.

        Takes the id rather than acting on whatever is selected, because the
        button removes the CURRENT location and a stale selection would delete
        the wrong one. Waits for the location to actually be gone: the delete is
        a round trip, and returning early would let a caller re-add the same
        name into a page that has not caught up.
        """
        self.select_location(location_id)
        library.click(self.page, self.page.locator(self.SEL["container"]).locator(
            self.SEL["remove_location"]), timeout=self.timeout, settle=False)
        try:
            self.page.wait_for_function(
                "id => !ko.mapping.toJS(productAreaViewModel).Locations"
                ".some(l => l.Id == id)",
                arg=str(location_id), timeout=self.timeout)
        except Exception:
            raise library.StepError(
                f"location {location_id} was still on the product after the "
                f"remove: {self.message() or 'no reason given'}",
                step="configurator") from None
        log.warning("removed location %s and its areas", location_id)

    # --- areas ------------------------------------------------------------

    def ensure_areas(self, location_id: int,
                     wanted: list[tuple[str, str]]) -> dict:
        """Put `wanted` [(Picario Name, thumbnail path)] on `location_id`.

        Returns {"added": [...], "kept": [...]} — the same distinction the
        colour swatches make, and for the same reason: an area this run created
        and an area somebody configured by hand look identical afterwards and
        mean opposite things about who chose it.

        Idempotent by Picario Name. An area already sitting on that spot is left
        exactly as it is, including its thumbnail: the plugin allows only one
        area per spot, so "already configured" is the answer, not a conflict to
        resolve — and overwriting a thumbnail a human picked is not this
        automation's call.
        """
        self.select_location(location_id)
        offered = self.scene_areas(location_id)
        thumbnails = set(self.thumbnails())

        # Resolve EVERYTHING before touching the page. A row that names one area
        # the scene does not have should not leave three others half-added.
        resolved: list[tuple[str, int, str]] = []
        for picario_name, thumbnail in wanted:
            if picario_name not in offered:
                raise library.StepError(
                    f"Picario scene for this location has no area named "
                    f"{picario_name!r} — it offers {sorted(offered)}",
                    step="configurator")
            if thumbnail not in thumbnails:
                raise library.StepError(
                    f"this store has no thumbnail {thumbnail!r} — check the "
                    f"Thumbnail Name column against the admin's list",
                    step="configurator")
            resolved.append((picario_name, offered[picario_name], thumbnail))

        already = {int(a["ImageIndex"]) for a in self.areas(location_id)}
        added, kept = [], []

        for picario_name, image_index, thumbnail in resolved:
            if image_index in already:
                kept.append(picario_name)
                continue
            self._add_area_row(image_index, thumbnail)
            added.append(picario_name)

        if added:
            self.save_areas()
        return {"added": added, "kept": kept}

    def _add_area_row(self, image_index: int, thumbnail: str) -> None:
        """Append one area row and set both of its dropdowns.

        A fresh row defaults to the scene's first area — "Full Color" on every
        scene seen so far — so leaving either select alone is not "no choice",
        it is a wrong choice that saves cleanly.
        """
        pane = self.page.locator(self.SEL["container"])
        before = pane.locator(self.SEL["area_rows"]).count()
        pane.locator(self.SEL["add_area"]).click(timeout=self.timeout)
        # Scoped to the pane: `table.adminContent` is a house style on this
        # admin and other tabs use it too, so an unscoped count would watch the
        # wrong table and never move.
        self.page.wait_for_function(
            "args => document.querySelectorAll("
            "'#productAreasContainer ' + args.sel).length > args.before",
            arg={"sel": self.SEL["area_rows"], "before": before},
            timeout=self.timeout)

        row = pane.locator(self.SEL["area_rows"]).last
        row.locator(self.SEL["picario_select"]).select_option(
            str(image_index), timeout=self.timeout)
        row.locator(self.SEL["thumbnail_select"]).select_option(
            thumbnail, timeout=self.timeout)

    def save_areas(self) -> None:
        """Save every area on the product.

        One button for all of them: the plugin posts its whole viewmodel, so
        this writes the areas of every location, not only the selected one.
        """
        pane = self.page.locator(self.SEL["container"])
        pane.locator(self.SEL["save_areas"]).click(timeout=self.timeout)
        try:
            self.page.wait_for_function(
                "() => productAreaViewModel.saving() === false",
                timeout=self.timeout)
        except Exception:
            pass                            # the flag is cosmetic; the check below is not

        problem = (self.page.locator(self.SEL["error"]).first.inner_text() or "").strip()
        if problem:
            raise library.StepError(f"the admin refused the areas: {problem}",
                                    step="configurator")

        # Every area the admin accepted comes back carrying an Id. One still at
        # zero was posted and not stored, which is the failure this catches —
        # the plugin reports it in a banner that clears itself after 2 seconds.
        unsaved = [a for a in (self.state().get("Areas") or [])
                   if not int(a.get("Id") or 0)]
        if unsaved:
            raise library.StepError(
                f"{len(unsaved)} area(s) were not saved: "
                f"{self.message() or 'the admin gave no reason'}",
                step="configurator")
