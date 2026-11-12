"""Street crime along the walking legs, by street and hour of the day.

Data: each city's police open data, the past 12 months of reported incidents with location
and time, downloaded ONCE into planner/crime_data/<city>.json by scripts/update_crime.py. The
planner only reads those files; keeping them fresh (re-running the script, e.g. monthly) is the
job of a separate service, not of the app. Sources:
- Portland: Portland Police Bureau "New Offense Data" (Tableau Public CSV, one file per year)
- San Francisco: SFPD incident reports (DataSF, Socrata wg3w-h783)
- Seattle: SPD crime data (data.seattle.gov, Socrata tazs-3rd5)
- Chicago: CPD crimes (data.cityofchicago.org, Socrata ijzp-q8t2)
Other cities have no source yet: the check says so instead of guessing.

Only crimes that happen to people on the street count, sorted by type: robbery, serious violence
(aggravated assault, shootings, homicide, kidnapping), sex offenses, simple assault, theft from a
person (pickpocketing, purse snatching), weapons and drug offenses. Car break-ins, burglary,
fraud, shoplifting and domestic violence don't. Incidents go into ~150 m cells, counted per
type and hour of the day. A walking leg is checked cell by cell along its line, in a 3-hour
window around when it is walked (over the whole past year).

Only serious violence reroutes a walk: robberies, serious assaults and shootings, sex offenses.
Drug and weapons offenses, simple assaults and thefts from a person are reported with the walk,
by type, but never force a ride. A plan with a ride taken to avoid crime scores lower (it's a
detour forced on the group), so a plan that walks safely wins when otherwise equal.

Tolerance (person's limits, safety.crime_tolerance; the group uses the most cautious):
- "low" (default): ride instead of walking past a block with 6+ serious crimes in that time window
  over the past year (bus if one serves the leg, else Uber/Lyft/taxi).
- "medium": only for 12+.
- "high": walk anyway; the block is noted.
"""
import csv
import datetime
import io
import json
import math
import os
import re
import threading
import urllib.parse
import urllib.request

CELL_DEG_LAT = 0.00135  # ~150 m
# Crime types, in the order they are checked (the first match wins).
TYPES = [
    ("robbery", re.compile(r"(?i)robbery")),
    ("violent", re.compile(r"(?i)aggravated|homicide|murder|manslaughter|shooting|kidnap|abduct")),
    ("sex", re.compile(r"(?i)rape|sexual assault|sex offense|fondling|sodomy|criminal sexual")),
    ("assault", re.compile(r"(?i)assault|battery|intimidation")),
    ("theft", re.compile(r"(?i)pocket|purse|from (the )?person|snatch")),
    ("weapons", re.compile(r"(?i)weapon")),
    ("drugs", re.compile(r"(?i)drug|narcotic")),
]
NOT_STREET = re.compile(r"(?i)domestic|vehicle|shoplift|fraud|burglary|vandal|forgery|embezzle|identity|counterfeit")
SERIOUS = ("robbery", "violent", "sex")
# Serious crimes (robbery, serious violence, sex offenses) in one ~150 m block, in the 3-hour
# window around the walk, over the past year: at this many, the walk becomes a ride.
SERIOUS_LIMIT = {"low": 6, "medium": 12}
# All street crimes at which a block is worth a note on the walk (never a reroute on its own).
TOTAL_NOTE = 30
NAMES = {"robbery": ("robbery", "robberies"), "violent": ("serious assault or shooting", "serious assaults or shootings"),
         "sex": ("sex offense", "sex offenses"), "assault": ("simple assault", "simple assaults"),
         "theft": ("theft from a person", "thefts from a person"), "weapons": ("weapons offense", "weapons offenses"),
         "drugs": ("drug offense", "drug offenses")}
LEVELS = ("low", "medium", "high")
_lock = threading.Lock()


