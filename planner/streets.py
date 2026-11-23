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


def _post_json(url, body, timeout=30):
    """POST JSON, read JSON. Like `_get`, falls back to curl when Python's TLS is refused (macOS's
    built-in Python 3.9 has an old TLS library some servers turn away)."""
    data = json.dumps(body).encode()
    try:
        req = urllib.request.Request(url, data=data, headers=dict(HEADERS, **{"Content-Type": "application/json"}))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.URLError as e:
        if "SSL" not in str(e) and "ssl" not in str(e):
            raise
        out = subprocess.run(["curl", "-s", "-f", "-m", str(timeout), "-H", "User-Agent: " + HEADERS["User-Agent"],
                              "-H", "Content-Type: application/json", "--data-binary", "@-", url],
                             input=data.decode(), capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError("curl failed (%d)" % out.returncode)
        return json.loads(out.stdout)


def _valhalla_paths(points, mode, timeout=30, alternates=0):
    """Valhalla routes through `points` in order: the best one first, then up to `alternates`
    others (only between two points)."""
    body = {"locations": [{"lat": p["lat"], "lon": p["lon"], "type": "break" if i in (0, len(points) - 1) else "through"}
                          for i, p in enumerate(points)],
            "costing": "pedestrian" if mode == "walk" else "auto"}
    if alternates and len(points) == 2:
        body["alternates"] = alternates
    d = _post_json(VALHALLA, body, timeout)
    out = []
    for trip in [d["trip"]] + [x["trip"] for x in d.get("alternates") or []]:
        pts = []
        for leg in trip["legs"]:
            pts += _decode_polyline6(leg["shape"])
        if len(pts) >= 2:
            out.append(pts)
    if not out:
        raise RuntimeError("Valhalla returned no path")
    return out


def _valhalla_path(a, b, mode, timeout=30):
    return _valhalla_paths([a, b], mode, timeout)[0]


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


# ---------------------------------------------------------------- not walking the same street twice

CELL_LAT, CELL_LON = 0.00011, 0.00015  # about 12 m: well under a block (Portland's are 61 m)
SAMPLE_M = 10  # a path is checked every 10 m
END_SKIP_M = 60  # leaving or reaching a stop may have to use its one street; that doesn't count
MAX_EXTRA_KM = 0.45  # a fresh street may make a walk up to 450 m longer: see more of the city
WORTH_M = 50  # switch only to save at least 50 m of walking a street again
REPEAT_W = 10  # a meter walked again costs like 10 more meters of walking
BUSY_W = 10  # so does a meter beside a busy street (weighted by how busy: planner/traffic.py)
BUSY_MAX_M = 100  # walk at most 100 m along busy streets when any route manages it...
MAX_BUSY_EXTRA_KM = 0.8  # ...even if that route is up to 800 m longer (within the walking limit)


def _m(a, b):
    import math
    dy = (b[0] - a[0]) * 111320
    dx = (b[1] - a[1]) * 111320 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def path_km(pts):
    return sum(_m(a, b) for a, b in zip(pts, pts[1:])) / 1000.0


def _samples(pts):
    """Points every SAMPLE_M along the path, each with its distance from the start."""
    out, done = [], 0.0
    for a, b in zip(pts, pts[1:]):
        d = _m(a, b)
        n = max(1, int(d // SAMPLE_M))
        for i in range(n):
            t = i / n
            out.append(((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t), done + d * t))
        done += d
    if pts:
        out.append((tuple(pts[-1]), done))
    return out


def _cell(p):
    return (round(p[0] / CELL_LAT), round(p[1] / CELL_LON))


def walked_cells(pts):
    return {_cell(p) for p, _ in _samples(pts)}


def overlap_m(pts, walked):
    """Meters of this path on streets already walked (its first and last END_SKIP_M aside)."""
    smp = _samples(pts)
    total = smp[-1][1] if smp else 0
    hit = 0
    for p, at in smp:
        if END_SKIP_M <= at <= total - END_SKIP_M:
            cy, cx = _cell(p)
            if any((cy + dy, cx + dx) in walked for dy in (-1, 0, 1) for dx in (-1, 0, 1)):
                hit += 1
    return hit * SAMPLE_M


def choose_path(options, walked, max_extra_km=None, busy=None):
    """The best path among the options: the shortest unless another one walks fewer streets
    again or fewer busy streets.

    `busy(path)` gives {"busy_m", "cost_m"} (planner/traffic.py profile). A path with at most
    BUSY_MAX_M on busy streets is preferred when one exists up to MAX_BUSY_EXTRA_KM longer;
    among the rest (up to MAX_EXTRA_KM longer), the lowest cost wins, where cost is meters walked
    plus REPEAT_W per meter walked again and BUSY_W per busy meter. Never more than
    `max_extra_km` longer (what the walking limit leaves), and a switch must save WORTH_M."""
    options = [o for o in options if o and len(o) >= 2]
    if not options:
        return None
    best = min(options, key=path_km)
    if not walked and not busy:
        return best
    seen = {}

    def prof(o):
        if id(o) not in seen:
            seen[id(o)] = busy(o) if busy else {"busy_m": 0, "cost_m": 0.0}
        return seen[id(o)]

    def cost(o):
        return path_km(o) * 1000 + REPEAT_W * (overlap_m(o, walked) if walked else 0) + BUSY_W * prof(o)["cost_m"]

    room = float("inf") if max_extra_km is None else max(0.0, max_extra_km)  # what the walking limit leaves
    within = lambda cap: [o for o in options if path_km(o) <= path_km(best) + min(room, cap) + 1e-9]
    ok = within(MAX_EXTRA_KM)
    calm = [o for o in within(MAX_BUSY_EXTRA_KM) if prof(o)["busy_m"] <= BUSY_MAX_M]
    if prof(best)["busy_m"] > BUSY_MAX_M and calm:  # off the busy street, even if it's a longer way
        return min(calm, key=cost)
    pool = [o for o in ok if prof(o)["busy_m"] <= max(BUSY_MAX_M, prof(best)["busy_m"])]
    pick = min(pool, key=cost)
    return pick if cost(best) - cost(pick) >= WORTH_M else best


def _vias(a, b):
    """Points that make a walk a-b take other streets: the two other corners of the box between
    a and b (in a street grid, the "other way around the block", often no longer), then points
    one and two blocks to each side of the middle (a parallel street)."""
    import math
    mid = ((a["lat"] + b["lat"]) / 2, (a["lon"] + b["lon"]) / 2)
    k = math.cos(math.radians(mid[0]))
    dy, dx = (b["lat"] - a["lat"]) * 111320, (b["lon"] - a["lon"]) * 111320 * k
    n = math.hypot(dx, dy) or 1
    out = []
    if min(abs(dx), abs(dy)) > 40:  # a diagonal leg: both corners
        out += [{"lat": a["lat"], "lon": b["lon"]}, {"lat": b["lat"], "lon": a["lon"]}]
    for side in (1, -1):
        for meters in (120, 250):
            out.append({"lat": mid[0] + side * meters * (dx / n) / 111320,
                        "lon": mid[1] - side * meters * (dy / n) / (111320 * k)})
    return out


def walk_options(a, b, timeout=12, detours=True):
    """Street paths for walking a-b: the router's alternatives and, if asked, routes through a
    parallel street to either side. Cached like `path`."""
    ck = "walkopts:%s:%s:%d" % (key(a), key(b), detours)
    with _lock:
        cache = _load()
        if ck in cache:
            return cache[ck]
    pts = lambda r: [[round(lat, 5), round(lon, 5)] for lon, lat in r["geometry"]["coordinates"]]
    url = (BASE % "foot") + "/route/v1/driving/%.5f,%.5f;%.5f,%.5f?overview=full&geometries=geojson&alternatives=3"
    try:
        opts = [pts(r) for r in _get(url % (a["lon"], a["lat"], b["lon"], b["lat"]), timeout)["routes"]]

        def through(v):
            u = (BASE % "foot") + "/route/v1/driving/%.5f,%.5f;%.5f,%.5f;%.5f,%.5f?overview=full&geometries=geojson" % (
                a["lon"], a["lat"], v["lon"], v["lat"], b["lon"], b["lat"])
            return pts(_get(u, timeout)["routes"][0])
    except Exception:  # OSRM busy or down: Valhalla, the second OpenStreetMap router, the same way
        opts = _valhalla_paths([a, b], "walk", timeout, alternates=2)

        def through(v):
            return _valhalla_paths([a, v, b], "walk", timeout)[0]
    if detours:
        for v in _vias(a, b):
            try:
                opts.append(through(v))
            except Exception:
                pass  # that detour isn't possible; the others still are
    with _lock:
        _cache[ck] = opts
        _save()
    return opts


def _legs(p, start):
    """(leg, from, to, stop) for each leg of a plan, the walk back included."""
    out, here = [], start
    for leg, stop in zip(p["legs"], p["stops"]):
        out.append((leg, here, stop, stop))
        # after a stroll, the next leg starts where the stroll ends
        here = {"lat": stop["end_lat"], "lon": stop["end_lon"]} if stop.get("end_lat") else stop
    if p.get("return_leg"):  # the walk back to the start
        out.append((p["return_leg"], here, start, None))
    return out


def add_paths(plans, start, max_walk_km=None, roads=None):
    """Attach a street path to every leg of every plan (in place); plans are fetched in parallel,
    the legs of one plan in order. A walk avoids streets the plan already walked when a
    parallel street costs at most MAX_EXTRA_KM more (choose_path), and never past
    `max_walk_km` for the whole plan. Walks keep off busy streets the same way (`roads`:
    planner/traffic.py; looked up around the plans when not given). If the router refuses or
    times out, stop asking: the map draws straight lines instead, so an outage costs seconds."""
    from concurrent.futures import ThreadPoolExecutor
    from . import traffic
    down = threading.Event()  # both routers keep failing: stop asking
    failures = []
    if roads is None and traffic.model().get("class") and plans:
        try:
            roads = traffic.roads_around([start] + [s for p in plans for s in p["stops"]])
        except Exception as e:  # no street data: plan as before, without the busy-street check
            print("Busy-street check off: %r" % e)
    busy = (lambda o: traffic.profile(o, roads)) if roads else None

    def fetch(p):
        walked = set()
        spare = [max_walk_km - p["walk_km"] if max_walk_km else MAX_EXTRA_KM * 10]  # km of detours allowed
        for leg, a, b, stop in _legs(p, start):
            if down.is_set():
                leg["path_error"] = "router unavailable"
                continue
            try:
                if leg["mode"] == "walk":
                    opts = walk_options(a, b, timeout=12, detours=False)
                    pick = choose_path(opts, walked, spare[0], busy)
                    if ((walked and overlap_m(pick, walked) >= WORTH_M)  # still repeating, or on a busy
                            or (busy and busy(pick)["busy_m"] > BUSY_MAX_M)):  # street: try side streets
                        pick = choose_path(walk_options(a, b, timeout=12), walked, spare[0], busy)
                    short = min(opts, key=path_km)
                    spare[0] -= path_km(pick) - path_km(short)
                    if path_km(pick) - path_km(short) > 0.02:  # the times are redone with it (planner.apply_detours)
                        leg["detour_km"] = round(path_km(pick) - path_km(short), 2)
                        leg["short_path"] = short
                    leg["path"] = pick
                    if busy:  # what's left on busy streets, for the plan's note and score
                        prof = busy(pick)
                        leg["busy_m"] = prof["busy_m"]
                        leg["busy_streets"] = {k: list(v) for k, v in prof["streets"].items()}
                    walked |= walked_cells(pick)
                else:
                    leg["path"] = path(a, b, leg["mode"], timeout=12)
            except Exception as e:  # one more try, the plain way (one path, either router)
                try:
                    leg["path"] = path(a, b, leg["mode"], timeout=12)
                except Exception:  # keep the plan; the map draws a straight line for this leg
                    leg["path_error"] = str(e)[:100]
                    failures.append(e)
                    if len(failures) >= 3:
                        down.set()
            if stop and stop.get("path"):  # a stroll walks its street too
                walked |= walked_cells(stop["path"])

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(fetch, plans))
