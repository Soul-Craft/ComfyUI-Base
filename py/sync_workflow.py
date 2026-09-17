#!/usr/bin/env python3
"""Sync a workflow's model widget paths to the canonical library layout.

Usage: sync_workflow.py <workflow.json> <models dir> <rows.tsv> <cats.tsv> [--dry]
  rows.tsv  category <TAB> Family <TAB> Purpose <TAB> file      (one line per MODELS row; empty fields allowed)
  cats.tsv  NodeType <TAB> category                             (which library folder a loader's dropdown reads)

For every loader node (root graph, every subgraph definition, and subgraph INSTANCES whose promoted widgets
carry model names without a category), each model-shaped string is normalised (backslashes -> /) and rewritten
to the row's dest "Family/Purpose/file" when the basename matches a row of that category. Names with no row
fall back to what is on disk in that category (a file already at its path is left alone). Names that resolve
to nothing are reported as ACTIVE (node mode 0 and, for a Power Lora row, on=true) or BYPASSED. The file is
rewritten only when something changed, with the original indent (or compact, if it was one line) and a one-time
.bak. Bypassed loaders are rewritten too when their file has a row; only unresolved names are split by mode. Never moves files.

--placeholder NAME (repeatable): a basename the package ships ON PURPOSE with nothing behind it (a "put your LoRA
here" slot). Reported as PLACEHOLDER, never counted as missing, never rewritten.

Machine-readable lines for the bash side:
  "  ACTIVE <where> <id> <value>", "  BYPASSED <where> <id> <value>", "  PLACEHOLDER <where> <id> <value>", and finally
  "SYNC fixed=<n> active_missing=<n> bypassed_missing=<n> placeholders=<n>"
"""
import json
import os
import shutil
import sys
from pathlib import Path

EXTS = (".safetensors", ".pt", ".pth", ".gguf", ".ckpt", ".bin", ".onnx")
LOCAL_PREFIX = "[local] "   # a few loaders (QwenVL GGUF) prefix a local file this way; the prefix is kept


