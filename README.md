# ComfyUI Base

[![verify](https://github.com/Soul-Craft/ComfyUI-Base/actions/workflows/verify.yml/badge.svg)](https://github.com/Soul-Craft/ComfyUI-Base/actions/workflows/verify.yml)

The shared toolchain for ComfyUI workflow packages on a rented or owned NVIDIA GPU: one installer that finds the
machine's ComfyUI, pins its node packs to commits, downloads every model row a package declares, owns the launch line
and the boot, and runs each package's own test suite from the zip it ships. MIT, maintained by SoulCraft.

## What it is

- **The core** (`lib/`, `py/`, `suite/`) runs on any Linux host with an NVIDIA GPU and one persistent volume root.
  Four settings say what differs between hosts: `BASE_HOST`, `BASE_VOLUME`, `BASE_VOLUME_KIND`, `BASE_LISTEN`
  (`lib/00-env.sh` documents them; the handbook §1 explains them).
- **The hosts** (`hosts/`) are the per-host subsets: RunPod (proven on live pods), Verda (experimental, written from
  first-party docs), Crusoe Cloud (interface only, so far) and an owned box (`hosts/local`). `hosts/README.md` is the matrix.
- **The driver** (`_build/pod/podctl.py`) runs on your Mac: `podctl --provider <host> pods | status | ensure | install |
  stop | start | restart | deploy | upload | lease | tunnel`. Everything it does over ssh is the same on every host;
  the provider answers where the machine is and how it boots.
- **The package contract** (handbook §4): a thin `<name>-script.sh` declares its packs, models and tokens and calls
  the base; `package.py` builds its zip; `verify.sh` runs its suite from the repository and from the extracted zip.

## Quick start

On a fresh machine, step one is the base, step two is a package:

```bash
python3 -m zipfile -e comfyui-base.zip . && bash comfyui-base/comfyui-base-script.sh
cd <volume>/packages && mkdir -p "<name>" && python3 -m zipfile -e "<name>-<host>.zip" "<name>" && BASE_RESTART=1 bash "<name>/<name>-script.sh"
```

From a Mac, with the driver, the same two steps are `podctl ensure <machine>` then `podctl install <machine> --base --pkg <dir>`.
The handbook (`comfyui-base-handbook.md`) has the design rules, the per-host recipes and the package contract.

## Using it from your own repository

A brand repository keeps this base as a git submodule at `base/comfyui-base`, declares itself once in `<brand>/brand.toml`
(`name = "..."`, `host = "runpod" | "verda" | "crusoe" | "local"`) and puts its packages at `<brand>/packages/<name>/`.
A `<brand>/families.txt` beside it lists the model families its packages file under, one spelling each. Every tool
then finds them: `package.py --all`, `verify.sh`, the driver's `--pkg`, the shared testbed. Nothing in the base knows a
brand's name; what is specific to a project (its baked image, its boot extension, its own download tooling) lives in
that project's repository and plugs in through the seams the base leaves for it (`hosts/`, `ext/*.sh`, `brand.toml`,
`LOADER_CATS`). Models come from Hugging Face or GitHub; a package needing a file from anywhere else declares it
`LOCAL` and puts it on the volume itself.

## Tests

`bash base.sh test` runs the base's own suite (tiers: unit, install, comfyui, runpod, gpu, bare); from the extracted zip
it runs the same way. `bash _build/verify.sh` does both, for the base and for every package of the brand repository
around it, and is what CI runs on every push.

The `comfyui` tier needs a local ComfyUI to test against. `bash testbed.sh --server` provisions one — upstream at its
newest release tag, plus the base's own pinned packs — and starts it on CPU, so a clone of this repository can run
that tier on its own. It is several GB and gitignored. Tiers that need a GPU or a pod skip by name.

## Comfy MCP

Every machine the base installs comes up drivable by an agent. `comfy-mcp` (the official Comfy-Org MCP server)
goes into the run's venv, and the driver writes the client configuration:

```bash
podctl --provider runpod mcp <machine>              # comfy-mcp runs ON the machine, over ssh: every tool
podctl --provider runpod mcp <machine> --over tunnel # it runs here, through `podctl tunnel`: the run tools only
```

The ssh transport is the default because `install_node`, `search_models`, `get_logs` and `fetch_outputs` need
the ComfyUI tree, and the tree is on the machine. `BASE_MCP=0` skips the install. comfy-cli's telemetry is
disabled in the stage, in the boot environment and in its config file — handbook §9.1 says why that is three
places and not one.

## Contributing

`CONTRIBUTING.md` has the gate, the house rules the suite enforces and the release flow. The short version: one
command, `bash _build/verify.sh`, has to be green before any commit, and CI runs the same one on every push and
pull request. You need `uv` on PATH.

## Status

| Host | Status |
|---|---|
| RunPod | proven on live pods |
| Verda | experimental: written from first-party docs and tested against a fake API server, not yet run on a live account |
| Crusoe Cloud | interface only: every call mapped in `hosts/crusoe/provider.py`, nothing implemented yet |
| an owned NVIDIA box | a recipe with a quickstart (`hosts/local/README.md`, RTX Pro 6000 / Blackwell), not yet reported end to end |

The Verda and Crusoe providers, and a report from an owned box, are the open items; issues and pull requests welcome.
