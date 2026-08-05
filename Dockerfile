# Playwright's own image: Chromium and the ~60 system libraries it needs are
# already here, pretested against this exact browser build. The alternative —
# `playwright install --with-deps chromium` on a generic base — is an apt
# transaction plus a 150 MB download on every build, and every one of those
# libraries is a new way for a deploy to fail.
#
# THE TAG MUST TRACK `playwright==` IN requirements.txt. Playwright locates its
# browsers by version; a mismatch does not fail here, it fails on the first run
# with "Executable doesn't exist at /ms-playwright/chromium-XXXX/...".
FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

# /app specifically, not an arbitrary directory: app/automation/runner.py
# resolves its ledger as the RELATIVE path var/created-products.json, so it
# only lands next to the ROOT/var that everything else uses when the working
# directory is the repo root.
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0

# Dependencies first, so editing application code doesn't reinstall them.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Prove Chromium actually launches, at BUILD time, with the same arguments the
# application uses at run time. ensure_dependencies() confirms the binary is
# resolvable; the launch that follows confirms its system libraries are present
# — which is the half a path check cannot tell you. Without this, a missing
# library first surfaces on someone's upload; with it, the build fails instead.
RUN python -c "import main; \
main.ensure_dependencies(); \
from playwright.sync_api import sync_playwright; \
pw = sync_playwright().start(); \
browser = pw.chromium.launch(**main.build_launch_kwargs(main.RunOptions())); \
page = browser.new_page(); \
page.set_content('<h1 id=probe>chromium ok</h1>'); \
assert page.text_content('#probe') == 'chromium ok'; \
print('playwright build check passed:', browser.version); \
browser.close(); \
pw.stop()"

# The same entry point in every environment. Railway injects PORT and HOST is
# set above; locally this command still defaults to 127.0.0.1:8000.
CMD ["python", "-m", "app.web.server"]
