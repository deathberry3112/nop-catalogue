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

/* The colour map is legacy — see the note in its section and the comment on its
   tab. It stays reachable at #colours for the case it still serves (an old-shape
   payload posted straight to /api/runs, where tier 1 does read the map), without
   putting a tab in front of everyone else that leads nowhere. */
if (location.hash === '#colours') {
  $('#tab-colours').hidden = false;
  show('colours');
}

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

/* ── rollback ────────────────────────────────────────────────────────────
   Deleting products from a live catalogue. The dialog names every product
   before anything happens and lets the operator pick which of them go — "the
   run half-worked" is the usual reason anyone opens this, and deleting all
   twelve is rarely the right answer to three of them breaking.

   The POST carries the SELECTED SKUs. The server recomputes the plan and lets
   the selection only NARROW it, so nothing clicked here can widen the scope,
   and a dialog left open while someone else deletes is refused outright rather
   than half-applied. */

let rbPlan = null;
let rbChosen = new Set();

/* The split the dialog is built around: a product the run created but never
   finished configuring is the one people came here to delete. Deleting one
   that worked is a bigger claim, so it lives in its own group and is never
   preselected. */
const rbUnfinished = (p) => p.row_status === 'failed';

$('#btn-rollback').addEventListener('click', async () => {
  rbPlan = null;
  rbChosen = new Set();
  $('#rb-confirm').value = '';
  $('#rb-blocked').hidden = true;
  $('#rb-excluded').hidden = true;
  $('#rb-picker').hidden = true;
  $('#rb-groups').innerHTML =
    '<div class="rb-list"><div class="rb-row muted">Working out what this run created…</div></div>';
  $('#rb-modal').hidden = false;
  syncRbArm();

  let plan;
  try {
    plan = await api(`/api/runs/${state.runId}/rollback`);
  } catch (e) {
    $('#rb-groups').innerHTML = '';
    $('#rb-blocked').textContent = `Could not work out what to delete: ${e.message}`;
    $('#rb-blocked').hidden = false;
    return;
  }

  $('#rb-target').textContent = plan.base_url || 'the configured admin';

  /* `excluded` is rendered for blocked plans too — it is the reason, and
     hiding it is how "this run created no products" reads as a lie to someone
     who just watched it create one. After a partial rollback it is also where
     "where did the other nine go?" gets answered. */
  if (plan.excluded.length) {
    $('#rb-excluded').innerHTML =
      `<strong>Left alone</strong><ul>${
        plan.excluded.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>`;
    $('#rb-excluded').hidden = false;
  }

  if (plan.blocked) {
    $('#rb-groups').innerHTML = '';
    $('#rb-intro').textContent = '';
    $('#rb-blocked').textContent = plan.blocked;
    $('#rb-blocked').hidden = false;
    $('#rb-confirm-field').hidden = true;
    return;
  }

  rbPlan = plan;
  const unfinished = plan.products.filter(rbUnfinished);

  /* Preselect the NARROWEST plausible set, never the widest: what the run left
     unfinished, or nothing at all when it finished everything. A destructive
     dialog that opens with every box ticked is one stray Enter from a very bad
     afternoon. */
  rbChosen = new Set(unfinished.map((p) => p.sku));

  $('#rb-confirm-field').hidden = false;
  $('#rb-picker').hidden = false;
  $('#rb-pick-incomplete').disabled = !unfinished.length;
  $('#rb-intro').innerHTML =
    `This run created <strong>${plan.count}</strong> product(s)`
    + (unfinished.length
        ? `, <strong>${unfinished.length}</strong> of which it never finished configuring. `
        : `, all of them fully configured. `)
    + `Products it resumed, skipped or only previewed are not listed, and cannot
       be deleted from here.`;

  renderRbGroups();
});

/* Grouped, not one flat list: "which of these actually worked" is the question
   the operator is answering, so the answer is the structure. */
