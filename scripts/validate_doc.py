#!/usr/bin/env python3
"""Validate a generated explain document against the kit's conventions.

Usage: validate_doc.py <ROOT>/<slug> [--project <dir>]

Checks the computable parts of the authoring guide — run it after writing
index.html and fix every ERROR before serving; treat WARNs as review items.

Structure:
  E1  index.html exists and contains <main id="explain-content">
  E2  view graph: unique data-view ids, exactly one root, parents exist
  E3  every #/view link and data-goto targets an existing view
  E4  every .ex-walkthrough .ex-chunk has a <pre> AND a non-empty .ex-note
  E5  every .ex-step has a non-empty data-label
  E6  colors in style=/fill=/stroke= use theme tokens (var(--ex-*)),
      currentColor, none, transparent or inherit — no raw hex/rgb/named
  E7  self-contained: no <script>, no external src/href assets
  E8  doc.json exists with title/kind/target/lang/created_at/updated_at
  E12 every .ex-node has data-node and a matching .ex-legend <li data-for>
  E13 figure.diagram[data-layers]: >=2 [data-layer] groups, indices 0..n with
      no gaps, a data-layer-label on every layer past 0, none orphaned
  E14 every .ex-predict has .ex-ask, >=2 .ex-choices <li>, exactly one
      [data-correct], a non-empty .ex-why per choice, and an .ex-reveal
  E15 no view opens with a .ex-predict (lead with the point, ask afterwards)
  E17 every .ex-contrast has exactly two labelled .ex-side panes, in front,
      and a non-empty .ex-note saying what the one difference means

Evidence (needs the project root — inferred when the doc lives in
<project>/.explain/<slug>, else pass --project; skipped with a note when
unavailable):
  E9  every .ex-walkthrough carries data-src="relative/path"
  E10 every data-src path exists in the project
  E11 code excerpts under a data-src element appear VERBATIM (contiguous,
      whitespace-insensitive) in that file; file:line citations in prose
      point at existing files and in-range lines
  E16 an excerpt marked data-rev="<rev>" appears verbatim in that file as of
      that git revision — how a diff doc's 'before' pane is held to the same
      standard as its 'after'

Warnings:
  W1  a view's text is very long (consider splitting / more structure)
  W2  <pre> code outside .ex-walkthrough/.ex-steps/<details> (notes missing?)
  W3  doc.json has no source commit stamp
  W4  a doc.json sources hash no longer matches the file (doc may be stale)
  W5  diff-kind doc without coverage info in doc.json
  W6  a file:line citation resolved only by basename search (ambiguous path)
  W7  figure.diagram without data-asks (the question the drawing answers)
  W8  a diagram whose nodes neither cite code nor drill down (decoration?)
  W9  more than 3 .ex-predict blocks in one document
  W10 a diagram over the node ceiling: 12 plain, or 6 added per layer / 24
      in total when it is layered
  W11 an .ex-legend row naming no node
  W12 a data-rev excerpt left unchecked because git is unavailable

Exit 0 = no errors (warnings allowed), 1 = errors. stdlib only, Python 3.11+.
"""

import hashlib
import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
COLOR_ATTRS = {"fill", "stroke", "color", "stop-color"}
COLOR_OK = re.compile(r"^(var\(--ex-[a-z0-9-]+\)|currentcolor|none|transparent|inherit|url\(#.*\))$", re.I)
CSS_COLOR_VALUE = re.compile(r"(#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\()")


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "text")

    def __init__(self, tag, attrs, parent):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []
        self.parent = parent
        self.text = ""

    def cls(self):
        return set((self.attrs.get("class") or "").split())

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def all_text(self):
        return self.text + "".join(c.all_text() for c in self.children)

    def ancestor(self, pred):
        n = self.parent
        while n is not None:
            if pred(n):
                return n
            n = n.parent
        return None


class DomParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", [], None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.text += data


CITATION_RE = re.compile(r"\b([A-Za-z0-9_@~/][A-Za-z0-9_@~./-]*\.[A-Za-z]{1,6}):(\d{1,6})\b")
SKIP_DIRS = {".git", ".explain", "node_modules", "__pycache__", "dist", "build", ".venv", "venv"}


