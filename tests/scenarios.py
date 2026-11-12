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

FULL = "Plan Saturday with Ana and Biscuit starting from Noe Valley: coffee, a museum, burritos, beach at sunset"
SOLO = "Plan Saturday starting from Noe Valley: coffee, a museum, burritos, beach at sunset"
SHARED_FIELDS = {"id", "name", "kind", "walking", "food", "activities", "weather", "safety"}


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
    stops = [{s["category"]: s["name"] for s in p["stops"]} for p in plans]
    diffs = [sum(a[c] != b[c] for c in a) for i, a in enumerate(stops) for b in stops[i + 1:]]
    r.check("D5", "3 different plans", len(plans) == 3 and min(diffs) >= routes.MIN_DIFFERENT_STOPS,
            "stops that differ between each pair of plans: %s" % diffs)
    travel = [p["travel_min"] for p in plans]
    r.check("D6", "Little time getting around", plans and max(travel) <= travel[0] + routes.MAX_EXTRA_TRAVEL_MIN,
            "travel minutes per plan: %s" % travel)
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
    me = [e for e in ev(run, "message") if e["phone"] == "me"]
    ana = [e for e in ev(run, "message") if e["phone"] == "ana"]
    me_plans = [e for e in me if e.get("msg_kind") == "plans"]
    ana_plans = [e for e in ana if e.get("msg_kind") == "plans"]
    # Each phone: an intro, one message per plan naming all its stops, then the question.
    def well_formed(msgs):
        parts = [e["part"] for e in msgs]
        per_plan = [e["text"] for e in msgs if e["part"] == "plan"]
        return (parts == ["intro"] + ["plan"] * len(plans) + ["question"]
                and all(s["name"] in t for p, t in zip(plans, per_plan) for s in p["stops"]))
    r.check("H2", "Both phones get the plans", plans and well_formed(me_plans) and well_formed(ana_plans)
            and any(e.get("ask") for e in ana),
            "messages per phone: %d and %d (intro + %d plans + question); written by %s"
            % (len(me_plans), len(ana_plans), len(plans), sorted({e.get("written_by") for e in me_plans + ana_plans})))


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
    if mode == "keenable" and os.environ.get("KEENABLE_API_KEY", "").strip():
        # F5: Keenable's prompted fetch reads a venue's own page for the facts.
        from planner import keenable
        a = keenable.parse_facts(keenable.fetch_facts("https://ritualcoffee.com/location/mission/",
                                                      "Ritual Coffee Roasters (Valencia)"))
        r.check("F5", "Keenable prompted fetch reads the page", a["same_place"] and a["hours"] == ("06:30", "20:00"),
                "answer: %s" % a)


def street_routing(r):
    """F4: real street routing (OSRM) gives every leg a street distance and a street path."""
    os.environ["PLANNER_ROUTING"] = "osrm"
    run, secs, _ = run_planner(FULL, "allow", timeout=180)
    st = (ev(run, "streets") or [{}])[0]
    plans = plans_of(run)
    legs = [l for p in plans for l in p["legs"]]
    no_path = [l for l in legs if not l.get("path") or len(l["path"]) < 3]
    on_streets = all(l.get("streets") for l in legs)
    # A street route can't be shorter than the straight line, nor wildly longer in a city grid.
    odd = []
    for p in plans:
        here = routes.START
        for l, s in zip(p["legs"], p["stops"]):
            straight = routes.km(here, s)
            if l["mode"] == "walk" and straight > 0.2 and not (0.95 * straight <= l["km"] <= 2.5 * straight):
                odd.append("%s %.2f km vs %.2f straight" % (s["name"][:20], l["km"], straight))
            here = s
    r.check("F4", "Street routing (OSRM)", st.get("ok") and plans and on_streets and not no_path and not odd,
            "%s; %d plans, %d legs, legs without a street path: %d, odd distances: %s; %.1f s"
            % (st.get("error") or "%d places in %s ms" % (st.get("places", 0), st.get("ms")), len(plans), len(legs),
               len(no_path), odd[:2], secs))


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