function renderRbGroups() {
  if (!rbPlan) return;
  const buckets = [
    ['Incomplete — created, never finished', 'warn',
     rbPlan.products.filter(rbUnfinished)],
    ['Complete — fully configured by this run', 'ok',
     rbPlan.products.filter((p) => !rbUnfinished(p))],
  ];

  $('#rb-groups').innerHTML = buckets.map(([label, cls, items]) => (
    !items.length ? '' : `<section class="rb-group">
      <h4 class="rb-group-title">
        <span class="pill ${cls}">${esc(label)}</span>
        <span class="muted">${items.length}</span>
      </h4>
      <div class="rb-list ${cls}">${items.map(rbRow).join('')}</div>
    </section>`)).join('');

  syncRbArm();
}

/* A real checkbox inside a real label, so the row is one big hit target and
   stays keyboard-reachable — which matters more on this dialog than on any
   other screen in the app. */
function rbRow(p) {
  const note = p.row_status === 'failed' ? 'the run failed after creating it'
             : p.row_status === 'added' ? ''
             : `row status: ${p.row_status || 'not recorded'}`;
  /* Only ever an http(s) link. The URL comes from our own store, but this is
     an anchor in a delete dialog and the check costs one line. */
  const url = /^https?:\/\//i.test(p.product_url || '') ? p.product_url : '';
  return `<div class="rb-row">
    <label class="rb-pick">
      <input type="checkbox" data-sku="${esc(p.sku)}"
             ${rbChosen.has(p.sku) ? 'checked' : ''}>
      <span class="sku">${esc(p.sku)}</span>
    </label>
    ${note ? `<span class="why">${esc(note)}</span>` : ''}
    ${url ? `<a class="rb-open" href="${esc(url)}" target="_blank"
                rel="noopener noreferrer" title="Open in the admin">↗</a>` : ''}
  </div>`;
}

$('#rb-groups').addEventListener('change', (e) => {
  const box = e.target.closest('input[type=checkbox][data-sku]');
  if (!box) return;
  if (box.checked) rbChosen.add(box.dataset.sku);
  else rbChosen.delete(box.dataset.sku);
  syncRbArm();
});

$('#rb-picker').addEventListener('click', (e) => {
  const chip = e.target.closest('[data-pick]');
  if (!chip || !rbPlan) return;
  rbChosen = new Set(
    chip.dataset.pick === 'all' ? rbPlan.products.map((p) => p.sku)
    : chip.dataset.pick === 'none' ? []
    : rbPlan.products.filter(rbUnfinished).map((p) => p.sku));
  renderRbGroups();
});

/* The button says what it is about to do, and deleting everything reads
   heavier than deleting a few, because it is. */
function syncRbArm() {
  const go = $('#rb-go');
  const n = rbChosen.size;
  const total = rbPlan ? rbPlan.count : 0;

  go.textContent = !n ? 'Delete'
    : n === total ? `Delete ALL ${n} product(s)`
    : `Delete ${n} of ${total} product(s)`;
  go.disabled = !(rbPlan && n && $('#rb-confirm').value === 'DELETE');

  /* The sentence reads "<span> removed from…", so the span carries its own
     verb — otherwise the markup has to guess at the plural. */
  $('#rb-count').textContent = !n ? 'The selected products are'
    : n === 1 ? 'One selected product is'
    : `${n} selected products are`;
}

/* Typing DELETE is the arming step. Deliberately exact — a case-insensitive
   match would arm on "delete", which people type by reflex. */
$('#rb-confirm').addEventListener('input', syncRbArm);

$('#rb-cancel').addEventListener('click', () => { $('#rb-modal').hidden = true; });
$('#rb-done').addEventListener('click', () => { $('#rb-result').hidden = true; });

$('#rb-go').addEventListener('click', async () => {
  if (!rbPlan || !rbChosen.size) return;
  const go = $('#rb-go');
  const label = go.textContent;
  const untouched = rbPlan.count - rbChosen.size;
  go.disabled = true;
  go.textContent = 'Deleting…';
  let report;
  try {
    report = await api(`/api/runs/${state.runId}/rollback`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      /* the SKUs the operator actually ticked. The server recomputes the plan
         and allows this only to narrow it, so an entry that is no longer
         deletable refuses the whole request rather than being skipped. */
      body: JSON.stringify({ skus: [...rbChosen], ...readCreds() }),
    });
  } catch (e) {
    go.disabled = false;
    go.textContent = label;
    $('#rb-blocked').textContent = e.message;
    $('#rb-blocked').hidden = false;
    return;
  }
  go.textContent = label;
  $('#rb-modal').hidden = true;
  showRollbackReport(report, untouched);
  pollRun();          // the run's own view is unchanged, but re-read it anyway
});

