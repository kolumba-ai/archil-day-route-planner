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

# Cuisines people ask for, mapped to OpenStreetMap's cuisine values.
CUISINES = {
    "korean": "korean", "japanese": "japanese", "sushi": "sushi", "ramen": "ramen", "mexican": "mexican",
    "tacos?": "mexican", "burritos?": "mexican", "thai": "thai", "chinese": "chinese", "italian": "italian",
    "pizza": "pizza", "indian": "indian", "vietnamese": "vietnamese", "pho": "vietnamese", "vegan": "vegan",
    "vegetarian food": "vegetarian", "burgers?": "burger", "bbq|barbecue": "barbecue", "mediterranean": "mediterranean",
    "greek": "greek", "french": "french", "ethiopian": "ethiopian", "seafood": "seafood",
}
FOOD_TRUCK = r"\b(food (trucks?|carts?|pods?)|street food)\b"
RETURN_TO_START = (r"\b(back (at|to) (the )?(place of departure|start(ing point)?|church|where we (started|began))|"
                   r"return(ing)? to (the )?(start|starting point|place of departure)|round[- ]trip|loop back)\b")
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

AVOID_HILLS = r"\b(avoid(ing)? (the )?hills?|no hills|without hills|flat (route|walk)|not hilly|stay(ing)? flat|keep it flat)\b"
PREFER_HILLS = (r"\b((with|some|lots of|plenty of|want|like|love|prefer|include|including) (some )?hills?|hilly|"
                r"uphill|a (good )?climb|climbing|workout|steep)\b")

TIME = r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)"
# A sentence-ending period ends the place, but not the one in "St." / "Mt." / "Ave." / "Dr.".
STOP_WORDS = (r"(?:[,;:!?]|(?<!\bSt)(?<!\bMt)(?<!\bAve)(?<!\bDr)\.(?:\s|$)|"
              r"\s+(?:and|with|at|in|start(?:ing)?|walk(?:ing)?|visit(?:ing)?|from|on)\b|$)")


def _clock(h, m, ap):
    h = int(h) % 12 + (12 if ap.lower().startswith("p") else 0)
    return h * 60 + int(m or 0)


END_TIME = (r"(?:finish(?:ing)?(?: time)?|end(?:ing)?(?: time)?|done|back|home|wrap(?:ping)? up|be finished)"
            r"(?:\s+(?:is|must be|should be|by|at|before))*\s*(?:no later than|not later than|by|before|at)?\s*")
IS_TIME = r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?|noon|midday"


def parse(text):
    low = text.lower()
    # Finish time: "finish no later than 3pm", "back by 4 pm", "end time is 15:00". Removed from
    # the text before reading the start time, so "finish at 3pm" isn't taken as the start.
    end_min = None
    low_start = low
    for rx in (r"\b(?:back|return\w*)\b[^.!?]{0,60}?\b(?:by|before|no later than|not later than)\s*" + TIME,
               END_TIME + TIME, r"(?:no later than|not later than)\s*" + TIME):
        for m in list(re.finditer(rx, low_start)):  # every finish phrase; the earliest time wins
            t = _clock(*m.groups()[-3:])
            end_min = t if end_min is None else min(end_min, t)
        low_start = re.sub(rx, " ", low_start)

    # Start time: "start at 10am", "start time is 9:30 am", "from 11am"; default 10:00.
    start_min = 10 * 60
    m = re.search(r"(?:start(?:ing)?(?: time)?(?:\s+is)?|leave|leaving|from|at)\s+(?:at\s+)?" + TIME, low_start)
    if m:
        start_min = _clock(*m.groups())

    # Starting place: "starting at X" / "start from X", where X is not a time. The LAST one
    # wins, so a reply to "Where should we start from?" replaces the earlier one. It stops
    # at " in <city>": "St Stephens Catholic Church in Portland" -> "St Stephens Catholic Church".
    start_place = None
    for m in re.finditer(r"\bstart(?:ing)?\s+(?:at|from)\s+(.+?)" + STOP_WORDS, text, re.I):
        cand = m.group(1).strip()
        if len(cand) > 2 and not re.fullmatch(IS_TIME, cand.lower()):
            start_place = cand

    # City: "in Portland Oregon", "in New York City". The first capitalized phrase after "in".
    city = None
    m = re.search(r"\bin\s+((?:[A-Z][\w.'-]*\s?)+(?:,\s*[A-Z][\w.'-]*)?)", text)
    if m:
        city = re.sub(r"\s+(Start|Starting|Walk|With|And)$", "", m.group(1).strip(" ,."))
    if not city:  # a street address with its city: "4055 SE Salmon St, Portland OR 97214"
        m = re.search(r",\s*((?:[A-Z][a-z][\w.'-]*\s?)+?),?\s+([A-Z][A-Za-z])\b\.?(?:\s*\d{5}(?:-\d{4})?)?\s*(?:[.!?]|$)", text)
        if m:
            city = "%s %s" % (m.group(1).strip(), m.group(2).upper())
    if city and re.fullmatch(r"(?i)(san francisco|sf)(,? ca(lifornia)?)?", city):
        city = None  # the built-in demo city

    # Walking limit: "no more than 2km", "at most 1.5 miles", "under 3 km".
    max_walk_km = None
    m = re.search(r"(?:(?:no|not)(?:\s+\w+){0,3}?\s+more than|at most|max(?:imum)?(?: of)?|under|less than|up to|within|"
                  r"limit(?: of)?|walk(?:ing)?)\s*(\d+(?:\.\d+)?)\s*(km|kilomet\w*|mi\b|miles?)", low)
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
        "end_min": end_min,  # latest time to finish, or None
        "cuisines": [c for w, c in CUISINES.items() if re.search(r"\b(%s)\b" % w, low)],
        "food_truck": bool(re.search(FOOD_TRUCK, low)),
        "return_to_start": bool(re.search(RETURN_TO_START, low)),
        "max_walk_km": max_walk_km,
        "avoid_hills": bool(re.search(AVOID_HILLS, low)),
        "prefer_hills": bool(re.search(PREFER_HILLS, low)) and not re.search(AVOID_HILLS, low),
        "categories": categories,
        "with": ["ana"] if re.search(r"\bana\b", low) else [],
        "dog": bool(re.search(r"\b(dog|biscuit)\b", low)),
    }
