#!/bin/bash
# boot.sh — the base's own boot on any host. Installed to <volume>/comfy-base/boot.sh; on RunPod podctl points the pod's
# start command at it, on a VM host the base's systemd unit runs it (comfy-base-boot.service, 2.2.0). bash 3.2-clean
# (the Mac runs these functions in the suite). Never `set -e`: a boot that dies takes the machine with it, and the whole
# point of this file is that it always comes up reachable.
#
#     bash boot.sh                          the boot (sshd → JupyterLab → ComfyUI → sleep infinity); BOOT_HOME=<volume>/comfy-base
#     bash boot.sh --print-sshd-bootstrap   the self-contained sshd snippet podctl puts in a RunPod start command
#
# BOOT_HOME defaults to this file's own directory (the image's ENV or the unit sets it explicitly). BOOT_ROOT prefixes
# /root and /run for the suite's fake pods; empty on a machine. BASE_LISTEN is 0.0.0.0 on RunPod and 127.0.0.1 elsewhere.

_boot_sshd_bin(){ # sshd on PATH; on a pod (no BOOT_ROOT) also the usual /usr/sbin/sshd, which is off PATH in some images
  command -v sshd 2>/dev/null && return 0
  if [ -z "${BOOT_ROOT:-}" ] && [ -x /usr/sbin/sshd ]; then echo /usr/sbin/sshd; return 0; fi
  return 1
}
boot_sshd(){ # authorized_keys from $PUBLIC_KEY, openssh-server when the image has none, sshd on 22 (keys only). Idempotent.
  # 2.1.0: no key, no sshd. A customer's pod from the public template has no PUBLIC_KEY (the toggle that sets it exists
  # only on RunPod's official templates), and a listener nobody can log in to is a port with no purpose.
  local root="${BOOT_ROOT:-}" sshd key="${PUBLIC_KEY:-}"
  if [ -z "$key" ]; then echo "boot: PUBLIC_KEY is empty — sshd not started (set PUBLIC_KEY on the pod to enable ssh)"; return 0; fi
  mkdir -p "$root/root/.ssh" && chmod 700 "$root/root/.ssh"
  printf '%s\n' "$key" > "$root/root/.ssh/authorized_keys" && chmod 600 "$root/root/.ssh/authorized_keys"
  if ! _boot_sshd_bin >/dev/null; then
    echo "boot: installing openssh-server"
    (apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server) >/dev/null 2>&1 || echo "boot: apt could not install openssh-server"
  fi
  mkdir -p "$root/run/sshd"
  ssh-keygen -A >/dev/null 2>&1 || true
  if pgrep -x sshd >/dev/null 2>&1; then echo "boot: sshd already running"; return 0; fi
  sshd="$(_boot_sshd_bin)" || { echo "boot: no sshd binary — the pod is reachable through JupyterLab only"; return 0; }
  if "$sshd" -p 22 -o PasswordAuthentication=no -o PermitRootLogin=prohibit-password; then echo "boot: sshd up on 22"
  else echo "boot: sshd did not start"; fi
  return 0
}

