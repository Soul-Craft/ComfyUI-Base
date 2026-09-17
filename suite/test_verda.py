"""unit tier: the Verda host provider (hosts/verda/provider.py) against a fake Verda REST server, a fake ssh banner
and a fake ssh_run. Nothing here touches the network, a real machine, the user's ssh config or ~/.verda: every
provider is built on an Api pointed at 127.0.0.1 with a token already in hand, so no oauth call ever happens
unless a test points one at the fake's own /oauth2/token."""
import importlib.util
import json
import pathlib
import re
import subprocess
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.unit
BASE = pathlib.Path(__file__).resolve().parents[1]
TOOL = BASE / "_build" / "pod" / "podctl.py"
STARTUP = BASE / "hosts" / "verda" / "startup.sh"
README = BASE / "hosts" / "verda" / "README.md"

MAC_KEY = "ssh-ed25519 AAAAC3fake mac@key"
INST_ID = "11111111-2222-4333-8444-555555555555"
OS_VOL = "aaaaaaaa-0000-4000-8000-00000000000a"
DATA_VOL = "bbbbbbbb-0000-4000-8000-00000000000b"
OTHER_OS = "cccccccc-0000-4000-8000-00000000000c"
OTHER_DATA = "dddddddd-0000-4000-8000-00000000000d"
ITYPE = "1RTXPRO6000.10V"
IMAGE = "ubuntu-24.04-cuda-13.0-open-docker"