def _city_check(r, tid, name, text, box, cats, limit_km, depart, max_climb=None):
    """Live: plan in a real city (OpenStreetMap, Open-Meteo, OSRM) and check the plans honor it."""
    os.environ["PLANNER_ROUTING"] = "osrm"
    run, secs, _ = run_planner(text, "allow" if " ana" in text.lower() else None, timeout=300)
    plans = plans_of(run)
    place = (ev(run, "place") or [{}])[0]
    sf = {v["name"] for c in ("cafe", "museum", "food", "beach") for v in search.load_seed(c)}
    inside = lambda la, lo: box[0] <= la <= box[1] and box[2] <= lo <= box[3]
    problems = []
    if not place or not inside(place.get("lat", 0), place.get("lon", 0)):
        problems.append("start not in the city: %s" % place)
    if not plans:
        problems.append("no plans; messages: %s" % [e["text"][:80] for e in ev(run, "message") if e["phone"] == "me"][-2:])
    for p in plans:
        got = sorted(s["category"] for s in p["stops"])
        if got != sorted(cats):
            problems.append("plan %d stops %s, asked %s" % (p["rank"], got, cats))
        if any(not inside(s["lat"], s["lon"]) or s["name"] in sf for s in p["stops"]):
            problems.append("plan %d has a stop outside the city" % p["rank"])
        if p["walk_km"] > limit_km + 1e-6:
            problems.append("plan %d walks %.2f km > %s" % (p["rank"], p["walk_km"], limit_km))
        if p["depart"] != depart:
            problems.append("plan %d leaves at %s, asked %s" % (p["rank"], p["depart"], depart))
        if max_climb is not None and p["climb_m"] > max_climb:
            problems.append("plan %d climbs %d m" % (p["rank"], p["climb_m"]))
    r.check(tid, name, not problems, "; ".join(problems[:3]) or "%s from %s: %d plans, walks %s km, climbs %s m; %.0f s"
            % (place.get("city"), place.get("start"), len(plans), [p["walk_km"] for p in plans],
               [p["climb_m"] for p in plans], secs))
    return plans


def city_portland(r):
    """M1: the exact request that used to come back with San Francisco routes."""
    _city_check(r, "M1", "Portland: start, 2 km, park + coffee, no hills",
                "Plan Saturday with Ana in Portland Oregon, starting at St Stephen's Catholic Church. Start at 10am and "
                "walk for no more than 2km, visiting at least one park, one coffee shop, and avoiding hills.",
                (45.43, 45.60, -122.80, -122.50), ["park", "cafe"], 2.0, "10:00", max_climb=routes.HILLY_AVOID_REQUEST_M)


def city_chicago(r):
    """M2: another city, start time, miles and three stop types."""
    _city_check(r, "M2", "Chicago: 9:30, 2 miles, coffee + museum + lunch",
                "Plan Sunday in Chicago starting at Millennium Park at 9:30am: coffee, a museum and lunch, walk at most 2 miles",
                (41.80, 41.95, -87.72, -87.55), ["cafe", "museum", "food"], 3.22, "09:30")


def hills_both_ways(r):
    """M3: the same day in hilly San Francisco, once avoiding hills and once wanting them."""
    base = "Plan Saturday in San Francisco starting at Mission Dolores Park, coffee and a park, walk at most 3 km, "
    box = (37.70, 37.83, -122.52, -122.35)
    flat = _city_check(r, "M3a", "Avoid hills (San Francisco)", base + "avoiding hills",
                       box, ["cafe", "park"], 3.0, "10:00", max_climb=routes.HILLY_AVOID_REQUEST_M)
    hilly = _city_check(r, "M3b", "Want hills (San Francisco)", base + "I want some hills",
                        box, ["cafe", "park"], 3.0, "10:00")
    if flat and hilly:
        r.check("M3", "Hill preference changes the route", hilly[0]["climb_m"] > flat[0]["climb_m"] + 20,
                "plan 1 climbs %d m wanting hills vs %d m avoiding them; parks: %s vs %s" % (
                    hilly[0]["climb_m"], flat[0]["climb_m"],
                    next(s["name"] for s in hilly[0]["stops"] if s["category"] == "park"),
                    next(s["name"] for s in flat[0]["stops"] if s["category"] == "park")))


