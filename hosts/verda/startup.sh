#!/usr/bin/env bash
# hosts/verda/startup.sh: the Verda first-boot script for a ComfyUI Base machine, and its --ensure pass.
#
#   as a Verda startup script   runs ONCE, as root, on the first boot of a fresh image (registered by
#                               `podctl --provider verda ensure` under the name comfy-base-startup)
#   bash startup.sh --ensure    the same work, idempotently, on a machine that is already running (what
#                               `podctl ensure` does over ssh: an instance deployed without the script, or
#                               redeployed from an OS volume, ends up equal to one that had it)
#
# What it does, in order:
#   1. finds the one data disk (/dev/vd[b-z], unmounted, not the OS disk), waits up to 10 minutes for it,
#      refuses when there is more than one candidate, makes an ext4 filesystem labelled comfy-volume when
#      the disk is blank, adds an fstab line by UUID at /workspace (nofail, 30 s device timeout), mounts it;
#   2. writes and enables the comfy-base-boot systemd unit, which runs /workspace/comfy-base/boot.sh when
#      the base is installed there and sleeps otherwise (so the unit is always healthy, and a base install
#      followed by `systemctl restart comfy-base-boot` or a machine restart boots the base);
#   3. writes /workspace/comfy-base/state/host.env (BASE_HOST=verda, BASE_VOLUME=/workspace);
#   4. prints one READY line.
#
# Never `set -e`: a partial first boot must still reach the READY line and the log. Pure ASCII.
# Log: /var/log/comfy-base-startup.log (also stderr, which Verda keeps in the instance console).

LOG=/var/log/comfy-base-startup.log
MOUNT=/workspace
LABEL=comfy-volume
UNIT=comfy-base-boot
UNIT_FILE=/etc/systemd/system/$UNIT.service
BOOT_HOME=$MOUNT/comfy-base
DISK_WAIT=600
MODE=first-boot
[ "${1:-}" = "--ensure" ] && MODE=ensure

