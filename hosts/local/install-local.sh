#!/usr/bin/env bash
# ComfyUI Base on an owned NVIDIA box: the one-command install.
#
#   bash hosts/local/install-local.sh [<volume dir>] [--unit] [installer args...]
#
# <volume dir> (default $HOME/comfy) becomes the base's BASE_VOLUME: a plain directory, no mount. The script creates it,
# writes <dir>/comfy-base/state/host.env, exports the four knobs, runs the base's own installer (comfyui-base-script.sh,
# two levels up from this folder, with the remaining arguments: --check | --latest | test | rescue | help, as on every
# host), then with --unit installs the unit the installer wrote beside boot.sh (the same comfy-base-boot.service as the
# VM hosts, its RequiresMountsFor line omitted for a directory) with a drop-in that runs it as you; needs sudo.
# Needs: Ubuntu 22.04/24.04, an NVIDIA driver 580 or newer (checked, never installed here), uv on PATH.
# Never `set -e`: the installer reports its own failures.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
INSTALLER="$HERE/../../comfyui-base-script.sh"
UNIT_PATH="/etc/systemd/system/comfy-base-boot.service"

VOLUME=""; WANT_UNIT=0; PASS=()
for a in "$@"; do
  case "$a" in
    --unit) WANT_UNIT=1 ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
    --*) PASS+=("$a") ;;
    *) if [ -z "$VOLUME" ]; then VOLUME="$a"; else PASS+=("$a"); fi ;;
  esac
done
VOLUME="${VOLUME:-$HOME/comfy}"
case "$VOLUME" in /*) ;; *) VOLUME="$PWD/$VOLUME" ;; esac

say(){ printf 'install-local: %s\n' "$*"; }
fail(){ printf 'install-local: %s\n' "$*" >&2; exit 1; }

[ -f "$INSTALLER" ] || fail "no installer at $INSTALLER (this script lives in hosts/local/ of the base)"
command -v uv >/dev/null 2>&1 || fail "uv is not on PATH: curl -LsSf https://astral.sh/uv/install.sh | sh, then open a new shell"
if command -v nvidia-smi >/dev/null 2>&1; then
  maj="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | cut -d. -f1)"
  if [ -n "$maj" ] && [ "$maj" -lt 580 ] 2>/dev/null; then say "NVIDIA driver $maj is below 580; the base will refuse it (install a newer driver first, this script never does)"; fi
else
  say "nvidia-smi not found; the base will refuse to install without a driver (this script never installs one)"
fi

mkdir -p "$VOLUME/comfy-base/state" || fail "could not create $VOLUME/comfy-base/state"
export BASE_HOST=local BASE_VOLUME="$VOLUME" BASE_VOLUME_KIND=dir BASE_LISTEN=127.0.0.1
printf 'BASE_HOST=local\nBASE_VOLUME=%s\nBASE_VOLUME_KIND=dir\nBASE_LISTEN=127.0.0.1\n' "$VOLUME" > "$VOLUME/comfy-base/state/host.env" \
  || fail "could not write $VOLUME/comfy-base/state/host.env"
say "volume $VOLUME (a directory); host.env written; BASE_HOST=local BASE_VOLUME_KIND=dir BASE_LISTEN=127.0.0.1"

install_unit(){ # the unit the base wrote beside boot.sh (RequiresMountsFor omitted for a directory), plus a drop-in that runs it as you
  local src="$VOLUME/comfy-base/comfy-base-boot.service" dropin="$UNIT_PATH.d/user.conf" user; user="$(id -un)"
  [ -s "$src" ] || { say "no $src (the installer writes it); the unit was not installed"; return 0; }
  local sudo=""; [ "$(id -u)" = 0 ] || sudo="sudo"
  if [ -f "$UNIT_PATH" ] && cmp -s "$src" "$UNIT_PATH"; then say "unit $UNIT_PATH is current"
  else $sudo cp "$src" "$UNIT_PATH" || { say "could not write $UNIT_PATH; continuing without the unit"; return 0; }; say "unit $UNIT_PATH installed from $src"; fi
  $sudo mkdir -p "$UNIT_PATH.d"
  printf '[Service]\nUser=%s\nEnvironment=HOME=%s\n' "$user" "$HOME" | $sudo tee "$dropin" >/dev/null && say "drop-in $dropin: runs as $user"
  $sudo systemctl daemon-reload
  $sudo systemctl enable comfy-base-boot.service >/dev/null 2>&1 || say "systemctl enable failed; enable it by hand"
  if $sudo systemctl restart comfy-base-boot.service; then say "comfy-base-boot.service running; logs: journalctl -u comfy-base-boot -f"
  else say "comfy-base-boot.service did not start; journalctl -u comfy-base-boot"; fi
  return 0
}

say "running the base's installer: bash $INSTALLER ${PASS[*]}"
bash "$INSTALLER" "${PASS[@]}"
rc=$?
if [ "$rc" != 0 ]; then say "the installer ended with rc=$rc; read its output above, fix, and run this script again (it resumes)"; exit "$rc"; fi
if [ "$WANT_UNIT" = 1 ]; then       # after the installer: it is what writes $VOLUME/comfy-base/comfy-base-boot.service
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then install_unit
  else say "--unit asked, but this box has no systemd; start the stack by hand: BOOT_HOME=$VOLUME/comfy-base bash $VOLUME/comfy-base/boot.sh"; fi
else
  say "start the stack by hand: BOOT_HOME=$VOLUME/comfy-base bash $VOLUME/comfy-base/boot.sh   (or re-run with --unit)"
fi
say "done. ComfyUI: http://127.0.0.1:8188 on this box, or ssh -L 8188:127.0.0.1:8188 <you>@<box> from another"
exit 0
