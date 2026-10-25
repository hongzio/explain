---
name: explain
description: >-
  Generate a visual HTML explanation (inline SVG diagrams, interactive
  sections) of a diff, a module/path, the whole repo, or a free question
  about the codebase; serve it on localhost from the project's .explain/
  directory and run a live conversation loop in which the agent answers the
  reader's comments and questions in near real time. Invoked explicitly
  only: /explain (Claude Code) or $explain (Codex).
disable-model-invocation: true
metadata:
  short-description: Visual HTML code explanations with a live comment loop
---

# explain

Produce a visual HTML explanation of code, serve it locally, open it in the
browser, and then answer the reader's comments as they arrive.

Definitions used below:

- `SKILL_DIR` — the directory containing this SKILL.md (you know its absolute
  path from how the skill was loaded). Scripts and assets are referenced
  relative to it. Do not rely on agent-specific variables.
- `PROJECT` — the project root (normally the current working directory).
- `ROOT` — `PROJECT/.explain`, where documents, comments, past versions, and
  server state live.

Requirements: `python3` ≥ 3.11 on PATH (stdlib only, no packages). Check with
`python3 --version` if you have any doubt.

## 1. Interpret the request

The argument is free text. Classify it:

| Argument looks like | Kind | Notes |
|---|---|---|
| a path (`src/auth/`, `pkg/db.go`) | `module` | explain that module/file |
| a git rev or range (`HEAD~3`, `main..feature`, working-tree diff, PR ref) | `diff` | explain the change; the doc is an immutable snapshot |
| empty | infer | pick the most recently discussed code in the conversation; if nothing plausible, ask |
| anything else (“how does auth flow work?”) | `question` | answer it as a document grounded in the code |
| “resume/continue X”, “catch up on comments” | resume | skip generation, go to steps 4–6 for the existing doc |
| “stop watching” | stop | see §7 |

Slug: derive a short lowercase slug matching `[a-z0-9][a-z0-9._-]*` from the
target (e.g. `src-auth`, `diff-main..feature-abc123`, `auth-flow`). For
`module`/`repo`/`question` targets, re-explaining the same target updates the
SAME slug in place (living document). For `diff`, include enough of the rev
range/short hashes in the slug that each diff gets its own immutable doc.

Language: write the document in the language the user is conversing in
(default English). An explicit `--lang <code>` in the argument overrides.
Comment replies instead follow the language of the comment they answer.

## 2. Generate the document

1. Investigate the target (read the code / diff as needed).
2. Read `SKILL_DIR/assets/template.html`, replace `{{TITLE}}`, `{{LANG}}`
   (BCP-47 code, e.g. `en`, `ko`), `{{SLUG}}`, and `{{CONTENT}}`, and write it
   to `ROOT/<slug>/index.html`. Everything you author goes inside
   `{{CONTENT}}`; do not alter the surrounding skeleton.
3. Write `ROOT/<slug>/doc.json`:
   `{"title", "kind", "target", "lang", "created_at", "updated_at",
   "commit", "sources"}` — ISO timestamps (keep `created_at` when updating);
   `commit` = `git rev-parse --short HEAD` of the project (null outside git);
   `sources` = `{path: sha256-12}` for the files the explanation is based on
   (`shasum -a 256` first 12 hex chars). These power freshness tracking:
   the page shows the commit stamp to the reader. For `diff` docs also
   record `coverage`: `{"hunks_total": N, "hunks_covered": M, "uncovered":
   ["path", …]}` and state that coverage plainly in the home view — an
   uncovered hunk the reader doesn't know about is worse than one they do.
4. Validate before serving:
   `python3 "SKILL_DIR/scripts/validate_doc.py" "ROOT/<slug>"` — fix every
   ERROR. Beyond structure, this VERIFIES EVIDENCE against the project:
   `data-src` files must exist, code excerpts must appear verbatim in them,
   and `file:line` citations must point at real files and in-range lines —
   fabricated or drifted evidence fails the build. Read the WARNs and act
   on the reasonable ones.
