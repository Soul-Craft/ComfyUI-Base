#!/usr/bin/env python3
"""For packs that ship no requirements file: what do they import, and can this interpreter import it?

Usage (run with the venv's python): import_scan.py "name|/path/to/pack" ...
Prints one line per pack:  name <TAB> comma-separated third-party imports <TAB> comma-separated missing ones

An import the pack wraps in try/except is optional by construction and is not counted. Advisory: the scan
cannot know whether the module doing the importing is ever loaded, so the caller warns and never stops.
"""
import ast
import importlib.util
import os
import sys

# what ComfyUI puts on sys.path for a custom node is not a third-party requirement
CORE = {"comfy", "comfy_extras", "comfy_api", "comfy_api_nodes", "comfy_execution", "comfy_config", "comfy_types",
        "folder_paths", "nodes", "server", "execution", "app", "utils", "latent_preview", "node_helpers",
        "model_management", "main", "custom_nodes"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", "web", ".venv", "venv", "tests", "test", "cupy_ops"}
STDLIB = set(getattr(sys, "stdlib_module_names", ())) or set(sys.builtin_module_names)


def scan(root):
    local, hard = set(), set()
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        local.update(dn)
        local.update(os.path.basename(dp) if f == "__init__.py" else f[:-3] for f in fn if f.endswith(".py"))
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for f in fn:
            if not f.endswith(".py"):
                continue
            try:
                tree = ast.parse(open(os.path.join(dp, f), encoding="utf-8", errors="ignore").read())
            except SyntaxError:
                continue
            guarded = {id(c) for n in ast.walk(tree) if isinstance(n, ast.Try) for c in ast.walk(n)
                       if isinstance(c, (ast.Import, ast.ImportFrom))}
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    names = [a.name.split(".")[0] for a in n.names]
                elif isinstance(n, ast.ImportFrom):
                    names = [n.module.split(".")[0]] if n.level == 0 and n.module else []
                else:
                    continue
                for m in names:
                    if m in STDLIB or m in CORE or m in local:
                        continue
                    if id(n) not in guarded:
                        hard.add(m)
    return sorted(hard)


for arg in sys.argv[1:]:
    name, _, d = arg.partition("|")
    req = scan(d) if os.path.isdir(d) else []
    missing = []
    for m in req:
        try:
            if importlib.util.find_spec(m) is None:
                missing.append(m)
        except Exception:  # noqa: BLE001
            missing.append(m)
    print("\t".join((name, ",".join(req), ",".join(missing))))
