"""Keeps data/ from growing without limit.

Every request leaves an outing-<id> folder (its checkpoints and route branches) and every
test run leaves a data/test-runs/<id> folder. They are only needed while the run is going
and for a look afterwards, so only the newest few are kept.
"""
import os
import shutil

KEEP_OUTINGS = int(os.environ.get("PLANNER_KEEP_OUTINGS", "20"))
KEEP_TEST_RUNS = int(os.environ.get("PLANNER_KEEP_TEST_RUNS", "10"))


def prune(parent, prefix="", keep=20, spare=()):
    """Delete all but the `keep` newest folders in `parent` whose names start with `prefix`.

    Folders named in `spare` are never deleted (the run that is starting), nor are mount
    points or symlinks (an Archil branch mounted there). Returns the names deleted.
    """
    try:
        names = [n for n in os.listdir(parent) if n.startswith(prefix) and n not in spare]
    except OSError:
        return []
    dirs = []
    for n in names:
        p = os.path.join(parent, n)
        if os.path.isdir(p) and not os.path.islink(p) and not os.path.ismount(p):
            dirs.append((os.path.getmtime(p), n))
    dirs.sort(reverse=True)
    gone = []
    for _, n in dirs[max(keep, 0):]:
        shutil.rmtree(os.path.join(parent, n), ignore_errors=True)
        gone.append(n)
    return gone
