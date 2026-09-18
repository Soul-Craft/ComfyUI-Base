"""unit tier: source contracts on the base library. Every test reads the library's text or runs a
function against a throwaway tree; nothing here needs the pod, the network, or a ComfyUI checkout."""
import os, re, json, subprocess, pathlib, sys, pytest
pytestmark = pytest.mark.unit
BASE = pathlib.Path(__file__).resolve().parents[1]
LIB = sorted((BASE / "lib").glob("*.sh"))


def _repo_only(what="_build/"):
    """The zip ships lib/ py/ suite/ hosts/, not _build/ (the stub package, package.py, rehearse.py) nor the repo's testbed:
    a test that needs them skips on a pod with the reason, instead of failing 35 times (pod run 2026-09-05)."""
    if not (BASE / "_build").exists():
        pytest.skip("%s is repo tooling, not in the shipped base" % what)


def _stub():
    _repo_only("_build/stub")
    return BASE / "_build" / "stub"


def _src(p):
    return p.read_text(encoding="utf-8")


def code_only(text):
    """Drop full-line comments and trailing '  # ...' comments so a word in a comment never satisfies a test."""
    out = []
    for ln in text.splitlines():
        s = ln.lstrip()
        if s.startswith("#"):
            continue
        out.append(re.sub(r"\s{2,}#.*$", "", ln))
    return "\n".join(out)


def _bash(snippet, env=None, cwd=None):
    """Source base.sh, then run a bash snippet. Returns CompletedProcess."""
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(["bash", "-c", f'source "{BASE}/base.sh"; {snippet}'], capture_output=True, text=True, env=e, cwd=cwd)


# ---------------------------------------------------------------- T1: skeleton and plumbing

def test_unit_every_bash_file_parses():
    for p in [BASE / "base.sh", *LIB]:
        assert subprocess.run(["bash", "-n", str(p)]).returncode == 0, p.name


