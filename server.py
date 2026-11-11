"""Tiny web server: serves the two-phone demo page and streams planner events (SSE).

Run:  python3 server.py   then open http://localhost:8765
"""
import json
import os
import re
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Limits people edit in the demo page are saved here (the defaults in planner/profiles stay).
os.environ.setdefault("PLANNER_PROFILES_DIR",
                      os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "profiles"))

from planner import planner  # noqa: E402

WEB = os.path.join(os.path.dirname(__file__), "web")
API_VERSION = 2  # bump when the page needs new endpoints; the page checks it (web/index.html)
PORT = int(os.environ.get("PORT", "8765"))

CHANGE_WORDS = [
    ("swap-cafe", ("cafe", "coffee", "café")),
    ("swap-museum", ("museum", "gallery")),
    ("swap-food", ("burrito", "food", "lunch", "taco", "restaurant")),
    ("swap-beach", ("beach",)),
    ("less-walking", ("walk", "shorter", "tired", "far")),
]
CHANGE_VERBS = r"\b(swap|change|different|another|other|instead|less|shorter|tired|too far|replace)\b"


def interpret(run, who, text):
    """Turn a typed message into an action: a new plan, a vote, a change, or a hint."""
    low = text.lower().strip()
    has_plans = run is not None and bool(run.state.get("plans"))
    if who == "Me" and (not has_plans or re.search(r"\bplan (saturday|sunday|a day|my|our)\b", low)):
        return "plan", None
    if not has_plans:
        return "hint", "No plans yet. Ask your friend to start one."
    m = re.search(r"\bplan\s*([1-3])\b", low) or re.fullmatch(r"([1-3])", low)
    if re.search(CHANGE_VERBS, low):
        change = next((c for c, words in CHANGE_WORDS if any(w in low for w in words)), None)
        if change:
            # Which plan to change: the one named, else the decided one, else this person's pick.
            rank = int(m.group(1)) if m else run.state.get("decided") or run.state["votes"].get(who) or 1
            return "change", (rank, change)
    if m:
        return "vote", int(m.group(1))
    return "hint", ("Try 'plan 2' to pick a plan, or 'different beach', 'another cafe', "
                    "'less walking' to change one.")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            with open(os.path.join(WEB, "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/version":
            self._json(200, {"api": API_VERSION})
        elif url.path == "/api/profiles":
            self._json(200, {pid: planner.load_profile(pid) for pid in ("me", "ana", "biscuit")})
        elif url.path == "/api/events":
            q = parse_qs(url.query)
            run = planner.RUNS.get(q.get("run", [""])[0])
            if not run:
                return self._json(404, {"error": "unknown run"})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            i = 0
            try:
                while True:
                    with run.cond:
                        while i >= len(run.events):
                            run.cond.wait(timeout=15)
                            if i >= len(run.events):
                                break
                        batch = run.events[i:]
                    if not batch:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    for ev in batch:
                        self.wfile.write(("data: %s\n\n" % json.dumps(ev)).encode())
                    self.wfile.flush()
                    i += len(batch)  # stay open after "done": votes and decisions come later
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/api/plan":
            text = self._body().get("text", "").strip()
            if not text:
                return self._json(400, {"error": "empty message"})
            run = planner.start(text)
            self._json(200, {"run": run.id})
        elif url.path == "/api/share":
            b = self._body()
            run = planner.RUNS.get(b.get("run"))
            if not run:
                return self._json(404, {"error": "unknown run"})
            run.share_answer = bool(b.get("allow"))
            run.share_event.set()
            self._json(200, {"ok": True})
        elif url.path == "/api/profile":
            b = self._body()
            try:
                prof = planner.save_profile(b.get("id"), b.get("profile") or {})
            except (ValueError, TypeError) as e:
                return self._json(400, {"error": str(e)})
            self._json(200, {"profile": prof})
        elif url.path == "/api/vote":
            b = self._body()
            run = planner.RUNS.get(b.get("run"))
            try:
                planner.vote(run, b.get("who"), int(b.get("rank", 0)))
            except (ValueError, AttributeError) as e:
                return self._json(400, {"error": str(e)})
            self._json(200, {"ok": True})
        elif url.path == "/api/change":
            b = self._body()
            prev = planner.RUNS.get(b.get("run"))
            try:
                run = planner.request_change(prev, b.get("who"), int(b.get("rank", 0)), b.get("change"))
            except (ValueError, AttributeError) as e:
                return self._json(400, {"error": str(e)})
            self._json(200, {"run": run.id})
        elif url.path == "/api/say":
            b = self._body()
            who, text = b.get("who"), (b.get("text") or "").strip()
            run = planner.RUNS.get(b.get("run") or "")
            if who not in ("Me", "Ana") or not text:
                return self._json(400, {"error": "need who and text"})
            action, arg = interpret(run, who, text)
            try:
                if action == "plan":
                    return self._json(200, {"action": "plan", "run": planner.start(text).id})
                if action == "vote":
                    planner.vote(run, who, arg)
                    return self._json(200, {"action": "vote"})
                if action == "change":
                    new = planner.request_change(run, who, arg[0], arg[1])
                    return self._json(200, {"action": "change", "run": new.id})
            except ValueError as e:
                return self._json(200, {"action": "hint", "text": "I couldn't do that: %s." % e})
            self._json(200, {"action": "hint", "text": arg})
        else:
            self._json(404, {"error": "not found"})


if __name__ == "__main__":
    import sys
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        if e.errno == 48:  # address already in use
            sys.exit("Port %d is already in use (another planner server is probably running).\n"
                     "Stop it, or pick another port:  PORT=8766 python3 server.py" % PORT)
        raise
    mode = "routes on Archil sandboxes" if os.environ.get("PLANNER_ROUTES") == "sandboxes" else "local routes"
    print("Day planner on http://localhost:%d (%s)" % (PORT, mode))
    server.serve_forever()
