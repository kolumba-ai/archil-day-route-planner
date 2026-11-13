"""Search step: find candidate venues for one stop type.

Uses Claude with the server-side web search tool when ANTHROPIC_API_KEY is set
and the `anthropic` package is installed. Otherwise falls back to seed data so
the demo always runs.
"""
import json
import os
import random
import re
import time

HERE = os.path.dirname(__file__)
MODEL = "claude-opus-5-5"

FIELDS = (
    'name, lat, lon, open ("HH:MM" on the requested day), close ("HH:MM"), '
    'dogs (one of "no", "patio", "leash", "offleash"), vegetarian_ok (true/false), '
    'kind ("museum" or "outdoor_alternative", museum stops only), '
    'area ("west", "northwest", "north", beach stops only), tags (list of short words), '
    'source (URL you used)'
)

CATEGORY_HINT = {
    "cafe": "coffee shops or cafes",
    "museum": "museums",
    "food": "burrito places or casual Mexican restaurants",
    "beach": "beaches",
    "waterfront": "riverside or waterfront walks, parks, piers and promenades",
    "park": "parks",
}


def live_search_available():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def load_seed(category):
    with open(os.path.join(HERE, "seed_venues.json")) as f:
        data = json.load(f)
    venues = data.get(category, [])
    for v in venues:
        v.setdefault("source", "seed data (illustrative)")
    return venues


def provider_label():
    mode = os.environ.get("PLANNER_SEARCH", "")
    if RECORDED_LABEL in os.environ:
        return "recorded search results"
    if mode == "keenable":
        return "Keenable (checks seed venues on the web)"
    if mode != "seed" and live_search_available():
        return "Claude web search"
    return "seed data"


RECORDED_LABEL = "PLANNER_CANDIDATES"


def search_category(category, context):
    """Return (venues, source_label). `context` is the combined outing context.

    PLANNER_SEARCH picks the provider: "keenable" (check candidates on the web with
    Keenable), "claude" (Claude web search, needs ANTHROPIC_API_KEY) or "seed".
    Default: Claude if a key is set, otherwise seed data.
    """
    mode = os.environ.get("PLANNER_SEARCH", "")
    if category == "stroll":  # a walk along the street they named, from OpenStreetMap
        from . import places
        street = (context.get("request") or {}).get("stroll_street")
        try:
            v = places.find_stroll(street, context["start"]) if street else None
        except Exception as e:
            print("Stroll lookup failed for %s: %r" % (street, e))
            v = None
        return ([v] if v else []), "OpenStreetMap"
    if context.get("city"):
        return search_city(category, context, mode)
    if mode == "keenable":
        from . import keenable
        try:
            return keenable.verify_all(load_seed(category), category), "Keenable search"
        except Exception as e:
            print("Keenable search failed for %s: %r" % (category, e))
            return load_seed(category), "seed data"
    if mode != "seed" and live_search_available():
        try:
            return _claude_search(category, context), "Claude web search"
        except Exception as e:  # fall back so the demo keeps going
            print("live search failed for %s: %r" % (category, e))
    time.sleep(random.uniform(0.6, 1.6))  # make the offline run look like real fan-out
    return load_seed(category), "seed data"


def search_radius_m(context, category=None):
    """How far from the start to look: about 60% of the walking limit, 600 m to 2.5 km.
    A riverside or beach is where the water is, so look up to 7 km out (a long leg is a ride)."""
    limit_km = context["limits"]["max_km"]
    r = int(max(600, min(2500, limit_km * 1000 * 0.6)))
    return max(r, 7000) if category in ("waterfront", "beach") else r


