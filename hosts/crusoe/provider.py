"""hosts/crusoe/provider.py: Crusoe Cloud behind the host interface. INTERFACE ONLY, so far.

Every method below raises PodctlError with one line naming the REST call or CLI command that will implement it. The
stub exists so that the driver's provider selection (`--provider crusoe`, `PODCTL_PROVIDER=crusoe`) and a brand.toml
`host = "crusoe"` work today: the driver binds the alias `crusoe`, the user `ubuntu`, the key and the volume root, prints
the EXPERIMENTAL note, and fails on the first call with the line that says what is missing. This docstring is the whole
command map, so whoever implements it needs no research.

THE API
  base URL      https://api.cloud.crusoe.ai/v1
  auth          an access key id and a secret. EVERY request is signed:
                  payload   = "/v1" + path + "\n" + query + "\n" + VERB + "\n" + timestamp + "\n"
                              (query: the parameters sorted by name, joined as "k=v&k=v"; empty when there are none)
                  key       = base64.urlsafe_b64decode(secret + padding)      (pad with "=" to a multiple of 4)
                  signature = base64.urlsafe_b64encode(hmac.new(key, payload, sha256).digest()).rstrip("=")
                  headers   X-Crusoe-Timestamp: <timestamp>          (RFC3339, UTC, whole seconds, e.g. 2026-09-16T10:00:00Z)
                            Authorization: Bearer 1.0:<access_key_id>:<signature>
  credentials   env CRUSOE_ACCESS_KEY_ID and CRUSOE_SECRET_KEY, else ~/.crusoe/config:
                  [default]
                  access_key_id = ...
                  secret_key = ...
                  default_project = ...
                Never printed, logged or placed in an argument (the RunPod provider's rule).
  project id    GET /organizations/projects  (the [default] default_project by name, else the one project)

VMs  (p = project id)
  list          GET  /projects/{p}/compute/vms/instances
  create        POST /projects/{p}/compute/vms/instances
                  name, type, location, image, ssh_public_key, startup_script (a create-time field, see below),
                  optionally disks to attach and --public-ip-type static
  get           GET  /projects/{p}/compute/vms/instances/{id}
                  public ip at network_interfaces[0].ips[0].public_ipv4.address; DYNAMIC by default, so it changes on
                  every stop/start unless the VM was created with a static public ip
  start/stop    PATCH /projects/{p}/compute/vms/instances/{id}   body {"action": "START"} | {"action": "STOP"} | {"action": "RESET"}
                  returns an async operation; poll GET /projects/{p}/compute/vms/instances/operations/{op_id}
                  until state != "IN_PROGRESS"
  delete        DELETE /projects/{p}/compute/vms/instances/{id}
  state         the vocabulary is undocumented; normalise case-insensitively: RUNNING -> running,
                STOPPED or SHUTOFF -> stopped, anything else -> transitional

DISKS
  create/list   POST / GET /projects/{p}/storage/disks   (zonal: the disk's location must be the VM's)
  attach        POST /projects/{p}/compute/vms/instances/{id}/attach-disks    body names the disk and mode read-write
  detach        POST /projects/{p}/compute/vms/instances/{id}/detach-disks
                one read-write attacher at a time; inside the VM it appears as /dev/vd[b-z], stable at
                /dev/disk/by-id/virtio-<serial>; hosts/crusoe/startup.sh formats it once and mounts it at /workspace by UUID

FIREWALL
  rules         POST /projects/{p}/networking/vpc-firewall-rules
                default-deny inbound; 22 is open by default; ingress destinations are the VMs' PRIVATE ips. Nothing
                here opens 8188 or 8888: the driver's ssh tunnel is the way in (BASE_LISTEN=127.0.0.1 on this host).

STARTUP SCRIPT
  a create-time field that Crusoe runs as root on EVERY boot, after the network is up. It cannot be changed through
  the API afterwards: edit the copy under /usr/local/bin/crusoe/ over ssh, or recreate the VM. hosts/crusoe/startup.sh
  is what to pass; it installs the base's systemd unit (comfy-base-boot.service) and writes state/host.env, so the
  driver's write_host_env() is a no-op confirmation here.

INSTANCE TYPES, IMAGE, BILLING
  types         a100-80gb.1x, l40s-48gb.1x, h100-80gb-sxm-ib.8x
  image         ubuntu22.04-nvidia-pcie-docker:latest
  billing       per second while running; a stopped VM bills its disks only

THE CLI (the fallback for every call above)
  brew install crusoecloud/cli/crusoe; then `crusoe <group> <verb> --json`:
    crusoe compute vms list|get|create|start|stop|reset|delete
    crusoe compute vms attach-disks <vm> --disk name=<disk>,mode=read-write
    crusoe compute vms detach-disks <vm> --disk name=<disk>
    crusoe storage disks list|create --name <n> --size 400GiB --location <zone> --block-size 4096
    crusoe networking vpc-firewall-rules create ...
  The provider may shell out to it (a subprocess with --json, the secret never on the command line) while the signed
  REST client is unwritten; both must produce the same normalised facts.

WHAT THE DRIVER DOES GENERICALLY once facts() and address() answer: the Host crusoe block, ssh, scp, upload, install,
the lease, the tunnel, the GPU gate. What a provider must answer is only: where the machine is, how to make it run the
base's boot, how to start, stop and clone it.
"""
from __future__ import annotations

