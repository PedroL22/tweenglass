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

    def probe(self, pad, info):
        Gst = self.Gst
        buf = info.get_buffer()
        now = time.monotonic()
        self.t.append(now)
        if self.caps is None:
            self.caps = pad.get_current_caps()
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

    def report(self) -> dict:
        s = self.caps.get_structure(0) if self.caps else None
        return {
            "caps": s.to_string() if s else None,
            "delivered": self._fps(self.t),
            "unique_content": self._fps(self.unique_t),
            "duplicate_frames": self.dups,
        }


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=60, help="measurement time after the stream starts")
    ap.add_argument("--no-display", action="store_true", help="only measure cadence, no Vulkan sink")
    ap.add_argument("--multiplier", type=int, default=2)
    ap.add_argument("--flow-scale", type=float, default=1.0)
    ap.add_argument("--performance", action="store_true")
    ap.add_argument("--forget", action="store_true", help="drop the saved portal restore token")
    args = ap.parse_args()

    if args.forget and TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
    if not args.no_display:
        configure_lsfg(args.multiplier, args.flow_scale, args.performance)

    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gio, GLib, Gst

    Gst.init(None)
    fd, node, props = Portal(Gio, GLib).start_window_cast()
    print(f"[probe] PipeWire node {node} props={dict(props)}")

    # vulkancolorconvert asserts on PipeWire's padded BGRx/RGBx formats
    # (gst-plugins-bad 1.28, swizzle assertion) — convert to RGBA on the CPU
    # first. The real renderer will sample BGRx directly in a shader.
    sink = "fakesink sync=false" if args.no_display else \
        "videoconvert ! video/x-raw,format=RGBA ! vulkanupload ! vulkansink sync=false"
    pipe = Gst.parse_launch(
        f"pipewiresrc name=src fd={fd} path={node} do-timestamp=true keepalive-time=0 "
        f"! video/x-raw ! queue max-size-buffers=2 leaky=downstream ! {sink}")

    cad = Cadence(Gst)
    pipe.get_by_name("src").get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, cad.probe)

    loop = GLib.MainLoop()
    bus = pipe.get_bus()
    bus.add_signal_watch()

    def on_msg(_b, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"[probe] GStreamer error: {err.message}\n{dbg}", file=sys.stderr)
            loop.quit()
        elif msg.type == Gst.MessageType.EOS:
            loop.quit()

    bus.connect("message", on_msg)
    pipe.set_state(Gst.State.PLAYING)
    print(f"[probe] Measuring for {args.seconds:.0f}s — play the video now.")
    GLib.timeout_add(int(args.seconds * 1000), loop.quit)

    def progress():
        if cad.t:
            r = cad.report()
            print(f"[probe] t+{time.monotonic() - cad.t[0]:.0f}s delivered={r['delivered'].get('fps_avg')} "
                  f"unique={r['unique_content'].get('fps_avg')} dups={r['duplicate_frames']}", flush=True)
        return True

    GLib.timeout_add_seconds(5, progress)
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    pipe.set_state(Gst.State.NULL)

    rep = cad.report()
    print("[probe] RESULT " + json.dumps(rep, indent=2))
    out = Path(os.environ.get("TWEENGLASS_PROBE_OUT", "phase0-result.json"))
    out.write_text(json.dumps(rep, indent=2))
    print(f"[probe] saved {out.resolve()}")


if __name__ == "__main__":
    main()
