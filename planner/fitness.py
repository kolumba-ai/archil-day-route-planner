"""Steps, calories and BMI for one person on one plan.

Estimates, using standard formulas:
- Stride length ~ 0.414 x height (a common walking estimate); steps = meters walked / stride.
- Calories from the Pandolf load-carriage equation (Pandolf, Givoni & Goldman 1977), leg by leg:
      M (watts) = 1.5 W + 2.0 (W + L) (L / W)^2 + n (W + L) (1.5 V^2 + 0.35 V G)
  W body weight (kg), L backpack (kg), V walking speed (m/s, from the leg's distance and time),
  G grade (%, the leg's climb over its distance), n terrain (1.0: paved city streets).
  kcal = M x seconds walking / 4184. The 1.5 W term is standing metabolism, so this is the total
  burned while walking, not only the extra. The backpack costs more than its weight: the
  (L / W)^2 term grows with heavier packs. Downhill counts as flat (no descent data per leg).
- BMI = weight (kg) / height (m) squared. Shown to its owner; not used in the calorie formula
  (weight is).

Body data is sensitive: each person's numbers are computed from their own profile by their
own agent, and shown only on their own phone. Nobody else's agent receives their weight.
"""

DEFAULT_STRIDE_M = 0.72  # when height is unknown
TERRAIN = 1.0            # Pandolf terrain factor: paved roads and sidewalks
J_PER_KCAL = 4184.0
WALK_TO_RIDE_M_PER_MIN = 80.0  # walking to and from a bus or taxi


def stride_m(body):
    h = (body or {}).get("height_cm")
    return round(h * 0.414 / 100.0, 3) if h else DEFAULT_STRIDE_M


def bmi(body):
    w, h = (body or {}).get("weight_kg"), (body or {}).get("height_cm")
    if not w or not h:
        return None
    value = w / (h / 100.0) ** 2
    label = ("underweight" if value < 18.5 else "healthy weight" if value < 25
             else "overweight" if value < 30 else "obesity range")
    return {"value": round(value, 1), "label": label}


def walking_totals(plan):
    """Meters walked, meters climbed and minutes walking on a plan (rides count only their
    short walks to and from the stop)."""
    meters = climb = minutes = 0.0
    for leg in plan["legs"] + ([plan["return_leg"]] if plan.get("return_leg") else []):
        if leg["mode"] == "walk":
            meters += leg["km"] * 1000
            climb += leg.get("climb_m", 0)
            minutes += leg["min"]
        else:
            meters += leg.get("walk_km", 0) * 1000
            minutes += leg.get("walk_km", 0) * 1000 / WALK_TO_RIDE_M_PER_MIN
    for s in plan.get("stops", []):  # a stroll along a street
        if s.get("stroll_km"):
            meters += s["stroll_km"] * 1000
            minutes += s.get("stroll_min") or 0
    return meters, climb, minutes


def walking_segments(plan):
    """(meters, meters climbed, minutes) for each stretch walked: every walking leg, and the
    walks to and from each ride."""
    for leg in plan["legs"] + ([plan["return_leg"]] if plan.get("return_leg") else []):
        if leg["mode"] == "walk":
            yield leg["km"] * 1000, leg.get("climb_m", 0), leg["min"]
        elif leg.get("walk_km"):
            yield leg["walk_km"] * 1000, 0, leg["walk_km"] * 1000 / WALK_TO_RIDE_M_PER_MIN
    for s in plan.get("stops", []):  # a stroll along a street
        if s.get("stroll_km"):
            yield s["stroll_km"] * 1000, 0, s.get("stroll_min") or s["stroll_km"] * 1000 / WALK_TO_RIDE_M_PER_MIN


def pandolf_watts(weight, load, speed_ms, grade_pct, terrain=TERRAIN):
    """Metabolic rate (W) walking at speed_ms up grade_pct carrying load kg (Pandolf 1977)."""
    return (1.5 * weight + 2.0 * (weight + load) * (load / weight) ** 2
            + terrain * (weight + load) * (1.5 * speed_ms ** 2 + 0.35 * speed_ms * grade_pct))


def pandolf_kcal(plan, weight, load):
    kcal = 0.0
    for meters, up, minutes in walking_segments(plan):
        if meters <= 0:
            continue
        seconds = max(minutes, 1e-6) * 60
        speed = meters / seconds
        grade = min(25.0, 100.0 * up / meters)
        kcal += pandolf_watts(weight, load, speed, grade) * seconds / J_PER_KCAL
    return kcal


def estimate(plan, body):
    """{"steps", "kcal", "km", "climb_m", "load_kg"} for one person on one plan."""
    body = body or {}
    meters, climb, minutes = walking_totals(plan)
    steps = int(round(meters / stride_m(body), -1))
    out = {"steps": steps, "km": round(meters / 1000.0, 2), "climb_m": int(climb)}
    weight = body.get("weight_kg")
    if weight:
        load = body.get("backpack_kg") or 0
        out["kcal"] = int(round(pandolf_kcal(plan, weight, load)))
        out["load_kg"] = round(weight + load, 1)
        out["kcal_model"] = "Pandolf"
    return out


TYPICAL_WEIGHT_KG = 70.0  # an average adult, for screens everyone sees


def typical_kcal(plan):
    """Calories burned walking for a typical 70 kg adult with no backpack, for screens everyone
    sees (no personal data); each phone shows its owner's own number."""
    return int(round(pandolf_kcal(plan, TYPICAL_WEIGHT_KG, 0)))


def typical_steps(plan):
    """Steps at a typical stride, for screens everyone sees (no personal data)."""
    meters, _, _ = walking_totals(plan)
    return int(round(meters / DEFAULT_STRIDE_M, -1))
