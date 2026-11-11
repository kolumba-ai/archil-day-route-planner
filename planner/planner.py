"""Orchestrates one outing: combine shards -> search branches -> route branches -> judge."""
import json
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import present, routes, search, streets
from .disk import OutingDisk

HERE = os.path.dirname(__file__)
DATA = os.environ.get("PLANNER_DATA", os.path.join(os.path.dirname(HERE), "data"))
SHARE_TIMEOUT = float(os.environ.get("PLANNER_SHARE_TIMEOUT", "120"))
RECORDED = os.environ.get("PLANNER_CANDIDATES")  # tests: reuse search results from a checkpoint
CATEGORY_WORDS = [
    ("cafe", ("coffee", "cafe", "café")),
    ("museum", ("museum", "gallery")),
    ("food", ("burrito", "lunch", "dinner", "food", "taco", "restaurant")),
    ("beach", ("beach", "sunset", "ocean")),
]


PROFILE_FIELDS = ("walking", "food", "activities", "weather")


def load_profile(pid):
    """A person's or pet's profile. Limits edited in the demo page (saved under
    PLANNER_PROFILES_DIR, which the server sets) override the defaults in profiles/."""
    with open(os.path.join(HERE, "profiles", pid + ".json")) as f:
        prof = json.load(f)
    override_dir = os.environ.get("PLANNER_PROFILES_DIR")
    path = os.path.join(override_dir, pid + ".json") if override_dir else None
    if path and os.path.exists(path):
        with open(path) as f:
            edits = json.load(f)
        for k, v in edits.items():
            if isinstance(v, dict) and isinstance(prof.get(k), dict):
                prof[k].update(v)
            elif k in prof:
                prof[k] = v
    return prof


def save_profile(pid, edits):
    """Save edited limits for one profile (only known fields are kept)."""
    override_dir = os.environ.get("PLANNER_PROFILES_DIR")
    if not override_dir or pid not in ("me", "ana", "biscuit"):
        raise ValueError("cannot save profile %r" % pid)
    base = load_profile(pid)
    keep = {k: v for k, v in edits.items() if k in base and k not in ("id", "kind", "name")}
    os.makedirs(override_dir, exist_ok=True)
    with open(os.path.join(override_dir, pid + ".json"), "w") as f:
        json.dump(keep, f, indent=1)
    return load_profile(pid)


def default_friend(pid, name):
    """Cautious limits used when a friend does not share theirs. Reads nothing from their profile."""
    return {
        "id": pid, "name": name, "kind": "person",
        "walking": {"max_km": 5, "hills": "avoid", "pace_kmh": 4.0, "rest_every_min": 45},
        "food": {"diet": "vegetarian", "loves": [], "avoids": []},
        "activities": {"museum_max_min": 60, "likes": [], "dislikes": ["crowds"]},
        "weather": {"cold_tolerance": "low", "wind_tolerance": "low"},
    }


def parse_request(text):
    low = text.lower()
    cats = [c for c, words in CATEGORY_WORDS if any(w in low for w in words)]
    if len(cats) < 4:
        cats = ["cafe", "museum", "food", "beach"]  # v1 always plans the full day shape
    return {
        "text": text,
        "day": "Saturday",
        "categories": cats,
        "with": ["ana"] if "ana" in low else [],
        "dog": any(w in low for w in ("dog", "biscuit")),
    }


class Run:
    """One planning request; the UI streams its events."""

    def __init__(self, parent=None):
        self.id = uuid.uuid4().hex[:8]
        self.parent = parent  # the run this one re-plans, if any
        self.events = []
        self.cond = threading.Condition()
        self.share_answer = None
        self.share_event = threading.Event()
        self.state = {}  # req, ctx, candidates, plans, votes: what later changes and votes need
        self.started = time.time()

    def emit(self, kind, **data):
        with self.cond:
            data.update(type=kind, t=round(time.time(), 3), seq=len(self.events))
            self.events.append(data)
            self.cond.notify_all()

    def say(self, phone, text, **extra):
        self.emit("message", phone=phone, text=text, **extra)


RUNS = {}


def start(text):
    run = Run()
    RUNS[run.id] = run
    threading.Thread(target=_safe_plan, args=(run, text), daemon=True).start()
    return run


def _safe_plan(run, text):
    try:
        plan(run, text)
    except Exception as e:  # surface failures in the UI instead of hanging
        import traceback
        traceback.print_exc()
        run.say("me", "Something went wrong while planning: %s" % e)
        run.emit("done", ok=False)