def ask_for_start(r):
    """Q2/Q4: a missing or unknown start stops everything; answering it plans, map first."""
    os.environ["PLANNER_ROUTING"] = "straight"
    explored = lambda run: [e["type"] for e in run.events if e["type"] in (
        "share_request", "checkpoint", "branch", "search_done", "plans")]
    asked = []
    for text, reason in (("Plan Saturday with Ana in Portland Oregon, coffee and a park, walk at most 2 km", "missing"),
                         ("Plan Saturday with Ana in Portland Oregon starting at Xyzzq Qwfp Plaza, coffee and a park", "not_found")):
        run, _, _ = run_planner(text, "allow", timeout=60)
        need = ev(run, "need_start")
        done = ev(run, "done")
        # An unknown place is "not_found"; if the map service is rate-limiting us, the planner says
        # "couldn't look up, try again" instead. Either way it must ask and explore nothing.
        accepted = (reason, "lookup_failed") if reason == "not_found" else (reason,)
        ok = need and need[0]["reason"] in accepted and not explored(run) and done and done[0].get("reason") == "need_start"
        asked.append((reason, bool(ok), explored(run), [e["text"][:60] for e in ev(run, "message")][-1:]))
    r.check("Q2", "Unclear start: ask, don't explore", all(a[1] for a in asked),
            "; ".join("%s: asked=%s, explored=%s, said %s" % a for a in asked))
    # Answer the question: the reply is added to the original request, then planning starts.
    import server
    first, _, _ = run_planner("Plan Saturday in Portland Oregon, coffee and a park, walk at most 2 km", None, timeout=60)
    action, text = server.interpret(first, "Me", "St Stephen's Catholic Church")
    run, _, _ = run_planner(text, None, timeout=240)
    types = [e["type"] for e in run.events]
    place_at = types.index("place") if "place" in types else 10 ** 6
    first_work = min([types.index(t) for t in ("share_request", "branch", "search_done") if t in types] or [10 ** 6])
    plans = plans_of(run)
    r.check("Q4", "Map moves to the start before any planning",
            action == "plan" and place_at < first_work and plans
            and routes.km(ev(run, "place")[0], {"lat": 45.5145, "lon": -122.6197}) < 1.0,
            "reply became: %r; start %s; place event #%s, first planning event #%s; %d plans"
            % (text[-60:], (ev(run, "place") or [{}])[0].get("start"), place_at, first_work, len(plans)))


USER_PORTLAND = ("Plan Saturday starting at St Stephens Catholic Church in Portland, visit a coffee shop, a park and a "
                 "restaurant. Start time is 10am. Finish time is no later than 3pm. Must not walk more than 3km.")


def replay_portland(r):
    """Q5: the conversation that kept answering "I couldn't find ...", replayed."""
    os.environ["PLANNER_ROUTING"] = "osrm"
    church = {"lat": 45.5145, "lon": -122.6197}
    problems, notes = [], []
    for label, text in (("as typed", USER_PORTLAND),
                        ("with the address reply", USER_PORTLAND + " Starting at 4055 SE Salmon St Portland Or 97214.")):
        run, secs, _ = run_planner(text, None, timeout=300)
        place = (ev(run, "place") or [{}])[0]
        plans = plans_of(run)
        if not place or routes.km(place, church) > 1.0:
            problems.append("%s: start %s" % (label, place.get("start") or [e["text"][:70] for e in ev(run, "message")][-1:]))
            continue
        for p in plans:
            if sorted(s["category"] for s in p["stops"]) != ["cafe", "food", "park"]:
                problems.append("%s: plan %d stops %s" % (label, p["rank"], [s["category"] for s in p["stops"]]))
            if routes.to_min(p["end"]) > 15 * 60 or p["walk_km"] > 3.0 or p["depart"] != "10:00":
                problems.append("%s: plan %d ends %s, walks %.2f km" % (label, p["rank"], p["end"], p["walk_km"]))
        if not plans:
            problems.append("%s: no plans: %s" % (label, [e["text"][:70] for e in ev(run, "message")][-1:]))
        notes.append("%s: %s, %d plans ending %s" % (label, place.get("start"), len(plans), [p["end"] for p in plans]))
    r.check("Q5", "Your Portland conversation, replayed", not problems, "; ".join(problems[:3]) or "; ".join(notes))


def claude_parsing(r):
    """L1: the model (OpenAI by default) reads the tricky phrasings that tripped the rules."""
    from planner import understand
    os.environ.pop("PLANNER_PARSER", None)
    if not understand.available():
        r.check("L1", "Model parses requests", None, "SKIPPED: needs OPENAI_API_KEY or ANTHROPIC_API_KEY in .env")
        r.rows[-1]["ok"] = None
        return
    cases = [
        (USER_PORTLAND, {"city": "Portland", "start_place": "St Stephens Catholic Church", "start_min": 600,
                         "end_min": 900, "max_walk_km": 3.0, "categories": ["cafe", "park", "food"]}),
        ("Plan Sunday in Chicago starting at Millennium Park at 9:30am: coffee, a museum and lunch, walk at most 2 miles",
         {"city": "Chicago", "start_place": "Millennium Park", "start_min": 570, "categories": ["cafe", "museum", "food"]}),
        ("Saturday with Ana, Portland Oregon. Let's meet at 4055 SE Salmon St and grab coffee then hit a park, "
         "nothing hilly, home by 2", {"start_place": "4055 SE Salmon St", "avoid_hills": True,
                                      "categories": ["cafe", "park"], "with": ["ana"]}),
        (KOREAN_TRUCK, {"city": "Portland", "start_place": "St Stephens Catholic Church", "start_min": 600,
                        "end_min": 900, "categories": ["cafe", "park", "food"], "cuisines": ["korean", "japanese"],
                        "food_truck": True, "return_to_start": True, "with": ["ana"]}),
    ]
    bad = []
    for text, want in cases:
        got = understand.understand(text)
        diffs = {k: (got.get(k), v) for k, v in want.items()
                 if (round(got.get(k) or 0, 1) != round(v, 1) if isinstance(v, float) else got.get(k) != v)}
        if got["parsed_by"] not in ("openai", "claude") or diffs:
            bad.append("%s...: %s %s" % (text[:30], got["parsed_by"], diffs))
    reply = understand.read_reply("let's go with the second one", "Which plan, 1, 2 or 3?")
    if (reply or {}).get("plan") != 2:
        bad.append("pick 'the second one' read as %s" % reply)
    start = understand.read_reply("Let's start at 4055 SE Salmon St please", "Where should we start from?")
    if "4055 SE Salmon St" not in (start or {}).get("start_place", ""):
        bad.append("start reply read as %s" % start)
    r.check("L1", "Model parses requests", not bad, "; ".join(bad) or "%s (%s): %d phrasings and 2 replies read correctly"
            % (understand.LABEL[understand.provider()], understand.model_name(understand.provider()), len(cases)))


