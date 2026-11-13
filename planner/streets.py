"""Street routing with OSRM (OpenStreetMap data) on the public FOSSGIS servers.

- `build_matrix(points)`: one walking table and one driving table for every pair of places,
  so route simulation (local or in 35 sandboxes) never calls the router itself.
- `paths_for(plan)`: street-following paths for each leg of the chosen plans, for the map.

Results are cached on disk (data/routing-cache.json) to keep load on the free servers low.
Set PLANNER_ROUTING=straight to skip the network (tests do this); then routes.py falls back
to straight-line distance x 1.3.
"""
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request

BASE = "https://routing.openstreetmap.de/routed-%s"
HEADERS = {"User-Agent": "group-day-planner-demo"}
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(os.environ.get("PLANNER_CACHE_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"), "routing-cache.json")
_lock = threading.Lock()
_cache = None


def enabled():
    return os.environ.get("PLANNER_ROUTING", "osrm") != "straight"


def key(p):
    # Rounded to ~100 m so a pin nudged by a few meters still finds its distances.
    return "%.3f,%.3f" % (p["lat"], p["lon"])


def _get(url, timeout=30):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.URLError as e:
        # macOS's built-in Python 3.9 ships an old TLS library that some servers refuse;
        # curl uses the system's TLS and works, so fall back to it.
        if "SSL" not in str(e) and "ssl" not in str(e):
            raise
        out = subprocess.run(["curl", "-s", "-f", "-m", str(timeout), "-H", "User-Agent: " + HEADERS["User-Agent"], url],
                             capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError("curl failed (%d)" % out.returncode)
        data = json.loads(out.stdout)
    if data.get("code") != "Ok":
        raise RuntimeError("OSRM: %s" % data.get("message", data.get("code")))
    return data


def _load():
    global _cache
    if _cache is None:
        try:
            with open(CACHE_FILE) as f:
                _cache = json.load(f)
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save():
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump(_cache, f)
    except OSError:
        pass


def build_matrix(points):
    """points: dicts with lat/lon. Returns {"walk_km": {a: {b: km}}, "car_min": {a: {b: min}}}."""
    uniq = {}
    for p in points:
        uniq[key(p)] = p
    keys = sorted(uniq)
    with _lock:
        cache = _load()
        ck = "matrix:" + "|".join(keys)
        if ck in cache:
            return cache[ck]
    coords = ";".join("%.5f,%.5f" % (uniq[k]["lon"], uniq[k]["lat"]) for k in keys)
    # Walking and driving tables in parallel: each takes several seconds on the public servers.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_foot = pool.submit(_get, (BASE % "foot") + "/table/v1/driving/" + coords + "?annotations=distance")
        f_car = pool.submit(_get, (BASE % "car") + "/table/v1/driving/" + coords + "?annotations=duration")
        foot, car = f_foot.result(), f_car.result()
    m = {"walk_km": {}, "car_min": {}, "source": "OSRM walking and driving tables (OpenStreetMap)"}
    for i, a in enumerate(keys):
        m["walk_km"][a] = {b: round((foot["distances"][i][j] or 0) / 1000.0, 3) for j, b in enumerate(keys)}
        m["car_min"][a] = {b: round((car["durations"][i][j] or 0) / 60.0, 1) for j, b in enumerate(keys)}
    with _lock:
        _cache[ck] = m
        _save()
    return m


VALHALLA = "https://valhalla1.openstreetmap.de/route"


def _decode_polyline6(s):
    """Valhalla's encoded shape (polyline, 6 decimals) -> [[lat, lon], ...]."""
    pts, i, lat, lon = [], 0, 0, 0
    while i < len(s):
        vals = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                result |= (b & 0x1f) << shift
                shift += 5
                if b < 0x20:
                    break
            vals.append(~(result >> 1) if result & 1 else result >> 1)
        lat += vals[0]
        lon += vals[1]
        pts.append([round(lat / 1e6, 5), round(lon / 1e6, 5)])
    return pts


def _valhalla_path(a, b, mode, timeout=30):
    body = json.dumps({"locations": [{"lat": a["lat"], "lon": a["lon"]}, {"lat": b["lat"], "lon": b["lon"]}],
                       "costing": "pedestrian" if mode == "walk" else "auto"}).encode()
    req = urllib.request.Request(VALHALLA, data=body, headers=dict(HEADERS, **{"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        trip = json.load(r)["trip"]
    pts = []
    for leg in trip["legs"]:
        pts += _decode_polyline6(leg["shape"])
    if len(pts) < 2:
        raise RuntimeError("Valhalla returned no path")
    return pts


def path(a, b, mode, timeout=30):
    """Street path from a to b as [[lat, lon], ...]; mode 'walk' (foot) or 'ride' (car)."""
    profile = "foot" if mode == "walk" else "car"
    ck = "path:%s:%s:%s" % (profile, key(a), key(b))
    with _lock:
        cache = _load()
        if ck in cache:
            return cache[ck]
    url = (BASE % profile) + "/route/v1/driving/%.5f,%.5f;%.5f,%.5f?overview=full&geometries=geojson" % (
        a["lon"], a["lat"], b["lon"], b["lat"])
    try:
        coords = _get(url, timeout=timeout)["routes"][0]["geometry"]["coordinates"]
        pts = [[round(lat, 5), round(lon, 5)] for lon, lat in coords]
    except Exception:  # OSRM busy or refusing: ask Valhalla, a second OpenStreetMap router
        pts = _valhalla_path(a, b, mode, timeout)
    with _lock:
        _cache[ck] = pts
        _save()
    return pts


def add_paths(plans, start):
    """Attach a street path to every leg of every plan (in place), fetching legs in parallel.
    If the router refuses or times out, stop asking: the map draws straight lines instead,
    so an outage costs seconds, not minutes."""
    from concurrent.futures import ThreadPoolExecutor
    jobs = []
    for p in plans:
        here = start
        for leg, stop in zip(p["legs"], p["stops"]):
            jobs.append((leg, here, stop))
            # after a stroll, the next leg starts where the stroll ends
            here = {"lat": stop["end_lat"], "lon": stop["end_lon"]} if stop.get("end_lat") else stop
        if p.get("return_leg"):  # the walk back to the start
            jobs.append((p["return_leg"], here, start))
    down = threading.Event()

    def fetch(job):
        leg, a, b = job
        if down.is_set():
            leg["path_error"] = "router unavailable"
            return
        try:
            leg["path"] = path(a, b, leg["mode"], timeout=12)
        except Exception as e:  # keep the plan; the map draws a straight line instead
            leg["path_error"] = str(e)[:100]
            down.set()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(fetch, jobs))
