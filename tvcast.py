#!/usr/bin/env python3
"""
TV Cast — Raspberry Pi host.

One process that:
  * captures the TV box (HDMI capture card: picture + sound) and streams it with go2rtc
    (WebRTC directly on your home network, MSE over the encrypted tunnel everywhere else)
  * controls the Android TV box over Wi-Fi with ADB (no IR, no cloud, no keys in the web page)
  * opens a free Cloudflare quick tunnel (encrypted, no account) and publishes its address,
    encrypted with your 8-character code, to ntfy.sh so the app can find the Pi from anywhere

Everything secret lives in ~/.config/tvcast/config.json on this Pi only.

Usage:
  tvcast.py                run the service (systemd does this)
  tvcast.py code           show the pairing code, link and QR
  tvcast.py newcode        make a new code (old links stop working)
  tvcast.py status         quick health check
  tvcast.py findbox        scan the network for TV boxes with ADB enabled
  tvcast.py set KEY VALUE  change a setting, e.g.  set box 192.168.1.45:5555   (or: set box bt)
  tvcast.py bt             Bluetooth remote status (when the box is controlled over Bluetooth)
  tvcast.py btforget       remove Bluetooth pairings
"""
import asyncio, base64, hashlib, hmac, ipaddress, json, logging, os, re, secrets, shutil, signal, socket, subprocess, sys, time
from pathlib import Path

VERSION = "1.0.0"
CONF_DIR = Path(os.environ.get("TVCAST_HOME", Path.home() / ".config" / "tvcast"))
CONF_FILE = CONF_DIR / "config.json"
BT_PORT = int(os.environ.get("TVCAST_BT_PORT", "8789"))                      # tvcast_bt.py listens here (Bluetooth remote helper)
BIN_DIR = Path(os.environ.get("TVCAST_BIN", Path.home() / ".local" / "share" / "tvcast"))
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"   # no 0/1/I/L/O — easy to read aloud
DEFAULTS = {
    "name": "TV Box",
    "code": "",
    "box": "",                       # "192.168.1.45:5555", or "mock" for testing
    "app_url": "",                   # your GitHub Pages address
    "quality": "720p30",             # 480p30 720p30 720p60 1080p30
    "video_device": "auto",          # "auto", "/dev/video0", or "test"
    "audio_device": "auto",          # "auto", "plughw:CARD=MS2130,DEV=0", "none", or "test"
    "av_offset_ms": 0,               # + delays sound, - delays picture
    "port": 8780,
    "ntfy": "https://ntfy.sh",
    "tunnel": True,
    "public_url": "",                # only if you run your own tunnel/domain instead of the free one
}
QUALITY = {
    "480p30":  (854, 480, 30, 1500),
    "720p30":  (1280, 720, 30, 3500),
    "720p60":  (1280, 720, 60, 5000),
    "1080p30": (1920, 1080, 30, 6000),
}

log = logging.getLogger("tvcast")


# ---------------------------------------------------------------- config & crypto
def load_conf():
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    conf = dict(DEFAULTS)
    if CONF_FILE.exists():
        conf.update(json.loads(CONF_FILE.read_text()))
    if not conf["code"]:
        conf["code"] = new_code()
        save_conf(conf)
    return conf


