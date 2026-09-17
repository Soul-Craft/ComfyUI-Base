#!/usr/bin/env python3
"""The GPU must be ours. nvidia-smi inside a container reports the whole card's memory but lists only THIS container's
processes: memory used that no visible process holds belongs to another container, or to a context leaked on the host.
Renders then die at their first node with the card idle — 2026-09-06, pod fakepod0000003: 95.3 GB of 97.9 used, 0 %
utilisation, our ComfyUI holding 1.7 GB, 93.5 GB held outside (2.0.26). Every check before that had passed.

Prints one line:  total=<MiB> used=<MiB> ours=<MiB> foreign=<MiB> procs=<n> verdict=ours|NOT-OURS|no-gpu
Exit 0 always; the caller reads the verdict. Standalone: no torch, no imports beyond the standard library."""
import subprocess, sys

FOREIGN_LIMIT_MIB = 4096   # the driver keeps ~600 MiB for itself on this card; past 4 GiB it is someone else's memory


def verdict(total, used, apps):
    """(total MiB, used MiB, [(pid, MiB)…] visible here) → (ours MiB, foreign MiB, verdict)."""
    ours = sum(m for _, m in apps)
    foreign = max(0, used - ours)
    return ours, foreign, ("ours" if foreign <= FOREIGN_LIMIT_MIB else "NOT-OURS")


def _query(args):
    try:
        r = subprocess.run(["nvidia-smi"] + args, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def parse_int(s):
    s = s.strip()
    return int(s) if s.isdigit() else 0


def facts():
    mem = _query(["--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"])
    if not mem or not mem.strip():
        return None
    total, used = [parse_int(x) for x in mem.strip().splitlines()[0].split(",")[:2]]
    apps = []
    for line in (_query(["--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"]) or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit():
            apps.append((int(parts[0]), parse_int(parts[1])))
    ours, foreign, v = verdict(total, used, apps)
    return {"total": total, "used": used, "ours": ours, "foreign": foreign, "procs": len(apps), "verdict": v}


def main():
    f = facts()
    if f is None:
        print("verdict=no-gpu"); return 0
    print("total=%d used=%d ours=%d foreign=%d procs=%d verdict=%s" % (f["total"], f["used"], f["ours"], f["foreign"], f["procs"], f["verdict"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
