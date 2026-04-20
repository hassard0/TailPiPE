#!/usr/bin/env python3
"""TailPiPE dashboard for a 480x320 SPI LCD (ILI9486 + XPT2046 touch).

Renders directly to /dev/fb1 via Pillow (no X11, no SDL). Touch events come
from evdev. Tapping the wifi icon in the top-right opens a network picker;
tapping an SSID opens an on-screen keyboard for the password and runs
`nmcli dev wifi connect`.

Service gates itself on /dev/fb1 existing (systemd ConditionPathExists), so
installing the dashboard on a pi without an LCD is a no-op at runtime.
"""
import os, sys, time, mmap, json, subprocess, threading, signal
from collections import deque
from PIL import Image, ImageDraw, ImageFont
import evdev
from evdev import ecodes
import numpy as np

# ---- config -----------------------------------------------------------------

def _find_lcd_fb():
    """Pick the framebuffer backed by the ILI9486 SPI driver.

    On Pi OS Trixie (KMS-only HDMI) the SPI LCD tends to register as fb0,
    while on older images with a legacy HDMI fbdev it shows up as fb1. Scan
    /sys/class/graphics/fbN/name and match the fbtft driver.
    """
    import glob
    for sys_path in sorted(glob.glob('/sys/class/graphics/fb[0-9]*')):
        try:
            with open(os.path.join(sys_path, 'name')) as f:
                name = f.read().strip()
        except OSError:
            continue
        if 'ili9486' in name.lower() or 'fbtft' in name.lower():
            return '/dev/' + os.path.basename(sys_path)
    # Fallback: if there's exactly one fbdev, use it
    devs = sorted(glob.glob('/dev/fb[0-9]*'))
    return devs[0] if len(devs) == 1 else None

_env_fb = os.environ.get('TAILPIPE_FB', 'auto')
FB_DEV = _find_lcd_fb() if _env_fb == 'auto' else _env_fb
LAN_IFACE = os.environ.get('TAILPIPE_LAN_IFACE', 'eth0')
WAN_IFACE = os.environ.get('TAILPIPE_WAN_IFACE', 'wlan0')
TS_IFACE = 'tailscale0'
LEASES_FILE = '/var/lib/misc/dnsmasq.leases'

# ---- framebuffer ------------------------------------------------------------

def fb_info(dev):
    n = dev.rsplit('fb', 1)[1]
    base = f'/sys/class/graphics/fb{n}'
    with open(f'{base}/virtual_size') as f:
        w, h = (int(x) for x in f.read().strip().split(','))
    with open(f'{base}/bits_per_pixel') as f:
        bpp = int(f.read().strip())
    with open(f'{base}/stride') as f:
        stride = int(f.read().strip())
    return w, h, bpp, stride

class FB:
    def __init__(self, dev):
        self.w, self.h, self.bpp, self.stride = fb_info(dev)
        self.fd = os.open(dev, os.O_RDWR)
        self.mm = mmap.mmap(self.fd, self.stride * self.h,
                            mmap.MAP_SHARED, mmap.PROT_WRITE | mmap.PROT_READ)
    def push(self, img):
        # Pillow RGB888 -> framebuffer RGB565 little-endian.
        if img.size != (self.w, self.h):
            img = img.resize((self.w, self.h))
        if self.bpp == 16:
            r, g, b = img.convert('RGB').split()
            import numpy as np
            r = (np.array(r, dtype=np.uint16) >> 3) << 11
            g = (np.array(g, dtype=np.uint16) >> 2) << 5
            b = (np.array(b, dtype=np.uint16) >> 3)
            buf = (r | g | b).astype('<u2').tobytes()
        elif self.bpp == 32:
            buf = img.convert('RGBA').tobytes()
        else:
            raise RuntimeError(f'unsupported fb bpp={self.bpp}')
        self.mm.seek(0)
        self.mm.write(buf)
    def close(self):
        try: self.mm.close()
        except Exception: pass
        try: os.close(self.fd)
        except Exception: pass

# ---- touch ------------------------------------------------------------------

