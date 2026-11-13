"""Read what people type in the chat box with OpenAI (or Claude Haiku 5.5), checked against
the message, with the rule-based parser (planner/request.py) as fallback.

Which model reads the text: PLANNER_PARSER=claude | openai | rules. By default Claude
(claude-haiku-5-5, CLAUDE_PARSER_MODEL) when ANTHROPIC_API_KEY is set, else OpenAI when
OPENAI_API_KEY is set (OPENAI_MODEL, default gpt-5.4-mini), else the rules. Besides the planning
request, the model also reads replies: the answer to "Where should we start from?" and picks
like "the second one".

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
- "stops": the kinds of places to visit, in the exact order the message names them (the day follows that order), from: stroll (a walk along a named street), cafe (coffee shop), park (park or garden), museum (museum or gallery), food (restaurant, lunch, burritos...), beach (beach or ocean), waterfront (riverside, waterfront, river walk, esplanade, lakefront, pier, harbor). "Riverside" is waterfront, not park.
- "stroll_street": for "a stroll down Hawthorne Street" / "walk along Valencia", the street name as written ("Hawthorne Street"); add "stroll" to "stops" in its place. Empty string if none.
- "stop_near": a stop the message ties to a street, neighborhood or landmark ("burritos near Hawthorne" -> {"stop": "food", "near": "Hawthorne"}; "coffee on Alberta Street" -> {"stop": "cafe", "near": "Alberta Street"}; "a park in Sellwood" -> {"stop": "park", "near": "Sellwood"}), the place as written. The stop must be in "stops". Empty if none. These are planned, so never put them in "not_supported".
- "named_stops": a stop the message names as a specific place by its proper name (not a description like "a nice restaurant") ("then go to Mt Tabor Park" -> {"stop": "park", "name": "Mt Tabor Park"}; "lunch at Pok Pok" -> {"stop": "food", "name": "Pok Pok"}), with the stop also in "stops". Empty if none. These are planned, so never put them in "not_supported".
- "not_supported": things the message asks for that none of the stop types or fields above can hold (e.g. "see the cherry blossoms", "a boat tour"), each as a short phrase from the message. Empty if everything fits.
- "sunset_stop": the stop the message wants at sunset ("riverside at sunset" -> "waterfront", "beach at sunset" -> "beach"). Must be one of "stops". Empty string if the message doesn't tie a stop to sunset. The starting place is not a stop. In the order mentioned.
- "avoid_hills" / "prefer_hills": true only if the message says so ("avoid hills", "flat" / "I want hills", "a good climb").
- Later text corrects earlier text: if the message ends with a correction ("make it riverside, not a park"), apply it.
- "with_ana": true if Ana is coming. "dog": true if a dog (Biscuit) is coming.
- "cuisines": cuisines asked for, lowercase English adjectives (e.g. "korean", "japanese", "mexican", "thai", "vegan"), including the cuisine a dish implies: burritos or tacos -> "mexican", sushi or ramen -> "japanese", pho -> "vietnamese", pad thai -> "thai", pizza -> "pizza", burgers -> "burger". Empty if none.
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
        "stops": {"type": "array", "items": {"type": "string", "enum": ["cafe", "park", "museum", "food", "beach", "waterfront", "stroll"]}},
        "sunset_stop": {"type": "string", "enum": ["", "cafe", "park", "museum", "food", "beach", "waterfront", "stroll"]},
        "stroll_street": {"type": "string"},
        "stop_near": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                  "required": ["stop", "near"],
                                                  "properties": {"stop": {"type": "string", "enum": ["cafe", "park", "museum", "food", "beach", "waterfront"]},
                                                                 "near": {"type": "string"}}}},
        "named_stops": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                    "required": ["stop", "name"],
                                                    "properties": {"stop": {"type": "string", "enum": ["cafe", "park", "museum", "food", "beach", "waterfront"]},
                                                                   "name": {"type": "string"}}}},
        "not_supported": {"type": "array", "items": {"type": "string"}},
        "avoid_hills": {"type": "boolean"},
        "prefer_hills": {"type": "boolean"},
        "with_ana": {"type": "boolean"},
        "dog": {"type": "boolean"},
        "cuisines": {"type": "array", "items": {"type": "string"}},
        "food_truck": {"type": "boolean"},
        "return_to_start": {"type": "boolean"},
    },
    "required": ["city", "start_place", "start_time", "end_time", "max_walk_km", "day", "stops",
                 "avoid_hills", "prefer_hills", "with_ana", "dog", "sunset_stop", "stroll_street", "stop_near", "named_stops", "not_supported", "cuisines", "food_truck", "return_to_start"],
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
    return "claude" if has_claude else "openai" if has_openai else "rules"


def claude_model():
    return os.environ.get("CLAUDE_PARSER_MODEL", "").strip() or MODEL


def model_name(who):
    return {"claude": claude_model(), "openai": openai_model()}.get(who, "rules")


def available():
    return provider() != "rules"


class NoLLM(RuntimeError):
    """No language model is connected: the request is not read at all."""


class LLMFailed(RuntimeError):
    """The language model was connected but couldn't read the request."""


