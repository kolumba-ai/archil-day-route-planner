"""Combine context shards, simulate a day's route, and score it for the group.

Works for any city and any set of stops. The context (ctx) says where and when:
ctx["start"] (name, lat, lon), ctx["start_min"] (minutes after midnight), ctx["categories"]
(the stop types asked for), ctx["conditions"] (weather and sunset), and optionally
ctx["elev"] (elevation per place, for hills) and ctx["streets"] (street distances).
Without those, it falls back to the San Francisco demo: Noe Valley at 10:00, the demo's
four stops, and rough San Francisco hill zones.
"""
import itertools
import json
import math
import os

from . import crime

HERE = os.path.dirname(__file__)

START = {"name": "Start (Noe Valley)", "lat": 37.7502, "lon": -122.4337}  # San Francisco demo
START_TIME = 10 * 60  # 10:00
DEFAULT_CATEGORIES = ["cafe", "museum", "food", "beach"]

# Rough San Francisco hill zones, used only for the demo city when no elevation data is given.
HILLS = [
    ("Twin Peaks", 37.7544, -122.4477, 1.2, 150),
    ("Nob Hill", 37.7930, -122.4161, 0.5, 70),
    ("Russian Hill", 37.8011, -122.4194, 0.4, 60),
    ("Pacific Heights", 37.7925, -122.4382, 0.8, 70),
    ("Bernal Heights", 37.7430, -122.4150, 0.5, 60),
    ("Lone Mountain", 37.7800, -122.4520, 0.6, 40),
    ("Lands End bluffs", 37.7850, -122.4990, 0.5, 40),
]

DWELL = {"cafe": 30, "food": 45, "park": 45, "outdoor_alternative": 50, "beach": 60, "waterfront": 45}
STROLL_BROWSE_MIN = 20  # looking in windows along the way


def sunset_stop(ctx):
    """The stop type timed for sunset ("riverside at sunset"); a beach by default."""
    req = (ctx or {}).get("request") or {}
    if "sunset_stop" in req:
        return req["sunset_stop"]
    return "beach"
HILLY_M = 200  # total climb that's too much for someone who avoids hills
HILLY_AVOID_REQUEST_M = 60  # when the request itself says "avoid hills"


def load_conditions():
    with open(os.path.join(HERE, "conditions.json")) as f:
        return json.load(f)


def key(p):
    return "%.3f,%.3f" % (p["lat"], p["lon"])  # same keys as streets.key


# ---------------------------------------------------------------- combine

def combine(profiles, request, start=None, conditions=None):
    """Merge person and pet shards, plus what the request asks, into limits for the group."""
    humans = [p for p in profiles if p["kind"] == "person"]
    dogs = [p for p in profiles if p["kind"] == "dog"]
    max_km = min([p["walking"]["max_km"] for p in profiles])
    if request.get("max_walk_km"):
        max_km = min(max_km, request["max_walk_km"])
    limits = {
        "max_km": max_km,
        "pace_kmh": min(p["walking"]["pace_kmh"] for p in humans),
        # The request wins over profiles: "I want hills" overrides a profile that avoids them.
        "avoid_hills": request.get("avoid_hills", False) or (
            not request.get("prefer_hills") and any(p["walking"].get("hills") == "avoid" for p in humans)),
        "hills_max_m": HILLY_AVOID_REQUEST_M if request.get("avoid_hills") else HILLY_M,
        "prefer_hills": bool(request.get("prefer_hills")),
        "diets": sorted({p["food"]["diet"] for p in humans} - {"omnivore"}),
        "avoid_foods": sorted({f for p in humans for f in p["food"]["avoids"]}),
        "museum_max_min": min(p["activities"]["museum_max_min"] for p in humans),
        "dog": bool(dogs),
        # Street crime: the most cautious person decides; "low" (avoid crime) by default.
        "crime_tolerance": min(((p.get("safety") or {}).get("crime_tolerance") or "low" for p in humans),
                               key=lambda t: {"low": 0, "medium": 1, "high": 2}.get(t, 0), default="low"),
        "dog_owner": dogs[0]["owner"] if dogs else None,
    }
    return {"request": request, "people": humans, "pets": dogs, "limits": limits,
            "conditions": conditions or load_conditions(),
            "start": start or START,
            "start_min": request.get("start_min", START_TIME),
            "end_min": request.get("end_min"),  # latest finish, if the request gave one
            "return_to_start": bool(request.get("return_to_start")),  # walk back to the start at the end
            "categories": request.get("categories") or list(DEFAULT_CATEGORIES),
            "city": request.get("city")}


