#!/bin/bash
# TailPiPE installer — turns a Raspberry Pi (or any Debian/NetworkManager box)
# into a tailnet-bridged LAN gateway.
#
# Topology:
#
#   +----------------+      +-----------------+      +---------+
#   | eth clients    | <--> | Pi ($LAN_IP)    | <--> | wlan0   | <--> internet / tailnet
#   | DHCP from pi   | eth  | dnsmasq, NAT,   | wifi |         |
#   |                |      | tailscale0      |      |         |
#   +----------------+      +-----------------+      +---------+
#
# What this script does:
#   1. Installs tailscale, dnsmasq, iptables, iptables-persistent.
#   2. Runs `tailscale up` advertising the LAN subnet (prints auth URL; you
#      open it to associate the node with your tailnet).
#   3. Assigns a static IP to the LAN interface via NetworkManager.
#   4. Writes dnsmasq config for DHCP + DNS on the LAN side, wires MagicDNS
#      forwarding to 100.100.100.100 for *.ts.net.
#   5. Enables IP forwarding and installs iptables MASQUERADE rules for both
#      the wifi uplink (internet) and tailscale0 (tailnet) — so eth clients
#      reach everything the pi can reach, without requiring Tailscale admin
#      approval of the subnet route.
#   6. Installs /usr/local/bin/ts-hosts-update + a 5-min systemd timer that
#      refreshes /etc/hosts.tailscale from `tailscale status`. That lets
#      dnsmasq answer queries like `ping pluto` with the peer's 100.x IP.
#
# Defaults are sane for a vanilla Pi with built-in wifi on wlan0 and the
# onboard ethernet port as the LAN side. Override with env vars:
#
#   LAN_IFACE=eth0 WAN_IFACE=wlan0 LAN_SUBNET=192.168.50.0/24 \
#     LAN_IP=192.168.50.1 DHCP_START=192.168.50.100 DHCP_END=192.168.50.200 \
#     ./install.sh

set -euo pipefail

# ---- configurable defaults ---------------------------------------------------
LAN_IFACE="${LAN_IFACE:-eth0}"
WAN_IFACE="${WAN_IFACE:-wlan0}"
LAN_SUBNET="${LAN_SUBNET:-192.168.50.0/24}"
LAN_IP="${LAN_IP:-192.168.50.1}"
LAN_NETMASK="${LAN_NETMASK:-255.255.255.0}"
DHCP_START="${DHCP_START:-192.168.50.100}"
DHCP_END="${DHCP_END:-192.168.50.200}"

# ---- helpers -----------------------------------------------------------------
log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\n\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[1;31mXX\033[0m %s\n' "$*" >&2; exit 1; }

require_root() {
  if [[ $EUID -ne 0 ]]; then
    die "Run as root (e.g. sudo ./install.sh)"
  fi
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command '$1' not found"
}

# ---- preflight ---------------------------------------------------------------
require_root

require_cmd apt-get
require_cmd systemctl

if ! systemctl is-active --quiet NetworkManager; then
  die "NetworkManager is not active. This installer targets NM-managed systems (Raspberry Pi OS Bookworm/Trixie, Ubuntu)."
fi

if ! ip link show "$WAN_IFACE" >/dev/null 2>&1; then
  die "uplink interface '$WAN_IFACE' not found. Set WAN_IFACE=... and re-run."
fi
if ! ip link show "$LAN_IFACE" >/dev/null 2>&1; then
  die "downlink interface '$LAN_IFACE' not found. Set LAN_IFACE=... and re-run."
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES_DIR="$SCRIPT_DIR/files"
[[ -d "$FILES_DIR" ]] || die "files/ directory missing next to install.sh"

# ---- 1. install packages -----------------------------------------------------
log "Installing tailscale, dnsmasq, iptables-persistent"

if [[ ! -f /etc/apt/sources.list.d/tailscale.list ]]; then
  # Detect distro codename (trixie, bookworm, jammy, etc.)
  . /etc/os-release
  CODENAME="${VERSION_CODENAME:-bookworm}"
  DISTRO_ID="${ID:-debian}"
  curl -fsSL "https://pkgs.tailscale.com/stable/${DISTRO_ID}/${CODENAME}.noarmor.gpg" \
    -o /usr/share/keyrings/tailscale-archive-keyring.gpg
  curl -fsSL "https://pkgs.tailscale.com/stable/${DISTRO_ID}/${CODENAME}.tailscale-keyring.list" \
    -o /etc/apt/sources.list.d/tailscale.list
fi

apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q tailscale dnsmasq iptables

echo iptables-persistent iptables-persistent/autosave_v4 boolean true | debconf-set-selections
echo iptables-persistent iptables-persistent/autosave_v6 boolean true | debconf-set-selections
DEBIAN_FRONTEND=noninteractive apt-get install -y -q iptables-persistent

# The default dnsmasq install may have started it with a config that conflicts
# with a system resolver — stop it now and restart once we've written our conf.
systemctl stop dnsmasq 2>/dev/null || true

# ---- 2. tailscale up ---------------------------------------------------------
log "Bringing up Tailscale (advertising $LAN_SUBNET)"

if tailscale status >/dev/null 2>&1; then
  warn "Tailscale is already logged in; re-running 'tailscale set' to ensure subnet route is advertised."
  tailscale set --advertise-routes="$LAN_SUBNET" --accept-dns=false
else
  echo
  echo "A URL will appear below. Open it in a browser to associate this node"
  echo "with your tailnet, then come back here."
  echo
  tailscale up --advertise-routes="$LAN_SUBNET" --accept-dns=false --hostname="$(hostname)"
fi

# Discover MagicDNS suffix (e.g. tailXXXXXX.ts.net) so we can configure dnsmasq.
MAGICDNS_SUFFIX="$(
  tailscale status --json 2>/dev/null \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("MagicDNSSuffix") or "")'
)"
if [[ -z "$MAGICDNS_SUFFIX" ]]; then
  die "Could not detect tailnet MagicDNS suffix from tailscale. Is MagicDNS enabled in your tailnet?"
