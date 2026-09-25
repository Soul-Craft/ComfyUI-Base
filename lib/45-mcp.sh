# 45-mcp.sh — Comfy MCP: the agent-facing control plane for this machine's ComfyUI.
#
# comfy-mcp is a stdio MCP server wrapping comfy-cli. It is installed INTO the run's venv, beside ComfyUI
# itself, because the tools worth having are the ones that only work where the tree is: install_node,
# search_models, get_logs, fetch_outputs, launch_comfyui. A client on a Mac reaches them by running this
# binary over ssh, which `podctl mcp <machine>` writes the configuration for. A client that only has the
# tunnel can still run workflows — COMFYUI_URL is enough for that — but it cannot see the machine.
#
# TELEMETRY. comfy-cli depends on mixpanel and posthog, and design rule 9 of the handbook says the outbound
# hosts are an allowlist and nothing is uploaded. Three things are done about it, in order of how much they
# can be relied on:
#   1. DO_NOT_TRACK and COMFY_NO_TELEMETRY are exported before the wheel can run anything. comfy-cli's own
#      tracking module honours the DO_NOT_TRACK convention by never importing the telemetry extension at all,
#      so this is not a request to be quiet, it is the code path never loading.
#   2. the same two go into the boot environment (lib/boot.sh), because a machine that reboots must come back
#      as quiet as it was installed.
#   3. `comfy tracking disable` writes the config file too (~/.config/comfy-cli/config.ini on Linux), for any
#      invocation that somehow arrives without the environment.
# suite/test_unit.py asserts 1 and 2 are present, so this cannot regress quietly.

base_mcp(){ # comfy-cli + comfy-mcp into the run's venv, bound to the discovered tree. Never fails an install.
  hdr "COMFY MCP"
  if [ "${BASE_MCP:-1}" = "0" ]; then note "skipped — BASE_MCP=0"; BASE_MCP_RESULT="skipped (BASE_MCP=0)"; return 0; fi
  if [ "$BASE_DRY" = "1" ] || [ "$BASE_NO_NET" = "1" ]; then note "skipped"; BASE_MCP_RESULT="skipped"; return 0; fi
  if [ -z "${PY:-}" ] || [ ! -x "${PY:-/nonexistent}" ]; then note "skipped — no venv interpreter"; BASE_MCP_RESULT="skipped (no venv)"; return 0; fi

  export DO_NOT_TRACK=1 COMFY_NO_TELEMETRY=1      # before anything from the wheel runs; see the header

  local c="${CONSTRAINTS:-${BASE_STATE:-}/constraints-torch.txt}" bin ver
  [ -f "$c" ] || c=""
  # comfy-cli pulls 29 dependencies and none of them is torch, torchvision, torchaudio or numpy, so the pinned
  # trio is not at risk; the constraints file is passed anyway, because "not at risk today" is not a guarantee.
  # uv rather than `$PY -m pip`: 30-venv.sh seeds the venv so pip is normally there, but uv is what actually
  # built it and it does not care either way — a venv made without --seed has no pip at all.
  # 3.0.0: present here; their NEWEST arrives in the one resolve after this stage (lib/47-latest.sh), never in a separate
  # --upgrade, which resolves only what it names and can overshoot a cap another package declares (2.12.3's lesson)
  # 3.3.0: the newest set (lib/47-latest.sh) already installs both; a second install is a second resolve for nothing
  if "$PY" -c 'import importlib.metadata as m; m.version("comfy-mcp"); m.version("comfy-cli")' >/dev/null 2>&1; then
    note "comfy-cli and comfy-mcp are in the venv (the newest set installed them)"
  elif ! _base_uvpip -q ${c:+-c "$c"} "comfy-cli>=1.14.0" comfy-mcp 2>&1 | tail -3; then
    miss "comfy-cli / comfy-mcp install failed"; BASE_MCP_RESULT="FAILED (install)"
    BASE_WARN+=("comfy mcp not installed: the machine runs, an agent cannot drive it")
    return 0
  fi

  bin="$(dirname "$PY")/comfy-mcp"
  if [ ! -x "$bin" ]; then
    miss "comfy-mcp is not in the venv after the install ($bin)"; BASE_MCP_RESULT="FAILED (no binary)"
    BASE_WARN+=("comfy mcp not installed: no comfy-mcp binary")
    return 0
  fi
  ver="$("$PY" -c 'import importlib.metadata as m; print(m.version("comfy-mcp"))' 2>/dev/null || echo "?")"

  # bind comfy-cli to the tree this run discovered, so its tools address that one and never a default guess
  if [ -n "${COMFY:-}" ] && [ -d "${COMFY:-/nonexistent}" ]; then
    "$(dirname "$PY")/comfy" --skip-prompt set-default "$COMFY" >/dev/null 2>&1 \
      && ok "workspace: $COMFY" || note "could not persist the default workspace — pass --workspace, or set COMFY_PROJECT"
  fi
  "$(dirname "$PY")/comfy" --skip-prompt tracking disable >/dev/null 2>&1 || true   # the config file; the environment already decided

  ok "comfy-mcp $ver in the venv — drive it with: podctl mcp <machine>"
  BASE_MCP_RESULT="ok (comfy-mcp $ver)"
  return 0
}
