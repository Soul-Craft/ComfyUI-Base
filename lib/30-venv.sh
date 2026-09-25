# 30-venv.sh — the venv start.sh activates: who owns it, whether it survives a restart, rebuild with rollback, rescue.
#
# Stamp: $VENV/.comfy-base-venv holds "python=<x.y.z> torch=<ver> base=<ver> ts=<ts> by=<pkg id>".
# An unstamped venv that passes the probe is adopted and stamped.

_base_dir_bytes(){ du -sk "$1" 2>/dev/null | awk '{print $1*1024}' || echo 0; }
_base_pip(){ "$PY" -m pip "$@"; }      # never bare pip: pip on PATH belongs to whatever interpreter start.sh did not use
_base_uvpip(){ uv pip install --python "$PY" "$@"; }   # uv is faster and hardlinks out of a same-filesystem cache; the interpreter is named, never inferred
# Every requirements file the venv must satisfy. 40-packs.sh replaces this with the full list (ComfyUI,
# baked packs, this package's packs, every ledger-installed package's packs); the stub keeps 30 self-contained.
_base_all_reqfiles(){ if [ -f "$COMFY/requirements.txt" ]; then echo "$COMFY/requirements.txt"; fi; }

_base_tools_venv(){ # the base's tools venv on the volume: $BASE_HOME/tools, made with the system python's own venv module.
  # It holds uv and JupyterLab. The system interpreter is never written to (PEP 668 refuses that on Ubuntu 24.04
  # images, and it is container disk anyway); no installer script is piped into a shell — PyPI is the one source.
  local t="$BASE_HOME/tools"
  [ -x "$t/bin/python" ] && "$t/bin/python" -m pip --version >/dev/null 2>&1 && return 0
  [ -n "$SYS_PY" ] || return 1
  if [ "$BASE_DRY" = "1" ]; then would "create the tools venv $t with $SYS_PY -m venv"; return 0; fi
  note "creating the tools venv $t (uv and JupyterLab live here; the system interpreter is never touched)"
  if ! "$SYS_PY" -m venv "$t" 2>&1 | tail -2; then err "$SYS_PY -m venv $t failed — the image lacks the venv module"; return 1; fi
  "$t/bin/python" -m pip --version >/dev/null 2>&1 || { err "$t has no pip after python -m venv (ensurepip missing on this image)"; return 1; }
  return 0
}
_base_ensure_uv(){ # uv is how the newest Python arrives: from PyPI into the tools venv on the volume, never into the system interpreter
  local t="$BASE_HOME/tools"
  if [ -x "$t/bin/uv" ]; then export PATH="$t/bin:$PATH"; hash -r; return 0; fi
  if [ "$BASE_DRY" = "1" ]; then command -v uv >/dev/null 2>&1 || would "install uv into the tools venv $t (its python -m pip, from PyPI)"; return 0; fi
  _base_tools_venv || return 1
  note "installing uv into $t (needed to choose the Python and resolve against it)"
  "$t/bin/python" -m pip install -q --disable-pip-version-check uv 2>&1 | tail -2 || true
  export PATH="$t/bin:$PATH"; hash -r
  [ -x "$t/bin/uv" ]
}