def find_touch():
    for path in evdev.list_devices():
        try:
            d = evdev.InputDevice(path)
        except Exception:
            continue
        caps = d.capabilities()
        name = (d.name or '').lower()
        if any(s in name for s in ('ads7846', 'xpt2046', 'touchscreen', 'touch')):
            return d
        if ecodes.EV_ABS in caps and ecodes.BTN_TOUCH in caps.get(ecodes.EV_KEY, []):
            return d
    return None

class Touch:
    """Reads from an evdev touchscreen, producing (x, y) 'tap' events.

    Absolute axis values are normalized against ABS_X / ABS_Y min-max and
    then mapped to screen pixels. Rotation is applied so the dashboard's
    480x320 landscape origin matches finger presses on a screen configured
    with `dtoverlay=waveshare35a,rotate=90`.
    """
    def __init__(self, dev, screen_w, screen_h, matrix=None, rotate=None):
        self.dev = dev
        self.w = screen_w; self.h = screen_h
        # Prefer a saved affine calibration (matrix). If absent, fall back to
        # a named rotation: piscreen + display rotate=90 nets to touch
        # rotate=270. Override with TAILPIPE_TOUCH_ROTATE=0|90|180|270.
        self.matrix = matrix  # 2x3 numpy array mapping [rx, ry, 1] -> [sx, sy]
        if rotate is None:
            try:
                rotate = int(os.environ.get('TAILPIPE_TOUCH_ROTATE', '270'))
            except ValueError:
                rotate = 270
        self.rotate = rotate
        ax = dict(dev.capabilities()[ecodes.EV_ABS])
        self.xmin, self.xmax = ax[ecodes.ABS_X].min, ax[ecodes.ABS_X].max
        self.ymin, self.ymax = ax[ecodes.ABS_Y].min, ax[ecodes.ABS_Y].max
        self._x = self._y = 0
        self._down = False
        self._down_t = 0.0
        # Queue entries: (kind, screen_x, screen_y, raw_x, raw_y) where
        # kind is 'tap' or 'long_tap'. Raw coords are preserved so the
        # calibration routine can learn the mapping directly from them.
        self.queue = []
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True).start()

    def set_matrix(self, M):
        self.matrix = M
    def _map(self, raw_x, raw_y):
        if self.matrix is not None:
            v = self.matrix @ np.array([raw_x, raw_y, 1.0])
            x, y = int(v[0]), int(v[1])
        else:
            # Portrait-native panel; named rotation compensates for the
            # overlay + touchscreen-swapped-x-y combination.
            fx = (raw_x - self.xmin) / max(1, self.xmax - self.xmin)
            fy = (raw_y - self.ymin) / max(1, self.ymax - self.ymin)
            if self.rotate == 90:
                x, y = int(fy * self.w), int((1 - fx) * self.h)
            elif self.rotate == 270:
                x, y = int((1 - fy) * self.w), int(fx * self.h)
            elif self.rotate == 180:
                x, y = int((1 - fx) * self.w), int((1 - fy) * self.h)
            else:
                x, y = int(fx * self.w), int(fy * self.h)
        return max(0, min(self.w - 1, x)), max(0, min(self.h - 1, y))
    def _loop(self):
        for ev in self.dev.read_loop():
            if ev.type == ecodes.EV_ABS:
                if ev.code == ecodes.ABS_X: self._x = ev.value
                elif ev.code == ecodes.ABS_Y: self._y = ev.value
            elif ev.type == ecodes.EV_KEY and ev.code == ecodes.BTN_TOUCH:
                if ev.value:
                    self._down = True
                    self._down_t = time.monotonic()
                else:
                    if self._down:
                        rx, ry = self._x, self._y
                        sx, sy = self._map(rx, ry)
                        kind = 'long_tap' if (time.monotonic() - self._down_t) > 1.2 else 'tap'
                        with self._lock:
                            self.queue.append((kind, sx, sy, rx, ry))
                    self._down = False
    def poll(self):
        with self._lock:
            out = self.queue[:]
            self.queue.clear()
        return out

