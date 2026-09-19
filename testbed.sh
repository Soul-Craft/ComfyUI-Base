#!/usr/bin/env bash
# ============================================================================
#  ComfyUI testbed — a shared, real ComfyUI + node-pack tree for every project
#  folder in this directory to test against.
#
#      bash testbed.sh            # provision/update source only  (fast, ~500 MB)
#      bash testbed.sh --server   # also build a CPU venv and start ComfyUI
#      bash testbed.sh --latest   # packs at their remotes' HEAD instead of the pinned commits
#      bash testbed.sh --stop     # stop the server
#      bash testbed.sh --status   # what is here and what is running
#
#  Then, from any package folder that runs on ComfyUI Base:
#      bash "<name>-script.sh" test             # auto-detects base/testbed and the server on the port --server took
#  Overrides: BASE_NODE_SRC="<this dir>/testbed"  BASE_SERVER=127.0.0.1:8199
#  (every package with a suite finds it the same way: BASE_NODE_SRC / BASE_SERVER, auto-detected by `test`;
#   --server writes the port it chose to .testbed-server.port beside this script, which the base's runner
#   and its _build/verify.sh read; --stop removes it)
#
#  WHICH PACKS
#  The base derives them: `base/comfyui-base/base.sh list-packs */` merges the four
#  shared base packs with every converted package's PACKS table, pinned to the
#  commits the pod installs. EXTRA below would hold the packs of packages NOT yet
#  converted onto the base; each conversion deletes its rows here.
#
#  WHY IT LIVES HERE, NOT AT ~/ComfyUI
#  Pod discovery scans $HOME/ComfyUI, /workspace/* and /ComfyUI. A tree at
#  ~/ComfyUI would be found by a plain `bash <name>-script.sh` run, which would
#  then rebuild its venv and restart it. This path is invisible to that scan.
#
#  WHAT THIS CAN AND CANNOT PROVE
#  ✔ source: every node type, widget order and input name the workflow uses,
#    read from each pack's real source — the checks that catch a pod install
#    failing to load the graph.
#  ✔ server (--server): the pure-Python packs import and register under a
#    current Python, and /object_info answers.
#  ✘ CUDA: this host has no NVIDIA GPU. SolAttn (Triton), the Nvidia RTX nodes
#    and the int8-ConvRot loader (comfy-kitchen) cannot import here.
#  ✘ rendering: needs the weights and an sm_120 GPU. That is the pod.
# ============================================================================
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMFY="$HERE/testbed"
CN="$COMFY/custom_nodes"
VENV="$COMFY/.venv-testbed"
PORT="${TESTBED_PORT:-8199}"
LOG="$HERE/.testbed-server.log"
PIDF="$HERE/.testbed-server.pid"
PORTF="$HERE/.testbed-server.port"   # the port a running server took; read by the base's runner and verify.sh (base 2.2.0)
COMFY_TAG="${COMFY_TAG:-}"          # empty = newest release tag
# The base is either this script's own directory (testbed.sh ships in the ComfyUI Base repository) or a
# comfyui-base/ beside it (a brand repository keeps the base as a submodule at base/comfyui-base).
if [ -f "$HERE/base.sh" ]; then BASE_SH="$HERE/base.sh"; else BASE_SH="$HERE/comfyui-base/base.sh"; fi
LATEST=0

GREEN='\033[0;32m'; YEL='\033[1;33m'; CYA='\033[0;36m'; NC='\033[0m'
[ -t 1 ] || { GREEN=''; YEL=''; CYA=''; NC=''; }
ok(){ echo -e "${GREEN}  ✔ $*${NC}"; }
note(){ echo "  ○ $*"; }
warn(){ echo -e "${YEL}  ✖ $*${NC}"; }
hdr(){ echo -e "\n${CYA}══ $* ══${NC}"; }

# dir | git url — packs of packages NOT yet converted onto ComfyUI Base (unpinned, pulled --ff-only).
# The four shared base packs are never listed here: list-packs supplies them, pinned.
EXTRA=(
  # empty since 2026-09-04: every package that declares packs runs on the base, and the Replacer, Continuer and
  # Klein installers will declare theirs the same way — nothing is hand-kept here any more
)

