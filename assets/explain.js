/* explain skill — conversation layer.
 *
 * The sidebar holds two kinds of thread, in one list: comments anchored to a
 * text selection, and document-level conversations started from the panel's
 * own button (anchor: null, optionally carrying the view the reader was on).
 *
 * Anchors: {exact, prefix, suffix} captured from the reader's selection and
 * re-located against #explain-content's concatenated text on every load, so
 * regenerated documents keep their comments (unlocatable ones show as
 * orphaned in the sidebar). Highlight rendering uses the CSS Custom
 * Highlight API when available; without it the sidebar still works.
 *
 * All writes go through the local server's JSON API; the page polls
 * /state every 2.5s and refetches comments when the rev moves.
 */
(() => {
  'use strict';

  const CFG = window.EXPLAIN || { slug: '', lang: 'en' };

  const STRINGS = {
    en: {
      panelTitle: 'Conversations',
      addComment: 'Comment',
      newConversation: 'New conversation',
      newPlaceholder: 'Ask anything about this document…',
      includePage: 'Include current page',
      placeholder: 'Leave a comment on the selected text…',
      replyPlaceholder: 'Reply…',
      send: 'Send',
      cancel: 'Cancel',
      reply: 'Reply',
      resolve: 'Resolve',
      reopen: 'Reopen',
      edit: 'Edit',
      delete: 'Delete',
      save: 'Save',
      you: 'You',
      agent: 'Agent',
      edited: 'edited',
      resolved: 'Resolved',
      unread: 'Waiting for agent',
      newReply: 'New reply',
      orphaned: 'Lost anchor',
      empty: 'Start a conversation, or select text in the document to comment on it.',
      watching: 'An agent session is watching this document.',
      notWatching: 'No agent is watching right now — comments will be answered when a session resumes.',
      docUpdated: 'This document was updated.',
      refresh: 'Reload',
      disconnected: 'Server unreachable — it may have shut down. Comments are read-only until a session restarts it.',
      confirmDelete: 'Delete this?',
      resolvedGroup: 'Resolved',
      expandAll: 'Expand all',
      tabView: 'Tabs',
      generatedFrom: 'Generated from',
      goToAnchor: 'Go to text',
      contents: 'Contents',
      toggleNav: 'Toggle sidebar',
      toggleBranch: 'Expand or collapse',
    },
    ko: {
      panelTitle: '대화',
      addComment: '댓글',
      newConversation: '새 대화',
      newPlaceholder: '이 문서에 대해 무엇이든 물어보세요…',
      includePage: '현재 페이지 포함',
      placeholder: '선택한 부분에 댓글을 남겨보세요…',
      replyPlaceholder: '답글…',
      send: '등록',
      cancel: '취소',
      reply: '답글',
      resolve: '해결됨으로 표시',
      reopen: '다시 열기',
      edit: '수정',
      delete: '삭제',
      save: '저장',
      you: '나',
      agent: '에이전트',
      edited: '수정됨',
      resolved: '해결됨',
      unread: '에이전트 응답 대기',
      newReply: '새 답글',
      orphaned: '위치 잃음',
      empty: '새 대화를 시작하거나, 문서에서 텍스트를 드래그해 댓글을 남겨보세요.',
      watching: '에이전트 세션이 이 문서를 감시하고 있습니다.',
      notWatching: '지금은 감시 중인 에이전트가 없습니다 — 다음 세션이 이어받을 때 답변됩니다.',
      docUpdated: '문서가 갱신되었습니다.',
      refresh: '새로고침',
      disconnected: '서버에 연결할 수 없습니다 — 종료된 것 같습니다. 세션이 재시작할 때까지 읽기 전용입니다.',
      confirmDelete: '삭제할까요?',
      resolvedGroup: '해결됨',
      expandAll: '모두 펼치기',
      tabView: '탭 보기',
      generatedFrom: '생성 기준',
      goToAnchor: '위치로 이동',
      contents: '목차',
      toggleNav: '사이드바 접기/펼치기',
      toggleBranch: '펼치기/접기',
    },
  };
  const S = STRINGS[CFG.lang] || STRINGS.en;
  const CTX_LEN = 48;
  const API = `/api/docs/${encodeURIComponent(CFG.slug)}`;

  let content, sidebar, toggleBtn;
  let data = { rev: -1, threads: [] };
  let idx = null;                 // {text, nodes:[{node,start}], nodeMap}
  let views = new Map();          // viewId -> {el, title, parent}
  let activeView = null;
  let crumbs = null;
  let navTree = null;             // the left hierarchy sidebar's <nav>
  const navCollapsed = new Set(); // view ids whose children are folded away
  let ranges = new Map();         // threadId -> Range|null
  let openId = null;              // expanded thread
  let editingId = null;           // message id being edited
  let composerOpen = false;       // the anchorless "new conversation" composer
  let composerDraft = '';         // survives the re-renders polling triggers
  let composerIncludeView = false;
  let initialEtag = null;
  let watched = null;
  let docMeta = null;
  let disconnected = false;
  let docUpdated = false;
  let renderPending = false;

  // ---------- API ----------

  async function req(method, path, body) {
    const res = await fetch(API + path, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) throw new Error(`${method} ${path}: ${res.status}`);
    return res.json();
  }

  async function refresh(force) {
    data = await req('GET', '/comments');
    relocate();
    render(force);
  }

  // ---------- drill-down views ----------

  function setupViews() {
    const els = content.querySelectorAll('[data-view]');
    if (!els.length) return;
    for (const el of els) {
      const id = el.dataset.view;
      const heading = el.querySelector('h1,h2,h3');
      views.set(id, {
        el,
        title: el.dataset.title || (heading ? heading.textContent : id),
        parent: el.dataset.parent || null,
      });
    }
    crumbs = document.createElement('nav');
    crumbs.className = 'ex-crumbs ex-ui';
    // delegated: activateView() rewrites the bar's innerHTML on every navigation
    crumbs.addEventListener('click', (e) => {
      if (e.target.closest('[data-act="toggle-nav"]')) toggleNav();
    });
    content.prepend(crumbs);
    setupNav();
    window.addEventListener('hashchange', applyHash);
    applyHash();
  }

  function homeId() {
    for (const [id, v] of views) if (!v.parent) return id;
    return views.keys().next().value;
  }

  function applyHash() {
    const m = location.hash.match(/^#\/(.+)$/);
    if (m) {
      const id = decodeURIComponent(m[1]);
      if (views.has(id)) {
        activateView(id);
        window.scrollTo({ top: 0 });
        return;
      }
    }
    if (location.hash.length > 1) {
      // plain in-page anchor: activate the view containing the target
      const target = document.getElementById(location.hash.slice(1));
      const v = target && target.closest('[data-view]');
      if (v) {
        activateView(v.dataset.view);
        target.scrollIntoView();
        return;
      }
      if (activeView) return; // unknown anchor — keep the current view
    }
    activateView(homeId());
  }

  function activateView(id) {
    if (!views.size || !views.has(id)) return;
    activeView = id;
    for (const [vid, v] of views) v.el.classList.toggle('ex-view-active', vid === id);
    const trail = [];
    for (let cur = id; cur; cur = (views.get(cur) || {}).parent) trail.unshift(cur);
    if (trail[0] !== homeId()) trail.unshift(homeId());
    crumbs.innerHTML =
      `<button class="ex-nav-toggle" data-act="toggle-nav" title="${esc(S.toggleNav)}"` +
      ` aria-label="${esc(S.toggleNav)}">☰</button>` +
      trail
        .map((vid, i) =>
          i === trail.length - 1
            ? `<span class="ex-crumb-here">${esc(views.get(vid).title)}</span>`
            : `<a href="#/${encodeURIComponent(vid)}">${esc(views.get(vid).title)}</a>`
        )
        .join('<span class="ex-crumb-sep">›</span>');
    syncNav(id);
    paintHighlights();
  }

  // ---------- left nav: the view hierarchy ----------

  function setupNav() {
    let nav = document.getElementById('ex-nav');
    if (!nav) {
      // documents generated before this kit shipped the nav have no placeholder
      nav = document.createElement('aside');
      nav.id = 'ex-nav';
      (content.closest('.ex-layout') || content.parentElement).prepend(nav);
    }
    nav.innerHTML =
      `<div class="ex-nav-head">${esc(S.contents)}</div><nav class="ex-nav-tree"></nav>`;
    navTree = nav.querySelector('.ex-nav-tree');
    navTree.addEventListener('click', onNavClick);
    document.body.classList.add('ex-has-nav');
    try {
      if (localStorage.getItem('explain:nav-collapsed') === '1') {
        document.body.classList.add('ex-nav-collapsed');
      }
    } catch { /* storage unavailable */ }
    renderNav();
  }

  function toggleNav() {
    if (window.innerWidth <= 960) {
      document.body.classList.toggle('ex-nav-open');
      return;
    }
    const collapsed = document.body.classList.toggle('ex-nav-collapsed');
    try {
      localStorage.setItem('explain:nav-collapsed', collapsed ? '1' : '0');
    } catch { /* storage unavailable */ }
  }

  /* parent -> [child ids] in document order; a view whose data-parent is
   * missing hangs off home rather than disappearing from the tree */
  function navChildren() {
    const home = homeId();
    const kids = new Map();
    for (const [id, v] of views) {
      if (id === home) continue;
      const parent = v.parent && views.has(v.parent) ? v.parent : home;
      if (!kids.has(parent)) kids.set(parent, []);
      kids.get(parent).push(id);
    }
    return kids;
  }

  function renderNav() {
    if (!navTree) return;
    const kids = navChildren();
    const drawn = new Set(); // also breaks data-parent cycles
    const build = (id, depth) => {
      if (drawn.has(id) || !views.has(id)) return '';
      drawn.add(id);
      const children = kids.get(id) || [];
      const folded = navCollapsed.has(id);
      const twisty = children.length
        ? `<button class="ex-nav-twisty${folded ? '' : ' ex-nav-expanded'}"` +
          ` data-twisty="${esc(id)}" aria-label="${esc(S.toggleBranch)}"` +
          ` aria-expanded="${folded ? 'false' : 'true'}">▶</button>`
        : '<span class="ex-nav-twisty ex-nav-leaf"></span>';
      const cur = id === activeView ? ' ex-nav-current' : '';
      const sub = children.length && !folded
        ? `<ul>${children.map((c) => build(c, depth + 1)).join('')}</ul>`
        : '';
      return `<li><div class="ex-nav-item${cur}" style="--ex-nav-depth:${depth}">${twisty}` +
        `<a class="ex-nav-link" href="#/${encodeURIComponent(id)}"${cur ? ' aria-current="page"' : ''}>` +
        `${esc(views.get(id).title)}</a></div>${sub}</li>`;
    };
    navTree.innerHTML = `<ul>${build(homeId(), 0)}</ul>`;
    const here = navTree.querySelector('.ex-nav-current');
    if (here) {
      try { here.scrollIntoView({ block: 'nearest' }); } catch { /* older browsers */ }
    }
  }

  // keep the active view visible: unfold its ancestors, then repaint
  function syncNav(id) {
    if (!navTree) return;
    let cur = (views.get(id) || {}).parent;
    for (let hops = 0; cur && hops < 64; hops++) {
      navCollapsed.delete(cur);
      cur = (views.get(cur) || {}).parent;
    }
    renderNav();
  }

  function onNavClick(e) {
    const twisty = e.target.closest('[data-twisty]');
    if (twisty) {
      e.preventDefault();
      const id = twisty.dataset.twisty;
      if (!navCollapsed.delete(id)) navCollapsed.add(id);
      renderNav();
      return;
    }
    // links navigate via the hash; on narrow screens close the drawer after
    if (e.target.closest('.ex-nav-link') && window.innerWidth <= 960) {
      document.body.classList.remove('ex-nav-open');
    }
  }

  // ---------- data-example stepper (.ex-steps) ----------

  function setupSteps() {
    for (const box of content.querySelectorAll('.ex-steps')) {
      const steps = Array.from(box.querySelectorAll(':scope > .ex-step'));
      if (steps.length < 2) continue;
      box._steps = steps;
      const bar = document.createElement('div');
      bar.className = 'ex-steps-bar ex-ui';
      steps.forEach((s, i) => {
        const b = document.createElement('button');
        b.className = 'ex-step-tab';
        b.textContent = s.dataset.label || String(i + 1);
        b.addEventListener('click', () => activateStep(s));
        bar.appendChild(b);
      });
      const tog = document.createElement('button');
      tog.className = 'ex-step-tab ex-steps-toggle';
      tog.textContent = S.expandAll;
      tog.addEventListener('click', () => {
        const on = box.classList.toggle('ex-steps-expanded');
        tog.textContent = on ? S.tabView : S.expandAll;
      });
      bar.appendChild(tog);
      box.prepend(bar);
      activateStep(steps[0]);
    }
  }

  function activateStep(step) {
    const box = step.closest('.ex-steps');
    if (!box || !box._steps) return;
    const tabs = box.querySelectorAll(':scope > .ex-steps-bar > .ex-step-tab');
    box._steps.forEach((s, i) => {
      s.classList.toggle('ex-step-active', s === step);
      if (tabs[i]) tabs[i].classList.toggle('ex-step-tab-active', s === step);
    });
  }

  /* Make a node visible: switch to its view, open ancestor <details>,
   * activate its step — outermost first. Used when opening a comment
   * thread whose anchor sits inside collapsed/hidden content. */
  function revealForNode(node) {
    const actions = [];
    let el = node.nodeType === 1 ? node : node.parentElement;
    while (el && el !== content) {
      const cur = el;
      if (cur.tagName === 'DETAILS') actions.push(() => { cur.open = true; });
      if (cur.classList.contains('ex-step')) actions.push(() => activateStep(cur));
      if (cur.dataset.view) {
        actions.push(() => {
          if (activeView !== cur.dataset.view) {
            history.replaceState(null, '', '#/' + encodeURIComponent(cur.dataset.view));
            activateView(cur.dataset.view);
          }
        });
      }
      el = el.parentElement;
    }
    actions.reverse().forEach((f) => f());
  }

  // ---------- text index + anchoring ----------

  // JS-injected chrome (breadcrumbs, stepper tabs) must not pollute the
  // anchor text index, so both the index and offsetOf skip .ex-ui subtrees.
  function indexable(textNode) {
    const p = textNode.parentElement;
    return !p || !p.closest('.ex-ui');
  }

  function buildIndex() {
    const nodes = [];
    const nodeMap = new Map();
    let text = '';
    const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (indexable(n) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT),
    });
    let n;
    while ((n = walker.nextNode())) {
      nodes.push({ node: n, start: text.length });
      nodeMap.set(n, text.length);
      text += n.nodeValue;
    }
    idx = { text, nodes, nodeMap };
  }

  function offsetOf(node, off) {
    if (node.nodeType === 3 && idx.nodeMap.has(node)) return idx.nodeMap.get(node) + off;
    // element boundary: first indexed text node at/after it
    const r = document.createRange();
    r.setStart(node, off);
    r.collapse(true);
    for (const e of idx.nodes) {
      try {
        if (r.comparePoint(e.node, 0) >= 0) return e.start;
      } catch { /* incomparable node */ }
    }
    return idx.text.length;
  }

  function posToNode(pos) {
    const ns = idx.nodes;
    let lo = 0, hi = ns.length - 1, ans = 0;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (ns[mid].start <= pos) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    const e = ns[ans];
    return { node: e.node, offset: Math.min(pos - e.start, e.node.nodeValue.length) };
  }

  function rangeFromOffsets(start, end) {
    const r = document.createRange();
    const s = posToNode(start), e = posToNode(end);
    r.setStart(s.node, s.offset);
    r.setEnd(e.node, e.offset);
    return r;
  }

  function captureAnchor(range) {
    const start = offsetOf(range.startContainer, range.startOffset);
    const end = offsetOf(range.endContainer, range.endOffset);
    if (end <= start) return null;
    return {
      exact: idx.text.slice(start, end),
      prefix: idx.text.slice(Math.max(0, start - CTX_LEN), start),
      suffix: idx.text.slice(end, end + CTX_LEN),
    };
  }

  function commonSuffixLen(a, b) {
    let i = 0;
    while (i < a.length && i < b.length && a[a.length - 1 - i] === b[b.length - 1 - i]) i++;
    return i;
  }

  function commonPrefixLen(a, b) {
    let i = 0;
    while (i < a.length && i < b.length && a[i] === b[i]) i++;
    return i;
  }

  function locate(anchor) {
    if (!anchor || !anchor.exact) return null;
    let best = -1, bestScore = -1, from = 0, pos;
    while ((pos = idx.text.indexOf(anchor.exact, from)) !== -1) {
      let score = 0;
      if (anchor.prefix) {
        score += commonSuffixLen(idx.text.slice(Math.max(0, pos - CTX_LEN), pos), anchor.prefix);
      }
      if (anchor.suffix) {
        const after = idx.text.slice(pos + anchor.exact.length, pos + anchor.exact.length + CTX_LEN);
        score += commonPrefixLen(after, anchor.suffix);
      }
      if (score > bestScore) { bestScore = score; best = pos; }
      from = pos + 1;
    }
    if (best === -1) return null;
    try {
      return rangeFromOffsets(best, best + anchor.exact.length);
    } catch {
      return null;
    }
  }

  function relocate() {
    buildIndex();
    ranges = new Map();
    for (const t of data.threads) ranges.set(t.id, locate(t.anchor));
    paintHighlights();
  }

  // ---------- highlights ----------

  function paintHighlights() {
    if (!('highlights' in CSS)) return;
    const normal = new Highlight();
    const active = new Highlight();
    for (const t of data.threads) {
      if (t.status === 'resolved') continue;
      const r = ranges.get(t.id);
      if (!r) continue;
      (t.id === openId ? active : normal).add(r);
    }
    CSS.highlights.set('ex-hl', normal);
    CSS.highlights.set('ex-hl-active', active);
  }

  function caretFromPoint(x, y) {
    if (document.caretPositionFromPoint) {
      const p = document.caretPositionFromPoint(x, y);
      return p ? { node: p.offsetNode, offset: p.offset } : null;
    }
    if (document.caretRangeFromPoint) {
      const r = document.caretRangeFromPoint(x, y);
      return r ? { node: r.startContainer, offset: r.startOffset } : null;
    }
    return null;
  }

  // ---------- selection -> fab -> popover ----------

  let fab = null, popover = null, pendingAnchor = null;

  function removeFloating() {
    if (fab) { fab.remove(); fab = null; }
    if (popover) { popover.remove(); popover = null; }
  }

  function onMouseUp(e) {
    if (e.target.closest && (e.target.closest('#ex-sidebar') || e.target.closest('.ex-popover') || e.target.closest('.ex-fab'))) return;
    setTimeout(() => {
      const sel = window.getSelection();
      if (fab) { fab.remove(); fab = null; }
      if (popover) return;
      if (!sel || sel.isCollapsed || sel.rangeCount === 0) return;
      const range = sel.getRangeAt(0);
      if (!content.contains(range.commonAncestorContainer)) return;
      if (!range.toString().trim()) return;
      pendingAnchor = captureAnchor(range);
      if (!pendingAnchor) return;
      const rect = range.getBoundingClientRect();
      fab = document.createElement('button');
      fab.className = 'ex-fab';
      fab.textContent = '💬 ' + S.addComment;
      fab.style.left = Math.min(window.scrollX + rect.right + 6, window.scrollX + window.innerWidth - 140) + 'px';
      fab.style.top = (window.scrollY + rect.bottom + 6) + 'px';
      fab.addEventListener('click', openPopover);
      document.body.appendChild(fab);
    }, 0);
  }

  function openPopover() {
    const rect = fab.getBoundingClientRect();
    fab.remove();
    fab = null;
    popover = document.createElement('div');
    popover.className = 'ex-popover';
    popover.innerHTML =
      `<textarea placeholder="${esc(S.placeholder)}"></textarea>` +
      `<div class="ex-popover-actions">` +
      `<button class="ex-btn" data-act="cancel">${esc(S.cancel)}</button>` +
      `<button class="ex-btn ex-btn-primary" data-act="send">${esc(S.send)}</button></div>`;
    popover.style.left = Math.min(window.scrollX + rect.left, window.scrollX + window.innerWidth - 340) + 'px';
    popover.style.top = (window.scrollY + rect.top) + 'px';
    document.body.appendChild(popover);
    const ta = popover.querySelector('textarea');
    ta.focus();
    popover.addEventListener('click', async (e) => {
      const act = e.target.dataset && e.target.dataset.act;
      if (act === 'cancel') removeFloating();
      if (act === 'send') {
        const body = ta.value.trim();
        if (!body) return;
        try {
          const res = await req('POST', '/threads', { anchor: pendingAnchor, body });
          openId = res.thread.id;
          removeFloating();
          window.getSelection().removeAllRanges();
          await refresh();
        } catch { /* keep the popover so the text isn't lost */ }
      }
    });
    ta.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) popover.querySelector('[data-act=send]').click();
      if (e.key === 'Escape') removeFloating();
    });
  }

  function onContentClick(e) {
    if (e.target.closest('a, button, summary, [data-goto], .ex-ui')) return;
    if (window.getSelection().toString()) return;
    const pos = caretFromPoint(e.clientX, e.clientY);
    if (!pos) return;
    for (const t of data.threads) {
      if (t.status === 'resolved') continue;
      const r = ranges.get(t.id);
      try {
        if (r && r.isPointInRange(pos.node, pos.offset)) {
          openThread(t.id);
          document.body.classList.add('ex-sidebar-open');
          return;
        }
      } catch { /* node not comparable */ }
    }
  }

  // ---------- sidebar ----------

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function fmtTime(iso) {
    try {
      return new Date(iso).toLocaleString(CFG.lang, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    } catch {
      return iso;
    }
  }

  function unseenCount(t) {
    return t.messages.filter((m) => m.author === 'agent' && !m.seen_by_user).length;
  }

  // message bodies are plain text, but #/view mentions become view links and
  // file:line references render as code
  function renderBody(s) {
    let h = esc(s);
    h = h.replace(/#\/([a-zA-Z0-9._-]+)/g, (m, id) =>
      views.has(id) ? `<a href="#/${id}">${m}</a>` : m
    );
    h = h.replace(/(^|[\s(])([\w@~./-]+\.[A-Za-z]{1,4}:\d+)/g, (m, pre, ref) => `${pre}<code>${ref}</code>`);
    return h;
  }

  // the view a document-level conversation was started from — a hint the
  // reader opted into, so it links but never anchors
  function contextChip(t) {
    const c = t.context;
    if (!c || !c.view) return '';
    const label = esc((c.title || c.view).slice(0, 40));
    return views.has(c.view)
      ? `<a class="ex-ctx-chip" href="#/${encodeURIComponent(c.view)}">${label}</a>`
      : `<span class="ex-ctx-chip ex-ctx-gone">${label}</span>`;
  }

  function threadCard(t) {
    const open = t.id === openId;
    const anchored = !!t.anchor;
    const orphan = anchored && !ranges.get(t.id);
    const chips = [];
    if (t.status === 'unread') chips.push(`<span class="ex-chip ex-chip-unread">${esc(S.unread)}</span>`);
    if (unseenCount(t)) chips.push(`<span class="ex-chip ex-chip-new">${esc(S.newReply)}</span>`);
    if (orphan) chips.push(`<span class="ex-chip ex-chip-orphan">${esc(S.orphaned)}</span>`);
    if (t.status === 'resolved') chips.push(`<span class="ex-chip">${esc(S.resolved)}</span>`);

    /* An anchor stays useful while the thread is open — it says what the
     * comment is about. A conversation's head line is its opening message,
     * which the expanded body repeats, so it gives way to a spacer. */
    let head;
    if (anchored) {
      head = `<span class="ex-excerpt">“${esc(t.anchor.exact.slice(0, 90))}”</span>`;
    } else if (open) {
      head = '<span class="ex-spacer"></span>';
    } else {
      const first = (t.messages[0] || {}).body || '';
      head = `<span class="ex-preview">${esc(first.slice(0, 120))}</span>`;
    }

    let inner = `<div class="ex-thread-top">${contextChip(t)}${head}${chips.join('')}</div>`;

    if (open) {
      inner += t.messages.map((m) => {
        const who = m.author === 'agent' ? `<span class="ex-msg-author ex-agent">${esc(S.agent)}</span>` : `<span class="ex-msg-author">${esc(S.you)}</span>`;
        const edited = m.edited_at ? ` · ${esc(S.edited)}` : '';
        const tools = m.author === 'user'
          ? `<span class="ex-msg-tools"><button class="ex-btn-ghost" data-act="edit-msg" data-mid="${m.id}">${esc(S.edit)}</button>` +
            `<button class="ex-btn-ghost" data-act="del-msg" data-mid="${m.id}">${esc(S.delete)}</button></span>`
          : '';
        const body = editingId === m.id
          ? `<textarea data-edit="${m.id}">${esc(m.body)}</textarea>` +
            `<div class="ex-popover-actions"><button class="ex-btn" data-act="cancel-edit">${esc(S.cancel)}</button>` +
            `<button class="ex-btn ex-btn-primary" data-act="save-edit" data-mid="${m.id}">${esc(S.save)}</button></div>`
          : `<div class="ex-msg-body">${renderBody(m.body)}</div>`;
        return `<div class="ex-msg"><div class="ex-msg-head">${who}<span>${esc(fmtTime(m.created_at))}${edited}</span>${tools}</div>${body}</div>`;
      }).join('');

      inner += `<div class="ex-msg"><textarea data-reply placeholder="${esc(S.replyPlaceholder)}"></textarea></div>`;
      const resolveBtn = t.status === 'resolved'
        ? `<button class="ex-btn" data-act="reopen">${esc(S.reopen)}</button>`
        : `<button class="ex-btn" data-act="resolve">${esc(S.resolve)}</button>`;
      const gotoBtn = ranges.get(t.id)
        ? `<button class="ex-btn" data-act="goto">${esc(S.goToAnchor)}</button>`
        : '';
      inner += `<div class="ex-thread-actions">${gotoBtn}${resolveBtn}` +
        `<button class="ex-btn-ghost" data-act="del-thread">${esc(S.delete)}</button><span class="ex-spacer"></span>` +
        `<button class="ex-btn ex-btn-primary" data-act="reply">${esc(S.reply)}</button></div>`;
    }
    return `<div class="ex-thread${open ? ' ex-open' : ''}" data-tid="${t.id}">${inner}</div>`;
  }

  /* Conversation recency, taken from the last message rather than the
   * thread's updated_at: the server bumps updated_at on 'seen' and
   * resolve/reopen too, so merely reading a thread would jump it to the top. */
  function threadTime(t) {
    const msgs = t.messages || [];
    const last = msgs.length ? msgs[msgs.length - 1] : null;
    return Date.parse((last && last.created_at) || t.created_at || '') || 0;
  }

  function composerHtml() {
    if (!composerOpen) return '';
    // no views, nothing to include: the whole document is the page
    const check = views.size
      ? `<label class="ex-check"><input type="checkbox" data-act="incl-view"` +
        `${composerIncludeView ? ' checked' : ''}>${esc(S.includePage)}</label>`
      : '';
    return `<div class="ex-composer">` +
      `<textarea data-new placeholder="${esc(S.newPlaceholder)}">${esc(composerDraft)}</textarea>` +
      `<div class="ex-composer-actions">${check}<span class="ex-spacer"></span>` +
      `<button class="ex-btn" data-act="cancel-new">${esc(S.cancel)}</button>` +
      `<button class="ex-btn ex-btn-primary" data-act="send-new">${esc(S.send)}</button>` +
      `</div></div>`;
  }

  // `force` renders even from a focused textarea — opening or closing the
  // composer is itself a keyboard action and must not be deferred
  function render(force) {
    if (!force && sidebar.contains(document.activeElement) &&
        (document.activeElement.tagName === 'TEXTAREA' || document.activeElement.tagName === 'INPUT')) {
      renderPending = true;
      return;
    }
    renderPending = false;

    const threads = data.threads.slice().sort((a, b) => threadTime(b) - threadTime(a));
    const active = threads.filter((t) => t.status !== 'resolved');
    const resolved = threads.filter((t) => t.status === 'resolved');
    const totalUnseen = threads.reduce((n, t) => n + unseenCount(t), 0);

    let html = `<div class="ex-side-head"><div class="ex-side-title">` +
      `<span class="ex-dot ${watched ? 'ex-dot-on' : 'ex-dot-off'}"></span>` +
      `<h2>${esc(S.panelTitle)}</h2>` +
      `<button class="ex-btn ex-new-btn" data-act="new-thread">＋ ${esc(S.newConversation)}</button>` +
      `</div>${composerHtml()}</div>`;
    if (docMeta && (docMeta.commit || docMeta.updated_at)) {
      const commit = docMeta.commit ? `<code>${esc(String(docMeta.commit).slice(0, 12))}</code> · ` : '';
      html += `<div class="ex-side-meta">${esc(S.generatedFrom)} ${commit}${esc(fmtTime(docMeta.updated_at || docMeta.created_at))}</div>`;
    }
    if (disconnected) html += `<div class="ex-banner ex-banner-warn">${esc(S.disconnected)}</div>`;
    if (docUpdated) html += `<div class="ex-banner ex-banner-warn">${esc(S.docUpdated)}<br><button class="ex-btn" data-act="reload-doc">${esc(S.refresh)}</button></div>`;
    if (!disconnected) html += `<div class="ex-banner ex-banner-info">${esc(watched ? S.watching : S.notWatching)}</div>`;

    if (!data.threads.length) {
      html += `<div class="ex-empty">${esc(S.empty)}</div>`;
    } else {
      html += active.map(threadCard).join('');
      if (resolved.length) {
        html += `<details class="ex-resolved-group"${resolved.some((t) => t.id === openId) ? ' open' : ''}>` +
          `<summary>${esc(S.resolvedGroup)} (${resolved.length})</summary>` +
          resolved.map(threadCard).join('') + `</details>`;
      }
    }
    sidebar.innerHTML = html;
    toggleBtn.textContent = '💬' + (totalUnseen ? ` ${totalUnseen}` : '');
    paintHighlights();
  }

  function revealAnchor(id) {
    const r = ranges.get(id);
    if (!r) return false;
    revealForNode(r.startContainer);
    const rect = r.getBoundingClientRect();
    if (rect.top < 0 || rect.bottom > window.innerHeight) {
      window.scrollTo({ top: window.scrollY + rect.top - window.innerHeight / 3, behavior: 'smooth' });
    }
    return true;
  }

  function openThread(id) {
    openId = id;
    editingId = null;
    render();
    const t = data.threads.find((x) => x.id === id);
    revealAnchor(id);
    if (t && unseenCount(t)) {
      req('PATCH', `/threads/${t.id}`, { action: 'seen' }).then(refresh).catch(() => {});
    }
  }

  function openComposer() {
    composerOpen = true;
    composerIncludeView = false;
    render(true);
    const ta = sidebar.querySelector('[data-new]');
    if (ta) ta.focus();
  }

  function closeComposer() {
    composerOpen = false;
    composerDraft = '';
    render(true);
  }

  async function submitNewThread() {
    const ta = sidebar.querySelector('[data-new]');
    const body = (ta ? ta.value : composerDraft).trim();
    if (!body) return;
    const payload = { body };
    if (composerIncludeView && activeView && views.has(activeView)) {
      payload.context = { view: activeView, title: views.get(activeView).title };
    }
    try {
      const res = await req('POST', '/threads', payload);
      composerOpen = false;
      composerDraft = '';
      openId = res.thread.id;
      // forced: on the Cmd+Enter path the composer's textarea still holds
      // focus, and an unforced render would defer until it blurs
      await refresh(true);
    } catch { /* keep the composer so the draft isn't lost */ }
  }

  async function onSidebarClick(e) {
    const actEl = e.target.closest('[data-act]');
    const card = e.target.closest('.ex-thread');
    if (!actEl) {
      // re-clicking an open thread's excerpt re-reveals its anchor (view switch + scroll)
      if (e.target.closest('a')) return; // context chip, view link in a message
      if (card && (card.dataset.tid !== openId || e.target.closest('.ex-thread-top'))) {
        openThread(card.dataset.tid);
      }
      return;
    }
    const act = actEl.dataset.act;
    // panel-level actions, outside any thread card
    if (act === 'reload-doc') { location.reload(); return; }
    if (act === 'new-thread') { openComposer(); return; }
    if (act === 'cancel-new') { closeComposer(); return; }
    if (act === 'send-new') { await submitNewThread(); return; }
    // record the checkbox without re-rendering — that would drop the draft's caret
    if (act === 'incl-view') { composerIncludeView = actEl.checked; return; }
    if (!card) return;
    const tid = card.dataset.tid;
    try {
      if (act === 'reply') {
        const ta = card.querySelector('[data-reply]');
        const body = ta && ta.value.trim();
        if (!body) return;
        await req('POST', `/threads/${tid}/messages`, { author: 'user', body });
        await refresh();
      } else if (act === 'goto') {
        revealAnchor(tid);
        // on narrow screens the sidebar drawer covers the content — close it
        if (window.innerWidth <= 960) document.body.classList.remove('ex-sidebar-open');
      } else if (act === 'resolve' || act === 'reopen') {
        await req('PATCH', `/threads/${tid}`, { action: act });
        if (act === 'resolve') openId = null;
        await refresh();
      } else if (act === 'del-thread') {
        if (!confirm(S.confirmDelete)) return;
        await req('DELETE', `/threads/${tid}`);
        openId = null;
        await refresh();
      } else if (act === 'del-msg') {
        if (!confirm(S.confirmDelete)) return;
        await req('DELETE', `/threads/${tid}/messages/${actEl.dataset.mid}`);
        await refresh();
      } else if (act === 'edit-msg') {
        editingId = actEl.dataset.mid;
        render();
      } else if (act === 'cancel-edit') {
        editingId = null;
        render();
      } else if (act === 'save-edit') {
        const ta = card.querySelector(`[data-edit="${actEl.dataset.mid}"]`);
        const body = ta && ta.value.trim();
        if (!body) return;
        await req('PATCH', `/threads/${tid}/messages/${actEl.dataset.mid}`, { body });
        editingId = null;
        await refresh();
      }
    } catch { /* next poll reconciles */ }
  }

  // ---------- polling ----------

  async function poll() {
    try {
      const st = await req('GET', '/state');
      if (disconnected) { disconnected = false; render(); }
      if (initialEtag === null) initialEtag = st.doc_etag;
      else if (st.doc_etag !== initialEtag && !docUpdated) { docUpdated = true; render(); }
      if (st.watched !== watched) { watched = st.watched; render(); }
      if (st.rev !== data.rev) await refresh();
    } catch {
      if (!disconnected) { disconnected = true; render(); }
    }
    setTimeout(poll, 2500);
  }

  // ---------- init ----------

  function init() {
    content = document.getElementById('explain-content');
    sidebar = document.getElementById('ex-sidebar');
    if (!content || !sidebar) return;

    toggleBtn = document.createElement('button');
    toggleBtn.className = 'ex-sidebar-toggle';
    toggleBtn.textContent = '💬';
    toggleBtn.addEventListener('click', () => document.body.classList.toggle('ex-sidebar-open'));
    document.body.appendChild(toggleBtn);

    setupViews();
    setupSteps();
    content.addEventListener('click', (e) => {
      const g = e.target.closest('[data-goto]');
      if (g && views.has(g.dataset.goto)) {
        e.preventDefault();
        location.hash = '#/' + encodeURIComponent(g.dataset.goto);
      }
    });

    document.addEventListener('mouseup', onMouseUp);
    content.addEventListener('click', onContentClick);
    sidebar.addEventListener('click', onSidebarClick);
    sidebar.addEventListener('focusout', () => {
      setTimeout(() => { if (renderPending) render(); }, 100);
    });
    // mirror the draft so a poll-triggered re-render can't swallow it
    sidebar.addEventListener('input', (e) => {
      if (e.target.matches('[data-new]')) composerDraft = e.target.value;
    });
    sidebar.addEventListener('keydown', (e) => {
      if (!e.target.matches || !e.target.matches('[data-new]')) return;
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); submitNewThread(); }
      if (e.key === 'Escape') closeComposer();
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') removeFloating(); });

    fetch(`/${encodeURIComponent(CFG.slug)}/doc.json`)
      .then((r) => (r.ok ? r.json() : null))
      .then((m) => { docMeta = m; render(); })
      .catch(() => {});

    buildIndex();
    render();
    poll();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
