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

Evidence (needs the project root — inferred when the doc lives in
<project>/.explain/<slug>, else pass --project; skipped with a note when
unavailable):
  E9  every .ex-walkthrough carries data-src="relative/path"
  E10 every data-src path exists in the project
  E11 code excerpts under a data-src element appear VERBATIM (contiguous,
      whitespace-insensitive) in that file; file:line citations in prose
      point at existing files and in-range lines

Warnings:
  W1  a view's text is very long (consider splitting / more structure)
  W2  <pre> code outside .ex-walkthrough/.ex-steps/<details> (notes missing?)
  W3  doc.json has no source commit stamp
  W4  a doc.json sources hash no longer matches the file (doc may be stale)
  W5  diff-kind doc without coverage info in doc.json
  W6  a file:line citation resolved only by basename search (ambiguous path)

Exit 0 = no errors (warnings allowed), 1 = errors. stdlib only, Python 3.11+.
"""

import hashlib
import json
import re
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

        for n in content.walk():
            if n.tag != "pre":
                continue
            carrier = n if n.attrs.get("data-src") else n.ancestor(lambda a: bool(a.attrs.get("data-src")))
            if carrier is None:
                continue
            rel = carrier.attrs["data-src"].strip()
            lines = load_src(rel)
            if lines is None:
                errors.append(f"E10: data-src '{rel}' not found under {project}")
                continue
            excerpt = n.all_text()
            if not excerpt_in_file(excerpt, lines):
                head = stripped_lines(excerpt)[:1]
                errors.append(f"E11: excerpt not found verbatim in '{rel}' (starts: {head[0][:50] if head else '?'})")

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
