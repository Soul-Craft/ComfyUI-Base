#!/bin/bash
# ComfyUI Base on Crusoe Cloud: the VM's startup script.
# It prepares the VM (the data disk, the driver gate, the CUDA toolchain) and hands over to the base's boot.sh when one
# is installed. The base and the packages are installed through `podctl install` over ssh, like on every other host.
#
# Give this file to the VM as its Crusoe startup script (console: "Startup script"; CLI: --startup-script). Crusoe
# runs it as root on EVERY boot, after the network is up, and it cannot be changed through the API afterwards. It is
# idempotent, and it can be run again by hand:
#
#   sudo bash startup.sh --ensure      the same stages, logging to the terminal as well as the log file
#
# Stages, each skipped once it is done:
#   1. the persistent disk at /workspace (wait for it, mkfs once, mount by UUID from fstab)
#   2. the NVIDIA driver gate (driver 580 or newer; installs it from NVIDIA's repository and reboots ONCE when older)
#   3. the CUDA 13 toolkit and build-essential (SageAttention builds from source and needs nvcc that links cuBLAS)
#   4. the base's systemd unit, comfy-base-boot.service, which runs /workspace/comfy-base/boot.sh at every boot
#      (the base's own unit text once installed; a matching bootstrap copy before that)
#   5. state/host.env: BASE_HOST=crusoe, BASE_VOLUME=/workspace
#
# Reach the app from your own computer (the Crusoe firewall leaves 8188 and 8888 closed to the internet, on purpose):
#   ssh -N -L 8188:localhost:8188 ubuntu@<the VM's public IP>     then open http://localhost:8188
#   or, from the Mac: uv run "_build/pod/podctl.py" --provider crusoe tunnel <vm>
#
# Progress:  tail -f /var/log/comfy-base-startup.log    (also /workspace/comfy-base/state/crusoe-startup.status)
#
# Rules of this file: plain ASCII (Crusoe's limit), under 64 KB, never `set -e` (a startup script that dies leaves
# the VM up with nothing running and no explanation). Every step logs what it did and what to do if it stopped.
# ---------------------------------------------------------------------------------------------------------------

# ---- settings (edit here; a startup script takes no arguments) ---------------------------------------------------
WORKSPACE="/workspace"                                          # the base's BASE_VOLUME; the persistent disk mounts here
DRIVER_MIN="${DRIVER_MIN:-580}"                                 # the base's own gate: CUDA 13 torch wheels need this
DRIVER_AUTOINSTALL="${DRIVER_AUTOINSTALL:-1}"                   # 1: install the 580 driver from NVIDIA's repo when older, then reboot once
DISK_DEV="${DISK_DEV:-}"                                        # empty: auto-detect the one unmounted vd[b-z] disk
SSH_USER="${SSH_USER:-ubuntu}"                                  # the login user the driver uses; owns /workspace so installs need no sudo
UNIT="/etc/systemd/system/comfy-base-boot.service"
SENTINEL_DIR="/var/lib/comfy-base"                              # on the OS disk: a new VM on the same data disk gets its own driver
LOG="/var/log/comfy-base-startup.log"

export DEBIAN_FRONTEND=noninteractive
MODE="${1:-}"
if [ "$MODE" = "--ensure" ]; then
  exec > >(tee -a "$LOG") 2>&1
else
  exec >>"$LOG" 2>&1
