#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""TweenGlass — Phase 0 probe.

Validates the core hypothesis before writing the real renderer:

  1. A window can be captured through xdg-desktop-portal ScreenCast + PipeWire
     (including DRM video such as Netflix).
  2. The capture cadence follows the *content* frame rate (e.g. ~24 fps for a
     film), not the display refresh rate. Duplicate frames are counted.
  3. Re-presenting the capture from a Vulkan sink lets the user-installed
     lsfg-vk layer hook the process (check the "(lsfg-vk)" log lines).

Uses the system Python (needs python-gobject + GStreamer with pipewiresrc and
the Vulkan elements from gst-plugins-bad).

Usage:
  /usr/bin/python3 tools/phase0/probe.py [--seconds 60] [--no-display]

lsfg-vk is configured through environment variables (LSFGVK_ENV=1 etc.); the
probe sets sensible defaults and auto-discovers Lossless.dll in every Steam
library, because lsfg-vk 2.0 only searches the default Steam library.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import filters  # noqa: E402  (GLSL scaling/sharpening filters)
from audio_sync import AudioSync, video_latency_ms  # noqa: E402

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "tweenglass"
TOKEN_FILE = STATE_DIR / "phase0-restore-token"
RESTART_DEBOUNCE_MS = 600
# Lossless Scaling–style "base / output" counter, burned into the frames we
# hand to mpv (so it costs no extra presents). Size is relative to the canvas.
HUD = ("textoverlay name=hud text=\"\" valignment=top halignment=left xpad=12 ypad=8 "
       "font-desc=\"Monospace Bold 6\" color=0xFFE6E6E6 draw-shadow=true draw-outline=true outline-color=0xC0000000 "
       "shaded-background=false")


# --------------------------------------------------------------------------
# lsfg-vk environment (must be set before Vulkan/GStreamer load the layer)
# --------------------------------------------------------------------------

def steam_library_paths() -> list[Path]:
    roots = [
        Path.home() / ".local/share/Steam",
        Path.home() / ".steam/steam",
        Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    ]
    libs: list[Path] = []
    for root in roots:
        vdf = root / "steamapps/libraryfolders.vdf"
        if vdf.is_file():
            for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(errors="replace")):
                libs.append(Path(m.group(1)))
        libs.append(root)
    seen, out = set(), []
    for p in libs:
        rp = p.resolve() if p.exists() else p
        if rp not in seen:
            seen.add(rp)
            out.append(p)
    return out


def find_lossless_dll() -> Path | None:
    for lib in steam_library_paths():
        dll = lib / "steamapps/common/Lossless Scaling/Lossless.dll"
        if dll.is_file():
            return dll
    return None


def lsfg_env(args) -> dict:
    """Per-presenter lsfg-vk settings (read when the presenter starts)."""
    return {"LSFGVK_ENV": "1", "LSFGVK_MULTIPLIER": str(args.multiplier),
            "LSFGVK_FLOW_SCALE": str(args.flow_scale),
            "LSFGVK_PERFORMANCE_MODE": "1" if args.performance else "0"}


def configure_lsfg(multiplier: int, flow_scale: float, performance: bool) -> None:
    os.environ.setdefault("LSFGVK_ENV", "1")
    if "LSFGVK_DLL_PATH" not in os.environ:
        dll = find_lossless_dll()
        if dll:
            os.environ["LSFGVK_DLL_PATH"] = str(dll)
            print(f"[probe] Lossless.dll: {dll}")
        else:
            print("[probe] WARNING: Lossless.dll not found in any Steam library", file=sys.stderr)


# --------------------------------------------------------------------------
# xdg-desktop-portal ScreenCast (window source)
# --------------------------------------------------------------------------

class Portal:
    BUS = "org.freedesktop.portal.Desktop"
    PATH = "/org/freedesktop/portal/desktop"
    IFACE = "org.freedesktop.portal.ScreenCast"

    def __init__(self, Gio, GLib):
        self.Gio, self.GLib = Gio, GLib
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.sender = self.bus.get_unique_name()[1:].replace(".", "_")

    def _token(self) -> str:
        return f"tweenglass_{random.randint(0, 2**31)}"

    def _request(self, method: str, params, token: str) -> dict:
        """Call a portal method and wait for its Request::Response signal."""
        GLib = self.GLib
        req_path = f"/org/freedesktop/portal/desktop/request/{self.sender}/{token}"
        loop = GLib.MainLoop()
        result: dict = {}

        def on_response(_c, _s, _p, _i, _sig, params_v):
            code, res = params_v.unpack()
            result["code"], result["res"] = code, res
            loop.quit()

        sub = self.bus.signal_subscribe(
            self.BUS, "org.freedesktop.portal.Request", "Response", req_path,
            None, self.Gio.DBusSignalFlags.NO_MATCH_RULE, on_response)
        self.bus.call_sync(self.BUS, self.PATH, self.IFACE, method, params,
                           None, self.Gio.DBusCallFlags.NONE, -1, None)
        loop.run()
        self.bus.signal_unsubscribe(sub)
        if result.get("code") != 0:
            raise SystemExit(f"[probe] portal {method} cancelled/failed (code {result.get('code')})")
        return result["res"]

    def forge_window_token(self, app_id: str, title: str) -> str:
        """Create a restore token that makes xdg-desktop-portal-kde pick the
        given window without showing its chooser dialog.

        The KDE backend restores window casts by (appId, title) — see
        tryMatchWindows() in xdg-desktop-portal-kde/src/screencast.cpp — from
        restore data xdg-desktop-portal keeps in the permission store
        (table "screencast", key = token, value = (suv) "KDE", 1, payload).
        The payload is a QDataStream-serialised QVariantMap:
          {"windows": QList<WindowRestoreInfo>{ {appId, title} }}
        xdg-desktop-portal deletes the entry once it is used."""
        import struct
        import uuid
        GLib = self.GLib

        def qstr(v: str) -> bytes:
            b = v.encode("utf-16-be")
            return struct.pack(">I", len(b)) + b

        type_name = b"QList<WindowRestoreInfo>\0"
        payload = (struct.pack(">I", 1) + qstr("windows")
                   + struct.pack(">IB", 65536, 0)                  # QVariant: user type, not null
                   + struct.pack(">I", len(type_name)) + type_name  # ... named by its metatype
                   + struct.pack(">I", 1) + qstr(app_id) + qstr(title))
        token = str(uuid.uuid4())  # xdg-desktop-portal requires a UUID string
        data = GLib.Variant("(suv)", ("KDE", 1, GLib.Variant("ay", payload)))
        self.bus.call_sync(
            "org.freedesktop.impl.portal.PermissionStore", "/org/freedesktop/impl/portal/PermissionStore",
            "org.freedesktop.impl.portal.PermissionStore", "Set",
            GLib.Variant("(sbsa{sas}v)", ("screencast", True, token, {"": ["yes"]}, data)),
            None, self.Gio.DBusCallFlags.NONE, 5000, None)
        return token

    def start_window_cast(self, restore_token: str | None = None) -> tuple[int, int, dict]:
        GLib = self.GLib
        V = GLib.Variant

        tok = self._token()
        res = self._request("CreateSession", V("(a{sv})", ({
            "handle_token": V("s", tok),
            "session_handle_token": V("s", self._token()),
        },)), tok)
        session = res["session_handle"]
        self.session = session

        opts = {
            "handle_token": None,
            "types": V("u", 2),        # WINDOW
            "multiple": V("b", False),
            "cursor_mode": V("u", 1),  # HIDDEN — the real cursor is drawn by the compositor
            "persist_mode": V("u", 2), # persistent until revoked
        }
        if restore_token:
            opts["restore_token"] = V("s", restore_token)
        elif TOKEN_FILE.is_file():
            opts["restore_token"] = V("s", TOKEN_FILE.read_text().strip())
        tok = self._token()
        opts["handle_token"] = V("s", tok)
        self._request("SelectSources", V("(oa{sv})", (session, opts)), tok)

        tok = self._token()
        if "restore_token" not in opts:
            print("[probe] Pick the window to capture in the KDE dialog...")
        res = self._request("Start", V("(osa{sv})", (session, "", {"handle_token": V("s", tok)})), tok)
        if "restore_token" in res:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            TOKEN_FILE.write_text(res["restore_token"])
        node_id, props = res["streams"][0]

        reply, fds = self.bus.call_with_unix_fd_list_sync(
            self.BUS, self.PATH, self.IFACE, "OpenPipeWireRemote",
            V("(oa{sv})", (session, {})), GLib.VariantType("(h)"),
            self.Gio.DBusCallFlags.NONE, -1, None, None)
        fd = fds.get(reply.unpack()[0])
        return fd, node_id, props

    def close_session(self) -> None:
        session = getattr(self, "session", None)
        self.session = None
        if not session:
            return
        try:
            self.bus.call_sync(self.BUS, session, "org.freedesktop.portal.Session", "Close",
                               None, None, self.Gio.DBusCallFlags.NONE, 2000, None)
        except Exception:
            pass


