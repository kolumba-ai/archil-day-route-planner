"""Orchestrates one outing: combine shards -> search branches -> route branches -> judge."""
import json
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import crime, decide, fitness, places, present, routes, search, streets
from . import request as request_mod
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
    ("waterfront", ("riverside", "river", "waterfront", "esplanade", "pier", "lakefront")),
]


PROFILE_FIELDS = ("walking", "food", "activities", "weather", "safety")


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
        "safety": {"crime_tolerance": "low"},
    }


def parse_request(text):
    """City, start, times, walking limit, stops, who's coming. Read by Claude Haiku 5.5 when an
    Anthropic key is set, checked field by field, with the rules as fallback (planner/understand.py)."""
    from .understand import understand
    return understand(text)


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


def start(text, confirmed=False):
    run = Run()
    RUNS[run.id] = run
    threading.Thread(target=_safe_plan, args=(run, text, confirmed), daemon=True).start()
    return run


def _safe_plan(run, text, confirmed=True):
    try:
        plan(run, text, confirmed)
    except Exception as e:  # surface failures in the UI instead of hanging
        import traceback
        traceback.print_exc()
        run.say("me", "Something went wrong while planning: %s" % e)
        run.emit("done", ok=False)


DEMO_STARTS = ("noe valley", "noe valley, san francisco", "noe valley sf")
FAR_FROM_CITY_KM = 30


def resolve_start(req):
    """Find the starting point. Returns (start, conditions or None, problem or None), where
    problem is (reason, question to ask). Never guesses: a missing, unknown or far-away
    starting place means asking, not planning."""
    city = req["city"] or "San Francisco"
    place = (req["start_place"] or "").strip()
    if not place:
        if req["city"]:
            question = ("Where in %s should we start from? Tell me a place or street address. "
                        "I'll plan as soon as I know." % req["city"])
        else:  # no city named: ask for both, never assume one
            question = ("Where should we start from? Tell me the place or street address and the city "
                        "(for example \"St Stephens Catholic Church in Portland, Oregon\"). I'll plan as soon as I know.")
        return None, None, ("missing", question)
    if not req["city"] and place.lower().rstrip(".") in DEMO_STARTS:
        return routes.START, None, None  # the built-in San Francisco demo
    try:
        start = places.find_start(place, city, city_center_fallback=False)
    except Exception as e:
        return None, None, ("lookup_failed", "I couldn't look up %s right now (%s). Please try again in a minute."
                            % (place, str(e)[:60]))
    if not start:
        return None, None, ("not_found", "I couldn't find \"%s\" in %s on the map. Which place do you mean? "
                                         "A street address or a nearby landmark works." % (place, city))
    try:
        center = places.geocode(city)
    except Exception:  # can't check the distance to the city center: trust the lookup above
        center = None
    if center and routes.km(start, center) > FAR_FROM_CITY_KM:
        return None, None, ("far", "I found \"%s\" at %s, about %d km from %s. Is that the right place? Please "
                                   "give a more specific place or address." % (
                                       place, start.get("display", start["name"])[:90], routes.km(start, center), city))
    return start, None, None


ICONS = {"cafe": "☕", "museum": "🏛️", "food": "🌯", "beach": "🏖️", "park": "🌳", "waterfront": "🌊", "stroll": "🛍️"}


def place_name(p):
    """ "Start (Noe Valley)" -> "Noe Valley" (the demo's start); other places as they are."""
    return re.sub(r"^Start \((.*)\)$", r"\1", p.get("name", ""))


def stop_label(c, req):
    if c == "stroll" and req.get("stroll_street"):
        return "a stroll along %s" % req["stroll_street"]
    label = request_mod.LABELS.get(c, c)
    if (req.get("named_stops") or {}).get(c):
        return req["named_stops"][c]
    near = (req.get("stop_near") or {}).get(c)
    return "%s near %s" % (label, near) if near else label


