"""hosts/verda/provider.py: Verda (DataCrunch), the second host. A VM behind a REST API, an OS volume that IS the
image next time, one data volume the first-boot script mounts at /workspace, and the base's boot as a systemd unit.

Written from first-party docs (docs.verda.com, api.verda.com/v1/docs) and NOT yet proven on a live account, so
`experimental = True` and the driver says so once per run. hosts/verda/README.md lists what the first live run must
verify (the ssh user, whether the startup script re-runs on a redeploy from an OS volume, when `ip` populates).

The credentials are a client id + client secret pair: VERDA_CLIENT_ID + VERDA_CLIENT_SECRET, else ~/.verda/credentials
(`client_id = ...` / `client_secret = ...`, quoted or not). They are exchanged for a bearer token at POST /oauth2/token
and are never printed, logged or placed in a URL or an argument. Everything is lazy: constructing the provider reads
nothing and opens no socket; the first REST call loads the pair and takes the token.

The machine model, and why `stop` DELETES:
  * a shut-down instance still bills compute (the GPU stays reserved), so the cheap stop is `delete` with
    `volume_ids: []`, which removes the instance and KEEPS every volume: the OS volume (bootable, redeployed with
    `image=<its id>`) and the data volume(s);
  * `pods()` therefore lists detached volumes beside instances, so a stopped machine stays visible, and `start` takes
    either an instance id (offline -> `start`) or an OS volume id (-> a fresh `POST /instances` from that volume with
    every detached data volume that shares its name prefix attached);
  * the public IP changes on every start, and there is no port mapping and no cloud firewall: sshd is root@<ip>:22
    and every bound port is public, so the base binds loopback on this host and is reached through the driver's tunnel.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

from comfyui_hosts import Address, PodctlError, Provider

HERE = pathlib.Path(__file__).resolve().parent
VOLUME_ROOT = "/workspace"                         # where startup.sh mounts the data volume; the base's BASE_VOLUME here
DEFAULT_API_URL = "https://api.verda.com/v1"
KEY_PATH = pathlib.Path.home() / ".ssh" / "verda_comfyui"
CREDENTIALS_PATH = pathlib.Path.home() / ".verda" / "credentials"
STARTUP_SCRIPT_NAME = "comfy-base-startup"         # the name the script is registered under (GET /scripts)
STARTUP_SCRIPT_FILE = HERE / "startup.sh"
REMOTE_STARTUP = "/root/comfy-base-startup.sh"     # where `ensure` puts the script on the running machine
SSH_PORT = 22
KEY_NAME = "comfyui-mac"                           # the name the Mac key is registered under (POST /sshkeys)

# Verda's `status` vocabulary (api.verda.com/v1/docs) and the driver's four words.
STATUS = {"running": "running", "offline": "stopped", "notfound": "gone", "deleting": "gone"}
TRANSITIONAL = ("provisioning", "discontinued", "unknown", "ordered", "new", "error", "validating", "no_capacity", "installation_failed")


def _c():
    """The driver module (podctl.py): write_ssh_config, ssh_banner, ssh_run, _ssh_hostname, _sq, SSH_ALIAS."""
    return sys.modules["comfyui_podctl"]


def _base_version():
    try:
        return (HERE.parents[1] / "VERSION").read_text().strip() or "0"
    except OSError:
        return "0"


BASE_VERSION_FOR_UA = _base_version()


# ---------------------------------------------------------------- credentials

def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v.strip()


def load_credentials(env=None, path=None):
    """(client_id, client_secret): VERDA_CLIENT_ID + VERDA_CLIENT_SECRET, else ~/.verda/credentials. Never echoed."""
    env = os.environ if env is None else env
    cid = (env.get("VERDA_CLIENT_ID") or "").strip()
    secret = (env.get("VERDA_CLIENT_SECRET") or "").strip()
    if cid and secret:
        return cid, secret
    path = pathlib.Path(path) if path else CREDENTIALS_PATH
    if path.exists():
        found = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line[0] in "#;[":
                continue
            m = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*(.*)$", line)
            if not m:
                continue
            key = m.group(1).lower()
            if key in ("client_id", "verda_client_id"):
                found["id"] = _unquote(m.group(2))
            elif key in ("client_secret", "verda_client_secret"):
                found["secret"] = _unquote(m.group(2))
        cid = cid or found.get("id", "")
        secret = secret or found.get("secret", "")
    if cid and secret:
        return cid, secret
    raise PodctlError("no Verda credentials: export VERDA_CLIENT_ID and VERDA_CLIENT_SECRET, or write both as "
                      "`client_id = ...` and `client_secret = ...` lines in %s (hosts/verda/README.md)" % path)


# ---------------------------------------------------------------- the REST client

class HttpError(PodctlError):
    """A non-2xx answer: the status and the body, never the token or the secret."""
    def __init__(self, method, path, status, text):
        self.status = status
        super().__init__("%s %s -> HTTP %s: %s" % (method, path, status, (text or "")[:300]))


class Api:
    """The few REST calls podctl needs: get/post/put, JSON in, JSON out, one bearer token taken lazily from the
    client-credentials grant. A test passes `base_url` and `token` and no oauth call ever happens."""

    USER_AGENT = "podctl/%s (ComfyUI Base; Verda REST client)" % BASE_VERSION_FOR_UA

    def __init__(self, base_url=None, token=None, credentials=None, env=None, credentials_path=None):
        env = os.environ if env is None else env
        self.base = (base_url or env.get("VERDA_API_URL") or DEFAULT_API_URL).rstrip("/")
        self._token = token
        self._until = float("inf") if token else 0.0
        self._credentials = credentials
        self._env = env
        self._credentials_path = credentials_path

    # -- the token
    def credentials(self):
        if self._credentials is None:
            self._credentials = load_credentials(env=self._env, path=self._credentials_path)
        return self._credentials

    def token(self):
        if self._token and time.time() < self._until:
            return self._token
        cid, secret = self.credentials()
        body = {"grant_type": "client_credentials", "client_id": cid, "client_secret": secret}
        r = self._call("POST", "/oauth2/token", body, auth=False)
        tok = (r or {}).get("access_token") if isinstance(r, dict) else None
        if not tok:
            raise PodctlError("POST /oauth2/token answered without an access_token (check the client id and secret)")
        try:
            ttl = float((r or {}).get("expires_in") or 3600)
        except (TypeError, ValueError):
            ttl = 3600.0
        self._token, self._until = tok, time.time() + max(ttl - 60, 30)
        return tok

    # -- the calls
    def _call(self, method, path, body=None, auth=True, _retry=True):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if auth:
            req.add_header("Authorization", "Bearer " + self.token())
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", self.USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            if e.code == 401 and auth and _retry:              # an expired token: take a fresh one, once
                self._token, self._until = None, 0.0
                return self._call(method, path, body, auth=auth, _retry=False)
            raise HttpError(method, path, e.code, text)
        except urllib.error.URLError as e:
            raise PodctlError("%s %s -> %s" % (method, path, e.reason))
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode(errors="replace").strip()

    def get(self, path):
        return self._call("GET", path)

    def post(self, path, body=None):
        return self._call("POST", path, body)

    def put(self, path, body=None):
        return self._call("PUT", path, body)


# ---------------------------------------------------------------- record helpers

def norm_status(raw):
    return STATUS.get((raw or "").strip().lower(), "transitional")


def inst_ip(inst):
    return inst.get("ip") or inst.get("public_ip") or ""


def is_volume(rec):
    return (rec or {}).get("kind") == "volume"


def new_id(resp):
    """POST /instances answers the new id as a JSON string or as {"id": ...}; accept both."""
    if isinstance(resp, str):
        return resp.strip().strip('"')
    if isinstance(resp, dict):
        v = resp.get("id") or resp.get("instance_id")
        if v:
            return str(v)
        for k in ("instances", "ids"):
            if isinstance(resp.get(k), list) and resp[k]:
                first = resp[k][0]
                return str(first.get("id") if isinstance(first, dict) else first)
    if isinstance(resp, list) and resp:
        first = resp[0]
        return str(first.get("id") if isinstance(first, dict) else first)
    raise PodctlError("POST /instances answered without an id: %r" % (resp,))


def volume_stem(name):
    """The part of a volume name that a data volume shares with its OS volume: `comfy-base-os` and `comfy-base-data`
    both stem to `comfy-base`, as do `OS-comfy-base` and `comfy-base`."""
    s = (name or "").strip().lower()
    s = re.sub(r"^(os|data)[-_ .]+", "", s)
    s = re.sub(r"[-_ .]+(os|root|boot|system|data|workspace|models|vol|volume|disk)$", "", s)
    return s


def stems_match(a, b):
    a, b = volume_stem(a), volume_stem(b)
    return bool(a) and bool(b) and (a == b or a.startswith(b) or b.startswith(a))


def hostname_for(name):
    h = re.sub(r"[^a-z0-9-]+", "-", (name or "").lower()).strip("-")[:63]
    return h or "comfy-base"


def key_matches(registered, pub):
    """Two OpenSSH public keys are the same key when their type and base64 body agree (the comment may differ)."""
    def core(k):
        parts = (k or "").split()
        return tuple(parts[:2]) if len(parts) >= 2 else (k or "").strip()
    return core(registered) == core(pub)


def startup_script_text():
    try:
        return STARTUP_SCRIPT_FILE.read_text(encoding="utf-8")
    except OSError as e:
        raise PodctlError("cannot read %s: %s" % (STARTUP_SCRIPT_FILE, e))


REMOTE_EOF = "COMFY_BASE_STARTUP_EOF"


def remote_ensure_cmd(text):
    """One ssh command: land startup.sh on the machine through a quoted heredoc and run its --ensure pass."""
    if REMOTE_EOF in text:
        raise PodctlError("startup.sh contains the heredoc delimiter %s" % REMOTE_EOF)
    return "cat > %s <<'%s'\n%s\n%s\nchmod 700 %s && bash %s --ensure" % (REMOTE_STARTUP, REMOTE_EOF, text.rstrip("\n"), REMOTE_EOF,
                                                                       REMOTE_STARTUP, REMOTE_STARTUP)


# ---------------------------------------------------------------- the provider

class VerdaProvider(Provider):
    """Verda behind the host interface. `sleep` and the banner are instance attributes so the suite drives every wait
    against a fake REST server without waiting; the ssh helpers are the driver's, reached at call time."""
    name = "verda"
    volume_root = VOLUME_ROOT
    ssh_user = "root"
    ssh_alias = "verda"
    key_path = KEY_PATH
    experimental = True

    def __init__(self, api=None, env=None):
        self.api = api or Api(env=env)
        self.env = os.environ if env is None else env
        self.sleep = time.sleep

    # -- small plumbing
    def banner(self, host, port=SSH_PORT):
        return _c().ssh_banner(host, port)

    def _wait(self, probe, what, tries=120, every=5):
        for _ in range(tries):
            v = probe()
            if v:
                return v
            self.sleep(every)
        raise PodctlError("gave up waiting for %s (%ds)" % (what, tries * every))

    def _instance(self, ident):
        """GET /instances/{id}, or None when Verda says it is gone (404 or status notfound)."""
        try:
            inst = self.api.get("/instances/" + ident)
        except HttpError as e:
            if e.status == 404:
                return None
            raise
        if not isinstance(inst, dict) or (inst.get("status") or "").lower() == "notfound":
            return None
        return inst

    def _volume(self, ident):
        try:
            return self.api.get("/volumes/" + ident)
        except HttpError as e:
            if e.status == 404:
                return None
            raise

    def _volumes(self):
        vols = self.api.get("/volumes") or []
        return [v for v in vols if isinstance(v, dict)]

    def _detached(self, vols=None):
        return [v for v in (vols if vols is not None else self._volumes()) if (v.get("status") or "").lower() == "detached"]

    def _volume_record(self, v):
        rec = dict(v)
        rec.update(kind="volume", name=v.get("name") or "", status="%s-volume" % ((v.get("status") or "unknown").lower()))
        return rec

    def _wait_running(self, ident, what="running with a public ip"):
        def probe():
            inst = self._instance(ident)
            if inst is None:
                raise PodctlError("%s is gone (Verda answers notfound) while waiting for it to be %s" % (ident, what))
            st = (inst.get("status") or "").lower()
            if st in ("error", "installation_failed", "no_capacity", "discontinued"):
                raise PodctlError("%s is %s: read the console (a discontinued instance usually means a zero balance)" % (ident, st))
            return inst if st == "running" and inst_ip(inst) else None
        return self._wait(probe, "%s to be %s" % (ident, what), tries=120, every=5)

    def _wait_banner(self, host):
        for _ in range(120):
            if self.banner(host, SSH_PORT):
                return
            self.sleep(5)
        raise PodctlError("sshd never answered on %s:%d: read the instance's console (the startup log is /var/log/comfy-base-startup.log)" % (host, SSH_PORT))

    def _up_and_configured(self, ident, ssh_config):
        inst = self._wait_running(ident)
        host = inst_ip(inst)
        self._wait_banner(host)
        _c().write_ssh_config(ssh_config or pathlib.Path.home() / ".ssh" / "config", host, SSH_PORT)
        return inst, host

    # -- keys and scripts
    def _local_pubkey(self):
        p = pathlib.Path(str(self.key_path) + ".pub")
        if not p.exists():
            raise PodctlError("no public key at %s: ssh-keygen -t ed25519 -f %s (hosts/verda/README.md)" % (p, self.key_path))
        return p.read_text(encoding="utf-8").strip()

    def key_ids(self, pubkey=None, register=True, log=None):
        """The ids of every registered key equal to the Mac key; registers it (once) when none is."""
        pub = (pubkey or self._local_pubkey()).strip()
        keys = [k for k in (self.api.get("/sshkeys") or []) if isinstance(k, dict)]
        ids = [str(k["id"]) for k in keys if key_matches(k.get("key"), pub) and k.get("id")]
        if ids or not register:
            return ids
        if log:
            log("ensure: registering the Mac key as %s (POST /sshkeys)" % KEY_NAME)
        r = self.api.post("/sshkeys", {"name": KEY_NAME, "key": pub})
        try:
            return [new_id(r)]
        except PodctlError:
            return [str(k["id"]) for k in (self.api.get("/sshkeys") or []) if key_matches(k.get("key"), pub) and k.get("id")]

    def script_id(self, register=True, log=None):
        """The id of the registered comfy-base-startup script; registers hosts/verda/startup.sh (once) when absent."""
        scripts = [s for s in (self.api.get("/scripts") or []) if isinstance(s, dict)]
        text = startup_script_text()
        for s in scripts:
            if s.get("name") == STARTUP_SCRIPT_NAME and s.get("id"):
                if log and (s.get("script") or "").strip() != text.strip():
                    log("ensure: the registered %s script differs from hosts/verda/startup.sh (a redeploy uses the registered one; "
                        "delete it in the console to re-register)" % STARTUP_SCRIPT_NAME)
                return str(s["id"])
        if not register:
            return None
        if log:
            log("ensure: registering hosts/verda/startup.sh as %s (POST /scripts)" % STARTUP_SCRIPT_NAME)
        r = self.api.post("/scripts", {"name": STARTUP_SCRIPT_NAME, "script": text})
        try:
            return new_id(r)
        except PodctlError:
            return next((str(s["id"]) for s in (self.api.get("/scripts") or []) if s.get("name") == STARTUP_SCRIPT_NAME), None)

    # -- the interface: listing and facts
    def pods(self):
        out = [i for i in (self.api.get("/instances") or []) if isinstance(i, dict)]
        out += [self._volume_record(v) for v in self._detached()]
        return out

    def pod(self, ident):
        inst = self._instance(ident)
        if inst is not None:
            return inst
        vol = self._volume(ident)
        if vol is not None:
            return self._volume_record(vol)
        raise PodctlError("%s: no such Verda instance or volume (podctl pods lists both)" % ident)

    def facts(self, pod):
        if is_volume(pod):
            return {"id": pod.get("id"), "name": pod.get("name") or "", "status": "stopped" if "detached" in (pod.get("status") or "") else "transitional",
                    "host": "", "port": 0, "user": self.ssh_user, "image": pod.get("id") if pod.get("is_os_volume") else "",
                    "gpu": "", "volumes": [pod.get("id")], "kind": "volume", "location": pod.get("location") or "",
                    "instance_type": pod.get("instance_type") or ""}
        ip = inst_ip(pod)
        vols = ([pod["os_volume_id"]] if pod.get("os_volume_id") else []) + [str(v) for v in (pod.get("volume_ids") or [])]
        return {"id": pod.get("id"), "name": pod.get("hostname") or "", "status": norm_status(pod.get("status")), "host": ip,
                "port": SSH_PORT if ip else 0, "user": self.ssh_user, "image": pod.get("image") or "", "gpu": pod.get("gpu") or "",
                "volumes": vols, "kind": "instance", "location": pod.get("location") or "", "instance_type": pod.get("instance_type") or ""}

    def address(self, pod):
        f = self.facts(pod)
        return Address(f["host"], f["port"], self.ssh_user) if f["status"] == "running" and f["host"] else None

    def status_text(self, pod):
        if is_volume(pod):
            kind = "OS volume" if pod.get("is_os_volume") else "data volume"
            lines = ["volume     %s (%s)" % (pod.get("id"), pod.get("name") or ""),
                     "kind       %s, %s" % (kind, pod.get("status")),
                     "size       %s GB %s" % (pod.get("size", "?"), pod.get("type") or ""),
                     "location   %s" % (pod.get("location") or "?")]
            if pod.get("is_os_volume"):
                lines.append("next       podctl start %s (redeploys from this OS volume; the data volumes sharing its name come along)" % pod.get("id"))
            else:
                lines.append("next       attached automatically when its OS volume is started (name prefix), or by the console")
            return "\n".join(lines)
        ip = inst_ip(pod)
        price = pod.get("price_per_hour")
        lines = ["instance   %s (%s)" % (pod.get("id"), pod.get("hostname") or ""),
                 "status     %s (%s)" % (pod.get("status"), norm_status(pod.get("status"))),
                 "type       %s%s%s" % (pod.get("instance_type") or "?", (" / %s" % pod.get("gpu")) if pod.get("gpu") else "",
                                         (" %s GB" % pod.get("gpu_memory")) if pod.get("gpu_memory") else ""),
                 "price      %s/h%s" % (price if price is not None else "?", " (spot)" if pod.get("is_spot") else ""),
                 "location   %s" % (pod.get("location") or "?"),
                 "image      %s" % (pod.get("image") or "?"),
                 "public ip  %s" % (ip or "(none yet)"),
                 "ssh        %s" % (("root@%s:%d" % (ip, SSH_PORT)) if ip else "(no public ip yet)"),
                 "os volume  %s" % (pod.get("os_volume_id") or "?"),
                 "volumes    %s" % (", ".join(str(v) for v in (pod.get("volume_ids") or [])) or "NONE (startup.sh has no data disk to mount)"),
                 "script     %s" % (pod.get("startup_script_id") or "(none: the first boot ran no startup script; podctl ensure installs it over ssh)"),
                 "created    %s" % (pod.get("created_at") or "?"),
                 "firewall   none on this host: every bound port is public; the base binds loopback and is reached through podctl tunnel"]
        return "\n".join(lines)

    def pods_lines(self):
        out = []
        for p in self.pods():
            if is_volume(p):
                out.append("%-36s %-18s %-22s %-12s %-15s %s" % (p.get("id"), (p.get("name") or "")[:18],
                                                                  "%s volume, %s GB" % ("OS" if p.get("is_os_volume") else "data", p.get("size", "?")),
                                                                  "detached", "-", p.get("location") or ""))
            else:
                out.append("%-36s %-18s %-22s %-12s %-15s %s" % (p.get("id"), (p.get("hostname") or "")[:18], (p.get("instance_type") or "")[:22],
                                                                  p.get("status"), inst_ip(p) or "-", p.get("location") or ""))
        return out

    # -- the interface: lifecycle
    def stop(self, pod_id, wait=True):
        """Delete the instance and KEEP every volume (`volume_ids: []`): a shut-down Verda instance still bills the GPU.
        The OS volume is what `start` redeploys from; the data volumes come along by name."""
        inst = self._instance(pod_id)
        if inst is None:
            vol = self._volume(pod_id)
            if vol is not None:
                return "%s is a %s volume, already detached: nothing to stop" % (pod_id, "OS" if vol.get("is_os_volume") else "data")
            raise PodctlError("%s: no such Verda instance" % pod_id)
        os_vol = inst.get("os_volume_id") or "?"
        data = [str(v) for v in (inst.get("volume_ids") or [])]
        self.api.put("/instances", {"id": pod_id, "action": "delete", "volume_ids": []})
        if wait:
            def gone():
                cur = self._instance(pod_id)
                return cur is None or (cur.get("status") or "").lower() in ("deleting", "notfound")
            self._wait(gone, "%s to be deleted" % pod_id, tries=60, every=5)
        return ("stopped %s: instance deleted, volumes kept: OS volume %s%s. `podctl start %s` redeploys from the OS volume; "
                "the ip will be new" % (pod_id, os_vol, (", data volumes " + ", ".join(data)) if data else " (no data volumes)", os_vol))

    def start(self, pod_id, wait=True, ssh_config=None):
        """`pod_id` is an instance (offline -> action start) or a detached OS volume (-> a new instance booted from it,
        with the detached data volumes that share its name attached). With wait: running + ip, an ssh banner, the
        Host block rewritten (the public ip changes on every start)."""
        inst = self._instance(pod_id)
        attached = []
        if inst is not None:
            st = (inst.get("status") or "").lower()
            if st == "offline":
                self.api.put("/instances", {"id": pod_id, "action": "start"})
            elif st == "running":
                pass
            elif norm_status(st) == "transitional":
                pass
            else:
                raise PodctlError("%s is %s: nothing to start" % (pod_id, st))
            new_id_ = pod_id
        else:
            vol = self._volume(pod_id)
            if vol is None:
                raise PodctlError("%s: no such Verda instance or volume (podctl pods lists the detached OS volumes a stopped machine leaves)" % pod_id)
            if not vol.get("is_os_volume"):
                raise PodctlError("%s is a data volume (%s), not an OS volume: start the OS volume and this one comes along by name" % (pod_id, vol.get("name")))
            if (vol.get("status") or "").lower() != "detached":
                raise PodctlError("%s is %s (instance %s): start that instance instead" % (pod_id, vol.get("status"), vol.get("instance_id")))
            location = vol.get("location") or ""
            itype = (self.env.get("VERDA_INSTANCE_TYPE") or "").strip() or vol.get("instance_type") or ""
            if not itype:
                raise PodctlError("which instance type? export VERDA_INSTANCE_TYPE=<type> (GET /instance-types names them; hosts/verda/README.md)")
            for dv in self._detached():
                if dv.get("id") != vol.get("id") and not dv.get("is_os_volume") and (dv.get("location") or "") == location \
                        and stems_match(dv.get("name"), vol.get("name")):
                    attached.append(str(dv["id"]))
            body = {"hostname": hostname_for(volume_stem(vol.get("name")) or vol.get("name")), "image": str(vol["id"]), "instance_type": itype,
                    "location_code": location, "ssh_key_ids": self.key_ids(register=True), "existing_volumes": attached,
                    "description": "ComfyUI Base, redeployed from OS volume %s by podctl" % vol["id"]}
            sid = self.script_id(register=False)
            if sid:
                body["startup_script_id"] = sid
            new_id_ = new_id(self.api.post("/instances", body))
        if not wait:
            if attached:
                return "started %s from OS volume %s with data volumes %s attached (no --wait: podctl ssh-config %s once it runs)" % (
                    new_id_, pod_id, ", ".join(attached), new_id_)
            return "started %s" % new_id_
        inst, host = self._up_and_configured(new_id_, ssh_config)
        extra = (" from OS volume %s, data volumes %s" % (pod_id, ", ".join(attached) or "none")) if new_id_ != pod_id else ""
        return "started %s%s: ssh answers on %s:%d (Host %s updated)" % (new_id_, extra, host, SSH_PORT, _c().SSH_ALIAS)

    def restart(self, pod_id, wait=True, ssh_config=None):
        """`shutdown`, wait offline, `start`, wait running + ip + banner, rewrite the Host block. Never `ssh reboot`:
        Verda's confidential-computing instances forbid an in-guest reboot, and the API path is the same for every
        type. The ip may change, so the block is always rewritten."""
        inst = self._instance(pod_id)
        if inst is None:
            raise PodctlError("%s: no such Verda instance (a detached OS volume is started, not restarted)" % pod_id)
        if (inst.get("status") or "").lower() != "offline":
            self.api.put("/instances", {"id": pod_id, "action": "shutdown"})

            def off():
                cur = self._instance(pod_id)
                if cur is None:
                    raise PodctlError("%s vanished during the shutdown" % pod_id)
                return (cur.get("status") or "").lower() == "offline"
            self._wait(off, "%s to be offline" % pod_id, tries=60, every=5)
        self.api.put("/instances", {"id": pod_id, "action": "start"})
        if not wait:
            return "restarted %s (no --wait: podctl ssh-config %s once it runs; the ip may be new)" % (pod_id, pod_id)
        inst, host = self._up_and_configured(pod_id, ssh_config)
        return "restarted %s: ssh answers on %s:%d (Host %s updated)" % (pod_id, host, SSH_PORT, _c().SSH_ALIAS)

    def ensure(self, pod_id, pubkey, ssh_config, log=print, **kw):
        """Make the machine reachable and hand its boot to the base. Idempotent:
        (1) the Mac key is registered; (2) hosts/verda/startup.sh is registered as comfy-base-startup so the console's
        next fresh deploy can pick it; (3) running + ip + banner; (4) the Host block; (5) startup.sh --ensure over ssh,
        which mounts the data volume at /workspace and installs the comfy-base-boot unit on the RUNNING machine
        (a startup script runs only on the first boot of a fresh image, so this is what makes an instance deployed
        without it, or redeployed from an OS volume, equal to one that had it); (6) `ssh <alias> hostname`."""
        log = log or (lambda s: None)
        rec = self.pod(pod_id)
        if is_volume(rec):
            raise PodctlError("%s is a detached %s volume: podctl start %s first" % (pod_id, "OS" if rec.get("is_os_volume") else "data", pod_id))
        if norm_status(rec.get("status")) == "stopped":
            raise PodctlError("%s is offline: podctl start %s first" % (pod_id, pod_id))
        ids = self.key_ids(pubkey=pubkey, register=True, log=log)
        log("ensure: Mac key registered as %s" % ", ".join(ids))
        sid = self.script_id(register=True, log=log)
        log("ensure: startup script %s registered as %s" % (STARTUP_SCRIPT_NAME, sid))
        inst, host = self._up_and_configured(pod_id, ssh_config)
        log("ensure: Host %s -> %s:%d" % (_c().SSH_ALIAS, host, SSH_PORT))
        if not inst.get("ssh_key_ids") or not any(str(k) in ids for k in inst.get("ssh_key_ids") or []):
            log("ensure: note: the instance was deployed without the Mac key (ssh_key_ids %s); if `ssh %s` is refused, add the key in the console"
                % (inst.get("ssh_key_ids"), _c().SSH_ALIAS))
        r = _c().ssh_run(remote_ensure_cmd(startup_script_text()), timeout=900)
        if r.returncode != 0:
            raise PodctlError("startup.sh --ensure failed on %s (rc %s): %s" % (pod_id, r.returncode, ((r.stderr or "") + (r.stdout or "")).strip()[-400:]))
        out_lines = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()      # startup.sh logs on stderr
        ready = next((ln for ln in reversed(out_lines) if "READY" in ln), out_lines[-1] if out_lines else "(startup.sh printed nothing)")
        log("ensure: %s" % ready)
        hostname = _c()._ssh_hostname()
        return "reachable: %s:%d (hostname %s)" % (host, SSH_PORT, hostname)

    def deploy_like(self, like, name=None, gpu=None, wait=True, ssh_config=None, say=print):
        """A NEW instance from the source's image, on the source's data volumes (moved: a Verda volume attaches to ONE
        instance, so the source must be offline and its data volumes are detached first). The clone boots a fresh OS
        disk, so the registered startup script runs and mounts /workspace. Returns the new id."""
        src = self._instance(like)
        if src is None:
            raise PodctlError("%s: no such Verda instance (a stopped machine is an OS volume: podctl start <volume id> instead)" % like)
        st = (src.get("status") or "").lower()
        if st != "offline":
            raise PodctlError("%s is %s: a Verda volume attaches to ONE instance at a time, so the source must be shut down (offline) before "
                              "its data volumes can move to the clone. Shut it down in the console (or `PUT /instances action shutdown`) "
                              "and deploy again; podctl stop would delete it instead" % (like, st))
        data = [str(v) for v in (src.get("volume_ids") or [])]
        for vid in data:
            self.api.put("/volumes", {"id": vid, "action": "detach", "instance_id": like})
        itype = gpu or src.get("instance_type")
        if not itype:
            raise PodctlError("%s: the API did not name the instance type; pass --gpu <instance type>" % like)
        if not src.get("image"):
            raise PodctlError("%s: the API did not name the image; deploy from the console" % like)
        body = {"hostname": hostname_for(name or "%s-2" % (src.get("hostname") or "comfy-base")), "image": src["image"], "instance_type": itype,
                "location_code": src.get("location") or "", "ssh_key_ids": self.key_ids(register=True), "existing_volumes": data,
                "description": "ComfyUI Base, cloned from %s by podctl" % like, "is_spot": bool(src.get("is_spot"))}
        sid = self.script_id(register=True)
        if sid:
            body["startup_script_id"] = sid
        pod_id = new_id(self.api.post("/instances", body))
        say("deployed %s (%s): %s on %s in %s, data volumes %s (detached from %s), startup script %s" % (
            pod_id, body["hostname"], body["image"], itype, body["location_code"], ", ".join(data) or "none", like, sid or "none"))
        if not wait:
            return pod_id
        inst, host = self._up_and_configured(pod_id, ssh_config)
        say("  ssh answers on %s:%d (Host %s -> %s)" % (host, SSH_PORT, _c().SSH_ALIAS, pod_id))
        return pod_id

    def record_volume(self, pod, env=None):
        """Nothing: Verda's data volume is a real block device, so `df` on /workspace is honest and the base's disk gate
        needs no recorded quota."""
        return None
