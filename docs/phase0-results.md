# Phase 0 — results

Setup: Dell G15 5530 (i5-13450HX), KDE Plasma 6.8.2 Wayland, Intel RPL-S iGPU + RTX 3050 6GB Laptop (NVIDIA 620.70), lsfg-vk 2.0.0, GStreamer 1.28.8, PipeWire 1.6.10. Panel: 1920×1080 @ 120.21 Hz, scale 1.2.

## 2026-11-10 — Widevine DRM clip (Bitmovin DRM demo, Chrome), renderer on RTX 3050

| Check | Result |
|---|---|
| Window capture via portal | ✅ BGRA 1920×970, `framerate=0/1` (damage-driven), `max-framerate=120.213` |
| DRM video visible in capture | ✅ Widevine video frames visible, not black |
| Cadence follows content | ✅ median interval **33.25 ms** (≈30 fps content) while playing; nothing is sent while paused |
| Duplicate frames | ⚠️ ~13% of delivered frames during playback were byte-identical (player UI / cursor damage) → **the renderer must dedupe** before presenting |
| lsfg-vk hooks the process | ✅ `Using profile '(environment)'`, `Initializing lsfg-vk instance with half precision enabled`, no errors (iGPU and dGPU) |
| Lossless.dll discovery | ✅ found on a secondary Steam library through `libraryfolders.vdf` |
| Visual smoothness | ⏳ subjective check pending |

Raw: delivered 694 frames, 604 unique, during roughly 30 s of playback followed by a pause. The averages over the 120 s window are diluted by the pause, so read the medians.

## Gotchas found
- Keep the D-Bus connection referenced, or the portal kills the session.
- Set `node.dont-fallback=true` on the stream, or PipeWire silently links the webcam.
- The GStreamer Vulkan sink in this build only opens XCB windows (XWayland). This does not matter for the real renderer.
- After a reboot, Hermes may lack `DISPLAY`/`WAYLAND_DISPLAY`/`XAUTHORITY`. Import them from `systemctl --user show-environment`.

## 2026-11-17 — Source window resize (browser fullscreen) on KWin 6.8.3

Symptom: the overlay froze for good as soon as the browser went fullscreen
(and stayed frozen after leaving fullscreen).

Measured with a 120 fps canvas page and a playing YouTube video:

| Scenario | Capture fps |
|---|---|
| Stream started windowed (1920x970) | ~110 |
| Same stream after window → fullscreen | 0 (one frame at 1920x1080, then nothing) |
| Stream started while already fullscreen (1920x1080) | ~35–55 (follows content) |
| Source covered by another fullscreen window / not minimized | rAF unaffected (Chrome does not throttle) |

Cause: KWin's window screencast stops producing frames after a mid-stream
renegotiation to a new size (`Failed to create an offscreen framebuffer`,
`GL_FRAMEBUFFER_INCOMPLETE_ATTACHMENT` in the KWin log; the renegotiated
memfd buffers are 1/10 of the frame size). Independent of `use-bufferpool`.
Not an mpv problem.

Workaround (probe): watch `pipewiresrc`'s format renegotiation (DEBUG log
tap — it fires immediately, while the first new-size frame may never come)
and, 600 ms later (fullscreen animation settles), close the portal session and
open a new one with the restore token: no dialog, ~20–40 ms gap. Verified over
4 fullscreen/windowed toggles with mpv + lsfg-vk running (no stall, mpv kept
presenting ~46 fps).

Renderer requirement: treat any source format change as "reconnect the
stream" on KWin until upstream is fixed; worth an upstream KWin bug report.

The KWin `showfps` overlay is now unloaded on every exit path (normal end,
mpv closed, SIGINT/SIGTERM/SIGHUP); before it stayed on screen when the probe
was killed.

## 2026-11-17 — Real output fps and click-through overlay

**KWin `showfps` is not a valid metric.** The effect repaints the whole screen
on every vblank, so it always reads the refresh rate (120/120 here),
whatever the overlay presents. Removed from the probe.

**Real output fps** = buffers the presenter process attaches to its
`wl_surface` (`WAYLAND_DEBUG=client`, `-> wl_surface#N.attach(`). lsfg-vk's
generated frames go through the same WSI, so they are counted. Validated
with a 30 fps source:

| Presenter | lsfg off | x2 | x3 | x4 |
|---|---|---|---|---|
| mpv window (720p) | 30 | 60 | — | 120 |
| GTK4 overlay, default frame clock | 30 | ~52 | ~71 | ~93 |
| GTK4 overlay, `GDK_DEBUG=no-vsync` | 30 | **60** | **90** | 111 |

x4 of 30 fps on a 120 Hz output leaves no headroom, so about 1 base frame/s
is dropped.

**Click-through overlay** (`tools/phase0/overlay.py`): GTK4 + gtk4-layer-shell
(layer OVERLAY, anchored to all edges, exclusive zone -1, keyboard NONE) with
an empty `wl_surface.set_input_region`. GSK Vulkan renderer, so lsfg-vk hooks
it. Pitfalls:
- gtk4-layer-shell must be loaded before libwayland → re-exec with
  `LD_PRELOAD=libgtk4-layer-shell.so`.
