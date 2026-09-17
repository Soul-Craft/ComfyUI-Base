"""hosts: the per-host subsets of ComfyUI Base, one folder per host, behind one interface.

The core of the base (lib/, py/, suite/) runs on any NVIDIA GPU on any Linux with one persistent volume root.
What differs between hosts is how a machine is created, reached and booted, and that lives here:

    hosts/runpod/    RunPod REST v1; a container whose start command runs the base's boot; /workspace is a network volume
    hosts/verda/     Verda (DataCrunch) REST; a VM with a first-boot script, the base's systemd unit, a block volume you mount
    hosts/crusoe/    Crusoe Cloud; a VM with an every-boot startup script and a default-deny firewall (interface only, so far)
    hosts/local/     an owned NVIDIA box: no API, a directory of your choosing as the volume root, loopback only

The Mac-side pod driver (_build/pod/podctl.py) loads one provider per run and does everything else generically:
ssh, scp, upload, install, the lease, the tunnel, the GPU gate. A provider answers the questions that differ:
where is the machine, how do I make it run the base's boot, how do I start, stop and clone it.

This package is imported by file path (the driver is a script, not an installed module), so it registers itself
in sys.modules under a fixed name and providers import it back the same way: `import comfyui_hosts`.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
NAMES = ("runpod", "verda", "crusoe", "local")

sys.modules.setdefault("comfyui_hosts", sys.modules.get(__name__))


class PodctlError(Exception):
    """A user-facing failure: the message is the whole diagnosis, exit code 1."""


@dataclasses.dataclass
class Address:
    host: str
    port: int
    user: str


class Provider:
    """What every host must answer. Concrete providers subclass this; the driver never branches on the host's name.

    Attributes a provider sets:
      name          "runpod" | "verda" | "crusoe" | "local"
      volume_root   the persistent root on the machine (the base's BASE_VOLUME), "/workspace" by default
      ssh_user      the login user ("root" on RunPod and Verda, "ubuntu" on Crusoe)
      ssh_alias     the `Host <alias>` block the driver writes and addresses (PODCTL_HOST overrides it)
      key_path      the private key for that block (Path)
      experimental  True until a live account has proven the provider; the driver says so once per run
    """
    name = "abstract"
    volume_root = "/workspace"
    ssh_user = "root"
    ssh_alias = "abstract"
    key_path = pathlib.Path.home() / ".ssh" / "id_ed25519"
    experimental = False

    def pods(self) -> list:
        """Every machine the account has, as provider records (dicts). `facts()` normalises one."""
        raise NotImplementedError

    def pod(self, ident: str) -> dict:
        raise NotImplementedError

    def facts(self, pod: dict) -> dict:
        """Normalised: id, name, status ('running' | 'stopped' | 'transitional' | 'gone'), host, port, user, image, gpu, volumes."""
        raise NotImplementedError

    def address(self, pod: dict) -> Address | None:
        """Where sshd answers, or None while the machine has no reachable address yet."""
        raise NotImplementedError

    def status_text(self, pod: dict) -> str:
        """The `podctl status` lines: facts, never secret values."""
        raise NotImplementedError

    def pods_lines(self) -> list:
        """The `podctl pods` table, one line per machine."""
        raise NotImplementedError

    def ensure(self, pod_id: str, pubkey: str, ssh_config, log=print, **kw) -> str:
        """Make the machine reachable and hand its boot to the base. Idempotent."""
        raise NotImplementedError

    def start(self, pod_id: str, wait: bool = True, ssh_config=None) -> str:
        raise NotImplementedError

    def stop(self, pod_id: str, wait: bool = True) -> str:
        raise NotImplementedError

    def restart(self, pod_id: str, wait: bool = True, ssh_config=None) -> str:
        raise NotImplementedError

    def deploy_like(self, like: str, name=None, gpu=None, wait: bool = True, ssh_config=None, say=print) -> str:
        """A NEW machine cloned from `like`, sharing (or moving) its persistent volume. Returns the new id."""
        raise NotImplementedError

    def record_volume(self, pod: dict, env=None) -> str | None:
        """Write what the base's disk gate needs about the volume (RunPod: state/volume.env). None when nothing applies.
        `env` is the environment the driver's ssh runs with (the suite hands in a stub PATH)."""
        return None

    def host_env(self) -> str:
        """The state/host.env the driver writes on the machine so the base knows which host it is on."""
        return "BASE_HOST=%s\nBASE_VOLUME=%s\n" % (self.name, self.volume_root)


def load_provider(name: str, base_dir=None):
    """Import hosts/<name>/provider.py by path (cached in sys.modules as comfyui_hosts_<name>) and return the module."""
    name = (name or "").strip().lower()
    if name not in NAMES:
        raise PodctlError("unknown host %r: one of %s" % (name, ", ".join(NAMES)))
    key = "comfyui_hosts_" + name
    if key in sys.modules:
        return sys.modules[key]
    base = pathlib.Path(base_dir).resolve() if base_dir else HERE.parent
    path = base / "hosts" / name / "provider.py"
    if not path.exists():
        raise PodctlError("%s has no provider.py (hosts/%s/ holds a recipe only)" % (name, name))
    spec = importlib.util.spec_from_file_location(key, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod
