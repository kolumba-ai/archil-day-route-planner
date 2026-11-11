"""Read what people type in the chat box with OpenAI (or Claude Haiku 5.5), checked against
the message, with the rule-based parser (planner/request.py) as fallback.

Which model reads the text: PLANNER_PARSER=openai | claude | rules. By default OpenAI when
OPENAI_API_KEY is set (model OPENAI_MODEL, default gpt-5.4-mini), else Claude when
ANTHROPIC_API_KEY is set, else the rules. Besides the planning request, OpenAI also reads
replies: the answer to "Where should we start from?" and picks like "the second one".

The model handles the many ways people phrase things; the rules are instant, free, work
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
- "with_ana": true if Ana is coming. "dog": true if a dog (Biscuit) is coming.
- "cuisines": cuisines asked for, lowercase English adjectives (e.g. "korean", "japanese", "mexican", "thai", "vegan"). Empty if none.
- "food_truck": true if they ask for a food truck, food cart or street food.
- "return_to_start": true if they must end back where they started ("back at the place of departure by 3pm")."""

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
        "cuisines": {"type": "array", "items": {"type": "string"}},
        "food_truck": {"type": "boolean"},
        "return_to_start": {"type": "boolean"},
    },
    "required": ["city", "start_place", "start_time", "end_time", "max_walk_km", "day", "stops",
                 "avoid_hills", "prefer_hills", "with_ana", "dog", "cuisines", "food_truck", "return_to_start"],
    "additionalProperties": False,
}


OPENAI_URL = "https://api.openai.com/v1/chat/completions"
LABEL = {"openai": "OpenAI", "claude": "Claude Haiku 5.5", "rules": "rules"}


def openai_model():
    return os.environ.get("OPENAI_MODEL", "").strip() or "gpt-5.4-mini"


def provider():
    """"openai", "claude" or "rules": who reads the chat text."""
    want = os.environ.get("PLANNER_PARSER", "").strip().lower()
    has_openai = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    has_claude = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    if want == "rules":
        return "rules"
    if want == "claude":
        return "claude" if has_claude else "rules"
    if want == "openai":
        return "openai" if has_openai else "rules"
    return "openai" if has_openai else "claude" if has_claude else "rules"


def available():
    return provider() != "rules"


def ask_openai(system, text, schema, name="fields", timeout=20):
    """One Chat Completions call with a strict JSON schema; returns the parsed object.
    Plain HTTP (no SDK to install). The key is sent only to api.openai.com, never printed."""
    import urllib.request
    body = {
        "model": openai_model(),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
        "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
        "reasoning_effort": os.environ.get("OPENAI_REASONING_EFFORT", "low"),
    }
    req = urllib.request.Request(OPENAI_URL, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": "Bearer " + os.environ["OPENAI_API_KEY"].strip(), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        msg = json.load(r)["choices"][0]["message"]
    if msg.get("refusal"):
        raise RuntimeError("OpenAI declined to read the message")
    return json.loads(msg["content"])


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


def merge(text, ai, base, who="claude"):
    """The model's fields where they pass the checks, the rules' fields otherwise."""
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
    known = set(rules.CUISINES)
    cuisines = [c.strip().lower() for c in ai.get("cuisines") or [] if c.strip().lower() in known]
    if cuisines:
        take("cuisines", list(dict.fromkeys(cuisines)))
    if ai.get("food_truck") and re.search(r"truck|cart|street food|pod", msg):
        take("food_truck", True)
    if ai.get("return_to_start"):
        take("return_to_start", True)
    out["parsed_by"] = who if used else "rules"
    out["claude_fields"] = used  # the fields the model supplied (name kept for the web page)
    out["model_fields"] = used
    return out


def understand(text):
    """The request as a dict (same fields as request.parse), read by OpenAI or Claude when available."""
    base = rules.parse(text)
    who = provider()
    if who == "rules":
        return dict(base, parsed_by="rules", claude_fields=[], model_fields=[])
    try:
        ai = ask_openai(SYSTEM, text, SCHEMA, "day_plan_request") if who == "openai" else ask_claude(text)
        out = merge(text, ai, base, who)
        if who == "openai":
            out["parser_model"] = openai_model()
        return out
    except Exception as e:  # network, refusal, bad JSON: the rules answer
        print("%s parsing failed, using the rules: %r" % (LABEL[who], e))
        return dict(base, parsed_by="rules", claude_fields=[], model_fields=[], parse_error=str(e)[:120])


REPLY_SYSTEM = """Someone is replying in a group chat about a day plan. Read their reply.

- If they were asked where to start from: "start_place" is the place or street address they give, copied exactly as written (without filler like "let's start at"). Empty string if they give none.
- If they were asked to choose one of the numbered plans: "plan" is the plan number they choose (e.g. "the second one" -> 2, "plan three" -> 3, "the first works" -> 1). 0 if they don't clearly choose one."""

REPLY_SCHEMA = {"type": "object", "properties": {"start_place": {"type": "string"}, "plan": {"type": "integer"}},
                "required": ["start_place", "plan"], "additionalProperties": False}


def read_reply(text, asked):
    """OpenAI reads a short reply. asked: "start" or "pick (1-3)". Returns the dict or None.
    The start place must appear in the reply; the plan number is range-checked by the caller."""
    if provider() != "openai":
        return None
    try:
        ai = ask_openai(REPLY_SYSTEM, "They were asked: %s\nTheir reply: %s" % (asked, text), REPLY_SCHEMA, "reply", timeout=10)
    except Exception as e:
        print("OpenAI reply reading failed, using the rules: %r" % e)
        return None
    place = (ai.get("start_place") or "").strip()
    if place and _norm(place) not in _norm(text):
        ai["start_place"] = ""
    return ai
