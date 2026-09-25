# 47-latest.sh: 3.0.0: every Python package in the venv at its newest, on every run.
#
# ComfyUI pins its frontend packages with ==, packs pin their own dependencies, and installing their requirement files
# as written puts each of those back down on every run. So the venv is never installed from an upstream file: py/reqlift.py
# merges ComfyUI's, every pack's and PIP_EXTRA into ONE derived file with the holds removed (it keeps floors, markers and
# the pack each line came from), and uv resolves the newest set from it. What is still behind after that is behind
# because another package CAPS it; uv's overrides force it to its newest anyway (protobuf 7 over google-generativeai's
# <6, measured 2026-09-25), and the import check after the hooks decides what that costs:
#   - a PACK that cannot import with a newest dependency is named ("needs upgrading upstream") and the install goes on;
#   - ComfyUI's OWN startup failing is the one thing rolled back, because a server that cannot start stops every
#     machine on the store at once. Only the packages this run changed go back, from the snapshot taken before any of it.
# The torch family and torch's whole dependency closure (nvidia-*, triton, ...) are held by the constraints file: they
# move together with torch, on the venv's CUDA backend, in 30-venv.sh, and nothing here may move one of them alone.

BASE_LIFT_DIR=""
BASE_LIFT_ROWS=()         # "name from → to (why)" for the summary
BASE_UPSTREAM=()          # "what needs upgrading upstream, and why" (named, never a failure)
BASE_FRONTEND_INFO=""
BASE_TORCH_BACKEND="${BASE_TORCH_BACKEND:-}"   # the venv's CUDA backend (cu130, cu132, ...): set by 30-venv.sh

_base_lift_dir(){ BASE_LIFT_DIR="$BASE_STATE/lift"; mkdir -p "$BASE_LIFT_DIR" 2>/dev/null || true; }
_base_uv(){ command uv "$@"; }
_base_uv_index_args(){ # the indexes every resolve in the venv uses: PyPI first, NVIDIA's, the local wheel cache; newest across all
  printf '%s\n' --index-strategy unsafe-best-match --extra-index-url https://pypi.nvidia.com
  if [ -d "$BASE_STATE/wheels" ]; then printf '%s\n' --find-links "$BASE_STATE/wheels"; fi
}
_base_venv_backend(){ # → the torch backend the venv was built on (cu132 ...), from torch itself; "" when it cannot tell
  "$PY" -c 'import torch; c = torch.version.cuda or ""; print("cu" + c.replace(".", "") if c else "cpu")' 2>/dev/null || true
}

_base_reqlift(){ # → writes $BASE_LIFT_DIR/derived.txt and map.tsv from every requirement file the venv must satisfy; prints the lift lines
  _base_lift_dir
  local f label args=() extras=() e
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if [ "$f" = "$COMFY/requirements.txt" ]; then label="ComfyUI"; else label="$(basename "$(dirname "$f")")"; fi
    args+=("$label=$f")
  done < <(_base_all_reqfiles)
  for e in ${PIP_EXTRA[@]+"${PIP_EXTRA[@]}"}; do extras+=(--extra "PIP_EXTRA=$e"); done
  local runner=()
  # packaging (or pip's vendored copy) is all reqlift needs: the venv has pip; before the venv exists, uv supplies it
  if [ -x "${PY:-/nonexistent}" ] && "$PY" -c 'import packaging.requirements' >/dev/null 2>&1 || { [ -x "${PY:-/nonexistent}" ] && "$PY" -c 'import pip._vendor.packaging.requirements' >/dev/null 2>&1; }; then runner=("$PY")
  elif command -v uv >/dev/null 2>&1; then runner=(uv run --no-project --quiet --with packaging python)
  else runner=("${SYS_PY:-python3}"); fi
  "${runner[@]}" "$BASE_DIR/py/reqlift.py" --out "$BASE_LIFT_DIR/derived.txt" --map "$BASE_LIFT_DIR/map.tsv" \
    ${extras[@]+"${extras[@]}"} ${args[@]+"${args[@]}"}
}
_base_lift_who(){ # <package> → which pack(s) asked for it, from the reqlift map
  [ -f "$BASE_LIFT_DIR/map.tsv" ] || return 0
  awk -F'\t' -v n="$(printf '%s' "$1" | tr 'A-Z_.' 'a-z--')" '$1==n {print $2}' "$BASE_LIFT_DIR/map.tsv" | sort -u | paste -sd, - 2>/dev/null || true
}

