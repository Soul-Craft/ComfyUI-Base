#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""podctl: the Mac-side pod driver. One host provider per run (hosts/<host>/provider.py); everything else is generic.

    uv run "base/comfyui-base/_build/pod/podctl.py" [--provider runpod|verda|crusoe] <command> ...

The provider is --provider (before or after the command), else $PODCTL_PROVIDER, else the one brand.toml host of the
repository this base sits in, else runpod. It answers where the machine is and how it starts, stops, clones and hands
its boot to the base; the driver does ssh, scp, upload, install, the lease, the tunnel and the GPU gate the same way
on every host. Credentials (RunPod's key, Verda's client secret) are read by the provider from the environment or its
tool's config file and are never printed, logged or placed in an argument.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import zipfile
import sys
import types
import urllib.error
import urllib.request

BASE_DIR = pathlib.Path(__file__).resolve().parents[2]


def _load_by_path(name, path, package_dir=None):
    """Import a base module by file path under a fixed sys.modules name, once (the driver is a script, not a package)."""
    import importlib.util
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=[str(package_dir)] if package_dir else None)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Self(types.ModuleType):
    """This module when it was loaded by path without being registered (the suite's _load): attribute reads go to its globals."""
    def __getattr__(self, name):
        try:
            return _GLOBALS[name]
        except KeyError:
            raise AttributeError(name)


_GLOBALS = globals()
sys.modules["comfyui_podctl"] = sys.modules[__name__] if __name__ in sys.modules else _Self("comfyui_podctl")   # providers reach back through this
_hosts = _load_by_path("comfyui_hosts", BASE_DIR / "hosts" / "__init__.py", BASE_DIR / "hosts")
_brand = _load_by_path("comfyui_brand", BASE_DIR / "py" / "brand.py")
PodctlError = _hosts.PodctlError
Provider = _hosts.Provider

# The RunPod half lives in hosts/runpod/provider.py since 2.2.0; its functions stay reachable here for the suite and for
# any caller that grew up with them.
_runpod = _hosts.load_provider("runpod", BASE_DIR)
RunPodProvider = _runpod.RunPodProvider
for _n in ("DEFAULT_API_URL", "PLACEHOLDERS", "SECRET_NAMES", "load_api_key", "Api", "pod_image", "pod_volume_id", "env_verdicts", "pod_facts",
           "BOOT_GUARD", "image_config", "wrap_entry", "sshd_bootstrap_text", "wait_for", "ensure", "stop", "start", "restart", "deploy_body",
           "deploy", "volume_size_gb", "record_volume"):
    _GLOBALS[_n] = getattr(_runpod, _n)
del _n


# ---------------------------------------------------------------- the host the run addresses (set once per run by set_provider)

# ONE alias, one Mac, several sessions, and the block is a single pointer. On 2026-09-08 a third session ran `ssh-config`
# for its own pod and every other session's ssh, scp, install and lease silently began addressing that pod instead.
# `PODCTL_HOST` gives a session its own block (export PODCTL_HOST=runpod-<podid>) so nothing it does moves anyone else's
# pointer. Without it the alias is the provider's own name (runpod, verda, crusoe), so each host keeps its own block.
PROVIDER = None
SSH_ALIAS = (os.environ.get("PODCTL_HOST") or "").strip() or "runpod"
SSH_USER = "root"
KEY_PATH = "~/.ssh/id_ed25519"
VOLUME_ROOT = "/workspace"                              # the persistent root on the machine: the base's BASE_VOLUME there
SSH_BLOCK = ("Host {alias}\n  HostName {host}\n  Port {port}\n  User {user}\n  IdentityFile {key}\n  StrictHostKeyChecking accept-new\n"
             "  LocalForward 8188 localhost:8188\n  LocalForward 9199 localhost:9199\n  LocalForward 11434 localhost:11434\n")


def _block_for(alias, user, key):
    """The block as it would be written for this run, host and port left as {host} {port} (the name RUNPOD_BLOCK is kept for
    the suite and for anyone who read it before 2.2.0; the provider fills the rest)."""
    return SSH_BLOCK.replace("{alias}", alias).replace("{user}", user).replace("{key}", key)


RUNPOD_BLOCK = _block_for(SSH_ALIAS, SSH_USER, KEY_PATH)


def set_provider(prov, env=None):
    """Bind the run to one host: the alias, user, key and volume root every generic function reads from here on."""
    global PROVIDER, SSH_ALIAS, SSH_USER, KEY_PATH, VOLUME_ROOT, LEASE_PATH, RUNPOD_BLOCK
    env = os.environ if env is None else env
    PROVIDER = prov
    SSH_ALIAS = (env.get("PODCTL_HOST") or "").strip() or prov.ssh_alias
    SSH_USER = prov.ssh_user
    KEY_PATH = str(prov.key_path)
    VOLUME_ROOT = prov.volume_root.rstrip("/") or "/"
    LEASE_PATH = VOLUME_ROOT + "/comfy-base/state/gpu.lease"
    RUNPOD_BLOCK = _block_for(SSH_ALIAS, SSH_USER, KEY_PATH)
    return prov


def pkgs_dir():
    """Where zips are uploaded and extracted on the machine: <volume>/packages."""
    return VOLUME_ROOT + "/packages"


def logs_dir_remote():
    return VOLUME_ROOT + "/comfy-base/state/logs"


def provider_for(name=None, base_dir=None, env=None):
    """The provider for this run: --provider, else $PODCTL_PROVIDER, else the one brand.toml host above this base, else runpod.
    Two brands with two hosts in one repository make --provider required."""
    env = os.environ if env is None else env
    base = pathlib.Path(base_dir or BASE_DIR)
    name = (name or env.get("PODCTL_PROVIDER") or "").strip().lower()
    if not name:
        root = repo_root(base)
        hosts = sorted({b["host"] for b in (_brand.brands_under(root) if root else []) if b.get("host")})
        if len(hosts) > 1:
            raise PodctlError("this repository has brands on %s: pass --provider <host>" % ", ".join(hosts))
        name = hosts[0] if hosts else "runpod"
    mod = _hosts.load_provider(name, base)
    cls = next(v for v in vars(mod).values() if isinstance(v, type) and issubclass(v, Provider) and v is not Provider)
    return cls()


def write_host_env(prov, env=None):
    """state/host.env on the machine, so the base knows which host it runs on (BASE_HOST) and where its volume is (BASE_VOLUME)."""
    text = prov.host_env()
    r = ssh_run("mkdir -p %s && printf '%%s' %s > %s" % (_sq(VOLUME_ROOT + "/comfy-base/state"), _sq(text), _sq(VOLUME_ROOT + "/comfy-base/state/host.env")), env=env)
    if r.returncode != 0:
        raise PodctlError("could not write state/host.env on the machine: %s" % (r.stderr or r.stdout).strip()[-300:])
    return "host    %s (state/host.env: %s)" % (prov.name, " ".join(text.split()))


