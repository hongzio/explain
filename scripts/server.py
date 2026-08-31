#!/usr/bin/env python3
"""Local server for the explain skill.

Serves generated explanation documents from a project's .explain/ directory
and owns ALL writes to each document's comments.json (readers may read the
files directly; writers must go through this HTTP API so writes serialize
in one process).

stdlib only. Python 3.11+.

Commands:
  server.py start  --root <project>/.explain [--open] [--doc <slug>]
  server.py serve  --root <root> --port <n>       (internal: the daemon)
  server.py status --root <root>
  server.py stop   --root <root>
"""

import argparse
import json
import mimetypes
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
import zlib
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = SKILL_DIR / "assets"

PORT_BASE = 49152
PORT_SPAN = 16000
LEASE_TTL = 180        # an owner.json younger than this marks a live watching session
HTTP_IDLE_GRACE = 300  # keep serving this long after the last HTTP request
GC_INTERVAL = 60
START_WAIT = 10.0
MAX_BODY = 1_000_000

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
API_DOC_RE = re.compile(r"^/api/docs/([^/]+)(/.*)?$")

VALID_AUTHORS = {"user", "agent"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(5)}"


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json_atomic(path: Path, data) -> None:
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.Lock()
        self.last_http = time.time()

    def doc_dir(self, slug: str) -> Path:
        return self.root / slug

    def comments_path(self, slug: str) -> Path:
        return self.doc_dir(slug) / "comments.json"

    def load_comments(self, slug: str) -> dict:
        return read_json(self.comments_path(slug), {"rev": 0, "threads": []})

    def mutate(self, slug: str, fn) -> dict:
        """Apply fn(data) under the lock, bump rev, persist, return the data."""
        if not self.doc_dir(slug).is_dir():
            raise ApiError(404, f"unknown doc: {slug}")
        with self.lock:
            data = self.load_comments(slug)
            fn(data)
            data["rev"] = data.get("rev", 0) + 1
            write_json_atomic(self.comments_path(slug), data)
            return data

    def docs(self) -> list[dict]:
        out = []
        try:
            entries = sorted(self.root.iterdir())
        except OSError:
            return out
        for d in entries:
            meta_path = d / "doc.json"
            if d.is_dir() and meta_path.is_file():
                meta = read_json(meta_path, {})
                meta["slug"] = d.name
                out.append(meta)
        return out

    def lease_fresh(self, slug: str) -> bool:
        try:
            return time.time() - (self.doc_dir(slug) / "owner.json").stat().st_mtime < LEASE_TTL
        except OSError:
            return False

    def any_fresh_lease(self) -> bool:
        return any(self.lease_fresh(d["slug"]) for d in self.docs())

    def doc_etag(self, slug: str) -> str:
        try:
            st = (self.doc_dir(slug) / "index.html").stat()
            return f"{st.st_mtime_ns}-{st.st_size}"
        except OSError:
            return "missing"


STORE: Store | None = None


def find_thread(data: dict, tid: str) -> dict:
    for t in data.get("threads", []):
        if t.get("id") == tid:
            return t
    raise ApiError(404, f"unknown thread: {tid}")


def find_message(thread: dict, mid: str) -> dict:
    for m in thread.get("messages", []):
        if m.get("id") == mid:
            return m
    raise ApiError(404, f"unknown message: {mid}")