base_venv_snapshot(){ # C1: the venv as it was BEFORE this run changed anything; the core rollback restores from it
  BASE_VENV_SNAPSHOT=""
  [ "$BASE_DRY" = "1" ] || [ "$BASE_NO_NET" = "1" ] && return 0
  [ -x "${PY:-/nonexistent}" ] || return 0
  command -v uv >/dev/null 2>&1 || return 0
  _base_lift_dir
  BASE_VENV_SNAPSHOT="$BASE_LIFT_DIR/pre-$(_base_ts).txt"
  if _base_uv pip freeze --python "$PY" > "$BASE_VENV_SNAPSHOT" 2>/dev/null; then
    printf '%s\n' "$(_base_venv_backend)" > "$BASE_VENV_SNAPSHOT.backend"
    ls -t "$BASE_LIFT_DIR"/pre-*.txt 2>/dev/null | sed -n '4,$p' | while IFS= read -r old; do rm -f "$old" "$old.backend"; done   # keep three
  else BASE_VENV_SNAPSHOT=""; fi
  return 0
}

_base_torch_closure(){ # → every distribution torch pulls in (torch included), one canonical name per line
  "$PY" - <<'PY' 2>/dev/null || true
import importlib.metadata as m, re
def canon(n): return re.sub(r"[-_.]+", "-", n).lower()
seen, todo = set(), ["torch", "torchvision", "torchaudio", "triton"]
while todo:
    n = canon(todo.pop())
    if n in seen:
        continue
    try:
        d = m.distribution(n)
    except m.PackageNotFoundError:
        continue
    seen.add(n)
    for r in d.requires or []:
        if "extra ==" in r:
            continue
        todo.append(re.split(r"[\s;<>=!~\[(]", r, 1)[0])
print("\n".join(sorted(seen)))
PY
}
base_torch_constraints(){ # C2: constraints-torch.txt, fresh from the venv: the torch family AND its whole dependency closure
  CONSTRAINTS="$BASE_STATE/constraints-torch.txt"
  [ "$BASE_DRY" = "1" ] && return 0
  if [ "$BASE_NO_NET" = "1" ] || [ ! -x "${PY:-/nonexistent}" ]; then [ -f "$CONSTRAINTS" ] || : > "$CONSTRAINTS"; return 0; fi
  local names; names="$(_base_torch_closure)"
  if [ -z "$names" ]; then return 0; fi
  _base_uv pip freeze --python "$PY" 2>/dev/null | awk -v list="$names" 'BEGIN{ n=split(list, a, "\n"); for (i=1;i<=n;i++) keep[a[i]]=1 }
    { split($0, p, "=="); nm=tolower(p[1]); gsub(/[-_.]+/, "-", nm); if ((nm in keep) && index($0, "==")) print }' > "$CONSTRAINTS.new" || true
  if [ -s "$CONSTRAINTS.new" ]; then mv "$CONSTRAINTS.new" "$CONSTRAINTS"; else rm -f "$CONSTRAINTS.new"; fi
  return 0
}

_base_derived_install(){ # [extra uv args...] → one resolve of the derived file (plus the base's venv tools), newest everything
  local backend="${BASE_TORCH_BACKEND:-$(_base_venv_backend)}" idx=() con=()
  while IFS= read -r a; do idx+=("$a"); done < <(_base_uv_index_args)
  if [ -n "${CONSTRAINTS:-}" ] && [ -s "$CONSTRAINTS" ]; then con=(-c "$CONSTRAINTS"); fi
  _base_uv pip install --python "$PY" --upgrade ${backend:+--torch-backend "$backend"} ${idx[@]+"${idx[@]}"} \
    ${con[@]+"${con[@]}"} -r "$BASE_LIFT_DIR/derived.txt" \
    pip setuptools wheel pytest "huggingface_hub[hf-xet]" "comfy-cli>=1.14.0" comfy-mcp ninja packaging "$@"
}