def plan(run, text):
    req = parse_request(text)
    root = os.path.join(DATA, "outing-" + run.id)
    shutil.rmtree(root, ignore_errors=True)
    disk = OutingDisk(root, run.emit)
    run.emit("backend", name=disk.backend.name + (" · routes: one Archil sandbox each"
                                                  if os.environ.get("PLANNER_ROUTES") == "sandboxes" else ""),
             search=search.provider_label())
    run.say("me", "On it. Checking Saturday's weather" + (" and asking Ana's agent for her limits." if req["with"] else "."), agent=True)

    # --- Phase 1: gather shards (Ana's comes from her agent, with consent)
    shards = [load_profile("me")]
    if req["dog"]:
        shards.append(load_profile("biscuit"))
    if "ana" in req["with"]:
        run.emit("share_request", frm="Me", to="Ana",
                 asks=["walking limits", "hills", "diet", "museum tolerance", "weather tolerance"])
        run.say("ana", "Your friend is inviting you to several activities on Saturday, "
                       "taking into account your limits. Accept?", agent=True, ask=True)
        run.share_event.wait(timeout=SHARE_TIMEOUT)
        if run.share_answer is False:
            # Declining the invitation means Ana isn't coming: plan without her.
            run.say("me", "Ana declined the invitation.", agent=True)
            run.say("ana", "You declined the invitation. Nothing was shared.", agent=True)
            req["with"] = [w for w in req["with"] if w != "ana"]
            run.emit("declined", who="Ana")
        else:
            if run.share_answer is None:
                run.say("me", "Ana didn't answer in time; planning with cautious defaults for her.", agent=True)
                ana = default_friend("ana", "Ana")
            else:
                ana = load_profile("ana")
                run.say("ana", "Accepted, and sent over your limits.", agent=True)
            summary = {k: ana[k] for k in ("id", "name", "kind", "walking", "food", "activities", "weather")}
            shards.append(summary)
            run.emit("shard", owner="Ana", fields=sorted(summary.keys()))

    ctx = routes.combine(shards, req)
    run.state.update(req=req, ctx=ctx)
    run.emit("combined", limits=ctx["limits"])
    work = disk.work
    disk.write_json(work, "request.json", req)
    for s in shards:
        disk.write_json(work, "shards/%s.json" % s["id"], s)
    disk.write_json(work, "conditions.json", ctx["conditions"])
    disk.checkpoint(work, "combined")

    # --- Phase 2: one search branch per stop type
    def search_branch(cat):
        name = "search-" + cat
        path = disk.branch("combined", name)
        if RECORDED:
            with open(RECORDED) as f:
                venues, source = json.load(f)[cat], "recorded search results"
        else:
            venues, source = search.search_category(cat, ctx)
        n = disk.write_json(path, "candidates/%s.json" % cat, venues)
        disk.record_write(name, n)
        run.emit("search_done", branch=name, category=cat, count=len(venues), source=source,
                 venues=[v["name"] for v in venues])
        return cat, venues

    with ThreadPoolExecutor(max_workers=4) as pool:
        candidates = dict(pool.map(search_branch, req["categories"]))
    disk.write_json(work, "candidates.json", candidates)
    disk.checkpoint(work, "candidates")
    run.state["candidates"] = candidates
    run.say("me", "Found %d places. Simulating routes in parallel branches..." %
            sum(len(v) for v in candidates.values()), agent=True)
    route_and_finish(run, req, disk, ctx, candidates)


def route_and_finish(run, req, disk, ctx, candidates):
    """Phases 3 and 4: simulate routes (sandboxes or local branches), judge, report."""
    work = disk.work
    # Real walking distances and driving times between every pair of places, fetched once
    # so the route branches (and sandboxes) never call the router themselves.
    ctx.pop("streets", None)
    if streets.enabled():
        t = time.time()
        points = [routes.START] + [v for vs in candidates.values() for v in vs]
        try:
            ctx["streets"] = streets.build_matrix(points)
            disk.write_json(work, "streets.json", ctx["streets"], compact=True)
            run.emit("streets", ok=True, places=len(ctx["streets"]["walk_km"]), ms=round((time.time() - t) * 1000),
                     source=ctx["streets"]["source"])
        except Exception as e:
            run.emit("streets", ok=False, error=str(e)[:120])
            run.say("me", "Street routing is unavailable (%s); estimating distances in straight lines." % str(e)[:80],
                    agent=True)
    pairs = [(m, b) for m in candidates["museum"] for b in candidates["beach"]]
    chosen = None
    if os.environ.get("PLANNER_ROUTES") == "sandboxes":
        try:
            chosen = routes_on_sandboxes(run, ctx, candidates)
        except Exception as e:
            run.say("me", "Archil sandboxes unavailable (%s); simulating routes here instead." % str(e)[:120], agent=True)
    if chosen is None:
        chosen = routes_locally(run, disk, ctx, candidates, pairs)
    finish(run, req, disk, work, chosen)


