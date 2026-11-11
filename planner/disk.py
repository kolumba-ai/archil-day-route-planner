"""Outing disk: checkpoints and copy-on-write branches.

Two backends:
- LocalCowBackend (default): simulates Archil on a laptop using APFS clones
  (`cp -c`), which are copy-on-write like Archil branches.
- ArchilBackend (PLANNER_DISK=archil): calls the `archil` CLI.
  The checkpoint/branch commands follow docs.archil.com/reference/archil-cli;
  the exact `archil mount --branch` arguments are an assumption to confirm.
"""
import json
import os
import platform
import shutil
import subprocess
import threading
import time


def _clone_tree(src, dst):
    """Copy a directory using copy-on-write clones when the filesystem allows."""
    if platform.system() == "Darwin":
        r = subprocess.run(["cp", "-cR", src, dst], capture_output=True)
        if r.returncode == 0:
            return
    shutil.copytree(src, dst)


class LocalCowBackend:
    name = "local copy-on-write (APFS clones)"

    def __init__(self, root):
        self.root = root
        self.work = os.path.join(root, "work")
        self.cp_dir = os.path.join(root, "checkpoints")
        self.br_dir = os.path.join(root, "branches")
        for d in (self.work, self.cp_dir, self.br_dir):
            os.makedirs(d, exist_ok=True)

    def checkpoint(self, src, name):
        dst = os.path.join(self.cp_dir, name)
        if os.path.exists(dst):
            shutil.rmtree(dst)
        _clone_tree(src, dst)
        return dst

    def branch(self, checkpoint, name):
        dst = os.path.join(self.br_dir, name)
        if os.path.exists(dst):
            shutil.rmtree(dst)
        _clone_tree(os.path.join(self.cp_dir, checkpoint), dst)
        return dst

    def delete_branch(self, name):
        shutil.rmtree(os.path.join(self.br_dir, name), ignore_errors=True)


class ArchilBackend:
    """Real Archil checkpoints and branches, via the `archil` CLI on Linux.

    Runs where the disk is mounted (an Archil exec microVM mounts it at /mnt/archil).
    Needs ARCHIL_DISK_ID, ARCHIL_REGION, ARCHIL_API_KEY and ARCHIL_MOUNT_TOKEN.

    A checkpoint captures a whole mount, so every name is prefixed with a run ID and
    each run works in its own folder. Archil cannot delete branches yet, so
    delete_branch only unmounts.
    """
    name = "Archil"

    def __init__(self, root, run_id=None):
        self.disk = os.environ["ARCHIL_DISK_ID"]
        self.region = os.environ["ARCHIL_REGION"]
        self.mount = os.environ.get("ARCHIL_MOUNT", "/mnt/archil")
        self.run = (run_id or time.strftime("%Y%m%d%H%M%S")).replace("_", "-")
        self.sub = "work-" + self.run
        self.work = os.path.join(self.mount, self.sub)
        self.br_dir = "/tmp/archil-branches"
        self.cp_dir = None  # checkpoints live in Archil, not in a local folder
        self.cp_branch = {}  # checkpoint name -> branch it was taken on (None = root)
        os.makedirs(self.work, exist_ok=True)
        os.makedirs(self.br_dir, exist_ok=True)

    def _run(self, *args):
        r = subprocess.run(["archil", *args, "--region", self.region], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("archil %s failed: %s" % (" ".join(args[:2]), (r.stderr or r.stdout)[-300:]))
        return r.stdout

    def _qualified(self, name):
        return "%s-%s" % (self.run, name)

    def _mount_of(self, path):
        """Return (mountpoint, branch name or None) for a path on the disk."""
        path = os.path.abspath(path)
        if path.startswith(self.br_dir + "/"):
            br = path[len(self.br_dir) + 1:].split("/")[0]
            return os.path.join(self.br_dir, br), self._qualified(br)
        return self.mount, None

    def checkpoint(self, src, name):
        mnt, branch = self._mount_of(src)
        subprocess.run(["archil", "sync", mnt], capture_output=True)
        q = self._qualified(name)
        self._run("checkpoints", "create", mnt, q)
        self.cp_branch[name] = branch
        return q

    def branch(self, checkpoint, name):
        q = self._qualified(name)
        args = ["branches", "create", self.disk, q, "--from-checkpoint", self._qualified(checkpoint)]
        if self.cp_branch.get(checkpoint):
            args += ["--from-branch", self.cp_branch[checkpoint]]
        self._run(*args)
        mnt = os.path.join(self.br_dir, name)
        os.makedirs(mnt, exist_ok=True)
        self._run("mount", "--branch", q, self.disk, mnt)
        return os.path.join(mnt, self.sub)

    def delete_branch(self, name):
        subprocess.run(["archil", "unmount", os.path.join(self.br_dir, name)], capture_output=True)


class OutingDisk:
    """Tracks timing and bytes written so the UI can show what branching costs."""

    def __init__(self, root, emit):
        if os.environ.get("PLANNER_DISK") == "archil":
            self.backend = ArchilBackend(root)
        else:
            self.backend = LocalCowBackend(root)
        self.emit = emit
        self.lock = threading.Lock()
        self.branches = {}  # name -> bytes written
        self.base_bytes = 0

    @property
    def work(self):
        return self.backend.work

    def write_json(self, base_dir, rel, obj, compact=False):
        path = os.path.join(base_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = json.dumps(obj, separators=(",", ":")) if compact else json.dumps(obj, indent=1)
        with open(path, "w") as f:
            f.write(data)
        return len(data)

    def read_json(self, base_dir, rel):
        with open(os.path.join(base_dir, rel)) as f:
            return json.load(f)

    def checkpoint(self, src, name):
        t = time.time()
        self.backend.checkpoint(src, name)
        ms = (time.time() - t) * 1000
        size = _dir_bytes(src)
        if name == "combined":
            self.base_bytes = size
        self.emit("checkpoint", name=name, ms=round(ms, 1), bytes=size)

    def branch(self, checkpoint, name):
        t = time.time()
        path = self.backend.branch(checkpoint, name)
        ms = (time.time() - t) * 1000
        with self.lock:
            self.branches[name] = 0
        self.emit("branch", name=name, parent=checkpoint, ms=round(ms, 1))
        return path

    def record_write(self, name, nbytes):
        with self.lock:
            self.branches[name] = self.branches.get(name, 0) + nbytes

    def storage(self):
        with self.lock:
            deltas = sum(self.branches.values())
            n = len(self.branches)
        return {
            "branches": n,
            "base_bytes": self.base_bytes,
            "delta_bytes": deltas,
            "full_copy_bytes": self.base_bytes * n,
        }


def _dir_bytes(path):
    total = 0
    for dp, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total
