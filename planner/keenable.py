"""Keenable search: check and correct each candidate's hours, location and dog policy.

Keenable returns ranked web results (title, url, snippet). Turning a whole page into a
venue record would need an LLM; here we parse the facts we need from snippets:
- Saturday opening hours ("Fri – Sat 6:30AM - 8:00PM", "6:30am–8:00pm Friday–Sunday")
- map coordinates (map links carry "coordinate=lat,lon")
- dog policy keywords ("dog-friendly", "no dogs", ...)

Uses the keyless public endpoint (1,000 requests/hour per IP) unless KEENABLE_API_KEY is set.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "https://api.keenable.ai/v1/search/public"
TITLE = "group-day-planner"

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_RE = r"(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?"
TIME_RE = r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?"
RANGE_RE = TIME_RE + r"\s*(?:–|-|to)\s*" + TIME_RE


IN_FLIGHT = threading.Semaphore(int(os.environ.get("KEENABLE_CONCURRENCY", "3")))


def search(query, n=5, tries=4):
    headers = {"Content-Type": "application/json", "X-Keenable-Title": TITLE}
    key = os.environ.get("KEENABLE_API_KEY")
    if key:
        headers["X-API-Key"] = key
    body = json.dumps({"query": query, "max_results": n}).encode()
    for attempt in range(tries):
        req = urllib.request.Request(API, data=body, headers=headers, method="POST")
        try:
            with IN_FLIGHT, urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r).get("results", [])
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == tries - 1:
                raise
            wait = e.headers.get("Retry-After")
            time.sleep(min(float(wait) if wait and wait.isdigit() else 2 ** attempt, 10))


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


def coordinates(results):
    for r in results:
        m = re.search(r"coordinate=(-?\d+\.\d+),(-?\d+\.\d+)", r.get("url", ""))
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            if 37.70 < lat < 37.83 and -122.53 < lon < -122.35:
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


def verify(venue, category):
    """Check one venue with two searches; returns an updated copy and what was verified."""
    v = dict(venue)
    name = re.sub(r"\s*\(.*?\)", "", v["name"])  # "Ritual Coffee Roasters (Valencia)" -> name only
    area = re.search(r"\((.*?)\)", v["name"])
    where = (area.group(1) + " ") if area and "alternative" not in area.group(1) else ""
    verified, sources = [], []
    try:
        res = search("%s %sSan Francisco hours" % (name, where))
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
    c = coordinates(res)
    if c:
        v["lat"], v["lon"] = c
        verified.append("location")
    try:
        dres = search("%s San Francisco dogs allowed" % name, n=4)
        for r in dres:
            pol = dog_policy(r.get("title", "") + " " + r.get("snippet", ""), category)
            if pol:
                # Keyword matching can misread "Are dogs allowed? No", so a search result
                # may only make the policy stricter, never looser.
                if STRICTNESS[pol] < STRICTNESS.get(v.get("dogs", "no"), 0):
                    v["dogs"] = pol
                    v["dogs_note"] = "stricter policy found on the web"
                verified.append("dogs")
                sources.append(r["url"])
                break
    except Exception:
        pass
    v["verified"] = verified
    v["source"] = sources[0] if sources else (res[0]["url"] if res else v.get("source", ""))
    return v


def verify_all(venues, category, workers=6):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda v: verify(v, category), venues))
