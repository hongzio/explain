#!/usr/bin/env python3
"""Smoke tests for the watcher's lease handling.

    python3 scripts/test_watch.py

These cover the one rule that never shows up on screen: a doc that has been
taken over must not wake its old watcher. Getting it wrong costs a duplicate
answer in someone's comment thread, days later, with nothing in any log to
say why two agents replied.

The watchers here run with --heartbeat-interval 999 on purpose. The lease was
always checked on the heartbeat; what these tests pin down is the check on the
firing path, so the heartbeat is pushed out of reach to keep it from passing
the test on the old code's behalf.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

WATCH_PY = str(Path(__file__).resolve().parent / "watch.py")
SERVER_PY = Path(__file__).resolve().parent / "server.py"

POLL = 0.1
SETTLE = 0.5      # long enough for the watcher to clear catch-up and be looping
GIVE_UP = 15


def unread_comments(rev: int) -> dict:
    return {
        "rev": rev,
        "threads": [
            {
                "id": "t-1",
                "status": "unread",
                "anchor": None,
                "messages": [{"id": "m-1", "author": "user", "body": "?"}],
            }
        ],
    }


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / ".explain"
        self.doc = self.root / "demo"
        self.doc.mkdir(parents=True)
        # the watcher checks the daemon on its first pass and exits 2 if the
        # pid is gone, so stand in as a live server this process can vouch for
        (self.root / "server.json").write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "port": 1,
                    "url": "http://127.0.0.1:1",
                    "source": hashlib.sha256(SERVER_PY.read_bytes()).hexdigest()[:12],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def owner(self) -> Path:
        return self.doc / "owner.json"

    def start_watcher(self, session: str) -> subprocess.Popen:
        return self.start_watcher_on(session, "demo")

    def start_watcher_on(self, session: str, docs: str) -> subprocess.Popen:
        proc = subprocess.Popen(
            [
                sys.executable, WATCH_PY,
                "--root", str(self.root),
                "--session", session,
                "--docs", docs,
                "--poll-interval", str(POLL),
                "--heartbeat-interval", "999",
                "--timeout", "10",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.time() + GIVE_UP
        while time.time() < deadline:
            if self.owner().is_file():
                break
            time.sleep(0.05)
        else:
            proc.kill()
            self.fail("watcher never acquired the lease")
        time.sleep(SETTLE)
        return proc

    def finish(self, proc: subprocess.Popen) -> tuple[int, str]:
        try:
            out, _ = proc.communicate(timeout=GIVE_UP)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("watcher did not exit")
        return proc.returncode, out

    def test_owner_is_woken_by_an_unread_comment(self):
        """The control: nobody takes over, so the watcher fires as always."""
        proc = self.start_watcher("sess-a")
        (self.doc / "comments.json").write_text(
            json.dumps(unread_comments(1)), encoding="utf-8"
        )
        code, out = self.finish(proc)
        self.assertEqual(code, 0, out)
        self.assertIn("EXPLAIN_EVENT unread demo", out)

    def test_taken_over_watcher_stands_down_instead_of_firing(self):
        """The regression: a comment arriving after a takeover must reach the
        new session only. The old watcher has not heartbeated since the
        handover, so it still holds a stale belief that it owns the doc."""
        proc = self.start_watcher("sess-a")
        self.owner().write_text(
            json.dumps({"session": "sess-b", "acquired_at": time.time()}),
            encoding="utf-8",
        )
        (self.doc / "comments.json").write_text(
            json.dumps(unread_comments(1)), encoding="utf-8"
        )
        code, out = self.finish(proc)
        self.assertIn("EXPLAIN_EVENT lease_lost demo", out)
        self.assertNotIn("unread", out)
        self.assertEqual(code, 3, out)

    def test_losing_one_doc_does_not_stop_the_others(self):
        """Standing down is per-doc. A session watching two docs that loses one
        of them keeps answering the other — dropping both would silently strand
        a document nobody else has claimed."""
        other = self.root / "other"
        other.mkdir()
        proc = self.start_watcher_on("sess-a", "demo,other")

        # take "other" away, then leave a comment on it
        (other / "owner.json").write_text(
            json.dumps({"session": "sess-b", "acquired_at": time.time()}),
            encoding="utf-8",
        )
        (other / "comments.json").write_text(
            json.dumps(unread_comments(1)), encoding="utf-8"
        )
        time.sleep(SETTLE)
        self.assertIsNone(proc.poll(), "watcher quit after losing one of two docs")

        # the doc it still owns must still wake it
        (self.doc / "comments.json").write_text(
            json.dumps(unread_comments(1)), encoding="utf-8"
        )
        code, out = self.finish(proc)
        self.assertIn("EXPLAIN_EVENT lease_lost other", out)
        self.assertNotIn("unread other", out)
        self.assertIn("EXPLAIN_EVENT unread demo", out)
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
