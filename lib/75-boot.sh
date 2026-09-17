# 75-boot.sh — the base owns the machine's boot: boot.sh + the launch lib on the volume, the tools venv (JupyterLab),
# state/boot.env (what boot.sh starts). On RunPod podctl points the pod's start command at <volume>/comfy-base/boot.sh;
# on a VM host (2.2.0) the base installs comfy-base-boot.service, which runs it at every boot. From then on every boot is
# the base's: sshd → JupyterLab → ComfyUI from the base's venv, and the script never exits.

_base_jupyter_auth(){ # which variable protects JupyterLab at boot (the value is never shown)
  local v
  for v in JUPYTER_TOKEN JUPYTER_PASSWORD; do
    if [ -n "${!v:-}" ] || [ -n "$(_base_pid1_env "$v")" ]; then echo "$v"; return 0; fi
  done
  echo "NONE — set JUPYTER_TOKEN on the pod"
}
base_boot_env_write(){ # the facts boot.sh needs; written by every real run, single-quoted so boot.sh can source it
  local f="$BASE_STATE/boot.env"
  [ "$BASE_DRY" = "1" ] && return 0
  mkdir -p "$BASE_STATE"
  { echo "# comfy-base boot facts — written by every install; boot.sh sources this"
    printf "COMFY='%s'\n" "$COMFY"; printf "VENV='%s'\n" "$VENV"; printf "PORT='%s'\n" "$PORT"
    printf "ARGS_FILE='%s'\n" "$ARGS_FILE"; printf "PERSIST='%s'\n" "${BASE_PERSIST_ROOT:-}"; printf "BASE_HOME='%s'\n" "$BASE_HOME"
    printf "PREVIEW_SIZE='%s'\n" "${BASE_PREVIEW_SIZE:-1024}"; printf "TS='%s'\n" "$(_base_ts)"; } > "$f"
  ok "boot facts written: $f"
}
base_boot_tools(){ # JupyterLab into the base's tools venv on the volume (the venv uv already lives in), once
  local t mm stamp; t="$BASE_HOME/tools"; stamp="$t/.comfy-base-tools"      # two statements: `local a=x b=$a` expands $a before it exists
  if [ -x "$t/bin/jupyter-lab" ] && [ -f "$stamp" ]; then ok "tools venv present ($(cat "$stamp"))"; BASE_TOOLS_RESULT="present"; return 0; fi
  if [ "$BASE_DRY" = "1" ]; then would "install JupyterLab into the tools venv $t (its python -m pip, from PyPI)"; BASE_TOOLS_RESULT="would-build"; return 0; fi
  if [ "$BASE_NO_NET" = "1" ]; then
    mkdir -p "$t/bin"; printf '#!/bin/bash\necho "fake jupyter-lab $*"\n' > "$t/bin/jupyter-lab"; chmod +x "$t/bin/jupyter-lab"
    echo "fake ts=$(_base_ts)" > "$stamp"; ok "fake tools venv (BASE_NO_NET)"; BASE_TOOLS_RESULT="built (fake)"; BASE_CHANGED+=("tools venv built (fake)"); return 0
  fi
  if ! _base_tools_venv; then warn "tools venv: could not be created — the boot starts without JupyterLab"; BASE_TOOLS_RESULT="failed"; return 0; fi
  if "$t/bin/python" -m pip install -q --disable-pip-version-check jupyterlab 2>&1 | tail -3 && [ -x "$t/bin/jupyter-lab" ]; then
    mm="$("$t/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo ?)"
    echo "python=$mm jupyterlab ts=$(_base_ts)" > "$stamp"; ok "tools venv: JupyterLab installed at $t (Python $mm)"
    BASE_CHANGED+=("tools venv: JupyterLab"); BASE_TOOLS_RESULT="built"
  else warn "tools venv: JupyterLab could not be installed — the boot starts without it"; BASE_TOOLS_RESULT="failed"; fi
  return 0
}
_base_boot_unit_text(){ # the systemd unit that runs the base's boot at every boot of a VM host (Verda, Crusoe, an owned box)
  local mounts=""
  [ "${BASE_VOLUME_KIND:-mount}" = mount ] && mounts="RequiresMountsFor=$BASE_VOLUME"$'\n'
  printf '%s' "[Unit]
Description=ComfyUI Base boot (sshd, JupyterLab, ComfyUI) from $BASE_HOME
After=network-online.target local-fs.target
Wants=network-online.target
${mounts}
[Service]
Type=simple
Environment=BOOT_HOME=$BASE_HOME
Environment=BASE_HOST=$BASE_HOST
Environment=BASE_LISTEN=$BASE_LISTEN
ExecStart=/bin/bash -c 'test -x $BASE_HOME/boot.sh && exec bash $BASE_HOME/boot.sh || exec sleep infinity'
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
"
}
base_boot_unit(){ # 2.2.0: the unit's text always lands beside boot.sh (what a person or a startup script installs); on a live
  # systemd host that is not RunPod it is installed and enabled too. RunPod (a container, no systemd) keeps its start command.
  local text unit="$BASE_HOME/comfy-base-boot.service" sys="/etc/systemd/system/comfy-base-boot.service" changed=0
  text="$(_base_boot_unit_text)"
  if [ "$BASE_DRY" = "1" ]; then would "write $unit (BASE_HOST=$BASE_HOST)"; return 0; fi
  if ! { [ -f "$unit" ] && [ "$(cat "$unit")" = "$text" ]; }; then printf '%s\n' "$text" > "$unit"; changed=1; fi
  if [ "$BASE_HOST" = runpod ] || [ -n "$BASE_FAKE_ROOT" ] || ! command -v systemctl >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
    [ "$changed" = 1 ] && ok "boot unit written: $unit (installed by the host's startup script or by hand)" || ok "boot unit current: $unit"
    return 0
  fi
  if [ "$(id -u 2>/dev/null)" != "0" ]; then warn "boot unit: not root, so $sys was not installed (sudo cp $unit $sys && sudo systemctl enable comfy-base-boot)"; return 0; fi
  if ! { [ -f "$sys" ] && cmp -s "$unit" "$sys"; }; then
    cp "$unit" "$sys" && systemctl daemon-reload && systemctl enable comfy-base-boot >/dev/null 2>&1 && ok "boot unit installed and enabled: $sys" && BASE_CHANGED+=("boot: unit $sys")
  else ok "boot unit installed: $sys"; fi
  return 0
}
base_boot_install(){ # boot.sh + lib/85-launch.sh on the volume (current copies), the boot unit on a VM host, the tools venv, state/boot.env
  hdr "BOOT · the base boots this pod (sshd → JupyterLab → ComfyUI, never exits)"
  local src="$BASE_DIR/lib/boot.sh" dst="$BASE_HOME/boot.sh" lsrc="$BASE_DIR/lib/85-launch.sh" ldst="$BASE_HOME/lib/85-launch.sh" changed=0
  if [ "$BASE_DRY" = "1" ]; then
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then ok "boot.sh current at $dst"; else would "install $dst (PID 1 once podctl points the pod's start command at it)"; fi
    base_boot_tools; would "write $BASE_STATE/boot.env (COMFY, VENV, PORT, ARGS_FILE)"
    BASE_BOOT_RESULT="would install · tools: $BASE_TOOLS_RESULT · auth: $(_base_jupyter_auth)"; return 0
  fi
  mkdir -p "$BASE_HOME/lib"
  if ! { [ -f "$dst" ] && cmp -s "$src" "$dst"; }; then cp "$src" "$dst" && chmod 755 "$dst" && changed=1; fi
  if ! { [ -f "$ldst" ] && cmp -s "$lsrc" "$ldst"; }; then cp "$lsrc" "$ldst" && changed=1; fi
  # VERSION rides beside boot.sh too (on a pod BASE_DIR is BASE_HOME and this is a no-op; in the suite the fake pod's
  # comfy-base/ gets exactly what the boot needs and nothing else)
  if [ -f "$BASE_DIR/VERSION" ] && ! { [ -f "$BASE_HOME/VERSION" ] && cmp -s "$BASE_DIR/VERSION" "$BASE_HOME/VERSION"; }; then cp "$BASE_DIR/VERSION" "$BASE_HOME/VERSION" && changed=1; fi
  if [ "$changed" = "1" ]; then ok "installed $dst (+ lib/85-launch.sh)"; BASE_CHANGED+=("boot: installed $dst"); else ok "boot.sh current at $dst"; fi
  base_boot_unit
  base_boot_tools
  base_boot_env_write
  BASE_BOOT_RESULT="$dst · tools: $BASE_TOOLS_RESULT · JupyterLab auth: $(_base_jupyter_auth)"
  return 0
}
