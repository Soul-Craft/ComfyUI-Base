#!/usr/bin/env python3
"""Rehearse an upload from the shipped zips, the way the pod will see it — offline, on a fake pod.

    cd <repo> && uv run --no-project --python 3.12 --with pytest python base/comfyui-base/_build/rehearse.py <brand>/packages/<name> [--layout official|community|bare|volume]
    cd <base>  && uv run --no-project --python 3.12 --with pytest python _build/rehearse.py stub --layout volume      # standalone: the fixture package

Unpacks the base zip (comfyui-base.zip) and the package zip into a scratch /workspace/packages, builds a fake pod
of the chosen layout beside it (basetest.fake_pod), then runs exactly the two commands the handbook gives:
  step one   bash "comfyui-base/comfyui-base-script.sh"          (self-installs to <root>/comfy-base)
  step two   bash "<package>-script.sh" --check · full run · second run · --check again
with BASE_NO_NET=1 (downloads are sparse files at the declared size), then boots the fake pod through
comfy-base/boot.sh with stubbed sshd/JupyterLab/python. Exit 0 when every step exits 0, the workflow
lands in the pod's workflow browser, the second run downloads nothing, --check changes nothing and the
boot starts ComfyUI from the base's venv on every layout. This proves the ZIPS, not the working tree: run it after `package.py` and before
uploading. It does not replace the pod (weights, GPU, the real packs' source): that is the package
handbook's acceptance list.

The package argument is a path relative to the repository root (the brand tree above this base), or a name under
this base's _build/ (`stub`, whose zip is built on the fly into the scratch dir when _build/stub/stub-runpod.zip
does not exist), or any path that exists as given.
"""
import argparse, os, pathlib, re, shutil, subprocess, sys, tempfile, time, zipfile

HERE = pathlib.Path(__file__).resolve().parent; BASE = HERE.parent
# The repository root: the brand tree's root when this base is its <root>/base/comfyui-base, else the base's parent.
REPO = BASE.parents[1] if len(BASE.parents) > 1 and (BASE.parents[1] / "base" / "comfyui-base").resolve() == BASE.resolve() else BASE.parent
sys.path.insert(0, str(BASE / "py")); sys.path.insert(0, str(HERE))
import basetest                                                        # noqa: E402
import package                                                         # noqa: E402  the packager: zip_host, package_members, build

LAYOUTS = ("official", "community", "bare", "volume")


def plain(s): return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)


