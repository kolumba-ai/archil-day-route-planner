"""Combine context shards, simulate a day's route, and score it for the group."""
import itertools
import json
import math
import os

HERE = os.path.dirname(__file__)

START = {"name": "Start (Noe Valley)", "lat": 37.7502, "lon": -122.4337}
START_TIME = 10 * 60  # 10:00

# Rough hill zones: center, radius (km), climb (m) when a walk crosses them.
HILLS = [
    ("Twin Peaks", 37.7544, -122.4477, 1.2, 150),
    ("Nob Hill", 37.7930, -122.4161, 0.5, 70),
    ("Russian Hill", 37.8011, -122.4194, 0.4, 60),
    ("Pacific Heights", 37.7925, -122.4382, 0.8, 70),
    ("Bernal Heights", 37.7430, -122.4150, 0.5, 60),
    ("Lone Mountain", 37.7800, -122.4520, 0.6, 40),
    ("Lands End bluffs", 37.7850, -122.4990, 0.5, 40),
]

DWELL = {"cafe": 30, "food": 45, "outdoor_alternative": 50, "beach": 60}


def load_conditions():
    with open(os.path.join(HERE, "conditions.json")) as f:
        return json.load(f)


# ---------------------------------------------------------------- combine

def combine(profiles, request):
    """Merge person and pet shards into hard limits for the group."""
    humans = [p for p in profiles if p["kind"] == "person"]
    dogs = [p for p in profiles if p["kind"] == "dog"]
    limits = {
        "max_km": min([p["walking"]["max_km"] for p in profiles]),
        "pace_kmh": min(p["walking"]["pace_kmh"] for p in humans),
        "avoid_hills": any(p["walking"].get("hills") == "avoid" for p in humans),
        "diets": sorted({p["food"]["diet"] for p in humans} - {"omnivore"}),
        "avoid_foods": sorted({f for p in humans for f in p["food"]["avoids"]}),
        "museum_max_min": min(p["activities"]["museum_max_min"] for p in humans),
        "dog": bool(dogs),
        "dog_owner": dogs[0]["owner"] if dogs else None,
    }
    return {"request": request, "people": humans, "pets": dogs, "limits": limits,
            "conditions": load_conditions()}


# ---------------------------------------------------------------- geometry

def km(a, b):
    r = 6371.0
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dp = p2 - p1
    dl = math.radians(b["lon"] - a["lon"])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def climb_m(a, b):
    total, crossed = 0, []
    for name, lat, lon, rad, climb in HILLS:
        for i in range(21):
            t = i / 20
            p = {"lat": a["lat"] + (b["lat"] - a["lat"]) * t, "lon": a["lon"] + (b["lon"] - a["lon"]) * t}
            if km(p, {"lat": lat, "lon": lon}) < rad:
                total += climb
                crossed.append(name)
                break
    return total, crossed


RIDE_OVERHEAD_MIN = 8  # waiting for Muni or a car, and walking to and from it


def leg(a, b, pace_kmh, streets=None):
    """Walk if reasonable, otherwise ride (Muni or rideshare).

    With a street matrix (streets.build_matrix) distances follow real streets and paths,
    and rides use real driving times; without one, straight-line distance x 1.3.
    """
    ka, kb = "%.3f,%.3f" % (a["lat"], a["lon"]), "%.3f,%.3f" % (b["lat"], b["lon"])  # same keys as streets.key
    on_streets = bool(streets) and kb in streets["walk_km"].get(ka, {})
    d = streets["walk_km"][ka][kb] if on_streets else km(a, b) * 1.3
    up, hills = climb_m(a, b)
    walk_min = d / pace_kmh * 60 + up / 10  # +1 min per 10 m climbed
    if walk_min <= 35:
        return {"mode": "walk", "km": round(d, 2), "min": round(walk_min), "climb_m": up, "hills": hills,
                "streets": on_streets}
    ride_min = (streets["car_min"][ka][kb] + RIDE_OVERHEAD_MIN) if on_streets else 12 + d * 3
    return {"mode": "ride", "km": round(d, 2), "min": round(ride_min), "climb_m": 0, "hills": [],
            "walk_km": 0.3, "streets": on_streets}


