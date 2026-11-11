"""Scenario tests (C, D5, E, F, G, H). Each scenario runs the whole planner inside its
own disk branch, launched by run_tests.py:

    python3 -m tests.scenarios <scenario-name>

It prints one JSON line: [{"id", "name", "ok", "detail"}, ...].
"""
import json
import os
import sys
import threading
import time

from planner import planner, routes, search

FULL = "Plan Saturday with Ana and Biscuit: coffee, a museum, burritos, beach at sunset"
SOLO = "Plan Saturday: coffee, a museum, burritos, beach at sunset"
SHARED_FIELDS = {"id", "name", "kind", "walking", "food", "activities", "weather"}


def run_planner(text, answer="allow", timeout=60):
    """Run one plan synchronously. answer: 'allow', 'decline' or None (never answer)."""
    run = planner.Run()
    t0 = time.time()
    th = threading.Thread(target=planner._safe_plan, args=(run, text), daemon=True)
    th.start()
    answered_at = None
    while th.is_alive() and time.time() - t0 < timeout:
        if answer and answered_at is None and any(e["type"] == "share_request" for e in run.events):
            answered_at = len(run.events)
            run.share_answer = answer == "allow"
            run.share_event.set()
        time.sleep(0.05)
    th.join(1)
    return run, time.time() - t0, answered_at


def ev(run, kind):
    return [e for e in run.events if e["type"] == kind]


def plans_of(run):
    p = ev(run, "plans")
    return p[0]["plans"] if p else []


def outing_dir(run):
    return os.path.join(planner.DATA, "outing-" + run.id)


def ana_shard(run):
    path = os.path.join(outing_dir(run), "work", "shards", "ana.json")
    with open(path) as f:
        return json.load(f)


class Results:
    def __init__(self):
        self.rows = []

    def check(self, tid, name, ok, detail=""):
        self.rows.append({"id": tid, "name": name, "ok": bool(ok), "detail": str(detail)[:200]})


# ------------------------------------------------------------------ scenarios

def dog_ana_allow(r):
    run, secs, answered_at = run_planner(FULL, "allow")
    plans = plans_of(run)
    lim = ev(run, "combined")[0]["limits"]
    r.check("C1", "No dog conflicts", plans and all(
        not any("dog" in v for v in p["violations"]) for p in plans) and all(
        s["name"] != "China Beach" for p in plans for s in p["stops"]),
        [s["name"] for p in plans for s in p["stops"]])
    seed = {v["name"]: v for c in ("cafe", "food") for v in search.load_seed(c)}
    r.check("C2", "Diet", all(seed.get(s["name"], {}).get("vegetarian_ok", True)
                              for p in plans for s in p["stops"] if s["category"] in ("cafe", "food")))
    r.check("C3", "Walking limit", all(p["walk_km"] <= lim["max_km"] for p in plans),
            [p["walk_km"] for p in plans])
    r.check("C4", "Hills", all(p["climb_m"] <= 200 for p in plans), [p["climb_m"] for p in plans])
    r.check("C5", "Hours", all(not p["violations"] for p in plans))
    museums = [p["stops"][1]["name"] if p["stops"][1]["category"] == "museum" else p["stops"][2]["name"] for p in plans]
    beaches = [p["stops"][-1]["name"] for p in plans]
    other = [(p["stops"][0]["name"], next(s["name"] for s in p["stops"] if s["category"] == "food")) for p in plans]
    r.check("D5", "3 different plans", len(plans) == 3 and len(set(museums)) == 3 and len(set(beaches)) == 3
            and len(set(other)) > 1, "cafe+food pairs: %s" % other)
    cps = [e["name"] for e in ev(run, "checkpoint")]
    r.check("E1", "Checkpoints exist", set(cps) >= {"combined", "candidates", "plan-1", "plan-2", "plan-3"}, cps)
    br_root = os.path.join(outing_dir(run), "branches")
    inherited = all(os.path.exists(os.path.join(br_root, b, f))
                    for b in os.listdir(br_root) for f in ("request.json", "conditions.json", "shards/me.json"))
    r.check("E3", "Inheritance", inherited, "%d branches checked" % len(os.listdir(br_root)))
    st = ev(run, "storage")[0]
    r.check("E4", "Cost", st["delta_bytes"] < st["full_copy_bytes"], st)
    fork = [e["ms"] for e in ev(run, "branch")]
    avg = sum(fork) / len(fork)
    r.check("E5", "Fork speed (local < 300 ms)", avg < 300, "avg %.0f ms over %d forks" % (avg, len(fork)))
    shard_seq = ev(run, "shard")[0]["seq"]
    r.check("G1", "Nothing before consent", answered_at is not None and shard_seq >= answered_at,
            "answered at event %s, shard at %s" % (answered_at, shard_seq))
    r.check("G3", "Fields shared", set(ana_shard(run).keys()) == SHARED_FIELDS, sorted(ana_shard(run)))
    r.check("H1", "Speed (offline < 10 s)", secs < 10 and plans, "%.1f s" % secs)
    me = [e["text"] for e in ev(run, "message") if e["phone"] == "me"]
    ana = [e for e in ev(run, "message") if e["phone"] == "ana"]
    r.check("H2", "Both phones", any("plans for Saturday" in t for t in me)
            and any(e.get("ask") for e in ana) and any("Plans for Saturday" in e["text"] for e in ana))


