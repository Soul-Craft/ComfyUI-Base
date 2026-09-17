# /// script
# requires-python = ">=3.12"
# ///
"""Queue a render on the pod, wait for it, bring the frames back — and time it honestly.

    uv run "base/comfyui-base/_build/pod/render.py" --prompt p.json --label draft-a [--set 940.unet_name=X] [--runs 3]

Nothing here is one package's: it takes an API prompt (the shape `app.graphToPrompt()` returns, which
`_build/canvas/probe.py` already dumps per radio state), applies widget overrides by node id, posts it to
ComfyUI on the pod, polls until it lands, and scp's the outputs back.

Why over ssh rather than a tunnel: the pod answers on 127.0.0.1:8188 from its own shell, so `curl` there needs
no forward, no local port to collide with, and no session to keep alive between calls.

Timing follows §11's instrumentation rather than a stopwatch: one warm-up discarded, N timed runs, the MEDIAN
reported. A single run is dominated by Triton compile, autotune and filesystem cache state, so a lone number
is not a measurement -- it is the first of the ones you were going to throw away."""
import argparse, json, os, pathlib, statistics, subprocess, sys, time, uuid
from pathlib import Path

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from podctl import SSH_CMD, SCP_CMD, PodctlError                    # noqa: E402

HOST = "runpod"


def ssh(cmd, timeout=180):
    r = subprocess.run(SSH_CMD + [HOST, cmd], capture_output=True, text=True, timeout=timeout)
    if r.returncode: raise PodctlError("ssh failed: %s" % (r.stderr.strip()[-400:] or r.stdout.strip()[-400:]))
    return r.stdout


def put(text, remote):
    """Write a blob to the pod without quoting it through a shell."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        f.write(text); local = f.name
    try:
        r = subprocess.run(SCP_CMD + [local, "%s:%s" % (HOST, remote)], capture_output=True, text=True, timeout=180)
        if r.returncode: raise PodctlError("scp failed: %s" % r.stderr.strip()[-300:])
    finally:
        os.unlink(local)


def apply_set_files(prompt, pairs):
    """--set-file 1035.value=brief.txt — for values a command line cannot hold.

    A brief is paragraphs with newlines and quotes in it; putting that through argv is how you end up
    debugging your own shell quoting instead of the render."""
    for s in pairs or []:
        key, _, path = s.partition("=")
        nid, _, field = key.partition(".")
        if nid not in prompt: raise SystemExit("--set-file %s: node %s is not in this prompt" % (s, nid))
        prompt[nid].setdefault("inputs", {})[field] = pathlib.Path(path).read_text(encoding="utf-8")
    return prompt


def apply_sets(prompt, sets):
    """--set 940.unet_name=Example/x.safetensors  ->  prompt["940"]["inputs"]["unet_name"] = "..."

    Values are read as JSON first so numbers and booleans stay typed, then fall back to the literal string,
    which is what a filename is. A node id that is not in the prompt is an error, not a silent no-op: the
    usual cause is that the node was bypassed in the state this prompt came from, and quietly ignoring that
    would mean rendering something other than what was asked for."""
    for s in sets or []:
        key, _, raw = s.partition("=")
        nid, _, field = key.partition(".")
        if nid not in prompt: raise SystemExit("--set %s: node %s is not in this prompt (bypassed in this state?)" % (s, nid))
        try: val = json.loads(raw)
        except json.JSONDecodeError: val = raw
        prompt[nid].setdefault("inputs", {})[field] = val
    return prompt


def drop_nodes(prompt, drops):
    """--drop 1435:1199=1435:1198 — remove a node and send its consumers to another source instead.

    For a node that REQUIRES an input the current radio state cannot give it. The graph's own idiom is that a
    bypassed chain resolves to nothing and an Any Switch falls through (§0 rule 3); a node that raises on empty
    has no such escape, so the honest thing at the prompt level is to take it out and reconnect around it.
    Use it to characterise a defect, never to hide one -- what it removes should be recorded."""
    for d in drops or []:
        gone, _, src = d.partition("=")
        if gone not in prompt: raise SystemExit("--drop %s: node %s is not in this prompt" % (d, gone))
        if src and src not in prompt: raise SystemExit("--drop %s: replacement %s is not in this prompt" % (d, src))
        del prompt[gone]
        for n in prompt.values():
            for k, v in list((n.get("inputs") or {}).items()):
                if isinstance(v, list) and len(v) == 2 and str(v[0]) == gone:
                    if src: n["inputs"][k] = [src, v[1]]
                    else:   n["inputs"].pop(k)
    return prompt


def queue(prompt, remote_json="/tmp/base_render_prompt.json"):
    """POST /prompt and return its id. node_errors comes back on a rejected graph -- surface it verbatim."""
    cid = uuid.uuid4().hex
    put(json.dumps({"prompt": prompt, "client_id": cid}), remote_json)
    out = ssh("curl -s -m 60 -X POST -H 'Content-Type: application/json' --data-binary @%s http://127.0.0.1:8188/prompt" % remote_json)
    try: got = json.loads(out)
    except json.JSONDecodeError: raise PodctlError("POST /prompt did not return JSON: %s" % out[:400])
    if "prompt_id" not in got:
        raise PodctlError("ComfyUI refused the prompt:\n%s" % json.dumps(got, indent=2)[:2000])
    return got["prompt_id"]


def wait(pid, every=5, timeout=3600):
    """Poll /history until the id appears. Returns (history entry, wall seconds)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        out = ssh("curl -s -m 30 http://127.0.0.1:8188/history/%s" % pid)
        try: h = json.loads(out)
        except json.JSONDecodeError: h = {}
        if pid in h:
            e = h[pid]
            st = (e.get("status") or {})
            if st.get("status_str") == "error" or st.get("completed") is False and st.get("messages"):
                bad = [m for m in st.get("messages", []) if m and m[0] in ("execution_error", "execution_interrupted")]
                if bad: raise PodctlError("render failed:\n%s" % json.dumps(bad, indent=2)[:2000])
            return e, time.time() - t0
        time.sleep(every)
    raise PodctlError("render did not finish within %ss" % timeout)