5. Adversarial pass: before announcing the doc, have it refuted. On Claude
   Code spawn a subagent (Explore) with the doc's HTML and the cited source
   files, instructed to find claims that misread the code, wrong values in
   data examples, and important behavior the doc omits; elsewhere, re-read
   the doc yourself against the sources with that same brief. Fix what
   survives scrutiny, re-validate, then serve. The validator proves the
   evidence is real; this pass is what checks the narrative around it.
6. Record the version — after every write of `index.html`, including the
   first. Needs the server, so on a new document this comes right after §3;
   on an update you are already serving, so do it as soon as the write lands:
   ```sh
   curl -s -X POST <url>/api/docs/<slug>/versions \
     -H 'Content-Type: application/json' \
     -d '{"summary": "…", "threads": ["t-…"]}'
   ```
   `summary` is ONE sentence in the document's language saying **what
   changed** (“first pass” for a new document). `threads` lists the thread
   ids whose feedback prompted the change — that is what lets a reader get
   from a version back to the conversation that caused it. The snapshot
   itself happens with or without you (the server takes one when it sees the
   file move, labelled “auto-recorded”); these two fields are the entire
   reason the entry is worth reading. The reader gets the before/after
   comparison from the page — never diff anything yourself.
7. Never write `comments.json` or `versions.json` yourself — the server owns
   both.
8. **Updating an existing doc** (code changed / “update it”): hash the
   current `sources` files and compare with `doc.json`. Rewrite only the
   sections whose evidence drifted; keep other sections verbatim (comment
   anchors survive on unchanged wording — unlocatable ones show as “lost
   anchor” instead of being dropped). If you know a section is outdated but
   are not rewriting it now, append `<span class="ex-stale">stale</span>` to
   its heading. Refresh `commit`, `sources`, `updated_at`.

### Authoring guide (inside {{CONTENT}})

Structure the document as a **drill-down pyramid, not an article**. The
reader lands on a map of the big structure and clicks into detail, down to
the code. Nobody scrolls a wall of text: if a view is mostly prose, turn it
into a diagram, a data example, a table, or split it. Paragraphs ≤ 3
sentences; a view should fit in roughly two screens.

**Views (drill-down pages).** Wrap each page in
`<section data-view="id" data-title="Short label" data-parent="parentid">`.
Exactly one view has no `data-parent` — that's the home view. Breadcrumbs and
a collapsible left sidebar listing the whole view tree render automatically,
both labelled from `data-title`, so keep those labels short and meaningful.
Search (the magnifier at the right of the breadcrumb bar) reads the whole
document, hidden views included, and labels every result with that same
`data-title` path — so a vague label costs the reader twice.
Navigate with `<a href="#/id">` or `data-goto="id"` on any element (including
SVG nodes wrapped in `<a href="#/id">`).

- **L0 home**: one big-structure diagram (every node clickable, linking to
  its L1 view) and/or an `.ex-cards` grid of `<a class="ex-card"
  href="#/id"><h3>…</h3><p>one-liner</p></a>`. At most one short paragraph.
- **L1 concept/module views**: how this part works — lead with a diagram or
  data example, prose second. Link down to L2 where code matters.
- **L2 code views**: actual code as walkthroughs (below), with file:line
  named. This is the floor — the reader must be able to reach real code.

**Evidence rules** — every substantive claim names its evidence: mention
`file.py:12` style references inline, and when a passage hinges on specific
code, embed the VERBATIM excerpt (walkthrough or `<details>`), not a
paraphrase — the excerpt is the reader's trust anchor and the staleness
canary for updates. Specificity is the anti-slop defense: real identifiers,
real paths, real values from THIS codebase, never prose that could describe
any system.

**Data examples (`.ex-steps`)** — every non-obvious piece of logic gets one
concrete example traced end-to-end, rendered as a tabbed stepper (readers
can also toggle “expand all” to read the steps stacked, so write steps that
read coherently in sequence):

```html
<div class="ex-steps">
  <div class="ex-step" data-label="Input">
    <p>An order arrives:</p>
    <pre class="ex-data">{"item": "book", "qty": 2, "coupon": "SAVE10"}</pre>
  </div>
  <div class="ex-step" data-label="Validate">…</div>
  <div class="ex-step" data-label="Result">…</div>
</div>
```

