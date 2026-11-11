"""Route phase on Archil sandboxes: one sandbox (microVM) per route branch.

From the planner (on your Mac) call `run_route_phase(...)`. It sends CONTROLLER to an
Archil exec microVM (Python 3.10 + Archil SDK), which:
1. deletes the sandboxes kept from the previous outing,
2. creates a base sandbox with the planner code and this outing's context + candidates,
   then stops it (the stopped base is the `candidates` checkpoint),
3. forks one sandbox per (museum, beach) pair, all at once, and runs planner.route_worker,
4. judges the results, keeps the 3 winning sandboxes (stopped) as plan-1..3 and the base,
   and deletes the rest.
"""
import io
import json
import os
import tarfile

from . import archil_remote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONTROLLER = r'''
import json, sys, time, tarfile
from concurrent.futures import ThreadPoolExecutor
T = time.time
opts = json.load(open("opts.json"))
oid, pairs = opts["outing_id"], opts["pairs"]
tarfile.open("app.tgz").extractall("app")
sys.path.insert(0, "app")
from planner import routes  # same code the sandboxes run, used here to judge

t_all = T()
# 1. clean up the previous outing (forks before their base)
old = [s for s in client.sandboxes.list() if s.name.startswith("gdp-out-") and not s.name.startswith("gdp-out-%s" % oid)]
def drop(s):
    try:
        if s.status not in ("stopped", "exited", "failed"):
            s = s.stop()
        s.delete()
    except Exception as e:
        return repr(e)[:100]
with ThreadPoolExecutor(32) as pool:
    list(pool.map(drop, [s for s in old if not s.name.endswith("-base")]))
    list(pool.map(drop, [s for s in old if s.name.endswith("-base")]))

# 2. base sandbox = the "candidates" checkpoint
t = T()
base_t0 = t - t_all
base = client.sandboxes.create(name="gdp-out-%s-base" % oid, vcpu_count=1, mem_size_mib=1024,
                               idle_ttl_seconds=600, max_ttl_seconds=3600)
base.files.upload_file("app.tgz", "/root/app.tgz")
base.files.upload_file("outing.json", "/root/outing.json")
base.exec("mkdir -p /root/app && cd /root/app && tar xzf /root/app.tgz")
base = base.stop()
base_s = T() - t

# 3. one sandbox per route branch, all at once
def branch(k_pair):
    k, (i, j) = k_pair
    name = "gdp-out-%s-r%03d" % (oid, k + 1)
    t0 = T()
    t_start = t0 - t_all
    try:
        sb = base.fork(name=name)
        fork_s = T() - t0
        t1 = T()
        r = sb.exec("cd /root/app && python3 -m planner.route_worker /root/outing.json %d %d" % (i, j), timeout_seconds=300)
        run_s = T() - t1
        line = next((l for l in (r.stdout or "").splitlines() if l.startswith("RESULT ")), None)
        res = json.loads(line[7:]) if line else {"error": (r.stderr or r.stdout or "")[-300:]}
        return dict(res, branch=name, fork_s=fork_s, run_s=run_s, sandbox=sb, t_start=t_start)
    except Exception as e:
        return {"branch": name, "error": repr(e)[:300], "fork_s": T() - t0, "run_s": 0, "sandbox": None,
                "t_start": t_start}

t = T()
with ThreadPoolExecutor(min(100, len(pairs))) as pool:
    results = list(pool.map(branch, enumerate(pairs)))
fan_s = T() - t

# 4. judge, keep winners (stopped) as plan checkpoints, delete the rest
plans = [(r["branch"], it) for r in results if "best" in r for it in r["best"]]
chosen = routes.pick_diverse(plans, n=3)
keep = {name for name, _ in chosen}
def finish(r):
    sb = r.pop("sandbox", None)
    if not sb:
        return
    try:
        sb = sb.stop()
        if r["branch"] not in keep:
            sb.delete()
    except Exception as e:
        r["cleanup_error"] = repr(e)[:100]
with ThreadPoolExecutor(32) as pool:
    list(pool.map(finish, results))

print("ROUTES " + json.dumps({
    "results": results,
    "chosen": [{"branch": n, "plan": it} for n, it in chosen],
    "timing": {"base_s": round(base_s, 2), "base_t0": round(base_t0, 3), "fanout_s": round(fan_s, 2),
               "total_s": round(T() - t_all, 3)},
    "kept": sorted(keep) + [base.name],
}, separators=(",", ":")))
'''


def _code_tarball():
    buf = io.BytesIO()
    skip = lambda ti: None if "__pycache__" in ti.name or ti.name.endswith(".pyc") else ti
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(os.path.join(ROOT, "planner"), arcname="planner", filter=skip)
    return buf.getvalue()


def run_route_phase(outing_id, ctx, candidates, pairs):
    """pairs: list of (museum index, beach index). Returns the controller's ROUTES dict."""
    outing = json.dumps({"ctx": ctx, "candidates": candidates}).encode()
    opts = json.dumps({"outing_id": outing_id, "pairs": pairs}).encode()
    out = archil_remote.run_python(CONTROLLER, files={
        "app.tgz": _code_tarball(), "outing.json": outing, "opts.json": opts})
    line = next((l for l in reversed(out.get("stdout", "").splitlines()) if l.startswith("ROUTES ")), None)
    if not line:
        raise RuntimeError("sandbox route phase failed: %s" % (out.get("stderr") or out.get("stdout", ""))[-800:])
    return json.loads(line[7:])