def make_message(author: str, body: str) -> dict:
    return {
        "id": new_id("m"),
        "author": author,
        "body": body,
        "created_at": now_iso(),
        "edited_at": None,
        "seen_by_user": author == "user",
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "explain"

    # -- plumbing ---------------------------------------------------------

    def send_json(self, status: int, obj) -> None:
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def send_html(self, status: int, html: str) -> None:
        payload = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(413, "body too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ApiError(400, "invalid JSON body") from None
        if not isinstance(body, dict):
            raise ApiError(400, "body must be a JSON object")
        return body

    def log_message(self, fmt, *args):  # keep the log terse
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    # -- routing ----------------------------------------------------------

    def handle_one_request(self):
        if STORE is not None:
            STORE.last_http = time.time()
        super().handle_one_request()

    def dispatch(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path.startswith("/api/"):
                self.handle_api(method, path)
            elif method == "GET":
                self.handle_static(path)
            else:
                raise ApiError(405, "method not allowed")
        except ApiError as e:
            self.send_json(e.status, {"error": e.message})
        except BrokenPipeError:
            pass
        except Exception as e:  # keep the daemon alive on handler bugs
            self.send_json(500, {"error": f"internal error: {e}"})

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    # -- API --------------------------------------------------------------

    def handle_api(self, method: str, path: str) -> None:
        if path == "/api/ping" and method == "GET":
            self.send_json(200, {"ok": True, "root": str(STORE.root), "pid": os.getpid()})
            return
        if path == "/api/docs" and method == "GET":
            docs = STORE.docs()
            for d in docs:
                comments = STORE.load_comments(d["slug"])
                d["open_threads"] = sum(
                    1 for t in comments["threads"] if t.get("status") != "resolved"
                )
                d["watched"] = STORE.lease_fresh(d["slug"])
            self.send_json(200, {"docs": docs})
            return

        m = API_DOC_RE.match(path)
        if not m:
            raise ApiError(404, "unknown endpoint")
        slug, rest = m.group(1), (m.group(2) or "")
        if not SLUG_RE.match(slug):
            raise ApiError(400, "bad slug")

        if rest == "/state" and method == "GET":
            data = STORE.load_comments(slug)
            unseen = sum(
                1
                for t in data["threads"]
                for msg in t.get("messages", [])
                if msg.get("author") == "agent" and not msg.get("seen_by_user")
            )
            self.send_json(
                200,
                {
                    "rev": data.get("rev", 0),
                    "doc_etag": STORE.doc_etag(slug),
                    "watched": STORE.lease_fresh(slug),
                    "unseen_for_user": unseen,
                },
            )
            return

        if rest == "/comments" and method == "GET":
            self.send_json(200, STORE.load_comments(slug))
            return

        if rest == "/threads" and method == "POST":
            body = self.read_body()
            self.api_create_thread(slug, body)
            return

        tm = re.match(r"^/threads/([^/]+)$", rest)
        if tm:
            self.api_thread(method, slug, tm.group(1))
            return

        tm = re.match(r"^/threads/([^/]+)/messages$", rest)
        if tm and method == "POST":
            self.api_add_message(slug, tm.group(1), self.read_body())
            return

        tm = re.match(r"^/threads/([^/]+)/messages/([^/]+)$", rest)
        if tm:
            self.api_message(method, slug, tm.group(1), tm.group(2))
            return

        raise ApiError(404, "unknown endpoint")

    def api_create_thread(self, slug: str, body: dict) -> None:
        anchor = body.get("anchor") or {}
        text = (body.get("body") or "").strip()
        author = body.get("author", "user")
        if author not in VALID_AUTHORS:
            raise ApiError(400, "author must be 'user' or 'agent'")
        if not text:
            raise ApiError(400, "empty comment body")
        if not isinstance(anchor.get("exact"), str) or not anchor["exact"]:
            raise ApiError(400, "anchor.exact is required")
        thread = {
            "id": new_id("t"),
            "status": "unread" if author == "user" else "answered",
            "anchor": {
                "exact": anchor["exact"],
                "prefix": anchor.get("prefix", ""),
                "suffix": anchor.get("suffix", ""),
            },
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "messages": [make_message(author, text)],
        }

        def fn(data):
            data.setdefault("threads", []).append(thread)

        data = STORE.mutate(slug, fn)
        self.send_json(201, {"rev": data["rev"], "thread": thread})

    def api_add_message(self, slug: str, tid: str, body: dict) -> None:
        author = body.get("author", "user")
        text = (body.get("body") or "").strip()
        if author not in VALID_AUTHORS:
            raise ApiError(400, "author must be 'user' or 'agent'")
        if not text:
            raise ApiError(400, "empty message body")
        msg = make_message(author, text)

        def fn(data):
            t = find_thread(data, tid)
            t["messages"].append(msg)
            t["status"] = "unread" if author == "user" else "answered"
            t["updated_at"] = now_iso()

        data = STORE.mutate(slug, fn)
        self.send_json(201, {"rev": data["rev"], "message": msg})

    def api_thread(self, method: str, slug: str, tid: str) -> None:
        if method == "PATCH":
            action = self.read_body().get("action")
            if action not in {"resolve", "reopen", "seen"}:
                raise ApiError(400, "action must be resolve, reopen, or seen")

            def fn(data):
                t = find_thread(data, tid)
                if action == "resolve":
                    t["status"] = "resolved"
                elif action == "reopen":
                    t["status"] = "unread"
                else:  # seen
                    for msg in t["messages"]:
                        if msg.get("author") == "agent":
                            msg["seen_by_user"] = True
                t["updated_at"] = now_iso()

            data = STORE.mutate(slug, fn)
            self.send_json(200, {"rev": data["rev"]})
        elif method == "DELETE":

            def fn(data):
                find_thread(data, tid)
                data["threads"] = [t for t in data["threads"] if t["id"] != tid]

            data = STORE.mutate(slug, fn)
            self.send_json(200, {"rev": data["rev"]})
        else:
            raise ApiError(405, "method not allowed")

    def api_message(self, method: str, slug: str, tid: str, mid: str) -> None:
        if method == "PATCH":
            text = (self.read_body().get("body") or "").strip()
            if not text:
                raise ApiError(400, "empty message body")

            def fn(data):
                t = find_thread(data, tid)
                msg = find_message(t, mid)
                msg["body"] = text
                msg["edited_at"] = now_iso()
                if msg["author"] == "user":
                    t["status"] = "unread"
                t["updated_at"] = now_iso()

            data = STORE.mutate(slug, fn)
            self.send_json(200, {"rev": data["rev"]})
        elif method == "DELETE":

            def fn(data):
                t = find_thread(data, tid)
                find_message(t, mid)
                # deleting the root comment deletes the whole thread
                if t["messages"][0]["id"] == mid:
                    data["threads"] = [x for x in data["threads"] if x["id"] != tid]
                    return
                t["messages"] = [m for m in t["messages"] if m["id"] != mid]
                t["updated_at"] = now_iso()

            data = STORE.mutate(slug, fn)
            self.send_json(200, {"rev": data["rev"]})
        else:
            raise ApiError(405, "method not allowed")

    # -- static -----------------------------------------------------------

    def handle_static(self, path: str) -> None:
        if path == "/":
            self.send_html(200, render_index())
            return
        if path.startswith("/assets/"):
            self.serve_file(ASSETS_DIR, path[len("/assets/") :])
            return
        # /<slug> -> /<slug>/
        m = re.match(r"^/([a-z0-9][a-z0-9._-]{0,80})$", path)
        if m and STORE.doc_dir(m.group(1)).is_dir():
            self.send_response(301)
            self.send_header("Location", path + "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        m = re.match(r"^/([a-z0-9][a-z0-9._-]{0,80})/(.*)$", path)
        if m:
            slug, rel = m.group(1), m.group(2) or "index.html"
            self.serve_file(STORE.doc_dir(slug), rel)
            return
        raise ApiError(404, "not found")

    def serve_file(self, base: Path, rel: str) -> None:
        target = (base / rel).resolve()
        base = base.resolve()
        if base not in target.parents and target != base:
            raise ApiError(403, "forbidden")
        if not target.is_file():
            raise ApiError(404, "not found")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        payload = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else ""))
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)


def render_index() -> str:
    rows = []
    for d in STORE.docs():
        slug = d["slug"]
        comments = STORE.load_comments(slug)
        open_threads = sum(1 for t in comments["threads"] if t.get("status") != "resolved")
        dot = "ex-dot-on" if STORE.lease_fresh(slug) else "ex-dot-off"
        title = d.get("title") or slug
        kind = d.get("kind", "")
        updated = d.get("updated_at", "")
        badge = f'<span class="ex-index-badge">{open_threads}</span>' if open_threads else ""
        rows.append(
            f'<a class="ex-index-row" href="/{slug}/">'
            f'<span class="ex-dot {dot}"></span>'
            f"<span class='ex-index-title'>{title}</span>"
            f"<span class='ex-index-meta'>{kind} · {updated}</span>{badge}</a>"
        )
    body = "\n".join(rows) or '<p class="ex-index-empty">No documents yet.</p>'
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>explain · {STORE.root.parent.name}</title>
<link rel="icon" href="data:,">
<link rel="stylesheet" href="/assets/explain.css"></head>
<body class="ex-index"><h1>explain · {STORE.root.parent.name}</h1>
{body}
</body></html>"""


# -- daemon lifecycle ------------------------------------------------------


def server_json_path(root: Path) -> Path:
    return root / "server.json"


def ping(port: int, timeout: float = 1.0):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def preferred_port(root: Path) -> int:
    return PORT_BASE + zlib.crc32(str(root).encode("utf-8")) % PORT_SPAN


def cmd_serve(args) -> int:
    global STORE
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    STORE = Store(root)

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        info = ping(args.port)
        if info and info.get("root") == str(root):
            return 0  # lost the startup race to an identical server
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    write_json_atomic(
        server_json_path(root),
        {"port": port, "pid": os.getpid(), "root": str(root), "url": f"http://127.0.0.1:{port}"},
    )

    def cleanup(*_):
        info = read_json(server_json_path(root), {})
        if info.get("pid") == os.getpid():
            try:
                server_json_path(root).unlink()
            except OSError:
                pass
        os._exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    def gc_loop():
        while True:
            time.sleep(GC_INTERVAL)
            if STORE.any_fresh_lease():
                continue
            if time.time() - STORE.last_http < HTTP_IDLE_GRACE:
                continue
            sys.stderr.write("no live sessions and no recent requests; shutting down\n")
            cleanup()

    threading.Thread(target=gc_loop, daemon=True).start()
    sys.stderr.write(f"explain server on http://127.0.0.1:{port} root={root}\n")
    httpd.serve_forever()
    return 0


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


def running_server(root: Path):
    info = read_json(server_json_path(root), {})
    if not info.get("port") or not info.get("pid"):
        return None
    if not alive(info["pid"]):
        return None
    live = ping(info["port"])
    if live and live.get("root") == str(root):
        return info
    return None


def cmd_start(args) -> int:
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)

    info = running_server(root)
    already = info is not None
    if info is None:
        port = preferred_port(root)
        log = open(root / "server.log", "ab")
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "serve", "--root", str(root), "--port", str(port)],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.time() + START_WAIT
        while time.time() < deadline:
            info = running_server(root)
            if info:
                break
            time.sleep(0.2)
        if info is None:
            print(json.dumps({"error": "server did not start; see .explain/server.log"}))
            return 1

    url = info["url"] + (f"/{args.doc}/" if args.doc else "/")
    result = {"url": url, "port": info["port"], "pid": info["pid"], "already_running": already}
    if args.open:
        try:
            result["opened"] = bool(webbrowser.open(url))
        except Exception:
            result["opened"] = False
    print(json.dumps(result))
    return 0


def cmd_status(args) -> int:
    root = Path(args.root).resolve()
    info = running_server(root)
    print(json.dumps({"running": info is not None, "server": info}))
    return 0


def cmd_stop(args) -> int:
    root = Path(args.root).resolve()
    info = running_server(root)
    if info is None:
        print(json.dumps({"stopped": False, "reason": "not running"}))
        return 0
    os.kill(info["pid"], signal.SIGTERM)
    for _ in range(20):
        if not alive(info["pid"]):
            break
        time.sleep(0.1)
    print(json.dumps({"stopped": True}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("start", "serve", "status", "stop"):
        p = sub.add_parser(name)
        p.add_argument("--root", required=True, help="the project's .explain directory")
        if name == "serve":
            p.add_argument("--port", type=int, required=True)
        if name == "start":
            p.add_argument("--open", action="store_true", help="open the browser (best effort)")
            p.add_argument("--doc", help="doc slug to open")
    args = parser.parse_args()
    return {"start": cmd_start, "serve": cmd_serve, "status": cmd_status, "stop": cmd_stop}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
