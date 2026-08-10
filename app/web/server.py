"""The HTTP layer: upload a workbook, run it, watch it, get the report back.

    python -m app.web.server            # http://127.0.0.1:8000
    uvicorn app.web.server:api --reload

The API is deliberately thin. It parses, it starts a run on a worker thread, and
it hands back snapshots — every decision about what a run does lives in
app/automation, and every decision about what gets recorded lives in app/store.

SECURITY POSTURE, because this handles admin credentials and a service key:
  * the Supabase SERVICE key never leaves the backend — the front end talks to
    this API only, never to Supabase directly. A service key bypasses RLS, so
    putting it in a browser would hand the database to anyone who opens
    devtools.
  * the admin password is never serialised into a response. /api/health reports
    presence, never values.

There is no authentication yet. That is a deployment decision (put it behind
your existing auth, or add a session layer here) and it is called out in the
design doc rather than guessed at — but do not expose this to the internet as
it stands.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from main import ConfigError   # the name, not the module — main() below shadows it
from app.colour.store import MAP, normalise
from app.config import settings
from app.parser import parse_workbook
from app.store.client import healthy, store_available
from app.web.runs import MANAGER

log = logging.getLogger("web.server")

STATIC = Path(__file__).parent / "static"

api = FastAPI(title="nopCommerce product upload", version="1.0.0")

#: Origins allowed to call this API, comma-separated, or "*" for any.
#:
#: The shipped topology has NO cross-origin traffic: one container serves the
#: console and the API, so the browser only ever sees one origin and this
#: middleware never fires. It is env-driven rather than absent so that putting
#: a CDN or a separate front end in front later is a variable, not a patch —
#: and so the allowed origins are never a literal in this file.
#:
#: allow_credentials is deliberately NOT set: the admin password travels in the
#: request body (see StartRun), never in a cookie, so there is nothing for it
#: to enable — and "*" with credentials is rejected by browsers anyway.
CORS_ALLOW_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ALLOW_ORIGINS", "*").split(",")
    if origin.strip()
]

if CORS_ALLOW_ORIGINS:
    api.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ALLOW_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------

#: Served with no-cache and a mtime stamp in the URL. Both matter, and for
#: different failures: without the header a browser may not revalidate at all,
#: and without the stamp a cached copy under the SAME url can still win. Getting
#: this wrong is not subtle — new markup against an old stylesheet renders every
#: <svg> at its default 300x150 with a default black fill, which is what a page
#: full of black rectangles means.
NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


def _asset(name: str) -> str:
    """`/app.css?v=<mtime>` — a new URL the moment the file changes."""
    return f"/{name}?v={int((STATIC / name).stat().st_mtime)}"


@api.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    html = (html.replace('href="/app.css"', f'href="{_asset("app.css")}"')
                .replace('src="/app.js"', f'src="{_asset("app.js")}"'))
    return HTMLResponse(html, headers=NO_CACHE)


@api.get("/app.css")
def stylesheet() -> FileResponse:
    return FileResponse(STATIC / "app.css", media_type="text/css",
                        headers=NO_CACHE)


@api.get("/app.js")
def script() -> FileResponse:
    return FileResponse(STATIC / "app.js", media_type="application/javascript",
                        headers=NO_CACHE)


# ---------------------------------------------------------------------------
# health — what is actually wired up
# ---------------------------------------------------------------------------

@api.get("/api/ping")
def ping() -> dict:
    """Liveness only: is this process up and serving? No I/O, no dependencies.

    Separate from /api/health because health() calls store.healthy(), which
    makes a live Supabase round-trip. A platform healthcheck pointed at that
    would let a slow or unreachable telemetry backend fail a deploy — the same
    inversion this codebase refuses everywhere else: telemetry is never allowed
    to break the thing it observes.
    """
    return {"ok": True}


@api.get("/api/health")
def health() -> dict:
    """Presence, never values. The UI uses this to tell the user which tiers
    are live, so a missing key is visible before a run rather than after."""
    config = settings()
    store_ok, store_detail = healthy()
    return {
        "admin_configured": bool(config.admin_base_url and config.admin_email
                                 and config.admin_password),
        "admin_base_url": config.admin_base_url,
        "admin_email": config.admin_email,   # not a secret; the password never leaves
        "llm_enabled": config.llm_enabled,
        "llm_model": config.openai_model,
        "store_configured": store_available(),
        "store_healthy": store_ok,
        "store_detail": store_detail,
        "colour_map_entries": len(MAP.entries),
    }


# ---------------------------------------------------------------------------
# parse — upload, validate, preview. Nothing is created here.
# ---------------------------------------------------------------------------

@api.post("/api/parse")
async def parse(file: UploadFile = File(...)) -> JSONResponse:
    """Parse an uploaded workbook and return rows plus every problem found.

    Separate from starting a run on purpose: the user should see what the
    automation understood — and what it refused — before anything touches the
    catalogue.
    """
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Upload an .xlsx workbook")

    import io
    body = await file.read()
    result = parse_workbook(io.BytesIO(body), filename=file.filename)

    if not result.rows and result.errors:
        raise HTTPException(400, "; ".join(result.errors))

    # ParseResult.to_payload() is the one definition of this envelope. Building
    # it by hand here meant a field added to the parser silently never reached
    # the console — which is exactly how sheet_warnings went missing.
    return JSONResponse({**result.to_payload(), "filename": file.filename})


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------

class StartRun(BaseModel):
    """A run request. The three credential fields come from the form; each one
    falls back to its ADMIN_* counterpart independently, so a deployment can
    pin the URL in .env and still let a person supply their own sign-in."""

    rows: list[dict]
    commit: bool = False
    filename: str = ""
    base_url: str = ""
    email: str = ""
    password: str = ""


@api.post("/api/runs")
def start_run(payload: StartRun) -> dict:
    if not payload.rows:
        raise HTTPException(400, "No rows to run")

    config = settings()
    if not (payload.base_url or config.admin_base_url):
        raise HTTPException(400, "An admin URL is required")
    if not (payload.email or config.admin_email) or \
       not (payload.password or config.admin_password):
        raise HTTPException(400, "Admin credentials are not configured")

    # A URL typed into a form is user input: Credentials rejects anything that
    # is not http(s), and that refusal belongs to the caller as a 400, not to
    # the process as a 500.
    try:
        run_id = MANAGER.start(
            payload.rows, commit=payload.commit, filename=payload.filename,
            base_url=payload.base_url, email=payload.email,
            password=payload.password)
    except ConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"run_id": run_id}


@api.get("/api/runs")
def list_runs() -> list[dict]:
    return MANAGER.list()


@api.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    snapshot = MANAGER.snapshot(run_id)
    if snapshot is None:
        raise HTTPException(404, "No such run")
    return snapshot


class Rollback(BaseModel):
    """Approval for a deletion, echoing back exactly what was shown.

    `skus` is a check, not an instruction: the server recomputes the set from
    the store and refuses if the two disagree. Nothing a caller sends here can
    widen the scope — an unknown SKU makes the lists differ and stops the
    operation, it does not get deleted.
    """

    skus: list[str]
    base_url: str = ""
    email: str = ""
    password: str = ""


@api.get("/api/runs/{run_id}/rollback")
def rollback_plan(run_id: str) -> dict:
    """What deleting this run's products would delete. Changes nothing."""
    return MANAGER.rollback_plan(run_id)


