"""Run the whole test suite, using disk branches for isolation.

    python3 run_tests.py            # unit tests + scenario branches
    python3 run_tests.py --keep     # keep every branch, not only failures

1. Unit tests (A, B, D) run directly: they take milliseconds.
2. The code, tests and seed data go onto a disk; checkpoint `test-base`.
3. One branch runs the search step once; checkpoint it as `after-search`.
4. Every scenario forks its own branch (from `test-base` or `after-search`) and runs in
   parallel, in its own process, writing only inside its branch.
5. Passing branches are deleted; failing ones are checkpointed as `failed-<name>` and kept.

Set PLANNER_DISK=archil (plus ARCHIL_DISK_ID, ARCHIL_REGION, ARCHIL_MOUNT) to use Archil;
otherwise branches are local copy-on-write clones.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from planner.disk import ArchilBackend, LocalCowBackend, _clone_tree, _dir_bytes
from tests.scenarios import SCENARIOS

ROOT = os.path.dirname(os.path.abspath(__file__))
KEEP = "--keep" in sys.argv
# Scenarios that call Nominatim, Overpass, Open-Meteo or OSRM: run one at a time.
LIVE_MAP_SCENARIOS = {"korean-food-truck", "replay-portland", "ask-start", "city-portland", "city-chicago", "hills-both-ways", "street-routing"}
MANUAL = [
    ("H3", "Map draws the selected plan", "check in the browser"),
    ("H4", "Layout at 1280x800", "check in the browser (known failing: map overlaps plans)"),
    ("H5", "A second message clears the previous run", "check in the browser"),
]


def run_unit():
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_unit")
    rows = []

    class Collect(unittest.TestResult):
        def _row(self, test, ok, detail=""):
            name = test._testMethodName[len("test_"):]
            tid, _, label = name.partition("_")
            rows.append({"id": tid, "name": label.replace("_", " ").capitalize(), "ok": ok,
                         "detail": detail.strip().splitlines()[-1] if detail else "", "where": "unit"})

        def addSuccess(self, test):
            self._row(test, True)

        def addFailure(self, test, err):
            self._row(test, False, self._exc_info_to_string(err, test))

        def addError(self, test, err):
            self._row(test, False, self._exc_info_to_string(err, test))

    suite.run(Collect())
    return rows


def files_written_since(path, t0):
    """Bytes of files a scenario wrote itself (skips the planner's own cloned branches)."""
    total = 0
    for dp, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in ("branches", "checkpoints")]
        for f in files:
            p = os.path.join(dp, f)
            try:
                if os.path.getmtime(p) >= t0:
                    total += os.path.getsize(p)
            except OSError:
                pass
    return total


def run_on_archil():
    """From your Mac: upload the code to the Archil disk and run the suite in Archil's microVM."""
    import base64
    import io
    import tarfile
    from planner import archil_api

    disk_id = os.environ.get("ARCHIL_DISK_ID")
    if not disk_id:
        sys.exit("ARCHIL_DISK_ID is not set in .env")
    buf = io.BytesIO()
    skip = lambda ti: None if "__pycache__" in ti.name or ti.name.endswith(".pyc") else ti
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for item in ("planner", "tests", "run_tests.py", "server.py"):
            tar.add(os.path.join(ROOT, item), arcname=item, filter=skip)
    payload = base64.b64encode(buf.getvalue()).decode()
    flags = " ".join(a for a in sys.argv[1:] if a in ("--keenable", "--keep"))
    # Each run gets a fresh folder: a previous microVM can hold its files for about a minute.
    app = "/mnt/archil/runs/app-" + time.strftime("%Y%m%d-%H%M%S")
    cmd = ("rm -f /mnt/archil/newfile.txt; export ARCHIL_DISK_ID=%s; mkdir -p %s && cd %s && "
           "echo %s | base64 -d | tar xz && "
           "python3 run_tests.py --in-archil-vm %s > /tmp/run.log 2>&1; rc=$?; "
           "archil sync /mnt/archil >/dev/null 2>&1; "
           "grep -v -e 'newer Archil CLI' -e 'archil upgrade' /tmp/run.log; exit $rc"
           % (disk_id, app, app, payload, flags))
    print("Uploading %d KB of code to %s and running the suite there..." % (len(buf.getvalue()) / 1e3, disk_id))
    t = time.time()
    out = archil_api.exec_on_disk(disk_id, cmd, with_key=True)
    print(out.get("stdout", ""))
    if out.get("stderr"):
        print("stderr:", out["stderr"][-2000:])
    print("Remote run took %.1f s (exit %s)" % (time.time() - t, out.get("exitCode")))
    sys.exit(out.get("exitCode") or 0)