def rules_mode():
    """Rule-based reading only when explicitly asked for (the tests, for repeatable runs)."""
    return os.environ.get("PLANNER_PARSER", "").strip().lower() == "rules"


def blank(text):
    """What a request means before the model fills anything in."""
    return {"text": text, "day": "Saturday", "city": None, "start_place": None, "start_min": 10 * 60,
            "end_min": None, "cuisines": [], "food_truck": False, "return_to_start": False, "max_walk_km": None,
            "avoid_hills": False, "prefer_hills": False, "categories": list(rules.DEFAULT_CATEGORIES),
            "sunset_stop": None, "stroll_street": None, "stop_near": {}, "named_stops": {}, "not_supported": [], "with": [], "dog": False}


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


_client = None


def ask_claude(text, system=SYSTEM, schema=SCHEMA, timeout=20):
    """One Messages API call with a JSON schema; returns the parsed object. The SDK reads
    ANTHROPIC_API_KEY from the environment (.env); the key is never printed."""
    global _client
    import anthropic
    if _client is None:
        _client = anthropic.Anthropic(max_retries=1)
    resp = _client.messages.create(
        model=claude_model(),
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": text}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        timeout=timeout,
    )
    if resp.stop_reason == "refusal":  # Haiku has no server-side fallback: use the rules
        raise RuntimeError("Claude declined to parse the message")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Claude's answer was cut off")
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


def ask_model(who, system, text, schema, name, timeout=20):
    if who == "claude":
        return ask_claude(text, system, schema, timeout)
    return ask_openai(system, text, schema, name, timeout)


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
    street = (ai.get("stroll_street") or "").strip()
    if street and _norm(re.sub(r"(?i)\s+(street|st|avenue|ave|boulevard|blvd|road|rd)$", "", street)) in msg:
        take("stroll_street", street)
    stops = [s for s in ai.get("stops") or [] if s in ("cafe", "park", "museum", "food", "beach", "waterfront", "stroll")]
    if not out.get("stroll_street"):
        stops = [s for s in stops if s != "stroll"]  # a stroll needs its street
    near = {}
    for item in ai.get("stop_near") or []:
        where = (item.get("near") or "").strip()
        if item.get("stop") in stops and where and _norm(where) in msg:
            near.setdefault(item["stop"], where)
    if near:
        take("stop_near", near)
    named = {}
    for item in ai.get("named_stops") or []:
        name = (item.get("name") or "").strip()
        # A proper name ("Mt Tabor Park", "Pok Pok"), not a description ("a nice restaurant")
        proper = re.search(r"\b[A-Z0-9][\w'’.]*", re.sub(r"(?i)^(a|an|the|some)\s+", "", name))
        if item.get("stop") in stops and name and proper and _norm(name) in msg:
            named.setdefault(item["stop"], name)
    if named:
        take("named_stops", named)
    covered = [_norm(w) for w in list(near.values()) + list(named.values())]
    take("not_supported", [x.strip() for x in ai.get("not_supported") or [] if x.strip()
                           and not any(c and c in _norm(x) for c in covered)][:5])  # planned after all
    if stops:
        take("categories", list(dict.fromkeys(stops)))
        sun = ai.get("sunset_stop") or ""
        if sun in out["categories"]:
            take("sunset_stop", sun)
        elif out.get("sunset_stop") not in out["categories"]:
            take("sunset_stop", "beach" if "beach" in out["categories"] else None)
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
    """The request as a dict (same fields as request.parse), read by Claude (or OpenAI).

    No rule-based guessing in the product: with no model connected this raises NoLLM, and if
    the model fails it raises LLMFailed, so the agent says so and plans nothing. A field the
    model gets wrong (a place not in the message, an impossible time) is left empty, never
    filled by guesswork; a missing start then means asking. PLANNER_PARSER=rules (tests only)
    uses the rule-based reader for repeatable runs."""
    if rules_mode():
        return dict(rules.parse(text), parsed_by="rules", claude_fields=[], model_fields=[])
    who = provider()
    if who == "rules":
        raise NoLLM("no language model is connected")
    try:
        ai = ask_model(who, SYSTEM, text, SCHEMA, "day_plan_request")
    except Exception as e:
        raise LLMFailed("%s couldn't read the request: %s" % (LABEL[who], str(e)[:100]))
    out = merge(text, ai, blank(text), who)
    out["parser_model"] = model_name(who)
    return out