def search_city(category, context, mode):
    """Any city other than the San Francisco demo: real places near the start from
    OpenStreetMap, then (with PLANNER_SEARCH=keenable) checked on the web with Keenable."""
    from . import places
    from .request import DAYS
    day_index = DAYS.index(context["request"].get("day", "Saturday").lower())
    req = context["request"]
    center, radius = context["start"], search_radius_m(context, category)
    near = (req.get("stop_near") or {}).get(category)
    if mode not in ("osm", "seed", "keenable") and web_venues_available():
        try:  # current places from the web; the map only gives their coordinates
            venues = web_venues(category, context, near)
            if venues:
                return venues, "Claude web search + OpenStreetMap coordinates"
            print("Web search found no %s; using the map data" % category)
        except Exception as e:
            print("Web venue search failed for %s: %r; using the map data" % (category, e))
    if near:  # "burritos near Hawthorne": look around that street or neighborhood, not the start
        try:
            area = places.find_area(near, context["city"], context["start"])
        except Exception as e:
            print("Couldn't place %s: %r" % (near, e))
            area = None
        if area:
            center, radius = area, area["radius_m"]
    venues = places.find_venues(category, center, radius, day_index=day_index,
                                cuisines=req.get("cuisines") or None, food_truck=bool(req.get("food_truck")))
    source = "OpenStreetMap"
    if venues and mode == "keenable":
        from . import keenable
        try:
            venues = keenable.verify_all(venues, category, city=context["city"])
            source = "OpenStreetMap + Keenable"
        except Exception as e:
            print("Keenable check failed for %s: %r" % (category, e))
    return venues, source


WEB_VENUE_MODEL = "claude-opus-5-5"
AREA_STOPS = ("park", "beach", "waterfront")  # placed by their outline on the map, not a mailing address
WEB_HINT = {
    "cafe": "coffee shops and cafes", "museum": "museums and galleries", "food": "restaurants for a meal",
    "beach": "beaches", "park": "parks and gardens",
    "waterfront": "riverside or waterfront walks, parks, piers and promenades",
}
VENUE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["places"], "properties": {"places": {
    "type": "array", "items": {"type": "object", "additionalProperties": False,
                               "required": ["name", "street_address", "open_that_day", "open", "close", "dogs",
                                            "dogs_known", "gluten_free", "vegetarian_ok", "cuisine", "food_truck",
                                            "tags", "source"],
                               "properties": {
                                   "name": {"type": "string"},
                                   "street_address": {"type": "string"},
                                   "open_that_day": {"type": "boolean"},
                                   "open": {"type": "string"}, "close": {"type": "string"},
                                   "dogs": {"type": "string", "enum": ["no", "patio", "leash", "offleash"]},
                                   "dogs_known": {"type": "boolean"},
                                   "gluten_free": {"type": "string", "enum": ["yes", "no", "unknown"]},
                                   "vegetarian_ok": {"type": "boolean"},
                                   "cuisine": {"type": "array", "items": {"type": "string"}},
                                   "food_truck": {"type": "boolean"},
                                   "tags": {"type": "array", "items": {"type": "string"}},
                                   "source": {"type": "string"}}}}}}
WEB_VENUE_SYSTEM = (
    "You find real, currently open places for a planned day out, using web search: official sites, maps "
    "listings and recent reviews. Never list a place that has closed for good or that you can't confirm. "
    "For each place give its name, its street address (number and street, no city), whether it is open on the "
    "day asked, its opening and closing time that day as 24-hour HH:MM (empty if unknown), its dog policy "
    "(no / patio / leash / offleash; dogs_known false if you couldn't confirm it), whether it has gluten-free "
    "options (yes / no / unknown), vegetarian options, its cuisines as lowercase English words (\"mexican\", "
    "\"thai\", \"burger\", \"coffee\"), whether it is a food truck or cart, and short lowercase tags that "
    "describe it: use \"crowds\" if it is usually crowded, \"noisy\" or \"quiet\", \"outdoor_seating\", "
    "\"views\", \"water\", \"small_indoor\" for a cramped room (single words only). Give the URL you relied on "
    "as source.")


