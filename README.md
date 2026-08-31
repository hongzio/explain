# explain

An agent skill (Claude Code & Codex) that turns "explain this diff / module /
repo" into a visual HTML document — inline SVG diagrams, interactive sections —
served on localhost, with a live comment loop: select text in the page, leave a
comment, and the agent session watching the document replies in near real time
(and may fix the document itself).

- Zero dependencies: Python ≥ 3.11 stdlib only.
- Documents and comments live in the project's `.explain/` directory
  (gitignored) and survive across sessions; a new session catches up on
  unanswered comments and takes over watching.
- One server per project (stable port derived from the project path); comment
  routing to the right session is done by per-session file watchers, so
  different sessions can own different documents concurrently — even mixed
  Claude/Codex.

## Install

```sh
npx skills add hongzio/explain        # project scope
npx skills add hongzio/explain -g     # global
```

## Use

- Claude Code: `/explain src/auth/` · `/explain main..feature` · `/explain how does caching work?`
- Codex: `$explain …`

The skill is explicit-invocation only on both agents.
