# hosts/local : an owned NVIDIA box (recipe)

No API, no provider: a machine you own, on your LAN, with a directory of your choosing as the volume root. This folder
is a recipe and one script. Nothing about the driver applies; you are already on the box.

## What the box needs

- Ubuntu 22.04 or 24.04 on x86_64.
- An NVIDIA driver, 580 or newer (`nvidia-smi` shows it). The base does NOT install a driver on a local box; it only
  checks, and refuses an older one before downloading anything.
- `uv` on PATH (`curl -LsSf https://astral.sh/uv/install.sh | sh`, then a new shell).
- Disk: about 40 GB for the base, plus each package's models (a large image or video package is 60 to 130 GB).

## The one command

    bash hosts/local/install-local.sh [<volume dir>] [--unit] [installer args...]

- `<volume dir>` defaults to `$HOME/comfy`. The script creates it, writes `<dir>/comfy-base/state/host.env` with all
  four knobs, exports them, and runs the base's own installer, `comfyui-base-script.sh`, two levels up from this folder
  (the same `--check | --latest | test | rescue | help` forms as on every host, passed through).
- `--unit`, after the installer has run, installs `comfy-base-boot.service` when systemd is present: the unit the
  installer itself wrote beside `boot.sh` (`<dir>/comfy-base/comfy-base-boot.service`, the same one the VM hosts use,
  its `RequiresMountsFor` line omitted because a directory is not a mount), plus a drop-in
  (`/etc/systemd/system/comfy-base-boot.service.d/user.conf`) that runs it as your user. It runs
  `<dir>/comfy-base/boot.sh` at every boot. Writing it needs `sudo`; the script asks once.

The knobs it exports, and what the base does differently on this host:

| knob | value | effect |
|---|---|---|
| `BASE_HOST` | `local` | no RunPod detection, no driver install, no proxy assumptions |
| `BASE_VOLUME` | `<dir>` | the base installs to `<dir>/comfy-base`, the packages to `<dir>/packages` |
| `BASE_VOLUME_KIND` | `dir` | no volume mount check: a plain directory is fine, and `df` on it is the truth |
| `BASE_LISTEN` | `127.0.0.1` | ComfyUI and JupyterLab bind loopback only; nothing on the LAN can reach them |

## Reaching ComfyUI

On the box: `http://127.0.0.1:8188`. From another machine: `ssh -L 8188:127.0.0.1:8188 <you>@<box>` and then
`http://localhost:8188` there. JupyterLab (8888) starts only when `JUPYTER_TOKEN` is set for `boot.sh` (a systemd
drop-in on the unit, or the environment of a manual `bash <dir>/comfy-base/boot.sh`); never tokenless.

Without `--unit`, start the stack by hand after the install:

    BOOT_HOME=<dir>/comfy-base bash <dir>/comfy-base/boot.sh      # never exits; ctrl-c stops it

With it: `systemctl status comfy-base-boot`, `journalctl -u comfy-base-boot -f`, and `sudo systemctl restart
comfy-base-boot` for what `podctl restart` does on a cloud host.