boot_tcmalloc(){ # the allocator the images preload; harmless when absent
  local so; so="$(ldconfig -p 2>/dev/null | grep -o 'libtcmalloc[^ ]*so[^ ]*' | head -1 || true)"
  [ -n "$so" ] && export LD_PRELOAD="$so" || true
}
boot_launch_lib(){ # 85-launch.sh: on the pod beside boot.sh's lib/, in the suite beside boot.sh itself
  local d here; here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  for d in "$BOOT_HOME/lib" "$here" "$here/lib"; do
    if [ -f "$d/85-launch.sh" ]; then . "$d/85-launch.sh"; return 0; fi
  done
  echo "boot: 85-launch.sh not found (looked in $BOOT_HOME/lib and beside boot.sh) — not starting ComfyUI"; return 1
}
boot_host(){ # 2.2.0: which host this is and where it may listen. RunPod's own variable, else the driver's state/host.env, else a VM.
  local f="$BOOT_HOME/state/host.env" h=""
  if [ -f "$f" ]; then h="$(sed -n 's/^BASE_HOST=//p' "$f" | head -1)"; fi
  if [ -n "${RUNPOD_POD_ID:-}" ]; then h=runpod; fi
  BASE_HOST="${BASE_HOST:-${h:-vm}}"
  if [ -z "${BASE_LISTEN:-}" ]; then if [ "$BASE_HOST" = runpod ]; then BASE_LISTEN=0.0.0.0; else BASE_LISTEN=127.0.0.1; fi; fi
  BOOT_VOLUME="$(dirname "$BOOT_HOME")"
  # 2.4.0: the shared library, so the machine's own boot starts ComfyUI on the same output/ and input/ the
  # install pointed it at. The unit RequiresMountsFor it, so by here it is mounted or the server never ran.
  if [ -z "${BASE_LIBRARY:-}" ] && [ -f "$f" ]; then BASE_LIBRARY="$(sed -n 's/^BASE_LIBRARY=//p' "$f" | head -1)"; fi
  BASE_LIBRARY="${BASE_LIBRARY%/}"
  if [ -n "${BASE_LIBRARY:-}" ] && [ ! -d "$BASE_LIBRARY" ]; then
    echo "boot: BASE_LIBRARY=$BASE_LIBRARY is not a directory, starting WITHOUT the shared library"
    BASE_LIBRARY=""
  fi
  export BASE_HOST BASE_LISTEN BASE_LIBRARY
  echo "boot: host $BASE_HOST · listen $BASE_LISTEN · volume $BOOT_VOLUME · library ${BASE_LIBRARY:-none}"
}
boot_extensions(){ # 2.2.0: a project's own boot stages, ext/*.sh, sourced in name order. Looked for on the volume first, then beside
  # this very file: on a baked image's FIRST boot the volume is empty and the seed's own ext/ (beside the seed's boot.sh) is the
  # copy that has to run, because copying the seed onto the volume is what such an extension does. The base ships no extension.
  local d f n=0 here; here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  for d in "$BOOT_HOME/ext" "$here/ext"; do
    [ -d "$d" ] || continue
    for f in "$d"/*.sh; do
      [ -f "$f" ] || continue
      echo "boot: extension $f"; . "$f"; n=$((n+1))
    done
    if [ "$n" -gt 0 ]; then break; fi
  done
  [ "$n" -gt 0 ] || echo "boot: no extensions (no ext/*.sh on the volume or beside boot.sh)"
  return 0
}
boot_jupyter(){ # JupyterLab from the base's tools venv; the token rides the environment (never argv); no token, no JupyterLab
  # 2.1.0: the tokenless start is gone. It was survivable while every pod set JUPYTER_TOKEN by hand; a pod from a public
  # template sets nothing, and an unauthenticated JupyterLab on a *.proxy.runpod.net URL is root on the box. A pod that wants a terminal sets JUPYTER_TOKEN (or JUPYTER_PASSWORD) and stops/starts.
  local jl="$BOOT_HOME/tools/bin/jupyter-lab" tok="${JUPYTER_TOKEN:-${JUPYTER_PASSWORD:-}}" made=""
  local listen="${BASE_LISTEN:-127.0.0.1}"
  if [ ! -x "$jl" ]; then echo "boot: no JupyterLab in $BOOT_HOME/tools (the base install builds it) — port 8888 stays dark"; return 0; fi
  # read the saved one FIRST, so this does not depend on boot_tokens having run: a second boot must reuse the
  # token, never append a new line. My own test caught it doing exactly that.
  local tf="$BOOT_HOME/state/tokens.env"
  if [ -z "$tok" ] && [ -f "$tf" ]; then tok="$(sed -n 's/^JUPYTER_TOKEN=//p' "$tf" | tail -1)"; fi
  if [ -z "$tok" ]; then
    # 2.5.6: with no credential, GENERATE one rather than leave the terminal dark — but ONLY where the server binds
    # loopback and is therefore reachable through an ssh tunnel and nothing else. That is every host but RunPod.
    # On a public bind 2.1.0's refusal stands unchanged: a generated token is still a service listening on a
    # *.proxy.runpod.net URL that nobody asked to start, and the operator has somewhere to read the token only if
    # they already have the box. Fail closed, the house default.
    case "$listen" in
      127.0.0.1|localhost|::1|'')
        tok="$( (openssl rand -hex 32 2>/dev/null) || (head -c32 /dev/urandom | od -An -tx1 | tr -d " \n") )"
        if [ -z "$tok" ]; then echo "boot: JupyterLab not started — no token could be generated and none was set (never tokenless)"; return 0; fi
        mkdir -p "$BOOT_HOME/state"; ( umask 077; printf 'JUPYTER_TOKEN=%s\n' "$tok" >> "$tf" )
        chmod 600 "$tf" 2>/dev/null || true
        export JUPYTER_TOKEN="$tok"; made=" (token generated, saved to state/tokens.env)"
        ;;
      *)
        echo "boot: JupyterLab not started — no JUPYTER_TOKEN or JUPYTER_PASSWORD, and this host binds $listen, which is"
        echo "boot:   reachable from outside. JupyterLab is never tokenless: set one and restart, or bind loopback and"
        echo "boot:   reach it through an ssh tunnel, which generates a token for you."
        return 0
        ;;
    esac
  fi
  mkdir -p "$BOOT_HOME/state/logs"
  JUPYTER_TOKEN="$tok" nohup "$jl" --ip "${BASE_LISTEN:-127.0.0.1}" --port 8888 --allow-root --no-browser --ServerApp.allow_origin='*' --notebook-dir "${BOOT_VOLUME:-$(dirname "$BOOT_HOME")}" >> "$BOOT_HOME/state/logs/jupyter.log" 2>&1 &
  echo "boot: JupyterLab starting on 8888$made"
  # The token is NEVER echoed here. This log is a file on the volume, and with a shared store that volume is mounted
  # by every machine and readable by every session on it; the suite has forbidden logging an operator-supplied token
  # since 2.1.0 and a generated one is the same secret. What goes in the log is how to GET it.
  # It is never reachable from the internet either: the server binds $BASE_LISTEN, and on every host but RunPod that
  # is loopback, which is what Verda's own documentation instructs ("Keep it private. Use SSH port-forwarding. Do
  # not open port 8888 to the internet"). hostname -I reads the machine's own NIC: nothing is asked of the network.
  local ip; ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo "boot: to use the terminal and upload files, from YOUR OWN computer run:"
  echo "boot:     podctl jupyter <this machine>     # prints the URL with the token already in it"
  echo "boot:   or by hand:  ssh -L 8888:127.0.0.1:8888 root@${ip:-THE-MACHINE-IP}"
  echo "boot:     then open http://127.0.0.1:8888/lab and paste the token from state/tokens.env (owner-only)"
  return 0
}
boot_tokens(){ # 2.1.0: the tokens an earlier install saved (state/tokens.env, 0600) join the pod's own; the Hub browser
  # pack reads its own name. Values are exported, never echoed. 20-comfyui.sh's _base_tokens_export_buttons is the
  # install-time twin; boot.sh sources none of lib/[0-9]* so it carries its own lines.
  local f="$BOOT_HOME/state/tokens.env" name val n=0
  if [ -f "$f" ]; then
    while IFS='=' read -r name val; do
      [[ "$name" =~ ^[A-Z_]+$ ]] || continue
      if [ -z "${!name:-}" ] && [ -n "$val" ]; then export "$name=$val"; n=$((n+1)); fi   # the pod's own value wins over a saved one
    done < "$f"
  fi
  local names=""
  if [ -n "${HF_TOKEN:-}" ]; then export HUGGINGFACE_API_KEY="$HF_TOKEN"; names="HUGGINGFACE_API_KEY"; fi
  echo "boot: tokens — $n loaded from state/tokens.env; exported for the download buttons: ${names:-(none)}"
  return 0
}
boot_comfy(){ # ComfyUI from the base's venv with the base's launch line, only when that venv can boot
  local f="$BOOT_HOME/state/boot.env"
  if [ ! -f "$f" ]; then echo "boot: no $f — the base has not installed ComfyUI on this volume yet; nothing to start"; return 0; fi
  . "$f"
  PY="$VENV/bin/python"; HOSTPORT="127.0.0.1:$PORT"; COMFY_LOG="$BOOT_HOME/state/logs/comfyui.log"
  BASE_PERSIST_ROOT="${PERSIST:-}"; BASE_PREVIEW_SIZE="${PREVIEW_SIZE:-1024}"; ARGS_IMPORT=""
  if [ ! -x "$PY" ]; then echo "boot: $PY is missing — not starting ComfyUI. Fix: bash '<package>-script.sh' rescue"; return 0; fi
  if ! "$PY" -c 'import torch' >/dev/null 2>&1; then echo "boot: $VENV cannot import torch — not starting ComfyUI. Fix: bash '<package>-script.sh' rescue, or re-run a package script"; return 0; fi
  boot_launch_lib || return 0
  if command -v nvidia-smi >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1 && [ -f "$BOOT_HOME/py/gpu_facts.py" ]; then
    echo "boot: gpu $(python3 "$BOOT_HOME/py/gpu_facts.py" 2>/dev/null || echo 'unreadable')"     # NOT-OURS here = memory held outside this container: stop/start or redeploy (2.0.26)
  fi
  echo "boot: starting ComfyUI: $(_base_start_cmd)"
  if _base_start_comfy; then echo "boot: ComfyUI answers on $HOSTPORT"; else echo "boot: ComfyUI did not answer within the wait — see $COMFY_LOG"; fi
  return 0
}
boot_main(){
  case "${1:-}" in
    --print-sshd-bootstrap) declare -f _boot_sshd_bin; declare -f boot_sshd; echo "boot_sshd"; return 0 ;;
  esac
  BOOT_HOME="${BOOT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"   # the image's ENV or the unit sets it; this file's own directory otherwise
  mkdir -p "$BOOT_HOME/state/logs"
  echo $$ > "$BOOT_HOME/state/boot.pid"        # the pod tier tells a base boot from the image's by this pid's cmdline (2.0.6)
  BOOT_LOG="$BOOT_HOME/state/logs/boot_$(date +%Y%m%d-%H%M%S).log"
  exec >> "$BOOT_LOG" 2>&1
  trap 'exit 0' TERM
  echo "== comfy-base boot $(date -u +%Y-%m-%dT%H:%M:%SZ) · $(hostname 2>/dev/null || echo ?) =="
  boot_tcmalloc
  boot_host
  boot_sshd
  boot_jupyter
  boot_tokens
  boot_extensions
  boot_comfy
  echo "boot: done — sleeping forever so the pod stays up (this script never exits on its own)"
  if [ "${BOOT_ONCE:-0}" = "1" ]; then return 0; fi
  sleep infinity
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then boot_main "$@"; fi
