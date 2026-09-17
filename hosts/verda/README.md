# hosts/verda: ComfyUI Base on a Verda (DataCrunch) VM

**EXPERIMENTAL.** This host was written from Verda's own documentation (docs.verda.com and the API reference at
api.verda.com/v1/docs) and has not yet been proven on a live account. `podctl --provider verda` says so on
every run. The first live pass must verify, in this order, and this notice comes off only when all three are known:

1. **The ssh user.** The provider assumes `root` on port 22 of the public ip (Verda's docs say the images log in as
   root). If a first `ssh verda hostname` is refused, try `ubuntu` and change `ssh_user` in `provider.py`.
2. **Whether the startup script re-runs on a redeploy from an OS volume.** The docs say a startup script runs once,
   on the first deploy of a fresh image. The provider does not depend on it either way (`podctl ensure` runs the
   same script's `--ensure` pass over ssh), but the answer decides whether a `podctl start <os volume>` machine
   is ready before or only after `ensure`.
3. **When `ip` populates.** The provider polls `GET /instances/{id}` until `status == "running"` and `ip` is not
   null, then probes port 22 for an ssh banner. If the ip is present while provisioning, or absent for a while
   after running, the wait still works but the timings in this README are wrong.

Everything below is the recipe for a person. The Mac side is `_build/pod/podctl.py --provider verda ...`; the
machine side is `hosts/verda/startup.sh` (mount the data volume, install the boot unit), and the base itself is
the same on every host.

## 1. Account and credentials

Verda authenticates with a client id and a client secret (OAuth2 client credentials, exchanged for a bearer
token at `POST /oauth2/token`). Make the pair in the console under API keys, then put it in ONE of these places:

- the environment: `export VERDA_CLIENT_ID=... VERDA_CLIENT_SECRET=...`
- the file `~/.verda/credentials`, mode 600:

      client_id = your-client-id
      client_secret = "your-client-secret"

  Quoted or unquoted values are both read; `#` lines are comments. The pair is never printed, logged or placed in
  an argument or a URL: it travels once, in the JSON body of the token request, and the token travels as
  `Authorization: Bearer`.

The base URL is `https://api.verda.com/v1`; `VERDA_API_URL` overrides it (the suite points it at a fake).

## 2. The ssh key

The provider's key is `~/.ssh/verda_comfyui`. Make it once:

    ssh-keygen -t ed25519 -f ~/.ssh/verda_comfyui -C comfyui-mac

`podctl ensure` registers the `.pub` with Verda (`POST /sshkeys`, name `comfyui-mac`) when no registered key
equals it, and every `start` and `deploy` passes the matching key ids. A machine deployed from the console
before the key was registered has no way to receive it after the fact except the console: add it there, or
redeploy.

## 3. Instance type, image, location, and the data volume

- **Instance type:** an RTX PRO 6000 class card, 1x. `GET /instance-types` lists the exact names; put the one
  you use in `VERDA_INSTANCE_TYPE` (the provider needs it when it redeploys from an OS volume, because a volume
  does not remember the machine it was on). `GET /instance-availability/<type>` says whether one is free.
- **Image:** `ubuntu-24.04-cuda-13.0-open-docker` (Ubuntu 24.04, CUDA 13.0, the open kernel module, Docker).
  `GET /images` lists the rest. The base needs nothing from the image but a driver and Python 3.
- **Location:** one of `FIN-01`, `FIN-02`, `FIN-03`, `ICE-01` (`GET /locations`). The instance and every volume it
  uses must be in the same location; pick one and stay in it.
- **The data volume:** an NVMe volume in that location, sized for the models you will keep (500 GB is a
  reasonable first size), named with the machine's name plus `-data`: `comfy-base-data` beside an instance
  `comfy-base`. The name matters: when the machine is stopped, `podctl start <os volume id>` attaches every
  detached data volume whose name shares the OS volume's prefix. `startup.sh` finds it as the one unmounted
  `/dev/vd[b-z]`, formats it ext4 (label `comfy-volume`) when blank, and mounts it at `/workspace` by UUID with
  `nofail`. A disk that already carries a filesystem is kept, never reformatted. Attach exactly ONE data disk;
  with two the script refuses to choose (set `COMFY_DATA_DISK=/dev/vdX` and run `--ensure` if you must).

## 4. The first deploy

Either way, the sequence ends with `ensure` and `install`.

**From the console:** create the instance with the type, image and location above, your ssh key, and the data
volume as an existing volume. If the startup script `comfy-base-startup` already exists in your account (a
previous `ensure` registered it), pick it: the first boot then mounts `/workspace` and installs the boot unit
by itself. If it does not exist yet, deploy without one; `ensure` does the same work over ssh a minute later.

**From podctl**, once one OS volume exists in the account (that is, after one console deploy and one stop):

    podctl --provider verda pods                    # instances, and the detached volumes of stopped machines
    podctl --provider verda start <os volume id>    # a new instance from that volume, data volumes attached

Then, for a running instance:

    podctl --provider verda ensure <instance id>    # key, script, ssh block, mount + unit over ssh
    podctl --provider verda install <instance id> --base
    podctl --provider verda tunnel <instance id>    # 8188 and 8888 on localhost

`ensure` is idempotent: the second run registers nothing, rewrites only the `Host verda` block, and `startup.sh
--ensure` reports the mount and the unit as unchanged. It prints the script's READY line, which names the disk,
the mount, the unit state and whether `/workspace/comfy-base/boot.sh` is present yet. Before the base is
installed the unit sits in `sleep infinity`; after `install --base`, `systemctl restart comfy-base-boot` on the
machine (or a `podctl restart`) boots the base.

## 5. Stop and start: delete keeping the volumes

A shut-down Verda instance still bills the GPU. So `podctl stop` does NOT shut down: it sends
`PUT /instances {"action": "delete", "volume_ids": []}`, which deletes the instance and keeps every volume,
and it prints the ids it kept:

    stopped <id>: instance deleted, volumes kept: OS volume <os-id>, data volumes <data-id>. podctl start <os-id> ...

From then on `podctl pods` shows the two detached volumes instead of the instance. `podctl start <os-id>`
deploys a new instance with `image=<os-id>` (the OS volume is the image next time: the disk, the packages,
the unit, the fstab line all come back), attaches the data volumes that share its name, and waits for
`running` plus an ip plus an ssh banner. Two things change on every start:

- **the public ip.** The provider rewrites the `Host verda` block, so `ssh verda` keeps working. OpenSSH
  remembers host keys by ip, and a reused ip with a different key is refused; `ssh-keygen -R <ip>` clears it.
- **the instance id.** The stopped id is gone; the OS volume id is the stable handle of a machine.

`podctl restart` is the API pair `shutdown` then `start` on the same instance (never `reboot` inside the
guest: Verda's confidential-computing instances forbid it, and the API path is the same for every type). The
ip may change here too; the block is rewritten.

## 6. The balance

Verda bills hourly from a prepaid balance. **At zero balance every instance is discontinued and every volume
goes to the trash, with 96 hours to restore them.** Watch the balance before a long run; the provider cannot
(there is no balance endpoint it reads) and a `discontinued` status in `podctl status` is the symptom.

## 7. No cloud firewall

Verda instances have no firewall in front of them: every port a process binds on `0.0.0.0` is on the
internet, on the public ip, immediately. On this host the base binds ComfyUI to loopback: `lib/00-env.sh`
reads `BASE_HOST=verda` from `state/host.env` and sets `BASE_LISTEN=127.0.0.1` for every host but RunPod, and
`lib/85-launch.sh` passes it as `--listen`. The way in is `podctl tunnel`, which forwards 8188 and 8888 over ssh.

JupyterLab follows the same setting: `lib/boot.sh` starts it on `BASE_LISTEN`, so on Verda it too listens on
loopback only and is reached through the tunnel on 8888.

Nothing else may listen on `0.0.0.0` on a Verda machine.

## 8. What the provider does, per command

| command | Verda calls |
|---|---|
| `pods` | `GET /instances` plus `GET /volumes` (the detached ones) |
| `status <id>` | `GET /instances/{id}`, falling back to `GET /volumes/{id}` for a stopped machine |
| `ensure <id>` | `GET/POST /sshkeys`, `GET/POST /scripts`, poll `GET /instances/{id}`, ssh: `startup.sh --ensure` |
| `ssh-config <id>` | `GET /instances/{id}`, then the `Host verda` block |
| `stop <id>` | `PUT /instances {action: delete, volume_ids: []}` |
| `start <id>` | instance: `PUT {action: start}`; OS volume: `POST /instances {image: <volume id>, existing_volumes: [...]}` |
| `restart <id>` | `PUT {action: shutdown}`, poll offline, `PUT {action: start}` |
| `deploy --like <id>` | source must be offline; `PUT /volumes {action: detach}` per data volume, then `POST /instances` with them |
| `install`, `upload`, `tunnel`, `lease` | generic: ssh and scp through `Host verda` |

`podctl image` has nothing to show on this host (a VM boots from its own image, not a container's), and
`record_volume` writes nothing: the data volume is a real block device, so `df` on `/workspace` is honest.

## 9. On the machine

- `/var/log/comfy-base-startup.log`: every run of `startup.sh`, first boot and `--ensure`, with a READY line.
- `/etc/systemd/system/comfy-base-boot.service`: `RequiresMountsFor=/workspace`; runs `boot.sh` or sleeps.
- `/etc/fstab`: one `UUID=... /workspace ext4 defaults,nofail,x-systemd.device-timeout=30 0 2` line.
- `/workspace/comfy-base/state/host.env`: `BASE_HOST=verda`, `BASE_VOLUME=/workspace`.
- `/root/comfy-base-startup.sh`: the copy `ensure` ran; `bash /root/comfy-base-startup.sh --ensure` repeats it.