_base_outdated(){ # → "name installed latest" for every package still behind, torch's closure excluded
  local idx=() closure
  while IFS= read -r a; do idx+=("$a"); done < <(_base_uv_index_args | grep -v -- '--find-links\|wheels$' || true)
  closure="$(_base_torch_closure)"
  _base_uv pip list --python "$PY" --outdated --format json ${idx[@]+"${idx[@]}"} 2>/dev/null | "$PY" -c '
import json, re, sys
closure = set(sys.argv[1].split())
def canon(n): return re.sub(r"[-_.]+", "-", n).lower()
try:
    rows = json.load(sys.stdin)
except Exception:
    rows = []
for r in rows:
    n = canon(r.get("name", ""))
    if n and n not in closure and r.get("latest_version") and r.get("latest_version") != r.get("version"):
        print(n, r.get("version"), r.get("latest_version"))
' "$closure" 2>/dev/null || true
}

_base_probe_forced(){ # <name>... → "forced<TAB>holder<TAB>holder-version<TAB>error" for each holder that no longer imports
  if [ -n "${BASE_FAKE_HOLDERS:-}" ]; then cat "$BASE_FAKE_HOLDERS" 2>/dev/null; return 0; fi   # the suite's stand-in
  "$PY" "$BASE_DIR/py/holders.py" "$@" 2>/dev/null | awk -F'\t' 'NF>=4 && $4 != "ok"' || true
}
_base_revert_forced(){ # <over-lines> <round-pairs> → sets _BASE_KEPT to the override lines to keep; names each refused force upstream.
  # A force is kept only if every package that capped it still imports (py/holders.py). One that refuses (transformers
  # 5.17.0 raises ImportError on huggingface-hub 2.0.0, measured 2026-09-25) is taken back out and named, never retried.
  local over="$1" pairs="$2" names=() line n f h hv e want
  while IFS= read -r line; do [ -n "$line" ] && names+=("${line%%==*}"); done <<< "$pairs"
  _BASE_KEPT="$over"
  [ "${#names[@]}" -gt 0 ] || return 0
  while IFS=$'\t' read -r f h hv e; do
    [ -n "$f" ] || continue
    want="$(printf '%s\n' "$pairs" | sed -n "s/^$f==//p" | head -1)"
    over="$(printf '%s' "$over" | grep -v "^$f==" || true)"; [ -n "$over" ] && over="$over"$'\n'
    BASE_UPSTREAM+=("$f ${want:-?}: cannot be forced: $h $hv refuses it at import ($e); $h needs upgrading upstream")
    note "not forcing $f ${want:-?}: $h $hv refuses it at import; named upstream"
  done < <(_base_probe_forced "${names[@]}")
  _BASE_KEPT="$over"
}
_base_install_overrides(){ # <over-lines> → the derived install with those overrides (none: the plain derived install)
  if [ -n "$1" ]; then printf '%s' "$1" > "$BASE_LIFT_DIR/overrides.txt"; _base_derived_install --overrides "$BASE_LIFT_DIR/overrides.txt"
  else : > "$BASE_LIFT_DIR/overrides.txt"; _base_derived_install; fi
}

