#!/usr/bin/env python3
"""Smoke a live ComfyUI against a workflow: every server-executable node type the workflow uses must be registered.

Usage: smoke.py <workflow.json> <host:port> [<workflow_api.json>]
Exit 0 when every type is registered (and, with an API export, the render finishes); 1 otherwise.
Subgraph instances carry their definition id as their type: the frontend expands them before the server sees
the graph, so the nodes INSIDE the definitions are what gets checked. Frontend-only types never reach the server.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# One list, in canvas.py: this file and the canvas tools had drifted apart, and the tools
# were the ones that ended up refusing a valid graph.
from canvas import FRONTEND_ONLY_TYPES as FRONTEND_ONLY


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: smoke.py <workflow.json> <host:port> [<workflow_api.json>]")
    wf_path, host = sys.argv[1], sys.argv[2]
    api = sys.argv[3] if len(sys.argv) > 3 else ""
    base = "http://" + host

    def get(path, timeout=15):
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            return json.loads(r.read())

    try:
        stats = get("/system_stats")
    except Exception as e:  # noqa: BLE001
        print("x ComfyUI is not reachable at %s [%s]" % (base, e))
        sys.exit(1)
    dev = (stats.get("devices") or [{}])[0]
    print("ok server up · ComfyUI %s · python %s · %s · %s GB VRAM" % (
        stats.get("system", {}).get("comfyui_version", "?"), str(stats.get("system", {}).get("python_version", "?")).split()[0],
        dev.get("name", "gpu?"), round(dev.get("vram_total", 0) / 2 ** 30)))
    obj = get("/object_info", timeout=120)
    wf = json.loads(Path(wf_path).read_text(encoding="utf-8"))
    subgraph_ids = {str(s.get("id")) for s in wf.get("definitions", {}).get("subgraphs", [])}
    types = {n["type"] for n in wf.get("nodes", [])} | {n["type"] for s in wf.get("definitions", {}).get("subgraphs", []) for n in s.get("nodes", [])}
    exec_types = sorted(t for t in types if t not in FRONTEND_ONLY and t not in subgraph_ids)
    missing = [t for t in exec_types if t not in obj]
    if missing:
        print("x node types MISSING on the server (pack not loaded — see the import check):")
        for t in missing:
            print("    missing: %s" % t)
        sys.exit(1)
    print("ok all %d server-executable node types registered (%d frontend-only/subgraph skipped)" % (len(exec_types), len(types) - len(exec_types)))
    if not api or not os.path.isfile(api):
        print("- no API export beside the workflow — preflight only (Workflow menu > Export (API) enables the render smoke)")
        sys.exit(0)
    prompt = json.loads(Path(api).read_text(encoding="utf-8"))
    if "nodes" in prompt:
        print("x %s is UI-format, not API-format — use the UI's 'Export (API)' option." % api)
        sys.exit(1)
    data = json.dumps({"prompt": prompt}).encode()
    req = urllib.request.Request(base + "/prompt", data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            pid = json.loads(r.read())["prompt_id"]
    except urllib.error.HTTPError as e:
        print("x server rejected the prompt:")
        print(e.read().decode()[:2000])
        sys.exit(1)
    print("-> queued prompt %s — waiting (up to 60 min)…" % pid)
    t0 = time.time()
    while time.time() - t0 < 3600:
        time.sleep(10)
        hist = get("/history/%s" % pid, timeout=30).get(pid)
        if not hist:
            continue
        status = hist.get("status", {})
        if status.get("completed"):
            outs = [o.get("filename") for node in hist.get("outputs", {}).values() for k in ("images", "gifs", "video", "audio") for o in (node.get(k) or [])]
            print("ok render finished in %ds — outputs: %s" % (round(time.time() - t0), ", ".join(x for x in outs if x) or "see output folder"))
            sys.exit(0)
        if status.get("status_str") == "error":
            print("x execution error:")
            for m in status.get("messages", []):
                if m[0] == "execution_error":
                    print("   ", json.dumps(m[1])[:1500])
            sys.exit(1)
    print("x timed out after 60 min")
    sys.exit(1)


if __name__ == "__main__":
    main()
