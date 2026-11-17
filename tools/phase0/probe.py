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


def configure_lsfg(multiplier: int, flow_scale: float, performance: bool) -> None:
    os.environ.setdefault("LSFGVK_ENV", "1")
    os.environ.setdefault("LSFGVK_MULTIPLIER", str(multiplier))
    os.environ.setdefault("LSFGVK_FLOW_SCALE", str(flow_scale))
    os.environ.setdefault("LSFGVK_PERFORMANCE_MODE", "1" if performance else "0")
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
            while len(self.dup_pts) > 64:
                self.dup_pts.popitem(last=False)
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

    def _spawn(self, w: int, h: int):
        import subprocess
        env = dict(os.environ)
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
    """Global shortcuts (KWin script) that call back into the probe over D-Bus.

    The overlay is click-through and never takes keyboard focus, so it can only
    be driven by global shortcuts. The toggle callback receives the window that
    is active when the keys are pressed (KWin knows it; Wayland clients don't)."""

    NAME = "dev.tweenglass.Probe"
    PATH = "/dev/tweenglass/Probe"
    SCRIPT = "tweenglass_probe_shortcuts"
    XML = f"""<node><interface name="{NAME}">
      <method name="Quit"/>
      <method name="Toggle"><arg type="s" name="app_id" direction="in"/>
        <arg type="s" name="title" direction="in"/><arg type="b" name="normal" direction="in"/></method>
    </interface></node>"""

    def __init__(self, Gio, GLib, bus, on_quit, on_toggle, quit_keys: str, toggle_keys: str):
        import subprocess
        self.sp, self.ok = subprocess, False
        node = Gio.DBusNodeInfo.new_for_xml(self.XML)

        def call(_c, _s, _p, _i, method, params, inv):
            inv.return_value(None)
            if method == "Quit":
                GLib.idle_add(lambda: (on_quit(), False)[1])
            elif method == "Toggle":
                app_id, title, normal = params.unpack()
                GLib.idle_add(lambda: (on_toggle(app_id, title, normal), False)[1])

        bus.register_object(self.PATH, node.interfaces[0], call, None, None)
        Gio.bus_own_name_on_connection(bus, self.NAME, Gio.BusNameOwnerFlags.REPLACE, None, None)
        n, pth = self.NAME, self.PATH
        js = f"""
registerShortcut("TweenGlassProbeStop", "TweenGlass probe: quit", "{quit_keys}", function() {{
    callDBus("{n}", "{pth}", "{n}", "Quit");
}});
function tgToggle() {{
    var w = workspace.activeWindow;
    if (!w) {{ callDBus("{n}", "{pth}", "{n}", "Toggle", "", "", false); return; }}
    callDBus("{n}", "{pth}", "{n}", "Toggle", String(w.desktopFileName || w.resourceClass || ""),
             String(w.caption || ""), !!w.normalWindow);
}}
registerShortcut("TweenGlassProbeToggle", "TweenGlass probe: frame generation on/off for the active window",
                 "{toggle_keys}", tgToggle);
"""
        alt = altgr_variant(toggle_keys)
        if alt:
            js += (f'registerShortcut("TweenGlassProbeToggleAltGr", "TweenGlass probe: on/off (AltGr variant)", '
                   f'"{alt}", tgToggle);\n')
        path = STATE_DIR / "shortcuts.js"
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(js)
        self._kwin("unloadScript", self.SCRIPT)
        sid = self._kwin("loadScript", str(path), self.SCRIPT).strip()
        if sid.lstrip("-").isdigit() and int(sid) >= 0:
            self.sp.run(["qdbus6", "org.kde.KWin", f"/Scripting/Script{sid}", "org.kde.kwin.Script.run"],
                        capture_output=True)
            self.ok = True
            print(f"[probe] {toggle_keys}{' (or ' + alt + ')' if alt else ''} = frame generation on/off "
                  f"for the active window; {quit_keys} = quit", flush=True)
        else:
            print("[probe] WARNING: could not register KWin shortcuts (KDE Plasma only)", file=sys.stderr)

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
    ap.add_argument("--forget", action="store_true", help="drop the saved portal restore token")
    ap.add_argument("--gpu", type=int, default=None,
                    help="Vulkan physical device index (see `vulkaninfo --summary`; default: the discrete GPU)")
    args = ap.parse_args()
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
    presenter_chain = (
        # Fixed canvas: the source window changes size (e.g. the browser going
        # fullscreen 1920x970 -> 1920x1080), but the presenter's raw input cannot.
        # Letterbox every frame into the canvas instead of dropping it.
        "videoconvert ! videoscale add-borders=true method=bilinear ! "
        f"video/x-raw,format=BGRA,width={args.canvas[0]},height={args.canvas[1]},pixel-aspect-ratio=1/1 ! "
        f"{HUD} ! appsink name=out emit-signals=true max-buffers=2 drop=true sync=false")
    sink = {
        "none": "fakesink sync=false",
        "vulkan": "videoconvert ! video/x-raw,format=BGRA ! vulkanupload ! vulkancolorconvert ! vulkansink sync=false",
        "overlay": presenter_chain,
        "mpv": presenter_chain,
    }[args.sink]
    has_presenter = args.sink in ("mpv", "overlay")

    cad = Cadence(Gst)
    loop = GLib.MainLoop()
    notify = Notifier()
    state = {"pipe": None, "mpv": None, "restart_id": 0, "restarts": 0, "formats": 0, "hud": None,
             "active": False, "target": None, "t0": None}

    def on_msg(_b, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
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
            f"! video/x-raw ! queue max-size-buffers=2 leaky=downstream ! {sink}")
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

    def activate(app_id: str | None = None, title: str | None = None):
        token = None
        if app_id is not None:
            try:
                token = portal.forge_window_token(app_id, title or "")
            except Exception as e:  # noqa: BLE001
                print(f"[probe] could not pre-select the window ({e}); the portal will ask", file=sys.stderr)
        state["mpv"] = MpvSink(args, Gst, cad) if has_presenter else None
        state["active"], state["target"], state["t0"] = True, (app_id, title), time.monotonic()
        cad.__init__(Gst)
        cad.on_resize = schedule_restart
        try:
            build(token)
        except BaseException as e:  # noqa: BLE001 — SystemExit from a cancelled portal dialog
            deactivate("start failed")
            notify("TweenGlass: could not capture the window", str(e), urgent=True)
            return
        notify(f"TweenGlass ON — {args.multiplier}x", title or "picked window")

    def deactivate(reason: str = "toggled off"):
        if not state["active"]:
            return
        state["active"] = False
        teardown_stream()
        if state["mpv"]:
            state["mpv"].close()
            state["mpv"] = None
        if reason == "quit":
            print(f"[probe] OFF ({reason})", flush=True)
        else:
            notify("TweenGlass OFF", reason)

    def on_toggle(app_id: str, title: str, normal: bool):
        if state["active"]:
            deactivate()
            return
        if not normal or not app_id:
            notify("TweenGlass: click the window first", f"can't capture {title or 'the desktop/panel'}")
            return
        activate(app_id, title)

    shortcuts = (Shortcuts(Gio, GLib, portal.bus, loop.quit, on_toggle, args.stop_keys, args.toggle_keys)
                 if has_presenter else None)

    # Always close the presenter and the KWin script, even on SIGTERM/SIGHUP.
    import atexit, signal

    def cleanup():
        deactivate("quit")
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

    if args.now or not shortcuts:
        activate()
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

    def hud_tick():
        mpv = state["mpv"]
        if mpv and mpv.proc:
            text = f"{mpv.base_fps():.0f} / {mpv.output_fps():.0f}"
            mpv.hud_text = text
            if state["hud"] is not None:
                state["hud"].set_property("text", text)
        return True

    GLib.timeout_add(500, hud_tick)
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    had_data = bool(cad.t)
    cleanup()

    if had_data:
        rep = cad.report()
        print("[probe] RESULT (last activation) " + json.dumps(rep, indent=2))
        out = Path(os.environ.get("TWEENGLASS_PROBE_OUT", "phase0-result.json"))
        out.write_text(json.dumps(rep, indent=2))
        print(f"[probe] saved {out.resolve()}")


if __name__ == "__main__":
    main()
