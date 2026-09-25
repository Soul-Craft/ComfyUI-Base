# 92-onepass.sh (3.2.0): an install does each piece of work once, and says what each stage cost.
# The stage clock itself is hdr's (lib/00-env.sh); this file turns it into the summary's block and the store's record.

_base_timings_block(){ # BASE_STAGE_TIMES → "time N s in K stages" then one line per stage that took a second or more, slowest first
  local total=0 n=0 row secs
  for row in ${BASE_STAGE_TIMES[@]+"${BASE_STAGE_TIMES[@]}"}; do total=$(( total + ${row%%|*} )); n=$(( n + 1 )); done
  echo "  time       $total s in $n stages, slowest first:"
  for row in ${BASE_STAGE_TIMES[@]+"${BASE_STAGE_TIMES[@]}"}; do echo "$row"; done | sort -t'|' -k1,1nr | while IFS='|' read -r secs row; do
    if [ "$secs" -gt 0 ]; then printf '             %4d s  %s\n' "$secs" "$row"; fi
  done
  return 0
}
_base_run_suite(){ # the pipeline's suite stage: a dry run skips it (it would test the server the real run is about to replace)
  if [ "$BASE_DRY" = "1" ]; then hdr "TEST SUITE"; note "skipped in --check: the real run tests the server it restarts"; BASE_TEST_RESULT="skipped (--check)"; BASE_TEST_RC=0; return 0; fi
  base_test ""
}
base_preflight(){ # `<script> preflight`: what podctl runs before an install in place of a whole --check pipeline.
  # It stops on exactly what would stop the real run before that run changes anything: invalid declarations or a
  # BASE_MIN this base does not meet (base_init, exit 2 or 3), no volume (exit 3), a workflow whose version is not
  # the script's (exit 1). The driver and the tokens are reported, never judged: the real run upgrades the driver
  # before its gate, and no token is required. No suite, no uv, no filesystem walk: those are the real run's.
  BASE_DRY=1
  base_init
  hdr "PREFLIGHT · what would stop the install before it changes anything"
  base_require_workflow
  note "driver ${DRIVER:-?} (the install upgrades it first, then gates at ${DRIVER_MIN:-580})"
  local row name
  for row in ${TOKENS[@]+"${TOKENS[@]}"}; do
    name="${row%%|*}"; [ -n "$name" ] || continue
    if [ -n "${!name:-}" ] || grep -qs "^$name=" "$BASE_STATE/tokens.env"; then ok "$name present"; else note "no $name yet (the install asks for it, or continues without it)"; fi
  done
  if [ "${#BASE_FAILED[@]}" -gt 0 ]; then err "the install would stop: ${BASE_FAILED[*]}"; exit 1; fi
  ok "nothing stops the install"
  BASE_SUMMARY_DONE=1
  exit 0
}
_base_timings_write(){ # the run's stage times beside its log: state/logs/<pkg>_<ts>.timings.tsv (seconds TAB stage), which
  # podctl fetches back to the Mac with the console after every install; never in a dry run
  [ "$BASE_DRY" = "1" ] && return 0
  [ -n "${BASE_STATE:-}" ] && [ "${#BASE_STAGE_TIMES[@]}" -gt 0 ] || return 0
  local d="$BASE_STATE/logs" row
  mkdir -p "$d" 2>/dev/null || return 0
  for row in "${BASE_STAGE_TIMES[@]}"; do printf '%s\t%s\n' "${row%%|*}" "${row#*|}"; done > "$d/${PKG_ID:-base}_$(_base_ts).timings.tsv" 2>/dev/null || true
  return 0
}