def write_ssh_config(path, host, port):
    """Point the run's `Host <alias>` block at host:port. Only the HostName and Port lines of that block change (plus, once, the
    `StrictHostKeyChecking accept-new` line of 2.0.28); every other byte of the file stays. A file without the block gets
    one appended (User, key, accept-new, the three forwards the base relies on)."""
    path = pathlib.Path(path)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.split("\n")
    start = next((i for i, ln in enumerate(lines) if re.match(r"^Host\s+%s\s*$" % re.escape(SSH_ALIAS), ln)), None)
    if start is None:
        sep = "" if text == "" or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        text = text + sep + SSH_BLOCK.format(alias=SSH_ALIAS, user=SSH_USER, key=KEY_PATH, host=host, port=port)
    else:
        end = next((j for j in range(start + 1, len(lines)) if re.match(r"^Host\s", lines[j])), len(lines))
        for j in range(start + 1, end):
            if re.match(r"^\s*HostName\s", lines[j]):
                lines[j] = re.sub(r"(HostName\s+).*$", r"\g<1>" + host, lines[j])
            elif re.match(r"^\s*Port\s", lines[j]):
                lines[j] = re.sub(r"(Port\s+).*$", r"\g<1>%d" % int(port), lines[j])
        if not any(re.match(r"^\s*StrictHostKeyChecking\s", lines[j]) for j in range(start + 1, end)):
            # 2.0.28: a new pod is always an unknown host, and BatchMode refuses one under the default (ask) — accept-new, once
            port_at = next((j for j in range(start + 1, end) if re.match(r"^\s*Port\s", lines[j])), start)
            lines.insert(port_at + 1, "  StrictHostKeyChecking accept-new")
        text = "\n".join(lines)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)


# ---------------------------------------------------------------- the image's own entry, and how the base wraps it


def ssh_banner(host, port, timeout=10):
    """True when something at host:port answers with an SSH banner. A mapped port alone proves nothing (RunPod
    allocates it for images that run no sshd at all)."""
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as s:
            s.settimeout(timeout)
            return s.recv(64).startswith(b"SSH-")
    except OSError:
        return False


def mac_public_key(path=None):
    p = pathlib.Path(os.path.expanduser(str(path or (KEY_PATH + ".pub"))))
    if not p.exists():
        raise PodctlError("no public key at %s" % p)
    return p.read_text(encoding="utf-8").strip()