def save_conf(conf):
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONF_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(conf, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(CONF_FILE)


def new_code():
    return "".join(secrets.choice(ALPHABET) for _ in range(8))


def norm_code(code):
    return re.sub(r"[^0-9A-Z]", "", code.upper())


def derive(code, purpose):
    # Must match derive() in index.html exactly.
    return hashlib.pbkdf2_hmac("sha256", norm_code(code).encode(), b"tvcast/v1/" + purpose.encode(), 150_000, 32)


class Keys:
    def __init__(self, code):
        self.code = norm_code(code)
        self.topic = "tvcast-" + derive(code, "topic").hex()[:24]
        self.aes = derive(code, "key")
        self.token = derive(code, "auth").hex()

    def seal(self, obj):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        iv = secrets.token_bytes(12)
        ct = AESGCM(self.aes).encrypt(iv, json.dumps(obj).encode(), b"tvcast")
        return json.dumps({"v": 1, "iv": base64.b64encode(iv).decode(), "ct": base64.b64encode(ct).decode()})


def pretty(code):
    return code[:4] + "-" + code[4:]


def invite_link(conf):
    base = (conf.get("app_url") or "").rstrip("/")
    return f"{base}/#c={conf['code']}" if base else ""


# ---------------------------------------------------------------- capture detection
def run(cmd, timeout=8):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def find_video():
    """First USB video-capture node (skips the Pi's own codec/ISP nodes and UVC metadata nodes)."""
    base = Path("/sys/class/video4linux")
    if not base.exists():
        return None
    for node in sorted(base.iterdir(), key=lambda p: int(re.sub(r"\D", "", p.name) or 0)):
        dev = f"/dev/{node.name}"
        try:
            real = os.path.realpath(node / "device")
        except OSError:
            continue
        if "usb" not in real:
            continue
        info = run(["v4l2-ctl", "-d", dev, "--all"])
        caps = info.split("Device Caps", 1)[-1][:400] if "Device Caps" in info else info
        if "Video Capture" in caps:
            name = (node / "name").read_text().strip() if (node / "name").exists() else dev
            return {"dev": dev, "name": name}
    return None


def list_formats(dev):
    """{'MJPG': {(1280,720): [60.0, 30.0]}, 'YUYV': {...}}"""
    out = run(["v4l2-ctl", "-d", dev, "--list-formats-ext"])
    fmts, cur, size = {}, None, None
    for line in out.splitlines():
        m = re.search(r"'(\w+)'", line)
        if m and "[" in line:
            cur = m.group(1); fmts[cur] = {}; continue
        m = re.search(r"Size: Discrete (\d+)x(\d+)", line)
        if m and cur:
            size = (int(m.group(1)), int(m.group(2))); fmts[cur][size] = []; continue
        m = re.search(r"\(([\d.]+) fps\)", line)
        if m and cur and size:
            fmts[cur][size].append(float(m.group(1)))
    return fmts


def pick_format(fmts, w, h, fps):
    """Prefer MJPEG at the wanted size (USB bandwidth), else raw, else the closest size."""
    for want in (("MJPG", "mjpeg"), ("YUYV", "yuyv422"), ("NV12", "nv12")):
        sizes = fmts.get(want[0], {})
        if (w, h) in sizes:
            rates = sizes[(w, h)] or [fps]
            ok = [r for r in rates if r >= fps - 1]
            return want[1], w, h, (fps if ok else int(max(rates)))
    for want in (("MJPG", "mjpeg"), ("YUYV", "yuyv422")):
        sizes = fmts.get(want[0], {})
        if sizes:
            (bw, bh) = min(sizes, key=lambda s: abs(s[1] - h))
            rates = sizes[(bw, bh)] or [fps]
            return want[1], bw, bh, int(min(fps, max(rates)))
    return None, w, h, fps


def find_audio(video_name=""):
    """ALSA card of the capture card (USB). Returns a plughw: string or None."""
    cards = Path("/proc/asound/cards").read_text() if Path("/proc/asound/cards").exists() else ""
    usb = []
    for m in re.finditer(r"^\s*(\d+)\s+\[(\S+)\s*\]:\s*(.+?)\n\s+(.+)$", cards, re.M):
        idx, cid, drv, desc = m.groups()
        if "USB" in drv or "usb" in desc:
            usb.append((cid, desc))
    if not usb:
        return None
    key = (video_name or "").split(":")[0].strip().lower()
    for cid, desc in usb:
        if key and (key in desc.lower() or cid.lower() in key):
            return f"plughw:CARD={cid},DEV=0"
    return f"plughw:CARD={usb[0][0]},DEV=0"


def capture_plan(conf):
    w, h, fps, kbps = QUALITY.get(conf["quality"], QUALITY["720p30"])
    plan = {"quality": conf["quality"], "kbps": kbps, "test": False}
    vd = conf["video_device"]
    if vd == "test":
        plan.update(test=True, video="test pattern", name="Test pattern", fmt="lavfi", w=w, h=h, fps=fps)
    else:
        v = {"dev": vd, "name": vd} if vd != "auto" else find_video()
        if not v:
            plan["error"] = "No capture card found. Plug it into a blue USB 3 port."
            return plan
        fmt, w, h, fps = pick_format(list_formats(v["dev"]), w, h, fps)
        plan.update(video=v["dev"], name=v["name"], fmt=fmt or "mjpeg", w=w, h=h, fps=fps)
    ad = conf["audio_device"]
    if ad == "auto":
        ad = "test" if plan["test"] else (find_audio(plan.get("name", "")) or "none")
    plan["audio"] = ad
    return plan


def ffmpeg_cmd(plan, offset_ms=0):
    w, h, fps, kbps = plan["w"], plan["h"], plan["fps"], plan["kbps"]
    a = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if plan["test"]:
        a += ["-re", "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps}"]
    else:
        a += ["-thread_queue_size", "1024", "-f", "v4l2", "-ts", "mono2abs",
              "-input_format", plan["fmt"], "-video_size", f"{w}x{h}", "-framerate", str(fps), "-i", plan["video"]]
    has_audio = plan["audio"] not in ("none", "", None)
    if has_audio:
        if offset_ms:
            a += ["-itsoffset", f"{offset_ms / 1000:.3f}"]
        if plan["audio"] == "test":
            a += ["-re", "-f", "lavfi", "-i", "sine=frequency=440:beep_factor=4:sample_rate=48000"]
        else:
            a += ["-thread_queue_size", "4096", "-f", "alsa", "-ac", "2", "-ar", "48000", "-i", plan["audio"]]
    # H.264 constrained baseline, no B-frames, 1 s keyframes: plays everywhere, starts fast, low delay.
    a += ["-map", "0:v"]
    if has_audio:
        a += ["-map", "1:a", "-map", "1:a"]
    a += ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-profile:v", "baseline",
          "-pix_fmt", "yuv420p", "-g", str(fps), "-keyint_min", str(fps), "-sc_threshold", "0", "-bf", "0",
          "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps // 2}k", "-threads", "4"]
    if has_audio:
        # aresample async fills capture gaps / drift so sound stays gapless and in sync with the picture.
        # Two audio tracks: Opus for WebRTC, AAC for MSE (iPhone Safari can't do Opus in MSE).
        a += ["-af", "aresample=async=1000:first_pts=0",
              "-c:a:0", "libopus", "-b:a:0", "128k", "-application:a:0", "audio", "-frame_duration:a:0", "20",
              "-c:a:1", "aac", "-b:a:1", "160k", "-ar", "48000", "-ac", "2"]
    a += ["-f", "rtsp", "{output}"]
    return " ".join(a)


