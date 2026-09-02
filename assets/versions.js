/* explain skill — version history: the list, and the diff between two versions.
 *
 * Loaded on demand by explain.js the first time a reader opens the history,
 * which is also why it reaches back into the page through the `H` host object
 * rather than importing anything: explain.js owns the document, the strings,
 * and the conversation panel.
 *
 * The diff is computed here, in the browser, on purpose. Parsing the two
 * snapshots with DOMParser gives exactly the text the live page's anchor index
 * sees, and the same DOM is what renders a before/after diagram — a
 * server-side differ would have to reimplement both and could disagree with
 * either.
 *
 * Shape of the comparison: views (data-view) are matched by id, each view's
 * blocks are aligned by content, and only blocks that actually differ are
 * drawn. A diagram whose labels and links are unchanged is reported as a
 * layout change and folded away, because regenerating a document nudges SVG
 * coordinates and that noise would otherwise drown the signal.
 */

const BLOCK_SEL =
  'p, h1, h2, h3, h4, h5, h6, li, pre, figure, table, blockquote, summary, .ex-note';
const LCS_CELL_LIMIT = 400000;

let ctx = null;                 // {host, routeId, H} — for refresh()
const listCache = { entries: null };
const docCache = new Map();     // version number -> parsed <main>

// ---------- entry point ----------

export async function route(host, routeId, H) {
  ctx = { host, routeId, H };
  try {
    const diff = /^~diff\/(\d+)\.\.(\d+)$/.exec(routeId);
    const full = /^~full\/(\d+)$/.exec(routeId);
    if (diff) return await showDiff(host, H, +diff[1], +diff[2]);
    if (full) return await showFull(host, H, +full[1]);
    return await showList(host, H, false);
  } catch (e) {
    host.innerHTML =
      bar(H, H.S.versionsTitle) +
      `<div class="ex-banner ex-banner-warn">${H.esc(H.S.versionsFailed)}` +
      (e && e.message ? ` (${H.esc(e.message)})` : '') +
      '</div>';
    wire(host, H);
  }
}

/* A new version landing while the overlay is open only rewrites the LIST —
 * repainting a diff under someone who is reading it loses their place. */
export function refresh() {
  if (ctx && ctx.routeId === '~versions') showList(ctx.host, ctx.H, true).catch(() => {});
}

// ---------- data ----------

async function entries(H, force) {
  if (!listCache.entries || force) listCache.entries = (await H.list()).versions || [];
  return listCache.entries;
}

/* The cached list is a snapshot too. A version recorded after the reader
 * opened the overlay is reachable by URL before it is in the cache, and a
 * miss there would render its date and commit as blanks. */
async function entriesWith(H, ...wanted) {
  let list = await entries(H, false);
  if (wanted.some((n) => !list.some((e) => e.n === n))) list = await entries(H, true);
  return list;
}

async function snapshot(H, n) {
  if (!docCache.has(n)) {
    const res = await fetch(H.snapshotUrl(n));
    if (!res.ok) throw new Error(`v${n}: ${res.status}`);
    const doc = new DOMParser().parseFromString(await res.text(), 'text/html');
    doc.querySelectorAll('script, style, link').forEach((n2) => n2.remove());
    docCache.set(n, doc.getElementById('explain-content') || doc.body);
  }
  return docCache.get(n);
}

function viewsOf(main) {
  const map = new Map();
  const els = main.querySelectorAll('[data-view]');
  if (!els.length) {
    map.set('', { el: main, title: '', order: 0 });
    return map;
  }
  els.forEach((el, i) => {
    const h = el.querySelector('h1, h2, h3');
    map.set(el.dataset.view, {
      el,
      title: el.dataset.title || (h ? norm(h.textContent) : el.dataset.view),
      order: i,
    });
  });
  return map;
}

// ---------- blocks ----------

function norm(s) {
  return String(s).replace(/\s+/g, ' ').trim();
}

