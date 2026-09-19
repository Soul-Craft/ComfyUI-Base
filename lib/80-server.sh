# 80-server.sh — the running ComfyUI: import check in the venv, the opt-in restart, smoke, combos. The launch line itself
# lives in 85-launch.sh, shared with boot.sh.
# By default nothing here stops a server: on a shared pod that process may be mid-render, so restarting is the
# user's call. BASE_RESTART=1 opts in.

_base_pids_from_table(){ # stdin: pid|ppid|cwd|argv → the pids of ComfyUI servers. A python running main.py from $COMFY
  # (by cwd), or one naming a …/ComfyUI/main.py path (an image's own launch, whatever its cwd). PID 1 is reported, never printed.
  local pid ppid cwd argv script dir
  while IFS='|' read -r pid ppid cwd argv; do
    [ -n "$pid" ] || continue
    set -- $argv; [ $# -ge 2 ] || continue
    case "$1" in *python*) ;; *) continue;; esac
    script="$2"; dir=""
    case "$script" in main.py) ;; */main.py) dir="${script%/main.py}";; *) continue;; esac
    if [ "$cwd" = "$COMFY" ] || { [ -n "$dir" ] && { [ "$dir" = "$COMFY" ] || [ "$(basename "$dir")" = "ComfyUI" ]; }; }; then
      if [ "$pid" = "1" ]; then echo "  ! PID 1 is ComfyUI itself ($argv) — never killed: stopping it would stop the pod" >&2; continue; fi
      echo "$pid"
    fi
  done
  return 0
}
_base_comfy_pids(){ # pids of ComfyUI servers on this pod: /proc → the one matcher above
  local pid
  [ -d /proc ] || return 0
  for pid in $(pgrep -f 'main\.py' 2>/dev/null || true); do
    [ -r "/proc/$pid/cmdline" ] || continue
    printf '%s|%s|%s|%s\n' "$pid" "$(awk '{print $4}' "/proc/$pid/stat" 2>/dev/null || echo 0)" "$(readlink "/proc/$pid/cwd" 2>/dev/null || true)" "$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)"
  done | _base_pids_from_table
  return 0
}
_base_preview_effective(){ # what preview method is ACTUALLY in effect: the UI setting overrides the CLI flag per queue
  local flag="" setting="" cfg="$(_base_user_dir)/default/comfy.settings.json"
  if [ -n "$RUN_PID" ] && [ -r "/proc/$RUN_PID/cmdline" ]; then tr '\0' '\n' < "/proc/$RUN_PID/cmdline" 2>/dev/null | grep -qx -- '--preview-method' && flag=1 || true
  else flag="?"; fi
  if [ -r "$cfg" ]; then setting="$("$SYS_PY" -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get("Comfy.Execution.PreviewMethod", "") or "")
except Exception: print("")' "$cfg" 2>/dev/null || true)"; fi
  case "$setting" in
    taesd|auto|latent2rgb) ok "live preview method is \"$setting\" in the UI — that overrides the CLI, previews are on" ;;
    none) warn "previews off: the UI has Live preview method = \"none\", which overrides the launch flag (Settings > Execution > Live preview method → auto)" ;;
    *) if [ "$flag" = "1" ]; then ok "running server carries --preview-method and the UI defers to it — previews are on"
       elif [ "$flag" = "?" ]; then note "no running server to check — previews depend on the launch line printed above"
       else warn "previews off: the running server was started WITHOUT --preview-method and the UI defers to it (fix without restarting: Settings > Execution > Live preview method → auto)"; fi ;;
  esac
}

