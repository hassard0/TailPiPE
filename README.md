# TailPiPE

Turn a Raspberry Pi into a **tailnet-bridged LAN gateway**: plug any device
(smart TV, game console, printer, a laptop that can't run Tailscale) into the
Pi's ethernet port and it gets DHCP, internet, **and** full reachability to
your Tailscale tailnet — including resolving tailnet hostnames by short name.

Optional extra: attach a 3.5" SPI touchscreen for a live dashboard with wifi
picker, bandwidth graphs, connected-client list, and a QR-code-driven phone
UI to re-auth Tailscale.

```
    +---------------------+                +--------------------+
    |   eth client        |  ethernet      |  Raspberry Pi      |  wifi
    |   DHCP 192.168.50.x | <------------> |  dnsmasq + NAT     | <-------> internet
    |   gw/DNS=pi         |                |  tailscale (wlan0) | <-------> tailnet (100.x.x.x)
    +---------------------+                +--------------------+
                                                    |
                                      (optional) 3.5" LCD / touch
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
git clone https://github.com/hassard0/TailPiPE.git
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

### Customising

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
you skip that step (or can't — e.g. a tailnet you don't admin), traffic
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

---

## Optional: 3.5" LCD dashboard

If you've attached a Waveshare-compatible 3.5" SPI touchscreen
(ILI9486 + XPT2046 — e.g. Waveshare 3.5", GeekPi 3.5", or any clone) you can
install a local touch UI. It renders directly to the LCD framebuffer through
Pillow (no X11, no SDL) and reads touch via `python-evdev`. ~20 MB of RAM.
Flow data is read from the `conntrack` CLI (netlink) — Pi OS Trixie is built
without `CONFIG_NF_CONNTRACK_PROCFS`, so `/proc/net/nf_conntrack` isn't an
option; the installer pulls `conntrack-tools` for this.

### What the screen shows

- **Header**: hostname, LAN IP, tailnet IP, current wifi SSID + signal bars.
- **Bandwidth**: live RX/TX for `wlan0`, `eth0`, and `tailscale0` with
  60-second spark lines.
- **Connected clients**: DHCP leases filtered by ARP reachability (a stale
  12-hour lease file entry won't resurface after the client disconnects),
  plus a live per-client activity line showing flow count, protocols, and
  the top destinations.
  Destinations are name-resolved — tailnet peers via `/etc/hosts.tailscale`
  (`100.67.189.24` → `pluto`), LAN clients via their DHCP hostname, and
  anything else via background rDNS (`44.215.138.159` →
  `amazonaws.com`). Named destinations sort ahead of raw IPs so a single
  `ping <peer>` doesn't get buried under the browser's CDN traffic.
- **Tap top-left of the header** → QR-code-driven phone UI for Tailscale
  disconnect / re-auth (details below).
- **Tap top-right of the header** → wifi picker with on-screen keyboard.
- **Tap 5× in rapid succession** anywhere → touch-calibration wizard.

### Install

```bash
# First time, LCD overlay not yet enabled (no /dev/fb* named 'fb_ili9486'):
sudo ./install-dashboard.sh --with-driver
sudo reboot

# Or if the driver is already loaded:
sudo ./install-dashboard.sh
```

Flags for `install-dashboard.sh`:

| Flag              | Default                     | Meaning                                           |
|-------------------|-----------------------------|---------------------------------------------------|
| `--with-driver`   | (off)                       | Append `dtoverlay=...` + `dtparam=spi=on` to `config.txt`. Reboot required. |
| `--overlay NAME`  | `piscreen`                  | Which device-tree overlay to use.                 |
| `--rotate N`      | `90`                        | Display rotation (0 / 90 / 180 / 270).            |

`piscreen` is the modern Pi OS name for the ILI9486+XPT2046 combo — the
legacy `waveshare35[abcg]` overlays were removed from Raspberry Pi's
`overlays/` directory in Pi OS Trixie. The extra params `speed=24000000`,
`fps=30`, and **`xohms=60`** are baked in: without the right `xohms` the
ads7846 driver reads pressure as zero and filters every tap as invalid.

If no LCD framebuffer is present at startup, the dashboard script exits
cleanly (status 0) and systemd does not retry. After physically attaching
the LCD (and adding the overlay) `systemctl restart tailpipe-dashboard`
brings it up — no reconfiguration needed.

The framebuffer device is auto-detected by scanning
`/sys/class/graphics/fbN/name` for the `fb_ili9486` / `fbtft` driver, so
the same build works whether the LCD lands on `fb0` (Pi OS Trixie with
KMS-only HDMI) or `fb1` (older images with a legacy HDMI fbdev). Override
with `TAILPIPE_FB=/dev/fbN` in the service unit if needed.

### Wifi picker

Tap the **top-right region of the header** (SSID text + signal% + bar
icon — all tappable, not just the icon pixels). The dashboard scans with
`nmcli dev wifi rescan`, presents a list sorted by signal strength, and
marks networks that require a password with `[P]`. Tap a network:

- **Open networks** connect immediately.
- **WPA networks** bring up a QWERTY on-screen keyboard with SHIFT, space,
  backspace, OK and CANCEL. The password is piped to `nmcli dev wifi
  connect <ssid> password <pw>`; status flashes on the bottom bar.

### Tailscale control (QR + phone)

Tap the **top-left region of the header** (hostname / lan / tailnet IP
text). The view shows a QR code pointing at a URL like
`http://<wifi_ip>:8080/?t=<session-token>` plus the current tailscale
state. Scan with a phone on the same wifi uplink and you'll see a small
HTML page with two actions:

- **Disconnect** — runs `tailscale logout`.
- **Connect to a different tailnet** — runs `tailscale logout &&
  tailscale up --reset`, captures the login URL from stdout within 15 s,
  and displays it on the phone as a clickable link *and* an auxiliary QR,
  so you can complete sign-in on whichever device owns the target tailnet
  account.

The page is gated by a session token minted when the LCD view opens and
invalidated when it closes. Anyone without current sight of the LCD can't
drive the endpoint even if they share your wifi. Server binds `0.0.0.0:8080`
by default; override with `TAILPIPE_TS_BIND=<ip>` and/or
`TAILPIPE_TS_PORT=<n>` in the service unit.

Tap anywhere on the LCD (or the `X` in the corner) to close the view and
expire the token.

### Touch calibration

If taps land in the wrong place (common — axes and offsets vary by panel
batch), **tap the screen 5 times in rapid succession** (within 2.5 s,
anywhere) to open the calibration wizard. You'll see a START button on
the LCD:

1. Tap anywhere to begin. (The button is a visual hint — its hitbox is the
   whole screen so the wizard still opens even when current mapping is
   completely wrong.)
2. A crosshair walks through 5 targets: top-left → top-right →
   bottom-right → bottom-left → center. Tap each centre carefully.
3. A 2×3 affine matrix is fit by least-squares. Each tap's residual is
   checked against the fitted model; if **any single point's error exceeds
   35 px**, the mapping is rejected (`calibration failed — NN px error. 5
   taps to retry`) and nothing is saved. Good runs show
   `calibration saved (max err N px)`.

Saved matrices live at `/etc/tailpipe/touch-cal.json` and load on startup.
If no calibration file is present, the dashboard falls back to a named
rotation (default 270°, override with `TAILPIPE_TOUCH_ROTATE=0|90|180|270`).
While the wizard is open, the 5-rapid-taps entry gesture is suppressed so
you can't accidentally reset mid-run.

### Dashboard env vars

| Variable                 | Default     | Meaning                                                   |
|--------------------------|-------------|-----------------------------------------------------------|
| `TAILPIPE_FB`            | `auto`      | Framebuffer path (`auto` scans for fb_ili9486).           |
| `TAILPIPE_LAN_IFACE`     | `eth0`      | Which interface's bandwidth to display as "eth0".         |
| `TAILPIPE_WAN_IFACE`     | `wlan0`     | Uplink interface name (for bandwidth + QR URL host).      |
| `TAILPIPE_TS_BIND`       | `0.0.0.0`   | Address the phone-UI HTTP server binds to.                |
| `TAILPIPE_TS_PORT`       | `8080`      | TCP port for the phone-UI HTTP server.                    |
| `TAILPIPE_TOUCH_ROTATE`  | `270`       | Rotation fallback when no calibration file is present.    |
| `TAILPIPE_LAN_SUBNET`    | `192.168.50.0/24` | Advertised via `tailscale up` during re-auth.       |

Set via `systemctl edit tailpipe-dashboard` (adds a drop-in override).

---

## Uninstall

```bash
sudo ./uninstall.sh                   # reverts the core gateway
sudo ./uninstall.sh --dashboard       # also removes the LCD dashboard
```

Both leave the Tailscale package itself in place. Run
`sudo apt-get purge tailscale` separately if you want that gone too.

## License

[MIT](LICENSE).
