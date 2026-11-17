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

    def start_window_cast(self) -> tuple[int, int, dict]:
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
        if TOKEN_FILE.is_file():
            opts["restore_token"] = V("s", TOKEN_FILE.read_text().strip())
        tok = self._token()
        opts["handle_token"] = V("s", tok)
        self._request("SelectSources", V("(oa{sv})", (session, opts)), tok)

        tok = self._token()
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
            if h == self.last_hash:
                self.dups += 1
            else:
                self.unique_t.append(now)
            self.last_hash = h
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

    def __init__(self, args, Gst):
        self.args, self.Gst = args, Gst
        self.proc = None
        self.size = None
        self.last_hash = None
        self.written: list[float] = []
        import threading
        self.cv = threading.Condition()
        self.pending: bytes | None = None
        self.closed = False

    def _spawn(self, w: int, h: int):
        import subprocess
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
            # --ontop: stay above the source window when the user focuses it to
            # toggle its fullscreen (the real renderer uses a layer-shell overlay).
            cmd += ["--fs", "--ontop"]
        dev = nvidia_device_name() if self.args.gpu != 0 else None
        if dev and self.args.gpu is not None:
            cmd.append(f"--vulkan-device={dev}")
        cmd.append("-")
        print(f"[probe] mpv: {' '.join(cmd)}", flush=True)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, bufsize=0)
        import threading
        threading.Thread(target=self._writer, daemon=True).start()

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
            try:
                self.proc.stdin.write(frame)
                self.written.append(time.monotonic())
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
        ok, m = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = m.data
            step = max(1, len(data) // 65536)
            hsh = hashlib.blake2b(bytes(data[::step]), digest_size=16).digest()
            if hsh == self.last_hash:
                return Gst.FlowReturn.OK  # duplicate: do not present
            self.last_hash = hsh
            if self.proc.poll() is None:
                frame = bytes(data[: w * h * 4])
                with self.cv:
                    self.pending = frame  # replaces an unwritten older frame
                    self.cv.notify()
        finally:
            buf.unmap(m)
        return Gst.FlowReturn.OK

    def recent(self, window: float) -> float:
        now = time.monotonic()
        return round(sum(1 for t in self.written if now - t <= window) / window, 1)

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            self.proc.terminate()


class KWinShowFps:
    """Load KWin's showfps effect for the duration of the probe (restored after)."""

    def __init__(self):
        import subprocess
        self.sp = subprocess
        self.qdbus = "qdbus6" if self._has("qdbus6") else "qdbus"
        self.was_loaded = "showfps" in self._call("loadedEffects")
        if not self.was_loaded:
            self._call("loadEffect", "showfps")
            print("[probe] KWin showfps effect loaded (compositor fps overlay)", flush=True)

    def _has(self, exe):
        from shutil import which
        return which(exe) is not None

    def _call(self, method, *a):
        r = self.sp.run([self.qdbus, "org.kde.KWin", "/Effects", f"org.kde.kwin.Effects.{method}", *a],
                        capture_output=True, text=True)
        return r.stdout

    def close(self):
        if not self.was_loaded and not getattr(self, "closed", False):
            self.closed = True
            self._call("unloadEffect", "showfps")


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=60, help="measurement time after the stream starts")
    ap.add_argument("--sink", choices=["mpv", "vulkan", "none"], default="mpv",
                    help="mpv: deduped frames piped to a fullscreen, resizable mpv (Vulkan) — default; "
                         "vulkan: GStreamer vulkansink (XWayland, fixed-size); none: measure only")
    ap.add_argument("--no-display", action="store_true", help="alias for --sink none")
    ap.add_argument("--canvas", type=lambda v: tuple(int(x) for x in v.lower().split("x")), default=None,
                    help="mpv input size WxH (default: largest active monitor mode)")
    ap.add_argument("--windowed", action="store_true", help="mpv: start windowed instead of fullscreen")
    ap.add_argument("--kwin-fps", action="store_true",
                    help="temporarily load KWin's 'showfps' effect (compositor fps = output fps of the overlay)")
    ap.add_argument("--multiplier", type=int, default=2)
    ap.add_argument("--flow-scale", type=float, default=1.0)
    ap.add_argument("--performance", action="store_true")
    ap.add_argument("--forget", action="store_true", help="drop the saved portal restore token")
    ap.add_argument("--gpu", type=int, default=None,
                    help="Vulkan physical device index for vulkansink (see `vulkaninfo --summary`)")
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
    sink = {
        "none": "fakesink sync=false",
        "vulkan": "videoconvert ! video/x-raw,format=BGRA ! vulkanupload ! vulkancolorconvert ! vulkansink sync=false",
        # Fixed canvas: the source window changes size (e.g. the browser going
        # fullscreen 1920x970 -> 1920x1080), but mpv's rawvideo input cannot.
        # Letterbox every frame into the canvas instead of dropping it.
        "mpv": ("videoconvert ! videoscale add-borders=true method=bilinear ! "
                f"video/x-raw,format=BGRA,width={args.canvas[0]},height={args.canvas[1]},pixel-aspect-ratio=1/1 ! "
                "appsink name=out emit-signals=true max-buffers=2 drop=true sync=false"),
    }[args.sink]

    cad = Cadence(Gst)
    mpv = MpvSink(args, Gst) if args.sink == "mpv" else None
    kwin_fps = KWinShowFps() if args.kwin_fps else None
    loop = GLib.MainLoop()

    # Always undo side effects (KWin fps overlay, mpv) even on SIGTERM/SIGHUP —
    # otherwise the compositor fps counter stays on screen after the probe dies.
    import atexit, signal

    def cleanup():
        if mpv:
            mpv.close()
        if kwin_fps:
            kwin_fps.close()
    atexit.register(cleanup)
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, lambda *_: (loop.quit(), False)[1])

    state = {"pipe": None, "restart_id": 0, "restarts": 0, "formats": 0}

    def on_msg(_b, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"[probe] GStreamer error: {err.message} — reconnecting\n{dbg}", file=sys.stderr)
            schedule_restart()
        elif msg.type == Gst.MessageType.EOS:
            loop.quit()

    def build():
        fd, node, props = portal.start_window_cast()
        print(f"[probe] PipeWire node {node} props={dict(props)}", flush=True)
        # Target by object serial: with pipewiresrc >= 1.x, `path=<node id>` may be
        # ignored and silently fall back to the default source (e.g. the webcam).
        serial = props.get("pipewire-serial")
        target = f"target-object={serial}" if serial is not None else f"path={node}"
        pipe = Gst.parse_launch(
            f"pipewiresrc name=src fd={fd} {target} do-timestamp=true "
            f"stream-properties=\"props,node.dont-fallback=(boolean)true,node.dont-reconnect=(boolean)true\" "
            f"! video/x-raw ! queue max-size-buffers=2 leaky=downstream ! {sink}")
        pipe.get_by_name("src").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, cad.probe)
        if mpv:
            pipe.get_by_name("out").connect("new-sample", mpv.on_sample)
        bus = pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message", on_msg)
        pipe.set_state(Gst.State.PLAYING)
        state["pipe"] = pipe

    def restart():
        state["restart_id"] = 0
        state["restarts"] += 1
        t = time.monotonic()
        old = state["pipe"]
        if old is not None:
            old.get_bus().remove_signal_watch()
            old.set_state(Gst.State.NULL)
        portal.close_session()
        cad.caps = None  # the next stream's first size is not a "change"
        state["formats"] = 0
        build()
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

    build()
    print(f"[probe] Measuring for {args.seconds:.0f}s — play the video now.")
    GLib.timeout_add(int(args.seconds * 1000), loop.quit)

    t0 = time.monotonic()

    def progress():
        d, u = cad.recent(5.0)
        extra = f" presented_to_mpv={mpv.recent(5.0)}" if mpv else ""
        print(f"[probe] t+{time.monotonic() - t0:.0f}s last5s: delivered={d}fps unique={u}fps{extra} "
              f"dups_total={cad.dups} reconnects={state['restarts']}", flush=True)
        if mpv and mpv.proc and mpv.proc.poll() is not None:
            print("[probe] mpv closed — stopping", flush=True)
            loop.quit()
            return False
        return True

    GLib.timeout_add_seconds(5, progress)
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    if state["pipe"] is not None:
        state["pipe"].set_state(Gst.State.NULL)
    portal.close_session()
    cleanup()

    rep = cad.report()
    print("[probe] RESULT " + json.dumps(rep, indent=2))
    out = Path(os.environ.get("TWEENGLASS_PROBE_OUT", "phase0-result.json"))
    out.write_text(json.dumps(rep, indent=2))
    print(f"[probe] saved {out.resolve()}")


if __name__ == "__main__":
    main()
