#!/bin/bash
# TailPiPE uninstaller — reverses what install.sh did.
# Does NOT uninstall Tailscale itself (you may want to keep it); use
# `apt-get purge tailscale` separately if desired.
set -euo pipefail

LAN_IFACE="${LAN_IFACE:-eth0}"
WAN_IFACE="${WAN_IFACE:-wlan0}"
LAN_SUBNET="${LAN_SUBNET:-192.168.50.0/24}"

[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }

echo "==> disabling timer + service"
systemctl disable --now ts-hosts-update.timer 2>/dev/null || true
rm -f /etc/systemd/system/ts-hosts-update.service
rm -f /etc/systemd/system/ts-hosts-update.timer
rm -f /usr/local/bin/ts-hosts-update
rm -f /etc/hosts.tailscale
systemctl daemon-reload

echo "==> removing dnsmasq config"
rm -f /etc/dnsmasq.d/tailpipe.conf
systemctl restart dnsmasq 2>/dev/null || true

echo "==> removing sysctl drop-in"
rm -f /etc/sysctl.d/99-tailpipe.conf
sysctl --system >/dev/null

echo "==> removing iptables rules"
iptables -t nat -D POSTROUTING -s "$LAN_SUBNET" -o "$WAN_IFACE" -j MASQUERADE 2>/dev/null || true
iptables -t nat -D POSTROUTING -s "$LAN_SUBNET" -o tailscale0    -j MASQUERADE 2>/dev/null || true
netfilter-persistent save >/dev/null 2>&1 || true

echo "==> reverting LAN connection to DHCP (NetworkManager)"
LAN_CON="$(nmcli -t -f NAME,DEVICE con show | awk -F: -v d="$LAN_IFACE" '$2==d{print $1;exit}')"
if [[ -n "$LAN_CON" ]]; then
  nmcli con modify "$LAN_CON" ipv4.method auto ipv4.addresses "" ipv4.gateway "" ipv4.never-default no ipv6.method auto
  nmcli con up "$LAN_CON" 2>/dev/null || true
fi

echo "done. (Tailscale package kept; run 'apt-get purge tailscale' to remove it too.)"