function codeText(el) {
  return el.textContent
    .split('\n')
    .map((l) => l.replace(/\s+$/, ''))   // trailing whitespace only: the
    .join('\n')                          // indentation is verified evidence
    .replace(/^\n+|\n+$/g, '');
}

/* A diagram's meaning is carried by its labels and the views its nodes link
 * to. Its coordinates are not meaning — they move whenever the document is
 * regenerated. So `key` decides whether it CHANGED and `deep` only decides
 * whether to mention that the drawing was redrawn. */
function figureKey(el) {
  const labels = Array.from(el.querySelectorAll('text, title, figcaption'))
    .map((n) => norm(n.textContent))
    .filter(Boolean);
  const links = Array.from(el.querySelectorAll('a[href]')).map((a) => a.getAttribute('href'));
  return JSON.stringify([labels, links]);
}

function blockInfo(el) {
  if (el.tagName === 'FIGURE') {
    return { kind: 'figure', el, key: figureKey(el), deep: norm(el.innerHTML) };
  }
  if (el.tagName === 'PRE') {
    const code = codeText(el);
    return { kind: 'code', el, key: code, deep: code };
  }
  if (el.tagName === 'TABLE') {
    return { kind: 'rich', el, key: norm(el.textContent), deep: norm(el.innerHTML) };
  }
  const t = norm(el.textContent);
  return { kind: 'text', el, key: t, deep: t };
}

/* Outermost matches only: a <pre> inside .ex-chunk, or a <p> inside <li>, is
 * part of its parent block rather than a block of its own. */
function blocksOf(viewEl, scoped) {
  const out = [];
  for (const el of viewEl.querySelectorAll(BLOCK_SEL)) {
    if (scoped && el.closest('[data-view]') !== viewEl) continue;
    const outer = el.parentElement && el.parentElement.closest(BLOCK_SEL);
    if (outer && viewEl.contains(outer)) continue;
    const info = blockInfo(el);
    if (!info.key) continue;
    out.push(info);
  }
  return out;
}

// ---------- alignment ----------

function lcsOps(a, b, ka, kb) {
  const n = a.length, m = b.length;
  const ops = [];
  if (n * m > LCS_CELL_LIMIT) {
    for (const x of a) ops.push({ op: 'del', a: x });
    for (const y of b) ops.push({ op: 'add', b: y });
    return ops;
  }
  const w = m + 1;
  const dp = new Int32Array((n + 1) * w);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i * w + j] = ka[i] === kb[j]
        ? dp[(i + 1) * w + j + 1] + 1
        : Math.max(dp[(i + 1) * w + j], dp[i * w + j + 1]);
    }
  }
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (ka[i] === kb[j]) { ops.push({ op: 'same', a: a[i], b: b[j] }); i++; j++; }
    else if (dp[(i + 1) * w + j] >= dp[i * w + j + 1]) { ops.push({ op: 'del', a: a[i] }); i++; }
    else { ops.push({ op: 'add', b: b[j] }); j++; }
  }
  while (i < n) ops.push({ op: 'del', a: a[i++] });
  while (j < m) ops.push({ op: 'add', b: b[j++] });
  return ops;
}

/* Alignment pins the blocks that are byte-identical; everything between two
 * pins is a run of removals and additions. Pairing them off in order is what
 * turns "this paragraph vanished and another appeared" into "these three
 * words changed" — without it there would be no word-level diff at all. */
function pairRuns(ops) {
  const out = [];
  let i = 0;
  while (i < ops.length) {
    if (ops[i].op === 'same') {
      const o = ops[i++];
      out.push({ op: o.a.deep === o.b.deep ? 'same' : 'minor', a: o.a, b: o.b });
      continue;
    }
    const dels = [], adds = [];
    while (i < ops.length && ops[i].op !== 'same') {
      (ops[i].op === 'del' ? dels : adds).push(ops[i].op === 'del' ? ops[i].a : ops[i].b);
      i++;
    }
    let d = 0, a = 0;
    while (d < dels.length && a < adds.length) {
      if (dels[d].kind === adds[a].kind) out.push({ op: 'mod', a: dels[d++], b: adds[a++] });
      else if (dels.length - d >= adds.length - a) out.push({ op: 'del', a: dels[d++] });
      else out.push({ op: 'add', b: adds[a++] });
    }
    while (d < dels.length) out.push({ op: 'del', a: dels[d++] });
    while (a < adds.length) out.push({ op: 'add', b: adds[a++] });
  }
  return out;
}

