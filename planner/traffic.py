"""Busy streets: how many vehicles a day a street carries, and how unpleasant that is to walk along.

1. Traffic volume. Few cities publish counts, but OpenStreetMap describes every street: its class
   (primary, secondary, residential...), lanes, speed limit, one-way, route number. Those tags
   predict daily traffic well (R^2 around 0.75 in published studies), so a street's volume is
   estimated from them with a log-linear model:

       ln(vehicles a day) = class + a * ln(lanes) + b * speed (km/h) + c * one-way + d * has route number

   The coefficients are not written here. `fit()` learns them from a city's measured counts
   (Portland's PBOT publishes about 25,000) matched to the street under each counter, and saves
   them to planner/traffic_model.json (scripts/fit_traffic.py). The planner only reads that file.

   A log-linear fit pulls predictions toward the middle (busy streets come out too low), so the
   fit also saves a calibration: predictions are mapped onto measured volumes by percentile.
   Where a city publishes counts (planner/traffic_counts/<city>.json, also written by the fit),
   a street uses the counts measured on that same street nearby instead of the estimate.

2. Busyness. An S curve (logistic in log volume) turns vehicles a day into 0 (quiet) .. 1 (busy):

       busy = 1 / (1 + exp(-(ln(volume) - ln(BUSY_ADT)) / WIDTH))

   centered at BUSY_ADT vehicles a day (PLANNER_BUSY_ADT, default 20,000: a four-lane arterial;
   a main street with shops and one lane each way carries less). Half busy there, 10% busy at a
   quarter less traffic, 90% at a third more.

3. Along a walk. Every 10 m of a walking path is matched to the nearest street for cars within
   20 m (a sidewalk mapped on its own still belongs to the street beside it), and gets that
   street's busyness. busy_m is the walking on busy streets (busyness at least 0.5).
"""
import json
import math
import os
import re
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_FILE = os.path.join(HERE, "traffic_model.json")
DATA = os.environ.get("PLANNER_CACHE_DIR") or os.path.join(os.path.dirname(HERE), "data")
ROADS_DIR = os.path.join(DATA, "roads")

BUSY_ADT = float(os.environ.get("PLANNER_BUSY_ADT", "20000"))
WIDTH = math.log(1.3) / math.log(9)  # 10% -> 90% busy from BUSY_ADT / 1.3 to BUSY_ADT x 1.3
MATCH_M = 20  # a walk point belongs to a street for cars at most this far away
SAMPLE_M = 10

# Streets for cars that people walk beside. Motorways aren't walked along; *_link counts as its road.
CLASSES = ("trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "living_street")
ROAD_RX = "^(%s)(_link)?$" % "|".join(CLASSES)

_model = None
_lock = threading.Lock()


# ---------------------------------------------------------------- the model

def model():
    global _model
    if _model is None:
        try:
            with open(MODEL_FILE) as f:
                _model = json.load(f)
        except (OSError, ValueError):
            _model = {}
    return _model


def road_class(tags):
    hw = (tags.get("highway") or "").replace("_link", "")
    return hw if hw in CLASSES else None


def speed_kmh(tags):
    """The speed limit in km/h ("25 mph" -> 40), or None when not tagged."""
    v = (tags.get("maxspeed") or "").strip().lower()
    try:
        if v.endswith("mph"):
            return float(v[:-3]) * 1.609
        return float(v.split()[0])
    except (ValueError, IndexError):
        return None


def lanes(tags):
    try:
        return max(1.0, float((tags.get("lanes") or "").split(";")[0]))
    except ValueError:
        return None


def features(tags, m=None):
    """(class, ln lanes, speed km/h, one-way 0/1, route number 0/1), with the city's usual lanes
    and speed for that class when a tag is missing."""
    m = m or model()
    c = road_class(tags)
    if not c:
        return None
    ln_lanes = math.log(lanes(tags) or (m.get("default_lanes") or {}).get(c, 2))
    kmh = speed_kmh(tags) or (m.get("default_kmh") or {}).get(c, 40)
    oneway = 1.0 if tags.get("oneway") in ("yes", "1", "-1") else 0.0
    ref = 1.0 if tags.get("ref") else 0.0
    return c, ln_lanes, kmh, oneway, ref


def estimate_adt(tags):
    """Vehicles a day on a street, estimated from its OpenStreetMap tags (None if not a street for cars
    or no model has been fitted)."""
    m = model()
    f = features(tags, m)
    if not f or not m.get("class"):
        return None
    c, ln_lanes, kmh, oneway, ref = f
    w = m["coef"]
    ln = (m["class"].get(c, min(m["class"].values())) + w["ln_lanes"] * ln_lanes
          + w["kmh"] * kmh + w["oneway"] * oneway + w["ref"] * ref)
    return math.exp(_calibrate(ln, m.get("calibration")))


def _calibrate(ln, table):
    """Predicted ln(volume) -> measured ln(volume) at the same percentile (piecewise linear)."""
    if not table:
        return ln
    if ln <= table[0][0]:
        return table[0][1] + (ln - table[0][0])
    for (p0, m0), (p1, m1) in zip(table, table[1:]):
        if ln <= p1:
            return m0 + (m1 - m0) * ((ln - p0) / (p1 - p0) if p1 > p0 else 1.0)
    return table[-1][1] + (ln - table[-1][0])


COUNTS_DIR = os.path.join(HERE, "traffic_counts")
SAME_STREET_M = 1500  # counts on the same street this close stand for it
_counts = None


def norm_name(name):
    return " ".join((name or "").lower().replace(".", " ").split())


STREET_WORDS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW", "NORTH", "SOUTH", "EAST", "WEST", "NORTHEAST",
                "NORTHWEST", "SOUTHEAST", "SOUTHWEST", "ST", "STREET", "AVE", "AV", "AVENUE", "BLVD", "BLV",
                "BOULEVARD", "RD", "ROAD", "DR", "DRIVE", "PL", "PLACE", "CT", "COURT", "LN", "LANE", "WAY",
                "HWY", "HIGHWAY", "PKWY", "PKY", "PARKWAY", "TER", "TERRACE", "CIR", "CIRCLE", "THE"}


