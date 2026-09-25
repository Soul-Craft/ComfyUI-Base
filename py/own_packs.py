#!/usr/bin/env python3
"""A package's own node packs (VENDORED_PACKS, 3.2.0): the folders beside its script that ship in its zip.

    own_packs.py digest <dir>                          sha256 of the pack's content (paths and bytes)
    own_packs.py version <dir>                         the pyproject.toml [project] version, or "-"
    own_packs.py newer <a> <b>                         exit 0 when version a is newer than b
    own_packs.py mirror <src> <dst> <stage> <prev>     make <dst> exactly <src>: copy into <stage>, move the live
                                                       folder to <prev>, move the copy into place

The digest ignores __pycache__, .DS_Store and the base's own stamp (.comfy-base-own), so the same pack reads the same
wherever it sits. The mirror never builds anything inside custom_nodes: ComfyUI imports every folder there that is not
__pycache__ or *.disabled, dot-folders included, so the copy is made in <stage> (on the same volume, so each move is a
rename) and the live folder is replaced in two moves.
"""
import hashlib
import os
import re
import shutil
import sys

STAMP = ".comfy-base-own"
SKIP = {"__pycache__", ".DS_Store", STAMP}


def _files(root):
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP)
        for f in sorted(files):
            if f not in SKIP and not f.endswith(".pyc"):
                yield os.path.join(d, f)


def digest(root):
    h = hashlib.sha256()
    for p in _files(root):
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        with open(p, "rb") as fh:
            body = hashlib.sha256(fh.read()).hexdigest()
        h.update(("%s\0%s\0" % (rel, body)).encode("utf-8"))
    return h.hexdigest()


def version(root):
    p = os.path.join(root, "pyproject.toml")
    try:
        with open(p, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return "-"
    m = re.search(r'^\s*version\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else "-"


def _key(v):
    return tuple(int(x) if x.isdigit() else x for x in re.split(r"[.\-+]", v))


def newer(a, b):
    try:
        return _key(a) > _key(b)
    except TypeError:          # 1.0 vs 1.0rc1: a numeric part against a text part; compare as text
        return a > b


def mirror(src, dst, stage, prev):
    name = os.path.basename(dst.rstrip("/"))
    new = os.path.join(stage, name)
    shutil.rmtree(new, ignore_errors=True)
    os.makedirs(stage, exist_ok=True)
    shutil.copytree(src, new, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store", "*.pyc", STAMP), symlinks=False)
    if os.path.lexists(dst):
        shutil.rmtree(prev, ignore_errors=True)
        os.makedirs(os.path.dirname(prev), exist_ok=True)
        shutil.move(dst, prev)
    shutil.move(new, dst)


def main(argv):
    if len(argv) < 3:
        sys.exit(__doc__)
    cmd = argv[1]
    if cmd == "digest":
        print(digest(argv[2]))
    elif cmd == "version":
        print(version(argv[2]))
    elif cmd == "newer" and len(argv) == 4:
        return 0 if newer(argv[2], argv[3]) else 1
    elif cmd == "mirror" and len(argv) == 6:
        mirror(*argv[2:6])
    else:
        sys.exit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