def _load():
    if not TOOL.exists():
        pytest.skip("no base/comfyui-base/_build/pod/podctl.py (not a repo checkout)")
    spec = importlib.util.spec_from_file_location("podctl", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _verda(podctl):
    return podctl._hosts.load_provider("verda", podctl.BASE_DIR)


def _instance(**over):
    # the fields api.verda.com/v1/docs lists for GET /instances/{id}
    i = {"id": INST_ID, "hostname": "comfy-base", "image": IMAGE, "instance_type": ITYPE, "ip": "127.0.0.1", "private_ip": "10.0.0.5",
         "status": "running", "os_volume_id": OS_VOL, "volume_ids": [DATA_VOL], "ssh_key_ids": ["key-1"], "startup_script_id": None,
         "location": "FIN-01", "gpu": "RTX PRO 6000", "gpu_memory": 96, "price_per_hour": 1.9, "is_spot": False,
         "created_at": "2026-09-16T10:00:00Z"}
    i.update(over)
    return i


def _volume(vid, name, is_os, status="attached", instance_id=INST_ID, location="FIN-01", size=500):
    return {"id": vid, "name": name, "size": size, "type": "NVMe", "status": status, "instance_id": instance_id,
            "is_os_volume": is_os, "location": location}


class FakeVerda:
    """An in-memory Verda REST v1: /oauth2/token, /instances (GET, POST, PUT actions), /volumes (GET, POST, PUT
    attach|detach), /sshkeys, /scripts. Records every request as (method, path, body) plus its auth and UA headers.
    A new or started instance is `provisioning` with no ip for `provision_polls` GETs, then `running` with one."""

    def __init__(self, instances=(), volumes=(), keys=(), scripts=(), provision_polls=2, token="tok-fake"):
        self.instances = {i["id"]: dict(i) for i in instances}
        self.volumes = {v["id"]: dict(v) for v in volumes}
        self.keys = [dict(k) for k in keys]
        self.scripts = [dict(s) for s in scripts]
        self.requests = []
        self.headers = []
        self.token_requests = []
        self.provision_polls = provision_polls
        self.polls = {}
        self.counter = 0
        self.token = token
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                n = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(n) if n else b""
                return json.loads(raw) if raw else None

            def _record(self, method, body):
                srv.requests.append((method, self.path, body))
                srv.headers.append({"auth": self.headers.get("Authorization", ""), "ua": self.headers.get("User-Agent", "")})

            def _authed(self):
                if self.headers.get("Authorization", "") != "Bearer " + srv.token:
                    self._send(401, {"code": "unauthorized"})
                    return False
                return True

            def do_GET(self):
                self._record("GET", None)
                if not self._authed():
                    return
                p = self.path
                if p == "/instances":
                    return self._send(200, list(srv.instances.values()))
                if p.startswith("/instances/"):
                    iid = p.split("/")[2]
                    inst = srv.instances.get(iid)
                    if inst is None:
                        return self._send(404, {"code": "not_found", "message": "instance not found"})
                    srv.polls[iid] = srv.polls.get(iid, 0) + 1
                    if inst["status"] == "provisioning" and srv.polls[iid] >= srv.provision_polls:
                        inst["status"], inst["ip"] = "running", "127.0.0.1"
                    return self._send(200, inst)
                if p == "/volumes":
                    return self._send(200, list(srv.volumes.values()))
                if p.startswith("/volumes/"):
                    vol = srv.volumes.get(p.split("/")[2])
                    return self._send(200, vol) if vol else self._send(404, {"code": "not_found"})
                if p == "/sshkeys":
                    return self._send(200, srv.keys)
                if p == "/scripts":
                    return self._send(200, srv.scripts)
                if p == "/instance-types":
                    return self._send(200, [{"instance_type": ITYPE}])
                self._send(404, {"code": "not_found"})

            def do_POST(self):
                body = self._body()
                self._record("POST", body)
                if self.path == "/oauth2/token":
                    srv.token_requests.append(body)
                    return self._send(200, {"access_token": srv.token, "expires_in": 3600})
                if not self._authed():
                    return
                srv.counter += 1
                if self.path == "/instances":
                    iid = "inst-new-%d" % srv.counter
                    vol = srv.volumes.get(body.get("image"))
                    os_id = vol["id"] if vol else "os-new-%d" % srv.counter
                    if vol:
                        vol["status"], vol["instance_id"] = "attached", iid
                    else:
                        srv.volumes[os_id] = _volume(os_id, "OS-" + body["hostname"], True, instance_id=iid, location=body.get("location_code"), size=100)
                    for did in body.get("existing_volumes") or []:
                        srv.volumes[did]["status"], srv.volumes[did]["instance_id"] = "attached", iid
                    srv.instances[iid] = _instance(id=iid, hostname=body["hostname"], image=body["image"], instance_type=body["instance_type"],
                                                   ip=None, status="provisioning", os_volume_id=os_id, volume_ids=list(body.get("existing_volumes") or []),
                                                   ssh_key_ids=list(body.get("ssh_key_ids") or []), startup_script_id=body.get("startup_script_id"),
                                                   location=body.get("location_code"), is_spot=bool(body.get("is_spot")))
                    srv.polls[iid] = 0
                    return self._send(202, iid)                      # the id as a JSON string
                if self.path == "/sshkeys":
                    kid = "key-%d" % srv.counter
                    srv.keys.append({"id": kid, "name": body["name"], "key": body["key"], "fingerprint": "SHA256:fake"})
                    return self._send(201, {"id": kid})               # the id in an object
                if self.path == "/scripts":
                    sid = "script-%d" % srv.counter
                    srv.scripts.append({"id": sid, "name": body["name"], "script": body["script"]})
                    return self._send(201, sid)
                if self.path == "/volumes":
                    vid = "vol-new-%d" % srv.counter
                    srv.volumes[vid] = _volume(vid, body["name"], False, status="detached", instance_id=None, location=body.get("location_code"), size=body.get("size"))
                    return self._send(202, vid)
                self._send(404, {"code": "not_found"})

            def do_PUT(self):
                body = self._body()
                self._record("PUT", body)
                if not self._authed():
                    return
                if self.path == "/instances":
                    inst = srv.instances.get(body.get("id"))
                    if inst is None:
                        return self._send(404, {"code": "not_found"})
                    action = body.get("action")
                    if action in ("shutdown", "force_shutdown"):
                        inst["status"], inst["ip"] = "offline", None
                    elif action == "start":
                        inst["status"], inst["ip"] = "provisioning", None
                        srv.polls[inst["id"]] = 0
                    elif action == "delete":
                        keep_not = set(body.get("volume_ids") or [])
                        for vid, vol in list(srv.volumes.items()):
                            if vol.get("instance_id") == inst["id"]:
                                if vid in keep_not:
                                    del srv.volumes[vid]
                                else:
                                    vol["status"], vol["instance_id"] = "detached", None
                        del srv.instances[inst["id"]]
                    else:
                        return self._send(400, {"code": "bad_action"})
                    return self._send(202, {})
                if self.path == "/volumes":
                    vol = srv.volumes.get(body.get("id"))
                    if vol is None:
                        return self._send(404, {"code": "not_found"})
                    if body.get("action") == "detach":
                        vol["status"], vol["instance_id"] = "detached", None
                        inst = srv.instances.get(body.get("instance_id") or "")
                        if inst and vol["id"] in inst.get("volume_ids", []):
                            inst["volume_ids"].remove(vol["id"])
                    elif body.get("action") == "attach":
                        vol["status"], vol["instance_id"] = "attached", body.get("instance_id")
                    return self._send(202, {})
                self._send(404, {"code": "not_found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def calls(self, method=None, path=None):
        return [(m, p, b) for m, p, b in self.requests if (method is None or m == method) and (path is None or p == path)]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _prov(podctl, fake, tmp_path, env=None):
    """A provider on the fake with the token in hand, a key pair under tmp_path, no waiting, and the driver bound to it."""
    mod = _verda(podctl)
    prov = mod.VerdaProvider(api=mod.Api(fake.url, token=fake.token), env=env if env is not None else {})
    prov.key_path = tmp_path / "verda_comfyui"
    (tmp_path / "verda_comfyui.pub").write_text(MAC_KEY + "\n", encoding="utf-8")
    prov.sleep = lambda s: None
    podctl.set_provider(prov, env={})                                   # env={}: a PODCTL_HOST in the real environment must not leak in
    return prov


def _cfg(tmp_path):
    cfg = tmp_path / "ssh_config"
    cfg.write_text("Host other\n  HostName 1.2.3.4\n  User me\n", encoding="utf-8")
    return cfg


# ---------------------------------------------------------------- the token and the credentials

def test_unit_verda_token_exchange_sends_the_secret_in_the_body_only_and_reuses_the_token():
    podctl = _load()
    mod = _verda(podctl)
    fake = FakeVerda(instances=[_instance()])
    try:
        api = mod.Api(fake.url, credentials=("cid-123", "sec-XYZ"))
        assert api.get("/instances")[0]["id"] == INST_ID
        assert fake.requests[0][:2] == ("POST", "/oauth2/token")
        assert fake.requests[0][2] == {"grant_type": "client_credentials", "client_id": "cid-123", "client_secret": "sec-XYZ"}
        assert fake.headers[0]["auth"] == ""                              # nothing to bear yet
        assert fake.headers[1]["auth"] == "Bearer tok-fake"
        for (_, path, _), h in zip(fake.requests, fake.headers):
            assert "sec-XYZ" not in path and "sec-XYZ" not in h["auth"] and "sec-XYZ" not in h["ua"]
        assert fake.headers[1]["ua"].startswith("podctl/") and "Verda" in fake.headers[1]["ua"]
        api.get("/instances")
        assert len(fake.token_requests) == 1                              # the token is kept, not re-requested
        # an explicit token skips the exchange entirely
        n = len(fake.requests)
        mod.Api(fake.url, token="tok-fake").get("/volumes")
        assert [p for _, p, _ in fake.requests[n:]] == ["/volumes"]
        # a 404 is an HttpError (a PodctlError) that carries its status
        with pytest.raises(podctl.PodctlError) as ei:
            api.get("/instances/nope")
        assert getattr(ei.value, "status", None) == 404 and "sec-XYZ" not in str(ei.value)
    finally:
        fake.close()


def test_unit_verda_credentials_come_from_env_or_the_file_and_a_missing_pair_names_both(tmp_path):
    podctl = _load()
    mod = _verda(podctl)
    f = tmp_path / "credentials"
    f.write_text('# verda\n[default]\nclient_id = "abc"\nclient_secret = \'s3cr3t\'\n', encoding="utf-8")
    assert mod.load_credentials(env={}, path=f) == ("abc", "s3cr3t")
    f.write_text("client_id=abc2\nCLIENT_SECRET = s3cr3t2   \n", encoding="utf-8")
    assert mod.load_credentials(env={}, path=f) == ("abc2", "s3cr3t2")
    assert mod.load_credentials(env={"VERDA_CLIENT_ID": "e", "VERDA_CLIENT_SECRET": "f"}, path=f) == ("e", "f")
    for env in ({}, {"VERDA_CLIENT_ID": "only-the-id"}):
        with pytest.raises(podctl.PodctlError) as ei:
            mod.load_credentials(env=env, path=tmp_path / "absent")
        assert "VERDA_CLIENT_ID" in str(ei.value) and "VERDA_CLIENT_SECRET" in str(ei.value)
    # the Api is lazy: constructing one reads nothing; the first call is where a missing pair surfaces
    api = mod.Api("http://127.0.0.1:1", env={}, credentials_path=tmp_path / "absent")
    with pytest.raises(podctl.PodctlError) as ei:
        api.get("/instances")
    assert "VERDA_CLIENT_SECRET" in str(ei.value)


# ---------------------------------------------------------------- listing and facts

def test_unit_verda_pods_lists_instances_and_detached_volumes(tmp_path):
    podctl = _load()
    fake = FakeVerda(instances=[_instance()],
                     volumes=[_volume(OS_VOL, "comfy-base-os", True), _volume(DATA_VOL, "comfy-base-data", False),
                              _volume(OTHER_OS, "old-box-os", True, status="detached", instance_id=None)])
    try:
        prov = _prov(podctl, fake, tmp_path)
        pods = prov.pods()
        assert [p["id"] for p in pods] == [INST_ID, OTHER_OS]                 # attached volumes are the instance's, not a row
        assert pods[1]["kind"] == "volume" and pods[1]["status"] == "detached-volume" and pods[1]["name"] == "old-box-os"
        lines = prov.pods_lines()
        assert len(lines) == 2 and INST_ID in lines[0] and "running" in lines[0] and "127.0.0.1" in lines[0]
        assert OTHER_OS in lines[1] and "detached" in lines[1] and "OS volume" in lines[1]
        assert prov.pod(INST_ID)["hostname"] == "comfy-base"
        assert prov.pod(OTHER_OS)["kind"] == "volume"
        with pytest.raises(podctl.PodctlError):
            prov.pod("nope")
        assert "OS volume" in prov.status_text(prov.pod(OTHER_OS)) and "podctl start %s" % OTHER_OS in prov.status_text(prov.pod(OTHER_OS))
    finally:
        fake.close()


def test_unit_verda_facts_normalise_the_status_vocabulary_and_address_waits_for_running_with_an_ip():
    podctl = _load()
    mod = _verda(podctl)
    prov = mod.VerdaProvider(api=mod.Api("http://127.0.0.1:1", token="t"))          # never called
    for raw, want in [("running", "running"), ("offline", "stopped"), ("notfound", "gone"), ("deleting", "gone"),
                      ("provisioning", "transitional"), ("ordered", "transitional"), ("new", "transitional"), ("validating", "transitional"),
                      ("no_capacity", "transitional"), ("installation_failed", "transitional"), ("error", "transitional"),
                      ("discontinued", "transitional"), ("unknown", "transitional"), (None, "transitional")]:
        assert prov.facts(_instance(status=raw))["status"] == want, raw
    f = prov.facts(_instance())
    assert (f["host"], f["port"], f["user"], f["image"], f["gpu"]) == ("127.0.0.1", 22, "root", IMAGE, "RTX PRO 6000")
    assert f["volumes"] == [OS_VOL, DATA_VOL] and f["name"] == "comfy-base"
    assert prov.address(_instance(status="provisioning", ip=None)) is None
    assert prov.address(_instance(status="running", ip=None)) is None
    assert prov.address(_instance(status="offline", ip="127.0.0.1")) is None      # a stale ip on an offline machine
    a = prov.address(_instance())
    assert (a.host, a.port, a.user) == ("127.0.0.1", 22, "root")
    text = prov.status_text(_instance())
    for needle in ("ssh        root@127.0.0.1:22", OS_VOL, DATA_VOL, "FIN-01", ITYPE, "1.9", IMAGE, "firewall"):
        assert needle in text, needle
    rec = dict(_volume(OS_VOL, "comfy-base-os", True, status="detached", instance_id=None), kind="volume", status="detached-volume")
    vf = prov.facts(rec)
    assert vf["status"] == "stopped" and vf["kind"] == "volume" and vf["volumes"] == [OS_VOL] and prov.address(rec) is None


# ---------------------------------------------------------------- stop, start, restart

def test_unit_verda_stop_deletes_the_instance_keeping_every_volume_and_names_them(tmp_path):
    podctl = _load()
    fake = FakeVerda(instances=[_instance()], volumes=[_volume(OS_VOL, "comfy-base-os", True), _volume(DATA_VOL, "comfy-base-data", False)])
    try:
        prov = _prov(podctl, fake, tmp_path)
        out = prov.stop(INST_ID, wait=True)
        assert fake.calls("PUT") == [("PUT", "/instances", {"id": INST_ID, "action": "delete", "volume_ids": []})]
        assert INST_ID not in fake.instances
        assert fake.volumes[OS_VOL]["status"] == "detached" and fake.volumes[DATA_VOL]["status"] == "detached"
        assert OS_VOL in out and DATA_VOL in out and "kept" in out and "podctl start %s" % OS_VOL in out
        assert [p["id"] for p in prov.pods()] == [OS_VOL, DATA_VOL]           # the stopped machine stays visible
        assert "nothing to stop" in prov.stop(OS_VOL)                         # a volume is already the stopped form
        with pytest.raises(podctl.PodctlError):
            prov.stop("nope")
    finally:
        fake.close()


def test_unit_verda_start_from_a_detached_os_volume_redeploys_with_its_data_volume_and_the_mac_key(tmp_path, monkeypatch):
    podctl = _load()
    fake = FakeVerda(volumes=[_volume(OS_VOL, "comfy-base-os", True, status="detached", instance_id=None),
                              _volume(DATA_VOL, "comfy-base-data", False, status="detached", instance_id=None),
                              _volume(OTHER_DATA, "other-box-data", False, status="detached", instance_id=None),
                              _volume("eeee", "comfy-base-data", False, status="detached", instance_id=None, location="ICE-01")],
                     keys=[{"id": "key-9", "name": "laptop", "key": "ssh-ed25519 AAAAOTHER other@key", "fingerprint": "x"}])
    cfg = _cfg(tmp_path)
    banners = iter([False, True])
    monkeypatch.setattr(podctl, "ssh_banner", lambda h, p, timeout=10: next(banners))
    try:
        prov = _prov(podctl, fake, tmp_path, env={"VERDA_INSTANCE_TYPE": ITYPE})
        out = prov.start(OS_VOL, wait=True, ssh_config=cfg)
        (_, _, post), = fake.calls("POST", "/instances")
        assert post["image"] == OS_VOL and post["instance_type"] == ITYPE and post["location_code"] == "FIN-01"
        assert post["existing_volumes"] == [DATA_VOL]                          # same name prefix, same location; not the others
        assert post["hostname"] == "comfy-base" and "startup_script_id" not in post   # none registered yet: nothing to pass
        keyposts = fake.calls("POST", "/sshkeys")
        assert len(keyposts) == 1 and keyposts[0][2]["key"] == MAC_KEY and post["ssh_key_ids"] == [fake.keys[-1]["id"]]
        new_id = next(i for i in fake.instances if i.startswith("inst-new-"))
        assert fake.instances[new_id]["status"] == "running" and fake.polls[new_id] >= 2   # it waited through provisioning
        assert fake.volumes[DATA_VOL]["instance_id"] == new_id
        text = cfg.read_text(encoding="utf-8")
        assert "Host verda\n  HostName 127.0.0.1\n  Port 22\n  User root\n  IdentityFile %s\n" % prov.key_path in text
        assert "Host other\n  HostName 1.2.3.4\n  User me\n" in text            # the other block is untouched
        assert new_id in out and "127.0.0.1:22" in out and OS_VOL in out and DATA_VOL in out
        # a data volume is not startable; an unknown id is not either
        with pytest.raises(podctl.PodctlError) as ei:
            prov.start(OTHER_DATA, wait=False)
        assert "data volume" in str(ei.value)
        with pytest.raises(podctl.PodctlError):
            prov.start("nope", wait=False)
    finally:
        fake.close()


def test_unit_verda_start_needs_an_instance_type_and_starts_an_offline_instance_with_the_api(tmp_path, monkeypatch):
    podctl = _load()
    fake = FakeVerda(instances=[_instance(status="offline", ip=None)],
                     volumes=[_volume(OTHER_OS, "old-box-os", True, status="detached", instance_id=None)])
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(podctl, "ssh_banner", lambda h, p, timeout=10: True)
    try:
        prov = _prov(podctl, fake, tmp_path, env={})
        with pytest.raises(podctl.PodctlError) as ei:
            prov.start(OTHER_OS, wait=False)
        assert "VERDA_INSTANCE_TYPE" in str(ei.value) and not fake.calls("POST")
        out = prov.start(INST_ID, wait=True, ssh_config=cfg)
        assert fake.calls("PUT") == [("PUT", "/instances", {"id": INST_ID, "action": "start"})]
        assert fake.instances[INST_ID]["status"] == "running" and "127.0.0.1:22" in out
        assert "Host verda\n  HostName 127.0.0.1\n  Port 22\n" in cfg.read_text(encoding="utf-8")
    finally:
        fake.close()


def test_unit_verda_restart_is_shutdown_then_start_through_the_api_never_a_reboot_in_the_guest(tmp_path, monkeypatch):
    podctl = _load()
    fake = FakeVerda(instances=[_instance()])
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(podctl, "ssh_banner", lambda h, p, timeout=10: True)

    def no_ssh(*a, **k):
        raise AssertionError("restart must not run anything in the guest")
    monkeypatch.setattr(podctl, "ssh_run", no_ssh)
    monkeypatch.setattr(podctl, "_ssh_hostname", no_ssh)
    try:
        prov = _prov(podctl, fake, tmp_path)
        out = prov.restart(INST_ID, wait=True, ssh_config=cfg)
        assert [b["action"] for _, _, b in fake.calls("PUT", "/instances")] == ["shutdown", "start"]
        assert fake.instances[INST_ID]["status"] == "running" and "127.0.0.1:22" in out
        assert "Host verda\n  HostName 127.0.0.1\n  Port 22\n" in cfg.read_text(encoding="utf-8")
        assert not any("reboot" in json.dumps(b or {}) for _, _, b in fake.requests)
        with pytest.raises(podctl.PodctlError):
            prov.restart(OS_VOL, wait=False)
    finally:
        fake.close()


# ---------------------------------------------------------------- ensure

def test_unit_verda_ensure_registers_the_key_and_the_script_once_writes_the_block_and_runs_the_ensure_pass(tmp_path, monkeypatch):
    podctl = _load()
    fake = FakeVerda(instances=[_instance(), _instance(id="off-1", hostname="asleep", status="offline", ip=None)],
                     volumes=[_volume(OS_VOL, "comfy-base-os", True), _volume(DATA_VOL, "comfy-base-data", False),
                              _volume(OTHER_OS, "old-box-os", True, status="detached", instance_id=None)])
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(podctl, "ssh_banner", lambda h, p, timeout=10: True)
    calls = []

    def fake_ssh_run(cmd, env=None, timeout=None):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="2026-09-16T10:00:00Z comfy-base-startup[ensure]: READY disk=/dev/vdb mounted=yes unit=active boot.sh=absent host.env=written\n")
    monkeypatch.setattr(podctl, "ssh_run", fake_ssh_run)
    monkeypatch.setattr(podctl, "_ssh_hostname", lambda: "comfy-base")
    logs = []
    try:
        prov = _prov(podctl, fake, tmp_path)
        out = prov.ensure(INST_ID, pubkey=MAC_KEY, ssh_config=cfg, log=logs.append)
        assert out == "reachable: 127.0.0.1:22 (hostname comfy-base)"
        posts = fake.calls("POST")
        assert [p for _, p, _ in posts] == ["/sshkeys", "/scripts"]
        assert posts[0][2]["key"] == MAC_KEY
        assert posts[1][2]["name"] == "comfy-base-startup" and posts[1][2]["script"] == STARTUP.read_text(encoding="utf-8")
        assert len(calls) == 1
        cmd = calls[0]
        assert "cat > /root/comfy-base-startup.sh <<'COMFY_BASE_STARTUP_EOF'\n" in cmd and cmd.rstrip().endswith("bash /root/comfy-base-startup.sh --ensure")
        assert "RequiresMountsFor=/workspace" in cmd and "BASE_HOST=verda" in cmd
        assert any("READY disk=/dev/vdb" in l for l in logs)
        assert MAC_KEY.split()[1] not in " ".join(l for l in logs if "registering" in l)  # the key body is never logged
        assert "Host verda\n  HostName 127.0.0.1\n  Port 22\n  User root\n" in cfg.read_text(encoding="utf-8")
        # again: nothing registered twice, the block rewritten, the ensure pass run again (it is idempotent on the machine)
        n = len(fake.requests)
        prov.ensure(INST_ID, pubkey=MAC_KEY, ssh_config=cfg, log=logs.append)
        assert not [1 for m, _, _ in fake.requests[n:] if m == "POST"] and len(calls) == 2
        # a detached volume and an offline instance are not ensurable: say what to do instead
        for ident in (OTHER_OS, "off-1"):
            with pytest.raises(podctl.PodctlError) as ei:
                prov.ensure(ident, pubkey=MAC_KEY, ssh_config=cfg, log=logs.append)
            assert "podctl start %s" % ident in str(ei.value)
        # a failing --ensure pass is the error, with the script's tail
        monkeypatch.setattr(podctl, "ssh_run", lambda cmd, env=None, timeout=None: types.SimpleNamespace(returncode=1, stdout="", stderr="no data disk appeared"))
        with pytest.raises(podctl.PodctlError) as ei:
            prov.ensure(INST_ID, pubkey=MAC_KEY, ssh_config=cfg, log=logs.append)
        assert "startup.sh --ensure failed" in str(ei.value) and "no data disk appeared" in str(ei.value)
    finally:
        fake.close()


# ---------------------------------------------------------------- deploy --like

def test_unit_verda_deploy_like_refuses_a_running_source_and_moves_the_data_volumes_of_an_offline_one(tmp_path, monkeypatch):
    podctl = _load()
    fake = FakeVerda(instances=[_instance()], volumes=[_volume(OS_VOL, "comfy-base-os", True), _volume(DATA_VOL, "comfy-base-data", False)],
                     keys=[{"id": "key-1", "name": "mac", "key": MAC_KEY + "-other-comment", "fingerprint": "x"}],
                     scripts=[{"id": "script-7", "name": "comfy-base-startup", "script": STARTUP.read_text(encoding="utf-8")}])
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(podctl, "ssh_banner", lambda h, p, timeout=10: True)
    said = []
    try:
        prov = _prov(podctl, fake, tmp_path)
        with pytest.raises(podctl.PodctlError) as ei:
            prov.deploy_like(INST_ID, wait=False, say=said.append)
        assert "offline" in str(ei.value) and not fake.calls("PUT") and not fake.calls("POST")
        fake.instances[INST_ID].update(status="offline", ip=None)
        new_id = prov.deploy_like(INST_ID, name="comfy-base-2", wait=True, ssh_config=cfg, say=said.append)
        seq = [(m, p, b) for m, p, b in fake.requests if m in ("PUT", "POST")]
        assert seq[0] == ("PUT", "/volumes", {"id": DATA_VOL, "action": "detach", "instance_id": INST_ID})
        assert seq[1][:2] == ("POST", "/instances") and len(seq) == 2         # the key and the script were already registered
        body = seq[1][2]
        assert body["existing_volumes"] == [DATA_VOL] and body["image"] == IMAGE and body["instance_type"] == ITYPE
        assert body["location_code"] == "FIN-01" and body["ssh_key_ids"] == ["key-1"] and body["startup_script_id"] == "script-7"
        assert body["hostname"] == "comfy-base-2" and body["is_spot"] is False
        assert new_id in fake.instances and fake.instances[new_id]["status"] == "running"
        assert fake.volumes[DATA_VOL]["instance_id"] == new_id
        assert said[0].startswith("deployed %s" % new_id) and DATA_VOL in said[0]
        assert "Host verda\n  HostName 127.0.0.1\n  Port 22\n" in cfg.read_text(encoding="utf-8")
        with pytest.raises(podctl.PodctlError):
            prov.deploy_like("nope", wait=False, say=said.append)
    finally:
        fake.close()


# ---------------------------------------------------------------- the provider's shape

def test_unit_verda_provider_shape_host_env_and_lazy_construction(monkeypatch, tmp_path):
    podctl = _load()
    mod = _verda(podctl)
    prov = mod.VerdaProvider(api=mod.Api("http://127.0.0.1:1", token="t"))
    assert prov.host_env() == "BASE_HOST=verda\nBASE_VOLUME=/workspace\n"
    assert prov.experimental is True and prov.name == "verda" and prov.ssh_user == "root" and prov.ssh_alias == "verda"
    assert prov.volume_root == "/workspace" and str(prov.key_path).endswith("/.ssh/verda_comfyui")
    assert prov.record_volume(_instance()) is None and prov.record_volume(_instance(), env={}) is None
    assert isinstance(prov, podctl.Provider)
    # the driver picks the one Provider subclass in the module
    subclasses = [v for v in vars(mod).values() if isinstance(v, type) and issubclass(v, podctl.Provider) and v is not podctl.Provider]
    assert subclasses == [mod.VerdaProvider]

    def boom(*a, **k):
        raise AssertionError("constructing the provider must open no socket")
    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    p2 = podctl.provider_for("verda", env={"VERDA_CLIENT_ID": "x", "VERDA_CLIENT_SECRET": "y"})
    assert isinstance(p2, mod.VerdaProvider)
    # the id helpers accept every shape the docs allow
    assert mod.new_id("abc") == "abc" and mod.new_id({"id": "d"}) == "d" and mod.new_id(["e"]) == "e" and mod.new_id([{"id": "f"}]) == "f"
    with pytest.raises(podctl.PodctlError):
        mod.new_id({})
    assert mod.volume_stem("comfy-base-os") == mod.volume_stem("OS-comfy-base") == mod.volume_stem("comfy-base-data") == "comfy-base"
    assert mod.stems_match("comfy-base-os", "comfy-base-data") and not mod.stems_match("comfy-base-os", "other-box-data")
    assert mod.key_matches("ssh-ed25519 AAAA x@y", "ssh-ed25519 AAAA other") and not mod.key_matches("ssh-ed25519 BBBB x", "ssh-ed25519 AAAA x")


def test_unit_verda_startup_script_parses_is_ascii_and_installs_the_unit_and_the_host_env():
    text = STARTUP.read_bytes()
    text.decode("ascii")                                                        # pure ASCII, or this raises
    r = subprocess.run(["bash", "-n", str(STARTUP)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    s = text.decode()
    for needle in ("comfy-base-boot", "RequiresMountsFor=/workspace", "BASE_HOST=verda", "BASE_VOLUME=/workspace",
                   "nofail,x-systemd.device-timeout=30", "mkfs.ext4", "comfy-volume", "/var/log/comfy-base-startup.log",
                   "/dev/vd[b-z]", "--ensure", "systemctl daemon-reload", "enable --now", "READY"):
        assert needle in s, needle
    assert not re.search(r"^\s*set -e", s, re.M)                                 # a partial boot still reaches READY
    readme = README.read_text(encoding="utf-8")
    assert "EXPERIMENTAL" in readme and "\u2014" not in readme and "\u2013" not in readme