@api.post("/api/runs/{run_id}/rollback")
def rollback_run(run_id: str, payload: Rollback) -> dict:
    """DELETE the products this run created. Irreversible.

    Synchronous on purpose. It is short, it must not be retried automatically,
    and the caller has to see the outcome — a fire-and-forget delete whose
    report you fetch later is the wrong shape for this.
    """
    try:
        return MANAGER.rollback(run_id, payload.skus, base_url=payload.base_url,
                                email=payload.email, password=payload.password)
    except ConfigError as exc:
        raise HTTPException(409, str(exc)) from exc


@api.get("/api/runs/{run_id}/report")
def get_report(run_id: str) -> FileResponse:
    path = MANAGER.report_path(run_id)
    if path is None:
        raise HTTPException(404, "No report for that run yet")
    return FileResponse(
        path, filename=f"report-{run_id[:8]}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------------------
# the colour review queue — where a proposed code becomes an authoritative one
# ---------------------------------------------------------------------------

@api.get("/api/colours")
def list_colours() -> dict:
    entries = [
        {"scope": e.scope, "colour": e.colour_raw or e.colour_key,
         "colour_key": e.colour_key, "code": e.code,
         "source": e.source, "verified": e.verified}
        for e in MAP.entries.values()
    ]
    entries.sort(key=lambda e: (e["verified"], e["scope"], e["colour"]))
    return {
        "entries": entries,
        "pending": sum(1 for e in entries if not e["verified"]),
    }


class ConfirmColour(BaseModel):
    scope: str
    colour: str
    code: str


@api.post("/api/colours/confirm")
def confirm_colour(payload: ConfirmColour) -> dict:
    """Promote a proposal to an authoritative entry, or correct it.

    This is the human half of tier 3. Verification proves an assignment is
    self-consistent with its payload — strong, but two colours with equally
    plausible codes could be swapped and still satisfy every count check. A
    person confirming closes that gap, which is why this exists and why the
    UI makes it a two-click job.
    """
    if not payload.colour.strip() or not payload.code.strip():
        raise HTTPException(400, "colour and code are both required")
    MAP.record(payload.scope or "global", payload.colour.strip(),
               payload.code.strip(), source="human", verified=True)
    return {"ok": True, "colour_key": normalise(payload.colour)}


@api.post("/api/colours/refresh")
def refresh_colours() -> dict:
    MAP.refresh()
    return {"entries": len(MAP.entries)}


# ---------------------------------------------------------------------------

def main() -> None:
    import uvicorn
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
        datefmt="%H:%M:%S")

    ok, detail = healthy()
    if not ok:
        log.warning("run store unavailable (%s) — runs will not be recorded", detail)
    if not settings().llm_enabled:
        log.warning("OPENAI_API_KEY not set — colour tier 3 is off")

    # Defaults are the local ones, so `python -m app.web.server` behaves
    # exactly as it always has. A container sets HOST=0.0.0.0 and the platform
    # injects PORT — the same command, one environment apart.
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    log.info("serving on http://%s:%d", host, port)
    uvicorn.run(api, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
