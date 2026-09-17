#!/usr/bin/env python3
"""Stamp a shipped workflow with ComfyUI's own `models` array, from the package's MODELS rows (2.1.0, spec D8).

    stamp_models.py <package dir> [--check]        the workflow the script's WF_NAME names; --check exits 1 when stale

The frontend's Missing Models dialog reads the top-level `models` array of a workflow (name, url, directory, optional
hash + hash_type) and offers to download what the graph names, from the hosts it allows (huggingface.co among them).
So every row with a URL becomes an entry: `name` is the file (a snapshot row's directory, with its trailing slash
dropped), `url` the row's URL (hf://owner/repo → https://huggingface.co/owner/repo), `directory` the row's
category/Family[/Purpose] path. LOCAL rows have no URL and are left out. Rows carry no sha256 today, so no hash is
written; the base's own download verifies bytes. Entries are sorted by name and the rest of the file is kept as it is:
the same indent, the same escaping, one trailing newline. Idempotent: stamping twice changes nothing.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def _array(text, name):
    m = re.search(r"^%s=\(([^\n]*)\)[^\n]*$" % re.escape(name), text, re.M)
    if not m:
        m = re.search(r"^%s=\((.*?)^\)" % re.escape(name), text, re.S | re.M)
    if not m:
        return []
    rows = []
    for ln in m.group(1).splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        rows.extend(re.findall(r'"([^"]*)"', s))
    return rows


def script_of(pkg):
    pkg = Path(pkg)
    s = pkg / (pkg.name + "-script.sh")
    if not s.exists():
        raise SystemExit("no %s" % s)
    return s


def entries(script_text):
    out = []
    for row in _array(script_text, "MODELS"):
        f = row.split("|")
        if len(f) < 6:
            continue
        cat, fam, purp, file, url = f[0], f[1], f[2], f[3], f[4]
        if url == "LOCAL" or not url:
            continue
        if url.startswith("hf://"):
            url = "https://huggingface.co/" + url[len("hf://"):]
        directory = "/".join(x for x in (cat, fam, purp) if x)
        name = file.rstrip("/")
        out.append({"name": name, "url": url, "directory": directory})
    out.sort(key=lambda e: (e["name"], e["directory"]))
    return out


def _indent_of(text):
    m = re.search(r"^( +)\"", text, re.M)
    return len(m.group(1)) if m else None


def stamp(pkg, check=False):
    s = script_of(pkg)
    t = s.read_text(encoding="utf-8")
    m = re.search(r'^[^#\n]*?\bWF_NAME="([^"]*)"', t, re.M)
    if not m or not m.group(1):
        print("%s: no WF_NAME, nothing to stamp" % Path(pkg).name)
        return 0
    wf = Path(pkg) / m.group(1)
    if not wf.exists():
        raise SystemExit("%s names %s, which is not beside it" % (s.name, m.group(1)))
    raw = wf.read_text(encoding="utf-8")
    doc = json.loads(raw)
    want = entries(t)
    if doc.get("models") == want:
        print("%s: current (%d model entries)" % (wf.name, len(want)))
        return 0
    if check:
        print("%s: STALE (%d entries wanted, %s present)" % (wf.name, len(want), len(doc.get("models") or []) if "models" in doc else "none"))
        return 1
    doc["models"] = want
    indent = _indent_of(raw)
    ensure_ascii = "\\u" in raw and not any(ord(c) > 127 for c in raw)
    out = json.dumps(doc, indent=indent, ensure_ascii=ensure_ascii) + "\n"
    wf.write_text(out, encoding="utf-8")
    print("%s: stamped %d model entries" % (wf.name, len(want)))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pkg")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    return stamp(a.pkg, check=a.check)


if __name__ == "__main__":
    sys.exit(main())
