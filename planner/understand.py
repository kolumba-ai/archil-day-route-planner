"""Read a planning request with Claude Haiku 5.5, checked against the message, with the
rule-based parser (planner/request.py) as fallback.

Claude handles the many ways people phrase things; the rules are instant, free, work
offline and give the same answer every time (tests use them: PLANNER_PARSER=rules).
Every field Claude returns is checked, and a field that fails falls back to the rules:
- a start place or city must actually appear in the message (no invented places),
- times must be real clock times, the walking limit a sensible distance,
- stops must be known stop types.
"""
import json
import os
import re

from . import request as rules

MODEL = "claude-haiku-5-5"  # small and fast: this runs on every message

SYSTEM = """You read one message asking to plan a day out and fill in the fields of a JSON object.

Rules:
- Use only what the message says. Never invent a place, time, limit or stop.
- "city": the city named in the message, as written (e.g. "Portland Oregon"). Empty string if none.
- "start_place": the starting point exactly as written in the message, without the city (e.g. from "starting at St Stephens Catholic Church in Portland" -> "St Stephens Catholic Church"). It may be a street address. If the message gives several starting points, use the last one (later text corrects earlier text). Empty string if none.
- "start_time" / "end_time": 24-hour "HH:MM". end_time is the latest time to finish ("back by 3pm", "finish no later than 3pm"). Empty string if not given.
- "max_walk_km": the walking limit in kilometers (convert miles: x 1.609). 0 if none.
- "day": the weekday, e.g. "Saturday". Empty string if none.
- "stops": the kinds of places to visit, from: cafe (coffee shop), park (park or garden), museum (museum or gallery), food (restaurant, lunch, burritos...), beach. The starting place is not a stop. In the order mentioned.
- "avoid_hills" / "prefer_hills": true only if the message says so ("avoid hills", "flat" / "I want hills", "a good climb").
- "with_ana": true if Ana is coming. "dog": true if a dog (Biscuit) is coming."""

SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "start_place": {"type": "string"},
        "start_time": {"type": "string"},
        "end_time": {"type": "string"},
        "max_walk_km": {"type": "number"},
        "day": {"type": "string"},
        "stops": {"type": "array", "items": {"type": "string", "enum": ["cafe", "park", "museum", "food", "beach"]}},
        "avoid_hills": {"type": "boolean"},
        "prefer_hills": {"type": "boolean"},
        "with_ana": {"type": "boolean"},
        "dog": {"type": "boolean"},
    },
    "required": ["city", "start_place", "start_time", "end_time", "max_walk_km", "day", "stops",
                 "avoid_hills", "prefer_hills", "with_ana", "dog"],
    "additionalProperties": False,
}


def available():
    if os.environ.get("PLANNER_PARSER") == "rules" or not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def ask_claude(text):
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM,
        messages=[{"role": "user", "content": text}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        timeout=20,
    )
    if resp.stop_reason == "refusal":  # Haiku has no server-side fallback: use the rules
        raise RuntimeError("Claude declined to parse the message")
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower().replace("’", "'")).strip()


def _clock(s):
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", (s or "").strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def merge(text, ai, base):
    """Claude's fields where they pass the checks, the rules' fields otherwise."""
    out, used = dict(base), []
    msg = _norm(text)

    def take(field, value):
        out[field] = value
        used.append(field)

    city = (ai.get("city") or "").strip()
    if city and _norm(city) in msg:
        take("city", None if re.fullmatch(r"(?i)(san francisco|sf)(,? ca(lifornia)?)?", city) else city)
    place = (ai.get("start_place") or "").strip()
    if place and _norm(place) in msg and len(place) > 2:
        take("start_place", place)
    for field, key in (("start_min", "start_time"), ("end_min", "end_time")):
        t = _clock(ai.get(key))
        if t is not None:
            take(field, t)
    km = ai.get("max_walk_km") or 0
    if 0.2 <= km <= 50:
        take("max_walk_km", round(float(km), 2))
    day = (ai.get("day") or "").strip().capitalize()
    if day.lower() in rules.DAYS:
        take("day", day)
    stops = [s for s in ai.get("stops") or [] if s in ("cafe", "park", "museum", "food", "beach")]
    if stops:
        take("categories", list(dict.fromkeys(stops)))
    if ai.get("avoid_hills") or ai.get("prefer_hills"):
        take("avoid_hills", bool(ai.get("avoid_hills")) and not ai.get("prefer_hills"))
        take("prefer_hills", bool(ai.get("prefer_hills")) and not ai.get("avoid_hills"))
    take("with", ["ana"] if ai.get("with_ana") else [])
    take("dog", bool(ai.get("dog")))
    out["parsed_by"] = "claude" if used else "rules"
    out["claude_fields"] = used
    return out


def understand(text):
    """The request as a dict (same fields as request.parse), read by Claude when available."""
    base = rules.parse(text)
    if not available():
        return dict(base, parsed_by="rules", claude_fields=[])
    try:
        return merge(text, ask_claude(text), base)
    except Exception as e:  # no key, network, refusal, bad JSON: the rules answer
        print("Claude parsing failed, using the rules: %r" % e)
        return dict(base, parsed_by="rules", claude_fields=[], parse_error=str(e)[:120])