def ana_decline(r):
    """Declining the invitation: Ana isn't coming, nothing of hers is shared, plans go on without her."""
    run, _, _ = run_planner(FULL, "decline")
    me = " ".join(e["text"] for e in ev(run, "message") if e["phone"] == "me")
    shard_path = os.path.join(outing_dir(run), "work", "shards", "ana.json")
    plans = plans_of(run)
    in_scores = any("Ana" in p["scores"] for p in plans)
    voters = (ev(run, "vote_open") or [{}])[0].get("voters", [])
    r.check("G2", "Decline", plans and "Ana declined the invitation." in me and not os.path.exists(shard_path)
            and not in_scores and voters == ["Me"],
            "plans=%d; ana shard on disk=%s; Ana scored=%s; voters=%s"
            % (len(plans), os.path.exists(shard_path), in_scores, voters))


def ana_timeout(r):
    run, secs, _ = run_planner(FULL, None, timeout=30)
    me = " ".join(e["text"] for e in ev(run, "message") if e["phone"] == "me")
    shard = ana_shard(run)
    r.check("G4", "Timeout", plans_of(run) and "didn't answer" in me
            and shard["food"]["loves"] != planner.load_profile("ana")["food"]["loves"],
            "%.1f s; message: %s" % (secs, me[:120]))


def solo(r):
    run, _, _ = run_planner(SOLO)
    r.check("A4e", "No friend: never asks Ana's agent", not ev(run, "share_request") and plans_of(run))


def nothing_fits(r):
    # This branch edits its own copy of the recorded search results: every beach bans dogs.
    path = os.environ["PLANNER_CANDIDATES"]
    with open(path) as f:
        cands = json.load(f)
    for b in cands["beach"]:
        b["dogs"] = "no"
    with open(path, "w") as f:
        json.dump(cands, f)
    run, _, _ = run_planner(FULL, "allow")
    me = " ".join(e["text"] for e in ev(run, "message") if e["phone"] == "me")
    r.check("C6", "No impossible plans", not plans_of(run) and "No plan fits" in me, me[-120:])


def seed_fallback(r):
    run, _, _ = run_planner(SOLO)
    srcs = [e["source"] for e in ev(run, "search_done")]
    r.check("F1", "Seed fallback", len(srcs) == 4 and all(s == "seed data" for s in srcs) and plans_of(run), srcs)


def bad_search(r):
    search.live_search_available = lambda: True

    def broken(category, context):
        raise ValueError("no JSON array in reply")
    search._claude_search = broken
    run, _, _ = run_planner(SOLO)
    srcs = [e["source"] for e in ev(run, "search_done")]
    r.check("F3", "Bad responses fall back", len(srcs) == 4 and all(s == "seed data" for s in srcs)
            and plans_of(run), srcs)