def understood(req, start, city):
    """What the agent understood, for the person to confirm before anyone else is contacted."""
    stops = " → ".join("%s %s%s" % (ICONS.get(c, "📍"), stop_label(c, req),
                                     " at sunset 🌅" if c == req.get("sunset_stop") else "")
                       for c in req["categories"])
    food = []
    if req.get("cuisines"):
        food.append(" or ".join(c.capitalize() for c in req["cuisines"]) + " food")
    if req.get("food_truck"):
        food.append("from a food truck")
    lines = ["Here's what I understood:",
             "📅 %s, %s" % (req["day"], city),
             "📍 Start: %s%s at %s" % (place_name(start), ", " + start["address"] if start.get("address") else "",
                                      routes.hhmm(req["start_min"])),
             "🗺️ " + stops]
    if food:
        lines.append("🍽️ " + " ".join(food))
    limits = []
    if req.get("end_min"):
        limits.append(("back at the start by %s" if req.get("return_to_start") else "done by %s")
                      % routes.hhmm(req["end_min"]))
    limits.append("walking up to %g km" % req["max_walk_km"] if req["max_walk_km"] else "your usual walking limit")
    if req.get("avoid_hills"):
        limits.append("avoiding hills")
    if req.get("prefer_hills"):
        limits.append("with some hills")
    lines.append("🚶 " + ", ".join(limits))
    who = (["Ana"] if "ana" in req["with"] else []) + (["Biscuit 🐕"] if req["dog"] else [])
    lines.append("👥 With " + " and ".join(who) if who else "👤 Just you")
    if req.get("not_supported"):  # never drop part of the request silently
        lines.append("⚠️ I can't plan %s yet, so it's left out." % " or ".join('"%s"' % x for x in req["not_supported"]))
    lines.append("Reply yes to %s, or tell me what to change." % (
        "send the invitation to Ana" if "ana" in req["with"] else "start planning"))
    return "\n".join(lines)