def main():
    run_id = time.strftime("%Y%m%d-%H%M%S")
    archil = "--in-archil-vm" in sys.argv or os.environ.get("PLANNER_DISK") == "archil"
    if archil:
        disk = ArchilBackend(None, run_id)
        root = os.path.join(disk.mount, "reports", run_id)
    else:
        root = os.path.join(ROOT, "data", "test-runs", run_id)
        disk = LocalCowBackend(root)
    os.makedirs(root, exist_ok=True)
    print("Disk: %s · run %s" % (disk.name, run_id))

    rows = run_unit()
    print("Unit tests: %d passed, %d failed" % (sum(r["ok"] for r in rows), sum(not r["ok"] for r in rows)))

    # --- test-base: code + tests + seed data
    for item in ("planner", "tests"):
        _clone_tree(os.path.join(ROOT, item), os.path.join(disk.work, item))
    shutil.copy(os.path.join(ROOT, "server.py"), os.path.join(disk.work, "server.py"))  # Q3 and Q4 use it
    t = time.time()
    disk.checkpoint(disk.work, "test-base")
    base_bytes = _dir_bytes(disk.work)
    print("checkpoint test-base (%.0f KB) in %.0f ms" % (base_bytes / 1e3, (time.time() - t) * 1000))

    # --- after-search: run the search once, reuse it everywhere
    rec = disk.branch("test-base", "record-search")
    out = subprocess.run([sys.executable, "-m", "tests.record_search", "candidates.json"],
                         cwd=rec, capture_output=True, text=True, timeout=600,
                         env=dict(os.environ, PLANNER_SEARCH="seed"))  # tests use fixed data
    print(out.stdout.strip().splitlines()[-1] if out.stdout else out.stderr[-500:])
    disk.checkpoint(rec, "after-search")
    disk.delete_branch("record-search")

    # --- scenarios, one branch each, in parallel
    def run_scenario(name):
        _, parent = SCENARIOS[name]
        t_fork = time.time()
        path = disk.branch(parent, "scn-" + name)
        fork_ms = (time.time() - t_fork) * 1000
        with open(os.path.join(path, "whoami.txt"), "w") as f:
            f.write(name)
        # Tests use fixed seed data and local routes, whatever .env says; the live-search and
        # sandbox-routes scenarios opt back in below and in tests/scenarios.py.
        env = dict(os.environ, PLANNER_DATA=os.path.join(path, "data"), PLANNER_SEARCH="seed",
                   PLANNER_ROUTING="straight",  # street-routing (F4) switches it back on itself
                   PLANNER_PRESENT="template",  # no Claude calls in tests
                   PLANNER_PARSER="rules",  # repeatable parsing; L1 tests Claude's parsing on its own
                   PLANNER_DECIDER="rules",  # repeatable decisions; J3 tests Claude's decisions on its own
                   # Share the project's caches of map, routing and Keenable lookups across test
                   # copies, so live tests don't repeat lookups the free services rate-limit.
                   PLANNER_CACHE_DIR=os.path.join(ROOT, "data"))
        env.pop("PLANNER_ROUTES", None)
        env.pop("PLANNER_PROFILES_DIR", None)
        if parent == "after-search":
            env["PLANNER_CANDIDATES"] = os.path.join(path, "candidates.json")
        if name == "ana-timeout":
            env["PLANNER_SHARE_TIMEOUT"] = "1"
        if name in ("seed-fallback", "bad-search"):
            env.pop("ANTHROPIC_API_KEY", None)
            env["PLANNER_SEARCH"] = "seed"
        if name == "live-search":  # F2 is the one test that searches live
            env["PLANNER_SEARCH"] = "keenable" if "--keenable" in sys.argv else "claude"
        t0 = time.time()
        try:
            p = subprocess.run([sys.executable, "-m", "tests.scenarios", name], cwd=path, env=env,
                               capture_output=True, text=True, timeout=480)
            out, err = p.stdout, p.stderr
        except subprocess.TimeoutExpired as e:  # a stuck live service fails that scenario, not the run
            out, err = "", "timed out after %d s" % e.timeout
        dur = time.time() - t0
        line = next((l for l in out.splitlines() if l.startswith("RESULTS ")), None)
        res = json.loads(line[8:]) if line else [
            {"id": "ERR", "name": "scenario " + name, "ok": False, "detail": err[-200:]}]
        for r in res:
            r["where"] = "branch scn-" + name
        return {"name": name, "path": path, "rows": res, "secs": dur, "fork_ms": fork_ms,
                "written": files_written_since(path, t0), "start": t0}

    names = [n for n in SCENARIOS if not (archil and n == "sandbox-routes")]
    serial = [n for n in names if n in LIVE_MAP_SCENARIOS]
    parallel = [n for n in names if n not in LIVE_MAP_SCENARIOS]
    t_all = time.time()
    with ThreadPoolExecutor(max_workers=len(parallel) + 1) as pool:
        # The live-map scenarios share free, rate-limited services, so they run one after
        # another in a single worker, alongside all the other scenarios in parallel.
        serial_job = pool.submit(lambda: [run_scenario(n) for n in serial])
        runs = list(pool.map(run_scenario, parallel)) + serial_job.result()
    wall = time.time() - t_all

    # --- E2 isolation and E7 parallel branches, checked across all branches
    crosstalk = []
    outing_ids = {}
    for r in runs:
        with open(os.path.join(r["path"], "whoami.txt")) as f:
            if f.read() != r["name"]:
                crosstalk.append(r["name"] + ": marker overwritten")
        data = os.path.join(r["path"], "data")
        mine = sorted(d for d in os.listdir(data) if d.startswith("outing-")) if os.path.isdir(data) else []
        # E8 plans twice (local, then sandboxes); M3 twice (avoiding hills, then wanting them)
        expected = {"sandbox-routes": 2, "hills-both-ways": 2, "ask-start": 4, "replay-portland": 2}.get(r["name"], 1)
        if len(mine) > expected:
            crosstalk.append("%s: %d outing folders" % (r["name"], len(mine)))
        for o in mine:
            if o in outing_ids:
                crosstalk.append("%s and %s share %s" % (r["name"], outing_ids[o], o))
            outing_ids[o] = r["name"]
    # The checkpoint (local) or the main disk the branches came from (Archil) must be untouched.
    base = os.path.join(disk.cp_dir, "test-base") if disk.cp_dir else disk.work
    if os.path.exists(os.path.join(base, "whoami.txt")) or os.path.exists(os.path.join(base, "data")):
        crosstalk.append("test-base was modified by a branch")
    rows.append({"id": "E2", "name": "Isolation", "ok": not crosstalk,
                 "detail": "; ".join(crosstalk) or "%d branches, no crosstalk" % len(runs), "where": "all branches"})
    busy = sum(r["secs"] for r in runs)
    # Parallel scenarios overlap; the live-map ones run one after another in a single worker.
    serial_s = sum(r["secs"] for r in runs if r["name"] in LIVE_MAP_SCENARIOS)
    slowest = max([serial_s] + [r["secs"] for r in runs if r["name"] not in LIVE_MAP_SCENARIOS])
    written = sum(r["written"] for r in runs)
    # Parallel means the whole run takes about as long as its slowest branch, not the sum.
    rows.append({"id": "E7", "name": "Tests run on parallel branches",
                 "ok": not crosstalk and wall < slowest + 15 and written < len(runs) * base_bytes,
                 "detail": "%d branches, %.1f s wall (longest worker %.1f s, sum %.1f s); wrote %.0f KB vs %.0f KB for full copies; avg fork %.0f ms"
                           % (len(runs), wall, slowest, busy, written / 1e3, len(runs) * base_bytes / 1e3,
                              sum(r["fork_ms"] for r in runs) / len(runs)),
                 "where": "all branches"})
    rows.append({"id": "E6", "name": "Archil backend", "ok": (not crosstalk) if archil else None,
                 "detail": ("%d scenario branches on Archil disk %s" % (len(runs), disk.disk)) if archil
                           else "SKIPPED: run python3 run_tests.py --archil", "where": "-"})

    # --- keep failures as checkpoints, drop passing branches
    for r in runs:
        rows += r["rows"]
        if any(x["ok"] is False for x in r["rows"]):
            disk.checkpoint(r["path"], "failed-" + r["name"])
            for x in r["rows"]:
                if x["ok"] is False:
                    x["where"] += " (kept as checkpoint failed-%s)" % r["name"]
        if not KEEP:
            disk.delete_branch("scn-" + r["name"])
    rows += [{"id": i, "name": n, "ok": None, "detail": "MANUAL: " + d, "where": "browser"} for i, n, d in MANUAL]
    report(rows, root)


