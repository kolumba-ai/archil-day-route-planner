"""The decision step: choose up to 3 plans and say why.

Every candidate here has already passed the hard limits in code (walking distance, finish
time, opening hours, dog rules, diet, hills), so a decision model only ranks and explains;
it can never pick a plan that breaks a limit.

Deciders (PLANNER_DECIDER):
- "rules":  the scoring in routes.py (happiness minus travel time, variety rules) and
            reasons written from the numbers. Instant and repeatable; tests use it.
- "claude": Claude Haiku 5.5 chooses from a shortlist and explains each pick
            (default when ANTHROPIC_API_KEY is set).
- "subq":   SubQ Snap 1.1, a long-context decision model (docs.subq.ai, Decisions API): one
            "choice" question over the shortlist; its probabilities set the order, and the
            reasons come from the rules plus SubQ's confidence. Default when SUBQ_API_KEY is set.
Any failure, or a pick that isn't on the shortlist, falls back to the rules.
"""
import json
import os
import urllib.error

from . import routes

CLAUDE_MODEL = "claude-haiku-5-5"
SHORTLIST = 8


def which():
    choice = os.environ.get("PLANNER_DECIDER", "").strip().lower()
    if choice in ("rules", "claude", "subq"):
        return choice
    if os.environ.get("SUBQ_API_KEY", "").strip():
        return "subq"
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return "claude"
    return "rules"


# ---------------------------------------------------------------- SubQ Snap

SUBQ_URL = "https://api.subq.ai/v1/systemone"
SUBQ_MODEL = "subq-snap-1.1"


def subq_request(shortlist, ctx):
    """One "choice" question over the shortlist (docs.subq.ai: Decisions API). The state is
    JSON: the group's limits and each candidate's facts; options are plan ids p0, p1, ..."""
    state = {
        "task": "Choose the day-out plan this group will enjoy most. Every candidate already respects "
                "their hard limits (walking distance, finish time, opening hours, dogs, diet, hills).",
        "people": [p["name"] for p in ctx["people"]],
        "limits": {k: ctx["limits"][k] for k in ("max_km", "avoid_hills", "prefer_hills", "dog") if k in ctx["limits"]},
        "candidates": {"p%d" % i: _card(i, it) for i, (_, it) in enumerate(shortlist)},
    }
    options = {"p%d" % i: "Plan p%d: %s; %d min getting around, %.1f km walking" % (
        i, " -> ".join(s["name"] for s in it["stops"]), it["travel_min"], it["walk_km"])
        for i, (_, it) in enumerate(shortlist)}
    return {
        "model": SUBQ_MODEL,
        "state": state,
        "questions": {"best_plan": {
            "type": "choice",
            "instructions": "Which candidate should the group do? Prefer little time getting around and "
                            "nobody unhappy (look at each person's score); weigh notes such as fog or a "
                            "dog waiting outside.",
            "criteria": options,
        }},
    }


def subq_rank(response, shortlist, n=3):
    """Shortlist entries ordered by SubQ's probabilities, with each one's probability."""
    ans = response["answers"]["best_plan"]
    probs = ans.get("probabilities") or {ans["choice"]: 1.0}
    order = sorted(((p, k) for k, p in probs.items() if k.startswith("p") and k[1:].isdigit()
                    and int(k[1:]) < len(shortlist)), reverse=True)
    picked = [(shortlist[int(k[1:])], p) for p, k in order][:n]
    if not picked:
        raise ValueError("SubQ returned no usable choice")
    return picked


