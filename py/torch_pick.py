#!/usr/bin/env python3
"""Newest torch on the cu130 index for one CPython tag.

Usage: torch_pick.py cp314  ->  prints 2.14.0

Genuine latest: no soak, no fallback. A fetch failure or an empty match exits 1 with the reason on
stderr, because a stale pick reported as "latest" is indistinguishable from success in the output.
"""
import re
import sys
import urllib.request
from pathlib import Path

INDEX = "https://download.pytorch.org/whl/cu130/torch/"


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def main():
    cp = sys.argv[1] if len(sys.argv) > 1 else ""
    if not re.fullmatch(r"cp3\d\d?", cp):
        die("usage: torch_pick.py cpXY   (e.g. cp314)")
    try:
        with urllib.request.urlopen(INDEX, timeout=60) as r:
            idx = r.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 - any failure here must stop the run
        die("could not read %s: %s" % (INDEX, e))
    vers = set(re.findall(r"torch-(\d+\.\d+\.\d+)\+cu130-%s-%s-manylinux" % (cp, cp), idx))
    if not vers:
        die("no cu130 torch wheel for %s on %s" % (cp, INDEX))
    print(max(vers, key=lambda v: tuple(int(x) for x in v.split("."))))


if __name__ == "__main__":
    main()