# ---------------------------------------------------------------- geometry

def km(a, b):
    r = 6371.0
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dp = p2 - p1
    dl = math.radians(b["lon"] - a["lon"])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def climb_m(a, b, ctx=None):
    """Meters climbed walking from a to b, and the named hills crossed.

    With elevation data (ctx["elev"]) it's the real uphill difference; in the San Francisco
    demo without it, rough hill zones; anywhere else without data, 0.
    """
    elev = (ctx or {}).get("elev")
    if elev and key(a) in elev and key(b) in elev:
        up = max(0, round(elev[key(b)] - elev[key(a)]))
        return up, (["uphill %d m" % up] if up >= 20 else [])
    if ctx and ctx.get("city"):
        return 0, []
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


CRIME_RIDE_PENALTY = 0.1  # off the group score for each ride taken to avoid serious crime
RIDE_OVERHEAD_MIN = 8  # waiting for transit or a car, and walking to and from it


def leg(a, b, pace_kmh, streets=None, ctx=None, minute=None):
    """Walk if reasonable and safe, otherwise ride: a bus when one route serves both ends,
    else an Uber/Lyft/taxi. "Safe": the walk passes no street-crime hotspot at that hour,
    for the group's crime tolerance (planner/crime.py).

    With a street matrix (streets.build_matrix) distances follow real streets and paths,
    and rides use real driving times; without one, straight-line distance x 1.3.
    """
    ka, kb = key(a), key(b)
    on_streets = bool(streets) and kb in streets["walk_km"].get(ka, {})
    d = streets["walk_km"][ka][kb] if on_streets else km(a, b) * 1.3
    up, hills = climb_m(a, b, ctx)
    walk_min = d / pace_kmh * 60 + up / 10  # +1 min per 10 m climbed
    unsafe = None
    if walk_min <= 35:
        tolerance = ((ctx or {}).get("limits") or {}).get("crime_tolerance", "low")
        spot = crime.hotspot((ctx or {}).get("crime"), a, b, minute if minute is not None else 12 * 60, tolerance)
        walk = {"mode": "walk", "km": round(d, 2), "min": round(walk_min), "climb_m": up, "hills": hills,
                "streets": on_streets}
        if not spot:
            return walk
        if not spot["avoid"]:  # below their limit (or they don't mind): walk, and say what's there
            walk["crime"] = spot
            return walk
        unsafe = spot  # too many serious crimes there at this hour: ride instead
    bus = (((ctx or {}).get("bus") or {}).get("pairs") or {}).get(transit_key(a), {}).get(transit_key(b))
    if bus:  # a real bus route stops near both ends (planner/transit.py)
        return {"mode": "ride", "km": round(d, 2), "min": bus["min"], "climb_m": 0, "hills": [],
                "walk_km": bus["walk_km"], "streets": on_streets, "bus": bus, "avoids": unsafe}
    ride_min = (streets["car_min"][ka][kb] + RIDE_OVERHEAD_MIN) if on_streets else 12 + d * 3
    return {"mode": "ride", "km": round(d, 2), "min": round(ride_min), "climb_m": 0, "hills": [],
            "walk_km": 0.3, "streets": on_streets, "rideshare": True, "avoids": unsafe}


def transit_key(p):
    return "%.5f,%.5f" % (p["lat"], p["lon"])


def round_departure(arrive, leave, close=None):
    """Departures on the nearest 5 minutes (10:00, 11:35), when feasible: never less than 10
    minutes at a stop and never past closing. Arrivals stay exact."""
    nearest = int(5 * round(leave / 5.0))
    for t in (nearest, nearest + 5, nearest - 5):
        if t % 5 == 0 and (arrive is None or t >= arrive + 10) and (close is None or t <= close):
            return t
    return leave


