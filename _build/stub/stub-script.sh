#!/usr/bin/env bash
# Stub 1.0.0 — the base's fixture package: three loaders, one LOCAL row, one bypassed loader, one legacy folder.
# It exists so the install tier can prove the base end to end against a throwaway tree. Not a real workflow.
set -Eeuo pipefail
PKG_NAME="Stub"; PKG_ID="stub"; PKG_VERSION="1.0.0"; BASE_MIN="1.0.0"
WF_NAME="stub-workflow.json"; COMFY_MIN="0.34.0"
TOKENS=( "HF_TOKEN|optional|lifts anonymous rate limits" )
PACKS=()                                   # no network in the install tier; the base packs are fake-cloned
# category|Family|Purpose|file|url|bytes|note|alts
MODELS=(
 "vae|Example|Real|stub_vae.safetensors|https://huggingface.co/x/y/resolve/main/stub_vae.safetensors|4096|the fixture VAE|"
 "loras|SDXL|Detailers|stub_local.safetensors|LOCAL|0|a file that only exists on the pod|"
)
SUPERSEDED=( stub_old.safetensors )
LEGACY_DIRS=( vae/stub_legacy )
PKG_NO_SUITE=1

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
CB="${COMFY_BASE:-/workspace/comfy-base}"
[ -f "$PKG_DIR/../../../base/testbed.sh" ] && [ -f "$PKG_DIR/../../../base/comfyui-base/base.sh" ] && CB="$PKG_DIR/../../../base/comfyui-base"
[ -f "$CB/base.sh" ] || { echo "ERROR: ComfyUI Base is not installed at $CB. Upload 'comfyui-base.zip', extract it, run 'comfyui-base-script.sh' first." >&2; exit 3; }
source "$CB/base.sh"
base_main "$@"