def test_unit_version_file_is_semver_and_base_prints_it():
    v = (BASE / "VERSION").read_text().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", v)
    r = subprocess.run(["bash", str(BASE / "base.sh"), "version"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == v
    # 2.4.0: the handbook SHIPS inside comfyui-base.zip, so a stale version line installs a guide describing a base
    # the machine is not running. Nothing executes prose, so nothing caught it until a person read it; this does.
    hb = (BASE / "comfyui-base-handbook.md").read_text()
    assert "**Version %s.**" % v in hb, "the handbook does not open with **Version %s.** - it has drifted from VERSION" % v
    assert "\n- %s:" % v in hb, "the handbook's Record section has no '- %s:' entry" % v


def test_unit_sourcing_defines_the_contract_helpers():
    probe = ('for f in ok miss err note hdr would todo warn base_mkdir base_run _base_confirm _base_confirm_yes '
             '_base_on_err _base_on_exit _base_install_traps base_env_setup _base_vge _base_fs_is_pool _base_sha256 '
             '_base_replace_file _base_git _base_ts _base_timeout _base_in_list _base_fsize _base_fdev _base_free_kb; do '
             'declare -F "$f" >/dev/null || { echo "missing $f"; exit 1; }; done; echo "v=$BASE_VERSION"')
    r = _bash(probe)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "v=" in r.stdout


def test_unit_every_trap_has_a_handler():
    src = code_only("\n".join(_src(p) for p in LIB))
    found = 0
    for m in re.finditer(r"trap\s+'?([^' ]+)", src):
        fn = m.group(1)
        if fn.startswith("_base_on_"):
            found += 1
            assert re.search(rf"^{re.escape(fn)}\(\)\s*\{{", src, re.M), fn
    assert found >= 2, "expected an ERR and an EXIT trap"


def test_unit_no_and_list_ends_a_loop_body():
    # `[ x ] && cmd` as the LAST statement of a loop body becomes the loop's exit status under errexit
    lines = "\n".join(_src(p) for p in LIB).splitlines()
    bad = []
    for i, ln in enumerate(lines):
        s = code_only(ln).strip()
        if re.match(r"^\[\[?.*\]\]?\s*&&", s) and not s.endswith(("|| true", "|| :")):
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if nxt.startswith("done"):
                bad.append((i + 1, s))
    assert not bad, bad


def test_unit_outbound_hosts_are_allowlisted():
    allow = {"huggingface.co", "github.com", "pypi.org", "pypi.nvidia.com", "download.pytorch.org",
             "127.0.0.1", "localhost", "0.0.0.0"}
    src = "\n".join(_src(p) for p in [*LIB, *sorted((BASE / "py").glob("*.py"))])
    hosts = set(re.findall(r"https?://([A-Za-z0-9.-]+)", src))
    assert hosts <= allow, hosts - allow


def test_unit_nothing_is_ever_uploaded():
    src = code_only("\n".join(_src(p) for p in [*LIB, *sorted((BASE / "py").glob("*.py"))]))
    assert not re.search(r"curl [^\n]*(-X ?(POST|PUT|PATCH)|--data|-F )", src)
    assert not re.search(r"urlopen\([^\n]*data=|requests\.post", src)


def test_unit_pip_is_never_bare():
    # pip on PATH belongs to whatever interpreter start.sh did not use. Only two forms name theirs:
    # `uv pip install --python "$PY"` and `"$PY" -m pip install`.
    src = code_only("\n".join(_src(p) for p in LIB))
    for m in re.finditer(r"\bpip3? install", src):
        before = src[max(0, m.start() - 3):m.start()]
        assert before.endswith("uv ") or before.endswith("-m "), src[max(0, m.start() - 60):m.end()]


def test_unit_fallbacks_are_loud():
    # every echo/return of a *_FALLBACK value is preceded by a '!!' line to stderr
    lines = "\n".join(_src(p) for p in LIB).splitlines()
    for i, ln in enumerate(lines):
        if "_FALLBACK" in ln and ("echo" in ln or "return" in ln) and "!!" not in ln:
            assert "!!" in lines[i - 1] and ">&2" in lines[i - 1], (i + 1, ln)


# ---------------------------------------------------------------- T2: discovery and banner

def test_unit_discover_finds_comfyui_by_main_py_not_models(tmp_path):
    (tmp_path / "ComfyUI" / "models").mkdir(parents=True)          # a stray tree: models/ but no main.py
    r = _bash("base_env_setup; base_discover quiet; echo rc=$?", env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "rc=4" in r.stdout, r.stdout + r.stderr          # 4 = the volume holds no tree (3 = no network volume at all)
    (tmp_path / "ComfyUI" / "main.py").write_text("")
    (tmp_path / "ComfyUI" / "comfyui_version.py").write_text('__version__ = "0.34.7"\n')
    r = _bash("base_env_setup; base_discover quiet; echo COMFY=$COMFY M=$M VENV=$VENV PORT=$PORT", env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert f"COMFY={tmp_path}/ComfyUI" in r.stdout and f"M={tmp_path}/ComfyUI/models" in r.stdout and "PORT=8188" in r.stdout, r.stdout + r.stderr


def test_unit_discover_honours_extra_model_paths_both_shapes(tmp_path):
    c = tmp_path / "ComfyUI"; (c / "models").mkdir(parents=True); (c / "main.py").write_text("")
    lib = tmp_path / "vol" / "library"; (lib / "diffusion_models").mkdir(parents=True)
    (c / "extra_model_paths.yaml").write_text(f"comfyui:\n  base_path: {lib}\n")
    r = _bash("base_env_setup; base_discover quiet; echo M=$M", env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert f"M={lib}" in r.stdout                                   # base_path holds category dirs → it IS the library
    parent = tmp_path / "vol2"; (parent / "models" / "loras").mkdir(parents=True)
    (c / "extra_model_paths.yaml").write_text(f"comfyui:\n  base_path: {parent}\n")
    r = _bash("base_env_setup; base_discover quiet; echo M=$M", env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert f"M={parent}/models" in r.stdout                        # else base_path/models


def test_unit_discover_reads_port_from_args_file_and_never_adopts_a_backup_venv(tmp_path):
    c = tmp_path / "ComfyUI"; c.mkdir(); (c / "main.py").write_text("")
    (tmp_path / "runpod-slim").mkdir(); (tmp_path / "runpod-slim" / "comfyui_args.txt").write_text("--port 8288\n--fast\n")
    for v in (".venv-cu130.pre-comfy-base-20260904", ".venv-cu130"):
        (c / v / "bin").mkdir(parents=True); (c / v / "bin" / "python").write_text("#!/bin/sh\n"); os.chmod(c / v / "bin" / "python", 0o755)
    r = _bash("base_env_setup; base_discover quiet; echo VENV=$VENV PORT=$PORT", env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert f"VENV={c}/.venv-cu130" in r.stdout and "PORT=8288" in r.stdout, r.stdout + r.stderr


def test_unit_comfy_version_reads_both_sources_and_is_loud_otherwise(tmp_path):
    d = tmp_path / "c"; d.mkdir()
    (d / "pyproject.toml").write_text('[project]\nname = "ComfyUI"\nversion = "0.35.1"\n')
    assert "0.35.1" in _bash(f'_base_comfy_version "{d}"').stdout
    (d / "comfyui_version.py").write_text('__version__ = "0.34.9"\n')
    assert "0.34.9" in _bash(f'_base_comfy_version "{d}"').stdout
    r = _bash(f'_base_comfy_version "{tmp_path}"')
    assert r.stdout.strip() == "0.0.0" and "!!" in r.stderr


def test_unit_discover_refuses_an_outside_comfy_dir_in_fake_mode(tmp_path):
    c = tmp_path / "ComfyUI"; c.mkdir(); (c / "main.py").write_text("")
    other = tmp_path.parent / (tmp_path.name + "-elsewhere") / "ComfyUI"; other.mkdir(parents=True); (other / "main.py").write_text("")
    r = _bash("base_env_setup; base_discover quiet; echo COMFY=$COMFY", env={"BASE_FAKE_ROOT": str(tmp_path), "COMFY_DIR": str(other)})
    assert f"COMFY={c}" in r.stdout   # a fake run can never address a real tree


def test_unit_banner_runs_on_a_fake_pod_and_names_the_pool_honestly(tmp_path):
    c = tmp_path / "ComfyUI"; (c / "models").mkdir(parents=True); (c / "main.py").write_text("")
    r = _bash("PKG_NAME=T; PKG_VERSION=1; base_env_setup; base_discover quiet; base_banner", env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert r.returncode == 0 and "ComfyUI" in r.stdout and "models" in r.stdout and "venv" in r.stdout, r.stdout + r.stderr
    src = code_only((BASE / "lib" / "10-discover.sh").read_text())
    assert "shared pool" in src and "_base_fs_is_pool" in src


# ---------------------------------------------------------------- T3: tokens, ComfyUI update, version gate

def _fake_comfy_repo(tmp_path):
    """A git repo shaped like ComfyUI with tags v0.34.0 and v0.35.0 and a bare remote."""
    origin = tmp_path / "origin.git"; work = tmp_path / "ComfyUI"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, stderr=subprocess.DEVNULL)
    def commit(ver):
        (work / "main.py").write_text(""); (work / "comfyui_version.py").write_text(f'__version__ = "{ver}"\n')
        subprocess.run(["git", "-C", str(work), "add", "."], check=True)
        subprocess.run(["git", "-C", str(work), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", ver], check=True)
        subprocess.run(["git", "-C", str(work), "tag", f"v{ver}"], check=True)
    commit("0.34.0"); commit("0.35.0")
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "HEAD:master", "--tags"], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "v0.34.0"], check=True)
    return work


def test_unit_update_checks_out_newest_tag_on_the_base_branch(tmp_path):
    work = _fake_comfy_repo(tmp_path)
    r = _bash('base_env_setup; base_discover quiet; base_update_comfyui; echo OLD=$COMFY_OLD NEW=$COMFY_NEW; git -C "$COMFY" rev-parse --abbrev-ref HEAD', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "OLD=0.34.0 NEW=0.35.0" in r.stdout and "comfy-base-stable" in r.stdout, r.stdout + r.stderr


def test_unit_check_mode_gates_on_the_post_update_version_without_touching_the_tree(tmp_path):
    work = _fake_comfy_repo(tmp_path)
    before = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    r = _bash("COMFY_MIN=0.35.0; base_env_setup; base_discover quiet; base_update_comfyui; base_comfy_gate; echo GATE=$?", env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_DRY": "1"})
    assert "GATE=0" in r.stdout, r.stdout + r.stderr
    assert before == subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True).stdout


def test_unit_gate_fails_below_the_floor(tmp_path):
    work = _fake_comfy_repo(tmp_path)
    r = _bash("COMFY_MIN=9.9.9; base_env_setup; base_discover quiet; base_update_comfyui; base_comfy_gate || echo GATE=$?; echo FAILED=${#BASE_FAILED[@]}", env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "GATE=1" in r.stdout and "FAILED=1" in r.stdout, r.stdout + r.stderr


def test_unit_tokens_are_saved_once_with_tight_permissions(tmp_path):
    (tmp_path / "ComfyUI").mkdir(); (tmp_path / "ComfyUI" / "main.py").write_text("")
    r = _bash('TOKENS=("HF_TOKEN|optional|x"); base_env_setup; base_discover quiet; base_tokens; ls -l "$BASE_STATE/tokens.env"; cat "$BASE_STATE/tokens.env"', env={"BASE_FAKE_ROOT": str(tmp_path), "HF_TOKEN": "hf_abc"})
    assert "-rw-------" in r.stdout and "HF_TOKEN=hf_abc" in r.stdout, r.stdout + r.stderr
    # the value is compared, never echoed: on the pod this test once printed the real token into the run log (2026-09-05)
    r2 = _bash('TOKENS=("HF_TOKEN|optional|x"); base_env_setup; base_discover quiet; base_tokens; grep -c HF_TOKEN "$BASE_STATE/tokens.env"; [ "$HF_TOKEN" = hf_abc ] && echo T=match', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert r2.stdout.strip().split("\n")[-2].endswith("1") and "T=match" in r2.stdout and "hf_abc" not in r2.stdout.split("tokens.env")[-1]   # not re-asked, not duplicated, re-loaded


# ---------------------------------------------------------------- T4: venv — picks, owner, probe, build, verify, rescue

def test_unit_torch_pick_has_no_soak_and_no_fallback():
    src = (BASE / "py" / "torch_pick.py").read_text()
    assert "AGE" not in src and "days" not in src and "cp" in src
    assert "sys.exit(1)" in src                                              # fetch failure is fatal, not a stale pick
    assert "pypi.org/pypi/torch/json" not in src


def test_unit_python_pick_resolves_pack_requirements_too():
    src = code_only((BASE / "lib" / "30-venv.sh").read_text())
    body = src[src.index("_base_python_pick()"):src.index("_base_torch_pick()")]
    assert "uv pip compile" in body and "_base_all_reqfiles" in body and "download.pytorch.org/whl/cu130" in body
    assert "FALLBACK" not in body and "BASE_FAILED" in body                   # no fallback Python


def test_unit_venv_interpreter_lives_on_the_volume():
    src = code_only((BASE / "lib" / "00-env.sh").read_text())
    assert "UV_MANAGED_PYTHON=1" in src
    line = [ln for ln in src.splitlines() if "export UV_PYTHON_INSTALL_DIR=" in ln][0]
    assert "/.cache/" not in line and "BASE_HOME" in line


def test_unit_persistence_predicate_knows_what_a_pod_restores():
    # truth table on the real bash function with the root forced
    cases = {"/workspace/comfy-base/python/cpython-3.14/bin/python3": 0, "/workspace/pkg/python/x/bin/python3": 0,
             "/usr/bin/python3.12": 0, "/opt/conda/bin/python3": 0,
             "/root/.local/share/uv/python/cpython-3.13/bin/python3": 1, "/tmp/py/bin/python3": 1, "/home/u/.local/x/python3": 1}
    for path, want in cases.items():
        r = _bash(f'HOME=/root; _base_path_is_persistent "{path}"; echo rc=$?', env={"BASE_PERSIST_ROOT_FORCE": "/workspace"})
        assert f"rc={want}" in r.stdout, (path, r.stdout, r.stderr)
    r = _bash('_base_path_is_persistent /tmp/x; echo rc=$?')                 # off the pod: nothing to prove
    assert "rc=0" in r.stdout


def test_unit_venv_owner_recognises_every_stamp(tmp_path):
    v = tmp_path / ".venv"; v.mkdir()
    assert "none" in _bash(f'VENV="{v}"; _base_venv_owner').stdout
    (v / ".comfy-base-venv").write_text("x"); assert "base" in _bash(f'VENV="{v}"; _base_venv_owner').stdout


def test_unit_probe_condemns_a_runtime_written_interpreter():
    src = (BASE / "py" / "venv_probe.py").read_text()
    assert "pyvenv.cfg" in src and "_base_executable" in src and "/root" in src and "/tmp" in src and "hard.append" in src
    assert "sm_120" not in code_only(src) and "BASE_WANT_SM" in src         # device sm is a parameter, not a constant


def test_unit_verify_checks_the_whole_startup_closure_and_extra_imports():
    src = (BASE / "py" / "venv_verify.py").read_text()
    for needle in ("_base_executable", "pyvenv.cfg", ".pth", "sys.path", "BASE_EXTRA_IMPORTS", "BASE_WANT_SM", "BASE_WANT_PY"):
        assert needle in src, needle


def test_unit_fake_discovery_never_leaves_the_fake_root(tmp_path):
    """On a community template image with its own /opt/venv, A fake pod's discovery (BASE_FAKE_ROOT) still
    walked the real container-disk candidates (/venv, /opt/venv): the dry run's suite wrote a fake stamp into the image's
    venv, and the real run's suite then saw "reused" where "fake" was expected (2026-09-06, the first base step on the new
    pod). Container-disk paths are consulted only through BASE_FAKE_IMAGE_ROOT in fake mode — never the real ones."""
    c = tmp_path / "pod" / "ComfyUI"; c.mkdir(parents=True); (c / "main.py").write_text(""); (c / "requirements.txt").write_text("")
    img = tmp_path / "img"; (img / "opt" / "venv" / "bin").mkdir(parents=True); (img / "opt" / "venv" / "bin" / "python").write_text("#!/bin/bash\nexit 0\n")
    (img / "opt" / "venv" / "bin" / "python").chmod(0o755)
    r = _bash('base_env_setup; base_discover quiet; echo "VENV=$VENV"', env={"BASE_FAKE_ROOT": str(tmp_path / "pod"), "BASE_NO_NET": "1", "BASE_FAKE_IMAGE_ROOT": str(img)})
    assert f"VENV={img}/opt/venv" in r.stdout, "the fake image's /opt/venv must be found THROUGH the image root: " + r.stdout + r.stderr
    r = _bash('base_env_setup; base_discover quiet; echo "VENV=$VENV"', env={"BASE_FAKE_ROOT": str(tmp_path / "pod"), "BASE_NO_NET": "1"})
    venv = [l for l in r.stdout.splitlines() if l.startswith("VENV=")][-1][5:]
    assert venv.startswith(str(tmp_path / "pod")), "a fake pod's venv lies outside the fake root: " + venv


def test_unit_fake_venv_path_stamps_and_reports(tmp_path):
    c = tmp_path / "ComfyUI"; c.mkdir(); (c / "main.py").write_text(""); (c / "requirements.txt").write_text("")
    r = _bash('PKG_ID=t; base_env_setup; base_discover quiet; base_venv; echo RESULT=$BASE_VENV_RESULT; cat "$VENV/.comfy-base-venv"', env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1"})
    assert "RESULT=fake" in r.stdout and "python=" in r.stdout and "by=t" in r.stdout, r.stdout + r.stderr
    r2 = _bash('PKG_ID=t; base_env_setup; base_discover quiet; base_venv; echo RESULT=$BASE_VENV_RESULT', env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1"})
    assert "RESULT=reused" in r2.stdout, r2.stdout + r2.stderr


def test_unit_rescue_never_runs_the_interpreter_it_repairs():
    src = code_only((BASE / "lib" / "30-venv.sh").read_text())
    body = src[src.index("base_rescue()"):]
    assert '"$PY"' not in body and "$PY " not in body


def test_unit_a_pre_backup_is_never_adopted_and_stale_backups_drop_only_at_swap():
    src = code_only((BASE / "lib" / "30-venv.sh").read_text())
    swap = src[src.index("_base_venv_swap_aside()"):src.index("_base_venv_verify()")]
    assert "_base_drop_stale_venv_backups" in swap
    assert "_base_drop_stale_venv_backups" not in src[:src.index("_base_venv_swap_aside()")].replace("_base_drop_stale_venv_backups()", "")
    build = src[src.index("_base_venv_build()"):src.index("base_venv()")]
    assert build.index("_base_venv_swap_aside") < build.index('--seed "$VENV"')


def test_unit_every_venv_change_flags_a_restart():
    src = code_only("\n".join(p.read_text() for p in LIB))
    assigned = set(re.findall(r'BASE_VENV_RESULT="?(built|rebuilt|adopted)\b', src))
    assert assigned >= {"built", "rebuilt", "adopted"}, assigned
    if "base_restart()" in src:
        body = src[src.index("base_restart()"):]
        m = re.search(r'case "\$BASE_VENV_RESULT" in\s*([^)]+)\)', body)
        assert m and {"built", "rebuilt", "adopted"} <= set(m.group(1).split("|")), m.group(1) if m else "no case"


# ---------------------------------------------------------------- T5: node packs

def _fake_pack_repo(tmp_path, name, n_commits=2):
    src = tmp_path / "remotes" / name; src.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    shas = []
    for i in range(n_commits):
        (src / "nodes.py").write_text(f"# {i}\nimport numpy\n"); (src / "requirements.txt").write_text("numpy\n")
        subprocess.run(["git", "-C", str(src), "add", "."], check=True)
        subprocess.run(["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", str(i)], check=True)
        shas.append(subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip())
    return src, shas


def test_unit_packs_pin_to_the_declared_commit_and_latest_prints_new_rows(tmp_path):
    c = tmp_path / "ComfyUI"; c.mkdir(); (c / "main.py").write_text("")
    src, shas = _fake_pack_repo(tmp_path, "FakePack")
    row = f"FakePack|{src}|{shas[0]}||test pack"
    r = _bash(f'BASE_PACKS=(); PACKS=("{row}"); base_env_setup; base_discover quiet; base_packs git; git -C "$CN/FakePack" rev-parse HEAD', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert shas[0] in r.stdout, r.stdout + r.stderr
    r = _bash(f'BASE_PACKS=(); PACKS=("{row}"); base_env_setup; base_discover quiet; base_packs git; printf "%s\\n" "${{BASE_PACKS_LATEST_ROWS[@]}}"', env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_LATEST": "1"})
    assert f"FakePack|{src}|{shas[1]}||test pack" in r.stdout, r.stdout + r.stderr


def test_unit_an_unreachable_pin_is_a_failure_not_a_warning(tmp_path):
    c = tmp_path / "ComfyUI"; c.mkdir(); (c / "main.py").write_text("")
    src, shas = _fake_pack_repo(tmp_path, "FakePack")
    row = f"FakePack|{src}|{'0' * 40}||test pack"
    r = _bash(f'BASE_PACKS=(); PACKS=("{row}"); base_env_setup; base_discover quiet; base_packs git || true; echo FAILED=${{#BASE_FAILED[@]}}', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "FAILED=1" in r.stdout, r.stdout + r.stderr


def test_unit_a_package_may_not_redeclare_a_base_pack():
    r = _bash('PACKS=("rgthree-comfy|https://github.com/rgthree/rgthree-comfy|' + "a" * 40 + '||dup"); _base_pack_rows >/dev/null; echo rc=$?')
    assert "rc=1" in r.stdout and "rgthree-comfy" in r.stderr


def test_unit_pack_rows_require_a_40_hex_sha():
    r = _bash('PACKS=("X|https://github.com/x/X|main||why"); _base_pack_rows >/dev/null; echo rc=$?')
    assert "rc=1" in r.stdout


def test_unit_base_packs_are_the_shared_six_and_pinned():
    r = _bash('printf "%s\\n" "${BASE_PACKS[@]}"')
    dirs = [ln.split("|")[0] for ln in r.stdout.splitlines() if ln]
    assert dirs == ["rgthree-comfy", "ComfyUI-KJNodes", "ComfyUI-VideoHelperSuite", "cg-use-everywhere", "ComfyUI-Manager", "ComfyUI-advanced-model-manager"], dirs
    for ln in r.stdout.splitlines():
        assert re.fullmatch(r"[0-9a-f]{40}", ln.split("|")[2]), ln


def test_unit_basetest_base_packs_matches_the_table():
    """A converted suite reads the base's packs through basetest, not by re-parsing 40-packs.sh itself."""
    from basetest import base_packs
    rows = base_packs()
    assert {p["dir"] for p in rows} == {"rgthree-comfy", "ComfyUI-KJNodes", "ComfyUI-VideoHelperSuite", "cg-use-everywhere", "ComfyUI-Manager", "ComfyUI-advanced-model-manager"}
    assert all(re.fullmatch(r"[0-9a-f]{40}", p["sha"]) for p in rows), rows
    assert all(p["url"].startswith("https://github.com/") for p in rows)


def test_unit_list_packs_merges_and_dedupes_across_package_dirs(tmp_path):
    for name, sha in (("A", "1" * 40), ("B", "1" * 40)):
        d = tmp_path / f"{name} Pkg"; d.mkdir()
        (d / f"{name} Pkg-script.sh").write_text(
            f'PKG_ID={name.lower()}; PKG_NAME="{name}"; PKG_VERSION=1; BASE_MIN=1.0.0; WF_NAME=w.json; COMFY_MIN=0.34.0\n'
            f'PACKS=("Shared|https://github.com/x/Shared|{sha}||s")\nMODELS=()\nsource "{BASE}/base.sh"; base_main "$@"\n')
    r = subprocess.run(["bash", str(BASE / "base.sh"), "list-packs", str(tmp_path / "A Pkg"), str(tmp_path / "B Pkg")], capture_output=True, text=True)
    assert r.returncode == 0 and sum(1 for ln in r.stdout.splitlines() if ln.startswith("Shared|")) == 1 and "rgthree-comfy|" in r.stdout, r.stdout + r.stderr
    s = tmp_path / "B Pkg" / "B Pkg-script.sh"; s.write_text(s.read_text().replace("1" * 40, "2" * 40))
    r = subprocess.run(["bash", str(BASE / "base.sh"), "list-packs", str(tmp_path / "A Pkg"), str(tmp_path / "B Pkg")], capture_output=True, text=True)
    assert r.returncode == 1 and "Shared" in r.stderr, r.stdout + r.stderr


def test_unit_all_reqfiles_covers_comfyui_and_every_located_pack(tmp_path):
    c = tmp_path / "ComfyUI"; (c / "custom_nodes" / "P").mkdir(parents=True); (c / "main.py").write_text("")
    (c / "requirements.txt").write_text(""); (c / "custom_nodes" / "P" / "requirements.txt").write_text("x\n")
    r = _bash('base_env_setup; base_discover quiet; PACK_DIRS=("P|$CN/P"); _base_all_reqfiles', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert f"{c}/requirements.txt" in r.stdout and f"{c}/custom_nodes/P/requirements.txt" in r.stdout


# ---------------------------------------------------------------- T6: models — rows, index, download, move, disk gate, prune

ROW_A = "diffusion_models|Example|Turbo|a.safetensors|https://huggingface.co/x/y/resolve/main/a.safetensors|1048576|A|"
ROW_LOCAL = "loras|SDXL|Detailers|mine.safetensors|LOCAL|0|private lora|"
ROW_BAD_FAMILY = "vae|No pe||v.safetensors|https://huggingface.co/x/y/resolve/main/v.safetensors|10|bad|"   # a space: not a folder name


def _pod(tmp_path):
    c = tmp_path / "ComfyUI"; (c / "models").mkdir(parents=True); (c / "main.py").write_text(""); return c


def test_unit_model_rows_validate_family_bytes_and_local():
    assert "rc=0" in _bash(f'MODELS=("{ROW_A}" "{ROW_LOCAL}"); _base_model_rows >/dev/null; echo rc=$?').stdout
    r = _bash(f'MODELS=("{ROW_BAD_FAMILY}"); _base_model_rows >/dev/null; echo rc=$?'); assert "rc=1" in r.stdout and "No pe" in r.stderr
    r = _bash('MODELS=("vae|Example||v.safetensors|https://huggingface.co/x/y/resolve/main/v.safetensors|0|zero|"); _base_model_rows >/dev/null; echo rc=$?'); assert "rc=1" in r.stdout
    r = _bash('MODELS=("vae|Example||v.safetensors|https://evil.example/v.safetensors|5|host|"); _base_model_rows >/dev/null; echo rc=$?'); assert "rc=1" in r.stdout
    assert "diffusion_models/Example/Turbo/a.safetensors" in _bash(f'_base_dest_rel "{ROW_A}"').stdout
    assert "LLM/Qwen2.5-Omni/x.gguf" in _bash('_base_dest_rel "LLM||Qwen2.5-Omni|x.gguf|https://huggingface.co/a/b/resolve/main/x.gguf|5|n|"').stdout
    assert "vae_approx/taeh3.safetensors" in _bash('_base_dest_rel "vae_approx|||taeh3.safetensors|https://huggingface.co/a/b/resolve/main/taeh3.safetensors|5|flat|"').stdout


def test_unit_fake_run_moves_a_stray_and_fake_downloads_the_rest(tmp_path):
    c = _pod(tmp_path); stray = tmp_path / "old" / "a.safetensors"; stray.parent.mkdir(); stray.write_bytes(b"\0" * 1048576)
    row_b = "vae|Example|Real|b.safetensors|https://huggingface.co/x/y/resolve/main/b.safetensors|4096|B|"
    r = _bash(f'MODELS=("{ROW_A}" "{row_b}"); base_env_setup; base_discover quiet; base_models; echo MOVED=${{#MODEL_MOVED[@]}} DL=${{#MODEL_DL[@]}}; ls "$M/diffusion_models/Example/Turbo" "$M/vae/Example/Real"',
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)})
    assert "MOVED=1 DL=1" in r.stdout and "a.safetensors" in r.stdout and "b.safetensors" in r.stdout and not stray.exists(), r.stdout + r.stderr
    assert (c / "models" / "vae" / "Example" / "Real" / "b.safetensors").stat().st_size == 4096


def test_unit_a_missing_local_file_fails_the_run_by_name(tmp_path):
    c = _pod(tmp_path)
    r = _bash(f'MODELS=("{ROW_LOCAL}"); base_env_setup; base_discover quiet; base_models; echo FAILED=${{#BASE_FAILED[@]}}; printf "%s\\n" "${{BASE_FAILED[@]}}"',
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)})
    assert "FAILED=1" in r.stdout and "mine.safetensors" in r.stdout and "loras/SDXL/Detailers" in r.stdout, r.stdout + r.stderr


def test_unit_a_found_local_file_is_relocated_any_size(tmp_path):
    c = _pod(tmp_path); (tmp_path / "somewhere").mkdir(); (tmp_path / "somewhere" / "mine.safetensors").write_bytes(b"xyz")
    r = _bash(f'MODELS=("{ROW_LOCAL}"); base_env_setup; base_discover quiet; base_models; echo FAILED=${{#BASE_FAILED[@]}}; ls "$M/loras/SDXL/Detailers"',
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)})
    assert "FAILED=0" in r.stdout and "mine.safetensors" in r.stdout, r.stdout + r.stderr


def test_unit_the_disk_gate_runs_before_anything_is_downloaded_and_admits_what_it_cannot_check(tmp_path):
    c = _pod(tmp_path)
    r = _bash(f'MODELS=("{ROW_A}"); base_env_setup; base_discover quiet; base_models; echo FAILED=${{#BASE_FAILED[@]}} DL=${{#MODEL_DL[@]}}',
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path), "BASE_FAKE_FREE_GB": "0"})
    assert "FAILED=1 DL=0" in r.stdout and "not enough space" in r.stdout, r.stdout + r.stderr
    src = code_only((BASE / "lib" / "50-models.sh").read_text()); gate = src[src.index("_base_disk_gate()"):src.index("base_models()")]
    assert gate.index("BASE_FAKE_FREE_GB") < gate.index("_base_fs_is_pool")


def test_unit_prune_offers_only_unclaimed_duplicates_and_needs_the_prompt(tmp_path):
    c = _pod(tmp_path); dest = c / "models" / "diffusion_models" / "Example" / "Turbo"; dest.mkdir(parents=True)
    (dest / "a.safetensors").write_bytes(b"\0" * 1048576); dup = tmp_path / "dup" / "a.safetensors"; dup.parent.mkdir(); dup.write_bytes(b"\0" * 1048576)
    sup = c / "models" / "diffusion_models" / "Example" / "old.safetensors"; sup.write_bytes(b"1")
    other = c / "models" / "diffusion_models" / "Example-V" / "old.safetensors"; other.parent.mkdir(parents=True); other.write_bytes(b"1")   # another family: never ours to offer
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)}
    r = _bash(f'MODELS=("{ROW_A}"); SUPERSEDED=(old.safetensors); base_env_setup; base_discover quiet; base_models; base_prune; echo DEL=${{#DEL_FILES[@]}}', env=env)
    assert "DEL=2" in r.stdout and dup.exists() and sup.exists(), r.stdout + r.stderr          # no TTY → No
    r = _bash(f'MODELS=("{ROW_A}"); SUPERSEDED=(old.safetensors); base_env_setup; base_discover quiet; base_models; base_prune', env={**env, "BASE_YES": "1"})
    assert not dup.exists() and not sup.exists() and other.exists() and (dest / "a.safetensors").exists(), r.stdout + r.stderr


def test_unit_prune_never_deletes_a_symlink_or_uses_rm_rf_on_files():
    src = code_only((BASE / "lib" / "50-models.sh").read_text()); body = src[src.index("base_prune()"):]
    assert "-L" in body and "rm -rf" not in body.split("DEL_DIRS")[0]


# ---------------------------------------------------------------- T7: workflow path sync

def _wf(nodes, subgraphs=None):
    d = {"id": "x", "revision": 0, "last_node_id": 99, "last_link_id": 0, "nodes": nodes, "links": [], "groups": [], "version": 0.4,
         "extra": {"package_version": "1.0.0"}}
    if subgraphs: d["definitions"] = {"subgraphs": subgraphs}
    return d


def _node(i, t, wv, mode=0):
    return {"id": i, "type": t, "mode": mode, "pos": [0, 0], "size": [100, 50], "widgets_values": wv, "inputs": [], "outputs": []}


def _sync(wf, m, rows, cats, *extra):
    return subprocess.run(["python3", str(BASE / "py" / "sync_workflow.py"), str(wf), str(m), str(rows), str(cats), *extra], capture_output=True, text=True)


def test_unit_sync_rewrites_to_canonical_dests_normalises_backslashes_and_reports_bypassed(tmp_path):
    wf = tmp_path / "w.json"
    wf.write_text(json.dumps(_wf([_node(1, "UNETLoader", ["Example2\\example-2-9b.safetensors", "default"]),
                                    _node(2, "UnetLoaderGGUF", ["Example2\\example-2-9b-Q8_0.gguf"], mode=4),
                                    _node(3, "VAELoader", ["unknown_active.safetensors"])]), indent=2) + "\n")
    rows = tmp_path / "rows.tsv"; rows.write_text("diffusion_models\tExample.2\tVariant\texample-2-9b.safetensors\n")
    cats = tmp_path / "cats.tsv"; cats.write_text("UNETLoader\tdiffusion_models\nUnetLoaderGGUF\tunet\nVAELoader\tvae\n")
    m = tmp_path / "models"; m.mkdir()
    r = _sync(wf, m, rows, cats)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(wf.read_text())
    assert out["nodes"][0]["widgets_values"][0] == "Example.2/Variant/example-2-9b.safetensors"
    assert out["nodes"][1]["widgets_values"][0] == "Example2\\example-2-9b-Q8_0.gguf"        # bypassed: reported, untouched
    assert "SYNC fixed=1 active_missing=1 bypassed_missing=1" in r.stdout, r.stdout
    assert "  ACTIVE " in r.stdout and "unknown_active.safetensors" in r.stdout and "  BYPASSED " in r.stdout
    assert (tmp_path / "w.json.bak").exists()
    assert wf.read_text().startswith('{\n  "id"')                                             # indent preserved (2)


def test_unit_sync_reaches_subgraph_definitions_instances_and_power_lora_rows(tmp_path):
    sg = {"id": "fc72fed4-0000-0000-0000-000000000000", "name": "Load", "nodes": [_node(171, "UNETLoader", ["example_engine_a_pruned_int8.safetensors", "default"])], "links": [], "inputs": [], "outputs": []}
    inst = _node(7, "fc72fed4-0000-0000-0000-000000000000", ["example_engine_a_pruned_int8.safetensors", "example_encoder_nvfp4.safetensors"])
    pl = _node(9, "Power Lora Loader (rgthree)", [{"type": "PowerLoraLoaderHeaderWidget"}, {"on": True, "lora": "Example 2\\Variant\\insta variant9b.safetensors", "strength": 1},
                                                  {"on": False, "lora": "Example 2\\Variant\\off_row.safetensors", "strength": 1}])
    wf = tmp_path / "w.json"; wf.write_text(json.dumps(_wf([inst, pl], [sg]), indent=1))
    rows = tmp_path / "rows.tsv"
    rows.write_text("diffusion_models\tExample-V\t\texample_engine_a_pruned_int8.safetensors\ntext_encoders\tExample-V\t\texample_encoder_nvfp4.safetensors\nloras\tExample.2\tVariant\tinsta variant9b.safetensors\n")
    cats = tmp_path / "cats.tsv"; cats.write_text("UNETLoader\tdiffusion_models\nCLIPLoader\ttext_encoders\n")
    (tmp_path / "models").mkdir()
    r = _sync(wf, tmp_path / "models", rows, cats)
    out = json.loads(wf.read_text())
    assert out["definitions"]["subgraphs"][0]["nodes"][0]["widgets_values"][0] == "Example-V/example_engine_a_pruned_int8.safetensors"
    assert out["nodes"][0]["widgets_values"] == ["Example-V/example_engine_a_pruned_int8.safetensors", "Example-V/example_encoder_nvfp4.safetensors"]
    assert out["nodes"][1]["widgets_values"][1]["lora"] == "Example.2/Variant/insta variant9b.safetensors"
    assert out["nodes"][1]["widgets_values"][2]["lora"] == "Example 2\\Variant\\off_row.safetensors"                 # an OFF row is bypassed: reported, untouched
    assert "SYNC fixed=4 active_missing=0 bypassed_missing=1" in r.stdout, r.stdout
    assert wf.read_text().startswith('{\n "id"')                                              # indent preserved (1)


def test_unit_sync_dry_run_writes_nothing(tmp_path):
    wf = tmp_path / "w.json"; wf.write_text(json.dumps(_wf([_node(1, "UNETLoader", ["Example2\\a.safetensors", "default"])])))
    rows = tmp_path / "rows.tsv"; rows.write_text("diffusion_models\tExample.2\tVariant\ta.safetensors\n")
    cats = tmp_path / "cats.tsv"; cats.write_text("UNETLoader\tdiffusion_models\n"); (tmp_path / "models").mkdir()
    before = wf.read_text(); r = _sync(wf, tmp_path / "models", rows, cats, "--dry")
    assert "SYNC fixed=1" in r.stdout and wf.read_text() == before and not (tmp_path / "w.json.bak").exists()


def test_unit_base_sync_fails_the_run_on_an_active_unresolved_loader_and_copies_the_workflow(tmp_path):
    c = _pod(tmp_path); (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "W.json").write_text(json.dumps(_wf([_node(1, "VAELoader", ["nowhere.safetensors"])])))
    r = _bash(f'PKG_DIR="{tmp_path}/pkg"; WF_NAME="W.json"; MODELS=(); base_env_setup; base_discover quiet; base_sync; echo FAILED=${{#BASE_FAILED[@]}} RESULT=$BASE_SYNC_RESULT', env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1"})
    assert "FAILED=1" in r.stdout and "nowhere.safetensors" in r.stdout, r.stdout + r.stderr
    assert (c / "user" / "default" / "workflows" / "W.json").exists()


def test_unit_loader_cats_cover_the_core_loaders_and_the_packages_known_ones():
    r = _bash('printf "%s\\n" "${BASE_LOADER_CATS[@]}"')
    have = {ln.split("|")[0] for ln in r.stdout.splitlines() if ln}
    for cls in ("UNETLoader", "CLIPLoader", "DualCLIPLoader", "VAELoader", "LoraLoader", "LoraLoaderModelOnly", "CheckpointLoaderSimple",
                "UpscaleModelLoader", "UnetLoaderGGUF", "ClipLoaderGGUF", "ModelPreviewOverrideKJ", "SAMLoader", "SAM_SmartInpainter"):
        assert cls in have, cls


# ---------------------------------------------------------------- T8: hygiene

def _base_args(tmp_path):
    return tmp_path / "comfy-base" / "state" / "comfyui_args.txt"


def test_unit_args_file_is_edited_by_flag_and_value_idempotently(tmp_path):
    """The base owns ComfyUI's flags in state/comfyui_args.txt. A template's runpod-slim/comfyui_args.txt is imported
    ONCE (its flags become the base's starting point) and is never edited or read again."""
    c = _pod(tmp_path); (tmp_path / "runpod-slim").mkdir(); a = tmp_path / "runpod-slim" / "comfyui_args.txt"
    a.write_text("--preview-method latent2rgb\n--use-sage-attention\n--fast\n")
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1"}
    r = _bash('base_env_setup; base_discover quiet; base_hygiene; echo CHANGED=${#BASE_CHANGED[@]}', env=env)
    b = _base_args(tmp_path); txt = b.read_text()
    assert "imported" in r.stdout and str(a) in r.stdout, r.stdout + r.stderr
    assert "--preview-method auto" in txt and "latent2rgb" not in txt and "--use-sage-attention" not in txt, txt + r.stdout + r.stderr
    assert "--fast" in txt and "--preview-size 1024" in txt and "--disable-api-nodes" in txt, txt
    assert a.read_text() == "--preview-method latent2rgb\n--use-sage-attention\n--fast\n"      # the template's file: untouched
    r2 = _bash('base_env_setup; base_discover quiet; base_hygiene; echo CHANGED=${#BASE_CHANGED[@]}', env=env)
    assert "CHANGED=0" in r2.stdout and b.read_text() == txt and "imported" not in r2.stdout, r2.stdout


def test_unit_sage_flag_survives_when_sageattention_imports(tmp_path):
    c = _pod(tmp_path); (tmp_path / "runpod-slim").mkdir(); a = tmp_path / "runpod-slim" / "comfyui_args.txt"; a.write_text("--use-sage-attention\n")
    v = c / ".venv-cu130" / "bin"; v.mkdir(parents=True); py = v / "python"; py.write_text("#!/bin/sh\nexit 0\n"); os.chmod(py, 0o755)   # every import "succeeds"
    r = _bash('base_env_setup; base_discover quiet; base_hygiene', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "--use-sage-attention" in _base_args(tmp_path).read_text(), r.stdout + r.stderr


def test_unit_check_mode_never_writes_the_args_file_or_manager_config(tmp_path):
    c = _pod(tmp_path); (tmp_path / "runpod-slim").mkdir(); a = tmp_path / "runpod-slim" / "comfyui_args.txt"; a.write_text("--fast\n")
    ini = c / "user" / "default" / "ComfyUI-Manager" / "config.ini"; ini.parent.mkdir(parents=True); ini.write_text("[default]\nsecurity_level = strong\n")
    _bash('base_env_setup; base_discover quiet; base_hygiene', env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_DRY": "1"})
    assert a.read_text() == "--fast\n" and "strong" in ini.read_text() and not _base_args(tmp_path).exists()


def test_unit_manager_security_level_is_set_to_normal_and_package_hygiene_hook_runs(tmp_path):
    c = _pod(tmp_path); (tmp_path / "runpod-slim").mkdir(); (tmp_path / "runpod-slim" / "comfyui_args.txt").write_text("")
    ini = c / "user" / "default" / "ComfyUI-Manager" / "config.ini"; ini.parent.mkdir(parents=True); ini.write_text("[default]\nsecurity_level = strong\nnetwork_mode = public\n")
    r = _bash('pkg_hygiene(){ echo HOOK_RAN; }; HYGIENE_ARGS=("--fp8_e4m3fn-unet"); base_env_setup; base_discover quiet; base_hygiene', env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1"})
    assert "security_level = normal" in ini.read_text() and "network_mode = public" in ini.read_text() and "HOOK_RAN" in r.stdout
    assert "--fp8_e4m3fn-unet" in _base_args(tmp_path).read_text()
    assert (tmp_path / "runpod-slim" / "comfyui_args.txt").read_text() == ""                  # the template's file: untouched


def test_unit_yaml_register_appends_once_and_respects_a_foreign_entry(tmp_path):
    y = tmp_path / "extra_model_paths.yaml"; y.write_text("comfyui:\n  base_path: /x\n")
    r = _bash(f'EXTRA_YAML="{y}"; _base_yaml_register pkg_a VHS_video_formats /workspace/pkg_a/video_formats; _base_yaml_register pkg_a VHS_video_formats /workspace/pkg_a/video_formats; echo CHANGED=${{#BASE_CHANGED[@]}}')
    assert y.read_text().count("VHS_video_formats") == 1 and "CHANGED=1" in r.stdout, y.read_text() + r.stdout
    r = _bash(f'EXTRA_YAML="{y}"; _base_yaml_register other VHS_video_formats /elsewhere; echo WARN=${{#BASE_WARN[@]}}')
    assert "/elsewhere" not in y.read_text() and "WARN=1" in r.stdout


# ---------------------------------------------------------------- T9: server — launch line, restart, import check, smoke, combos

def test_unit_never_restarts_comfyui_by_default():
    src = code_only((BASE / "lib" / "80-server.sh").read_text()); body = src[src.index("base_restart()"):]
    guard = body.index('"$BASE_RESTART" = "1"')
    for m in re.finditer(r"\b(kill|pkill)\b", body):
        assert m.start() > guard, "a kill before the BASE_RESTART guard"


def test_unit_restart_reasons_are_matched_by_pattern_not_one_string():
    src = code_only((BASE / "lib" / "80-server.sh").read_text()); body = src[src.index("base_restart()"):]
    m = re.search(r'case "\$BASE_VENV_RESULT" in\s*([^)]+)\)', body)
    assert m and {"built", "rebuilt", "adopted"} <= set(m.group(1).split("|")), body[:400]
    assert '= "built"' not in body


def test_unit_launch_line_defers_preview_flags_to_the_args_file(tmp_path):
    c = _pod(tmp_path); (tmp_path / "runpod-slim").mkdir(); (tmp_path / "runpod-slim" / "comfyui_args.txt").write_text("--preview-method taesd\n")
    r = _bash('base_env_setup; base_discover quiet; _base_start_cmd', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert r.stdout.count("--preview-method") == 1 and "taesd" in r.stdout and "--preview-size" in r.stdout, r.stdout
    assert "--listen 0.0.0.0" in r.stdout and "--enable-cors-header" in r.stdout


def test_unit_a_dead_server_is_not_a_smoke_failure_and_check_mode_skips(tmp_path):
    c = _pod(tmp_path); (tmp_path / "runpod-slim").mkdir(); (tmp_path / "runpod-slim" / "comfyui_args.txt").write_text("--port 8198\n")
    r = _bash('PKG_DIR=/nonexistent; WF_NAME=w.json; base_env_setup; base_discover quiet; base_restart; base_smoke; base_combos; echo R=$BASE_RESTART_RESULT S=$BASE_SMOKE_RESULT C=$BASE_COMBO_RESULT FAILED=${#BASE_FAILED[@]}', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "FAILED=0" in r.stdout and "not running" in r.stdout and "S=skipped" in r.stdout and "C=skipped" in r.stdout, r.stdout + r.stderr
    assert "nohup" in r.stdout and "main.py" in r.stdout                                           # the launch line is printed
    r = _bash('PKG_DIR=/nonexistent; WF_NAME=w.json; base_env_setup; base_discover quiet; base_import_check; base_restart; echo I=$BASE_IMPORT_RESULT R=$BASE_RESTART_RESULT', env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_DRY": "1"})
    assert "I=skipped" in r.stdout and "R=skipped" in r.stdout


def test_unit_combos_never_rewrite_with_a_restart_pending():
    src = code_only((BASE / "lib" / "80-server.sh").read_text()); body = src[src.index("base_combos()"):]
    assert "BASE_RESTART_NEEDED" in body and "--check-only" in body


def test_unit_smoke_and_combo_tools_agree_on_frontend_only_types():
    """A first pod run failed smoke on GetNode, SetNode and LC Groups Bypasser: KJNodes' Get/Set and
    LC123's group bypasser live in the packs' web/js only and are never in /object_info — frontend-only, like rgthree's.

    2.0.33: this used to grep the two files for each name, which was a proxy for "the tool
    treats it as frontend-only" and stopped being true the moment the three copies of the set
    became one import. Assert the VALUE the tools actually use."""
    import smoke, combofix, canvas
    for t in ("Label (rgthree)", "Fast Groups Bypasser (rgthree)", "MarkdownNote", "PrimitiveNode",
              "GetNode", "SetNode", "LC Groups Bypasser", "LC Bypasser", "LC Bypasser Panel"):
        assert t in smoke.FRONTEND_ONLY, f"smoke does not treat {t!r} as frontend-only"
        assert t in combofix.FRONTEND_ONLY, f"combofix does not treat {t!r} as frontend-only"
    assert smoke.FRONTEND_ONLY is canvas.FRONTEND_ONLY_TYPES is combofix.FRONTEND_ONLY, \
        "three copies of this set drifted once already; there is one now"
    s = (BASE / "py" / "smoke.py").read_text(); c = (BASE / "py" / "combofix.py").read_text()
    assert "definitions" in s and "definitions" in c                                                 # both look inside subgraphs


# ---------------------------------------------------------------- T10: the ledger

def test_unit_ledger_records_packs_models_and_hooks_and_scopes_deletion(tmp_path):
    c = _pod(tmp_path); env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)}
    r = _bash(f'PKG_ID=a; PKG_NAME=A; PKG_VERSION=1; pkg_post_venv(){{ :; }}; MODELS=("{ROW_A}"); SUPERSEDED=(old.safetensors); base_env_setup; base_discover quiet; base_models; base_ledger_write; cat "$BASE_STATE/packages/a.manifest"', env=env)
    assert "pkg=a" in r.stdout and "model\tdiffusion_models/Example/Turbo/a.safetensors\t1048576" in r.stdout and "hooks=pkg_post_venv" in r.stdout and "superseded\told.safetensors" in r.stdout, r.stdout + r.stderr
    r = _bash('PKG_ID=b; base_env_setup; _base_ledger_claims diffusion_models/Example/Turbo/a.safetensors; echo rc=$?', env=env); assert "rc=0" in r.stdout
    r = _bash('PKG_ID=a; base_env_setup; _base_ledger_claims diffusion_models/Example/Turbo/a.safetensors; echo rc=$?', env=env); assert "rc=1" in r.stdout   # own claim does not count
    r = _bash('PKG_ID=a; base_env_setup; _base_ledger_claims diffusion_models/Example/Turbo/nope.safetensors; echo rc=$?', env=env); assert "rc=1" in r.stdout


def test_unit_prune_keeps_a_file_another_package_claims(tmp_path):
    c = _pod(tmp_path); env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)}
    shared = c / "models" / "diffusion_models" / "Example" / "old.safetensors"; shared.parent.mkdir(parents=True); shared.write_bytes(b"1")
    row_b = "diffusion_models|Example||old.safetensors|https://huggingface.co/x/y/resolve/main/old.safetensors|1|B still needs it|"
    _bash(f'PKG_ID=b; PKG_NAME=B; PKG_VERSION=1; MODELS=("{row_b}"); base_env_setup; base_discover quiet; base_models; base_ledger_write', env=env)
    r = _bash(f'PKG_ID=a; PKG_NAME=A; PKG_VERSION=1; MODELS=("{ROW_A}"); SUPERSEDED=(old.safetensors); base_env_setup; base_discover quiet; base_models; base_prune; echo DEL=${{#DEL_FILES[@]}}', env={**env, "BASE_YES": "1"})
    assert "DEL=0" in r.stdout and shared.exists() and "claimed by another installed package" in r.stdout, r.stdout + r.stderr


def test_unit_dropped_pack_claimed_elsewhere_is_not_warned_about(tmp_path):
    c = _pod(tmp_path); (c / "custom_nodes" / "OldPack").mkdir(parents=True); env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1"}
    r = _bash('PKG_ID=a; DROPPED_PACKS="OldPack"; BASE_PACKS=(); PACKS=(); base_env_setup; base_discover quiet; base_packs git', env=env)
    assert "OldPack is no longer used" in r.stdout
    (tmp_path / "comfy-base" / "state" / "packages").mkdir(parents=True, exist_ok=True)
    (tmp_path / "comfy-base" / "state" / "packages" / "b.manifest").write_text(f"pkg=b\tname=B\tversion=1\tbase=1.0.0\tts=x\tstatus=ok\npack\tOldPack\t{'1'*40}\t{c}/custom_nodes/OldPack\thttps://github.com/x/OldPack\n")
    r = _bash('PKG_ID=a; DROPPED_PACKS="OldPack"; BASE_PACKS=(); PACKS=(); base_env_setup; base_discover quiet; base_packs git', env=env)
    assert "OldPack is no longer used" not in r.stdout, r.stdout


def test_unit_status_lists_installed_packages(tmp_path):
    c = _pod(tmp_path); env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1"}
    _bash('PKG_ID=a; PKG_NAME="Pkg A"; PKG_VERSION=1.2.3; MODELS=(); base_env_setup; base_discover quiet; base_ledger_write', env=env)
    r = subprocess.run(["bash", str(BASE / "base.sh"), "status"], capture_output=True, text=True, env={**os.environ, **env})
    assert r.returncode == 0 and "Pkg A" in r.stdout and "1.2.3" in r.stdout, r.stdout + r.stderr


def test_unit_check_mode_writes_no_ledger(tmp_path):
    c = _pod(tmp_path); env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_DRY": "1"}
    _bash('PKG_ID=a; PKG_NAME=A; PKG_VERSION=1; MODELS=(); base_env_setup; base_discover quiet; base_ledger_write', env=env)
    assert not (tmp_path / "comfy-base" / "state" / "packages" / "a.manifest").exists()


# ---------------------------------------------------------------- T12: step-one entry, self-install, manifest, packager

def test_unit_manifest_covers_every_shipped_file_and_a_tamper_is_fatal(tmp_path):
    _repo_only("_build/package.py")
    import shutil, zipfile
    r = subprocess.run(["python3", str(BASE / "_build" / "package.py"), "--base", "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    with zipfile.ZipFile(BASE / "comfyui-base.zip") as z: z.extractall(tmp_path / "up")
    inst = tmp_path / "up" / "comfyui-base"
    listed = {ln.split(None, 1)[1].lstrip("*") for ln in (inst / "MANIFEST.sha256").read_text().splitlines() if ln.strip()}
    shipped = {str(p.relative_to(inst)) for p in inst.rglob("*") if p.is_file() and p.name != "MANIFEST.sha256"}
    assert listed == shipped, (listed ^ shipped)
    assert "base.sh" in shipped and "lib/00-env.sh" in shipped and "py/basetest.py" in shipped and "comfyui-base-script.sh" in shipped
    assert not any(p.startswith("_build") or p.endswith(".zip") for p in shipped)
    home = tmp_path / "comfy-base"; shutil.copytree(inst, home)
    (home / "lib" / "00-env.sh").write_text((home / "lib" / "00-env.sh").read_text() + "\n# tamper\n")
    r = subprocess.run(["bash", str(home / "base.sh"), "version"], capture_output=True, text=True, env={**os.environ, "BASE_FAKE_ROOT": str(tmp_path)})
    assert r.returncode == 5 and "00-env.sh" in r.stderr, r.stdout + r.stderr
    r = subprocess.run(["bash", str(inst / "base.sh"), "version"], capture_output=True, text=True, env={**os.environ, "BASE_FAKE_ROOT": str(tmp_path)})
    assert r.returncode == 0                                                                        # an extracted (not installed) copy is not checked


def test_unit_step_one_installs_itself_and_runs_the_toolchain_in_fake_mode(tmp_path):
    _repo_only("the built zip")
    import zipfile
    with zipfile.ZipFile(BASE / "comfyui-base.zip") as z: z.extractall(tmp_path / "up")
    c = tmp_path / "ComfyUI"; c.mkdir(); (c / "main.py").write_text(""); (c / "comfyui_version.py").write_text('__version__ = "0.34.7"\n'); (c / "requirements.txt").write_text("")
    env = {k: v for k, v in os.environ.items() if not k.startswith("BASE_") and k not in ("HF_TOKEN", "COMFY_DIR")}
    env.update({"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_INNER": "1", "HOME": str(tmp_path / "home")})
    r = subprocess.run(["bash", str(tmp_path / "up" / "comfyui-base" / "comfyui-base-script.sh")], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-1500:]
    assert (tmp_path / "comfy-base" / "base.sh").exists() and (tmp_path / "comfy-base" / "MANIFEST.sha256").exists()
    assert (tmp_path / "comfy-base" / "state" / "packages" / "base.manifest").exists()
    assert "SUMMARY" in r.stdout and "INSTALL BASE" in r.stdout
    r2 = subprocess.run(["bash", str(tmp_path / "up" / "comfyui-base" / "comfyui-base-script.sh"), "--check"], capture_output=True, text=True, env=env)
    assert r2.returncode == 0 and "already installed" in r2.stdout, r2.stdout[-2000:]
    # 2.0.30: the SAME version with different content (an amended zip) must refresh the home — the pod kept a stale suite
    # for three runs on 2026-09-06 because only the version and the installed copy's own manifest were consulted
    import hashlib, re
    up = tmp_path / "up" / "comfyui-base"; fam = up / "pytest.ini"; fam.write_text(fam.read_text() + "# amended under the same version\n")
    man = up / "MANIFEST.sha256"; digest = hashlib.sha256(fam.read_bytes()).hexdigest()
    man.write_text(re.sub(r"^[0-9a-f]{64}(?=\s+\*?pytest\.ini$)", digest, man.read_text(), flags=re.M))
    note = "base %s already installed" % (up / "VERSION").read_text().strip()          # the base's own note, not a pack's "already installed"
    r3 = subprocess.run(["bash", str(tmp_path / "up" / "comfyui-base" / "comfyui-base-script.sh"), "--check"], capture_output=True, text=True, env=env)
    assert r3.returncode == 0 and "INSTALL BASE" in r3.stdout and note not in r3.stdout, r3.stdout[-2000:]
    assert "amended under the same version" in (tmp_path / "comfy-base" / "pytest.ini").read_text(), "the home must carry the amended file"
    r4 = subprocess.run(["bash", str(tmp_path / "up" / "comfyui-base" / "comfyui-base-script.sh"), "--check"], capture_output=True, text=True, env=env)
    assert r4.returncode == 0 and note in r4.stdout, r4.stdout[-2000:]


def test_unit_package_zip_is_reproducible_and_check_detects_drift(tmp_path):
    _repo_only("_build/package.py")
    import hashlib, shutil
    a = hashlib.sha256((BASE / "comfyui-base.zip").read_bytes()).hexdigest()
    subprocess.run(["python3", str(BASE / "_build" / "package.py"), "--base"], check=True, capture_output=True)
    assert hashlib.sha256((BASE / "comfyui-base.zip").read_bytes()).hexdigest() == a
    # a package dir: the packager builds <Name>-runpod.zip from the package's files, flat, with modes
    pkg = tmp_path / "Toy Pkg"; pkg.mkdir()
    (pkg / "Toy Pkg-script.sh").write_text("#!/usr/bin/env bash\nZIP_EXTRA=( notes.txt )\n"); (pkg / "Toy Pkg Workflow.json").write_text("{}"); (pkg / "suite.py").write_text(""); (pkg / "notes.txt").write_text("n")
    r = subprocess.run(["python3", str(BASE / "_build" / "package.py"), str(pkg)], capture_output=True, text=True); assert r.returncode == 0, r.stdout + r.stderr
    import zipfile
    with zipfile.ZipFile(pkg / "Toy Pkg-runpod.zip") as z:
        names = z.namelist(); assert set(names) == {"Toy Pkg Workflow.json", "Toy Pkg-script.sh", "suite.py", "notes.txt"}
        assert (z.getinfo("Toy Pkg-script.sh").external_attr >> 16) & 0o777 == 0o700
    assert subprocess.run(["python3", str(BASE / "_build" / "package.py"), str(pkg), "--check"], capture_output=True).returncode == 0
    (pkg / "notes.txt").write_text("changed")
    assert subprocess.run(["python3", str(BASE / "_build" / "package.py"), str(pkg), "--check"], capture_output=True).returncode == 1


def test_unit_zip_extra_carries_filenames_with_spaces(tmp_path):
    """Every filename this project ships has spaces in it. A whitespace split turned
    ZIP_EXTRA=( "A B.json" ) into two members that do not exist, and the packager then died with
    `missing: .../A` — naming a file nobody had written, which is the worst kind of error message.
    Unquoted single-word entries must keep parsing exactly as before, or this breaks every package."""
    _repo_only("_build/package.py")
    import zipfile
    pkg = tmp_path / "Toy Pkg"; pkg.mkdir()
    (pkg / "Toy Pkg-script.sh").write_text(
        '#!/usr/bin/env bash\nZIP_EXTRA=( plain.txt "Spaced Name.json" )   # trailing comment\n')
    (pkg / "Toy Pkg Workflow.json").write_text("{}")
    (pkg / "suite.py").write_text("")
    (pkg / "plain.txt").write_text("p")
    (pkg / "Spaced Name.json").write_text("{}")
    r = subprocess.run(["python3", str(BASE / "_build" / "package.py"), str(pkg)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    with zipfile.ZipFile(pkg / "Toy Pkg-runpod.zip") as z:
        assert set(z.namelist()) == {"Toy Pkg Workflow.json", "Toy Pkg-script.sh", "suite.py",
                                     "plain.txt", "Spaced Name.json"}


def test_unit_a_package_can_ask_for_a_restart():
    """base_restart() REBUILDS BASE_RESTART_WHY from the base's own signals, so a hook appending to
    that array had its reason thrown away. A package that ships its own ComfyUI node then ended up
    with the file on disk, never registered (ComfyUI imports custom nodes at start), and a GREEN
    install report — the worst shape a failure can take. PKG_RESTART_WHY is the package's channel
    and base_restart folds it in."""
    src = (BASE / "lib" / "00-env.sh").read_text()
    assert "PKG_RESTART_WHY=()" in src, "PKG_RESTART_WHY is not initialised"
    srv = (BASE / "lib" / "80-server.sh").read_text()
    body = srv[srv.index("base_restart()"):]
    wipe = body.index("BASE_RESTART_WHY=()")
    fold = body.index("PKG_RESTART_WHY[@]")
    assert fold > wipe, "PKG_RESTART_WHY must be folded in AFTER base_restart clears the array"
    # Structural on purpose. The end-to-end path needs a live ComfyUI to answer /system_stats —
    # base_restart returns early otherwise — and standing one up here would test the harness, not
    # the fold. What actually regressed was the ORDER, and that is what is asserted above.
    assert "PKG_RESTART_WHY" in (BASE / "comfyui-base-handbook.md").read_text(), \
        "a package-facing hook that is not in the handbook is a hook nobody will find"


def test_unit_pod_tiers_skip_off_pod_and_the_handbook_exists():
    src = (BASE / "suite" / "test_pod.py").read_text()
    assert "BASE_ON_POD" in src and "pytest.skip" in src
    hb = (BASE / "comfyui-base-handbook.md").read_text()
    for needle in ("step one", "BASE_RESTART", "LOCAL", "category|Family|Purpose|file", "dir|url|sha|cnr_id|why", "pkg_post_venv", "/workspace/comfy-base", "rescue"):
        assert needle in hb, needle


# ---------------------------------------------------------------- T13: the testbed derives its packs; list-packs is safe on unconverted scripts

def test_unit_list_packs_never_runs_a_script_that_does_not_source_the_base(tmp_path):
    d = tmp_path / "Old Pkg"; d.mkdir(); marker = tmp_path / "ran"
    (d / "Old Pkg-script.sh").write_text('#!/usr/bin/env bash\ntouch "%s"\nexit 0\n' % marker)
    r = subprocess.run(["bash", str(BASE / "base.sh"), "list-packs", str(d), str(BASE)], capture_output=True, text=True)
    assert r.returncode == 0 and not marker.exists() and "does not source the base" in r.stderr, r.stdout + r.stderr
    assert sum(1 for ln in r.stdout.splitlines() if ln.startswith("rgthree-comfy|")) == 1        # the base dir itself is not a package


def test_unit_testbed_derives_its_packs_from_the_packages():
    _repo_only("the repo's testbed.sh")
    if not (BASE.parent / "testbed.sh").exists():
        pytest.skip("no testbed.sh beside the base: this base is its own repository (2.2.0); the brand repositories carry one")
    tb = (BASE.parent / "testbed.sh").read_text()
    assert "list-packs" in tb and "rgthree/rgthree-comfy" not in tb and "cg-use-everywhere" not in tb   # the four base packs are never hand-listed
    assert "EXTRA=(" in tb                                                                        # packs of packages not yet converted
    r = subprocess.run(["bash", str(BASE.parent / "testbed.sh"), "--status"], capture_output=True, text=True)
    assert r.returncode == 0 and "BASE_NODE_SRC=" in r.stdout and "BASE_SERVER=" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------- plan 2 / T1: tier markers by prefix; converted packages agree

def test_unit_prefix_markers_apply_to_converted_suites(tmp_path):
    import sys
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = --strict-markers\nmarkers =\n    graph: g\n    unit: u\n")
    (tmp_path / "suite.py").write_text("def test_graph_a(): pass\ndef test_unit_b(): pass\ndef test_other_c(): pass\n")
    r = subprocess.run([sys.executable, "-m", "pytest", str(tmp_path / "suite.py"), "-c", str(tmp_path / "pytest.ini"), "-q", "-p", "no:cacheprovider", "-p", "basetest", "-m", "graph"],
                       capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(BASE / "py")})
    assert "1 passed" in r.stdout and "2 deselected" in r.stdout, r.stdout + r.stderr


def test_unit_every_package_family_is_in_its_brands_families_file():
    """2.3.0: the base lists no model families of its own. A brand keeps <brand>/families.txt beside brand.toml (one
    spelling per line) and every MODELS row of every package in that brand names one of them, so two packages on the
    same pod never file the same family under two spellings. Standalone, or for a brand without the file, this skips."""
    from basetest import brand_families, converted_packages, load_package
    checked = 0
    for d in converted_packages(BASE.parents[1]):
        fams = brand_families(d)
        if fams is None:
            continue
        p = load_package(d); checked += 1
        for m in p.models:
            if m["family"]:
                assert m["family"] in fams, "%s: family %r for %s is not in the brand's families.txt (%s)" % (d.name, m["family"], m["file"], ", ".join(sorted(fams)))
    if not checked: pytest.skip("no brand with a families.txt beside this base")


def test_unit_converted_packages_agree_on_dests_superseded_and_pins():
    from basetest import converted_packages, load_package
    pkgs = [load_package(d) for d in converted_packages(BASE.parents[1])]     # the repository root, two above the base
    if not pkgs: pytest.skip("no converted package beside the base yet")
    dest_by_name, sup_by_pkg, claims = {}, {}, {}
    for p in pkgs:
        for m in p.models:
            dest = "/".join(x for x in (m["category"], m["family"], m["purpose"], m["file"]) if x)
            assert dest_by_name.setdefault(m["file"].lower(), dest) == dest, "%s filed at two dests: %s vs %s" % (m["file"], dest_by_name[m["file"].lower()], dest)
            claims.setdefault(m["file"].lower(), set()).add(p.id)
        sup_by_pkg[p.id] = {s.lower() for s in p.superseded}
    for pid, sup in sup_by_pkg.items():
        for name in sup:
            others = claims.get(name, set()) - {pid}
            assert not others, "%s supersedes %s, which %s still installs" % (pid, name, sorted(others))
    r = subprocess.run(["bash", str(BASE / "base.sh"), "list-packs", *[str(p.dir) for p in pkgs]], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_unit_sageattention_builder_is_dry_run_aware_and_idempotent(tmp_path):
    """Three packages need SageAttention built for THIS GPU's sm (no PyPI wheel has sm_100/sm_120 kernels; 1.0.6 crashes
    there): one package carried the build in its own hook, another's first render died on `No module named
    'sageattention'`. The base offers the build to every hook: a dry run says "would", a fake venv skips,
    an importable sageattention is left alone, a missing GPU is a failure by name."""
    v = tmp_path / "venv"; (v / "bin").mkdir(parents=True); py = v / "bin" / "python"
    py.write_text('#!/bin/bash\ncase "$*" in *"import sageattention"*) exit "${SAGE_STUB_RC:-1}";; *) exit 0;; esac\n'); py.chmod(0o755)
    base = f'VENV="{v}"; PY="{py}"; BASE_STATE="{tmp_path}"; '
    r = _bash(base + 'BASE_DRY=1; base_build_sageattention; echo F=${#BASE_FAILED[@]}')
    assert "would build SageAttention" in r.stdout and "F=0" in r.stdout, r.stdout + r.stderr
    r = _bash(base + 'BASE_DRY=0; BASE_NO_NET=1; base_build_sageattention; echo F=${#BASE_FAILED[@]}')
    assert "SageAttention: skipped" in r.stdout and "F=0" in r.stdout, r.stdout + r.stderr
    r = _bash(base + 'BASE_DRY=0; BASE_NO_NET=0; base_build_sageattention; echo F=${#BASE_FAILED[@]}', env={"SAGE_STUB_RC": "0"})
    assert "already in" in r.stdout and "F=0" in r.stdout, r.stdout + r.stderr
    r = _bash(base + 'BASE_DRY=0; BASE_NO_NET=0; BASE_GPU_SM=""; base_build_sageattention; echo F=${#BASE_FAILED[@]}', env={"SAGE_STUB_RC": "1"})
    assert "no CUDA device visible" in r.stdout and "F=1" in r.stdout, r.stdout + r.stderr
    # torch 2.14's headers demand C++20 and upstream's setup.py hardcodes -std=c++17: every kernel failed at the first include
    # on the pod (2.0.25). The build clones the ref and patches the flag before pip sees it; the source says so once.
    src = (BASE / "lib" / "40-packs.sh").read_text()
    body = src[src.index("base_build_sageattention()"):src.index("base_consolidate()")]
    assert "git clone" in body and "SAGE_REF" in body and "s/-std=c++17/-std=c++20/g" in body and "setup.py" in body, body[-600:]


def _fake_nvidia_smi(tmp_path, total, used, apps):
    """A nvidia-smi answering the two queries gpu_facts.py makes, with the given card and visible-process numbers."""
    d = tmp_path / "smibin"; d.mkdir(parents=True, exist_ok=True)
    apps_csv = "\\n".join("%d, %d" % a for a in apps)
    (d / "nvidia-smi").write_text('#!/bin/bash\ncase "$*" in *query-gpu=memory*) echo "%d, %d";; *query-compute-apps*) printf "%s\\n";; *) exit 1;; esac\n' % (total, used, apps_csv))
    (d / "nvidia-smi").chmod(0o755)
    return d


def test_unit_gpu_facts_names_memory_held_outside_the_container(tmp_path):
    """The 2026-09-06 numbers: 97887 MiB card, 95258 used, one visible process holding 1698 — 93560 MiB are nobody's we can see."""
    import subprocess, sys
    sys.path.insert(0, str(BASE / "py")); import gpu_facts
    assert gpu_facts.verdict(97887, 95258, [(1903, 1698)]) == (1698, 93560, "NOT-OURS")
    assert gpu_facts.verdict(97887, 620, []) == (0, 620, "ours")                      # the driver's own reserve is not a tenant
    assert gpu_facts.verdict(97887, 20000, [(1, 19500)]) == (19500, 500, "ours")     # our models, nothing foreign
    env = {**os.environ, "PATH": "%s:%s" % (_fake_nvidia_smi(tmp_path, 97887, 95258, [(1903, 1698)]), os.environ["PATH"])}
    out = subprocess.run([sys.executable, str(BASE / "py" / "gpu_facts.py")], capture_output=True, text=True, env=env).stdout.strip()
    assert out == "total=97887 used=95258 ours=1698 foreign=93560 procs=1 verdict=NOT-OURS", out
    env["PATH"] = "%s:%s" % (_fake_nvidia_smi(tmp_path / "clean", 97887, 620, []), os.environ["PATH"])
    assert subprocess.run([sys.executable, str(BASE / "py" / "gpu_facts.py")], capture_output=True, text=True, env=env).stdout.strip().endswith("verdict=ours")
    empty = tmp_path / "empty-path"; empty.mkdir()                                    # no nvidia-smi at all: say so, never guess
    env["PATH"] = str(empty)                                                          # (2.0.29: a pod's container HAS /usr/bin/nvidia-smi — the real card answered there)
    assert subprocess.run([sys.executable, str(BASE / "py" / "gpu_facts.py")], capture_output=True, text=True, env=env).stdout.strip() == "verdict=no-gpu"


def test_unit_discovery_fails_the_run_when_the_gpu_is_not_ours():
    """The gate: a NOT-OURS line is a failed run with the remedy printed; an ours line is a fact; no answer is a note."""
    r = _bash('BASE_GPU_LINE="total=97887 used=95258 ours=1698 foreign=93560 procs=1 verdict=NOT-OURS"; _base_gpu_headroom; echo F=${#BASE_FAILED[@]}; printf "%s\\n" "${BASE_FAILED[@]}"')
    assert "F=1" in r.stdout and "91 GiB" in r.stdout and "OUTSIDE this container" in r.stdout and "stop and start the pod" in r.stdout, r.stdout + r.stderr
    r = _bash('BASE_GPU_LINE="total=97887 used=20000 ours=19500 foreign=500 procs=1 verdict=ours"; _base_gpu_headroom; echo F=${#BASE_FAILED[@]}')
    assert "F=0" in r.stdout and "the GPU is ours" in r.stdout, r.stdout + r.stderr
    r = _bash('BASE_GPU_LINE="verdict=no-gpu"; _base_gpu_headroom; echo F=${#BASE_FAILED[@]}')
    assert "F=0" in r.stdout and "not verifiable" in r.stdout, r.stdout + r.stderr


def test_unit_check_never_restamps_a_reused_real_venv(tmp_path):
    """A --check on the live pod rewrote the venv stamp (ts, base version, by=) through the reuse path: the stamp write there
    had no dry guard, and the fake-pod tests return before it. A dry run
    changes nothing; the real path still refreshes the stamp."""
    v = tmp_path / "venv"; (v / "bin").mkdir(parents=True)
    py = v / "bin" / "python"
    py.write_text('#!/bin/bash\ncase "$*" in *version_info*) echo 3.14.7;; *torch*) echo "torch 2.14.0 · CUDA 13.0";; *) exit 0;; esac\n'); py.chmod(0o755)
    stamp = v / ".comfy-base-venv"; before = "python=3.14.7 torch=2.14.0 base=0.0.1 ts=20260101-000000 by=pkg_a\n"; stamp.write_text(before)
    def run(dry):
        snippet = (f'VENV="{v}"; PY="{py}"; BASE_STATE="{tmp_path}"; BASE_DRY={dry}; BASE_NO_NET=0; '
                   '_base_ensure_uv(){ return 0; }; _base_python_pick(){ echo 3.14; }; _base_torch_pick(){ echo 2.14.0; }; '
                   '_base_venv_probe(){ return 0; }; _base_venv_owner(){ echo base; }; base_venv; echo "RESULT=$BASE_VENV_RESULT"')
        return _bash(snippet)
    r = run(1)
    assert "RESULT=reused" in r.stdout, r.stdout + r.stderr
    assert stamp.read_text() == before, "a --check rewrote the venv stamp: " + stamp.read_text()
    assert "would" in r.stdout and "stamp" in r.stdout, r.stdout
    r = run(0)
    assert "RESULT=reused" in r.stdout and stamp.read_text() != before and "by=base" in stamp.read_text(), r.stdout + r.stderr


def test_unit_check_mode_reports_a_full_disk_as_a_warning_not_a_failure(tmp_path):
    c = _pod(tmp_path)
    r = _bash(f'MODELS=("{ROW_A}"); base_env_setup; base_discover quiet; base_models; echo FAILED=${{#BASE_FAILED[@]}} WARN=${{#BASE_WARN[@]}}',
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path), "BASE_FAKE_FREE_GB": "0", "BASE_DRY": "1"})
    assert "FAILED=0 WARN=1" in r.stdout and "would stop here" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------- plan 3 / T1: the launched server keeps its Hugging Face cache on the volume

def test_unit_launch_line_puts_the_hf_cache_on_the_volume(tmp_path):
    c = _pod(tmp_path); (tmp_path / "runpod-slim").mkdir(); (tmp_path / "runpod-slim" / "comfyui_args.txt").write_text("")
    r = _bash('base_env_setup; base_discover quiet; _base_start_cmd; echo; echo "HOME_ENV=${HF_HOME:-unset}"', env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_PERSIST_ROOT_FORCE": "/workspace"})
    assert r.stdout.startswith("env HF_HOME=/workspace/huggingface ")   # env: the line is printed after nohup (2.0.11) and "main.py" in r.stdout, r.stdout + r.stderr
    r = _bash('base_env_setup; base_discover quiet; _base_start_cmd', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "HF_HOME" not in r.stdout                                  # off the pod there is no volume to prefer
    src = code_only((BASE / "lib" / "85-launch.sh").read_text())            # the launch line lives in 85-launch.sh (shared with boot.sh)
    assert "HF_HOME" in src[src.index("_base_start_comfy()"):]


def test_unit_sync_treats_declared_placeholders_as_intentional(tmp_path):
    c = _pod(tmp_path); (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "W.json").write_text(json.dumps(_wf([_node(1, "LoraLoaderModelOnly", ["Example/Subject/PUT-YOUR-TRAINED-LORA-HERE.safetensors", 1.0]),
                                                              _node(2, "VAELoader", ["really_missing.safetensors"])])))
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1"}
    r = _bash(f'PKG_DIR="{tmp_path}/pkg"; WF_NAME="W.json"; MODELS=(); PLACEHOLDERS=(PUT-YOUR-TRAINED-LORA-HERE.safetensors); base_env_setup; base_discover quiet; base_sync; echo FAILED=${{#BASE_FAILED[@]}} RESULT=$BASE_SYNC_RESULT', env=env)
    assert "PLACEHOLDER" in r.stdout and "placeholders=1" in r.stdout and "active_missing=1" in r.stdout and "FAILED=1" in r.stdout, r.stdout + r.stderr
    (tmp_path / "pkg" / "W.json").write_text(json.dumps(_wf([_node(1, "LoraLoaderModelOnly", ["Example/Subject/PUT-YOUR-TRAINED-LORA-HERE.safetensors", 1.0])])))
    r = _bash(f'PKG_DIR="{tmp_path}/pkg"; WF_NAME="W.json"; MODELS=(); PLACEHOLDERS=(PUT-YOUR-TRAINED-LORA-HERE.safetensors); base_env_setup; base_discover quiet; base_sync; echo FAILED=${{#BASE_FAILED[@]}} RESULT=$BASE_SYNC_RESULT', env=env)
    assert "FAILED=0" in r.stdout and "placeholders=1" in r.stdout and "PLACEHOLDER" in r.stdout, r.stdout + r.stderr


def test_unit_sync_checks_a_local_labelled_value_without_rewriting_it(tmp_path):
    wf = tmp_path / "w.json"
    wf.write_text(json.dumps(_wf([_node(1, "AILab_QwenVL_GGUF_PromptEnhancer", ["[local] Qwen3-4B-instruct-bf16.gguf", "prompt"]),
                                    _node(2, "AILab_QwenVL_GGUF_PromptEnhancer", ["[local] nowhere.gguf", "prompt"])])))
    rows = tmp_path / "rows.tsv"; rows.write_text("LLM\t\tGGUF\tQwen3-4B-instruct-bf16.gguf\n")
    cats = tmp_path / "cats.tsv"; cats.write_text("AILab_QwenVL_GGUF_PromptEnhancer\tLLM\n"); (tmp_path / "models").mkdir()
    r = _sync(wf, tmp_path / "models", rows, cats)
    out = json.loads(wf.read_text())
    assert out["nodes"][0]["widgets_values"][0] == "[local] Qwen3-4B-instruct-bf16.gguf"     # a pack label, not a path: untouched
    assert "SYNC fixed=0 active_missing=1" in r.stdout and "nowhere.gguf" in r.stdout, r.stdout


# ---------------------------------------------------------------- plan 5: snapshots, flat embeddings, extras on every run
def test_unit_dir_size_ignores_the_download_cache(tmp_path):
    """`hf download --local-dir` writes .cache/huggingface/ beside the files; a snapshot verified by total size must not count it."""
    d = tmp_path / "snap"; (d / ".cache" / "huggingface").mkdir(parents=True); (d / "sub").mkdir()
    (d / "a.bin").write_bytes(b"1234"); (d / "sub" / "b.bin").write_bytes(b"56"); (d / ".cache" / "huggingface" / "meta").write_bytes(b"xyz")
    # a pack that imports code from its snapshot (ComfyUI-RMBG's birefnet.py) leaves __pycache__ behind after the first
    # render; the byte total then no longer equals the declared size and the base re-fetched a complete snapshot (2.0.25)
    (d / "__pycache__").mkdir(); (d / "__pycache__" / "birefnet.cpython-314.pyc").write_bytes(b"pyc-bytes"); (d / "sub" / "__pycache__").mkdir(); (d / "sub" / "__pycache__" / "x.pyc").write_bytes(b"!")
    assert _bash(f'_base_dir_size "{d}"').stdout.strip() == "6"
    assert _bash('_base_dir_size /nonexistent').stdout.strip() == "0"


def test_unit_embeddings_are_a_flat_freeform_category():
    """ComfyUI resolves `embedding:<name>` in the embeddings folder root only (sd1_clip.load_embed joins dir + name), so no Family."""
    row = "embeddings|||example_x.safetensors|https://huggingface.co/x/y/resolve/main/e.safetensors|512|style|"
    assert "rc=0" in _bash(f'MODELS=("{row}"); _base_model_rows >/dev/null; echo rc=$?').stdout
    assert _bash(f'_base_dest_rel "{row}"').stdout.strip() == "embeddings/example_x.safetensors"
    r = _bash('MODELS=("embeddings|Example-V||e.safetensors|https://huggingface.co/x/y/resolve/main/e.safetensors|5|no|"); _base_model_rows >/dev/null; echo rc=$?')
    assert "rc=1" in r.stdout and "freeform" in r.stderr
    # the packs that os.listdir their folder (SAM3 SmartInpainter, DepthAnythingV2, SeedVR2, ComfyUI-RMBG's models/RMBG/<name>/,
    # LayerStyle's models/grounding-dino/) get flat categories too (RMBG, grounding-dino: 2.0.17)
    for cat in ("sams", "depthanything", "SEEDVR2", "RMBG", "grounding-dino"):
        assert "rc=0" in _bash(f'MODELS=("{cat}|||m.pt|https://huggingface.co/x/y/resolve/main/m.pt|9|flat|"); _base_model_rows >/dev/null; echo rc=$?').stdout, cat
        assert _bash(f'_base_dest_rel "{cat}|||m.pt|https://huggingface.co/x/y/resolve/main/m.pt|9|flat|"').stdout.strip() == f"{cat}/m.pt"
    assert _bash('_base_dest_rel "RMBG|||RMBG-2.0/|hf://1038lab/RMBG-2.0|884972398|snapshot|"').stdout.strip() == "RMBG/RMBG-2.0/"
    r = _bash('MODELS=("RMBG|RMBG||RMBG-2.0/|hf://1038lab/RMBG-2.0|884972398|snapshot|"); _base_model_rows >/dev/null; echo rc=$?')
    assert "rc=1" in r.stdout and "freeform" in r.stderr, "RMBG with a Family must be refused: the pack reads models/RMBG/<name>/"


SNAP_ROW = "LLM||Qwen-VL|Qwen3-VL-32B-Std-v2/|LOCAL|0|the describers' vision LLM; its Hub repo is gone|"



def test_unit_latent_upscale_models_is_a_flat_freeform_category():
    """ComfyUI resolves `embedding:<name>` in the latent_upscale_models folder root only (sd1_clip.load_embed joins dir + name), so no Family."""
    row = "latent_upscale_models|||example_x.safetensors|https://huggingface.co/x/y/resolve/main/e.safetensors|512|style|"
    assert "rc=0" in _bash(f'MODELS=("{row}"); _base_model_rows >/dev/null; echo rc=$?').stdout
    assert _bash(f'_base_dest_rel "{row}"').stdout.strip() == "latent_upscale_models/example_x.safetensors"
    r = _bash('MODELS=("latent_upscale_models|Example-V||e.safetensors|https://huggingface.co/x/y/resolve/main/e.safetensors|5|no|"); _base_model_rows >/dev/null; echo rc=$?')
    assert "rc=1" in r.stdout and "freeform" in r.stderr
    # the packs that os.listdir their folder (SAM3 SmartInpainter, DepthAnythingV2, SeedVR2, ComfyUI-RMBG's models/RMBG/<name>/,
    # LayerStyle's models/grounding-dino/) get flat categories too (RMBG, grounding-dino: 2.0.17)
    for cat in ("sams", "depthanything", "SEEDVR2", "RMBG", "grounding-dino"):
        assert "rc=0" in _bash(f'MODELS=("{cat}|||m.pt|https://huggingface.co/x/y/resolve/main/m.pt|9|flat|"); _base_model_rows >/dev/null; echo rc=$?').stdout, cat
        assert _bash(f'_base_dest_rel "{cat}|||m.pt|https://huggingface.co/x/y/resolve/main/m.pt|9|flat|"').stdout.strip() == f"{cat}/m.pt"
    assert _bash('_base_dest_rel "RMBG|||RMBG-2.0/|hf://1038lab/RMBG-2.0|884972398|snapshot|"').stdout.strip() == "RMBG/RMBG-2.0/"
    r = _bash('MODELS=("RMBG|RMBG||RMBG-2.0/|hf://1038lab/RMBG-2.0|884972398|snapshot|"); _base_model_rows >/dev/null; echo rc=$?')
    assert "rc=1" in r.stdout and "freeform" in r.stderr, "RMBG with a Family must be refused: the pack reads models/RMBG/<name>/"


SNAP_ROW = "LLM||Qwen-VL|Qwen3-VL-32B-Std-v2/|LOCAL|0|the describers' vision LLM; its Hub repo is gone|"

def test_unit_a_local_snapshot_folder_is_relocated_from_anywhere_on_the_pod(tmp_path):
    """A LOCAL row whose file ends in / is a FOLDER: found by name (holding a config.json) through the one directory scan, moved into place."""
    c = _pod(tmp_path); src = tmp_path / "old-install" / "models" / "LLM" / "Qwen3-VL-32B-Std-v2"; src.mkdir(parents=True)
    (src / "config.json").write_text("{}"); (src / "model-00001.safetensors").write_bytes(b"\0" * 4096)
    r = _bash(f'MODELS=("{SNAP_ROW}"); base_env_setup; base_discover quiet; base_models; echo FAILED=${{#BASE_FAILED[@]}} MOVED=${{#MODEL_MOVED[@]}}; ls "$M/LLM/Qwen-VL/Qwen3-VL-32B-Std-v2"',
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)})
    assert "FAILED=0 MOVED=1" in r.stdout and "config.json" in r.stdout and not src.exists(), r.stdout + r.stderr
    # second run: in place, verified by the config.json, nothing moved
    r = _bash(f'MODELS=("{SNAP_ROW}"); base_env_setup; base_discover quiet; base_models; echo FAILED=${{#BASE_FAILED[@]}} MOVED=${{#MODEL_MOVED[@]}} OK=${{#MODEL_OK[@]}}',
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)})
    assert "FAILED=0 MOVED=0 OK=1" in r.stdout, r.stdout + r.stderr


def test_unit_a_missing_local_snapshot_fails_the_run_by_name_and_check_only_reports(tmp_path):
    c = _pod(tmp_path); (tmp_path / "decoy" / "Qwen3-VL-32B-Std-v2").mkdir(parents=True)     # a folder of that name WITHOUT config.json is not it
    for dry in ("", "BASE_DRY=1;"):
        r = _bash(f'{dry} MODELS=("{SNAP_ROW}"); base_env_setup; base_discover quiet; base_models; echo FAILED=${{#BASE_FAILED[@]}}; printf "%s\\n" "${{BASE_FAILED[@]}}"',
                  env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_SEARCH_ROOTS": str(tmp_path)})
        assert "FAILED=1" in r.stdout and "Qwen3-VL-32B-Std-v2" in r.stdout and "LLM/Qwen-VL" in r.stdout, r.stdout + r.stderr
    assert not (c / "models" / "LLM").exists()


def test_unit_pip_extra_is_installed_every_run_not_only_on_a_rebuild():
    """A venv the base built for another package (or before any package) has never seen this package's PIP_EXTRA."""
    body = code_only(_src(BASE / "lib" / "40-packs.sh"))
    pip_half = body[body.index('hdr "NODE PACKS · requirements'):]
    assert 'PIP_EXTRA[@]' in pip_half and "--extra-index-url https://pypi.nvidia.com" in pip_half, "the pip stage must install PIP_EXTRA"
    tail = pip_half[pip_half.index("extras="):][:900]
    assert "BASE_DRY" in tail and "BASE_NO_NET" in tail, "dry-run and fake runs must not pip"


def test_unit_sync_rewrites_a_bypassed_loader_whose_file_has_a_row_and_keeps_a_compact_file(tmp_path):
    """A mode radio ships every option but one bypassed (quality tiers, engine variants); the user flips them later, so a
    bypassed loader's path must be canonical too. Only a name that resolves to NOTHING is treated by mode. And a one-line
    workflow stays one line."""
    wf = tmp_path / "w.json"
    wf.write_text(json.dumps(_wf([_node(1, "UNETLoader", ["example_engine_pruned_int8.safetensors", "default"]),
                                    _node(2, "UNETLoader", ["example_engine_bf16.safetensors", "default"], mode=4),
                                    _node(3, "LoraLoaderModelOnly", ["Other/turbo.safetensors", 1], mode=4)]), ensure_ascii=False) + "\n")
    rows = tmp_path / "rows.tsv"; rows.write_text("diffusion_models\tExample-V\t\texample_engine_pruned_int8.safetensors\ndiffusion_models\tExample-V\t\texample_engine_bf16.safetensors\n")
    cats = tmp_path / "cats.tsv"; cats.write_text("UNETLoader\tdiffusion_models\nLoraLoaderModelOnly\tloras\n"); (tmp_path / "models").mkdir()
    r = _sync(wf, tmp_path / "models", rows, cats)
    assert r.returncode == 0, r.stdout + r.stderr
    text = wf.read_text(); out = json.loads(text)
    assert out["nodes"][1]["widgets_values"][0] == "Example-V/example_engine_bf16.safetensors", "the bypassed Master engine must be rewritten"
    assert out["nodes"][2]["widgets_values"][0] == "Other/turbo.safetensors", "no row: untouched"
    assert "SYNC fixed=2 active_missing=0 bypassed_missing=1" in r.stdout, r.stdout
    assert text.count("\n") == 1 and text.startswith('{"'), "a compact workflow must stay compact"


# ---------------------------------------------------------------- T11: the three fake pod layouts (base V2.0.0)

def test_unit_fake_pod_layouts_official_community_bare(tmp_path):
    """official = RunPod's ComfyUI image (tree on the volume, template venv, args file); community = code on the
    container disk (<root>/image/ComfyUI), persist dirs on the volume (<root>/ComfyUI without main.py); bare = an
    empty volume. Every layout carries PID 1's env in <root>/pid1.env and run_script hands both facts to the script."""
    from basetest import fake_pod, run_script
    STUB = _stub()
    off = fake_pod(tmp_path / "off", STUB, layout="official")
    assert (off / "main.py").exists() and (off / ".venv-cu130" / "bin" / "python").exists() and (tmp_path / "off" / "runpod-slim" / "comfyui_args.txt").exists()
    hm = fake_pod(tmp_path / "hm", STUB, layout="community")
    assert hm == tmp_path / "hm" / "ComfyUI" and not (hm / "main.py").exists()
    for d in ("models", "user", "output", "input", "custom_nodes"):
        assert (hm / d).is_dir()
    img = tmp_path / "hm" / "image"
    assert (img / "ComfyUI" / "main.py").exists() and (img / "comfyui-runtime" / "src" / "start.sh").exists()
    assert (img / "comfyui-template" / "template.json").exists() and (img / "opt" / "venv" / "bin" / "python3").exists()
    assert (hm / "models" / "diffusion_models" / "example_engine_pruned_int8.safetensors").exists()   # the template's own file, unclaimed by any row
    bare = fake_pod(tmp_path / "bare", STUB, layout="bare")
    assert bare == tmp_path / "bare" / "ComfyUI" and not bare.exists()
    for root in (tmp_path / "off", tmp_path / "hm", tmp_path / "bare"):
        env = (root / "pid1.env").read_text()
        assert "PUBLIC_KEY=" in env and "HF_TOKEN=token_here" in env and "SOME_IDS=" in env and "JUPYTER_PASSWORD=" in env
        probe = root / "pkg" / "probe.sh"
        probe.write_text('#!/bin/bash\necho "PID1=$BASE_FAKE_PID1_ENV IMG=${BASE_FAKE_IMAGE_ROOT:-none}"\n'); probe.chmod(0o755)
        r = run_script(probe, fake_root=root)
        assert r.returncode == 0 and ("PID1=%s" % (root / "pid1.env")) in r.stdout, r.stdout + r.stderr
        assert ("IMG=%s" % (root / "image")) in r.stdout if root.name == "hm" else "IMG=none" in r.stdout


# ---------------------------------------------------------------- T12: discovery on the volume only (base V2.0.0)

def test_unit_discover_never_adopts_a_container_disk_tree(tmp_path):
    """community layout: the only main.py is on the container disk (<root>/image/ComfyUI). --check must not adopt it
    and must say what a real run does: materialise the tree beside the persist dirs on the volume."""
    from basetest import fake_pod, run_script
    STUB = _stub()
    pod = fake_pod(tmp_path, STUB, layout="community")
    r = run_script(STUB / "stub-script.sh", "--check", fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
    assert "would materialise" in r.stdout and str(pod) in r.stdout
    assert str(tmp_path / "image" / "ComfyUI") not in r.stdout.split("would materialise")[0]   # never a candidate
    assert not (pod / "main.py").exists()
    # a real run puts the code beside the persist dirs and leaves them untouched
    r = run_script(STUB / "stub-script.sh", fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-1000:]
    assert (pod / "main.py").exists() and (pod / "comfyui_version.py").exists()
    assert (pod / "models" / "diffusion_models" / "example_engine_pruned_int8.safetensors").exists()
    assert ("tree         %s" % pod) in r.stdout or ("ComfyUI      %s" % pod) in r.stdout or str(pod) in r.stdout


def test_unit_discover_records_the_canonical_tree_and_prefers_it_over_a_second_one(tmp_path):
    """Two trees on one volume (a volume that met two templates): the one recorded in state/boot.env is canonical,
    the other is reported, never deleted."""
    from basetest import fake_pod, run_script, _fake_code_tree
    STUB = _stub()
    pod = fake_pod(tmp_path, STUB, layout="official")                          # <root>/ComfyUI
    second = tmp_path / "runpod-slim" / "ComfyUI"; _fake_code_tree(second, "0.34.7")
    state = tmp_path / "comfy-base" / "state"; state.mkdir(parents=True, exist_ok=True)
    (state / "boot.env").write_text("COMFY=%s\n" % second)
    r = run_script(STUB / "stub-script.sh", "--check", fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
    assert ("ComfyUI      %s" % second) in r.stdout or ("tree         %s" % second) in r.stdout, r.stdout[:2500]
    assert "other tree" in r.stdout and str(pod) in r.stdout.split("other tree")[1][:300]
    assert (pod / "main.py").exists() and (second / "main.py").exists()


def test_unit_a_pod_without_a_network_volume_is_refused(tmp_path):
    from basetest import fake_pod, run_script
    STUB = _stub()
    fake_pod(tmp_path, STUB, layout="official")
    r = run_script(STUB / "stub-script.sh", "--check", fake_root=tmp_path, env={"BASE_FAKE_NO_VOLUME": "1"})
    assert r.returncode == 3, r.stdout[-2000:] + r.stderr[-1000:]
    assert "network volume" in r.stdout + r.stderr and "stop/start" in r.stdout + r.stderr


# ---------------------------------------------------------------- T13: tokens (base V2.0.0)

def _stub_with_github_row(root):
    """The copied stub gains one model served by GitHub (a release asset), so the curl leg is exercised."""
    sc = root / "pkg" / "stub-script.sh"; s = sc.read_text()
    s = s.replace('MODELS=(\n', 'MODELS=(\n "loras|Example|Real|stub_gh.safetensors|https://github.com/x/y/releases/download/v1/stub_gh.safetensors|2048|a release asset|"\n', 1)
    assert "stub_gh" in s
    sc.write_text(s)


def _run_logs(root):
    return [p.read_text(errors="replace") for p in (root / "comfy-base" / "state" / "logs").glob("*.log")]


def test_unit_tokens_come_from_pid1_env_with_aliases_and_placeholders_are_named_not_used(tmp_path):
    """pid1.env carries HF_TOKEN=token_here (a placeholder) and HF_HUB_TOKEN=abc-secret (an alias other templates use).
    The placeholder is named and skipped; the alias becomes HF_TOKEN; no value ever reaches stdout or the log."""
    from basetest import fake_pod, run_script
    STUB = _stub()
    fake_pod(tmp_path, STUB, layout="official")
    with open(tmp_path / "pid1.env", "a") as f: f.write("HF_HUB_TOKEN=abc-secret\n")
    r = run_script(STUB / "stub-script.sh", "--check", fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
    assert "HF_TOKEN" in r.stdout and "placeholder" in r.stdout and "token_here" in r.stdout
    assert "HF_TOKEN present (pod env: HF_HUB_TOKEN)" in r.stdout
    assert "abc-secret" not in r.stdout + r.stderr and not any("abc-secret" in l for l in _run_logs(tmp_path))
    tokens = tmp_path / "comfy-base" / "state" / "tokens.env"
    assert not tokens.exists()                                             # --check stores nothing
    r2 = run_script(STUB / "stub-script.sh", fake_root=tmp_path)
    assert r2.returncode == 0, r2.stdout[-3000:] + r2.stderr[-1000:]
    assert tokens.read_text() == "HF_TOKEN=abc-secret\n" and (tokens.stat().st_mode & 0o777) == 0o600
    assert "abc-secret" not in r2.stdout + r2.stderr and not any("abc-secret" in l for l in _run_logs(tmp_path))


def test_unit_token_rejected_by_its_service_fails_the_run_before_models(tmp_path):
    """A token its service answers 401 to fails the run before any model is fetched, naming the SOURCE, never the value."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from basetest import fake_pod, run_script
    STUB = _stub()
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            self.send_response(401); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")
    srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        pod = fake_pod(tmp_path, STUB, layout="official")
        r = run_script(STUB / "stub-script.sh", fake_root=tmp_path,
                       env={"HF_TOKEN": "hf_real_looking_value", "BASE_TOKEN_CHECK_URL_HF": "http://127.0.0.1:%d/whoami" % srv.server_address[1]})
    finally:
        srv.shutdown(); srv.server_close()
    assert r.returncode != 0, r.stdout[-3000:]
    assert "HF_TOKEN" in r.stdout and "rejected" in r.stdout and "401" in r.stdout and "(env)" in r.stdout
    assert not (pod / "models" / "vae" / "Example" / "Real" / "stub_vae.safetensors").exists()      # nothing was fetched
    assert "hf_real_looking_value" not in r.stdout + r.stderr and not any("hf_real_looking_value" in l for l in _run_logs(tmp_path))


def test_unit_hf_gets_the_token_off_the_process_list(tmp_path):
    """hf sees HF_TOKEN in its environment, never --token. BASE_FAKE_DL=real routes the fake run through the real
    download branches with a stubbed hf on PATH."""
    from basetest import fake_pod, run_script
    STUB = _stub()
    pod = fake_pod(tmp_path, STUB, layout="official")
    bin_ = tmp_path / "bin"; bin_.mkdir(); log = tmp_path / "calls.log"
    (bin_ / "hf").write_text('#!/bin/bash\necho "hf argv: $*" >> "%s"; echo "hf env HF_TOKEN=${HF_TOKEN:-unset}" >> "%s"\n'
                             'd=""; while [ $# -gt 0 ]; do case "$1" in --local-dir) d="$2"; shift;; esac; shift; done\n'
                             'mkdir -p "$d"; python3 -c "open(\\"$d/stub_vae.safetensors\\",\\"wb\\").truncate(4096)"\n' % (log, log))
    (bin_ / "curl").write_text('#!/bin/bash\necho "curl argv: $*" >> "%s"\nexit 0\n' % log)
    for p in (bin_ / "hf", bin_ / "curl"): p.chmod(0o755)
    r = run_script(STUB / "stub-script.sh", fake_root=tmp_path,
                   env={"PATH": "%s:%s" % (bin_, os.environ["PATH"]), "BASE_FAKE_DL": "real", "HF_TOKEN": "hf_env_value"})
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-1000:]
    calls = log.read_text()
    assert "hf env HF_TOKEN=hf_env_value" in calls and "--token" not in calls and "hf_env_value" not in "".join(l for l in calls.splitlines() if "argv" in l)
    assert (pod / "models" / "vae" / "Example" / "Real" / "stub_vae.safetensors").stat().st_size == 4096
    assert "hf_env_value" not in r.stdout + r.stderr and not any("hf_env_value" in l for l in _run_logs(tmp_path))


# ---------------------------------------------------------------- T14: the base owns the launch line (base V2.0.0)

@pytest.mark.parametrize("layout", ["official", "community", "bare", "volume"])
def test_unit_args_file_is_base_owned_on_every_layout_and_imports_the_official_file_once(tmp_path, layout):
    from basetest import fake_pod, run_script
    STUB = _stub()
    fake_pod(tmp_path, STUB, layout=layout)
    r = run_script(STUB / "stub-script.sh", fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-1000:]
    b = _base_args(tmp_path); txt = b.read_text()
    assert txt.startswith("#") and "--preview-method auto" in txt and "--preview-size 1024" in txt and "--disable-api-nodes" in txt
    assert "--use-sage-attention" not in txt
    if layout == "official":
        assert "imported" in r.stdout and (tmp_path / "runpod-slim" / "comfyui_args.txt").read_text() == "# template args\n--use-sage-attention\n"
    else:
        assert not (tmp_path / "runpod-slim").exists()                                        # never a phantom
    r2 = run_script(STUB / "stub-script.sh", fake_root=tmp_path)
    assert r2.returncode == 0 and b.read_text() == txt and "imported" not in r2.stdout.split("SUMMARY")[0]


def test_unit_hygiene_remove_entries_never_reappear(tmp_path):
    """After the one-time import the template's file has no say: a flag that re-appears there (an image bump) does
    not come back, and HYGIENE_ARGS_REMOVE keeps the base file clean of it on every run."""
    c = _pod(tmp_path); (tmp_path / "runpod-slim").mkdir(); a = tmp_path / "runpod-slim" / "comfyui_args.txt"; a.write_text("--fast\n")
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1"}
    _bash('base_env_setup; base_discover quiet; base_hygiene', env=env)
    a.write_text("--fast\n--use-sage-attention\n--evil\n")
    r = _bash('HYGIENE_ARGS_REMOVE=(--evil); base_env_setup; base_discover quiet; base_hygiene; _base_start_cmd', env=env)
    txt = _base_args(tmp_path).read_text()
    assert "--use-sage-attention" not in txt and "--evil" not in txt and "--fast" in txt, txt + r.stdout
    assert "--evil" not in r.stdout.splitlines()[-1] and "--fast" in r.stdout.splitlines()[-1]


def test_unit_launch_line_is_composed_from_the_args_file_only(tmp_path):
    c = _pod(tmp_path)
    b = _base_args(tmp_path); b.parent.mkdir(parents=True)
    b.write_text("# comfy-base launch args\n--preview-method taesd\n--fast\n")
    (tmp_path / "runpod-slim").mkdir(); (tmp_path / "runpod-slim" / "comfyui_args.txt").write_text("--port 9999\n--evil\n")
    r = _bash('base_env_setup; base_discover quiet; _base_start_cmd; echo; echo PORT=$PORT', env={"BASE_FAKE_ROOT": str(tmp_path)})
    line = r.stdout.splitlines()[0]
    assert "--fast" in line and "taesd" in line and line.count("--preview-method") == 1 and "--preview-size" in line, line
    assert "--evil" not in line and "--port 8188" in line and "PORT=8188" in r.stdout                # the template's file is not consulted once the base's exists


def test_unit_no_phantom_runpod_slim_is_ever_created(tmp_path):
    from basetest import fake_pod, run_script
    STUB = _stub()
    for layout in ("community", "bare"):
        root = tmp_path / layout; fake_pod(root, STUB, layout=layout)
        r = run_script(STUB / "stub-script.sh", fake_root=root)
        assert r.returncode == 0, r.stdout[-3000:]
        assert not (root / "runpod-slim").exists() and (root / "comfy-base" / "state" / "comfyui_args.txt").exists()


# ---------------------------------------------------------------- T15: the base boots the pod (base V2.0.0)

def _boot_stubs(tmp_path, torch_ok=True):
    from basetest import boot_stubs
    return boot_stubs(tmp_path, torch_ok=torch_ok)


def _run_boot_sh(tmp_path, bin_, extra_env=None):
    from basetest import boot_fake_pod
    return boot_fake_pod(tmp_path, bin_, extra_env)


def test_unit_boot_env_is_written_by_every_run_and_read_by_boot_sh(tmp_path):
    from basetest import fake_pod, run_script
    STUB = _stub()
    pod = fake_pod(tmp_path, STUB, layout="official")
    r = run_script(STUB / "stub-script.sh", "--check", fake_root=tmp_path)
    assert r.returncode == 0 and not (tmp_path / "comfy-base" / "state" / "boot.env").exists() and "would" in r.stdout
    r = run_script(STUB / "stub-script.sh", fake_root=tmp_path)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-1000:]
    env = (tmp_path / "comfy-base" / "state" / "boot.env").read_text()
    assert ("COMFY='%s'" % pod) in env and ("VENV='%s'" % (pod / ".venv-cu130")) in env and "PORT='8188'" in env
    assert ("ARGS_FILE='%s'" % (tmp_path / "comfy-base" / "state" / "comfyui_args.txt")) in env
    assert (tmp_path / "comfy-base" / "boot.sh").exists() and (tmp_path / "comfy-base" / "lib" / "85-launch.sh").exists()
    assert "boot       " in r.stdout and "tools" in r.stdout.split("boot       ")[1].split("\n")[0]


def test_unit_boot_sh_starts_sshd_then_jupyter_then_comfyui_and_never_exits(tmp_path):
    from basetest import fake_pod, run_script
    STUB = _stub()
    pod = fake_pod(tmp_path, STUB, layout="official")
    assert run_script(STUB / "stub-script.sh", fake_root=tmp_path).returncode == 0
    bin_, log = _boot_stubs(tmp_path)
    r, blog = _run_boot_sh(tmp_path, bin_, {"JUPYTER_PASSWORD": "pw-secret"})
    assert r.returncode == 0, r.stderr + blog
    calls = log.read_text()
    # the boot log is written in order (JupyterLab is backgrounded, so its stub's line may land after ComfyUI's)
    assert blog.index("boot: sshd up") < blog.index("boot: JupyterLab starting") < blog.index("boot: starting ComfyUI"), blog
    assert calls.splitlines()[0].startswith(("ssh-keygen", "sshd", "pgrep")) and "jupyter-lab " in calls and "python main.py" in calls, calls
    assert "JUPYTER_TOKEN=pw-secret" in calls and "--ServerApp.token" not in calls and "pw-secret" not in blog     # env, not argv; never logged
    launch = [ln for ln in calls.splitlines() if ln.startswith("python main.py")][0]
    want = _bash("base_env_setup; base_discover quiet; _base_start_cmd", env={"BASE_FAKE_ROOT": str(tmp_path)}).stdout.strip()
    assert launch == "python " + want.split(" ", 1)[1], (launch, want)                        # the SAME line the base's restart uses
    assert "--preview-method auto" in launch and "--disable-api-nodes" in launch and "--listen 0.0.0.0" in launch
    assert "boot: done" in blog and "sleeping" in blog
    # sleep infinity only when not BOOT_ONCE: the script's last statement
    src = (BASE / "lib" / "boot.sh").read_text()
    assert "sleep infinity" in src and "trap 'exit 0' TERM" in src


def test_unit_boot_sh_refuses_to_start_a_venv_that_cannot_import_torch_and_says_so(tmp_path):
    from basetest import fake_pod, run_script
    STUB = _stub()
    fake_pod(tmp_path, STUB, layout="official")
    assert run_script(STUB / "stub-script.sh", fake_root=tmp_path).returncode == 0
    bin_, log = _boot_stubs(tmp_path, torch_ok=False)
    r, blog = _run_boot_sh(tmp_path, bin_)
    assert r.returncode == 0 and "cannot import torch" in blog and "rescue" in blog
    assert "main.py" not in log.read_text()
    # and with no boot.env at all (a volume the base never installed on): sshd and jupyter still come up
    (tmp_path / "comfy-base" / "state" / "boot.env").unlink()
    r, blog = _run_boot_sh(tmp_path, bin_)
    assert r.returncode == 0 and "no " in blog and "boot.env" in blog and "sshd" in log.read_text()


def test_unit_boot_sh_never_starts_jupyter_tokenless_and_no_sshd_without_a_key(tmp_path):
    """2.1.0: a stranger's pod from the public template sets neither JUPYTER_TOKEN nor PUBLIC_KEY (RunPod's toggles exist only
    on official templates, distribution plan blocker 2). The old tokenless start was root on the box; now nothing listens."""
    from basetest import fake_pod, run_script
    STUB = _stub()
    fake_pod(tmp_path, STUB, layout="official")
    assert run_script(STUB / "stub-script.sh", fake_root=tmp_path).returncode == 0
    bin_, log = _boot_stubs(tmp_path)
    r, blog = _run_boot_sh(tmp_path, bin_)                       # PUBLIC_KEY set by the helper, no JUPYTER_*
    assert r.returncode == 0 and "JupyterLab not started" in blog and "never tokenless" in blog, blog
    assert "jupyter-lab " not in log.read_text() and "--IdentityProvider.token=" not in log.read_text()
    assert "python main.py" in log.read_text()                    # ComfyUI still comes up
    src = (BASE / "lib" / "boot.sh").read_text()
    assert "IdentityProvider.token=''" not in code_only(src)      # the tokenless line is gone from the code, not just unreachable
    # no PUBLIC_KEY: no sshd at all (a listener nobody can log in to is a port with no purpose)
    (tmp_path / "sshd.running").unlink(missing_ok=True); log.write_text("")
    r, blog = _run_boot_sh(tmp_path, bin_, {"PUBLIC_KEY": ""})
    assert r.returncode == 0 and "sshd not started" in blog and "PUBLIC_KEY" in blog, blog
    assert not [ln for ln in log.read_text().splitlines() if ln.startswith("sshd ")], log.read_text()
    assert "python main.py" in log.read_text()


def test_unit_boot_sh_exports_the_button_names_and_loads_saved_tokens_without_logging_values(tmp_path):
    """2.1.0: boot_tokens — state/tokens.env joins the pod's own environment (the pod's value wins), and the Hub browser
    pack gets its own name; no value ever reaches the boot log or the ComfyUI stub's argv."""
    from basetest import fake_pod, run_script
    STUB = _stub()
    fake_pod(tmp_path, STUB, layout="official")
    assert run_script(STUB / "stub-script.sh", fake_root=tmp_path).returncode == 0
    st = tmp_path / "comfy-base" / "state"; st.mkdir(parents=True, exist_ok=True)
    (st / "tokens.env").write_text("HF_TOKEN=hf_saved_value\nOTHER_SAVED=other_saved_value\n")
    bin_, log = _boot_stubs(tmp_path)
    # the python stub records its environment for this test only
    py = tmp_path / "ComfyUI" / ".venv-cu130" / "bin" / "python"
    py.write_text(py.read_text().replace('echo "python $*"', 'echo "python $* ENV hf=${HUGGINGFACE_API_KEY-unset} oth=${OTHER_SAVED-unset} tok=${HF_TOKEN-unset}"'))
    r, blog = _run_boot_sh(tmp_path, bin_, {"HF_TOKEN": "hf_pod_value"})
    assert r.returncode == 0, r.stderr + blog
    assert "boot: tokens" in blog and "HUGGINGFACE_API_KEY" in blog, blog
    assert "hf_pod_value" not in blog and "other_saved_value" not in blog and "hf_saved_value" not in blog, blog
    launch = [ln for ln in log.read_text().splitlines() if ln.startswith("python main.py")][0]
    assert "hf=hf_pod_value" in launch and "tok=hf_pod_value" in launch and "oth=other_saved_value" in launch, launch   # the pod's HF_TOKEN won; the other saved one loaded


# ---------------------------------------------------------------- T16: restart finds ComfyUI by what it runs (base V2.0.0)

def test_unit_comfy_pids_match_by_argv_or_cwd_and_never_pid1(tmp_path):
    """_base_pids_from_table reads `pid|ppid|cwd|argv` lines (what /proc gives on the pod) and prints the pids of
    ComfyUI servers: a python running main.py from $COMFY (by cwd), or one naming a …/ComfyUI/main.py path (an image's
    own launch, whatever its cwd). PID 1 is never returned, only reported."""
    comfy = tmp_path / "ComfyUI"; comfy.mkdir()
    table = "\n".join([
        "1|0|/|bash /comfyui-runtime/src/start.sh /comfyui-template",
        "100|1|/|python3 /ComfyUI/main.py --listen --enable-cors-header *",         # the image's launch: argv names the tree
        "200|50|%s|python main.py --listen 0.0.0.0 --port 8188" % comfy,             # ours: cwd is the canonical tree
        "300|50|/tmp|python other.py main.py",                                       # not a ComfyUI server
        "400|50|/tmp|/usr/bin/python3 /somewhere/else/app.py",
        "500|1|/root|bash -c sleep infinity",
    ])
    r = _bash('COMFY="%s"; printf "%%s\\n" "%s" | _base_pids_from_table' % (comfy, table.replace('"', '\\"')))
    assert r.stdout.split() == ["100", "200"], r.stdout + r.stderr
    # PID 1 running ComfyUI itself (a bare image whose CMD is python main.py): reported, never killed
    table2 = "1|0|%s|python main.py --listen" % comfy
    r = _bash('COMFY="%s"; printf "%%s\\n" "%s" | _base_pids_from_table' % (comfy, table2))
    assert r.stdout.split() == [] and "PID 1" in r.stdout + r.stderr, r.stdout + r.stderr


def test_unit_restart_uses_the_same_launch_function_as_boot():
    lib = BASE / "lib"
    defs = {p.name: code_only(p.read_text()) for p in lib.glob("*.sh")}
    assert sum("_base_start_cmd()" in t for t in defs.values()) == 1 and "_base_start_cmd()" in defs["85-launch.sh"]
    assert sum("_base_start_comfy()" in t for t in defs.values()) == 1 and "_base_start_comfy()" in defs["85-launch.sh"]
    assert "_base_start_comfy" in defs["80-server.sh"][defs["80-server.sh"].index("base_restart()"):]
    assert "85-launch.sh" in defs["boot.sh"] and "_base_start_comfy" in defs["boot.sh"][defs["boot.sh"].index("boot_comfy()"):]
    # the real /proc walk feeds the same matcher (no second opinion on what a ComfyUI process is)
    body = defs["80-server.sh"][defs["80-server.sh"].index("_base_comfy_pids()"):defs["80-server.sh"].index("base_import_check()")]
    assert "_base_pids_from_table" in body


# ---------------------------------------------------------------- T17: packs and the scan root (base V2.0.0)

def test_unit_base_packs_are_six_and_pinned():
    from basetest import base_packs
    rows = base_packs()
    assert [r["dir"] for r in rows] == ["rgthree-comfy", "ComfyUI-KJNodes", "ComfyUI-VideoHelperSuite", "cg-use-everywhere", "ComfyUI-Manager", "ComfyUI-advanced-model-manager"]
    assert all(re.fullmatch(r"[0-9a-f]{40}", r["sha"]) for r in rows), rows
    assert "BAKED_PACKS" not in "".join(code_only(p.read_text()) for p in LIB)      # no image-owned pack list: the tree is ours


def test_unit_venv_requirement_set_covers_every_pack_dir_present(tmp_path):
    """Whatever sits in custom_nodes — managed by a row or not — gets its requirements into the venv, so the tree imports."""
    c = _pod(tmp_path); (c / "requirements.txt").write_text("")
    for d in ("Foo", "some-unmanaged-pack"):
        (c / "custom_nodes" / d).mkdir(parents=True); (c / "custom_nodes" / d / "requirements.txt").write_text("x\n")
    (c / "custom_nodes" / "NoReqs").mkdir()
    r = _bash("base_env_setup; base_discover quiet; _base_all_reqfiles", env={"BASE_FAKE_ROOT": str(tmp_path)})
    files = r.stdout.split()
    assert f"{c}/requirements.txt" in files and f"{c}/custom_nodes/Foo/requirements.txt" in files and f"{c}/custom_nodes/some-unmanaged-pack/requirements.txt" in files, r.stdout + r.stderr


# ---------------------------------------------------------------- T18: the version gate, the pod tier's inputs, the rehearsal (base V2.0.0)

def test_unit_version_gate_accepts_an_older_base_min_and_refuses_a_newer_one(tmp_path):
    """A package declares BASE_MIN, the base it was written against. Base 2.0.0 runs a package that says 1.0.0; a package
    that needs a newer base than the one installed is refused with both versions named."""
    r = _bash('BASE_MIN=1.0.0; _base_base_min_gate && echo GATE_OK')
    assert "GATE_OK" in r.stdout, r.stdout + r.stderr
    r = _bash('BASE_MIN=99.0.0; PKG_NAME=Future; _base_base_min_gate || echo GATE_REFUSED')
    assert "GATE_REFUSED" in r.stdout and "99.0.0" in r.stdout + r.stderr and "Future" in r.stdout + r.stderr, r.stdout + r.stderr
    v = (BASE / "VERSION").read_text().strip()
    assert v in r.stdout + r.stderr
    src = code_only((BASE / "lib" / "95-summary.sh").read_text())
    assert "_base_base_min_gate" in src[src.index("_base_validate_tables()"):src.index("_base_declare_dump()")]


def test_unit_base_test_hands_the_suite_the_venv_and_the_tree():
    src = code_only((BASE / "lib" / "95-summary.sh").read_text()); body = src[src.index("base_test()"):src.index("_base_pkg_command()")]
    assert 'BASE_VENV="${VENV:-}"' in body and 'BASE_COMFY="${COMFY:-}"' in body
    pod = (BASE / "suite" / "test_pod.py").read_text()
    assert 'os.environ.get("BASE_VENV")' in pod and "test_runpod_venv_is_stamped_at_the_path_the_boot_activates" in pod
    assert "test_runpod_the_running_comfyui_uses_the_base_venv" in pod and "test_runpod_boot_sh_is_pid1_and_sshd_jupyter_answer" in pod


def test_unit_rehearsal_knows_the_three_layouts_and_boots_the_fake_pod():
    _repo_only("_build/rehearse.py")
    src = (BASE / "_build" / "rehearse.py").read_text()
    assert '"--layout"' in src and all(l in src for l in ("official", "community", "bare"))
    assert "boot_stubs" in src and "boot.sh" in src and "layout=a.layout" in src
    assert "def boot_stubs" in (BASE / "py" / "basetest.py").read_text()          # shared with the unit tier's boot tests


# ---------------------------------------------------------------- T19: found on the live pod, 2026-09-05 (base V2.0.0 fix loop)

SILENT_GIT = """#!/bin/bash
while [ "${1:-}" = "-c" ]; do shift 2; done
case "${1:-}" in
  clone) d="${@: -1}"; mkdir -p "$d/.git"; exit 0 ;;
  -C) shift 2
      case "${1:-}" in
        remote) [ "${2:-}" = "get-url" ] && echo "https://example.invalid/x"; exit 0 ;;
        rev-parse) echo 0123456789abcdef0123456789abcdef01234567; exit 0 ;;
        *) exit 0 ;;
      esac ;;
  *) exit 0 ;;
esac
"""

FAILING_GIT = """#!/bin/bash
while [ "${1:-}" = "-c" ]; do shift 2; done
[ "${1:-}" = clone ] && exit 128
exit 0
"""


def test_unit_a_silent_git_clone_is_a_success_not_a_failure(tmp_path):
    """On a live pod (a community template image) every base pack 'clone failed' although the directories were there —
    git -q printed nothing, `grep -v` on empty input exited 1, and pipefail called that a failed clone. git's own
    exit status is the only judge."""
    c = _pod(tmp_path); (c / "custom_nodes").mkdir(exist_ok=True)
    bin_ = tmp_path / "bin"; bin_.mkdir(); (bin_ / "git").write_text(SILENT_GIT); (bin_ / "git").chmod(0o755)
    env = {"BASE_FAKE_ROOT": str(tmp_path), "PATH": "%s:/usr/bin:/bin" % bin_}
    # a real run has pipefail on (base_main); the harness must too, or grep's exit status is masked by tail's
    r = _bash('set -o pipefail; base_env_setup; base_discover quiet; base_packs git; echo CLONED=${#PACK_CLONED[@]} FAILED=${#BASE_FAILED[@]}', env=env)
    assert "clone failed" not in r.stdout and "CLONED=6" in r.stdout and "FAILED=0" in r.stdout, r.stdout + r.stderr   # six shared packs
    assert (c / "custom_nodes" / "rgthree-comfy" / ".git").exists()
    # a clone that really fails (git exits non-zero, silently) is still a failure
    (bin_ / "git").write_text(FAILING_GIT)
    (c / "custom_nodes" / "rgthree-comfy").rename(tmp_path / "gone")
    r = _bash('set -o pipefail; base_env_setup; base_discover quiet; base_packs git; echo FAILED=${#BASE_FAILED[@]}', env=env)
    assert "clone failed: rgthree-comfy" in r.stdout and "FAILED=0" not in r.stdout, r.stdout + r.stderr


def test_unit_uv_is_installed_into_the_tools_venv_on_the_volume_never_the_system_interpreter(tmp_path):
    """Live pod 2026-09-05: `pip install --user uv` into the image's python is refused (PEP 668, externally managed).
    uv comes from PyPI into the base's tools venv on the volume ($BASE_HOME/tools, made with the system python's venv
    module); the system interpreter is never written to; the second run finds it."""
    c = _pod(tmp_path); bin_ = tmp_path / "bin"; bin_.mkdir(); log = tmp_path / "calls.log"
    tools = tmp_path / "comfy-base" / "tools"
    # one stub python: as the SYSTEM interpreter it refuses `-m pip` (PEP 668); copied into the tools venv by `-m venv`
    # it becomes that venv's python, whose `-m pip install … uv` creates bin/uv. Every call logs "$0 $*".
    py = bin_ / "python3"
    py.write_text('#!/bin/bash\necho "$0 $*" >> "%s"\n'
                  'case "$0" in */tools/bin/python)\n'
                  '  case "$*" in "-m pip --version") echo "pip 99 (stub)"; exit 0;; *"-m pip install"*uv*) printf "#!/bin/bash\\necho uv 9.9-stub\\n" > "$(dirname "$0")/uv"; chmod +x "$(dirname "$0")/uv"; exit 0;; esac; exit 0;;\n'
                  'esac\n'
                  'if [ "$1 $2" = "-m venv" ]; then d="$3"; mkdir -p "$d/bin"; cp "$0" "$d/bin/python"; exit 0; fi\n'
                  'if [ "$1 $2" = "-m pip" ]; then echo "error: externally-managed-environment" >&2; exit 1; fi\nexit 0\n' % log)
    py.chmod(0o755)
    env = {"BASE_FAKE_ROOT": str(tmp_path), "PATH": "%s:/usr/bin:/bin" % bin_, "SYS_PY": str(py)}
    r = _bash('base_env_setup; _base_ensure_uv && echo UV=$(command -v uv)', env=env)
    assert ("UV=%s" % (tools / "bin" / "uv")) in r.stdout, r.stdout + r.stderr
    calls = log.read_text()
    assert ("%s -m venv %s" % (py, tools)) in calls and ("%s/bin/python -m pip install" % tools) in calls and "uv" in calls.split("-m pip install")[1]
    assert ("%s -m pip" % py) not in calls                                                 # the system interpreter is never touched
    r2 = _bash('base_env_setup; _base_ensure_uv && echo UV=$(command -v uv)', env=env)
    assert "UV=" in r2.stdout and log.read_text().count("-m venv") == 1                  # found, not rebuilt


UV_STUB = """#!/bin/bash
echo "uv $*" >> "__LOG__"
case "$1 $2" in
  "python list") printf 'cpython-3.14.7-linux-x86_64-gnu    <download available>\\ncpython-3.13.5-linux-x86_64-gnu    <download available>\\n'; exit 0 ;;
  "pip compile")
    # uv 0.12: requirement files are positional; `-r` is rejected exactly like this
    for a in "$@"; do [ "$a" = "-r" ] && { echo "error: unexpected argument '-r' found" >&2; exit 2; }; done
    [ -n "__FAIL__" ] && { echo "  x No solution found when resolving dependencies: torch==0 has no wheel for cp313" >&2; exit 1; }
    for a in "$@"; do case "$prev" in -o) : > "$a";; esac; prev="$a"; done
    exit 0 ;;
  "--version"*) echo "uv 0.12.10"; exit 0 ;;
esac
exit 0
"""


def _uv_stub(tmp_path, fail=False):
    """A uv 0.12-shaped stub in the tools venv (so _base_ensure_uv finds it) that logs argv and either resolves or not."""
    t = tmp_path / "comfy-base" / "tools" / "bin"; t.mkdir(parents=True, exist_ok=True); log = tmp_path / "calls.log"
    (t / "uv").write_text(UV_STUB.replace("__LOG__", str(log)).replace("__FAIL__", "1" if fail else "")); (t / "uv").chmod(0o755)
    return log


def test_unit_python_pick_passes_requirement_files_positionally_and_a_failed_pick_fails_the_run(tmp_path):
    """Live pod 2026-09-05: uv 0.12 rejects `uv pip compile -r file` (files are positional), so every Python 'did not
    resolve'; uv's message was discarded; and the pick's BASE_FAILED entry, made inside $(…), was lost — the run exited 0."""
    c = _pod(tmp_path); (c / "requirements.txt").write_text("torch\n")
    (c / "custom_nodes" / "Pack").mkdir(parents=True); (c / "custom_nodes" / "Pack" / "requirements.txt").write_text("x\n")
    log = _uv_stub(tmp_path)
    env = {"BASE_FAKE_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin"}
    r = _bash('base_env_setup; base_discover quiet; _base_ensure_uv; PICK=$(_base_python_pick); echo PICK=$PICK', env=env)
    assert "PICK=3.14" in r.stdout, r.stdout + r.stderr
    compile_line = [ln for ln in log.read_text().splitlines() if "pip compile" in ln][0]
    assert " -r " not in compile_line and str(c / "requirements.txt") in compile_line and str(c / "custom_nodes" / "Pack" / "requirements.txt") in compile_line
    # a pick that really cannot resolve: the reason is shown, and the run is FAILED (exit 1), not a silent 0
    log = _uv_stub(tmp_path, fail=True)
    r = _bash('base_env_setup; base_discover quiet; base_venv; echo FAILED=${#BASE_FAILED[@]}; base_summary', env=env)
    assert "FAILED=1" in r.stdout and "No solution found" in r.stdout + r.stderr, r.stdout + r.stderr
    assert r.returncode == 1


OPTIONAL_DECLS = ("DRIVER_MIN", "SUPERSEDED", "LEGACY_DIRS", "DROPPED_PACKS", "PIP_EXTRA", "HYGIENE_ARGS", "HYGIENE_ARGS_REMOVE",
                  "LOADER_CATS", "PKG_COMMANDS", "PKG_IMPORT_CHECK", "PKG_NO_SUITE", "ZIP_EXTRA")


def test_unit_pip_extra_step_is_safe_when_a_package_declares_none(tmp_path):
    """Live pod 2026-09-05: the base's own step one died seven minutes into the venv build with
    'PIP_EXTRA: unbound variable' — an optional package declaration counted with ${#…[@]} under set -u."""
    r = _bash('set -u; _base_pip(){ echo "PIP $*"; }; CONSTRAINTS=/dev/null; _base_venv_pip_extra; echo RC=$?')
    assert "RC=0" in r.stdout and "PIP" not in r.stdout, r.stdout + r.stderr
    r = _bash('set -u; _base_pip(){ echo "PIP $*"; }; CONSTRAINTS=/dev/null; PIP_EXTRA=(nvidia-vfx sageattention); _base_venv_pip_extra; echo RC=$?')
    assert "RC=0" in r.stdout and "nvidia-vfx sageattention" in r.stdout and "-c /dev/null" in r.stdout, r.stdout + r.stderr


def test_unit_optional_declarations_are_never_counted_unguarded():
    """${#NAME[@]} on an array a package may not declare is an unbound-variable exit under set -u on bash 5:
    every such count must sit behind ${NAME[@]+…} on the same line."""
    bad = []
    for p in LIB:
        for i, ln in enumerate(code_only(p.read_text()).splitlines(), 1):
            for name in OPTIONAL_DECLS:
                if ("${#%s[@]}" % name) in ln and ("${%s+" % name) not in ln and ("${%s[@]+" % name) not in ln:
                    bad.append("%s:%d %s" % (p.name, i, ln.strip()[:90]))
    assert not bad, bad


# ---------------------------------------------------------------- T20: found on the live pod, 2026-09-05, second batch

def test_unit_a_fake_run_never_reads_the_real_pid1_environment(tmp_path):
    """On the pod a fake-mode test picked up the pod's REAL HF_TOKEN from /proc/1/environ. In fake mode the only PID 1
    environment is BASE_FAKE_PID1_ENV; in real mode BASE_PID1_ENVIRON (a test seam) stands in for /proc/1/environ."""
    environ = tmp_path / "environ"; environ.write_bytes(b"HF_TOKEN=real-value\0OTHER=x\0")
    r = _bash('_base_pid1_env HF_TOKEN', env={"BASE_PID1_ENVIRON": str(environ)})
    assert r.stdout.strip() == "real-value", r.stdout + r.stderr                        # real mode: read
    r = _bash('_base_pid1_env HF_TOKEN; echo "[end]"', env={"BASE_PID1_ENVIRON": str(environ), "BASE_FAKE_ROOT": str(tmp_path)})
    assert r.stdout.strip() == "[end]", r.stdout + r.stderr                             # fake mode, no fake file: nothing
    fake = tmp_path / "pid1.env"; fake.write_text("HF_TOKEN=fake-value\n")
    r = _bash('_base_pid1_env HF_TOKEN', env={"BASE_PID1_ENVIRON": str(environ), "BASE_FAKE_ROOT": str(tmp_path), "BASE_FAKE_PID1_ENV": str(fake)})
    assert r.stdout.strip() == "fake-value", r.stdout + r.stderr


def test_unit_on_pod_means_linux_with_any_nvidia_device_node(tmp_path):
    """The pod's GPU was /dev/nvidia3, not /dev/nvidia0: the pod tiers skipped and the volume check thought it was off-pod."""
    dev = tmp_path / "dev"; dev.mkdir(); (dev / "nvidiactl").write_text(""); (dev / "nvidia3").write_text("")
    r = _bash('_base_on_pod && echo POD || echo NOPOD', env={"BASE_DEV_DIR": str(dev), "BASE_FAKE_UNAME": "Linux"})
    assert "POD" in r.stdout and "NOPOD" not in r.stdout, r.stdout + r.stderr
    (dev / "nvidia3").unlink()
    r = _bash('_base_on_pod && echo POD || echo NOPOD', env={"BASE_DEV_DIR": str(dev), "BASE_FAKE_UNAME": "Linux"})
    assert "NOPOD" in r.stdout
    r = _bash('_base_on_pod && echo POD || echo NOPOD', env={"BASE_DEV_DIR": str(dev), "BASE_FAKE_UNAME": "Darwin"})
    assert "NOPOD" in r.stdout
    text = "".join(code_only(q.read_text()) for q in LIB) + (BASE / "py" / "basetest.py").read_text()
    assert "nvidia0" not in text, "a literal /dev/nvidia0 gate is still there"


def test_unit_an_empty_model_index_is_not_a_warning(tmp_path):
    """GNU xargs runs `stat` with no arguments on empty input (BSD does not): a fresh volume's empty index raised
    'the model index walk did not complete' on the pod. The pipeline goes through _base_xargs0, which passes -r where it exists."""
    c = _pod(tmp_path)
    r = _bash('base_env_setup; base_discover quiet; _base_build_index; echo WARN=${#BASE_WARN[@]} LINES=$(wc -l < "$IDX")', env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_SEARCH_ROOTS": str(tmp_path / "ComfyUI" / "models")})
    assert "WARN=0" in r.stdout and "LINES=0" in r.stdout.replace(" ", ""), r.stdout + r.stderr
    src = code_only((BASE / "lib" / "50-models.sh").read_text())
    assert "_base_xargs0" in src[src.index("_base_build_index()"):]
    r = _bash('printf "" | _base_xargs0 stat -c %s; echo RC=$?')
    assert "RC=0" in r.stdout and "missing operand" not in r.stdout + r.stderr


def test_unit_the_shipped_suite_skips_repo_only_tests_instead_of_failing():
    unit = (BASE / "suite" / "test_unit.py").read_text(); inst = (BASE / "suite" / "test_install.py").read_text()
    assert not re.findall(r'^\s+STUB = BASE / "_build" / "stub"$', unit, re.M)            # every use goes through the skipping helper
    for needle in ("package.py", "rehearse.py", "testbed.sh", 'comfyui-base.zip"'):
        for m in re.finditer(r"def (test_unit_[a-z_]+)\(", unit):
            body = unit[m.end():m.end() + 1800].split("\ndef ")[0]
            if needle in body and "_repo_only(" not in body and "_stub()" not in body:
                raise AssertionError("%s uses %s without _repo_only()" % (m.group(1), needle))
    assert "pytestmark = [pytest.mark.install, pytest.mark.skipif(not STUB.exists()" in inst


def test_unit_the_test_command_runs_the_suite_with_the_base_s_own_uv_when_none_is_on_path(tmp_path):
    """On the pod `…-script.sh test` said "no pytest in the venv and no uv": uv lives in the base's tools venv on the
    volume (2.0.1), which the install path puts on PATH and the test command, run on its own, did not (2.0.5)."""
    _pod(tmp_path)
    tools = tmp_path / "comfy-base" / "tools" / "bin"; tools.mkdir(parents=True)
    uv = tools / "uv"; uv.write_text('#!/bin/bash\necho "uv $*" >> "$(dirname "$0")/calls.log"\necho "1 passed"\n'); uv.chmod(0o755)
    r = _bash('BASE_INNER=""; base_env_setup; base_discover quiet; PY=/nonexistent/python; BASE_NODE_SRC=""; base_test unit; echo "RESULT=$BASE_TEST_RESULT RC=$BASE_TEST_RC"',
              env={"BASE_FAKE_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")})   # HOME too: base_env_setup adds ~/.local/bin, where the Mac's real uv lives (a real uv here re-runs the whole suite inside this test)
    assert "did NOT run" not in r.stdout and "RESULT=1 passed RC=0" in r.stdout, r.stdout + r.stderr
    assert (tools / "calls.log").exists() and "pytest" in (tools / "calls.log").read_text()
    # 2.0.14: `bash base.sh test` reported "✔ suite: 1 failed …" — the pipeline into tee hid pytest's exit code (base.sh, unlike
    # the package scripts, never set pipefail). The runner judges pytest's OWN status, whatever the shell's options are.
    uv.write_text('#!/bin/bash\necho "uv $*" >> "$(dirname "$0")/calls.log"\necho "1 failed, 3 passed"\nexit 1\n')
    r = _bash('BASE_INNER=""; base_env_setup; base_discover quiet; PY=/nonexistent/python; BASE_NODE_SRC=""; base_test unit; echo "RESULT=$BASE_TEST_RESULT RC=$BASE_TEST_RC"',
              env={"BASE_FAKE_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")})
    assert "RESULT=1 failed, 3 passed RC=1" in r.stdout and "✖ suite" in r.stdout and "✔ suite" not in r.stdout, r.stdout + r.stderr


def test_unit_boot_sh_records_its_pid_so_the_pod_tier_can_tell_a_base_boot_from_the_images(tmp_path):
    """On the pod both runpod tests took PID 1 for boot.sh because podctl's wrapper text mentions boot.sh (2.0.6)."""
    from basetest import fake_pod, run_script
    STUB = _stub()
    fake_pod(tmp_path, STUB, layout="official")
    assert run_script(STUB / "stub-script.sh", fake_root=tmp_path).returncode == 0
    bin_, log = _boot_stubs(tmp_path)
    r, blog = _run_boot_sh(tmp_path, bin_, {"JUPYTER_PASSWORD": "pw"})
    pidfile = tmp_path / "comfy-base" / "state" / "boot.pid"
    assert r.returncode == 0 and pidfile.exists() and pidfile.read_text().strip().isdigit(), r.stderr + blog


def _pod_tier():
    import importlib.util
    spec = importlib.util.spec_from_file_location("test_pod", BASE / "suite" / "test_pod.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def test_unit_the_pod_tier_tells_a_base_boot_from_the_images_wrapper(tmp_path):
    m = _pod_tier()
    state = tmp_path / "state"; state.mkdir(); proc = tmp_path / "proc"; proc.mkdir()
    assert m._booted_through_base(state, proc) is False                                      # no boot.pid: the image's boot
    (state / "boot.pid").write_text("7\n")
    assert m._booted_through_base(state, proc) is False                                      # a dead pid (a previous boot)
    (proc / "7").mkdir(); (proc / "7" / "cmdline").write_bytes(b"bash\0-c\0if [ -x /workspace/comfy-base/boot.sh ]; then exec bash /workspace/comfy-base/boot.sh; fi; exec /start_script.sh\0")
    assert m._booted_through_base(state, proc) is False                                      # podctl's wrapper is not boot.sh
    (proc / "7" / "cmdline").write_bytes(b"bash\0/workspace/comfy-base/boot.sh\0")
    assert m._booted_through_base(state, proc) is True
    src = (BASE / "suite" / "test_pod.py").read_text()
    assert src.count("_booted_through_base(") >= 3 and '"comfy-base/boot.sh" not in' not in src   # both runpod tests use it; the substring test is gone
    assert 'BASE_STATE=' in code_only((BASE / "lib" / "95-summary.sh").read_text())           # the runner hands the state dir over


def test_unit_a_base_upgrade_refreshes_the_boot_script_the_pod_runs(tmp_path):
    """`test` on the pod re-installed base 2.0.6 into /workspace/comfy-base and left the 2.0.3 boot.sh that podctl's
    wrapper execs: only the install flow wrote it. Every self-install refreshes an existing boot.sh (2.0.7)."""
    from basetest import fake_pod, run_script
    STUB = _stub()
    fake_pod(tmp_path, STUB, layout="official")
    assert run_script(STUB / "stub-script.sh", fake_root=tmp_path).returncode == 0
    boot = tmp_path / "comfy-base" / "boot.sh"; assert boot.exists()
    boot.write_text("#!/bin/bash\n# a boot.sh from an older base\n")
    r = run_script(BASE / "comfyui-base-script.sh", "test", fake_root=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert boot.read_text() == (BASE / "lib" / "boot.sh").read_text(), r.stdout
    assert "refreshed" in r.stdout and "boot.sh" in r.stdout


def test_unit_the_banner_never_threatens_a_rebuild_of_a_venv_whose_interpreter_survives(tmp_path):
    """On the pod the banner's "boots from" line said "on the volume — survives a pod restart; this run rebuilds the
    venv" in one breath: it handed the interpreter's home DIRECTORY to a predicate that expects a venv (2.0.8).
    The line is one function, _base_venv_boot_line <venv>, judged with the same predicate the venv stage uses."""
    vol = tmp_path / "vol"; venv = vol / "ComfyUI" / ".venv-cu130"; venv.mkdir(parents=True)
    venv.joinpath("pyvenv.cfg").write_text("home = %s/comfy-base/python/cpython-3.14/bin\nversion = 3.14.7\n" % vol)
    r = _bash('BASE_PERSIST_ROOT="%s"; _base_venv_boot_line "%s"' % (vol, venv))        # set AFTER sourcing: base.sh resets its globals
    assert "boots from" in r.stdout and "survives" in r.stdout and "rebuilds" not in r.stdout, r.stdout + r.stderr
    venv.joinpath("pyvenv.cfg").write_text("home = /root/.local/share/uv/python/cpython-3.14/bin\n")
    r = _bash('BASE_PERSIST_ROOT="%s"; _base_venv_boot_line "%s"' % (vol, venv))
    assert "CONTAINER DISK" in r.stdout and "rebuilds the venv" in r.stdout, r.stdout + r.stderr
    src = code_only((BASE / "lib" / "10-discover.sh").read_text())
    assert "_base_venv_boot_line" in src and "this run rebuilds the venv" not in src   # the banner calls the helper, no second copy


def test_unit_a_failed_download_prints_the_tools_last_lines_and_fails_the_run(tmp_path):
    """On the pod a model failed three attempts and the console said only "attempt N failed": the tool ran quietly and
    its output went nowhere. The tool's last lines are the diagnosis (as uv's are since 2.0.2) — 2.0.9; the curl leg
    (GitHub) keeps that behaviour."""
    from basetest import fake_pod, run_script
    STUB = _stub()
    fake_pod(tmp_path, STUB, layout="official"); _stub_with_github_row(tmp_path)
    bin_ = tmp_path / "bin"; bin_.mkdir(); log = tmp_path / "calls.log"
    (bin_ / "hf").write_text('#!/bin/bash\nd=""; while [ $# -gt 0 ]; do case "$1" in --local-dir) d="$2"; shift;; esac; shift; done\nmkdir -p "$d"; python3 -c "open(\\"$d/stub_vae.safetensors\\",\\"wb\\").truncate(4096)"\n'); (bin_ / "hf").chmod(0o755)
    (bin_ / "curl").write_text('#!/bin/bash\necho "curl argv: $*" >> "%s"\ncase " $* " in *github.com*) echo "curl: (22) The requested URL returned error: 404"; exit 1;; esac\nexit 0\n' % log)
    (bin_ / "curl").chmod(0o755)
    r = run_script(STUB / "stub-script.sh", fake_root=tmp_path,
                   env={"PATH": "%s:%s" % (bin_, os.environ["PATH"]), "BASE_FAKE_DL": "real", "HF_TOKEN": "hf_env_value"})
    assert r.returncode != 0
    assert "returned error: 404" in r.stdout and "download failed" in r.stdout and "stub_gh" in r.stdout, r.stdout[-3000:]
    assert len([l for l in log.read_text().splitlines() if l.startswith("curl argv:") and "github.com" in l]) == 3   # three attempts, then the verdict


def test_unit_a_snapshot_download_keeps_the_token_off_argv_and_shows_its_error(tmp_path):
    bin_ = tmp_path / "bin"; bin_.mkdir(); log = tmp_path / "calls.log"
    (bin_ / "hf").write_text('#!/bin/bash\necho "hf argv: $*" >> "%s"; echo "hf env HF_TOKEN=${HF_TOKEN:-unset}" >> "%s"\necho "401 Client Error: Unauthorized for url"; exit 1\n' % (log, log)); (bin_ / "hf").chmod(0o755)
    r = _bash('export HF_TOKEN=hf_env_value; VENV=/nonexistent; BASE_NO_NET=0; _base_snapshot hf://owner/repo "%s" 4096; echo RC=$?' % (tmp_path / "dest"),
              env={"PATH": "%s:%s" % (bin_, os.environ["PATH"])})
    calls = log.read_text()
    assert "--token" not in calls and "hf env HF_TOKEN=hf_env_value" in calls and calls.count("hf argv:") == 3
    assert "Unauthorized" in r.stdout and "RC=1" in r.stdout, r.stdout + r.stderr


def test_unit_the_launch_sets_hf_home_in_the_environment_and_still_runs_python(tmp_path):
    """The pod's restart FAILED with "85-launch.sh: line 37: HF_HOME=/workspace/huggingface: No such file or directory":
    the launch line put the assignment in front of nohup as a WORD, and bash ran it as a command. The fake boot test
    never saw it — its curl stub answers "up" whatever happened. This test runs the launch with a python stub (2.0.10)."""
    bin_ = tmp_path / "bin"; bin_.mkdir(); log = tmp_path / "calls.log"; vol = tmp_path / "vol"; comfy = vol / "ComfyUI"; comfy.mkdir(parents=True)
    (bin_ / "python").write_text('#!/bin/bash\necho "python argv: $*" >> "%s"; echo "python env HF_HOME=${HF_HOME:-unset} cwd=$PWD" >> "%s"\n' % (log, log)); (bin_ / "python").chmod(0o755)
    (bin_ / "curl").write_text('#!/bin/bash\nexit 0\n'); (bin_ / "curl").chmod(0o755)
    r = _bash('BASE_PERSIST_ROOT="%s"; PY="%s"; COMFY="%s"; PORT=8188; HOSTPORT=127.0.0.1:8188; ARGS_FILE=/nonexistent; COMFY_LOG="%s"; BASE_START_TRIES=1; _base_start_comfy; echo RC=$?; sleep 1'
              % (vol, bin_ / "python", comfy, tmp_path / "comfyui.log"), env={"PATH": "%s:/usr/bin:/bin" % bin_})
    assert "RC=0" in r.stdout, r.stdout + r.stderr
    calls = log.read_text() if log.exists() else (tmp_path / "comfyui.log").read_text() if (tmp_path / "comfyui.log").exists() else ""
    assert "python argv: main.py --listen 0.0.0.0 --port 8188" in calls, (calls, r.stdout, r.stderr)
    assert "python env HF_HOME=%s/huggingface cwd=%s" % (vol, comfy) in calls, calls
    assert "No such file" not in (tmp_path / "comfyui.log").read_text()


def test_unit_pytest_is_installed_into_our_venv_so_the_suite_runs_inside_it(tmp_path):
    """On the pod a package suite's gpu tier failed with "No module named comfy_kitchen" and "no metadata for nvidia-vfx" —
    both ARE in the venv; the suite ran under uv's throwaway Python 3.12 because our venv had no pytest (2.0.10).
    pytest is installed in the build, checked on every reuse, and installed by base_test itself when missing — proven by import."""
    bin_ = tmp_path / "bin"; bin_.mkdir()
    (bin_ / "py-with").write_text('#!/bin/bash\ncase "$*" in *pytest*) echo 8.4.0;; esac; exit 0\n'); (bin_ / "py-with").chmod(0o755)
    (bin_ / "py-without").write_text('#!/bin/bash\ncase "$*" in *"import pytest"*) exit 1;; esac; exit 0\n'); (bin_ / "py-without").chmod(0o755)
    r = _bash('set -u; _base_pip(){ echo "PIP $*"; }; CONSTRAINTS=/dev/null; PY="%s"; _base_venv_pytest; echo RC=$?' % (bin_ / "py-with"))
    assert "RC=0" in r.stdout and "PIP install" in r.stdout and " pytest" in r.stdout and "-c /dev/null" in r.stdout and "pytest 8.4.0 in the venv" in r.stdout, r.stdout + r.stderr
    r = _bash('set -u; _base_pip(){ echo "PIP $*"; }; CONSTRAINTS=/dev/null; PY="%s"; _base_venv_pytest; echo RC=$?' % (bin_ / "py-without"))
    assert "RC=1" in r.stdout and "does not import" in r.stdout, r.stdout + r.stderr                 # an install that leaves no pytest is a failure, said so
    r = _bash('set -u; _base_venv_pytest(){ echo INSTALL; }; PY="%s"; _base_venv_ensure_pytest; PY="%s"; _base_venv_ensure_pytest; echo RC=$?' % (bin_ / "py-with", bin_ / "py-without"))
    assert r.stdout.count("INSTALL") == 1 and "RC=0" in r.stdout, r.stdout + r.stderr
    src30 = code_only((BASE / "lib" / "30-venv.sh").read_text()); src95 = code_only((BASE / "lib" / "95-summary.sh").read_text())
    assert "_base_venv_pytest || step_fail=1" in src30 and "_base_venv_ensure_pytest" in src30[src30.index("base_venv()"):]
    assert "_base_venv_pytest" in src95[src95.index("base_test()"):]
    assert '-c "$CONSTRAINTS" pytest "huggingface_hub' not in src30     # the one-line install whose pytest half silently never happened


def _launch_stubs(tmp_path):
    """A python that records argv + HF_HOME and raises an `up` marker; a curl that answers only once the marker exists."""
    bin_ = tmp_path / "bin"; bin_.mkdir(exist_ok=True); log = tmp_path / "calls.log"; up = tmp_path / "up"
    (bin_ / "python").write_text('#!/bin/bash\necho "python argv: $*" >> "%s"; echo "python env HF_HOME=${HF_HOME:-unset} cwd=$PWD" >> "%s"; touch "%s"\n' % (log, log, up))
    (bin_ / "curl").write_text('#!/bin/bash\n[ -e "%s" ]\n' % up)
    for p in (bin_ / "python", bin_ / "curl"): p.chmod(0o755)
    return bin_, log


def test_unit_restart_opt_in_starts_a_server_when_none_runs(tmp_path):
    """Second step two on the pod (2.0.10): BASE_RESTART=1, no ComfyUI running (the first run had stopped the image's and
    its own launch had died) — and the stage only PRINTED a start line. Opting in means a running server at the end (2.0.11)."""
    _pod(tmp_path); bin_, log = _launch_stubs(tmp_path)
    snippet = ('PKG_DIR=/nonexistent; WF_NAME=w.json; base_env_setup; base_discover quiet; BASE_NO_NET=0; BASE_DRY=0; PY="%s"; COMFY_LOG="%s"; BASE_START_TRIES=3; '
               'MODEL_DL=(one); BASE_RESTART=1 base_restart; echo R=$BASE_RESTART_RESULT FAILED=${#BASE_FAILED[@]}') % (bin_ / "python", tmp_path / "comfyui.log")
    r = _bash(snippet, env={"BASE_FAKE_ROOT": str(tmp_path), "PATH": "%s:/usr/bin:/bin" % bin_})
    assert "R=started FAILED=0" in r.stdout, r.stdout + r.stderr
    assert log.exists() and "python argv: main.py --listen 0.0.0.0 --port" in log.read_text(), r.stdout
    # the import check's block still wins: nothing is started on top of a failed import check
    (tmp_path / "up").unlink(); log.unlink()
    r = _bash(snippet.replace("BASE_RESTART=1 base_restart", "BASE_BLOCK_RESTART=1 BASE_RESTART=1 base_restart"), env={"BASE_FAKE_ROOT": str(tmp_path), "PATH": "%s:/usr/bin:/bin" % bin_})
    assert "R=started" not in r.stdout and not log.exists() and "import check failed" in r.stdout, r.stdout + r.stderr


def test_unit_the_hand_off_line_is_runnable_as_printed(tmp_path):
    """The stage's hand-off was `cd … && nohup HF_HOME=/x python main.py …`: nohup executes "HF_HOME=/x" as a command.
    Whatever _base_start_cmd prints must run verbatim after nohup (2.0.11)."""
    bin_, log = _launch_stubs(tmp_path); vol = tmp_path / "vol"; comfy = vol / "ComfyUI"; comfy.mkdir(parents=True)
    r = _bash('BASE_PERSIST_ROOT="%s"; PY="%s"; PORT=8188; ARGS_FILE=/nonexistent; line="$(_base_start_cmd)"; echo "LINE=$line"; cd "%s" && nohup $line > "%s" 2>&1; echo RC=$?'
              % (vol, bin_ / "python", comfy, tmp_path / "nohup.out"), env={"PATH": "%s:/usr/bin:/bin" % bin_})
    assert "RC=0" in r.stdout and "HF_HOME=%s/huggingface" % vol in r.stdout, r.stdout + r.stderr
    assert log.exists() and "python env HF_HOME=%s/huggingface cwd=%s" % (vol, comfy) in log.read_text(), (r.stdout, (tmp_path / "nohup.out").read_text())


def test_unit_combofix_reports_a_widget_order_the_server_would_reject(tmp_path, capsys):
    """On the pod, `combos ok` — and the server refused the queue: node 1199's widgets_values were stored in an older
    pack's order (its mask string first), the frontend restores widgets POSITIONALLY in /object_info's order, and so the
    server got `model: '{}'` and `seed: 'standard'`. The value-by-membership check cannot see a shift; a positional pass
    can, because it models the widget sequence the frontend builds (required then optional inputs of widget types,
    forceInput skipped, control_after_generate after a seed) — 2.0.12."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("combofix", BASE / "py" / "combofix.py"); cf = importlib.util.module_from_spec(spec); spec.loader.exec_module(cf)
    labels = ["Qwen2.5-Omni-3B · 3.4 GB", "Qwen2.5-Omni-7B · 5.8 GB", "on disk: Qwen2.5-Omni-7B-Q4_K_M.gguf"]
    obj = {"Cap": {"input": {"required": {"model": ["COMBO", {"options": labels}], "length": ["COMBO", {"options": ["short", "standard", "long"]}], "seed": ["INT", {"default": 0, "control_after_generate": True}]},
                             "optional": {"previous": ["STRING", {"forceInput": True}], "clip": ["CLIP", {}], "options": ["PKG_OPTIONS", {}], "enabled_mask": ["STRING", {"default": "{}"}],
                                          "max_frames": ["INT", {"default": 8}], "context_size": ["INT", {"default": 0}], "bypass": ["BOOLEAN", {"default": False}],
                                          "reference_instructions": ["STRING", {"default": ""}], "repeat_last": ["BOOLEAN", {"default": False}]}}}}
    assert [w[0] for w in cf.widget_sequence(obj["Cap"])] == ["model", "length", "seed", "control_after_generate", "enabled_mask", "max_frames", "context_size", "bypass", "reference_instructions", "repeat_last"]
    shifted = {"nodes": [{"id": 1199, "type": "Cap", "widgets_values": ["{}", labels[1], "standard", 42, "fixed", 8, 0, False, "{}", False]}]}
    good = {"nodes": [{"id": 1199, "type": "Cap", "widgets_values": [labels[1], "standard", 42, "fixed", "{}", 8, 0, False, "", False]}]}
    f = tmp_path / "wf.json"; f.write_text(json.dumps(shifted, indent=2))
    assert cf.process(obj, str(f), True) is True
    out = capsys.readouterr().out
    assert "UNRESOLVED #1199 Cap" in out and "position" in out and "model" in out and "'{}'" in out, out
    assert json.loads(f.read_text()) == shifted                                          # a shift is never "repaired" by guessing
    f.write_text(json.dumps(good, indent=2)); capsys.readouterr()
    assert cf.process(obj, str(f), True) is False and "UNRESOLVED" not in capsys.readouterr().out
    # 2.0.13: two shapes the first model skipped, which made TWO false positives on the pod (two nodes of one package):
    # a multi-type input ("STRING,COMBO") is a widget; a dynamic combo (COMFY_DYNAMICCOMBO_V3) is a selector whose
    # sub-widgets depend on the selection, so the judgement STOPS there (nothing after it is known positionally)
    obj["Guide"] = {"input": {"required": {"prompt": ["STRING", {"multiline": True}], "task": ["COMBO", {"options": ["T2VA", "FL2VA"]}], "duration": ["INT", {"default": 10}]},
                              "optional": {"aspect_ratio": ["STRING,COMBO", {"default": ""}], "auto_download": ["BOOLEAN", {"default": True}], "format": ["COMBO", {"options": ["plain", "json"]}]}}}
    assert [w[0] for w in cf.widget_sequence(obj["Guide"])] == ["prompt", "task", "duration", "aspect_ratio", "auto_download", "format"]
    obj["Up"] = {"input": {"required": {"model_name": ["COMBO", {"options": ["a.safetensors"]}], "resize_type": ["COMFY_DYNAMICCOMBO_V3", {"options": [{"key": "scale by multiplier"}, {"key": "target dimensions"}]}],
                                        "align": ["INT", {"default": 32}], "device": ["COMBO", {"options": ["cuda", "cpu"]}]}}}
    seq = cf.widget_sequence(obj["Up"]); assert [w[0] for w in seq] == ["model_name", "resize_type"] and seq[-1][1] == "DYNAMIC", seq
    wf3 = {"nodes": [{"id": 1195, "type": "Guide", "widgets_values": ["", "FL2VA", 5, "", True, "plain"]},
                     {"id": 819, "type": "Up", "widgets_values": ["a.safetensors", "scale by multiplier", 2, 32, "cuda"]}]}
    f.write_text(json.dumps(wf3, indent=2)); capsys.readouterr()
    assert cf.process(obj, str(f), True) is False and "UNRESOLVED" not in capsys.readouterr().out


# ---------------------------------------------------------------- T21: the sweep after the sixth run (base 2.0.14)

def test_unit_the_disk_gate_uses_the_volume_size_podctl_recorded_on_a_pooled_filesystem(tmp_path):
    """Every run on the pod warned "the volume reports 677 TB free — the backing pool, not your quota". podctl records
    the volume's size (RunPod REST) in state/volume.env; the gate then measures what the volume holds (du) and judges
    the real headroom. Without the record it still warns — and says how to record it."""
    _pod(tmp_path)
    st = tmp_path / "comfy-base" / "state"; st.mkdir(parents=True, exist_ok=True)
    (st / "volume.env").write_text("VOLUME_GB='300'\nVOLUME_ID='vol123'\nRECORDED='2026-09-06T03:00:00Z'\n")
    r = _bash('base_env_setup; base_discover quiet; BASE_FAKE_POOL=1; BASE_FAKE_USED_KB=100000000; _base_disk_gate 50000000000; echo RC=$? W=${#BASE_WARN[@]}', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "RC=0 W=0" in r.stdout and "300" in r.stdout and "podctl" in r.stdout and "not verifiable" not in r.stdout, r.stdout + r.stderr   # 100 GB used of 300: 200 free ≥ 60 needed
    r = _bash('base_env_setup; base_discover quiet; BASE_FAKE_POOL=1; BASE_FAKE_USED_KB=260000000; _base_disk_gate 50000000000; echo RC=$? W=${#BASE_WARN[@]} F=${#BASE_FAILED[@]}', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "RC=1" in r.stdout and "F=1" in r.stdout and "not enough space" in r.stdout, r.stdout + r.stderr                    # 260 used: 40 free < 60
    (st / "volume.env").unlink()
    r = _bash('base_env_setup; base_discover quiet; BASE_FAKE_POOL=1; _base_disk_gate 50000000000; echo RC=$? W=${#BASE_WARN[@]}', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "RC=0 W=1" in r.stdout and "not verifiable" in r.stdout and "podctl" in r.stdout, r.stdout + r.stderr                # honest, and tells how to fix it


def test_unit_a_green_latest_run_settles_its_warning_into_a_note():
    """`--latest` warns that packs are off their pins and untested — true until the run's own suite proves them. A green
    run settles the warning into a note (save the printed pins); a red one keeps it."""
    entry = "--latest: packs are off their pinned commits; the suites have not been run against these"
    r = _bash('BASE_WARN=("%s" "other"); BASE_FAILED=(); BASE_TEST_RC=0; BASE_LATEST=1; _base_warn_settle; echo N=${#BASE_WARN[@]}' % entry)
    assert "N=1" in r.stdout and "green" in r.stdout and "pin" in r.stdout, r.stdout + r.stderr
    r = _bash('BASE_WARN=("%s"); BASE_FAILED=(); BASE_TEST_RC=1; BASE_LATEST=1; _base_warn_settle; echo N=${#BASE_WARN[@]}' % entry)
    assert "N=1" in r.stdout and "green" not in r.stdout, r.stdout + r.stderr
    src = code_only((BASE / "lib" / "95-summary.sh").read_text())
    assert src.index("_base_warn_settle") < src.index('warnings   ${#BASE_WARN[@]}')          # settled before the summary counts them


def test_unit_snapshot_console_errors_are_classified_not_counted(tmp_path):
    """The snapshot printed "console errors: 15" on the pod and 13 on the testbed and nobody could say which mattered.
    Every line is either a KNOWN pattern with a written reason (the frontend's, not ours) or unexplained — and the
    tier fails on an unexplained one, so a new frontend cannot slip a new error past the count."""
    _repo_only("_build/canvas/snapshot.py")
    import importlib.util
    spec = importlib.util.spec_from_file_location("snapshot", BASE / "_build" / "canvas" / "snapshot.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    lines = ["[error] Failed to load resource: the server responded with a status of 404 (Not Found) http://x/api/userdata/comfy.settings.json",
             "[error] Failed to load resource: the server responded with a status of 400 (Bad Request) http://x/api/userdata/workflows%2Fa.json",
             "[error] Loading media from 'https://cdn.example/x.mp4' violates the following Content Security Policy directive: \"default-src 'self'\"",
             "[error] Loading the image 'https://cdn.example/x.png' violates the following Content Security Policy directive: \"img-src 'self' data: blob:\"",
             "[error] ComfyApp graph accessed before initialization",
             "[error] [vite:preloadError] {url: null, fileType: unknown, chunkName: null, message: Extension named 'ColorOverlay' already registered",
             "[error] TypeError: Cannot read properties of undefined (reading 'foo') at ourNode.js:12",
             "[log] fine"]
    known, unexplained = m.classify_console(lines)
    assert len(known) == 6 and len(unexplained) == 1 and "TypeError" in unexplained[0]
    assert all(reason for _line, reason in known) and len({reason for _l, reason in known}) >= 3
    assert set(m.console_summary(lines).keys()) == {"errors", "known", "unexplained", "reasons"}
    assert m.console_summary(lines)["unexplained"] == ["[error] TypeError: Cannot read properties of undefined (reading 'foo') at ourNode.js:12"]


def test_unit_foreign_source_is_parsed_with_its_syntax_warnings_accounted_for(tmp_path):
    """A package suite parses the packs' own .py files (ast) and Python's SyntaxWarning for THEIR invalid escapes surfaced
    as OUR suite's "12 warnings". parse_foreign_source keeps the tree, records (file, warning) — nothing escapes, and
    a test can assert every recorded file is a pack's, never ours."""
    import warnings
    from basetest import parse_foreign_source, FOREIGN_SYNTAX_WARNINGS
    f = tmp_path / "custom_nodes" / "pack" / "node.py"; f.parent.mkdir(parents=True); f.write_text('import re\nX = re.compile("\\s+\\d")\nclass N: pass\n')
    del FOREIGN_SYNTAX_WARNINGS[:]
    with warnings.catch_warnings(record=True) as escaped:
        warnings.simplefilter("always")
        tree = parse_foreign_source(f)
    assert tree is not None and any(isinstance(n, __import__("ast").ClassDef) for n in tree.body)
    assert escaped == [] and len(FOREIGN_SYNTAX_WARNINGS) >= 1 and FOREIGN_SYNTAX_WARNINGS[0][0] == str(f) and "escape" in FOREIGN_SYNTAX_WARNINGS[0][1]
    assert parse_foreign_source(tmp_path / "missing.py") is None
    g = tmp_path / "bad.py"; g.write_text("def (:\n")
    assert parse_foreign_source(g) is None                                  # a SyntaxError is not a warning: skipped, as before


def test_unit_the_suite_never_inherits_the_runs_tokens_and_a_red_suite_fails_the_run(tmp_path):
    """On the pod (2.0.14) the base's fake-mode tokens test found the REAL HF token in its shell environment — the run had
    exported the pod's tokens and the suite inherited them — and validated it against huggingface.co from inside a test.
    The runner strips every Hub token name from the suite's environment. And the step reported STEP RC=0 with
    "suite: 2 failed": a red suite is a failed run, not a warning."""
    _pod(tmp_path)
    tools = tmp_path / "comfy-base" / "tools" / "bin"; tools.mkdir(parents=True)
    uv = tools / "uv"; uv.write_text('#!/bin/bash\necho "uv env HF_TOKEN=${HF_TOKEN:-unset} HF_HUB_TOKEN=${HF_HUB_TOKEN:-unset}" >> "$(dirname "$0")/calls.log"\necho "2 failed, 3 passed"\nexit 1\n'); uv.chmod(0o755)
    r = _bash('BASE_INNER=""; base_env_setup; base_discover quiet; PY=/nonexistent/python; BASE_NODE_SRC=""; export HF_TOKEN=hf_real HF_HUB_TOKEN=hub_real; base_test unit; echo "RC=$BASE_TEST_RC FAILED=${#BASE_FAILED[@]} WARN=${#BASE_WARN[@]}"',
              env={"BASE_FAKE_ROOT": str(tmp_path), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")})
    assert "uv env HF_TOKEN=unset HF_HUB_TOKEN=unset" in (tools / "calls.log").read_text(), (tools / "calls.log").read_text()
    assert "RC=1 FAILED=1" in r.stdout and "hf_real" not in r.stdout, r.stdout + r.stderr

    # 2.0.16: the rehearsal's fake step one ran the shipped suite with the OUTER run's mode variables in its environment
    # (BASE_NO_NET=1, BASE_FAKE_ROOT, BASE_DRY…): tests that build their own fake pods inherited them and saw fake clones and
    # skipped stages. The suite gets the documented hand-over (BASE_COMFY, BASE_VENV, BASE_STATE, BASE_ON_POD…) and no other BASE_*.
    uv.write_text('#!/bin/bash\nenv | grep "^BASE_" | sort | tr "\\n" " " >> "$(dirname "$0")/env.log"; echo "1 passed"\nexit 0\n')
    r = _bash('BASE_INNER=""; base_env_setup; base_discover quiet; PY=/nonexistent/python; BASE_NODE_SRC=""; export BASE_NO_NET=1 BASE_DRY=1 BASE_LATEST=1 BASE_RESTART=1 BASE_FAKE_DL=fail; base_test unit; echo "RC=$BASE_TEST_RC"',
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_FAKE_PID1_ENV": str(tmp_path / "pid1.env"), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")})
    seen = (tools / "env.log").read_text()
    for leaked in ("BASE_NO_NET=", "BASE_FAKE_ROOT=", "BASE_FAKE_PID1_ENV=", "BASE_DRY=", "BASE_LATEST=", "BASE_RESTART=", "BASE_FAKE_DL="):
        assert leaked not in seen, (leaked, seen)
    for handed in ("BASE_COMFY=", "BASE_VENV=", "BASE_STATE=", "BASE_LIB=", "BASE_ON_POD="):
        assert handed in seen, (handed, seen)
    assert "RC=0" in r.stdout, r.stdout + r.stderr


def test_unit_a_fake_run_never_validates_a_token_over_the_network(tmp_path):
    """The same pod failure's other half: base_tokens in fake mode reached huggingface.co. A fake run (BASE_FAKE_ROOT,
    BASE_NO_NET) accepts a present token as "not validated (fake run)" and calls nothing."""
    _pod(tmp_path)
    bin_ = tmp_path / "bin"; bin_.mkdir(); log = tmp_path / "curl.log"
    (bin_ / "curl").write_text('#!/bin/bash\necho "curl $*" >> "%s"\nexit 0\n' % log); (bin_ / "curl").chmod(0o755)
    r = _bash('TOKENS=("HF_TOKEN|optional|x"); base_env_setup; base_discover quiet; export HF_TOKEN=hf_fake; base_tokens; echo RC=$?',
              env={"BASE_FAKE_ROOT": str(tmp_path), "PATH": "%s:/usr/bin:/bin" % bin_})
    assert "RC=0" in r.stdout and not log.exists() and "whoami" not in r.stdout and "hf_fake" not in r.stdout, r.stdout + r.stderr
    assert "not validated" in r.stdout or "fake" in r.stdout.lower(), r.stdout


def test_unit_start_waits_while_the_process_lives_and_gives_up_when_it_dies(tmp_path):
    """MEASURED 2026-09-08 on pod fakepod0000002: the custom nodes import for over three minutes, so a flat
    180 s window called a healthy restart FAILED and failed the install — the server was up a minute later.
    Waiting longer alone would make a DEAD server cost ten minutes of silence, so the wait is patient while
    the process lives and gives up the moment it does not, saying which happened.

    `_base_comfy_pids` is stubbed here on purpose: this is about the two BRANCHES, and pid discovery has its
    own tests. A fake python cannot be recognised as ComfyUI, so without the stub both branches look alike."""
    import time
    log = tmp_path / "comfy.log"
    py = tmp_path / "python"
    py.write_text("#!/bin/bash\nsleep 30\n"); py.chmod(0o755)
    base = ('BASE_PERSIST_ROOT="%s"; PY="%s"; COMFY="%s"; PORT=8188; HOSTPORT=127.0.0.1:59999; '
            'ARGS_FILE=/nonexistent; COMFY_LOG="%s"; ' % (tmp_path, py, tmp_path, log))

    # the process is gone: give up at once rather than sitting out the window
    t0 = time.time()
    r = _bash(base + '_base_comfy_pids(){ return 0; }; BASE_START_WAIT=60; _base_start_comfy; echo RC=$?')
    assert "RC=1" in r.stdout, r.stdout + r.stderr
    assert "did not survive its own start" in r.stdout, r.stdout
    assert time.time() - t0 < 30, "a dead process must not cost the whole window"

    # the process lives and never answers: wait the window out, then say it is still alive
    r = _bash(base + '_base_comfy_pids(){ echo 424242; }; BASE_START_WAIT=6; _base_start_comfy; echo RC=$?')
    assert "RC=1" in r.stdout and "still alive" in r.stdout, r.stdout

    # the old tries-based knob still sizes the window, so existing callers keep working
    r = _bash(base + '_base_comfy_pids(){ echo 424242; }; BASE_START_TRIES=2; _base_start_comfy; echo RC=$?')
    assert "RC=1" in r.stdout and "after 4 s" in r.stdout, r.stdout


# ---------------------------------------------------------------- T21: the two modes of 2.1.0 (a baked template image)

def test_unit_noninteractive_means_no_tty_and_every_prompt_takes_its_default():
    """BASE_NONINTERACTIVE=1 is the boot on a customer's pod: nobody answers, so the deletion prompt is No, the safe-default
    questions are yes, and BASE_TTY is 0 whatever is attached. BASE_YES=1 still answers the deletion prompt."""
    r = _bash('echo TTY=$BASE_TTY; _base_confirm "delete?" && echo YES || echo NO; _base_confirm_yes "roll back?" && echo SAFE_YES', env={"BASE_NONINTERACTIVE": "1"})
    assert "TTY=0" in r.stdout and "\nNO" in r.stdout and "default No" in r.stdout and "SAFE_YES" in r.stdout, r.stdout + r.stderr
    r2 = _bash('_base_confirm "delete?" && echo YES || echo NO', env={"BASE_NONINTERACTIVE": "1", "BASE_YES": "1"})
    assert "YES" in r2.stdout, r2.stdout
    r3 = _bash('echo "NI=$BASE_NONINTERACTIVE PIN=$BASE_PINNED"; bash -c \'echo "child NI=$BASE_NONINTERACTIVE PIN=$BASE_PINNED"\'', env={"BASE_NONINTERACTIVE": "1", "BASE_PINNED": "1"})
    assert "NI=1 PIN=1" in r3.stdout and "child NI=1 PIN=1" in r3.stdout, r3.stdout   # exported: a package script the boot runs inherits them


def test_unit_pinned_mode_fetches_nothing_checks_out_nothing_and_refuses_latest(tmp_path):
    work = _fake_comfy_repo(tmp_path)
    before = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    r = _bash('base_env_setup; base_discover quiet; base_update_comfyui; echo OLD=$COMFY_OLD NEW=$COMFY_NEW', env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_PINNED": "1", "COMFY_TAG": "v0.34.0"})
    assert "pinned at v0.34.0" in r.stdout and "image tag v0.34.0" in r.stdout and "OLD=0.34.0 NEW=0.34.0" in r.stdout, r.stdout + r.stderr
    assert before == subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    quiet = r.stdout.replace("no fetch, no checkout", "")
    assert "fetch" not in quiet and "checkout" not in quiet, r.stdout
    # the gate still applies: a seed below a package's floor fails there, loudly
    r2 = _bash("COMFY_MIN=9.9.9; base_env_setup; base_discover quiet; base_update_comfyui; base_comfy_gate || echo GATE=$?", env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_PINNED": "1"})
    assert "GATE=1" in r2.stdout, r2.stdout + r2.stderr
    # --latest is refused before anything runs, in both entries
    r3 = _bash('PKG_ID=t; PKG_NAME=t; PKG_VERSION=1; BASE_MIN=1; COMFY_MIN=0.1.0; PACKS=(); MODELS=(); (base_main --latest); echo RC=$?', env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_PINNED": "1"})
    assert "refused in pinned mode" in r3.stderr and "RC=2" in r3.stdout, r3.stdout + r3.stderr
    r4 = subprocess.run(["bash", str(BASE / "base.sh"), "latest"], capture_output=True, text=True, env={**os.environ, "BASE_FAKE_ROOT": str(tmp_path), "BASE_PINNED": "1"})
    assert r4.returncode == 2 and "refused in pinned mode" in r4.stderr, r4.stdout + r4.stderr
    r5 = _bash('base_env_setup; base_discover quiet; base_update_comfyui; echo OLD=$COMFY_OLD NEW=$COMFY_NEW', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "OLD=0.34.0 NEW=0.35.0" in r5.stdout, r5.stdout + r5.stderr                    # unpinned (a hand-installed pod): unchanged behaviour


def test_unit_pinned_venv_needs_the_seed_stamp_reuses_it_and_never_rebuilds(tmp_path):
    c = tmp_path / "ComfyUI"; c.mkdir(); (c / "main.py").write_text(""); (c / "requirements.txt").write_text("")
    env = {"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1", "BASE_PINNED": "1"}
    r = _bash('PKG_ID=t; base_env_setup; base_discover quiet; base_venv || echo VENV_RC=$?; echo RESULT=$BASE_VENV_RESULT FAILED=${#BASE_FAILED[@]}', env=env)
    assert "VENV_RC=1" in r.stdout and "RESULT=failed-pinned" in r.stdout and "FAILED=1" in r.stdout and "no .comfy-base-venv stamp" in r.stdout, r.stdout + r.stderr
    assert not (c / ".venv-cu130" / "bin" / "python").exists()                          # nothing is built in pinned mode
    # the seed arrives (an unpinned fake run writes the stamp), then pinned reuses it
    r2 = _bash('PKG_ID=t; base_env_setup; base_discover quiet; base_venv; echo RESULT=$BASE_VENV_RESULT', env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_NO_NET": "1"})
    assert "RESULT=fake" in r2.stdout, r2.stdout + r2.stderr
    r3 = _bash('PKG_ID=t; base_env_setup; base_discover quiet; base_venv; echo RESULT=$BASE_VENV_RESULT', env=env)
    assert "RESULT=reused" in r3.stdout, r3.stdout + r3.stderr
    # the real (networked) path reads the picks from the stamp and never calls the pickers
    src = code_only(_src(BASE / "lib" / "30-venv.sh"))
    pinned = src[src.index('if [ "$BASE_PINNED" = "1" ]; then\n    local stamp;'):src.index("owner=\"$(_base_venv_owner)\"")]
    assert "_base_python_pick" not in pinned.split("else")[0] and "torch=" in pinned and "no rebuild in pinned mode" in src


def test_unit_tokens_export_the_button_names_and_never_a_value(tmp_path):
    (tmp_path / "ComfyUI").mkdir(); (tmp_path / "ComfyUI" / "main.py").write_text("")
    r = _bash('TOKENS=("HF_TOKEN|optional|x"); base_env_setup; base_discover quiet; base_tokens; echo "B=${HUGGINGFACE_API_KEY:-unset}"; bash -c \'echo "child=${HUGGINGFACE_API_KEY:-unset}"\'',
              env={"BASE_FAKE_ROOT": str(tmp_path), "HF_TOKEN": "hf_abc"})
    assert "exported for the download buttons: HUGGINGFACE_API_KEY" in r.stdout and "B=hf_abc" in r.stdout and "child=hf_abc" in r.stdout, r.stdout + r.stderr
    note_line = [ln for ln in r.stdout.splitlines() if "exported for the download buttons" in ln][0]
    assert "hf_abc" not in note_line
    bare = tmp_path / "bare"; (bare / "ComfyUI").mkdir(parents=True); (bare / "ComfyUI" / "main.py").write_text("")
    r2 = _bash('TOKENS=("HF_TOKEN|optional|x"); base_env_setup; base_discover quiet; base_tokens; echo "B=${HUGGINGFACE_API_KEY:-unset}"', env={"BASE_FAKE_ROOT": str(bare)})
    assert "B=unset" in r2.stdout and "exported for the download buttons" not in r2.stdout, r2.stdout + r2.stderr


# ---------------------------------------------------------------- T22: the workflow's own `models` array, and the template tool (2.1.0)

def test_unit_stamp_models_writes_the_frontend_model_list_from_the_rows_and_is_idempotent(tmp_path):
    """py/stamp_models.py on the stub: every URL row becomes an entry (name, url, directory), LOCAL rows do not, hf:// becomes a
    Hub URL, the file keeps its indent, stamping twice changes nothing, --check reports staleness."""
    import shutil
    STUB = _stub()
    pkg = tmp_path / "stub"; shutil.copytree(STUB, pkg)
    wf = pkg / "stub-workflow.json"
    r = subprocess.run([sys.executable, str(BASE / "py" / "stamp_models.py"), str(pkg)], capture_output=True, text=True)
    assert r.returncode == 0 and "stamped 1 model entries" in r.stdout, r.stdout + r.stderr
    doc = json.loads(wf.read_text())
    assert doc["models"] == [{"name": "stub_vae.safetensors", "url": "https://huggingface.co/x/y/resolve/main/stub_vae.safetensors", "directory": "vae/Example/Real"}], doc["models"]
    assert doc["nodes"]                                                                     # the rest of the file is intact
    before = wf.read_text()
    r2 = subprocess.run([sys.executable, str(BASE / "py" / "stamp_models.py"), str(pkg)], capture_output=True, text=True)
    assert r2.returncode == 0 and "current" in r2.stdout and wf.read_text() == before
    assert subprocess.run([sys.executable, str(BASE / "py" / "stamp_models.py"), str(pkg), "--check"], capture_output=True, text=True).returncode == 0
    doc["models"] = []; wf.write_text(json.dumps(doc, indent=2) + "\n")
    r3 = subprocess.run([sys.executable, str(BASE / "py" / "stamp_models.py"), str(pkg), "--check"], capture_output=True, text=True)
    assert r3.returncode == 1 and "STALE" in r3.stdout
    # an hf:// snapshot row becomes a repository URL with the directory as its name
    sc = pkg / "stub-script.sh"
    sc.write_text(sc.read_text().replace(' "loras|SDXL|Detailers|stub_local.safetensors|LOCAL|0|a file that only exists on the pod|"',
                                         ' "RMBG|||BEN2/|hf://1038lab/BEN2|100|a snapshot|"'))
    subprocess.run([sys.executable, str(BASE / "py" / "stamp_models.py"), str(pkg)], check=True, capture_output=True)
    names = {(e["name"], e["url"], e["directory"]) for e in json.loads(wf.read_text())["models"]}
    assert ("BEN2", "https://huggingface.co/1038lab/BEN2", "RMBG") in names
    r4 = _bash('bash "$BASE_DIR/base.sh" stamp-models "%s" --check; echo RC=$?' % pkg)
    assert "RC=0" in r4.stdout, r4.stdout + r4.stderr


def test_unit_every_shipped_workflow_models_array_equals_its_rows():
    """All instances: every converted package's workflow carries the `models` array its rows say (2.1.0). A package not yet
    stamped is reported by name and skipped, never silently passed."""
    from basetest import converted_packages
    root = BASE.parents[1]
    unstamped, stale = [], []
    for d in converted_packages(root):
        s = d / (d.name + "-script.sh")
        if not s.exists():
            continue
        m = re.search(r'^[^#\n]*?\bWF_NAME="([^"]*)"', s.read_text(encoding="utf-8"), re.M)
        if not m or not m.group(1) or not (d / m.group(1)).exists():
            continue
        doc = json.loads((d / m.group(1)).read_text(encoding="utf-8"))
        if "models" not in doc:
            unstamped.append(d.name); continue
        r = subprocess.run([sys.executable, str(BASE / "py" / "stamp_models.py"), str(d), "--check"], capture_output=True, text=True)
        if r.returncode != 0:
            stale.append((d.name, r.stdout.strip()))
    assert not stale, stale
    if unstamped:
        pytest.skip("not stamped yet (run `base.sh stamp-models <dir>` in each package, with its bump): %s" % ", ".join(unstamped))



def test_unit_the_shared_library_link_replaces_comfyui_placeholders_but_never_real_models(tmp_path):
    """MEASURED: a freshly materialised ComfyUI tree is never empty. It ships a placeholder in every category
    folder (put_checkpoints_here and friends), so counting "has files" as "has models" made the link refuse on
    exactly the fresh machine it exists for, sending every machine back to its own copy of the library, silently
    and looking like it had worked."""
    lib = tmp_path / "library"
    (lib / "models").mkdir(parents=True)

    comfy = tmp_path / "ComfyUI"
    for d in ("checkpoints", "loras", "vae"):
        (comfy / "models" / d).mkdir(parents=True)
        (comfy / "models" / d / ("put_%s_here" % d)).write_text("")
    r = _bash(f'COMFY="{comfy}"; BASE_LIBRARY="{lib}"; EXTRA_YAML="{comfy}/extra_model_paths.yaml"; _base_library_link')
    assert r.returncode == 0, r.stdout + r.stderr
    assert (comfy / "models").is_symlink(), "placeholders are not content: it must link. said: " + r.stdout
    assert os.readlink(str(comfy / "models")) == str(lib / "models")
    # with models/ pointing AT the library, a search path to the same place would list every model twice
    assert not (comfy / "extra_model_paths.yaml").exists(), r.stdout

    # a real model IS content: never moved without being asked, and the library becomes a search path instead
    comfy2 = tmp_path / "ComfyUI2"
    (comfy2 / "models" / "loras").mkdir(parents=True)
    (comfy2 / "models" / "loras" / "put_loras_here").write_text("")
    (comfy2 / "models" / "loras" / "real.safetensors").write_text("x")
    r2 = _bash(f'COMFY="{comfy2}"; BASE_LIBRARY="{lib}"; EXTRA_YAML="{comfy2}/extra_model_paths.yaml"; _base_library_link')
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert not (comfy2 / "models").is_symlink(), "a tree holding a real model must be left alone"
    assert (comfy2 / "models" / "loras" / "real.safetensors").exists(), "it must never delete a model"
    assert "real.safetensors" in r2.stdout, "it must name what stopped it: " + r2.stdout
    y = (comfy2 / "extra_model_paths.yaml").read_text()
    assert "comfy-library:" in y and str(lib / "models") in y

    # the migration this function's own warning tells people to run: move the models, run again. The link is made
    # AND the now-redundant search path is dropped, or every model would be listed twice in every dropdown.
    (comfy2 / "models" / "loras" / "real.safetensors").unlink()
    r2b = _bash(f'COMFY="{comfy2}"; BASE_LIBRARY="{lib}"; EXTRA_YAML="{comfy2}/extra_model_paths.yaml"; _base_library_link')
    assert r2b.returncode == 0, r2b.stdout + r2b.stderr
    assert (comfy2 / "models").is_symlink(), r2b.stdout
    assert not (comfy2 / "extra_model_paths.yaml").exists(), (
        "the search path must go when models/ becomes the library itself, or every model is listed twice: " + r2b.stdout)

    # no library configured: nothing happens at all, on any host
    comfy3 = tmp_path / "ComfyUI3"
    (comfy3 / "models").mkdir(parents=True)
    r3 = _bash(f'COMFY="{comfy3}"; BASE_LIBRARY=""; EXTRA_YAML="{comfy3}/extra_model_paths.yaml"; _base_library_link')
    assert r3.returncode == 0 and not (comfy3 / "models").is_symlink()
    assert not (comfy3 / "extra_model_paths.yaml").exists()


def test_unit_the_install_lock_serialises_installs_only_on_a_shared_volume(tmp_path):
    """2.5.0: with the whole root on a store several machines mount, two installs writing one venv corrupts it,
    because pip and uv write in place and neither expects a second writer. RUNNING is never locked: running only
    reads, which is what makes several GPUs on one store safe. mkdir is the lock because it is atomic over NFSv4."""
    vol = tmp_path / "vol"; (vol / "comfy-base" / "state").mkdir(parents=True)
    lock = vol / "comfy-base" / "state" / "install.lock"
    env = {"BASE_VOLUME": str(vol), "BASE_HOST": "local", "BASE_VOLUME_SHARED": "1", "BASE_DRY": "0"}

    # a real run installs the traps first (base_main does), and the EXIT trap is what releases the lock, so a
    # finished run never blocks the next machine. Without the traps the lock would outlive the process, which is
    # why this asserts the release rather than assuming it.
    r = _bash('_base_install_traps; _base_install_lock && echo GOT', env=env)
    assert "GOT" in r.stdout, r.stdout + r.stderr
    assert not lock.exists(), "the exit trap must release the lock: " + r.stdout

    # a lock a LIVE run holds is honoured
    lock.mkdir()
    (lock / "owner").write_text("host=other-machine\npid=1\nsince=%d\n" % int(__import__("time").time()))
    r = _bash('_base_install_lock && echo GOT || echo BLOCKED', env={**env, "BASE_LOCK_WAIT": "10"})
    assert "GOT" not in r.stdout, "a live lock must not be taken: " + r.stdout
    assert "other-machine is installing" in r.stdout, r.stdout

    # a lock whose owner died is honoured for two hours, then broken WITH THE OWNER NAMED
    (lock / "owner").write_text("host=dead-machine\npid=1\nsince=%d\n" % (int(__import__("time").time()) - 10800))
    r = _bash('_base_install_lock && echo GOT', env=env)
    assert "GOT" in r.stdout and "breaking" in r.stdout and "dead-machine" in r.stdout, r.stdout

    # and none of it happens when the root is the machine's own disk
    lock.mkdir(exist_ok=True)
    r = _bash('_base_install_lock && echo GOT', env={**env, "BASE_VOLUME_SHARED": "0"})
    assert "GOT" in r.stdout and "installing" not in r.stdout, r.stdout
