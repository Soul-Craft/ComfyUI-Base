"""unit tier, 3.4.0: each model file's sha256, taken from the Hub, never hashed here.

The Hub answers a HEAD on a file's resolve URL with its sha256 (x-linked-etag, for every LFS file), so a package can
carry its models' checksums in `models.sha256` beside its script without anyone downloading or hashing 100+ GB. The
workflow's `models` array then carries hash + hash_type for ComfyUI's Missing Models dialog, and the declarations
dump prints them for tools (a Comfy Build spec) that need them."""
import importlib.util, json, os, pathlib, shutil, stat, subprocess, textwrap, pytest
pytestmark = pytest.mark.unit
BASE = pathlib.Path(__file__).resolve().parents[1]
STUB = BASE / "_build" / "stub"
SHA = "a" * 64


def _need_stub():
    if not STUB.exists():
        pytest.skip("_build/stub is repo tooling, not in the shipped base")


def _pkg(tmp_path):
    _need_stub()
    d = tmp_path / "stub"; shutil.copytree(STUB, d)
    return d


def _hub(tmp_path, sha=SHA):
    """A curl stand-in answering a HEAD like the Hub: a redirect, then the file's size and its sha256 as x-linked-etag."""
    b = tmp_path / "bin"; b.mkdir(exist_ok=True)
    c = b / "curl"
    c.write_text("#!/bin/bash\n" + textwrap.dedent(f"""\
        printf 'HTTP/2 302\\r\\nlocation: https://cas-bridge.xethub.hf.co/x\\r\\n\\r\\n'
        printf 'HTTP/2 200\\r\\nx-linked-size: 4096\\r\\nx-linked-etag: "{sha}"\\r\\n\\r\\n'
        """))
    c.chmod(c.stat().st_mode | stat.S_IEXEC)
    return {"PATH": f"{b}:{os.environ['PATH']}", "COMFY_BASE": str(BASE)}


def _base(*args, env=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith("BASE_")}
    e.update(env or {})
    return subprocess.run(["bash", str(BASE / "base.sh"), *args], capture_output=True, text=True, env=e)


def test_unit_gen_hashes_takes_each_sha256_from_the_hub_and_check_catches_drift(tmp_path):
    pkg = _pkg(tmp_path); env = _hub(tmp_path)
    r = _base("gen-hashes", str(pkg), env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (pkg / "models.sha256").read_text() == "%s  stub_vae.safetensors\n" % SHA      # the LOCAL row has no URL: none
    assert _base("gen-hashes", str(pkg), "--check", env=env).returncode == 0
    r = _base("gen-hashes", str(pkg), "--check", env=_hub(tmp_path, sha="b" * 64))
    assert r.returncode == 1 and "stub_vae.safetensors" in (r.stdout + r.stderr)
    assert (pkg / "models.sha256").read_text().startswith(SHA)                               # --check writes nothing


def test_unit_the_declarations_dump_prints_each_models_sha256(tmp_path):
    pkg = _pkg(tmp_path)
    (pkg / "models.sha256").write_text("%s  stub_vae.safetensors\n" % SHA)
    r = subprocess.run(["bash", str(pkg / "stub-script.sh")], capture_output=True, text=True,
                       env={**{k: v for k, v in os.environ.items() if not k.startswith("BASE_")}, "BASE_DECLARE_ONLY": "1", "COMFY_BASE": str(BASE)})
    assert r.returncode == 0, r.stderr
    assert "MODELHASH stub_vae.safetensors|%s" % SHA in r.stdout, r.stdout
    (pkg / "models.sha256").write_text("not-a-sha  stub_vae.safetensors\n")
    r = subprocess.run(["bash", str(pkg / "stub-script.sh")], capture_output=True, text=True,
                       env={**{k: v for k, v in os.environ.items() if not k.startswith("BASE_")}, "BASE_DECLARE_ONLY": "1", "COMFY_BASE": str(BASE)})
    assert r.returncode != 0 and "models.sha256" in r.stderr


def test_unit_stamp_models_writes_hash_and_hash_type_only_when_the_package_carries_them(tmp_path):
    pkg = _pkg(tmp_path)
    assert _base("stamp-models", str(pkg)).returncode == 0
    doc = json.loads((pkg / "stub-workflow.json").read_text())
    assert all("hash" not in m for m in doc["models"])                                      # no sidecar: exactly as before
    (pkg / "models.sha256").write_text("%s  stub_vae.safetensors\n" % SHA)
    assert _base("stamp-models", str(pkg), "--check").returncode == 1                       # the sidecar makes the stamp stale
    assert _base("stamp-models", str(pkg)).returncode == 0
    vae = [m for m in json.loads((pkg / "stub-workflow.json").read_text())["models"] if m["name"] == "stub_vae.safetensors"]
    assert vae and vae[0]["hash"] == SHA and vae[0]["hash_type"] == "SHA256", vae


def test_unit_the_packager_ships_the_models_checksums(tmp_path):
    pkg = _pkg(tmp_path)
    pkg = pkg.rename(tmp_path / "stub2").parent / "stub2"
    (pkg / "stub-script.sh").rename(pkg / "stub2-script.sh")
    (pkg / "models.sha256").write_text("%s  stub_vae.safetensors\n" % SHA)
    spec = importlib.util.spec_from_file_location("package_py", BASE / "_build" / "package.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    assert ("models.sha256", 0o644) in mod.package_members(pkg)
