"""Controller: runs the test suite on Archil sandboxes, one sandbox per scenario.

Runs inside an Archil exec microVM (Python 3.10 + Archil SDK), launched by
`python3 run_tests.py --sandboxes`. Expects `client` (archil.Archil) to be defined by the
prelude, plus `app.tgz` (the code) and `opts.json` next to this file.

1. Create a base sandbox, upload and unpack the code, run the unit tests and record the
   search results once, then stop it. The stopped base is the checkpoint.
2. Fork one sandbox per scenario, all at once, and run each scenario in its own microVM.
3. Delete passing sandboxes; keep failing ones (stopped) for inspection.
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor

opts = json.load(open("opts.json"))
RUN = time.strftime("%m%d-%H%M%S")
SCENARIOS = opts["scenarios"]  # name -> "test-base" | "after-search"
T = time.time
APP = "/root/app"


def sh(sb, cmd, timeout=600):
    r = sb.exec(cmd, timeout_seconds=timeout)
    return r.exit_code, r.stdout or "", r.stderr or ""


def last_json(prefix, text):
    line = next((l for l in reversed(text.splitlines()) if l.startswith(prefix)), None)
    return json.loads(line[len(prefix):]) if line else None


rows = []
t_all = T()

# --- 1. base sandbox
t = T()
base = client.sandboxes.create(name="gdp-%s-base" % RUN, vcpu_count=2, mem_size_mib=2048,
                               idle_ttl_seconds=600, max_ttl_seconds=3600)
base.files.upload_file("app.tgz", "/root/app.tgz")
code, out, err = sh(base, "mkdir -p %s && cd %s && tar xzf /root/app.tgz && python3 run_tests.py --unit-json" % (APP, APP))
unit = last_json("UNIT ", out) or [{"id": "ERR", "name": "unit tests did not run", "ok": False,
                                     "detail": (err or out)[-200:], "where": "base sandbox"}]
for r in unit:
    r["where"] = "base sandbox"
rows += unit
rec_env = "PLANNER_SEARCH=keenable " if opts.get("record_keenable") else ""
code, out, err = sh(base, "cd %s && %spython3 -m tests.record_search candidates.json" % (APP, rec_env))
print(out.strip().splitlines()[-1] if out.strip() else "record_search failed: " + err[-300:])
base_bytes = int(sh(base, "du -sb %s | cut -f1" % APP)[1].strip() or 0)
base = base.stop()
print("base sandbox ready in %.1f s (%d KB of code + data), stopped as the checkpoint" % (T() - t, base_bytes / 1e3))


# --- 2. one sandbox per scenario
def scenario(name):
    parent = SCENARIOS[name]
    t_fork = T()
    sb = base.fork(name="gdp-%s-%s" % (RUN, name))
    fork_s = T() - t_fork
    env = ["PLANNER_DATA=%s/data" % APP]
    if parent == "after-search":
        env.append("PLANNER_CANDIDATES=%s/candidates.json" % APP)
    if name == "ana-timeout":
        env.append("PLANNER_SHARE_TIMEOUT=1")
    if name in ("seed-fallback", "bad-search"):
        env.append("PLANNER_SEARCH=seed")
    if name == "live-search" and opts.get("keenable"):
        env.append("PLANNER_SEARCH=keenable")
    cmd = ("cd {app} && echo {n} > whoami.txt && touch .start && {env} python3 -m tests.scenarios {n}; "
           "echo WHOAMI $(cat whoami.txt); echo OUTINGS $(ls data 2>/dev/null | wc -l); "
           "echo WRITTEN $(find {app} -newer .start -type f -not -path '*/branches/*' "
           "-not -path '*/checkpoints/*' -printf '%s\\n' | awk '{{s+=$1}} END {{print s+0}}')"
           ).format(app=APP, n=name, env=" ".join(env))
    t0 = T()
    code, out, err = sh(sb, cmd)
    dur = T() - t0
    res = last_json("RESULTS ", out) or [{"id": "ERR", "name": "scenario " + name, "ok": False,
                                          "detail": (err or out)[-200:]}]
    meta = {k: (l.split(" ", 1)[1].strip() if " " in l else "") for l in out.splitlines()
            for k in ("WHOAMI", "OUTINGS", "WRITTEN") if l.startswith(k + " ")}
    failed = any(r["ok"] is False for r in res)
    for r in res:
        r["where"] = "sandbox " + sb.name + (" (kept, stopped)" if failed and r["ok"] is False else "")
    try:
        sb = sb.stop()
        if not failed:
            sb.delete()
    except Exception as e:
        print("cleanup of %s failed: %r" % (sb.name, e))
    return {"name": name, "rows": res, "secs": dur, "fork_s": fork_s, "meta": meta}


t0 = T()
with ThreadPoolExecutor(len(SCENARIOS)) as pool:
    runs = list(pool.map(scenario, SCENARIOS))
wall = T() - t0

# --- 3. cross-sandbox checks
crosstalk = []
for r in runs:
    if r["meta"].get("WHOAMI") != r["name"]:
        crosstalk.append("%s: marker shows %r" % (r["name"], r["meta"].get("WHOAMI")))
    if int(r["meta"].get("OUTINGS") or 0) > 1:
        crosstalk.append("%s: %s outing folders" % (r["name"], r["meta"]["OUTINGS"]))
rows.append({"id": "E2", "name": "Isolation", "ok": not crosstalk,
             "detail": "; ".join(crosstalk) or "%d sandboxes, no crosstalk" % len(runs), "where": "all sandboxes"})
slowest = max(r["secs"] for r in runs)
written = sum(int(r["meta"].get("WRITTEN") or 0) for r in runs)
rows.append({"id": "E7", "name": "Tests run on parallel sandboxes",
             "ok": not crosstalk and wall < slowest + 10 and written < len(runs) * base_bytes,
             "detail": "%d sandboxes, %.1f s wall (slowest %.1f s, sum %.1f s); wrote %.0f KB vs %.0f KB for full copies; avg fork %.1f s"
                       % (len(runs), wall, slowest, sum(r["secs"] for r in runs), written / 1e3,
                          len(runs) * base_bytes / 1e3, sum(r["fork_s"] for r in runs) / len(runs)),
             "where": "all sandboxes"})
rows.append({"id": "E6", "name": "Archil backend", "ok": not crosstalk,
             "detail": "%d scenario sandboxes forked from one stopped base sandbox" % len(runs), "where": "-"})
for r in runs:
    rows += r["rows"]
rows += [{"id": i, "name": n, "ok": None, "detail": "MANUAL: " + d, "where": "browser"} for i, n, d in opts["manual"]]
if not any(r["ok"] is False for r in rows):
    base.delete()
else:
    print("base sandbox kept (stopped) because something failed:", base.name)
print("Total %.1f s" % (T() - t_all))
print("ROWS " + json.dumps(rows))
