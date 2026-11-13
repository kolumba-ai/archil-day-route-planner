"""Real places anywhere, from free, keyless services:

- geocode(): OpenStreetMap Nominatim, for the starting place or the city.
- find_venues(): OpenStreetMap Overpass, for cafes, parks, museums, food and beaches near the start,
  with opening hours, dog rules and vegetarian options when OpenStreetMap has them.
- elevations(): Open-Meteo elevation, so hills come from real terrain.
- conditions(): Open-Meteo forecast for that day: temperature, wind, fog (low visibility), sunset.

Only public place names and coordinates are sent. Results are cached in data/places-cache.json.
"""
import datetime
import json
import math
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "group-day-planner-demo"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(os.environ.get("PLANNER_CACHE_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"), "places-cache.json")
_lock = threading.Lock()
_venues_lock = threading.Lock()
_nominatim_last = [0.0]

OSM_TAGS = {
    "cafe": ['nwr["amenity"="cafe"]["name"]'],
    "park": ['nwr["leisure"="park"]["name"]', 'nwr["leisure"="garden"]["name"]["access"!="private"]'],
    "museum": ['nwr["tourism"="museum"]["name"]', 'nwr["tourism"="gallery"]["name"]'],
    "food": ['nwr["amenity"="restaurant"]["name"]', 'nwr["amenity"="fast_food"]["name"]',
             'nwr["amenity"="food_court"]["name"]', 'nwr["street_vendor"="yes"]["name"]'],
    "beach": ['nwr["natural"="beach"]["name"]'],
    # Waterfront: piers and promenades, plus parks and beaches whose name says water (see WATER_NAME).
    "waterfront": ['nwr["man_made"="pier"]["name"]', 'nwr["leisure"="promenade"]["name"]',
                   'nwr["highway"="pedestrian"]["name"~"Esplanade|Waterfront|Riverfront|Riverwalk|Promenade|Embarcadero"]'],
}
DEFAULT_HOURS = {"cafe": ("07:00", "18:00"), "park": ("05:00", "22:00"), "museum": ("10:00", "17:00"),
                 "food": ("11:00", "21:00"), "beach": ("00:00", "23:59"), "waterfront": ("05:00", "23:59")}
WATER_NAME = re.compile(r"(?i)\b(water ?front|river ?front|river ?side|river ?walk|river|esplanade|lake ?front|"
                        r"lake ?side|lake|harbou?r|marina|pier|promenade|boardwalk|bay ?front|shore|embarcadero|"
                        r"beach|crissy field)\b")


# ---------------------------------------------------------------- http + cache

def _http(url, data=None, timeout=40, extra_headers=None):
    headers = dict({"User-Agent": UA}, **(extra_headers or {}))
    try:
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.URLError as e:
        if "SSL" not in str(e) and "ssl" not in str(e):
            raise
        cmd = ["curl", "-s", "-f", "-m", str(timeout), "-H", "User-Agent: " + UA, url]
        if data is not None:
            cmd[1:1] = ["--data-binary", data.decode()]
        out = subprocess.run(cmd, capture_output=True, text=True)
        if out.returncode:
            raise RuntimeError("curl failed (%d)" % out.returncode)
        return json.loads(out.stdout)


def _cached(key, fn, ttl=30 * 24 * 3600):
    with _lock:
        try:
            with open(CACHE_FILE) as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
        hit = cache.get(key)
    if hit and time.time() - hit["at"] < ttl:
        return hit["value"]
    value = fn()
    if value is None:  # never cache "not found": a later spelling or retry may succeed
        return None
    with _lock:
        try:
            with open(CACHE_FILE) as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
        cache[key] = {"at": time.time(), "value": value}
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f)
    return value


# ---------------------------------------------------------------- geocoding

OVERPASS_CH = "https://api.overpass.ch/v1"  # hosted OpenStreetMap service (overpass.ch); needs OVERPASS_CH_KEY


def _ch_key():
    return os.environ.get("OVERPASS_CH_KEY", "").strip()