function diffBlocks(before, after) {
  const ops = lcsOps(
    before, after,
    before.map((x) => x.kind + '\u0000' + x.key),
    after.map((x) => x.kind + '\u0000' + x.key)
  );
  return pairRuns(ops);
}

// ---------- inline diffs ----------

/* Words, numbers and whitespace as runs; punctuation on its own. Splitting
 * the full stop off "path." is what lets it align with "path" when a sentence
 * grows a clause, instead of reporting the whole word as replaced. */
function tokenize(s) {
  return s.match(/\s+|[\p{L}\p{N}_]+|[^\s]/gu) || [];
}

function inlineDiff(before, after, esc) {
  const A = tokenize(before), B = tokenize(after);
  const ops = lcsOps(A, B, A, B);
  let html = '', del = '', add = '';
  const flush = () => {
    if (del) html += `<del>${esc(del)}</del>`;
    if (add) html += `<ins>${esc(add)}</ins>`;
    del = ''; add = '';
  };
  for (const o of ops) {
    if (o.op === 'same') { flush(); html += esc(o.a); }
    else if (o.op === 'del') del += o.a;
    else add += o.b;
  }
  flush();
  return html;
}

function lineDiff(before, after, esc) {
  const A = before.split('\n'), B = after.split('\n');
  return lcsOps(A, B, A, B)
    .map((o) => {
      if (o.op === 'same') return `<span>${esc(o.a) || '&nbsp;'}</span>`;
      if (o.op === 'del') return `<del>${esc(o.a) || '&nbsp;'}</del>`;
      return `<ins>${esc(o.b) || '&nbsp;'}</ins>`;
    })
    .join('');
}

// ---------- rendering helpers ----------

function bar(H, title, extra) {
  return `<div class="ex-ver-bar"><h2>${H.esc(title)}</h2>${extra || ''}` +
    `<span class="ex-spacer"></span>` +
    `<button class="ex-btn" data-act="ver-close">${H.esc(H.S.close)}</button></div>`;
}

/* Imported nodes come from another document: their ids would duplicate the
 * live page's, their in-page links would navigate out from under the overlay,
 * and steppers have no JS here to drive their tabs. */
function adopt(el, interactive) {
  const node = document.importNode(el, true);
  const scrub = (n) => {
    n.removeAttribute('id');
    n.removeAttribute('data-view');
    n.removeAttribute('data-goto');
    const href = n.getAttribute('href');
    if (href && href.startsWith('#')) n.removeAttribute('href');
  };
  if (node.nodeType === 1) scrub(node);
  node.querySelectorAll('*').forEach(scrub);
  node.querySelectorAll('.ex-steps').forEach((s) => s.classList.add('ex-steps-expanded'));
  if (!interactive) node.querySelectorAll('details').forEach((d) => { d.open = true; });
  return node;
}

function slot(slots, el) {
  slots.push(el);
  return `<div class="ex-ver-slot" data-slot="${slots.length - 1}"></div>`;
}

function fillSlots(host, slots, interactive) {
  host.querySelectorAll('.ex-ver-slot').forEach((holder) => {
    const el = slots[+holder.dataset.slot];
    if (el) holder.appendChild(adopt(el, interactive));
  });
}