# ---------------------------------------------------------------- picks: genuine latest, no fallback
_base_python_pick(){ # → newest CPython minor whose uv pip compile resolves ComfyUI + every pack requirements file
  # The resolve output must go to a real file: `uv pip compile -o /dev/null` fails because uv writes its
  # temp file beside the output path. `|| true` on the greps: an empty grep exits 1 and pipefail would
  # fire the ERR trap inside the substitution.
  local mm tmp list cands f reqargs=() errf
  _base_tmp; tmp="$BASE_TMPD/resolve.txt"; errf="$BASE_TMPD/resolve.err"
  # requirement files are POSITIONAL for `uv pip compile` (uv 0.12 rejects `-r`: "unexpected argument", 2026-09-05)
  while IFS= read -r f; do if [ -n "$f" ]; then reqargs+=("$f"); fi; done < <(_base_all_reqfiles)
  list="$(uv python list 2>/dev/null || true)"
  cands="$(printf '%s\n' "$list" | grep -oE '^cpython-3\.[0-9]+\.[0-9]+-' | sed -E 's/^cpython-(3\.[0-9]+)\..*/\1/' | sort -u -rV || true)"   # -rV, not tac: no tac on macOS
  if [ -z "$cands" ]; then
    err "uv listed no CPython versions — cannot pick a Python (uv $(uv --version 2>/dev/null || echo missing))"
    BASE_FAILED+=("venv: uv python list returned nothing"); return 1
  fi
  if [ ${#reqargs[@]} -eq 0 ]; then printf '%s\n' "$cands" | head -1; return 0; fi     # nothing to resolve against: take newest
  for mm in $cands; do
    printf '    trying Python %s ... ' "$mm" >&2
    if uv pip compile --quiet --python-version "$mm" --python-platform x86_64-unknown-linux-gnu \
         --extra-index-url https://download.pytorch.org/whl/cu130 --index-strategy unsafe-best-match \
         "${reqargs[@]}" -o "$tmp" >/dev/null 2>"$errf"; then
      rm -f "$tmp"; echo "resolves" >&2; echo "$mm"; return 0
    fi
    echo "no (the requirement set does not resolve)" >&2
    # uv's own words, or the failure is undiagnosable from the log (a whole run said "no" seven times over an argument error)
    { grep -v '^\s*$' "$errf" | tail -3 | sed 's/^/        /' || true; } >&2
  done
  err "no CPython minor resolves ComfyUI + pack requirements (tried: $(printf '%s ' $cands))"
  return 1
}
_base_torch_pick(){ "$SYS_PY" "$BASE_DIR/py/torch_pick.py" "$1"; }   # <cpXY> → newest +cu130 wheel version

# ---------------------------------------------------------------- ownership and stamps
_base_venv_owner(){ # → base | none
  [ -f "$VENV/.comfy-base-venv" ] && { echo base; return 0; }
  echo none
}
_base_venv_stamp_write(){ # <python x.y.z> <torch>
  echo "python=$1 torch=$2 base=$BASE_VERSION ts=$(_base_ts) by=${PKG_ID:-base}" > "$VENV/.comfy-base-venv"
}
_base_venv_probe(){ # [python-mm] [torch] → 0 reusable, 1 rebuild (py/venv_probe.py)
  BASE_WANT_PY="${1:-}" BASE_WANT_TORCH="${2:-}" BASE_WANT_SM="${BASE_GPU_SM:-}" BASE_PERSIST_ROOT="$BASE_PERSIST_ROOT" VENV="$VENV" \
    "$PY" "$BASE_DIR/py/venv_probe.py" 2>/dev/null
}

# ---------------------------------------------------------------- what survives a pod restart
_base_path_is_persistent(){ # <path> → 0 when a pod stop/start keeps it
  # TWO kinds of path survive a restart: the volume, and anything the IMAGE provides (a start restores the
  # container disk FROM the image, so /usr/bin/python3.12 is back every time). What does not come back is
  # anything WRITTEN AT RUNTIME onto the container disk: $HOME, /root, /home, /tmp. That is exactly where uv
  # puts a managed interpreter. Getting this wrong the other way quarantines the template's own venv.
  local p="${1:-}"
  [ -n "$BASE_PERSIST_ROOT" ] || return 0                       # not the pod layout: nothing to prove
  [ -n "$p" ] || return 1
  case "$p/" in
    "$BASE_PERSIST_ROOT"/*) return 0;;
    "${HOME:-/root}"/*|/root/*|/home/*|/tmp/*|/var/tmp/*) return 1;;
    *) return 0;;                                               # /usr, /opt, ...: the image restores it
  esac
}
_base_venv_base_home(){ # <venv> → the base interpreter dir from pyvenv.cfg; readable even when bin/python dangles
  awk '/^[[:space:]]*home[[:space:]]*=/ {sub(/^[^=]*=[[:space:]]*/,""); print; exit}' "$1/pyvenv.cfg" 2>/dev/null || true
}
_base_venv_base_is_persistent(){ # [venv] → 0 when the venv's interpreter survives a pod stop/start
  local v="${1:-${VENV:-}}" home=""
  [ -n "$BASE_PERSIST_ROOT" ] || return 0
  case "$v/" in "$BASE_PERSIST_ROOT"/*) ;; *) return 0;; esac     # a venv off the volume is ephemeral anyway
  home="$(_base_venv_base_home "$v")"
  [ -n "$home" ] || return 1                                    # no pyvenv.cfg: not provable, so not trusted
  _base_path_is_persistent "$home"
}
_base_venv_base_verdict(){ # [venv] → one phrase
  local v="${1:-${VENV:-}}" home; home="$(_base_venv_base_home "$v")"
  if [ -z "$BASE_PERSIST_ROOT" ]; then echo "not the pod layout — nothing to prove"; return 0; fi
  if [ -z "$home" ]; then echo "no pyvenv.cfg — cannot prove it survives, so treated as if it does not"; return 0; fi
  case "$home/" in "$BASE_PERSIST_ROOT"/*) echo "on the volume — survives a pod restart"; return 0;; esac
  if _base_path_is_persistent "$home"; then echo "provided by the image — restored at every pod start"
  else echo "CONTAINER DISK, written at runtime — the next pod start wipes it"; fi
}
_base_venv_boot_line(){ # [venv] → the banner's "boots from" line, judged by the SAME predicate the venv stage uses (2.0.8)
  local v="${1:-${VENV:-}}" bh bv
  bh="$(_base_venv_base_home "$v")"; bv="$(_base_venv_base_verdict "$v")"
  if _base_venv_base_is_persistent "$v"; then echo "  boots from   ${bh:-?} · $bv"
  else echo -e "  boots from   ${bh:-?} · ${RED}$bv; this run rebuilds the venv${NC}"; fi
}

# ---------------------------------------------------------------- backups, swap-aside, rollback
_base_pick_pristine_backup(){ # → the newest .pre-* whose python actually runs, or nothing (ts sorts oldest→newest)
  local b last=""
  for b in "$VENV".pre-*; do
    [ -d "$b" ] || continue
    [ -x "$b/bin/python" ] || continue
    "$b/bin/python" -c 'import sys' >/dev/null 2>&1 || continue
    last="$b"
  done
  printf '%s' "$last"
}
_base_drop_stale_venv_backups(){ # only the run that replaces a venv may remove the backup it is replacing
  # You can roll back exactly one step, so a second backup is dead weight — but only provably dead at the
  # moment a new one is about to be written. Deleting it earlier throws away a rollback the user was told to keep.
  local old
  for old in "$VENV".pre-*; do
    [ -d "$old" ] || continue
    note "superseded venv backup removed: $old ($(_base_bytes_to_gb "$(_base_dir_bytes "$old")") GB)"
    rm -rf "$old"
  done
  return 0
}
_base_venv_rollback(){
  if [ -z "$BASE_VENV_BACKUP" ] || [ ! -d "$BASE_VENV_BACKUP" ]; then err "no venv backup to restore"; return 1; fi
  rm -rf "$VENV" && mv "$BASE_VENV_BACKUP" "$VENV" && ok "venv rolled back: $BASE_VENV_BACKUP → $VENV"
  BASE_VENV_BACKUP=""; BASE_VENV_RESULT="rolled-back"
}
_base_venv_swap_aside(){ # <ts> — move $VENV out of the way and arm the rollback
  local ts="$1" pristine="" dead=""
  [ -d "$VENV" ] || return 0
  # healthy: back it up in place, dropping the superseded backup
  if [ -x "$PY" ] && _base_venv_base_is_persistent; then
    _base_drop_stale_venv_backups; BASE_VENV_BACKUP="$VENV.pre-comfy-base-$ts"
    mv "$VENV" "$BASE_VENV_BACKUP"; note "old venv → $BASE_VENV_BACKUP"
    return 0
  fi
  # This venv cannot boot (interpreter on the container disk, or bin/python already dangles). Backing IT up
  # is worthless, and dropping the existing .pre-* would delete the only proven-bootable venv on the volume.
  # Keep that as the rollback and quarantine the corpse — OUT of $COMFY, so nothing matching .venv* is left.
  pristine="$(_base_pick_pristine_backup)"
  dead="$BASE_HOME/dead-venvs"; mkdir -p "$dead" 2>/dev/null || true
  if [ -d "$dead" ] && mv "$VENV" "$dead/$(basename "$VENV")-$ts" 2>/dev/null; then
    note "unbootable venv quarantined → $dead/$(basename "$VENV")-$ts"
  else
    BASE_VENV_BACKUP="$VENV.pre-comfy-base-$ts"; mv "$VENV" "$BASE_VENV_BACKUP"
    note "old venv → $BASE_VENV_BACKUP (quarantine unavailable; the pristine backup is kept too)"
    return 0
  fi
  if [ -n "$pristine" ]; then BASE_VENV_BACKUP="$pristine"; note "rollback target kept: $pristine"
  else BASE_VENV_BACKUP=""; miss "no bootable venv to roll back to — if this build fails the path is left empty, which start.sh rebuilds on the next boot"; fi
}
_base_venv_verify(){ # [python-mm] — prints the facts, non-zero on any miss (py/venv_verify.py)
  BASE_TORCH_INFO="$(VENV="$VENV" BASE_PERSIST_ROOT="$BASE_PERSIST_ROOT" BASE_WANT_PY="${1:-}" BASE_WANT_SM="${BASE_GPU_SM:-}" \
    BASE_EXTRA_IMPORTS="${PKG_IMPORT_CHECK:-}" "$PY" "$BASE_DIR/py/venv_verify.py" 2>&1)"; local rc=$?
  echo "$BASE_TORCH_INFO" | sed 's/^/  /'
  return $rc
}

# ---------------------------------------------------------------- build
_base_venv_pip_extra(){ # the package's PIP_EXTRA into the venv — an OPTIONAL declaration: guarded, or `set -u` kills the run
  # (the base's own step one died seven minutes into a build on 2026-09-05 over ${#PIP_EXTRA[@]} with none declared)
  # ${NAME+x} (element 0) is the test bash 3.2 AND 5 accept for an array that may not be declared at all; ${NAME[@]+x} is not
  [ -n "${PIP_EXTRA+x}" ] && [ "${#PIP_EXTRA[@]}" -gt 0 ] || return 0
  _base_pip install -q --upgrade -c "$CONSTRAINTS" --extra-index-url https://pypi.nvidia.com "${PIP_EXTRA[@]}" 2>&1 | tail -2 || { miss "PIP_EXTRA install failed"; return 1; }
  return 0
}
_base_venv_pytest(){ # pytest into OUR venv, proven by import — so base_test's first choice ($PY -m pytest) is taken and every
  # package's comfyui/gpu tier imports the packs it tests (2.0.10: a package suite ran under uv's throwaway 3.12 on the pod and
  # could not see the packs' modules; the old one-line "pytest + huggingface_hub" install had left pytest out, unseen)
  local c="${CONSTRAINTS:-$BASE_STATE/constraints-torch.txt}" v
  _base_pip install -q --upgrade ${c:+-c "$c"} pytest 2>&1 | tail -2 || { miss "pytest install failed"; return 1; }
  v="$("$PY" -c 'import pytest; print(pytest.__version__)' 2>/dev/null)" || { miss "pytest does not import from $PY after the install"; return 1; }
  ok "pytest $v in the venv — the suite runs inside it"
  return 0
}
_base_venv_reconcile_names(){ # stdin: `pip check` output -> the distribution names on both sides of every conflict, one per line
  # pip's two shapes: "A 1.0 has requirement B<2,>=1, but you have B 2.0." and "A 1.0 requires B, which is not installed."
  # a pair per line, split by tr: "\n" in a sed replacement is a newline in GNU sed and a literal n in BSD sed (the Mac)
  sed -n -e 's/^\([A-Za-z0-9._-]*\) [^ ]* has requirement \([A-Za-z0-9._-]*\).*/\1 \2/p' \
         -e 's/^\([A-Za-z0-9._-]*\) [^ ]* requires \([A-Za-z0-9._-]*\), which is not installed.*/\1 \2/p' | tr ' ' '\n' | sort -u
}
_base_venv_reconcile(){ # every run, after the pip half: the venv's requirements must agree with each other, at their newest
  # 2.12.3, measured on a Verda machine: a REUSED venv held transformers 5.17.0 (the newest release, which requires
  # huggingface-hub<2.0) and the per-pack loop took huggingface-hub to 2.0.0, because `pip install --upgrade -r` always takes a
  # requirement the file NAMES (several packs name a bare huggingface_hub) to its newest, and pip does not resolve against
  # what is installed outside the request; it only warns. main.py then died in transformers' import-time version check.
  # The answer is not a pin: re-resolve both sides of each conflict in ONE call, so the resolver sees them together and
  # lands on the newest set that agrees (that night: transformers 5.17.0 with huggingface-hub 1.33.0). A conflict pip
  # cannot resolve is reported and left to the import check, which stays the gate; this step never fails a run by itself.
  local out names
  if [ "$BASE_DRY" = "1" ]; then would "pip check the venv and re-resolve any conflicting requirements together"; return 0; fi
  out="$(_base_pip check 2>&1)" && { ok "pip check: every requirement in the venv agrees"; return 0; }
  names="$(printf '%s\n' "$out" | _base_venv_reconcile_names | tr '\n' ' ')"
  names="${names% }"
  if [ -z "$names" ]; then note "pip check reported something it did not name as a conflict: $(printf '%s' "$out" | tail -1)"; return 0; fi
  if [ "$BASE_NO_NET" = "1" ]; then note "pip check found conflicts ($names); not re-resolved (BASE_NO_NET)"; return 0; fi
  note "pip check found conflicts; re-resolving together: $names"
  # shellcheck disable=SC2086  # the names are word-split on purpose: one argument per distribution
  _base_pip install -q --upgrade ${CONSTRAINTS:+-c "$CONSTRAINTS"} $names 2>&1 | tail -2 || true
  if out="$(_base_pip check 2>&1)"; then ok "pip check: conflicts re-resolved ($names)"
  else note "pip check still reports, left to the import check: $(printf '%s' "$out" | tr '\n' ' ' | cut -c1-300)"; fi
  return 0
}
_base_venv_ensure_pytest(){ # every run, reuse included: a venv without pytest is a venv the suite cannot run in
  "$PY" -c 'import pytest' >/dev/null 2>&1 && return 0
  if [ "$BASE_DRY" = "1" ]; then would "install pytest into the venv (the suite runs inside it)"; return 0; fi
  _base_venv_pytest
}
_base_venv_build(){ # <python-mm> <torch> → sets BASE_VENV_RESULT to built | rebuilt | failed-* | rolled-back
  local pymm="$1" tv="$2" ts cp rv f reqargs=() step_fail=0 had=0
  ts="$(_base_ts)"; cp="cp${pymm/./}"
  [ -d "$VENV" ] && had=1 || true
  CONSTRAINTS="$BASE_STATE/constraints-torch.txt"
  while IFS= read -r f; do if [ -n "$f" ]; then reqargs+=(-r "$f"); fi; done < <(_base_all_reqfiles)
  # (0) prove the whole set resolves BEFORE the old venv is touched — a throwaway venv in a temp dir
  rv="$(_base_mktemp_d)/resolve"
  echo "  resolvability dry-run for Python $pymm over ${#reqargs[@]} requirement file(s)…"
  if ! uv venv -q --python "$pymm" "$rv" 2>&1 | tail -3; then
    err "uv cannot provide Python $pymm"; BASE_FAILED+=("venv: uv venv --python $pymm failed"); BASE_VENV_RESULT="failed-python"; return 1
  fi
  if ! uv pip install --python "$rv/bin/python" --dry-run -q \
        --index-url https://download.pytorch.org/whl/cu130 --extra-index-url https://pypi.org/simple --extra-index-url https://pypi.nvidia.com \
        --index-strategy unsafe-best-match \
        "torch==$tv" torchvision torchaudio ${reqargs[@]+"${reqargs[@]}"} ${PIP_EXTRA[@]+"${PIP_EXTRA[@]}"} pytest "huggingface_hub[hf-xet]" 2>&1 | tail -15; then
    err "the requirement set does NOT resolve for Python $pymm — nothing was changed (the offending name is above)"
    BASE_FAILED+=("venv: resolvability dry-run failed for $pymm"); BASE_VENV_RESULT="failed-resolve"; return 1
  fi
  ok "every requirement resolves for Python $pymm"
  # (1) swap the venv at the SAME path: start.sh only creates one when the path is missing
  _base_venv_swap_aside "$ts"
  if ! uv venv -q --python "$pymm" --seed "$VENV" 2>&1 | tail -3; then
    err "uv venv failed"; BASE_FAILED+=("venv: uv venv failed"); BASE_VENV_RESULT="failed-uv-venv"; _base_venv_rollback || true; return 1
  fi
  # (a) torch trio FIRST from the cu130 index (requirements first once pulled a cu12 torch)
  if ! _base_run_watched "torch $tv+cu130 · torchvision · torchaudio" \
         _base_uvpip "torch==$tv" torchvision torchaudio --index-url https://download.pytorch.org/whl/cu130; then
    err "torch install failed"; BASE_FAILED+=("venv: torch $tv+cu130 install failed"); BASE_VENV_RESULT="failed-torch"; _base_venv_rollback || true; return 1
  fi
  # (b) pin what is installed, then everything else under that pin — only-if-needed, never eager
  _base_pip freeze 2>/dev/null | grep -E '^(torch|torchvision|torchaudio|triton)==' > "$CONSTRAINTS" || true
  if [ ! -s "$CONSTRAINTS" ]; then err "no torch in the venv to pin"; BASE_FAILED+=("venv: torch missing after install"); BASE_VENV_RESULT="failed-torch"; _base_venv_rollback || true; return 1; fi
  echo "  constraints: $(tr '\n' ' ' < "$CONSTRAINTS")"
  for f in $(_base_all_reqfiles); do
    _base_run_watched "requirements $(basename "$(dirname "$f")")/$(basename "$f")" \
      _base_pip install -q --upgrade --upgrade-strategy only-if-needed -c "$CONSTRAINTS" -r "$f" || { miss "$f failed"; step_fail=1; }
  done
  # (c) what the packages asked for, the suite's runner, the download tool
  _base_venv_pip_extra || step_fail=1
  _base_venv_pytest || step_fail=1
  _base_pip install -q --upgrade -c "$CONSTRAINTS" "huggingface_hub[hf-xet]" 2>&1 | tail -2 || { miss "huggingface_hub install failed"; step_fail=1; }
  _base_venv_reconcile            # 2.12.3: the upgrades above can overshoot a cap another installed package declares
  # (d) verify inside the venv → stamp, else roll back
  if ! _base_venv_verify "$pymm" || [ "$step_fail" = "1" ]; then
    err "venv verify failed — rolling back"
    BASE_FAILED+=("venv: verify failed ($BASE_TORCH_INFO)"); BASE_VENV_RESULT="failed-verify"
    _base_venv_rollback || true
    return 1
  fi
  _base_xet_check_soft
  local t0 t1; t0=$(date +%s); _base_pip --version >/dev/null 2>&1 || true; t1=$(date +%s)
  echo "  warm-up: python -m pip --version $((t1 - t0)) s (ComfyUI-Manager allows 5 s)"
  if [ -n "$BASE_PERSIST_ROOT" ]; then note "venv interpreter: $(_base_venv_base_home "$VENV") ($(_base_venv_base_verdict "$VENV"))"; fi
  _base_venv_stamp_write "$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')" "$tv"
  if [ "$had" = "1" ]; then BASE_VENV_RESULT="rebuilt"; BASE_CHANGED+=("venv rebuilt on Python $pymm / torch $tv (previous at ${BASE_VENV_BACKUP:-quarantine})")
  else BASE_VENV_RESULT="built"; BASE_CHANGED+=("venv built on Python $pymm / torch $tv"); fi
  ok "venv ready: $BASE_TORCH_INFO"
}
_base_xet_check_soft(){ # a silent fall back to plain HTTPS is several times slower and reports no error at all
  local xv; xv="$("$PY" -c 'import hf_xet; print(getattr(hf_xet, "__version__", "present"))' 2>/dev/null || true)"
  if [ -n "$xv" ]; then ok "hf_xet $xv — chunked Xet transfer, ${HF_XET_NUM_CONCURRENT_RANGE_GETS:-32} concurrent range gets"
  else warn "hf_xet is NOT importable — downloads fall back to plain HTTPS and will be far slower"; fi
}