fi
log(){ printf '%s startup: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }   # stderr: safe inside $(...) captures, and stderr is the log too
status(){ mkdir -p "$WORKSPACE/comfy-base/state" 2>/dev/null && printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" > "$WORKSPACE/comfy-base/state/crusoe-startup.status" 2>/dev/null; log "$*"; }
log "=== boot $(uname -r) on $(hostname), $(nproc) vCPU, $(free -g | awk '/^Mem/{print $2}') GB RAM (mode: ${MODE:-boot}) ==="

# ---- 1. the persistent disk at /workspace --------------------------------------------------------------------------
mount_workspace(){
  if mountpoint -q "$WORKSPACE"; then log "$WORKSPACE is mounted ($(findmnt -no SOURCE "$WORKSPACE"))"; return 0; fi
  mkdir -p "$WORKSPACE"
  if grep -q " $WORKSPACE " /etc/fstab; then
    mount "$WORKSPACE" && { log "$WORKSPACE mounted from fstab"; return 0; }
    log "fstab has $WORKSPACE but the mount failed; looking for the disk again"
  fi
  local dev="$DISK_DEV" cands waited=0
  if [ -z "$dev" ]; then
    # Crusoe persistent disks are virtio: /dev/vd[b-z]; the OS disk holds / (vda). Take the one candidate, refuse two.
    # The disk is often attached seconds AFTER the VM is created (the recipe attaches it as a second step), so wait for it.
    while :; do
      cands="$(lsblk -dn -o NAME,TYPE,MOUNTPOINT | awk '$2=="disk" && $1 ~ /^vd[b-z]$/ && $3=="" {print "/dev/"$1}')"
      cands="$(for d in $cands; do lsblk -n -o MOUNTPOINT "$d" | grep -q . || echo "$d"; done)"
      [ -n "$cands" ] && break
      if [ "$waited" -ge 900 ]; then
        status "STOPPED: no persistent disk was attached within 15 minutes. Create one (crusoe storage disks create --size 400GiB ...), attach it to this VM, then reboot."; return 1
      fi
      [ "$waited" = 0 ] && status "waiting for a persistent disk to be attached (up to 15 minutes)"
      sleep 15; waited=$((waited + 15))
    done
    set -- $cands
    if [ "$#" -gt 1 ]; then
      status "STOPPED: $# unmounted disks ($*); set DISK_DEV=/dev/vdX at the top of the startup script and reboot."; return 1
    fi
    dev="$1"
  fi
  if ! blkid "$dev" >/dev/null 2>&1; then
    log "formatting $dev as ext4 (a new, empty persistent disk)"
    mkfs.ext4 -q -L comfy-workspace "$dev" || { status "STOPPED: mkfs.ext4 $dev failed"; return 1; }
  else
    log "$dev already carries a filesystem ($(blkid -o value -s TYPE "$dev")); keeping it"
  fi
  local uuid; uuid="$(blkid -o value -s UUID "$dev")"
  [ -n "$uuid" ] || { status "STOPPED: no UUID on $dev"; return 1; }
  cp -n /etc/fstab /etc/fstab.comfy-base-backup 2>/dev/null
  grep -q "UUID=$uuid " /etc/fstab || echo "UUID=$uuid $WORKSPACE ext4 defaults,nofail,x-systemd.device-timeout=30 0 2" >> /etc/fstab
  mount "$WORKSPACE" || { status "STOPPED: mount $WORKSPACE failed (see $LOG)"; return 1; }
  log "$WORKSPACE mounted from $dev (UUID $uuid), $(df -h --output=size "$WORKSPACE" | tail -1 | tr -d ' ')"
}

own_workspace(){ # the driver logs in as $SSH_USER and installs into /workspace without sudo, so that user owns the root
  if id "$SSH_USER" >/dev/null 2>&1; then
    [ "$(stat -c %U "$WORKSPACE")" = "$SSH_USER" ] || { chown "$SSH_USER:$SSH_USER" "$WORKSPACE" && log "$WORKSPACE owned by $SSH_USER"; }
  else
    log "no user $SSH_USER on this VM; $WORKSPACE stays root's (set SSH_USER at the top of the startup script)"
  fi
  return 0
}

# ---- apt helpers: wait for the boot-time unattended-upgrades lock, add NVIDIA's CUDA repository once ------------------
apt_i(){ apt-get -o DPkg::Lock::Timeout=600 install -y -qq "$@"; }
nvidia_repo(){
  [ -f /usr/share/keyrings/cuda-archive-keyring.gpg ] && return 0
  local kr="/tmp/cuda-keyring_1.1-1_all.deb"
  . /etc/os-release
  local dist="ubuntu${VERSION_ID//./}"
  curl -fsSL -o "$kr" "https://developer.download.nvidia.com/compute/cuda/repos/$dist/x86_64/cuda-keyring_1.1-1_all.deb" \
    && dpkg -i "$kr" >/dev/null && apt-get -o DPkg::Lock::Timeout=600 update -qq
}

# ---- 2. the NVIDIA driver gate (the base refuses a driver below DRIVER_MIN before downloading anything) ------------
driver_major(){ nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | cut -d. -f1; }
driver_gate(){
  local maj sentinel="$SENTINEL_DIR/driver-installed"
  maj="$(driver_major)"
  if [ -n "$maj" ] && [ "$maj" -ge "$DRIVER_MIN" ] 2>/dev/null; then
    log "NVIDIA driver $(nvidia-smi --query-gpu=driver_version,name,memory.total --format=csv,noheader | head -1) (>= $DRIVER_MIN)"; return 0
  fi
  log "NVIDIA driver is '${maj:-absent}', below $DRIVER_MIN"
  if [ "$DRIVER_AUTOINSTALL" != "1" ]; then
    status "STOPPED: driver ${maj:-absent} < $DRIVER_MIN and DRIVER_AUTOINSTALL=0. Install driver $DRIVER_MIN and reboot."; return 1
  fi
  if [ -f "$sentinel" ]; then
    status "STOPPED: driver $DRIVER_MIN was installed on $(cat "$sentinel") and the driver is still '${maj:-absent}'. Not rebooting again; check $LOG and 'apt list --installed | grep nvidia'."; return 1
  fi
  status "installing NVIDIA driver $DRIVER_MIN from NVIDIA's CUDA repository (one reboot follows)"
  nvidia_repo || { status "STOPPED: could not add NVIDIA's apt repository (see $LOG)"; return 1; }
  # the branch package names NVIDIA publishes for Ubuntu; the first that exists wins
  local pkg ok=0
  for pkg in "nvidia-driver-$DRIVER_MIN-open" "nvidia-open-$DRIVER_MIN" "nvidia-driver-$DRIVER_MIN" "cuda-drivers-$DRIVER_MIN"; do
    if apt-cache show "$pkg" >/dev/null 2>&1; then
      log "apt-get install $pkg"
      if apt_i "$pkg"; then ok=1; break; fi
    fi
  done
  [ "$ok" = 1 ] || { status "STOPPED: no installable driver $DRIVER_MIN package found (see $LOG)"; return 1; }
  mkdir -p "$SENTINEL_DIR"; date -u +%FT%TZ > "$sentinel"
  if [ "$MODE" = "--ensure" ]; then
    status "driver $DRIVER_MIN installed; reboot once (sudo reboot), then run this script again with --ensure"; return 1
  fi
  status "driver $DRIVER_MIN installed; rebooting once. Reconnect in about two minutes and tail $LOG"
  sleep 3; reboot
  exit 0
}

# ---- 3. the CUDA toolchain: a package may build SageAttention from source for this GPU, which needs nvcc that links
#         cuBLAS, plus a C++ compiler. Crusoe's image documents a driver and a docker runtime, not a toolkit. -------------
toolchain_gate(){
  export PATH="/usr/local/cuda/bin:$PATH"
  if command -v nvcc >/dev/null 2>&1 && command -v g++ >/dev/null 2>&1; then log "CUDA toolchain: $(nvcc --version | grep -o 'release [0-9.]*'), $(g++ --version | head -1)"; return 0; fi
  status "installing build-essential and the CUDA 13 toolkit (nvcc, cuBLAS headers); about 3 GB, 5 to 10 minutes"
  nvidia_repo || { status "STOPPED: could not add NVIDIA's apt repository (see $LOG)"; return 1; }
  apt_i build-essential || { status "STOPPED: apt could not install build-essential (see $LOG)"; return 1; }
  local pkg ok=0
  for pkg in cuda-toolkit-13-0 cuda-toolkit-13 cuda-toolkit; do
    if apt-cache show "$pkg" >/dev/null 2>&1; then log "apt-get install $pkg"; if apt_i "$pkg"; then ok=1; break; fi; fi
  done
  [ "$ok" = 1 ] && command -v nvcc >/dev/null 2>&1 || { status "STOPPED: no CUDA toolkit package installed (see $LOG); SageAttention cannot build without nvcc"; return 1; }
  log "CUDA toolchain installed: $(nvcc --version | grep -o 'release [0-9.]*')"
}

# ---- 4. the base's systemd unit: /workspace/comfy-base/boot.sh at every boot ---------------------------------------
# The base writes its own unit text beside boot.sh during `podctl install --base` ($WORKSPACE/comfy-base/comfy-base-boot.service,
# lib/75-boot.sh _base_boot_unit_text) and installs it only when it runs as root, which the driver's ssh user is not. So this
# script installs it: the base's file when it exists, else the same text as a bootstrap. Its ExecStart sleeps until boot.sh
# exists, so the unit is safe to enable before the install; after the first install it is restarted so boot.sh replaces the sleep.
unit_text(){
  cat <<EOF_UNIT
[Unit]
Description=ComfyUI Base boot (sshd, JupyterLab, ComfyUI) from $WORKSPACE/comfy-base
After=network-online.target local-fs.target
Wants=network-online.target
RequiresMountsFor=$WORKSPACE

[Service]
Type=simple
Environment=BOOT_HOME=$WORKSPACE/comfy-base
Environment=BASE_HOST=crusoe
Environment=BASE_LISTEN=127.0.0.1
ExecStart=/bin/bash -c 'test -x $WORKSPACE/comfy-base/boot.sh && exec bash $WORKSPACE/comfy-base/boot.sh || exec sleep infinity'
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF_UNIT
}
install_unit(){
  local want src="$WORKSPACE/comfy-base/comfy-base-boot.service"
  if [ -s "$src" ]; then want="$(cat "$src")"; log "unit text: the base's own ($src)"; else want="$(unit_text)"; log "unit text: this script's bootstrap copy (the base is not installed yet)"; fi
  if [ -f "$UNIT" ] && [ "$(cat "$UNIT")" = "$want" ]; then
    log "unit $UNIT is current"
  else
    printf '%s\n' "$want" > "$UNIT" || { status "STOPPED: could not write $UNIT"; return 1; }
    systemctl daemon-reload
    log "unit $UNIT written"
  fi
  systemctl enable comfy-base-boot.service >/dev/null 2>&1 || log "systemctl enable failed (see $LOG)"
  if ! systemctl is-active --quiet comfy-base-boot.service; then
    systemctl start comfy-base-boot.service && log "comfy-base-boot.service started" || log "comfy-base-boot.service did not start; journalctl -u comfy-base-boot"
  elif [ -x "$WORKSPACE/comfy-base/boot.sh" ] && ! pgrep -f "comfy-base/boot.sh" >/dev/null 2>&1; then
    systemctl restart comfy-base-boot.service && log "comfy-base-boot.service restarted: boot.sh replaces the pre-install sleep"
  else
    log "comfy-base-boot.service is running"
  fi
  return 0
}

# ---- 5. host.env: what the base's core reads to know which host it is on -------------------------------------------
write_host_env(){
  local f="$WORKSPACE/comfy-base/state/host.env"
  mkdir -p "$WORKSPACE/comfy-base/state" || { status "STOPPED: could not create $WORKSPACE/comfy-base/state"; return 1; }
  printf 'BASE_HOST=crusoe\nBASE_VOLUME=%s\n' "$WORKSPACE" > "$f.tmp" && mv "$f.tmp" "$f" || { status "STOPPED: could not write $f"; return 1; }
  if id "$SSH_USER" >/dev/null 2>&1; then chown -R "$SSH_USER:$SSH_USER" "$WORKSPACE/comfy-base/state" 2>/dev/null; fi
  log "host.env: BASE_HOST=crusoe BASE_VOLUME=$WORKSPACE"
}

# ---- run ------------------------------------------------------------------------------------------------------------
mount_workspace || exit 0
own_workspace
mkdir -p "$WORKSPACE/comfy-base/state/logs"
driver_gate || exit 0
toolchain_gate || exit 0
install_unit || exit 0
write_host_env || exit 0
if [ -f "$WORKSPACE/comfy-base/state/boot.env" ]; then
  status "READY: the base is installed; comfy-base-boot.service runs it. From your computer: ssh -N -L 8188:localhost:8188 $SSH_USER@<public ip>, then http://localhost:8188"
else
  status "READY for the install: disk, driver, toolchain and unit are in place. From the Mac: uv run _build/pod/podctl.py --provider crusoe install <vm> --base --pkg <package dir>"
fi
exit 0