# stderr, not stdout: several functions below are called as $(...) and their stdout IS their answer
log() { printf '%s comfy-base-startup[%s]: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$MODE" "$*" | tee -a "$LOG" >&2; }

if [ "$(id -u)" != "0" ]; then
  echo "comfy-base-startup: must run as root" >&2
  exit 1
fi
touch "$LOG" 2>/dev/null

# ---------------------------------------------------------------- 1. the data disk

os_disk() {
  # the whole disk the root filesystem lives on (pkname of /'s source, or the source itself when it is a whole disk)
  local src
  src=$(findmnt -n -o SOURCE / 2>/dev/null | head -n1)
  [ -n "$src" ] || return 0
  local pk
  pk=$(lsblk -n -o PKNAME "$src" 2>/dev/null | head -n1)
  if [ -n "$pk" ]; then echo "/dev/$pk"; else echo "$src"; fi
}

disk_is_busy() {
  # anything on this disk (or one of its partitions) mounted or in use as swap
  local dev=$1
  lsblk -n -o MOUNTPOINT "$dev" 2>/dev/null | grep -q . && return 0
  lsblk -n -o TYPE "$dev" 2>/dev/null | grep -qx 'part' && return 0
  return 1
}

candidates() {
  # every /dev/vd[b-z] whole disk that is not the OS disk and is not in use
  local osd d
  osd=$(os_disk)
  for d in /dev/vd[b-z]; do
    [ -b "$d" ] || continue
    [ "$d" = "$osd" ] && continue
    disk_is_busy "$d" && continue
    echo "$d"
  done
}

find_data_disk() {
  # prints the device; an explicit COMFY_DATA_DISK wins; otherwise the single candidate, or the single
  # candidate already labelled comfy-volume when several disks are present; waits up to DISK_WAIT seconds
  local waited=0 list n labelled
  if [ -n "${COMFY_DATA_DISK:-}" ]; then
    if [ -b "$COMFY_DATA_DISK" ]; then echo "$COMFY_DATA_DISK"; return 0; fi
    log "COMFY_DATA_DISK=$COMFY_DATA_DISK is not a block device"
    return 1
  fi
  while :; do
    list=$(candidates)
    n=$(printf '%s\n' "$list" | grep -c .)
    if [ "$n" = "1" ]; then echo "$list"; return 0; fi
    if [ "$n" -gt 1 ]; then
      labelled=$(for d in $list; do [ "$(blkid -s LABEL -o value "$d" 2>/dev/null)" = "$LABEL" ] && echo "$d"; done)
      if [ "$(printf '%s\n' "$labelled" | grep -c .)" = "1" ]; then echo "$labelled"; return 0; fi
      log "refusing to choose: $n unmounted data disks ($(echo $list)) and not exactly one labelled $LABEL; set COMFY_DATA_DISK=/dev/vdX and run --ensure"
      return 1
    fi
    if [ "$waited" -ge "$DISK_WAIT" ]; then
      log "no data disk appeared within ${DISK_WAIT}s (no unmounted /dev/vd[b-z]): attach a volume in the console and run --ensure"
      return 1
    fi
    [ "$waited" = "0" ] && log "waiting for the data disk to appear (up to ${DISK_WAIT}s)"
    sleep 5
    waited=$((waited + 5))
  done
}

mount_data_disk() {
  local dev uuid fs line
  if findmnt -n "$MOUNT" >/dev/null 2>&1; then
    dev=$(findmnt -n -o SOURCE "$MOUNT")
    log "$MOUNT already mounted from $dev"
    DISK=$dev
    return 0
  fi
  dev=$(find_data_disk) || return 1
  DISK=$dev
  fs=$(blkid -s TYPE -o value "$dev" 2>/dev/null)
  if [ -z "$fs" ]; then
    log "$dev is blank: mkfs.ext4 -L $LABEL"
    if ! mkfs.ext4 -q -F -L "$LABEL" "$dev" >>"$LOG" 2>&1; then
      log "mkfs.ext4 failed on $dev (see $LOG)"
      return 1
    fi
  else
    log "$dev already carries $fs (kept)"
  fi
  uuid=$(blkid -s UUID -o value "$dev" 2>/dev/null)
  if [ -z "$uuid" ]; then
    log "no UUID on $dev after mkfs; not mounting"
    return 1
  fi
  line="UUID=$uuid $MOUNT ext4 defaults,nofail,x-systemd.device-timeout=30 0 2"
  if grep -qF "$line" /etc/fstab 2>/dev/null; then
    log "fstab line present"
  else
    # drop any older line for the mount point, then add ours
    if awk -v m="$MOUNT" '$1 !~ /^#/ && $2 == m { found = 1 } END { exit !found }' /etc/fstab 2>/dev/null; then
      cp /etc/fstab /etc/fstab.comfy-base.bak
      awk -v m="$MOUNT" '$1 !~ /^#/ && $2 == m { next } { print }' /etc/fstab.comfy-base.bak >/etc/fstab
      log "replaced the old fstab line for $MOUNT (backup /etc/fstab.comfy-base.bak)"
    fi
    printf '%s\n' "$line" >>/etc/fstab
    log "fstab: $line"
  fi
  mkdir -p "$MOUNT"
  systemctl daemon-reload 2>/dev/null
  if ! mount -a 2>>"$LOG"; then
    log "mount -a reported an error (see $LOG)"
  fi
  if findmnt -n "$MOUNT" >/dev/null 2>&1; then
    log "$MOUNT mounted from $dev"
    return 0
  fi
  log "$MOUNT is NOT mounted after mount -a"
  return 1
}

# ---------------------------------------------------------------- 2. the boot unit

write_unit() {
  local tmp
  tmp=$(mktemp)
  cat >"$tmp" <<UNIT_EOF
[Unit]
Description=ComfyUI Base boot (sshd, JupyterLab, ComfyUI) from $BOOT_HOME
After=network-online.target local-fs.target
Wants=network-online.target
RequiresMountsFor=/workspace

[Service]
Type=simple
Environment=BOOT_HOME=$BOOT_HOME
ExecStart=/bin/bash -c 'test -x $BOOT_HOME/boot.sh && exec bash $BOOT_HOME/boot.sh || exec sleep infinity'
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT_EOF
  if [ -f "$UNIT_FILE" ] && cmp -s "$tmp" "$UNIT_FILE"; then
    rm -f "$tmp"
    log "$UNIT_FILE unchanged"
  else
    install -m 0644 "$tmp" "$UNIT_FILE"
    rm -f "$tmp"
    log "wrote $UNIT_FILE"
  fi
  systemctl daemon-reload 2>>"$LOG"
  if ! systemctl enable --now "$UNIT" >>"$LOG" 2>&1; then
    log "systemctl enable --now $UNIT failed (see $LOG)"
    return 1
  fi
  UNIT_STATE=$(systemctl is-active "$UNIT" 2>/dev/null || true)
  log "$UNIT is $UNIT_STATE ($(systemctl is-enabled "$UNIT" 2>/dev/null || echo unknown))"
  return 0
}

# ---------------------------------------------------------------- 3. host.env

write_host_env() {
  local dir="$BOOT_HOME/state" want
  HOST_ENV=skipped
  if ! findmnt -n "$MOUNT" >/dev/null 2>&1; then
    log "host.env skipped: $MOUNT is not a mount point (never written to the OS disk)"
    return 1
  fi
  if ! mkdir -p "$dir" 2>>"$LOG"; then
    log "host.env skipped: cannot make $dir"
    return 1
  fi
  want=$(printf 'BASE_HOST=verda\nBASE_VOLUME=/workspace\n')
  if [ -f "$dir/host.env" ] && [ "$(cat "$dir/host.env")" = "$want" ]; then
    HOST_ENV=kept
  else
    printf '%s\n' "$want" >"$dir/host.env"
    HOST_ENV=written
  fi
  log "host.env $HOST_ENV ($dir/host.env)"
  return 0
}

# ---------------------------------------------------------------- run

DISK=none
UNIT_STATE=absent
HOST_ENV=skipped
RC=0

log "start ($(hostname), kernel $(uname -r))"
mount_data_disk || RC=1
write_unit || RC=1
write_host_env || RC=1

MOUNTED=no
findmnt -n "$MOUNT" >/dev/null 2>&1 && MOUNTED=yes
BOOT=absent
[ -x "$BOOT_HOME/boot.sh" ] && BOOT=present
if [ "$RC" = "0" ]; then
  log "READY disk=$DISK mounted=$MOUNTED unit=$UNIT_STATE boot.sh=$BOOT host.env=$HOST_ENV"
else
  log "READY-WITH-ERRORS disk=$DISK mounted=$MOUNTED unit=$UNIT_STATE boot.sh=$BOOT host.env=$HOST_ENV (read $LOG)"
fi
exit $RC