base_import_check(){ # main.py --quick-test-for-ci in the venv; a pack WE installed failing to import blocks the restart advice
  hdr "IMPORT CHECK · main.py --quick-test-for-ci in $VENV"
  if [ "$BASE_DRY" = "1" ]; then note "skipped in --check"; BASE_IMPORT_RESULT="skipped (--check)"; return 0; fi
  if [ "$BASE_NO_NET" = "1" ]; then note "skipped (BASE_NO_NET: fake venv)"; BASE_IMPORT_RESULT="skipped (fake)"; return 0; fi
  case "$BASE_VENV_RESULT" in failed*|rolled*) note "skipped — the venv step failed"; BASE_IMPORT_RESULT="skipped (venv failed)"; BASE_BLOCK_RESTART=1; return 0;; esac
  if [ ! -x "$PY" ]; then note "skipped — no interpreter at $PY"; BASE_IMPORT_RESULT="skipped (no venv)"; return 0; fi
  _base_tmp; local log="$BASE_TMPD/import_check.log" rc=0 entry name dir ours=() others=()
  if (cd "$COMFY" && _base_timeout 900 "$PY" main.py --quick-test-for-ci --disable-auto-launch >"$log" 2>&1); then :; else rc=$?; fi
  for entry in ${PACK_DIRS[@]+"${PACK_DIRS[@]}"}; do
    name="${entry%%|*}"; dir="${entry#*|}"
    if grep -F "IMPORT FAILED" "$log" | grep -qF -- "$(basename "$dir")"; then ours+=("$name"); fi
  done
  while IFS= read -r name; do
    if [ -n "$name" ] && ! _base_in_list "$name" ${ours[@]+"${ours[@]}"}; then others+=("$name"); fi
  done < <(grep -E '\(IMPORT FAILED\):' "$log" | sed -E 's/.*\(IMPORT FAILED\):[[:space:]]*//' | xargs -n1 basename 2>/dev/null || true)
  if [ "${#ours[@]}" -gt 0 ]; then
    err "pack(s) this run installed do not import in this venv: ${ours[*]}"
    local diag
    for name in "${ours[@]}"; do
      echo "  ── $name"
      diag="$(grep -n -B2 -A25 -F "$name" "$log" 2>/dev/null | grep -E 'Traceback|Error|error|File "' 2>/dev/null | head -20 || true)"
      if [ -n "$diag" ]; then printf '%s\n' "$diag" | sed 's/^/     /'; else echo "     (no traceback line matched '$name' — read the full log)"; fi
    done
    cp "$log" "$BASE_STATE/import_check.log" 2>/dev/null || true; echo "  full log: $BASE_STATE/import_check.log"
    BASE_FAILED+=("import check: ${ours[*]} failed to import"); BASE_IMPORT_RESULT="FAILED: ${ours[*]}"; BASE_BLOCK_RESTART=1
    if declare -F pkg_import_check >/dev/null; then pkg_import_check || true; fi
    if [ -n "$BASE_VENV_BACKUP" ] && _base_confirm_yes "Roll the venv back to $BASE_VENV_BACKUP (the old server keeps running either way)? [Y/n] "; then _base_venv_rollback || true; fi
    return 0
  fi
  if [ "${#others[@]}" -gt 0 ]; then warn "other packs failed to import (not installed by this run, not blocking): ${others[*]}"; fi
  if [ "$rc" -ne 0 ] && ! grep -q "IMPORT FAILED" "$log"; then
    miss "quick-test exited $rc without an IMPORT FAILED line — last lines:"; tail -15 "$log" | sed 's/^/     /'
    BASE_FAILED+=("import check: main.py exited $rc"); BASE_IMPORT_RESULT="FAILED (exit $rc)"; BASE_BLOCK_RESTART=1; return 0
  fi
  if declare -F pkg_import_check >/dev/null; then pkg_import_check || { BASE_BLOCK_RESTART=1; BASE_IMPORT_RESULT="FAILED (package check)"; return 0; }; fi
  BASE_IMPORT_RESULT="ok (${#PACK_DIRS[@]} packs import)"; ok "every pack imports; ComfyUI loaded and exited cleanly"
}