/* The four outcomes, in the order someone reads them: what went, what was
   already gone, and — the two that need action — what is still there. */
const RB_GROUPS = [
  ['deleted',      'Deleted',                  'ok'],
  ['already_gone', 'Already gone',             'warn'],
  ['refused',      'Refused — still in place', 'bad'],
  ['failed',       'Failed — still in place',  'bad'],
];

function showRollbackReport(report, untouched = 0) {
  const counts = Object.fromEntries(
    RB_GROUPS.map(([key]) => [key, (report[key] || []).length]));
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const left = counts.refused + counts.failed;

  /* Every tile, including the zeroes. On a destructive operation "0 refused,
     0 failed" is the reassurance you came for — hiding it would leave you
     wondering whether it was checked. */
  const tiles = RB_GROUPS.map(([key, label, cls]) =>
    `<div class="stat ${counts[key] ? cls : ''}">
       <span class="stat-n">${counts[key]}</span>
       <span class="stat-l">${esc(label.split(' —')[0])}</span>
     </div>`).join('');

  const row = (item) => (typeof item === 'string'
    ? `<div class="rb-row"><span class="sku">${esc(item)}</span></div>`
    : `<div class="rb-row detail">
         <span class="sku">${esc(item.sku)}</span>
         <span class="rb-why">${esc(item.why)}</span>
       </div>`);

  const groups = RB_GROUPS.map(([key, label, cls]) => {
    const items = report[key] || [];
    if (!items.length) return '';
    return `<section class="rb-group">
      <h4 class="rb-group-title">
        <span class="pill ${cls}">${esc(label)}</span>
        <span class="muted">${items.length}</span>
      </h4>
      <div class="rb-list ${cls}">${items.map(row).join('')}</div>
    </section>`;
  }).join('');

  /* Without this line a partial rollback and a whole one produce identical
     reports, and "did I get the other nine?" is not a question to leave
     someone holding after a destructive operation. */
  const remainder = untouched > 0
    ? `<p class="muted">${untouched} product(s) from this run were not selected
        and are still in the catalogue. You can delete them later — re-open
        this dialog and they will be the only ones listed.</p>`
    : '';

  $('#rb-report').innerHTML = total
    ? `<p class="rb-headline ${left ? 'warn' : ''}">
         ${left
           ? `<strong>${left} of ${total}</strong> product(s) could not be removed and are
              still in the catalogue.`
           : `<strong>${counts.deleted}</strong> product(s) removed from the catalogue.`}
       </p>
       ${remainder}
       <div class="stats wide">${tiles}</div>
       ${groups}`
    : '<p class="muted">Nothing to report.</p>';

  $('#rb-result').hidden = false;
}

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

  /* Only offered once the run has stopped and only when it could have created
     something. A dry run has nothing to undo, and a live run's final set is
     not knowable yet — the server refuses both, this just stops the button
     from inviting it.

     'failed' counts as much as 'added': a row that saved the product and then
     died configuring it is claimed, and deleting that orphan is the whole
     reason this button exists. Gating on `added` alone hid it completely on
     the one run that needed it most — every creation failing means added is 0
     and the products are still there. */
  const c0 = run.counts || {};
  const undoable = (c0.added || 0) + (c0.failed || 0);
  $('#btn-rollback').hidden = run.status === 'running' || !run.commit || !undoable;

  renderOrphanWarning(run);
  renderFailures(run);
  renderSwatchReview(run);
  renderRows(run);
  renderLog(run);
}

/* Half-built products are the one outcome this screen used to leave implicit.
   A failed row that got as far as saving left a real product in the catalogue,
   configured up to whichever step killed it — and the run still reports itself
   as finished. Somebody reading "5 failed" has no reason to know there are now
   five incomplete products live on the store, so say it. */