Use realistic values and carry the SAME example through every step so the
reader watches the data transform. When you can actually execute the
example (run a snippet, a REPL, or a test that produces these values), do
it and mark the block `<pre class="ex-data" data-verified="executed">` — it
renders a “✓ executed” chip. Never mark values you only derived by hand;
unmarked blocks honestly read as reasoning, marked ones as evidence.

**Code walkthroughs (`.ex-walkthrough`)** — assume the reader is NOT a
developer in this language and does not read code comments. Split code into
chunks, each paired with a plain-language note in domain terms (reusing the
running example where one exists):

```html
<div class="ex-walkthrough" data-src="src/pricing.py">
  <div class="ex-chunk">
    <pre><code>coupon = db.find_coupon(code)</code></pre>
    <div class="ex-note">Looks the coupon code up in the database — for our
    order, "SAVE10" is found and gives 10% off.</div>
  </div>
</div>
```

`data-src` (project-relative path) is REQUIRED on every walkthrough — the
validator checks each chunk's code appears verbatim in that file (a chunk
from a different file can override with its own `data-src`). Copy excerpts
exactly; paraphrased code fails validation. Never paste a code block
without notes. Long boilerplate goes in `<details>` (give its `<pre>` a
`data-src` too so it is verified).

**Diagrams** — hand-authored inline `<svg>` inside
`<figure class="diagram" data-asks="…">…<figcaption>`: architecture
boxes-and-arrows, flows, sequences, state machines. `data-asks` is the
question the drawing answers, in the reader's words; it renders above the
figure and is the test of whether the figure earns its place. Use `viewBox`
(no fixed width), theme tokens for colors (`var(--ex-fg)`, `var(--ex-muted)`,
`var(--ex-line)`, `var(--ex-accent)`; fills with `var(--ex-surface)` or
`var(--ex-panel)`, raised elements with `var(--ex-elevated)`), `font-size`
≥ 12, short labels. A diagram must show real structure from the code, not
decoration. Prefer a **task-scoped trace** (one flow, its actual call path)
over a whole-repo map — whole-repo graphs overwhelm and mislead; top-down
layout for wide trees, left-to-right for deep chains.

**Annotate the nodes; don't caption them from a distance.** Mark every
meaningful node `class="ex-node" data-node="id"`, give it `data-ref="path:line"`
for the code it stands for and `data-goto="viewid"` where a deeper view
exists, and write its note as a row of the figure's legend:

```html
<figure class="diagram" data-asks="How does a comment reach the agent?">
  <svg viewBox="0 0 440 120">
    <g class="ex-node" data-node="watch" data-ref="scripts/watch.py:88" data-goto="watch">
      <rect x="10" y="30" width="120" height="44" rx="8"
            fill="var(--ex-surface)" stroke="var(--ex-line-strong)"/>
      <text x="70" y="57" text-anchor="middle" font-size="13" fill="var(--ex-fg)">watch.py</text>
    </g>
  </svg>
  <figcaption>A comment reaches the session through a file watcher, not the server.
    <ol class="ex-legend">
      <li data-for="watch">Polls the document's comments and wakes the session
      that owns it — scripts/watch.py:88.</li>
    </ol>
  </figcaption>
</figure>
```

Hovering or tab-focusing a node raises its legend row over the node itself
and lights the row. The note stays in the document, so it is searchable,
quotable and readable with JS off — a note written into an attribute is
invisible to both search and comments, so don't. Every `.ex-node` needs a
legend row and every legend row needs a node; the validator checks both ways.

**Layer a diagram instead of shrinking it.** Up to 12 nodes, draw one
figure. Past that, split the drawing into cumulative layers and let the
reader add them a press at a time:

```html
<figure class="diagram" data-asks="How does a document get served?" data-layers>
  <svg viewBox="0 0 520 200">
    <g data-layer="0">…the skeleton: the boxes the answer cannot do without…</g>
    <g data-layer="1" data-layer-label="Freshness check">…</g>
    <g data-layer="2" data-layer-label="Version snapshot">…</g>
  </svg>
  <figcaption>…</figcaption>
</figure>
```

