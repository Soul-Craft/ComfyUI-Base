"""unit tier, 3.0.0: always the newest, for everything the base installs. Every test runs the real library against a
throwaway tree, with uv, apt and the GPU tools replaced by stand-ins on PATH; nothing here reaches the network or the OS."""
import json, os, pathlib, re, stat, subprocess, sys, textwrap, pytest
pytestmark = pytest.mark.unit
BASE = pathlib.Path(__file__).resolve().parents[1]


def _bash(snippet, env=None, cwd=None):
    e = dict(os.environ)
    for k in list(e):
        if k.startswith(("BASE_", "COMFY_REF", "COMFY_TAG", "SAGE_")):
            e.pop(k)
    e.update(env or {})
    return subprocess.run(["bash", "-c", f'source "{BASE}/base.sh"; {snippet}'], capture_output=True, text=True, env=e, cwd=cwd)


def _stub(d, name, body):
    p = pathlib.Path(d) / name
    p.write_text("#!/bin/bash\n" + textwrap.dedent(body))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


def _git(*a, cwd=None):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


# ---------------------------------------------------------------- A: ComfyUI, the newest release or a newer COMFY_REF

def _comfy_repo(tmp_path):
    """v0.34.0, v0.35.0 on master, one untagged master commit after it (still reporting 0.35.0, as master does), and
    v0.35.1 on a side branch cut from v0.35.0: the shape ComfyUI has (v0.37.x beside master)."""
    origin = tmp_path / "origin.git"; work = tmp_path / "ComfyUI"
    _git("init", "-q", "--bare", str(origin))
    _git("clone", "-q", str(origin), str(work))
    cfg = ["-c", "user.email=t@t", "-c", "user.name=t"]
    def commit(ver, msg, tag=None):
        (work / "main.py").write_text(msg); (work / "comfyui_version.py").write_text(f'__version__ = "{ver}"\n')
        _git("add", ".", cwd=work); _git(*cfg, "commit", "-q", "-m", msg, cwd=work)
        if tag:
            _git("tag", tag, cwd=work)
    _git("checkout", "-q", "-b", "master", cwd=work)
    commit("0.34.0", "a", "v0.34.0"); commit("0.35.0", "b", "v0.35.0")
    _git("checkout", "-q", "-b", "release-0.35", cwd=work); commit("0.35.1", "side", "v0.35.1")
    _git("checkout", "-q", "master", cwd=work); commit("0.35.0", "master-fix")
    master = _git("rev-parse", "HEAD", cwd=work)
    _git("push", "-q", "origin", "master", "release-0.35", "--tags", cwd=work)
    _git("checkout", "-q", "v0.34.0", cwd=work)
    return work, master