def _ssh_hostname():
    import subprocess
    r = subprocess.run(SSH_CMD + ["-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=accept-new", SSH_ALIAS, "hostname"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise PodctlError("ssh %s hostname failed: %s" % (SSH_ALIAS, r.stderr.strip()[-300:]))
    return r.stdout.strip()


# ---------------------------------------------------------------- stop / start / restart, upload


SSH_CMD = ["ssh", "-o", "BatchMode=yes", "-o", "ClearAllForwardings=yes", "-o", "StrictHostKeyChecking=accept-new"]
SCP_CMD = ["scp", "-q", "-o", "ClearAllForwardings=yes", "-o", "StrictHostKeyChecking=accept-new"]
TUNNEL_PORTS = [8188, 8888]      # ComfyUI, JupyterLab


def ssh_run(remote_cmd, env=None, timeout=None):
    """One command on the pod, no forwards, no TTY. Returns the CompletedProcess."""
    import subprocess
    return subprocess.run(SSH_CMD + [SSH_ALIAS, remote_cmd], capture_output=True, text=True, env=env or os.environ, timeout=timeout)


def tunnel_argv(ports=None, host=None, port=None, key=None, user=None):
    """The one ssh session that forwards: `podctl tunnel <pod>` runs this in the foreground (Claude backgrounds it).
    Explicit -L per port, keepalives, and a hard failure when a port is taken — never a silent half-tunnel.
    It ignores ~/.ssh/config (-F /dev/null) and addresses the pod by ip:port from the API: OpenSSH applies
    ClearAllForwardings after the whole command line, so the old `-o ClearAllForwardings=yes … -L …` cleared its own
    forwards and bound nothing (2.0.21), while the config's LocalForward lines would double-bind 8188."""
    argv = ["ssh", "-N", "-F", "/dev/null", "-i", str(key), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ServerAliveInterval=30", "-o", "ExitOnForwardFailure=yes"]
    for p in (ports or TUNNEL_PORTS):
        argv += ["-L", "%d:127.0.0.1:%d" % (int(p), int(p))]
    return argv + ["-p", str(int(port)), "%s@%s" % (user or SSH_USER, host)]


def upload(files, to, env=None):
    """scp each file to <to>/ through the run's Host block, then compare size and sha256 on both sides. A mismatch fails."""
    import hashlib
    import subprocess
    env = env or os.environ
    files = [pathlib.Path(f) for f in files]
    for f in files:
        if not f.exists():
            raise PodctlError("no such file: %s" % f)
    r = ssh_run("mkdir -p %s" % _sq(to), env=env)
    if r.returncode != 0:
        raise PodctlError("ssh %s mkdir failed: %s" % (SSH_ALIAS, r.stderr.strip()[-300:]))
    r = subprocess.run(SCP_CMD + [str(f) for f in files] + ["%s:%s/" % (SSH_ALIAS, to)], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise PodctlError("scp failed: %s" % r.stderr.strip()[-300:])
    remote_cmd = "cd %s && for f in %s; do printf '%%s %%s  %%s\\n' \"$(stat -c %%s \"$f\")\" \"$(sha256sum \"$f\" | cut -d' ' -f1)\" \"$PWD/$f\"; done" % (
        _sq(to), " ".join(_sq(f.name) for f in files))
    r = ssh_run(remote_cmd, env=env)
    if r.returncode != 0:
        raise PodctlError("ssh %s sha256 failed: %s" % (SSH_ALIAS, r.stderr.strip()[-300:]))
    remote = {}
    for line in r.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            remote[pathlib.Path(parts[2].strip()).name] = (int(parts[0]), parts[1])
    lines = []
    for f in files:
        size, digest = f.stat().st_size, hashlib.sha256(f.read_bytes()).hexdigest()
        got = remote.get(f.name)
        if got != (size, digest):
            raise PodctlError("%s: size/sha256 differ after upload (local %d %s, remote %s)" % (f.name, size, digest[:12], got))
        lines.append("ok  %10d  %s  %s/%s" % (size, digest[:12], to, f.name))
    return "\n".join(lines)


def _sq(s):
    import shlex
    return shlex.quote(str(s))


# ---------------------------------------------------------------- install: the one-command Claude path (plan Part D1)
#
# The sequence proven by hand on 2026-09-05/06, for each item in order (the base first, then packages):
#   1. the local zip is current (package.py --check)      2. record the volume size, upload the zip
#   3. extract on the pod (/workspace/packages; a package into its own folder there, 2.0.17)
#   4. `--check` on the pod — non-zero stops everything
#   5. the real run, detached, with --latest (BASE_RESTART=1 for a package), its console under state/logs
#   6. poll the console for "STEP RC=" (progress = the last section header)
#   7. copy the pod's state/logs beside the item (_build/podruns/<date>/pod-logs/), print the summary block
#   8. red → stop with the console's path; green → save the printed pin rows into the local script, rebuild its zip
# Every pod-side action goes through a PodIO so the whole sequence runs against a local stand-in in the suite.

PIN_ROW = re.compile(r'^\s*"([^|"\s]+)\|(https?://[^|"]+)\|([0-9a-f]{7,40})\|([^"]*)"\s*$', re.M)
STEP_RC = re.compile(r"^STEP RC=(\d+)\s*$", re.M)


def pin_rows(console_text):
    """The pin rows a run printed (its NODE PACKS section): (name, url, sha, rest), in order."""
    return [(m.group(1), m.group(2), m.group(3), m.group(4)) for m in PIN_ROW.finditer(console_text)]


def save_pins(script_path, rows):
    """Rewrite the sha of every local PACKS row whose name AND url match a printed row ("latest, then tested, then saved").
    Rows the script does not declare are ignored. Returns [(name, old, new)] for what changed."""
    script_path = pathlib.Path(script_path)
    text = script_path.read_text(encoding="utf-8")
    changes = []
    for name, url, sha, _rest in rows:
        rx = re.compile(r'("%s\|%s\|)([0-9a-f]{7,40})(\|)' % (re.escape(name), re.escape(url)))
        m = rx.search(text)
        if m and m.group(2) != sha:
            text = text[:m.start(2)] + sha + text[m.end(2):]
            changes.append((name, m.group(2), sha))
    if changes:
        script_path.write_text(text, encoding="utf-8")
    return changes


def repo_root(path):
    """The repository root above a package or base path: the nearest ancestor holding base/comfyui-base/base.sh
    (the layout <repo>/<brand>/packages/<name>/ and <repo>/base/comfyui-base/). None when this base is its own
    repository with no brand tree above it."""
    p = pathlib.Path(path).resolve()
    for a in p.parents:
        if (a / "base" / "comfyui-base" / "base.sh").exists():
            return a
    return None                                            # 2.2.0: a base that is its own repository has no brand tree above it


def pins_elsewhere(script_path, rows):
    """What a green --latest run MEASURED for files the run does not own — every other '*-script.sh' beside the
    package and the base's own pack list — WITHOUT writing any of them. Returns [(path, pack, old, new)].

    It used to write them. That is the fault behind three separate blockages on 2026-09-08: an install is a
    process whose job is to install, and it was mutating source files belonging to sessions that were not
    watching, leaving each one's zip stale against its tree — and `package.py --check` then refused the next
    upload by whoever touched that package next, for a pin they had never seen. A pin is a MEASUREMENT ("these
    commits were green on this pod at this time"), and a measurement belongs in an artefact that a human
    accepts, not in someone else's source file."""
    script_path = pathlib.Path(script_path).resolve()
    root = repo_root(script_path)
    if root is None:
        return []
    targets = []
    for d in sorted(root.glob("*/packages/*/")):          # every package of every brand, 2026-09-12 layout
        if d.is_dir() and d.resolve() != script_path.parent:
            targets += sorted(d.glob("*-script.sh"))
    packs_sh = root / "base" / "comfyui-base" / "lib" / "40-packs.sh"
    if packs_sh.exists():
        targets.append(packs_sh)
    report = []
    for t in targets:
        text = t.read_text(encoding="utf-8")
        for name, url, sha, _rest in rows:
            m = re.search(r'("%s\|%s\|)([0-9a-f]{7,40})(\|)' % (re.escape(name), re.escape(url)), text)
            if m and m.group(2) != sha:
                report.append((t, name, m.group(2), sha))
    return report


def write_pins_artefact(base_dir, pod_id, slug, report, today=None):
    """The measurement, kept where it can be applied deliberately: who measured it, on which pod, when, and the
    one command that applies it. Under _build/, which is not in the shipped zip."""
    import datetime
    today = today or datetime.date.today().isoformat()
    out = pathlib.Path(base_dir) / "_build" / "pins" / ("%s-%s-%s.txt" % (today, pod_id, slug))
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# pins measured by a green --latest install — NOT applied: these files belong to other packages.",
             "# measured by %s on pod %s at %s" % (lease_me(), pod_id, datetime.datetime.now().isoformat(timespec="seconds")),
             "# apply deliberately, then run that package's suite and rebuild its zip:",
             "#     uv run \"base/comfyui-base/_build/pod/podctl.py\" pins %s --apply" % out.name, ""]
    for path, name, old, new in report:
        lines.append("%s|%s|%s|%s" % (path, name, old, new))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def apply_pins_artefact(path, base_dir):
    """Apply an artefact's rows to the files it names. Deliberate, by the session that owns them; it does NOT
    rebuild anything, because a zip rebuilt without its suite having run is what blocked an install today."""
    path = pathlib.Path(path)
    if not path.exists():
        path = pathlib.Path(base_dir) / "_build" / "pins" / path.name
    applied = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        target, name, old, new = line.rsplit("|", 3)
        t = pathlib.Path(target)
        if not t.exists():
            continue
        text = t.read_text(encoding="utf-8")
        if old in text:
            t.write_text(text.replace(old, new, 1), encoding="utf-8")
            applied.append((t.name, name, old, new))
    return applied


# ---------------------------------------------------------------- the GPU must be ours (2.0.26)
# nvidia-smi inside a container reports the whole card's memory but lists only that container's processes: memory used
# that nobody visible holds belongs to another container or a leaked host context, and every render dies at its first
# node with the card idle (93.5 GB held outside pod fakepod0000003, 2026-09-06 — every check had passed). The same rule
# as py/gpu_facts.py, asked over ssh BEFORE anything is uploaded; a suite test proves the two agree.
GPU_FOREIGN_LIMIT_MIB = 4096


def gpu_verdict(total, used, apps):
    ours = sum(m for _, m in apps)
    foreign = max(0, used - ours)
    return ours, foreign, ("ours" if foreign <= GPU_FOREIGN_LIMIT_MIB else "NOT-OURS")


def gpu_probe(io):
    """(line, verdict) from the pod: 'verdict=no-gpu' when nvidia-smi does not answer (a GPU-less pod, or the stand-in)."""
    rc, out, err = io.remote("nvidia-smi --query-gpu=memory.total,memory.used --format=csv,noheader,nounits 2>/dev/null; echo ---; "
                             "nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits 2>/dev/null")
    head, _, tail = (out or "").partition("---")
    nums = [x.strip() for x in head.strip().splitlines()[0].split(",")] if head.strip() else []
    if rc != 0 or len(nums) < 2 or not all(n.isdigit() for n in nums[:2]):
        return "verdict=no-gpu", "no-gpu"
    total, used = int(nums[0]), int(nums[1])
    apps = []
    for line in tail.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit():
            apps.append((int(parts[0]), int(parts[1]) if parts[1].isdigit() else 0))
    ours, foreign, v = gpu_verdict(total, used, apps)
    return "total=%d used=%d ours=%d foreign=%d procs=%d verdict=%s" % (total, used, ours, foreign, len(apps), v), v


def gpu_gate(io, say, uploading=True):
    """Print the verdict; True when the run may proceed. `uploading`: the caller was about to upload (install)."""
    line, v = gpu_probe(io)
    if v == "ours":
        say("gpu     %s — the GPU is ours" % line); return True
    if v == "no-gpu":
        say("gpu     no nvidia-smi answer on the pod — headroom not verified"); return True
    m = dict(kv.split("=") for kv in line.split())
    say("STOP: the pod's GPU is not ours — %d GiB of %d GiB are held by processes outside its container (ours: %d GiB, %s visible); "
        "every render would die at its first node.%s\n"
        "      fix: stop and start the pod (podctl stop --wait, then start --wait — a fresh container; the leak may live on this machine),\n"
        "      else redeploy the pod on the volume (a new machine), else RunPod support with the machine id (podctl status)."
        % (int(m["foreign"]) // 1024, int(m["total"]) // 1024, int(m["ours"]) // 1024, m["procs"], " Nothing was uploaded." if uploading else ""))
    return False


def item_for(directory):
    """What an item is on both sides: the base (its zip extracts to 'comfyui-base/…') or a package (a flat zip that
    extracts into '<Name>/…' — see below)."""
    d = pathlib.Path(directory).resolve()
    if (d / "base.sh").exists():
        return {"kind": "base", "dir": d, "name": "ComfyUI Base", "zip": d / "comfyui-base.zip",
                "script": "comfyui-base/comfyui-base-script.sh", "local_script": d / "comfyui-base-script.sh", "slug": "base"}
    name = d.name
    # a package zip is flat, so it extracts into its OWN folder: two packages both ship suite.py + pytest.ini, and flat
    # extraction let the second overwrite the first's. The base zip carries its own prefix.
    return {"kind": "pkg", "dir": d, "name": name, "zip": d / ("%s-%s.zip" % (name, _brand.host_of(d, "runpod"))), "script": "%s/%s-script.sh" % (name, name),
            "local_script": d / ("%s-script.sh" % name), "slug": re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()}


class PodIO:
    """The real pod: ssh (no forwards), scp, the packager. `say` prints progress lines."""
    def __init__(self, api, pod, env=None, packager=None):
        self.api, self.pod, self.env = api, pod, env or os.environ
        self.provider = api if isinstance(api, Provider) else RunPodProvider(api)      # an Api is RunPod's; a provider is any host's
        self.packager = packager or (pathlib.Path(__file__).resolve().parents[1] / "package.py")
    def check_zip(self, d):
        import subprocess
        it = item_for(d)
        args = ["python3", str(self.packager)] + (["--base"] if it["kind"] == "base" else [str(it["dir"])]) + ["--check"]
        r = subprocess.run(args, capture_output=True, text=True, env=self.env)
        if r.returncode != 0:
            raise PodctlError("%s: the zip is not current — rebuild it (%s) and commit before installing:\n%s" % (it["name"], " ".join(args[:-1]), (r.stdout + r.stderr).strip()[-400:]))
        return it["zip"]
    def gate(self, d, say=None):
        """Rule: run the package's OWN suite from the EXTRACTED ZIP, on the Mac,
        BEFORE anything is uploaded — which is what the pod's `--check` does after the upload, ten minutes later.

        `check_zip` already proves the zip matches the working tree. It cannot prove the tree is GOOD: on
        2026-09-08 a base zip was rebuilt mid-iteration, matched the tree exactly, and carried a test that
        failed on the pod — another session picked it up in that window and its install died there instead of
        here. Extraction matters as much as the run: a test that reads `_build/` or an installed pack passes
        beside the repo and fails from the zip, which is two of the four uploads that day."""
        import shutil, subprocess, tempfile
        it = item_for(d)
        say = say or (lambda *_: None)
        tmp = tempfile.mkdtemp(prefix="podctl-gate-")
        try:
            root = pathlib.Path(tmp)
            dest = root if it["kind"] == "base" else root / it["name"]
            dest.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(it["zip"]) as z:
                z.extractall(dest)
            # __file__ is <repo>/comfyui-base/_build/pod/podctl.py, so parents[2] IS the base directory —
            # not the repo root. Joining "ComfyUI Base" onto it again produced a COMFY_BASE that does not
            # exist and every package's own script correctly refused it (found when this gate blocked an
            # install). The base's own gate never noticed:
            # base.sh needs neither variable.
            base_dir = pathlib.Path(__file__).resolve().parents[2]
            script = (dest / "comfyui-base" / "base.sh") if it["kind"] == "base" else (dest / ("%s-script.sh" % it["name"]))
            if not script.exists():
                raise PodctlError("%s: the zip has no %s to run" % (it["name"], script.name))
            env = dict(self.env)
            env.setdefault("BASE_NODE_SRC", str(base_dir.parent / "testbed"))
            env.setdefault("BASE_SERVER", env.get("BASE_SERVER", "127.0.0.1:8199"))
            if it["kind"] != "base":
                env["COMFY_BASE"] = str(base_dir)
            for name in ("COMFY_BASE", "BASE_NODE_SRC"):     # a path that is not there makes the gate lie
                v = env.get(name)
                if v and not pathlib.Path(v).exists():
                    raise PodctlError("%s: the gate would run with %s=%s, which does not exist — the gate must "
                                      "not report a failure it caused itself" % (it["name"], name, v))
            r = subprocess.run(["bash", str(script), "test"], cwd=str(script.parent),
                               capture_output=True, text=True, env=env, timeout=1800)
            tail = (r.stdout + r.stderr).strip().splitlines()
            summary = next((l.strip() for l in reversed(tail) if "passed" in l or "failed" in l), "")
            if r.returncode != 0:
                raise PodctlError("%s: its own suite FAILS from the built zip — fix it here, not on the pod:\n%s"
                                  % (it["name"], "\n".join(tail[-25:])))
            say("gate    %s: %s" % (it["name"], summary or "suite green from the zip"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def upload(self, files, to):
        line = self.provider.record_volume(self.pod, env=self.env)
        if line:
            self.say(line)
        self.say(upload(files, to, env=self.env))
    def remote(self, cmd):
        r = ssh_run(cmd, env=self.env)
        return r.returncode, r.stdout, r.stderr
    def fetch(self, remote_dir, local_dir, newer_than=None):
        """The pod's logs as ONE gzipped tar stream over ssh. `scp <dir>/*` paid a round trip per file, and the directory
        keeps every run's log: 281 files on 2026-09-10, where each fetch took about five minutes of an install step (2.0.54).

        Two things 2.0.54 got wrong, both fixed here (2.0.55):

        * **The directory holds the LIVE comfyui.log.** GNU tar exits **1** ("file changed as we read it") when a file
          grows while it is being archived, and 2.0.54 read any non-zero exit as failure — so an install that finished
          green (STEP RC=0) could die on its own log, before the SUMMARY and before the pins were saved. The bytes are
          a valid archive either way, and the LOCAL extract is the judge of that: exit 1 with a clean extract is a pass.
        * **It copied every run's log every time**, so the cost grew with the history. `newer_than` is a path ON THE POD
          whose mtime is the cutoff — GNU tar's `--newer` takes a file name when the value starts with `/` or `.` — so
          the step's own console file selects exactly what that step wrote. Using the pod's own file, not a timestamp
          computed here, means a clock that differs between this Mac and the pod cannot silently drop the logs.
        """
        import subprocess
        local_dir = pathlib.Path(local_dir); local_dir.mkdir(parents=True, exist_ok=True)
        pick = ("--newer %s " % _sq(newer_than)) if newer_than else ""
        with subprocess.Popen(SSH_CMD + [SSH_ALIAS, "tar -C %s %s-czf - ." % (_sq(remote_dir), pick)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env) as src:
            dst = subprocess.run(["tar", "-xzf", "-", "-C", str(local_dir)], stdin=src.stdout, capture_output=True, env=self.env)
            src.stdout.close()
            err = src.stderr.read().decode(errors="replace")
        # GNU tar exits 1 for "some files differ" — here, a log that grew or rotated while it was being read, where
        # the archive is still whole. Anything else that exits 1 (a missing directory, a permission error) is a real
        # failure, and the exit code alone cannot tell them apart: an EMPTY stream extracts as an empty archive
        # without complaint (macOS bsdtar, measured), so a failed fetch would otherwise pass in silence. The stderr
        # text is what separates them.
        benign = ("file changed as we read it", "file removed before we read it", "socket ignored")
        notes = [l for l in err.strip().splitlines() if l.strip()]
        grew = src.returncode == 1 and bool(notes) and all(any(b in n for b in benign) for n in notes)
        if (src.returncode != 0 and not grew) or dst.returncode != 0:
            raise PodctlError("fetching the pod's logs failed: %s" % (err + dst.stderr.decode(errors="replace")).strip()[-300:])
        if grew:
            self.say("  (the pod kept writing while its logs were copied — that is expected)")
    def rebuild(self, d):
        import subprocess
        it = item_for(d)
        args = ["python3", str(self.packager)] + (["--base"] if it["kind"] == "base" else [str(it["dir"])])
        r = subprocess.run(args, capture_output=True, text=True, env=self.env)
        if r.returncode != 0:
            raise PodctlError("rebuild failed: %s" % (r.stdout + r.stderr).strip()[-300:])
    def sleep(self, s):
        import time
        time.sleep(s)
    def say(self, *a):
        print(*a, flush=True)


def _tail(text, n=25):
    lines = text.rstrip().splitlines()
    return "\n".join("    " + l for l in lines[-n:])


class _Flag:
    """A module-level switch for --no-gate: install()'s signature is part of the suite's contract."""
    value = False


args_no_gate = _Flag()


def install(pod_id, dirs, io, every=30, timeout=3 * 3600, latest=True, save_pins_after=None, save_pins=True, today=None):
    """Run the documented sequence for each directory in order. Returns 0 when every item ended green, 1 at the first
    failure (after printing what stopped it and where the console is). `io` is a PodIO or the suite's stand-in."""
    import datetime
    today = today or datetime.date.today().isoformat()
    items = [item_for(d) for d in dirs]
    for it in items:
        if not it["zip"].exists():
            io.say("STOP: %s has no zip (%s) — build it first" % (it["name"], it["zip"].name)); return 1
    if not gpu_gate(io, io.say):
        return 1
    for it in items:
        io.say("══ %s ══" % it["name"])
        try:
            z = io.check_zip(it["dir"])
            if not getattr(args_no_gate, "value", False) and hasattr(io, "gate"):
                io.gate(it["dir"], io.say)
        except PodctlError as e:
            io.say("STOP: %s" % e); return 1
        io.upload([z], pkgs_dir())
        if it["kind"] == "base":
            rc, out, err = io.remote("cd " + _sq(pkgs_dir()) + " && python3 -m zipfile -e %s ." % _sq(z.name))
        else:
            rc, out, err = io.remote("cd " + _sq(pkgs_dir()) + " && mkdir -p %s && python3 -m zipfile -e %s %s" % (_sq(it["name"]), _sq(z.name), _sq(it["name"])))
        if rc != 0:
            io.say("STOP: extracting %s on the pod failed (exit %d)\n%s" % (z.name, rc, _tail(out + err))); return 1
        io.say("  extracted %s" % z.name)
        rc, out, err = io.remote("cd " + _sq(pkgs_dir()) + " && bash %s --check" % _sq(it["script"]))
        if rc != 0:
            io.say("STOP: %s --check exited %d on the pod — nothing was run:\n%s" % (it["name"], rc, _tail(out + err))); return 1
        io.say("  --check ok")
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
        console = logs_dir_remote() + "/install_%s_%s.console" % (it["slug"], ts)
        prefix = "BASE_RESTART=1 " if it["kind"] == "pkg" else ""
        flags = " --latest" if latest else ""
        inner = '%sbash "%s"%s; echo "STEP RC=$?"' % (prefix, it["script"], flags)      # double quotes: readable in the log, and _sq wraps the whole line
        # braces: only the run goes to the background. `A && B && nohup X &` backgrounds the whole list in a subshell whose
        # stdout is the ssh channel, and that subshell waits for X — the launch ssh then lasted the whole step (2.0.19)
        rc, out, err = io.remote("cd " + _sq(pkgs_dir()) + " && mkdir -p " + _sq(logs_dir_remote()) + " && { nohup setsid bash -c %s </dev/null > %s 2>&1 & }" % (_sq(inner), _sq(console)))
        if rc != 0:
            io.say("STOP: could not start %s on the pod (exit %d)\n%s" % (it["name"], rc, _tail(out + err))); return 1
        io.say("  running: %s\n  console: %s" % (inner, console))
        waited, last_hdr, text = 0, "", ""
        while True:
            rc, out, err = io.remote("cat %s 2>/dev/null" % _sq(console))
            text = out
            m = STEP_RC.search(text)
            if m:
                break
            hdrs = [l for l in text.splitlines() if l.startswith("══")]
            if hdrs and hdrs[-1] != last_hdr:
                last_hdr = hdrs[-1]; io.say("  %s" % last_hdr[:110])
            if waited >= timeout:
                io.say("STOP: %s did not finish within %d s — the run is still going on the pod; console: %s" % (it["name"], timeout, console)); return 1
            io.sleep(every); waited += every
        step_rc = int(m.group(1))
        logs_dir = it["dir"] / "_build" / "podruns" / today / "pod-logs"
        io.fetch(logs_dir_remote(), logs_dir, newer_than=console)   # only what THIS step wrote (2.0.55)
        io.say("  logs → %s" % logs_dir)
        i = text.find("══ SUMMARY")
        io.say(text[i:] if i >= 0 else _tail(text, 40))
        if step_rc != 0:
            io.say("STOP: %s ended with STEP RC=%d — the log is the deliverable: %s" % (it["name"], step_rc, console)); return 1
        if save_pins and it["local_script"].exists():
            changes = save_pins_fn(it["local_script"], pin_rows(text))
            if changes:
                io.say("  pins saved into %s: %s" % (it["local_script"].name, ", ".join("%s %s→%s" % (n, o[:7], w[:7]) for n, o, w in changes)))
                io.rebuild(it["dir"]); io.say("  %s rebuilt — commit the script and the zip" % it["zip"].name)
            else:
                io.say("  pins unchanged (the printed rows equal the script's)")
            # A shared pack may not sit at two commits, so the same rows matter to every other package and to
            # the base's own list — but those files belong to other sessions. Report and record; never write.
            elsewhere = pins_elsewhere(it["local_script"], pin_rows(text))
            if elsewhere:
                art = write_pins_artefact(pathlib.Path(__file__).resolve().parents[2], pod_id, it["slug"], elsewhere, today)
                for path in sorted({c[0] for c in elsewhere}):
                    io.say("  pins MEASURED for %s (not written): %s" % (
                        path.name, ", ".join("%s %s→%s" % (c[1], c[2][:7], c[3][:7]) for c in elsewhere if c[0] == path)))
                io.say("  recorded in %s — apply it from that package's own session" % art)
        io.say("  %s: green" % it["name"])
    return 0


save_pins_fn = save_pins


# ---------------------------------------------------------------- CLI

def cmd_stop(prov, args):
    print(prov.stop(args.pod, wait=args.wait))


def cmd_start(prov, args):
    print(prov.start(args.pod, wait=args.wait, ssh_config=args.ssh_config))
    if args.wait:
        print(gpu_line_over_ssh())  # a fresh machine: the first thing to know is whether the card came back clean


def cmd_deploy(prov, args):
    pod_id = prov.deploy_like(args.like, name=args.name, gpu=args.gpu, wait=args.wait, ssh_config=args.ssh_config)
    if args.wait:
        print(gpu_line_over_ssh())
    print("next: podctl status %s · podctl stop %s --wait (the old machine; remove it in the console when the new one is proven) · podctl install %s --base" % (pod_id, args.like, pod_id))


def cmd_restart(prov, args):
    print(prov.restart(args.pod, wait=args.wait, ssh_config=args.ssh_config))
    if args.wait:
        print(gpu_line_over_ssh())


def cmd_upload(prov, args):
    line = prov.record_volume(prov.pod(args.pod))
    if line:
        print(line)
    print(upload(args.files, args.to or pkgs_dir()))


# ============================================================== the GPU lease: the pod is leased, not grabbed
# One pod, several sessions. One session's install once restarted ComfyUI in the middle of another's
# render, and two packages filed the same 288 MB model at two destinations because neither knew the other was
# working. The lease is the smallest thing that prevents the first: a file on the VOLUME (so it survives a pod
# restart and a new pod on the same volume) naming who holds the GPU, for what, and until when.
#
# It is ADVISORY and it EXPIRES. A crashed session must never lock the pod out, so an expired lease is not a
# blocker and never needs cleaning up; `--force-lease` overrides a live one when a human has decided.
LEASE_PATH = "/workspace/comfy-base/state/gpu.lease"


def lease_session_id(env=None):
    """The session id a blocked session needs in order to message the holder ($PODCTL_SESSION_ID), or ""."""
    env = env if env is not None else os.environ
    return env.get("PODCTL_SESSION_ID", "")


def lease_me(env=None):
    """This session's name in the lease: $PODCTL_SESSION, else user@host/pid — always something a person can ask."""
    env = env if env is not None else os.environ
    who = env.get("PODCTL_SESSION")
    if who:
        return who
    import getpass, socket
    try:
        return "%s@%s/%d" % (getpass.getuser(), socket.gethostname().split(".")[0], os.getpid())
    except Exception:                                        # pragma: no cover - defensive
        return "unknown/%d" % os.getpid()


def lease_encode(holder, purpose, minutes, now=None, session=None):
    """The file's one line. JSON so it can grow a field without breaking a reader that does not know it —
    `session` was added on 2026-09-08 at a waiting session's request: the holder's name says who has the GPU,
    the session id says whom to message."""
    import datetime, json as _json
    now = now or datetime.datetime.now(datetime.timezone.utc)
    exp = now + datetime.timedelta(minutes=max(1, int(minutes)))
    d = {"holder": holder, "purpose": purpose or "",
         "taken": now.isoformat(timespec="seconds"),
         "expires": exp.isoformat(timespec="seconds")}
    session = lease_session_id() if session is None else session
    if session:
        d["session"] = session
    return _json.dumps(d, sort_keys=True)


def lease_parse(text):
    """The lease, or None for empty/absent/corrupt — an unreadable lease is a free pod, never a stuck one."""
    import json as _json
    text = (text or "").strip()
    if not text:
        return None
    try:
        d = _json.loads(text)
    except ValueError:
        return None
    return d if isinstance(d, dict) and d.get("holder") else None


def lease_expired(lease, now=None):
    import datetime
    if not lease:
        return True
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        exp = datetime.datetime.fromisoformat(lease["expires"])
    except (KeyError, ValueError, TypeError):
        return True                                          # no readable expiry: treat as expired, never as forever
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=datetime.timezone.utc)
    return exp <= now


def lease_blocks(lease, me, now=None):
    """(blocked, why). Free, expired, or mine → not blocked. Someone else's live lease → blocked, and `why`
    is what to tell the person: who, doing what, until when."""
    if not lease or lease_expired(lease, now):
        return False, ""
    if lease.get("holder") == me:
        return False, ""
    return True, "the GPU is leased by %s%s until %s%s" % (
        lease["holder"], (" [%s]" % lease["session"]) if lease.get("session") else "",
        lease.get("expires", "?"),
        (" — %s" % lease["purpose"]) if lease.get("purpose") else "")


def lease_read(timeout=30):
    """The lease on the pod, or None. A pod that cannot be reached reads as free: this is advisory."""
    try:
        r = ssh_run("cat %s 2>/dev/null || true" % LEASE_PATH, timeout=timeout)
    except Exception:                                        # pragma: no cover - defensive
        return None
    return lease_parse(r.stdout if r.returncode == 0 else "")


def lease_minutes_keeping(current, holder, minutes, now=None):
    """Never SHORTEN a lease we already hold: a session that took 150 minutes for a long chain and then ran an
    install (which asks for its own, smaller window) had its own lease cut to 90 — measured 2026-09-08, and the
    waiting session saw a window that did not match what it had been told."""
    import datetime
    if not current or current.get("holder") != holder or lease_expired(current, now):
        return minutes
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        exp = datetime.datetime.fromisoformat(current["expires"])
    except (KeyError, ValueError, TypeError):
        return minutes
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=datetime.timezone.utc)
    left = int((exp - now).total_seconds() // 60)
    return max(int(minutes), left)


def lease_take(holder, purpose, minutes, timeout=30):
    """Write the lease. The caller decides whether it is allowed to (lease_blocks)."""
    import shlex
    line = lease_encode(holder, purpose, minutes)
    r = ssh_run("mkdir -p %s && printf '%%s\\n' %s > %s" % (
        shlex.quote(os.path.dirname(LEASE_PATH)), shlex.quote(line), shlex.quote(LEASE_PATH)), timeout=timeout)
    if r.returncode != 0:
        raise PodctlError("could not write the lease: %s" % (r.stderr or r.stdout).strip()[:200])
    return lease_parse(line)


def lease_release(holder, timeout=30):
    """Drop the lease if it is ours (or already gone). Never removes someone else's."""
    cur = lease_read(timeout=timeout)
    if cur and cur.get("holder") != holder:
        return False
    ssh_run("rm -f %s" % LEASE_PATH, timeout=timeout)
    return True


def cmd_pins(api, args):
    base_dir = pathlib.Path(__file__).resolve().parents[2]
    path = pathlib.Path(args.artefact)
    if not path.exists():
        path = base_dir / "_build" / "pins" / path.name
    if not path.exists():
        raise PodctlError("no such pins artefact: %s" % args.artefact)
    if not args.apply:
        print(path.read_text(encoding="utf-8").rstrip()); return 0
    applied = apply_pins_artefact(path, base_dir)
    for fname, name, old, new in applied:
        print("pins    %s: %s %s→%s" % (fname, name, old[:7], new[:7]))
    print("pins    %d row(s) applied — now run that package's suite and rebuild its zip" % len(applied))
    return 0


def cmd_lease(api, args):
    me = args.holder or lease_me()
    if args.release:
        ok = lease_release(me)
        print("lease   released" if ok else "lease   NOT released — it belongs to someone else")
        return 0 if ok else 3
    cur = lease_read()
    if not args.take:
        if not cur:
            print("lease   free")
        else:
            print("lease   %s%s%s · taken %s · expires %s%s" % (
                cur["holder"], (" [%s]" % cur["session"]) if cur.get("session") else "",
                " (EXPIRED)" if lease_expired(cur) else "",
                cur.get("taken", "?"), cur.get("expires", "?"),
                (" · %s" % cur["purpose"]) if cur.get("purpose") else ""))
        return 0
    blocked, why = lease_blocks(cur, me)
    if blocked and not args.force:
        print("lease   REFUSED — %s\n        wait, ask that session, or pass --force" % why)
        return 3
    got = lease_take(me, args.purpose, args.minutes)
    print("lease   held by %s until %s%s" % (got["holder"], got["expires"],
                                             (" · %s" % got["purpose"]) if got.get("purpose") else ""))
    return 0


def resolve_pkg_dir(root, d):
    """A --pkg value as a directory: absolute as given; else relative to the repository root (<brand>/packages/<name>);
    else a bare <name> that exactly one */packages/<name> matches. Never the base folder's parent,
    which is what 2.0.55 joined it onto and which holds no packages."""
    p = pathlib.Path(d)
    if p.is_absolute():
        return p
    if root is None:
        raise PodctlError("--pkg %r: this base is not inside a brand repository, so pass an absolute package path" % d)
    if (root / d).is_dir():
        return root / d
    hits = sorted(x for x in root.glob("*/packages/%s" % d) if x.is_dir())
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise PodctlError("--pkg %r is in more than one brand (%s): pass the <brand>/packages/<name> path" % (d, ", ".join(str(h.relative_to(root)) for h in hits)))
    raise PodctlError("--pkg %r is not a directory under %s (try <brand>/packages/<name>)" % (d, root))


# The old-layout folders the 2026-09-12 rename left on the volume: a package used to be /workspace/packages/<Display Name>/
# with '<Display Name> Script.sh' inside, and the base extracted to 'ComfyUI Base/'. The new install lands beside them
# (/workspace/packages/<name>/ with <name>-script.sh), so a folder is stale exactly when it has no <name>-script.sh and
# either is 'ComfyUI Base' or holds a '* Script.sh'. Nothing else on the volume is touched, and the scan is read-only.
STALE_SCAN = (
    'cd /workspace/packages 2>/dev/null || exit 0; for d in */; do d="${d%/}"; [ -f "$d/$d-script.sh" ] && continue; '
    'if [ "$d" = "ComfyUI Base" ] || ls "$d"/*" Script.sh" >/dev/null 2>&1; then printf \'%s\\n\' "$d"; fi; done'
)


def stale_package_dirs(io):
    """The old-layout folder names under /workspace/packages (see STALE_SCAN), sorted; [] when there are none or no packages dir."""
    rc, out, err = io.remote(STALE_SCAN.replace("/workspace", VOLUME_ROOT))
    return sorted(l for l in (out or "").splitlines() if l.strip())


def stale_workflows_cmd(names):
    """The shell that lists the old shipped workflows those folders put into ComfyUI's browser ('<Display Name> Workflow.json'
    under any /workspace/*/user/default/workflows), by exact name, so a user's own file is never matched."""
    tests = " -o ".join("-name %s" % _sq("%s Workflow.json" % n) for n in names if n != "ComfyUI Base")
    if not tests:
        return "true"
    return "find " + VOLUME_ROOT + "/*/user/default/workflows -maxdepth 1 -type f \\( %s \\) 2>/dev/null" % tests


def prune(io, say, yes=False):
    """List (and with yes=True remove) the old-layout package folders and their workflow copies. Returns the exit code:
    0 with nothing to do or after a removal, 0 after a dry run that found something (the list is the result)."""
    stale = stale_package_dirs(io)
    if not stale:
        say("prune   nothing from the old layout under %s" % pkgs_dir()); return 0
    rc, out, err = io.remote(stale_workflows_cmd(stale))
    wfs = sorted(l for l in (out or "").splitlines() if l.strip())
    for d in stale:
        say("prune   old layout: %s/%s" % (pkgs_dir(), d))
    for w in wfs:
        say("prune   old workflow: %s" % w)
    if not yes:
        say("prune   dry run: pass --yes to remove them (the new <name>/ folders are untouched; a re-install puts a package back)"); return 0
    cmd = "cd " + _sq(pkgs_dir()) + " && rm -rf -- " + " ".join(_sq(d) for d in stale)
    if wfs:
        cmd += " && rm -f -- " + " ".join(_sq(w) for w in wfs)
    rc, out, err = io.remote(cmd)
    if rc != 0:
        raise PodctlError("prune failed (exit %s): %s" % (rc, (err or out).strip()[-400:]))
    say("prune   removed %d folder(s) and %d workflow copy(ies)" % (len(stale), len(wfs)))
    return 0


def cmd_prune(prov, args):
    prov.pod(args.pod)                                   # the machine must exist; the Host block must point at it (podctl ssh-config)
    return prune(_SshIO(), print, yes=args.yes)


def cmd_install(prov, args):
    dirs = []
    here = pathlib.Path(__file__).resolve().parents[2]
    if args.base:
        dirs.append(here)
    root = repo_root(here)
    for d in args.pkg or []:
        dirs.append(resolve_pkg_dir(root, d))
    if not dirs:
        raise PodctlError("nothing to install: --base and/or --pkg <dir>")
    pod = prov.pod(args.pod)
    # The lease before the upload: an install restarts ComfyUI, which kills whatever another session is
    # rendering or training. Advisory and expiring — see the lease block above.
    args_no_gate.value = bool(getattr(args, "no_gate", False))
    me = lease_me()
    blocked, why = lease_blocks(lease_read(), me)
    if blocked and not getattr(args, "force_lease", False):
        print("STOP: %s\n      wait for it, message that session, or pass --force-lease" % why)
        return 3
    minutes = max(20, int(args.timeout / 60) * len(dirs) // 4 or 20)
    minutes = lease_minutes_keeping(lease_read(), me, minutes)      # an install inside a longer chain keeps the chain's window
    try:
        got = lease_take(me, "install: " + ", ".join(d.name for d in dirs), minutes)
        print("lease   held by %s until %s" % (got["holder"], got["expires"]))
    except PodctlError as e:
        print("lease   could not be taken (%s) — continuing; it is advisory" % e)
    try:
        print(write_host_env(prov))
        return install(args.pod, dirs, PodIO(prov, pod), every=args.every, timeout=args.timeout, latest=args.latest, save_pins=args.save_pins)
    finally:
        lease_release(me)


def tunnel_key(pubkey_arg, env=None):
    """The private key beside --pubkey: the flag, else $PODCTL_PUBKEY, else ~/.ssh/id_ed25519.pub (the flag's default is
    None when nothing was given — 2.0.23: the tunnel crashed on it)."""
    env = os.environ if env is None else env
    pub = pubkey_arg or env.get("PODCTL_PUBKEY") or (KEY_PATH + ".pub")
    return re.sub(r"\.pub$", "", os.path.expanduser(pub))


def cmd_tunnel(prov, args):
    import subprocess
    addr = prov.address(prov.pod(args.pod))
    if addr is None:
        print("tunnel  the machine has no reachable address yet (podctl ensure / start --wait)"); return 1
    key = tunnel_key(getattr(args, "pubkey", None))
    argv = tunnel_argv(args.port, host=addr.host, port=addr.port, key=key, user=addr.user)
    print("tunnel  %s" % " ".join(argv), flush=True)
    return subprocess.call(argv)


class _SshIO:
    """The smallest PodIO for a read-only probe over `Host runpod` (a timeout: `status` must never hang on a dead pod)."""
    def remote(self, cmd):
        r = ssh_run(cmd, timeout=40)
        return r.returncode, r.stdout, r.stderr


def gpu_line_over_ssh():
    """The GPU verdict as one printable line, best-effort: no ssh answer is a note, never an error."""
    import subprocess
    try:
        r = ssh_run("true", timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return "gpu     (ssh did not answer within 20 s — podctl ssh-config, then again)"
    if r.returncode != 0:
        return "gpu     (Host %s does not answer — podctl ssh-config first)" % SSH_ALIAS
    lines = []
    gpu_gate(_SshIO(), lines.append, uploading=False)
    return "\n".join(lines)


def cmd_status(prov, args):
    print(prov.status_text(prov.pod(args.pod)))
    print(gpu_line_over_ssh())


def cmd_pods(prov, args):
    for line in prov.pods_lines():
        print(line)


def cmd_image(prov, args):
    if not hasattr(prov, "image_text"):
        raise PodctlError("%s has no image to inspect (a VM host boots from its own image; see hosts/%s/README.md)" % (prov.name, prov.name))
    print(prov.image_text(prov.pod(args.pod)))


def cmd_ensure(prov, args):
    print(prov.ensure(args.pod, pubkey=mac_public_key(args.pubkey), ssh_config=args.ssh_config, entry=getattr(args, "entry", None),
                      library=getattr(args, "library", None), workspace_shared=getattr(args, "workspace_shared", False)))
    print(write_host_env(prov))
    line = prov.record_volume(prov.pod(args.pod))
    if line:
        print(line)


def cmd_ssh_config(prov, args):
    pod = prov.pod(args.pod)
    addr = prov.address(pod)
    if addr is None:
        raise PodctlError("%s has no reachable address yet (status %s)" % (args.pod, prov.facts(pod).get("status")))
    write_ssh_config(args.ssh_config, addr.host, addr.port)
    print("Host %s -> %s:%s" % (SSH_ALIAS, addr.host, addr.port))


class _FreshAppend(__import__("argparse").Action):
    """append that REPLACES the default list on first use (argparse's append extends the default)."""
    def __call__(self, parser, ns, values, option_string=None):
        items = getattr(ns, self.dest, None)
        if items is None or items is self.default:
            items = []
        items.append(values); setattr(ns, self.dest, items)


def build_parser():
    ap = argparse.ArgumentParser(prog="podctl", description=__doc__.split("\n")[0])
    ap.add_argument("--provider", default=None, choices=list(_hosts.NAMES), help="the host (default: $PODCTL_PROVIDER, else the repository's brand.toml, else runpod)")
    ap.add_argument("--ssh-config", default=os.environ.get("PODCTL_SSH_CONFIG") or str(pathlib.Path.home() / ".ssh" / "config"))
    ap.add_argument("--pubkey", default=os.environ.get("PODCTL_PUBKEY"), help="the Mac's public key file (default: the provider's key plus .pub)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pods", help="every pod: id, name, image, status, public ip, port 22")
    p = sub.add_parser("status", help="the pod's facts; env NAMES and placeholder verdicts, never values; the GPU verdict over ssh"); p.add_argument("pod")
    p = sub.add_parser("image", help="the image's entrypoint/cmd and how ensure would wrap them"); p.add_argument("pod")
    p = sub.add_parser("ensure", help="TCP 22, PUBLIC_KEY, the base's boot as the start command, Host runpod"); p.add_argument("pod")
    p.add_argument("--entry", help="the image's own start command when its config cannot be read, e.g. '/start_script.sh'")
    p.add_argument("--workspace-shared", action="store_true", dest="workspace_shared",
                   help="Verda: make the attached SHARED volume BE /workspace, so the base, ComfyUI, the venv, the packages and the "
                        "models all live on one store and this machine needs no data volume (the RunPod shape)")
    p.add_argument("--library", metavar="HOST:/EXPORT", help="Verda: the NFS endpoint of the SHARED library to mount at /mnt/comfy-library "
                                                            "(the shared volume must already be attached to the machine; env VERDA_LIBRARY does the same)")
    p = sub.add_parser("ssh-config", help="write the Host runpod block from the pod's public ip and port 22"); p.add_argument("pod")
    for name, hlp in (("stop", "stop the pod (--wait: until it reports stopped)"), ("start", "start the pod (--wait: until sshd answers; Host runpod refreshed)"),
                      ("restart", "restart the pod (--wait: until sshd answers)")):
        p = sub.add_parser(name, help=hlp); p.add_argument("pod"); p.add_argument("--wait", action="store_true")
    p = sub.add_parser("deploy", help="a NEW pod on the same network volume, cloned from --like <pod> (image, GPU, disk, ports, env, the base's boot as start command); --wait: until sshd answers, Host runpod → the new pod, then the GPU verdict")
    p.add_argument("--like", required=True, metavar="POD"); p.add_argument("--name"); p.add_argument("--gpu", help="a different GPU type id (default: the source pod's)")
    p.add_argument("--wait", action="store_true")
    p = sub.add_parser("upload", help="scp files to the pod (through Host runpod) and verify size + sha256 on both sides")
    p.add_argument("pod"); p.add_argument("files", nargs="+"); p.add_argument("--to", default=VOLUME_ROOT + "/packages")
    p = sub.add_parser("install", help="the whole documented sequence, one command: upload, extract, --check, run with --latest, poll, copy logs, save pins")
    p.add_argument("pod"); p.add_argument("--base", action="store_true", help="ComfyUI Base as step one")
    p.add_argument("--pkg", action="append", help="a package directory (repeatable, in order); relative to the project folder")
    p.add_argument("--every", type=int, default=30, help="poll interval in seconds"); p.add_argument("--timeout", type=int, default=3 * 3600, help="per item, seconds")
    p.add_argument("--no-latest", dest="latest", action="store_false", help="run with the saved pins instead of --latest")
    p.add_argument("--no-save-pins", dest="save_pins", action="store_false", help="do not write the printed pins back after a green run")
    p.add_argument("--force-lease", action="store_true", help="install even though another session holds the GPU lease")
    p.add_argument("--no-gate", action="store_true", help="skip the Mac gate (each zip's own suite, run from the extracted zip) before uploading")
    p = sub.add_parser("prune", help="list the old-layout folders the 2026-09-12 rename left under /workspace/packages (and their workflow copies); --yes removes them")
    p.add_argument("pod"); p.add_argument("--yes", action="store_true", help="remove them (without it, only the list)")
    p = sub.add_parser("pins", help="apply a pins artefact a green --latest install measured for OTHER packages (deliberate, by that package's session)")
    p.add_argument("artefact"); p.add_argument("--apply", action="store_true", help="write the rows (without it, they are only printed)")
    p = sub.add_parser("lease", help="who holds the pod's GPU: read it, take it for N minutes, or release it (advisory, expires)")
    p.add_argument("pod"); p.add_argument("--take", action="store_true"); p.add_argument("--release", action="store_true")
    p.add_argument("--holder", default=None, help="defaults to $PODCTL_SESSION, else user@host/pid")
    p.add_argument("--purpose", default="", help="what you are doing, so the next session knows whether to wait")
    p.add_argument("--minutes", type=int, default=60, help="how long to hold it (default 60)")
    p.add_argument("--force", action="store_true", help="take it even though another session holds a live one")
    p = sub.add_parser("tunnel", help="the ONE ssh session that forwards ports (ComfyUI 8188, JupyterLab 8888); runs until killed")
    p.add_argument("pod"); p.add_argument("--port", type=int, action=_FreshAppend, default=list(TUNNEL_PORTS), help="repeatable; default 8188 and 8888")
    for sp in sub.choices.values():                                  # --provider after the verb too (a hook that reads argv sees both forms)
        sp.add_argument("--provider", dest="provider_after", default=None, choices=list(_hosts.NAMES), help=argparse.SUPPRESS)
    return ap


def main(argv=None):
    import shlex
    args = build_parser().parse_args(argv)
    if getattr(args, "entry", None):
        args.entry = shlex.split(args.entry)
    try:
        prov = set_provider(provider_for(args.provider or getattr(args, "provider_after", None)))
        if prov.experimental:
            print("note    host %s is EXPERIMENTAL: written from first-party docs, not yet proven on a live account (hosts/%s/README.md)" % (prov.name, prov.name), file=sys.stderr)
        rc = {"status": cmd_status, "pods": cmd_pods, "image": cmd_image, "ensure": cmd_ensure, "ssh-config": cmd_ssh_config,
         "stop": cmd_stop, "start": cmd_start, "restart": cmd_restart, "deploy": cmd_deploy, "upload": cmd_upload, "tunnel": cmd_tunnel, "install": cmd_install,
         "lease": cmd_lease, "pins": cmd_pins, "prune": cmd_prune}[args.cmd](prov, args)
        if isinstance(rc, int):
            return rc
    except PodctlError as e:
        print("podctl: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