function wire(host, H) {
  host.onclick = (e) => {
    const el = e.target.closest('[data-act]');
    if (!el) return;
    const act = el.dataset.act;
    if (act === 'ver-close') { H.close(); return; }
    if (act === 'ver-go') { H.goto(el.dataset.hash); return; }
    if (act === 'ver-thread') { H.openThread(el.dataset.tid); return; }
    if (act === 'ver-discuss') { H.discuss(el.dataset.vw, el.dataset.vt); return; }
    if (act === 'ver-compare') {
      const picked = Array.from(host.querySelectorAll('.ex-ver-pick:checked')).map((c) => +c.dataset.n);
      if (picked.length === 2) H.goto(`#/~diff/${Math.min(...picked)}..${Math.max(...picked)}`);
      return;
    }
  };
  host.onchange = (e) => {
    if (!e.target.classList.contains('ex-ver-pick')) return;
    const picked = host.querySelectorAll('.ex-ver-pick:checked').length;
    const btn = host.querySelector('[data-act="ver-compare"]');
    if (btn) btn.disabled = picked !== 2;
  };
  // move focus into the overlay without landing it on a button, where Enter
  // would navigate somewhere the reader never asked to go
  host.tabIndex = -1;
  host.focus({ preventScroll: true });
}

// ---------- the list ----------

async function showList(host, H, force) {
  const list = await entries(H, force);
  const S = H.S;
  const live = new Map((H.liveThreads() || []).map((t) => [t.id, t]));
  const latest = list.length ? list[list.length - 1].n : null;

  const rows = list.slice().reverse().map((e, i) => {
    const prev = list[list.length - 2 - i];
    const target = prev ? `#/~diff/${prev.n}..${e.n}` : `#/~full/${e.n}`;
    const chips = [];
    if (e.n === latest) chips.push(`<span class="ex-chip">${H.esc(S.current)}</span>`);
    if (e.source !== 'agent') chips.push(`<span class="ex-chip ex-chip-auto">${H.esc(S.autoRecorded)}</span>`);
    // a thread can be deleted after the fact; a chip for one that is gone
    // would resurrect something the reader cleared, so it is simply omitted
    for (const tid of e.threads || []) {
      const t = live.get(tid);
      if (!t) continue;
      const label = norm(((t.messages || [])[0] || {}).body || '').slice(0, 28);
      chips.push(`<button class="ex-ver-thread" data-act="ver-thread" data-tid="${H.esc(tid)}">💬 ${H.esc(label)}</button>`);
    }
    return `<div class="ex-ver-row">` +
      `<input type="checkbox" class="ex-ver-pick" data-n="${e.n}" aria-label="v${e.n}">` +
      `<button class="ex-ver-open" data-act="ver-go" data-hash="${H.esc(target)}">` +
      `<span class="ex-ver-num">v${e.n}</span>` +
      `<span class="ex-ver-when">${H.esc(H.fmtTime(e.created_at))}</span>` +
      `<span class="ex-ver-sum${e.summary ? '' : ' ex-ver-nosum'}">${H.esc(e.summary || S.noSummary)}</span>` +
      `</button><span class="ex-ver-chips">${chips.join('')}</span></div>`;
  });

  host.innerHTML =
    bar(H, S.versionsTitle,
      `<button class="ex-btn" data-act="ver-compare" disabled>${H.esc(S.compareSelected)}</button>`) +
    `<div class="ex-ver-list">${rows.join('')}</div>` +
    `<p class="ex-ver-note">${H.esc(S.historyStart)}</p>`;
  host.scrollTop = 0;
  wire(host, H);
  H.seen(latest);
}

// ---------- the diff ----------