def _nominatim_get(url, tries=4):
    """Nominatim-style search. With OVERPASS_CH_KEY, overpass.ch's Nominatim-compatible endpoint
    first (300 requests/minute, key in a header, never in the URL); otherwise, or if it fails,
    public Nominatim, politely: one request per second, backing off on "429 Too many requests"."""
    key = _ch_key()
    if key:
        try:
            ch_url = url.replace("https://nominatim.openstreetmap.org/", OVERPASS_CH + "/nominatim/")
            return _http(ch_url, timeout=20, extra_headers={"X-API-Key": key})
        except Exception:
            pass  # fall back to public Nominatim below
    for attempt in range(tries):
        with _lock:
            wait = 1.1 - (time.time() - _nominatim_last[0])
            if wait > 0:
                time.sleep(wait)
            _nominatim_last[0] = time.time()
        try:
            return _http(url, timeout=30)
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == tries - 1:
                raise
            time.sleep(5 * (attempt + 1))


def geocode(query):
    """Return {"name", "lat", "lon", "display"} for a place, or None."""
    def fetch():
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"q": query, "format": "jsonv2", "limit": 1})
        res = _nominatim_get(url)
        if not res:
            return None
        r = res[0]
        return {"name": r.get("name") or query, "lat": float(r["lat"]), "lon": float(r["lon"]),
                "display": r.get("display_name", query)}
    return _cached("geo:" + query.lower(), fetch)


def spellings(place):
    """Variants OpenStreetMap is more likely to know: "St Stephen's" -> "Saint Stephen"."""
    out = [place]
    p = re.sub(r"(\w)'s\b", r"\1", place)  # possessive: Stephen's -> Stephen
    out.append(p)
    out.append(re.sub(r"\bSt\.?\s", "Saint ", p))
    out.append(re.sub(r"\bSt\.?\s", "St. ", p))
    # Possessive typed without the apostrophe: "St Stephens" -> "Saint Stephen".
    q = re.sub(r"\b(St\.?|Saint)\s+(\w{3,}?)s\b", r"Saint \2", p)
    if q != p:
        out.append(q)
    seen, uniq = set(), []
    for v in out:
        if v.lower() not in seen:
            seen.add(v.lower())
            uniq.append(v)
    return uniq


NEAR_CITY_KM = 30
GENERIC = {"the", "and", "of", "at", "saint", "st", "church", "catholic", "cathedral", "parish", "chapel",
           "park", "cafe", "coffee", "museum", "gallery", "restaurant", "bar", "station", "square", "center",
           "centre", "building", "plaza", "street", "avenue", "road", "blvd", "boulevard", "hall", "house"}


def _stem(w):
    w = w.lower().replace("’", "'")
    w = re.sub(r"'s$", "", w)
    return w[:-1] if len(w) > 4 and w.endswith("s") else w


def same_place(query, found_name):
    """Does a geocoder hit really match what was asked? Its name must contain the query's
    distinctive words ("St Stephens Catholic Church" is not "Saint Patrick Catholic Church").
    Street addresses ("4055 SE Salmon St") are not checked: they have no name to compare."""
    if re.match(r"^\s*\d", query):
        return True
    want = {_stem(w) for w in re.findall(r"[A-Za-z']{3,}", query)} - GENERIC
    have = {_stem(w) for w in re.findall(r"[A-Za-z']{3,}", found_name or "")}
    return not want or want <= have


def short_address(hit):
    """ "Saint Stephen Catholic Church, 1112, Southeast 41st Avenue, Hawthorne, ..." -> "1112 Southeast 41st Avenue"."""
    parts = [p.strip() for p in (hit.get("display") or "").split(",")]
    if len(parts) > 2 and parts[1].isdigit():
        return "%s %s" % (parts[1], parts[2])
    return parts[1] if len(parts) > 1 else ""


NEAR_WORDS = r"(?:at|on|near|by|off|next to)\s+(?:the\s+)?(?:corner\s+of\s+)?"
STREETISH = re.compile(r"(?i)\d|\b(st|street|ave|avenue|blvd|boulevard|rd|road|way|dr|drive|pl|place|ln|lane)\b|&|\band\b")


def split_place(place):
    """ "St Stephens Catholic Church at 41st and Salmon" -> ("St Stephens Catholic Church", "41st and Salmon").
    Returns (place, None) when there are no cross streets or address after the name."""
    m = re.match(r"^(.*?\S)\s+" + NEAR_WORDS + r"(.+)$", place.strip(), re.I)
    if m and STREETISH.search(m.group(2)):
        return m.group(1).strip(" ,"), m.group(2).strip(" ,.")
    return place, None