def test_unit_comfyui_default_is_the_newest_release_even_on_a_side_branch(tmp_path):
    work, _ = _comfy_repo(tmp_path)
    r = _bash('base_env_setup; base_discover quiet; base_update_comfyui; echo OLD=$COMFY_OLD NEW=$COMFY_NEW; echo INFO=$COMFY_REF_INFO; git -C "$COMFY" rev-parse --abbrev-ref HEAD', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "OLD=0.34.0 NEW=0.35.1" in r.stdout and "INFO=v0.35.1 @" in r.stdout and "comfy-base-stable" in r.stdout, r.stdout + r.stderr


def test_unit_comfy_ref_master_is_honoured_and_says_what_it_reports(tmp_path):
    work, master = _comfy_repo(tmp_path)
    r = _bash('base_env_setup; base_discover quiet; base_update_comfyui; echo NEW=$COMFY_NEW; echo INFO=$COMFY_REF_INFO; echo FAILED=${#BASE_FAILED[@]}; git -C "$COMFY" rev-parse HEAD',
              env={"BASE_FAKE_ROOT": str(tmp_path), "COMFY_REF": "master"})
    assert master in r.stdout and "FAILED=0" in r.stdout, r.stdout + r.stderr
    assert "INFO=master @" in r.stdout and "newest release v0.35.1" in r.stdout, r.stdout   # master reports 0.35.0, below the release


def test_unit_comfy_ref_full_sha_works_and_a_short_one_is_refused(tmp_path):
    work, master = _comfy_repo(tmp_path)
    r = _bash('base_env_setup; base_discover quiet; base_update_comfyui; echo FAILED=${#BASE_FAILED[@]}; git -C "$COMFY" rev-parse HEAD', env={"BASE_FAKE_ROOT": str(tmp_path), "COMFY_REF": master})
    assert r.stdout.strip().endswith(master) and "FAILED=0" in r.stdout, r.stdout + r.stderr
    r2 = _bash('base_env_setup; base_discover quiet; base_update_comfyui; echo FAILED=${#BASE_FAILED[@]}', env={"BASE_FAKE_ROOT": str(tmp_path), "COMFY_REF": master[:12]})
    assert "FAILED=1" in r2.stdout and "full 40-hex" in r2.stdout, r2.stdout + r2.stderr


def test_unit_comfy_ref_older_than_the_newest_release_is_refused(tmp_path):
    work, _ = _comfy_repo(tmp_path)
    before = _git("rev-parse", "HEAD", cwd=work)
    r = _bash('base_env_setup; base_discover quiet; base_update_comfyui; echo FAILED=${#BASE_FAILED[@]}', env={"BASE_FAKE_ROOT": str(tmp_path), "COMFY_REF": "v0.34.0"})
    assert "FAILED=1" in r.stdout and "OLDER than the newest release" in r.stdout, r.stdout + r.stderr
    assert _git("rev-parse", "HEAD", cwd=work) == before


def test_unit_retired_comfy_tag_and_pinned_are_notes_never_freezes(tmp_path):
    work, _ = _comfy_repo(tmp_path)
    r = _bash('base_env_setup; base_discover quiet; base_update_comfyui; echo NEW=$COMFY_NEW', env={"BASE_FAKE_ROOT": str(tmp_path), "COMFY_TAG": "v0.34.0", "BASE_PINNED": "1"})
    assert "NEW=0.35.1" in r.stdout and "retired" in (r.stdout + r.stderr), r.stdout + r.stderr


def test_unit_dry_run_reads_the_refs_own_version_and_leaves_the_tree(tmp_path):
    work, _ = _comfy_repo(tmp_path)
    before = _git("rev-parse", "HEAD", cwd=work)
    r = _bash("COMFY_MIN=0.35.0; base_env_setup; base_discover quiet; base_update_comfyui; base_comfy_gate; echo GATE=$? NEW=$COMFY_NEW",
              env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_DRY": "1", "COMFY_REF": "master"})
    assert "GATE=0 NEW=0.35.0" in r.stdout, r.stdout + r.stderr
    assert _git("rev-parse", "HEAD", cwd=work) == before


# ---------------------------------------------------------------- C: py/reqlift.py lifts every upstream hold

def test_unit_reqlift_lifts_holds_keeps_floors_markers_and_names_the_pack(tmp_path):
    (tmp_path / "inc.txt").write_text("included==1.0\n")
    (tmp_path / "req.txt").write_text(textwrap.dedent("""\
        comfyui-frontend-package==1.52.7   # pinned by ComfyUI
        protobuf>=3.20.2,<6.0.0
        pydantic~=2.0
        numpy>=1.25.0,!=2.0.1
        pywin32==306 ; platform_system == "Windows"
        requests[socks]<=2.0
        torch==2.1.0
        torchsde==0.2.6
        -r inc.txt
        -c constraints.txt
        --index-url https://example.invalid/simple
        git+https://github.com/o/r.git@v1.2#egg=thing
        withhash==1.0 --hash=sha256:abc
        https://example.invalid/a-1.0.whl
        """))
    out, mp = tmp_path / "derived.txt", tmp_path / "map.tsv"
    r = subprocess.run([sys.executable, str(BASE / "py" / "reqlift.py"), "--out", str(out), "--map", str(mp),
                        "--extra", "PIP_EXTRA=nvidia-vfx==1.0", f"PackA={tmp_path / 'req.txt'}"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    d = out.read_text().splitlines()
    assert "comfyui-frontend-package" in d and "protobuf>=3.20.2" in d and "pydantic>=2.0" in d
    assert "numpy!=2.0.1,>=1.25.0" in d or "numpy>=1.25.0,!=2.0.1" in d
    assert any(l.startswith("pywin32 ; platform_system") and "==" in l for l in d)          # the marker's == stays
    assert "requests[socks]" in d and "included" in d and "torchsde" in d and "nvidia-vfx" in d
    assert not any(l.startswith("torch") and not l.startswith("torchsde") for l in d)       # the torch step owns torch
    assert "--extra-index-url https://example.invalid/simple" in d                          # never a replacement for PyPI
    assert "git+https://github.com/o/r.git#egg=thing" in d and "withhash" in d
    assert "https://example.invalid/a-1.0.whl" in d
    rows = [l.split("\t") for l in mp.read_text().splitlines()]
    who = {r_[0]: r_[1] for r_ in rows if r_[0]}
    assert who["comfyui-frontend-package"] == "PackA" and who["nvidia-vfx"] == "PIP_EXTRA"
    assert any(r_[2] == "dropped-constraints" for r_ in rows) and any(r_[2] == "held-archive" for r_ in rows)
    assert "lifted\tPackA\tcomfyui-frontend-package" in r.stdout


# ---------------------------------------------------------------- D: torch from uv's --torch-backend=auto, never backwards

def _uv_torch(tmp_path, auto, no_family=""):
    """uv stand-in: `--torch-backend auto` answers `auto`; the torch family (with torchaudio) fails on `no_family`."""
    d = tmp_path / "bin"; d.mkdir(exist_ok=True)
    _stub(d, "uv", f"""\
        if [ "$1 $2" = "pip install" ] && [ "$3" = "--help" ]; then echo "possible values: auto, cpu, cu132, cu130, cu129"; exit 0; fi
        b=""; while [ $# -gt 0 ]; do [ "$1" = "--torch-backend" ] && b="$2"; shift; done
        [ "$b" = auto ] && b="{auto}"
        req="$(cat)"
        case "$req" in *torchaudio*) [ "$b" = "{no_family}" ] && {{ echo "no torchaudio for $b" >&2; exit 1; }};; esac
        if [ "$b" = cpu ]; then echo "torch==2.14.0"; else echo "torch==2.14.0+$b"; fi
        """)
    return {"UV": str(d / "uv"), "PATH": f"{d}:{os.environ['PATH']}"}


@pytest.mark.parametrize("auto,keep,want,why", [
    ("cu130", "", "2.14.0 cu130", "uv --torch-backend=auto"),
    ("cu130", "cu132", "2.14.0 cu132", "never backwards"),
    ("cpu", "", "2.14.0 cu132", "newest CUDA backend uv knows"),
    ("cpu", "cu130", "2.14.0 cu130", "the venv's cu130 is kept"),
    ("cu132", "cu120", "2.14.0 cu132", "newer CUDA major"),
    ("cu120", "cu130", "2.14.0 cu130", "OLDER CUDA major"),
])
def test_unit_torch_pick_asks_uv_and_never_moves_a_shared_venv_backwards(tmp_path, auto, keep, want, why):
    env = dict(os.environ); env.update(_uv_torch(tmp_path, auto))
    r = subprocess.run([sys.executable, str(BASE / "py" / "torch_pick.py"), "3.14", keep], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout.strip() == want and why in r.stderr, (r.stdout, r.stderr)


def test_unit_no_cuda_or_index_literal_and_no_pin_remains_in_the_library():
    for p in [*(BASE / "lib").glob("*.sh"), *(BASE / "py").glob("*.py")]:
        code = "\n".join(l for l in p.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#"))
        assert not re.search(r"whl/cu\d|\+cu\d{3}\b", code), p.name                        # uv picks the CUDA build
        assert not re.search(r"install[^\n]*\s[\"']?[A-Za-z0-9_.-]+==[0-9]", code), p.name  # no package pinned to a number


# ---------------------------------------------------------------- B: packs at their HEAD, never backwards

def _pack_origin(tmp_path, name):
    origin = tmp_path / f"{name}.git"; seed = tmp_path / f"{name}-seed"
    _git("init", "-q", "--bare", str(origin)); _git("clone", "-q", str(origin), str(seed))
    cfg = ["-c", "user.email=t@t", "-c", "user.name=t"]
    for i in (1, 2):
        (seed / "__init__.py").write_text(f"v = {i}\n"); _git("add", ".", cwd=seed); _git(*cfg, "commit", "-q", "-m", f"c{i}", cwd=seed)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed)
    _git("--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main")
    return origin, _git("rev-parse", "HEAD", cwd=seed), _git("rev-parse", "HEAD~1", cwd=seed)


def _packs_env(tmp_path):
    c = tmp_path / "ComfyUI"; (c / "custom_nodes").mkdir(parents=True, exist_ok=True); (c / "main.py").write_text("")
    return {"BASE_FAKE_ROOT": str(tmp_path)}


def test_unit_packs_move_to_head_stash_edits_and_print_their_records(tmp_path):
    origin, head, old = _pack_origin(tmp_path, "PackA")
    env = _packs_env(tmp_path); cn = tmp_path / "ComfyUI" / "custom_nodes"
    _git("clone", "-q", str(origin), str(cn / "PackA")); _git("checkout", "-q", "--detach", old, cwd=cn / "PackA")
    (cn / "PackA" / "__init__.py").write_text("local edit\n")
    row = f"PackA|{origin}|{'0' * 40}||test pack"
    r = _bash(f'base_env_setup; base_discover quiet; BASE_PACKS=(); PACKS=("{row}"); base_packs git; echo FAILED=${{#BASE_FAILED[@]}}', env=env)
    assert _git("rev-parse", "HEAD", cwd=cn / "PackA") == head, r.stdout + r.stderr
    assert "stashed as 'comfy-base" in r.stdout and "FAILED=0" in r.stdout, r.stdout + r.stderr
    assert f'"PackA|{origin}|{head}||test pack"' in r.stdout                                  # the record podctl saves


def test_unit_a_pack_ahead_of_its_remote_is_never_moved_back(tmp_path):
    origin, head, _ = _pack_origin(tmp_path, "PackB")
    env = _packs_env(tmp_path); cn = tmp_path / "ComfyUI" / "custom_nodes"
    _git("clone", "-q", str(origin), str(cn / "PackB"))
    (cn / "PackB" / "x.py").write_text("x"); _git("add", ".", cwd=cn / "PackB")
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "ahead", cwd=cn / "PackB")
    ahead = _git("rev-parse", "HEAD", cwd=cn / "PackB")
    row = f"PackB|{origin}|{'0' * 40}||t"
    _bash(f'base_env_setup; base_discover quiet; BASE_PACKS=(); PACKS=("{row}"); base_packs git', env=env)
    assert _git("rev-parse", "HEAD", cwd=cn / "PackB") == ahead


def test_unit_a_declared_pack_that_is_not_a_checkout_goes_aside_and_is_cloned(tmp_path):
    origin, head, _ = _pack_origin(tmp_path, "PackC")
    env = _packs_env(tmp_path); cn = tmp_path / "ComfyUI" / "custom_nodes"
    (cn / "PackC").mkdir(); (cn / "PackC" / "__init__.py").write_text("registry copy")
    row = f"PackC|{origin}|{'0' * 40}||t"
    r = _bash(f'base_env_setup; base_discover quiet; BASE_PACKS=(); PACKS=("{row}"); base_packs git', env=env)
    assert (cn / "PackC" / ".git").exists() and _git("rev-parse", "HEAD", cwd=cn / "PackC") == head, r.stdout + r.stderr
    assert list((cn / "comfy-base-aside.disabled").glob("PackC-*"))                          # ComfyUI skips *.disabled


def test_unit_an_undeclared_git_pack_moves_to_its_head_too(tmp_path):
    origin, head, old = _pack_origin(tmp_path, "PackD")
    env = _packs_env(tmp_path); cn = tmp_path / "ComfyUI" / "custom_nodes"
    _git("clone", "-q", str(origin), str(cn / "PackD")); _git("checkout", "-q", "--detach", old, cwd=cn / "PackD")
    _bash('base_env_setup; base_discover quiet; BASE_PACKS=(); PACKS=(); base_packs git', env=env)
    assert _git("rev-parse", "HEAD", cwd=cn / "PackD") == head


def test_unit_list_packs_notes_differing_records_and_fails_on_differing_urls():
    a = 'BASE_PACKS=("X|https://h/x|' + "1" * 40 + '||" "X|https://h/x.git|' + "2" * 40 + '||")'
    r = _bash(f"{a}; base_list_packs; echo RC=$?")
    assert "RC=0" in r.stdout and "records differ" in r.stderr, r.stdout + r.stderr
    b = 'BASE_PACKS=("X|https://h/x|' + "1" * 40 + '||" "X|https://h/other|' + "1" * 40 + '||")'
    r2 = _bash(f"{b}; base_list_packs; echo RC=$?")
    assert "RC=1" in r2.stdout and "two URLs" in r2.stderr, r2.stdout + r2.stderr


# ---------------------------------------------------------------- C: the lift, the override loop, the import check

def _lift_env(tmp_path, outdated_rounds, override_fails=()):
    """A fake venv whose python is this interpreter, and a uv stand-in that records every call. `outdated_rounds` is the
    JSON `uv pip list --outdated` answers, one per call; an install whose overrides name a package in `override_fails`
    exits 1."""
    c = tmp_path / "ComfyUI"; c.mkdir(parents=True, exist_ok=True); (c / "main.py").write_text("")
    (c / "requirements.txt").write_text("comfyui-frontend-package==1.52.7\nprotobuf\n")
    v = c / ".venv-cu130" / "bin"; v.mkdir(parents=True); (v / "python").symlink_to(sys.executable)
    (c / ".venv-cu130" / ".comfy-base-venv").write_text("python=3 torch=0 backend=cu130")
    d = tmp_path / "bin"; d.mkdir(); log = tmp_path / "uv.log"; n = tmp_path / "n"
    (tmp_path / "rounds.json").write_text(json.dumps(outdated_rounds))
    fails = " ".join(override_fails)
    _stub(d, "uv", f"""\
        echo "$*" >> "{log}"
        if [ "$1 $2" = "pip list" ]; then
          i=$(cat "{n}" 2>/dev/null || echo 0); echo $((i+1)) > "{n}"
          "{sys.executable}" -c 'import json,sys; r=json.load(open(sys.argv[1])); i=int(sys.argv[2]); print(json.dumps(r[min(i,len(r)-1)]))' "{tmp_path / 'rounds.json'}" "$i"
          exit 0
        fi
        if [ "$1 $2" = "pip freeze" ]; then echo "protobuf==5.29.6"; exit 0; fi
        if [ "$1 $2" = "pip check" ]; then exit 0; fi
        if [ "$1 $2" = "pip install" ]; then
          ov=""; prev=""; for a in "$@"; do [ "$prev" = "--overrides" ] && ov="$a"; prev="$a"; done
          for f in {fails}; do [ -n "$ov" ] && grep -q "^$f==" "$ov" && exit 1; done
          exit 0
        fi
        if [ "$1" = run ]; then shift; while [ "$1" != python ]; do shift; done; shift; exec "{sys.executable}" "$@"; fi
        exit 0
        """)
    # HOME too: base_env_setup puts ~/.local/bin first, where the Mac's real uv lives, and a real uv here would install
    return {"BASE_FAKE_ROOT": str(tmp_path), "PATH": f"{d}:/usr/bin:/bin", "HOME": str(tmp_path / "home"), "BASE_FAKE_HOLDERS": "/dev/null"}, log


def test_unit_the_lift_forces_a_capped_package_to_its_newest(tmp_path):
    env, log = _lift_env(tmp_path, [[{"name": "protobuf", "version": "5.29.6", "latest_version": "7.36.2"}], []])
    r = _bash('base_env_setup; base_discover quiet; base_venv_latest; echo FAILED=${#BASE_FAILED[@]}; printf "ROW %s\\n" "${BASE_LIFT_ROWS[@]}"; cat "$BASE_LIFT_DIR/overrides.txt"', env=env)
    assert "protobuf==7.36.2" in r.stdout and "FAILED=0" in r.stdout, r.stdout + r.stderr
    assert "ROW protobuf → 7.36.2 (forced" in r.stdout and "ROW comfyui-frontend-package" in r.stdout, r.stdout
    calls = [l for l in log.read_text().splitlines() if l.startswith("pip install")]
    assert calls and all(" -r " in l for l in calls) and any("--overrides" in l for l in calls), calls


def test_unit_a_package_the_base_could_move_and_did_not_fails_the_run(tmp_path):
    stuck = [{"name": "protobuf", "version": "5.29.6", "latest_version": "7.36.2"}]
    env, _ = _lift_env(tmp_path, [stuck, stuck, stuck, stuck, stuck])
    r = _bash('base_env_setup; base_discover quiet; base_venv_latest; printf "F %s\\n" "${BASE_FAILED[@]}"', env=env)
    assert "still behind after the override loop: protobuf 5.29.6 < 7.36.2" in r.stdout, r.stdout + r.stderr


def test_unit_a_newest_that_cannot_install_here_is_named_upstream_not_failed(tmp_path):
    env, _ = _lift_env(tmp_path, [[{"name": "oddpkg", "version": "1.0", "latest_version": "2.0"}], [{"name": "oddpkg", "version": "1.0", "latest_version": "2.0"}]], override_fails=("oddpkg",))
    r = _bash('base_env_setup; base_discover quiet; base_venv_latest; echo FAILED=${#BASE_FAILED[@]}; printf "U %s\\n" "${BASE_UPSTREAM[@]}"', env=env)
    assert "FAILED=0" in r.stdout and "U oddpkg 2.0: cannot be installed here" in r.stdout, r.stdout + r.stderr


def test_unit_a_pack_that_cannot_import_is_named_and_the_restart_goes_on(tmp_path):
    env, _ = _lift_env(tmp_path, [[]])
    (tmp_path / "ComfyUI" / "main.py").write_text('print("   0.1 seconds (IMPORT FAILED): /x/custom_nodes/PackZ")\n')
    r = _bash('base_env_setup; base_discover quiet; BASE_VENV_RESULT=reused; base_import_check; echo FAILED=${#BASE_FAILED[@]} BLOCK=${BASE_BLOCK_RESTART:-0}; printf "U %s\\n" "${BASE_UPSTREAM[@]}"', env=env)
    assert "FAILED=0 BLOCK=0" in r.stdout and "U pack PackZ does not import" in r.stdout, r.stdout + r.stderr


def test_unit_comfyui_core_failing_rolls_back_only_what_this_run_changed(tmp_path):
    env, log = _lift_env(tmp_path, [[]])
    flag = tmp_path / "rolled"
    (tmp_path / "ComfyUI" / "main.py").write_text(f'import os, sys\nif not os.path.exists({str(flag)!r}):\n    print("Traceback: core"); sys.exit(1)\n')
    snap = tmp_path / "snap.txt"; snap.write_text("protobuf==5.29.6\ncomfy-aimdo==0.5.5\n")
    r = _bash(f'base_env_setup; base_discover quiet; _base_lift_dir; BASE_VENV_SNAPSHOT="{snap}"; BASE_VENV_RESULT=reused; '
              f'_base_uv(){{ if [ "$1 $2" = "pip freeze" ]; then echo "protobuf==5.29.6"; echo "comfy-aimdo==0.6.0"; else touch "{flag}"; fi; }}; '
              'base_import_check; printf "F %s\\n" "${BASE_FAILED[@]}"; cat "$BASE_LIFT_DIR/rollback.txt"', env=env)
    rb = next(pathlib.Path(tmp_path).rglob("rollback.txt")).read_text().split()
    assert rb == ["comfy-aimdo==0.5.5"], rb                                                # only what this run changed
    assert "did not start with the newest of: comfy-aimdo==0.5.5" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------- G: the OS and the NVIDIA driver, against stand-ins

def _apt_env(tmp_path, installed, running="580.178.04", module="615.71.09", kernels=("6.8.0-139-generic", "6.8.0-142-generic"), host="verda"):
    d = tmp_path / "fakeapt"; d.mkdir(); log = tmp_path / "apt.log"
    for k in kernels:
        (d / "root" / "lib" / "modules" / k).mkdir(parents=True)
    _stub(d, "apt-get", f'echo "apt-get $*" >> "{log}"; [ "$*" != "${{*#*full-upgrade}}" ] && echo "3 upgraded, 1 newly installed, 0 to remove and 0 not upgraded."; exit 0\n')
    _stub(d, "apt-cache", 'echo "  Candidate: 615.71.09-1"\n')
    _stub(d, "apt-mark", f'echo "apt-mark $*" >> "{log}"\n')
    (tmp_path / "installed.txt").write_text("".join(p_ + "\n" for p_ in installed))
    _stub(d, "dpkg-query", r"""
        inst="INSTFILE"; fmt="$3"; shift 3
        case "$fmt" in
          '${db:Status-Abbrev} ${Package}\n') sed 's/^/ii  /' "$inst" ;;
          '${db:Status-Abbrev}') if [ "$1" = nvidia-open ]; then grep -qx nvidia-open "$inst" && printf 'ii ' || printf 'un '; else printf 'ii '; fi ;;
          '${Package}\n') cat "$inst" ;;
          '${Version}') echo 615.71.09 ;;
        esac
        exit 0
        """.replace("INSTFILE", str(tmp_path / "installed.txt")))
    _stub(d, "nvidia-smi", f'case "$*" in *driver_version*) echo "{running}";; *) echo "CUDA Version: 13.0";; esac\n')
    _stub(d, "modinfo", f'[ "$2" = "{kernels[-1]}" ] && [ -n "{module}" ] && echo "{module}"; exit 0\n')
    _stub(d, "dkms", 'echo "nvidia/580.178.04, 6.8.0-139-generic, x86_64: installed"\n')
    _stub(d, "systemctl", f'echo "systemctl $*" >> "{log}"; exit 0\n')
    _stub(d, "uname", f'[ "$1" = -r ] && echo "{kernels[0]}" || /usr/bin/uname "$@"\n')
    _stub(d, "curl", "exit 0\n")
    c = tmp_path / "ComfyUI"; c.mkdir(); (c / "main.py").write_text("")
    return {"BASE_FAKE_ROOT": str(tmp_path), "BASE_FAKE_APT": str(d), "BASE_HOST": host, "HOME": str(tmp_path / "home")}, log


def test_unit_the_driver_switches_family_in_one_transaction_first_then_stops_for_a_restart(tmp_path):
    env, log = _apt_env(tmp_path, ["nvidia-driver-580-server-open", "nvidia-dkms-580-server-open",
                                   "linux-modules-nvidia-580-server-open-6.8.0-139-generic", "libnvidia-compute-580-server",
                                   "nvidia-container-toolkit"])
    r = _bash("base_env_setup; base_discover quiet; base_system; echo RC=$? REBOOT=$BASE_REBOOT_REQUIRED; printf 'F %s\\n' \"${BASE_FAILED[@]}\"", env=env)
    lines = log.read_text().splitlines()
    sw = next(i for i, l in enumerate(lines) if "install --purge nvidia-open cuda-toolkit" in l)
    fu = next(i for i, l in enumerate(lines) if "full-upgrade" in l)
    assert sw < fu, lines                                                                       # the switch comes FIRST
    for p in ("nvidia-driver-580-server-open-", "nvidia-dkms-580-server-open-", "linux-modules-nvidia-580-server-open-6.8.0-139-generic-", "libnvidia-compute-580-server-"):
        assert p in lines[sw], (p, lines[sw])
    assert "nvidia-container-toolkit-" not in lines[sw]
    assert "RC=10" in r.stdout and "restart required: driver 580.178.04 -> 615.71.09" in r.stdout, r.stdout + r.stderr
    stop = next(i for i, l in enumerate(lines) if "systemctl stop comfy-base-boot" in l)
    assert stop < sw, lines                                                                     # the unit stops before the purge


def test_unit_no_module_for_the_newest_kernel_fails_and_advises_no_restart(tmp_path):
    env, _ = _apt_env(tmp_path, ["nvidia-open"], running="615.71.09", module="")
    r = _bash("base_env_setup; base_discover quiet; base_system; echo RC=$?; printf 'F %s\\n' \"${BASE_FAILED[@]}\"", env=env)
    assert "RC=10" in r.stdout and "not built for kernel 6.8.0-142-generic" in r.stdout, r.stdout + r.stderr


def test_unit_a_kernel_only_update_is_a_warning(tmp_path):
    env, _ = _apt_env(tmp_path, ["nvidia-open"], running="615.71.09", module="615.71.09")
    r = _bash("base_env_setup; base_discover quiet; base_system; echo RC=$? F=${#BASE_FAILED[@]}; printf 'W %s\\n' \"${BASE_WARN[@]}\"", env=env)
    assert "RC=0 F=0" in r.stdout and "restart when convenient" in r.stdout, r.stdout + r.stderr


def test_unit_runpod_holds_the_hosts_driver_and_installs_none(tmp_path):
    env, log = _apt_env(tmp_path, ["libnvidia-compute-580"], host="runpod")
    _bash("base_env_setup; base_discover quiet; base_system", env=env)
    text = log.read_text()
    assert "apt-mark hold" in text and "nvidia-open" not in text, text


def test_unit_no_root_warns_and_the_run_goes_on(tmp_path):
    env, log = _apt_env(tmp_path, [])
    env["BASE_FAKE_APT_NOROOT"] = "1"
    r = _bash("base_env_setup; base_discover quiet; base_system; echo RC=$? F=${#BASE_FAILED[@]}; printf 'W %s\\n' \"${BASE_WARN[@]}\"", env=env)
    assert "RC=0 F=0" in r.stdout and "no root and no passwordless sudo" in r.stdout and not log.exists(), r.stdout


def test_unit_a_fake_run_without_the_stand_ins_leaves_the_os_alone(tmp_path):
    (tmp_path / "ComfyUI").mkdir(); (tmp_path / "ComfyUI" / "main.py").write_text("")
    r = _bash("base_env_setup; base_discover quiet; base_system; echo R=$BASE_SYSTEM_RESULT", env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "R=skipped (fake)" in r.stdout, r.stdout + r.stderr


def test_unit_sudo_carries_the_non_interactive_settings(tmp_path):
    d = tmp_path / "bin"; d.mkdir()
    _stub(d, "sudo", 'echo "SUDO $*"\n'); _stub(d, "id", 'echo 1000\n')
    r = _bash("_base_apt update", env={"PATH": f"{d}:{os.environ['PATH']}"})
    assert "SUDO -n env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l" in r.stdout and "--force-confold" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------- H: what the install says it put on the store

def test_unit_the_versions_block_names_everything_and_the_store_only_when_shared(tmp_path):
    (tmp_path / "ComfyUI").mkdir(); (tmp_path / "ComfyUI" / "main.py").write_text("")
    snip = ('base_env_setup; base_discover quiet; COMFY_NEW=0.37.2; COMFY_REF_INFO="v0.37.2 @ 830232b"; BASE_TORCH_BACKEND=cu132; '
            'BASE_FRONTEND_INFO="frontend 1.54.7"; BASE_LIFT_RESULT=ok; BASE_UPSTREAM=("pack X needs upgrading upstream"); _base_versions_block')
    r = _bash(snip, env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "VERSIONS   base" in r.stdout and "v0.37.2 @ 830232b" in r.stdout and "frontend 1.54.7" in r.stdout and "(cu132)" in r.stdout, r.stdout
    assert "UPSTREAM   1" in r.stdout and "STORE" not in r.stdout
    r2 = _bash(snip, env={"BASE_FAKE_ROOT": str(tmp_path), "BASE_VOLUME_SHARED": "1"})
    assert "EVERY machine on this store" in r2.stdout, r2.stdout


def test_unit_a_machine_on_an_older_cuda_major_is_named(tmp_path):
    (tmp_path / "ComfyUI").mkdir(); (tmp_path / "ComfyUI" / "main.py").write_text("")
    r = _bash('base_env_setup; base_discover quiet; mkdir -p "$BASE_STATE/machines"; printf "host=other\\ncuda=12.8\\n" > "$BASE_STATE/machines/other.env"; '
              'BASE_TORCH_BACKEND=cu132; _base_machines_behind', env={"BASE_FAKE_ROOT": str(tmp_path)})
    assert "other runs a driver for CUDA 12.8" in r.stdout, r.stdout + r.stderr


# ---------------------------------------------------------------- the rehearsal's fake machine carries what the package ships

def test_unit_the_fake_machine_takes_the_packages_comfy_min_and_its_vendored_packs(tmp_path):
    """Measured by the Qwen 2.1 session against 2.12.2: the fake machine was always ComfyUI 0.34.7, so a package needing
    0.37.0 stopped at the version gate; and only the package's top-level files were copied, so its vendored node packs
    (which its zip ships) were missing and every suite test reading a pack file failed inside the rehearsal."""
    sys.path.insert(0, str(BASE / "py"))
    from basetest import fake_pod
    pkg = tmp_path / "src" / "my-pkg"; (pkg / "ComfyUI-My-Pack").mkdir(parents=True); (pkg / "_build").mkdir()
    (pkg / "my-pkg-script.sh").write_text('PKG_ID=x; COMFY_MIN="0.37.0"\n')
    (pkg / "ComfyUI-My-Pack" / "__init__.py").write_text("NODE_CLASS_MAPPINGS = {}\n")
    (pkg / "_build" / "tool.py").write_text("")
    c = fake_pod(tmp_path / "pod", pkg, layout="official")
    assert '__version__ = "0.37.0"' in (c / "comfyui_version.py").read_text()
    assert (tmp_path / "pod" / "pkg" / "ComfyUI-My-Pack" / "__init__.py").exists()
    assert not (tmp_path / "pod" / "pkg" / "_build").exists()                                  # repo tooling stays out


def test_unit_the_cuda_backend_carries_the_whole_torch_family_comfyui_needs(tmp_path):
    """Measured 2026-09-25: torchaudio (which ComfyUI requires) has no cu132 build, so a driver that allows cu132 must
    still get the newest backend on which torch, torchvision and torchaudio resolve together, and say what holds it."""
    env = dict(os.environ); env.update(_uv_torch(tmp_path, "cu132", no_family="cu132"))
    r = subprocess.run([sys.executable, str(BASE / "py" / "torch_pick.py"), "3.14", ""], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout.strip() == "2.14.0 cu130" and "held by the torch family" in r.stderr, (r.stdout, r.stderr)


def test_unit_a_force_its_holder_refuses_at_import_is_taken_back_and_named(tmp_path):
    """Measured on a live machine 2026-09-25: transformers 5.17.0 (its newest) raises ImportError on huggingface-hub
    2.0.0. Forcing hub past transformers' cap would have broken ComfyUI's startup on every install; the force is taken
    back out, hub stays at the newest transformers allows, and the pair is named upstream. The run stays green."""
    env, log = _lift_env(tmp_path, [[{"name": "huggingface-hub", "version": "1.33.0", "latest_version": "2.0.0"}],
                                     [{"name": "huggingface-hub", "version": "1.33.0", "latest_version": "2.0.0"}]])
    r = _bash('base_env_setup; base_discover quiet; '
              '_base_probe_forced(){ printf "huggingface-hub\\ttransformers\\t5.17.0\\tImportError: huggingface-hub<2.0 is required\\n"; }; '
              'base_venv_latest; echo FAILED=${#BASE_FAILED[@]}; printf "U %s\\n" "${BASE_UPSTREAM[@]}"; echo OVR=$(cat "$BASE_LIFT_DIR/overrides.txt")', env=env)
    assert "FAILED=0" in r.stdout, r.stdout + r.stderr
    assert "U huggingface-hub 2.0.0: cannot be forced: transformers 5.17.0 refuses it at import" in r.stdout, r.stdout
    assert "OVR=\n" in r.stdout or r.stdout.rstrip().endswith("OVR="), r.stdout                   # nothing forced in the end