# ---- touch calibration ------------------------------------------------------

CALIB_PATH = '/etc/tailpipe/touch-cal.json'
# Target screen points for the 5-dot affine calibration, in screen coords.
# Order: top-left, top-right, bottom-right, bottom-left, center.
CAL_TARGETS = [(40, 30), (440, 30), (440, 290), (40, 290), (240, 160)]

def load_calibration():
    try:
        with open(CALIB_PATH) as f:
            d = json.load(f)
        M = np.array(d['matrix'], dtype=float)
        if M.shape == (2, 3):
            return M
    except (OSError, ValueError, KeyError):
        pass
    return None

def save_calibration(M):
    os.makedirs(os.path.dirname(CALIB_PATH), exist_ok=True)
    with open(CALIB_PATH, 'w') as f:
        json.dump({'matrix': M.tolist(), 'targets': CAL_TARGETS}, f)

def compute_affine(screen_pts, raw_pts):
    """Least-squares fit of 2x3 affine: screen = M @ [rx, ry, 1]."""
    S = np.asarray(screen_pts, dtype=float)   # (N, 2)
    R = np.asarray(raw_pts, dtype=float)      # (N, 2)
    A = np.column_stack([R, np.ones(len(R))])  # (N, 3)
    Mt, *_ = np.linalg.lstsq(A, S, rcond=None)
    return Mt.T   # (2, 3)

# ---- system probes ----------------------------------------------------------

