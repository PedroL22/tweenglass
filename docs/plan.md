# TweenGlass — roadmap

## Phase 0: validation (Python probe, throwaway)
`tools/phase0/probe.py`: portal window capture → PipeWire → `vulkansink`, with lsfg-vk on.

Exit criteria:
- [ ] DRM video (Netflix in Chrome/Firefox) appears in the capture, not a black frame.
- [ ] The cadence of unique frames is close to the content fps (≈24 for films, ≈60 for 60 fps YouTube), with a low interval stdev.
- [ ] lsfg-vk logs `Using profile ... (environment)` and initializes a swapchain with no errors.
- [ ] Visually, the interpolated output looks smoother than the source.
- [ ] The same checks pass on the dGPU (RTX 3050 laptop), not only on the iGPU.

## Phase 1: renderer MVP (Rust)
- `ashpd` portal session with a restore token, and `pipewire-rs` stream (SHM first, then DMA-BUF import with an SHM fallback).
- Vulkan presenter with `ash`, FIFO present, **presenting only when a new unique frame arrives**.
- Wayland `zwlr_layer_shell_v1` overlay: fullscreen on the source's output, empty input region (click-through), native pixels under fractional scaling (`wp_fractional_scale_v1` + `wp_viewporter`).
- Refresh rate auto-detected from `wl_output`, and content fps measured live → multiplier suggestion.
- GPU selection: pin to the dGPU on hybrid laptops (lsfg-vk must run on the presenting GPU).
- JSON-over-stdio control protocol (start/stop/settings/stats).

## Phase 2: UI (Electron + React + shadcn/ui)
- Window picker (portal) with a live thumbnail, one large **Activate** button, and a global shortcut (GlobalShortcuts portal).
- Presets: Film/series, YouTube 60, Game; auto-suggested from measured fps and display Hz.
- Advanced: multiplier, flow scale, performance mode, GPU, on-screen FPS counter.
- Tray icon.
- First-run doctor: lsfg-vk layer present? Lossless.dll found in **any** Steam library (parse `libraryfolders.vdf`)? Which GPU? Is the driver healthy?

## Phase 3: reach
- "Follow window" mode (KWin script reports window geometry).
- GNOME fallback (XWayland always-on-top window plus an XShape input pass-through).
- MAKO backend (adaptive target fps, e.g. 24 → 120 Hz).
- Packaging: AppImage, .deb, .rpm, AUR (no Flatpak at first: the sandbox hides host Vulkan layers).

## Hard rules
- Never bundle or redistribute lsfg-vk (CC BY-NC-ND 4.0) or any Lossless Scaling file.
- Never edit the user's `~/.config/lsfg-vk/conf.toml`. Use per-process env vars (`LSFGVK_ENV=1`, `LSFGVK_MULTIPLIER`, `LSFGVK_FLOW_SCALE`, `LSFGVK_PERFORMANCE_MODE`, `LSFGVK_DLL_PATH`) or a private `LSFGVK_CONFIG`.
