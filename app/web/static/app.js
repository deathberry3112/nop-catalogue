/* Product upload console — no framework, no build step.
   The whole app is: parse a workbook, show what we understood, run it, watch.  */

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const state = {
  parsed: null,       // the last parse result
  runId: null,        // the run currently on screen
  poll: null,         // its polling timer
  health: {},
  durations: [],      // seconds per finished product — feeds the ETA
  openRows: new Set(),// activity dropdowns the user has opened, by row index
  logSig: null,       // what the log was last built from
  selected: new Set(),// preview rows to actually run, by index into parsed.rows
};

/* ── helpers ─────────────────────────────────────────────────────────── */

const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

function fmtDuration(seconds) {
  if (!isFinite(seconds) || seconds < 0) return '';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  return m < 60 ? `${m}m ${Math.round(seconds % 60)}s`
                : `${Math.floor(m / 60)}h ${m % 60}m`;
}

const STATUS_PILL = {
  added: 'ok', resumed: 'ok', succeeded: 'ok', previewed: 'ok',
  failed: 'bad', blocked: 'bad',
  skipped: '', pending: '', queued: '',
  running: 'running', partial: 'warn', cancelled: '',
};
const pill = (s) => `<span class="pill ${STATUS_PILL[s] ?? ''}">${esc(s)}</span>`;

/* ── views ───────────────────────────────────────────────────────────── */

function show(view) {
  $$('.tab').forEach((t) => t.classList.toggle('is-active', t.dataset.view === view));
  $$('.view').forEach((v) => v.classList.toggle('is-active', v.id === `view-${view}`));
  if (view === 'runs')    refreshRunList();
  if (view === 'colours') loadColours();
}
$$('.tab').forEach((t) => t.addEventListener('click', () => show(t.dataset.view)));

/* ── theme ───────────────────────────────────────────────────────────── */
/* Three states, not two: "system" has to stay reachable, because following the
   OS is the sensible default and a binary switch has no way back to it. The
   <head> applies the stored choice before first paint. */

const THEME_KEY = 'nop.theme';
const THEMES = ['system', 'light', 'dark'];

function applyTheme(mode) {
  if (mode === 'system') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', mode);
  $$('#theme .seg-btn').forEach((b) =>
    b.setAttribute('aria-checked', String(b.dataset.themeSet === mode)));
  try { localStorage.setItem(THEME_KEY, mode); } catch { /* private mode */ }
}

function storedTheme() {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch { /* private mode */ }
  return THEMES.includes(saved) ? saved : 'system';
}

$$('#theme .seg-btn').forEach((b) =>
  b.addEventListener('click', () => applyTheme(b.dataset.themeSet)));
applyTheme(storedTheme());

/* ── admin connection ────────────────────────────────────────────────── */
/* The URL and the email are conveniences worth remembering between visits.
   The password is not: it is only ever held in the field the user typed it
   into, sent over the run request, and dropped when the tab closes. */

const CONN_KEY = 'nop.admin';

function readCreds() {
  return {
    base_url: $('#a-url').value.trim(),
    email: $('#a-email').value.trim(),
    password: $('#a-pass').value,
  };
}

/* Blank fields fall back to .env server-side, per field — so a run is ready
   when the form is complete, or when .env is. */
function credsReady() {
  const c = readCreds();
  return (!!c.base_url && !!c.email && !!c.password) || state.health.admin_configured === true;
}

const targetUrl = () =>
  readCreds().base_url || state.health.admin_base_url || 'the configured admin';

['#a-url', '#a-email', '#a-pass'].forEach((sel) =>
  $(sel).addEventListener('input', () => {
    try {
      localStorage.setItem(CONN_KEY, JSON.stringify({
        base_url: $('#a-url').value, email: $('#a-email').value,
      }));
    } catch { /* private mode — this run still works, it just is not remembered */ }
    syncActions();
  }));

/* The icon swap is pure CSS, keyed off the input's type — this only flips the
   type and keeps the assistive label honest. */
$('#a-pass-toggle').addEventListener('click', (e) => {
  e.preventDefault();                      // it lives inside a <label>
  const input = $('#a-pass');
  const reveal = input.type === 'password';
  input.type = reveal ? 'text' : 'password';
  const label = reveal ? 'Hide password' : 'Show password';
  e.currentTarget.setAttribute('aria-label', label);
  e.currentTarget.title = label;
});