- GTK always picks Vulkan device 0 (the Intel iGPU here; lsfg ran at ~5 fps
  there) and ignores `__NV_PRIME_RENDER_OFFLOAD` and `GDK_VULKAN_DEVICE` →
  `MESA_VK_DEVICE_SELECT=10de:25ec!`.
- GTK's frame clock merges frames once lsfg adds its own presents → `GDK_DEBUG=no-vsync`
  (we only queue a redraw per new frame, so it costs nothing).
- A layer-shell overlay is above fullscreen windows. An mpv/xdg_toplevel
  window drops below the source as soon as the user clicks a fullscreen
  source (KWin's active fullscreen layer).

The HUD (`base / output`) is burned into the frames with GStreamer
`textoverlay`, so it adds no presents. `keepalive-time` keeps it updating
while the source is static (paused video shows `0 / 0`).

## 2026-11-17 — Hotkey toggle on the active window (no picker dialog)

`Ctrl+Alt+S` (KWin `registerShortcut`) reads `workspace.activeWindow` and
calls the probe over D-Bus with `(desktopFileName, caption)`. To capture that
exact window without the portal dialog, the probe writes its own restore entry
into the permission store and passes its token to `SelectSources`:

- xdg-desktop-portal 1.22: the token must be a UUID string
  (`g_uuid_string_is_valid`), the entry lives in table `screencast` with
  permissions `{"": ["yes"]}` (host apps), and it is deleted after use.
- xdg-desktop-portal-kde 6.8: data = `(suv) ("KDE", 1, payload)`, where payload is a
  QDataStream `QVariantMap {"windows": QList<WindowRestoreInfo>{(appId, title)}}`
  (user-type QVariant: u32 65536, u8 0, QByteArray type name, list).
  Windows are matched by appId plus exact or close title (Levenshtein < len/2).

Verified with a 30 fps mpv test window: toggle ON → overlay showed that
window (`31 / 60` at x2), OFF → presenter gone, ON again works, quit shortcut
unloads the KWin script with no leftover processes. This depends on KDE
internals. The Rust renderer should use KWin's `zkde_screencast_unstable_v1`
`stream_window(uuid)` directly on Plasma, with the portal + dialog as the
generic fallback.


## 2026-11-17 — A/V latency and GPU filters

Pedro's complaint: with TweenGlass on, voices come before the lips.

### Video latency, measured on screen

Method: a source mpv window shows the frame number as 10 binary bands. The
overlay is restricted to the rest of the screen (`TWEENGLASS_OVERLAY_REGION`
debug env, `x,y,w,h` in logical px). Each KWin screenshot (spectacle) contains
the source and its copy at the same instant, so
`(source frame − overlay frame) / fps` is the added delay. The resolution is
one source frame, so these are means over 12–16 screenshots. Test conditions:
1080p fullscreen source, overlay almost fullscreen, lsfg-vk 2.0 on the RTX
3050, 120 Hz. Harness: `tg_latfs.py` (scratch, not committed).

| source fps | lsfg | filter | delay |
|---|---|---|---|
| 30 | off | – | 38–42 ms |
| 30 | 2x | – | 38–45 ms |
| 30 | 3x | – | 39 ms |
| 24 | 2x | – | 48 ms |
| 30 | 2x | FSR | 57 ms |
| 30 | 2x | BCAS | ~67 ms (all reads = 2 frames) |

Model: `1 source frame + 5 ms + (processing − 3.5 ms)`, where processing is
measured live per frame from pipewiresrc arrival to the presenter handoff
(3.5–3.7 ms on the CPU path).

### Audio delay

`pw-loopback --delay D` adds exactly D. This was measured inaudibly: a
combine-sink sends the same click directly to a null sink and through the
loopback, and the two clicks land 40/100/200 ms apart for D = 40/100/200 ms.
The loopback's own buffering is compensated, so `--delay 0` adds nothing.
The app's streams are matched by PID tree (window PID from KWin), falling back
to the app name for Flatpak. They are moved via `pw-metadata <id> target.object`
and the metadata is deleted on stop. WirePlumber's `stream-properties` stays
clean; `AudioSync._forget_targets` cleans it if a target ever gets saved.

### GPU filters (GStreamer GL, Intel iGPU via Mesa EGL)

Per-frame cost at 1080p, from chain entry to a mapped (downloaded) buffer:
nearest 7.3 ms, SGSR 9.3, bicubic 13.4, FSR 16.9, BCAS 17.3. The CPU bilinear
path is 3.5 ms. Benchmarks must map the output buffer: gldownload is lazy, and
a fakesink alone measured 0.7 ms.

- Dropping the final glcolorconvert saves ~1 ms: the last shader writes BGRA
  byte order into an RGBA-labelled buffer. Relabelling GL memory with
  capssetter breaks the video meta (`gst_video_frame_map_id: assertion
  'info->finfo->format == meta->format'`).
- NVIDIA EGL (`__EGL_VENDOR_LIBRARY_FILENAMES=…10_nvidia.json`) can't create
  an EGLDisplay on this KWin session. The filter falls back to off, with a UI
  error.
- 1:1 sources (fullscreen video) skip the scaling kernel; only the sharpening
  pass does work.
