"""Air OS Home: TP-Link Tapo lights and plugs, RTSP cameras, and motion alerts.

Tapo devices are controlled on the home network with their local KLAP protocol (needs the Tapo account email and
password, which never leave the TV). Cameras are read with ffmpeg: it keeps the newest frame as a JPEG for previews,
and feeds tiny greyscale frames to a simple motion detector.
"""
import base64
import hashlib
import ipaddress
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:  # Tapo needs python3-cryptography (installed by install.sh)
    Cipher = None

CONFIG = Path.home() / ".config" / "airos" / "home.json"
FRAMES = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()) / "airos-cams"
MOTION_W, MOTION_H = 64, 36
lock = threading.Lock()
events = []           # recent motion events: {"id", "camera", "name", "at"}
on_motion = []        # callbacks(event), set by server.py (for alerts over other apps)


def load():
    try:
        return {"tapo": {}, "devices": [], "cameras": [], "alerts": True, **json.loads(CONFIG.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return {"tapo": {}, "devices": [], "cameras": [], "alerts": True}


def save(cfg):
    with lock:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)  # holds the Tapo and camera passwords
        os.replace(tmp, CONFIG)


def features():
    return {"tapo": Cipher is not None, "cameras": bool(shutil.which("ffmpeg"))}


# ---------- Tapo (KLAP protocol) ----------

class TapoError(Exception):
    pass


class Tapo:
    """One Tapo bulb or plug, spoken to directly over the home network."""

    def __init__(self, ip, email, password):
        if Cipher is None:
            raise TapoError("Tapo support isn't installed on this TV")
        self.ip = ip
        self.auth = hashlib.sha256(hashlib.sha1(email.encode()).digest() + hashlib.sha1(password.encode()).digest()).digest()
        self.cookie = None

    def _post(self, path, data, timeout=5):
        req = urllib.request.Request(f"http://{self.ip}/app/{path}", data=data, method="POST",
                                     headers={"Cookie": self.cookie} if self.cookie else {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            cookie = r.headers.get("Set-Cookie")
        return body, cookie

    def login(self):
        local = secrets.token_bytes(16)
        try:
            body, cookie = self._post("handshake1", local)
        except OSError as e:
            raise TapoError(f"Can't reach the device at {self.ip}") from e
        remote, server_hash = body[:16], body[16:48]
        if hashlib.sha256(local + remote + self.auth).digest() != server_hash:
            raise TapoError("The Tapo email or password is wrong")
        self.cookie = cookie.split(";")[0] if cookie else None
        self._post("handshake2", hashlib.sha256(remote + local + self.auth).digest())
        seeds = local + remote + self.auth
        self.key = hashlib.sha256(b"lsk" + seeds).digest()[:16]
        full_iv = hashlib.sha256(b"iv" + seeds).digest()
        self.iv, self.seq = full_iv[:12], int.from_bytes(full_iv[-4:], "big", signed=True)
        self.sig = hashlib.sha256(b"ldk" + seeds).digest()[:28]

    def request(self, method, params=None):
        if not self.cookie:
            self.login()
        self.seq += 1
        seq = self.seq.to_bytes(4, "big", signed=True)
        cipher = Cipher(algorithms.AES(self.key), modes.CBC(self.iv + seq))
        padder = padding.PKCS7(128).padder()
        plain = padder.update(json.dumps({"method": method, **({"params": params} if params else {})}).encode()) + padder.finalize()
        enc = cipher.encryptor()
        data = enc.update(plain) + enc.finalize()
        try:
            body, _ = self._post(f"request?seq={self.seq}", hashlib.sha256(self.sig + seq + data).digest() + data)
        except OSError:
            self.cookie = None  # session expired; log in again next time
            raise TapoError(f"{self.ip} didn't answer")
        dec = cipher.decryptor()
        unpadder = padding.PKCS7(128).unpadder()
        reply = json.loads(unpadder.update(dec.update(body[32:]) + dec.finalize()) + unpadder.finalize())
        if reply.get("error_code"):
            raise TapoError(f"The device said no (error {reply['error_code']})")
        return reply.get("result", {})


clients = {}


def tapo(ip):
    cfg = load()["tapo"]
    if not cfg.get("email"):
        raise TapoError("Sign in to your Tapo account first")
    c = clients.get(ip)
    if not c:
        c = clients[ip] = Tapo(ip, cfg["email"], cfg["password"])
    return c


def describe(ip, info):
    name = info.get("nickname", "")
    try:
        name = base64.b64decode(name).decode() or name
    except ValueError:
        pass
    kind = "light" if "BULB" in info.get("type", "") or "brightness" in info else "plug"
    return {"id": info.get("device_id", ip), "ip": ip, "name": name or info.get("model", "Tapo"), "model": info.get("model", ""),
            "kind": kind, "on": bool(info.get("device_on")), "brightness": info.get("brightness")}


def tapo_login(email, password):
    """Save the Tapo account; checked against any device already added."""
    email = email.strip()
    cfg = load()
    for d in cfg["devices"][:1]:
        Tapo(d["ip"], email, password).login()  # raises if the password is wrong
    cfg["tapo"] = {"email": email, "password": password}
    save(cfg)
    clients.clear()


def is_tapo(ip):
    """Tapo devices answer a KLAP handshake with exactly 48 bytes."""
    try:
        req = urllib.request.Request(f"http://{ip}/app/handshake1", data=secrets.token_bytes(16), method="POST")
        with urllib.request.urlopen(req, timeout=1.5) as r:
            return len(r.read()) == 48
    except OSError:
        return False


def scan(local_ip):
    """Find Tapo devices on the home network and add them."""
    if not local_ip:
        raise TapoError("This TV isn't connected to a network")
    hosts = [str(h) for h in ipaddress.ip_network(local_ip + "/24", strict=False).hosts() if str(h) != local_ip]

    def probe(ip):
        try:
            with socket.create_connection((ip, 80), timeout=0.4):
                pass
        except OSError:
            return None
        return ip if is_tapo(ip) else None

    with ThreadPoolExecutor(64) as pool:
        found = [ip for ip in pool.map(probe, hosts) if ip]
    added = []
    for ip in found:
        try:
            added.append(add_device(ip))
        except TapoError:
            pass
    return added


def add_device(ip):
    ipaddress.ip_address(ip)
    info = describe(ip, tapo(ip).request("get_device_info"))
    cfg = load()
    cfg["devices"] = [d for d in cfg["devices"] if d["id"] != info["id"] and d["ip"] != ip] + [
        {"id": info["id"], "ip": ip, "name": info["name"], "kind": info["kind"]}]
    save(cfg)
    return info


def device_states():
    out = []
    for d in load()["devices"]:
        try:
            out.append(describe(d["ip"], tapo(d["ip"]).request("get_device_info")))
        except (TapoError, ValueError) as e:
            out.append({**d, "on": None, "error": str(e)})
    return out


def set_device(dev_id, on=None, brightness=None):
    d = next((d for d in load()["devices"] if d["id"] == dev_id), None)
    if not d:
        raise TapoError("Unknown device")
    params = {}
    if on is not None:
        params["device_on"] = bool(on)
    if brightness is not None:
        params["brightness"] = max(1, min(100, int(brightness)))
        params["device_on"] = True
    tapo(d["ip"]).request("set_device_info", params)
    return describe(d["ip"], tapo(d["ip"]).request("get_device_info"))


def remove_device(dev_id):
    cfg = load()
    cfg["devices"] = [d for d in cfg["devices"] if d["id"] != dev_id]
    save(cfg)


# ---------- cameras ----------

workers = {}


class CameraWorker(threading.Thread):
    """Keeps ffmpeg reading one camera: newest frame on disk for previews, small frames for motion detection."""

    def __init__(self, cam):
        super().__init__(daemon=True)
        self.cam, self.stop, self.proc = cam, False, None
        self.frame = FRAMES / f"{cam['id']}.jpg"
        self.last_motion = 0
        self.online = False

    def run(self):
        FRAMES.mkdir(parents=True, exist_ok=True)
        size = MOTION_W * MOTION_H
        while not self.stop:
            cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", self.cam["url"], "-an",
                   "-filter_complex", f"[0:v]fps=6,scale=960:-2,split=2[p][m];[m]fps=2,scale={MOTION_W}:{MOTION_H},format=gray[g]",
                   "-map", "[p]", "-f", "image2", "-update", "1", "-q:v", "6", "-y", str(self.frame),
                   "-map", "[g]", "-f", "rawvideo", "pipe:1"]
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            prev, hits = None, 0
            while not self.stop:
                buf = self.proc.stdout.read(size)
                if len(buf) < size:
                    break
                self.online = True
                if prev is not None:
                    changed = sum(1 for a, b in zip(buf, prev) if abs(a - b) > 28)
                    hits = hits + 1 if changed > size * self.cam.get("sensitivity", 0.03) else 0
                    if hits >= 2 and time.time() - self.last_motion > 30:  # two changed frames in a row, at most every 30 s
                        self.last_motion = time.time()
                        motion(self.cam)
                prev = buf
            self.online = False
            if self.proc.poll() is None:
                self.proc.kill()
            time.sleep(5)  # camera offline or stream ended: try again shortly

    def close(self):
        self.stop = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


def motion(cam):
    if not load().get("alerts", True) or not cam.get("alerts", True):
        return
    ev = {"id": secrets.token_hex(4), "camera": cam["id"], "name": cam["name"], "at": time.time()}
    events.append(ev)
    del events[:-20]
    for cb in on_motion:
        try:
            cb(ev)
        except Exception:
            pass


def start_cameras():
    if not features()["cameras"]:
        return
    wanted = {c["id"]: c for c in load()["cameras"]}
    for cid in list(workers):
        if cid not in wanted or workers[cid].cam != wanted[cid]:
            workers.pop(cid).close()
    for cid, cam in wanted.items():
        if cid not in workers:
            workers[cid] = CameraWorker(cam)
            workers[cid].start()


def camera_url(ip, user, password, stream="stream2"):
    """Tapo cameras: the 'camera account' from the Tapo app (Advanced settings). stream1 = HD, stream2 = SD."""
    from urllib.parse import quote
    return f"rtsp://{quote(user, safe='')}:{quote(password, safe='')}@{ip}:554/{stream}"


def add_camera(name, url):
    if not url.startswith(("rtsp://", "rtsps://")):
        raise ValueError("Camera addresses start with rtsp://")
    cfg = load()
    cam = {"id": secrets.token_hex(4), "name": (name or "Camera").strip()[:30], "url": url, "alerts": True}
    cfg["cameras"].append(cam)
    save(cfg)
    start_cameras()
    return public_camera(cam)


def remove_camera(cam_id):
    cfg = load()
    cfg["cameras"] = [c for c in cfg["cameras"] if c["id"] != cam_id]
    save(cfg)
    start_cameras()


def set_camera(cam_id, **changes):
    cfg = load()
    for c in cfg["cameras"]:
        if c["id"] == cam_id:
            c.update({k: v for k, v in changes.items() if k in ("name", "alerts")})
    save(cfg)
    start_cameras()


def public_camera(c):
    w = workers.get(c["id"])
    return {"id": c["id"], "name": c["name"], "alerts": c.get("alerts", True), "online": bool(w and w.online)}


def cameras():
    return [public_camera(c) for c in load()["cameras"]]


def frame(cam_id):
    try:
        return (FRAMES / f"{cam_id}.jpg").read_bytes()
    except OSError:
        return None


def mjpeg(write, cam_id, seconds=3600):
    """Stream a camera as MJPEG (multipart JPEG), which <img> tags play natively."""
    end, last = time.time() + seconds, 0
    path = FRAMES / f"{cam_id}.jpg"
    while time.time() < end:
        try:
            m = path.stat().st_mtime
        except OSError:
            m = 0
        if m and m != last:
            last = m
            data = frame(cam_id)
            if data:
                write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
        time.sleep(0.12)


def state():
    cfg = load()
    return {"features": features(), "tapo": {"email": cfg["tapo"].get("email", "")}, "devices": cfg["devices"],
            "cameras": cameras(), "alerts": cfg.get("alerts", True)}
