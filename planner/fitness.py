"""Steps, calories and BMI for one person on one plan.

Estimates, using standard formulas:
- Stride length ~ 0.414 x height (a common walking estimate); steps = meters walked / stride.
- Calories from the ACSM walking equation: oxygen used (ml per kg) = 0.1 x meters walked
  + 1.8 x meters climbed + 3.5 x minutes walking (resting), times the mass moved (body weight
  plus backpack), at about 5 kcal per liter of oxygen.
- BMI = weight (kg) / height (m) squared.

Body data is sensitive: each person's numbers are computed from their own profile by their
own agent, and shown only on their own phone. Nobody else's agent receives their weight.
"""

DEFAULT_STRIDE_M = 0.72  # when height is unknown
KCAL_PER_LITER_O2 = 5.0


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
            minutes += leg.get("walk_km", 0) * 1000 / 80.0  # about 80 m per minute
    return meters, climb, minutes


def estimate(plan, body):
    """{"steps", "kcal", "km", "climb_m", "load_kg"} for one person on one plan."""
    body = body or {}
    meters, climb, minutes = walking_totals(plan)
    steps = int(round(meters / stride_m(body), -1))
    out = {"steps": steps, "km": round(meters / 1000.0, 2), "climb_m": int(climb)}
    weight = body.get("weight_kg")
    if weight:
        mass = weight + (body.get("backpack_kg") or 0)
        o2_ml_per_kg = 0.1 * meters + 1.8 * climb + 3.5 * minutes
        out["kcal"] = int(round(o2_ml_per_kg * mass / 1000.0 * KCAL_PER_LITER_O2))
        out["load_kg"] = round(mass, 1)
    return out


def typical_steps(plan):
    """Steps at a typical stride, for screens everyone sees (no personal data)."""
    meters, _, _ = walking_totals(plan)
    return int(round(meters / DEFAULT_STRIDE_M, -1))
