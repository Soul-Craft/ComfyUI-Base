#!/usr/bin/env python3
"""The newest torch, on the newest CUDA build this machine's driver runs, for one Python minor.

Usage: torch_pick.py 3.14 [VENV_BACKEND]  ->  prints "2.14.0 cu132" (and, on stderr, why)

3.0.0: uv decides the CUDA build. `uv pip compile --torch-backend=auto` reads the installed driver and picks the most
compatible PyTorch index (uv's own documentation: docs/guides/integration/pytorch.md); this helper asks it for `torch`
and reads the answer. There is no index scraping and no CUDA literal here any more.

VENV_BACKEND is the backend the existing venv was built on (from torch.version.cuda). The venv may be SHARED by
several machines on one store, and they need not run the same driver:
  - never backwards: when the venv's backend is newer than this machine's pick in the SAME CUDA major, the venv's is
    kept (NVIDIA's minor-version compatibility runs it on the older driver), so machines do not rebuild the venv back
    and forth;
  - a newer major moves: the venv follows this machine, and the install names the machines that must upgrade;
  - a machine whose pick is an OLDER major than the venv keeps the venv as it is (it cannot run it; the summary says so).
With no driver readable (a fake run, a GPU-less image build, an NVML mismatch) uv's auto answers "cpu": the venv's
backend is kept, and a fresh build takes the newest CUDA backend uv itself knows, with a warning.

The backend must carry the WHOLE torch family ComfyUI requires (torch, torchvision, torchaudio), for this Python: the
newest backend on which the family resolves, the same "newest that resolves" rule the Python pick follows. Measured on
2026-09-25: torchaudio's newest (2.11.0) has cu130 builds and none for cu132, so a driver that allows cu132 still gets
cu130 until torchaudio ships there; the reason says which package holds it ("held by the torch family").

Genuine latest, no fallback: a failure exits 1 with the reason on stderr.
"""
import os
import platform
import re
import subprocess
import sys


def die(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


def cu_key(b):
    m = re.fullmatch(r"cu(\d+)", b or "")
    if not m:
        return None
    d = m.group(1)
    return (int(d[:-1]), int(d[-1]))  # cu132 -> (13, 2); cu118 -> (11, 8)


def plat():
    return "aarch64-unknown-linux-gnu" if platform.machine().lower() in ("aarch64", "arm64") else "x86_64-unknown-linux-gnu"


def compile_torch(pymm, backend):
    cmd = [os.environ.get("UV", "uv"), "pip", "compile", "-", "--quiet", "--no-header", "--no-annotate",
           "--python-version", pymm, "--python-platform", plat(), "--torch-backend", backend]
    r = subprocess.run(cmd, input="torch\n", capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        die("uv could not resolve torch for Python %s on backend %s: %s" % (pymm, backend, (r.stderr or r.stdout).strip()[-400:]))
    m = re.search(r"^torch==([0-9][0-9.]*)(?:\+([a-z0-9.]+))?\s*$", r.stdout, re.M)
    if not m:
        die("uv resolved no torch for Python %s on backend %s" % (pymm, backend))
    return m.group(1), (m.group(2) or "cpu")


def family_resolves(pymm, backend):
    """True when torch, torchvision and torchaudio resolve together for this Python on this backend."""
    cmd = [os.environ.get("UV", "uv"), "pip", "compile", "-", "--quiet", "--no-header", "--no-annotate",
           "--python-version", pymm, "--python-platform", plat(), "--torch-backend", backend]
    r = subprocess.run(cmd, input="torch\ntorchvision\ntorchaudio\n", capture_output=True, text=True, timeout=300)
    return r.returncode == 0


def uv_cuda_backends():
    r = subprocess.run([os.environ.get("UV", "uv"), "pip", "install", "--help"], capture_output=True, text=True)
    return sorted({b for b in re.findall(r"\bcu\d{3}\b", r.stdout) if cu_key(b)}, key=cu_key, reverse=True)


def newest_uv_cuda():
    r = subprocess.run([os.environ.get("UV", "uv"), "pip", "install", "--help"], capture_output=True, text=True)
    found = [b for b in re.findall(r"\bcu\d{3}\b", r.stdout) if cu_key(b)]
    return max(found, key=cu_key) if found else ""


def main():
    pymm = sys.argv[1] if len(sys.argv) > 1 else ""
    keep = sys.argv[2] if len(sys.argv) > 2 else ""
    if not re.fullmatch(r"3\.\d+", pymm):
        die("usage: torch_pick.py 3.XY [venv-backend]")
    ver, backend = compile_torch(pymm, "auto")
    why = "uv --torch-backend=auto: %s" % backend
    if backend == "cpu":
        if cu_key(keep):
            backend, why = keep, "no driver readable: the venv's %s is kept" % keep
        else:
            backend = newest_uv_cuda()
            if not backend:
                die("no driver readable and uv lists no CUDA backend")
            why = "no driver readable: the newest CUDA backend uv knows (%s); check the driver" % backend
        ver, _ = compile_torch(pymm, backend)
    elif cu_key(keep) and cu_key(backend):
        k, p = cu_key(keep), cu_key(backend)
        if k[0] == p[0] and k > p:
            why = "never backwards: the venv's %s is newer than this driver's %s (same major, minor-version compatible)" % (keep, backend)
            backend = keep
            ver, _ = compile_torch(pymm, backend)
        elif k[0] > p[0]:
            why = ("this driver's %s is an OLDER CUDA major than the venv's %s: the venv is kept, and this machine "
                   "cannot run it until its driver is upgraded" % (backend, keep))
            backend = keep
            ver, _ = compile_torch(pymm, backend)
        elif p[0] > k[0]:
            why = "a newer CUDA major (%s over the venv's %s): the venv moves; other machines on the store must upgrade" % (backend, keep)
    if backend != "cpu" and not family_resolves(pymm, backend):
        # the newest backend this driver allows does not carry the whole family: step down to the newest that does
        down = [b for b in uv_cuda_backends() if cu_key(b) < cu_key(backend)]
        held = backend
        backend = next((b for b in down if family_resolves(pymm, b)), "")
        if not backend:
            die("the torch family (torch, torchvision, torchaudio) resolves on no CUDA backend at or below %s for Python %s" % (held, pymm))
        ver, _ = compile_torch(pymm, backend)
        why = ("held by the torch family: torchvision/torchaudio have no %s build for Python %s; the newest backend the "
               "whole family resolves on is %s (it moves by itself once they ship there)" % (held, pymm, backend))
    print("%s %s" % (ver, backend))
    print(why, file=sys.stderr)


if __name__ == "__main__":
    main()
