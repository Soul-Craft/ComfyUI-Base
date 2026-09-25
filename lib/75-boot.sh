# 75-boot.sh: the base owns the machine's boot: boot.sh + the launch lib on the volume, the tools venv (JupyterLab, newest),
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
base_boot_tools(){ # JupyterLab in the base's tools venv, on the newest Python, at its newest (3.0.0: every run, never "present")
  # $BASE_HOME/tools (the path boot.sh starts JupyterLab from) is a SYMLINK to tools.<python minor>.<ts>. A new Python
  # minor builds a new venv beside the old and swaps the link in one rename, so a JupyterLab another machine on the
  # store is running keeps its files; otherwise JupyterLab is upgraded in place. The newest two are kept.
  local link="$BASE_HOME/tools" mm cur_mm target ts stamp
  if [ "$BASE_DRY" = "1" ]; then would "JupyterLab at its newest in the tools venv $link, on the newest Python uv provides"; BASE_TOOLS_RESULT="would-upgrade"; return 0; fi
  if [ "$BASE_NO_NET" = "1" ]; then
    stamp="$link/.comfy-base-tools"
    if [ -x "$link/bin/jupyter-lab" ] && [ -f "$stamp" ]; then ok "tools venv present (fake)"; BASE_TOOLS_RESULT="present (fake)"; return 0; fi
    mkdir -p "$link/bin"; printf '#!/bin/bash\necho "fake jupyter-lab $*"\n' > "$link/bin/jupyter-lab"; chmod +x "$link/bin/jupyter-lab"
    echo "fake ts=$(_base_ts)" > "$stamp"; ok "fake tools venv (BASE_NO_NET)"; BASE_TOOLS_RESULT="built (fake)"; BASE_CHANGED+=("tools venv built (fake)"); return 0
  fi
  command -v uv >/dev/null 2>&1 || _base_ensure_uv || { warn "tools venv: no uv: the boot starts without JupyterLab"; BASE_TOOLS_RESULT="failed (no uv)"; return 0; }
  mm="$(_base_newest_python_minor)"
  [ -n "$mm" ] || { warn "tools venv: uv lists no Python: JupyterLab not upgraded"; BASE_TOOLS_RESULT="failed (no python)"; return 0; }
  cur_mm="$("$link/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
  if [ -L "$link" ] && [ "$cur_mm" = "$mm" ] && [ -x "$link/bin/jupyter-lab" ]; then
    if uv pip install -q --python "$link/bin/python" --upgrade jupyterlab 2>&1 | tail -2; then
      echo "python=$mm jupyterlab=$("$link/bin/python" -c 'import importlib.metadata as m; print(m.version("jupyterlab"))' 2>/dev/null) ts=$(_base_ts)" > "$link/.comfy-base-tools"
      ok "tools venv: $(cat "$link/.comfy-base-tools")"; BASE_TOOLS_RESULT="newest"
    else warn "tools venv: JupyterLab could not be upgraded"; BASE_TOOLS_RESULT="failed (upgrade)"; fi
    return 0
  fi
  ts="$(_base_ts)"; target="$BASE_HOME/tools.$mm.$ts"
  uv python install "$mm" >/dev/null 2>&1 || true
  if ! uv venv -q --python "$mm" --seed "$target" 2>&1 | tail -2 || ! uv pip install -q --python "$target/bin/python" jupyterlab 2>&1 | tail -2 || [ ! -x "$target/bin/jupyter-lab" ]; then
    rm -rf "$target"; warn "tools venv: could not build JupyterLab on Python $mm: the previous one stays"; BASE_TOOLS_RESULT="failed (build)"; return 0
  fi
  echo "python=$mm jupyterlab=$("$target/bin/python" -c 'import importlib.metadata as m; print(m.version("jupyterlab"))' 2>/dev/null) ts=$ts" > "$target/.comfy-base-tools"
  if [ -d "$link" ] && [ ! -L "$link" ]; then mv "$link" "$BASE_HOME/tools.pre-$ts" || true; fi    # the 2.x directory, kept for a running JupyterLab
  # one rename replaces the link (a plain `mv` onto a symlink to a directory would move INTO the directory)
  ln -sfn "$(basename "$target")" "$link.new" && "${SYS_PY:-python3}" -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' "$link.new" "$link"
  ok "tools venv: $(cat "$link/.comfy-base-tools") at $target"; BASE_CHANGED+=("tools venv: JupyterLab on Python $mm"); BASE_TOOLS_RESULT="built (Python $mm)"
  # keep the newest two (the one the link names, and the one before it)
  ls -dt "$BASE_HOME"/tools.[0-9]* "$BASE_HOME"/tools.pre-* 2>/dev/null | sed -n '3,$p' | while IFS= read -r old; do rm -rf "$old"; done
  return 0
}
_base_systemd_live(){ # systemd is this machine's init and the base may drive it: never RunPod (a container) nor a fake root
  [ "$BASE_HOST" != runpod ] && [ -z "$BASE_FAKE_ROOT" ] && command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]
}
_base_boot_unit_enabled(){ # 3.0.3: comfy-base-boot is installed and enabled here, so the unit is what owns ComfyUI
  _base_systemd_live && systemctl is-enabled --quiet comfy-base-boot.service 2>/dev/null
}
_base_in_boot_unit(){ # 3.0.3: this shell runs INSIDE the unit (a JupyterLab terminal, a boot extension), so stopping the unit stops it
  grep -q 'comfy-base-boot\.service' "/proc/$$/cgroup" 2>/dev/null
}
_base_boot_unit_owns(){ # 3.0.3: a restart must go through the unit: it owns ComfyUI here and stopping it does not stop this run
  _base_boot_unit_enabled && ! _base_in_boot_unit
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
  if ! _base_systemd_live; then
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