REPLY_SYSTEM = """Someone is replying in a group chat about a day plan. Read their reply.

- If they were asked where to start from: "start_place" is the place or street address they give, copied exactly as written (without filler like "let's start at"). Empty string if they give none.
- If they were asked to choose one of the numbered plans: "plan" is the plan number they choose (e.g. "the second one" -> 2, "plan three" -> 3, "the first works" -> 1). 0 if they don't clearly choose one."""

REPLY_SCHEMA = {"type": "object", "properties": {"start_place": {"type": "string"}, "plan": {"type": "integer"}},
                "required": ["start_place", "plan"], "additionalProperties": False}


def read_reply(text, asked):
    """The model reads a short reply. asked: the question they answer. Returns the dict or None.
    The start place must appear in the reply; the plan number is range-checked by the caller."""
    who = provider()
    if who == "rules":
        return None
    try:
        ai = ask_model(who, REPLY_SYSTEM, "They were asked: %s\nTheir reply: %s" % (asked, text), REPLY_SCHEMA,
                       "reply", timeout=10)
    except Exception as e:
        print("%s reply reading failed, using the rules: %r" % (LABEL[who], e))
        return None
    place = (ai.get("start_place") or "").strip()
    if place and _norm(place) not in _norm(text):
        ai["start_place"] = ""
    return ai


TURN_SYSTEM = """You read one chat message sent to a day-planning agent, given what came before. Decide what the person wants:
- "pick": they choose one of the plans offered; "plan" is its number ("the second one" -> 2, "let's do the Laurelhurst one" -> the plan with that name).
- "new_plan": they ask for a different or changed day out ("start again, ...", "instead let's ...", "make it a museum instead", "can we skip the park?", a whole new request). Write "request": one complete, standalone request in their words that the planner can read on its own. Fill in whatever they refer to from the earlier request with its value ("the same place" -> the earlier start place, "same people" -> who was coming) and keep the earlier day, city, people and dog unless the message changes them. Keep the stops in the order they give.
- "message": something meant for the other person in the group (a question or comment to them).
- "question": a question to the agent about the plans; put a short, helpful answer in "reply" using only what you were told.
Fill unused fields with 0 or an empty string."""

TURN_SCHEMA = {"type": "object", "additionalProperties": False,
               "required": ["intent", "plan", "request", "reply"],
               "properties": {"intent": {"type": "string", "enum": ["pick", "new_plan", "message", "question"]},
                              "plan": {"type": "integer"}, "request": {"type": "string"}, "reply": {"type": "string"}}}


def read_turn(text, earlier="", plans=(), who="Me"):
    """What a chat message means in context: a plan pick, a new or changed request (rewritten to
    stand on its own), a message for the other person, or a question. None without a model."""
    model = provider()
    if model == "rules" or rules_mode():
        return None
    ctx = ["Sender: %s" % ("the organizer" if who == "Me" else "Ana, the invited friend")]
    if earlier:
        ctx.append("Earlier request: %s" % earlier)
    if plans:
        ctx.append("Plans offered: " + "; ".join("%d. %s" % (i + 1, p) for i, p in enumerate(plans)))
    try:
        ai = ask_model(model, TURN_SYSTEM, "\n".join(ctx) + "\nTheir message: %s" % text, TURN_SCHEMA, "turn",
                       timeout=20)
    except Exception as e:
        print("%s couldn't read the message: %r" % (LABEL[model], e))
        return None
    if ai.get("intent") == "new_plan" and not (ai.get("request") or "").strip():
        ai["request"] = text
    return ai