# A package's own node pack: <brand>/packages/<pkg>/<Pack>/__init__.py. Derived, never hand-kept — nothing
# here knows a brand's name, and the list used to go stale every time a package was added or moved. In a
# standalone base checkout the glob matches nothing and no links are made, which is correct: there are no
# packages to vendor. Absolute paths, so the link loop does not re-anchor them.
VENDORED=()
derive_vendored(){
  local d
  for d in "$HERE"/../*/packages/*/*/; do
    [ -d "$d" ] || continue
    case "$(basename "$d")" in _*) continue;; esac      # _build/ and friends are not node packs
    [ -f "$d/__init__.py" ] || continue
    VENDORED+=("${d%/}")
  done
}
derive_vendored

# PACKS rows are dir|url|sha (sha empty = unpinned). Derived first, EXTRA appended for dirs not yet covered.
PACKS=()
_has_pack(){ local r; for r in ${PACKS[@]+"${PACKS[@]}"}; do [ "${r%%|*}" = "$1" ] && return 0; done; return 1; }
derive_packs() {
  local dir url sha row
  if [ -f "$BASE_SH" ]; then
    while IFS='|' read -r dir url sha; do
      if [ -n "$dir" ] && ! _has_pack "$dir"; then PACKS+=("$dir|$url|$sha"); fi
    done < <(bash "$BASE_SH" list-packs "$HERE"/../*/packages/*/ 2>/dev/null || true)
  else
    warn "no ComfyUI Base beside this script — only EXTRA packs are provisioned"
  fi
  for row in ${EXTRA[@]+"${EXTRA[@]}"}; do      # EXTRA may be empty (bash 3.2 + set -u)
    dir="${row%%|*}"
    if ! _has_pack "$dir"; then PACKS+=("$row|"); fi
  done
}
derive_packs

_latest_tag() {
  git ls-remote --tags --refs https://github.com/comfyanonymous/ComfyUI 2>/dev/null \
    | awk -F/ '{print $NF}' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1
}

provision_source() {
  hdr "SOURCE"
  local tag="${COMFY_TAG:-$(_latest_tag)}"
  [ -n "$tag" ] || { warn "could not reach github to read ComfyUI's tags"; return 1; }
  if [ -d "$COMFY/.git" ]; then
    git -C "$COMFY" fetch --depth 1 origin "refs/tags/$tag:refs/tags/$tag" >/dev/null 2>&1 || true
    git -C "$COMFY" checkout -q "$tag" 2>/dev/null || true
    ok "ComfyUI already here → $tag"
  else
    note "cloning ComfyUI $tag …"
    git clone -q --depth 1 --branch "$tag" https://github.com/comfyanonymous/ComfyUI "$COMFY"
    ok "ComfyUI $tag"
  fi
  mkdir -p "$CN"
  local row dir url sha
  for row in "${PACKS[@]}"; do
    IFS='|' read -r dir url sha <<< "$row"
    if [ ! -d "$CN/$dir/.git" ]; then
      if git clone -q --depth 1 "$url" "$CN/$dir" 2>/dev/null; then ok "$dir (cloned)"; else warn "$dir — clone failed ($url)"; continue; fi
    fi
    if [ -n "$sha" ] && [ "$LATEST" != "1" ]; then
      # what the pod installs: the pinned commit. --depth 1 clones need the object fetched first.
      if ! git -C "$CN/$dir" cat-file -e "$sha^{commit}" 2>/dev/null; then
        git -C "$CN/$dir" fetch -q --depth 1 origin "$sha" >/dev/null 2>&1 || git -C "$CN/$dir" fetch -q --tags --force origin >/dev/null 2>&1 || true
      fi
      if git -C "$CN/$dir" checkout -q --detach "$sha" 2>/dev/null; then ok "$dir @ ${sha:0:12} (pinned)"; else warn "$dir — pinned commit ${sha:0:12} unreachable"; fi
    else
      git -C "$CN/$dir" pull -q --ff-only >/dev/null 2>&1 && ok "$dir (HEAD)" || note "$dir (kept — detached or not fast-forwardable)"
    fi
  done
  # Packages' own vendored packs (no git URL; the pod copies them from the package, the testbed links them
  # so an edit is live on the next server restart). Keep this list in step with each package's VENDORED_PACKS.
  local v
  for v in ${VENDORED[@]+"${VENDORED[@]}"}; do
    local d="$CN/$(basename "$v")"
    if [ ! -f "$v/__init__.py" ]; then warn "vendored pack missing: $v"
    elif [ -d "$d" ] && [ ! -L "$d" ]; then note "$(basename "$v") is a real copy in custom_nodes — left alone (replace it with a link by hand if you want edits live)"
    else ln -sfn "$v" "$d"; ok "$(basename "$v") (vendored, linked)"; fi
  done
  echo
  note "source tree: $COMFY  ($(du -sh "$COMFY" 2>/dev/null | cut -f1)) · ${#PACKS[@]} packs + ${#VENDORED[@]} vendored"
  note "use it with:  BASE_NODE_SRC=\"$COMFY\"   (converted packages find it on their own)"
}

