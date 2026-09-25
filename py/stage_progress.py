#!/usr/bin/env python3
"""state/progress.json (3.1.0): where a staged install's later files are, for whatever shows a buyer the state.

    stage_progress.py --queue state/later.queue --standins state/standins.list --failed state/later.failed
                      --out state/progress.json [--stage first|later|done]

Every file's state is read from the disk, so the report is right whoever last wrote it and whenever:
present (its declared size), standin (listed and still 0 bytes), failed (the last pass could not get it), downloading
(anything else: not yet there, or arriving). A package's own page reads this file to label or disable a flow whose
files are not present yet. Written to a temporary name and renamed, so a reader never sees half a file.

{"format": 1, "updated": "<utc>", "stage": "first|later|done",
 "files": [{"file": "<basename>", "rel": "<category/Family/...>", "bytes": N, "state": "..."}],
 "later": {"files": N, "bytes": N, "done_files": N, "done_bytes": N}}
"""
import argparse
import datetime
import json
import os
import pathlib
import sys


def _lines(p):
    try:
        return [ln.rstrip("\n") for ln in pathlib.Path(p).read_text(encoding="utf-8").splitlines() if ln.strip()]
    except FileNotFoundError:
        return []


def report(queue, standins, failed, stage):
    listed = set(_lines(standins))
    failed_dests = {ln.rsplit("|", 1)[-1] for ln in _lines(failed)}
    files, done_files, done_bytes, total = [], 0, 0, 0
    seen = set()
    for ln in _lines(queue):
        parts = ln.split("|")
        if len(parts) < 5:
            continue
        _kind, rel, _url, nbytes, dest = parts[0], parts[1], parts[2], parts[3], parts[4]
        if dest in seen:
            continue
        seen.add(dest)
        n = int(nbytes) if nbytes.isdigit() else 0
        size = os.stat(dest).st_size if os.path.isfile(dest) else -1
        if size == n and n > 0:
            state = "present"
        elif size == 0 and dest in listed:
            state = "failed" if dest in failed_dests else "standin"
        elif dest in failed_dests:
            state = "failed"
        else:
            state = "downloading"
        total += n
        if state == "present":
            done_files += 1
            done_bytes += n
        files.append({"file": os.path.basename(dest), "rel": rel, "bytes": n, "state": state})
    if files and done_files == len(files):
        stage = "done"
    return {
        "format": 1,
        "updated": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "stage": stage, "files": files,
        "later": {"files": len(files), "bytes": total, "done_files": done_files, "done_bytes": done_bytes},
    }


def main(argv=None):
    ap = argparse.ArgumentParser(prog="stage_progress.py")
    ap.add_argument("--queue", required=True)
    ap.add_argument("--standins", required=True)
    ap.add_argument("--failed", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--stage", default="later", choices=("first", "later", "done"))
    a = ap.parse_args(argv)
    doc = report(a.queue, a.standins, a.failed, a.stage)
    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