def weather_brief(conditions, req, city):
    """One short line from the forecast for the hours of the outing."""
    hourly = (conditions or {}).get("hourly") or {}
    first = req["start_min"] // 60
    last = (req["end_min"] // 60) if req.get("end_min") else max(first + 6, int(conditions["sunset"][:2]))
    # Real cities have one forecast ("all"); the San Francisco demo has one per area (west, north...).
    pick = lambda hs: [w for h in hs for w in (hourly[h].values())]
    hours = pick([h for h in sorted(hourly, key=int) if first <= int(h) <= last]) or pick(list(hourly))
    if not hours:
        return None
    lo, hi = min(h["temp_c"] for h in hours), max(h["temp_c"] for h in hours)
    wind = max(h["wind_kmh"] for h in hours)
    fog = any(h.get("fog") for h in hours)
    sky = "🌫️" if fog else "💨" if wind >= 30 else "🌤️"
    temps = "%d°C" % round(hi) if round(lo) == round(hi) else "%d–%d°C" % (round(lo), round(hi))
    return "%s Weather for %s in %s: %s, wind up to %d km/h%s. 🌅 Sunset %s." % (
        sky, req["day"], city, temps, round(wind), ", fog likely" if fog else ", no fog", conditions["sunset"])


def plan(run, text, confirmed=True):
    from .understand import NoLLM, LLMFailed
    try:
        req = parse_request(text)
    except NoLLM:
        run.emit("no_llm")
        run.say("me", "⚠️ No LLM is connected, so I can't read your request and won't guess. Add "
                      "ANTHROPIC_API_KEY (or OPENAI_API_KEY) to .env and restart the server.", agent=True)
        run.emit("done", ok=False, reason="no_llm")
        return
    except LLMFailed as e:
        run.say("me", "⚠️ I couldn't read your request: the LLM didn't answer (%s). Nothing was sent to "
                      "anyone. Please try again." % str(e)[:90], agent=True)
        run.emit("done", ok=False, reason="llm_failed")
        return
    root = os.path.join(DATA, "outing-" + run.id)
    shutil.rmtree(root, ignore_errors=True)
    disk = OutingDisk(root, run.emit)
    run.emit("backend", name=disk.backend.name + (" · routes: one Archil sandbox each"
                                                  if os.environ.get("PLANNER_ROUTES") == "sandboxes" else ""),
             search=search.provider_label())
    run.emit("request", **{k: v for k, v in req.items() if k != "text"})

    # Where, before anything else: no invitation, search or branch until the starting point
    # is clear. The San Francisco demo starts at Noe Valley on built-in data; anywhere else the
    # start is found on OpenStreetMap, with weather and sunset from Open-Meteo.
    city = req["city"] or "San Francisco"
    start, conditions, problem = resolve_start(req)
    if problem:
        run.state.update(needs_start=True, original=text)
        run.emit("need_start", reason=problem[0], place=req["start_place"], city=city)
        run.say("me", problem[1], agent=True)
        run.emit("done", ok=False, reason="need_start")
        return
    if conditions is None and start is not routes.START:
        try:
            conditions = places.conditions(start, req["day"])
        except Exception as e:
            run.say("me", "No weather forecast available (%s); assuming mild weather." % str(e)[:60], agent=True)
            conditions = {"sunset": "17:00", "hourly": {"12": {"all": {"temp_c": 16, "wind_kmh": 10, "fog": False}}}}
    # The map moves to the start now, before any route is planned.
    run.emit("place", city=city, start=start["name"], lat=start["lat"], lon=start["lon"],
             sunset=(conditions or routes.load_conditions())["sunset"], date=(conditions or {}).get("date"))
    if not confirmed:
        # Nothing goes to Ana (or anyone) until the person confirms what the agent understood.
        run.state.update(needs_confirm=True, original=text)
        run.emit("confirm", request={k: v for k, v in req.items() if k != "text"})
        run.say("me", understood(req, start, city), agent=True)
        run.emit("done", ok=False, reason="confirm")
        return
    brief = weather_brief(conditions or routes.load_conditions(), req, city)
    run.say("me", "👍 On it%s.%s" % (", inviting Ana" if req["with"] else "", ("\n" + brief) if brief else ""),
            agent=True)

    # --- Phase 1: gather shards (Ana's comes from her agent, with consent)
    shards = [load_profile("me")]
    if req["dog"]:
        shards.append(load_profile("biscuit"))
    if "ana" in req["with"]:
        run.emit("share_request", frm="Me", to="Ana",
                 asks=["walking limits", "hills", "diet", "museum tolerance", "weather tolerance", "street crime tolerance"])
        run.say("ana", "Your friend is inviting you to several activities on Saturday, "
                       "taking into account your limits. Accept?", agent=True, ask=True)
        run.say("me", "📨 Invitation sent to Ana. Waiting for her answer…", agent=True)
        run.state["awaiting_share"] = True
        run.share_event.wait(timeout=SHARE_TIMEOUT)
        run.state["awaiting_share"] = False
        run.emit("share_answered", answer=run.share_answer)
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
                run.say("me", "✅ Ana accepted and shared her limits: %s. Planning now…" % limits_brief(ana),
                        agent=True)
            summary = {k: ana.get(k, {"crime_tolerance": "low"} if k == "safety" else None)
                       for k in ("id", "name", "kind", "walking", "food", "activities", "weather", "safety")}
            shards.append(summary)
            run.emit("shard", owner="Ana", fields=sorted(summary.keys()))

    ctx = routes.combine(shards, req, start=start, conditions=conditions)
    if start is not routes.START and not req["city"]:
        ctx["city"] = "San Francisco"  # a custom start in the demo city: use real places too
    run.state.update(req=req, ctx=ctx)
    run.emit("combined", limits=ctx["limits"])
    work = disk.work
    disk.write_json(work, "request.json", req)
    for s in shards:
        disk.write_json(work, "shards/%s.json" % s["id"], s)
    disk.write_json(work, "conditions.json", ctx["conditions"])
    disk.checkpoint(work, "combined")

    # Street distances don't depend on the search when the candidates are the seed venues
    # (seed or Keenable mode, which only checks them): fetch the table in parallel.
    prefetch = None
    if (streets.enabled() and not ctx.get("city") and not search.live_search_available()
            and os.environ.get("PLANNER_SEARCH") in ("seed", "keenable", None, "")):
        seed_points = [routes.START] + [v for cat in req["categories"] for v in search.load_seed(cat)]
        prefetch = ThreadPoolExecutor(max_workers=1).submit(streets.build_matrix, seed_points)
        run.state["streets_started"] = time.time()

    # --- Phase 2: one search branch per stop type
    def search_branch(cat):
        name = "search-" + cat
        path = disk.branch("combined", name)
        if RECORDED and not ctx.get("city"):  # recorded results are San Francisco demo data
            with open(RECORDED) as f:
                venues, source = json.load(f)[cat], "recorded search results"
        else:
            try:
                venues, source = search.search_category(cat, ctx)
            except Exception as e:  # a map service down shouldn't crash the plan
                venues, source = [], "search failed: %s" % str(e)[:80]
        n = disk.write_json(path, "candidates/%s.json" % cat, venues)
        disk.record_write(name, n)
        run.emit("search_done", branch=name, category=cat, count=len(venues), source=source,
                 venues=[v["name"] for v in venues])
        return cat, venues

    with ThreadPoolExecutor(max_workers=4) as pool:
        candidates = dict(pool.map(search_branch, req["categories"]))
    missing = [c for c in req["categories"] if not candidates.get(c)]
    if missing:
        failed = sorted({e["source"] for e in run.events if e["type"] == "search_done"
                         and e["source"].startswith("search failed")})
        if failed:
            run.say("me", "The map service didn't answer (%s). Please try again in a minute." % failed[0][15:],
                    agent=True)
        else:
            what = " or ".join(missing)
            if "food" in missing and req.get("cuisines"):
                what = what.replace("food", "%s food%s" % (" or ".join(c.capitalize() for c in req["cuisines"]),
                                                           " (truck or restaurant)" if req.get("food_truck") else ""))
            run.say("me", "I couldn't find any %s within reach of %s. Try a longer walk or other stops."
                    % (what, ctx["start"]["name"]), agent=True)
        run.emit("done", ok=False)
        return
    if ctx.get("city"):  # hills from real terrain: elevation of the start and every candidate
        pts = [ctx["start"]] + [v for vs in candidates.values() for v in vs]
        try:
            ctx["elev"] = {routes.key(p): e for p, e in zip(pts, places.elevations(pts))}
            run.emit("elevation", places=len(ctx["elev"]), source="Open-Meteo")
        except Exception as e:
            run.emit("elevation", ok=False, error=str(e)[:100])
    disk.write_json(work, "candidates.json", candidates)
    disk.checkpoint(work, "candidates")
    run.state["candidates"] = candidates
    run.say("me", "Found %d places. Simulating routes in parallel branches..." %
            sum(len(v) for v in candidates.values()), agent=True)
    route_and_finish(run, req, disk, ctx, candidates, prefetch)


def route_and_finish(run, req, disk, ctx, candidates, prefetch=None):
    """Phases 3 and 4: simulate routes (sandboxes or local branches), judge, report."""
    work = disk.work
    # Real walking distances and driving times between every pair of places, fetched once
    # so the route branches (and sandboxes) never call the router themselves.
    ctx.pop("streets", None)
    if streets.enabled():
        t = run.state.get("streets_started") if prefetch else time.time()
        points = [ctx["start"]] + [v for vs in candidates.values() for v in vs]
        try:
            matrix = None
            if prefetch:  # fetched while searching; use it if it covers every final place
                try:
                    matrix = prefetch.result(timeout=120)
                    if not all(streets.key(p) in matrix["walk_km"] for p in points):
                        matrix = None
                except Exception:
                    matrix = None
            ctx["streets"] = matrix or streets.build_matrix(points)
            disk.write_json(work, "streets.json", ctx["streets"], compact=True)
            run.emit("streets", ok=True, places=len(ctx["streets"]["walk_km"]), ms=round((time.time() - t) * 1000),
                     source=ctx["streets"]["source"], in_parallel_with_search=bool(matrix))
        except Exception as e:
            run.emit("streets", ok=False, error=str(e)[:120])
            run.say("me", "Street routing is unavailable (%s); estimating distances in straight lines." % str(e)[:80],
                    agent=True)
    # Bus routes: legs too long to walk become a bus ride when one route serves both ends.
    ctx.pop("bus", None)
    if streets.enabled() and os.environ.get("PLANNER_TRANSIT", "on") != "off":
        from . import transit
        t = time.time()
        points = [ctx["start"]] + [v for vs in candidates.values() for v in vs]
        reach = max(routes.km(ctx["start"], p) for p in points) * 1000 + transit.STOP_RADIUS_M
        try:
            ctx["bus"] = transit.bus_matrix(points, ctx["start"], min(reach, 12000), ctx["limits"]["pace_kmh"])
            disk.write_json(work, "bus.json", ctx["bus"], compact=True)
            run.emit("bus", ok=True, routes=ctx["bus"]["routes"], pairs=sum(len(v) for v in ctx["bus"]["pairs"].values()),
                     ms=round((time.time() - t) * 1000))
        except Exception as e:
            run.emit("bus", ok=False, error=str(e)[:120])
    # Street crime along every possible walk, from the city's police data file (planner/crime.py).
    ctx.pop("crime", None)
    idx = crime.load(ctx.get("city"))
    points = [ctx["start"]] + [v for vs in candidates.values() for v in vs]
    if idx:
        ctx["crime"] = crime.pair_cells(idx, points)
        disk.write_json(work, "crime.json", ctx["crime"], compact=True)
        run.emit("crime", ok=True, source=idx.get("source"), covers=idx.get("covers"),
                 tolerance=ctx["limits"]["crime_tolerance"], cells=len(ctx["crime"]["cells"]))
    else:
        run.emit("crime", ok=False, error="no crime data for %s" % (ctx.get("city") or "San Francisco"))
    specs = routes.branch_specs(candidates, ctx["categories"])  # one branch per anchor combination
    chosen = None
    if os.environ.get("PLANNER_ROUTES") == "sandboxes":
        try:
            chosen = routes_on_sandboxes(run, ctx, candidates, specs)
        except Exception as e:
            run.say("me", "Archil sandboxes unavailable (%s); simulating routes here instead." % str(e)[:120], agent=True)
    if chosen is None:
        chosen = routes_locally(run, disk, ctx, candidates, specs)
    finish(run, req, disk, work, chosen)


def routes_on_sandboxes(run, ctx, candidates, specs):
    """Phase 3 + 4 with one Archil sandbox per route branch. Returns the judge's picks."""
    from . import sandbox_routes
    run.say("me", "Forking %d Archil sandboxes, one per route..." % len(specs), agent=True)
    run.emit("sandbox_phase", count=len(specs))
    out = sandbox_routes.run_route_phase(run.id, ctx, candidates, specs)
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
            run.emit("route_done", branch=r["branch"], label="?", best=0, feasible=0,
                     dropped=["sandbox error: " + r["error"][:80]])
            continue
        best = r["best"]
        written += len(json.dumps({"best": best[0] if best else None, "dropped": r["dropped"]},
                                  separators=(",", ":")))
        run.emit("route_done", branch=r["branch"], label=r["label"],
                 best=best[0]["group_score"] if best else 0, feasible=r["feasible"], dropped=r["dropped"],
                 run_s=round(r["run_s"], 2))
    base = len(json.dumps({"ctx": ctx, "candidates": candidates}))
    n = len(out["results"])
    run.emit("storage", branches=n, base_bytes=base, delta_bytes=written, full_copy_bytes=base * n)
    run.emit("sandbox_timing", kept=out["kept"], **out["timing"])
    # The decision step chooses from every sandbox's plans. The controller kept the sandboxes of
    # its own rule-based picks; if a decision model picks differently, say which were kept.
    pool = [(r["branch"], it) for r in out["results"] if "best" in r for it in r["best"]]
    chosen = decide_and_record(run, pool, ctx)
    for i, (name, _) in enumerate(chosen, 1):
        run.emit("checkpoint", name="plan-%d" % i, ms=0, bytes=0, sandbox=name, kept=name in out["kept"])
    return chosen


def decide_and_record(run, pool, ctx):
    """Phase 4: the decision step picks up to 3 plans and says why (planner/decide.py)."""
    chosen, reasons, decided_by, note = decide.decide(pool, ctx, n=3)
    run.state["reasons"] = reasons
    run.emit("decided", by=decided_by, reasons=reasons, note=note, candidates=len(pool))
    return chosen


def routes_locally(run, disk, ctx, candidates, specs):
    """Phase 3 + 4 on local copy-on-write branches. Returns the judge's picks."""

    def route_work(i_spec):
        i, spec = i_spec
        fixed = {c: candidates[c][j] for c, j in spec.items()}
        label = routes.branch_label(fixed)
        name = "route-%03d" % (i + 1)
        path = disk.branch("candidates", name)
        run.emit("route_start", branch=name, label=label)
        best, drops, ok = routes.route_branch(fixed, candidates, ctx)
        # On disk: the best plan in full, the runners-up as one-line summaries.
        result = {
            "best": best[0] if best else None,
            "runners_up": [{"stops": [s["name"] for s in it["stops"]], "group_score": it["group_score"]}
                           for it in best[1:]],
            "dropped": drops,
        }
        n = disk.write_json(path, "result.json", result, compact=True)
        disk.record_write(name, n)
        run.emit("route_done", branch=name, label=label,
                 best=best[0]["group_score"] if best else 0, feasible=ok, dropped=drops)
        return name, best

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(route_work, enumerate(specs)))
    run.emit("storage", **disk.storage())

    # --- Phase 4: judge picks 3 different plans
    all_plans = [(name, it) for name, best in results for it in best]
    chosen = decide_and_record(run, all_plans, ctx)
    for i, (name, it) in enumerate(chosen, 1):
        disk.checkpoint(os.path.join(disk.backend.br_dir, name), "plan-%d" % i)
    return chosen