def report(rows, root):
    """Print the results table, save report.json under `root`, and exit 1 on any failure."""
    os.makedirs(root, exist_ok=True)
    order = lambda r: (r["id"][0], int("".join(c for c in r["id"][1:] if c.isdigit()) or 0), r["id"])
    rows.sort(key=order)
    print()
    print("%-4s %-6s %-36s %s" % ("ID", "Result", "Test", "Detail"))
    for r in rows:
        res = {True: "PASS", False: "FAIL", None: "SKIP"}[r["ok"]]
        print("%-4s %-6s %-36s %s  [%s]" % (r["id"], res, r["name"][:36], r["detail"][:110], r["where"]))
    n_pass = sum(r["ok"] is True for r in rows)
    n_fail = sum(r["ok"] is False for r in rows)
    n_skip = sum(r["ok"] is None for r in rows)
    print("\n%d passed, %d failed, %d skipped or manual · report in %s"
          % (n_pass, n_fail, n_skip, os.path.join(root, "report.json")))
    with open(os.path.join(root, "report.json"), "w") as f:
        json.dump(rows, f, indent=1)
    sys.exit(1 if n_fail else 0)


def app_tarball():
    import io
    import tarfile
    buf = io.BytesIO()
    skip = lambda ti: None if "__pycache__" in ti.name or ti.name.endswith(".pyc") else ti
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for item in ("planner", "tests", "run_tests.py", "server.py"):
            tar.add(os.path.join(ROOT, item), arcname=item, filter=skip)
    return buf.getvalue()