# --------------------------------------------------------------------------
# Cadence statistics
# --------------------------------------------------------------------------

class Cadence:
    def __init__(self, Gst):
        self.Gst = Gst
        self.t: list[float] = []
        self.unique_t: list[float] = []
        self.dups = 0
        self.last_hash: bytes | None = None
        self.caps = None
        self.on_resize = None  # callback(w, h) when the source size changes mid-stream
        import collections
        self.dup_pts = collections.OrderedDict()  # pts -> is_duplicate (bounded)
        self.arrival = collections.OrderedDict()  # pts -> monotonic arrival time (bounded)

    def probe(self, pad, info):
        Gst = self.Gst
        buf = info.get_buffer()
        now = time.monotonic()
        self.t.append(now)
        caps = pad.get_current_caps()
        if caps is not None and (self.caps is None or not caps.is_equal(self.caps)):
            st = caps.get_structure(0)
            w, h = st.get_value('width'), st.get_value('height')
            print(f"[probe] source size {w}x{h}", flush=True)
            changed = self.caps is not None
            self.caps = caps
            if changed and self.on_resize:
                self.on_resize(w, h)
        ok, m = buf.map(Gst.MapFlags.READ)
        if ok:
            data = m.data
            # Sample ~64 KiB spread across the frame: cheap and enough to spot duplicates.
            step = max(1, len(data) // 65536)
            h = hashlib.blake2b(bytes(data[::step]), digest_size=16).digest()
            buf.unmap(m)
            dup = h == self.last_hash
            if dup:
                self.dups += 1
            else:
                self.unique_t.append(now)
            self.last_hash = h
            self.dup_pts[buf.pts] = dup
            self.arrival[buf.pts] = now
            while len(self.dup_pts) > 64:
                self.dup_pts.popitem(last=False)
            while len(self.arrival) > 64:
                self.arrival.popitem(last=False)
        return Gst.PadProbeReturn.OK

    @staticmethod
    def _fps(ts: list[float]) -> dict:
        if len(ts) < 3:
            return {"frames": len(ts)}
        d = [b - a for a, b in zip(ts, ts[1:])]
        span = ts[-1] - ts[0]
        return {
            "frames": len(ts),
            "fps_avg": round((len(ts) - 1) / span, 2),
            "interval_ms_median": round(statistics.median(d) * 1000, 2),
            "interval_ms_p95": round(sorted(d)[int(len(d) * 0.95) - 1] * 1000, 2),
            "interval_ms_stdev": round(statistics.pstdev(d) * 1000, 2),
        }

    def recent(self, window: float = 5.0) -> tuple[float, float]:
        """Delivered and unique fps over the last `window` seconds."""
        now = time.monotonic()
        d = sum(1 for t in self.t if now - t <= window) / window
        u = sum(1 for t in self.unique_t if now - t <= window) / window
        return round(d, 1), round(u, 1)

    def report(self) -> dict:
        s = self.caps.get_structure(0) if self.caps else None
        return {
            "caps": s.to_string() if s else None,
            "delivered": self._fps(self.t),
            "unique_content": self._fps(self.unique_t),
            "duplicate_frames": self.dups,
        }


# --------------------------------------------------------------------------
# mpv presenter (Vulkan, resizable/fullscreen) fed with de-duplicated frames
# --------------------------------------------------------------------------

def nvidia_device_name() -> str | None:
    import subprocess
    try:
        out = subprocess.run(["vulkaninfo", "--summary"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    names = re.findall(r"deviceName\s*=\s*(.+)", out)
    types = re.findall(r"deviceType\s*=\s*(\S+)", out)
    for name, kind in zip(names, types):
        if "DISCRETE" in kind:
            return name.strip()
    return None


def vulkan_devices() -> list[dict]:
    """[{index, name, vendor, device, discrete}] from `vulkaninfo --summary`."""
    import subprocess
    try:
        out = subprocess.run(["vulkaninfo", "--summary"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    devs = []
    for i, block in enumerate(re.split(r"\nGPU\d+:\n", out)[1:]):
        g = lambda k: (re.search(rf"{k}\s*=\s*(.+)", block) or [None, ""])[1].strip()
        devs.append({"index": i, "name": g("deviceName"), "vendor": g("vendorID").replace("0x", ""),
                     "device": g("deviceID").replace("0x", ""), "discrete": "DISCRETE" in g("deviceType")})
    return devs


def monitor_mode() -> dict | None:
    """Largest enabled output's current mode {width, height, hz} (kscreen-doctor)."""
    import subprocess
    try:
        out = subprocess.run(["kscreen-doctor", "-o"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    out = re.sub(r"\x1b\[[0-9;]*m", "", out)
    modes = [(int(w), int(h), float(hz)) for w, h, hz in re.findall(r"\d+:(\d+)x(\d+)@([\d.]+)\*", out)]
    if not modes:
        return None
    w, h, hz = max(modes, key=lambda m: m[0] * m[1])
    return {"width": w, "height": h, "hz": round(hz, 2)}


def system_checks(Gst) -> dict:
    """First-run doctor: everything TweenGlass needs, without changing anything."""
    import glob
    layer_dirs = ["/usr/share/vulkan/implicit_layer.d", "/etc/vulkan/implicit_layer.d",
                  str(Path.home() / ".local/share/vulkan/implicit_layer.d")]
    layers = [f for d in layer_dirs for f in glob.glob(os.path.join(d, "*.json")) if "lsfg" in f.lower()]
    dll = os.environ.get("LSFGVK_DLL_PATH") or (str(find_lossless_dll() or "") or None)
    libs = ("/usr/lib", "/usr/lib64", "/usr/lib/x86_64-linux-gnu", "/usr/local/lib")
    return {
        "lsfg_layer": layers[0] if layers else None,
        "lossless_dll": dll,
        "gpus": vulkan_devices(),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
        "wayland": bool(os.environ.get("WAYLAND_DISPLAY")),
        "layer_shell": any(os.path.exists(os.path.join(d, "libgtk4-layer-shell.so")) for d in libs),
        "pipewire_capture": Gst.ElementFactory.find("pipewiresrc") is not None,
        "display": monitor_mode(),
    }


def monitor_size() -> tuple[int, int]:
    """Current mode of the largest enabled output (kscreen-doctor), fallback 1920x1080."""
    import subprocess
    try:
        out = subprocess.run(["kscreen-doctor", "-o"], capture_output=True, text=True, timeout=5).stdout
        out = re.sub(r"\x1b\[[0-9;]*m", "", out)
        modes = [(int(w), int(h)) for w, h in re.findall(r"\d+:(\d+)x(\d+)@[\d.]+\*", out)]
        if modes:
            return max(modes, key=lambda m: m[0] * m[1])
    except Exception:
        pass
    return (1920, 1080)


class MpvSink:
    """Pipe raw BGRA frames into mpv (gpu-next/Vulkan). Only *new* content
    frames are written, so lsfg-vk interpolates between distinct images."""

    def __init__(self, args, Gst, cadence):
        self.args, self.Gst, self.cad = args, Gst, cadence
        self.proc = None
        self.size = None
        self.last_hash = None
        self.written: list[float] = []       # every frame handed to mpv
        self.base_t: list[float] = []        # ... of which carried NEW source content
        self.present_t: list[float] = []     # buffers mpv's process attached to its surface
        self.hud_text = ""
        self.hud_written = ""
        import threading
        self.cv = threading.Condition()
        self.pending: bytes | None = None
        self.closed = False
        self.proc_ms: float | None = None  # EMA: capture arrival -> frame handed to the presenter
        self.proc_n = 0

    def _spawn(self, w: int, h: int):
        import subprocess
        env = dict(os.environ, **lsfg_env(self.args))
        if self.args.sink == "overlay":
            cmd = [sys.executable, str(Path(__file__).with_name("overlay.py")), str(w), str(h)]
            env["GSK_RENDERER"] = "vulkan"
            # GTK's frame clock coalesces our frames once lsfg-vk adds its own
            # presents (30 in -> ~25 drawn at 2x). We only redraw when a new
            # frame arrives, so painting immediately costs nothing extra.
            env["GDK_DEBUG"] = "no-vsync"
            devs = vulkan_devices()
            dev = (next((d for d in devs if d["index"] == self.args.gpu), None) if self.args.gpu is not None
                   else next((d for d in devs if d["discrete"]), None))
            if dev:
                # GTK always takes Vulkan device 0 (the iGPU here: lsfg ran at ~5 fps).
                # Mesa's device-select layer with "!" hides every other GPU.
                env["MESA_VK_DEVICE_SELECT"] = f"{dev['vendor']}:{dev['device']}!"
                print(f"[probe] overlay GPU: {dev['name']}", flush=True)
            self._popen(cmd, env)
            return
        cmd = [
            "mpv", "--no-config", "--really-quiet", "--title=TweenGlass probe",
            "--demuxer=rawvideo", f"--demuxer-rawvideo-w={w}", f"--demuxer-rawvideo-h={h}",
            "--demuxer-rawvideo-mp-format=bgra", "--demuxer-rawvideo-fps=1000",
            # NOTE: keep the demuxer thread ON. With --demuxer-thread=no the
            # blocking stdin read runs on mpv's main thread, so whenever the
            # source window is static (no new frames) mpv stops handling input:
            # "not responding", can't quit, can't toggle fullscreen.
            "--untimed", "--cache=no", "--demuxer-readahead-secs=0",
            "--vd-queue-enable=no", "--video-latency-hacks=yes",
            "--vo=gpu-next", "--gpu-api=vulkan", "--keepaspect=yes",
            "--osd-level=0", "--input-default-bindings=yes", "--keep-open=yes",
            f"--input-ipc-server={STATE_DIR / 'probe-mpv.sock'}",
        ]
        if not self.args.windowed:
            cmd += ["--fs"]
        dev = nvidia_device_name() if self.args.gpu != 0 else None
        if dev and self.args.gpu is not None:
            cmd.append(f"--vulkan-device={dev}")
        cmd.append("-")
        print(f"[probe] mpv: {' '.join(cmd)}", flush=True)
        self._popen(cmd, env)

    def _popen(self, cmd, env):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        # WAYLAND_DEBUG makes libwayland log every request of the mpv process.
        # Each wl_surface.attach of a buffer is one frame handed to the
        # compositor — including the frames lsfg-vk generates inside
        # vkQueuePresentKHR. That is the real output frame rate.
        import subprocess
        env = dict(env, WAYLAND_DEBUG="client")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, env=env)
        import threading
        threading.Thread(target=self._writer, daemon=True).start()
        threading.Thread(target=self._wayland_reader, daemon=True).start()

    _ATTACH = re.compile(rb"-> wl_surface#(\d+)\.attach\((?!nil)")

    def _wayland_reader(self):
        """Count buffer attaches per surface; the busiest surface is the video."""
        import collections
        per_surface: dict[bytes, collections.deque] = {}
        for line in self.proc.stderr:
            m = self._ATTACH.search(line)
            if m:
                now = time.monotonic()
                dq = per_surface.setdefault(m.group(1), collections.deque(maxlen=1024))
                dq.append(now)
                main = max(per_surface.values(), key=lambda d: sum(1 for t in d if now - t <= 1.0))
                self.present_t = main
            elif not line.startswith(b"[") and line.strip():
                sys.stderr.write("[presenter] " + line.decode(errors="replace"))

    def _writer(self):
        """Write frames off the GStreamer streaming thread; keep only the newest
        pending frame so a slow mpv never stalls capture."""
        while True:
            with self.cv:
                while self.pending is None and not self.closed:
                    self.cv.wait()
                if self.closed:
                    return
                frame, self.pending = self.pending, None
            frame, is_base = frame
            try:
                self.proc.stdin.write(frame)
                now = time.monotonic()
                self.written.append(now)
                if is_base:
                    self.base_t.append(now)
            except (BrokenPipeError, OSError, ValueError):
                return

    def on_sample(self, sink):
        Gst = self.Gst
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        s = sample.get_caps().get_structure(0)
        w, h = s.get_value("width"), s.get_value("height")
        if self.proc is None:
            self.size = (w, h)
            self._spawn(w, h)
        if (w, h) != self.size:  # cannot happen with the fixed canvas caps
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        self.base_t = self.base_t[-512:]
        self.written = self.written[-512:]
        ok, m = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            # Duplicate = same SOURCE content (decided before the HUD is drawn).
            # Present it only if the HUD text changed (e.g. keepalive buffers
            # while the video is paused), never as a "base" frame.
            src_dup = self.cad.dup_pts.get(buf.pts, False)
            hud = self.hud_text
            if src_dup and hud == self.hud_written:
                return Gst.FlowReturn.OK
            self.hud_written = hud
            if self.proc.poll() is None:
                frame = bytes(m.data[: w * h * 4])
                t_in = self.cad.arrival.get(buf.pts)
                if t_in is not None and not src_dup:
                    # Skip the warm-up (shader compile, first allocations) and outliers.
                    self.proc_n += 1
                    ms = (time.monotonic() - t_in) * 1000
                    if self.proc_n > 15 and ms < 250:
                        self.proc_ms = ms if self.proc_ms is None else self.proc_ms * 0.9 + ms * 0.1
                with self.cv:
                    self.pending = (frame, not src_dup)  # replaces an unwritten older frame
                    self.cv.notify()
        finally:
            buf.unmap(m)
        return Gst.FlowReturn.OK

    @staticmethod
    def _rate(ts, window: float) -> float:
        now = time.monotonic()
        return sum(1 for t in list(ts)[-1024:] if now - t <= window) / window

    def recent(self, window: float) -> float:
        return round(self._rate(self.written, window), 1)

    def base_fps(self, window: float = 1.0) -> float:
        return self._rate(self.base_t, window)

    def output_fps(self, window: float = 1.0) -> float:
        return self._rate(self.present_t, window)

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify()
        if self.proc and self.proc.poll() is None:
            # Keep the stderr reader alive while mpv exits: with WAYLAND_DEBUG
            # on, an undrained stderr pipe blocks mpv forever (orphan window).
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()
            try:
                self.proc.stdin.close()
            except OSError:
                pass


QT_MODS = {"ctrl": 0x04000000, "control": 0x04000000, "alt": 0x08000000, "shift": 0x02000000,
           "meta": 0x10000000, "super": 0x10000000}


def qt_key_code(keys: str) -> int | None:
    """'Ctrl+Alt+S' -> Qt key combination int (what kglobalaccel stores)."""
    *mods, key = keys.split("+")
    code = 0
    for m in mods:
        if m.strip().lower() not in QT_MODS:
            return None
        code |= QT_MODS[m.strip().lower()]
    key = key.strip()
    if len(key) == 1 and key.isascii() and key.isalnum():
        return code | ord(key.upper())
    if re.fullmatch(r"F([1-9]|1[0-9]|2[0-4])", key, re.I):
        return code | (0x01000030 + int(key[1:]) - 1)
    return None


def shortcut_conflicts(Gio, GLib, bus, keys: str, own_action: str) -> list[str]:
    """Other kglobalaccel actions bound to the same keys. kglobalaccel lets
    an app's .desktop X-KDE-Shortcuts (e.g. Crow Translate: Ctrl+Alt+S =
    speak selection) keep a key that a KWin script also registers, and the
    other one wins: the shortcut then silently does nothing."""
    code = qt_key_code(keys)
    if code is None:
        return []
    try:  # args: (QKeySequence as (ai), SearchType as (i))
        res = bus.call_sync("org.kde.kglobalaccel", "/kglobalaccel", "org.kde.KGlobalAccel", "globalShortcutsByKey",
                            GLib.Variant("((ai)(i))", (([code],), (0,))), None, Gio.DBusCallFlags.NONE, 3000, None)
    except Exception:
        return []
    out = []
    for action, friendly, comp, comp_friendly, *_ in res.unpack()[0]:
        if action != own_action:
            out.append(f"{comp_friendly or comp} — {friendly or action}")
    return out


def altgr_variant(keys: str) -> str | None:
    """Ctrl+Alt+<letter> pressed with the RIGHT Alt on layouts where it is
    AltGr (us-intl, br-abnt2, de, ...) reaches KWin as Ctrl+<AltGr symbol>,
    which never matches. Register that symbol too for the current layout."""
    m = re.fullmatch(r"(?i)ctrl\+alt\+([a-z])", keys.strip())
    if not m:
        return None
    try:
        import subprocess
        layout = subprocess.run(["setxkbmap", "-query"], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return None
    # AltGr level-3 symbols for the letter S on common layouts; extend as needed.
    table = {("us", "intl"): {"s": "ß"}, ("us", "altgr-intl"): {"s": "ß"}, ("de", ""): {"s": "ſ"},
             ("br", ""): {"s": "§"}}
    lay = (re.search(r"layout:\s*(\S+)", layout) or [None, ""])[1].split(",")[0]
    var = (re.search(r"variant:\s*(\S+)", layout) or [None, ""])[1].split(",")[0]
    sym = table.get((lay, var), {}).get(m.group(1).lower())
    return f"Ctrl+{sym}" if sym else None


class Notifier:
    """Desktop notifications (notify-send), replacing the previous one so
    toggling quickly doesn't pile them up. The overlay is click-through and
    keyboard-less, so this is the only feedback that a shortcut was received."""

    def __init__(self):
        from shutil import which
        self.exe = which("notify-send")
        self.last_id = None

    def __call__(self, summary: str, body: str = "", urgent: bool = False):
        print(f"[probe] {summary}{' — ' + body if body else ''}", flush=True)
        if not self.exe:
            return
        import subprocess
        cmd = [self.exe, "-a", "TweenGlass", "-i", "video-display", "-p", "-t", "3000",
               "-h", "boolean:transient:true", "-u", "critical" if urgent else "low"]
        if self.last_id:
            cmd += ["-r", self.last_id]
        cmd += [summary, body]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout.strip()
            self.last_id = out or self.last_id
        except Exception:
            pass


class Shortcuts:
    """Global shortcuts and window queries via KWin scripts, calling back into
    the backend over D-Bus.

    The overlay is click-through and never takes keyboard focus, so it can only
    be driven by global shortcuts. The toggle callback receives the window that
    is active when the keys are pressed (KWin knows it; Wayland clients don't)."""

    NAME = "dev.tweenglass.Probe"
    PATH = "/dev/tweenglass/Probe"
    SCRIPT = "tweenglass_probe_shortcuts"
    XML = f"""<node><interface name="{NAME}">
      <method name="Quit"/>
      <method name="Toggle"><arg type="s" name="app_id" direction="in"/>
        <arg type="s" name="title" direction="in"/><arg type="b" name="normal" direction="in"/>
        <arg type="s" name="pid" direction="in"/></method>
      <method name="Windows"><arg type="s" name="json" direction="in"/></method>
    </interface></node>"""

    def __init__(self, Gio, GLib, bus, on_quit, on_toggle, quit_keys: str | None, toggle_keys: str,
                 on_windows=None):
        import subprocess
        self.sp, self.ok, self.conflicts, self.alt = subprocess, False, [], None
        self.Gio, self.GLib, self.bus = Gio, GLib, bus
        node = Gio.DBusNodeInfo.new_for_xml(self.XML)

        def call(_c, _s, _p, _i, method, params, inv):
            inv.return_value(None)
            if method == "Quit":
                GLib.idle_add(lambda: (on_quit(), False)[1])
            elif method == "Toggle":
                app_id, title, normal, pid = params.unpack()
                GLib.idle_add(lambda: (on_toggle(app_id, title, normal, int(pid) if pid.isdigit() else None), False)[1])
            elif method == "Windows" and on_windows:
                (payload,) = params.unpack()
                GLib.idle_add(lambda: (on_windows(json.loads(payload)), False)[1])

        bus.register_object(self.PATH, node.interfaces[0], call, None, None)
        Gio.bus_own_name_on_connection(bus, self.NAME, Gio.BusNameOwnerFlags.REPLACE, None, None)
        self.register(toggle_keys, quit_keys)

    def register(self, toggle_keys: str, quit_keys: str | None = None) -> None:
        n, pth = self.NAME, self.PATH
        js = f"""
function tgToggle() {{
    var w = workspace.activeWindow;
    if (!w) {{ callDBus("{n}", "{pth}", "{n}", "Toggle", "", "", false, ""); return; }}
    callDBus("{n}", "{pth}", "{n}", "Toggle", String(w.desktopFileName || w.resourceClass || ""),
             String(w.caption || ""), !!w.normalWindow, String(w.pid || ""));
}}
registerShortcut("TweenGlassProbeToggle", "TweenGlass: frame generation on/off for the active window",
                 "{toggle_keys}", tgToggle);
"""
        if quit_keys:
            js += (f'registerShortcut("TweenGlassProbeStop", "TweenGlass: quit", "{quit_keys}", function() {{'
                   f' callDBus("{n}", "{pth}", "{n}", "Quit"); }});\n')
        self.alt = altgr_variant(toggle_keys)
        if self.alt:
            js += (f'registerShortcut("TweenGlassProbeToggleAltGr", "TweenGlass: on/off (AltGr variant)", '
                   f'"{self.alt}", tgToggle);\n')
        self.close()
        # kglobalaccel keeps the old key of an action that is registered again
        # with a new default: drop the actions so the new keys take effect.
        for action in ("TweenGlassProbeToggle", "TweenGlassProbeToggleAltGr", "TweenGlassProbeStop"):
            try:
                self.bus.call_sync("org.kde.kglobalaccel", "/kglobalaccel", "org.kde.KGlobalAccel", "unregister",
                                   self.GLib.Variant("(ss)", ("kwin", action)), None,
                                   self.Gio.DBusCallFlags.NONE, 2000, None)
            except Exception:
                pass
        self.ok = self._run_script(js, self.SCRIPT)
        if self.ok:
            print(f"[probe] {toggle_keys}{' (or ' + self.alt + ')' if self.alt else ''} = frame generation on/off "
                  f"for the active window{'; ' + quit_keys + ' = quit' if quit_keys else ''}", flush=True)
            self.conflicts = shortcut_conflicts(self.Gio, self.GLib, self.bus, toggle_keys, "TweenGlassProbeToggle")
        else:
            self.conflicts = []
            print("[probe] WARNING: could not register KWin shortcuts (KDE Plasma only)", file=sys.stderr)

    def _run_script(self, js: str, name: str) -> bool:
        path = STATE_DIR / f"{name}.js"
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(js)
        self._kwin("unloadScript", name)
        sid = self._kwin("loadScript", str(path), name).strip()
        if sid.lstrip("-").isdigit() and int(sid) >= 0:
            self.sp.run(["qdbus6", "org.kde.KWin", f"/Scripting/Script{sid}", "org.kde.kwin.Script.run"],
                        capture_output=True)
            return True
        return False

    def _oneshot(self, js: str, name: str) -> bool:
        ok = self._run_script(js, name)
        self.GLib.timeout_add(1500, lambda: (self._kwin("unloadScript", name), False)[1])
        return ok

    def list_windows(self, exclude_pid: int | None = None) -> bool:
        """Ask KWin for the normal windows; the answer arrives via on_windows()."""
        n, pth = self.NAME, self.PATH
        return self._oneshot(f"""
var a = workspace.activeWindow, out = [];
for (const w of workspace.windowList()) {{
    if (!w.normalWindow || w.skipTaskbar || w.pid == {exclude_pid or -1}) continue;
    out.push({{ id: String(w.internalId), app_id: String(w.desktopFileName || w.resourceClass || ""),
               title: String(w.caption || ""), fullscreen: !!w.fullScreen, minimized: !!w.minimized,
               active: w === a, pid: Number(w.pid || 0) }});
}}
callDBus("{n}", "{pth}", "{n}", "Windows", JSON.stringify(out));
""", "tweenglass_list_windows")

    def focus_window(self, window_id: str) -> bool:
        wid = json.dumps(window_id)
        return self._oneshot(f"""
for (const w of workspace.windowList()) if (String(w.internalId) === {wid}) {{
    w.minimized = false; workspace.activeWindow = w;
}}""", "tweenglass_focus_window")

    def _kwin(self, method, *a) -> str:
        try:
            return self.sp.run(["qdbus6", "org.kde.KWin", "/Scripting", f"org.kde.kwin.Scripting.{method}", *a],
                               capture_output=True, text=True, timeout=5).stdout
        except Exception:
            return ""

    def close(self):
        if self.ok:
            self.ok = False
            self._kwin("unloadScript", self.SCRIPT)


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=0,
                    help="quit after N seconds (0 = run until the quit shortcut / Ctrl+C)")
    ap.add_argument("--now", action="store_true",
                    help="start capturing immediately (last window / portal dialog) instead of waiting for the toggle shortcut")
    ap.add_argument("--sink", choices=["overlay", "mpv", "vulkan", "none"], default="overlay",
                    help="overlay: click-through fullscreen layer-shell overlay (GTK4/Vulkan) — default; "
                         "mpv: fullscreen mpv window (Vulkan, takes clicks); "
                         "vulkan: GStreamer vulkansink (XWayland, fixed-size); none: measure only")
    ap.add_argument("--no-display", action="store_true", help="alias for --sink none")
    ap.add_argument("--canvas", type=lambda v: tuple(int(x) for x in v.lower().split("x")), default=None,
                    help="presenter input size WxH (default: largest active monitor mode)")
    ap.add_argument("--windowed", action="store_true", help="mpv: start windowed instead of fullscreen")
    ap.add_argument("--toggle-keys", default="Ctrl+Alt+S",
                    help="global shortcut: frame generation on/off for the active window (KDE)")
    ap.add_argument("--stop-keys", default="Meta+Alt+Q", help="global shortcut that quits the probe (KDE)")
    ap.add_argument("--no-hud", action="store_true",
                    help="hide the 'base / output' fps counter drawn in the top-left corner")
    ap.add_argument("--multiplier", type=int, default=2)
    ap.add_argument("--flow-scale", type=float, default=1.0)
    ap.add_argument("--performance", action="store_true")
    ap.add_argument("--filter", choices=filters.FILTERS, default="off",
                    help="scaling/sharpening filter (GPU): " + ", ".join(filters.FILTERS))
    ap.add_argument("--sharpness", type=float, default=0.5, help="0..1, for fsr / sgsr / bcas")
    ap.add_argument("--no-audio-sync", action="store_true",
                    help="don't delay the captured app's audio to match the overlay")
    ap.add_argument("--audio-offset", type=float, default=0.0,
                    help="ms added to the automatic audio delay (negative = less delay)")
    ap.add_argument("--forget", action="store_true", help="drop the saved portal restore token")
    ap.add_argument("--gpu", type=int, default=None,
                    help="Vulkan physical device index (see `vulkaninfo --summary`; default: the discrete GPU)")
    ap.add_argument("--ipc", action="store_true",
                    help="UI mode: JSON events on stdout, JSON commands on stdin (one object per line)")
    args = ap.parse_args()

    # In IPC mode stdout carries only protocol lines; human logs go to stderr.
    ipc_out = None
    if args.ipc:
        ipc_out = sys.stdout
        sys.stdout = sys.stderr
    import threading
    ipc_lock = threading.Lock()

    def emit(kind: str, **data) -> None:
        if ipc_out is None:
            return
        with ipc_lock:
            ipc_out.write(json.dumps({"type": kind, **data}) + "\n")
            ipc_out.flush()
    if args.no_display:
        args.sink = "none"
    if args.canvas is None:
        args.canvas = monitor_size()

    if args.gpu is not None:
        os.environ["GST_VULKAN_DEVICE"] = str(args.gpu)

    if args.forget and TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
    if args.sink != "none":
        configure_lsfg(args.multiplier, args.flow_scale, args.performance)

    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gio, GLib, Gst

    Gst.init(None)
    portal = Portal(Gio, GLib)  # keep alive: owns the session D-Bus state

    # Normalise to BGRA (KWin already sends BGRA/BGRx; videoconvert is then a
    # passthrough). vulkansink needs vulkancolorconvert in front of it.
    def presenter_chain() -> str:
        # Fixed canvas: the source window changes size (e.g. the browser going
        # fullscreen 1920x970 -> 1920x1080), but the presenter's raw input cannot.
        # Letterbox every frame into the canvas instead of dropping it.
        if args.filter != "off":
            scale = f"videoconvert ! {filters.gl_chain(args.filter, args.canvas)} ! "
        else:
            scale = ("videoconvert ! videoscale add-borders=true method=bilinear ! "
                     f"video/x-raw,format=BGRA,width={args.canvas[0]},height={args.canvas[1]},pixel-aspect-ratio=1/1 ! ")
        # max-buffers=1: never queue a stale frame in front of the presenter (latency).
        return scale + f"{HUD} ! appsink name=out emit-signals=true max-buffers=1 drop=true sync=false"

    has_presenter = args.sink in ("mpv", "overlay")

    def sink_chain() -> str:
        return {
            "none": "fakesink sync=false",
            "vulkan": "videoconvert ! video/x-raw,format=BGRA ! vulkanupload ! vulkancolorconvert ! vulkansink sync=false",
        }.get(args.sink) or presenter_chain()

    audio = AudioSync(log=lambda m: print(m, flush=True))

    cad = Cadence(Gst)
    loop = GLib.MainLoop()
    notify = Notifier()
    state = {"pipe": None, "mpv": None, "restart_id": 0, "restarts": 0, "formats": 0, "hud": None,
             "active": False, "target": None, "t0": None, "pid": None}

    def on_msg(_b, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            src = msg.src.get_name() if msg.src else ""
            if args.filter != "off" and src.startswith(("tgf", "gl")):
                # No usable OpenGL (ES) for the filter: keep going without it.
                print(f"[probe] filter {args.filter} failed ({err.message}); disabling it", file=sys.stderr)
                emit("error", message=f"The {args.filter} filter could not run on this GPU ({err.message}). "
                                      "Frame generation continues without it.")
                args.filter = "off"
                emit_state("settings")
            print(f"[probe] GStreamer error: {err.message} — reconnecting\n{dbg}", file=sys.stderr)
            schedule_restart()
        elif msg.type == Gst.MessageType.EOS:
            deactivate("source ended")

    def build(restore_token: str | None = None):
        fd, node, props = portal.start_window_cast(restore_token)
        print(f"[probe] PipeWire node {node} props={dict(props)}", flush=True)
        # Target by object serial: with pipewiresrc >= 1.x, `path=<node id>` may be
        # ignored and silently fall back to the default source (e.g. the webcam).
        serial = props.get("pipewire-serial")
        target = f"target-object={serial}" if serial is not None else f"path={node}"
        keepalive = "keepalive-time=500 " if has_presenter else ""
        pipe = Gst.parse_launch(
            f"pipewiresrc name=src fd={fd} {target} do-timestamp=true {keepalive}"
            f"stream-properties=\"props,node.dont-fallback=(boolean)true,node.dont-reconnect=(boolean)true\" "
            f"! video/x-raw ! queue max-size-buffers=2 leaky=downstream ! {sink_chain()}")
        if has_presenter and args.filter != "off":
            filters.configure(pipe, Gst, args.filter, args.canvas, args.sharpness)
        pipe.get_by_name("src").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, cad.probe)
        if state["mpv"]:
            pipe.get_by_name("out").connect("new-sample", state["mpv"].on_sample)
            state["hud"] = pipe.get_by_name("hud")
            state["hud"].set_property("silent", args.no_hud)
        bus = pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message", on_msg)
        pipe.set_state(Gst.State.PLAYING)
        state["pipe"] = pipe

    def teardown_stream():
        if state["restart_id"]:
            GLib.source_remove(state["restart_id"])
            state["restart_id"] = 0
        old = state["pipe"]
        state["pipe"], state["hud"] = None, None
        if old is not None:
            old.get_bus().remove_signal_watch()
            old.set_state(Gst.State.NULL)
        portal.close_session()
        cad.caps = None  # the next stream's first size is not a "change"
        state["formats"] = 0

    def restart():
        state["restart_id"] = 0
        if not state["active"]:
            return False
        state["restarts"] += 1
        t = time.monotonic()
        teardown_stream()
        try:
            build()  # token returned by the previous Start (same window, no dialog)
        except BaseException as e:  # noqa: BLE001 — portal refused; give up on this window
            print(f"[probe] reconnect failed: {e}", file=sys.stderr)
            deactivate("reconnect failed")
            return False
        print(f"[probe] stream reconnected in {(time.monotonic() - t) * 1000:.0f} ms "
              f"(#{state['restarts']})", flush=True)
        return False

    def schedule_restart(*_):
        # KWin's window cast stops producing frames after a mid-stream resize
        # (browser fullscreen 1920x970 <-> 1920x1080): one frame arrives at the
        # new size, then nothing. A fresh portal session (restore token, no
        # dialog) captures the new size fine. Debounce so the fullscreen
        # animation settles first; otherwise the new stream starts at the stale
        # size and breaks again.
        def arm():
            if not state["active"]:
                return False
            if state["restart_id"]:
                GLib.source_remove(state["restart_id"])
            state["restart_id"] = GLib.timeout_add(RESTART_DEBOUNCE_MS, restart)
            return False
        GLib.idle_add(arm)  # called from the streaming thread

    cad.on_resize = schedule_restart

    # Detect the renegotiation itself: the first frame at the new size can come
    # seconds late (or never), but pipewiresrc logs the format change at once.
    # GStreamer exposes no signal for it, so tap its DEBUG log (a handful of
    # lines per stream, not per frame).
    def pw_log(cat, _level, _file, func, _line, _obj, msg, _data):
        if cat.get_name() != "pipewiresrc":
            return
        if func == "handle_format_change" and msg.get().startswith("doing finish format"):
            state["formats"] += 1
            if state["formats"] > 1:
                print("[probe] source renegotiated (window resized) — reconnecting", flush=True)
                schedule_restart()

    if not os.environ.get("GST_DEBUG"):
        Gst.debug_remove_log_function(None)  # keep stderr clean
    Gst.debug_add_log_function(pw_log, None)
    Gst.debug_set_active(True)
    Gst.debug_set_threshold_for_name("pipewiresrc", Gst.DebugLevel.DEBUG)

    def emit_state(reason: str | None = None):
        app_id, title = state["target"] or (None, None)
        emit("state", active=state["active"], reason=reason,
             target={"app_id": app_id, "title": title} if state["active"] else None,
             settings={"multiplier": args.multiplier, "flow_scale": args.flow_scale,
                       "performance": args.performance, "hud": not args.no_hud, "gpu": args.gpu,
                       "toggle_keys": args.toggle_keys, "filter": args.filter, "sharpness": args.sharpness,
                       "audio_sync": not args.no_audio_sync, "audio_offset_ms": args.audio_offset})

    def audio_target_ms() -> float:
        mpv = state["mpv"]
        base = mpv.base_fps(3.0) if mpv and mpv.proc else 0.0
        # The on-screen model (1 frame + 5 ms) was measured with the CPU path,
        # whose processing takes ~3.5 ms; add whatever a GPU filter costs on top.
        extra = max(0.0, (mpv.proc_ms or 0.0) - 3.5) if mpv else 0.0
        return max(0.0, video_latency_ms(base) + extra + args.audio_offset)

    def start_audio():
        if args.no_audio_sync or not state["active"] or not has_presenter:
            return
        if not audio.start(state["pid"], (state["target"] or ("", ""))[0] or "", audio_target_ms()):
            emit("error", message="Audio sync unavailable (needs PipeWire with pw-loopback, pw-metadata and pactl).")

    def activate(app_id: str | None = None, title: str | None = None, quiet: bool = False, pid: int | None = None):
        token = None
        if app_id is not None:
            try:
                token = portal.forge_window_token(app_id, title or "")
            except Exception as e:  # noqa: BLE001
                print(f"[probe] could not pre-select the window ({e}); the portal will ask", file=sys.stderr)
        state["mpv"] = MpvSink(args, Gst, cad) if has_presenter else None
        state["active"], state["target"], state["t0"] = True, (app_id, title), time.monotonic()
        state["pid"] = pid
        cad.__init__(Gst)
        cad.on_resize = schedule_restart
        try:
            build(token)
        except BaseException as e:  # noqa: BLE001 — SystemExit from a cancelled portal dialog
            deactivate("start failed", quiet=True)
            emit("error", message=f"Could not capture the window: {e}")
            notify("TweenGlass: could not capture the window", str(e), urgent=True)
            return
        start_audio()
        emit_state("activated")
        if not quiet:
            notify(f"TweenGlass ON — {args.multiplier}x", title or "picked window")

    def deactivate(reason: str = "toggled off", quiet: bool = False):
        if not state["active"]:
            return
        state["active"] = False
        audio.stop()
        teardown_stream()
        if state["mpv"]:
            state["mpv"].close()
            state["mpv"] = None
        emit_state(reason)
        if reason == "quit" or quiet:
            print(f"[probe] OFF ({reason})", flush=True)
        else:
            notify("TweenGlass OFF", reason)

    def restart_presenter():
        """Apply new lsfg-vk settings: they are read when the presenter starts."""
        if not state["active"]:
            return
        teardown_stream()
        if state["mpv"]:
            state["mpv"].close()
        state["mpv"] = MpvSink(args, Gst, cad) if has_presenter else None
        try:
            build()  # same window: token returned by the last Start
        except BaseException as e:  # noqa: BLE001
            deactivate("restart failed", quiet=True)
            emit("error", message=f"Could not restart with the new settings: {e}")
            return
        emit_state("settings applied")

    def rebuild_stream():
        """Apply a new filter: rebuild only the capture pipeline; the presenter
        (and lsfg-vk) keep running because the canvas size does not change."""
        if not state["active"]:
            return
        teardown_stream()
        try:
            build()
        except BaseException as e:  # noqa: BLE001
            deactivate("restart failed", quiet=True)
            emit("error", message=f"Could not apply the filter: {e}")

    def on_toggle(app_id: str, title: str, normal: bool, pid: int | None = None):
        if state["active"]:
            deactivate()
            return
        if not normal or not app_id:
            notify("TweenGlass: click the window first", f"can't capture {title or 'the desktop/panel'}")
            return
        activate(app_id, title, pid=pid)

    def on_windows(items):
        emit("windows", items=items)

    shortcuts = (Shortcuts(Gio, GLib, portal.bus, loop.quit, on_toggle,
                           None if args.ipc else args.stop_keys, args.toggle_keys, on_windows)
                 if has_presenter else None)

    def emit_ready():
        emit("ready", toggle_keys=args.toggle_keys, altgr_keys=shortcuts.alt if shortcuts else None,
             shortcuts_ok=bool(shortcuts and shortcuts.ok), conflicts=shortcuts.conflicts if shortcuts else [])

    # ---- UI commands (one JSON object per line on stdin)
    def handle(cmd: dict):
        c = cmd.get("cmd")
        if c == "list_windows":
            if not (shortcuts and shortcuts.list_windows(exclude_pid=os.getppid())):
                emit("windows", items=[])
        elif c == "activate":
            if state["active"]:
                deactivate("switching window", quiet=True)
            app_id, title, wid = cmd.get("app_id") or "", cmd.get("title") or "", cmd.get("id")
            pid = int(cmd["pid"]) if str(cmd.get("pid") or "").isdigit() and int(cmd["pid"]) > 0 else None
            if shortcuts and wid:
                shortcuts.focus_window(wid)  # raise it: the overlay mirrors it and clicks land on it
            GLib.timeout_add(350, lambda: (activate(app_id, title, quiet=True, pid=pid), False)[1])
        elif c == "deactivate":
            deactivate("stopped", quiet=True)
        elif c == "set":
            lsfg_changed = False
            if "multiplier" in cmd:
                v = max(2, min(4, int(cmd["multiplier"])))
                lsfg_changed |= v != args.multiplier
                args.multiplier = v
            if "flow_scale" in cmd:
                v = max(0.25, min(1.0, float(cmd["flow_scale"])))
                lsfg_changed |= v != args.flow_scale
                args.flow_scale = v
            if "performance" in cmd:
                v = bool(cmd["performance"])
                lsfg_changed |= v != args.performance
                args.performance = v
            if "gpu" in cmd:
                v = cmd["gpu"] if cmd["gpu"] is None else int(cmd["gpu"])
                lsfg_changed |= v != args.gpu
                args.gpu = v
            if "hud" in cmd:
                args.no_hud = not bool(cmd["hud"])
                if state["hud"] is not None:
                    state["hud"].set_property("silent", args.no_hud)
            filter_changed = False
            if "filter" in cmd and cmd["filter"] in filters.FILTERS and cmd["filter"] != args.filter:
                args.filter, filter_changed = cmd["filter"], True
            if "sharpness" in cmd:
                v = max(0.0, min(1.0, float(cmd["sharpness"])))
                filter_changed |= v != args.sharpness and args.filter in filters.SHARPNESS_FILTERS
                args.sharpness = v
            if "audio_offset_ms" in cmd:
                args.audio_offset = max(-200.0, min(500.0, float(cmd["audio_offset_ms"])))
                if audio.sink:
                    audio.set_delay(audio_target_ms())
            if "audio_sync" in cmd:
                args.no_audio_sync = not bool(cmd["audio_sync"])
                if args.no_audio_sync:
                    audio.stop()
                elif state["active"] and not audio.sink:
                    start_audio()
            if cmd.get("toggle_keys") and cmd["toggle_keys"] != args.toggle_keys and shortcuts:
                args.toggle_keys = cmd["toggle_keys"]
                shortcuts.register(args.toggle_keys)
                emit_ready()
            if lsfg_changed:
                restart_presenter()
            elif filter_changed:
                rebuild_stream()
            emit_state("settings")
        elif c == "checks":
            emit("checks", **system_checks(Gst))
        elif c == "state":
            emit_state()
        elif c == "quit":
            loop.quit()
        else:
            emit("error", message=f"unknown command {c!r}")

    stdin_buf = bytearray()

    def on_stdin(_source, condition):
        # Read raw bytes: a buffered readline() would swallow several queued
        # commands at once and GLib would never wake us up for the rest.
        data = b""
        if condition & GLib.IOCondition.IN:
            try:
                data = os.read(sys.stdin.fileno(), 65536)
            except BlockingIOError:
                return True
        if not data:
            loop.quit()  # EOF: the UI went away
            return False
        stdin_buf.extend(data)
        while b"\n" in stdin_buf:
            line, _, rest = bytes(stdin_buf).partition(b"\n")
            stdin_buf[:] = rest
            if not line.strip():
                continue
            try:
                handle(json.loads(line))
            except Exception as e:  # noqa: BLE001 — never let a bad command kill the backend
                emit("error", message=f"bad command {line[:200]!r}: {e}")
        return True

    if args.ipc:
        GLib.io_add_watch(GLib.IOChannel.unix_new(sys.stdin.fileno()), GLib.PRIORITY_DEFAULT,
                          GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR, on_stdin)

    # Always close the presenter and the KWin script, even on SIGTERM/SIGHUP.
    import atexit, signal

    def cleanup():
        deactivate("quit")
        audio.stop()
        if shortcuts:
            shortcuts.close()
    atexit.register(cleanup)
    try:
        gi.require_version("GLibUnix", "2.0")
        from gi.repository import GLibUnix
        signal_add = GLibUnix.signal_add
    except (ImportError, ValueError):
        signal_add = GLib.unix_signal_add
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal_add(GLib.PRIORITY_HIGH, sig, lambda *_: (loop.quit(), False)[1])

    if args.ipc:
        emit("hello", protocol=1)
        emit_ready()
        emit_state("started")
    elif args.now or not shortcuts:
        activate()
    else:
        if shortcuts.conflicts:
            notify(f"TweenGlass: {args.toggle_keys} is taken",
                   "Also bound to " + "; ".join(shortcuts.conflicts)
                   + ". Free it in System Settings → Shortcuts, or use --toggle-keys.", urgent=True)
        else:
            notify("TweenGlass ready", f"Click the video window and press {args.toggle_keys}")
    if args.seconds:
        GLib.timeout_add(int(args.seconds * 1000), loop.quit)

    def progress():
        mpv = state["mpv"]
        if state["active"]:
            d, u = cad.recent(5.0)
            extra = (f" base={mpv.base_fps(5.0):.1f}fps output={mpv.output_fps(5.0):.1f}fps"
                     f" (written={mpv.recent(5.0)})") if mpv and mpv.proc else ""
            print(f"[probe] t+{time.monotonic() - state['t0']:.0f}s last5s: delivered={d}fps unique={u}fps{extra} "
                  f"dups_total={cad.dups} reconnects={state['restarts']}", flush=True)
        if mpv and mpv.proc and mpv.proc.poll() is not None:
            deactivate("presenter closed")
        return True

    GLib.timeout_add_seconds(5, progress)

    def stats_tick():
        mpv = state["mpv"]
        if state["active"] and mpv and mpv.proc:
            emit("stats", base=round(mpv.base_fps(2.0), 1), output=round(mpv.output_fps(2.0), 1),
                 capture=round(cad.recent(2.0)[1], 1), reconnects=state["restarts"],
                 audio_delay_ms=round(audio.delay_ms) if audio.sink else None, audio_streams=audio.streams,
                 processing_ms=round(mpv.proc_ms, 1) if mpv.proc_ms is not None else None)
        return True

    if args.ipc:
        GLib.timeout_add(1000, stats_tick)

    def hud_tick():
        mpv = state["mpv"]
        if mpv and mpv.proc:
            text = f"{mpv.base_fps():.0f} / {mpv.output_fps():.0f}"
            mpv.hud_text = text
            if state["hud"] is not None:
                state["hud"].set_property("text", text)
        return True

    GLib.timeout_add(500, hud_tick)

    tick = {"n": 0}

    def audio_tick():
        # New streams appear whenever the app starts another video/tab; follow
        # the content frame rate (24/30/60 fps) every few seconds.
        if state["active"] and audio.sink:
            audio.poll()
            tick["n"] += 1
            mpv = state["mpv"]
            settled = state["t0"] is not None and time.monotonic() - state["t0"] > 3
            if settled and tick["n"] % 3 == 0 and mpv and mpv.base_fps(3.0) >= 5 and mpv.proc_ms is not None:
                audio.set_delay(audio_target_ms())
        return True

    GLib.timeout_add(1000, audio_tick)
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    had_data = bool(cad.t)
    cleanup()

    if had_data and not args.ipc:
        rep = cad.report()
        print("[probe] RESULT (last activation) " + json.dumps(rep, indent=2))
        out = Path(os.environ.get("TWEENGLASS_PROBE_OUT", "phase0-result.json"))
        out.write_text(json.dumps(rep, indent=2))
        print(f"[probe] saved {out.resolve()}")


if __name__ == "__main__":
    main()