function renderOrphanWarning(run) {
  const box = $('#r-orphans');
  const failed = run.commit
    ? run.rows.filter((r) => r.status === 'failed') : [];
  box.hidden = !failed.length;
  if (!failed.length) return;

  /* Rows record the product they created before dying, so the count is exact.
     Runs from before that was recorded have no product_url on a failed row —
     there the honest answer is "some of these", and the rollback dialog is
     the thing that actually knows. */
  const known = failed.filter((r) => r.product_url).length;
  box.innerHTML = known
    ? `<strong>${known} half-configured product(s) are still in the catalogue.</strong>
       These rows created their product and then failed part-way through
       configuring it, so what is on the store is incomplete rather than
       absent. Use <em>Delete created products</em> above — they are already
       preselected in that dialog.`
    : `<strong>${failed.length} row(s) failed, and some may have left a
       half-configured product behind.</strong> A row that got as far as saving
       created a real product before it failed. Open <em>Delete created
       products</em> above to see exactly which — they are listed under
       <em>Incomplete</em> and preselected.`;
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
      <td>${swatchCell(r)}</td>
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
    <span class="m">${esc(label)}${retry}${swatchCounts(step.detail)}</span>
    <span class="d">${esc(fmtMs(step.duration_ms))}</span>
  </div>${swatchBlock(step.detail)}${failed && step.error
    ? `<div class="fmsg">${esc(step.error_class || 'failed')} — ${esc(step.error)}</div>`
    : ''}`;
}

/* ── swatches ────────────────────────────────────────────────────────── */
/* Recorded on the values_colour step by app/automation/runner.py, one entry
   per colour the sheet ASKED a swatch for. Colours it did not ask about are
   absent rather than listed as zero, so a product that wanted no swatches
   shows nothing here at all — which is what keeps this an exception report
   rather than another column of noise.

   `set` and `kept` are deliberately not the same word. Both end with a swatch
   on the value and both look identical on the storefront, but `kept` means a
   human had already chosen one and this run refused to overwrite it. */

/* Two words per state: one for the cell, one for the counts line, because
   "1 no swatch" and "a colour that is skipped" both read badly. `hint` is the
   hover text — the states have to make sense to somebody who has never seen
   this screen before, and a one-word cell cannot carry that on its own. */
const SWATCH_STATE = {
  set: { word: 'set', count: 'set', cls: 'ok',
         hint: 'this run chose this swatch and saved it' },
  kept: { word: 'kept', count: 'kept', cls: '',
          hint: 'this colour already had a swatch, so the run left it alone' },
  missed: { word: 'not applied', count: 'not applied', cls: 'warn',
            hint: 'a swatch was chosen but the admin never confirmed it' },
  skipped: { word: 'no swatch', count: 'without a swatch', cls: 'warn',
             hint: 'nothing confident enough to use, so the colour has no swatch' },
  planned: { word: 'not reached', count: 'not reached', cls: '',
             hint: 'the run stopped before it got to this colour' },
};

/* Mirrors CAUSE in app/colour/swatch.py. Two copies because the console never
   asks the server what a slug means — but the slugs are the contract, so a
   rename there is a rename here.

   Headings say what happened in the store's own terms; the line beside each
   says who can fix it. Neither mentions the map, the tiers or the resolver —
   the person reading this uploaded a spreadsheet. */
const SWATCH_CAUSE = {
  unnamed:        'No swatch in this store has that name',
  map_stale:      'Your saved swatch list is out of date for this store',
  map_missing_id: 'Your saved swatch list names a swatch this store does not have',
  unpaired:       'The sheet lists a different number of swatches and colours',
  not_applied:    'A swatch was chosen but the admin never confirmed it',
  duplicate:      'Two swatches share a name, so the first one was used',
};

const SWATCH_FIX = {
  unnamed:        'tell the app which swatch this colour means',
  map_stale:      'the saved entry needs updating for this store',
  map_missing_id: 'the saved entry needs updating for this store',
  unpaired:       'fix the sheet — one swatch per colour, spelled the same',
  not_applied:    'check these colours on the product',
  duplicate:      'nothing to do, unless the other one was meant',
};

/* The last attempt wins: a retried step is recorded once per attempt, and only
   the final one describes what the product actually ended up with. */
function swatchDetail(row) {
  const steps = ((row && row.steps) || [])
    .filter((s) => s.name === 'values_colour' && s.detail
                   && (s.detail.unavailable || (s.detail.swatches || []).length));
  return steps.length ? steps[steps.length - 1].detail : null;
}

/* Counted from the entries rather than read from detail.counts, which the
   runner only writes once the value editor has answered. A step that DIED has
   entries and no counts, and that is the one time somebody is reading this
   closely — "—" would say the sheet asked for nothing. */
function swatchTally(detail) {
  const tally = {};
  for (const entry of (detail && detail.swatches) || []) {
    const state = SWATCH_STATE[entry.state] ? entry.state : 'planned';
    tally[state] = (tally[state] || 0) + 1;
  }
  return tally;
}

function swatchCounts(detail) {
  if (!detail) return '';
  if (detail.unavailable) return '<span class="swc warn">no swatch field</span>';
  const c = swatchTally(detail);
  const parts = ['set', 'kept', 'missed', 'skipped', 'planned']
    .filter((k) => c[k]).map((k) => `${c[k]} ${SWATCH_STATE[k].count}`);
  if (!parts.length) return '';
  const attention = (c.skipped || 0) + (c.missed || 0);
  return `<span class="swc ${attention ? 'warn' : ''}">${esc(parts.join(' · '))}</span>`;
}

function swatchBlock(detail) {
  if (!detail) return '';
  if (detail.unavailable) {
    return `<div class="fmsg">this store's admin has no colour-swatch box, so the
      sheet's Color Swatch column could not be used</div>`;
  }
  const list = detail.swatches || [];
  if (!list.length) return '';
  return `<div class="swatches">${list.map(swatchLine).join('')}</div>`;
}

