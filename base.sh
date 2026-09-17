#!/usr/bin/env bash
# ComfyUI Base — the shared toolchain for every workflow package on this pod.
#   Sourced by a package script:  source /workspace/comfy-base/base.sh; base_main "$@"
#   Run directly:                 bash base.sh version | install-self | status | latest |
#                                              list-packs <pkg dir>... | gen-models <pkg dir> | stamp-models <pkg dir> [--check] |
#                                              test [tier] | rescue
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
BASE_VERSION="$(tr -d '[:space:]' < "$BASE_DIR/VERSION")"
# An INSTALLED copy (the volume, or the fake root in tests) must match its manifest: a partial or edited upload
# fails here, loudly, instead of running half a library. A repo checkout or an extracted zip is not checked.
case "$BASE_DIR" in
  "${BASE_FAKE_ROOT:-/nonexistent}/comfy-base"|"${BASE_VOLUME:-/workspace}/comfy-base")
    if [ -f "$BASE_DIR/MANIFEST.sha256" ]; then
      if command -v sha256sum >/dev/null 2>&1; then _base_mf_tool="sha256sum"; else _base_mf_tool="shasum -a 256"; fi
      if ! (cd "$BASE_DIR" && $_base_mf_tool -c MANIFEST.sha256 >/dev/null 2>&1); then
        echo "ERROR: ComfyUI Base files at $BASE_DIR do not match MANIFEST.sha256 — a partial or edited upload." >&2
        (cd "$BASE_DIR" && $_base_mf_tool -c MANIFEST.sha256 2>&1 | grep -v ': OK$' | head -8 | sed 's/^/  /') >&2 || true
        echo "  Re-extract 'comfyui-base.zip' and run 'comfyui-base-script.sh' again." >&2
        exit 5
      fi
      unset _base_mf_tool
    fi;;
esac
for _f in "$BASE_DIR"/lib/[0-9][0-9]-*.sh; do source "$_f"; done; unset _f
# A package declares the base it was written against. Older is refused before anything runs.
if [ -n "${BASE_MIN:-}" ] && ! _base_vge "$BASE_VERSION" "$BASE_MIN"; then
  echo "ERROR: ${PKG_NAME:-this package} needs ComfyUI Base >= $BASE_MIN; $BASE_DIR is $BASE_VERSION." >&2
  echo "  Upload the newer 'comfyui-base.zip', extract it, and run 'comfyui-base-script.sh' again." >&2
  exit 3
fi
if [ "${BASH_SOURCE[0]}" = "$0" ]; then base_cli "$@"; fi
