"""Keenable search: check and correct each candidate's hours, location and dog policy.

Keenable returns ranked web results (title, url, snippet). Turning a whole page into a
venue record would need an LLM; here we parse the facts we need from snippets:
- Saturday opening hours ("Fri – Sat 6:30AM - 8:00PM", "6:30am–8:00pm Friday–Sunday")
- map coordinates (map links carry "coordinate=lat,lon")
- dog policy keywords ("dog-friendly", "no dogs", ...)

With KEENABLE_API_KEY set, calls go to the keyed endpoint (10 requests/second for the
organization, no hourly cap); without it, to the keyless public endpoint, which shares
1,000 requests/hour per IP. Checked venues are cached on disk for a day (VERIFY_TTL_S).
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

KEYED_API = "https://api.keenable.ai/v1/search"
PUBLIC_API = "https://api.keenable.ai/v1/search/public"
TITLE = "group-day-planner"
CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "keenable-cache.json")
VERIFY_TTL_S = 24 * 3600

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_RE = r"(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?"
TIME_RE = r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?"
RANGE_RE = TIME_RE + r"\s*(?:–|-|to)\s*" + TIME_RE


def _keyed():
    return bool(os.environ.get("KEENABLE_API_KEY", "").strip())


class _Pacer:
    """At most `rate` request starts per second and `slots` requests in flight."""

    def __init__(self, rate, slots):
        self.gap, self.lock, self.next = 1.0 / rate, threading.Lock(), 0.0
        self.slots = threading.Semaphore(slots)

    def __enter__(self):
        self.slots.acquire()
        with self.lock:
            now = time.time()
            wait, self.next = max(0.0, self.next - now), max(now, self.next) + self.gap
        if wait:
            time.sleep(wait)

    def __exit__(self, *exc):
        self.slots.release()


# Keyed: Keenable allows 10/s per organization, so stay just under. Keyless: be gentle.
PACER = _Pacer(9, 12) if _keyed() else _Pacer(3, 3)


def search(query, n=5, tries=4):
    key = os.environ.get("KEENABLE_API_KEY", "").strip()
    headers = {"Content-Type": "application/json", "X-Keenable-Title": TITLE}
    if key:
        headers["X-API-Key"] = key
    url = KEYED_API if key else PUBLIC_API
    body = json.dumps({"query": query, "max_results": n}).encode()
    for attempt in range(tries):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with PACER, urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r).get("results", [])
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == tries - 1:
                raise
            wait = e.headers.get("Retry-After")
            time.sleep(min(float(wait) if wait and wait.isdigit() else 2 ** attempt, 10))


_cache_lock = threading.Lock()


def _cache_load():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _cache_get(key):
    with _cache_lock:
        hit = _cache_load().get(key)
    if hit and time.time() - hit["at"] < VERIFY_TTL_S:
        return hit["venue"]
    return None


def _cache_put(key, venue):
    with _cache_lock:
        data = _cache_load()
        data[key] = {"at": time.time(), "venue": venue}
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            with open(CACHE_FILE, "w") as f:
                json.dump(data, f)
        except OSError:
            pass


def _to_hhmm(h, m, ap):
    h = int(h) % 12 + (12 if ap.lower() == "p" else 0)
    return "%02d:%02s" % (h, m or "00")


def _covers_saturday(text):
    t = text.lower()
    if re.search(r"\b(daily|every day|7 days)\b", t):
        return True
    for a, b in re.findall(DAY_RE + r"\s*(?:–|-|to|through)\s*" + DAY_RE, t):
        i, j = DAYS.index(a), DAYS.index(b)
        span = range(i, j + 1) if i <= j else list(range(i, 7)) + list(range(0, j + 1))
        if 5 in span:
            return True
    return bool(re.search(r"\bsat(urday)?\b", t)) and not re.search(r"sat(urday)?\s*:?\s*closed", t)


def saturday_hours(text):
    """Return ("HH:MM", "HH:MM") for Saturday if a snippet states it, else None."""
    for line in re.split(r"\n|·|\|| {2,}", text):
        for m in re.finditer(RANGE_RE, line, re.I):
            # The days that apply are written just before or just after the time range.
            around = line[max(0, m.start() - 40):m.end() + 30]
            if _covers_saturday(around):
                g = m.groups()
                return _to_hhmm(*g[0:3]), _to_hhmm(*g[3:6])
    return None


def coordinates(results, near=None):
    for r in results:
        m = re.search(r"coordinate=(-?\d+\.\d+),(-?\d+\.\d+)", r.get("url", ""))
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            # Accept a map pin only near where we expected the venue (default: San Francisco).
            ref = near or {"lat": 37.765, "lon": -122.44}
            if abs(lat - ref["lat"]) < 0.08 and abs(lon - ref["lon"]) < 0.1:
                return lat, lon
    return None


NO_DOGS = r"no dogs|dogs (are )?not (allowed|permitted)|service (animals|dogs) only|only service"
OFF_LEASH = r"off[- ]leash"
DOG_OK = r"dog[- ]friendly|dogs (are )?(welcome|allowed|permitted)|pup|bring your dog"


STRICTNESS = {"no": 0, "patio": 1, "leash": 1, "offleash": 2}


def dog_policy(text, category):
    t = text.lower()
    if re.search(NO_DOGS, t):
        return "no"
    if category == "beach" and re.search(OFF_LEASH, t):
        return "offleash"
    if re.search(DOG_OK, t):
        return "leash" if category in ("beach", "museum") else "patio"
    return None


def verify(venue, category, city="San Francisco"):
    """Check one venue (cached for a day): one combined search for hours, location and dogs,
    plus a dogs-only search only if the first found nothing about dogs."""
    ck = "%s|%s|%s" % (city, category, venue["name"]) if city != "San Francisco" else "%s|%s" % (category, venue["name"])
    hit = _cache_get(ck)
    if hit:
        return dict(hit, cached=True)
    v = _verify(venue, category, city)
    if not v.get("verify_error"):
        _cache_put(ck, v)
    return v


def _verify(venue, category, city="San Francisco"):
    v = dict(venue)
    name = re.sub(r"\s*\(.*?\)", "", v["name"])  # "Ritual Coffee Roasters (Valencia)" -> name only
    area = re.search(r"\((.*?)\)", v["name"])
    where = (area.group(1) + " ") if area and "alternative" not in area.group(1) else ""
    verified, sources = [], []
    try:
        res = search("%s %s%s hours dogs allowed" % (name, where, city), n=8)
    except Exception as e:
        v["verify_error"] = repr(e)
        return v
    outdoors = category == "beach" or v.get("kind") == "outdoor_alternative"
    if not outdoors:  # beaches and trails are open all day; their pages list visitor-center hours
        for r in res:
            hrs = saturday_hours(r.get("snippet", ""))
            if hrs:
                v["open"], v["close"] = hrs
                if v["close"] <= v["open"]:  # closes after midnight
                    v["close"] = "23:59"
                verified.append("hours")
                sources.append(r["url"])
                break
    c = coordinates(res, near=venue)
    if c:
        v["lat"], v["lon"] = c
        verified.append("location")
    def check_dogs(results):
        for r in results:
            pol = dog_policy(r.get("title", "") + " " + r.get("snippet", ""), category)
            if pol:
                # Keyword matching can misread "Are dogs allowed? No", so a search result
                # may only make the policy stricter, never looser.
                if STRICTNESS[pol] < STRICTNESS.get(v.get("dogs", "no"), 0):
                    v["dogs"] = pol
                    v["dogs_note"] = "stricter policy found on the web"
                verified.append("dogs")
                sources.append(r["url"])
                return True
        return False

    if not check_dogs(res):  # nothing about dogs in the combined search: one dogs-only search
        try:
            check_dogs(search("%s %s dogs allowed" % (name, city), n=4))
        except Exception:
            pass
    v["verified"] = verified
    v["source"] = sources[0] if sources else (res[0]["url"] if res else v.get("source", ""))
    return v


def verify_all(venues, category, workers=None, city="San Francisco"):
    workers = workers or (12 if _keyed() else 3)  # PACER keeps the request rate legal
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda v: verify(v, category, city), venues))