def subq_decides(r):
    """J4: SubQ Snap picks the plans for a real request (needs SUBQ_API_KEY)."""
    from planner import decide
    if not os.environ.get("SUBQ_API_KEY", "").strip():
        r.check("J4", "SubQ Snap decides", None, "SKIPPED: needs SUBQ_API_KEY in .env")
        r.rows[-1]["ok"] = None
        return
    os.environ["PLANNER_DECIDER"] = "subq"
    run, secs, _ = run_planner(FULL, "allow", timeout=120)
    d = (ev(run, "decided") or [{}])[0]
    plans = plans_of(run)
    r.check("J4", "SubQ Snap decides", d.get("by") == "SubQ Snap 1.1" and plans
            and all("SubQ Snap" in (p.get("why") or "") for p in plans),
            "decided by %s %s; %d plans; first reason: %s" % (d.get("by"), d.get("note") or "", len(plans),
                                                             (plans[0].get("why") if plans else "")[:120]))


KOREAN_TRUCK = ("Propose a Saturday plan with Ana starting at St Stephens Catholic Church in Portland that includes a "
                "visit to a coffee place, a dog-friendly park, and a food truck with Korean or Japanese food. Starting "
                "at 10am and must be back at the place of departure by 3pm.")


def korean_food_truck(r):
    """M4: the request that came back with Pepino's (Mexican, not a truck), replayed."""
    from planner import places
    os.environ["PLANNER_ROUTING"] = "osrm"
    run, secs, _ = run_planner(KOREAN_TRUCK, "allow", timeout=300)
    plans = plans_of(run)
    place = (ev(run, "place") or [{}])[0]
    cands = {v["name"]: v for v in (run.state.get("candidates") or {}).get("food", [])}
    problems = []
    if not place or routes.km(place, {"lat": 45.5145, "lon": -122.6197}) > 1.0:
        problems.append("start not Saint Stephen, Portland OR: %s" % place.get("start"))
    for p in plans:
        food = next(s["name"] for s in p["stops"] if s["category"] == "food")
        v = cands.get(food, {})
        if not set(v.get("cuisine") or []) & {"korean", "japanese", "sushi", "ramen"}:
            problems.append("plan %d food %s is %s" % (p["rank"], food, v.get("cuisine")))
        if not p.get("return_leg") or routes.to_min(p["end"]) > 15 * 60:
            problems.append("plan %d: back at start %s by %s" % (p["rank"], bool(p.get("return_leg")), p["end"]))
    trucks = [p for p in plans if cands.get(next(s["name"] for s in p["stops"] if s["category"] == "food"), {}).get("food_truck")]
    if not plans:
        problems.append("no plans: %s" % [e["text"][:70] for e in ev(run, "message")][-1:])
    r.check("M4", "Korean or Japanese food truck, back by 3pm", not problems,
            "; ".join(problems[:3]) or "%d plans, %d with a food truck; food: %s; back by %s; %.0f s" % (
                len(plans), len(trucks), [next(s["name"] for s in p["stops"] if s["category"] == "food") for p in plans],
                [p["end"] for p in plans], secs))


SCENARIOS = {
    "korean-food-truck": (korean_food_truck, "test-base"),
    "subq-decides": (subq_decides, "after-search"),
    "claude-parsing": (claude_parsing, "test-base"),
    "replay-portland": (replay_portland, "test-base"),
    "ask-start": (ask_for_start, "test-base"),
    "city-portland": (city_portland, "test-base"),
    "city-chicago": (city_chicago, "test-base"),
    "hills-both-ways": (hills_both_ways, "test-base"),
    "street-routing": (street_routing, "after-search"),
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