def web_venues_available():
    from . import understand
    return live_search_available() and not understand.rules_mode()


def web_venues(category, context, near=None, want=8):
    """Places for one stop type found with Claude's web search (current hours, closures, dog rules,
    gluten-free options), then placed on the map at their street address. Places the map can't
    find near the start (or near the street they asked for) are left out."""
    import anthropic
    from concurrent.futures import ThreadPoolExecutor
    from . import places
    from .routes import km
    req, start, lim = context["request"], context["start"], context["limits"]
    radius_km = search_radius_m(context, category) / 1000.0
    where = "near %s in %s" % (near, context["city"]) if near else "within %.1f km walking of %s%s in %s" % (
        radius_km, start.get("name", "the start"), (", " + start["address"]) if start.get("address") else "",
        context["city"])
    asks = []
    if category == "food":
        if req.get("cuisines"):
            asks.append("cuisine: %s" % " or ".join(req["cuisines"]))
        if req.get("food_truck"):
            asks.append("food trucks or carts")
        diets = set(lim.get("diets") or [])
        if diets:
            asks.append("the group's diets: %s" % ", ".join(sorted(d.replace("_", "-") for d in diets)))
        if lim.get("avoid_foods"):
            asks.append("avoid: %s" % ", ".join(lim["avoid_foods"]))
    if lim.get("dog"):
        asks.append("a dog is coming, so check dog policies")
    prompt = "Find %d %s %s, for %s %s. List as many as you can confirm, up to that number.%s" % (
        want, WEB_HINT.get(category, category), where, req.get("day", "Saturday"),
        places.next_date(req.get("day", "Saturday")),
        (" Needs: " + "; ".join(asks) + ".") if asks else "")
    client = anthropic.Anthropic(max_retries=1)
    messages = [{"role": "user", "content": prompt}]
    resp = None
    for _ in range(4):  # a long search turn can pause: send it back to continue
        resp = client.messages.create(
            model=WEB_VENUE_MODEL, max_tokens=16000, system=WEB_VENUE_SYSTEM, messages=messages,
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 8}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": VENUE_SCHEMA}},
            timeout=180)
        if resp.stop_reason != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": resp.content}]
    if resp.stop_reason != "end_turn":
        raise RuntimeError("web search ended with %s" % resp.stop_reason)
    found = json.loads([b.text for b in resp.content if b.type == "text"][-1])["places"]
    center = start
    if near:
        try:
            center = places.find_area(near, context["city"], start) or start
        except Exception:
            center = start
    reach_km = 2.0 if near else radius_km * 1.5

    found = [v for v in found if v.get("open_that_day", True) and v.get("street_address")]
    try:  # every address in one map query
        spots = places.find_addresses([v["street_address"] for v in found], center,
                                      radius_m=int(max(reach_km, 3) * 1000))
    except Exception as e:
        print("Address lookup failed: %r" % e)
        spots = {}

    def locate(v):
        pos = None if category in AREA_STOPS else spots.get(v["street_address"])
        if not pos:  # the place by name on the map (a park's middle, not its mailing address), then the geocoder
            try:
                named = places.find_by_name(v["name"], center, radius_m=int(max(reach_km, 3) * 1000))
                pos = (named and {"lat": named["lat"], "lon": named["lon"]}) or spots.get(v["street_address"]) \
                    or places.geocode_near("%s, %s" % (v["street_address"], context["city"]), center)
            except Exception as e:
                print("Couldn't place %s: %r" % (v["name"], e))
                pos = None
        return pos

    def place(v):
        pos = locate(v)
        if not pos or km(pos, center) > reach_km:
            print("Left out %s (%s): %s" % (v["name"], v["street_address"],
                                            "not on the map" if not pos else "%.1f km away" % km(pos, center)))
            return None
        hours = (v.get("open") or "", "23:59" if v.get("close") in ("00:00", "24:00") else v.get("close") or "")
        known = all(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", h) for h in hours)
        open_, close = hours if known else places.DEFAULT_HOURS[category]
        out = {"name": v["name"], "lat": round(pos["lat"], 6), "lon": round(pos["lon"], 6),
               "address": v["street_address"], "open": open_, "close": close, "hours_known": known,
               "dogs": v["dogs"], "dogs_known": v["dogs_known"], "vegetarian_ok": v["vegetarian_ok"],
               "gluten_free_ok": {"yes": True, "no": False}.get(v["gluten_free"]),
               "tags": list(dict.fromkeys([t.lower() for t in v["tags"] + v["cuisine"]
                                           if re.fullmatch(r"[a-z_]{2,20}", t.lower())] + [category])),
               "cuisine": sorted({c.lower() for c in v["cuisine"]}), "food_truck": v["food_truck"],
               "source": v.get("source") or "Claude web search"}
        if category == "museum":
            out["kind"] = "museum"
        if category in ("beach", "waterfront"):
            out["area"] = "all"
        return out
    with ThreadPoolExecutor(2) as pool:  # the map services refuse bursts
        venues = [v for v in pool.map(place, found) if v]
    print("Web search: %d %s found, %d placed on the map" % (len(found), category, len(venues)))
    if category == "food" and req.get("cuisines"):  # never a stand-in from another cuisine
        venues = [v for v in venues if places.matches_cuisine({"cuisine": ";".join(v["cuisine"]), "name": v["name"]},
                                                               req["cuisines"])] or venues
    seen, out = set(), []
    for v in sorted(venues, key=lambda v: km(v, center)):
        if v["name"].lower() not in seen:
            seen.add(v["name"].lower())
            out.append(v)
    return out


def _claude_search(category, context):
    import anthropic

    client = anthropic.Anthropic()
    limits = context["limits"]
    dog_line = ""
    if limits["dog"]:
        dog_line = (
            "A dog is coming. Check each place's dog policy. "
            + ("For museums, also include 2 dog-friendly outdoor alternatives with kind "
               '"outdoor_alternative". ' if category == "museum" else "")
        )
    prompt = (
        "Find 6 good %s in San Francisco for a day out on %s.\n"
        "Group needs: diets %s; avoid %s; hills: %s. %s\n"
        "Use web search to check opening hours for that day and dog rules.\n"
        "Reply with ONLY a JSON array in a ```json code block. Each item has: %s."
        % (
            CATEGORY_HINT.get(category, category),
            context["request"]["day"],
            ", ".join(limits["diets"]) or "none",
            ", ".join(limits["avoid_foods"]) or "nothing",
            "avoid steep hills" if limits["avoid_hills"] else "fine",
            dog_line,
            FIELDS,
        )
    )
    messages = [{"role": "user", "content": prompt}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 4}]
    resp = None
    for _ in range(4):  # resume server-tool turns that pause
        resp = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            messages=messages,
            tools=tools,
            extra_body={"output_config": {"effort": "low"}, "fallbacks": "default"},
            extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
        )
        if resp.stop_reason != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": resp.content}]
    if resp.stop_reason == "refusal":
        raise RuntimeError("model declined the request")
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S) or re.search(r"(\[.*\])", text, re.S)
    if not m:
        raise ValueError("no JSON array in reply")
    venues = json.loads(m.group(1))
    clean = []
    for v in venues:
        try:
            v["lat"], v["lon"] = float(v["lat"]), float(v["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        v.setdefault("open", "09:00")
        v.setdefault("close", "18:00")
        v.setdefault("dogs", "no")
        v.setdefault("vegetarian_ok", True)
        v.setdefault("tags", [])
        if category == "museum":
            v.setdefault("kind", "museum")
        if category == "beach":
            v.setdefault("area", "west" if v["lon"] < -122.495 else "north")
        clean.append(v)
    if not clean:
        raise ValueError("no usable venues")
    return clean