NAME_SYSTEM = """You name day-out routes, like a friend suggesting them by text. For each plan (JSON, in order), write a short, natural, distinct name of 2 to 5 words that captures the outing's character, e.g. "Hawthorne Art & Riverside Sunset", "Vegan Lunch, Esplanade Sunset", "Gallery Morning, Taco Afternoon".
Rules:
- Describe the outing (its kinds of stops and highlights); don't just list stop names. Use at most ONE stop name, and only if it is short and recognizable.
- Make each name differ by what differs between the plans (look at which stops change); a shared final stop doesn't need repeating in every name.
- Use only facts given. No emoji, no quotes, no plan numbers."""
NAME_SCHEMA = {"type": "object", "properties": {"names": {"type": "array", "items": {"type": "string"}}},
               "required": ["names"], "additionalProperties": False}
SHORT = {"cafe": "Coffee", "museum": "Art", "food": "Lunch", "beach": "Beach", "park": "Park", "waterfront": "Riverside",
         "stroll": "Stroll"}


def name_routes(plans, req):
    """A name for each route: written by the LLM, checked; else from what makes the route different."""
    from . import understand
    names = None
    who = understand.provider()
    if who != "rules" and not understand.rules_mode() and plans:
        facts = [{"stops": [{"name": s["name"], "kind": s["category"]} for s in p["stops"]],
                  "sunset_stop": req.get("sunset_stop"), "city": p.get("city")} for p in plans]
        try:
            got = understand.ask_model(who, NAME_SYSTEM, json.dumps(facts, ensure_ascii=False), NAME_SCHEMA,
                                       "route_names", timeout=15).get("names") or []
            got = [re.sub(r"[\"“”]", "", n).strip() for n in got]
            if (len(got) == len(plans) and len(set(n.lower() for n in got)) == len(got)
                    and all(0 < len(n) <= 48 for n in got)):
                names = got
        except Exception as e:
            print("Route naming failed, using built-in names: %r" % e)
    if not names:
        names = []
        for p in plans:
            others = {s["name"] for q in plans if q is not p for s in q["stops"]}
            own = [s for s in p["stops"] if s["name"] not in others] or p["stops"]
            lead = max(own, key=lambda s: ANCHOR_RANK.get(s["category"], 0))
            tail = SHORT.get(req.get("sunset_stop") or "", "")
            name = "%s %s" % (re.sub(r"\s*\(.*?\)", "", lead["name"]), "& %s Sunset" % tail if tail else "Route")
            names.append(name if name not in names else "%s %d" % (name, p["rank"]))
    for p, n in zip(plans, names):
        p["name"] = n


