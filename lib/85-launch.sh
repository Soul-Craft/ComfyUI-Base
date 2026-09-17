# 85-launch.sh — ComfyUI's launch line, in ONE place: the base's restart (80-server.sh) and the pod's boot (boot.sh) both
# run exactly this. Pure functions over COMFY, PY, PORT, HOSTPORT, ARGS_FILE, COMFY_LOG, BASE_PERSIST_ROOT; no side effects
# beyond starting the server. boot.sh sources this file from <volume>/comfy-base/lib/. BASE_LISTEN (2.2.0) is the bind
# address, resolved by base_env_setup or boot_host: 0.0.0.0 on RunPod, whose HTTP proxy needs it; 127.0.0.1 on every
# other host, reached through the driver's tunnel. A caller that sources this file raw gets the pre-2.2.0 default, 0.0.0.0.

_base_args_extra(){ # the base's args file, verbatim, one word per line (before its first creation: the image's file it will import)
  local line _w f="$ARGS_FILE"
  [ -f "$f" ] || f="${ARGS_IMPORT:-}"
  [ -n "$f" ] && [ -f "$f" ] || return 0
  while IFS= read -r line; do case "$line" in ''|'#'*) continue;; esac; read -r -a _w <<< "$line"; printf '%s\n' ${_w[@]+"${_w[@]}"}; done < "$f"
}
_base_preview_flags(){ # the args file is appended LAST and argparse takes the last flag, so never emit a flag the file already carries
  local have out=()
  have="$(_base_args_extra)"
  grep -qx -- '--preview-method' <<< "$have" || out+=(--preview-method auto)
  grep -qx -- '--preview-size'   <<< "$have" || out+=(--preview-size "${BASE_PREVIEW_SIZE:-1024}")
  printf '%s' "${out[*]-}"
}
_base_hf_home(){ # packs that fetch their own weights at first queue (RMBG, Florence2, DepthAnything, SAM, SeedVR2: ~21 GB) use
  # ~/.cache/huggingface, which is container disk on the pod — re-downloaded after every stop. On the volume it persists.
  if [ -n "$BASE_PERSIST_ROOT" ]; then echo "$BASE_PERSIST_ROOT/huggingface"; fi
}
_base_start_cmd(){ # the exact launch line, in one place, so the hand-off prints what would actually run
  local extra pv hf
  extra="$(_base_args_extra | tr '\n' ' ')"; extra="${extra% }"
  pv="$(_base_preview_flags)"; hf="$(_base_hf_home)"
  # `env HF_HOME=…`, not a bare assignment: the line is printed after `nohup`, and nohup would execute the assignment (2.0.11)
  printf '%s%s main.py --listen %s --port %s --enable-cors-header%s%s' "${hf:+env HF_HOME=$hf }" "$PY" "${BASE_LISTEN:-0.0.0.0}" "$PORT" "${pv:+ $pv}" "${extra:+ $extra}"
}
_base_start_comfy(){
  local extra=() line i _w pv=()
  if [ -f "$ARGS_FILE" ]; then
    while IFS= read -r line; do case "$line" in ''|'#'*) continue;; esac; read -r -a _w <<< "$line"; extra+=(${_w[@]+"${_w[@]}"}); done < "$ARGS_FILE"
  fi
  echo "  $(_base_start_cmd)"
  read -r -a pv <<< "$(_base_preview_flags)"
  mkdir -p "$(dirname "$COMFY_LOG")" 2>/dev/null || true
  local hf; hf="$(_base_hf_home)"; [ -n "$hf" ] && mkdir -p "$hf" 2>/dev/null || true
  # HF_HOME is EXPORTED in the subshell: an expansion that yields `HF_HOME=/x` in command position is a command name to
  # bash, not an assignment — the pod's restart died on "HF_HOME=/workspace/huggingface: No such file or directory" (2.0.10)
  (cd "$COMFY" && { if [ -n "$hf" ]; then export HF_HOME="$hf"; fi
     nohup "$PY" main.py --listen "${BASE_LISTEN:-0.0.0.0}" --port "$PORT" --enable-cors-header ${pv[@]+"${pv[@]}"} ${extra[@]+"${extra[@]}"} >> "$COMFY_LOG" 2>&1 & })
  # MEASURED on a live pod: the custom nodes alone import for over three minutes
  # (ComfyUI-SeedVR2_VideoUpscaler 67 s, and thirty more packs behind it), so a flat 180 s window
  # declared `restart FAILED`, skipped smoke and combos, and failed the whole install — while the
  # server came up healthy a minute later. Two packages hit it; fixed here.
  #
  # Waiting longer alone would be worse, not better: a server that DIED would then cost ten minutes
  # of silence. So the wait is patient while the process lives and gives up the moment it does not,
  # and it says which of the two happened.
  local tries="${BASE_START_TRIES:-}" waited=0 limit="${BASE_START_WAIT:-600}"
  [ -n "$tries" ] && limit=$((tries * 2))            # older callers (and the suite) size it in 2 s tries
  while [ "$waited" -lt "$limit" ]; do
    sleep 2; waited=$((waited + 2))
    if curl -sf --max-time 5 "http://$HOSTPORT/system_stats" >/dev/null 2>&1; then echo "  up after $waited s"; return 0; fi
    if [ -z "$(_base_comfy_pids 2>/dev/null || true)" ]; then
      echo "  the ComfyUI process is gone after $waited s — it did not survive its own start"
      return 1
    fi
    case "$waited" in 60|120|240|360|480) echo "  still importing after $waited s (of $limit)";; esac
  done
  echo "  no answer after $limit s, and the process is still alive — importing, hung, or wedged"
  return 1
}