def hhmm(m):
    return "%02d:%02d" % (int(m) // 60, int(m) % 60)


def to_min(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def weather_at(cond, minute, area=None):
    hourly = cond["hourly"]
    hours = sorted(int(h) for h in hourly)
    h = min(hours, key=lambda x: abs(x - minute // 60))
    slot = hourly[str(h)]
    return slot.get(area) or slot.get("all") or next(iter(slot.values()))


# ---------------------------------------------------------------- simulate

def simulate(stops, ctx):
    """stops: list of (category, venue) in visiting order. Returns an itinerary with scores."""
    lim = ctx["limits"]
    cond = ctx["conditions"]
    sunset = to_min(cond["sunset"])
    start = ctx.get("start") or START
    start_min = round_departure(None, ctx.get("start_min", START_TIME))
    t = start_min
    idle = 0
    here = start
    legs, timeline, violations, notes = [], [], [], []
    walk_km = climb = 0
    for cat, v in stops:
        lg = leg(here, v, lim["pace_kmh"], ctx.get("streets"), ctx, t)
        if lg.get("avoids"):
            notes.append("%s to %s instead of walking: %s" % (
                "Bus " + lg["bus"]["route"] if lg.get("bus") else "Uber/Lyft/taxi", v["name"], crime.describe(lg["avoids"])))
        elif lg.get("crime"):
            notes.append("Walk to %s passes %s" % (v["name"], crime.describe(lg["crime"])))
        legs.append(lg)
        t += lg["min"]
        walk_km += lg["km"] if lg["mode"] == "walk" else lg.get("walk_km", 0)
        climb += lg["climb_m"]
        arrive = t
        if cat == sunset_stop(ctx):  # aim to be there for sunset
            if arrive < sunset - 60:
                idle = sunset - 60 - arrive
                t = arrive = sunset - 60
                if idle >= 45:
                    notes.append("About %d min of free time before %s" % (idle, v["name"]))
        if cat == "stroll":  # walking the street at an easy pace, with time to look around
            dwell = round(v.get("stroll_km", 1.0) / lim["pace_kmh"] * 60 * 1.3) + STROLL_BROWSE_MIN
            walk_km += v.get("stroll_km", 0)
        elif cat == "museum":
            dwell = min(lim["museum_max_min"], 90) if v.get("kind") == "museum" else DWELL["outdoor_alternative"]
        else:
            dwell = DWELL.get(cat, 45)
        leave = round_departure(arrive, arrive + dwell, to_min(v["close"]))
        if arrive < to_min(v["open"]) or leave > to_min(v["close"]):
            violations.append("%s is closed then (%s-%s)" % (v["name"], v["open"], v["close"]))
        if lim["dog"]:
            if cat in ("cafe", "food") and v["dogs"] == "no":
                violations.append("%s: no dogs, not even on a patio" % v["name"])
            if cat in ("beach", "park", "waterfront") and v["dogs"] == "no":
                violations.append("%s: dogs not allowed" % v["name"])
            if cat == "museum" and v.get("kind") == "museum":
                notes.append("Dogs can't go into %s; one person walks Biscuit outside" % v["name"])
        if "vegetarian" in lim["diets"] and cat in ("cafe", "food") and not v.get("vegetarian_ok", True):
            violations.append("%s: nothing vegetarian" % v["name"])
        if "gluten_free" in lim["diets"] and cat in ("cafe", "food") and v.get("gluten_free_ok") is False:
            violations.append("%s: no gluten-free options" % v["name"])
        if v.get("verify_error"):
            notes.append("Couldn't check %s online; confirm its hours and dog rules" % v["name"])
        if cat == "food" and (ctx.get("request") or {}).get("food_truck") and not v.get("food_truck"):
            notes.append("No food truck fits this route; %s is a restaurant" % v["name"])
        stop = {"category": cat, "name": v["name"], "lat": v["lat"], "lon": v["lon"],
                "arrive": hhmm(arrive), "leave": hhmm(leave), "source": v.get("source", "")}
        if cat == "stroll":
            stop.update(stroll_km=v.get("stroll_km"), stroll_min=dwell - STROLL_BROWSE_MIN, path=v.get("path"),
                        end_lat=v["end_lat"], end_lon=v["end_lon"], shops=v.get("shops"))
        timeline.append(stop)
        t = leave
        here = {"name": v["name"], "lat": v["end_lat"], "lon": v["end_lon"]} if cat == "stroll" else v
    back = None
    if ctx.get("return_to_start"):  # "back at the place of departure": the walk home counts too
        back = leg(here, start, lim["pace_kmh"], ctx.get("streets"), ctx, t)
        if back.get("avoids"):
            notes.append("%s back to the start instead of walking: %s" % (
                "Bus " + back["bus"]["route"] if back.get("bus") else "Uber/Lyft/taxi", crime.describe(back["avoids"])))
        t += back["min"]
        walk_km += back["km"] if back["mode"] == "walk" else back.get("walk_km", 0)
        climb += back["climb_m"]
    walk_km = round(walk_km, 2)
    if walk_km > lim["max_km"] + 1e-9:
        violations.append("walks %.1f km; the limit is %s km" % (walk_km, ("%g" % lim["max_km"])))
    if ctx.get("end_min") and t > ctx["end_min"]:
        violations.append("finishes at %s, after %s" % (hhmm(t), hhmm(ctx["end_min"])))
    if lim["avoid_hills"] and not lim.get("prefer_hills") and climb > lim.get("hills_max_m", HILLY_M):
        violations.append("climbs %d m; too hilly for the group" % climb)

    last_cat, last = stops[-1]
    wx = weather_at(cond, to_min(timeline[-1]["arrive"]), last.get("area"))
    if last_cat in ("beach", "waterfront") and wx["fog"]:
        notes.append("Fog expected at %s around %s" % (last["name"], timeline[-1]["arrive"]))

    it = {"depart": hhmm(start_min), "start": start["name"], "city": ctx.get("city") or "San Francisco", "start_lat": start["lat"], "start_lon": start["lon"],
          "stops": timeline, "legs": legs, "walk_km": walk_km, "climb_m": climb,
          "travel_min": sum(lg["min"] for lg in legs) + (back["min"] if back else 0), "idle_min": idle,
          "return_leg": back,
          "end": hhmm(t), "violations": violations, "notes": notes, "weather_at_end": wx}
    score_itinerary(it, stops, ctx, sunset)
    return it


def clip(x):
    return max(0.0, min(1.0, x))


TRAVEL_WEIGHT = 0.004  # score lost per minute of travel: an extra hour costs 0.24
MAX_EXTRA_TRAVEL_MIN = 30  # alternatives may take at most this much longer to get around than plan 1
MIN_DIFFERENT_STOPS = 2  # each alternative differs from every other plan in at least this many stops
HILL_BONUS = 0.3  # for "I want hills": score added for climbing, full at 100 m


def group_score(vals):
    """Favor plans where nobody is unhappy: 60% the worst-off person, 40% the average."""
    return 0.6 * min(vals) + 0.4 * sum(vals) / len(vals)


# What people love, in their words, as OpenStreetMap cuisine tags ("burgers" -> burger).
LOVE_TAGS = {"burger": {"burger"}, "steak": {"steak_house", "steak"}, "salad": {"salad"}, "thai": {"thai"},
             "coffee": {"coffee_shop", "coffee", "cafe"}, "burrito": {"burrito", "mexican", "burritos"},
             "pastrie": {"pastry", "bakery", "pastries"}, "pizza": {"pizza"}, "sushi": {"sushi", "japanese"}}


def loves(person, cat, tags):
    """Does the place serve something this person loves? Plurals and OSM tag names both count."""
    tags = {t.lower() for t in tags} | ({"cafe"} if cat == "cafe" else set())
    for word in person["food"]["loves"]:
        stem = word.lower().strip().rstrip("s")
        if tags & (LOVE_TAGS.get(stem, set()) | {word.lower(), stem}):
            return True
    return False


def dislikes(person, word):
    return any(word in d.lower() for d in person["activities"].get("dislikes", []))


def score_itinerary(it, stops, ctx, sunset):
    lim = ctx["limits"]
    per = {}
    for p in ctx["people"]:
        parts = []
        mk = min(p["walking"]["max_km"], lim["max_km"])
        parts.append(1 - clip((it["walk_km"] - 0.6 * mk) / (0.4 * mk)))
        if lim.get("prefer_hills"):  # the request asks for hills: climbing is the point
            parts.append(clip(it["climb_m"] / 120))
        elif p["walking"].get("hills") == "avoid" or lim["avoid_hills"]:
            parts.append(1 - clip(it["climb_m"] / 150))
        else:
            parts.append(1 - 0.3 * clip(it["climb_m"] / 400))
        rides = sum(1 for lg in it["legs"] if lg["mode"] == "ride")
        parts.append(1 - clip(0.15 * rides))  # people asked for a walking day
        parts.append(1 - clip(it["idle_min"] / 150.0))  # hours of nothing before sunset
        crowd = 0
        for cat, v in stops:
            tags = v.get("tags", [])
            if "crowds" in tags and dislikes(p, "crowd"):
                crowd += 0.08
            if "noisy" in tags and dislikes(p, "noisy"):  # web reviews say so: "noisy restaurants"
                crowd += 0.08
            if "small_indoor" in tags and dislikes(p, "indoor"):  # "tight indoor spaces"
                crowd += 0.08
            if cat == "museum":
                if v.get("kind") == "museum":
                    if lim["dog"] and lim["dog_owner"] == p["id"]:
                        parts.append(0.35)  # waits outside with the dog
                    else:
                        s = min(1.0, p["activities"]["museum_max_min"] / 90.0)
                        if set(tags) & set(p["activities"]["likes"]):
                            s = min(1.0, s + 0.1)
                        if dislikes(p, "indoor"):  # "tight indoor spaces"
                            s -= 0.15
                        parts.append(clip(s))
                else:  # they asked for a museum; an outdoor swap is a compromise
                    parts.append(0.75 if "views" in p["activities"]["likes"] else 0.55)
            elif cat in ("cafe", "food"):
                parts.append(1.0 if loves(p, cat, tags) else 0.7)
                if p["food"].get("diet") == "gluten_free":  # known gluten-free options beat unknown ones
                    parts.append({True: 1.0, None: 0.8}.get(v.get("gluten_free_ok"), 0.0))
                if cat == "food" and (ctx.get("request") or {}).get("food_truck"):
                    parts.append(1.0 if v.get("food_truck") else 0.5)  # they asked for a food truck
            elif cat == "park":
                parts.append(0.9 if "views" in p["activities"]["likes"] else 0.8)
            elif cat in ("beach", "waterfront"):
                wx = it["weather_at_end"]
                s = 1.0 if cat == "beach" else (0.95 if "views" in p["activities"]["likes"] else 0.85)
                if "water" in p["activities"]["likes"]:
                    s = min(1.0, s + 0.1)
                if wx["fog"]:
                    s -= 0.3
                tol = {"low": 15, "medium": 25, "high": 40}[p["weather"]["wind_tolerance"]]
                if wx["wind_kmh"] > tol:
                    s -= 0.3
                if p["weather"]["cold_tolerance"] == "low" and wx["temp_c"] < 14:
                    s -= 0.2
                arrive = to_min(it["stops"][-1]["arrive"])
                if cat == sunset_stop(ctx) and not (sunset - 60 <= arrive <= sunset):
                    s -= 0.2  # misses the sunset window
                parts.append(clip(s))
        per[p["name"]] = round(clip(sum(parts) / len(parts) - crowd), 2)
    vals = list(per.values())
    happiness = group_score(vals)
    # Minimizing time spent getting around is a main goal: each minute of travel (walking
    # or riding) costs TRAVEL_WEIGHT, so a plan that zigzags across the city ranks low.
    group = happiness - TRAVEL_WEIGHT * it["travel_min"]
    # A ride forced by crime is a detour on the group's day: a plan that walks safely ranks higher.
    crime_rides = sum(1 for lg in it["legs"] + ([it["return_leg"]] if it.get("return_leg") else [])
                      if lg.get("avoids"))
    it["crime_rides"] = crime_rides
    group -= CRIME_RIDE_PENALTY * crime_rides
    if lim.get("prefer_hills"):  # they asked for hills: climbing is a goal, up to HILL_BONUS
        group += HILL_BONUS * clip(it["climb_m"] / 100.0)
    group = 0.0 if it["violations"] else max(0.001, group)
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
    MIN_DIFFERENT_STOPS stops (1 when a day has only one or two stops) and take at most
    MAX_EXTRA_TRAVEL_MIN longer to get around than plan 1, so no alternative zigzags across
    the city. Each stop it shares with an already chosen plan costs `penalty`. Returns
    fewer than n plans rather than a bad one.
    """
    pool = sorted(plans, key=lambda p: -p[1]["group_score"])
    if not pool:
        return []
    chosen = [pool[0]]
    limit = pool[0][1]["travel_min"] + MAX_EXTRA_TRAVEL_MIN
    cats = sorted(stop_names(pool[0][1]))
    need = min(MIN_DIFFERENT_STOPS, max(1, len(cats) - 1))
    while len(chosen) < n:
        used = [stop_names(it) for _, it in chosen]

        def shared(item, u):
            s = stop_names(item[1])
            return sum(s.get(c) == u.get(c) for c in cats)

        ok = [p for p in pool if p not in chosen and p[1]["travel_min"] <= limit
              and all(len(cats) - shared(p, u) >= need for u in used)]
        if not ok:
            break
        best = max(ok, key=lambda p: p[1]["group_score"] - penalty * sum(shared(p, u) for u in used))
        chosen.append(best)
    return chosen


ANCHOR_ORDER = ["museum", "beach", "waterfront", "park", "food", "cafe"]


def anchors(categories):
    """The (up to) two stop types that define a branch: one branch per combination of them."""
    return [c for c in ANCHOR_ORDER if c in categories][:2]


def branch_specs(candidates, categories):
    """One branch per combination of anchor venues, as {category: index into candidates}."""
    an = anchors(categories)
    return [dict(zip(an, idx)) for idx in itertools.product(*[range(len(candidates[c])) for c in an])]


def orders(categories, last="beach"):
    """The order the person asked for, kept as is. The one exception is a stop they want at
    sunset ("riverside at sunset"), which can only come last."""
    order = [c for c in categories if c != last] + ([last] if last in categories else [])
    return [tuple(order)]


def route_branch(fixed, candidates, ctx, keep=4):
    """Everything one route branch does. `fixed` maps the anchor stop types to the venues this
    branch owns; it tries every combination of the other stops in every sensible order."""
    cats = ctx.get("categories") or list(DEFAULT_CATEGORIES)
    free = [c for c in cats if c not in fixed]
    results, dropped = [], {}
    for combo in itertools.product(*[candidates[c] for c in free]):
        pick = dict(fixed, **dict(zip(free, combo)))
        for order in orders(cats, sunset_stop(ctx)):
            it = simulate([(c, pick[c]) for c in order], ctx)
            if it["violations"]:
                for v in it["violations"]:
                    dropped[v] = dropped.get(v, 0) + 1
                continue
            results.append(it)
    results.sort(key=lambda r: -r["group_score"])
    best, seen = [], set()
    for r in results:  # keep the best plan per combination of the other stops, for variety
        combo = tuple(s["name"] for s in r["stops"] if s["category"] in free)
        if combo not in seen:
            seen.add(combo)
            best.append(r)
        if len(best) == keep:
            break
    top_drops = sorted(dropped.items(), key=lambda kv: -kv[1])[:3]
    return best, [d for d, _ in top_drops], len(results)


def branch_label(fixed):
    return " + ".join(v["name"] for v in fixed.values())