ANCHOR_RANK = {"museum": 5, "waterfront": 4, "beach": 4, "park": 3, "food": 2, "cafe": 1}


def save_outing(run, req, plans):
    """Keep the routes with the request and the limits they were planned under:
    data/saved/<date>-<run>.json (and the outing disk has plans.json, request.json too)."""
    ctx = run.state.get("ctx") or {}
    out = {"saved": time.strftime("%Y-%m-%d %H:%M"), "run": run.id, "request": req.get("text"),
           "understood": {k: v for k, v in req.items() if k != "text"},
           "limits": limits_view(ctx, req),
           "routes": [{"rank": p["rank"], "name": p.get("name"), "depart": p["depart"], "end": p["end"],
                       "start": p.get("start"), "stops": p["stops"],
                       "legs": [{k: v for k, v in l.items() if k != "path"} for l in p["legs"]],
                       "return_leg": {k: v for k, v in p["return_leg"].items() if k != "path"} if p.get("return_leg") else None,
                       "walk_km": p["walk_km"], "climb_m": p["climb_m"], "travel_min": p["travel_min"],
                       "ride_cost_usd": p.get("ride_cost", 0),
                       "notes": p["notes"], "why": p.get("why")} for p in plans]}
    folder = os.path.join(DATA, "saved")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "%s-%s.json" % (time.strftime("%Y%m%d-%H%M"), run.id))
    with open(path, "w") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    return path


