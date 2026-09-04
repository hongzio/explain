#!/usr/bin/env python3
"""Comment watcher for the explain skill.

Acquires the ownership lease for each watched doc (overwriting any previous
owner — that IS the takeover), heartbeats via the lease file's mtime, and
exits with a sentinel line on stdout when something needs the agent:

  EXPLAIN_EVENT unread <slug>     exit 0   unread comment(s) — fetch, reply, relaunch
  EXPLAIN_EVENT server_dead       exit 2   restart the server, then relaunch
  EXPLAIN_EVENT server_stale      exit 5   server.py changed since the daemon
                                           started — restart it, then relaunch
  EXPLAIN_EVENT lease_lost <slug> (info)   another session took this doc over
  (all leases lost)               exit 3   stop watching entirely
  EXPLAIN_EVENT timeout           exit 4   --timeout elapsed, nothing happened

stdlib only. Python 3.11+.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

SERVER_PATH = Path(__file__).resolve().parent / "server.py"


def emit(*parts) -> None:
    print("EXPLAIN_EVENT", *parts, flush=True)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json_atomic(path: Path, data) -> None:
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def server_digest() -> str:
    """server.py's bytes on disk, digested the same way server.py stamps
    itself into server.json — a mismatch means the daemon is running code
    that has since been replaced."""
    try:
        return hashlib.sha256(SERVER_PATH.read_bytes()).hexdigest()[:12]
    except OSError:
        return "unknown"


def has_unread(comments: dict) -> bool:
    return any(t.get("status") == "unread" for t in comments.get("threads", []))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True, help="the project's .explain directory")
    ap.add_argument("--session", required=True, help="this session's id (any unique string)")
    ap.add_argument("--docs", required=True, help="comma-separated doc slugs to watch")
    ap.add_argument("--timeout", type=float, default=0, help="give up after N seconds (0 = never)")
    ap.add_argument("--poll-interval", type=float, default=2.0)
    ap.add_argument("--heartbeat-interval", type=float, default=30.0)
    args = ap.parse_args()

    root = Path(args.root).resolve()
    docs = [s for s in args.docs.split(",") if s]
    if not docs:
        print("no docs to watch", file=sys.stderr)
        return 1

    def owner_path(slug: str) -> Path:
        return root / slug / "owner.json"

    def comments_path(slug: str) -> Path:
        return root / slug / "comments.json"

    def owns(slug: str) -> bool:
        return read_json(owner_path(slug), {}).get("session") == args.session

    # acquire (or take over) the lease for every doc
    for slug in docs:
        if not (root / slug).is_dir():
            print(f"unknown doc: {slug}", file=sys.stderr)
            return 1
        write_json_atomic(owner_path(slug), {"session": args.session, "acquired_at": time.time()})

    # catch-up trigger: if anything is already unread, fire immediately
    baseline = {}
    fired = False
    for slug in docs[:]:
        c = read_json(comments_path(slug), {})
        baseline[slug] = c.get("rev", 0)
        if not has_unread(c):
            continue
        # two watchers can start close enough together that the second one's
        # acquire lands between our write above and this read
        if not owns(slug):
            emit("lease_lost", slug)
            docs.remove(slug)
            continue
        emit("unread", slug)
        fired = True
    if fired:
        return 0
    if not docs:
        return 3

    start = time.time()
    last_beat = start
    last_srv = 0.0
    while True:
        time.sleep(args.poll_interval)
        now = time.time()

        if args.timeout and now - start > args.timeout:
            emit("timeout")
            return 4

        if now - last_beat >= args.heartbeat_interval:
            for slug in docs[:]:
                if not owns(slug):
                    emit("lease_lost", slug)
                    docs.remove(slug)
                    continue
                try:
                    os.utime(owner_path(slug))
                except OSError:
                    pass
            if not docs:
                return 3
            last_beat = now

        if now - last_srv >= 15:
            info = read_json(root / "server.json", {})
            if not info.get("pid") or not alive(info["pid"]):
                emit("server_dead")
                return 2
            # same remedy as server_dead, but the daemon is alive and wrong
            # rather than gone: the page has already reloaded newer assets
            if info.get("source") != server_digest():
                emit("server_stale")
                return 5
            last_srv = now

        fired = False
        for slug in docs[:]:
            c = read_json(comments_path(slug), {})
            rev = c.get("rev", 0)
            if rev == baseline[slug]:
                continue
            if not has_unread(c):
                baseline[slug] = rev
                continue
            # Re-check ownership at the moment of firing, not just on the 30s
            # heartbeat. A takeover lands by overwriting owner.json and never
            # signals the old watcher, so between two heartbeats it would still
            # believe it owns the doc — and since this branch never consulted
            # the lease, both watchers fired on the same comment and both went
            # off to answer it. The window is now one poll, not one heartbeat.
            if not owns(slug):
                emit("lease_lost", slug)
                docs.remove(slug)
                continue
            emit("unread", slug)
            fired = True
        if fired:
            return 0
        if not docs:
            return 3


if __name__ == "__main__":
    sys.exit(main())
