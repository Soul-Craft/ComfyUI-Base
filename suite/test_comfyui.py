"""comfyui tier: needs the shared testbed (../testbed beside the base, provisioned by ../testbed.sh) and,
for the live checks, its server on 127.0.0.1:8199. Everything here skips cleanly when neither is present."""
import json, os, pathlib, re, subprocess, urllib.request, pytest
from pathlib import Path
pytestmark = pytest.mark.comfyui
BASE = pathlib.Path(__file__).resolve().parents[1]
NODE_SRC = os.environ.get("BASE_NODE_SRC") or (str(BASE.parent / "testbed") if (BASE.parent / "testbed" / "main.py").exists() else "")
SERVER = os.environ.get("BASE_SERVER") or "127.0.0.1:%s" % os.environ.get("TESTBED_PORT", "8199")


def _server_up():
    try:
        urllib.request.urlopen("http://%s/system_stats" % SERVER, timeout=2); return True
    except Exception:
        return False


def test_comfyui_loader_map_knows_every_core_loader_class_in_the_tree():
    if not NODE_SRC: pytest.skip("no testbed at ../testbed")
    if not (pathlib.Path(NODE_SRC) / "nodes.py").exists(): pytest.skip("%s is not a full ComfyUI tree (a fake pod's): no nodes.py to read" % NODE_SRC)   # 2.0.16
    cats = {ln.split("|")[0] for ln in re.findall(r'"([A-Za-z0-9_ ()]+\|[A-Za-z0-9_]+)"', (BASE / "lib" / "60-sync.sh").read_text())}
    core = (pathlib.Path(NODE_SRC) / "nodes.py").read_text()
    for cls in ("UNETLoader", "CLIPLoader", "DualCLIPLoader", "VAELoader", "LoraLoader", "LoraLoaderModelOnly", "CheckpointLoaderSimple", "ControlNetLoader", "CLIPVisionLoader", "StyleModelLoader"):
        assert ("class %s" % cls) in core, cls
        assert cls in cats, cls


def test_comfyui_smoke_tool_covers_every_type_including_subgraphs_against_the_live_server(tmp_path):
    if not _server_up(): pytest.skip("testbed server not running on " + SERVER)
    wf = tmp_path / "w.json"
    wf.write_text(json.dumps({"nodes": [{"id": 1, "type": "VAELoader", "mode": 0, "widgets_values": ["x"]}, {"id": 2, "type": "NoSuchNodeXYZ", "mode": 0, "widgets_values": []},
                                        {"id": 3, "type": "Label (rgthree)", "mode": 0, "widgets_values": []}],
                              "definitions": {"subgraphs": [{"id": "abc", "nodes": [{"id": 4, "type": "UNETLoader", "mode": 0, "widgets_values": []}]}]}, "links": [], "extra": {}}))
    r = subprocess.run(["python3", str(BASE / "py" / "smoke.py"), str(wf), SERVER], capture_output=True, text=True)
    assert r.returncode != 0 and "NoSuchNodeXYZ" in r.stdout, r.stdout + r.stderr
    wf.write_text(json.dumps({"nodes": [{"id": 1, "type": "VAELoader", "mode": 0, "widgets_values": ["x"]}], "definitions": {"subgraphs": [{"id": "abc", "nodes": [{"id": 4, "type": "UNETLoader", "mode": 0, "widgets_values": []}]}]}, "links": [], "extra": {}}))
    r = subprocess.run(["python3", str(BASE / "py" / "smoke.py"), str(wf), SERVER], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_comfyui_combofix_tool_handles_the_live_object_info_shape(tmp_path):
    if not _server_up(): pytest.skip("testbed server not running on " + SERVER)
    # a stored value the server does offer, plus a renamed one that shares its head with an offered option
    with urllib.request.urlopen("http://%s/object_info/KSampler" % SERVER, timeout=60) as r:
        obj = json.loads(r.read())["KSampler"]
    samplers = obj["input"]["required"]["sampler_name"]
    samplers = samplers[0] if isinstance(samplers[0], list) else samplers[1]["options"]
    wf = tmp_path / "w.json"
    wf.write_text(json.dumps({"nodes": [{"id": 1, "type": "KSampler", "mode": 0, "widgets_values": [1, "fixed", 20, 8.0, samplers[0], "simple", 1.0]}], "links": [], "extra": {}}, indent=2))
    r = subprocess.run(["python3", str(BASE / "py" / "combofix.py"), SERVER, str(wf)], capture_output=True, text=True)
    assert r.returncode == 0 and "COMBOFIX 0 repaired 0 unresolved" in r.stdout, r.stdout + r.stderr
