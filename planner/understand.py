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
- "start_time" / "end_time": 24-hour "HH:MM", only when the message gives the time (never guess one). end_time is the latest time to finish ("back by 3pm", "finish no later than 3pm"). Empty string if not given.
- "max_walk_km": the walking limit in kilometers (convert miles: x 1.609). 0 if none.
- "day": the weekday, e.g. "Saturday". Empty string if none.
- "stops": the kinds of places to visit, in the exact order the message names them (the day follows that order), from: stroll (a walk along a named street), cafe (coffee shop), park (park or garden), museum (museum or gallery), food (restaurant, lunch, burritos...), beach (beach or ocean), waterfront (riverside, waterfront, river walk, esplanade, lakefront, pier, harbor). "Riverside" is waterfront, not park. A kind asked for more than once is listed each time: "a park, then coffee, then another park" -> ["park", "cafe", "park"]; "two parks" -> ["park", "park"]. A cuisine alone ("then Thai") is a food stop. "repeat_words": when a kind is listed more than once, the words of the message that ask for it again, copied exactly (e.g. "another park", "otro parque", "two parks"); empty string otherwise.
- "stroll_street": for "a stroll down Hawthorne Street" / "walk along Valencia", the street name as written ("Hawthorne Street"); add "stroll" to "stops" in its place. Empty string if none.
- "stop_near": a stop the message ties to a street, neighborhood or landmark ("burritos near Hawthorne" -> {"stop": "food", "near": "Hawthorne"}; "coffee on Alberta Street" -> {"stop": "cafe", "near": "Alberta Street"}; "a park in Sellwood" -> {"stop": "park", "near": "Sellwood"}), the place as written. The stop must be in "stops". Empty if none. These are planned, so never put them in "not_supported".
- "named_stops": a stop the message names as a specific place by its proper name (not a description like "a nice restaurant") ("then go to Mt Tabor Park" -> {"stop": "park", "name": "Mt Tabor Park"}; "lunch at Pok Pok" -> {"stop": "food", "name": "Pok Pok"}), with the stop also in "stops". Empty if none. These are planned, so never put them in "not_supported".
- "not_supported": things the message asks for that none of the stop types or fields above can hold (e.g. "see the cherry blossoms", "a boat tour"), each as a short phrase from the message. Empty if everything fits. Asking to plan again ("repeat the route", "plan it again", "try again", "redo it") is not a wish to leave out: it is what the planner does.
- "sunset_stop": the stop the message wants at sunset ("riverside at sunset" -> "waterfront", "beach at sunset" -> "beach"). Must be one of "stops". Empty string if the message doesn't tie a stop to sunset. The starting place is not a stop. In the order mentioned.
- "avoid_hills" / "prefer_hills": true only if the message says so ("avoid hills", "flat" / "I want hills", "a good climb").
- Later text corrects earlier text: if the message ends with a correction ("make it riverside, not a park"), apply it.
- The message may be in any language; read it the same way. "language": the message's language as a two-letter ISO 639-1 code ("en", "es", "fr"...). Places, stop types and cuisines are still given in English as listed here.
- "with_ana": true if Ana is coming. "dog": true if a dog (Biscuit) is coming.
- "cuisines": cuisines asked for, lowercase English adjectives (e.g. "korean", "japanese", "mexican", "thai", "vegan"), including the cuisine a dish implies: burritos or tacos -> "mexican", sushi or ramen -> "japanese", pho -> "vietnamese", pad thai -> "thai", pizza -> "pizza", burgers -> "burger". Empty if none.
- "food_truck": true if they ask for a food truck, food cart or street food. "food_truck_words": the words of the message that ask for it, copied exactly (e.g. "food cart", "carrito de comida"); empty string if none.
- "outdoor_seating": true if they want to eat or drink outside: outdoor, open, open-air, street or sidewalk seating, a patio, al fresco, sitting outside, a beer garden. A food cart pod counts as outdoor seating. This is planned, so never put it in "not_supported". "outdoor_seating_words": the words of the message that ask for it, copied exactly (e.g. "open street seating", "mesas en la terraza"); empty string if none.
- "min_kcal" / "min_steps" / "min_walk_km": a fitness goal for the walking ("burn at least 300 calories" -> min_kcal 300; "10,000 steps" -> min_steps 10000; "walk at least 5 km" -> min_walk_km 5, miles x 1.609). 0 if none. A goal is planned (with extra walking), so never put it in "not_supported".
- "return_to_start": true if they must end back where they started ("finally get back again to <the start place>", "back at the church by 3pm") ("back at the place of departure by 3pm")."""

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
        "outdoor_seating": {"type": "boolean"},
        "outdoor_seating_words": {"type": "string"},
        "food_truck_words": {"type": "string"},
        "repeat_words": {"type": "string"},
        "language": {"type": "string"},
        "min_kcal": {"type": "number"},
        "min_steps": {"type": "number"},
        "min_walk_km": {"type": "number"},
        "return_to_start": {"type": "boolean"},
    },
    "required": ["city", "start_place", "start_time", "end_time", "max_walk_km", "day", "stops",
                 "avoid_hills", "prefer_hills", "with_ana", "dog", "sunset_stop", "stroll_street", "stop_near", "named_stops", "not_supported", "cuisines", "food_truck", "outdoor_seating", "min_kcal", "min_steps", "min_walk_km", "return_to_start",
                 "outdoor_seating_words", "food_truck_words", "repeat_words", "language"],
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
    return {"text": text, "day": "Saturday", "city": None, "start_place": None, "start_min": None,
            "end_min": None, "cuisines": [], "food_truck": False, "outdoor_seating": False, "language": None, "min_kcal": None, "min_steps": None, "min_walk_km": None,
            "return_to_start": False, "max_walk_km": None,
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


def quoted(words, msg):
    """True if the model's quote (its evidence for a field) is really in the message: works in any
    language, unlike looking for English words."""
    q = _norm(words or "")
    return bool(q) and len(q) >= 3 and q in msg


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
    # A fitness goal must be a number the message says (300 calories, 10,000 steps, 5 km or 3 miles).
    digits = set(re.findall(r"\d+", text.replace(",", "")))
    for field, lo, hi in (("min_kcal", 20, 3000), ("min_steps", 500, 60000), ("min_walk_km", 0.2, 40)):
        v = ai.get(field) or 0
        said = str(int(v)) in digits or (field == "min_steps" and str(int(v // 1000)) in digits) or \
            (field == "min_walk_km" and str(int(round(v / 1.609))) in digits)
        if lo <= v <= hi and said:
            take(field, float(v))
    if out.get("min_walk_km") and out.get("max_walk_km") and out["min_walk_km"] >= out["max_walk_km"]:
        take("min_walk_km", None)  # "must not walk more than 3 km" is a limit, not a goal
    lang = (ai.get("language") or "").strip().lower()
    if re.fullmatch(r"[a-z]{2}", lang):
        take("language", lang)
    outdoor = bool(ai.get("outdoor_seating")) and (quoted(ai.get("outdoor_seating_words"), msg) or bool(
        re.search(rules.OUTDOOR_SEATING + "|" + rules.FOOD_TRUCK, text.lower())))
    if outdoor:
        take("outdoor_seating", True)
    covered = [_norm(w) for w in list(near.values()) + list(named.values())]
    if any(out.get(f) for f in ("min_kcal", "min_steps", "min_walk_km")):  # planned: not "left out"
        covered += ["calor", "kcal", "steps", "burn"]
    take("not_supported", [x.strip() for x in ai.get("not_supported") or [] if x.strip()
                           and not any(c and c in _norm(x) for c in covered)
                           and not (outdoor and re.search(rules.OUTDOOR_SEATING + r"|\bseat", x.lower()))
                           and not re.search(r"\b(repeat|redo|re-?plan|again|retry)\b", x.lower())][:5])
    if stops:
        # The same kind twice ("then another park") only when the message says so; otherwise
        # a repeat is the model listing one stop twice ("burritos for lunch").
        if not (quoted(ai.get("repeat_words"), msg)
                or re.search(r"\b(another|second|one more|a different|other|two|both|again)\b", text.lower())):
            stops = list(dict.fromkeys(stops))
        take("categories", stops[:6])
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
    if ai.get("food_truck") and (quoted(ai.get("food_truck_words"), msg) or re.search(r"truck|cart|street food|pod", msg)):
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
- If they were asked to choose one of the numbered plans: "plan" is the plan number they choose (e.g. "the second one" -> 2, "plan three" -> 3, "the first works" -> 1). 0 if they don't clearly choose one.
- If they were asked a yes-or-no question (to confirm, to join): "answer" is "yes" if they agree ("sí", "dale", "claro", "oui", "ok"), "no" if they decline, "other" if they say something else (a change, a question). Empty string otherwise.
- If they were asked what time to start: "start_time" is the time they give, 24-hour "HH:MM" ("a las diez" -> "10:00", "mediodía" -> "12:00"). Empty string if none.
The reply may be in any language."""