def _street_rx(token):
    """OSM regex for one street of an intersection: "41st" matches "Southeast 41st Avenue"."""
    t = re.sub(r"(?i)\b(st|street|ave|avenue|blvd|boulevard|rd|road)\.?$", "", token.strip()).strip()
    t = re.sub(r"(?i)^(se|sw|ne|nw|n|s|e|w|southeast|southwest|northeast|northwest)\s+", "", t)
    return "(^| )%s( |$)" % re.escape(t).replace("\\ ", " ") if t else None


def find_intersection(where, center, radius_m=20000):
    """Where two named streets cross ("41st and Salmon", "SE 41st Ave & Salmon St"), near center.
    Returns [{"lat", "lon", "name"}], nearest first."""
    parts = [p for p in re.split(r"(?i)\s+(?:and|&|at|/|x)\s+|\s*&\s*", where) if p.strip()]
    if len(parts) != 2:
        return []
    rx = [_street_rx(p) for p in parts]
    if not all(rx):
        return []
    q = ('[out:json][timeout:30];way[highway][name~"{a}",i](around:{r},{la:.5f},{lo:.5f})->.a;'
         'way[highway][name~"{b}",i](around:{r},{la:.5f},{lo:.5f})->.b;node(w.a)(w.b);out;').format(
        a=rx[0], b=rx[1], r=radius_m, la=center["lat"], lo=center["lon"])
    from .routes import km
    nodes = _cached("x:%s:%.3f,%.3f" % (where.lower(), center["lat"], center["lon"]), lambda: _overpass(q),
                    ttl=30 * 24 * 3600) or []
    hits = [{"lat": n["lat"], "lon": n["lon"], "name": "%s & %s" % (parts[0].strip(), parts[1].strip())}
            for n in nodes if "lat" in n]
    return sorted(hits, key=lambda h: km(h, center))


def find_start_near(place, near, city, center):
    """A named place given with its cross streets or address ("St Stephens Catholic Church" near
    "41st and Salmon"): the place by name if it is within 600 m of those streets, else the
    intersection or address itself (named after the place as typed)."""
    from .routes import km
    spots = find_intersection(near, center) if center else []
    if not spots:
        addr = geocode("%s, %s" % (near, city)) if city else geocode(near)
        spots = [dict(addr, name=near)] if addr and (not center or km(addr, center) <= NEAR_CITY_KM) else []
    named = None
    try:
        named = find_start(place, city, city_center_fallback=False)
    except Exception:
        named = None
    if named and spots:
        best = min(spots, key=lambda s: km(s, named))
        if km(best, named) <= 0.6:  # the cross streets confirm the place found by name
            return dict(named, near=near)
        return dict(best, name="%s (%s)" % (place, near), found_as=best["name"])
    if spots:
        return dict(spots[0], name="%s (%s)" % (place, near), found_as=spots[0]["name"])
    return named


def find_start(place, city, city_center_fallback=True):
    """Locate the starting place, trying several spellings. A match far from the named city
    (a geocoder can drop the city: "St Stephens Catholic Church, Portland" -> Skipton, England)
    is skipped while another spelling may land in the city; if none does, the far match is
    returned so the planner asks instead of planning there."""
    from .routes import km
    center = None
    if city:
        try:
            center = geocode(city)
        except Exception:
            center = None
    far = None
    if place:
        for v in spellings(place):
            hit = geocode("%s, %s" % (v, city) if city else v)
            if not hit or not same_place(v, hit.get("name")):
                continue  # nothing, or a different place with a similar name
            found = dict(hit, name=hit["name"] if hit.get("name") and not re.match(r"^\s*\d", v) else v,
                         found_as=hit["name"], address=short_address(hit))
            if center and km(hit, center) > NEAR_CITY_KM:
                far = far or found
                continue
            return found
        name, near = split_place(place)
        if near:  # "X at 41st and Salmon": find X, checked against (or replaced by) the cross streets
            hit = find_start_near(name, near, city, center)
            if hit and not (center and km(hit, center) > NEAR_CITY_KM):
                return hit
        if far:
            return far
    if city and city_center_fallback:
        hit = geocode(city)
        if hit:
            return dict(hit, name="%s (city center)" % city)
    return None


# ---------------------------------------------------------------- venues