def crash(r):
    def boom(*a, **k):
        raise RuntimeError("simulated crash")
    routes.combine = boom
    run, secs, _ = run_planner(SOLO, timeout=10)
    me = " ".join(e["text"] for e in ev(run, "message") if e["phone"] == "me")
    done = ev(run, "done")
    r.check("H6", "Errors are visible", done and done[0]["ok"] is False and "went wrong" in me, me[-100:])


def live_search(r):
    """F2 with whichever live provider is configured (Keenable or Claude web search)."""
    mode = os.environ.get("PLANNER_SEARCH", "")
    expected = {"keenable": "Keenable search"}.get(mode, "Claude web search")
    if mode != "keenable" and not search.live_search_available():
        r.check("F2", "Live search", None, "SKIPPED: set PLANNER_SEARCH=keenable or ANTHROPIC_API_KEY")
        r.rows[-1]["ok"] = None
        return
    ctx = routes.combine([planner.load_profile("me")], planner.parse_request(SOLO))
    bad, summary = [], []
    for cat in ("cafe", "museum", "food", "beach"):
        venues, src = search.search_category(cat, ctx)
        usable = [v for v in venues if 37.70 < v["lat"] < 37.83 and -122.53 < v["lon"] < -122.35
                  and "open" in v and "dogs" in v and v.get("source", "").startswith("http")]
        checked = [v for v in usable if v.get("verified")] if mode == "keenable" else usable
        summary.append("%s %d/%d" % (cat, len(checked), len(venues)))
        if src != expected or len(checked) < 3:
            bad.append("%s: %s, %d usable" % (cat, src, len(checked)))
    r.check("F2", "Live search (%s)" % expected, not bad, bad or "verified with sources: " + ", ".join(summary))


def sandbox_routes(r):
    """E8: the route phase on Archil sandboxes gives the same plans as the local route phase."""
    if not os.environ.get("ARCHIL_API_KEY"):
        r.check("E8", "Routes on Archil sandboxes", None, "SKIPPED: needs ARCHIL_API_KEY in .env")
        r.rows[-1]["ok"] = None
        return
    os.environ.pop("PLANNER_ROUTES", None)
    local, _, _ = run_planner(FULL, "allow")
    os.environ["PLANNER_ROUTES"] = "sandboxes"
    remote, secs, _ = run_planner(FULL, "allow", timeout=300)
    sbx = [e for e in ev(remote, "branch") if e.get("sandbox")]
    errors = [e["dropped"][0] for e in ev(remote, "route_done") if e["dropped"] and "sandbox error" in e["dropped"][0]]
    kept = [e.get("sandbox") for e in ev(remote, "checkpoint") if e["name"].startswith("plan-")]
    same = [[s["name"] for s in p["stops"]] for p in plans_of(local)] == \
           [[s["name"] for s in p["stops"]] for p in plans_of(remote)]
    timing = (ev(remote, "sandbox_timing") or [{}])[0]
    r.check("E8", "Routes on Archil sandboxes", len(sbx) == 35 and not errors and len(kept) == 3
            and all(kept) and same,
            "%d sandboxes, fan-out %.1f s, plan %.1f s, same plans as local: %s, errors: %s"
            % (len(sbx), timing.get("fanout_s", 0), secs, same, errors[:1]))


SCENARIOS = {
    "sandbox-routes": (sandbox_routes, "after-search"),
    "dog-ana-allow": (dog_ana_allow, "after-search"),
    "ana-decline": (ana_decline, "after-search"),
    "ana-timeout": (ana_timeout, "after-search"),
    "solo": (solo, "after-search"),
    "nothing-fits": (nothing_fits, "after-search"),
    "seed-fallback": (seed_fallback, "test-base"),
    "bad-search": (bad_search, "test-base"),
    "crash": (crash, "test-base"),
    "live-search": (live_search, "test-base"),
}


if __name__ == "__main__":
    name = sys.argv[1]
    res = Results()
    try:
        SCENARIOS[name][0](res)
    except Exception as e:
        import traceback
        traceback.print_exc()
        res.check("ERR", "scenario %s crashed" % name, False, repr(e))
    print("RESULTS " + json.dumps(res.rows))
