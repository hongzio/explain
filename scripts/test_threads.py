#!/usr/bin/env python3
"""Smoke tests for comment thread status transitions.

    python3 scripts/test_threads.py

These cover the rule that decides whether a question ever reaches an agent.
'unread' is the only state the watcher wakes on, so a thread that reaches
'answered' with an unread question inside it is not late — it is lost, and
nothing in the UI says so. The `after` field on an agent reply is what keeps
that from happening; these tests pin down both directions of it.
"""

import json
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

SERVER_PY = str(Path(__file__).resolve().parent / "server.py")


def doc_html(body: str) -> str:
    return (
        '<!DOCTYPE html><html lang="en"><head><title>t</title></head><body>'
        f'<main id="explain-content">{body}</main></body></html>'
    )


class ThreadStatusTests(unittest.TestCase):
    """The routes, over a real daemon, so the lock is the real one."""

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

    def post(self, path: str, payload: dict):
        req = urllib.request.Request(
            self.url + path, method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def thread(self, tid: str) -> dict:
        with urllib.request.urlopen(self.url + "/api/docs/demo/comments", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        return next(t for t in data["threads"] if t["id"] == tid)

    def open_thread(self, body: str = "q") -> tuple[str, str]:
        """Returns (thread id, id of its opening message)."""
        res = self.post("/api/docs/demo/threads", {"author": "user", "body": body})
        t = res["thread"]
        return t["id"], t["messages"][-1]["id"]

    def reply(self, tid: str, body: str, author: str = "agent", after: str | None = None):
        payload = {"author": author, "body": body}
        if after is not None:
            payload["after"] = after
        return self.post(f"/api/docs/demo/threads/{tid}/messages", payload)

    # ---- the baseline the field has to preserve ----

    def test_user_question_opens_a_thread_unread(self):
        tid, _ = self.open_thread()
        self.assertEqual(self.thread(tid)["status"], "unread")

    def test_reply_answers_the_thread_when_nothing_arrived_meanwhile(self):
        tid, last = self.open_thread()
        self.reply(tid, "a", after=last)
        self.assertEqual(self.thread(tid)["status"], "answered")

    def test_reply_without_after_keeps_the_old_behaviour(self):
        """Documents generated before the field exists still answer normally."""
        tid, _ = self.open_thread()
        self.reply(tid, "a")
        self.assertEqual(self.thread(tid)["status"], "answered")

    # ---- the regression ----

    def test_question_arriving_while_the_agent_writes_is_not_marked_answered(self):
        """The agent read the thread, and only then did the second question
        land. Replying must not bury it: the reply is an answer to what was
        read, and the thread still holds something nobody has looked at."""
        tid, first = self.open_thread("first question")
        self.reply(tid, "second question", author="user")   # lands mid-write
        self.reply(tid, "answer to the first", after=first)
        t = self.thread(tid)
        self.assertEqual(t["status"], "unread", "the later question was buried")
        self.assertEqual(len(t["messages"]), 3)

    def test_the_next_reply_clears_it_once_caught_up(self):
        """And the state is not sticky — answering what actually arrived ends
        the cycle rather than leaving the thread unread forever."""
        tid, first = self.open_thread("first question")
        self.reply(tid, "second question", author="user")
        self.reply(tid, "answer to the first", after=first)
        caught_up = self.thread(tid)["messages"][-1]["id"]
        self.reply(tid, "answer to the second", after=caught_up)
        self.assertEqual(self.thread(tid)["status"], "answered")

    def test_an_unknown_after_id_is_treated_as_stale(self):
        """Failure direction: an id the thread does not carry — a deleted
        message, a garbled field — must re-wake the agent, never swallow."""
        tid, _ = self.open_thread()
        self.reply(tid, "a", after="m-does-not-exist")
        self.assertEqual(self.thread(tid)["status"], "unread")

    def test_a_user_message_ignores_after_and_still_marks_unread(self):
        tid, last = self.open_thread()
        self.reply(tid, "more", author="user", after=last)
        self.assertEqual(self.thread(tid)["status"], "unread")


if __name__ == "__main__":
    unittest.main(verbosity=2)