def _saturday_hours(spec, day_index=5):
    """Opening hours for one day from an OSM opening_hours string, or None if unclear.
    Handles the common forms: "24/7", "Mo-Fr 07:00-18:00; Sa,Su 08:00-17:00"."""
    if not spec:
        return None
    if spec.strip() == "24/7":
        return ("00:00", "23:59")
    days = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
    result = None
    for rule in spec.split(";"):
        rule = rule.strip()
        m = re.match(r"^((?:[A-Z][a-z](?:-[A-Z][a-z])?,?)+)\s+(.*)$", rule)
        if m:
            covered = set()
            for part in m.group(1).split(","):
                if "-" in part:
                    a, b = part.split("-")
                    if a in days and b in days:
                        i, j = days.index(a), days.index(b)
                        covered |= set(range(i, j + 1)) if i <= j else set(range(i, 7)) | set(range(0, j + 1))
                elif part in days:
                    covered.add(days.index(part))
            times = m.group(2)
        else:
            covered, times = set(range(7)), rule  # "07:00-18:00" applies every day
        if day_index in covered:
            if "off" in times or "closed" in times:
                result = "closed"
                continue
            t = re.findall(r"(\d{1,2}:\d{2})-(\d{1,2}:\d{2})", times)
            if t:
                result = (t[0][0].zfill(5), t[-1][1].zfill(5) if t[-1][1] != "24:00" else "23:59")
    return result


OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter"]
_overpass_lock = threading.Lock()


def _overpass(query):
    """Run an Overpass query, with retries and a second public server. The free servers
    refuse bursts, so calls from this process go one at a time."""
    data = urllib.parse.urlencode({"data": query}).encode()
    last = None
    key = _ch_key()
    if key:  # overpass.ch answers in about a second; the public servers are the fallback
        try:
            return _http(OVERPASS_CH + "/interpreter", data=data, timeout=40,
                         extra_headers={"X-API-Key": key}).get("elements", [])
        except Exception as e:
            last = e
    with _overpass_lock:
        for attempt in range(4):
            url = OVERPASS[attempt % len(OVERPASS)]
            try:
                return _http(url, data=data, timeout=90).get("elements", [])
            except Exception as e:  # rate limit, reset connection, timeout: wait and try the next server
                last = e
                time.sleep(2 + 3 * attempt)
    raise RuntimeError("OpenStreetMap venue search failed: %s" % str(last)[:100])


def _all_elements(center, radius_m):
    """Every stop type near the start in ONE Overpass query (cached), tagged by category."""
    def fetch():
        parts = "".join("%s(around:%d,%.5f,%.5f);" % (t, radius_m, center["lat"], center["lon"])
                        for tags in OSM_TAGS.values() for t in tags)
        return _overpass("[out:json][timeout:60];(%s);out center tags 1500;" % parts)
    return _cached("osm-all2:%.4f,%.4f:%d" % (center["lat"], center["lon"], radius_m), fetch, ttl=7 * 24 * 3600)


def _categories_of(tags):
    """Every stop type a place can serve as: a riverside park is both a park and a waterfront."""
    cats = {_category_of(tags)} - {None}
    if (tags.get("man_made") == "pier" or tags.get("leisure") == "promenade" or tags.get("natural") == "beach"
            or tags.get("highway") == "pedestrian"
            or (tags.get("leisure") in ("park", "garden") and WATER_NAME.search(tags.get("name", "")))):
        cats.add("waterfront")
    return cats


def _category_of(tags):
    if tags.get("amenity") == "cafe":
        return "cafe"
    if tags.get("leisure") in ("park", "garden"):
        return "park"
    if tags.get("tourism") in ("museum", "gallery"):
        return "museum"
    if tags.get("amenity") in ("restaurant", "fast_food", "food_court") or tags.get("street_vendor") == "yes":
        return "food"
    if tags.get("natural") == "beach":
        return "beach"
    return None


NOMINATIM_WORDS = {"cafe": ["cafe", "coffee"], "park": ["park", "garden"], "museum": ["museum", "gallery"],
                   "food": ["restaurant", "fast food"], "beach": ["beach"],
                   "waterfront": ["waterfront park", "riverfront", "esplanade", "pier"]}
NOMINATIM_KINDS = {"cafe": {("amenity", "cafe")}, "park": {("leisure", "park"), ("leisure", "garden")},
                   "museum": {("tourism", "museum"), ("tourism", "gallery")},
                   "food": {("amenity", "restaurant"), ("amenity", "fast_food"), ("amenity", "food_court")},
                   "beach": {("natural", "beach")},
                   "waterfront": {("leisure", "park"), ("man_made", "pier"), ("natural", "beach"),
                                  ("leisure", "promenade"), ("highway", "pedestrian")}}