def limits_view(ctx, req):
    """The constraints the routes were planned under, as label/value rows for people to read."""
    lim = ctx.get("limits") or {}
    rows = [("Day", "%s in %s" % (req["day"], req.get("city") or "San Francisco")),
            ("Start", "%s at %s" % (place_name(ctx.get("start") or {"name": "?"}),
                                    routes.hhmm(routes.round_departure(None, req["start_min"]))))]
    if req.get("end_min"):
        rows.append(("Finish", ("back at the start by %s" if req.get("return_to_start") else "by %s")
                     % routes.hhmm(req["end_min"])))
    elif req.get("return_to_start"):
        rows.append(("Finish", "back at the start"))
    rows.append(("Stops", " → ".join(stop_label(c, req) for c in req["categories"])))
    if req.get("sunset_stop"):
        rows.append(("At sunset", "%s (sunset %s)" % (request_mod.LABELS.get(req["sunset_stop"], req["sunset_stop"]),
                                                     (ctx.get("conditions") or {}).get("sunset", "?"))))
    if req.get("cuisines") or req.get("food_truck"):
        rows.append(("Food", " ".join(filter(None, [" or ".join(c.capitalize() for c in req.get("cuisines") or []),
                                                     "from a food truck" if req.get("food_truck") else ""]))))
    if lim:
        rows.append(("Walking", "up to %g km in total, at %g km/h" % (lim["max_km"], lim["pace_kmh"])))
        rows.append(("Hills", "wanted" if lim.get("prefer_hills") else
                     "avoid (climbs over %d m)" % lim.get("hills_max_m", 0) if lim.get("avoid_hills") else "fine"))
        rows.append(("Diet", ", ".join(d.replace("_", "-") for d in lim.get("diets") or []) or "no restrictions"))
        if lim.get("avoid_foods"):
            rows.append(("Avoids", ", ".join(lim["avoid_foods"])))
        rows.append(("Museums", "at most %d min" % lim["museum_max_min"]))
        rows.append(("Dog", "yes (Biscuit): dog-friendly stops only" if lim.get("dog") else "no"))
        tol = lim.get("crime_tolerance") or "low"
        rows.append(("Street crime", ("%s tolerance: bus or taxi instead of walking past a block with %d+ robberies, "
                                      "serious assaults or sex offenses around that hour in the past year "
                                      "(such rides lower a plan's score)" % (tol, crime.SERIOUS_LIMIT[tol]))
                     if tol in crime.SERIOUS_LIMIT else "high tolerance: walk anyway; risky blocks are noted"))
    people = [p["name"] for p in ctx.get("people") or []]
    if people:
        rows.append(("Group", ", ".join(people) + " (the most cautious limits apply)"))
    return [{"label": a, "value": b} for a, b in rows]