def _get(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "group-day-planner/1.0 (hackathon demo)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _since():
    return datetime.date.today() - datetime.timedelta(days=365)


# ------------------------------------------------------------------ sources

def _portland(center, radius_m):
    """PPB New Offense Data: this year's and last year's files, the past 12 months."""
    since = _since()
    out = []
    for year in sorted({since.year, datetime.date.today().year}):
        raw = _get("https://public.tableau.com/views/PPBOpenDataDownloads/New_Offense_Data_%d.csv?:showVizHome=no"
                   % year, timeout=240).decode("utf-8-sig", "replace")
        for r in csv.DictReader(io.StringIO(raw)):
            if not r.get("OpenDataLat"):
                continue
            kind = "%s %s" % (r.get("OffenseCategory", ""), r.get("OffenseType", ""))
            try:
                day = datetime.datetime.strptime(r["OccurDate"], "%m/%d/%Y").date()
                hour = int(r.get("OccurTime") or 0) // 100
            except ValueError:
                continue
            if day < since:
                continue
            out.append((float(r["OpenDataLat"]), float(r["OpenDataLon"]), hour % 24, kind, r.get("Address") or ""))
    return out


def _socrata(domain, dataset, fields, where_box, center, radius_m):
    dlat = radius_m / 111000.0
    dlon = dlat / max(0.2, math.cos(math.radians(center["lat"])))
    box = where_box(center["lat"] - dlat, center["lat"] + dlat, center["lon"] - dlon, center["lon"] + dlon)
    q = {"$select": ",".join(fields + ["latitude", "longitude"]), "$limit": "300000",
         "$where": "%s > '%s' AND %s" % (fields[0], _since().isoformat(), box)}
    data = json.loads(_get("https://%s/resource/%s.json?%s" % (domain, dataset, urllib.parse.urlencode(q))))
    out = []
    for r in data:
        try:
            lat, lon = float(r.get("latitude")), float(r.get("longitude"))
            hour = int(r[fields[0]][11:13])
        except (TypeError, ValueError, KeyError):
            continue
        kind = " ".join(str(r.get(f, "")) for f in fields[1:3])
        out.append((lat, lon, hour, kind, str(r.get(fields[-1]) or "")))
    return out


def _between(la1, la2, lo1, lo2):
    return "latitude between %.5f and %.5f AND longitude between %.5f and %.5f" % (la1, la2, lo1, lo2)


def _between_text(la1, la2, lo1, lo2):  # Seattle stores coordinates as text, some "REDACTED"
    return ("latitude between '%.5f' and '%.5f' AND longitude between '%.5f' and '%.5f'"
            % (la1, la2, lo2, lo1))  # negative longitudes sort reversed as text


SOURCES = {
    "portland": ("Portland Police Bureau open data", _portland),
    "san francisco": ("SFPD incident reports (DataSF)", lambda c, r: _socrata(
        "data.sf.gov", "wg3w-h783", ["incident_datetime", "incident_category", "incident_subcategory", "intersection"],
        _between, c, r)),
    "seattle": ("Seattle Police crime data", lambda c, r: _socrata(
        "data.seattle.gov", "tazs-3rd5", ["offense_date", "offense_category", "nibrs_offense_code_description",
                                          "block_address"], _between_text, c, r)),
    "chicago": ("Chicago Police crime data", lambda c, r: _socrata(
        "data.cityofchicago.org", "ijzp-q8t2", ["date", "primary_type", "description", "block"], _between, c, r)),
}


def source_for(city):
    c = (city or "San Francisco").lower()
    return next((k for k in SOURCES if k in c), None)


# ------------------------------------------------------------------ index

def cell(lat, lon):
    dlon = CELL_DEG_LAT / math.cos(math.radians(lat))
    return "%d:%d" % (math.floor(lat / CELL_DEG_LAT), math.floor(lon / dlon))


def _window(hours, h):
    return hours[(h - 1) % 24] + hours[h % 24] + hours[(h + 1) % 24]


def crime_type(kind):
    if NOT_STREET.search(kind):
        return None
    return next((t for t, rx in TYPES if rx.search(kind)), None)


def build_index(incidents):
    """{"cells": {cell: {"t": {type: [24 counts by hour]}, "street": most-named block}}}."""
    cells, names = {}, {}
    total = 0
    for lat, lon, hour, kind, addr in incidents:
        t = crime_type(kind)
        if not t:
            continue
        c = cell(lat, lon)
        cells.setdefault(c, {}).setdefault(t, [0] * 24)[hour] += 1
        total += 1
        if addr:
            names.setdefault(c, {}).setdefault(addr, 0)
            names[c][addr] += 1
    return {"cells": {c: {"t": v, "street": _tidy(max(names[c], key=names[c].get)) if c in names else ""}
                      for c, v in cells.items()},
            "incidents": total, "serious_limit": SERIOUS_LIMIT}


def counts_at(info, h):
    """{type: count} in the 3-hour window around hour h, for one cell."""
    return {t: _window(hours, h) for t, hours in info["t"].items() if _window(hours, h)}


def serious(counts):
    return sum(counts.get(t, 0) for t in SERIOUS)


def notable(counts):
    """Worth a note on the walk (half the "low" limits)."""
    return serious(counts) >= SERIOUS_LIMIT["low"] // 2 or sum(counts.values()) >= TOTAL_NOTE


def too_much(counts, tolerance):
    """Only serious violence reroutes a walk; the other street crimes are reported, not acted on."""
    if tolerance == "high":
        return False
    return serious(counts) >= SERIOUS_LIMIT["medium" if tolerance == "medium" else "low"]


def _tidy(addr):
    a = re.sub(r"\s+", " ", addr.replace("\\", "&")).strip()
    return a.title().replace(" Of ", " of ")


DATA_DIR = os.path.join(os.path.dirname(__file__), "crime_data")

# The area each city's file covers (center, radius): the whole city.
AREAS = {"portland": ({"lat": 45.5230, "lon": -122.6500}, 16000),
         "san francisco": ({"lat": 37.7600, "lon": -122.4400}, 10000),
         "seattle": ({"lat": 47.6100, "lon": -122.3300}, 15000),
         "chicago": ({"lat": 41.8400, "lon": -87.6800}, 25000)}


def path_for(src):
    return os.path.join(DATA_DIR, src.replace(" ", "_") + ".json")


_loaded = {}


def load(city):
    """The crime index for the city, read from its file, or None (no file: no source yet)."""
    src = source_for(city)
    if not src or not os.path.exists(path_for(src)):
        return None
    with _lock:
        if src not in _loaded:
            with open(path_for(src)) as f:
                _loaded[src] = json.load(f)
        return _loaded[src]


def update(src):
    """Download one city's past 12 months and write its file. Run by scripts/update_crime.py
    (or whatever service keeps the data fresh), never by the planner."""
    label, fetch = SOURCES[src]
    center, radius = AREAS[src]
    idx = build_index(fetch(center, radius))
    idx.update(source=label, city=src, downloaded=datetime.date.today().isoformat(),
               covers="%s to %s" % (_since().isoformat(), datetime.date.today().isoformat()))
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path_for(src), "w") as f:
        json.dump(idx, f, separators=(",", ":"))
    _loaded.pop(src, None)
    return idx


