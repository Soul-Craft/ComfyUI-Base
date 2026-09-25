# hosts/local : an owned NVIDIA box (recipe)

No API, no provider: a machine you own, on your LAN, with a directory of your choosing as the volume root. This folder
is a recipe and one script. Nothing about the driver applies; you are already on the box.

## Status

Still a recipe. The sequence below is the same code every host runs (the RunPod path is proven on live pods), but
nobody has reported an owned box end to end, so nothing here claims to be tested on one. If you run it, the
host-report issue template exists for exactly that, and a report either way moves the row in the top-level README.

## What the box needs

- Ubuntu 22.04 or 24.04 on x86_64.
- An NVIDIA driver (`nvidia-smi` shows it). Since 3.0.0 the base keeps a local box's OS packages and driver at their
  newest too (NVIDIA's `nvidia-open`) when it can act as root (root, or passwordless `sudo`); a driver change stops the
  run with "restart required", and you reboot. Without root it says what it could not upgrade and carries on; a
  driver below 580 is still refused before anything downloads.
- `uv` on PATH (`curl -LsSf https://astral.sh/uv/install.sh | sh`, then a new shell).
- Disk: about 40 GB for the base, plus each package's models (a large image or video package is 60 to 130 GB).

## An RTX Pro 6000, specifically

The RTX Pro 6000 is Blackwell: compute capability 12.0, which the base reads from `nvidia-smi --query-gpu=compute_cap`
and turns into `sm_120` (`lib/10-discover.sh`). Everything else on this host follows from that one number, and it is
worth knowing the chain before something in it refuses to move:

| | what happens | where |
|---|---|---|
| the architecture | `12.0` → `sm_120`, derived at runtime, never a constant | `lib/10-discover.sh` |
| the wheels | torch comes from the newest CUDA build this driver runs, as `uv --torch-backend=auto` picks it | `py/torch_pick.py` |
| the driver | CUDA 13 needs driver **>= 580**, so the gate refuses anything older *before downloading anything* | `_base_driver_gate` |
| the version | the newest torch on that build for the interpreter's own Python; torch is never pinned to a number | `py/torch_pick.py` |
| SageAttention | no wheel on PyPI carries `sm_100`/`sm_120` kernels, so it is built from source for this GPU's sm | `base_build_sageattention` |

Two practical consequences. A driver below 580 stops the run at the gate with nothing downloaded and the venv
untouched. That is the gate working, not a failure to work around: install a newer driver and run it again. And the
SageAttention build wants the GPU visible, because it reads the compute capability to build for it; if you are
building somewhere the card is not present, pass `SAGE_ARCHS="12.0"`.

The base prints what it found before it commits to anything. `bash base.sh status` on the box shows the driver, the
derived sm, the tree it discovered and the venv's torch, which is the fastest way to tell whether the chain above
lines up on your machine.

## The one command

    bash hosts/local/install-local.sh [<volume dir>] [--unit] [installer args...]

- `<volume dir>` defaults to `$HOME/comfy`. The script creates it, writes `<dir>/comfy-base/state/host.env` with all
  four knobs, exports them, and runs the base's own installer, `comfyui-base-script.sh`, two levels up from this folder
  (the same `--check | test | rescue | help` forms as on every host, passed through; `--latest` is a plain run).
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

## Driving it from an agent

`BASE_MCP` defaults to 1, so the install puts `comfy-mcp` in the venv here too. There is no pod and no ssh alias to
point at, so the client just runs it locally against the box's own ComfyUI:

```json
{
  "mcpServers": {
    "comfy": {
      "command": "<volume dir>/ComfyUI/.venv-cu130/bin/comfy-mcp",
      "env": { "COMFY_NO_TELEMETRY": "1", "DO_NOT_TRACK": "1" }
    }
  }
}
```

With ComfyUI on its default `127.0.0.1:8188` nothing more is needed; on another port, add
`"COMFY_LOCAL_URL": "http://127.0.0.1:<port>"`. From a laptop, forward 8188 as below and use the `--over tunnel`
shape instead. Handbook §9.1 has the transports and why the telemetry switches are there.

## Reaching ComfyUI

On the box: `http://127.0.0.1:8188`. From another machine: `ssh -L 8188:127.0.0.1:8188 <you>@<box>` and then
`http://localhost:8188` there. JupyterLab (8888) starts only when `JUPYTER_TOKEN` is set for `boot.sh` (a systemd
drop-in on the unit, or the environment of a manual `bash <dir>/comfy-base/boot.sh`); never tokenless.

Without `--unit`, start the stack by hand after the install:

    BOOT_HOME=<dir>/comfy-base bash <dir>/comfy-base/boot.sh      # never exits; ctrl-c stops it

With it: `systemctl status comfy-base-boot`, `journalctl -u comfy-base-boot -f`, and `sudo systemctl restart
comfy-base-boot` for what `podctl restart` does on a cloud host.
