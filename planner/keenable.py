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
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

KEYED_API = "https://api.keenable.ai/v1/search"
PUBLIC_API = "https://api.keenable.ai/v1/search/public"
TITLE = "group-day-planner"
CACHE_FILE = os.path.join(os.environ.get("PLANNER_CACHE_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"), "keenable-cache.json")
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
            with PACER, urllib.request.urlopen(req, timeout=CALL_TIMEOUT_S) as r:
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
    if category in ("beach", "waterfront") and re.search(OFF_LEASH, t):
        return "offleash"
    if re.search(DOG_OK, t):
        return "leash" if category in ("beach", "museum", "waterfront", "park") else "patio"
    return None


KEYED_FETCH = "https://api.keenable.ai/v1/fetch"

FACTS_PROMPT = ("About {name} in {city}: answer ONLY from this page, in exactly these four lines.\n"
                "same_place: yes if this page is about {name} in {city}, otherwise no\n"
                "saturday_hours: HH:MM-HH:MM in 24-hour time, or closed, or unknown\n"
                "dogs: inside, patio, leash, offleash, no, or unknown\n"
                "vegetarian: yes, no, or unknown")


def fetch_facts(url, name, city="San Francisco"):
    """Keenable's fetch with a prompt: it reads the page and answers our question (keyed only)."""
    key = os.environ.get("KEENABLE_API_KEY", "").strip()
    if not key:
        return None
    q = urllib.parse.urlencode({"url": url, "prompt": FACTS_PROMPT.format(name=name, city=city)})
    req = urllib.request.Request(KEYED_FETCH + "?" + q, headers={"X-API-Key": key, "X-Keenable-Title": TITLE})
    for attempt in range(3):
        try:
            with PACER, urllib.request.urlopen(req, timeout=CALL_TIMEOUT_S) as r:
                return json.load(r).get("content", "")
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 2:
                raise
            time.sleep(2 ** attempt)


def parse_facts(text):
    """Read the three-line answer: {"hours": ("HH:MM","HH:MM") | "closed" | None,
    "dogs": "inside|patio|leash|offleash|no" | None, "vegetarian": True | False | None}."""
    out = {"same_place": None, "hours": None, "dogs": None, "vegetarian": None}
    for line in (text or "").splitlines():
        k, _, v = line.partition(":")
        k, v = k.strip().lower().replace(" ", "_"), v.strip().lower()
        if k.startswith("same"):
            out["same_place"] = v.startswith("yes")
        elif k.startswith("saturday"):
            m = re.search(r"(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})", v)
            if m:
                out["hours"] = ("%02d:%s" % (int(m.group(1)), m.group(2)), "%02d:%s" % (int(m.group(3)), m.group(4)))
            elif "closed" in v:
                out["hours"] = "closed"
            else:  # "6:30am-8:00pm" style, if the model ignored the 24-hour format
                out["hours"] = saturday_hours("Sat " + v)
        elif k.startswith("dog"):
            word = re.search(r"\b(inside|patio|offleash|off-leash|leash|no)\b", v)
            if word and "unknown" not in v:
                out["dogs"] = word.group(1).replace("off-leash", "offleash")
        elif k.startswith("veg"):
            if v.startswith("yes"):
                out["vegetarian"] = True
            elif v.startswith("no"):
                out["vegetarian"] = False
    return out


def own_site(url, name):
    """True if the site's registered domain (e.g. ritualcoffee.com) carries the venue's name."""
    host = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    domain = ".".join(host.split(".")[-2:])
    tokens = [t for t in re.findall(r"[a-z]{4,}", name.lower()) if t not in ("coffee", "park", "museum", "cafe", "beach")]
    return any(t in domain.replace("-", "") for t in tokens)


def best_page(results, name):
    """The result most likely to be the venue's own page: its name in the title or domain."""
    tokens = [t for t in re.findall(r"[a-z]{4,}", name.lower()) if t not in ("coffee", "park", "museum", "cafe", "beach")]
    for r in results:
        host = urllib.parse.urlparse(r.get("url", "")).netloc.lower()
        if any(t in host for t in tokens):
            return r
    for r in results:
        if name.lower().split(" (")[0] in (r.get("title") or "").lower():
            return r
    return results[0] if results else None


