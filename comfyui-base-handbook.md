# ComfyUI Base — Handbook

**Version 2.5.6.** The shared toolchain every workflow package on a machine sources, on **any host with an NVIDIA
GPU** (RunPod, Verda, Crusoe, an owned box) and on any image or OS that gives it a driver and Python 3. One command,
run once per machine as **step one**; then each workflow package is **step two**, still one command. From a Mac,
`podctl` reaches the machine and hands its boot to the base (§1); after that every boot is the base's.

## 0. Design rules

1. **The base owns the machine — its boot included; a package owns only what is unique to it.** Toolchain (uv,
   Python, torch, ComfyUI, the shared node packs, launch-arg hygiene), the model library layout, discovery and
   relocation on the volume, the boot (sshd, JupyterLab, ComfyUI), restart survival and shared state are the base's.
   A package declares its workflow, its models, its extra packs and its own steps (hooks), nothing more.
2. **One canonical ComfyUI tree on the volume, one venv beside it, one stamp.** The tree is the one recorded in
   `state/boot.env`, else an existing volume tree (`/workspace/runpod-slim/ComfyUI`, `/workspace/ComfyUI`,
   `/workspace/*/ComfyUI`), else the base materialises `/workspace/ComfyUI` from GitHub at the newest release tag —
   beside whatever `models/ user/ output/ input/ custom_nodes/` an image already keeps there. The container disk
   (`/ComfyUI`, `/opt/ComfyUI`, `$HOME/ComfyUI`) is never adopted and never scanned: it is restored from the image at
   every start. The venv is `$COMFY/.venv-cu130`, stamped `.comfy-base-venv` (Python, torch, base, the package that
   last stamped it); an unstamped venv that passes the probe is adopted and stamped; a rebuild happens at the same
   path with a backup and automatic rollback. A second tree on the volume is reported (`other tree`), its models are
   consolidated into the canonical one, nothing is deleted.
3. **Genuine latest, no fallbacks.** Python is the newest CPython minor whose `uv pip compile` resolves ComfyUI's
   requirements plus every pack present in the tree (with the cu130 index); torch is the newest `+cu130` wheel for
   it; ComfyUI is its newest `v*` release tag on branch `comfy-base-stable`. A pick that cannot be made stops the
   run. A venv behind the pick is rebuilt — about ten minutes with wheels cached on the volume. Packs are pinned;
   `--latest` moves them to their remotes' HEAD and prints the rows to paste back (latest, then tested, then saved:
   the pins equal the last green run).
4. **Everything the venv boots from lives on the volume, and the boot cannot brick the machine.**
   `UV_PYTHON_INSTALL_DIR=<volume>/comfy-base/python`, `UV_MANAGED_PYTHON=1`; the probe is HARD on an interpreter
   written to the container disk, so a poisoned venv is never reused. `boot.sh` starts sshd first and never exits,
   so a venv that cannot boot leaves a reachable machine with the reason in the boot log; `rescue` repairs it without
   executing the interpreter. A volume root that is not a mountpoint is refused outright, unless
   `BASE_VOLUME_KIND=dir` says the machine is an owned box (§1).
