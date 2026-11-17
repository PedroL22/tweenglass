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
