"""brand.py: which brand a package belongs to, and which host that brand ships to.

A brand repository (or one brand folder of a monorepo) declares itself once, in <brand>/brand.toml:

    name = "Example"
    host = "runpod"          # runpod | verda | crusoe | local: the zip suffix and the pod driver's default provider

Nothing in the base derives a venue from a folder name any more (2.1.0's zip_host() looked for a brand's folder name in the path, which could not express a third host). The reader is stdlib only and works on any
Python the base meets: tomllib where it exists (3.11+), else a small parser for the flat `key = "value"` file
this is. Read from a package dir, a brand dir, or the repository root.
"""
from __future__ import annotations

import pathlib
import re

HOSTS = ("runpod", "verda", "crusoe", "local")


def parse_brand_toml(text: str) -> dict:
    """The few flat keys brand.toml carries. tomllib when available; the fallback accepts `key = "value"` lines only."""
    try:
        import tomllib
        return {k: v for k, v in tomllib.loads(text).items() if isinstance(v, str)}
    except ImportError:                     # no tomllib (Python before 3.11, or a test that hides it)
        out = {}
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"([^"]*)"\s*$', line)
            if m:
                out[m.group(1)] = m.group(2)
        return out


def brand_dir_of(path) -> pathlib.Path | None:
    """The <brand> directory for a path inside <brand>/packages/<name>/ or <brand>/ itself, else None."""
    p = pathlib.Path(path).resolve()
    for cand in (p, *p.parents):
        if (cand / "brand.toml").is_file():
            return cand
    return None


def brand_of(path) -> dict:
    """{name, host, dir} for the brand a path belongs to. No brand.toml above the path: {} (a flat tree, an extracted zip)."""
    d = brand_dir_of(path)
    if d is None:
        return {}
    cfg = parse_brand_toml((d / "brand.toml").read_text(encoding="utf-8"))
    host = (cfg.get("host") or "").strip().lower()
    if host and host not in HOSTS:
        raise ValueError("%s: host %r is not one of %s" % (d / "brand.toml", host, ", ".join(HOSTS)))
    return {"name": cfg.get("name", d.name), "host": host, "dir": d}


def host_of(path, default: str = "runpod") -> str:
    """The venue a package ships to: its brand's host, else the default (the base's own zip has no venue at all)."""
    return brand_of(path).get("host") or default


def brands_under(root) -> list:
    """Every <root>/<brand>/brand.toml, as brand_of() dicts, sorted by directory name."""
    root = pathlib.Path(root)
    return [brand_of(p.parent) for p in sorted(root.glob("*/brand.toml"))]