Layer 0 is what shows on load and must answer the view's headline on its own
— layering paces the detail, it never hides the point. Indices run 0..n with
no gaps, every layer past 0 carries a `data-layer-label`, each layer adds
≤ 6 nodes, and the whole figure stays ≤ 24. Nothing animates: the reader's
own pacing is the mechanism; motion is not, and for a reader meeting this
code for the first time it costs more than it gives.

**Predict then reveal (`.ex-predict`)** — at the two or three points where a
reader's reasonable guess is WRONG, ask before you tell:

```html
<div class="ex-predict">
  <p class="ex-ask">Two sessions have the same document open. Which one answers
  a new comment?</p>
  <ul class="ex-choices">
    <li>Both — each session sees the comment
      <span class="ex-why">Both do see it, but the watcher checks ownership
      first, so only one of them replies.</span></li>
    <li data-correct>Whichever session took the watch over last
      <span class="ex-why">Ownership is a claim the newest watcher writes —
      scripts/watch.py:120.</span></li>
  </ul>
  <div class="ex-reveal">
    <p>…the answer, with the excerpt that proves it…</p>
  </div>
</div>
```

This is the component that is easiest to overuse, so:

- **Two or three per document, never more.** The gain is specific to the
  question asked — asking buys nothing for the material you didn't ask
  about, so one question at a real misconception beats five on easy facts.
- **Every choice answers back** (`.ex-why`, on the correct one too). A guess
  with nothing said back teaches nothing.
- **Never a view's lead element.** The view's point comes first and the
  question deepens it; the validator rejects a view that opens with one.
- The reader can always press straight past it, so never write prose that
  assumes they answered.

**Before/after contrast (`.ex-contrast`)** — for diff documents: the two
states side by side with ONE difference between the panes, and a note saying
what that difference means. A sequential before-then-after loses the
comparison that makes the change legible.

```html
<div class="ex-contrast">
  <div class="ex-side" data-label="Before">
    <pre data-src="scripts/watch.py" data-rev="HEAD~1">…</pre>
  </div>
  <div class="ex-side" data-label="After">
    <pre data-src="scripts/watch.py">…</pre>
  </div>
  <div class="ex-note">The watcher now claims the document before replying, so
  a second session stops answering the same comment.</div>
</div>
```

`data-rev` is a git revision: the validator verifies that pane's excerpt with
`git show <rev>:<path>`, which is what holds "before" code to the same
verbatim standard as working-tree code. Both panes come first, the note last.

**Toolbox summary**: views + breadcrumbs + hierarchy sidebar + search,
annotated and layered SVG diagrams, clickable cards, `.ex-steps`,
`.ex-walkthrough`, `.ex-predict`, `.ex-contrast`, `<details>`, tables. These
are plain HTML conventions that `assets/explain.js` enhances — write no
`<script>` of your own, and no external URLs, images, or fonts (only the
`/assets/…` references already in the template).

### Quality gates

Before authoring, commit to a one-paragraph plan: the view tree (ids,
titles, parents), the ONE worked example you will trace through the whole
document, a diagram inventory (each diagram: the question it answers, what
real structure it shows, which views its nodes drill into, and its layers if
it needs them), and the two or three places you will ask before telling —
naming, for each, the wrong answer a reasonable reader would give. Then
author against it. The inventory is where a decorative diagram gets caught:
if you cannot write the question a figure answers, it has none.

Anti-patterns — if the document matches one of these, it is wrong:

- A stepper for non-sequential content (alternatives, comparisons → cards
  or a table; steppers are only for one thing transforming over time).
- A whole-repo architecture graph; any unlayered diagram over 12 nodes, or
  a layered one over 24.
- A code block with no walkthrough notes (outside `<details>` boilerplate).
- Prose that would be true of any codebase — no real identifiers, paths,
  or values in a paragraph is the tell.