function swatchLine(s) {
  const state = SWATCH_STATE[s.state] ? s.state : 'planned';
  const named = s.swatch_label
    ? `${s.swatch_label} (${s.swatch_id})`
    : (s.swatch_id ? `swatch ${s.swatch_id}` : '');
  // The short form of the duplicate case. The full sentence is on the roll-up
  // above, where there is room for it; on one line of forty this is the part
  // that tells you it was a coin flip.
  const alts = (s.alternatives || []).length
    ? `${s.alternatives.length + 1} swatches share this name — used the first`
    : '';
  const note = alts || s.note || '';
  const asked = s.asked && s.asked !== s.colour
    ? ` <span class="asked">sheet asked for ${esc(s.asked)}</span>` : '';
  return `<div class="sw ${esc(state)}">
    <span class="c">${esc(s.colour)}${asked}</span>
    <span class="st ${esc(SWATCH_STATE[state].cls)}"
          title="${esc(SWATCH_STATE[state].hint)}">${esc(SWATCH_STATE[state].word)}</span>
    <span class="v">${named ? esc(named) : '<span class="muted">—</span>'}</span>
    <span class="n" title="${esc(s.note || note)}">${esc(note)}</span>
  </div>`;
}

/* The per-product cell: enough to know which row is worth opening, never a zero. */
function swatchCell(row) {
  const detail = swatchDetail(row);
  if (!detail) return '<span class="muted">—</span>';
  if (detail.unavailable) return '<span class="pill warn">no field</span>';
  const c = swatchTally(detail);
  const pills = [];
  if (c.set) pills.push(`<span class="pill ok">${c.set} set</span>`);
  if (c.kept) pills.push(`<span class="pill">${c.kept} kept</span>`);
  const attention = (c.skipped || 0) + (c.missed || 0);
  if (attention) pills.push(`<span class="pill warn">${attention} without</span>`);
  if (c.planned) pills.push(`<span class="pill">${c.planned} not reached</span>`);
  return pills.join(' ') || '<span class="muted">—</span>';
}

/* Grouped by CAUSE, for the same reason failures are: a 200-product run does
   not have 40 swatch problems, it has two — and opening 40 dropdowns to find
   that out is how a report gets ignored. Only colours worth a look appear here;
   one that was set or kept with nothing surprising about it does not. */