base_venv_latest(){ # C2-C4, C6: constraints from the venv, the derived install, the override loop, the report
  hdr "NEWEST · every package in $VENV"
  BASE_LIFT_ROWS=()                    # BASE_UPSTREAM keeps what the venv stage already named (the torch family)
  if [ "$BASE_DRY" = "1" ]; then
    would "lift every upstream pin (py/reqlift.py), install the newest set in one resolve, force anything still behind with uv overrides, then report"
    BASE_LIFT_RESULT="would lift"; return 0
  fi
  if [ "$BASE_NO_NET" = "1" ]; then note "skipped (BASE_NO_NET: fake venv)"; BASE_LIFT_RESULT="skipped (fake)"; return 0; fi
  if [ ! -x "${PY:-/nonexistent}" ] || ! command -v uv >/dev/null 2>&1; then note "skipped: no venv interpreter or no uv"; BASE_LIFT_RESULT="skipped"; return 0; fi
  case "$BASE_VENV_RESULT" in failed*|rolled*) note "skipped: the venv step failed"; BASE_LIFT_RESULT="skipped (venv failed)"; return 0;; esac
  base_torch_constraints
  local out rc=0 round=0 line n have want over="" tried=" " log
  _base_lift_dir; log="$BASE_LIFT_DIR/lift.log"; : > "$log"
  if ! out="$(_base_reqlift 2>&1)"; then err "py/reqlift.py failed: $(printf '%s' "$out" | tail -2)"; BASE_FAILED+=("newest: could not derive the requirement set"); BASE_LIFT_RESULT="FAILED (reqlift)"; return 0; fi
  printf '%s\n' "$out" | awk -F'\t' '$1=="lifted"{printf "  ↑ %s (%s)\n", $4, $2} $1=="held"{printf "  ~ held: %s [%s]\n", $4, $2} $1=="summary"{printf "  %s\n", $2}'
  while IFS=$'\t' read -r kind label name what; do
    if [ "$kind" = lifted ]; then BASE_LIFT_ROWS+=("${what} (upstream pin in $label)"); fi
    if [ "$kind" = held ]; then BASE_UPSTREAM+=("$what: kept as written ($label)"); fi
  done <<< "$out"
  note "resolving and installing the newest set in one uv call (its log: $log)"
  _base_derived_install >>"$log" 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then
    miss "the lifted set did not install in one resolve: uv's reason:"; grep -v '^\s*$' "$log" | tail -8 | sed 's/^/      /'
    BASE_FAILED+=("newest: the lifted requirement set did not install (see $log)"); BASE_LIFT_RESULT="FAILED (resolve)"; return 0
  fi
  ok "the newest set is installed (one resolve)"
  # C4: what is still behind is capped by another package; force it with overrides, a package at a time when a batch fails
  while [ "$round" -lt 3 ]; do
    round=$((round + 1))
    local behind=() pairs=""
    while read -r n have want; do [ -n "$n" ] && behind+=("$n|$have|$want"); done < <(_base_outdated)
    [ "${#behind[@]}" -gt 0 ] || break
    local fresh=0 b
    for b in "${behind[@]}"; do
      IFS='|' read -r n have want <<< "$b"
      case "$tried" in *" $n=$want "*) continue;; esac
      tried="$tried$n=$want "; fresh=1; pairs="$pairs$n==$want"$'\n'
    done
    [ "$fresh" = "1" ] || break
    printf '%s' "$pairs" > "$BASE_LIFT_DIR/overrides.txt"
    if [ -n "$over" ]; then printf '%s' "$over" >> "$BASE_LIFT_DIR/overrides.txt"; fi
    note "round $round: forcing $(printf '%s' "$pairs" | grep -c . | tr -d ' ') package(s) past another package's cap"
    if _base_derived_install --overrides "$BASE_LIFT_DIR/overrides.txt" >>"$log" 2>&1; then
      _base_revert_forced "$over$pairs" "$pairs"
      if [ "$_BASE_KEPT" != "$over$pairs" ]; then _base_install_overrides "$_BASE_KEPT" >>"$log" 2>&1 || true; fi
      over="$_BASE_KEPT"
    else
      # one at a time: a package whose newest has no build for this Python or platform must not stop the others
      while IFS= read -r line; do
        [ -n "$line" ] || continue
        printf '%s%s\n' "$over" "$line" > "$BASE_LIFT_DIR/overrides.txt"
        if _base_derived_install --overrides "$BASE_LIFT_DIR/overrides.txt" >>"$log" 2>&1; then
          _base_revert_forced "$over$line"$'\n' "$line"
          if [ "$_BASE_KEPT" != "$over$line"$'\n' ]; then _base_install_overrides "$_BASE_KEPT" >>"$log" 2>&1 || true; fi
          over="$_BASE_KEPT"
        else BASE_UPSTREAM+=("${line%%==*} ${line#*==}: cannot be installed here ($(grep -v '^\s*$' "$log" | tail -1 | cut -c1-160)); asked for by $(_base_lift_who "${line%%==*}")"); fi
      done <<< "$pairs"
      printf '%s' "$over" > "$BASE_LIFT_DIR/overrides.txt"
    fi
  done
  # whatever the loop moved is recorded; whatever is STILL behind is a package the base could have moved and did not
  local forced; forced="$(printf '%s' "$over" | grep -c . | tr -d ' ')"
  if [ "$forced" != "0" ]; then
    while IFS= read -r line; do [ -n "$line" ] && BASE_LIFT_ROWS+=("${line%%==*} → ${line#*==} (forced past another package's cap; asked for by $(_base_lift_who "${line%%==*}"))"); done <<< "$over"
    ok "$forced package(s) forced to their newest past another package's cap"
  fi
  local still=()
  while read -r n have want; do
    [ -n "$n" ] || continue
    case "${BASE_UPSTREAM[*]-}" in *"$n $want:"*) continue;; esac
    still+=("$n $have < $want")
  done < <(_base_outdated)
  if [ "${#still[@]}" -gt 0 ]; then
    for n in "${still[@]}"; do miss "still behind: $n"; done
    BASE_FAILED+=("newest: still behind after the override loop: ${still[*]}")
  fi
  # C6: the report
  BASE_FRONTEND_INFO="$("$PY" -c 'import importlib.metadata as m