- Raw hex/rgb colors instead of `var(--ex-*)` tokens (validator catches it).
- Emoji as section markers or bullet decoration; decorative diagrams that
  answer no question.
- A view whose main point is buried behind an interaction: each view leads
  with its diagram/example/claim; interactions deepen and drill, they don't
  hide the headline. Layer 0 and the text around a prediction are the two
  places this goes wrong.
- A metaphor or analogy drawing — a pipeline as plumbing, a queue as a
  conveyor belt. Readers skip them as unserious and go back to the code;
  draw the structure the code actually has instead.
- Anything that moves on its own: animated transitions, auto-advancing
  steppers or layers. The reader sets the pace.
- A prediction in every section, or one the reader cannot skip; a diagram
  node whose note is an attribute rather than a legend row.

After serving, do a render inspection: open the page (browser tooling if
available — check each view for overflow, collisions, dead space, unreadable
diagram labels; fix and refresh). Exercise the interactions too: hover a
diagram node and check the note lands over the drawing rather than off its
edge, step a layered figure from 0 to the end, and answer one prediction
wrongly to see that the feedback reads as an answer. The validator checks the
computable parts; only eyes catch layout.

## 3. Serve and open

```sh
python3 "SKILL_DIR/scripts/server.py" start --root "PROJECT/.explain" --open --doc <slug>
```

