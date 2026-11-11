"""Turn the judge's plans into friendly text messages with icons, for one phone.

Each phone gets several messages: a short intro, one message per plan, then a question
asking which plan they want. With ANTHROPIC_API_KEY set and the `anthropic` package
installed, Claude writes them (keeping every fact); otherwise, or if the call fails, a
built-in template with the same icons is used, so the demo also works offline.
"""
import json
import os

MODEL = "claude-opus-5-5"
NUMBERS = ["1️⃣", "2️⃣", "3️⃣"]

SYSTEM = """You are a friendly personal assistant agent texting the person you work for about a day out in San Francisco.
You get a JSON list of plans the planner already chose, and who is reading.

Write the messages as JSON with these fields:
- "intro": one short opening text, e.g. that you have N options for Saturday.
- "plans": one text per plan, in the same order as the input.
- "question": one short closing text asking which plan they want, or whether they'd like a change.

Rules for every text:
- Keep every fact exactly as given. Never invent places, times or facts. Name every stop of every plan, exactly as written.
- A plan text is short: the plan number, the departure time ("depart"), then each stop in order, one per line. Nothing else: no arrival times, travel times, distances, scores or notes (those are shown elsewhere).
- Use emoji icons that match the content: 🕙 departure, ☕ cafe, 🏛️ museum, 🌳 park or trail, 🌯 burritos or lunch, 🏖️ beach, 🐕 the dog, and start each plan with 1️⃣, 2️⃣ or 3️⃣.
- Plain text with line breaks. No markdown: no headings, bold or asterisks.
- Address the reader in the second person. If the reader is Ana, call the person who invited her "your friend"."""

SCHEMA = {
    "type": "object",
    "properties": {
        "intro": {"type": "string"},
        "plans": {"type": "array", "items": {"type": "string"}},
        "question": {"type": "string"},
    },
    "required": ["intro", "plans", "question"],
    "additionalProperties": False,
}


def _stop_icon(stop):
    if stop["category"] == "museum" and "outdoor" in stop["name"].lower():
        return "🌳"
    return {"cafe": "☕", "museum": "🏛️", "food": "🌯", "beach": "🏖️"}.get(stop["category"], "📍")


def _note_icon(note):
    n = note.lower()
    return "🌫️" if "fog" in n else "🐕" if "dog" in n else "⏳" if "free time" in n else "⚠️"


def template(plans, reader):
    """Offline version: the same facts and icons, written by code. Returns a list of texts."""
    who = "you and your friend" if reader == "Ana" else "your group"
    out = ["🗓️ I have %d plan%s for Saturday for %s%s. Here they are:"
           % (len(plans), "" if len(plans) == 1 else "s", who,
              " 🐕" if any("Biscuit" in p["scores"] for p in plans) else "")]
    for p in plans:
        # On the phone: just when you leave and the stops. Details live in the Routes tab.
        lines = ["%s Plan %d · 🕙 leave at %s" % (NUMBERS[p["rank"] - 1] if p["rank"] <= 3 else p["rank"], p["rank"],
                                                   p.get("depart", "10:00"))]
        lines += ["%s %s" % (_stop_icon(s), s["name"]) for s in p["stops"]]
        out.append("\n".join(lines))
    nums = " ".join(NUMBERS[: len(plans)])
    out.append("Which one do you want? %s Tap your pick below, or ask me for a change (another cafe, beach, less walking...)." % nums)
    return out


def _facts(plans):
    return [{"rank": p["rank"], "depart": p.get("depart", "10:00"),
             "stops": [{"name": s["name"], "category": s["category"]} for s in p["stops"]]} for p in plans]


def with_claude(plans, reader):
    import anthropic
    client = anthropic.Anthropic()
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM,
        betas=["server-side-fallback-2026-07-01"],
        messages=[{"role": "user", "content": "Reader: %s\nPlans (JSON):\n%s"
                   % (reader, json.dumps(_facts(plans), ensure_ascii=False))}],
        extra_body={"fallbacks": "default",
                    "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}},
        timeout=60,
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined")
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text").strip()
    data = json.loads(text)
    if len(data["plans"]) != len(plans):
        raise RuntimeError("Claude returned %d plan texts for %d plans" % (len(data["plans"]), len(plans)))
    # Every stop must still be named; otherwise use the template rather than show wrong facts.
    for p, t in zip(plans, data["plans"]):
        missing = [s["name"] for s in p["stops"] if s["name"] not in t]
        if missing:
            raise RuntimeError("Claude left out %s" % missing)
    return [data["intro"]] + data["plans"] + [data["question"]]


def claude_available():
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip() or os.environ.get("PLANNER_PRESENT") == "template":
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def messages(plans, reader):
    """Returns (list of texts, source) where source is 'claude' or 'template'."""
    if claude_available():
        try:
            return with_claude(plans, reader), "claude"
        except Exception as e:
            print("Claude formatting failed, using the template: %r" % e)
    return template(plans, reader), "template"