# Words that, in a name, show a cuisine even when the cuisine tag is missing.
CUISINE_NAME_WORDS = {"korean": ["korean", "kimchi", "bibimbap", "bulgogi", "seoul"],
                      "japanese": ["japanese", "sushi", "ramen", "izakaya", "teriyaki", "udon", "bento"],
                      "sushi": ["sushi"], "ramen": ["ramen"], "mexican": ["taqueria", "taco", "burrito", "mexican"],
                      "thai": ["thai"], "vietnamese": ["pho", "banh mi", "vietnamese"], "pizza": ["pizza"],
                      "indian": ["indian", "curry", "tandoor"], "chinese": ["chinese", "dim sum", "dumpling"]}


def cuisine_of(tags):
    """The cuisines a place serves: its OpenStreetMap cuisine tag, else words in its name."""
    tagged = {c.strip().lower() for c in (tags.get("cuisine") or "").split(";") if c.strip()}
    name = (tags.get("name") or "").lower()
    named = {c for c, words in CUISINE_NAME_WORDS.items() if any(w in name for w in words)}
    return tagged | named


def matches_cuisine(tags, wanted):
    """True if the place serves one of the wanted cuisines (sushi and ramen count as Japanese)."""
    have = cuisine_of(tags)
    if "sushi" in have or "ramen" in have:
        have |= {"japanese"}
    return bool(have & set(wanted))


def is_food_truck(tags):
    name = (tags.get("name") or "").lower()
    return (tags.get("street_vendor") == "yes" or tags.get("amenity") == "food_court"
            or any(w in name for w in ("truck", "cart", "food pod", "carts")))


def _nominatim_elements(category, center, radius_m, words=None):
    """Places of one category in a box around the start, from Nominatim (fast, includes
    opening hours). Returned in the same shape as Overpass elements."""
    d_lat = radius_m / 111000.0
    d_lon = d_lat / max(0.2, __import__("math").cos(__import__("math").radians(center["lat"])))
    box = "%f,%f,%f,%f" % (center["lon"] - d_lon, center["lat"] + d_lat, center["lon"] + d_lon, center["lat"] - d_lat)
    out = []
    for word in (words or NOMINATIM_WORDS[category]):
        def fetch(word=word):
            url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
                {"q": word, "format": "jsonv2", "limit": 40, "bounded": 1, "extratags": 1, "viewbox": box})
            return _nominatim_get(url)
        for r in _cached("nom:%s:%s" % (word, box), fetch, ttl=7 * 24 * 3600) or []:
            if (r.get("category"), r.get("type")) not in NOMINATIM_KINDS[category] or not r.get("name"):
                continue
            if category == "waterfront" and r.get("category") == "leisure" and not WATER_NAME.search(r["name"]):
                continue  # a park counts as waterfront only when its name says so
            tags = dict(r.get("extratags") or {}, name=r["name"])
            out.append({"type": r.get("osm_type", "node"), "id": r.get("osm_id"), "lat": float(r["lat"]),
                        "lon": float(r["lon"]), "tags": tags})
    return out


