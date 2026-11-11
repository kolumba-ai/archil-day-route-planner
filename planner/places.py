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
CACHE_FILE = os.path.join(os.path.dirname(HERE), "data", "places-cache.json")
_lock = threading.Lock()
_venues_lock = threading.Lock()
_nominatim_last = [0.0]

OSM_TAGS = {
    "cafe": ['nwr["amenity"="cafe"]["name"]'],
    "park": ['nwr["leisure"="park"]["name"]', 'nwr["leisure"="garden"]["name"]["access"!="private"]'],
    "museum": ['nwr["tourism"="museum"]["name"]', 'nwr["tourism"="gallery"]["name"]'],
    "food": ['nwr["amenity"="restaurant"]["name"]', 'nwr["amenity"="fast_food"]["name"]'],
    "beach": ['nwr["natural"="beach"]["name"]'],
}
DEFAULT_HOURS = {"cafe": ("07:00", "18:00"), "park": ("05:00", "22:00"), "museum": ("10:00", "17:00"),
                 "food": ("11:00", "21:00"), "beach": ("00:00", "23:59")}


# ---------------------------------------------------------------- http + cache

def _http(url, data=None, timeout=40):
    headers = {"User-Agent": UA}
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

def geocode(query):
    """Return {"name", "lat", "lon", "display"} for a place, or None."""
    def fetch():
        with _lock:  # Nominatim's usage policy: at most one request per second
            wait = 1.1 - (time.time() - _nominatim_last[0])
            if wait > 0:
                time.sleep(wait)
            _nominatim_last[0] = time.time()
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"q": query, "format": "jsonv2", "limit": 1})
        res = _http(url)
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
    seen, uniq = set(), []
    for v in out:
        if v.lower() not in seen:
            seen.add(v.lower())
            uniq.append(v)
    return uniq


def find_start(place, city):
    """Locate the starting place (trying several spellings), else the city center."""
    if place:
        for v in spellings(place):
            hit = geocode("%s, %s" % (v, city) if city else v)
            if hit:
                return dict(hit, name=v if v == place else hit["name"], found_as=hit["name"])
    if city:
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
    return _cached("osm-all:%.4f,%.4f:%d" % (center["lat"], center["lon"], radius_m), fetch, ttl=7 * 24 * 3600)


def _category_of(tags):
    if tags.get("amenity") == "cafe":
        return "cafe"
    if tags.get("leisure") in ("park", "garden"):
        return "park"
    if tags.get("tourism") in ("museum", "gallery"):
        return "museum"
    if tags.get("amenity") in ("restaurant", "fast_food"):
        return "food"
    if tags.get("natural") == "beach":
        return "beach"
    return None


NOMINATIM_WORDS = {"cafe": ["cafe", "coffee"], "park": ["park", "garden"], "museum": ["museum", "gallery"],
                   "food": ["restaurant", "fast food"], "beach": ["beach"]}
NOMINATIM_KINDS = {"cafe": {("amenity", "cafe")}, "park": {("leisure", "park"), ("leisure", "garden")},
                   "museum": {("tourism", "museum"), ("tourism", "gallery")},
                   "food": {("amenity", "restaurant"), ("amenity", "fast_food")}, "beach": {("natural", "beach")}}


def _nominatim_elements(category, center, radius_m):
    """Places of one category in a box around the start, from Nominatim (fast, includes
    opening hours). Returned in the same shape as Overpass elements."""
    d_lat = radius_m / 111000.0
    d_lon = d_lat / max(0.2, __import__("math").cos(__import__("math").radians(center["lat"])))
    box = "%f,%f,%f,%f" % (center["lon"] - d_lon, center["lat"] + d_lat, center["lon"] + d_lon, center["lat"] - d_lat)
    out = []
    for word in NOMINATIM_WORDS[category]:
        def fetch(word=word):
            with _lock:  # Nominatim's usage policy: at most one request per second
                wait = 1.1 - (time.time() - _nominatim_last[0])
                if wait > 0:
                    time.sleep(wait)
                _nominatim_last[0] = time.time()
            url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
                {"q": word, "format": "jsonv2", "limit": 40, "bounded": 1, "extratags": 1, "viewbox": box})
            return _http(url, timeout=30)
        for r in _cached("nom:%s:%s" % (word, box), fetch, ttl=7 * 24 * 3600) or []:
            if (r.get("category"), r.get("type")) not in NOMINATIM_KINDS[category] or not r.get("name"):
                continue
            tags = dict(r.get("extratags") or {}, name=r["name"])
            out.append({"type": r.get("osm_type", "node"), "id": r.get("osm_id"), "lat": float(r["lat"]),
                        "lon": float(r["lon"]), "tags": tags})
    return out


def find_venues(category, center, radius_m, limit=8, day_index=5):
    """Named places of one category within radius_m of center, nearest first.
    Nominatim first (fast); Overpass only if Nominatim finds nothing."""
    from .routes import km

    elements = [e for e in _nominatim_elements(category, center, radius_m)
                if km(e, center) * 1000 <= radius_m * 1.05]
    if not elements:
        with _venues_lock:  # the search branches run in parallel; fetch the shared query once
            elements = [e for e in _all_elements(center, radius_m) if _category_of(e.get("tags", {})) == category]
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
                "no": "no", "unleashed": "offleash"}.get(dog, "leash" if category in ("park", "beach") else "patio")
        v = {"name": name, "lat": round(float(lat), 6), "lon": round(float(lon), 6), "open": open_, "close": close,
             "hours_known": hours is not None, "dogs": dogs, "dogs_known": dog is not None,
             "vegetarian_ok": tags.get("diet:vegetarian", "yes") != "no",
             "tags": [t for t in (tags.get("cuisine", "").split(";") + [category]) if t],
             "source": "https://www.openstreetmap.org/%s/%s" % (e["type"], e["id"])}
        if category == "museum":
            v["kind"] = "museum"
        if category == "beach":
            v["area"] = "all"
        venues.append(v)
    venues.sort(key=lambda v: km(v, center))
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