def street_core(name):
    """ "Southeast César E. Chávez Boulevard" and "SE CESAR E CHAVEZ BLV" -> "CESAR CHAVEZ":
    the words that tell a street apart, without accents, directions or street types."""
    import unicodedata
    plain = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().upper()
    words = [{"JUNIOR": "JR", "SAINT": "ST"}.get(w, w) for w in re.findall(r"[A-Z0-9]+", plain)]
    return " ".join(w for w in words if w not in STREET_WORDS)


def counts_by_street():
    """Measured counts, {street name: [(lat, lon, vehicles a day), ...]}, from every city's file."""
    global _counts
    if _counts is None:
        _counts = {}
        try:
            files = [f for f in os.listdir(COUNTS_DIR) if f.endswith(".json")]
        except OSError:
            files = []
        for fn in files:
            try:
                with open(os.path.join(COUNTS_DIR, fn)) as f:
                    for lat, lon, adt, name in json.load(f)["counts"]:
                        _counts.setdefault(norm_name(name), []).append((lat, lon, adt))
            except (OSError, ValueError, KeyError):
                continue
    return _counts


NEAREST_COUNTS = 3


def measured_adt(name, lat, lon):
    """The median of the NEAREST_COUNTS counts measured on this street within SAME_STREET_M (a
    street's traffic changes along it), or None."""
    k = math.cos(math.radians(lat))
    near = sorted((math.hypot((la - lat) * 111320, (lo - lon) * 111320 * k), adt)
                  for la, lo, adt in counts_by_street().get(norm_name(name), ()))
    near = sorted(adt for d, adt in near[:NEAREST_COUNTS] if d <= SAME_STREET_M)
    return near[len(near) // 2] if near else None


def busyness(adt, busy_adt=None):
    """0 (quiet) .. 1 (busy): an S curve in log traffic, half busy at BUSY_ADT vehicles a day."""
    if not adt:
        return 0.0
    x = (math.log(adt) - math.log(busy_adt or BUSY_ADT)) / WIDTH
    return 1.0 / (1.0 + math.exp(-max(-50.0, min(50.0, x))))


# ---------------------------------------------------------------- streets along a walk

def _xy(p, lat0):
    return p[1] * 111320.0 * math.cos(math.radians(lat0)), p[0] * 111320.0


def _seg_dist(p, a, b):
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


class Roads:
    """Streets for cars in an area, each with its estimated traffic, indexed for nearest lookups."""
    CELL = 50.0

    def __init__(self, ways):
        self.lat0 = None
        self.grid = {}
        for w in ways:
            tags = w.get("tags") or {}
            adt = estimate_adt(tags)
            pts = w.get("geometry") or []
            if adt is None or len(pts) < 2:
                continue
            if self.lat0 is None:
                self.lat0 = pts[0]["lat"]
            mid = pts[len(pts) // 2]
            adt = (tags.get("name") and measured_adt(tags["name"], mid["lat"], mid["lon"])) or adt
            name = tags.get("name") or tags.get("ref") or "a busy road"
            xy = [_xy((p["lat"], p["lon"]), self.lat0) for p in pts]
            for a, b in zip(xy, xy[1:]):
                seg = (a, b, adt, name)
                x0, x1 = sorted((a[0], b[0]))
                y0, y1 = sorted((a[1], b[1]))
                for gx in range(int((x0 - MATCH_M) // self.CELL), int((x1 + MATCH_M) // self.CELL) + 1):
                    for gy in range(int((y0 - MATCH_M) // self.CELL), int((y1 + MATCH_M) // self.CELL) + 1):
                        self.grid.setdefault((gx, gy), []).append(seg)

    def at(self, lat, lon):
        """(vehicles a day, street name) of the nearest street for cars within MATCH_M, or None."""
        if self.lat0 is None:
            return None
        p = _xy((lat, lon), self.lat0)
        best, best_d = None, MATCH_M
        for seg in self.grid.get((int(p[0] // self.CELL), int(p[1] // self.CELL)), ()):
            d = _seg_dist(p, seg[0], seg[1])
            if d <= best_d:
                best, best_d = seg, d
        return (best[2], best[3]) if best else None


def _overpass_roads(s, w, n, e):
    from . import places
    q = '[out:json][timeout:90];way["highway"~"%s"](%.4f,%.4f,%.4f,%.4f);out tags geom;' % (ROAD_RX, s, w, n, e)
    return places._overpass(q)


def roads_around(points, margin_m=400):
    """The streets for cars around these points ({"lat", "lon"}), from OpenStreetMap; cached on disk
    for a month, by the area rounded to about a kilometer."""
    import time
    lats = [p["lat"] for p in points]
    lons = [p["lon"] for p in points]
    dl = margin_m / 111320.0
    dn = dl / max(0.2, math.cos(math.radians(sum(lats) / len(lats))))
    s, n = math.floor((min(lats) - dl) * 100) / 100, math.ceil((max(lats) + dl) * 100) / 100
    w, e = math.floor((min(lons) - dn) * 100) / 100, math.ceil((max(lons) + dn) * 100) / 100
    path = os.path.join(ROADS_DIR, "%.2f_%.2f_%.2f_%.2f.json" % (s, w, n, e))
    with _lock:
        try:
            if time.time() - os.path.getmtime(path) < 30 * 24 * 3600:
                with open(path) as f:
                    return Roads(json.load(f))
        except (OSError, ValueError):
            pass
    ways = _overpass_roads(s, w, n, e)
    with _lock:
        os.makedirs(ROADS_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump([{"tags": x.get("tags", {}), "geometry": x.get("geometry", [])} for x in ways], f)
    return Roads(ways)


def profile(path, roads, skip_m=40):
    """How busy the streets along a walking path ([[lat, lon], ...]) are:
    {"busy_m": meters on busy streets, "cost_m": meters weighted by busyness,
     "streets": {name: (meters, vehicles a day)} for the busy ones}. The first and last skip_m
    (leaving or reaching a stop that is on a busy street) don't count."""
    from .streets import _samples
    out = {"busy_m": 0, "cost_m": 0.0, "streets": {}}
    if not roads or not path or len(path) < 2:
        return out
    smp = _samples(path)
    total = smp[-1][1]
    for p, at in smp:
        if not skip_m <= at <= total - skip_m:
            continue
        hit = roads.at(p[0], p[1])
        if not hit:
            continue
        b = busyness(hit[0])
        out["cost_m"] += SAMPLE_M * b
        if b >= 0.5:
            out["busy_m"] += SAMPLE_M
            m, _ = out["streets"].get(hit[1], (0, 0))
            out["streets"][hit[1]] = (m + SAMPLE_M, round(hit[0], -2))
    return out


# ---------------------------------------------------------------- fitting the model to measured counts

PBOT = "https://www.portlandmaps.com/od/rest/services/COP_OpenData_Transportation/MapServer/250/query"


FIT_DIR = os.path.join(DATA, "traffic-fit")  # downloads kept for refitting


def _kept(name, fetch):
    path = os.path.join(FIT_DIR, name)
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    value = fetch()
    os.makedirs(FIT_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(value, f)
    return value


def _pbot_counts(log=print):
    """Portland's traffic counts: [(two-way vehicles a day, lat, lon, bounds, description, year)], the latest
    count at each place. A count of one direction on a two-way street is doubled later."""
    return [tuple(r) for r in _kept("pbot-counts-v2.json", lambda: _download_pbot(log))]


def _download_pbot(log):
    import datetime
    import urllib.parse
    from .places import _http
    rows, offset = [], 0
    while True:
        url = PBOT + "?" + urllib.parse.urlencode({
            "where": "ADTVolume > 0", "outFields": "CountID,LocationID,ChannelNum,Bound,ADTVolume,StartDate,LocationDesc",
            "outSR": "4326", "f": "json", "resultOffset": offset, "resultRecordCount": 1000,
            "orderByFields": "OBJECTID"})
        page = _http(url, timeout=90).get("features", [])
        rows += page
        offset += len(page)
        if offset % 5000 < len(page):
            log("  %d count rows" % offset)
        if not page:
            break
    counts = {}
    for f in rows:
        a, g = f["attributes"], f.get("geometry") or {}
        if "y" not in g:
            continue
        c = counts.setdefault(a["CountID"], {"adt": 0, "lat": g["y"], "lon": g["x"], "bounds": set(), "ch": set(),
                                             "desc": a.get("LocationDesc") or "",
                                             "loc": a.get("LocationID") or "%.4f,%.4f" % (g["y"], g["x"]),
                                             "when": a.get("StartDate") or 0})
        if a.get("ChannelNum") in c["ch"]:
            continue
        c["ch"].add(a.get("ChannelNum"))
        c["adt"] += a["ADTVolume"]
        c["bounds"].add(a.get("Bound") or "")
    latest = {}
    for c in counts.values():
        if c["loc"] not in latest or c["when"] > latest[c["loc"]]["when"]:
            latest[c["loc"]] = c
    log("%d counts at %d places" % (len(counts), len(latest)))
    return [(c["adt"], c["lat"], c["lon"], len([b for b in c["bounds"] if b]), c["desc"],
             datetime.datetime.fromtimestamp(c["when"] / 1000, datetime.timezone.utc).year) for c in latest.values()]


def _solve(A, y, ridge=1e-3):
    """Least squares with a little ridge (normal equations, Gaussian elimination): no numpy needed."""
    k = len(A[0])
    M = [[sum(r[i] * r[j] for r in A) + (ridge if i == j else 0.0) for j in range(k)] for i in range(k)]
    v = [sum(r[i] * t for r, t in zip(A, y)) for i in range(k)]
    for i in range(k):
        p = max(range(i, k), key=lambda r: abs(M[r][i]))
        M[i], M[p], v[i], v[p] = M[p], M[i], v[p], v[i]
        for r in range(i + 1, k):
            f = M[r][i] / M[i][i]
            M[r] = [a - f * b for a, b in zip(M[r], M[i])]
            v[r] -= f * v[i]
    x = [0.0] * k
    for i in reversed(range(k)):
        x[i] = (v[i] - sum(M[i][j] * x[j] for j in range(i + 1, k))) / M[i][i]
    return x


def fit(log=print, since_year=2010):
    """Fit the traffic model to Portland's measured counts and save it to MODEL_FILE."""
    import statistics
    counts = [c for c in _pbot_counts(log) if c[5] >= since_year]
    s, n = min(c[1] for c in counts), max(c[1] for c in counts)
    w, e = min(c[2] for c in counts), max(c[2] for c in counts)
    ways, step = [], 0.04
    tiles = [(la, lo) for la in frange(s, n, step) for lo in frange(w, e, step)]
    for i, (la, lo) in enumerate(tiles):  # the city's streets in tiles of about 4 km
        ways += _kept("ways-%.2f-%.2f.json" % (la, lo), lambda: [
            {"tags": x.get("tags", {}), "geometry": x.get("geometry", [])}
            for x in _overpass_roads(la, lo, la + step, lo + step)])
        if i % 10 == 9:
            log("  streets: %d of %d tiles, %d ways" % (i + 1, len(tiles), len(ways)))
    roads = _TagIndex(ways)
    rows, located = [], []
    for adt, lat, lon, nbounds, desc, year in counts:
        # The counted street is the first one its description names ("SE HAWTHORNE BLV W/SE 33RD AVE"):
        # a counter near a corner belongs to that street, not to whichever street is nearest.
        counted = street_core(re.split(r"\s+[NSEW](?:/|\s+OF\s)|\s+BETWEEN\s|\s+AT\s", desc.upper())[0])
        tags = next((t for t in roads.near(lat, lon) if road_class(t) and street_core(t.get("name")) == counted), None)
        if not tags or not counted:
            continue
        if nbounds == 1 and tags.get("oneway") not in ("yes", "1", "-1"):
            adt *= 2  # one direction of a two-way street
        rows.append((tags, adt))
        if tags.get("name"):
            located.append([round(lat, 5), round(lon, 5), int(adt), tags["name"]])
    log("%d counts matched to a street" % len(rows))
    by_class = {}
    for tags, _ in rows:
        by_class.setdefault(road_class(tags), []).append(tags)
    default_lanes = {c: statistics.median([lanes(t) for t in ts if lanes(t)] or [2]) for c, ts in by_class.items()}
    default_kmh = {c: statistics.median([speed_kmh(t) for t in ts if speed_kmh(t)] or [40]) for c, ts in by_class.items()}
    classes = sorted(by_class)
    base = {"default_lanes": default_lanes, "default_kmh": default_kmh}
    A, y = [], []
    for tags, adt in rows:
        c, ln_lanes, kmh, oneway, ref = features(tags, base)
        A.append([1.0 if c == k else 0.0 for k in classes] + [ln_lanes, kmh, oneway, ref])
        y.append(math.log(adt))
    x = _solve(A, y)
    pred = [sum(a * b for a, b in zip(r, x)) for r in A]
    mean = sum(y) / len(y)
    r2 = 1 - sum((p - t) ** 2 for p, t in zip(pred, y)) / sum((t - mean) ** 2 for t in y)
    sp, sy = sorted(pred), sorted(y)  # percentile by percentile: predicted -> measured
    pick = lambda v, q: v[min(len(v) - 1, int(q / 100.0 * len(v)))]
    calibration = [[round(pick(sp, q), 4), round(pick(sy, q), 4)] for q in range(0, 101, 2)]
    os.makedirs(COUNTS_DIR, exist_ok=True)
    with open(os.path.join(COUNTS_DIR, "portland.json"), "w") as f:
        json.dump({"source": "City of Portland (PBOT) traffic volume counts since %d, two-way vehicles a day, "
                             "named by the OpenStreetMap street under each counter" % since_year,
                   "counts": located}, f, separators=(",", ":"))
    m = dict(base, source="City of Portland (PBOT) traffic volume counts since %d, matched to OpenStreetMap streets"
             % since_year, counts=len(rows), r2_log=round(r2, 3), calibration=calibration,
             **{"class": dict(zip(classes, (round(v, 4) for v in x[:len(classes)]))),
                "coef": dict(zip(("ln_lanes", "kmh", "oneway", "ref"), (round(v, 4) for v in x[len(classes):])))})
    with open(MODEL_FILE, "w") as f:
        json.dump(m, f, indent=1, sort_keys=True)
    global _model
    _model = m
    return m


def frange(a, b, step):
    while a < b:
        yield a
        a += step


class _TagIndex(Roads):
    """Like Roads, but returns the nearest street's tags (for matching counters to streets)."""

    def __init__(self, ways):
        self.lat0, self.grid = None, {}
        for w in ways:
            pts = w.get("geometry") or []
            if len(pts) < 2:
                continue
            if self.lat0 is None:
                self.lat0 = pts[0]["lat"]
            xy = [_xy((p["lat"], p["lon"]), self.lat0) for p in pts]
            for a, b in zip(xy, xy[1:]):
                for gx in range(int((min(a[0], b[0]) - MATCH_M) // self.CELL), int((max(a[0], b[0]) + MATCH_M) // self.CELL) + 1):
                    for gy in range(int((min(a[1], b[1]) - MATCH_M) // self.CELL), int((max(a[1], b[1]) + MATCH_M) // self.CELL) + 1):
                        self.grid.setdefault((gx, gy), []).append((a, b, w.get("tags") or {}, None))

    def near(self, lat, lon):
        """Tags of every street within MATCH_M, nearest first."""
        if self.lat0 is None:
            return []
        p = _xy((lat, lon), self.lat0)
        hits = sorted((_seg_dist(p, a, b), id(t), t) for a, b, t, _ in
                      self.grid.get((int(p[0] // self.CELL), int(p[1] // self.CELL)), ()))
        out, seen = [], set()
        for d, i, t in hits:
            if d <= MATCH_M and i not in seen:
                seen.add(i)
                out.append(t)
        return out