def finish(run, req, disk, work, chosen):
    reasons = run.state.get("reasons") or []
    plans = [dict(it, branch=name, rank=i, day=req["day"], city=req.get("city") or "San Francisco",
                  steps_typical=fitness.typical_steps(it),  # steps at a typical stride, for shared screens
                  why=reasons[i - 1] if i <= len(reasons) else "")
             for i, (name, it) in enumerate(chosen, 1)]
    if plans and run.state.get("ctx", {}).get("streets"):
        streets.add_paths(plans, run.state["ctx"]["start"])  # street-following lines for the map
        straight = sum(1 for p in plans for l in p["legs"] + ([p["return_leg"]] if p.get("return_leg") else [])
                       if not l.get("path"))
        if straight:
            run.say("me", "The street routing services are busy right now, so the map shows %d of the legs as "
                          "straight lines. Walking times still use street distances." % straight, agent=True)
    elif plans and streets.enabled():
        run.say("me", "Street routing is unavailable right now: the map shows straight lines and walking times "
                      "are estimates.", agent=True)
    name_routes(plans, req)
    limits = limits_view(run.state.get("ctx") or {}, req)
    disk.write_json(work, "plans.json", plans)
    run.state.update(plans=plans, votes={})
    run.emit("plans", plans=plans, limits=limits)
    if plans:
        try:
            run.emit("saved", path=os.path.relpath(save_outing(run, req, plans), os.path.dirname(HERE)))
        except OSError as e:
            run.emit("saved", error=str(e)[:100])
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
            # Each person's own agent adds their own steps and calories, from their own profile:
            # body data never leaves its owner (it isn't part of what Ana shares).
            msgs = list(pool.map(lambda r: present.messages(plans, r[1], load_profile(r[0]).get("body")), readers))
        for (phone, _), (texts, source) in zip(readers, msgs):
            for i, text in enumerate(texts):  # intro, one per plan, then the question
                part = "intro" if i == 0 else "question" if i == len(texts) - 1 else "plan"
                run.say(phone, text, agent=True, msg_kind="plans", part=part, written_by=source)
    run.emit("done", ok=True)