def routes_on_sandboxes(run, ctx, candidates):
    """Phase 3 + 4 with one Archil sandbox per route branch. Returns the judge's picks."""
    from . import sandbox_routes
    idx = [(i, j) for i in range(len(candidates["museum"])) for j in range(len(candidates["beach"]))]
    run.say("me", "Forking %d Archil sandboxes, one per route..." % len(idx), agent=True)
    run.emit("sandbox_phase", count=len(idx))
    out = sandbox_routes.run_route_phase(run.id, ctx, candidates, idx)
    # Timings come back as offsets from the controller's start; place them on our clock.
    t_base = time.time() - out["timing"]["total_s"]
    tm = out["timing"]
    if "base_t0" in tm:
        run.emit("sandbox_base", name=out["kept"][-1] if out["kept"] else "base",
                 start=round(t_base + tm["base_t0"], 3), end=round(t_base + tm["base_t0"] + tm["base_s"], 3))
    written = 0
    for r in out["results"]:
        start = t_base + r.get("t_start", 0)
        run.emit("branch", name=r["branch"], parent="candidates (sandbox)", ms=round(r["fork_s"] * 1000, 1),
                 sandbox=True, start=round(start, 3), fork_end=round(start + r["fork_s"], 3),
                 end=round(start + r["fork_s"] + r.get("run_s", 0), 3),
                 kept=r["branch"] in out["kept"])
        if "error" in r:
            run.emit("route_done", branch=r["branch"], museum="?", beach="?", best=0, feasible=0,
                     dropped=["sandbox error: " + r["error"][:80]])
            continue
        best = r["best"]
        written += len(json.dumps({"best": best[0] if best else None, "dropped": r["dropped"]},
                                  separators=(",", ":")))
        run.emit("route_done", branch=r["branch"], museum=r["museum"], beach=r["beach"],
                 best=best[0]["group_score"] if best else 0, feasible=r["feasible"], dropped=r["dropped"],
                 run_s=round(r["run_s"], 2))
    base = len(json.dumps({"ctx": ctx, "candidates": candidates}))
    n = len(out["results"])
    run.emit("storage", branches=n, base_bytes=base, delta_bytes=written, full_copy_bytes=base * n)
    for i, c in enumerate(out["chosen"], 1):
        run.emit("checkpoint", name="plan-%d" % i, ms=0, bytes=0, sandbox=c["branch"])
    run.emit("sandbox_timing", kept=out["kept"], **out["timing"])
    return [(c["branch"], c["plan"]) for c in out["chosen"]]


def routes_locally(run, disk, ctx, candidates, pairs):
    """Phase 3 + 4 on local copy-on-write branches. Returns the judge's picks."""

    def route_work(i_pair):
        i, (museum, beach) = i_pair
        name = "route-%03d" % (i + 1)
        path = disk.branch("candidates", name)
        run.emit("route_start", branch=name, museum=museum["name"], beach=beach["name"])
        best, drops, ok = routes.route_branch(museum, beach, candidates, ctx)
        # On disk: the best plan in full, the runners-up as one-line summaries.
        result = {
            "best": best[0] if best else None,
            "runners_up": [{"stops": [s["name"] for s in it["stops"]], "group_score": it["group_score"]}
                           for it in best[1:]],
            "dropped": drops,
        }
        n = disk.write_json(path, "result.json", result, compact=True)
        disk.record_write(name, n)
        run.emit("route_done", branch=name, museum=museum["name"], beach=beach["name"],
                 best=best[0]["group_score"] if best else 0, feasible=ok, dropped=drops)
        return name, best

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(route_work, enumerate(pairs)))
    run.emit("storage", **disk.storage())

    # --- Phase 4: judge picks 3 different plans
    all_plans = [(name, it) for name, best in results for it in best]
    chosen = routes.pick_diverse(all_plans, n=3)
    for i, (name, it) in enumerate(chosen, 1):
        disk.checkpoint(os.path.join(disk.backend.br_dir, name), "plan-%d" % i)
    return chosen


def finish(run, req, disk, work, chosen):
    plans = [dict(it, branch=name, rank=i) for i, (name, it) in enumerate(chosen, 1)]
    if plans and run.state.get("ctx", {}).get("streets"):
        streets.add_paths(plans, routes.START)  # street-following lines for the map
    disk.write_json(work, "plans.json", plans)
    run.state.update(plans=plans, votes={})
    run.emit("plans", plans=plans)
    if plans:
        voters = ["Me"] + (["Ana"] if "ana" in req["with"] else [])
        run.state["voters"] = voters
        run.emit("vote_open", voters=voters, ranks=[p["rank"] for p in plans])

    if not plans:
        run.say("me", "No plan fits everyone's limits. Try fewer stops or a shorter day.", agent=True)
    else:
        # Each phone gets its own message, written by Claude when available (both in parallel).
        readers = [("me", "Me")] + ([("ana", "Ana")] if "ana" in req["with"] else [])
        with ThreadPoolExecutor(max_workers=2) as pool:
            msgs = list(pool.map(lambda r: present.messages(plans, r[1]), readers))
        for (phone, _), (texts, source) in zip(readers, msgs):
            for i, text in enumerate(texts):  # intro, one per plan, then the question
                part = "intro" if i == 0 else "question" if i == len(texts) - 1 else "plan"
                run.say(phone, text, agent=True, msg_kind="plans", part=part, written_by=source)
    run.emit("done", ok=True)


