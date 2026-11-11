"""Run a Python script inside an Archil exec microVM that has the Archil Python SDK.

Your Mac has Python 3.9; the SDK needs 3.10+. The exec microVM has 3.10, so scripts that
drive sandboxes run there. The SDK is installed into a venv on the disk once and reused.
"""
import base64
import os

from . import archil_api

SETUP = ("[ -x /mnt/archil/.sdk/bin/python ] || (python3 -m venv /mnt/archil/.sdk && "
         "/mnt/archil/.sdk/bin/pip install -q 'archil>=0.16' >/dev/null 2>&1); ")


PRELUDE = r'''
import os, re, archil, archil._models as _m
from datetime import datetime as _dt
def _parse(value):  # SDK bug on Python 3.10: timestamps with 5-digit fractions don't parse
    v = value.replace("Z", "+00:00")
    v = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], v)
    return _dt.fromisoformat(v)
_m._parse_datetime = _parse
client = archil.Archil(api_key=os.environ["ARCHIL_API_KEY"], region=os.environ["ARCHIL_REGION"])
'''


def run_python(script, files=None, timeout_note=""):
    """Run `script` (Python source) in the microVM. `files` maps remote name -> bytes
    placed next to it. Returns the exec result dict (stdout, stderr, exitCode, timing)."""
    disk = os.environ["ARCHIL_DISK_ID"]
    parts = [SETUP, "mkdir -p /tmp/job && cd /tmp/job && "]
    for name, data in (files or {}).items():
        parts.append("echo %s | base64 -d > %s && " % (base64.b64encode(data).decode(), name))
    parts.append("echo %s | base64 -d > job.py && " % base64.b64encode((PRELUDE + script).encode()).decode())
    parts.append("export ARCHIL_DISK_ID=%s; /mnt/archil/.sdk/bin/python -u job.py" % disk)
    return archil_api.exec_on_disk(disk, "".join(parts), with_key=True)
