"""Bus rides for legs too long to walk, from the bus routes on OpenStreetMap.

One Overpass query (overpass.ch when OVERPASS_CH_KEY is set) fetches every bus stop near the
start and every bus route that serves them, with each route's stops in order. A leg is a bus
ride when one route stops near both ends, in the right direction: walk to the stop, wait,
ride, walk to the venue. The options are computed once for every pair of places, so the
route branches (and sandboxes) never call the map service themselves.

Times are estimates: OpenStreetMap has the routes and stops, not the timetables. Waiting is
half a typical 15-minute headway, and buses average about 18 km/h in town.
"""
import math

from . import places

STOP_RADIUS_M = 500     # walk at most this far to or from a stop
WAIT_MIN = 8            # half a 15-minute headway
BUS_KMH = 18            # city bus average, stops included
MIN_LEG_KM = 0.3        # shorter legs are always walked (longer ones may need a bus to skip a hotspot)


def _km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def key(p):
    return "%.5f,%.5f" % (p["lat"], p["lon"])


def network(center, radius_m):
    """{"stops": {id: {lat, lon, name}}, "routes": [{"ref", "name", "stops": [ids in order]}]}."""
    def fetch():
        q = ("[out:json][timeout:60];"
             "(node(around:{r},{la:.5f},{lo:.5f})[highway=bus_stop];"
             "node(around:{r},{la:.5f},{lo:.5f})[public_transport=platform][bus=yes];)->.s;"
             ".s out body;"
             "rel(bn.s)[route=bus];out body;").format(r=int(radius_m), la=center["lat"], lo=center["lon"])
        return places._overpass(q)
    elements = places._cached("bus:%.4f,%.4f:%d" % (center["lat"], center["lon"], radius_m), fetch,
                              ttl=7 * 24 * 3600)
    stops = {}
    for e in elements:
        if e["type"] == "node" and "lat" in e:
            stops[e["id"]] = {"lat": e["lat"], "lon": e["lon"], "name": (e.get("tags") or {}).get("name", "")}
    routes = []
    for e in elements:
        if e["type"] != "relation":
            continue
        tags = e.get("tags") or {}
        seq = [m["ref"] for m in e.get("members", []) if m["type"] == "node" and m["ref"] in stops
               and m.get("role", "") in ("", "stop", "platform", "stop_entry_only", "stop_exit_only",
                                         "platform_entry_only", "platform_exit_only")]
        if len(seq) >= 2:
            routes.append({"ref": tags.get("ref") or tags.get("name", "bus"), "name": tags.get("name", ""),
                           "stops": seq})
    return {"stops": stops, "routes": routes}


def _near(net, p):
    out = []
    for sid, s in net["stops"].items():
        d = _km(p, s) * 1000
        if d <= STOP_RADIUS_M:
            out.append((sid, d))
    return dict(out)


def best_bus(net, a, b, pace_kmh=4.5, near_a=None, near_b=None):
    """The quickest single bus ride from a to b, or None. Walking to and from the stops is
    included (street detours ~1.3x the straight line)."""
    near_a = _near(net, a) if near_a is None else near_a
    near_b = _near(net, b) if near_b is None else near_b
    if not near_a or not near_b:
        return None
    best = None
    for r in net["routes"]:
        seq = r["stops"]
        boards = [(i, near_a[s]) for i, s in enumerate(seq) if s in near_a]
        alights = [(j, near_b[s]) for j, s in enumerate(seq) if s in near_b]
        for i, da in boards:
            for j, db in alights:
                if j <= i:
                    continue  # wrong direction (each direction is its own route on OSM)
                s1, s2 = net["stops"][seq[i]], net["stops"][seq[j]]
                ride_km = sum(_km(net["stops"][seq[k]], net["stops"][seq[k + 1]]) for k in range(i, j))
                walk_km = (da + db) * 1.3 / 1000
                minutes = walk_km / pace_kmh * 60 + WAIT_MIN + ride_km / BUS_KMH * 60
                if best is None or minutes < best["min"]:
                    # Only what routing and the Routes tab use: this travels to every route sandbox.
                    best = {"route": r["ref"], "board": s1["name"] or "the stop", "alight": s2["name"] or "the stop",
                            "stops": j - i, "walk_km": round(walk_km, 2), "min": round(minutes)}
    return best


def bus_matrix(points, center, radius_m, pace_kmh=4.5):
    """Bus options between every pair of places too far apart to always walk:
    {key(a): {key(b): option}}. Pairs with no bus are left out (a rideshare then)."""
    net = network(center, radius_m)
    if not net["routes"]:
        return {"routes": 0, "pairs": {}}
    uniq = {key(p): p for p in points}
    near = {k: _near(net, p) for k, p in uniq.items()}
    pairs = {}
    for ka, a in uniq.items():
        for kb, b in uniq.items():
            if ka == kb or _km(a, b) < MIN_LEG_KM:
                continue
            opt = best_bus(net, a, b, pace_kmh, near[ka], near[kb])
            if opt:
                pairs.setdefault(ka, {})[kb] = opt
    return {"routes": len(net["routes"]), "stops": len(net["stops"]), "pairs": pairs}
