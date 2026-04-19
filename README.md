# TailPiPE

Turn a Raspberry Pi into a **tailnet-bridged LAN gateway**: plug any device
(smart TV, game console, printer, a laptop that can't run Tailscale) into the
Pi's ethernet port and it gets DHCP, internet, **and** full reachability to
your Tailscale tailnet — including resolving tailnet hostnames by short name.

```
    +---------------------+                +--------------------+
    |   eth client        |  ethernet      |  Raspberry Pi      |  wifi
    |   DHCP 192.168.50.x | <------------> |  dnsmasq + NAT     | <-------> internet
    |   gw/DNS=pi         |                |  tailscale (wlan0) | <-------> tailnet (100.x.x.x)
    +---------------------+                +--------------------+
```

## What it does

1. **DHCP + DNS server** (dnsmasq) on the Pi's ethernet interface — clients
   get an IP, gateway, DNS, and the tailnet's MagicDNS suffix as a search
   domain.
2. **NAT to the internet** through the Pi's wifi uplink.
3. **NAT to the tailnet** through `tailscale0` — so eth clients can reach
   every tailnet peer without the subnet route having to be approved in the
   Tailscale admin console.
4. **Tailnet hostname resolution**: dnsmasq serves authoritative A records
   for every tailnet peer (`ping pluto` → the real `100.x` IP). A 5-minute
   systemd timer keeps the list in sync with `tailscale status`.

## Requirements

- Raspberry Pi OS Bookworm or Trixie (or any recent Debian/Ubuntu) using
  **NetworkManager** for networking. Older `dhcpcd`-based setups are not
  supported by the installer (patches welcome).
- A Tailscale account with MagicDNS enabled.
- Pi wifi configured and working as the uplink.
- Root on the Pi.

## Install

```bash
git clone https://github.com/YOUR_USERNAME/TailPiPE.git
cd TailPiPE
sudo ./install.sh
```

The installer will:

1. Add the Tailscale apt repo and install `tailscale`, `dnsmasq`,
   `iptables`, `iptables-persistent`.
2. Run `tailscale up --advertise-routes=192.168.50.0/24 --accept-dns=false`
   and print an auth URL — open it in a browser to associate the Pi with
   your tailnet.
3. Assign `192.168.50.1/24` to `eth0` via NetworkManager.
4. Write `/etc/dnsmasq.d/tailpipe.conf` with the DHCP range, gateway/DNS
   options, and a MagicDNS forwarder.
5. Enable IPv4/IPv6 forwarding and install `MASQUERADE` rules for both the
   wifi uplink and `tailscale0` (saved via `iptables-persistent`).
6. Install `/usr/local/bin/ts-hosts-update` and a systemd timer that
   refreshes `/etc/hosts.tailscale` from `tailscale status` every 5 min,
   SIGHUP'ing dnsmasq on change.

### Customizing

Set env vars before running the installer to change defaults:

| Variable       | Default             | Meaning                                  |
|----------------|---------------------|------------------------------------------|
| `LAN_IFACE`    | `eth0`              | Pi's LAN-side (downlink) interface       |
| `WAN_IFACE`    | `wlan0`             | Pi's internet-side (uplink) interface    |
| `LAN_SUBNET`   | `192.168.50.0/24`   | Subnet to serve on the LAN side          |
| `LAN_IP`       | `192.168.50.1`      | Pi's address on the LAN                  |
| `LAN_NETMASK`  | `255.255.255.0`     | Netmask for that subnet                  |
| `DHCP_START`   | `192.168.50.100`    | First address in the DHCP pool           |
| `DHCP_END`     | `192.168.50.200`    | Last address in the DHCP pool            |

Example:

```bash
sudo LAN_SUBNET=10.42.0.0/24 LAN_IP=10.42.0.1 \
     DHCP_START=10.42.0.100 DHCP_END=10.42.0.200 \
     ./install.sh
```

## How it works

### Why MASQUERADE on `tailscale0`?

Tailscale's subnet-router feature normally requires the subnet to be
**approved** in the admin console before peers have a route back to it. If
you skip that step (or can't use it — e.g. a tailnet you don't admin), traffic
from eth clients reaches tailnet peers but replies have nowhere to go.

TailPiPE adds an `iptables -t nat ... -o tailscale0 -j MASQUERADE` rule, so
outbound traffic from the LAN subnet appears to come from the Pi's own
tailnet IP. Peers reply to the Pi, which un-NATs and delivers back to the LAN
client. Tradeoff: peers can't see the real LAN client source IP, and
peer-*initiated* connections to LAN clients require the subnet route to be
approved as usual.

### Why a hosts file instead of `--accept-dns`?

Setting `--accept-dns=true` makes `tailscaled` rewrite `/etc/resolv.conf`,
which then interacts awkwardly with NetworkManager and dnsmasq. Instead,
TailPiPE keeps the Pi's own resolver untouched, and tells dnsmasq:

- Forward `*.ts.net` to `100.100.100.100` (tailscale's MagicDNS listener).
- Use `/etc/hosts.tailscale` as an authoritative source for tailnet peer
  short names and FQDNs.

The systemd timer regenerates `/etc/hosts.tailscale` from
`tailscale status --json` every 5 minutes, so new/removed peers show up
automatically without re-running the installer.

## Verifying

```bash
# on the pi
ip -br a show eth0                     # should show 192.168.50.1/24
systemctl status dnsmasq               # active
cat /etc/hosts.tailscale               # list of peers
sudo iptables -t nat -L POSTROUTING -n # two MASQUERADE rules

# on a client plugged into the pi
ping 192.168.50.1                      # the pi itself
ping 8.8.8.8                           # internet via NAT
ping <peer-name>                       # a tailnet peer by short name
```

If a client got a DHCP lease from this Pi **before** running the installer,
force a renewal so it picks up the domain-search option:

```
# Windows
ipconfig /release && ipconfig /renew

# Linux (systemd-networkd)
sudo networkctl renew <iface>

# Linux (NetworkManager)
sudo nmcli connection down "<conn>" && sudo nmcli connection up "<conn>"
```

## Optional: on-device LCD dashboard

If you've attached a Waveshare-compatible 3.5" SPI touchscreen (ILI9486 +
XPT2046 — e.g. the Waveshare 3.5", GeekPi 3.5", or any clone of either), you
can install a small dashboard that shows:

- Hostname, LAN IP, tailnet IP
- Live RX/TX bandwidth for `wlan0`, `eth0`, and `tailscale0`, with 60-second
  spark lines
- Current DHCP leases (IP / name / MAC) — same list dnsmasq hands out
- A wifi icon in the top-right corner with signal strength; tap it to scan
  networks and tap an SSID to connect (on-screen keyboard for the password)

```bash
# If the LCD overlay is not yet enabled (no /dev/fb1):
sudo ./install-dashboard.sh --with-driver
sudo reboot

# If the LCD driver is already set up:
sudo ./install-dashboard.sh
```

The systemd unit is gated on `ConditionPathExists=/dev/fb1`, so installing
the dashboard on a Pi without an LCD is safe — the service stays dormant
until the framebuffer appears, and you can flip the LCD on/off without
reconfiguring anything.

Rendering goes directly to `/dev/fb1` via Pillow (no X11, no SDL) and touch
comes from `python-evdev`. About 20 MB of RAM at idle.

## Uninstall

```bash
sudo ./uninstall.sh
```

Reverts everything except the Tailscale package itself. Use
`sudo apt-get purge tailscale` if you want that gone too.

## License

[MIT](LICENSE).
