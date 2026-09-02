#!/usr/bin/env python3
"""Smoke tests for document version history.

    python3 scripts/test_versions.py

These cover the rules that are invisible on screen and only surface weeks
later as "my versions disappeared": hash-based upsert, the half-written-file
guard, pruning, and number recovery after the index is lost.
"""

import json
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import server  # noqa: E402

SERVER_PY = str(Path(__file__).resolve().parent / "server.py")


def doc_html(body: str) -> str:
    return (
        '<!DOCTYPE html><html lang="en"><head><title>t</title></head><body>'
        f'<main id="explain-content">{body}</main></body></html>'
    )


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / ".explain"
        self.doc = self.root / "demo"
        self.doc.mkdir(parents=True)
        self.write(doc_html("<p>one</p>"))
        (self.doc / "doc.json").write_text(
            json.dumps({"title": "Demo", "commit": "abc1234"}), encoding="utf-8"
        )
        self.store = server.Store(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, html: str) -> None:
        (self.doc / "index.html").write_text(html, encoding="utf-8")

    def entries(self) -> list:
        return self.store.load_versions("demo")["versions"]

    def test_baseline_snapshot_copies_html_and_meta(self):
        entry = self.store.ensure_version("demo")
        self.assertEqual(entry["n"], 1)
        self.assertEqual(entry["source"], "auto")
        self.assertEqual(entry["commit"], "abc1234")
        vdir = self.doc / "versions" / "0001"
        self.assertEqual((vdir / "index.html").read_text(encoding="utf-8"), doc_html("<p>one</p>"))
        self.assertEqual(json.loads((vdir / "doc.json").read_text(encoding="utf-8"))["title"], "Demo")

    def test_unchanged_document_records_once(self):
        self.store.ensure_version("demo")
        self.store.ensure_version("demo")
        self.assertEqual(len(self.entries()), 1)

    def test_rewrite_records_a_new_version(self):
        self.store.ensure_version("demo")
        self.write(doc_html("<p>one, revised at length</p>"))
        entry = self.store.ensure_version("demo")
        self.assertEqual(entry["n"], 2)
        self.assertEqual(len(self.entries()), 2)

    def test_agent_metadata_lands_on_the_auto_snapshot(self):
        """The page's poll and the agent's POST race; both describe one file."""
        self.write(doc_html("<p>two</p>"))
        self.store.ensure_version("demo")  # the poll gets there first
        entry = self.store.ensure_version(
            "demo", summary="fixed the cache claim", threads=["t-1"], source="agent"
        )
        self.assertEqual(len(self.entries()), 1)
        self.assertEqual(entry["summary"], "fixed the cache claim")
        self.assertEqual(entry["threads"], ["t-1"])
        self.assertEqual(entry["source"], "agent")

    def test_thread_refs_accumulate_without_duplicates(self):
        self.store.ensure_version("demo", threads=["t-1"], source="agent")
        entry = self.store.ensure_version("demo", threads=["t-1", "t-2"], source="agent")
        self.assertEqual(entry["threads"], ["t-1", "t-2"])

    def test_half_written_file_is_not_recorded(self):
        self.write('<!DOCTYPE html><html><body><main id="explain-content"><p>half')
        self.assertIsNone(self.store.ensure_version("demo"))
        self.assertEqual(self.entries(), [])
        self.write(doc_html("<p>whole</p>"))
        self.assertEqual(self.store.ensure_version("demo")["n"], 1)

    def test_half_written_file_does_not_take_the_previous_summary(self):
        self.store.ensure_version("demo", summary="first", source="agent")
        self.write('<!DOCTYPE html><html><body><main id="explain-content"><p>half')
        self.assertIsNone(self.store.ensure_version("demo", summary="second", source="agent"))
        self.assertEqual(self.entries()[0]["summary"], "first")

    def test_pruning_keeps_the_first_version_and_the_newest(self):
        for i in range(server.MAX_VERSIONS + 5):
            self.write(doc_html("<p>" + "x" * i + "</p>"))
            self.store.ensure_version("demo")
        entries = self.entries()
        self.assertEqual(len(entries), server.MAX_VERSIONS)
        self.assertEqual(entries[0]["n"], 1)
        self.assertEqual(entries[-1]["n"], server.MAX_VERSIONS + 5)
        kept = {d.name for d in (self.doc / "versions").iterdir()}
        self.assertEqual(kept, {f"{e['n']:04d}" for e in entries})

    def test_lost_index_never_recycles_a_snapshot_number(self):
        for i in range(3):
            self.write(doc_html("<p>" + "y" * i + "</p>"))
            self.store.ensure_version("demo")
        (self.doc / "versions.json").write_text("{ not json", encoding="utf-8")
        self.write(doc_html("<p>after the index was lost</p>"))
        entry = self.store.ensure_version("demo")
        self.assertEqual(entry["n"], 4)
        self.assertTrue((self.doc / "versions" / "0001" / "index.html").is_file())


class HttpTests(unittest.TestCase):
    """The routes, over a real daemon — including snapshots served as files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / ".explain"
        self.doc = self.root / "demo"
        self.doc.mkdir(parents=True)
        (self.doc / "index.html").write_text(doc_html("<p>one</p>"), encoding="utf-8")
        (self.doc / "doc.json").write_text(json.dumps({"title": "Demo"}), encoding="utf-8")
        out = subprocess.run(
            [sys.executable, SERVER_PY, "start", "--root", str(self.root)],
            capture_output=True, text=True, timeout=30,
        )
        self.url = json.loads(out.stdout)["url"]

    def tearDown(self):
        subprocess.run([sys.executable, SERVER_PY, "stop", "--root", str(self.root)],
                       capture_output=True, timeout=30)
        self.tmp.cleanup()

    def get(self, path: str):
        with urllib.request.urlopen(self.url + path, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def raw(self, path: str) -> str:
        with urllib.request.urlopen(self.url + path, timeout=5) as r:
            return r.read().decode("utf-8")

    def post(self, path: str, payload: dict):
        req = urllib.request.Request(
            self.url + path, method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_state_reports_the_current_version(self):
        self.assertEqual(self.get("/api/docs/demo/state")["version"], 1)

    def test_serving_the_document_records_a_baseline(self):
        self.raw("/demo/")
        self.assertEqual([e["n"] for e in self.get("/api/docs/demo/versions")["versions"]], [1])

    def test_post_then_list_then_fetch_the_snapshot(self):
        created = self.post("/api/docs/demo/versions", {"summary": "first pass", "threads": ["t-9"]})
        self.assertEqual(created["version"]["source"], "agent")
        listed = self.get("/api/docs/demo/versions")["versions"]
        self.assertEqual(listed[0]["summary"], "first pass")
        self.assertEqual(listed[0]["threads"], ["t-9"])
        # past snapshots come back through ordinary static serving
        self.assertEqual(self.raw("/demo/versions/0001/index.html"), doc_html("<p>one</p>"))

    def test_recording_a_half_written_document_is_refused(self):
        (self.doc / "index.html").write_text('<html><main id="explain-content"><p>half', encoding="utf-8")
        time.sleep(0.01)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post("/api/docs/demo/versions", {"summary": "should not land"})
        self.assertEqual(cm.exception.code, 409)


if __name__ == "__main__":
    unittest.main(verbosity=2)