Prints JSON: `{"url", "host", "port", "pid", "already_running",
"restarted_stale", "restarted_for_port", "restarted_for_host", "exposed",
"opened"}`. The server is one-per-project, binds 127.0.0.1 by default (port
derived from the project path, so it's stable), is idempotent to start, and
shuts itself down when no session lease is fresh and no HTTP request has
arrived for a few minutes.

Add `--port <n>` to pin the port instead — for a port the user has already
forwarded or bookmarked. An explicit port is a requirement, not a hint: if it
is taken, `start` prints `{"error": "port <n> is unavailable"}` and exits 1
rather than quietly landing somewhere else, and a daemon already serving this
root from another port is retired and relaunched there
(`restarted_for_port: true`), which drops any open page onto a dead URL until
it reloads. Without `--port`, an occupied derived port still falls back to a
free one.

Add `--host <addr>` to bind somewhere other than 127.0.0.1 — `0.0.0.0` to
reach the page from a phone or another machine, a specific interface address
to limit it to that one. It behaves like `--port`: an address that cannot be
bound fails with `{"error": "cannot bind <addr>; see .explain/server.log"}`
and exit 1, and a daemon serving this root on another address is retired and
relaunched (`restarted_for_host: true`). `url` stays a loopback URL for a
wildcard bind, since `0.0.0.0` is a bind instruction, not a destination.

**A non-loopback bind exposes writes.** The HTTP API has no authentication, so
anyone who can reach the port can post comments and replies as the user.
`start` reports `exposed: true` and the daemon logs a warning; say so in chat
when the user asks for one. Only pass `--host` when the user asked for it.

**Always show the URL to the user in chat.** Browser opening is best-effort —
in sandboxed environments (e.g. Codex) it may fail or need approval; the URL
line is the fallback.

### Version skew: checking and restarting

Assets are re-read from disk on every request, but `server.py` is only loaded
when the daemon starts. So after the skill is updated, a daemon left running
from before keeps serving the new page against its own old API — the page
outruns the endpoints it calls, and requests fail for reasons that look like
UI bugs. The daemon stamps a digest of its own source into `server.json`;
comparing that with the file on disk is how the skew gets caught.

```sh
python3 "SKILL_DIR/scripts/server.py" status --root "PROJECT/.explain"
python3 "SKILL_DIR/scripts/server.py" start  --root "PROJECT/.explain"   # the fix
python3 "SKILL_DIR/scripts/server.py" stop   --root "PROJECT/.explain"
```

- `status` prints `{"running", "server", "stale", "expected_source"}`.
  `stale: true` means restart. Also reachable live at `GET <url>/api/ping`,
  which answers `{"ok", "root", "pid", "source", "stale"}`.
- `start` is the whole remedy: it retires a stale daemon and reports
  `restarted_stale: true`. Do NOT `stop` first — `stop` on its own leaves
  the project unserved, and `start` alone would otherwise see a live server
  and do nothing.
- You usually will not have to look: the reader's page raises a banner, and
  the watcher fires `server_stale` (§5) so a watching session restarts
  unprompted. Check by hand when someone reports that a control "does
  nothing" — that is the shape this failure takes.
- `stop` is for shutting down on request, not part of the restart path.

## 4. Catch up on unread comments

Fetch `GET <url>/api/docs/<slug>/comments` and handle every thread with
`"status": "unread"` (§6) — anchored comments and document-level
conversations arrive the same way. Do this before starting the watch loop —
it covers comments left while no session was watching.

## 5. Watch loop

Generate a session id once per conversation (e.g. `sess-` + 8 random hex
chars) and reuse the literal value. Then run:

```sh
python3 "SKILL_DIR/scripts/watch.py" --root "PROJECT/.explain" --session <id> --docs <slug1,slug2>
```

Starting the watcher acquires (or takes over) the ownership lease for those
docs. It exits printing a sentinel when you're needed:

| Sentinel | Exit | What you do |
|---|---|---|
| `EXPLAIN_EVENT unread <slug>` | 0 | handle unread threads in that doc (§6), then relaunch the watcher |
| `EXPLAIN_EVENT server_dead` | 2 | rerun `server.py start`, then relaunch the watcher |
| `EXPLAIN_EVENT server_stale` | 5 | same: rerun `server.py start` (it retires the old daemon itself), then relaunch the watcher |
| `EXPLAIN_EVENT lease_lost <slug>` + exit 3 | 3 | another session took over — stop watching (all docs lost) |
| `EXPLAIN_EVENT timeout` | 4 | only with `--timeout`; relaunch if still watching |

One watcher per session: before relaunching with a changed doc list (e.g. the
session now explains a second doc), kill the previous one:
`pkill -f "watch.py.*--session <id>"`.

Per-agent wake mechanics:

- **Claude Code**: run the watcher with the Bash tool in background mode
  (`run_in_background: true`). Its exit re-invokes you automatically with the
  sentinel in the task output. Handle the event, relaunch in background, and
  carry on with whatever else you were doing — interleaving is expected.
- **Codex**: no background wake exists. Run the watcher in your persistent
  terminal and keep waiting/polling the session until a sentinel line
  appears — this dedicates the session to watching; tell the user so. For
  long waits a larger `background_terminal_max_timeout` config helps; you may
  also pass `--timeout 1800` and relaunch on exit 4.
- **Fallback (any agent)**: if the user prefers, skip the loop; on “check the
  comments”, run steps 4 then relaunch nothing.

## 6. Handling comments

For each unread thread (`GET …/comments`, `threads[].status == "unread"`):

1. Read the whole thread: `anchor.exact` is the highlighted document text;
   `messages` is the conversation so far. Keep the id of the last message you
   read — step 6 needs it. Note that the anchor is RENDERED
   text (concatenated text nodes), so a selection spanning inline tags won't
   grep verbatim in the HTML source (`the <code>foo</code> function` anchors
   as `the foo function`) — locate it ignoring tags, narrowing with
   `anchor.prefix`/`suffix` when the exact text appears more than once.
2. `anchor: null` means a **document-level conversation** — the reader
   started it from the sidebar rather than by selecting text, so there is no
   passage to look up. If `context` is present (`{view, title}`) the reader
   chose to attach the view they were reading; treat it as a hint about what
   they mean, not as an anchor.
3. Answer in the language the comment is written in. Ground the reply in
   evidence: cite `file.py:12` references where relevant (rendered as code),
   and `#/viewid` mentions render as links that jump to that view.
4. Scope: questions about the wider repo are fair game, not just the
   document. Do NOT change code from here — the sidebar is a reading
   surface with no review or approval path. Answer the question and say
   the edit belongs in the session; editing the DOCUMENT is still fine
   (next step).
5. If the comment points out an error or asks for a change in the document,
   you may edit the doc: rewrite `ROOT/<slug>/index.html` (and bump
   `doc.json.updated_at`), then record the version (§2-6) with this thread's
   id in `threads`. Readers get a reload banner automatically, and it offers
   them the diff.

   Split the two texts by audience rather than repeating one in the other:
   the **reply** says *why* — what the comment got right, what was wrong
   before — for the person following the conversation; the **version
   summary** says *what changed*, in one line, for whoever scans the history
   weeks later. Don't restate the whole edit in the reply; the diff shows it.
6. Reply, passing `after` — the id of the last message you actually read in
   that thread:
   ```sh
   curl -s -X POST <url>/api/docs/<slug>/threads/<tid>/messages \
     -H 'Content-Type: application/json' \
     -d '{"author": "agent", "body": "…", "after": "m-…"}'
   ```
   An agent reply marks the thread `answered` automatically — but only if
   `after` still matches the thread's last message. Writing a reply takes
   real time, and a reader who adds a second question while you write would
   otherwise have it marked answered by a reply that predates it: read by
   nobody, and unrecoverable, since the watcher only ever wakes on `unread`.
   With `after`, that thread stays `unread` and wakes you again for the part
   you have not seen. ALWAYS send it; omitting it restores the old
   swallow-on-race behaviour. Never mark threads resolved yourself — that's
   the reader's button — and don't touch `resolved` threads unless the reader
   reopens them.
7. Escape/quote carefully; body is plain text (no markdown rendering).

You may also OPEN a document-level thread yourself — `POST /threads` with
`{"author": "agent", "body": "…"}` and no anchor — when something needs
saying that answers no existing thread. Reserve it for that; a panel of
agent-initiated notes is noise the reader has to clear.

## 7. Ownership, takeover, stopping

- The lease (`ROOT/<slug>/owner.json`) marks which session answers a doc's
  comments; the watcher heartbeats it. Starting a watcher for a doc TAKES
  OVER from any previous session — that's the intended way to resume in a new
  conversation (catch-up in §4 first, then watch).
- A takeover is silent: it overwrites the file, it does not signal the old
  watcher. That watcher re-reads the lease before every event it would fire,
  so it stands down with `lease_lost` rather than answering a doc it no
  longer owns — the old session's process may still be alive, and two agents
  replying to one comment is the failure this prevents. Per-doc: a session
  watching several docs keeps the ones it still holds.
- “Stop watching”: `pkill -f "watch.py.*--session <id>"`, and optionally
  remove the `owner.json` files so the server can GC sooner. The server and
  page stay usable; nobody answers until some session resumes.

## API quick reference

Base: `<url>/api/docs/<slug>`

| Method & path | Body | Effect |
|---|---|---|
| GET `/state` | — | `{rev, doc_etag, watched, unseen_for_user, server_stale, version}` |
| GET `/versions` | — | `{versions:[{n, hash, created_at, summary, threads, source, commit}, …]}` |
| POST `/versions` | `{summary?, threads?}` | record/annotate the current `index.html` (§2-6); 409 if the file is missing or half-written |
| GET `<url>/api/ping` | — | `{ok, root, pid, source, stale}` — not doc-scoped; `stale` means the daemon predates `server.py` on disk (§3) |
| GET `/comments` | — | full comment data `{rev, threads:[…]}` |
| POST `/threads` | `{body, anchor?:{exact,prefix,suffix}, context?:{view,title}, author?}` | new thread (user → `unread`); no `anchor` → document-level |
| POST `/threads/<tid>/messages` | `{author, body, after?}` | reply; user → `unread`, agent → `answered` — unless `after` (the last message id the sender read) no longer matches the thread's tail, which keeps it `unread` so the unseen part still wakes someone |
| PATCH `/threads/<tid>` | `{action: resolve\|reopen\|seen}` | status / mark agent msgs seen |
| PATCH `/threads/<tid>/messages/<mid>` | `{body}` | edit; user edit → thread `unread` |
| DELETE `/threads/<tid>`, `/threads/<tid>/messages/<mid>` | — | delete (root message deletes thread) |

`GET <url>/api/docs` lists all docs; `GET <url>/` is the human index page.
