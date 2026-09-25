#!/usr/bin/env python3
"""After packages were FORCED past another package's cap: does every package that set the cap still import?

Usage (with the venv's own interpreter):  holders.py NAME [NAME ...]
Prints one line per (forced, holder) pair: "forced<TAB>holder<TAB>holder-version<TAB>ok|<the import error>".

3.0.0 forces anything still behind to its newest with uv's overrides. A cap is often only a declaration (protobuf 7 under
google-generativeai's <6 imports fine, measured 2026-09-25), but some packages enforce theirs when they are imported
(transformers 5.17.0 raises ImportError on huggingface-hub 2.0.0, measured the same day on a live machine). This finds
the holders from the installed metadata (every installed distribution whose requirement on a forced package the
installed version does not satisfy) and imports each one in a fresh interpreter, so one failure cannot mask another.
"""
import importlib.metadata as md
import re
import subprocess
import sys

try:
    from packaging.requirements import Requirement
except ImportError:
    from pip._vendor.packaging.requirements import Requirement


def canon(n):
    return re.sub(r"[-_.]+", "-", n).lower()


def modules_of(dist):
    """The importable top-level names of a distribution: top_level.txt, else its files (one level into a namespace)."""
    try:
        tl = dist.read_text("top_level.txt")
    except Exception:  # noqa: BLE001
        tl = None
    if tl:
        names = [n.strip() for n in tl.splitlines() if n.strip() and not n.strip().startswith("_")]
        if names:
            return names[:3]
    found = []
    for f in dist.files or []:
        parts = f.parts
        if len(parts) >= 2 and parts[-1] == "__init__.py" and not parts[0].endswith((".dist-info", ".data")):
            mod = ".".join(parts[:-1])
            if mod.count(".") <= 1 and not any(m.startswith(mod + ".") or mod.startswith(m + ".") for m in found):
                found.append(mod)
    return sorted(found, key=len)[:3]


def main():
    forced = {canon(n) for n in sys.argv[1:]}
    installed = {}
    for d in md.distributions():
        try:
            installed[canon(d.metadata["Name"])] = d
        except Exception:  # noqa: BLE001
            pass
    for name in sorted(forced):
        fd = installed.get(name)
        if not fd:
            continue
        have = fd.version
        for hn, hd in sorted(installed.items()):
            for raw in hd.requires or []:
                try:
                    req = Requirement(raw)
                except Exception:  # noqa: BLE001
                    continue
                if canon(req.name) != name or (req.marker and not req.marker.evaluate({"extra": ""})):
                    continue
                if req.specifier.contains(have, prereleases=True):
                    continue
                err = "ok"
                for mod in modules_of(hd):
                    r = subprocess.run([sys.executable, "-c", "import %s" % mod], capture_output=True, text=True, timeout=300)
                    if r.returncode != 0:
                        last = [l for l in (r.stderr or r.stdout).strip().splitlines() if l.strip()]
                        err = (last[-1] if last else "import %s failed" % mod)[:240]
                        break
                print("%s\t%s\t%s\t%s" % (name, hn, hd.version, err))
                break
    return 0


if __name__ == "__main__":
    sys.exit(main())
