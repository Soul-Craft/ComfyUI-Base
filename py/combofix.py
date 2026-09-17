#!/usr/bin/env python3
"""Reconcile a workflow's stored COMBO values against a LIVE ComfyUI.

Usage: combofix.py <host:port> <workflow.json>... [--check-only]

A node pack can rename its dropdown options between the last test and this install; ComfyUI rejects a COMBO
value that is not in the list, so the graph loads, the user queues, and the render dies. Matching is by VALUE,
never by widget index (a V3 autogrow node stores its widgets in the order its own JS builds them). A renamed
option that shares its head (the text before the first "·") with exactly one offered option is repaired;
anything else that plainly came from a combo is reported UNRESOLVED. File-list values belong to the model stage.
Prints "COMBOFIX <repaired> repaired <unresolved> unresolved" per file; exit 2 if anything is unresolved.
--check-only reports and never writes (a restart is pending, so the server is the OLD code).
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

MODEL_EXT = (".safetensors", ".pt", ".pth", ".gguf", ".ckpt", ".bin", ".pkl", ".onnx")
# One list, in canvas.py. Three copies of this set existed and two had drifted (2.0.33).
from canvas import FRONTEND_ONLY_TYPES as FRONTEND_ONLY


def combo_lists(info):
    """Every option list this node type declares, in both /object_info shapes (old: list in the type slot;
    ComfyUI 0.34.x: ["COMBO", {"options": [...]}])."""
    out = []
    for sect in ("required", "optional"):
        for _name, spec in (info.get("input", {}).get(sect) or {}).items():
            typ = spec[0] if isinstance(spec, (list, tuple)) else spec
            o = spec[1] if isinstance(spec, (list, tuple)) and len(spec) > 1 and isinstance(spec[1], dict) else {}
            opts = typ if isinstance(typ, list) else (o.get("options") if typ == "COMBO" else None)
            if opts:
                out.append([x for x in opts if isinstance(x, str)])
    return out


WIDGET_TYPES = ("INT", "FLOAT", "STRING", "BOOLEAN")


def widget_sequence(info):
    """The widgets the frontend creates for a node type, in order — what widgets_values is restored INTO, positionally:
    every required then optional input of a widget type (a COMBO list, INT, FLOAT, STRING, BOOLEAN), forceInput skipped,
    an INT/FLOAT with control_after_generate (or named seed / noise_seed) followed by that control, an upload COMBO
    followed by its upload button. Non-widget types (CLIP, IMAGE, custom sockets) create no widget."""
    seq = []
    for sect in ("required", "optional"):
        for name, spec in (info.get("input", {}).get(sect) or {}).items():
            typ = spec[0] if isinstance(spec, (list, tuple)) else spec
            o = spec[1] if isinstance(spec, (list, tuple)) and len(spec) > 1 and isinstance(spec[1], dict) else {}
            if o.get("forceInput"):
                continue
            parts = typ.split(",") if isinstance(typ, str) else []
            if isinstance(typ, list):
                seq.append((name, "COMBO", [x for x in typ if isinstance(x, str)]))
            elif typ == "COMBO":
                seq.append((name, "COMBO", [x for x in (o.get("options") or []) if isinstance(x, str)]))
            elif typ == "COMFY_DYNAMICCOMBO_V3":
                seq.append((name, "DYNAMIC", None))          # a selector plus the sub-widgets of the chosen option: unknown from here on
                return seq
            elif typ in WIDGET_TYPES:
                seq.append((name, typ, None))
            elif len(parts) > 1 and any(x in WIDGET_TYPES or x == "COMBO" for x in parts):
                seq.append((name, "MULTI", None))            # "STRING,COMBO": one widget, any value shape
            else:
                continue
            if typ in ("INT", "FLOAT") and (o.get("control_after_generate") or name in ("seed", "noise_seed")):
                seq.append(("control_after_generate", "CONTROL", ["fixed", "increment", "decrement", "randomize"]))
            if (isinstance(typ, list) or typ == "COMBO") and any(o.get(k) for k in ("image_upload", "video_upload", "audio_upload")):
                seq.append(("upload", "UPLOAD", None))
    return seq


def position_faults(n, info):
    """Stored values that sit in a position the server would reject: a COMBO position holding a value its list does
    not offer while another position's list does (a shift, not a rename), an INT/FLOAT position holding text, a
    BOOLEAN position holding text. Only a positional list (widgets_values as a list) can be judged."""
    w = n.get("widgets_values")
    if not isinstance(w, list):
        return []
    seq = widget_sequence(info)
    offered_elsewhere = {x for _name, kind, opts in seq if opts for x in opts}
    faults = []
    for i, (name, kind, opts) in enumerate(seq):
        if i >= len(w):
            break
        v = w[i]
        if kind in ("COMBO", "CONTROL") and isinstance(v, str) and opts and v not in opts:
            if "/" in v or "\\" in v or v.lower().endswith(MODEL_EXT):
                continue                                   # a model file a download provides: the membership pass judges it
            if v in offered_elsewhere or v == "{}":        # an EMPTY value is a slot left for the user (LoadImage), the membership pass's business
                faults.append((name, v))
        elif kind in ("INT", "FLOAT") and isinstance(v, str):
            faults.append((name, v))
        elif kind == "BOOLEAN" and isinstance(v, str) and v not in ("true", "false", "True", "False"):
            faults.append((name, v))
    return faults


def head(v):
    return v.split("·")[0].strip()


def process(obj, path, check_only):
    raw = Path(path).read_text(encoding="utf-8")
    wf = json.loads(raw)
    lines = raw.split("\n")
    indent = 2
    if len(lines) > 1:
        lead = len(lines[1]) - len(lines[1].lstrip(" "))
        indent = lead if lead in (1, 2, 4) else 2
    nodes = list(wf.get("nodes", [])) + [n for sg in wf.get("definitions", {}).get("subgraphs", []) for n in sg.get("nodes", [])]
    fixed, broken = [], []
    for n in nodes:
        info = obj.get(n.get("type"))
        if not info or n.get("type") in FRONTEND_ONLY:
            continue
        for name, v in position_faults(n, info):       # a stored order the server rejects: reported, never repaired by guessing
            broken.append((n["id"], n["type"], v, "position: the server reads %r there — the stored order is an older pack's" % name))
        lists = combo_lists(info)
        if not lists:
            continue
        offered = {x for lst in lists for x in lst}
        for store in ("widgets_values", "widgets_values_named"):
            w = n.get(store)
            keys = range(len(w)) if isinstance(w, list) else (list(w.keys()) if isinstance(w, dict) else [])
            for k in keys:
                v = w[k]
                if not isinstance(v, str) or not v:
                    continue
                if "/" in v or "\\" in v or v.lower().endswith(MODEL_EXT):
                    continue
                if v in offered:
                    continue
                cand = sorted({x for x in offered if head(x) == head(v)})
                if len(cand) == 1:
                    if not check_only:
                        w[k] = cand[0]
                    fixed.append((n["id"], n["type"], store, v, cand[0]))
                elif len(cand) > 1:
                    broken.append((n["id"], n["type"], v, "%d options share that prefix" % len(cand)))
                elif any(any(head(x) == head(v) for x in lst) for lst in lists):
                    broken.append((n["id"], n["type"], v, "not offered"))
    for nid, t, store, old, new in fixed:
        print("    %s #%s %s [%s]\n        %r\n     -> %r" % ("would repair" if check_only else "repaired", nid, t, store, old, new))
    for nid, t, v, why in broken:
        print("    UNRESOLVED #%s %s = %r (%s)" % (nid, t, v, why))
    if fixed and not check_only:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(wf, indent=indent, ensure_ascii=False) + ("\n" if raw.endswith("\n") else ""))
    print("COMBOFIX %d repaired %d unresolved" % (len(fixed), len(broken)))
    return bool(broken)


def main():
    args = [a for a in sys.argv[1:] if a != "--check-only"]
    check_only = "--check-only" in sys.argv
    if len(args) < 2:
        sys.exit("usage: combofix.py <host:port> <workflow.json>... [--check-only]")
    host, files = args[0], args[1:]
    with urllib.request.urlopen("http://%s/object_info" % host, timeout=180) as r:
        obj = json.loads(r.read())
    bad = False
    for f in files:
        if os.path.isfile(f):
            bad = process(obj, f, check_only) or bad
    sys.exit(2 if bad else 0)


if __name__ == "__main__":
    main()