5. **The library is `models/<category>/<Family>[/<Purpose>]/<upstream filename>`.** A Family is a plain folder name
   (letters, digits, `. _ -`); the base checks the shape, and a brand keeps one spelling per family across its
   packages in `<brand>/families.txt` (§4). Two packages that want the same file declare the same row and it is
   downloaded once. `vae_approx/` is flat; `LLM`, `custom_nodes/<Pack>`, `embeddings`, `sams`, `depthanything`,
   `SEEDVR2`, `RMBG`, `grounding-dino`, `latent_upscale_models` and `insightface` are freeform (flat by their
   consumers' design). Model files in a flat category folder that no row declares (an image's own downloads, the
   user's files) are reported as `unclaimed` and left where they are.
6. **No alternate or optional models.** A package's rows cover every loader that is ACTIVE in its shipped workflow;
   bypassed loaders are reported by name, not downloaded. A `LOCAL` row must already be on the volume or the run
   fails (upload it with `podctl upload`; the next run's scan claims it by name).
7. **The base boots the machine; a run restarts nothing by itself.** `boot.sh` (PID 1 once podctl has pointed a
   RunPod pod's start command at it; the process `comfy-base-boot.service` runs on a VM host) runs sshd → JupyterLab →
   ComfyUI from the base's venv with the base's launch line, then sleeps forever. An install prints the exact launch
   line; `BASE_RESTART=1` stops the running server (found by what it runs, never PID 1) and starts that line, the same
   function boot.sh uses (`lib/85-launch.sh`).
8. **One deletion prompt, default No.** Duplicates, superseded files (only inside the package's own family
   folders) and partials, never a file another installed package claims (the ledger). `BASE_YES=1` approves.
9. **Privacy and secrets.** Outbound hosts are huggingface.co, github.com, pypi.org, pypi.nvidia.com and
   download.pytorch.org; a `MODELS` row may name no other host. Nothing is uploaded. `--disable-api-nodes` is
   launch-arg hygiene. Tokens come from the shell env, else the machine's own environment (PID 1's: `HF_TOKEN` or its
   aliases `HUGGING_FACE_HUB_TOKEN` / `HF_HUB_TOKEN`), else `state/tokens.env` (0600), else one prompt. Template
   placeholders (`token_here`, `replace_with_ids`…) are named and ignored; a token its service rejects fails the run
   before anything downloads. No token ever appears in a log or on the process list: `hf` reads it from its
   environment, and the suite runs with every token name stripped. JupyterLab's token rides `JUPYTER_TOKEN` (or
   `JUPYTER_PASSWORD`) in its environment; without one the boot does not start it and says so (2.1.0). Since 2.5.6,
   where `BASE_LISTEN` is loopback (every host but RunPod, so the terminal is reachable only through an ssh tunnel)
   the boot GENERATES a token instead of leaving the terminal dark, and writes it owner-only to `state/tokens.env`.
   It is still never printed, for the same reason no other token is: with a shared store the boot log is a file every
   machine mounts. `podctl jupyter <machine>` reads it over ssh and prints the URL with the token already in it. On a
   public bind with no credential, 2.1.0's refusal is unchanged.
10. **The base owns ComfyUI's launch flags.** `state/comfyui_args.txt` is the only source. An image's own args file
    (RunPod's `runpod-slim/comfyui_args.txt`) is imported once, on the first real run, and never read again; nothing
    creates a phantom file on images that have none. `HYGIENE_ARGS` entries are ensured present, `HYGIENE_ARGS_REMOVE`
    entries absent (the sage flag stays only when `sageattention` imports in the venv).
11. **The GPU must be ours.** `nvidia-smi` inside a container reports the whole card's memory but lists only the
    container's own processes; memory in use that no visible process holds belongs to another container or a leaked
    context on the host, and every render dies at its first node with the card idle. `py/gpu_facts.py` measures it
    (foreign = used − ours; more than 4 GiB is `NOT-OURS`), discovery prints the numbers and fails the run on
    `NOT-OURS`, `boot.sh` logs them at every start, and podctl asks the same question over ssh BEFORE it uploads
    anything (`install`), after every `start`/`restart --wait`, and in `status`. Nothing inside the pod can free that
    memory: the remedy is a fresh container (stop and start; the leak may live on the machine), else a redeploy of the
    pod on the volume — `podctl deploy --like <pod> --wait`, a NEW pod cloned onto the same volume (image, GPU type,
    disk, ports, env, the base's boot as its start command; the volume's datacenter; never a templateId) that lands
    wherever the datacenter has the GPU free — else the host's support with the machine id.
12. **A pinned, unattended machine is a mode, not a fork** (2.1.0). A baked image may set `BASE_PINNED=1` and
    `BASE_NONINTERACTIVE=1`: `base_update_comfyui` fetches and checks out nothing (`COMFY_TAG` is the image's),
    `base_venv` takes the Python and torch from the seed's stamp and refuses to rebuild, `--latest` is refused, every
    prompt takes its default. Such an image is the base built at the volume's own paths by a project's image generator
    (`BASE_SEED=1`: toolchain and packs, no models) and parked on the container disk; a boot extension (`ext/`, §9)
    copies it onto an empty volume once. The base ships neither the generator nor the extension: what is specific to a
    project lives in that project's repository. Nothing about rules 1 to 11 changes on an unpinned machine.
13. **The model library may be SHARED by several machines** (2.4.0). `BASE_LIBRARY` names a store that more than
    one machine mounts at once, and with it set `models/` and ComfyUI's `output/` and `input/` live there instead of
    on the machine's own volume: a model is downloaded once for all of them rather than once each, and a file one
    machine writes is there for the next with no copy. ComfyUI's own `models/` becomes a LINK to it, not an extra
    search path, because several packs never ask `folder_paths` where their weights are and join
    `folder_paths.models_dir` with their own folder name instead. `user/` and `temp/` stay on the machine: `user/`
    holds the saved workflows the App view opens, and each machine carries only its own package. With no library set
    nothing changes and every path in this handbook reads as it always did. On Verda the store is an `NVMe_Shared`
    volume over NFS at `/mnt/comfy-library`, and a configured library is a hard requirement of the boot unit, so a
    missing mount means no server rather than a panel of empty dropdowns.

14. **The WHOLE root may be the shared store, and then a machine is just a GPU** (2.5.0). This is the RunPod shape
    carried to a host whose machines are virtual machines rather than containers. On RunPod a pod has no disk of its
    own: one network volume mounts at `/workspace` and holds `comfy-base/`, ComfyUI, the venv, `packages/` and the
    models, so the pod is disposable and the volume is the asset. With `BASE_VOLUME_SHARED=1` the same is true here,
    and a machine carries only the disk it boots from. Two things follow, and the base does both for you: ComfyUI's
    `user/` and `temp/` move to `BASE_LOCAL_STATE` on the machine itself, because saved workflows, frontend settings
    and scratch are per machine and two servers writing one `user/` tread on each other; and an install takes a
    lock on the store, because pip and uv write a venv in place and neither expects a second writer. RUNNING takes
    no lock at all, which is what makes several GPUs on one store safe: running only reads.
## 1. Hosts

The core of the base (`lib/`, `py/`, `suite/`) runs on any NVIDIA GPU on any Linux with one persistent volume root.
What differs between hosts is how a machine is created, reached and booted, and that lives in `hosts/<host>/`: the
host's provider (Mac-side, loaded by `podctl`), its startup script and its README. `hosts/README.md` is the matrix.

| Host | The machine | Volume root | The boot | Listen | Status |
|---|---|---|---|---|---|
| `runpod` | a container behind RunPod REST v1, sshd on a mapped port | `/workspace`, a network volume (`mount`) | the start command `podctl ensure` wraps around `boot.sh` | `0.0.0.0` (RunPod's HTTP proxy needs it) | proven |
| `verda` | a VM behind Verda's (DataCrunch's) REST API, `root@<ip>:22`; an OS volume that IS the image next time | `/workspace`, a block volume `startup.sh` formats once and mounts | `comfy-base-boot.service` | `127.0.0.1`, reached through `podctl tunnel` | EXPERIMENTAL: written from first-party docs, not yet proven on a live account |
| `crusoe` | a VM with an every-boot startup script and a default-deny firewall, `ubuntu@<ip>:22` | `/workspace` (`mount`) | `comfy-base-boot.service` | `127.0.0.1`, through the tunnel | interface only |
| `local` | an owned NVIDIA box, no API | a directory of your choosing (`BASE_VOLUME_KIND=dir`) | `comfy-base-boot.service`, installed by hand | `127.0.0.1` | a recipe |

**The four knobs** the core reads (`lib/00-env.sh`; `_base_host_resolve` settles them once every lib is loaded):

- `BASE_HOST`: `runpod | verda | crusoe | local | vm`. Explicit, else `runpod` when PID 1's environment carries
  `RUNPOD_POD_ID`, else read from `<volume>/comfy-base/state/host.env` (the driver writes it on `ensure` and
  `install`), else `vm` (a VM whose host is unknown). `boot.sh` answers the same question the same way (`boot_host`).
- `BASE_VOLUME`: the persistent root, `/workspace` by default. Every path the base keeps across a restart derives
  from it (`<volume>/comfy-base`, `<volume>/packages`, `<volume>/ComfyUI` when the tree is materialised).
- `BASE_VOLUME_KIND`: `mount` (the default: the root must be a mountpoint, never a directory on the container disk)
  or `dir` (the default when `BASE_HOST=local`: a directory on an owned box is the volume).
- `BASE_LISTEN`: the address ComfyUI and JupyterLab bind. `0.0.0.0` on RunPod because its HTTP proxy needs it,
  `127.0.0.1` everywhere else (Verda has no cloud firewall; the driver's ssh tunnel is the way in).

**How the driver picks a host.** `podctl [--provider <host>] <command>`, the flag before or after the verb; without
it `$PODCTL_PROVIDER`, else the one `brand.toml` host of the repository this base sits in (two brands on two hosts
make the flag required), else `runpod`. The provider answers where the machine is and how it starts, stops, clones
and hands its boot to the base; ssh, scp, upload, install, the lease, the tunnel and the GPU gate are the driver's and
the same on every host. The `Host <alias>` block the driver writes is the provider's name (`runpod`, `verda`,
`crusoe`) unless `PODCTL_HOST` overrides it, and the key is the provider's (`~/.ssh/id_ed25519` on RunPod,
`~/.ssh/verda_comfyui` on Verda). `ensure` and `install` write `state/host.env` on the machine (`BASE_HOST`,
`BASE_VOLUME`). An experimental provider says so once per run, on stderr. Credentials (RunPod's key, Verda's client
secret) are read by the provider from the environment or its tool's own config file and are never printed, logged or
placed in an argument. A host whose provider is not implemented (Crusoe today) is refused by name: `hosts/<host>/`
holds the interface and a recipe.

### 1.1 RunPod (proven)

**The one command (2.0.14).** Once a pod is reachable (`podctl ensure`), the whole documented sequence is
`uv run "base/comfyui-base/_build/pod/podctl.py" install <pod> --base --pkg "<package dir>"…`. For each item in order,
the local zip is checked current, the volume size is recorded for the disk gate, the zip is uploaded and extracted,
`--check` runs on the pod (non-zero stops everything), the real run goes detached with `--latest` (`BASE_RESTART=1` for a
package) and is polled until `STEP RC=`, the pod's `state/logs` are copied to `<item>/_build/podruns/<date>/pod-logs/`,
the summary block is printed, and a green run saves the printed pin rows into the local script and rebuilds its zip
("latest, then tested, then saved"). A red run stops with the console's path: the log is the deliverable, the fix loop
starts there. `podctl tunnel <pod>` is the one ssh session that forwards ports (8188, 8888) for the canvas tools and
the browser. `podctl prune <pod>` lists the old-layout folders an earlier layout left under `/workspace/packages`
(`<Display Name>/` with a `<Display Name> Script.sh`, and `ComfyUI Base/`) and their workflow copies in ComfyUI's
browser; `--yes` removes exactly those, after the re-install has put the `<name>/` folders beside them. `--pkg` takes
the repository-relative package path (`<brand>/packages/<name>`) or a bare `<name>` that one brand has.

The REST key comes from `RUNPOD_API_KEY`, else `~/.runpod/config.toml` (written by `runpodctl config --apiKey=…`);
it rides a bearer header and is never printed.

```bash
uv run "base/comfyui-base/_build/pod/podctl.py" status <pod>     # facts; env NAMES and placeholder verdicts, never values; the GPU verdict
uv run "base/comfyui-base/_build/pod/podctl.py" deploy --like <pod> --wait   # a NEW pod cloned onto the same volume (rule 11's redeploy rung)
uv run "base/comfyui-base/_build/pod/podctl.py" ensure <pod>     # 22/tcp · PUBLIC_KEY = the Mac key · the start command · Host runpod · state/host.env
uv run "base/comfyui-base/_build/pod/podctl.py" upload <zip>...  # to /workspace/packages, size + sha256 checked both sides
uv run "base/comfyui-base/_build/pod/podctl.py" stop|start|restart <pod> --wait
```

`ensure` sets the pod's start command to `bash -c "if [ -x /workspace/comfy-base/boot.sh ]; then exec bash …; fi;
<sshd bootstrap>; exec <the image's own entry>"` (the image's entry read from Docker Hub's registry; `--entry` for
private images). Before the base is installed that boots the image as it was plus sshd; afterwards it boots the base.
`stop`/`start --wait` rewrite the `Host runpod` block, because RunPod maps a new public port on every start. ComfyUI
(8188) and JupyterLab (8888) are also on RunPod's HTTP proxy ports, which is why this host binds `0.0.0.0`. Fallback
when the API is not available: the JupyterLab terminal on port 8888, where `bash lib/boot.sh --print-sshd-bootstrap`
prints the sshd snippet to paste.

### 1.2 Verda (experimental)

Written from Verda's own documentation and API reference and not yet proven on a live account; `podctl --provider
verda` says so on every run. The recipe, in full, is `hosts/verda/README.md`; the shape:

- **Credentials:** a client id and secret (OAuth2 client credentials, exchanged for a bearer token), from
  `VERDA_CLIENT_ID` + `VERDA_CLIENT_SECRET` or `~/.verda/credentials` (mode 600). The key is `~/.ssh/verda_comfyui`;
  `ensure` registers its `.pub` with Verda when no registered key equals it.
- **The machine:** an RTX PRO 6000 class instance type (`VERDA_INSTANCE_TYPE`), the `ubuntu-24.04-cuda-13.0-open-docker`
  image, one location for the instance and every volume, and ONE data volume named `<machine>-data` beside it.
  `hosts/verda/startup.sh` (registered as `comfy-base-startup`, and run again by `ensure` over ssh) finds it as the one
  unmounted `/dev/vd[b-z]`, formats it ext4 only when blank, mounts it at `/workspace` by UUID with `nofail`, and
  installs `comfy-base-boot.service`.
- **The sequence:** `podctl --provider verda pods`, `start <os volume id>` (a new instance from that volume, data
  volumes attached), then `ensure <instance id>`, `install <instance id> --base`, `tunnel <instance id>`.
- **Stop and start are delete and redeploy.** A shut-down Verda instance still bills the GPU, so `stop` deletes the
  instance keeping every volume, and `start <os volume id>` redeploys from the OS volume (the disk, the packages, the
  unit and the fstab line all come back). The public ip and the instance id both change on every start; the provider
  rewrites the `Host verda` block, and the OS volume id is the stable handle of a machine.
- **No cloud firewall:** every port bound on `0.0.0.0` is on the internet at once, so the base binds loopback here and
  the tunnel is the way in. The balance is prepaid and at zero every volume goes to the trash, with 96 hours to restore.

The first live pass must settle the ssh user, whether the startup script re-runs on a redeploy, and when `ip`
populates (the README's numbered list); the EXPERIMENTAL notice comes off only when all three are known.

### 1.3 Crusoe (interface only)

A VM with an every-boot startup script and a default-deny firewall, `ubuntu` as the login user, `/workspace` as a
mounted block volume and the base's systemd unit as the boot. `hosts/crusoe/provider.py` is the interface with every
call mapped in its docstring and no body yet, so `podctl --provider crusoe` refuses with the reason; the recipe is
`hosts/crusoe/README.md`. Contributions welcome.

### 1.4 local (a recipe)

An owned NVIDIA box: no API, no driver. `BASE_HOST=local` makes `BASE_VOLUME_KIND=dir`, so `BASE_VOLUME` is a
directory of your choosing; the base binds loopback; `base_boot_install` writes `comfy-base-boot.service` beside
`boot.sh` and installs it itself when it runs as root on a live systemd host, else it prints the `cp` and
`systemctl enable` to run. The recipe is `hosts/local/README.md`.

## 2. Step one on a fresh machine

```bash
cd <volume>/packages
python3 -m zipfile -e "comfyui-base.zip" .      # slim images have no unzip
bash "comfyui-base/comfyui-base-script.sh" [--latest]
```

The base's zip is `comfyui-base.zip`, one artefact for every host. The script copies itself to `<volume>/comfy-base/`
and re-executes from there, then: resolves the host (§1), refuses a volume root that is not a mountpoint (unless
`BASE_VOLUME_KIND=dir`), finds or materialises the canonical tree (§0.2), reads the tokens (§0.9), moves ComfyUI to
its newest tag, gates on the version floor, pins the six shared packs (rgthree, KJNodes, VideoHelperSuite,
cg-use-everywhere, ComfyUI-Manager, ComfyUI-advanced-model-manager), builds or adopts the venv with every present
pack's requirements, sets the launch-arg hygiene, installs `boot.sh` + the boot unit on a VM host + the tools venv
(JupyterLab) + `state/boot.env`, writes its ledger entry, and prints the launch line. `--check` is the dry run (on a
volume with no tree it stops after "would materialise"). `bash base.sh status` lists every package installed on the
machine and, when the driver wrote one, prints `state/host.env`.

On a machine from a baked image step one has already happened inside the image: a boot extension (`ext/*.sh`, §9)
copies the seed onto the empty volume on the first boot, and the project's packages install themselves from their
zips with `BASE_YES=1 BASE_NONINTERACTIVE=1 BASE_PINNED=1 BASE_NO_SUITE=1`. The base ships no such extension.

## 3. Step two: a workflow package

```bash
cd <volume>/packages && mkdir -p "<name>" && python3 -m zipfile -e "<name>-<host>.zip" "<name>" && BASE_RESTART=1 bash "<name>/<name>-script.sh" [--latest]
```

A package zip is `<name>-<host>.zip`, the host being its brand's `brand.toml` (§4; `runpod` for a package with no
brand above it). It is flat and extracts into its own folder (`podctl install` does this): two packages both ship
`suite.py` and `pytest.ini`, and flat extraction let the second overwrite the first's (2.0.17).
Same six forms everywhere: install · `--check` · `--latest` (packs to remote HEAD, prints the rows to paste back) ·
`test [tier]` · `rescue` · `help`, plus the package's own `PKG_COMMANDS`. `BASE_RESTART=1` makes the summary's smoke,
combos and suite run against the server this run installed.

## 4. The package contract

A thin `<name>-script.sh` (60–200 lines) declares, then sources the base and calls `base_main "$@"`:

- Required: `PKG_NAME PKG_ID PKG_VERSION BASE_MIN WF_NAME COMFY_MIN PACKS MODELS`. A package whose `BASE_MIN` is newer
  than the installed base is refused, both versions named. `WF_NAME=""` is allowed (2.2.0): a package with no workflow
  yet; the packager ships no workflow member, and the sync, smoke and combo stages report "no workflow" and pass.
- The brand: a package belongs to the brand whose `brand.toml` sits above it (`<brand>/brand.toml`, with `name` and
  `host = runpod | verda | crusoe | local`). `py/brand.py` reads it; the packager names the zip `<name>-<host>.zip`
  by it and the driver takes its default provider from it. A tree with no `brand.toml` above it (an extracted zip, a
  flat checkout) is host `runpod` by default.
- The brand's families: `<brand>/families.txt` beside `brand.toml`, one family spelling per line (`#` comments), is
  the brand's list of `Family` folders. The base's unit tier, run from the brand repository, checks every package's
  rows against it (`test_unit_every_package_family_is_in_its_brands_families_file`); on the machine a Family is
  checked for shape only, so no zip carries the list. A brand without the file skips the check.
- Optional: `WF_VERSION_KEY` (the `extra` key carrying the workflow's version; default `package_version`),
  `DRIVER_MIN SUPERSEDED LEGACY_DIRS DROPPED_PACKS TOKENS PIP_EXTRA HYGIENE_ARGS HYGIENE_ARGS_REMOVE LOADER_CATS
  PKG_COMMANDS PKG_IMPORT_CHECK PKG_NO_SUITE ZIP_EXTRA`. `PKG_NO_SUITE=1` is how a package that ships no `suite.py`
  says so: the run's test stage notes it, and `verify.sh` passes the package as "no suite, declared". `LOADER_CATS`
  rows (`NodeType|library category`) extend the base's map of loader node types to library folders (`lib/60-sync.sh`
  covers ComfyUI's core loaders and the shared packs'); a package whose own pack loads models declares its loaders here.
- Hooks, called if defined, dry-run aware via `$BASE_DRY`: `pkg_pre_venv pkg_post_venv pkg_pre_models
  pkg_post_models pkg_hygiene pkg_import_check pkg_post_install pkg_summary`, and `pkg_cmd_<name>` per
  `PKG_COMMANDS` row `name|function|help`. A failing hook fails the run. A package whose `pkg_post_venv` compiles
  something (SageAttention) is listed for re-run after any venv rebuild.
- `PACKS` rows: `dir|url|sha|cnr_id|why` — `sha` is 40 hex; a base pack may not be redeclared; two packages pinning
  one pack to two commits is an error naming both.
- `MODELS` rows: `category|Family|Purpose|file|url|bytes|note|alts` — `bytes` exact (`base.sh gen-models <dir>`
  fills it from the Hub); `url` is a Hugging Face `resolve` URL, a GitHub URL (a release asset), `LOCAL`, or
  `hf://owner/repo` with `file` ending in `/` for a repo snapshot; no other host is accepted; `alts` are legacy
  basenames the index may adopt. `base.sh stamp-models <pkg dir>` writes ComfyUI's own `models` array into the
  shipped workflow from these rows (`--check` compares).
- The workflow beside the script must carry `extra.<WF_VERSION_KEY> == PKG_VERSION`; the run refuses a mismatch.
- Helpers a hook may use: `ok miss err note warn hdr would todo`, `base_mkdir`, `base_run` (dry-run aware),
  `_base_yaml_register <section> <key> <path>`, `base_build_sageattention` (SageAttention 2 from source for this GPU's sm,
  dry-run aware; `SAGE_WHEEL` / `SAGE_REF` override; 2.0.22), `$COMFY $CN $M $VENV $PY $PKG_DIR $BASE_STATE`.
  A hook that changed something the running server must reload appends its reason to `PKG_RESTART_WHY` (an array);
  the run's restart hand-off then restarts the server (`BASE_RESTART=1`) or prints the launch line with those reasons,
  never a green summary on a stale server.

Suites ship as `suite.py` + `pytest.ini` beside the script and load the base's plugin (`-p basetest`), which gives
`load_package`, `active_loaders`, `manifest_covers_active_loaders`, `brand_families`, `fake_pod` (layouts `official`,
`community`, `bare`, `volume`), `run_script`, `boot_stubs`, `boot_fake_pod`, `tree_hash`, `code_only` and the per-tier
table. `bash "<name>-script.sh" test` auto-detects the repo testbed (`base/testbed`, `base/testbed.sh`) and its server on
8199, or on the port a running testbed recorded in `base/.testbed-server.port` beside `testbed.sh` (2.2.0:
`_base_use_testbed` and `verify.sh` both read it, so a brand whose testbed runs elsewhere needs no env var);
`BASE_NODE_SRC` / `BASE_SERVER` override; the runner hands the suite `BASE_COMFY` and `BASE_VENV`.

## 5. Pipeline (install)

init (env · log · volume check · discover, or materialise the tree · declarations + `BASE_MIN` gate · banner + driver
gate) → require workflow → tokens (validated; a rejected one stops here) → update ComfyUI → version gate → consolidate
strays (the volume only) → packs git → `pkg_pre_venv` → venv → `pkg_post_venv` → packs pip → `pkg_pre_models` →
models (one index over the volume; move on the same device else copy-verify-delete; hf-xet for the Hub, curl for
GitHub; disk gate first) → prune (one prompt; `unclaimed` reported) → sync workflow paths → `pkg_post_models` →
import check → hygiene → boot (boot.sh, tools venv, boot.env) → ledger → restart hand-off (or `BASE_RESTART=1`) →
smoke → combos → the package's suite → `pkg_post_install` → summary (exit 0, or 1 when anything failed).

## 6. Any host: the contract

| Fact | On every host |
|---|---|
| The volume | the volume root, `BASE_VOLUME` (`/workspace` on the three clouds; your directory on an owned box): the only filesystem a stop/start keeps; a root that is not a mountpoint is refused unless `BASE_VOLUME_KIND=dir` |
| The tree | recorded in `state/boot.env`, else an existing volume tree, else materialised at `<volume>/ComfyUI` (§0.2) |
| The venv | `$COMFY/.venv-cu130`, interpreter under `<volume>/comfy-base/python` |
| Packs | the base's six + each package's, cloned into `$COMFY/custom_nodes`; other directories there are left alone but their requirements go into the venv |
| Models | scanned and consolidated on the volume only; the container disk is never read (it is gone at the next start) |
| Launch flags | `state/comfyui_args.txt` (§0.10) |
| Tokens | the machine's own environment, then `state/tokens.env` (§0.9) |
| The host | `BASE_HOST`, `BASE_VOLUME`, `BASE_VOLUME_KIND`, `BASE_LISTEN` (§1); `state/host.env` when the driver wrote it |
| The boot | `boot.sh`: PID 1 via podctl's start command on RunPod, the process of `comfy-base-boot.service` on a VM host: sshd (keys only) → JupyterLab (base tools venv, `JUPYTER_TOKEN`/`JUPYTER_PASSWORD`) → boot extensions (§9) → ComfyUI (base venv, base line, bound to `BASE_LISTEN`) → sleep forever |
| Restart | `_base_pids_from_table`: a python running `main.py` from the tree, or naming a `…/ComfyUI/main.py`; never PID 1 |
| Uploads | `<volume>/packages/` (`podctl upload`); each package extracted into its own folder there |
| A baked image | a project's own (the base ships no generator): the base built at these very paths and parked on the container disk; a boot extension copies it onto an empty volume once and installs the project's packages, pinned and non-interactive (§0.12) |

**RunPod, three image families:** RunPod's ComfyUI (tree `/workspace/runpod-slim/ComfyUI`,
`runpod-slim/comfyui_args.txt` imported once, sshd native); community templates that keep the code on the container
disk (`/ComfyUI`) with the persist dirs at `/workspace/ComfyUI`, so the base materialises the code beside them (no sshd;
JupyterLab unauthenticated unless `JUPYTER_TOKEN`); a bare GPU image (everything materialised). `/workspace` is a
network filesystem: `df` reports the shared pool, so the disk gate says when it cannot verify headroom. Slim images
have no `unzip`; `python3 -m zipfile -e` does the job.

**A VM host (Verda, Crusoe):** a fresh OS image with a driver and Python 3, and an empty block volume the host's
startup script formats (ext4, only when blank) and mounts at `/workspace` by UUID; the base's `comfy-base-boot.service`
runs `boot.sh` at every boot (`RequiresMountsFor=/workspace`, and it sleeps rather than fails while the base is not
installed yet). Everything else is the bare-image case: the tree, the venv, the packs are all materialised on the
volume, and the machine is reached through the driver's tunnel because the base binds loopback.

**An owned box (local):** `BASE_HOST=local BASE_VOLUME=<dir>`; the directory is the volume (`BASE_VOLUME_KIND=dir`),
the unit is installed by hand or by the base when it runs as root, sshd is whatever the box already runs, and nothing
listens beyond loopback.

## 7. State on the machine

`<volume>/comfy-base/` holds the library (`base.sh lib/ py/ suite/ hosts/`, verified against `MANIFEST.sha256` on
every source), `boot.sh`, `comfy-base-boot.service` (the unit's text, always written; installed under
`/etc/systemd/system` on a VM host), `ext/` when a project's image put extensions there (§9), `state/` (`boot.env`,
`host.env` (`BASE_HOST`, `BASE_VOLUME`, written by the driver), `comfyui_args.txt`, `tokens.env` 0600,
`constraints-torch.txt`, `boot-facts.txt`, `volume.env` on RunPod, `gpu.lease`, `packages/<id>.manifest`, `logs/`: install
logs, `boot_<ts>.log`, `comfyui.log`, `jupyter.log`), `python/` (the uv-managed interpreter), `tools/` (JupyterLab) and
`dead-venvs/` (quarantined venvs). A package manifest records its packs, model claims, superseded names and hooks;
`bash base.sh status` renders them and prints `state/host.env` when there is one; `bash base.sh latest` asks every
pinned pack's remote what HEAD is now.

## 8. Tests

Tiers: `unit` (source contracts on the library, podctl and the RunPod provider against a fake RunPod REST server,
boot.sh with stubs, the host layer in `suite/test_hosts.py` (the four knobs, the boot's host detection and extension
seam, the systemd unit, brand.toml, the driver's provider binding), the Verda provider against a fake REST server in
`suite/test_verda.py`, the derive kit in `suite/test_derive.py`, the canvas kit on a synthetic workflow in
`suite/test_canvas.py`), `install` (the stub package under `_build/stub` run end to end against fake pods of every
layout, offline), `comfyui` (the repo testbed), `runpod` / `gpu` (pod only: the venv the boot activates, the running
server on it, PID 1 = boot.sh with sshd and JupyterLab answering; the last two skip until the stop/start hands the boot
over), `bare` (the shell sources without a machine).

```bash
cd base/comfyui-base && bash base.sh test                                                        # every tier
uv run --no-project --python 3.12 --with pytest python base/comfyui-base/_build/rehearse.py <brand>/packages/<name> --layout official|community|bare|volume
bash base/comfyui-base/_build/verify.sh [<brand>/packages/<name>...]                            # zip current, suite from the repo, suite from the zip
```

The rehearsal unpacks the shipped zips, runs the two steps on a fake pod of that layout, then boots it through
`comfy-base/boot.sh` with stubs; ALL OK means the zips are ready to upload. Standalone (this base as its own
repository), `rehearse.py stub --layout volume` uses the fixture package.

### 8.1 The canvas kit — what every package's canvas is tested with

The same shape as the installer: the base owns the machinery, a package owns its tables. Each piece was written
against the real frontend and a real defect.

| What | Where | Package side |
|---|---|---|
| graph helpers (`byid` through subgraphs, `links`, the frontend's bypass resolver `resolve_frontend`), geometry (`nbound`, rgthree's `membership`), the size floor, `structure_sha`, `note_height`, `Budgets`, `CanvasSpec` | `py/canvas.py` | `from canvas import ...` in suite.py |
| 41 generic tests: the `uiux` budgets (collisions, spacing, grid, notes and labels fit, LOD cliff, fill, bloat, flow, palette, titles, reading order), the `snapshot` tier (the real frontend), the shape-only `config` guards (zip = shipped pair, versions agree, tier gates, handbook numbers, packs pinned) | `py/canvastest.py` | `CANVAS = CanvasSpec(...)` then `from canvastest import *`; markers `uiux`, `snapshot`, `config` in pytest.ini |
| `snapshot.py` (photograph + `sizes.json`), `render.py` (offline review, `--review`), `page.py` (before/after), `panels.py` (every rgthree row clicked in the real frontend), `probe.py` (`graphToPrompt` per state), `canvaslayout.py` (the layout engine) | `_build/canvas/` (repo tooling, not in the zip) | every tool takes `--pkg <package dir>`; `<pkg>/_build/layout.py` = tables + `run(globals())` |

Adopting a package: (1) put its constants in suite.py as literals and declare `CANVAS = CanvasSpec(user_facing=...,
markers=..., cuda_only=..., start_card=..., display="1512x982@2", nest=..., partial=..., tier_gates=...)`, then
`from canvastest import *` before the suite's marker loop; (2) `uv run "base/comfyui-base/_build/canvas/snapshot.py" --pkg <dir>`
once, with the testbed server up, for its `_build/sizes.json`; (3) `python3 "base/comfyui-base/_build/canvas/render.py" --pkg <dir>
--review` and fix what the budgets find -- for a canvas that never had them, expect off-grid boxes, frames too big for
what they hold and a saved view under the LOD cliff; `canvaslayout.py` with a tables file is how a package relays
itself out.

## 9. Boot extensions

`boot.sh` runs sshd, JupyterLab and the saved tokens, then sources every `ext/*.sh` it finds, in name order, before it
starts ComfyUI (`boot_extensions`, 2.2.0). It looks in two places and takes the first that has any: `$BOOT_HOME/ext`
(that is `<volume>/comfy-base/ext`, the installed base on the volume), else `ext/` beside `boot.sh` itself. The second
place exists for a baked image's FIRST boot: the volume is still empty then, so the only copy that can run is the
seed's own, beside the seed's `boot.sh` on the container disk, and copying the seed onto the volume is exactly what
such an extension does. An extension is a bash file sourced into the boot's own shell, so it sees `BOOT_HOME`,
`BOOT_VOLUME`, `BASE_HOST` and `BASE_LISTEN` and must not `exit`; the boot logs each file it sourced, or "no
extensions" when neither place has one.

The base ships no extension. A project's image adds its own stages this way (copying its seed onto an empty volume,
provisioning, a status page), in the project's own repository.

## 10. Record

- 2.5.6: the terminal a customer needs, and four things that had to be right before it could exist. JupyterLab
  now GENERATES a token where `BASE_LISTEN` is loopback, so the machine is not dark until somebody invents a
  credential; on a public bind with no credential 2.1.0's refusal is unchanged, because a generated token there is
  still a service on a proxy URL that nobody asked to start. The token is written owner-only to `state/tokens.env`
  and is NEVER printed: the boot log is a file on the volume, and with a shared store every machine mounts it, so
  the rule that has forbidden logging a supplied token since 2.1.0 applies to a generated one too. `podctl jupyter
  <machine>` reads it over ssh and prints the URL. The IP in that hint is read from the machine's own NIC
  (`hostname -I`); an earlier draft asked api.ipify.org, which the outbound-host allowlist caught — the base asks
  the network nothing about itself. `--port LOCAL:REMOTE` and a per-alias offset (`tunnel_locals`) because with one
  GPU per workflow every machine serves 8188 and one number can reach only one of them; a plain `8188` is
  unchanged. `_base_local_dirs` creates the machine-local directories the launch line names, which is why ComfyUI
  refused to start on a shared root: `--user-directory` pointed at a path no machine had made yet. And
  `recall_library` no longer feeds the shared workspace back to itself as a separate library, which left a stale
  fstab line naming a mount that no longer exists.
- 2.5.5: the GPU lease is the MACHINE's, so it moved off the volume to `BASE_LOCAL_STATE`. It was written to
  `$BASE_VOLUME/comfy-base/state/gpu.lease`, which is per machine only while the volume is. Under the shared root
  (2.5.0) `/workspace` IS one store that every machine mounts, so that one file became a global mutex: three
  machines, three GPUs, one lease, and `lease_blocks` refusing whoever asked second. That is the exact inverse of
  the shape's own claim, which is that installs serialise on the mkdir lock and RUNNING does not, precisely so
  several GPUs can render at once. A pre-2.5.5 lease still on the volume is REPORTED by `podctl lease` and not
  obeyed, because obeying it would reinstate the mutex and it may belong to another machine entirely.
  The rule, and the one to apply to anything added here: anything about the MACHINE is per machine, anything
  about the WORK is shared. Deliberately not moved with it, after checking every `VOLUME_ROOT`-derived path
  rather than the one that bit: `pkgs_dir()` and `upload --to` are the work's; `state/logs/` is the machine's but
  the SHELL side writes there too (`lib/10-discover.sh:5`, `lib/boot.sh`), so moving only the driver's half would
  split an install's logs across two directories, which is worse than one interleaved directory and wants a
  two-sided change. `state/host.env` is the machine's too and is worse than stale: it PROPAGATES, because a
  machine that ensures correctly has `recall_library` read another machine's `BASE_LIBRARY` back out of the
  shared file and mount the store twice. Measured on two machines 2026-09-18; it is owed its own release.
- 2.5.4: a dry run is a report, not a gate, where a report is all it can honestly be. `--check` failed any
  `LOCAL` model row whose file was not on disk, although a `LOCAL` row is placed by the package's own
  `pkg_pre_models`, which does nothing under `BASE_DRY`. So the file was absent BY DESIGN at check time and its
  absence carried no information, and `podctl install --pkg` was gated on a condition the gate itself created:
  every package with a `LOCAL` row stopped at "nothing was run" before it could place the file. Both `LOCAL`
  branches, the file one and the folder-snapshot one, now report the row as unjudgeable **when the package
  declares `pkg_pre_models`**, and are unchanged otherwise: with no such hook a missing file really is missing
  and saying so before the run is the useful answer. A real run still fails either way. Measured on
  video-creator-minimax-h3, whose engine is CivitAI only and has no Hub mirror. Four cases in one test hold it,
  including the positive control that a `LOCAL` file which IS present still reports ok, so the guard cannot
  quietly widen to every row. Also: a `SUPERSEDED` entry the sweep can never match (a path, or a dotfile) is
  still skipped, because that sweep matches basenames only and a path pattern would widen it across a store
  several machines share, but it now says so once per pattern instead of silently doing nothing.
- 2.5.3: the Verda host is no longer EXPERIMENTAL. The three questions its README asked are answered by live
  runs: the ssh user is root, the startup script does re-run on a redeploy and fstab survives on the OS volume, and
  `ip` populates within about 30 s. The driver no longer prints the warning on every command.
- 2.5.2: proven on a live machine, and two things it found. `ensure --workspace-shared` could not swap a mount
  under a running system, so the script reported success while the machine was still on its own disk; it now says
  REBOOT and fails the step, because a report line that lies is worse than an error. And `BASE_VOLUME_SHARED` with
  `BASE_LIBRARY` are alternatives rather than layers: together they mount one store twice and the run warns about
  its own tree seen through the second path, so a shared root now ignores the library and says so. Measured with
  the whole root on the store: a cold `import torch` costs 9.28 s against 2.15 s local, warm 1.55 s.
- 2.5.1: `_base_auth_file` read its own argument. `local a="$1" b="...$a"` expands every word before `local`
  binds any, so the second read the CALLER's scope; it worked only because every in-tree caller held `name` set to
  the same token name, and a caller holding a different one sent a bearer token for the wrong name with no error.
  The library link also ignores everything ComfyUI itself ships, `models/configs/*.yaml` as well as the
  placeholders, and copies it onto the library rather than discarding it.
- 2.5.0: `BASE_VOLUME_SHARED`, the whole root on the shared store (§0.14), which is what makes a machine
  disposable rather than only its models shared. `user/` and `temp/` move to `BASE_LOCAL_STATE`; installs take a
  mkdir lock on the store, honoured for two hours and then broken with the owner named; running is unlocked. On
  Verda `podctl ensure --workspace-shared` mounts the shared volume AS `/workspace`, so the machine needs no data
  volume and `startup.sh` never hunts for one.
- 2.4.0: `BASE_LIBRARY`, a model library several machines mount at once (§0.13). Discovery prefers it over
  `extra_model_paths.yaml`, which is not yet written on a machine's first install; staging is per machine so two
  installs cannot collide on one half-file; the disk gate measures the library rather than the volume. On Verda:
  a shared volume is resolved from the account and attached by type and location, never by name stem.
- 2.3.0: the first public release. Models come from Hugging Face and GitHub only; model families are a brand's list,
  not the base's; the loader map covers ComfyUI's core loaders and the shared packs', packages extend it with
  `LOADER_CATS`; the fake-pod layouts are `official | community | bare | volume`.