# ------------------------------------------------------------------ votes and changes

PHONE = {"Me": "me", "Ana": "ana"}


def vote(run, who, rank):
    """Record one person's preferred plan; when everyone has voted, settle it."""
    plans = run.state.get("plans") or []
    if who not in run.state.get("voters", []) or not 1 <= rank <= len(plans):
        raise ValueError("invalid vote")
    run.state["votes"][who] = rank
    run.emit("vote", who=who, rank=rank)
    for other in run.state["voters"]:
        if other != who:
            run.say(PHONE[other], "%s prefers plan %d." % (who, rank), agent=True)
    votes = run.state["votes"]
    if len(votes) < len(run.state["voters"]):
        return
    picked = sorted(set(votes.values()))
    if len(picked) == 1:
        rank, why = picked[0], "everyone picked it"
    else:
        # Different picks: choose the voted plan that keeps the least-happy person happiest.
        def worst(r):
            return min(v for v in plans[r - 1]["scores"].values() if isinstance(v, float))
        rank = max(picked, key=worst)
        why = "you picked different plans; plan %d leaves nobody worse off" % rank
    run.state["decided"] = rank
    p = plans[rank - 1]
    run.emit("decision", rank=rank, why=why)
    text = "Decided: plan %d (%s). %s." % (rank, why, " → ".join(s["name"] for s in p["stops"]))
    for v in run.state["voters"]:
        run.say(PHONE[v], text, agent=True)


CHANGES = {
    "swap-cafe": "a different cafe",
    "swap-museum": "a different museum or outdoor stop",
    "swap-food": "a different lunch place",
    "swap-beach": "a different beach",
    "less-walking": "less walking",
}


def request_change(prev, who, rank, change):
    """Start a new run that re-plans from the previous run's saved candidates."""
    if change not in CHANGES or not prev.state.get("plans") or not 1 <= rank <= len(prev.state["plans"]):
        raise ValueError("invalid change")
    run = Run(parent=prev.id)
    RUNS[run.id] = run
    threading.Thread(target=_safe_replan, args=(run, prev, who, rank, change), daemon=True).start()
    return run


def _safe_replan(run, prev, who, rank, change):
    try:
        replan(run, prev, who, rank, change)
    except Exception as e:
        import traceback
        traceback.print_exc()
        run.say("me", "Something went wrong while re-planning: %s" % e)
        run.emit("done", ok=False)


def replan(run, prev, who, rank, change):
    import copy
    req = copy.deepcopy(prev.state["req"])
    ctx = copy.deepcopy(prev.state["ctx"])
    cands = copy.deepcopy(prev.state["candidates"])
    plan_ = prev.state["plans"][rank - 1]
    note = ""
    if change.startswith("swap-"):
        cat = change[5:]
        old = next(s["name"] for s in plan_["stops"] if s["category"] == cat)
        cands[cat] = [v for v in cands[cat] if v["name"] != old]
        note = "without %s" % old
        if not cands[cat]:
            run.say(PHONE[who], "There is no other option for that stop.", agent=True)
            run.emit("done", ok=False)
            return
    elif change == "less-walking":
        ctx["limits"]["max_km"] = max(1.0, round(plan_["walk_km"] * 0.75, 1))
        note = "walking at most %.1f km" % ctx["limits"]["max_km"]
    for v in ["Me"] + (["Ana"] if "ana" in req["with"] else []):
        run.say(PHONE[v], "%s asked for %s on plan %d. Re-planning %s from the saved candidates..."
                % (who, CHANGES[change], rank, note), agent=True)
    run.state.update(req=req, ctx=ctx, candidates=cands)
    root = os.path.join(DATA, "outing-" + run.id)
    disk = OutingDisk(root, run.emit)
    run.emit("backend", name=disk.backend.name + (" · routes: one Archil sandbox each"
                                                  if os.environ.get("PLANNER_ROUTES") == "sandboxes" else ""),
             search="reused from the previous plan", replan_of=prev.id, change=change)
    disk.write_json(disk.work, "request.json", req)
    disk.write_json(disk.work, "candidates.json", cands)
    disk.checkpoint(disk.work, "candidates")
    route_and_finish(run, req, disk, ctx, cands)
