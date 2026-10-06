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
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

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


def get_volume():
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


def launch_app(name, url=None):
    if url:  # an app from the App Store: a TV website, opened the same way as YouTube
        if not re.match(r"^https://[^\s]+$", url):
            raise ValueError("Apps must use https")
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
            "volume": bool(shutil.which("wpctl") or shutil.which("amixer")),
            "audio": has_audio_output(),
            "wifi": has_wifi_adapter(),
            "outputs": bool(linux and shutil.which("pactl")),
            "bluetooth": bool(linux and shutil.which("bluetoothctl") and any(Path("/sys/class/bluetooth").glob("hci*"))),
            "timezone": bool(linux and shutil.which("timedatectl")),
            "brightness": bool(linux and shutil.which("brightnessctl") and any(Path("/sys/class/backlight").glob("*"))),
            "accounts": accounts_configured(),
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
# The TV shows a QR code with a one-time link. The phone opens a small sign-in page that the TV serves on the
# home network (PAIR_PORT), and the TV signs in with what the viewer types there. Links expire after 10 minutes.
PAIR_PORT = int(os.environ.get("AIROS_PAIR_PORT", "8090"))
pairing = {}  # code -> {"expires", "attempts", "email"}
pair_server = None
PAIR_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in to Air OS</title><style>
body{margin:0;font-family:system-ui,sans-serif;background:#0b0c10;color:#fff;display:flex;justify-content:center}
main{width:100%;max-width:420px;padding:40px 22px}h1{font-size:28px;margin:18px 0 6px}p{color:#ffffff99;line-height:1.45;margin:0 0 22px}
.tabs{display:flex;background:#ffffff14;border-radius:12px;padding:4px;margin-bottom:22px}.tabs button{flex:1;padding:10px;border:0;border-radius:9px;background:none;color:#fff;font-size:15px;font-weight:600}
.tabs button.on{background:#fff;color:#000}input{width:100%;box-sizing:border-box;padding:15px;margin-bottom:12px;border-radius:12px;border:1px solid #ffffff26;background:#ffffff10;color:#fff;font-size:17px}
.go{width:100%;padding:16px;border:0;border-radius:12px;background:#4f8cff;color:#fff;font-size:17px;font-weight:700;margin-top:6px}
#msg{margin-top:16px;min-height:22px;color:#ffb4a8}#msg.ok{color:#7ee2a8}a{color:#8fb4ff}
</style></head><body><main>
<svg viewBox="0 0 64 64" width="56" height="56"><circle cx="32" cy="32" r="29" fill="none" stroke="#4f8cff" stroke-width="4"/><path d="M18 46 L32 16 L46 46" fill="none" stroke="#4f8cff" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/><path d="M22 36 Q32 30 42 36" fill="none" stroke="#fff" stroke-width="3.5" stroke-linecap="round"/></svg>
<h1>Air OS account</h1><p>Sign in or create an account. <b>__TV__</b> signs in by itself when you're done.</p>
<div class="tabs"><button id="t-new" class="on" type="button">Create account</button><button id="t-in" type="button">Sign in</button></div>
<form id="f"><input id="email" type="email" autocomplete="email" placeholder="Email" required>
<input id="pw" type="password" autocomplete="new-password" placeholder="Password (6+ characters)" minlength="6" required>
<input id="pw2" type="password" autocomplete="new-password" placeholder="Type the password again" minlength="6">
<button class="go" id="go">Create account</button></form>
<p style="margin-top:14px"><a href="#" id="forgot">Forgot password?</a></p><div id="msg"></div></main>
<script>
let mode='signup';const $=id=>document.getElementById(id),msg=(t,ok)=>{$('msg').textContent=t;$('msg').className=ok?'ok':''};
function tab(m){mode=m;$('t-new').className=m==='signup'?'on':'';$('t-in').className=m==='login'?'on':'';
 $('pw2').style.display=m==='signup'?'':'none';$('pw2').required=m==='signup';$('go').textContent=m==='signup'?'Create account':'Sign in';
 $('pw').autocomplete=m==='signup'?'new-password':'current-password';msg('')}
$('t-new').onclick=()=>tab('signup');$('t-in').onclick=()=>tab('login');
async function send(action,body){const r=await fetch(location.pathname,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,...body})});return r.json()}
$('f').onsubmit=async e=>{e.preventDefault();
 if(mode==='signup'&&$('pw').value!==$('pw2').value)return msg("The passwords don't match");
 $('go').disabled=true;msg('Please wait…',true);
 try{const r=await send(mode,{email:$('email').value,password:$('pw').value});
  if(r.error)msg(r.error);else if(r.confirm){msg('Check your email for a link to confirm your account, then come back and choose Sign in.',true);tab('login')}
  else{msg('Done! Your TV is now signed in. You can close this page.',true);$('f').style.display='none'}}
 catch{msg("Couldn't reach your TV. Make sure your phone is on the same Wi-Fi.")}
 $('go').disabled=false};
$('forgot').onclick=async e=>{e.preventDefault();if(!$('email').value)return msg('Type your email first');
 const r=await send('recover',{email:$('email').value});msg(r.error||'Password reset link sent to '+$('email').value,!r.error)};
</script></body></html>"""


def start_pairing():
    global pair_server
    if not accounts_configured():
        raise AccountError("Accounts aren't set up on this build of Air OS")
    ip = local_ip()
    if not ip:
        raise AccountError("Connect to the internet first")
    if pair_server is None:
        pair_server = ThreadingHTTPServer(("0.0.0.0", PAIR_PORT), PairHandler)
        pair_server.daemon_threads = True
        threading.Thread(target=pair_server.serve_forever, daemon=True).start()
    now = time.time()
    for c in [c for c, v in pairing.items() if v["expires"] < now]:
        del pairing[c]
    code = secrets.token_urlsafe(9)
    pairing[code] = {"expires": now + 600, "attempts": 0, "email": None}
    return {"code": code, "url": f"http://{ip}:{PAIR_PORT}/link/{code}"}


def pairing_status(code):
    p = pairing.get(code)
    if not p or p["expires"] < time.time():
        return {"state": "expired"}
    return {"state": "done", "account": {"email": p["email"]}} if p["email"] else {"state": "waiting"}


class PairHandler(SimpleHTTPRequestHandler):
    """The phone sign-in page. Serves nothing but /link/<code> while that code is valid."""
    server_version = "AirOS/" + VERSION

    def log_message(self, fmt, *args):
        if DEBUG:
            super().log_message(fmt, *args)

    def pair(self):
        code = urlparse(self.path).path.removeprefix("/link/")
        p = pairing.get(code)
        return (code, p) if p and p["expires"] > time.time() and not p["email"] else (code, None)

    def reply(self, status, body, ctype):
        body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        _, p = self.pair()
        if not p:
            return self.reply(404, "<h1>This link has expired</h1><p>Show a new QR code on your TV.</p>", "text/html; charset=utf-8")
        name = read_config().get("name") or "Your TV"
        self.reply(200, PAIR_PAGE.replace("__TV__", name.replace("&", "&amp;").replace("<", "&lt;")), "text/html; charset=utf-8")

    def do_POST(self):
        code, p = self.pair()
        if not p:
            return self.reply(404, json.dumps({"error": "This link has expired. Show a new QR code on your TV."}), "application/json")
        p["attempts"] += 1
        if p["attempts"] > 15:
            return self.reply(429, json.dumps({"error": "Too many tries. Show a new QR code on your TV."}), "application/json")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(min(length, 4096)) or b"{}")
            action = data.get("action")
            if action not in ("signup", "login", "recover"):
                raise AccountError("Unknown action")
            result = account_action(action, data)
            if result.get("account"):
                p["email"] = result["account"]["email"]
            self.reply(200, json.dumps(result), "application/json")
        except (AccountError, ValueError) as e:
            self.reply(400, json.dumps({"error": str(e)}), "application/json")


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
            if name == "reset" and method == "POST":
                factory_reset()
                return self.send_json({"ok": True})
            if name == "launch" and method == "POST":
                data = self.body()
                return self.send_json({"mode": launch_app(str(data.get("app")), data.get("url"))})
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
    if "--open" in sys.argv:
        open_airos_window()
    print(f"Air OS {VERSION} on http://{HOST}:{PORT}  (media: {', '.join(map(str, media_roots())) or 'none'})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