def by_subq(pool, ctx, n=3):
    import urllib.request
    key = os.environ.get("SUBQ_API_KEY", "").strip()
    if not key:
        raise RuntimeError("SUBQ_API_KEY is not set")
    shortlist = routes.pick_diverse(pool, n=SHORTLIST)
    if len(shortlist) <= 1:
        return by_rules(pool, n)
    body = json.dumps(subq_request(shortlist, ctx)).encode()
    req = urllib.request.Request(SUBQ_URL, data=body, method="POST", headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            picked = subq_rank(json.load(r), shortlist, n)
    except urllib.error.HTTPError as e:  # e.g. {"detail": {"error_type": ..., "message": ...}}
        detail = e.read()[:200].decode(errors="replace").replace(key, "[key]")
        raise RuntimeError("SubQ HTTP %d: %s" % (e.code, detail))
    chosen = [item for item, _ in picked]
    first = chosen[0][1]
    fastest = min(it["travel_min"] for _, it in chosen)
    reasons = ["%s SubQ Snap: %d%% likely the best fit." % (explain(it, first, i, fastest), round(p * 100))
               for i, ((_, it), p) in enumerate(picked, 1)]
    return chosen, reasons


# ---------------------------------------------------------------- rules

def _stop(it, cat):
    return next((s["name"] for s in it["stops"] if s["category"] == cat), None)


def explain(it, first, rank, fastest=None):
    """One sentence on why this plan, from its numbers. `fastest` is the least travel time
    among the plans shown; "least time" is said only when it's true."""
    flat = "flat" if it["climb_m"] < 20 else "%d m of climbing" % it["climb_m"]
    people = [v for k, v in it["scores"].items() if isinstance(v, float)]
    mood = (" Nobody below %d/10." % round(min(people) * 10)) if people else ""
    if rank == 1:
        lead = "Least time getting around" if fastest is None or it["travel_min"] <= fastest else "Top pick"
        return "%s: %d min (%.1f km walking, %s).%s" % (lead, it["travel_min"], it["walk_km"], flat, mood)
    changed = [_stop(it, c) for c in sorted(routes.stop_names(it)) if _stop(it, c) != _stop(first, c)]
    extra = it["travel_min"] - first["travel_min"]
    return "Different %s: %s. %d min getting around (%s than plan 1), %s.%s" % (
        "stop" if len(changed) == 1 else "stops", ", ".join(changed), it["travel_min"],
        ("%d min more" % extra) if extra > 0 else ("%d min less" % -extra) if extra < 0 else "same", flat, mood)


def by_rules(pool, n=3):
    chosen = routes.pick_diverse(pool, n=n)
    first = chosen[0][1] if chosen else None
    return chosen, [explain(it, first, i) for i, (_, it) in enumerate(chosen, 1)]


# ---------------------------------------------------------------- Claude

SCHEMA = {
    "type": "object",
    "properties": {"picks": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "reason": {"type": "string"}},
        "required": ["id", "reason"], "additionalProperties": False}}},
    "required": ["picks"], "additionalProperties": False,
}

SYSTEM = """You choose the plans a group will see for a day out. Every candidate already respects the group's hard limits, so judge only quality.
Pick up to 3 candidates, best first. Prefer little time getting around and nobody unhappy (look at each person's score); the alternatives should be real alternatives (different stops), not near-copies.
For each pick write one short sentence on why, using only the facts given (times, distances, scores, notes). Never invent facts."""


def _card(i, it):
    return {"id": i, "stops": [s["name"] + " (" + s["category"] + ")" for s in it["stops"]],
            "travel_min": it["travel_min"], "walk_km": it["walk_km"], "climb_m": it["climb_m"],
            "finish": it["end"], "scores": it["scores"], "notes": it["notes"]}


def by_claude(pool, ctx, n=3):
    import anthropic
    shortlist = routes.pick_diverse(pool, n=SHORTLIST)  # varied, sensible candidates only
    if len(shortlist) <= 1:
        return by_rules(pool, n)
    group = {"limits": {k: ctx["limits"][k] for k in ("max_km", "avoid_hills", "dog") if k in ctx["limits"]},
             "people": [p["name"] for p in ctx["people"]]}
    resp = anthropic.Anthropic().messages.create(
        model=CLAUDE_MODEL, max_tokens=2000, system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps({"group": group, "candidates": [
            _card(i, it) for i, (_, it) in enumerate(shortlist)]}, ensure_ascii=False)}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        timeout=30)
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined")
    picks = json.loads(next(b.text for b in resp.content if b.type == "text"))["picks"]
    return validate(picks, shortlist, n)


def validate(picks, shortlist, n):
    """Keep only real, distinct shortlist ids; raise if nothing usable is left."""
    chosen, reasons, seen = [], [], set()
    for p in picks:
        i = p.get("id")
        if isinstance(i, int) and 0 <= i < len(shortlist) and i not in seen and p.get("reason", "").strip():
            seen.add(i)
            chosen.append(shortlist[i])
            reasons.append(p["reason"].strip()[:300])
        if len(chosen) == n:
            break
    if not chosen:
        raise ValueError("no valid picks")
    return chosen, reasons


# ---------------------------------------------------------------- entry point

def decide(pool, ctx, n=3):
    """Returns (chosen [(branch, itinerary)], reasons [str], decided_by, note)."""
    who = which()
    note = ""
    try:
        if who == "claude":
            chosen, reasons = by_claude(pool, ctx, n)
            return chosen, reasons, "Claude Haiku 5.5", note
        if who == "subq":
            chosen, reasons = by_subq(pool, ctx, n)
            return chosen, reasons, "SubQ Snap 1.1", note
    except Exception as e:
        note = "%s; used the rules instead" % str(e)[:120]
    chosen, reasons = by_rules(pool, n)
    return chosen, reasons, "rules", note