function renderSwatchReview(run) {
  const groups = {};
  const unavailable = [];
  for (const row of run.rows) {
    const detail = swatchDetail(row);
    if (!detail) continue;
    if (detail.unavailable) { unavailable.push(row.sku); continue; }
    for (const entry of detail.swatches || []) {
      const cause = entry.cause || (entry.state === 'missed' ? 'not_applied' : '');
      if (!cause) continue;
      (groups[cause] ||= []).push({ sku: row.sku, entry });
    }
  }

  const keys = Object.keys(groups);
  $('#r-swatches').hidden = !keys.length && !unavailable.length;
  if (!keys.length && !unavailable.length) return;

  // Problems first, the informational duplicate group last — it needs no
  // action, and putting it on top would bury the ones that do.
  keys.sort((a, b) => (a === 'duplicate') - (b === 'duplicate')
                      || groups[b].length - groups[a].length);

  const banner = unavailable.length
    ? `<div class="alert warn">This store's admin has no colour-swatch box, so the
       sheet's Color Swatch column could not be used on ${unavailable.length}
       product(s).</div>`
    : '';

  $('#r-swatch-groups').innerHTML = banner + keys.map((cause) => `
    <details class="fgroup${cause === 'duplicate' ? ' info' : ''}">
      <summary>${esc(SWATCH_CAUSE[cause] || cause)}
        <span class="pill ${cause === 'duplicate' ? '' : 'warn'}">${groups[cause].length}</span>
        <span class="blame">${esc(SWATCH_FIX[cause] || '')}</span></summary>
      <div class="fbody">
        ${groups[cause].map(({ sku, entry }) => `
          <div>
            <strong class="mono">${esc(sku)}</strong>
            <span class="swname">${esc(entry.colour)}</span>
            <div class="fmsg">${esc(entry.note || '')}</div>
          </div>`).join('')}
      </div>
    </details>`).join('');
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
  // Includes the swatch counts: a step is recorded once and then never grows,
  // but the LAST attempt of values_colour is what carries them, so a retry can
  // change this block without changing how many steps there are.
  const sig = `${events.length}|${run.rows
    .map((r) => `${r.status}:${(r.steps || []).length}:${
      JSON.stringify(swatchTally(swatchDetail(r)))}`).join(',')}`;
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
    // Worth saying on the closed line: a swatch never fails a product, so
    // without this the only clue is one row of a block nobody has opened.
    const counts = swatchTally(swatchDetail(row));
    const without = (counts.skipped || 0) + (counts.missed || 0);
    return `<details class="alog" data-idx="${esc(index)}"
              ${state.openRows.has(String(index)) ? 'open' : ''}>
      <summary>
        <span class="t">${esc(clock(e.at))}</span>
        <span class="m">${esc(e.message)}</span>
        ${row ? pill(row.status) : ''}
        <span class="acount">${entries.length} action${entries.length === 1 ? '' : 's'}${
          failed ? `, ${failed} failed` : ''}${
          without ? `, ${without} colour${without === 1 ? '' : 's'} without a swatch` : ''}</span>
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

  // No pending badge. Unconfirmed entries used to be work worth doing — a
  // confirmation saved the next run a model call. Nothing reads the map back
  // now, so counting them on the tab was a permanent nag for a job with no
  // payoff. The rows still show their status for anyone who opens the view.
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

/* Lock the page while any dialog is open. Watched rather than toggled at each
   call site: every modal here is shown and hidden by setting `hidden` on its
   backdrop, so observing that one attribute covers all of them and cannot
   drift out of sync with a new one. */
(function lockScrollBehindModals() {
  const backdrops = $$('.modal-backdrop');
  const sync = () => document.body.classList.toggle(
    'modal-open', backdrops.some((b) => !b.hidden));
  backdrops.forEach((b) => new MutationObserver(sync)
    .observe(b, { attributes: true, attributeFilter: ['hidden'] }));
  sync();
})();

restoreCreds();
loadHealth();
refreshRunList();
setInterval(() => { if (!state.runId) refreshRunList(); }, 5000);