def main():
    argv = sys.argv[1:]
    placeholders = set()
    while "--placeholder" in argv:
        i = argv.index("--placeholder")
        placeholders.add(argv[i + 1].lower())
        del argv[i:i + 2]
    args = [a for a in argv if a != "--dry"]
    dry = "--dry" in argv
    if len(args) < 4:
        sys.exit("usage: sync_workflow.py <workflow.json> <models dir> <rows.tsv> <cats.tsv> [--dry]")
    wf, m, rows_p, cats_p = args[:4]
    rows = {}            # (category, lower basename) -> dest rel within the category
    by_name = {}         # lower basename -> set of (category, dest) for category-less lookups
    for ln in open(rows_p, encoding="utf-8"):
        f = ln.rstrip("\n").split("\t")
        if len(f) < 4 or not f[0] or not f[3]:
            continue
        cat, fam, purp, name = f[0], f[1], f[2], f[3]
        dest = "/".join(p for p in (fam, purp, name) if p)
        rows[(cat, name.lower())] = dest
        by_name.setdefault(name.lower(), set()).add((cat, dest))
    cats = {}
    for ln in open(cats_p, encoding="utf-8"):
        f = ln.rstrip("\n").split("\t")
        if len(f) >= 2 and f[0]:
            cats[f[0]] = f[1]
    # per-category index of what is already on disk: lower basename -> relative path as ComfyUI lists it
    index = {}
    for cat in set(cats.values()) | {c for c, _ in rows}:
        root = os.path.join(m, cat)
        idx = {}
        for dp, _, fns in os.walk(root):
            for fn in fns:
                if fn.lower().endswith(EXTS):
                    idx.setdefault(fn.lower(), os.path.relpath(os.path.join(dp, fn), root))
        index[cat] = idx

    raw = Path(wf).read_text(encoding="utf-8")
    d = json.loads(raw)
    lines = raw.split("\n")
    # keep the file's own shape: its indent when it has one, one compact line when it is one (a compact one stays compact)
    indent = 2
    if len([ln for ln in lines if ln.strip()]) <= 1:
        indent = None
    elif len(lines) > 1:
        lead = len(lines[1]) - len(lines[1].lstrip(" "))
        indent = lead if lead in (1, 2, 4) else 2
    trailing_nl = raw.endswith("\n")
    fixed, active_missing, bypassed_missing, placeheld = [], [], [], []

    def resolve(cat, value):
        """-> (new value or None, status) where status is 'ok' | 'fixed' | 'missing'."""
        prefix = ""
        v = value
        if v.startswith(LOCAL_PREFIX):
            # "[local] <name>" is how a pack labels a file it found itself in its own folder scan; the label is a bare
            # basename by construction, so it is checked (a row or a file of that name in the category) but never rewritten.
            base = os.path.basename(v[len(LOCAL_PREFIX):].replace("\\", "/")).lower()
            known = (cat is not None and (cat, base) in rows) or base in by_name or any(base in idx for idx in index.values())
            return (value, "ok") if known else (None, "missing")
        want = v.replace("\\", "/")
        base = os.path.basename(want).lower()
        if cat is not None:
            hit = rows.get((cat, base))
            if hit is None and os.path.isfile(os.path.join(m, cat, want)):
                hit = want                                  # already where it lives; only the slashes may differ
            if hit is None:
                hit = index.get(cat, {}).get(base)
        else:
            cands = by_name.get(base, set())
            if len(cands) == 1:
                hit = next(iter(cands))[1]
            elif len(cands) > 1:
                hit = None
            else:
                disk = {c: idx[base] for c, idx in index.items() if base in idx}
                hit = next(iter(disk.values())) if len(disk) == 1 else None
        if hit is None:
            return None, "missing"
        new = prefix + hit
        return (new, "fixed") if new != value else (value, "ok")

    def is_model(v):
        return isinstance(v, str) and v.lower().endswith(EXTS)

    def report(where, nid, value, active):
        if os.path.basename(value.replace("\\", "/")).lower() in placeholders:
            placeheld.append("  PLACEHOLDER %s %s %s" % (where, nid, value))
            return
        (active_missing if active else bypassed_missing).append("  %s %s %s %s" % ("ACTIVE" if active else "BYPASSED", where, nid, value))

    def sync_values(n, where, cat):
        active = n.get("mode", 0) == 0
        for key in ("widgets_values", "widgets_values_named"):
            wv = n.get(key)
            items = list(enumerate(wv)) if isinstance(wv, list) else list(wv.items()) if isinstance(wv, dict) else []
            for k, v in items:
                if isinstance(v, dict) and is_model(v.get("lora", "")):      # Power Lora Loader rows
                    row_active = active and bool(v.get("on"))
                    new, st = resolve("loras", v["lora"])
                    if st == "fixed":
                        v["lora"] = new                                        # rewritten whether the row is on or off: the file is installed
                        if key == "widgets_values":
                            fixed.append("[%s] %s lora row  %s  ->  %s" % (where, n["id"], v.get("lora") if st != "fixed" else new, new))
                    elif st == "missing" and key == "widgets_values":
                        report(where, n["id"], v["lora"], row_active)
                    continue
                if not is_model(v):
                    continue
                # A bypassed or muted loader is rewritten too when its file has a row: a mode radio (quality tiers,
                # engine variants) ships every option but one bypassed, and the user flips them. Only what resolves
                # to NOTHING is treated by mode: an ACTIVE miss fails the run, a BYPASSED one is reported.
                new, st = resolve(cat, v)
                if st == "fixed":
                    wv[k] = new
                    if key == "widgets_values":
                        fixed.append("[%s] %s:%s  %s  ->  %s" % (where, n["id"], str(n.get("title") or n.get("type"))[:28], v, new))
                elif st == "missing" and key == "widgets_values":
                    report(where, n["id"], v, active)

    subgraph_ids = {str(sg.get("id")) for sg in d.get("definitions", {}).get("subgraphs", [])}
    for n in d.get("nodes", []):
        t = str(n.get("type"))
        if t in subgraph_ids:
            sync_values(n, "subgraph-instance", None)
        elif t == "Power Lora Loader (rgthree)" or t in cats:
            sync_values(n, "main", cats.get(t))
    for sg in d.get("definitions", {}).get("subgraphs", []):
        where = str(sg.get("name") or str(sg.get("id"))[:8])[:12]
        for n in sg.get("nodes", []):
            t = str(n.get("type"))
            if t in subgraph_ids:
                sync_values(n, where, None)
            elif t == "Power Lora Loader (rgthree)" or t in cats:
                sync_values(n, where, cats.get(t))

    # Power Lora rows: the fixed-list line above recorded the value after rewrite; fine for the report.
    for f in fixed:
        print("  fixed  " + f)
    for ln in active_missing + bypassed_missing + placeheld:
        print(ln)
    if fixed and not dry:
        if not os.path.exists(wf + ".bak"):
            shutil.copy2(wf, wf + ".bak")
        text = json.dumps(d, indent=indent, ensure_ascii=False)
        with open(wf, "w", encoding="utf-8") as fh:
            fh.write(text + ("\n" if trailing_nl else ""))
        print("  -> %s updated in place (backup: .bak)" % wf)
    elif fixed:
        print("  -> --dry: %d change(s) not written" % len(fixed))
    else:
        print("  -> %s: no changes needed, file untouched" % wf)
    print("SYNC fixed=%d active_missing=%d bypassed_missing=%d placeholders=%d" % (len(fixed), len(active_missing), len(bypassed_missing), len(placeheld)))


if __name__ == "__main__":
    main()