# ------------------------------------------------------------------ votes (plans can't be changed)

PHONE = {"Me": "me", "Ana": "ana"}


def limits_brief(p):
    """Ana's shared limits in one line for the caller's phone."""
    w, f, a = p.get("walking") or {}, p.get("food") or {}, p.get("activities") or {}
    out = []
    if w.get("max_km"):
        out.append("walks up to %g km" % w["max_km"] + (" at %g km/h" % w["pace_kmh"] if w.get("pace_kmh") else ""))
    if w.get("hills"):
        out.append("hills %s" % w["hills"])
    if w.get("rest_every_min"):
        out.append("rest every %d min" % w["rest_every_min"])
    if f.get("diet") and f["diet"] != "omnivore":
        out.append(f["diet"].replace("_", "-"))
    if f.get("loves"):
        out.append("loves " + ", ".join(f["loves"]))
    if a.get("dislikes"):
        out.append("dislikes " + ", ".join(a["dislikes"]))
    crime = (p.get("safety") or {}).get("crime_tolerance")
    if crime:
        out.append("%s street-crime tolerance" % crime)
    return "; ".join(out) or "her usual limits"


def answer_share(run, allow):
    """Ana's answer to the invitation, from the buttons or typed on her phone."""
    if not run or not run.state.get("awaiting_share"):
        raise ValueError("no invitation is waiting for an answer")
    run.share_answer = bool(allow)
    run.share_event.set()


def vote(run, who, rank):
    """Record one person's preferred plan; when everyone has voted, settle it."""
    plans = run.state.get("plans") or []
    if who not in run.state.get("voters", []) or not 1 <= rank <= len(plans):
        raise ValueError("invalid vote")
    run.state["votes"][who] = rank
    run.emit("vote", who=who, rank=rank)
    for other in run.state["voters"]:
        if other != who:  # "Me" is the person who invited Ana: on Ana's phone that's "Your friend"
            run.say(PHONE[other], "👍 %s prefers plan %s." % ("Your friend" if who == "Me" else who,
                                                              present.number(rank)), agent=True)
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
    for v in run.state["voters"]:
        run.say(PHONE[v], present.decision(p, rank, why, load_profile(PHONE[v]).get("body")), agent=True,
                msg_kind="decision")
