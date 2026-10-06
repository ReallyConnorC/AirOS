#!/usr/bin/env python3
"""Air OS system service.

Serves the TV interface and gives it access to the device: local media files,
volume, Wi-Fi and power. Listens on localhost only. Standard library only.
"""
import base64
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import zipfile
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import home  # lights, cameras and motion alerts (airos/home.py)

HERE = Path(__file__).resolve().parent
VERSION = (HERE / "VERSION").read_text().strip() if (HERE / "VERSION").is_file() else "dev"
try:  # online services this build uses (accounts, updates); see README
    SERVICES = json.loads((HERE / "services.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    SERVICES = {}
UPDATE_STATUS = Path("/var/lib/airos/update.json")  # written by updater.py
UI_DIR = HERE / "ui"
HOME = Path.home()
CONFIG_PATH = Path(os.environ.get("AIROS_CONFIG", HOME / ".config" / "airos" / "config.json"))
HOST = os.environ.get("AIROS_HOST", "127.0.0.1")
PORT = int(os.environ.get("AIROS_PORT", "8080"))
DEBUG = bool(os.environ.get("AIROS_DEBUG"))

VIDEO_EXT = {".mp4", ".m4v", ".webm", ".mkv", ".mov", ".ogv"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav"}
MIME_OVERRIDES = {".mkv": "video/webm", ".m4a": "audio/mp4", ".flac": "audio/flac", ".opus": "audio/ogg", ".js": "text/javascript"}
MAX_ITEMS = 5000
STARTED = time.time()
config_lock = threading.Lock()


# ---------- media ----------

def media_roots():
    roots = [HOME / "Videos", HOME / "Music", HOME / "Movies"]
    for base in (Path("/media"), Path("/mnt"), Path("/run/media")):  # USB drives
        if base.is_dir():
            roots.append(base)
    if os.environ.get("AIROS_MEDIA"):
        roots += [Path(p) for p in os.environ["AIROS_MEDIA"].split(os.pathsep)]
    return [r.resolve() for r in roots if r.is_dir()]


def token(path):
    return base64.urlsafe_b64encode(str(path).encode()).decode().rstrip("=")


def resolve_token(tok):
    """Map a media token back to a file, refusing anything outside the media folders."""
    try:
        path = Path(base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4)).decode()).resolve()
    except (ValueError, UnicodeDecodeError):
        return None
    if path.is_file() and any(path.is_relative_to(root) for root in media_roots()):
        return path
    return None


def scan(kind):
    exts = VIDEO_EXT if kind == "video" else AUDIO_EXT
    items, seen = [], set()
    for root in media_roots():
        for dirpath, dirnames, filenames in os.walk(root):
            depth = len(Path(dirpath).relative_to(root).parts)
            dirnames[:] = [] if depth >= 6 else sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if name.startswith(".") or path.suffix.lower() not in exts or path in seen:
                    continue
                seen.add(path)
                items.append({"title": re.sub(r"[._]+", " ", path.stem).strip(), "url": "/media/" + token(path),
                              "folder": Path(dirpath).name})
                if len(items) >= MAX_ITEMS:
                    return items
    return items


# ---------- device controls ----------

def run(cmd, timeout=15):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


WINDOWS_VOL = [50]  # Windows doesn't report its level without extra libraries; track the changes we make


def windows_volume_key(delta):
    import ctypes
    vk = 0xAF if delta > 0 else 0xAE  # VK_VOLUME_UP / VK_VOLUME_DOWN, 2% per press
    for _ in range(abs(delta) // 2 or 1):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)


def get_volume():
    if sys.platform == "win32":
        return WINDOWS_VOL[0]
    if shutil.which("wpctl"):
        m = re.search(r"([\d.]+)", run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"]).stdout)
        if m:
            return round(float(m.group(1)) * 100)
    if shutil.which("amixer"):
        m = re.search(r"\[(\d+)%\]", run(["amixer", "get", "Master"]).stdout)
        if m:
            return int(m.group(1))
    raise RuntimeError("No audio mixer found")


def set_volume(level):
    level = max(0, min(100, int(level)))
    if sys.platform == "win32":
        windows_volume_key(level - WINDOWS_VOL[0])
        WINDOWS_VOL[0] = level
        return level
    if shutil.which("wpctl"):
        run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{level}%"])
    else:
        run(["amixer", "-q", "set", "Master", f"{level}%"])
    return get_volume()


def has_audio_output():
    if not LINUX:
        return True
    try:
        return "no soundcards" not in Path("/proc/asound/cards").read_text()
    except OSError:
        return False


def battery():
    """Laptop battery as {level, charging}, or None on a device without one."""
    for bat in sorted(Path("/sys/class/power_supply").glob("BAT*")) if LINUX else []:
        try:
            status = (bat / "status").read_text().strip()
            return {"level": int((bat / "capacity").read_text()), "charging": status in ("Charging", "Full")}
        except (OSError, ValueError):
            pass
    return None


def audio_outputs():
    """Speakers and HDMI outputs the viewer can pick between (PipeWire, via pactl)."""
    sinks = json.loads(run(["pactl", "-f", "json", "list", "sinks"]).stdout or "[]")
    default = run(["pactl", "get-default-sink"]).stdout.strip()
    outs = [{"id": "sink:" + k["name"], "label": friendly_output(k.get("description") or k["name"]),
             "active": k["name"] == default} for k in sinks]
    # On many laptops HDMI and the speakers are separate sound-card profiles, so only one exists as an output
    # at a time; offer the other by switching profile.
    for card in json.loads(run(["pactl", "-f", "json", "list", "cards"]).stdout or "[]"):
        for kind in ("hdmi", "analog"):
            if any(kind in o["id"].lower() for o in outs):
                continue
            for name, prof in (card.get("profiles") or {}).items():
                if name.startswith("output:" + kind) and "input" not in name and prof.get("available", True):
                    outs.append({"id": f"profile:{card['name']}|{name}", "label": friendly_output(prof.get("description") or name),
                                 "active": False})
                    break
    return outs


def friendly_output(desc):
    d = desc.lower()
    if "hdmi" in d or "displayport" in d:
        return "TV (HDMI)"
    if "speaker" in d or "analog" in d or "built-in" in d:
        return "This device's speakers"
    if "headphone" in d:
        return "Headphones"
    return desc


def set_audio_output(out_id):
    kind, _, value = out_id.partition(":")
    if kind == "profile":
        card, _, profile = value.partition("|")
        run(["pactl", "set-card-profile", card, profile])
        time.sleep(0.5)  # the new sink appears a moment later
        sinks = json.loads(run(["pactl", "-f", "json", "list", "sinks"]).stdout or "[]")
        kind = "hdmi" if "hdmi" in profile else "analog"
        value = next((k["name"] for k in sinks if kind in k["name"].lower()), "")
    if not value:
        raise ValueError("That sound output isn't available")
    run(["pactl", "set-default-sink", value])
    for stream in json.loads(run(["pactl", "-f", "json", "list", "sink-inputs"]).stdout or "[]"):
        run(["pactl", "move-sink-input", str(stream["index"]), value])  # move what's playing now too
    return audio_outputs()


def get_brightness():
    out = run(["brightnessctl", "-m", "-c", "backlight"]).stdout.split(",")
    return int(out[3].rstrip("%")) if len(out) > 3 else None


def set_brightness(level):
    run(["brightnessctl", "-q", "-c", "backlight", "set", f"{max(5, min(100, int(level)))}%"])
    return get_brightness()


def get_balance():
    """Left/right balance of the current output, -100 (left) to 100 (right)."""
    vols = [int(v) for v in re.findall(r"(\d+)%", run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"]).stdout)][:2]
    if len(vols) < 2 or not max(vols):
        return 0
    left, right = vols
    return round((right - left) / max(vols) * 100)


def set_balance(value):
    value = max(-100, min(100, int(value)))
    level = get_volume()
    left = round(level * (1 - max(0, value) / 100))
    right = round(level * (1 - max(0, -value) / 100))
    run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{left}%", f"{right}%"])
    return get_balance()


def network_status():
    """How this device is online: by cable, by Wi-Fi (and which network), and its address."""
    wired = False
    for iface in Path("/sys/class/net").glob("*") if LINUX else []:
        if iface.name == "lo" or (iface / "wireless").exists() or not (iface / "device").exists():
            continue
        try:
            wired = wired or (iface / "carrier").read_text().strip() == "1"
        except OSError:
            pass
    ssid = None
    if LINUX and shutil.which("nmcli"):
        for line in run(["nmcli", "-t", "-f", "ACTIVE,SSID", "device", "wifi"]).stdout.splitlines():
            if line.startswith("yes:"):
                ssid = line[4:].replace("\\:", ":")
    return {"wired": wired, "wifi": ssid, "ip": local_ip()}


# ---------- Bluetooth (headphones, speakers, remotes, game controllers) ----------
MAC_RE = re.compile(r"[0-9A-F]{2}(?::[0-9A-F]{2}){5}")


def bt(*args, timeout=15):
    return run(["bluetoothctl", *args], timeout)


def bluetooth_devices(scan=False):
    bt("power", "on")
    if scan:
        bt("--timeout", "8", "scan", "on", timeout=20)
    paired = set(re.findall(r"Device (\S+)", bt("devices", "Paired").stdout))
    connected = set(re.findall(r"Device (\S+)", bt("devices", "Connected").stdout))
    devices = [{"mac": mac, "name": name.strip(), "paired": mac in paired, "connected": mac in connected}
               for mac, name in re.findall(r"Device (\S+) (.+)", bt("devices").stdout)
               if MAC_RE.fullmatch(mac) and not MAC_RE.fullmatch(name.strip().replace("-", ":"))]  # skip nameless ones
    return sorted(devices, key=lambda d: (not d["connected"], not d["paired"], d["name"].lower()))


def bluetooth_action(action, mac):
    if not MAC_RE.fullmatch(mac):
        raise ValueError("Unknown device")
    if action == "remove":
        bt("remove", mac)
    elif action == "disconnect":
        bt("disconnect", mac)
    elif action == "connect":
        if mac not in {d["mac"] for d in bluetooth_devices() if d["paired"]}:
            out = bt("--timeout", "25", "pair", mac, timeout=35).stdout
            if "Pairing successful" not in out and "AlreadyExists" not in out:
                raise RuntimeError("Couldn't pair. Put the device in pairing mode and try again.")
        bt("trust", mac)
        if "Connection successful" not in bt("--timeout", "15", "connect", mac, timeout=25).stdout:
            raise RuntimeError("Paired, but couldn't connect. Make sure the device is on and nearby.")
    else:
        raise ValueError("Unknown action")
    return bluetooth_devices()


def timezones():
    current = run(["timedatectl", "show", "-p", "Timezone", "--value"]).stdout.strip()
    return {"current": current, "zones": run(["timedatectl", "list-timezones"]).stdout.split()}


def set_timezone(zone):
    if zone not in timezones()["zones"]:
        raise ValueError("Unknown time zone")
    r = run(["timedatectl", "set-timezone", zone])
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or "Couldn't change the time zone")
    return timezones()["current"]


def has_wifi_adapter():
    if not (LINUX and shutil.which("nmcli")):
        return False
    return "wifi" in run(["nmcli", "-t", "-f", "TYPE", "device"]).stdout.split()


def wifi_list():
    out = run(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "auto"], 30).stdout
    nets = {}
    for line in out.splitlines():
        parts = re.split(r"(?<!\\):", line)
        if len(parts) < 4 or not parts[1]:
            continue
        ssid = parts[1].replace("\\:", ":")
        net = {"ssid": ssid, "signal": int(parts[2] or 0), "secure": parts[3] not in ("", "--"), "active": parts[0] == "*"}
        prev = nets.get(ssid)
        if not prev or net["active"] or (not prev["active"] and net["signal"] > prev["signal"]):
            nets[ssid] = net
    return sorted(nets.values(), key=lambda n: (not n["active"], -n["signal"]))


def wifi_connect(ssid, password):
    cmd = ["nmcli", "device", "wifi", "connect", ssid] + (["password", password] if password else [])
    r = run(cmd, 60)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip() or "Couldn't connect")


# ---------- external apps (YouTube, Netflix) ----------
# Each runs as its own fullscreen browser window on top of Air OS. Closing it reveals Air OS again.
TV_UA = ("Mozilla/5.0 (SMART-TV; Linux; Tizen 6.5) AppleWebKit/537.36 (KHTML, like Gecko) "
         "85.0.4183.93/6.5 TV Safari/537.36")  # YouTube only serves its TV interface to TV browsers
CROS_UA = ("Mozilla/5.0 (X11; CrOS aarch64 15359.58.0) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/140.0.0.0 Safari/537.36")  # Netflix only streams to Linux browsers it treats as ChromeOS
APPS = {
    "youtube": {"url": "https://www.youtube.com/tv", "ua": TV_UA},
    "netflix": {"url": "https://www.netflix.com/browse", "ua": CROS_UA},
}
# YouTube's TV interface is driven by the remote, so hide the mouse pointer while it's open.
HIDE_CURSOR_JS = ("(()=>{const s=document.createElement('style');"
                  "s.textContent='*,*::before,*::after{cursor:none!important}';"
                  "(document.head||document.documentElement).appendChild(s)})()")
HOME_KEYS = {102, 172}  # KEY_HOME (keyboards), KEY_HOMEPAGE (TV remotes)
VOLUME_KEYS = {115: 5, 13: 5, 78: 5, 114: -5, 12: -5, 74: -5, 113: 0}  # volume up/down/mute keys, + and - (also keypad)
app_process = None


LINUX = sys.platform.startswith("linux")


def browser_path():
    if sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
            for rel in (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe"):
                if base and Path(base, rel).is_file():
                    return str(Path(base, rel))
        return None
    if sys.platform == "darwin":
        mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        return str(mac) if mac.is_file() else None
    # Google Chrome includes Widevine (Netflix) on PCs; Chromium is used where Chrome doesn't exist (Raspberry Pi).
    return shutil.which("google-chrome-stable") or shutil.which("chromium") or shutil.which("chromium-browser")


def has_widevine():
    if not LINUX:
        return True  # Chrome and Edge on Windows/macOS ship with Widevine
    return (Path("/opt/google/chrome/WidevineCdm").is_dir() or Path("/opt/WidevineCdm").is_dir()
            or any(Path("/usr/lib").glob("chromium*/WidevineCdm")))


def app_features():
    ok = bool(browser_path()) and (not LINUX or bool(os.environ.get("WAYLAND_DISPLAY")))
    return {"youtube": ok, "netflix": ok and has_widevine()}


# --- Opening apps inside the Air OS window ---
# The Air OS browser runs with a local DevTools port. To open an app, we connect to it, give the page
# the app's browser identity (YouTube needs a TV one), and navigate it there. Home navigates back.
# The identity override lasts only while our DevTools connection is open, so we hold it for the app's lifetime.
DEVTOOLS_PORT = int(os.environ.get("AIROS_DEVTOOLS_PORT", "9222"))
AIROS_URL = f"http://127.0.0.1:{PORT}/"


class DevTools:
    """Minimal Chrome DevTools Protocol client (WebSocket, standard library only)."""

    def __init__(self, ws_url):
        u = urlparse(ws_url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise ConnectionError("DevTools closed the connection")
            head += chunk
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError("DevTools refused the connection")
        self.buf = head.split(b"\r\n\r\n", 1)[1]
        self.next_id = 0
        self.lock = threading.Lock()

    def _recv(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("DevTools closed the connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _read_message(self):
        b0, b1 = self._recv(2)
        length = b1 & 0x7F
        if length == 126:
            length = int.from_bytes(self._recv(2), "big")
        elif length == 127:
            length = int.from_bytes(self._recv(8), "big")
        payload = self._recv(length)
        return b0 & 0x0F, payload

    def call(self, method, params=None):
        with self.lock:
            self.next_id += 1
            msg = json.dumps({"id": self.next_id, "method": method, "params": params or {}}).encode()
            mask = os.urandom(4)
            n = len(msg)
            header = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + n.to_bytes(2, "big")
                                      if n < 65536 else bytes([0x80 | 127]) + n.to_bytes(8, "big"))
            self.sock.sendall(header + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(msg)))
            while True:  # skip events until our reply arrives
                opcode, payload = self._read_message()
                if opcode == 8:
                    raise ConnectionError("DevTools closed the connection")
                if opcode == 1:
                    reply = json.loads(payload)
                    if reply.get("id") == self.next_id:
                        if "error" in reply:
                            raise RuntimeError(reply["error"].get("message", "DevTools error"))
                        return reply.get("result", {})

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def airos_page():
    """The DevTools address of the Air OS window, or None if Air OS isn't running with DevTools."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{DEVTOOLS_PORT}/json", timeout=2) as r:
            targets = json.load(r)
    except (OSError, ValueError):
        return None
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    return pages[0]["webSocketDebuggerUrl"] if pages else None


app_session = None  # DevTools connection held while an app is open inside Air OS


def open_inside(name):
    global app_session
    ws = airos_page()
    if not ws:
        return False
    close_inside(navigate_home=False)
    session = DevTools(ws)
    if name in ("youtube", "store") or LINUX:  # desktop Chrome/Edge already satisfy Netflix as-is
        session.call("Emulation.setUserAgentOverride", {"userAgent": APPS[name]["ua"]})
    if name in ("youtube", "store"):
        session.call("Page.addScriptToEvaluateOnNewDocument", {"source": HIDE_CURSOR_JS})
    session.call("Page.navigate", {"url": APPS[name]["url"]})
    app_session = session
    threading.Thread(target=watch_return, args=(session,), daemon=True).start()
    return True


def close_inside(navigate_home=True):
    global app_session
    session, app_session = app_session, None
    if not session:
        return
    if navigate_home:
        try:
            session.call("Page.navigate", {"url": AIROS_URL + "?resume=1"})
        except (OSError, ConnectionError, RuntimeError):
            pass
    session.close()  # also drops the TV identity override


def watch_return(session):
    """Drop the app session if the viewer leaves the app on their own (e.g. YouTube's Exit)."""
    while app_session is session:
        time.sleep(2)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{DEVTOOLS_PORT}/json", timeout=2) as r:
                urls = [t.get("url", "") for t in json.load(r) if t.get("type") == "page"]
        except (OSError, ValueError):
            urls = []
        if not urls or any(u.startswith(AIROS_URL) for u in urls):
            if app_session is session:
                close_inside(navigate_home=False)


def launch_app(name, url=None, app_id=None):
    HOME_TOKEN[0] = None  # whatever was open before loses Home access
    if url:  # an app from the App Store: a TV website or an installed package, opened the same way as YouTube
        if not (re.match(r"^https://[^\s]+$", url) or url.startswith(APPS_URL)):
            raise ValueError("Apps must use https")
        if app_id and url.startswith(APPS_URL) and "home" in grants().get(app_id, []):
            HOME_TOKEN[0] = secrets.token_urlsafe(24)
            url += "#token=" + HOME_TOKEN[0]
        APPS["store"] = {"url": url, "ua": TV_UA}
        name = "store"
    elif name not in APPS or not app_features().get(name):
        raise ValueError(f"{name} isn't available on this device")
    if open_inside(name):
        return "inside"
    launch_window(name)
    return "window"


def go_home():
    close_inside()
    stop_app()


def launch_window(name):
    """Fallback when Air OS isn't running with DevTools: open the app as its own fullscreen window."""
    global app_process
    stop_app()
    app = APPS[name]
    profile = HOME / ".local" / "share" / "airos" / "apps" / name
    cmd = [browser_path(), "--app=" + app["url"], f"--user-data-dir={profile}",
           "--autoplay-policy=no-user-gesture-required", "--noerrdialogs", "--no-first-run",
           "--no-default-browser-check", "--disable-session-crashed-bubble", "--disable-features=Translate"]
    if LINUX:
        cmd += ["--kiosk", "--ozone-platform=wayland"]
    else:
        cmd += ["--start-fullscreen"]  # on a computer, F11 or Alt+F4 gets you out
    if name == "youtube" or LINUX:  # desktop Chrome/Edge already satisfy Netflix as-is
        cmd.append(f"--user-agent={app['ua']}")
    app_process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=LINUX)


def stop_app():
    global app_process
    if app_process and app_process.poll() is None and not LINUX:
        app_process.terminate()
    elif app_process and app_process.poll() is None:
        import signal
        try:
            os.killpg(app_process.pid, signal.SIGTERM)
            app_process.wait(5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            os.killpg(app_process.pid, signal.SIGKILL)
    app_process = None


def watch_home_key():
    """Close the running app when Home is pressed on any remote or keyboard.

    Apps like YouTube take over the keys, so this listens to the input devices directly."""
    import glob
    import select
    import struct
    fmt = "llHHi"  # struct input_event: timeval, type, code, value
    size = struct.calcsize(fmt)
    fds = {}
    while True:
        for path in glob.glob("/dev/input/event*"):
            if path not in fds.values():
                try:
                    fds[os.open(path, os.O_RDONLY | os.O_NONBLOCK)] = path
                except OSError:
                    pass
        ready, _, _ = select.select(list(fds), [], [], 5)  # timeout lets us pick up newly plugged remotes
        for fd in ready:
            try:
                data = os.read(fd, size * 64)
            except OSError:  # device unplugged
                os.close(fd)
                fds.pop(fd)
                continue
            for i in range(0, len(data) - size + 1, size):
                _, _, typ, code, value = struct.unpack_from(fmt, data, i)
                if typ == 1 and value == 1 and code in HOME_KEYS:  # EV_KEY press
                    go_home()
                elif typ == 1 and value in (1, 2) and code in VOLUME_KEYS and app_session:  # Air OS itself handles them otherwise
                    app_volume(VOLUME_KEYS[code])


def app_volume(delta):
    """Change the volume while an app is open, and show a small volume bar over the app."""
    try:
        level = (0 if get_volume() else 30) if delta == 0 else set_volume(get_volume() + delta)
        if delta == 0:
            set_volume(level)
        level = get_volume()
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return
    session = app_session
    if session:
        js = ("(()=>{let d=document.getElementById('__airos_vol');if(!d){d=document.createElement('div');d.id='__airos_vol';"
              "d.style.cssText='position:fixed;top:4vh;right:4vw;z-index:2147483647;background:#1c1d22ee;color:#fff;"
              "font:600 2.2vh system-ui,sans-serif;padding:1.4vh 2.4vh;border-radius:3vh;display:flex;align-items:center;gap:1.4vh;"
              "box-shadow:0 1vh 3vh #0008;transition:opacity .3s';document.documentElement.appendChild(d)}"
              f"d.innerHTML='<span>{'🔇' if level == 0 else '🔊'}</span><span style=\"width:16vh;height:.8vh;background:#fff4;border-radius:.4vh;overflow:hidden\">"
              f"<i style=\"display:block;height:100%;width:{level}%;background:#fff\"></i></span><span>{level}%</span>';"
              "d.style.opacity=1;clearTimeout(window.__airosVolT);window.__airosVolT=setTimeout(()=>d.style.opacity=0,1800)})()")
        try:
            session.call("Runtime.evaluate", {"expression": js})
        except (OSError, ConnectionError, RuntimeError):
            pass


def motion_over_app(ev):
    """Show a motion alert in the top-right corner of whatever app is open, with the camera's picture."""
    session = app_session
    if not session:
        return  # on the Air OS screens, the interface shows its own alert
    name = json.dumps(ev["name"])

    def show():
        for i in range(10):  # refresh the picture about once a second for 10 s
            if app_session is not session:
                return
            data = home.frame(ev["camera"])
            pic = json.dumps("data:image/jpeg;base64," + base64.b64encode(data).decode()) if data else '""'
            js = ("(()=>{let d=document.getElementById('__airos_motion');if(!d){d=document.createElement('div');d.id='__airos_motion';"
                  "d.style.cssText='position:fixed;top:3vh;right:2.5vw;z-index:2147483647;width:26vw;background:#1c1d22f2;color:#fff;"
                  "font:500 1.9vh system-ui,sans-serif;border-radius:2vh;overflow:hidden;box-shadow:0 2vh 5vh #000a;transition:opacity .4s,transform .4s';"
                  "d.innerHTML='<img style=\"display:block;width:100%;aspect-ratio:16/9;object-fit:cover;background:#000\"><div style=\"padding:1.4vh 1.8vh\">"
                  "<b style=\"display:block;font-size:2.1vh\"></b><span style=\"opacity:.7\">Motion detected</span></div>';document.documentElement.appendChild(d)}"
                  f"d.querySelector('b').textContent={name};if({pic})d.querySelector('img').src={pic};d.style.opacity=1;d.style.transform='none';"
                  f"clearTimeout(window.__airosMotT);window.__airosMotT=setTimeout(()=>{{d.style.opacity=0;d.style.transform='translateX(2vw)'}},{2500 if i == 9 else 2000})}})()")
            try:
                session.call("Runtime.evaluate", {"expression": js})
            except (OSError, ConnectionError, RuntimeError):
                return
            time.sleep(1)

    threading.Thread(target=show, daemon=True).start()


home.on_motion.append(motion_over_app)


def watch_home_key_windows():
    """Windows version: while an app is open, poll the keyboard for Home."""
    import ctypes
    user32 = ctypes.windll.user32
    VK_HOME, VK_BROWSER_HOME = 0x24, 0xAC
    while True:
        time.sleep(0.1)
        if not (app_session or (app_process and app_process.poll() is None)):
            continue
        if any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (VK_HOME, VK_BROWSER_HOME)):
            go_home()
            time.sleep(0.5)
        for vk, delta in ((0xBB, 5), (0x6B, 5), (0xBD, -5), (0x6D, -5)):  # + and - keys, also on the keypad
            if user32.GetAsyncKeyState(vk) & 0x8000:
                app_volume(delta)
                time.sleep(0.25)


def open_airos_window():
    """`server.py --open`: show Air OS fullscreen in Chrome/Edge (how you run it on a Windows or Mac computer)."""
    browser = browser_path()
    if not browser:
        print("Chrome or Edge not found; open", AIROS_URL, "in a browser instead")
        return
    profile = HOME / ".local" / "share" / "airos" / "browser"
    subprocess.Popen([browser, "--app=" + AIROS_URL, "--start-fullscreen", f"--user-data-dir={profile}",
                      f"--remote-debugging-port={DEVTOOLS_PORT}", "--autoplay-policy=no-user-gesture-required",
                      "--no-first-run", "--no-default-browser-check", "--disable-session-crashed-bubble"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # UDP connect sends nothing; it just picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return None


def info():
    linux = sys.platform.startswith("linux")
    return {
        "version": VERSION, "hostname": socket.gethostname(), "ip": local_ip(),
        "uptime": int(time.time() - STARTED), "platform": sys.platform, "battery": battery(),
        "network": network_status(),
        "features": {
            "volume": bool(sys.platform == "win32" or shutil.which("wpctl") or shutil.which("amixer")),
            "audio": has_audio_output(),
            "wifi": has_wifi_adapter(),
            "outputs": bool(linux and shutil.which("pactl")),
            "bluetooth": bool(linux and shutil.which("bluetoothctl") and any(Path("/sys/class/bluetooth").glob("hci*"))),
            "timezone": bool(linux and shutil.which("timedatectl")),
            "brightness": bool(linux and shutil.which("brightnessctl") and any(Path("/sys/class/backlight").glob("*"))),
            "accounts": accounts_configured(),
            "home": bool(home.load()["cameras"]),
            "updates": bool(linux and SERVICES.get("update_repo") and shutil.which("systemctl")),
            "power": bool(linux and shutil.which("systemctl")),
            "apps": app_features(),
        },
    }


def read_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_config(data, sync=True):
    with config_lock:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
    if sync:
        schedule_push()


# ---------- accounts ----------
# Air OS accounts live in a Supabase project (email + password sign-in). Signed-in TVs keep the
# viewer's theme, Live TV playlist and watch history in sync through the `profiles` table.
ACCOUNT_PATH = CONFIG_PATH.parent / "account.json"
SYNCED_KEYS = ("theme", "playlist", "history")
push_timer = None


class AccountError(ValueError):
    pass


def accounts_configured():
    return bool(SERVICES.get("supabase_url") and SERVICES.get("supabase_anon_key"))


def supabase(method, path, body=None, token=None, headers=None):
    if not accounts_configured():
        raise AccountError("Accounts aren't set up on this build of Air OS")
    key = SERVICES["supabase_anon_key"]
    req = urllib.request.Request(SERVICES["supabase_url"].rstrip("/") + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"apikey": key, "Authorization": "Bearer " + (token or key),
                                          "Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read())
            msg = j.get("msg") or j.get("error_description") or j.get("message") or j.get("error")
        except ValueError:
            msg = None
        raise AccountError(msg or f"Account server error ({e.code})") from None
    except OSError:
        raise AccountError("Can't reach the account server. Check the internet connection.") from None
    return json.loads(data) if data else None


def save_session(s):
    ACCOUNT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNT_PATH.write_text(json.dumps({
        "email": s["user"]["email"], "user_id": s["user"]["id"], "access_token": s["access_token"],
        "refresh_token": s["refresh_token"], "expires_at": time.time() + s.get("expires_in", 3600) - 60}), encoding="utf-8")
    os.chmod(ACCOUNT_PATH, 0o600)


def account_session():
    """The signed-in account with a fresh access token, or None."""
    try:
        s = json.loads(ACCOUNT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() > s["expires_at"]:
        save_session(supabase("POST", "/auth/v1/token?grant_type=refresh_token", {"refresh_token": s["refresh_token"]}))
        s = json.loads(ACCOUNT_PATH.read_text(encoding="utf-8"))
    return s


def pull_settings(s):
    """After sign-in: take the account's saved settings, or upload this TV's if the account has none."""
    rows = supabase("GET", f"/rest/v1/profiles?select=settings&id=eq.{s['user_id']}", token=s["access_token"])
    if rows and rows[0].get("settings"):
        cfg = read_config()
        cfg.update({k: v for k, v in rows[0]["settings"].items() if k in SYNCED_KEYS})
        write_config(cfg, sync=False)
    else:
        push_settings()


def push_settings():
    try:
        s = account_session()
        if not s:
            return
        cfg = read_config()
        supabase("POST", "/rest/v1/profiles", {"id": s["user_id"], "settings": {k: cfg[k] for k in SYNCED_KEYS if k in cfg},
                                                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                 token=s["access_token"], headers={"Prefer": "resolution=merge-duplicates"})
    except AccountError as e:
        if DEBUG:
            print("sync failed:", e)


def schedule_push():
    """Upload settings 30 s after the last change (watch progress saves every few seconds)."""
    global push_timer
    if not ACCOUNT_PATH.is_file():
        return
    if push_timer:
        push_timer.cancel()
    push_timer = threading.Timer(30, push_settings)
    push_timer.daemon = True
    push_timer.start()


def account_action(action, data):
    if action == "logout":
        s = None
        try:
            s = account_session()
        except AccountError:
            pass
        if s:
            try:
                supabase("POST", "/auth/v1/logout", {}, token=s["access_token"])
            except AccountError:
                pass
        ACCOUNT_PATH.unlink(missing_ok=True)
        return {"account": None}
    email = str(data.get("email", "")).strip()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise AccountError("Enter a valid email address")
    if action == "recover":
        supabase("POST", "/auth/v1/recover", {"email": email})
        return {"ok": True}
    password = str(data.get("password", ""))
    if action == "signup":
        r = supabase("POST", "/auth/v1/signup", {"email": email, "password": password})
        if not r.get("access_token"):
            return {"confirm": True}  # the project requires email confirmation first
    elif action == "login":
        r = supabase("POST", "/auth/v1/token?grant_type=password", {"email": email, "password": password})
    else:
        raise AccountError("Unknown account action")
    save_session(r)
    pull_settings(account_session())
    return {"account": {"email": r["user"]["email"]}}


# ---------- signing in from a phone ----------
# The TV shows a QR code for the Air OS sign-in page on the web (services.json "pair_page") with a one-time code.
# The phone signs in there and hands its session to that code in Supabase; the TV collects it. Codes last 10 minutes.
PAIR_PAGE = SERVICES.get("pair_page", "https://reallyconnorc.github.io/AirOS/link/")


def start_pairing():
    code = secrets.token_urlsafe(18)
    supabase("POST", "/rest/v1/rpc/create_pair", {"p_code": code})
    return {"code": code, "url": f"{PAIR_PAGE}?c={code}"}


def pairing_status(code):
    token = supabase("POST", "/rest/v1/rpc/claim_pair", {"p_code": code})
    if not token:
        return {"state": "waiting"}
    save_session(supabase("POST", "/auth/v1/token?grant_type=refresh_token", {"refresh_token": token}))
    s = account_session()
    pull_settings(s)
    return {"state": "done", "account": {"email": s["email"]}}


def factory_reset():
    try:
        account_action("logout", {})
    except AccountError:
        pass
    CONFIG_PATH.unlink(missing_ok=True)
    if LINUX and shutil.which("nmcli"):
        for line in run(["nmcli", "-t", "-f", "UUID,TYPE", "connection", "show"]).stdout.splitlines():
            uuid, _, kind = line.partition(":")
            if kind == "802-11-wireless":
                run(["nmcli", "connection", "delete", "uuid", uuid])


# ---------- App Store apps (.atv) ----------
# An .atv file is a zip: manifest.json (id, name, version, icon, and either "start": a page inside the package, or
# "url": a website) plus the app's files. Packaged apps are served from their own port, a different web origin from
# Air OS, so they can't reach the Air OS service (Wi-Fi, power, account). Installed apps update themselves.
APPS_DIR = HOME / ".local" / "share" / "airos" / "apps"
APPS_PORT = int(os.environ.get("AIROS_APPS_PORT", "8081"))
APPS_URL = f"http://127.0.0.1:{APPS_PORT}/"
STORE_JSON = "https://raw.githubusercontent.com/ReallyConnorC/AirOS/main/store/apps.json"  # website-only apps
MAX_PACKAGE = 50 * 1024 * 1024
ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{2,63}$")


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v)))


def store_catalog():
    apps = []
    if accounts_configured():
        try:
            rows = supabase("GET", "/rest/v1/store_apps?select=id,name,description,version,color,icon_url,package_url&order=name")
            apps += [{"id": r["id"], "name": r["name"], "description": r["description"], "version": r["version"],
                      "color": r.get("color"), "icon": r.get("icon_url"), "package": r["package_url"]} for r in rows or []]
        except AccountError:
            pass
    try:
        listed = json.loads(fetch_text(STORE_JSON)).get("apps", [])
        apps += [a for a in listed if isinstance(a, dict) and a.get("id") and str(a.get("url", "")).startswith("https://")]
    except (OSError, ValueError):
        pass
    for a in apps:
        a["installed"] = installed_version(a["id"])
    return apps


def installed_manifest(app_id):
    try:
        return json.loads((APPS_DIR / app_id / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def installed_version(app_id):
    m = installed_manifest(app_id) if ID_RE.match(str(app_id)) else None
    return m.get("version") if m else None


def launch_info(app_id):
    """What the TV needs to show and open an installed package."""
    m = installed_manifest(app_id)
    if not m:
        raise ValueError("That app isn't installed")
    url = m["url"] if m.get("url") else f"{APPS_URL}{app_id}/{m.get('start', 'index.html')}"
    icon = f"{APPS_URL}{app_id}/{m['icon']}" if m.get("icon") else None
    return {"id": app_id, "name": m.get("name", app_id), "version": m.get("version"), "url": url, "icon": icon,
            "color": m.get("color"), "description": m.get("description", ""),
            "permissions": [{"id": p, "text": PERMISSIONS[p]} for p in requested_permissions(app_id)]}


def install_package(app_id, package_url):
    if not ID_RE.match(app_id):
        raise ValueError("Invalid app ID")
    base = SERVICES.get("supabase_url", "").rstrip("/") + "/storage/v1/object/public/apps/"
    if not package_url.startswith(base):
        raise ValueError("Apps can only come from the Air OS App Store")
    req = urllib.request.Request(package_url, headers={"User-Agent": f"AirOS/{VERSION}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read(MAX_PACKAGE + 1)
    if len(data) > MAX_PACKAGE:
        raise ValueError("App is too large")
    with zipfile.ZipFile(BytesIO(data)) as z:
        manifest = json.loads(z.read("manifest.json"))
        if manifest.get("id") != app_id:
            raise ValueError("The app file doesn't match its store listing")
        tmp = APPS_DIR / f".{app_id}.new"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            for info in z.infolist():
                target = (tmp / info.filename).resolve()
                if not target.is_relative_to(tmp.resolve()):  # refuse ../ tricks in the zip
                    raise ValueError("Unsafe file in app package")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(z.read(info))
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    final = APPS_DIR / app_id
    shutil.rmtree(final, ignore_errors=True)
    os.replace(tmp, final)
    return launch_info(app_id)


def remove_package(app_id):
    if ID_RE.match(app_id):
        shutil.rmtree(APPS_DIR / app_id, ignore_errors=True)
        set_grant(app_id, "home", False)


# Apps ask for extra powers in their manifest ("permissions": ["home"]); the viewer allows them on the TV.
GRANTS_PATH = CONFIG_PATH.parent / "permissions.json"
PERMISSIONS = {"home": "control your lights and see your cameras"}
HOME_TOKEN = [None]  # given only to the Home-permitted app that is open right now


def grants():
    try:
        return json.loads(GRANTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def set_grant(app_id, perm, allow):
    g = grants()
    perms = set(g.get(app_id, []))
    perms.add(perm) if allow else perms.discard(perm)
    g[app_id] = sorted(perms)
    GRANTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    GRANTS_PATH.write_text(json.dumps(g), encoding="utf-8")


def requested_permissions(app_id):
    m = installed_manifest(app_id) or {}
    return [p for p in m.get("permissions", []) if p in PERMISSIONS]


def update_apps_forever():
    """Keep installed apps on the newest version, with nothing for the viewer to do."""
    time.sleep(60)
    while True:
        try:
            for a in store_catalog():
                if a.get("package") and a["installed"] and version_tuple(a["version"]) > version_tuple(a["installed"]):
                    install_package(a["id"], a["package"])
                    print(f"Updated app {a['id']} to {a['version']}", flush=True)
        except Exception as e:  # offline, store unreachable, bad package: try again later
            if DEBUG:
                print("app update check failed:", e)
        time.sleep(6 * 3600)


class AppFilesHandler(SimpleHTTPRequestHandler):
    """Serves installed packaged apps on their own origin."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(APPS_DIR), **kwargs)

    def log_message(self, fmt, *args):
        if DEBUG:
            super().log_message(fmt, *args)

    def list_directory(self, path):
        self.send_error(404)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_json(self, data, status=200):
        return Handler.send_json(self, data, status)

    def home_allowed(self):
        given = self.headers.get("X-Air-Token") or parse_qs(urlparse(self.path).query).get("t", [""])[0]
        return bool(HOME_TOKEN[0]) and secrets.compare_digest(given, HOME_TOKEN[0])

    def do_GET(self):
        if self.path.startswith("/_home/"):
            return self.home_api("GET")
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith("/_home/"):
            return self.home_api("POST")
        self.send_error(405)

    def home_api(self, method):
        reply = self.send_json
        if not self.home_allowed():
            return reply({"error": "This app isn't allowed to use Home"}, 403)
        name = urlparse(self.path).path[len("/_home/"):]
        try:
            data = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(min(length, 65536)) or b"{}")
            if name == "state":
                return reply(home.state())
            if name == "devices":
                return reply({"devices": home.device_states()})
            if name.startswith("cam/"):
                return Handler.send_camera(self, name[4:])
            if method != "POST":
                return reply({"error": "Not found"}, 404)
            if name == "tapo/login":
                home.tapo_login(str(data["email"]), str(data["password"]))
                return reply(home.state())
            if name == "tapo/scan":
                return reply({"added": home.scan(local_ip())})
            if name == "tapo/add":
                return reply(home.add_device(str(data["ip"])))
            if name == "tapo/set":
                return reply(home.set_device(str(data["id"]), data.get("on"), data.get("brightness")))
            if name == "tapo/remove":
                home.remove_device(str(data["id"]))
                return reply({"ok": True})
            if name == "camera/add":
                url = str(data.get("url") or "") or home.camera_url(str(data["ip"]), str(data["user"]), str(data["password"]))
                return reply(home.add_camera(str(data.get("name", "")), url))
            if name == "camera/remove":
                home.remove_camera(str(data["id"]))
                return reply({"ok": True})
            if name == "camera/set":
                home.set_camera(str(data["id"]), **{k: v for k, v in data.items() if k in ("name", "alerts")})
                return reply({"ok": True})
            if name == "alerts":
                cfg = home.load()
                cfg["alerts"] = bool(data.get("on"))
                home.save(cfg)
                return reply({"ok": True})
            return reply({"error": "Not found"}, 404)
        except (home.TapoError, ValueError, KeyError) as e:
            return reply({"error": str(e)}, 400)
        except Exception as e:
            return reply({"error": str(e)}, 500)


def serve_apps():
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", APPS_PORT), AppFilesHandler)
    server.daemon_threads = True
    server.serve_forever()


def update_status():
    try:
        st = json.loads(UPDATE_STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    return {"current": VERSION, **st}


def fetch_text(url, limit=10 * 1024 * 1024):
    """Download an IPTV playlist on behalf of the UI (avoids browser CORS limits)."""
    if not re.match(r"^https?://", url):
        raise ValueError("Only http(s) URLs are allowed")
    req = urllib.request.Request(url, headers={"User-Agent": f"AirOS/{VERSION}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Playlist is too large")
    return data.decode("utf-8", "replace")


# ---------- HTTP ----------

class Handler(SimpleHTTPRequestHandler):
    server_version = "AirOS/" + VERSION
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".js": "text/javascript", ".html": "text/html"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(UI_DIR), **kwargs)

    def log_message(self, fmt, *args):
        if DEBUG:
            super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def local_request(self):
        # Blocks DNS-rebinding: only accept requests addressed to this machine by loopback name.
        host = self.headers.get("Host", "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost", "[::1]")

    def do_GET(self):
        url = urlparse(self.path)
        if url.path.startswith("/api/"):
            return self.api("GET", url)
        if url.path.startswith("/media/"):
            return self.send_media(url.path[len("/media/"):])
        return super().do_GET()

    def do_POST(self):
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            return self.send_json({"error": "Not found"}, 404)
        # A JSON content type forces a CORS preflight, so other websites can't post here.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send_json({"error": "JSON required"}, 415)
        return self.api("POST", url)

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1024 * 1024:
            raise ValueError("Request too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def api(self, method, url):
        if not self.local_request():
            return self.send_json({"error": "Forbidden"}, 403)
        name, query = url.path[len("/api/"):], parse_qs(url.query)
        try:
            if name == "info":
                return self.send_json(info())
            if name == "config":
                if method == "POST":
                    data = self.body()
                    if not isinstance(data, dict):
                        raise ValueError("Config must be an object")
                    write_config(data)
                    return self.send_json({"ok": True})
                return self.send_json(read_config())
            if name == "account":
                s = account_session() if accounts_configured() else None
                return self.send_json({"account": {"email": s["email"]} if s else None})
            if name == "account/pair":
                if method == "POST":
                    return self.send_json(start_pairing())
                return self.send_json(pairing_status(query.get("code", [""])[0]))
            if name.startswith("account/") and method == "POST":
                return self.send_json(account_action(name[len("account/"):], self.body()))
            if name == "update":
                if method == "POST":
                    run(["systemctl", "start", "--no-block", "airos-update.service"])
                    return self.send_json({**update_status(), "state": "checking"})
                return self.send_json(update_status())
            if name == "media":
                kind = query.get("kind", ["video"])[0]
                return self.send_json({"items": scan("audio" if kind == "audio" else "video")})
            if name == "volume":
                if method == "POST":
                    data = self.body()
                    level = data["level"] if "level" in data else get_volume() + int(data.get("delta", 0))
                    return self.send_json({"level": set_volume(level)})
                return self.send_json({"level": get_volume()})
            if name == "audio":
                if method == "POST":
                    return self.send_json({"outputs": set_audio_output(str(self.body()["id"]))})
                return self.send_json({"outputs": audio_outputs()})
            if name == "balance" and method == "POST":
                return self.send_json({"value": set_balance(self.body()["value"])})
            if name == "balance":
                return self.send_json({"value": get_balance()})
            if name == "bluetooth":
                if method == "POST":
                    data = self.body()
                    return self.send_json({"devices": bluetooth_action(str(data.get("action")), str(data.get("mac")))})
                return self.send_json({"devices": bluetooth_devices(scan=query.get("scan") == ["1"])})
            if name == "timezone":
                if method == "POST":
                    return self.send_json({"current": set_timezone(str(self.body()["zone"]))})
                return self.send_json(timezones())
            if name == "brightness":
                if method == "POST":
                    return self.send_json({"level": set_brightness(self.body()["level"])})
                return self.send_json({"level": get_brightness()})
            if name == "wifi":
                if method == "POST":
                    data = self.body()
                    wifi_connect(str(data["ssid"]), str(data.get("password") or ""))
                    return self.send_json({"ok": True})
                return self.send_json({"networks": wifi_list()})
            if name == "power" and method == "POST":
                action = self.body().get("action")
                if action not in ("reboot", "poweroff"):
                    raise ValueError("Unknown power action")
                self.send_json({"ok": True})
                subprocess.Popen(["systemctl", action])
                return
            if name == "store":
                return self.send_json({"apps": store_catalog()})
            if name == "store/install" and method == "POST":
                data = self.body()
                return self.send_json(install_package(str(data["id"]), str(data["package"])))
            if name == "store/grant" and method == "POST":
                data = self.body()
                if data.get("perm") not in PERMISSIONS:
                    raise ValueError("Unknown permission")
                set_grant(str(data["id"]), data["perm"], bool(data.get("allow")))
                return self.send_json({"ok": True})
            if name == "home/events":
                since = float(query.get("since", ["0"])[0] or 0)
                return self.send_json({"events": [e for e in home.events if e["at"] > since], "now": time.time()})
            if name.startswith("home/cam/"):
                return self.send_camera(name[len("home/cam/"):])
            if name == "store/remove" and method == "POST":
                remove_package(str(self.body()["id"]))
                return self.send_json({"ok": True})
            if name == "store/app":
                return self.send_json(launch_info(query.get("id", [""])[0]))
            if name == "reset" and method == "POST":
                factory_reset()
                return self.send_json({"ok": True})
            if name == "launch" and method == "POST":
                data = self.body()
                return self.send_json({"mode": launch_app(str(data.get("app")), data.get("url"), data.get("id"))})
            if name == "home" and method == "POST":
                go_home()
                return self.send_json({"ok": True})
            if name == "fetch":
                body = fetch_text(query.get("url", [""])[0]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return self.send_json({"error": "Not found"}, 404)
        except (ValueError, KeyError) as e:
            return self.send_json({"error": str(e)}, 400)
        except Exception as e:  # device command failures surface to the UI as a message
            return self.send_json({"error": str(e)}, 500)

    def send_camera(self, rest):
        cam_id, _, kind = rest.partition("/")
        if not re.fullmatch(r"[0-9a-f]{8}", cam_id):
            return self.send_json({"error": "Unknown camera"}, 404)
        if kind == "snap":
            data = home.frame(cam_id)
            if not data:
                return self.send_json({"error": "No picture yet"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            home.mjpeg(self.wfile.write, cam_id)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def send_media(self, tok):
        path = resolve_token(tok)
        if not path:
            return self.send_json({"error": "Not found"}, 404)
        size = path.stat().st_size
        ctype = MIME_OVERRIDES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:
                start = max(0, size - int(m.group(2)))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        remaining = end - start + 1
        try:
            with open(path, "rb") as f:
                f.seek(start)
                while remaining > 0:
                    chunk = f.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the player seeked or closed; normal for video


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    if LINUX and Path("/dev/input").is_dir():
        threading.Thread(target=watch_home_key, daemon=True).start()
    elif sys.platform == "win32":
        threading.Thread(target=watch_home_key_windows, daemon=True).start()
    threading.Thread(target=serve_apps, daemon=True).start()
    home.start_cameras()
    threading.Thread(target=update_apps_forever, daemon=True).start()
    if "--open" in sys.argv:
        open_airos_window()
    print(f"Air OS {VERSION} on http://{HOST}:{PORT}  (media: {', '.join(map(str, media_roots())) or 'none'})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
