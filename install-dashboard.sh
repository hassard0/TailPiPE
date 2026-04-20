#!/bin/bash
# Optional TailPiPE LCD dashboard installer.
#
# Safe to run on systems without the 3.5" LCD: the installed systemd unit has
# ConditionPathExists=/dev/fb1, so the dashboard is a no-op unless the LCD
# driver has brought up /dev/fb1. Re-run this script after physically
# attaching the LCD and adding the overlay; nothing else needs to change.
#
# Supported LCD: Waveshare-compatible 3.5" ILI9486 + XPT2046 SPI boards
# (including the GeekPi 3.5" clone). Overlay: waveshare35a by default.
#
# Flags:
#   --with-driver     also append 'dtoverlay=waveshare35a,rotate=90' to
#                     /boot/firmware/config.txt (or /boot/config.txt). A
#                     reboot is required for /dev/fb1 to appear.
#   --overlay NAME    override the overlay name (e.g. waveshare35b).
#   --rotate N        overlay rotation (0, 90, 180, 270). Default 90.
set -euo pipefail

OVERLAY="piscreen"
ROTATE="90"
# xohms=60 is the critical bit on most ILI9486/XPT2046 clones: the overlay
# default (~400) leaves pressure readings near zero, so the kernel driver
# filters every tap as invalid. 60 is in the 60-100 range the fbtft docs
# recommend for these panels.
OVERLAY_EXTRA="speed=24000000,fps=30,xohms=60"
WITH_DRIVER=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-driver) WITH_DRIVER=1; shift ;;
    --overlay)     OVERLAY="$2"; shift 2 ;;
    --rotate)      ROTATE="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "run as root (sudo ./install-dashboard.sh)" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES="$SCRIPT_DIR/files/dashboard"
[[ -d "$FILES" ]] || { echo "missing files/dashboard/ next to installer" >&2; exit 1; }

log() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }

log "Installing dashboard dependencies"
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
  python3 python3-pil python3-evdev python3-numpy fonts-dejavu-core

log "Installing dashboard script + systemd unit"
# We avoid 'install' here: on some Pi OS images it has produced 0-byte
# targets when invoked in rapid succession (fsync/tmpfile race on vfat).
# Plain cp has been reliable.
cp    "$FILES/dashboard.py"                /usr/local/bin/tailpipe-dashboard
chmod 0755                                 /usr/local/bin/tailpipe-dashboard
cp    "$FILES/tailpipe-dashboard.service"  /etc/systemd/system/tailpipe-dashboard.service
chmod 0644                                 /etc/systemd/system/tailpipe-dashboard.service
# Verify the copies actually landed — diagnostics-friendly fail path.
[[ -s /usr/local/bin/tailpipe-dashboard ]] || { echo "copy produced empty dashboard binary" >&2; exit 1; }
[[ -s /etc/systemd/system/tailpipe-dashboard.service ]] || { echo "copy produced empty service unit" >&2; exit 1; }
systemctl daemon-reload
systemctl enable tailpipe-dashboard.service

if [[ $WITH_DRIVER -eq 1 ]]; then
  # Pi OS bookworm/trixie uses /boot/firmware/config.txt; older /boot/config.txt.
  CFG=/boot/firmware/config.txt
  [[ -f "$CFG" ]] || CFG=/boot/config.txt
  [[ -f "$CFG" ]] || { echo "cannot find config.txt" >&2; exit 1; }

  LINE="dtoverlay=${OVERLAY},rotate=${ROTATE}"
  [[ -n "${OVERLAY_EXTRA:-}" ]] && LINE="${LINE},${OVERLAY_EXTRA}"
  if grep -q "^dtoverlay=${OVERLAY}" "$CFG"; then
    log "overlay already present in $CFG"
  else
    log "adding '$LINE' to $CFG"
    printf '\n# TailPiPE: 3.5" SPI LCD\n%s\n' "$LINE" >> "$CFG"
    if ! grep -qE '^dtparam=spi=on' "$CFG"; then
      echo 'dtparam=spi=on' >> "$CFG"
    fi
    # Force vfat to flush — we've seen the boot partition lose a fresh
    # append across power-cycles without this.
    sync
    REBOOT_NEEDED=1
  fi
fi

# Start service now (safe — ConditionPathExists=/dev/fb1 will skip if no LCD)
if [[ -e /dev/fb1 ]]; then
  log "/dev/fb1 present — starting dashboard now"
  systemctl restart tailpipe-dashboard.service
else
  log "/dev/fb1 not present yet — dashboard will start after LCD is up"
fi

cat <<EOF

$(tput bold 2>/dev/null)Dashboard installed.$(tput sgr0 2>/dev/null)

Service:   tailpipe-dashboard.service
Binary:    /usr/local/bin/tailpipe-dashboard
Condition: runs only when /dev/fb1 exists

Tail logs with:   journalctl -u tailpipe-dashboard -f
Manually test:    sudo /usr/local/bin/tailpipe-dashboard

EOF

if [[ "${REBOOT_NEEDED:-0}" -eq 1 ]]; then
  echo "*** REBOOT REQUIRED *** — the LCD overlay has been added. Run:"
  echo "  sudo reboot"
fi