async function showDiff(host, H, a, b) {
  const S = H.S;
  const [list, beforeMain, afterMain] = await Promise.all([
    entriesWith(H, a, b), snapshot(H, a), snapshot(H, b),
  ]);
  const meta = (n) => list.find((e) => e.n === n) || {};
  const ea = meta(a), eb = meta(b);
  const beforeViews = viewsOf(beforeMain);
  const afterViews = viewsOf(afterMain);
  const slots = [];
  const sections = [];

  const ids = [];
  for (const id of afterViews.keys()) ids.push(id);
  for (const id of beforeViews.keys()) if (!afterViews.has(id)) ids.push(id);

  for (const id of ids) {
    const before = beforeViews.get(id);
    const after = afterViews.get(id);
    const section = after
      ? (before ? changedView(H, id, before, after, slots) : wholeView(H, id, after, 'add', slots))
      : wholeView(H, id, before, 'del', slots);
    if (section) sections.push(section);
  }

  const commits = ea.commit !== eb.commit
    ? `<span class="ex-ver-commits">${H.esc(S.generatedFrom)} <code>${H.esc(String(ea.commit || '—').slice(0, 12))}</code>` +
      ` → <code>${H.esc(String(eb.commit || '—').slice(0, 12))}</code></span>`
    : '';

  host.innerHTML =
    bar(H, `v${a} → v${b}`,
      `<button class="ex-btn" data-act="ver-go" data-hash="#/~versions">${H.esc(S.versionsTitle)}</button>` +
      `<button class="ex-btn" data-act="ver-go" data-hash="#/~full/${b}">${H.esc(S.fullVersion)}</button>`) +
    `<div class="ex-ver-meta">${H.esc(H.fmtTime(eb.created_at))}${commits}` +
    (eb.summary ? `<p class="ex-ver-summary">${H.esc(eb.summary)}</p>` : '') + `</div>` +
    (sections.length
      ? `<h3 class="ex-ver-h">${H.esc(S.changedSections)} (${sections.length})</h3>` + sections.join('')
      : `<p class="ex-ver-note">${H.esc(S.noChanges)}</p>`);
  host.scrollTop = 0;
  fillSlots(host, slots, false);
  wire(host, H);
}

function sectionHead(H, id, title, chips) {
  // NOT data-view: `#explain-content [data-view]` hides views, and the
  // overlay lives inside #explain-content
  const discuss = id
    ? `<button class="ex-btn-ghost" data-act="ver-discuss" data-vw="${H.esc(id)}" data-vt="${H.esc(title)}">` +
      `${H.esc(H.S.discussSection)}</button>`
    : '';
  return `<div class="ex-ver-sec-head"><h4>${H.esc(title || H.S.contents)}</h4>` +
    `${chips || ''}<span class="ex-spacer"></span>${discuss}</div>`;
}

function changedView(H, id, before, after, slots) {
  const S = H.S;
  const scoped = !!after.el.dataset.view;
  const ops = diffBlocks(blocksOf(before.el, scoped), blocksOf(after.el, scoped));
  let body = renderOps(H, ops, slots);
  const titleChanged = before.title !== after.title;

  if (!body) {
    // nothing the block extractor covers moved; fall back to the whole view's
    // rendered text so a change in unusual markup is still reported
    const bt = norm(before.el.textContent), at = norm(after.el.textContent);
    if (bt !== at) body = `<div class="ex-ver-block ex-ver-mod"><p>${inlineDiff(bt, at, H.esc)}</p></div>`;
  }
  if (!body && !titleChanged) return '';

  const chips = titleChanged
    ? `<span class="ex-chip">${H.esc(S.titleChanged)}: ${H.esc(before.title)} → ${H.esc(after.title)}</span>`
    : '';
  return `<section class="ex-ver-sec">${sectionHead(H, id, after.title, chips)}${body}</section>`;
}

function wholeView(H, id, view, op, slots) {
  const S = H.S;
  const chip = `<span class="ex-chip ${op === 'add' ? 'ex-chip-add' : 'ex-chip-del'}">` +
    `${H.esc(op === 'add' ? S.sectionAdded : S.sectionRemoved)}</span>`;
  const blocks = blocksOf(view.el, !!view.el.dataset.view);
  const shown = blocks.slice(0, 12);
  const rest = blocks.length - shown.length;
  const body = shown.map((blk) =>
    `<div class="ex-ver-block ex-ver-${op}">${slot(slots, blk.el)}</div>`).join('') +
    (rest > 0 ? `<p class="ex-ver-note">+${rest}</p>` : '');
  // a removed view cannot be discussed by id — it is not in the document
  const head = op === 'add' ? sectionHead(H, id, view.title, chip)
    : `<div class="ex-ver-sec-head"><h4>${H.esc(view.title)}</h4>${chip}</div>`;
  return `<section class="ex-ver-sec">${head}${body}</section>`;
}

