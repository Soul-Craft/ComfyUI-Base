# ComfyUI Base

The shared toolchain for ComfyUI workflow packages on a rented or owned NVIDIA GPU: one installer that finds the
machine's ComfyUI, pins its node packs to commits, downloads every model row a package declares, owns the launch line
and the boot, and runs each package's own test suite from the zip it ships. Version 2.3.0, MIT, maintained by SoulCraft.

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
around it. A test that needs the shared ComfyUI testbed skips by name when there is none beside the base.

## Status

| Host | Status |
|---|---|
| RunPod | proven on live pods |
| Verda | experimental: written from first-party docs and tested against a fake API server, not yet run on a live account |
| Crusoe Cloud | interface only: every call mapped in `hosts/crusoe/provider.py`, nothing implemented yet |
| an owned NVIDIA box | a recipe (`hosts/local/`), not yet run end to end |

The Verda and Crusoe providers, and a report from an owned box, are the open items; issues and pull requests welcome.
