# ComfyUI Base: how work is done in this repository

The shared toolchain for ComfyUI workflow packages on a rented or owned NVIDIA GPU. MIT, maintained by
SoulCraft. The current version is whatever `VERSION` holds; never restate it here, it goes stale. `README.md`
says what this is; `comfyui-base-handbook.md` is the design reference (§0 design rules, §1 hosts, §4 the
package contract, §8 tests). This file is only the working agreement.

## Who consumes it

A consumer repository keeps this one as a git submodule at `base/comfyui-base`, pinned to its own commit, so
several consumers can sit on different versions at the same time; `git submodule status base/comfyui-base` in
the consumer says which. Nothing here knows a brand's name: what is brand-specific lives
in the brand repository and plugs in through `hosts/`, `ext/*.sh`, `brand.toml`, `LOADER_CATS`. Keep it that way.

## Layout

    base.sh                     entry point, sourced by package scripts or run directly (version | install-self | status | test [tier] | rescue)
    lib/00-env.sh … 95-summary.sh, boot.sh   the install and boot stages, in order
    py/                         the Python side (brand.py, canvas.py, ledger.py, torch_pick.py, …)
    suite/                      the base's own pytest suite (test_unit … test_verda)
    hosts/{runpod,verda,crusoe,local}/  per-host providers and recipes; hosts/README.md is the matrix
    _build/package.py           the one packager: package zips and the base zip (writes MANIFEST.sha256 first)
    _build/verify.sh            the gate: zip freshness, suite from the repo, suite from the extracted zip
    _build/pod/podctl.py        the Mac-side driver (`podctl --provider <host> …`)
    comfyui-base.zip, MANIFEST.sha256   built artefacts, committed; an installed copy must match the manifest

## The gate

`bash _build/verify.sh` is the check before any commit. It runs `package.py --check`, then the suite from the
repository, then the suite from the extracted zip, because a test that passes at step 2 and fails at step 3 is
what breaks a pod ten minutes and one upload later. `bash base.sh test [tier]` runs the suite alone; tiers are
`unit`, `install`, `comfyui` (needs the shared testbed beside the base), `runpod`, `gpu`, `bare`.

`pytest.ini` sets `filterwarnings = error`. Warnings are fixed, never filtered, and that line is not negotiable.

## Releasing a base version

1. Change the code, keep `verify.sh` green.
2. Bump `VERSION`, then `python3 _build/package.py --base` (rewrites `MANIFEST.sha256`, then `comfyui-base.zip`).
3. Run `bash _build/verify.sh` again; it fails if the zip is stale.
4. Commit as `ComfyUI Base X.Y.Z: <what changed>` (the log's existing convention) and tag `base-vX.Y.Z`.
5. In each consumer repository, bump the submodule pin in a base-only commit (`git add base/comfyui-base`).

Never edit `MANIFEST.sha256` or the zip by hand, and never edit this code from inside a brand repository's
submodule checkout: fix it here, release, then bump the pin there.

## Hosts

RunPod is proven on live pods. Verda is experimental (tested against a fake API only). Crusoe is an interface
stub. `hosts/local` is a recipe. Say which host a change was actually exercised on when you describe it.