base_restart(){ # reports what needs a restart and prints the exact launch line; BASE_RESTART=1 actually does it
  hdr "COMFYUI RESTART"
  if [ "$BASE_DRY" = "1" ] || [ "$BASE_NO_NET" = "1" ]; then note "skipped"; BASE_RESTART_RESULT="skipped"; return 0; fi
  BASE_RESTART_WHY=()
  if [ -n "$COMFY_NEW" ] && [ "$COMFY_NEW" != "$COMFY_OLD" ] && [ "$COMFY_NEW" != "?" ]; then BASE_RESTART_WHY+=("ComfyUI moved $COMFY_OLD → $COMFY_NEW; the running process is the old code"); fi
  if [ "${#MODEL_DL[@]}" -gt 0 ]; then BASE_RESTART_WHY+=("${#MODEL_DL[@]} model file(s) downloaded"); fi
  if [ "${#MODEL_MOVED[@]}" -gt 0 ]; then BASE_RESTART_WHY+=("${#MODEL_MOVED[@]} model file(s) relocated"); fi
  if [ "${#PACK_CLONED[@]}" -gt 0 ] || [ "${#PACK_UPDATED[@]}" -gt 0 ]; then BASE_RESTART_WHY+=("node packs cloned or moved to their pinned commits"); fi
  # every value that means the venv CHANGED — matching one string here once silently missed "rebuilt"
  case "$BASE_VENV_RESULT" in built|rebuilt|adopted) BASE_RESTART_WHY+=("the venv was $BASE_VENV_RESULT");; esac
  if [ "${#BASE_CHANGED[@]}" -gt 0 ]; then BASE_RESTART_WHY+=("${#BASE_CHANGED[@]} hygiene change(s), including launch args"); fi
  # A package's own reasons, set by a hook BEFORE this runs (pkg_pre_models / pkg_post_models).
  # ComfyUI registers custom nodes at import time, so a package that ships one has no other way
  # to say "the running process cannot see this yet".
  if [ "${#PKG_RESTART_WHY[@]}" -gt 0 ]; then BASE_RESTART_WHY+=(${PKG_RESTART_WHY[@]+"${PKG_RESTART_WHY[@]}"}); fi
  local live=0
  curl -sf --max-time 5 "http://$HOSTPORT/system_stats" >/dev/null 2>&1 && live=1
  if [ "$live" = 0 ]; then
    if [ "${BASE_BLOCK_RESTART:-0}" = "1" ]; then miss "do NOT start ComfyUI — the import check failed; fix that first"; BASE_RESTART_RESULT="do not start (import check failed)"; BASE_RESTART_NEEDED=1; return 0; fi
    if [ "$BASE_RESTART" = "1" ]; then       # opting in means a running server at the end, whether or not one was running before (2.0.11)
      if _base_start_comfy; then ok "ComfyUI started (BASE_RESTART=1)"; BASE_RESTART_RESULT="started"; RUN_PID="$(_base_comfy_pids | head -1 || true)"; _base_preview_effective
      else err "ComfyUI did not answer within ${BASE_START_WAIT:-600} s — last 40 lines of $COMFY_LOG:"; tail -40 "$COMFY_LOG" 2>/dev/null | sed 's/^/     /'; BASE_RESTART_RESULT="FAILED"; BASE_FAILED+=("ComfyUI did not come up"); fi
      return 0
    fi
    BASE_RESTART_NEEDED=1; BASE_RESTART_RESULT="ComfyUI is not running"
    todo "ComfyUI is not running on $HOSTPORT — start it:"
    echo ""; echo "        cd $COMFY && nohup $(_base_start_cmd) >> $COMFY_LOG 2>&1 &"; echo ""
    echo "      Then verify:  bash \"$(basename "${PKG_SCRIPT:-$0}")\" test"
    return 0
  fi
  if [ "${BASE_BLOCK_RESTART:-0}" = "1" ]; then miss "do NOT restart — the import check failed; the running server is still healthy"; BASE_RESTART_RESULT="do not restart (import check failed)"; return 0; fi
  if [ "${#BASE_RESTART_WHY[@]}" -eq 0 ]; then ok "nothing changed that needs a restart"; BASE_RESTART_RESULT="not needed"; _base_preview_effective; return 0; fi
  if [ "$BASE_RESTART" = "1" ]; then
    local pids; pids="$(_base_comfy_pids || true)"
    if [ -n "$pids" ]; then
      echo "  stopping $pids"; kill $pids 2>/dev/null || true
      local i; for i in $(seq 1 20); do sleep 1; if [ -z "$(_base_comfy_pids || true)" ]; then break; fi; done
      if [ -n "$(_base_comfy_pids || true)" ]; then kill -9 $(_base_comfy_pids) 2>/dev/null || true; sleep 2; fi
    fi
    if _base_start_comfy; then ok "ComfyUI restarted (BASE_RESTART=1)"; BASE_RESTART_RESULT="restarted"; RUN_PID="$(_base_comfy_pids | head -1 || true)"; _base_preview_effective
    else err "ComfyUI did not answer within ${BASE_START_WAIT:-600} s — last 40 lines of $COMFY_LOG:"; tail -40 "$COMFY_LOG" 2>/dev/null | sed 's/^/     /'; BASE_RESTART_RESULT="FAILED"; BASE_FAILED+=("ComfyUI did not come back up"); fi
    return 0
  fi
  BASE_RESTART_NEEDED=1; BASE_RESTART_RESULT="YOURS TO DO"
  todo "ComfyUI needs a restart before it can see this:"
  local r; for r in "${BASE_RESTART_WHY[@]}"; do echo "        - $r"; done
  echo "      Nothing has been stopped — the server you had running is untouched."
  local pids; pids="$(_base_comfy_pids 2>/dev/null | tr '\n' ' ' || true)"
  echo "      From this shell that would be:"; echo ""
  if [ -n "${pids// /}" ]; then echo "        kill ${pids% }"; fi
  echo "        cd $COMFY && nohup $(_base_start_cmd) >> $COMFY_LOG 2>&1 &"; echo ""
  echo "      Then verify:  bash \"$(basename "${PKG_SCRIPT:-$0}")\" test"
}

