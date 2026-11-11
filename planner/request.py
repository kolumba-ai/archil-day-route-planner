"""Read a plain-English planning request: who, where, when, how far, and which stops.

Example: "Plan Saturday with Ana in Portland Oregon, starting at St Stephen's Catholic
Church. Start at 10am and walk for no more than 2km, visiting at least one park, one
coffee shop, and avoiding hills."
"""
import re

SF = "San Francisco"

# Stop types, the words that ask for them, and their order when nothing else decides it.
CATEGORIES = [
    ("cafe", r"\b(coffee|caf[eé]s?|espresso)\b"),
    ("park", r"\b(parks?|gardens?|green space)\b"),
    ("museum", r"\b(museums?|galler(y|ies)|exhibits?)\b"),
    ("food", r"\b(burritos?|lunch|dinner|food|tacos?|restaurants?|brunch|eat)\b"),
    ("beach", r"\b(beach(es)?|ocean|sunset)\b"),
]
DEFAULT_CATEGORIES = ["cafe", "museum", "food", "beach"]  # the San Francisco demo's day
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

AVOID_HILLS = r"\b(avoid(ing)? (the )?hills?|no hills|without hills|flat (route|walk)|not hilly|stay(ing)? flat|keep it flat)\b"
PREFER_HILLS = (r"\b((with|some|lots of|plenty of|want|like|love|prefer|include|including) (some )?hills?|hilly|"
                r"uphill|a (good )?climb|climbing|workout|steep)\b")

TIME = r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)"
STOP_WORDS = r"(?:[,.;:!?]|\s+(?:and|with|at|start(?:ing)?|walk(?:ing)?|visit(?:ing)?|from|on)\b|$)"


def _clock(h, m, ap):
    h = int(h) % 12 + (12 if ap.lower().startswith("p") else 0)
    return h * 60 + int(m or 0)


def parse(text):
    low = text.lower()
    # Start time: "start at 10am", "at 9:30 am", "from 11am"; default 10:00.
    start_min = 10 * 60
    m = re.search(r"(?:start(?:ing)?|leave|leaving|from|at)\s+(?:at\s+)?" + TIME, low)
    if m:
        start_min = _clock(*m.groups())

    # Starting place: "starting at X" / "start from X", where X is not a time.
    start_place = None
    for m in re.finditer(r"\bstart(?:ing)?\s+(?:at|from)\s+(.+?)" + STOP_WORDS, text, re.I):
        cand = m.group(1).strip()
        if not re.match(r"^\d", cand) and len(cand) > 2:
            start_place = cand
            break

    # City: "in Portland Oregon", "in New York City". The first capitalized phrase after "in".
    city = None
    m = re.search(r"\bin\s+((?:[A-Z][\w.'-]*\s?)+(?:,\s*[A-Z][\w.'-]*)?)", text)
    if m:
        city = re.sub(r"\s+(Start|Starting|Walk|With|And)$", "", m.group(1).strip(" ,."))
    if city and re.fullmatch(r"(?i)(san francisco|sf)(,? ca(lifornia)?)?", city):
        city = None  # the built-in demo city

    # Walking limit: "no more than 2km", "at most 1.5 miles", "under 3 km".
    max_walk_km = None
    m = re.search(r"(?:no more than|not more than|at most|max(?:imum)?(?: of)?|under|less than|up to|within)\s*"
                  r"(\d+(?:\.\d+)?)\s*(km|kilomet\w*|mi\b|miles?)", low)
    if m:
        v = float(m.group(1))
        max_walk_km = round(v * 1.609, 2) if m.group(2).startswith("mi") else v

    # Stop types, in the order mentioned. The starting place and city don't count:
    # "starting at Millennium Park" is not a request to visit a park.
    stops_text = low
    for name in (start_place, city):
        if name:
            stops_text = stops_text.replace(name.lower(), " ")
    found = []
    for cat, rx in CATEGORIES:
        m = re.search(rx, stops_text)
        if m:
            found.append((m.start(), cat))
    categories = [c for _, c in sorted(found)] or list(DEFAULT_CATEGORIES)

    day = next((d.capitalize() for d in DAYS if d in low), "Saturday")
    return {
        "text": text,
        "day": day,
        "city": city,  # None = San Francisco demo
        "start_place": start_place,
        "start_min": start_min,
        "max_walk_km": max_walk_km,
        "avoid_hills": bool(re.search(AVOID_HILLS, low)),
        "prefer_hills": bool(re.search(PREFER_HILLS, low)) and not re.search(AVOID_HILLS, low),
        "categories": categories,
        "with": ["ana"] if re.search(r"\bana\b", low) else [],
        "dog": bool(re.search(r"\b(dog|biscuit)\b", low)),
    }