function renderOps(H, ops, slots) {
  const S = H.S;
  let html = '', sameRun = 0;
  const flushSame = () => {
    if (sameRun) html += `<p class="ex-ver-skip">${H.esc(H.fmt(S.unchangedBlocks, { n: sameRun }))}</p>`;
    sameRun = 0;
  };
  let changes = 0;

  for (const o of ops) {
    if (o.op === 'same') { sameRun++; continue; }
    if (o.op === 'minor') {
      // labels and links identical: the drawing was redrawn, not rewritten
      flushSame();
      changes++;
      html += `<details class="ex-ver-block ex-ver-minor"><summary>${H.esc(S.diagramLayout)}</summary>` +
        beforeAfter(H, o.a.el, o.b.el, slots) + `</details>`;
      continue;
    }
    flushSame();
    changes++;
    if (o.op === 'mod' && o.a.kind === 'text') {
      html += `<div class="ex-ver-block ex-ver-mod"><p>${inlineDiff(o.a.key, o.b.key, H.esc)}</p></div>`;
    } else if (o.op === 'mod' && o.a.kind === 'code') {
      html += `<div class="ex-ver-block ex-ver-mod"><pre class="ex-ver-lines">${lineDiff(o.a.key, o.b.key, H.esc)}</pre></div>`;
    } else if (o.op === 'mod') {
      html += `<div class="ex-ver-block ex-ver-mod">` +
        (o.a.kind === 'figure' ? `<div class="ex-ver-label">${H.esc(S.diagramChanged)}</div>` : '') +
        beforeAfter(H, o.a.el, o.b.el, slots) + `</div>`;
    } else if (o.op === 'del') {
      html += `<div class="ex-ver-block ex-ver-del">${slot(slots, o.a.el)}</div>`;
    } else {
      html += `<div class="ex-ver-block ex-ver-add">${slot(slots, o.b.el)}</div>`;
    }
  }
  flushSame();
  return changes ? html : '';
}

function beforeAfter(H, a, b, slots) {
  return `<div class="ex-ver-ba">` +
    `<div><div class="ex-ver-label">${H.esc(H.S.before)}</div>${slot(slots, a)}</div>` +
    `<div><div class="ex-ver-label">${H.esc(H.S.after)}</div>${slot(slots, b)}</div></div>`;
}

// ---------- a whole past version ----------

async function showFull(host, H, n) {
  const S = H.S;
  const [list, main] = await Promise.all([entriesWith(H, n), snapshot(H, n)]);
  const e = list.find((x) => x.n === n) || {};
  const views = viewsOf(main);
  const slots = [];
  // sections keep their own headings, so label one only when data-title says
  // something the heading doesn't
  const body = Array.from(views.values())
    .map((v) => {
      const h = v.el.querySelector('h1, h2, h3');
      const dup = h && norm(h.textContent).toLowerCase() === norm(v.title).toLowerCase();
      return (v.title && !dup ? `<h3 class="ex-ver-h">${H.esc(v.title)}</h3>` : '') + slot(slots, v.el);
    })
    .join('');
  const prev = list[list.findIndex((x) => x.n === n) - 1];

  host.innerHTML =
    bar(H, `v${n}`,
      `<button class="ex-btn" data-act="ver-go" data-hash="#/~versions">${H.esc(S.versionsTitle)}</button>` +
      (prev ? `<button class="ex-btn" data-act="ver-go" data-hash="#/~diff/${prev.n}..${n}">${H.esc(S.backToChanges)}</button>` : '')) +
    `<div class="ex-ver-meta">${H.esc(H.fmtTime(e.created_at))}` +
    (e.summary ? `<p class="ex-ver-summary">${H.esc(e.summary)}</p>` : '') + `</div>` +
    `<div class="ex-ver-full">${body}</div>`;
  host.scrollTop = 0;
  fillSlots(host, slots, true);
  wire(host, H);
}
