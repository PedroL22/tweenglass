# Phase 0 — results

Setup: Lenovo laptop, KDE Plasma 6.8.2 Wayland, Intel RPL-S iGPU + RTX 3050 6GB Laptop (NVIDIA 620.70), lsfg-vk 2.0.0, GStreamer 1.28.8, PipeWire 1.6.10. Panel: 1920×1080 @ 120.21 Hz, scale 1.2.

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