def run_on_sandboxes():
    """From your Mac: run every scenario in its own Archil sandbox (microVM), all at once.

    The controller runs in an Archil exec microVM (it has Python 3.10 for the Archil SDK).
    """
    from planner import archil_remote
    opts = {
        # sandbox-routes drives sandboxes itself; it runs only from your Mac (python3 run_tests.py)
        "scenarios": {n: parent for n, (_, parent) in SCENARIOS.items() if n != "sandbox-routes"},
        "keenable": "--keenable" in sys.argv,
        "manual": MANUAL,
    }
    with open(os.path.join(ROOT, "tests", "sandbox_runner.py")) as f:
        script = f.read()
    tar = app_tarball()
    print("Running %d scenarios on Archil sandboxes (%d KB of code)..." % (len(SCENARIOS), len(tar) / 1e3))
    t = time.time()
    out = archil_remote.run_python(script, files={"app.tgz": tar, "opts.json": json.dumps(opts).encode()})
    text = out.get("stdout", "")
    rows_line = next((l for l in reversed(text.splitlines()) if l.startswith("ROWS ")), None)
    for line in text.splitlines():
        if not line.startswith("ROWS "):
            print(line)
    print("Remote controller took %.1f s" % (time.time() - t))
    if not rows_line:
        print("stderr:", out.get("stderr", "")[-3000:])
        sys.exit(1)
    report(json.loads(rows_line[5:]), os.path.join(ROOT, "data", "test-runs", "sandboxes-" + time.strftime("%Y%m%d-%H%M%S")))


if __name__ == "__main__":
    if "--unit-json" in sys.argv:  # used inside the base sandbox
        print("UNIT " + json.dumps(run_unit()))
    elif "--sandboxes" in sys.argv:
        run_on_sandboxes()
    elif "--archil" in sys.argv:
        run_on_archil()
    else:
        main()