def find_venues(category, center, radius_m, limit=8, day_index=5, cuisines=None, food_truck=False):
    """Named places of one category within radius_m of center, nearest first.
    Nominatim first (fast); Overpass only if Nominatim finds nothing.

    For food, `cuisines` (e.g. ["korean", "japanese"]) keeps only places whose OpenStreetMap
    cuisine tag or name matches: never a stand-in from another cuisine. `food_truck` puts
    food trucks and carts first."""
    from .routes import km

    words = None
    if category == "food" and (cuisines or food_truck):
        words = ["%s restaurant" % c for c in cuisines or []] + list(cuisines or [])
        for c in cuisines or []:  # dish and style words find places whose cuisine tag is missing
            words += [w for w in CUISINE_NAME_WORDS.get(c, []) if w not in words]
        if food_truck:
            words += ["food truck", "food cart"] + ["%s food truck" % c for c in cuisines or []]
    elements = []
    if _ch_key():  # one fast Overpass query with full tags (cuisine included) for every stop type
        try:
            with _venues_lock:  # the search branches run in parallel; fetch the shared query once
                elements = [e for e in _all_elements(center, radius_m) if category in _categories_of(e.get("tags", {}))]
        except Exception:
            elements = []
    if not elements:
        elements = [e for e in _nominatim_elements(category, center, radius_m, words)
                    if km(e, center) * 1000 <= radius_m * 1.05]
    if not elements and not _ch_key():
        with _venues_lock:
            elements = [e for e in _all_elements(center, radius_m) if category in _categories_of(e.get("tags", {}))]
    if category == "food" and cuisines:
        elements = [e for e in elements if matches_cuisine(e.get("tags", {}), cuisines)]
    venues, seen = [], set()
    for e in elements:
        tags = e.get("tags", {})
        name = tags.get("name")
        lat = e.get("lat") or (e.get("center") or {}).get("lat")
        lon = e.get("lon") or (e.get("center") or {}).get("lon")
        if not name or lat is None or name in seen:
            continue
        seen.add(name)
        hours = _saturday_hours(tags.get("opening_hours"), day_index)
        if hours == "closed":
            continue
        open_, close = hours or DEFAULT_HOURS[category]
        dog = tags.get("dog")
        dogs = {"yes": "patio" if category in ("cafe", "food") else "leash", "leashed": "leash",
                "no": "no", "unleashed": "offleash"}.get(dog, "leash" if category in ("park", "beach", "waterfront") else "patio")
        v = {"name": name, "lat": round(float(lat), 6), "lon": round(float(lon), 6), "open": open_, "close": close,
             "hours_known": hours is not None, "dogs": dogs, "dogs_known": dog is not None,
             "vegetarian_ok": tags.get("diet:vegetarian", "yes") != "no",
             # gluten-free: True if tagged yes/only, False if tagged no, None when OSM doesn't say
             "gluten_free_ok": {"yes": True, "only": True, "limited": True, "no": False}.get(tags.get("diet:gluten_free")),
             "tags": [t for t in (tags.get("cuisine", "").split(";") + [category]) if t],
             "cuisine": sorted(cuisine_of(tags)), "food_truck": is_food_truck(tags),
             "source": "https://www.openstreetmap.org/%s/%s" % (e["type"], e["id"])}
        if category == "museum":
            v["kind"] = "museum"
        if category in ("beach", "waterfront"):
            v["area"] = "all"
        venues.append(v)
    # Nearest first; when food trucks were asked for, trucks and carts come before restaurants.
    venues.sort(key=lambda v: (not (food_truck and v.get("food_truck")), km(v, center)))
    return venues[:limit]


# ---------------------------------------------------------------- terrain and weather

def elevations(points):
    """Elevation in meters for each point (same order). Open-Meteo takes up to 100 at a time."""
    out = []
    for i in range(0, len(points), 100):
        chunk = points[i:i + 100]
        key = "elev:" + "|".join("%.4f,%.4f" % (p["lat"], p["lon"]) for p in chunk)

        def fetch(chunk=chunk):
            url = "https://api.open-meteo.com/v1/elevation?" + urllib.parse.urlencode({
                "latitude": ",".join("%.5f" % p["lat"] for p in chunk),
                "longitude": ",".join("%.5f" % p["lon"] for p in chunk)})
            return _http(url)["elevation"]
        out += _cached(key, fetch, ttl=365 * 24 * 3600)
    return out


def next_date(day_name, today=None):
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    today = today or datetime.date.today()
    ahead = (days.index(day_name) - today.weekday()) % 7
    return today + datetime.timedelta(days=ahead)


def conditions(center, day_name):
    """Hourly weather and sunset for the requested day, shaped like conditions.json."""
    date = next_date(day_name).isoformat()

    def fetch():
        url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
            "latitude": "%.4f" % center["lat"], "longitude": "%.4f" % center["lon"],
            "hourly": "temperature_2m,wind_speed_10m,visibility", "daily": "sunset",
            "timezone": "auto", "start_date": date, "end_date": date})
        return _http(url)

    d = _cached("wx:%.2f,%.2f:%s" % (center["lat"], center["lon"], date), fetch, ttl=3 * 3600)
    hourly = {}
    h = d["hourly"]
    for i, t in enumerate(h["time"]):
        hour = int(t[11:13])
        vis = h["visibility"][i]
        hourly[str(hour)] = {"all": {"temp_c": round(h["temperature_2m"][i] or 0), "wind_kmh": round(h["wind_speed_10m"][i] or 0),
                                     "fog": vis is not None and vis < 1000}}
    return {"day": day_name, "date": date, "sunset": d["daily"]["sunset"][0][11:16], "hourly": hourly,
            "source": "Open-Meteo forecast"}


# ---------------------------------------------------------------- strolls