function restoreCreds() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(CONN_KEY) || 'null'); } catch { /* ignore */ }
  if (!saved) return;
  if (saved.base_url) $('#a-url').value = saved.base_url;
  if (saved.email) $('#a-email').value = saved.email;
}

/* ── health ──────────────────────────────────────────────────────────── */
/* Presence only — the API never sends a secret back, so this can tell the user
   what a blank field would fall back to without knowing what it is. */

async function loadHealth() {
  try {
    state.health = await api('/api/health');
    // Show what .env would use, so an empty form is not a mystery. Anything
    // already typed or remembered wins.
    if (!$('#a-url').value && state.health.admin_base_url) {
      $('#a-url').value = state.health.admin_base_url;
    }
    if (!$('#a-email').value && state.health.admin_email) {
      $('#a-email').value = state.health.admin_email;
    }
  } catch {
    state.health = { unreachable: true };
  }
  syncActions();
}

/* ── upload + parse ──────────────────────────────────────────────────── */

const drop = $('#drop');
$('#browse').addEventListener('click', (e) => { e.stopPropagation(); $('#file').click(); });
drop.addEventListener('click', () => $('#file').click());
drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('hot'); });
drop.addEventListener('dragleave', () => drop.classList.remove('hot'));
drop.addEventListener('drop', (e) => {
  e.preventDefault(); drop.classList.remove('hot');
  if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]);
});
$('#file').addEventListener('change', (e) => e.target.files[0] && upload(e.target.files[0]));

async function upload(file) {
  $('#parse-error').hidden = true;
  $('#preview').hidden = true;
  const body = new FormData();
  body.append('file', file);
  try {
    state.parsed = await api('/api/parse', { method: 'POST', body });
    renderPreview();
  } catch (err) {
    $('#parse-error').textContent = `Could not read that workbook — ${err.message}`;
    $('#parse-error').hidden = false;
  }
}

/* Must match app.parser.DUPLICATE_WARNING — the console keys its "remove them"
   control off this prefix, so the two sides agree on one string. */
const DUP_PREFIX = 'duplicated:';

const hasDuplicates = (r) =>
  !!(r.duplicates && ((r.duplicates.colors || []).length
                      || (r.duplicates.combinations || []).length));

/* The human half of the duplicate check. Nothing is removed until someone
   clicks this — see the note in app.parser._validate for why that has to be a
   decision rather than a silent tidy-up. */
function removeDuplicates(i) {
  const r = state.parsed.rows[i];
  const unique = (xs) => [...new Set(xs || [])];   // insertion order is kept
  r.colors = unique(r.colors);
  r.combinations = unique(r.combinations);
  r.duplicates = {};
  r.warnings = (r.warnings || []).filter((w) => !w.startsWith(DUP_PREFIX));
  // If that was the row's only problem it is now a clean row, so it earns the
  // same default a clean row gets.
  if (!r.errors?.length && !r.warnings.length) state.selected.add(i);
  renderPreview(true);
}