import pathlib

from comfyui_hosts import PodctlError, Provider

VOLUME_ROOT = "/workspace"          # the persistent disk's mount point (hosts/crusoe/startup.sh); the base's BASE_VOLUME on this host


def _todo(what):
    """The one failure shape of this stub: which REST call or CLI command will implement the method."""
    return PodctlError("crusoe: not implemented yet: " + what)


class CrusoeProvider(Provider):
    """Crusoe Cloud behind the host interface; every method raises until it is implemented."""
    name = "crusoe"
    volume_root = VOLUME_ROOT
    ssh_user = "ubuntu"
    ssh_alias = "crusoe"
    key_path = pathlib.Path.home() / ".ssh" / "id_ed25519"
    experimental = True

    def __init__(self, api=None):
        self.api = api          # the signed REST client, or the CLI wrapper, once written; None keeps the stub constructible

    def pods(self):
        raise _todo("GET /projects/{p}/compute/vms/instances lists every VM (CLI: crusoe compute vms list --json)")

    def pod(self, ident):
        raise _todo("GET /projects/{p}/compute/vms/instances/{id} reads one VM by id or name (CLI: crusoe compute vms get <vm> --json)")

    def facts(self, pod):
        raise _todo("normalise the instance record: id, name, state (case-insensitive: RUNNING, STOPPED/SHUTOFF, else transitional), "
                    "network_interfaces[0].ips[0].public_ipv4.address, port 22, user ubuntu, type, image, attached disks")

    def address(self, pod):
        raise _todo("Address(network_interfaces[0].ips[0].public_ipv4.address, 22, 'ubuntu') from GET .../instances/{id}, None while the ip is empty")

    def status_text(self, pod):
        raise _todo("the `podctl status` lines from GET .../instances/{id}: name, type, image, state, public ip, disks, startup script present")

    def pods_lines(self):
        raise _todo("one line per VM from GET /projects/{p}/compute/vms/instances: id, name, type, state, public ip, '22'")

    def ensure(self, pod_id, pubkey, ssh_config, log=print, **kw):
        raise _todo("no API step: the create-time startup script installs the unit; ensure will check that sshd answers on "
                    "<public ip>:22 as ubuntu with the Mac key, write the Host crusoe block, and confirm state/host.env "
                    "(22 is open by default, so POST .../networking/vpc-firewall-rules is not needed)")

    def start(self, pod_id, wait=True, ssh_config=None):
        raise _todo("PATCH /projects/{p}/compute/vms/instances/{id} {\"action\": \"START\"}, poll GET .../instances/operations/{op_id} "
                    "until state != IN_PROGRESS, then rewrite the Host crusoe block (the dynamic public ip changes)")

    def stop(self, pod_id, wait=True):
        raise _todo("PATCH /projects/{p}/compute/vms/instances/{id} {\"action\": \"STOP\"}, poll GET .../instances/operations/{op_id} until state != IN_PROGRESS")

    def restart(self, pod_id, wait=True, ssh_config=None):
        raise _todo("PATCH /projects/{p}/compute/vms/instances/{id} {\"action\": \"RESET\"}, poll the operation, then wait for the ssh banner on <public ip>:22")

    def deploy_like(self, like, name=None, gpu=None, wait=True, ssh_config=None, say=print):
        raise _todo("GET the source VM, POST /projects/{p}/compute/vms/instances with its type (or --gpu), location, image, ssh key and "
                    "startup script, then POST .../instances/{source}/detach-disks and POST .../instances/{new}/attach-disks "
                    "(one read-write attacher; the disk is zonal, so the new VM must be in its zone)")

    def record_volume(self, pod):
        return None             # df on an ext4 persistent disk reports the truth; nothing to record (the RunPod pool quirk does not apply)
