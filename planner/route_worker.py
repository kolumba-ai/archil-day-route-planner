"""One route branch, run inside its own Archil sandbox.

    python3 -m planner.route_worker <outing.json> <museum index> <beach index>

Reads the combined context and candidates (inherited from the base sandbox it was forked
from), simulates every cafe x food x order for its museum + beach pair, writes
/root/result.json on its own disk and prints one RESULT line.
"""
import json
import sys
import time

from . import routes

if __name__ == "__main__":
    path, i, j = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    t = time.time()
    with open(path) as f:
        outing = json.load(f)
    cands = outing["candidates"]
    museum, beach = cands["museum"][i], cands["beach"][j]
    best, drops, feasible = routes.route_branch(museum, beach, cands, outing["ctx"])
    result = {"museum": museum["name"], "beach": beach["name"], "best": best,
              "dropped": drops, "feasible": feasible, "secs": round(time.time() - t, 3)}
    with open("/root/result.json", "w") as f:
        json.dump({"best": best[0] if best else None, "dropped": drops}, f, separators=(",", ":"))
    print("RESULT " + json.dumps(result, separators=(",", ":")))