def stripped_lines(text: str) -> list[str]:
    lines = [ln.strip() for ln in text.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def excerpt_in_file(excerpt: str, file_lines: list[str]) -> bool:
    """Contiguous, indentation-insensitive match of excerpt within the file."""
    needle = stripped_lines(excerpt)
    if not needle:
        return True
    hay = [ln.strip() for ln in file_lines]
    limit = len(hay) - len(needle)
    for i in range(limit + 1):
        if hay[i : i + len(needle)] == needle:
            return True
    return False


def resolve_citation(project: Path, ref: str) -> tuple[Path | None, bool]:
    """Return (path, resolved_by_basename). Bounded basename search fallback."""
    direct = project / ref
    if direct.is_file():
        return direct, False
    if "/" in ref:
        return None, False
    matches = []
    for p in project.rglob(ref):
        if any(part in SKIP_DIRS for part in p.relative_to(project).parts):
            continue
        matches.append(p)
        if len(matches) > 1:
            break
    if len(matches) == 1:
        return matches[0], True
    return None, False


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--project"]
    project_arg = None
    if "--project" in sys.argv:
        idx = sys.argv.index("--project")
        if idx + 1 < len(sys.argv):
            project_arg = Path(sys.argv[idx + 1]).resolve()
            args = [a for a in sys.argv[1:] if a not in ("--project", sys.argv[idx + 1])]
    if len(args) != 1:
        print(__doc__)
        return 1
    doc_dir = Path(args[0]).resolve()
    if project_arg is not None:
        project = project_arg
    elif doc_dir.parent.name == ".explain":
        project = doc_dir.parent.parent
    else:
        project = None
    errors: list[str] = []
    warns: list[str] = []

    html_path = doc_dir / "index.html"
    if not html_path.is_file():
        print(f"ERROR E1: {html_path} not found")
        return 1
    parser = DomParser()
    parser.feed(html_path.read_text(encoding="utf-8"))
    root = parser.root

    content = next((n for n in root.walk() if n.attrs.get("id") == "explain-content"), None)
    if content is None:
        print('ERROR E1: no <main id="explain-content"> found')
        return 1

    # E2: view graph
    views: dict[str, Node] = {}
    for n in content.walk():
        vid = n.attrs.get("data-view")
        if vid is not None:
            if vid in views:
                errors.append(f"E2: duplicate data-view id '{vid}'")
            views[vid] = n
    if views:
        roots = [v for v, n in views.items() if not n.attrs.get("data-parent")]
        if len(roots) != 1:
            errors.append(f"E2: expected exactly one root view (no data-parent), found {len(roots)}: {roots}")
        for vid, n in views.items():
            parent = n.attrs.get("data-parent")
            if parent and parent not in views:
                errors.append(f"E2: view '{vid}' has unknown data-parent '{parent}'")

    # E3: link targets
    for n in content.walk():
        href = n.attrs.get("href") or ""
        if href.startswith("#/"):
            target = href[2:]
            if target not in views:
                errors.append(f"E3: link '{href}' targets no existing view")
        goto = n.attrs.get("data-goto")
        if goto and goto not in views:
            errors.append(f"E3: data-goto='{goto}' targets no existing view")

    # E4: walkthrough chunks
    for n in content.walk():
        if "ex-chunk" in n.cls():
            has_pre = any(c.tag == "pre" for c in n.walk())
            note = next((c for c in n.walk() if "ex-note" in c.cls()), None)
            if not has_pre:
                errors.append("E4: .ex-chunk without a <pre> code block")
            if note is None or not note.all_text().strip():
                errors.append("E4: .ex-chunk without a non-empty .ex-note")

    # E5: step labels
    for n in content.walk():
        if "ex-step" in n.cls() and not (n.attrs.get("data-label") or "").strip():
            errors.append("E5: .ex-step without a data-label")

    # E6: colors must be theme tokens
    for n in content.walk():
        for attr in COLOR_ATTRS:
            val = (n.attrs.get(attr) or "").strip()
            if val and not COLOR_OK.match(val):
                errors.append(f"E6: <{n.tag} {attr}=\"{val}\"> — use var(--ex-*) tokens")
        style = n.attrs.get("style") or ""
        if CSS_COLOR_VALUE.search(style):
            errors.append(f"E6: raw color in style=\"{style[:60]}\" — use var(--ex-*) tokens")

    # E7: self-contained
    for n in content.walk():
        if n.tag == "script":
            errors.append("E7: <script> inside the content region is forbidden")
        for attr in ("src", "href"):
            val = n.attrs.get(attr) or ""
            if val.startswith(("http://", "https://", "//")):
                if n.tag == "a" and attr == "href":
                    warns.append(f"W: external link {val[:60]} (allowed, but the doc must not depend on it)")
                else:
                    errors.append(f"E7: external asset <{n.tag} {attr}=\"{val[:60]}\">")

    # E12 / W11: diagram node notes live in the figure's .ex-legend
    legend_rows: list[tuple[str, Node]] = []
    for n in content.walk():
        if n.tag == "li" and n.parent is not None and "ex-legend" in n.parent.cls():
            legend_rows.append(((n.attrs.get("data-for") or "").strip(), n))
    claimed: set[str] = set()
    for n in content.walk():
        if "ex-node" not in n.cls():
            continue
        node_id = (n.attrs.get("data-node") or "").strip()
        if not node_id:
            errors.append('E12: .ex-node without data-node="id"')
            continue
        claimed.add(node_id)
        fig = n.ancestor(lambda a: a.tag == "figure" and "diagram" in a.cls())
        rows = [r for fid, r in legend_rows if fid == node_id and (fig is None or r.ancestor(lambda a: a is fig))]
        if not rows:
            errors.append(f"E12: .ex-node data-node='{node_id}' has no .ex-legend <li data-for=\"{node_id}\">")
        elif not rows[0].all_text().strip():
            errors.append(f"E12: the legend note for '{node_id}' is empty")
    for fid, _row in legend_rows:
        if not fid:
            warns.append("W11: .ex-legend <li> without data-for — it names no node")
        elif fid not in claimed:
            warns.append(f"W11: .ex-legend row for '{fid}' matches no .ex-node")

    # E13: diagram layers
    for n in content.walk():
        if not (n.tag == "figure" and "diagram" in n.cls() and "data-layers" in n.attrs):
            continue
        groups = [m for m in n.walk() if m.attrs.get("data-layer") is not None]
        idx: list[int] = []
        for g in groups:
            raw = (g.attrs.get("data-layer") or "").strip()
            if not raw.isdigit():
                errors.append(f"E13: data-layer='{raw}' is not a layer index")
                continue
            idx.append(int(raw))
            if int(raw) > 0 and not (g.attrs.get("data-layer-label") or "").strip():
                errors.append(f"E13: layer {raw} has no data-layer-label — the bar would label it with a bare number")
        if len(groups) < 2:
            errors.append("E13: figure.diagram[data-layers] needs at least two [data-layer] groups")
        elif sorted(idx) != list(range(len(idx))):
            errors.append(f"E13: layer indices must run 0..n with no gaps or repeats, got {sorted(idx)}")
    for n in content.walk():
        if n.attrs.get("data-layer") is None:
            continue
        if n.ancestor(lambda a: a.tag == "figure" and "data-layers" in a.attrs) is None:
            errors.append("E13: [data-layer] outside figure.diagram[data-layers] — nothing would ever reveal it")

    # E14 / E15 / W9: predictions
    predicts = [n for n in content.walk() if "ex-predict" in n.cls()]
    for n in predicts:
        ask = next((c for c in n.walk() if "ex-ask" in c.cls()), None)
        if ask is None or not ask.all_text().strip():
            errors.append("E14: .ex-predict without a non-empty .ex-ask")
        box = next((c for c in n.walk() if "ex-choices" in c.cls()), None)
        lis = [c for c in box.children if c.tag == "li"] if box is not None else []
        if len(lis) < 2:
            errors.append("E14: .ex-predict needs an .ex-choices list of at least two <li>")
        correct = [c for c in lis if "data-correct" in c.attrs]
        if lis and len(correct) != 1:
            errors.append(f"E14: .ex-predict needs exactly one <li data-correct>, found {len(correct)}")
        for c in lis:
            why = next((d for d in c.walk() if "ex-why" in d.cls()), None)
            if why is None or not why.all_text().strip():
                errors.append("E14: an .ex-choices <li> has no non-empty .ex-why — a guess with nothing said back teaches nothing")
        if not any("ex-reveal" in c.cls() for c in n.walk()):
            errors.append("E14: .ex-predict without an .ex-reveal")
    for vid, v in views.items():
        lead = next((c for c in v.children if c.tag not in ("h1", "h2", "h3")), None)
        if lead is not None and "ex-predict" in lead.cls():
            errors.append(f"E15: view '{vid}' opens with a .ex-predict — lead with the point, ask afterwards")
    if len(predicts) > 3:
        warns.append(
            f"W9: {len(predicts)} .ex-predict blocks — the gain is specific to what you ask, "
            "so 2-3 questions at the real misconceptions beat a quiz"
        )

    # E17: before/after contrast
    for n in content.walk():
        if "ex-contrast" not in n.cls():
            continue
        sides = [c for c in n.children if "ex-side" in c.cls()]
        if len(sides) != 2:
            errors.append(f"E17: .ex-contrast needs exactly two .ex-side panes, found {len(sides)}")
        if n.children[:2] != sides[:2]:
            errors.append("E17: the two .ex-side panes must come first in .ex-contrast (the note follows them)")
        for side in sides:
            if not (side.attrs.get("data-label") or "").strip():
                errors.append('E17: .ex-side without data-label="…" — the panes would be unlabelled')
        note = next((c for c in n.children if "ex-note" in c.cls()), None)
        if note is None or not note.all_text().strip():
            errors.append("E17: .ex-contrast without a non-empty .ex-note saying what the one difference means")

    # W7 / W8 / W10: diagram upkeep
    for n in content.walk():
        if not (n.tag == "figure" and "diagram" in n.cls()):
            continue
        if not (n.attrs.get("data-asks") or "").strip():
            warns.append("W7: figure.diagram without data-asks — name the question the drawing answers")
        cites = any((m.attrs.get("data-ref") or "").strip() for m in n.walk())
        drills = any(
            (m.attrs.get("data-goto") or "").strip()
            or (m.tag == "a" and (m.attrs.get("href") or "").startswith("#/"))
            for m in n.walk()
        )
        if not cites and not drills:
            warns.append("W8: figure.diagram whose nodes neither cite code (data-ref) nor drill down — decoration?")
        nodes = [m for m in n.walk() if "ex-node" in m.cls()]
        if "data-layers" in n.attrs:
            # cumulative reveal: the last layer has everything on screen, so what
            # each layer must keep small is its INCREMENT, and the figure its total
            per: dict[str, int] = {}
            for m in nodes:
                g = m.ancestor(lambda a: a.attrs.get("data-layer") is not None)
                per.setdefault((g.attrs.get("data-layer") or "?") if g is not None else "?", 0)
                per[(g.attrs.get("data-layer") or "?") if g is not None else "?"] += 1
            for key, count in sorted(per.items()):
                if count > 6:
                    warns.append(f"W10: layer {key} adds {count} nodes at once — 6 per step is the ceiling")
            if len(nodes) > 24:
                warns.append(f"W10: layered figure.diagram totals {len(nodes)} nodes — 24 is the ceiling")
        elif len(nodes) > 12:
            warns.append(f"W10: figure.diagram has {len(nodes)} .ex-node — split it or give it layers (12 is the ceiling)")

    # E8 / W3: doc.json
    meta = {}
    meta_path = doc_dir / "doc.json"
    if not meta_path.is_file():
        errors.append("E8: doc.json missing")
    else:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError:
            errors.append("E8: doc.json is not valid JSON")
        for field in ("title", "kind", "target", "lang", "created_at", "updated_at"):
            if not meta.get(field):
                errors.append(f"E8: doc.json missing '{field}'")
        if meta and not meta.get("commit"):
            warns.append("W3: doc.json has no 'commit' stamp — freshness tracking won't work")

    # W1: overly long views
    for vid, n in views.items():
        length = len(re.sub(r"\s+", " ", n.all_text()))
        if length > 6000:
            warns.append(f"W1: view '{vid}' has ~{length} chars of text — split it or convert prose to components")

    # W2: bare code blocks
    for n in content.walk():
        if n.tag == "pre":
            sheltered = n.ancestor(
                lambda a: a.tag == "details"
                or "ex-walkthrough" in a.cls()
                or "ex-steps" in a.cls()
                # a contrast carries one note for the pair (E17), not one per pane
                or "ex-contrast" in a.cls()
            )
            if sheltered is None:
                warns.append("W2: <pre> outside .ex-walkthrough/.ex-steps/<details> — where are its notes?")

    # ---- evidence verification (needs the project root) ----
    if project is None or not project.is_dir():
        warns.append("evidence checks SKIPPED — pass --project <dir> to enable them")
    else:
        # E9/E10/E11: data-src coverage and verbatim excerpts
        for n in content.walk():
            if "ex-walkthrough" in n.cls() and not (n.attrs.get("data-src") or "").strip():
                errors.append("E9: .ex-walkthrough without data-src=\"relative/path\"")
        src_cache: dict[str, list[str] | None] = {}

        def load_src(rel: str) -> list[str] | None:
            if rel not in src_cache:
                p = project / rel
                try:
                    src_cache[rel] = p.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    src_cache[rel] = None
            return src_cache[rel]

        rev_cache: dict[tuple[str, str], list[str] | None] = {}
        git_ok: bool | None = None

        def have_git() -> bool:
            nonlocal git_ok
            if git_ok is None:
                try:
                    r = subprocess.run(
                        ["git", "-C", str(project), "rev-parse", "--git-dir"],
                        capture_output=True, text=True, timeout=10,
                    )
                    git_ok = r.returncode == 0
                except (OSError, subprocess.SubprocessError):
                    git_ok = False
            return git_ok

        def load_rev(rev: str, rel: str) -> list[str] | None:
            """The file as of a git revision. This is what holds a diff doc's
            'before' pane to the same verbatim standard as its 'after' — the
            working tree cannot vouch for code that is no longer in it."""
            key = (rev, rel)
            if key not in rev_cache:
                try:
                    r = subprocess.run(
                        ["git", "-C", str(project), "show", f"{rev}:{rel}"],
                        capture_output=True, text=True, timeout=20,
                    )
                    rev_cache[key] = r.stdout.splitlines() if r.returncode == 0 else None
                except (OSError, subprocess.SubprocessError):
                    rev_cache[key] = None
            return rev_cache[key]

        for n in content.walk():
            if n.tag != "pre":
                continue
            carrier = n if n.attrs.get("data-src") else n.ancestor(lambda a: bool(a.attrs.get("data-src")))
            if carrier is None:
                continue
            rel = carrier.attrs["data-src"].strip()
            rev_at = n if n.attrs.get("data-rev") else n.ancestor(lambda a: bool(a.attrs.get("data-rev")))
            rev = rev_at.attrs["data-rev"].strip() if rev_at is not None else ""
            excerpt = n.all_text()
            head = stripped_lines(excerpt)[:1]
            where = head[0][:50] if head else "?"
            if rev:
                if not have_git():
                    warns.append(f"W12: data-rev '{rev}' unchecked — {project} is not a git repo (or git is unavailable)")
                    continue
                lines = load_rev(rev, rel)
                if lines is None:
                    errors.append(f"E16: data-rev '{rev}' has no '{rel}' (git show {rev}:{rel} failed)")
                elif not excerpt_in_file(excerpt, lines):
                    errors.append(f"E16: excerpt not found verbatim in '{rel}' at '{rev}' (starts: {where})")
                continue
            lines = load_src(rel)
            if lines is None:
                errors.append(f"E10: data-src '{rel}' not found under {project}")
                continue
            if not excerpt_in_file(excerpt, lines):
                errors.append(f"E11: excerpt not found verbatim in '{rel}' (starts: {where})")

        # E11/W6: file:line citations in prose (outside pre/code)
        def prose_text(node: Node) -> str:
            if node.tag in ("pre", "code"):
                return ""
            return node.text + "".join(prose_text(c) for c in node.children)

        seen: set[tuple[str, str]] = set()
        for ref, line_s in CITATION_RE.findall(prose_text(content)):
            if (ref, line_s) in seen:
                continue
            seen.add((ref, line_s))
            path, by_basename = resolve_citation(project, ref)
            if path is None:
                errors.append(f"E11: citation '{ref}:{line_s}' — file not found in project")
                continue
            if by_basename:
                warns.append(f"W6: citation '{ref}:{line_s}' resolved by basename search — use the fuller path")
            try:
                count = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                count = 0
            if int(line_s) > count:
                errors.append(f"E11: citation '{ref}:{line_s}' — file has only {count} lines")

        # W4: doc.json sources drift
        for rel, recorded in (meta.get("sources") or {}).items():
            p = project / rel
            if not p.is_file():
                warns.append(f"W4: sources entry '{rel}' no longer exists")
                continue
            actual = hashlib.sha256(p.read_bytes()).hexdigest()[:12]
            if actual != recorded:
                warns.append(f"W4: '{rel}' changed since generation (doc may be stale)")

    # W5: diff docs should state coverage
    if meta.get("kind") == "diff" and not meta.get("coverage"):
        warns.append("W5: diff doc without doc.json 'coverage' — record hunks_total/hunks_covered/uncovered")

    for e in errors:
        print("ERROR", e)
    for w in warns:
        print("WARN ", w)
    print(f"{'FAIL' if errors else 'OK'}: {len(errors)} error(s), {len(warns)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
