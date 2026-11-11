"""One route branch, run inside its own Archil sandbox.

    python3 -m planner.route_worker <outing.json> <spec>

<spec> names the venues this branch owns, as category=index pairs, e.g. "museum=3,beach=2"
or "park=1,cafe=4". The worker reads the combined context and candidates (inherited from the
base sandbox it was forked from), simulates every combination of the other stops in every
sensible order, writes /root/result.json on its own disk and prints one RESULT line.
"""
import json
import sys
import time

from . import routes


def parse_spec(s):
    return {c: int(i) for c, i in (part.split("=") for part in s.split(",") if part)}


def format_spec(spec):
    return ",".join("%s=%d" % (c, i) for c, i in spec.items())


if __name__ == "__main__":
    path, spec = sys.argv[1], parse_spec(sys.argv[2])
    t = time.time()
    with open(path) as f:
        outing = json.load(f)
    cands = outing["candidates"]
    fixed = {c: cands[c][i] for c, i in spec.items()}
    best, drops, feasible = routes.route_branch(fixed, cands, outing["ctx"])
    result = {"label": routes.branch_label(fixed), "best": best,
              "dropped": drops, "feasible": feasible, "secs": round(time.time() - t, 3)}
    with open("/root/result.json", "w") as f:
        json.dump({"best": best[0] if best else None, "dropped": drops}, f, separators=(",", ":"))
    print("RESULT " + json.dumps(result, separators=(",", ":")))