REPLY_SCHEMA = {"type": "object", "properties": {
    "start_place": {"type": "string"}, "plan": {"type": "integer"},
    "answer": {"type": "string", "enum": ["", "yes", "no", "other"]}, "start_time": {"type": "string"}},
    "required": ["start_place", "plan", "answer", "start_time"], "additionalProperties": False}


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
- "change": they keep the same day out and add or change a condition or a stop: a goal ("I want to burn at least 300 calories", "10,000 steps"), a limit ("no hills", "finish by 3pm", "start at 11"), a stop ("make it a museum instead", "skip the park"), often with "plan it again" or "repeat the route". The planner plans the earlier request again with their words added, so leave "request" empty.
- "new_plan": they ask for a different day out altogether ("start again, ...", "instead let's go to the beach on Sunday", a whole new request). Write "request": one complete, standalone request in their words that the planner can read on its own. Fill in whatever they refer to from the earlier request with its value ("the same place" -> the earlier start place, "same people" -> who was coming) and keep the earlier day, city, people and dog unless the message changes them. Keep the stops in the order they give.
- "message": something meant for the other person in the group (a question or comment to them).
- "question": a question to the agent about the plans; put a short, helpful answer in "reply" using only what you were told.
Fill unused fields with 0 or an empty string."""

TURN_SCHEMA = {"type": "object", "additionalProperties": False,
               "required": ["intent", "plan", "request", "reply"],
               "properties": {"intent": {"type": "string", "enum": ["pick", "change", "new_plan", "message", "question"]},
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


# ---------------------------------------------------------------- replying in their language

TRANSLATE_SYSTEM = """Translate a chat message from a day-planning assistant into the language given. Keep every emoji,
number, time, distance, line break and bullet exactly; keep the names of places, businesses, streets and people as
they are. Keep the tone friendly and short. Return only the translation."""
TRANSLATE_SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
                    "additionalProperties": False}
_translated = {}


def translate(text, lang):
    """The agent's message in the person's language (ISO 639-1); unchanged for English, without a
    model, or if the model fails. Cached, since the same lines come back."""
    if not text or not lang or lang == "en" or rules_mode():
        return text
    who = provider()
    if who == "rules":
        return text
    if (lang, text) not in _translated:
        try:
            out = ask_model(who, TRANSLATE_SYSTEM, "Language: %s\nMessage:\n%s" % (lang, text), TRANSLATE_SCHEMA,
                            "translation", timeout=20).get("text") or ""
        except Exception as e:
            print("Translation to %s failed, sending English: %r" % (lang, e))
            return text
        _translated[(lang, text)] = out.strip() or text
    return _translated[(lang, text)]