def outputs_of(entry):
    """Every file the run wrote, as (subfolder, filename, type)."""
    got = []
    for _nid, out in (entry.get("outputs") or {}).items():
        for kind in ("images", "gifs", "videos", "audio"):
            for f in out.get(kind, []) or []:
                if f.get("filename"): got.append((f.get("subfolder", ""), f["filename"], f.get("type", "output")))
    return got


def fetch(files, dest, comfy="/workspace/ComfyUI"):
    dest = pathlib.Path(dest); dest.mkdir(parents=True, exist_ok=True)
    got = []
    for sub, name, kind in files:
        remote = "%s/%s/%s" % (comfy, {"output": "output", "temp": "temp", "input": "input"}.get(kind, "output"),
                               ("%s/%s" % (sub, name)) if sub else name)
        local = dest / name
        r = subprocess.run(SCP_CMD + ["%s:%s" % (HOST, remote.replace(" ", "\\ ")), str(local)],
                           capture_output=True, text=True, timeout=900)
        if r.returncode == 0: got.append(local)
        else: print("  ! could not fetch %s: %s" % (remote, r.stderr.strip()[-200:]))
    return got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True, help="API prompt JSON (probe.py's prompt-<state>.json)")
    ap.add_argument("--label", default="run", help="names the local output folder")
    ap.add_argument("--set", action="append", dest="sets", metavar="NODE.input=VALUE")
    ap.add_argument("--set-file", action="append", dest="set_files", metavar="NODE.input=PATH", help="value read from a file")
    ap.add_argument("--drop", action="append", dest="drops", metavar="NODE[=SOURCE]", help="remove a node; rewire its consumers to SOURCE")
    ap.add_argument("--runs", type=int, default=1, help="timed runs AFTER one discarded warm-up (§11)")
    ap.add_argument("--no-warmup", action="store_true", help="skip the warm-up; the timing is then not a measurement")
    ap.add_argument("--out", default=None, help="default _build/podruns/<date>/<label>")
    ap.add_argument("--comfy", default="/workspace/ComfyUI")
    a = ap.parse_args()

    prompt = drop_nodes(apply_set_files(apply_sets(json.loads(Path(a.prompt).read_text(encoding="utf-8")), a.sets), a.set_files), a.drops)
    out = pathlib.Path(a.out or (pathlib.Path.cwd() / "_build" / "podruns" / time.strftime("%Y-%m-%d") / a.label))
    print("→ %s  (%d nodes)" % (a.label, len(prompt)))

    if not a.no_warmup:
        print("  warm-up (discarded) …", end="", flush=True)
        _e, s = wait(queue(prompt)); print(" %.1fs" % s)

    times, last = [], None
    for i in range(a.runs):
        print("  run %d/%d …" % (i + 1, a.runs), end="", flush=True)
        last, s = wait(queue(prompt)); times.append(s); print(" %.1fs" % s)

    files = outputs_of(last) if last else []
    got = fetch(files, out, a.comfy) if files else []
    med = statistics.median(times) if times else float("nan")
    print("  median %.1fs over %d run(s)%s" % (med, len(times), "" if a.no_warmup else " after a warm-up"))
    print("  %d file(s) → %s" % (len(got), out))
    (out / "timing.json").write_text(json.dumps(
        {"label": a.label, "runs": times, "median_s": med, "warmup": not a.no_warmup,
         "sets": a.sets or [], "set_files": a.set_files or [], "drops": a.drops or [],
         "files": [f.name for f in got]}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    try: sys.exit(main())
    except PodctlError as e: print("ERROR: %s" % e, file=sys.stderr); sys.exit(1)
