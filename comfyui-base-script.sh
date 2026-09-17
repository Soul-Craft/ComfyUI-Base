#!/usr/bin/env bash
# ComfyUI Base — STEP ONE on a fresh pod. Extract the zip anywhere and run this once:
#   python3 -m zipfile -e "comfyui-base.zip" . && bash "comfyui-base/comfyui-base-script.sh"
# It installs itself to /workspace/comfy-base (the volume), then brings the toolchain up: uv, the newest Python
# that resolves, torch cu130, ComfyUI at its newest release tag, the shared node packs, launch-arg hygiene, the
# exact launch line. Then run any workflow package. Same forms as every package: --check | --latest | test | rescue | help
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PKG_ID="base"; PKG_NAME="ComfyUI Base"; PKG_VERSION="$(tr -d '[:space:]' < "$HERE/VERSION")"; BASE_MIN="$PKG_VERSION"
WF_NAME=""; COMFY_MIN="0.34.0"
PACKS=(); MODELS=()
TOKENS=( "HF_TOKEN|optional|lifts anonymous rate limits; needed for gated repos" )
PKG_SCRIPT="$HERE/comfyui-base-script.sh"; PKG_DIR="$HERE"
source "$HERE/base.sh"
base_install_self "$@"
base_main "$@"