out = []
for n, k in (("comfyui-frontend-package", "frontend"), ("comfyui-workflow-templates", "templates"), ("comfyui-embedded-docs", "docs")):
    try: out.append("%s %s" % (k, m.version(n)))
    except Exception: pass
print(" · ".join(out))' 2>/dev/null || true)"
  [ -n "$BASE_FRONTEND_INFO" ] && ok "$BASE_FRONTEND_INFO" || true
  local conflicts; conflicts="$(_base_uv pip check --python "$PY" 2>/dev/null | grep -iE 'requires|incompatible|conflict' | head -12 || true)"
  if [ -n "$conflicts" ]; then
    while IFS= read -r line; do [ -n "$line" ] && BASE_WARN+=("pip check: $line (a pin was lifted past what its package declares)"); done <<< "$conflicts"
  fi
  BASE_LIFT_RESULT="ok: ${#BASE_LIFT_ROWS[@]} lifted or forced · ${#BASE_UPSTREAM[@]} named upstream"
  return 0
}

base_lift_rollback_core(){ # C5, core only: put back exactly the packages this run changed, from the pre-run snapshot
  [ -n "${BASE_VENV_SNAPSHOT:-}" ] && [ -f "$BASE_VENV_SNAPSHOT" ] || { miss "no pre-run snapshot to roll back to"; return 1; }
  local backend now back
  backend="$(cat "$BASE_VENV_SNAPSHOT.backend" 2>/dev/null || true)"
  _base_lift_dir; now="$BASE_LIFT_DIR/post.txt"; back="$BASE_LIFT_DIR/rollback.txt"
  _base_uv pip freeze --python "$PY" > "$now" 2>/dev/null || return 1
  # lines in the snapshot that are not in the venv now: exactly what this run moved (VCS lines carry their commit)
  grep -vxFf "$now" "$BASE_VENV_SNAPSHOT" > "$back" || true
  if [ ! -s "$back" ]; then note "nothing this run changed differs from the snapshot"; return 0; fi
  note "rolling back $(grep -c . "$back" | tr -d ' ') package(s) this run changed, to the pre-run snapshot"
  local idx=(); while IFS= read -r a; do idx+=("$a"); done < <(_base_uv_index_args)
  _base_uv pip install --python "$PY" ${backend:+--torch-backend "$backend"} ${idx[@]+"${idx[@]}"} -r "$back" 2>&1 | tail -4
}