# ------------------------------------------------------------------ legs

def _cells_along(a, b, step_m=75):
    """Cells a straight walk from a to b passes (plus the cells either side of the line)."""
    d = math.hypot((b["lat"] - a["lat"]) * 111000, (b["lon"] - a["lon"]) * 111000 * math.cos(math.radians(a["lat"])))
    n = max(1, int(d // step_m))
    out = []
    for i in range(n + 1):
        lat = a["lat"] + (b["lat"] - a["lat"]) * i / n
        lon = a["lon"] + (b["lon"] - a["lon"]) * i / n
        c = cell(lat, lon)
        if c not in out:
            out.append(c)
    return out


def key(p):
    return "%.5f,%.5f" % (p["lat"], p["lon"])


def pair_cells(idx, points, max_walk_km=3.0):
    """For every pair of places close enough to walk between, the blocks along the way that
    matter: serious crimes reach the "low" limit at some hour, or are notable (for the note).
    Small on purpose: it travels with the outing to every route sandbox."""
    uniq = {key(p): p for p in points}
    hot = {c for c, v in idx["cells"].items() if any(notable(counts_at(v, h)) for h in range(24))}
    pairs, used = {}, {}
    for ka, a in uniq.items():
        for kb, b in uniq.items():
            if ka == kb or _km(a, b) * 1.3 > max_walk_km:
                continue
            hit = [c for c in _cells_along(a, b) if c in hot]
            if hit:
                pairs.setdefault(ka, {})[kb] = hit
                for c in hit:
                    used[c] = idx["cells"][c]
    return {"source": idx.get("source"), "covers": idx.get("covers"), "cells": used,
            "pairs": pairs, "incidents": idx.get("incidents", 0)}


def _km(a, b):
    return math.hypot((b["lat"] - a["lat"]) * 111.0, (b["lon"] - a["lon"]) * 111.0 * math.cos(math.radians(a["lat"])))


def hotspot(crime, a, b, minute, tolerance):
    """The worst block on the walk from a to b around that time, or None:
    {"street", "hour", "serious", "total", "counts": {type: n}, "avoid": bool}. avoid is True when
    the block reaches the tolerance's limits; otherwise it is only worth a note."""
    if not crime:
        return None
    cells = (crime.get("pairs") or {}).get(key(a), {}).get(key(b)) or []
    h = int(minute // 60) % 24
    worst = None
    for c in cells:
        info = crime["cells"].get(c)
        if not info:
            continue
        counts = counts_at(info, h)
        if not notable(counts):
            continue
        rank = (too_much(counts, tolerance), serious(counts), sum(counts.values()))
        if worst is None or rank > worst["_rank"]:
            worst = {"street": info.get("street") or "a block on the way", "hour": h, "serious": serious(counts),
                     "total": sum(counts.values()), "counts": counts, "avoid": too_much(counts, tolerance),
                     "_rank": rank}
    if worst:
        worst.pop("_rank")
    return worst


def describe(spot):
    """ "24th St & Osage Aly around 10am, past year: 65 street crimes (4 robberies, 1 serious
    assault or shooting, 38 drug offenses, 14 simple assaults, ...)": serious types first." """
    def part(t, n):
        return "%d %s" % (n, NAMES[t][0 if n == 1 else 1])
    c = spot["counts"]
    kinds = [part(t, c[t]) for t in sorted(c, key=lambda t: (t not in SERIOUS, -c[t]))]
    when = "%d%s" % ((spot["hour"] % 12) or 12, "am" if spot["hour"] < 12 else "pm")
    return "%s around %s, past year: %d street crimes (%s)" % (spot["street"], when, sum(c.values()),
                                                               ", ".join(kinds))