base_venv(){ # the stage: reuse when the probe passes, otherwise rebuild with rollback
  hdr "VENV · $VENV"
  CONSTRAINTS="$BASE_STATE/constraints-torch.txt"
  local ts owner pymm tv cp; ts="$(_base_ts)"
  # ---- 2.1.0, pinned: the seed's venv is the venv. No stamp means the seed never reached this volume; nothing is built here.
  if [ "$BASE_PINNED" = "1" ] && [ ! -f "$VENV/.comfy-base-venv" ]; then
    err "BASE_PINNED=1 but $VENV carries no .comfy-base-venv stamp — the seed is missing or incomplete; nothing is built in pinned mode"
    echo "      fix: re-copy the seed (the image's /opt/comfy-seed) onto the volume, or run the base unpinned once"
    BASE_FAILED+=("venv: pinned, no seed stamp at $VENV"); BASE_VENV_RESULT="failed-pinned"; return 1
  fi
  # ---- fake / offline: a venv-shaped directory so every later step exercises its real code path
  if [ "$BASE_NO_NET" = "1" ]; then
    if [ -f "$VENV/.comfy-base-venv" ] && _base_venv_base_is_persistent; then ok "reusing our venv (stamp: $(cat "$VENV/.comfy-base-venv"))"; BASE_VENV_RESULT="reused"; BASE_TORCH_INFO="(fake venv)"
    elif [ -f "$VENV/.comfy-base-venv" ] && [ "$BASE_DRY" != "1" ]; then
      miss "our venv boots from $(_base_venv_base_home "$VENV") — $(_base_venv_base_verdict "$VENV"); rebuilding (fake)"
      _base_venv_swap_aside "$ts"
      local real_py; real_py="$("$SYS_PY" -c 'import sys; print(sys.executable)' 2>/dev/null || echo "$SYS_PY")"
      mkdir -p "$VENV/bin"; ln -sfn "$real_py" "$VENV/bin/python"; _base_venv_stamp_write "fake" "fake"
      ok "fake venv rebuilt"; BASE_VENV_RESULT="rebuilt"
    elif [ "$BASE_DRY" = "1" ]; then would "build a fake venv at $VENV (BASE_NO_NET)"; BASE_VENV_RESULT="would-build"
    else
      _base_venv_swap_aside "$ts"
      local real_py; real_py="$("$SYS_PY" -c 'import sys; print(sys.executable)' 2>/dev/null || echo "$SYS_PY")"
      mkdir -p "$VENV/bin"; ln -sfn "$real_py" "$VENV/bin/python"
      _base_venv_stamp_write "fake" "fake"
      ok "fake venv created (bin/python → $real_py, stamp written); pip skipped (BASE_NO_NET)"; BASE_VENV_RESULT="fake"
    fi
    if [ "$BASE_DRY" != "1" ]; then [ -f "$CONSTRAINTS" ] || : > "$CONSTRAINTS"; fi
    BASE_TORCH_INFO="(fake venv: $("$PY" --version 2>&1 || echo 'no python at the venv path'))"; return 0
  fi
  # ---- uv first: the picks need it
  if ! _base_ensure_uv; then err "uv could not be installed from PyPI"; BASE_FAILED+=("venv: uv missing"); BASE_VENV_RESULT="failed-uv"; return 1; fi
  note "uv $(uv --version 2>/dev/null | awk '{print $2}')"
  # 2.0.43: a dry run must not write, so _base_ensure_uv only PROMISES uv when the tools venv is absent. The picks
  # below then have no uv to run, and the step failed — over a venv that does not exist yet to be stale. A dry run
  # predicts; this it cannot predict, so it says so and lets the rest of the check run. Found on a brand-new pod
  # (no network volume): every earlier pod already carried tools/bin/uv from an install on the shared volume.
  if [ "$BASE_DRY" = "1" ] && ! command -v uv >/dev/null 2>&1; then
    would "uv pick the newest Python that resolves the requirement set, then the newest torch for it"
    note "picks not computed — uv arrives with the real run"
    BASE_VENV_RESULT="$([ -d "$VENV" ] && echo would-rebuild || echo would-build)"
    BASE_TORCH_INFO="(not computed — the dry run has no uv yet)"
    return 0
  fi
  if [ "$BASE_PINNED" = "1" ]; then
    # ---- 2.1.0, pinned: the picks are the seed's stamp (python=x.y.z torch=t ...), never the newest anything
    local stamp; stamp="$(cat "$VENV/.comfy-base-venv")"
    pymm="$(printf '%s' "$stamp" | sed -n 's/.*python=\([0-9][0-9]*\.[0-9][0-9]*\)[^ ]*.*/\1/p')"
    tv="$(printf '%s' "$stamp" | sed -n 's/.*torch=\([^ ]*\).*/\1/p')"
    if [ -z "$pymm" ] || [ -z "$tv" ]; then
      err "BASE_PINNED=1 but the seed's stamp names no python/torch ($stamp)"; BASE_FAILED+=("venv: pinned, unreadable stamp"); BASE_VENV_RESULT="failed-pinned"; return 1
    fi
    echo "  pinned: Python $pymm · torch $tv (the seed's stamp; no pick)${BASE_GPU_SM:+ · $BASE_GPU_SM}"
  else
  # ---- the picks: newest Python that resolves, newest torch for it. No fallback.
  # the pick runs in a command substitution: anything it appends to BASE_FAILED is lost — the caller records the failure
  if ! pymm="$(_base_python_pick)"; then BASE_FAILED+=("venv: no Python resolves the requirement set (uv's reason is above)"); BASE_VENV_RESULT="failed-pick"; return 1; fi
  cp="cp${pymm/./}"
  if ! tv="$(_base_torch_pick "$cp" 2>&1)"; then err "torch pick failed: $tv"; BASE_FAILED+=("venv: torch pick failed ($tv)"); BASE_VENV_RESULT="failed-pick"; return 1; fi
  echo "  target: Python $pymm · torch $tv+cu130 (newest wheel for $cp on the cu130 index)${BASE_GPU_SM:+ · $BASE_GPU_SM}"
  fi
  owner="$(_base_venv_owner)"
  # ---- reuse when the probe passes (the probe is HARD on a runtime-written interpreter, so a poisoned venv never reuses)
  if [ -x "$PY" ] && _base_venv_probe "$pymm" "$tv"; then
    case "$owner" in
      base)   BASE_VENV_RESULT="reused"; ok "reusing our venv (stamp: $(cat "$VENV/.comfy-base-venv"))";;
      none)   BASE_VENV_RESULT="adopted"; ok "adopting the existing venv — it passes every check"; BASE_CHANGED+=("venv adopted");;
    esac
    _base_venv_ensure_pytest || warn "pytest could not be installed into $VENV — the suite cannot run inside it"
    if [ "$BASE_DRY" = "1" ]; then would "refresh the venv stamp (base $BASE_VERSION, by ${PKG_ID:-base})"      # 2.0.18: a --check restamped the pod's venv
    else _base_venv_stamp_write "$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')" "$tv"; fi
    BASE_TORCH_INFO="$("$PY" -c 'import torch; print("torch %s · CUDA %s" % (torch.__version__, torch.version.cuda))' 2>/dev/null || echo "?")"
    return 0
  fi
  if [ "$BASE_PINNED" = "1" ]; then   # 2.1.0: a rebuild is the slow path pinned mode exists to avoid, and it would not be the tested set
    err "the seed's venv at $VENV does not pass the probe (details above) — no rebuild in pinned mode"
    echo "      fix: bash '<package>-script.sh' rescue, re-copy the seed, or run the base unpinned once"
    BASE_FAILED+=("venv: pinned seed fails the probe"); BASE_VENV_RESULT="failed-pinned"; return 1
  fi
  if [ -x "$PY" ]; then miss "the venv at $VENV does not pass the probe (details above) — rebuilding"; else note "no venv at $VENV — building"; fi
  if [ "$BASE_DRY" = "1" ]; then
    would "uv dry-run the whole requirement set for Python $pymm ($(_base_all_reqfiles | wc -l | tr -d ' ') requirement files + torch trio + extras)"
    [ -d "$VENV" ] && would "mv $VENV → $VENV.pre-comfy-base-<ts> (or quarantine it if it cannot boot)"
    would "uv venv --python $pymm --seed $VENV; install torch==$tv from cu130, ComfyUI + pack requirements under a torch constraint, extras, pytest, huggingface_hub[hf-xet]; verify; stamp"
    BASE_VENV_RESULT="$([ -d "$VENV" ] && echo would-rebuild || echo would-build)"; return 0
  fi
  _base_venv_build "$pymm" "$tv" || return 1
}