def hhmm(m):
    return "%02d:%02d" % (int(m) // 60, int(m) % 60)


def to_min(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


# ---------------------------------------------------------------- simulate

def simulate(stops, ctx):
    """stops: list of (category, venue). Returns an itinerary with scores."""
    lim = ctx["limits"]
    cond = ctx["conditions"]
    sunset = to_min(cond["sunset"])
    t = START_TIME
    idle = 0
    here = START
    legs, timeline, violations, notes = [], [], [], []
    walk_km = climb = 0
    for cat, v in stops:
        lg = leg(here, v, lim["pace_kmh"], ctx.get("streets"))
        legs.append(lg)
        t += lg["min"]
        walk_km += lg["km"] if lg["mode"] == "walk" else lg.get("walk_km", 0)
        climb += lg["climb_m"]
        arrive = t
        if cat == "beach":  # aim to be there for sunset
            if arrive < sunset - 60:
                idle = sunset - 60 - arrive
                t = arrive = sunset - 60
                if idle >= 45:
                    notes.append("About %d min of free time before %s" % (idle, v["name"]))
        if cat == "museum":
            dwell = min(lim["museum_max_min"], 90) if v.get("kind") == "museum" else DWELL["outdoor_alternative"]
        else:
            dwell = DWELL[cat]
        leave = arrive + dwell
        if arrive < to_min(v["open"]) or leave > to_min(v["close"]):
            violations.append("%s is closed then (%s-%s)" % (v["name"], v["open"], v["close"]))
        if lim["dog"]:
            if cat in ("cafe", "food") and v["dogs"] == "no":
                violations.append("%s: no dogs, not even on a patio" % v["name"])
            if cat == "beach" and v["dogs"] == "no":
                violations.append("%s: dogs not allowed" % v["name"])
            if cat == "museum" and v.get("kind") == "museum":
                notes.append("Dogs can't go into %s; one person walks Biscuit outside" % v["name"])
        if "vegetarian" in lim["diets"] and cat in ("cafe", "food") and not v.get("vegetarian_ok", True):
            violations.append("%s: nothing vegetarian" % v["name"])
        if v.get("verify_error"):
            notes.append("Couldn't check %s online; confirm its hours and dog rules" % v["name"])
        timeline.append({"category": cat, "name": v["name"], "lat": v["lat"], "lon": v["lon"],
                         "arrive": hhmm(arrive), "leave": hhmm(leave), "source": v.get("source", "")})
        t = leave
        here = v
    walk_km = round(walk_km, 1)
    if walk_km > lim["max_km"]:
        violations.append("walks %.1f km; group limit is %d km" % (walk_km, lim["max_km"]))
    if lim["avoid_hills"] and climb > 200:
        violations.append("climbs %d m; too hilly for the group" % climb)

    beach = stops[-1][1]
    hour = str(min(max(int(to_min(timeline[-1]["arrive"]) // 60), 10), 18))
    wx = cond["hourly"][hour][beach.get("area", "north")]
    if wx["fog"]:
        notes.append("Fog expected at %s around %s" % (beach["name"], timeline[-1]["arrive"]))

    it = {"depart": hhmm(START_TIME), "start": START["name"], "stops": timeline, "legs": legs,
          "walk_km": walk_km, "climb_m": climb,
          "travel_min": sum(lg["min"] for lg in legs), "idle_min": idle,
          "end": hhmm(t), "violations": violations, "notes": notes, "weather_at_beach": wx}
    score_itinerary(it, stops, ctx, sunset)
    return it


def clip(x):
    return max(0.0, min(1.0, x))


TRAVEL_WEIGHT = 0.004  # score lost per minute of travel: an extra hour costs 0.24
MAX_EXTRA_TRAVEL_MIN = 30  # alternatives may take at most this much longer to get around than plan 1
MIN_DIFFERENT_STOPS = 2  # each alternative differs from every other plan in at least this many stops


def group_score(vals):
    """Favor plans where nobody is unhappy: 60% the worst-off person, 40% the average."""
    return 0.6 * min(vals) + 0.4 * sum(vals) / len(vals)


def score_itinerary(it, stops, ctx, sunset):
    lim = ctx["limits"]
    per = {}
    for p in ctx["people"]:
        parts = []
        mk = p["walking"]["max_km"]
        parts.append(1 - clip((it["walk_km"] - 0.6 * mk) / (0.4 * mk)))
        if p["walking"].get("hills") == "avoid":
            parts.append(1 - clip(it["climb_m"] / 150))
        else:
            parts.append(1 - 0.3 * clip(it["climb_m"] / 400))
        rides = sum(1 for lg in it["legs"] if lg["mode"] == "ride")
        parts.append(1 - clip(0.15 * rides))  # people asked for a walking day
        parts.append(1 - clip(it["idle_min"] / 150.0))  # hours of nothing before sunset
        crowd = 0
        for cat, v in stops:
            tags = v.get("tags", [])
            if "crowds" in tags and "crowds" in p["activities"]["dislikes"]:
                crowd += 0.08
            if cat == "museum":
                if v.get("kind") == "museum":
                    if lim["dog"] and lim["dog_owner"] == p["id"]:
                        parts.append(0.35)  # waits outside with the dog
                    else:
                        s = min(1.0, p["activities"]["museum_max_min"] / 90.0)
                        if set(tags) & set(p["activities"]["likes"]):
                            s = min(1.0, s + 0.1)
                        parts.append(s)
                else:  # they asked for a museum; an outdoor swap is a compromise
                    parts.append(0.75 if "views" in p["activities"]["likes"] else 0.55)
            elif cat in ("cafe", "food"):
                parts.append(1.0 if set(tags) & set(p["food"]["loves"]) else 0.7)
            elif cat == "beach":
                wx = it["weather_at_beach"]
                s = 1.0
                if wx["fog"]:
                    s -= 0.3
                tol = {"low": 15, "medium": 25, "high": 40}[p["weather"]["wind_tolerance"]]
                if wx["wind_kmh"] > tol:
                    s -= 0.3
                if p["weather"]["cold_tolerance"] == "low" and wx["temp_c"] < 14:
                    s -= 0.2
                arrive = to_min(it["stops"][-1]["arrive"])
                if not (sunset - 60 <= arrive <= sunset):
                    s -= 0.2  # misses the sunset window
                parts.append(clip(s))
        per[p["name"]] = round(clip(sum(parts) / len(parts) - crowd), 2)
    vals = list(per.values())
    happiness = group_score(vals)
    # Minimizing time spent getting around is a main goal: each minute of travel (walking
    # or riding) costs TRAVEL_WEIGHT, so a plan that zigzags across the city ranks low.
    group = 0.0 if it["violations"] else max(0.001, happiness - TRAVEL_WEIGHT * it["travel_min"])
    it["scores"] = per
    it["happiness"] = round(happiness, 3)
    it["group_score"] = round(group, 3)
    if lim["dog"]:
        it["scores"]["Biscuit"] = "happy" if not any("dog" in x for x in it["violations"]) else "left out"


# ---------------------------------------------------------------- branch work

def stop_names(it):
    return {s["category"]: s["name"] for s in it["stops"]}


def pick_diverse(plans, n=3, penalty=0.03):
    """Judge: the best plan, then up to n-1 real alternatives.

    plans: list of (branch_name, itinerary). Plan 1 is the highest score (happiness minus
    travel time). An alternative must differ from every chosen plan in at least
    MIN_DIFFERENT_STOPS stops and take at most MAX_EXTRA_TRAVEL_MIN longer to get around
    than plan 1, so no alternative zigzags across the city. Each stop it shares with an
    already chosen plan costs `penalty`. Returns fewer than n plans rather than a bad one.
    """
    pool = sorted(plans, key=lambda p: -p[1]["group_score"])
    if not pool:
        return []
    chosen = [pool[0]]
    limit = pool[0][1]["travel_min"] + MAX_EXTRA_TRAVEL_MIN
    cats = ("cafe", "museum", "food", "beach")
    while len(chosen) < n:
        used = [stop_names(it) for _, it in chosen]

        def shared(item, u):
            s = stop_names(item[1])
            return sum(s.get(c) == u.get(c) for c in cats)

        ok = [p for p in pool if p not in chosen and p[1]["travel_min"] <= limit
              and all(len(cats) - shared(p, u) >= MIN_DIFFERENT_STOPS for u in used)]
        if not ok:
            break
        best = max(ok, key=lambda p: p[1]["group_score"] - penalty * sum(shared(p, u) for u in used))
        chosen.append(best)
    return chosen


def route_branch(museum, beach, candidates, ctx, keep=4):
    """Everything one route branch does: try every cafe x food x order."""
    results, dropped = [], {}
    for cafe, food in itertools.product(candidates["cafe"], candidates["food"]):
        for order in (("cafe", "museum", "food", "beach"), ("cafe", "food", "museum", "beach")):
            pick = {"cafe": cafe, "museum": museum, "food": food, "beach": beach}
            it = simulate([(c, pick[c]) for c in order], ctx)
            if it["violations"]:
                for v in it["violations"]:
                    dropped[v] = dropped.get(v, 0) + 1
                continue
            results.append(it)
    results.sort(key=lambda r: -r["group_score"])
    best, seen = [], set()
    for r in results:  # keep the best plan per cafe+food pair, so the judge has variety
        pair = tuple(s["name"] for s in r["stops"] if s["category"] in ("cafe", "food"))
        if pair not in seen:
            seen.add(pair)
            best.append(r)
        if len(best) == keep:
            break
    top_drops = sorted(dropped.items(), key=lambda kv: -kv[1])[:3]
    return best, [d for d, _ in top_drops], len(results)