base_smoke(){ # every node type the workflow uses is registered on the live server; never blocks
  hdr "SMOKE · $HOSTPORT"
  if [ "$BASE_DRY" = "1" ] || [ "$BASE_NO_NET" = "1" ]; then note "skipped"; BASE_SMOKE_RESULT="skipped"; return 0; fi
  if [ -z "${WF_NAME:-}" ] || [ ! -f "${PKG_DIR:-.}/$WF_NAME" ]; then note "skipped — no workflow to smoke"; BASE_SMOKE_RESULT="skipped (no workflow)"; return 0; fi
  if ! curl -sf --max-time 5 "http://$HOSTPORT/system_stats" >/dev/null 2>&1; then
    note "skipped — nothing is listening on $HOSTPORT; start ComfyUI, then: bash \"$(basename "${PKG_SCRIPT:-$0}")\" test"
    BASE_SMOKE_RESULT="skipped (ComfyUI not running)"; return 0
  fi
  local label=""; [ "$BASE_RESTART_NEEDED" = "1" ] && label=" (pre-restart: the server is the OLD code)"
  local api="${PKG_DIR}/${WF_NAME%.json}_api.json"; [ -f "$api" ] || api=""
  if "$SYS_PY" "$BASE_DIR/py/smoke.py" "$PKG_DIR/$WF_NAME" "$HOSTPORT" $api 2>&1 | sed 's/^/  /'; then BASE_SMOKE_RESULT="ok$label"; ok "smoke passed$label"
  elif [ "$BASE_RESTART_NEEDED" = "1" ]; then BASE_SMOKE_RESULT="failed pre-restart"; warn "smoke failed against the pre-restart server — restart, then: bash \"$(basename "${PKG_SCRIPT:-$0}")\" test"
  else BASE_SMOKE_RESULT="FAILED"; BASE_FAILED+=("smoke failed (see above)"); fi
}

base_combos(){ # stored dropdown values → live /object_info; repairs renames (never with a restart pending), fails on values no pack offers
  hdr "COMBO CHECK · stored dropdown values → live /object_info"
  if [ "$BASE_DRY" = "1" ] || [ "$BASE_NO_NET" = "1" ]; then note "skipped"; BASE_COMBO_RESULT="skipped"; return 0; fi
  if [ -z "${WF_NAME:-}" ] || [ ! -f "${PKG_DIR:-.}/$WF_NAME" ]; then note "skipped — no workflow"; BASE_COMBO_RESULT="skipped (no workflow)"; return 0; fi
  if ! curl -sf --max-time 5 "http://$HOSTPORT/system_stats" >/dev/null 2>&1; then note "skipped — no server to ask"; BASE_COMBO_RESULT="skipped (no server)"; return 0; fi
  local pristine="$PKG_DIR/$WF_NAME" copy="$(_base_workflows_dir)/$WF_NAME" t out pre ts f b n_fix=0 n_bad=0 flags=()
  # with a restart pending the server is the old code: report, never rewrite
  [ "$BASE_RESTART_NEEDED" = "1" ] && flags=(--check-only)
  for t in "$pristine" "$copy"; do
    [ -f "$t" ] || continue
    pre="$(_base_mktemp_d)/pre.json"; cp -p "$t" "$pre"
    out="$("$SYS_PY" "$BASE_DIR/py/combofix.py" "$HOSTPORT" "$t" ${flags[@]+"${flags[@]}"} 2>&1)" || true
    printf '%s\n' "$out" | sed '/^COMBOFIX /d' | sed 's/^/  /'
    f="$(printf '%s' "$out" | sed -n 's/^COMBOFIX \([0-9]*\) repaired.*/\1/p' | tail -1)"
    b="$(printf '%s' "$out" | sed -n 's/^COMBOFIX [0-9]* repaired \([0-9]*\).*/\1/p' | tail -1)"
    n_fix=$(( n_fix + ${f:-0} )); n_bad=$(( n_bad + ${b:-0} ))
    if ! cmp -s "$t" "$pre"; then ts="$(_base_ts)"; cp -p "$pre" "$t.$ts.bak"; note "changed — previous version kept as $(basename "$t").$ts.bak"; fi
  done
  if [ "${n_bad:-0}" != "0" ]; then
    if [ "$BASE_RESTART_NEEDED" = "1" ]; then warn "$n_bad dropdown value(s) unresolved against the pre-restart server — re-check after the restart"; BASE_COMBO_RESULT="pre-restart: $n_bad unresolved"
    else err "$n_bad stored dropdown value(s) are not offered by the installed packs — ComfyUI will reject them at queue time"; BASE_FAILED+=("combo check: $n_bad value(s) no pack offers"); BASE_COMBO_RESULT="FAILED ($n_bad unresolved)"; fi
  elif [ "${n_fix:-0}" != "0" ]; then
    if [ "$BASE_RESTART_NEEDED" = "1" ]; then BASE_COMBO_RESULT="pre-restart: $n_fix would be repaired"; note "$n_fix renamed value(s) would be repaired after the restart"
    else ok "repaired $n_fix dropdown value(s) a pack had renamed"; BASE_COMBO_RESULT="repaired $n_fix"; BASE_CHANGED+=("combo: repaired $n_fix renamed dropdown value(s)"); fi
  else ok "every stored dropdown value is offered by the installed packs"; BASE_COMBO_RESULT="ok"; fi
}
