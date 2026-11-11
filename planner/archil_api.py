"""Minimal client for Archil's control-plane REST API (docs.archil.com/api-reference).

Reads ARCHIL_API_KEY and ARCHIL_REGION from the environment (.env). Never prints the key.
"""
import json
import os
import shlex
import urllib.error
import urllib.request

from . import env  # noqa: F401

BASES = {
    "aws-us-east-1": "https://control.green.us-east-1.aws.prod.archil.com",
    "aws-eu-west-1": "https://control.green.eu-west-1.aws.prod.archil.com",
    "aws-us-west-2": "https://control.green.us-west-2.aws.prod.archil.com",
    "gcp-us-central1": "https://control.blue.us-central1.gcp.prod.archil.com",
}


class ArchilError(Exception):
    pass


def region():
    r = os.environ.get("ARCHIL_REGION", "").strip()
    if r not in BASES:  # accept short forms such as "us-west-2"
        r = next((k for k in BASES if k.split("-", 1)[1] == r), r)
    if r not in BASES:
        raise ArchilError("ARCHIL_REGION must be one of %s (got %r)" % (sorted(BASES), r))
    return r


def base_url():
    return BASES[region()]


def _key():
    key = os.environ.get("ARCHIL_API_KEY", "").strip()
    if not key:
        raise ArchilError("ARCHIL_API_KEY is not set in .env")
    return key


def _scrub(text):
    for name in ("ARCHIL_API_KEY", "ARCHIL_DISK_TOKEN"):
        secret = os.environ.get(name, "").strip()
        if secret:
            text = text.replace(secret, "[%s]" % name)
    return text


def call(method, path, body=None, timeout=120):
    req = urllib.request.Request(
        base_url() + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + _key(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = _scrub(e.read()[:500].decode(errors="replace"))
        raise ArchilError("%s %s -> HTTP %d: %s" % (method, path, e.code, detail))


def list_disks():
    return call("GET", "/api/disks")[1]


def create_disk(name):
    return call("POST", "/api/disks", {"name": name})[1]


def exec_on_disk(disk_id, command, with_key=False, **opts):
    """Run a shell command (sh) in a microVM with the disk mounted at /mnt/archil.

    with_key=True exports ARCHIL_API_KEY and ARCHIL_REGION inside the VM so the archil
    CLI can create branches. The key is sent only to Archil and scrubbed from output.
    """
    if with_key:
        token = os.environ.get("ARCHIL_DISK_TOKEN", "").strip()
        command = "export ARCHIL_API_KEY=%s ARCHIL_REGION=%s%s; %s" % (
            shlex.quote(_key()), region(),
            (" ARCHIL_MOUNT_TOKEN=" + shlex.quote(token)) if token else "", command)
    data = call("POST", "/api/disks/%s/exec" % disk_id, dict(command=command, **opts), timeout=900)[1]
    out = data.get("data", data)
    for k in ("stdout", "stderr"):
        if isinstance(out.get(k), str):
            out[k] = _scrub(out[k])
    return out
