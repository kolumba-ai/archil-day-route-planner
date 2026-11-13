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

from planner import planner, understand  # noqa: E402

WEB = os.path.join(os.path.dirname(__file__), "web")
API_VERSION = 3  # bump when the page needs new endpoints; the page checks it (web/index.html)
PORT = int(os.environ.get("PORT", "8765"))

def interpret(run, who, text):
    """Turn a typed message into an action: a new plan, a pick (1, 2 or 3), or a hint.

    Plans can't be changed: people only choose one by texting its number.
    """
    low = text.lower().strip()
    has_plans = run is not None and bool(run.state.get("plans"))
    fresh = re.search(r"\bplan (monday|tuesday|wednesday|thursday|friday|saturday|sunday|a day|my|our)\b", low)
    if who == "Me" and run is not None and run.state.get("needs_confirm") and not fresh:
        # The answer to "Here's what I understood ... Reply yes": go ahead, or apply the change
        # (the model re-reads the request with the correction and asks again).
        if re.fullmatch(r"(yes|yep|yeah|y|ok|okay|sure|go|go ahead|send( it)?|confirm(ed)?|correct|"
                        r"looks good|sounds good|perfect|👍)[.! ]*", low):
            return "plan_confirmed", run.state["original"]
        return "plan", "%s. %s" % (run.state["original"].rstrip(". "), text.strip())
    if who == "Me" and run is not None and run.state.get("needs_start") and not fresh:
        # The answer to "Where should we start from?": add it to the original request.
        place = re.sub(r"^(?:let'?s\s+)?(?:start(?:ing)?\s+)?(?:at|from|in)?\s*", "", text.strip(), flags=re.I).rstrip(".!")
        ai = understand.read_reply(text, "Where should we start from?")
        if ai and ai.get("start_place"):
            place = ai["start_place"].rstrip(".!")
        return "plan", "%s. Starting at %s." % (run.state["original"].rstrip(". "), place)
    if who == "Ana" and run is not None and run.state.get("awaiting_share"):
        # Ana answers the invitation by typing: yes or no; anything else goes to the caller.
        if re.fullmatch(r"(yes|yep|yeah|y|ok|okay|sure|accept(ed)?|i'?m in|count me in|sounds good|👍)[.! ]*", low):
            return "share", True
        if re.fullmatch(r"(no|nope|n|decline(d)?|can'?t|cannot|not this time|pass|👎)[.! ]*", low):
            return "share", False
        return "relay", text.strip()
    if who == "Me" and (not has_plans or fresh):
        return "plan", text
    if not has_plans:
        return "hint", "No plans yet. Ask your friend to start one."
    n = len(run.state["plans"])
    nums = [str(i) for i in range(1, n + 1)]
    choices = nums[0] if n == 1 else ", ".join(nums[:-1]) + " or " + nums[-1]
    if run.state.get("decided"):
        return "hint", "Plan %d is already decided." % run.state["decided"]
    m = re.fullmatch(r"(?:plan\s*)?#?\s*([1-9])\s*[.!]?", low)
    if m and 1 <= int(m.group(1)) <= n:
        return "vote", int(m.group(1))
    ai = understand.read_reply(text, "Which plan, %s?" % choices)  # "the second one", "plan two works"
    if ai and 1 <= int(ai.get("plan") or 0) <= n:
        return "vote", int(ai["plan"])
    return "hint", "Just reply with the plan number: %s." % choices


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
            try:
                planner.answer_share(run, b.get("allow"))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
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
        elif url.path == "/api/say":
            b = self._body()
            who, text = b.get("who"), (b.get("text") or "").strip()
            run = planner.RUNS.get(b.get("run") or "")
            if who not in ("Me", "Ana") or not text:
                return self._json(400, {"error": "need who and text"})
            action, arg = interpret(run, who, text)
            try:
                if action == "plan":
                    return self._json(200, {"action": "plan", "run": planner.start(arg or text).id})
                if action == "plan_confirmed":
                    return self._json(200, {"action": "plan", "run": planner.start(arg, confirmed=True).id})
                if action == "vote":
                    planner.vote(run, who, arg)
                    return self._json(200, {"action": "vote"})
                if action == "share":
                    planner.answer_share(run, arg)
                    return self._json(200, {"action": "share"})
                if action == "relay":  # a message for the other person: shown on their phone
                    run.say("me" if who == "Ana" else "ana", "💬 %s: %s" % ("Ana" if who == "Ana" else "Your friend", arg),
                            agent=True)
                    return self._json(200, {"action": "relay", "text": "Sent to %s." % ("your friend" if who == "Ana" else "Ana")})
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