STROLL_KM = 1.2  # how much of the street the stroll covers


def find_stroll(street, center, radius_m=5000):
    """A stroll along a named street ("Hawthorne"): the liveliest ~1.2 km stretch near center,
    where the most shops, cafes and restaurants line the street (OpenStreetMap). Returns a venue
    that starts at one end and finishes at the other, or None."""
    from .routes import km
    rx = _street_rx(street)
    if not rx:
        return None
    q = ('[out:json][timeout:40];way[highway][name~"{rx}",i](around:{r},{la:.5f},{lo:.5f})->.st;'
         '.st out geom;node(around.st:35)[~"^(shop|amenity)$"~"."];out;').format(
        rx=rx, r=radius_m, la=center["lat"], lo=center["lon"])
    elements = _cached("stroll:%s:%.3f,%.3f:%d" % (street.lower(), center["lat"], center["lon"], radius_m),
                       lambda: _overpass(q), ttl=7 * 24 * 3600) or []
    ways = [e for e in elements if e["type"] == "way" and e.get("geometry")]
    pois = [e for e in elements if e["type"] == "node" and "lat" in e]
    if not ways:
        return None
    # The street itself, not "Hawthorne Bridge": among the streets of that name, the one with
    # the most shops and cafes along it (bridges, ramps and trails never qualify).
    names = {}
    for w in ways:
        n = (w.get("tags") or {}).get("name", street)
        if re.search(r"(?i)\b(bridge|viaduct|ramp|tunnel|trail|path|exit|onramp|offramp)\b", n):
            continue
        names.setdefault(n, []).extend((g["lat"], g["lon"]) for g in w["geometry"])
    if not names:
        return None

    def shops_along(pts):
        grid = {(round(la, 3), round(lo, 3)) for la, lo in pts}
        return sum(1 for p in pois if (round(p["lat"], 3), round(p["lon"], 3)) in grid)
    name = max(names, key=lambda n: (shops_along(names[n]), len(names[n])))
    pts = names[name]
    # Position along the street: project on its main direction (streets are mostly straight lines).
    (la1, lo1), (la2, lo2) = max(((a, b) for a in pts[:: max(1, len(pts) // 40)] for b in pts[:: max(1, len(pts) // 40)]),
                                 key=lambda ab: km({"lat": ab[0][0], "lon": ab[0][1]}, {"lat": ab[1][0], "lon": ab[1][1]}))
    cos = math.cos(math.radians(la1))
    dx, dy = (lo2 - lo1) * cos, la2 - la1
    norm = math.hypot(dx, dy) or 1.0
    along = lambda la, lo: ((lo - lo1) * cos * dx + (la - la1) * dy) / norm * 111.0  # km from one end
    length = along(la2, lo2)
    if length < 0.3:
        return None
    win = min(STROLL_KM, length)
    marks = sorted(along(p["lat"], p["lon"]) for p in pois)
    best, best_n = 0.0, -1
    step = 0.1
    x = 0.0
    while x + win <= length + 1e-9:
        n = sum(1 for m in marks if x <= m <= x + win)
        # ties: the stretch nearer the start
        mid = x + win / 2
        if n > best_n or (n == best_n and abs(mid - along(center["lat"], center["lon"])) <
                          abs(best + win / 2 - along(center["lat"], center["lon"]))):
            best, best_n = x, n
        x += step
    seg = sorted((p for p in pts if best - 0.02 <= along(*p) <= best + win + 0.02), key=lambda p: along(*p))
    if len(seg) < 2:
        return None
    a, b = {"lat": seg[0][0], "lon": seg[0][1]}, {"lat": seg[-1][0], "lon": seg[-1][1]}
    if km(b, center) < km(a, center):  # start at the end nearer the starting point
        a, b, seg = b, a, seg[::-1]
    return {"name": "Stroll along %s" % name, "lat": round(a["lat"], 6), "lon": round(a["lon"], 6),
            "end_lat": round(b["lat"], 6), "end_lon": round(b["lon"], 6), "stroll_km": round(win, 2),
            "shops": max(best_n, 0), "path": [[round(p[0], 6), round(p[1], 6)] for p in seg[:: max(1, len(seg) // 60)]],
            "open": "00:00", "close": "23:59", "dogs": "leash", "dogs_known": False, "tags": ["stroll", "street"],
            "source": "https://www.openstreetmap.org/search?query=%s" % urllib.parse.quote(name)}