def resolve_package(arg):
    """The package folder: <repo>/<arg> first, then this base's _build/<arg> (the stub), then the path as given."""
    for cand in (REPO / arg, BASE / "_build" / arg, pathlib.Path(arg)):
        cand = cand.resolve()
        if cand.is_dir() and (cand / f"{cand.name}-script.sh").exists():
            return cand
    print(f"no package folder for {arg!r}: tried {REPO / arg}, {BASE / '_build' / arg} and the path as given", file=sys.stderr)
    sys.exit(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("package", help="the package folder, repository-relative (<brand>/packages/<name>), or `stub`")
    ap.add_argument("--layout", default="official", choices=LAYOUTS, help="the fake pod's image family")
    ap.add_argument("--keep", action="store_true", help="leave the scratch tree in place and print its path")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    pkg_dir = resolve_package(a.package); name = pkg_dir.name                      # <name> is the pod folder and the zip stem
    base_zip = BASE / "comfyui-base.zip"
    if not base_zip.exists(): print(f"missing {base_zip} -- build it with _build/package.py --base first", file=sys.stderr); sys.exit(2)
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="rehearse-", dir=os.environ.get("REHEARSE_TMP")))
    pkg_zip = pkg_dir / f"{name}-{package.zip_host(pkg_dir)}.zip"
    if not pkg_zip.exists():
        if pkg_dir == (BASE / "_build" / name).resolve():                       # the stub ships no zip: pack it into the scratch dir
            pkg_zip = scratch / pkg_zip.name
            pkg_zip.write_bytes(package.build(pkg_dir, package.package_members(pkg_dir)))
            print(f"=== built {pkg_zip.name} on the fly (no zip beside {pkg_dir})")
        else:
            print(f"missing {pkg_zip} -- build it with _build/package.py first", file=sys.stderr); shutil.rmtree(scratch, ignore_errors=True); sys.exit(2)
    slim = scratch / "packages"; slim.mkdir()                              # the upload folder: /workspace/packages on a pod
    with zipfile.ZipFile(base_zip) as z: z.extractall(slim)                  # -> packages/comfyui-base/
    with zipfile.ZipFile(pkg_zip) as z: z.extractall(slim / name)           # -> packages/<name>/
    root = scratch / "pod"; root.mkdir()
    pod = basetest.fake_pod(root, slim / name, layout=a.layout)
    print(f"=== layout: {a.layout} (canonical tree {pod})")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BASE_", "COMFY_"))}
    env.update({"BASE_FAKE_ROOT": str(root), "BASE_SEARCH_ROOTS": str(root), "BASE_NO_NET": "1", "HOME": str(root / "home"),
                "BASE_FAKE_FREE_GB": "500", "PYTHONDONTWRITEBYTECODE": "1"})
    results = []

    def run(label, script, *args, extra=None):
        e = dict(env); e.update(extra or {}); t = time.time()
        r = subprocess.run(["bash", str(script), *args], capture_output=True, text=True, env=e, input="\n\n", timeout=1800)
        out = plain(r.stdout + r.stderr); print(f"=== {label}: exit {r.returncode} in {time.time() - t:.0f}s")
        keep = [l for l in out.splitlines() if re.search(r"^\s+(packs|models|workflow|venv)\s|SUMMARY|ERR|refus", l)]
        print("\n".join("   " + l[:140] for l in keep[-6:]))
        if r.returncode or a.verbose: print(out[-3000:])
        results.append(r.returncode == 0); return out

    run("step one · base self-install (from the base zip)", slim / "comfyui-base" / "comfyui-base-script.sh")
    cb = root / "comfy-base"; ok_cb = cb.exists(); print("   comfy-base installed:", ok_cb); results.append(ok_cb)
    script = root / "pkg" / f"{name}-script.sh"; ex = {"COMFY_BASE": str(cb)}
    run("step two · --check (from the package zip)", script, "--check", extra=ex)
    run("step two · full run", script, extra=ex)
    wf = list((pod / "user" / "default" / "workflows").glob("*.json")); print("   workflow in the pod's browser:", [p.name for p in wf]); results.append(bool(wf))
    out = run("step two · second run", script, extra=ex)
    d0 = bool(re.search(r"downloaded 0\b", out)); print("   second run downloaded 0:", d0); results.append(d0)
    before = basetest.tree_hash(root, ignore=("logs",)); run("step two · --check after install", script, "--check", extra=ex)
    same = basetest.tree_hash(root, ignore=("logs",)) == before; print("   --check changed nothing:", same); results.append(same)
    # the boot: the base's boot.sh as the fake pod's PID 1 — sshd, JupyterLab, then ComfyUI from the base's venv
    bin_, log = basetest.boot_stubs(root)
    r, blog = basetest.boot_fake_pod(root, bin_, {"JUPYTER_PASSWORD": "rehearsal"})
    calls = log.read_text() if log.exists() else ""
    booted = r.returncode == 0 and "boot: sshd up" in blog and "JupyterLab starting" in blog and "python main.py" in calls and "boot: done" in blog
    print(f"=== boot · comfy-base/boot.sh (fake pod): exit {r.returncode}"); print("   sshd → JupyterLab → ComfyUI from the base venv:", booted)
    if not booted or a.verbose: print("\n".join("   " + l for l in blog.splitlines()[-12:]))
    results.append(booted)
    ok = all(results); print("RESULT:", "ALL OK" if ok else "FAILURES ABOVE", "" if not a.keep else f"(kept: {scratch})")
    if not a.keep: shutil.rmtree(scratch, ignore_errors=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
