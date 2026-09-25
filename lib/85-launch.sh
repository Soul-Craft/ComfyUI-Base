# 85-launch.sh — ComfyUI's launch line, in ONE place: the base's restart (80-server.sh) and the pod's boot (boot.sh) both
# run exactly this. Pure functions over COMFY, PY, PORT, HOSTPORT, ARGS_FILE, COMFY_LOG, BASE_PERSIST_ROOT; no side effects
# beyond starting the server. boot.sh sources this file from <volume>/comfy-base/lib/. BASE_LISTEN (2.2.0) is the bind
# address, resolved by base_env_setup or boot_host: 0.0.0.0 on RunPod, whose HTTP proxy needs it; 127.0.0.1 on every
# other host, reached through the driver's tunnel. A caller that sources this file raw gets the pre-2.2.0 default, 0.0.0.0.
# 3.0.3: which processes ARE ComfyUI (the matcher below) lives here too, so the boot can find and stop one it did not start,
# and so the boot's own wait can tell a live server from a dead one (before 3.0.3 boot.sh had no matcher at all).

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
_base_stop_comfy(){ # 3.0.3: stop EVERY ComfyUI server on this machine (TERM, then KILL); 1 when one survives both
  # The house rule is one GPU process per machine: two main.py hold the port and the VRAM between them, and the one that
  # holds the port is not necessarily the newest code.
  local pids i
  pids="$(_base_comfy_pids 2>/dev/null | tr '\n' ' ' || true)"; pids="${pids% }"
  [ -n "$pids" ] || return 0
  echo "  stopping ComfyUI: $pids"; kill $pids 2>/dev/null || true
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    sleep 1; if [ -z "$(_base_comfy_pids 2>/dev/null || true)" ]; then return 0; fi
  done
  pids="$(_base_comfy_pids 2>/dev/null | tr '\n' ' ' || true)"
  if [ -n "$pids" ]; then kill -9 $pids 2>/dev/null || true; sleep 2; fi
  pids="$(_base_comfy_pids 2>/dev/null | tr '\n' ' ' || true)"
  if [ -n "$pids" ]; then echo "  ComfyUI $pids survived TERM and KILL: not starting a second one beside it"; return 1; fi
  return 0
}
_base_args_extra(){ # the base's args file, verbatim, one word per line (before its first creation: the image's file it will import)
  local line _w f="$ARGS_FILE"
  [ -f "$f" ] || f="${ARGS_IMPORT:-}"
  [ -n "$f" ] && [ -f "$f" ] || return 0
  while IFS= read -r line; do case "$line" in ''|'#'*) continue;; esac; read -r -a _w <<< "$line"; printf '%s\n' ${_w[@]+"${_w[@]}"}; done < "$f"
}
_base_mount_of(){ # <path> → the mount point holding it; a path not made yet is judged by its nearest existing ancestor
  local p="$1"
  while [ -n "$p" ] && [ ! -e "$p" ]; do p="$(dirname "$p")"; done
  df -P "${p:-/}" 2>/dev/null | awk 'NR==2{print $NF}'
}
_base_off_store(){ # <dir> → 0 when the store is a mount of its own and <dir> is on the machine's OS disk instead
  # 3.0.3, MEASURED on a Verda machine on a shared store: the args file still named a library mount the machine no longer
  # had, and ComfyUI made that path on the OS disk and wrote every render and upload there, off the store. On an owned box
  # whose store IS the OS disk there is no second disk to land on by mistake, so nothing is off the store there.
  local os store
  os="$(_base_mount_of /)"; store="$(_base_mount_of "${BASE_HOME:-$COMFY}")"
  [ -n "$os" ] && [ -n "$store" ] && [ "$store" != "$os" ] || return 1
  [ "$(_base_mount_of "$1")" = "$os" ]
}
_base_launch_args(){ # 3.0.3: _base_args_extra minus an --output-directory or --input-directory that is off the store
  # The launch line is the last guard: the install's hygiene cleans the args file, but a boot runs no hygiene, and a
  # library that is not mounted at boot would otherwise send every render to the OS disk. Dropped, ComfyUI falls back
  # to its own output/ and input/ inside the tree, which is on the store. Said on stderr, once per launch.
  local w prev=""
  while IFS= read -r w; do
    if [ -n "$prev" ]; then
      if _base_off_store "$w"; then echo "  !! $prev $w is on this machine's OS disk, not on the store: left off the launch line" >&2
      else printf '%s\n' "$prev" "$w"; fi
      prev=""; continue
    fi
    case "$w" in --output-directory|--input-directory) prev="$w" ;; *) printf '%s\n' "$w" ;; esac
  done < <(_base_args_extra)
  if [ -n "$prev" ]; then printf '%s\n' "$prev"; fi
  return 0
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
_base_ffmpeg_path(){ # a system ffmpeg WITH NVENC, for the packs that encode video
  # VideoHelperSuite prefers its own bundled imageio_ffmpeg binary, and that build carries NO nvenc encoders at
  # all. A package whose video format asks for h264_nvenc therefore dies at the very last node, after the whole
  # sample has been computed and paid for: "Unknown encoder 'h264_nvenc'". Measured on a GPU 2026-09-19, on a
  # render that had already cleared its policy gate and produced frames.
  # VHS honours VHS_FORCE_FFMPEG_PATH, so the fix is to find a real ffmpeg and name it. Only when it HAS nvenc:
  # forcing a system build without it would trade one silent wrong encoder for another.
  local f; f="$(command -v ffmpeg 2>/dev/null || true)"
  [ -n "$f" ] || return 0
  "$f" -hide_banner -encoders 2>/dev/null | grep -q h264_nvenc || return 0
  echo "$f"
}
_base_local_dirs(){ # 2.5.6: every directory the launch line names on the MACHINE must exist before it runs
  # The args file lives on the SHARED store, so --user-directory and --temp-directory reach every machine, while
  # the directories they name are per machine and were created only by the install that wrote them. A machine
  # that joined the store without installing therefore had the flags and not the folders, and ComfyUI REFUSED to
  # start: "argument --user-directory: The path ... does not exist". Measured on two machines at once, 2.5.6.
  # 3.0.3: only what the launch line will actually carry, so a path off the store is never made on the OS disk
  local d
  for d in $(_base_launch_args 2>/dev/null | awk '/^\//{print}'); do
    case "$d" in /*) mkdir -p "$d" 2>/dev/null || true ;; esac
  done
}
_base_start_cmd(){ # the exact launch line, in one place, so the hand-off prints what would actually run
  local extra pv hf ff
  extra="$(_base_launch_args 2>/dev/null | tr '\n' ' ')"; extra="${extra% }"
  pv="$(_base_preview_flags)"; hf="$(_base_hf_home)"; ff="$(_base_ffmpeg_path)"
  # `env HF_HOME=…`, not a bare assignment: the line is printed after `nohup`, and nohup would execute the assignment (2.0.11)
  printf '%s%s%s main.py --listen %s --port %s --enable-cors-header%s%s' "${hf:+env HF_HOME=$hf }" "${ff:+env VHS_FORCE_FFMPEG_PATH=$ff }" "$PY" "${BASE_LISTEN:-0.0.0.0}" "$PORT" "${pv:+ $pv}" "${extra:+ $extra}"
}
_base_start_comfy(){
  local extra=() w pv=()
  _base_local_dirs
  while IFS= read -r w; do extra+=("$w"); done < <(_base_launch_args)
  echo "  $(_base_start_cmd)"
  read -r -a pv <<< "$(_base_preview_flags)"
  mkdir -p "$(dirname "$COMFY_LOG")" 2>/dev/null || true
  local hf; hf="$(_base_hf_home)"; [ -n "$hf" ] && mkdir -p "$hf" 2>/dev/null || true
  # HF_HOME is EXPORTED in the subshell: an expansion that yields `HF_HOME=/x` in command position is a command name to
  # bash, not an assignment — the pod's restart died on "HF_HOME=/workspace/huggingface: No such file or directory" (2.0.10)
  local ff; ff="$(_base_ffmpeg_path)"; [ -n "$ff" ] && echo "  ffmpeg with nvenc: $ff (VHS_FORCE_FFMPEG_PATH)" || true
  (cd "$COMFY" && { if [ -n "$hf" ]; then export HF_HOME="$hf"; fi
     if [ -n "$ff" ]; then export VHS_FORCE_FFMPEG_PATH="$ff"; fi
     nohup "$PY" main.py --listen "${BASE_LISTEN:-0.0.0.0}" --port "$PORT" --enable-cors-header ${pv[@]+"${pv[@]}"} ${extra[@]+"${extra[@]}"} >> "$COMFY_LOG" 2>&1 & })
  # MEASURED on a live pod: the custom nodes alone import for over three minutes
  # (ComfyUI-SeedVR2_VideoUpscaler 67 s, and thirty more packs behind it), so a flat 180 s window
  # declared `restart FAILED`, skipped smoke and combos, and failed the whole install — while the
  # server came up healthy a minute later. Two packages hit it; fixed here.
  #
  # Waiting longer alone would be worse, not better: a server that DIED would then cost ten minutes
  # of silence. So the wait is patient while the process lives and gives up the moment it does not,
  # and it says which of the two happened.
  _base_wait_comfy
}
_base_wait_comfy(){ # [grace s] → 0 when HOSTPORT answers; patient while a ComfyUI process lives, done the moment none does
  # grace (3.0.3): how long no process at all still means "not started yet" rather than "died". A start through the boot
  # unit runs sshd, JupyterLab and the extensions first, so ComfyUI appears seconds after the unit does, not at once.
  local grace="${1:-0}" seen=0 tries="${BASE_START_TRIES:-}" waited=0 limit="${BASE_START_WAIT:-600}"
  [ -n "$tries" ] && limit=$((tries * 2))            # older callers (and the suite) size it in 2 s tries
  while [ "$waited" -lt "$limit" ]; do
    sleep 2; waited=$((waited + 2))
    if curl -sf --max-time 5 "http://$HOSTPORT/system_stats" >/dev/null 2>&1; then echo "  up after $waited s"; return 0; fi
    if [ -n "$(_base_comfy_pids 2>/dev/null || true)" ]; then seen=1
    elif [ "$seen" = 1 ] || [ "$waited" -ge "$grace" ]; then
      if [ "$seen" = 1 ] || [ "$grace" = 0 ]; then echo "  the ComfyUI process is gone after $waited s — it did not survive its own start"
      else echo "  no ComfyUI process after $waited s: the boot never started one (see state/logs/boot_*.log)"; fi
      return 1
    fi
    case "$waited" in 60|120|240|360|480) echo "  still importing after $waited s (of $limit)";; esac
  done
  if [ "$seen" = 0 ] && [ -z "$(_base_comfy_pids 2>/dev/null || true)" ]; then echo "  no answer after $limit s, and no ComfyUI process was ever seen"
  else echo "  no answer after $limit s, and the process is still alive — importing, hung, or wedged"; fi
  return 1
}
