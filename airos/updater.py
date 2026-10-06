#!/usr/bin/env python3
"""Air OS updater. Installs the newest GitHub release of Air OS, then restarts the TV session on it.

Run as root by airos-update.service: daily from airos-update.timer, or on demand from Settings → Updates.
The repository comes from "update_repo" in services.json (e.g. "yourname/AirTV").
"""
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATUS = Path("/var/lib/airos/update.json")  # read by server.py for Settings → Updates


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def status(**fields):
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(fields), encoding="utf-8")
    tmp.chmod(0o644)
    tmp.replace(STATUS)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "AirOS-updater", "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=60)


def main():
    current = (HERE / "VERSION").read_text().strip()
    repo = json.loads((HERE / "services.json").read_text(encoding="utf-8")).get("update_repo", "").strip()
    base = {"current": current, "checked": int(time.time())}
    if not repo:
        status(state="off", **base)
        return
    status(state="checking", **base)
    try:
        with fetch(f"https://api.github.com/repos/{repo}/releases/latest") as r:
            release = json.load(r)
        latest = release["tag_name"].lstrip("vV")
        if version_tuple(latest) <= version_tuple(current):
            status(state="uptodate", latest=latest, **base)
            return
        status(state="installing", latest=latest, **base)
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp, "release.tar.gz")
            with fetch(release["tarball_url"]) as r:
                archive.write_bytes(r.read())
            with tarfile.open(archive) as t:
                try:
                    t.extractall(tmp, filter="data")
                except TypeError:  # Python without extraction filters
                    t.extractall(tmp)
            src = next(p for p in Path(tmp).iterdir() if (p / "install.sh").is_file())
            if subprocess.run(["bash", str(src / "install.sh")]).returncode:
                raise RuntimeError("The installer failed (see: journalctl -u airos-update)")
        status(state="updated", latest=latest, **{**base, "current": latest})
        print(f"Updated Air OS {current} -> {latest}; restarting the TV session", flush=True)
        subprocess.run(["systemctl", "restart", "getty@tty1.service"])
    except Exception as e:
        status(state="error", error=str(e)[:300], **base)
        print("Update failed:", e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