# ---------------------------------------------------------------- rescue: the only stage that must work when $PY is dead
_base_venv_probe_ro(){ # <dir> — describe one .venv* directory without executing it; 0 when it could boot
  local v="$1" kind="live" stamp="none" link="(not a symlink)" tgt="present" home verdict rc=0
  case "$v" in *.pre-*) kind="backup (restore source only)";; esac
  [ -f "$v/.comfy-base-venv" ] && stamp="$(cat "$v/.comfy-base-venv" 2>/dev/null || echo present)"
  [ -L "$v/bin/python" ] && link="$(readlink "$v/bin/python" 2>/dev/null || echo "?")" || true
  if [ ! -x "$v/bin/python" ]; then tgt="MISSING — dangling symlink"; rc=1; fi
  home="$(_base_venv_base_home "$v")"; [ -n "$home" ] || home="none (no pyvenv.cfg)"
  verdict="$(_base_venv_base_verdict "$v")"
  _base_venv_base_is_persistent "$v" || rc=1
  echo "  $v   ($(_base_bytes_to_gb "$(_base_dir_bytes "$v")") GB)"
  echo "      kind          $kind"
  echo "      stamp         $stamp"
  echo "      bin/python    $link   [$tgt]"
  echo "      pyvenv home   $home"
  echo "      verdict       $verdict"
  return $rc
}
base_rescue(){ # diagnosis is test/readlink/cat, repair is mv; the only interpreter ever executed is a candidate BACKUP's
  hdr "RESCUE · $COMFY"
  local facts="$BASE_STATE/boot-facts.txt" v live_ok=0 found=0 ts; ts="$(_base_ts)"
  BASE_RESCUE_RC=0
  echo "Every venv-shaped directory beside ComfyUI:"
  for v in "$COMFY"/.venv*; do [ -d "$v" ] || continue; found=1; _base_venv_probe_ro "$v" || true; done
  [ "$found" = "1" ] || note "no .venv* directory at all — start.sh rebuilds one on the next boot"
  echo
  note "the run would use: ${VENV:-<none>}"
  case "${VENV:-}" in *.pre-*) miss "that is a BACKUP, not the venv start.sh activates — do not build into it";; esac
  # the template boot command is the one fact only a live pod can give: it says where start.sh lives
  mkdir -p "$BASE_STATE" 2>/dev/null || true
  { echo "captured $ts"
    echo "PID 1 cmdline: $(tr '\0' ' ' < /proc/1/cmdline 2>/dev/null || echo unavailable)"
    echo "PID 1 exe:     $(readlink /proc/1/exe 2>/dev/null || echo unavailable)"
    echo "COMFY:         $COMFY"
    echo "VENV selected: ${VENV:-<none>}"
    echo "persist root:  ${BASE_PERSIST_ROOT:-<none>}"; } > "$facts" 2>/dev/null || true
  [ -s "$facts" ] && note "boot facts written: $facts" || true
  if [ -n "${VENV:-}" ] && [ -d "$VENV" ] && _base_venv_probe_ro "$VENV" >/dev/null 2>&1; then live_ok=1; fi
  if [ "$live_ok" = "1" ]; then ok "healthy — the venv start.sh activates can boot; nothing changed"; return 0; fi
  err "the venv at the template path cannot boot — this is why the pod stops seconds after starting"
  echo "  the models on the volume are untouched by any of this; nothing below re-downloads them."
  if [ "$BASE_DRY" = "1" ]; then would "restore a bootable venv at $VENV"; BASE_RESCUE_RC=1; return 0; fi
  local pristine dead; pristine="$(_base_pick_pristine_backup)"; dead="$BASE_HOME/dead-venvs"
  if [ -n "$pristine" ] && _base_confirm_yes "Restore the previous venv from $pristine? [Y/n] "; then
    mkdir -p "$dead" 2>/dev/null || true
    if [ -d "$VENV" ]; then mv "$VENV" "$dead/$(basename "$VENV")-$ts" || { err "could not move the dead venv aside"; BASE_RESCUE_RC=1; return 0; }; fi
    if mv "$pristine" "$VENV" && "$VENV/bin/python" -V >/dev/null 2>&1; then
      ok "restored: $(basename "$pristine") → $VENV ($("$VENV/bin/python" -V 2>&1))"
      note "quarantined: $dead/$(basename "$VENV")-$ts   (delete it once the pod boots)"
      note "now start the pod normally, then re-run any package to rebuild the venv properly"
      return 0
    fi
    err "the restore did not produce a working python — leaving the path empty so start.sh rebuilds it"
    BASE_RESCUE_RC=1; return 0
  fi
  if _base_confirm_yes "Move the dead venv aside so start.sh rebuilds a clean one on the next boot? [Y/n] "; then
    mkdir -p "$dead" 2>/dev/null || true
    if [ -d "$VENV" ] && mv "$VENV" "$dead/$(basename "$VENV")-$ts"; then
      ok "quarantined → $dead/$(basename "$VENV")-$ts"
      note "start the pod: the template sees a missing path and builds its own venv"
      return 0
    fi
    err "could not move $VENV aside"; BASE_RESCUE_RC=1; return 0
  fi
  note "nothing changed. To do it by hand:"
  echo "      mkdir -p '$dead' && mv '$VENV' '$dead/$(basename "$VENV")-$ts'"
  [ -n "$pristine" ] && echo "      mv '$pristine' '$VENV'      # or leave it missing and let start.sh rebuild" || true
  BASE_RESCUE_RC=1; return 0
}
