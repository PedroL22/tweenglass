#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""TweenGlass — Phase 0 click-through overlay presenter.

Reads raw BGRX frames (WIDTH x HEIGHT, 4 bytes/px) from stdin and shows them
on a full-output wlr-layer-shell surface (layer OVERLAY) with an EMPTY input
region: mouse clicks and scrolling go to the window underneath, so the
captured player can still be paused, seeked, etc. Keyboard focus is never
taken.

GTK draws with its Vulkan renderer (GSK_RENDERER=vulkan, set by the
launcher), so the user-installed lsfg-vk layer hooks this process and
interpolates between the frames we present. A frame is presented only when a
new one arrives: no redundant presents.

Usage (normally spawned by probe.py):
  overlay.py WIDTH HEIGHT < frames
"""

from __future__ import annotations

import os
import sys
import threading

if __name__ == "__main__" and "libgtk4-layer-shell.so" not in os.environ.get("LD_PRELOAD", ""):
    pass  # handled in ensure_layer_shell_preload() before GTK is used

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, GLib, Gtk, Gtk4LayerShell as LayerShell  # noqa: E402

import cairo  # noqa: E402


def main() -> None:
    w, h = int(sys.argv[1]), int(sys.argv[2])
    frame_size = w * h * 4
    stride = w * 4

    app = Gtk.Application(application_id="dev.tweenglass.Overlay")
    state: dict = {"pending": None, "scheduled": False, "recv": 0, "shown": 0, "replaced": 0}
    stats = bool(os.environ.get("TWEENGLASS_OVERLAY_STATS"))
    lock = threading.Lock()

    def on_activate(app: Gtk.Application) -> None:
        win = Gtk.ApplicationWindow(application=app, title="TweenGlass overlay")
        LayerShell.init_for_window(win)
        LayerShell.set_namespace(win, "tweenglass")
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
        region = os.environ.get("TWEENGLASS_OVERLAY_REGION")  # debug/measurement: "x,y,w,h" (logical px)
        if region:
            # Anchor all four edges and let margins carve out the rectangle: the
            # compositor then dictates the size (a picture's natural size can't grow it).
            rx, ry, rw, rh = (int(v) for v in region.split(","))
            mon = Gdk.Display.get_default().get_monitors().get_item(0).get_geometry()
            margins = {LayerShell.Edge.LEFT: rx, LayerShell.Edge.TOP: ry,
                       LayerShell.Edge.RIGHT: mon.width - rx - rw, LayerShell.Edge.BOTTOM: mon.height - ry - rh}
            for edge, m in margins.items():
                LayerShell.set_margin(win, edge, max(0, m))
        for edge in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM, LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(win, edge, True)
        LayerShell.set_exclusive_zone(win, -1)  # cover panels too
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.NONE)

        css = Gtk.CssProvider()
        css.load_from_string("window, picture { background: black; }")
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        pic = Gtk.Picture()
        pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        pic.set_can_target(False)
        win.set_child(pic)

        def make_click_through(*_):
            surface = win.get_surface()
            if surface is not None:
                surface.set_input_region(cairo.Region())  # empty: all input passes through

        win.connect("realize", make_click_through)
        win.connect("map", make_click_through)
        win.present()
        make_click_through()

        def show_latest() -> bool:
            with lock:
                data, state["pending"] = state["pending"], None
                state["scheduled"] = False
            if data is not None:
                state["shown"] += 1
                tex = Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.B8G8R8X8, GLib.Bytes.new(data), stride)
                pic.set_paintable(tex)
            return False

        def reader() -> None:
            stdin = sys.stdin.buffer
            while True:
                buf = bytearray(frame_size)
                view = memoryview(buf)
                got = 0
                while got < frame_size:
                    n = stdin.readinto(view[got:])
                    if not n:
                        GLib.idle_add(app.quit)
                        return
                    got += n
                with lock:
                    state["recv"] += 1
                    if state["pending"] is not None:
                        state["replaced"] += 1
                    state["pending"] = bytes(buf)  # newest wins; older unshown frame is dropped
                    if not state["scheduled"]:
                        state["scheduled"] = True
                        GLib.idle_add(show_latest, priority=GLib.PRIORITY_HIGH)

        threading.Thread(target=reader, daemon=True).start()

        if stats:
            def report() -> bool:
                print(f"[overlay] recv={state['recv']} shown={state['shown']} replaced={state['replaced']}",
                      file=sys.stderr, flush=True)
                return True
            GLib.timeout_add_seconds(1, report)

    app.connect("activate", on_activate)
    app.hold()
    sys.exit(app.run([sys.argv[0]]))


LAYER_SHELL_LIB = "libgtk4-layer-shell.so"


def ensure_layer_shell_preload() -> None:
    """gtk4-layer-shell must be loaded before libwayland-client, which Python's
    gi import cannot guarantee — re-exec once with LD_PRELOAD."""
    if LAYER_SHELL_LIB in os.environ.get("LD_PRELOAD", ""):
        return
    for d in ("/usr/lib", "/usr/lib64", "/usr/lib/x86_64-linux-gnu", "/usr/local/lib"):
        path = os.path.join(d, LAYER_SHELL_LIB)
        if os.path.exists(path):
            pre = os.environ.get("LD_PRELOAD", "")
            os.environ["LD_PRELOAD"] = f"{path} {pre}".strip()
            os.execv(sys.executable, [sys.executable, *sys.argv])
    sys.exit("[overlay] gtk4-layer-shell not found (install gtk4-layer-shell)")


if __name__ == "__main__":
    ensure_layer_shell_preload()
    os.environ.setdefault("GSK_RENDERER", "vulkan")
    main()