def run(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, '', str(e)

def ip4(iface):
    _, out, _ = run(['ip', '-4', '-br', 'addr', 'show', iface])
    parts = out.split()
    return parts[2].split('/')[0] if len(parts) >= 3 else '-'

def tailscale_ip():
    _, out, _ = run(['tailscale', 'ip', '-4'])
    return (out.strip().splitlines() or ['-'])[0]

def wifi_status():
    _, out, _ = run(['nmcli', '-t', '-f', 'IN-USE,SIGNAL,SSID', 'dev', 'wifi'])
    for line in out.splitlines():
        p = line.split(':')
        if len(p) >= 3 and p[0] == '*':
            try: sig = int(p[1] or 0)
            except ValueError: sig = 0
            return (':'.join(p[2:]), sig)
    return (None, 0)

def scan_wifi():
    run(['nmcli', 'dev', 'wifi', 'rescan'], timeout=12)
    _, out, _ = run(['nmcli', '-t', '-f', 'IN-USE,SIGNAL,SECURITY,SSID', 'dev', 'wifi'])
    seen = set(); nets = []
    for line in out.splitlines():
        p = line.split(':')
        if len(p) < 4: continue
        ssid = ':'.join(p[3:])
        if not ssid or ssid in seen: continue
        seen.add(ssid)
        try: sig = int(p[1] or 0)
        except ValueError: sig = 0
        nets.append({'in_use': p[0] == '*', 'signal': sig,
                     'security': bool(p[2].strip()), 'ssid': ssid})
    nets.sort(key=lambda n: -n['signal'])
    return nets

def connect_wifi(ssid, password=None):
    cmd = ['nmcli', 'dev', 'wifi', 'connect', ssid]
    if password: cmd += ['password', password]
    rc, out, err = run(cmd, timeout=45)
    return rc == 0, (out + err).strip()

def read_bytes(iface):
    try:
        with open('/proc/net/dev') as f:
            for line in f:
                if ':' not in line: continue
                name, rest = line.split(':', 1)
                if name.strip() == iface:
                    p = rest.split()
                    return int(p[0]), int(p[8])
    except Exception: pass
    return 0, 0

class Rate:
    def __init__(self, iface, hist=60):
        self.iface = iface
        self.rx_prev, self.tx_prev = read_bytes(iface)
        self.t_prev = time.monotonic()
        self.rx_rate = self.tx_rate = 0.0
        self.rx_hist = deque([0]*hist, maxlen=hist)
        self.tx_hist = deque([0]*hist, maxlen=hist)
    def tick(self):
        rx, tx = read_bytes(self.iface)
        now = time.monotonic()
        dt = max(0.1, now - self.t_prev)
        self.rx_rate = max(0, (rx - self.rx_prev) / dt)
        self.tx_rate = max(0, (tx - self.tx_prev) / dt)
        self.rx_prev, self.tx_prev, self.t_prev = rx, tx, now
        self.rx_hist.append(self.rx_rate); self.tx_hist.append(self.tx_rate)

def human(n):
    for u in ('B', 'K', 'M', 'G'):
        if n < 1024: return f'{n:5.1f}{u}'
        n /= 1024
    return f'{n:5.1f}T'

def read_leases():
    out = []
    try:
        with open(LEASES_FILE) as f:
            for line in f:
                p = line.split()
                if len(p) >= 4:
                    out.append({'mac': p[1], 'ip': p[2], 'name': p[3]})
    except FileNotFoundError:
        pass
    return out

# ---- rendering --------------------------------------------------------------

W, H = 480, 320
BG = (10, 12, 18); PANEL = (22, 26, 36); BAR = (30, 35, 50)
FG = (232, 232, 232); DIM = (150, 152, 160)
ACCENT = (70, 170, 255); GREEN = (80, 200, 110); YELLOW = (240, 200, 80); RED = (240, 80, 70)

def _font(size, bold=False):
    for path in ('/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf' if bold
                 else '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',
                 '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()
F_SM = _font(11); F_MD = _font(14, True); F_LG = _font(18, True)

def draw_wifi_icon(d, x, y, signal):
    bars = max(0, min(4, (signal + 12) // 25))
    for i in range(4):
        on = i < bars
        h = 4 + i * 4
        d.rectangle([x + i*6, y + 18 - h, x + i*6 + 4, y + 18],
                    fill=(FG if on else DIM))

def draw_header(d, state):
    d.rectangle([0, 0, W, 34], fill=PANEL)
    # Visual hint: subtly highlight the whole tappable wifi zone (x=290..W).
    d.rectangle([290, 0, W, 34], fill=(28, 34, 50))
    d.text((8, 4), os.uname().nodename, font=F_MD, fill=FG)
    d.text((8, 20), f'lan {state["lan_ip"]}   tailnet {state["ts_ip"]}',
           font=F_SM, fill=DIM)
    ssid, sig = state['wifi']
    if ssid:
        s = ssid if len(ssid) <= 18 else ssid[:17] + '.'
        d.text((298, 4), s, font=F_SM, fill=FG)
        d.text((298, 18), f'{sig}%', font=F_SM, fill=DIM)
    draw_wifi_icon(d, 448, 8, sig)

def draw_bandwidth(d, state):
    y0 = 40
    d.text((8, y0), 'BANDWIDTH', font=F_MD, fill=ACCENT)
    y = y0 + 20
    for label, r, color in (('wlan0', state['wlan_rate'], GREEN),
                            ('eth0',  state['eth_rate'],  YELLOW),
                            ('ts0',   state['ts_rate'],   ACCENT)):
        d.text((8, y), f'{label:<6} RX {human(r.rx_rate)}/s  TX {human(r.tx_rate)}/s',
               font=F_SM, fill=FG)
        # dual spark: rx below line, tx above. Shared autoscale.
        gx, gy, gw, gh = 260, y - 1, 210, 14
        d.rectangle([gx, gy, gx + gw, gy + gh], fill=BAR)
        history = list(r.rx_hist) + list(r.tx_hist)
        mx = max(history) if history else 0
        if mx > 0:
            n = len(r.rx_hist)
            step = gw / n
            for i, v in enumerate(r.rx_hist):
                h = int(v / mx * (gh - 2))
                if h: d.line([gx + i*step, gy + gh - 1,
                              gx + i*step, gy + gh - 1 - h], fill=color)
            for i, v in enumerate(r.tx_hist):
                h = int(v / mx * (gh - 2))
                if h: d.line([gx + i*step, gy,
                              gx + i*step, gy + h], fill=(color[0]//2, color[1]//2, color[2]//2))
        y += 18

def draw_clients(d, state):
    y0 = 118
    leases = state['leases']
    d.text((8, y0), f'CONNECTED  ({len(leases)})', font=F_MD, fill=ACCENT)
    d.text((160, y0 + 2), 'ip              name                mac', font=F_SM, fill=DIM)
    y = y0 + 20
    for l in leases[:11]:
        name = l['name'] if l['name'] != '*' else '(unknown)'
        name = name if len(name) <= 18 else name[:17] + '.'
        d.text((8, y), f'{l["ip"]:<15} {name:<19} {l["mac"]}', font=F_SM, fill=FG)
        y += 14
    if not leases:
        d.text((8, y), '(no active leases)', font=F_SM, fill=DIM)

def draw_wifi_modal(d, state):
    d.rectangle([0, 0, W, H], fill=BG)
    d.rectangle([0, 0, W, 34], fill=PANEL)
    d.text((8, 8), 'WIFI  — tap a network', font=F_MD, fill=FG)
    d.rectangle([436, 4, 474, 28], fill=(80, 30, 30))
    d.text((449, 6), 'X', font=F_MD, fill=FG)
    if state['scanning']:
        d.text((8, 50), 'scanning...', font=F_MD, fill=DIM)
        return
    y = 42; row_h = 24
    for i, n in enumerate(state['wifi_list'][:11]):
        bg = PANEL if i % 2 == 0 else BG
        d.rectangle([0, y, W, y + row_h - 2], fill=bg)
        mark = '>' if n['in_use'] else ' '
        lock = '[P]' if n['security'] else '   '
        ssid = n['ssid'] if len(n['ssid']) <= 30 else n['ssid'][:29] + '.'
        d.text((8, y + 5), f'{mark} {ssid}', font=F_MD, fill=FG)
        d.text((370, y + 5), f'{n["signal"]:>3}%  {lock}', font=F_SM, fill=DIM)
        y += row_h

# on-screen keyboard
KBD = [
    list('1234567890'),
    list('qwertyuiop'),
    list('asdfghjkl;'),
    ['^', 'z', 'x', 'c', 'v', 'b', 'n', 'm', ',', '<'],
    ['sp', 'ok', 'x'],
]
KEY_H = 44

def kbd_hit(x, y):
    base_y = 92
    r = (y - base_y) // KEY_H
    if r < 0 or r >= len(KBD): return None
    row = KBD[r]
    py = base_y + r * KEY_H
    if len(row) == 10:
        kw = 46
        c = (x - 8) // (kw + 2)
        if 0 <= c < 10: return row[c]
        return None
    # bottom row: variable widths (SPACE + OK + CANCEL)
    px = 8
    for ch in row:
        kw = 300 if ch == 'sp' else 80
        if px <= x < px + kw: return ch
        px += kw + 2
    return None

def draw_kbd(d, state):
    d.rectangle([0, 0, W, H], fill=BG)
    d.rectangle([0, 0, W, 34], fill=PANEL)
    title = f'connect: {state["selected_ssid"]}'
    d.text((8, 8), title if len(title) <= 44 else title[:43] + '.', font=F_MD, fill=FG)
    d.rectangle([8, 44, W - 8, 78], fill=BAR)
    pw = state['password_buf']
    shown = '*' * len(pw) if state['password_mask'] else pw
    d.text((16, 52), shown, font=F_LG, fill=FG)
    if state['shift']:
        d.text((W - 36, 52), 'SHIFT', font=F_SM, fill=YELLOW)
    base_y = 92
    for r, row in enumerate(KBD):
        py = base_y + r * KEY_H
        if len(row) == 10:
            kw = 46; px = 8
            for ch in row:
                lbl = {'^': 'SH', '<': 'BS'}.get(ch, ch.upper() if state['shift'] else ch)
                col = PANEL
                d.rectangle([px, py, px + kw, py + KEY_H - 4], fill=col)
                d.text((px + kw//2 - 6, py + 10), lbl, font=F_MD, fill=FG)
                px += kw + 2
        else:
            px = 8
            for ch in row:
                kw = 300 if ch == 'sp' else 80
                lbl = {'sp': 'SPACE', 'ok': 'OK', 'x': 'CANCEL'}[ch]
                col = (40, 90, 40) if ch == 'ok' else (90, 40, 40) if ch == 'x' else PANEL
                d.rectangle([px, py, px + kw, py + KEY_H - 4], fill=col)
                d.text((px + 8, py + 12), lbl, font=F_MD, fill=FG)
                px += kw + 2

def draw_status(d, state):
    if state['status_until'] > time.monotonic() and state['status']:
        d.rectangle([0, H - 16, W, H], fill=PANEL)
        d.text((8, H - 14), state['status'], font=F_SM, fill=YELLOW)

# ---- touch handlers ---------------------------------------------------------

def handle_main_tap(state, x, y):
    # Whole top-right region (SSID text + signal% + bars + icon) opens the
    # wifi picker — not just the 4-bar icon.
    if y < 34 and x > 290:
        state['view'] = 'wifi'; state['scanning'] = True
        threading.Thread(target=_do_scan, args=(state,), daemon=True).start()

def draw_calibration(d, state):
    d.rectangle([0, 0, W, H], fill=BG)
    idx = state.get('cal_idx', 0)
    if idx >= len(CAL_TARGETS):
        return
    tx, ty = CAL_TARGETS[idx]
    d.ellipse([tx-14, ty-14, tx+14, ty+14], outline=ACCENT, width=2)
    d.line([tx-20, ty, tx+20, ty], fill=ACCENT, width=1)
    d.line([tx, ty-20, tx, ty+20], fill=ACCENT, width=1)
    d.ellipse([tx-2, ty-2, tx+2, ty+2], fill=ACCENT)
    d.text((W//2 - 90, H//2 - 20), 'TOUCH CALIBRATION', font=F_LG, fill=FG)
    d.text((W//2 - 70, H//2 + 6), f'tap target {idx + 1} of {len(CAL_TARGETS)}',
           font=F_MD, fill=DIM)
    d.text((W//2 - 150, H - 18), '5 rapid taps any time to restart',
           font=F_SM, fill=DIM)

def handle_calibration_tap(state, sx, sy, rx, ry):
    pts = state.setdefault('cal_raw_pts', [])
    pts.append((rx, ry))
    state['cal_idx'] = state.get('cal_idx', 0) + 1
    if state['cal_idx'] >= len(CAL_TARGETS):
        try:
            M = compute_affine(CAL_TARGETS, pts)
            save_calibration(M)
            state['touch'].set_matrix(M)
            set_status(state, 'calibration saved', 4)
        except Exception as e:
            set_status(state, f'calibration failed: {e}'[:50], 5)
        state['view'] = 'main'
        state.pop('cal_raw_pts', None); state.pop('cal_idx', None)

def handle_wifi_tap(state, x, y):
    if x >= 436 and y < 32:
        state['view'] = 'main'; return
    if state['scanning']: return
    if y < 42: return
    row = (y - 42) // 24
    idx = row
    if 0 <= idx < len(state['wifi_list']):
        n = state['wifi_list'][idx]
        state['selected_ssid'] = n['ssid']
        if n['security']:
            state['password_buf'] = ''
            state['shift'] = False
            state['view'] = 'kbd'
        else:
            threading.Thread(target=_do_connect, args=(state, n['ssid'], None),
                             daemon=True).start()
            state['view'] = 'main'

def handle_kbd_tap(state, x, y):
    # top-right X close
    if y < 32 and x > 440:
        state['view'] = 'wifi'; return
    # password visibility toggle: tap the password field
    if 44 <= y < 78 and x > W - 40:
        state['password_mask'] = not state['password_mask']; return
    ch = kbd_hit(x, y)
    if not ch: return
    if ch == '^':
        state['shift'] = not state['shift']
    elif ch == '<':
        state['password_buf'] = state['password_buf'][:-1]
    elif ch == 'sp':
        state['password_buf'] += ' '
    elif ch == 'x':
        state['view'] = 'wifi'
    elif ch == 'ok':
        threading.Thread(target=_do_connect,
                         args=(state, state['selected_ssid'], state['password_buf']),
                         daemon=True).start()
        state['view'] = 'main'
    elif len(ch) == 1:
        state['password_buf'] += ch.upper() if state['shift'] else ch
        state['shift'] = False

def _do_scan(state):
    try:
        state['wifi_list'] = scan_wifi()
    finally:
        state['scanning'] = False

def _do_connect(state, ssid, password):
    set_status(state, f'connecting to {ssid}...', 30)
    ok, msg = connect_wifi(ssid, password)
    set_status(state, (f'connected: {ssid}' if ok else f'failed: {msg[:50]}'), 6)

def set_status(state, msg, secs):
    state['status'] = msg
    state['status_until'] = time.monotonic() + secs

# ---- main -------------------------------------------------------------------

def main():
    if not FB_DEV or not os.path.exists(FB_DEV):
        print('no LCD framebuffer detected; exiting.', file=sys.stderr); sys.exit(0)
    fb = FB(FB_DEV)
    touch_dev = find_touch()
    touch = Touch(touch_dev, W, H, matrix=load_calibration()) if touch_dev else None
    wlan = Rate(WAN_IFACE); eth = Rate(LAN_IFACE); ts = Rate(TS_IFACE)
    state = {
        'view': 'main', 'lan_ip': '-', 'ts_ip': '-', 'wifi': (None, 0),
        'leases': [], 'wlan_rate': wlan, 'eth_rate': eth, 'ts_rate': ts,
        'wifi_list': [], 'scanning': False, 'selected_ssid': None,
        'password_buf': '', 'password_mask': True, 'shift': False,
        'status': '', 'status_until': 0, 'touch': touch,
    }
    stop = {'flag': False}
    def _term(*_): stop['flag'] = True
    signal.signal(signal.SIGTERM, _term); signal.signal(signal.SIGINT, _term)

    last_slow = 0
    img = Image.new('RGB', (W, H), BG)
    while not stop['flag']:
        now = time.monotonic()
        wlan.tick(); eth.tick(); ts.tick()
        if now - last_slow >= 3:
            state['lan_ip'] = ip4(LAN_IFACE); state['ts_ip'] = tailscale_ip()
            state['wifi'] = wifi_status(); state['leases'] = read_leases()
            last_slow = now
        if touch:
            for kind, sx, sy, rx, ry in touch.poll():
                # Secret gesture: 5 taps within 2.5 s opens the calibration
                # wizard. Works from any view, so it's recoverable even when
                # touch mapping is badly wrong. Resets the running window on
                # entry so you don't accidentally re-trigger.
                tnow = time.monotonic()
                recent = state.setdefault('recent_taps', [])
                recent.append(tnow)
                cutoff = tnow - 2.5
                while recent and recent[0] < cutoff:
                    recent.pop(0)
                if len(recent) >= 5:
                    state['recent_taps'] = []
                    state['view'] = 'calibrate'
                    state['cal_idx'] = 0
                    state['cal_raw_pts'] = []
                    set_status(state, 'calibration — tap each crosshair', 4)
                    continue
                if state['view'] == 'main':
                    handle_main_tap(state, sx, sy)
                elif state['view'] == 'wifi':
                    handle_wifi_tap(state, sx, sy)
                elif state['view'] == 'kbd':
                    handle_kbd_tap(state, sx, sy)
                elif state['view'] == 'calibrate':
                    handle_calibration_tap(state, sx, sy, rx, ry)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, H], fill=BG)
        if state['view'] == 'main':
            draw_header(d, state); draw_bandwidth(d, state); draw_clients(d, state)
        elif state['view'] == 'wifi':
            draw_wifi_modal(d, state)
        elif state['view'] == 'kbd':
            draw_kbd(d, state)
        elif state['view'] == 'calibrate':
            draw_calibration(d, state)
        draw_status(d, state)
        fb.push(img)
        time.sleep(0.2)  # ~5 FPS
    fb.close()

if __name__ == '__main__':
    main()