# ---------------------------------------------------------------- box control (ADB over Wi-Fi)
class Box:
    """Keeps one ADB shell open and batches key presses, so taps feel instant and stay in order."""

    def __init__(self, target):
        self.target = target.strip()
        self.state = "not set" if not self.target else "connecting"
        self.detail = ""
        self.proc = None
        self.queue = asyncio.Queue()
        self.mock_log = []

    @property
    def mock(self):
        return self.target == "mock"

    async def adb(self, *args, timeout=8):
        p = await asyncio.create_subprocess_exec("adb", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(p.communicate(), timeout)
        except asyncio.TimeoutError:
            p.kill(); return "timeout"
        return out.decode(errors="replace").strip()

    async def connect(self):
        if self.mock:
            self.state, self.detail = "ready", "mock box (testing)"; return True
        if not self.target:
            self.state, self.detail = "not set", "Run: tvcast.py set box <TV box IP>"; return False
        if not shutil.which("adb"):
            self.state, self.detail = "error", "adb is not installed"; return False
        out = await self.adb("connect", self.target)
        st = await self.adb("-s", self.target, "get-state")
        if st == "device":
            self.proc = await asyncio.create_subprocess_exec(
                "adb", "-s", self.target, "shell", stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            self.state, self.detail = "ready", self.target
            return True
        if "unauthorized" in (out + st):
            self.state, self.detail = "unauthorized", "Accept 'Allow USB debugging' on the TV (tick Always allow)"
        else:
            self.state, self.detail = "offline", (out or st or "no answer")[:160]
        return False

    async def worker(self):
        delay = 2
        while True:
            if self.state != "ready" or (self.proc and self.proc.returncode is not None):
                self.state = "connecting" if self.target else "not set"
                if not await self.connect():
                    await asyncio.sleep(delay); delay = min(delay * 2, 30); continue
                delay = 2
            first = await self.queue.get()
            batch = [first]
            while not self.queue.empty() and len(batch) < 12:
                batch.append(self.queue.get_nowait())
            await self._send(batch)

    async def _send(self, batch):
        # Group consecutive plain presses into ONE `input keyevent a b c` (one JVM start instead of many).
        t0 = time.monotonic()
        cmds, plain = [], []
        for item in batch:
            k, mode = item["key"], item.get("mode", "press")
            if mode == "press":
                plain.append(k); continue
            if plain:
                cmds.append("input keyevent " + " ".join(plain)); plain = []
            if mode == "long":
                cmds.append(f"input keyevent --longpress {k}")
            elif mode == "text":
                cmds.append("input text " + shell_quote(k))
        if plain:
            cmds.append("input keyevent " + " ".join(plain))
        ok, err = True, ""
        try:
            if self.mock:
                self.mock_log.extend(cmds); self.mock_log[:] = self.mock_log[-50:]
                await asyncio.sleep(0.03)
            else:
                marker = f"__tvcast_{secrets.token_hex(4)}__"
                self.proc.stdin.write(("; ".join(cmds) + f"; echo {marker}\n").encode())
                await self.proc.stdin.drain()
                await asyncio.wait_for(self._read_until(marker), 6)
        except Exception as e:
            ok, err = False, str(e) or e.__class__.__name__
            self.state = "offline"
            if self.proc and self.proc.returncode is None:
                self.proc.kill()
        ms = int((time.monotonic() - t0) * 1000)
        for item in batch:
            fut = item.get("fut")
            if fut and not fut.done():
                fut.set_result({"ok": ok, "ms": ms, "err": err})

    async def _read_until(self, marker):
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                raise ConnectionError("box disconnected")
            if marker in line.decode(errors="replace"):
                return

    async def press(self, key, mode="press"):
        if not re.fullmatch(r"(KEYCODE_[A-Z0-9_]+|\d{1,3})", key) and mode != "text":
            return {"ok": False, "err": "bad key"}
        if self.state != "ready":
            return {"ok": False, "err": f"box {self.state}: {self.detail}"}
        fut = asyncio.get_running_loop().create_future()
        await self.queue.put({"key": key, "mode": mode, "fut": fut})
        return await asyncio.wait_for(fut, 8)


    async def learn(self, seconds=10):
        """Wait for a button on the box's own remote and return the Android key it sends."""
        if self.mock:
            await asyncio.sleep(1.0)
            return {"ok": True, "key": "KEYCODE_DPAD_UP", "raw": "KEY_UP"}
        if self.state != "ready":
            return {"ok": False, "err": f"box {self.state}"}
        p = await asyncio.create_subprocess_exec("adb", "-s", self.target, "shell", "getevent", "-lq",
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                line = await asyncio.wait_for(p.stdout.readline(), max(0.1, end - time.monotonic()))
                if not line:
                    return {"ok": False, "err": "This box doesn't allow learning. Pick the key from the list instead."}
                m = re.search(r"EV_KEY\s+(KEY_\w+|BTN_\w+|[0-9a-f]{4})\s+DOWN", line.decode(errors="replace"))
                if m:
                    raw = m.group(1)
                    key = LINUX_TO_ANDROID.get(raw)
                    if key:
                        return {"ok": True, "key": key, "raw": raw}
                    return {"ok": False, "raw": raw, "err": f"Got {raw}, which has no Android key. Pick one from the list."}
                if "permission denied" in line.decode(errors="replace").lower():
                    return {"ok": False, "err": "This box doesn't allow learning. Pick the key from the list instead."}
            return {"ok": False, "err": "No button press heard within 10 seconds."}
        except asyncio.TimeoutError:
            return {"ok": False, "err": "No button press heard within 10 seconds."}
        finally:
            if p.returncode is None:
                p.kill()


LINUX_TO_ANDROID = {
    "KEY_UP": "KEYCODE_DPAD_UP", "KEY_DOWN": "KEYCODE_DPAD_DOWN", "KEY_LEFT": "KEYCODE_DPAD_LEFT",
    "KEY_RIGHT": "KEYCODE_DPAD_RIGHT", "KEY_ENTER": "KEYCODE_DPAD_CENTER", "KEY_OK": "KEYCODE_DPAD_CENTER",
    "KEY_SELECT": "KEYCODE_DPAD_CENTER", "KEY_KPENTER": "KEYCODE_DPAD_CENTER", "KEY_BACK": "KEYCODE_BACK",
    "KEY_ESC": "KEYCODE_BACK", "KEY_HOME": "KEYCODE_HOME", "KEY_HOMEPAGE": "KEYCODE_HOME", "KEY_MENU": "KEYCODE_MENU",
    "KEY_COMPOSE": "KEYCODE_MENU", "KEY_VOLUMEUP": "KEYCODE_VOLUME_UP", "KEY_VOLUMEDOWN": "KEYCODE_VOLUME_DOWN",
    "KEY_MUTE": "KEYCODE_VOLUME_MUTE", "KEY_POWER": "KEYCODE_POWER", "KEY_SLEEP": "KEYCODE_SLEEP",
    "KEY_WAKEUP": "KEYCODE_WAKEUP", "KEY_PLAYPAUSE": "KEYCODE_MEDIA_PLAY_PAUSE", "KEY_PLAY": "KEYCODE_MEDIA_PLAY",
    "KEY_PAUSE": "KEYCODE_MEDIA_PAUSE", "KEY_PAUSECD": "KEYCODE_MEDIA_PAUSE", "KEY_PLAYCD": "KEYCODE_MEDIA_PLAY",
    "KEY_FASTFORWARD": "KEYCODE_MEDIA_FAST_FORWARD", "KEY_REWIND": "KEYCODE_MEDIA_REWIND",
    "KEY_NEXTSONG": "KEYCODE_MEDIA_NEXT", "KEY_PREVIOUSSONG": "KEYCODE_MEDIA_PREVIOUS", "KEY_STOP": "KEYCODE_MEDIA_STOP",
    "KEY_STOPCD": "KEYCODE_MEDIA_STOP", "KEY_CHANNELUP": "KEYCODE_CHANNEL_UP", "KEY_CHANNELDOWN": "KEYCODE_CHANNEL_DOWN",
    "KEY_PAGEUP": "KEYCODE_PAGE_UP", "KEY_PAGEDOWN": "KEYCODE_PAGE_DOWN", "KEY_INFO": "KEYCODE_INFO",
    "KEY_EPG": "KEYCODE_GUIDE", "KEY_PROGRAM": "KEYCODE_GUIDE", "KEY_SEARCH": "KEYCODE_SEARCH",
    "KEY_VOICECOMMAND": "KEYCODE_VOICE_ASSIST", "KEY_ASSISTANT": "KEYCODE_ASSIST", "KEY_SETUP": "KEYCODE_SETTINGS",
    "KEY_SETTINGS": "KEYCODE_SETTINGS", "KEY_RED": "KEYCODE_PROG_RED", "KEY_GREEN": "KEYCODE_PROG_GREEN",
    "KEY_YELLOW": "KEYCODE_PROG_YELLOW", "KEY_BLUE": "KEYCODE_PROG_BLUE", "KEY_TV": "KEYCODE_TV",
    "KEY_DVR": "KEYCODE_DVR", "KEY_SUBTITLE": "KEYCODE_CAPTIONS", "KEY_LAST": "KEYCODE_LAST_CHANNEL",
    "KEY_APPSELECT": "KEYCODE_APP_SWITCH", "KEY_BOOKMARKS": "KEYCODE_BOOKMARK", "KEY_DELETE": "KEYCODE_DEL",
    "KEY_BACKSPACE": "KEYCODE_DEL", "KEY_RECORD": "KEYCODE_MEDIA_RECORD",
    **{f"KEY_{i}": f"KEYCODE_{i}" for i in range(10)},
}


class BTBox:
    """Box control through tvcast-bt: the Pi acts as a Bluetooth remote the box is paired with.
    Used when the box doesn't allow USB debugging (set with:  tvcast set box bt)."""
    PAIR_HINT = ("Pair it once: on the TV box open Settings → Remotes & Accessories → Add accessory, "
                 "then pick 'TV Cast Remote'.")

    def __init__(self):
        self.target, self.state, self.detail = "bt", "connecting", ""
        self.proc, self.mock_log, self.rw = None, [], None
        self.lock = asyncio.Lock()

    @property
    def mock(self):
        return False

    async def _call(self, obj, timeout=4):
        async with self.lock:
            for attempt in (1, 2):
                try:
                    if not self.rw:
                        self.rw = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", BT_PORT), 2)
                    r, w = self.rw
                    w.write((json.dumps(obj) + "\n").encode())
                    await w.drain()
                    line = await asyncio.wait_for(r.readline(), timeout)
                    if not line:
                        raise ConnectionError("helper closed the connection")
                    return json.loads(line)
                except Exception:
                    if self.rw:
                        self.rw[1].close()
                        self.rw = None
                    if attempt == 2:
                        raise

    async def worker(self):
        while True:
            try:
                st = await self._call({"cmd": "status"})
                if st.get("error"):
                    self.state, self.detail = "error", "Bluetooth: " + st["error"]
                elif st.get("subscribed"):
                    self.state = "ready"
                    self.detail = "Bluetooth · " + (", ".join(st.get("connected") or []) or "TV box")
                elif st.get("connected"):
                    self.state, self.detail = "connecting", "Box found, finishing pairing…"
                else:
                    self.state, self.detail = "not paired", self.PAIR_HINT
            except Exception:
                self.state, self.detail = "error", "Bluetooth helper isn't running. Run: sudo systemctl restart tvcast-bt"
            await asyncio.sleep(2 if self.state != "ready" else 4)

    async def press(self, key, mode="press"):
        if mode == "text":
            return {"ok": False, "err": "Typing isn't supported over Bluetooth"}
        if not re.fullmatch(r"KEYCODE_[A-Z0-9_]+", key):
            return {"ok": False, "err": "Over Bluetooth, pick a KEYCODE_ name (numbers aren't supported)"}
        if self.state != "ready":
            return {"ok": False, "err": f"box {self.state}: {self.detail}"}
        t0 = time.monotonic()
        try:
            res = await self._call({"cmd": "key", "key": key, "mode": mode}, timeout=6)
        except Exception as e:
            return {"ok": False, "err": f"Bluetooth helper: {e}"}
        res.setdefault("ms", int((time.monotonic() - t0) * 1000))
        return res

    async def learn(self, seconds=10):
        return {"ok": False, "err": "Learn needs Wi-Fi (USB debugging) control. With Bluetooth, pick the key from the list and tap Test."}


def shell_quote(s):
    s = s[:200].replace(" ", "%s")
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------- the service
class Service:
    def __init__(self):
        self.conf = load_conf()
        self.keys = Keys(self.conf["code"])
        self.box = BTBox() if self.conf["box"] == "bt" else Box(self.conf["box"])
        self.plan = {}
        self.go2rtc = None
        self.tunnel_url = ""
        self.tunnel_proc = None
        self.published_at = 0
        self.publish_err = ""
        self.bad_auth = {}
        self.started = time.time()

    # ---- go2rtc
    def go2rtc_bin(self):
        p = BIN_DIR / "go2rtc"
        return str(p) if p.exists() else (shutil.which("go2rtc") or "go2rtc")

    async def start_stream(self):
        await self.stop_stream()
        self.plan = await asyncio.to_thread(capture_plan, self.conf)
        if self.plan.get("error"):
            log.warning(self.plan["error"])
        cfg = {
            "log": {"level": "warn"},
            "api": {"listen": "127.0.0.1:1984", "origin": "*"},
            "rtsp": {"listen": "127.0.0.1:8554"},
            "rtmp": {"listen": ""},
            "srtp": {"listen": ""},
            "webrtc": {"listen": ":8555",
                       "ice_servers": [{"urls": ["stun:stun.cloudflare.com:3478", "stun:stun.l.google.com:19302"]}]},
            "ffmpeg": {"bin": "ffmpeg"},
            "streams": {} if self.plan.get("error") else
                       {"tv": "exec:" + ffmpeg_cmd(self.plan, int(self.conf.get("av_offset_ms") or 0))},
        }
        if not self.plan.get("error"):
            # Always-on: the card is opened once and kept open, so the picture starts instantly and
            # two viewers (or a quick reconnect) never fight over the card ("device busy").
            cfg["preload"] = {"tv": ""}
        if not self.plan.get("test") and self.plan.get("video"):
            subprocess.run(["pkill", "-f", f"ffmpeg.*{re.escape(self.plan['video'])}( |$)"], capture_output=True)
            await asyncio.sleep(0.5)
        path = CONF_DIR / "go2rtc.yaml"
        path.write_text(json.dumps(cfg, indent=1))     # JSON is valid YAML
        self.go2rtc = await asyncio.create_subprocess_exec(self.go2rtc_bin(), "-config", str(path),
                                                           stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        log.info("stream: %s", {k: v for k, v in self.plan.items() if k != "kbps"})

    async def stop_stream(self):
        if self.go2rtc and self.go2rtc.returncode is None:
            self.go2rtc.terminate()
            try:
                await asyncio.wait_for(self.go2rtc.wait(), 5)
            except asyncio.TimeoutError:
                self.go2rtc.kill()

    async def stream_watchdog(self):
        while True:
            await asyncio.sleep(5)
            if self.go2rtc and self.go2rtc.returncode is not None:
                log.warning("go2rtc exited, restarting"); await self.start_stream()
            elif self.plan.get("error"):
                plan = await asyncio.to_thread(capture_plan, self.conf)
                if not plan.get("error"):
                    log.info("capture card appeared"); await self.start_stream()

    # ---- tunnel + rendezvous
    async def tunnel_loop(self):
        if not self.conf.get("tunnel", True):
            if self.conf.get("public_url"):
                self.tunnel_url = self.conf["public_url"].rstrip("/")
                await self.publish()
            return
        exe = str(BIN_DIR / "cloudflared") if (BIN_DIR / "cloudflared").exists() else shutil.which("cloudflared")
        if not exe:
            log.error("cloudflared missing — remote access disabled"); return
        while True:
            self.tunnel_url = ""
            self.tunnel_proc = await asyncio.create_subprocess_exec(
                exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.conf['port']}",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            async for raw in self.tunnel_proc.stderr:
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", raw.decode(errors="replace"))
                if m and not self.tunnel_url:
                    self.tunnel_url = m.group(0)
                    log.info("tunnel: %s", self.tunnel_url)
                    asyncio.create_task(self.publish())
            await self.tunnel_proc.wait()
            log.warning("tunnel stopped, restarting in 5 s")
            await asyncio.sleep(5)

    async def publish(self):
        """Post the tunnel address, encrypted with the pairing code, to ntfy (free, no account)."""
        import aiohttp
        if not self.tunnel_url:
            return
        await asyncio.sleep(3)          # let the new hostname resolve before phones try it
        body = self.keys.seal({"url": self.tunnel_url, "name": self.conf["name"], "ts": int(time.time())})
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.post(f"{self.conf['ntfy'].rstrip('/')}/{self.keys.topic}", data=body,
                                  headers={"X-Cache": "yes", "X-Firebase": "no"}) as r:
                    if r.status >= 300:
                        raise RuntimeError(f"HTTP {r.status}")
            self.published_at, self.publish_err = time.time(), ""
            log.info("published address to ntfy")
        except Exception as e:
            self.publish_err = str(e)
            log.warning("ntfy publish failed: %s", e)

    async def republish_loop(self):
        while True:
            await asyncio.sleep(60)
            if self.tunnel_url and (time.time() - self.published_at > 3 * 3600 or self.publish_err):
                await self.publish()

    async def tunnel_health(self):
        import aiohttp
        fails = 0
        while True:
            await asyncio.sleep(90)
            if not self.tunnel_url or not self.tunnel_proc:
                continue
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                    async with s.get(self.tunnel_url + "/health") as r:
                        fails = 0 if r.status == 200 else fails + 1
            except Exception:
                fails += 1
            if fails >= 3 and self.tunnel_proc.returncode is None:
                log.warning("tunnel unreachable, restarting it"); self.tunnel_proc.terminate(); fails = 0

    # ---- HTTP
    def authed(self, request):
        ip = request.headers.get("CF-Connecting-IP") or request.remote or "?"
        now = time.time()
        hits = [t for t in self.bad_auth.get(ip, []) if now - t < 300]
        if len(hits) >= 20:
            return False
        tok = request.query.get("t") or request.headers.get("X-TVCast-Token", "")
        if hmac.compare_digest(tok.encode(), self.keys.token.encode()):
            return True
        hits.append(now); self.bad_auth[ip] = hits
        return False

    def status(self):
        p = self.plan
        return {
            "name": self.conf["name"], "version": VERSION, "uptime": int(time.time() - self.started),
            "capture": {"ok": not p.get("error"), "error": p.get("error", ""), "device": p.get("name", ""),
                        "format": p.get("fmt", ""), "size": f"{p.get('w')}x{p.get('h')}" if p.get("w") else "",
                        "fps": p.get("fps"), "audio": p.get("audio", ""), "quality": self.conf["quality"],
                        "av_offset_ms": self.conf.get("av_offset_ms", 0)},
            "stream": {"running": bool(self.go2rtc and self.go2rtc.returncode is None)},
            "box": {"state": self.box.state, "detail": self.box.detail, "target": self.box.target},
            "tunnel": {"url": self.tunnel_url, "published": int(self.published_at), "error": self.publish_err},
        }

    def build_app(self):
        from aiohttp import web, ClientSession, WSMsgType
        svc = self

        @web.middleware
        async def cors(request, handler):
            if request.method == "OPTIONS":
                resp = web.Response()
            else:
                resp = await handler(request)
            if not resp.prepared:
                resp.headers["Access-Control-Allow-Origin"] = "*"
                resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-TVCast-Token"
                resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
                resp.headers["Cache-Control"] = "no-store"
            return resp

        app = web.Application(middlewares=[cors])

        async def health(request):
            return web.json_response({"ok": True, "app": "tvcast", "version": VERSION})

        async def status(request):
            if not svc.authed(request):
                return web.json_response({"error": "wrong code"}, status=401)
            return web.json_response(svc.status())

        async def settings(request):
            if not svc.authed(request):
                return web.json_response({"error": "wrong code"}, status=401)
            data = await request.json()
            restart = False
            if data.get("quality") in QUALITY:
                svc.conf["quality"] = data["quality"]; restart = True
            if "av_offset_ms" in data:
                svc.conf["av_offset_ms"] = max(-1000, min(1000, int(data["av_offset_ms"]))); restart = True
            if data.get("name"):
                svc.conf["name"] = str(data["name"])[:40]
            if data.get("restart"):
                restart = True
            save_conf(svc.conf)
            if restart:
                await svc.start_stream()
            return web.json_response(svc.status())

        async def player_js(request):
            # go2rtc's player (MIT) — served from the Pi so the web page stays tiny and in lock-step with go2rtc.
            async with ClientSession() as s:
                async with s.get(f"http://127.0.0.1:1984/{request.match_info['f']}") as r:
                    body = await r.read()
                    return web.Response(body=body, status=r.status, content_type="application/javascript")

        async def media_ws(request):
            if not svc.authed(request):
                return web.Response(status=401, text="wrong code")
            client = web.WebSocketResponse(heartbeat=20, max_msg_size=0)
            await client.prepare(request)
            async with ClientSession() as s:
                try:
                    async with s.ws_connect("http://127.0.0.1:1984/api/ws?src=tv", max_msg_size=0) as up:
                        async def down():
                            async for m in up:
                                if m.type == WSMsgType.TEXT:
                                    await client.send_str(m.data)
                                elif m.type == WSMsgType.BINARY:
                                    await client.send_bytes(m.data)
                                else:
                                    break
                        t = asyncio.create_task(down())
                        async for m in client:
                            if m.type == WSMsgType.TEXT:
                                await up.send_str(m.data)
                            elif m.type == WSMsgType.BINARY:
                                await up.send_bytes(m.data)
                            else:
                                break
                        t.cancel()
                except Exception as e:
                    if not client.closed:
                        await client.send_str(json.dumps({"type": "error", "value": f"stream: {e}"}))
            await client.close()
            return client

        async def control_ws(request):
            if not svc.authed(request):
                return web.Response(status=401, text="wrong code")
            ws = web.WebSocketResponse(heartbeat=20)
            await ws.prepare(request)
            await ws.send_str(json.dumps({"type": "hello", "box": svc.box.state, "detail": svc.box.detail}))
            async for m in ws:
                if m.type != WSMsgType.TEXT:
                    continue
                try:
                    msg = json.loads(m.data)
                except ValueError:
                    continue
                if msg.get("type") == "ping":
                    await ws.send_str(json.dumps({"type": "pong", "id": msg.get("id"), "box": svc.box.state})); continue
                if msg.get("type") == "key":
                    async def do(msg=msg):
                        res = await svc.box.press(str(msg.get("key", "")), msg.get("mode", "press"))
                        if not ws.closed:
                            await ws.send_str(json.dumps({"type": "ack", "id": msg.get("id"), **res}))
                    asyncio.create_task(do())
                if msg.get("type") == "learn":
                    async def lrn(msg=msg):
                        res = await svc.box.learn()
                        if not ws.closed:
                            await ws.send_str(json.dumps({"type": "learned", "id": msg.get("id"), **res}))
                    asyncio.create_task(lrn())
            return ws

        async def mock_log(request):
            if not svc.authed(request):
                return web.Response(status=401)
            return web.json_response(svc.box.mock_log)

        app.router.add_get("/health", health)
        app.router.add_get("/api/status", status)
        app.router.add_post("/api/settings", settings)
        app.router.add_route("OPTIONS", "/{tail:.*}", health)
        app.router.add_get("/player/{f:video-rtc\\.js|video-stream\\.js}", player_js)
        app.router.add_get("/api/ws", media_ws)
        app.router.add_get("/ctl", control_ws)
        app.router.add_get("/api/mocklog", mock_log)
        return app

    async def main(self):
        from aiohttp import web
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        log.info("TV Cast %s — code %s", VERSION, pretty(self.conf["code"]))
        await self.start_stream()
        runner = web.AppRunner(self.build_app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1" if self.conf.get("tunnel", True) else "0.0.0.0", self.conf["port"]).start()
        tasks = [asyncio.create_task(c) for c in (self.box.worker(), self.stream_watchdog(), self.tunnel_loop(),
                                                    self.republish_loop(), self.tunnel_health())]
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        await stop.wait()
        for t in tasks:
            t.cancel()
        for p in (self.tunnel_proc, self.box.proc):
            if p and p.returncode is None:
                p.terminate()
        await self.stop_stream()
        await runner.cleanup()


# ---------------------------------------------------------------- CLI helpers
def show_code(conf):
    link = invite_link(conf)
    print(f"\n  Pairing code:  {pretty(conf['code'])}")
    if link:
        print(f"  Invite link:   {link}\n")
        if shutil.which("qrencode"):
            subprocess.run(["qrencode", "-t", "ansiutf8", "-m", "2", link])
    else:
        print("  (Set your app address to get a link and QR:  tvcast.py set app_url https://YOU.github.io/tvcast)\n")


def find_boxes():
    """Scan this Pi's /24 for devices answering on the ADB port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80)); me = s.getsockname()[0]
    except OSError:
        print("No network."); return []
    finally:
        s.close()
    net = ipaddress.ip_network(me + "/24", strict=False)

    async def probe(ip):
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(str(ip), 5555), 0.6)
            w.close(); return str(ip)
        except Exception:
            return None

    async def scan():
        res = await asyncio.gather(*(probe(ip) for ip in net.hosts() if str(ip) != me))
        return [r for r in res if r]

    return asyncio.run(scan())


def cli():
    args = sys.argv[1:]
    if not args:
        return asyncio.run(Service().main())
    conf = load_conf()
    cmd = args[0]
    if cmd == "code":
        show_code(conf)
    elif cmd == "newcode":
        conf["code"] = new_code(); save_conf(conf); show_code(conf)
        print("  Restart to apply:  sudo systemctl restart tvcast\n")
    elif cmd == "findbox":
        boxes = find_boxes()
        print("\n".join(f"  {b}:5555" for b in boxes) if boxes else "  No TV box with ADB found. Turn on USB/Network debugging on the box.")
    elif cmd == "set" and len(args) == 3:
        k, v = args[1], args[2]
        if k not in DEFAULTS:
            sys.exit(f"Unknown setting {k}. Options: {', '.join(DEFAULTS)}")
        if isinstance(DEFAULTS[k], bool):
            v = v.lower() in ("1", "true", "yes", "on")
        elif isinstance(DEFAULTS[k], int):
            v = int(v)
        if k == "box" and v not in ("", "mock", "bt") and ":" not in v:
            v += ":5555"
        conf[k] = v; save_conf(conf); print(f"  {k} = {v}")
    elif cmd == "status":
        import urllib.request
        keys = Keys(conf["code"])
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{conf['port']}/api/status?t={keys.token}", timeout=5) as r:
                print(json.dumps(json.load(r), indent=2))
        except Exception as e:
            print("Service not running:", e)
    elif cmd in ("bt", "btforget"):
        import socket as _s
        try:
            c = _s.create_connection(("127.0.0.1", BT_PORT), timeout=3); f = c.makefile("rw")
            f.write(json.dumps({"cmd": "status" if cmd == "bt" else "forget"}) + "\n"); f.flush()
            st = json.loads(f.readline())
        except Exception as e:
            sys.exit(f"  Bluetooth helper not running ({e}).  Try: sudo systemctl restart tvcast-bt")
        if cmd == "btforget":
            print("  Removed all pairings. Pair again from the box: Settings → Remotes & Accessories → Add accessory."); return
        print(f"  Name:        {st.get('name')}")
        print(f"  Advertising: {'yes' if st.get('advertising') else 'no'}{('  — ' + st['error']) if st.get('error') else ''}")
        print(f"  Paired:      {', '.join(st.get('paired') or []) or 'nothing yet'}")
        print(f"  Remote:      {'READY — the box is connected' if st.get('subscribed') else 'waiting for the box'}")
        if not st.get("subscribed"):
            print("\n  " + BTBox.PAIR_HINT)
    elif cmd == "detect":
        print(json.dumps(capture_plan(conf), indent=2)); print(ffmpeg_cmd(capture_plan(conf)) if not capture_plan(conf).get("error") else "")
    else:
        print(__doc__)


if __name__ == "__main__":
    cli()