provision_server() {
  hdr "VENV + SERVER (CPU)"
  command -v uv >/dev/null || { warn "uv is not installed — needed to build the venv"; return 1; }
  if [ ! -x "$VENV/bin/python" ]; then
    note "creating a Python 3.12 venv …"
    uv venv --python 3.12 "$VENV" >/dev/null
  fi
  note "installing torch (CPU) + ComfyUI requirements — this is the slow part …"
  uv pip install -q --python "$VENV/bin/python" torch torchvision torchaudio >/dev/null
  uv pip install -q --python "$VENV/bin/python" -r "$COMFY/requirements.txt" pytest >/dev/null   # pytest: the suites run under this venv so they can import ComfyUI
  # pack requirements, best effort: several are CUDA-only and will not resolve here
  local row dir
  for row in "${PACKS[@]}"; do
    dir="${row%%|*}"
    [ -f "$CN/$dir/requirements.txt" ] || continue
    uv pip install -q --python "$VENV/bin/python" -r "$CN/$dir/requirements.txt" >/dev/null 2>&1 \
      && ok "reqs: $dir" || warn "reqs: $dir — some deps do not resolve on this host (expected for CUDA-only packs)"
  done
  stop_server
  note "starting ComfyUI --cpu on port $PORT …"
  ( cd "$COMFY" && nohup "$VENV/bin/python" main.py --cpu --port "$PORT" --disable-auto-launch \
      >"$LOG" 2>&1 & echo $! >"$PIDF" )
  echo "$PORT" >"$PORTF"     # the base's runner (lib/95-summary.sh) and _build/verify.sh read this to find the server
  local i
  for i in $(seq 1 120); do
    if curl -fsS "http://127.0.0.1:$PORT/system_stats" >/dev/null 2>&1; then
      ok "server up at http://127.0.0.1:$PORT (pid $(cat "$PIDF"), port recorded in $PORTF)"; return 0
    fi
    sleep 2
  done
  rm -f "$PORTF"
  warn "server did not answer within 240 s — see $LOG"; tail -30 "$LOG" || true; return 1
}

stop_server() {
  # $! inside the subshell is the SUBSHELL, not python — nohup's child ends up one pid along.
  # Kill whatever is actually listening on the port, then fall back to the recorded pid.
  # NB: lsof exits 1 when nothing matches, and `set -o pipefail` propagates that, so every
  # lookup here needs `|| true` or `set -e` kills the caller before the server ever starts.
  local pids=""
  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $2}' | sort -u || true)"
  fi
  if [ -z "$pids" ] && [ -f "$PIDF" ]; then pids="$(cat "$PIDF" 2>/dev/null || true)"; fi
  local p
  for p in $pids; do
    kill -0 "$p" 2>/dev/null || continue
    kill "$p" 2>/dev/null || true
    ok "stopped pid $p"
  done
  if [ -n "$pids" ]; then
    sleep 2
    for p in $pids; do kill -9 "$p" 2>/dev/null || true; done
  fi
  rm -f "$PIDF" "$PORTF"
}

status() {
  hdr "STATUS"
  [ -d "$COMFY/.git" ] && ok "ComfyUI $(git -C "$COMFY" describe --tags 2>/dev/null || echo '?') · $(ls -1 "$CN" 2>/dev/null | wc -l | tr -d ' ') packs" || note "no source tree yet"
  [ -x "$VENV/bin/python" ] && ok "venv $($VENV/bin/python -V 2>&1)" || note "no venv"
  if curl -fsS "http://127.0.0.1:$PORT/system_stats" >/dev/null 2>&1; then ok "server answering on $PORT"; else note "server not running"; fi
  [ -f "$PORTF" ] && note "port file: $PORTF ($(tr -d '[:space:]' < "$PORTF"))"
  echo; note "BASE_NODE_SRC=\"$COMFY\"   BASE_SERVER=127.0.0.1:$PORT"
}

case "${1:-}" in
  "")        provision_source; status;;
  --latest)  LATEST=1; provision_source; status;;
  --server)  provision_source; provision_server; status;;
  --stop)    stop_server;;
  --status)  status;;
  *) echo "usage: bash testbed.sh [--server | --latest | --stop | --status]"; exit 2;;
esac