fi
log "Detected MagicDNS suffix: $MAGICDNS_SUFFIX"

# ---- 3. configure LAN interface with a static IP via NetworkManager ----------
log "Configuring $LAN_IFACE with static IP $LAN_IP"

# Find an existing NM connection for the LAN interface; create one if absent.
LAN_CON="$(nmcli -t -f NAME,DEVICE con show | awk -F: -v d="$LAN_IFACE" '$2==d{print $1;exit}')"
if [[ -z "$LAN_CON" ]]; then
  LAN_CON="tailpipe-$LAN_IFACE"
  nmcli con add type ethernet ifname "$LAN_IFACE" con-name "$LAN_CON"
fi
nmcli con modify "$LAN_CON" \
  ipv4.method manual \
  ipv4.addresses "$LAN_IP/${LAN_NETMASK}" \
  ipv4.gateway "" \
  ipv4.dns "" \
  ipv4.never-default yes \
  ipv6.method disabled \
  connection.autoconnect yes

# Bring up (OK if no carrier — config persists for when a cable is plugged in)
nmcli con up "$LAN_CON" 2>/dev/null \
  || warn "$LAN_IFACE not active yet (likely no cable). Config persisted — it'll activate on plug-in."

# ---- 4. dnsmasq --------------------------------------------------------------
log "Writing /etc/dnsmasq.d/tailpipe.conf"

# Render template
sed \
  -e "s|__LAN_IFACE__|$LAN_IFACE|g" \
  -e "s|__LAN_IP__|$LAN_IP|g" \
  -e "s|__LAN_NETMASK__|$LAN_NETMASK|g" \
  -e "s|__DHCP_START__|$DHCP_START|g" \
  -e "s|__DHCP_END__|$DHCP_END|g" \
  -e "s|__MAGICDNS_SUFFIX__|$MAGICDNS_SUFFIX|g" \
  "$FILES_DIR/dnsmasq-tailpipe.conf.tmpl" > /etc/dnsmasq.d/tailpipe.conf

systemctl enable dnsmasq
systemctl restart dnsmasq

# ---- 5. IP forwarding + NAT --------------------------------------------------
log "Enabling IPv4/IPv6 forwarding"
install -m 0644 "$FILES_DIR/99-tailpipe-sysctl.conf" /etc/sysctl.d/99-tailpipe.conf
sysctl --system >/dev/null

log "Installing iptables MASQUERADE rules"
# MASQUERADE for internet (WAN uplink)
iptables -t nat -D POSTROUTING -s "$LAN_SUBNET" -o "$WAN_IFACE" -j MASQUERADE 2>/dev/null || true
iptables -t nat -A POSTROUTING -s "$LAN_SUBNET" -o "$WAN_IFACE" -j MASQUERADE
# MASQUERADE for tailnet egress (so replies come back to the pi without
# requiring admin-console route approval)
iptables -t nat -D POSTROUTING -s "$LAN_SUBNET" -o tailscale0 -j MASQUERADE 2>/dev/null || true
iptables -t nat -A POSTROUTING -s "$LAN_SUBNET" -o tailscale0 -j MASQUERADE

netfilter-persistent save >/dev/null

# ---- 6. tailnet-hostname bridge ----------------------------------------------
log "Installing ts-hosts-update + 5-min systemd timer"
install -m 0755 "$FILES_DIR/ts-hosts-update"         /usr/local/bin/ts-hosts-update
install -m 0644 "$FILES_DIR/ts-hosts-update.service" /etc/systemd/system/ts-hosts-update.service
install -m 0644 "$FILES_DIR/ts-hosts-update.timer"   /etc/systemd/system/ts-hosts-update.timer

# Seed the hosts file now
/usr/local/bin/ts-hosts-update || true
systemctl kill -s HUP dnsmasq 2>/dev/null || true

systemctl daemon-reload
systemctl enable --now ts-hosts-update.timer

# ---- done --------------------------------------------------------------------
cat <<EOF

$(tput bold 2>/dev/null)TailPiPE installed.$(tput sgr0 2>/dev/null)

Summary
  LAN:    $LAN_IFACE → $LAN_IP ($LAN_SUBNET), DHCP $DHCP_START–$DHCP_END
  WAN:    $WAN_IFACE (internet uplink via NAT)
  Tailnet: advertised $LAN_SUBNET, MagicDNS suffix $MAGICDNS_SUFFIX

Verify:
  ip -br a show $LAN_IFACE
  systemctl status dnsmasq
  cat /etc/hosts.tailscale
  ping <any-tailnet-peer-name>

Optional: approve the $LAN_SUBNET subnet route in the Tailscale admin console
so peers can initiate connections to your LAN clients by their real
192.168.x.x IPs (outbound from LAN already works via the tailscale0 SNAT rule).

If clients already had a DHCP lease from before this install ran, force them
to renew once so they pick up the domain-name option and bare-name ping
works (e.g. on Windows: 'ipconfig /release && ipconfig /renew').
EOF
