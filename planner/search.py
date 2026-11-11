"""Search step: find candidate venues for one stop type.

Uses Claude with the server-side web search tool when ANTHROPIC_API_KEY is set
and the `anthropic` package is installed. Otherwise falls back to seed data so
the demo always runs.
"""
import json
import os
import random
import re
import time

HERE = os.path.dirname(__file__)
MODEL = "claude-opus-5-5"

FIELDS = (
    'name, lat, lon, open ("HH:MM" on the requested day), close ("HH:MM"), '
    'dogs (one of "no", "patio", "leash", "offleash"), vegetarian_ok (true/false), '
    'kind ("museum" or "outdoor_alternative", museum stops only), '
    'area ("west", "northwest", "north", beach stops only), tags (list of short words), '
    'source (URL you used)'
)

CATEGORY_HINT = {
    "cafe": "coffee shops or cafes",
    "museum": "museums",
    "food": "burrito places or casual Mexican restaurants",
    "beach": "beaches",
}


def live_search_available():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def load_seed(category):
    with open(os.path.join(HERE, "seed_venues.json")) as f:
        data = json.load(f)
    venues = data.get(category, [])
    for v in venues:
        v.setdefault("source", "seed data (illustrative)")
    return venues


def provider_label():
    mode = os.environ.get("PLANNER_SEARCH", "")
    if RECORDED_LABEL in os.environ:
        return "recorded search results"
    if mode == "keenable":
        return "Keenable (checks seed venues on the web)"
    if mode != "seed" and live_search_available():
        return "Claude web search"
    return "seed data"


RECORDED_LABEL = "PLANNER_CANDIDATES"


def search_category(category, context):
    """Return (venues, source_label). `context` is the combined outing context.

    PLANNER_SEARCH picks the provider: "keenable" (check candidates on the web with
    Keenable), "claude" (Claude web search, needs ANTHROPIC_API_KEY) or "seed".
    Default: Claude if a key is set, otherwise seed data.
    """
    mode = os.environ.get("PLANNER_SEARCH", "")
    if mode == "keenable":
        from . import keenable
        try:
            return keenable.verify_all(load_seed(category), category), "Keenable search"
        except Exception as e:
            print("Keenable search failed for %s: %r" % (category, e))
            return load_seed(category), "seed data"
    if mode != "seed" and live_search_available():
        try:
            return _claude_search(category, context), "Claude web search"
        except Exception as e:  # fall back so the demo keeps going
            print("live search failed for %s: %r" % (category, e))
    time.sleep(random.uniform(0.6, 1.6))  # make the offline run look like real fan-out
    return load_seed(category), "seed data"


def _claude_search(category, context):
    import anthropic

    client = anthropic.Anthropic()
    limits = context["limits"]
    dog_line = ""
    if limits["dog"]:
        dog_line = (
            "A dog is coming. Check each place's dog policy. "
            + ("For museums, also include 2 dog-friendly outdoor alternatives with kind "
               '"outdoor_alternative". ' if category == "museum" else "")
        )
    prompt = (
        "Find 6 good %s in San Francisco for a day out on %s.\n"
        "Group needs: diets %s; avoid %s; hills: %s. %s\n"
        "Use web search to check opening hours for that day and dog rules.\n"
        "Reply with ONLY a JSON array in a ```json code block. Each item has: %s."
        % (
            CATEGORY_HINT.get(category, category),
            context["request"]["day"],
            ", ".join(limits["diets"]) or "none",
            ", ".join(limits["avoid_foods"]) or "nothing",
            "avoid steep hills" if limits["avoid_hills"] else "fine",
            dog_line,
            FIELDS,
        )
    )
    messages = [{"role": "user", "content": prompt}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 4}]
    resp = None
    for _ in range(4):  # resume server-tool turns that pause
        resp = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            messages=messages,
            tools=tools,
            extra_body={"output_config": {"effort": "low"}, "fallbacks": "default"},
            extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
        )
        if resp.stop_reason != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": resp.content}]
    if resp.stop_reason == "refusal":
        raise RuntimeError("model declined the request")
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S) or re.search(r"(\[.*\])", text, re.S)
    if not m:
        raise ValueError("no JSON array in reply")
    venues = json.loads(m.group(1))
    clean = []
    for v in venues:
        try:
            v["lat"], v["lon"] = float(v["lat"]), float(v["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        v.setdefault("open", "09:00")
        v.setdefault("close", "18:00")
        v.setdefault("dogs", "no")
        v.setdefault("vegetarian_ok", True)
        v.setdefault("tags", [])
        if category == "museum":
            v.setdefault("kind", "museum")
        if category == "beach":
            v.setdefault("area", "west" if v["lon"] < -122.495 else "north")
        clean.append(v)
    if not clean:
        raise ValueError("no usable venues")
    return clean