function renderPreview(keepSelection = false) {
  const p = state.parsed;
  const blocked = p.total_rows - p.valid_rows;

  $('#p-filename').textContent = p.filename;
  $('#p-sheets').textContent = `${p.sheets.length} sheet(s): ${p.sheets.join(', ')}`;
  $('#p-total').textContent = p.total_rows;
  $('#p-valid').textContent = p.valid_rows;
  $('#p-blocked').textContent = blocked;
  $('#p-blocked-wrap').hidden = blocked === 0;

  // Warned rows run and commit like any other. Counting them here is the
  // difference between an informed commit and a surprised one.
  const warned = p.rows.filter((r) => r.warnings?.length).length;
  $('#p-warned').textContent = warned;
  $('#p-warned-wrap').hidden = warned === 0;

  // Sheet-level problems are different from row-level ones: a sheet we could
  // not read at all is not a row that failed validation.
  if (p.sheet_errors.length) {
    $('#p-issues').innerHTML =
      `<strong>Some sheets could not be read</strong><ul>${
        p.sheet_errors.map((e) => `<li>${esc(e)}</li>`).join('')}</ul>`;
    $('#p-issues').hidden = false;
  } else $('#p-issues').hidden = true;

  // A column is wrong, not a row. Said once, at the top, in the words that
  // appear in their spreadsheet.
  const template = p.sheet_warnings || [];
  if (template.length) {
    $('#p-template').innerHTML =
      `<strong>Check the sheet's columns</strong><ul>${
        template.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>`;
    $('#p-template').hidden = false;
  } else $('#p-template').hidden = true;

  // Clean rows start selected; anything with a warning or an error starts
  // unselected. The default is the recommendation — a row you have not looked
  // at yet should not be on its way into the catalogue — and unticking is
  // always cheaper to undo than a product that should not exist.
  if (!keepSelection) {
    state.selected = new Set(
      p.rows.map((r, i) => i)
            .filter((i) => !p.rows[i].errors?.length && !p.rows[i].warnings?.length));
  }

  $('#p-table tbody').innerHTML = p.rows.map((r, i) => {
    const problems = [...(r.errors || []), ...(r.warnings || [])];
    return `<tr class="${r.errors?.length ? 'bad' : ''}">
      <td class="pick"><input type="checkbox" data-i="${i}"
            ${state.selected.has(i) ? 'checked' : ''}
            aria-label="Include ${esc(r.sku)}"></td>
      <td class="muted">${i + 1}</td>
      <td class="sku">${esc(r.sku)}</td>
      <td>${esc(r.product_name)}</td>
      <td class="muted">${esc(r.source_sheet || '')}</td>
      <td class="muted">${esc(r.spec_value || '—')}</td>
      <td class="num">${r.colors?.length || 0}</td>
      <td class="num">${r.sizes?.length || 0}</td>
      <td class="num">${r.combinations?.length || 0}</td>
      <td>${problems.length
            ? problems.map((e) => `<div class="${r.errors?.includes(e)
                ? 'pill bad' : 'pill warn'}">${esc(e)}</div>`).join(' ')
            : '<span class="pill ok">ok</span>'}${
          hasDuplicates(r)
            ? `<button class="btn sm dedupe" data-i="${i}">Remove duplicates</button>`
            : ''}</td>
    </tr>`;
  }).join('');

  syncActions();
  $('#preview').hidden = false;
}

/* One listener on the body, not one per row: a 1000-row workbook would
   otherwise attach 1000 of them on every re-render. */
$('#p-table tbody').addEventListener('change', (e) => {
  const box = e.target.closest('input[type=checkbox]');
  if (!box) return;
  const i = Number(box.dataset.i);
  if (box.checked) state.selected.add(i); else state.selected.delete(i);
  syncActions();
});

$('#p-table tbody').addEventListener('click', (e) => {
  const button = e.target.closest('button.dedupe');
  if (button) removeDuplicates(Number(button.dataset.i));
});

$('#p-all').addEventListener('change', (e) => {
  const all = e.target.checked;
  state.selected = new Set(all ? state.parsed.rows.map((_, i) => i) : []);
  $$('#p-table tbody input[type=checkbox]').forEach((b) => { b.checked = all; });
  syncActions();
});

const selectedRows = () =>
  [...state.selected].sort((a, b) => a - b)
    .map((i) => state.parsed.rows[i]).filter(Boolean);

/* What can be run right now, and why not. Three halves, since selection: a
   batch needs an admin to sign in to, at least one row chosen, and — to
   commit — nothing chosen that carries an error. The server refuses all three
   too; saying so here just beats a rejection after the click.

   Note what selection changes: a workbook with one bad row used to be
   uncommittable outright. Now that only the chosen rows are sent, unticking it
   is a real fix rather than a way to hide the problem. */
function syncActions() {
  const p = state.parsed;
  if (!p) return;
  const chosen = selectedRows();
  const chosenBad = chosen.filter((r) => r.errors?.length).length;
  const ready = credsReady();
  const noCreds = 'Enter the admin URL, email and password first';

  $('#btn-preview').disabled = chosen.length === 0 || !ready;
  $('#btn-preview').title = ready ? '' : noCreds;

  $('#btn-commit').disabled = chosen.length === 0 || chosenBad > 0 || !ready;
  $('#btn-commit').title = !ready ? noCreds
    : chosenBad > 0 ? `${chosenBad} selected row(s) have problems — untick them, `
                      + 'or fix the workbook'
    : chosen.length === 0 ? 'Tick at least one row'
    : '';

  const all = $('#p-all');
  all.checked = chosen.length === p.rows.length && chosen.length > 0;
  all.indeterminate = chosen.length > 0 && chosen.length < p.rows.length;

  const skipped = p.rows.length - chosen.length;
  $('#p-selection').innerHTML =
    `<strong>${chosen.length}</strong> of ${p.rows.length} row(s) selected`
    + (skipped ? ` — ${skipped} will not be touched` : '')
    + (chosenBad ? ` · <span class="bad-text">${chosenBad} selected row(s) `
                   + 'have problems and block committing</span>' : '');
}

$('#btn-reset').addEventListener('click', () => {
  state.parsed = null;
  $('#preview').hidden = true;
  $('#file').value = '';
});

/* ── starting a run ──────────────────────────────────────────────────── */

$('#btn-preview').addEventListener('click', () => startRun(false));
$('#btn-commit').addEventListener('click', () => {
  $('#m-count').textContent = selectedRows().length;
  $('#m-target').textContent = targetUrl();

  // Last chance to see them: a warning does not stop the commit, so this is
  // the point where it has to be in front of the person clicking. Every
  // warning, not the first per row — the row that matters here had two, and
  // the one worth reading was the second.
  const warned = selectedRows().filter((r) => r.warnings?.length);
  const notes = warned.flatMap((r) => r.warnings.map((w) => [r.sku, w]));
  $('#m-warn').hidden = notes.length === 0;
  if (notes.length) {
    $('#m-warn').innerHTML =
      `<strong>${warned.length} row(s) have warnings</strong> — they will be
       created as listed.<ul>${notes.slice(0, 6).map(([sku, w]) =>
        `<li><span class="mono">${esc(sku)}</span> — ${esc(w)}</li>`).join('')}${
        notes.length > 6 ? `<li>+${notes.length - 6} more</li>` : ''}</ul>`;
  }
  $('#modal').hidden = false;
});
$('#m-cancel').addEventListener('click', () => { $('#modal').hidden = true; });
$('#m-confirm').addEventListener('click', () => { $('#modal').hidden = true; startRun(true); });

async function startRun(commit) {
  try {
    const { run_id } = await api('/api/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        rows: selectedRows(), commit, filename: state.parsed.filename,
        ...readCreds(),
      }),
    });
    show('runs');
    openRun(run_id);
  } catch (err) {
    alert(`Could not start the run — ${err.message}`);
  }
}

/* ── run list ────────────────────────────────────────────────────────── */

async function refreshRunList() {
  let runs = [];
  try { runs = await api('/api/runs'); } catch { /* shown as empty */ }

  const live = runs.filter((r) => r.status === 'running').length;
  $('#runs-badge').hidden = live === 0;
  $('#runs-badge').textContent = live;

  $('#runs-empty').hidden = runs.length > 0;
  $('#run-list').innerHTML = runs.map((r) => {
    const when = r.started_at ? new Date(r.started_at).toLocaleString() : '';
    return `<div class="run-card" data-id="${r.id}">
      <div class="grow">
        <div class="rc-title">${esc(r.source_filename || 'Run')} ${pill(r.status)}</div>
        <div class="rc-sub">${when} · ${r.completed}/${r.total_rows} rows
          ${r.commit ? '· committed' : '· preview only'}</div>
      </div>
      <span class="muted mono">${r.id.slice(0, 8)}</span>
    </div>`;
  }).join('');

  $$('#run-list .run-card').forEach((c) =>
    c.addEventListener('click', () => openRun(c.dataset.id)));
}

/* ── run detail ──────────────────────────────────────────────────────── */

$('#btn-back').addEventListener('click', () => {
  clearInterval(state.poll);
  state.runId = null;
  $('#run-detail').hidden = true;
  $('#run-list-wrap').hidden = false;
  refreshRunList();
});

function openRun(runId) {
  state.runId = runId;
  state.durations = [];
  state.openRows.clear();       // dropdowns are per run, not per session
  state.logSig = null;
  $('#run-list-wrap').hidden = true;
  $('#run-detail').hidden = false;
  clearInterval(state.poll);
  pollRun();
  state.poll = setInterval(pollRun, 1500);
}

async function pollRun() {
  if (!state.runId) return;
  let run;
  try { run = await api(`/api/runs/${state.runId}`); }
  catch { return; }

  renderRun(run);
  if (run.status !== 'running') {
    clearInterval(state.poll);
    state.poll = null;
  }
}

function renderRun(run) {
  $('#r-title').textContent = run.source_filename || 'Run';
  $('#r-id').textContent = run.id;
  $('#r-status').className = `pill ${STATUS_PILL[run.status] ?? ''}`;
  $('#r-status').textContent = run.status;
  $('#r-mode').className = `pill ${run.commit ? 'warn' : ''}`;
  $('#r-mode').textContent = run.commit ? 'committed' : 'preview — nothing saved';

  const pct = run.total_rows ? (run.completed / run.total_rows) * 100 : 0;
  $('#r-fill').style.width = `${pct}%`;
  $('#r-progress').textContent = `${run.completed} / ${run.total_rows} products`;

  // ETA from THIS run's finished rows, not a global average: duration is
  // dominated by combination count, which varies hugely between products.
  const finished = run.rows.filter((r) => r.duration_ms || r.finished_at);
  const times = run.rows
    .filter((r) => r.started_at && r.finished_at)
    .map((r) => (new Date(r.finished_at) - new Date(r.started_at)) / 1000)
    .filter((s) => s > 0);
  if (run.status === 'running' && times.length >= 2) {
    const median = times.slice().sort((a, b) => a - b)[Math.floor(times.length / 2)];
    const left = (run.total_rows - run.completed) * median;
    $('#r-eta').textContent = `~${fmtDuration(left)} remaining`;
  } else if (run.finished_at && run.started_at) {
    $('#r-eta').textContent =
      `took ${fmtDuration((new Date(run.finished_at) - new Date(run.started_at)) / 1000)}`;
  } else $('#r-eta').textContent = '';

  // counts
  const c = run.counts || {};
  const tiers = run.colour_tiers || {};
  const cells = run.commit
    ? [['added',     c.added     || 0, 'ok'],
       ['resumed',   c.resumed   || 0, 'ok'],
       ['skipped',   c.skipped   || 0, ''],
       ['failed',    c.failed    || 0, 'bad']]
    : [['checked',   c.previewed || 0, 'ok'],
       ['would fail', c.failed   || 0, 'bad']];
  if (Object.keys(tiers).length) {
    for (const [tier, n] of Object.entries(tiers)) {
      cells.push([`colour: ${tier}`, n, tier === 'llm' ? 'warn' : '']);
    }
  }
  if (run.llm_cost_usd) cells.push(['AI cost', `$${run.llm_cost_usd.toFixed(4)}`, 'warn']);
  $('#r-stats').innerHTML = cells.map(([label, n, cls]) =>
    `<div class="stat ${cls}"><span class="stat-n">${esc(n)}</span>
       <span class="stat-l">${esc(label)}</span></div>`).join('');

  $('#btn-report').hidden = run.status === 'running';
  $('#btn-report').href = `/api/runs/${run.id}/report`;

  renderFailures(run);
  renderRows(run);
  renderLog(run);
}

/* Failures grouped by CAUSE. A 200-product run with 40 failures is usually two
   problems; a flat list of 40 rows hides that completely. */
function renderFailures(run) {
  const failed = run.rows.filter((r) => r.status === 'failed');
  $('#r-failures').hidden = failed.length === 0;
  if (!failed.length) return;

  const groups = {};
  for (const row of failed) {
    const key = row.error_class || 'unknown';
    (groups[key] ||= []).push(row);
  }

  const BLAME = {
    transient_dom: 'usually resolves on retry',
    transient_net: 'infrastructure',
    auth: 'session',
    selector_miss: 'the admin changed — needs a code fix',
    data_colour: 'the workbook or the colour map',
    data_invalid: 'the workbook',
    nop_rejected: 'the admin refused the value',
    conflict: 'another run owns this SKU',
    unknown: 'needs triage',
  };

  $('#r-failure-groups').innerHTML = Object.entries(groups)
    .sort((a, b) => b[1].length - a[1].length)
    .map(([cls, rows]) => `
      <details class="fgroup">
        <summary>${esc(cls)} <span class="pill bad">${rows.length}</span>
          <span class="blame">${esc(BLAME[cls] || '')}</span></summary>
        <div class="fbody">
          ${rows.map((r) => `
            <div>
              <strong class="mono">${esc(r.sku)}</strong>
              <div class="fmsg">${esc(r.error || '')}</div>
            </div>`).join('')}
        </div>
      </details>`).join('');
}

function renderRows(run) {
  $('#r-table tbody').innerHTML = run.rows.map((r) => {
    const steps = r.steps || [];
    const failedSteps = steps.filter((s) => s.status === 'failed').length;
    const tiers = Object.entries(r.colour_tiers || {})
      .map(([t, n]) => `<span class="pill ${t === 'llm' ? 'warn' : ''}">${esc(t)} ${n}</span>`)
      .join(' ');
    const link = r.product_url
      ? `<a href="${esc(r.product_url)}" target="_blank" rel="noopener">open</a>` : '';
    return `<tr class="${r.status === 'failed' ? 'bad' : ''}">
      <td class="sku">${esc(r.sku)}</td>
      <td>${pill(r.status)}</td>
      <td>${tiers || '<span class="muted">—</span>'}</td>
      <td class="muted">${steps.length}${failedSteps ? ` (${failedSteps} failed)` : ''}</td>
      <td>${r.error ? `<span class="muted">${esc(r.error.slice(0, 120))}</span>` : link}</td>
    </tr>`;
  }).join('');
}

/* ── activity ────────────────────────────────────────────────────────── */
/* The log has two levels, because it describes two things. The stream is the
   run's narrative — signed in, [2/7] CP-V-200, report written. Each product
   line then expands into what was actually done to that product in the admin:
   the steps, in order, with what each one cost and any retry it took.

   Row-scoped events move INSIDE their product rather than being repeated in
   the stream, so "retrying values_colour after transient_dom" appears next to
   the step it is about instead of forty lines away from it. */

const STEP_LABEL = {
  lookup: 'Searched the catalogue for this SKU',
  create: 'Opened the new-product form',
  fill_form: 'Filled the create form (not saved)',
  save: 'Filled the create form and saved the product',
  resolve_colours: 'Resolved the colour codes',
  specification: 'Added the specification attribute',
  attribute_colour: 'Added the colour attribute',
  values_colour: 'Added every colour value',
  attribute_size: 'Added the size attribute',
  values_size: 'Added every size value',
  generate_combinations: 'Generated the attribute combinations',
  combination_skus: 'Wrote the child SKU onto each combination',
};

const fmtMs = (ms) =>
  ms == null ? '' : ms < 1000 ? `${ms}ms` : fmtDuration(ms / 1000);

const clock = (at) => (at ? new Date(at).toLocaleTimeString() : '');

function stepEntry(step) {
  const failed = step.status === 'failed';
  const label = STEP_LABEL[step.name] || step.name;
  const retry = step.attempt > 1 ? `<span class="try">attempt ${step.attempt}</span>` : '';
  return `<div class="astep ${failed ? 'bad' : 'ok'}">
    <span class="t">${esc(clock(step.started_at))}</span>
    <span class="m">${esc(label)}${retry}</span>
    <span class="d">${esc(fmtMs(step.duration_ms))}</span>
  </div>${failed && step.error
    ? `<div class="fmsg">${esc(step.error_class || 'failed')} — ${esc(step.error)}</div>`
    : ''}`;
}

function eventEntry(e) {
  return `<div class="astep note ${esc(e.level)}">
    <span class="t">${esc(clock(e.at))}</span>
    <span class="m">${esc(e.message)}</span>
  </div>`;
}

function renderLog(run) {
  const log = $('#r-log');
  const events = run.events || [];

  // A poll every 1.5s that rewrote this would snap open dropdowns shut under
  // the reader's cursor. Only rebuild when something actually changed.
  const sig = `${events.length}|${run.rows
    .map((r) => `${r.status}:${(r.steps || []).length}`).join(',')}`;
  if (sig === state.logSig) return;
  state.logSig = sig;

  const rowByIndex = new Map(run.rows.map((r) => [r.row_index, r]));
  const indexByRowId = new Map(run.rows.map((r) => [r.id, r.row_index]));

  const nested = new Map();          // row_index -> that product's events
  const stream = [];                 // everything else, in order
  for (const e of events) {
    if (e.data && e.data.kind === 'product') { stream.push(e); continue; }
    const index = indexByRowId.get(e.row_id);
    if (index === undefined) { stream.push(e); continue; }
    if (!nested.has(index)) nested.set(index, []);
    nested.get(index).push(e);
  }

  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  const scrolled = log.scrollTop;

  log.innerHTML = stream.map((e) => {
    if (!(e.data && e.data.kind === 'product')) {
      return `<div class="log-line ${esc(e.level)}">
        <span class="t">${esc(clock(e.at))}</span>
        <span class="m">${esc(e.message)}</span></div>`;
    }

    const index = e.data.index;
    const row = rowByIndex.get(index);
    const entries = [
      ...((row && row.steps) || []).map((s) => ({
        at: s.started_at, failed: s.status === 'failed', html: stepEntry(s) })),
      ...(nested.get(index) || []).map((n) => ({
        at: n.at, failed: n.level === 'error', html: eventEntry(n) })),
    ].sort((a, b) => String(a.at).localeCompare(String(b.at)));

    // Nothing recorded yet — a bare line beats a dropdown that opens on air.
    if (!entries.length) {
      return `<div class="log-line ${esc(e.level)}">
        <span class="t">${esc(clock(e.at))}</span>
        <span class="m">${esc(e.message)}</span></div>`;
    }

    const failed = entries.filter((x) => x.failed).length;
    return `<details class="alog" data-idx="${esc(index)}"
              ${state.openRows.has(String(index)) ? 'open' : ''}>
      <summary>
        <span class="t">${esc(clock(e.at))}</span>
        <span class="m">${esc(e.message)}</span>
        ${row ? pill(row.status) : ''}
        <span class="acount">${entries.length} action${entries.length === 1 ? '' : 's'}${
          failed ? `, ${failed} failed` : ''}</span>
      </summary>
      <div class="abody">${entries.map((x) => x.html).join('')}</div>
    </details>`;
  }).join('');

  $$('#r-log details.alog').forEach((d) => d.addEventListener('toggle', () => {
    if (d.open) state.openRows.add(d.dataset.idx);
    else state.openRows.delete(d.dataset.idx);
  }));

  // Follow a live run, but never yank a reader who has scrolled up to expand
  // something — restoring the offset is the whole point of the two branches.
  log.scrollTop = atBottom ? log.scrollHeight : scrolled;
}

/* ── colour map ──────────────────────────────────────────────────────── */

async function loadColours() {
  let data;
  try { data = await api('/api/colours'); } catch { return; }

  $('#colour-badge').hidden = data.pending === 0;
  $('#colour-badge').textContent = data.pending;

  $('#c-table tbody').innerHTML = data.entries.map((e) => `
    <tr>
      <td>${esc(e.scope)}</td>
      <td>${esc(e.colour)}</td>
      <td class="sku">${esc(e.code)}</td>
      <td><span class="pill ${e.source === 'llm' ? 'warn' : ''}">${esc(e.source)}</span></td>
      <td>${e.verified ? '<span class="pill ok">confirmed</span>'
                       : '<span class="pill warn">unconfirmed</span>'}</td>
      <td>${e.verified ? '' :
        `<button class="btn sm" data-scope="${esc(e.scope)}"
                 data-colour="${esc(e.colour)}" data-code="${esc(e.code)}">Confirm</button>`}</td>
    </tr>`).join('');

  $$('#c-table button').forEach((b) => b.addEventListener('click', () =>
    confirmColour(b.dataset.scope, b.dataset.colour, b.dataset.code)));
}

async function confirmColour(scope, colour, code) {
  try {
    await api('/api/colours/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope, colour, code }),
    });
    loadColours();
    loadHealth();
  } catch (err) { alert(`Could not save — ${err.message}`); }
}

$('#c-add').addEventListener('click', () => {
  const scope = $('#c-scope').value.trim() || 'global';
  const colour = $('#c-colour').value.trim();
  const code = $('#c-code').value.trim();
  if (!colour || !code) return alert('Colour and code are both required.');
  confirmColour(scope, colour, code);
  $('#c-colour').value = ''; $('#c-code').value = '';
});

/* ── boot ────────────────────────────────────────────────────────────── */

restoreCreds();
loadHealth();
refreshRunList();
loadColours();
setInterval(() => { if (!state.runId) refreshRunList(); }, 5000);
