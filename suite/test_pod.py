"""runpod / gpu tiers: only meaningful on the pod (BASE_ON_POD=1, set by base_test when Linux + a /dev/nvidia[0-9]* node +
custom_nodes). Everything here skips off-pod; nothing here is vacuous when it runs."""
import os, pathlib, re, subprocess, pytest
BASE = pathlib.Path(__file__).resolve().parents[1]
ON_POD = os.environ.get("BASE_ON_POD") == "1"
COMFY = pathlib.Path(os.environ.get("BASE_COMFY") or "/nonexistent")
STATE = pathlib.Path(os.environ.get("BASE_STATE") or "/workspace/comfy-base/state")


def _booted_through_base(state=STATE, proc=pathlib.Path("/proc")):
    """True when this pod booted through the base: boot.sh wrote state/boot.pid and that pid is alive AND is boot.sh.
    PID 1's cmdline is no evidence — podctl's wrapper text names boot.sh even while the image's own boot runs (2.0.6)."""
    try:
        pid = (state / "boot.pid").read_text().strip()
        argv = (proc / pid / "cmdline").read_bytes().split(b"\0")
    except (OSError, ValueError):
        return False
    return len(argv) >= 2 and argv[1].endswith(b"/boot.sh")


def _venv():
    """The venv the base chose — handed over by base_test as BASE_VENV; the glob is only for a bare pytest run."""
    v = os.environ.get("BASE_VENV")
    if v and (pathlib.Path(v) / ".comfy-base-venv").exists(): return pathlib.Path(v)
    for v in sorted(COMFY.glob(".venv*")):
        if ".pre-" in v.name: continue
        if (v / ".comfy-base-venv").exists(): return v
    return None


def _proc_table():
    """pid|ppid|cwd|argv for every python running main.py (what _base_pids_from_table reads on the pod)."""
    out = []
    for d in pathlib.Path("/proc").glob("[0-9]*"):
        try:
            argv = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
            if "main.py" not in argv: continue
            out.append((int(d.name), os.readlink(d / "cwd"), argv))
        except OSError:
            continue
    return out


def _py(*code):
    v = _venv(); assert v, "no stamped venv beside ComfyUI"
    r = subprocess.run([str(v / "bin" / "python"), "-c", "\n".join(code)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.mark.runpod
def test_runpod_venv_is_stamped_at_the_path_the_boot_activates():
    if not ON_POD: pytest.skip("pod only")
    v = _venv(); assert v is not None and v.parent == COMFY and (v / "bin" / "python").exists()
    stamp = (v / ".comfy-base-venv").read_text()
    assert re.search(r"python=\d+\.\d+\.\d+", stamp) and "torch=" in stamp and "base=" in stamp


@pytest.mark.runpod
def test_runpod_the_venv_survives_a_pod_restart():
    if not ON_POD: pytest.skip("pod only")
    v = _venv(); assert v
    env = {**os.environ, "VENV": str(v), "BASE_PERSIST_ROOT": "/workspace"}
    r = subprocess.run([str(v / "bin" / "python"), str(BASE / "py" / "venv_verify.py")], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr        # every path in the startup closure is on the volume


@pytest.mark.gpu
def test_gpu_torch_is_cuda13_with_the_devices_sm():
    if not ON_POD: pytest.skip("pod only")
    cc = subprocess.run(["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"], capture_output=True, text=True).stdout.strip().split("\n")[0].strip()
    want = "sm_" + cc.replace(".", "")
    out = _py("import torch", "print(torch.version.cuda, ','.join(torch.cuda.get_arch_list()), torch.cuda.is_available())")
    cuda, archs, avail = out.split()
    assert cuda.startswith("13") and want in archs.split(",") and avail == "True", out


@pytest.mark.gpu
def test_gpu_driver_and_vram():
    if not ON_POD: pytest.skip("pod only")
    q = subprocess.run(["nvidia-smi", "--query-gpu=driver_version,memory.total", "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout.strip().split("\n")[0]
    drv, mem = [x.strip() for x in q.split(",")]
    assert int(drv.split(".")[0]) >= 580 and int(float(mem)) >= 16000, q


@pytest.mark.gpu
def test_gpu_python_stack_matches_the_stamp():
    if not ON_POD: pytest.skip("pod only")
    v = _venv(); stamp = (v / ".comfy-base-venv").read_text()
    py = re.search(r"python=(\d+\.\d+\.\d+)", stamp).group(1); torch = re.search(r"torch=(\S+)", stamp).group(1)
    out = _py("import sys, torch", "print('%d.%d.%d' % sys.version_info[:3], torch.__version__.split('+')[0])")
    assert out.split() == [py, torch], (out, stamp)


@pytest.mark.runpod
def test_runpod_the_running_comfyui_uses_the_base_venv():
    """The ComfyUI answering on this pod runs on the base's venv — after the base's boot has taken over. Before that
    (the very first install on an image's own boot) the server is the image's; the test says so and skips."""
    if not ON_POD: pytest.skip("pod only")
    v = _venv(); assert v
    procs = [(pid, cwd, argv) for pid, cwd, argv in _proc_table() if re.match(r"\S*python\S* (\S*/)?main\.py", argv)]
    if not procs: pytest.skip("no ComfyUI process is running")
    ours = [p for p in procs if os.path.realpath(p[2].split()[0]) == os.path.realpath(str(v / "bin" / "python")) or p[1] == str(COMFY)]
    if not ours and not _booted_through_base():
        pytest.skip("ComfyUI is still the image's own launch (pid %d): a package run with BASE_RESTART=1, or the next stop/start, hands it to the base" % procs[0][0])
    assert ours, procs


@pytest.mark.runpod
def test_runpod_boot_sh_is_pid1_and_sshd_jupyter_answer():
    """After the stop/start: PID 1 is the base's boot.sh, sshd answers on 22 with a banner, JupyterLab answers on 8888."""
    if not ON_POD: pytest.skip("pod only")
    import socket, urllib.request
    if not _booted_through_base():
        pytest.skip("this pod has not booted through the base yet (no live state/boot.pid): the next stop/start hands the boot to boot.sh (item 9)")
    with socket.create_connection(("127.0.0.1", 22), timeout=5) as sk:
        sk.settimeout(5); assert sk.recv(64).startswith(b"SSH-")
    try:
        code = urllib.request.urlopen("http://127.0.0.1:8888/", timeout=10).getcode()
    except urllib.error.HTTPError as e:
        code = e.code
    assert code in (200, 302, 401, 403), code


@pytest.mark.gpu
def test_gpu_vram_is_ours():
    """nvidia-smi in a container shows the whole card's memory but only our processes: what nobody visible holds belongs to
    another container or a leaked host context, and every render dies at its first node (2026-09-06, 93.5 GB held outside
    pod fakepod0000003 with the card idle). Stop/start or redeploy the pod; nothing in it can free that memory."""
    if not ON_POD: pytest.skip("pod only")
    line = subprocess.run(["python3", str(BASE / "py" / "gpu_facts.py")], capture_output=True, text=True).stdout.strip()
    m = re.search(r"verdict=(\S+)", line)
    assert m and m.group(1) != "no-gpu", "gpu_facts could not read nvidia-smi: " + line
    assert m.group(1) == "ours", "the GPU is not ours: " + line + " — stop/start or redeploy the pod"