def verify(venue, category, city="San Francisco"):
    """Check one venue (cached for a day). With a key: find its page with a search, then ask a
    prompted fetch for hours, dogs and vegetarian options. Snippet matching fills what's left."""
    ck = "v2|%s|%s|%s" % (city, category, venue["name"])
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
    outdoors = category in ("beach", "waterfront", "park") or v.get("kind") == "outdoor_alternative"
    # 1. The venue's own page, read by Keenable with our question (prompted fetch).
    page = best_page(res, v["name"])
    if page and _keyed():
        try:
            facts = parse_facts(fetch_facts(page["url"], v["name"], city))
            v["facts_source"] = page["url"]
            if facts["same_place"] is not True:  # a page about another place (or unsure): ignore it
                v["facts_note"] = "page not about this venue: %s" % page["url"]
                facts = {"hours": None, "dogs": None, "vegetarian": None}
            if facts["hours"] == "closed" and not outdoors:
                # "Closed" drops the venue, so only the venue's own site may say it (its domain
                # carries the name); aggregator pages are often out of date.
                if own_site(page["url"], v["name"]):
                    v["open"], v["close"] = "00:00", "00:00"
                    verified.append("hours (own site: closed)")
                else:
                    v["facts_note"] = "a third-party page says closed on Saturday; not trusted: %s" % page["url"]
            elif facts["hours"] and not outdoors:
                v["open"], v["close"] = facts["hours"]
                if v["close"] <= v["open"]:
                    v["close"] = "23:59"
                verified.append("hours (page)")
            if facts["dogs"]:
                # Read from the page by a model: trusted both ways, unlike keyword matching.
                v["dogs"] = {"inside": "patio" if category in ("cafe", "food") else "leash"}.get(facts["dogs"], facts["dogs"])
                if category in ("cafe", "food") and v["dogs"] in ("leash", "offleash"):
                    v["dogs"] = "patio"
                verified.append("dogs (page)")
            if facts["vegetarian"] is not None and category in ("cafe", "food"):
                v["vegetarian_ok"] = facts["vegetarian"]
                verified.append("vegetarian (page)")
            sources.append(page["url"])
        except Exception as e:
            v["facts_error"] = str(e)[:100]
    # 2. Snippet matching for whatever the page didn't answer.
    if not outdoors and not any(x.startswith("hours") for x in verified):
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

    if any(x.startswith("dogs") for x in verified):
        pass  # the page already answered
    elif not check_dogs(res):  # nothing about dogs in the combined search: one dogs-only search
        try:
            check_dogs(search("%s %s dogs allowed" % (name, city), n=4))
        except Exception:
            pass
    v["verified"] = verified
    v["source"] = sources[0] if sources else (res[0]["url"] if res else v.get("source", ""))
    return v


CALL_TIMEOUT_S = 10  # one Keenable search or prompted fetch; a hung page shouldn't stall the plan
STEP_DEADLINE_S = float(os.environ.get("KEENABLE_DEADLINE_S", "15"))  # one whole search step


def verify_all(venues, category, workers=None, city="San Francisco", deadline_s=None):
    """Check every venue in parallel, but stop waiting after the step's deadline: a venue not
    checked in time keeps its OpenStreetMap/seed facts with a note. Its check keeps running in
    the background and is cached, so the next request gets it instantly."""
    from concurrent.futures import wait
    workers = workers or (12 if _keyed() else 3)  # PACER keeps the request rate legal
    deadline_s = STEP_DEADLINE_S if deadline_s is None else deadline_s
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = [pool.submit(verify, v, category, city) for v in venues]
    wait(futures, timeout=deadline_s)
    out = []
    for v, f in zip(venues, futures):
        if f.done() and not f.exception():
            out.append(f.result())
        else:
            out.append(dict(v, verify_error="not checked within %gs" % deadline_s))
    pool.shutdown(wait=False)  # don't block on the stragglers
    return out
