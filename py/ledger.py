#!/usr/bin/env python3
"""Read the per-package manifests the base writes under <state>/packages/*.manifest.

Usage: ledger.py <state dir> status|json

Manifest format (tab-separated):
  pkg=<id>  name=<n>  version=<v>  base=<v>  ts=<ts>  status=<ok|failed>
  comfy=<v>  venv=<path>  python=<v>  torch=<v>  workflow=<path|->  script=<path|->  hooks=<a,b|->
  pack  <dir>  <sha>  <path>  <url>
  model  <dest rel>  <bytes>
  superseded  <basename>
  own  <dir>  <version>  <digest>            (3.2.0: a package's own node pack, as installed)
"""
import glob
import json
import os
import sys


def parse(path):
    d = {"packs": [], "models": [], "superseded": [], "own": []}
    for ln in open(path, encoding="utf-8"):
        ln = ln.rstrip("\n")
        if not ln:
            continue
        f = ln.split("\t")
        if f[0] == "pack" and len(f) >= 4:
            d["packs"].append({"dir": f[1], "sha": f[2], "path": f[3], "url": f[4] if len(f) > 4 else ""})
        elif f[0] == "model" and len(f) >= 3:
            d["models"].append({"dest": f[1], "bytes": int(f[2]) if f[2].isdigit() else 0})
        elif f[0] == "own" and len(f) >= 4:
            d["own"].append({"dir": f[1], "version": f[2], "digest": f[3]})
        elif f[0] == "superseded" and len(f) >= 2:
            d["superseded"].append(f[1])
        else:
            for kv in f:
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    d[k] = v
    return d


def load(state):
    return [parse(p) for p in sorted(glob.glob(os.path.join(state, "packages", "*.manifest")))]


def gb(b):
    return "%.1f" % (b / 1e9)


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: ledger.py <state dir> status|json")
    state, mode = sys.argv[1], sys.argv[2]
    pkgs = load(state)
    if mode == "json":
        print(json.dumps(pkgs, indent=2))
        return
    if not pkgs:
        print("  no packages installed yet (nothing under %s/packages)" % state)
        return
    print("  %-28s %-10s %-8s %-16s %-7s %s" % ("package", "version", "base", "installed", "status", "models"))
    fam_total, seen = {}, {}
    for p in pkgs:
        by_fam = {}
        for m in p["models"]:
            parts = m["dest"].split("/")
            fam = parts[1] if len(parts) > 2 else parts[0]
            by_fam[fam] = by_fam.get(fam, 0) + m["bytes"]
            if m["dest"] not in seen:
                seen[m["dest"]] = p.get("pkg", "?")
                fam_total[fam] = fam_total.get(fam, 0) + m["bytes"]
        models = ", ".join("%s %s GB" % (f, gb(b)) for f, b in sorted(by_fam.items())) or "-"
        print("  %-28s %-10s %-8s %-16s %-7s %s" % (p.get("name", p.get("pkg", "?"))[:28], p.get("version", "?"), p.get("base", "?"), p.get("ts", "?"), p.get("status", "?"), models))
    shared = {}
    for p in pkgs:
        for m in p["models"]:
            shared.setdefault(m["dest"], []).append(p.get("pkg", "?"))
    both = {d: v for d, v in shared.items() if len(v) > 1}
    print("  library: %s" % (", ".join("%s %s GB" % (f, gb(b)) for f, b in sorted(fam_total.items())) or "-"))
    if both:
        print("  shared by more than one package: %d file(s), %s GB stored once" % (len(both), gb(sum(next(m["bytes"] for p in pkgs for m in p["models"] if m["dest"] == d) for d in both))))
    hooks = [(p.get("pkg"), p.get("hooks")) for p in pkgs if p.get("hooks") and p.get("hooks") != "-" and "pkg_post_venv" in p.get("hooks", "")]
    if hooks:
        print("  packages with a post-venv hook (re-run after a venv rebuild): %s" % ", ".join(h[0] for h in hooks))


if __name__ == "__main__":
    main()
