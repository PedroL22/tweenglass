# TweenGlass

**Frame generation for any window on Linux.** Watch a 24/30 fps video (Netflix, YouTube, any player) at 48–120 fps, in the style of Lossless Scaling on Windows.

> [!WARNING]
> **Tested only on CachyOS with KDE Plasma 6.8 (Wayland)**, on one laptop: an NVIDIA RTX 3050 Laptop GPU plus an Intel iGPU, with a 1920×1080 @ 120 Hz screen at 120% scale.
> Other distributions, desktops and GPUs have **not been tested**. Some parts are KDE-only today: the global shortcut, picking a window without a dialog, and the window list. Expect rough edges anywhere else, and please open an issue with what you find.

> Status: **alpha**. The current engine is the Phase 0 Python/GStreamer prototype (`tools/phase0/`). A Rust renderer is planned (see [`docs/plan.md`](docs/plan.md)).

## What it does

TweenGlass captures a window and re-presents it from its own Vulkan process in an always-on-top, **click-through** overlay. Because the overlay is a Vulkan app, your installed [lsfg-vk](https://github.com/PancakeTAS/lsfg-vk) layer interpolates it, using the frame generation from Lossless Scaling.

```
any window ─(xdg-desktop-portal ScreenCast + PipeWire)─► capture ─► [GPU filter] ─► overlay (GTK4/Vulkan, layer-shell)
                                                                                      │ only new content frames
                                                                                      ▼
                                                                         lsfg-vk (installed by you) ─► display
the window's audio ─► PipeWire delay (= video latency) ─► your speakers/headphones
```

- **Works with DRM video:** Netflix and other Widevine players in the browser are captured like any other window.
- **Click-through:** pause or seek the video through the overlay. Keyboard focus stays on the window below.
- **One shortcut:** focus a window and press `Ctrl+Alt+S` (configurable) to turn it on or off. You can also pick the window in the app.
- **Real fps counter** in the top-left corner (`original / interpolated`), counted from the frames actually handed to the compositor.
- **A/V sync:** the overlay shows the video slightly late (about one source frame). TweenGlass delays that app's audio by the same amount, automatically, so voices stay on the lips. Details below.
- **Scaling and sharpening filters** on the GPU (see below).
- Tray app. Diagnostics on first run. English and Brazilian Portuguese.

## Requirements

- Linux, Wayland session, **KDE Plasma 6** (the only tested desktop).
- [lsfg-vk](https://github.com/PancakeTAS/lsfg-vk) installed, and **Lossless Scaling** owned and installed through Steam. Any Steam library folder works.
- A Vulkan GPU. On hybrid laptops the overlay runs on the dedicated GPU.
- GStreamer 1.24+ with `pipewiresrc` and the GL elements (gst-plugins-base/good/bad), `python-gobject`, `gtk4`, `gtk4-layer-shell`.
- PipeWire with `pw-loopback`, `pw-metadata` and `pactl` (for A/V sync).
- [Bun](https://bun.sh) (or npm) to build the app.

TweenGlass does **not** include lsfg-vk or any Lossless Scaling file. It detects and uses what you installed.

## Install (from source)

```sh
git clone https://github.com/PedroL22/tweenglass
cd tweenglass/app
bun install
bun run build
scripts/install-desktop-entry.sh            # "TweenGlass" in the app menu
scripts/install-desktop-entry.sh --autostart  # optional: start hidden in the tray at login
```

## Use

1. Open **TweenGlass**. It lives in the tray, and closing the window keeps it running.
2. Put the video in **fullscreen**. The overlay covers the whole screen, so clicks only land in the right place when the window is 1:1 with the screen.
3. Click the video's window and press **`Ctrl+Alt+S`**, or press **Activate** next to it in the app. Press `Ctrl+Alt+S` again to stop.

| Tab | |
|---|---|
| **Home** | Status, live `original → interpolated` fps, open windows. |
| **Settings** | Frame generation: multiplier (with a recommendation from your refresh rate), flow scale, performance mode, fps counter, GPU, shortcut. Image: scaling filter and sharpness. Audio: A/V sync and fine adjustment. Changes apply live. |
| **Diagnostics** | lsfg-vk layer, Lossless.dll, dedicated GPU, Plasma/Wayland, gtk4-layer-shell, PipeWire capture, display mode. |

Pick the multiplier so that `source fps × multiplier ≤ refresh rate`. For example, on 120 Hz: 30 fps → 3x (or 2x), 24 fps → 4x, 60 fps → 2x.

### A/V sync

Interpolation needs the *next* frame before it can draw the in-between ones, so the overlay always runs a little behind the real window. Measured on screen, with the source and its copy in the same screenshot:

| source | filter | video delay |
|---|---|---|
| 30 fps, lsfg off / 2x / 3x | none | ~38–45 ms |
| 24 fps, 2x | none | ~48 ms |
| 30 fps, 2x | FSR | ~57 ms |

While frame generation is on, TweenGlass moves the captured app's audio streams to a PipeWire loopback. The loopback delays them by `1 source frame + 5 ms + filter time` (measured live). The streams go back to your default output when you stop. If voices still don't match, use **Settings → Audio → Fine adjustment**. Note that the delay applies to everything that app plays, for example every tab of the browser.

### Filters

Filters run on the GPU, in OpenGL (ES) shaders, between the capture and frame generation.

| Filter | Good for | Source |
|---|---|---|
| Off (bilinear) | default, lowest latency | — |
| **FSR 1** (EASU + RCAS) | upscaling video/games, sharpening | AMD FidelityFX, MIT |
| **SGSR** | light, edge-aware upscaling | Qualcomm Snapdragon GSR 1, BSD-3-Clause |
| **BCAS** | bicubic + contrast-adaptive sharpening | bicubic + AMD FidelityFX CAS, MIT |
| Bicubic | smooth upscaling | Catmull-Rom |
| Sharp bilinear | pixel art at non-integer scales | — |
| Integer | pixel art, exact multiples | — |
| Nearest neighbour | pixel art | — |

- Upscalers only change the image when the window is **smaller than the screen**. On fullscreen video they work as sharpening, or do nothing.
- A smaller window is stretched to the screen, but clicks still go to the real (small) window below. That is fine for video or keyboard-driven games, not for mouse use.
- Each filter adds a few ms of video delay (up to ~15 ms for FSR/BCAS at 1080p). A/V sync compensates for it.
- Not available: **LS1** (closed-source, part of Lossless Scaling itself), and not yet: NIS, Anime4K, xBR.

### Games?

- **Vulkan, Proton or DXVK games:** use lsfg-vk directly on the game. It hooks the game itself, with less latency than capturing it.
- **Where TweenGlass helps:** things lsfg-vk can't hook, like OpenGL-only games, browser games and some emulators. Run them fullscreen. Keyboard and mouse go to the game through the overlay, at the cost of a little extra latency.
- **Upscaling games properly:** use [gamescope](https://github.com/ValveSoftware/gamescope) (`-F fsr`), which also maps the mouse correctly.

## Known limitations

- Tested only on CachyOS + KDE Plasma 6.8 (Wayland). The shortcut, window list and dialog-free window selection use KWin scripting and xdg-desktop-portal-kde. Other desktops would need the portal dialog and are untested.
- The overlay covers the whole screen and doesn't follow the window, so use fullscreen sources.
- KWin 6.8 stops a window screencast when the window is resized mid-stream (browser fullscreen toggle). TweenGlass reconnects automatically, which causes a short hiccup.
- lsfg-vk multipliers are integers (2–4), so 24 fps on 120 Hz caps at 4x (96 fps).

## Development

- `cd app && bun run dev`: the UI with hot reload. It drives the backend (`tools/phase0/probe.py --ipc`) over JSON lines on stdio. The protocol is in [`app/src/shared/protocol.ts`](app/src/shared/protocol.ts).
- Settings are stored in `~/.config/TweenGlass/settings.json`, and the log in `~/.config/TweenGlass/tweenglass.log`.
- The backend also runs on its own: `/usr/bin/python3 tools/phase0/probe.py --multiplier 2 [--filter fsr]`. Use `Ctrl+Alt+S` to toggle and `Meta+Alt+Q` to quit; see `--help`.
- Measurements and findings are in [`docs/phase0-results.md`](docs/phase0-results.md), and the roadmap in [`docs/plan.md`](docs/plan.md).

## License

TweenGlass is free software: [GPL-3.0-or-later](LICENSE).

The ported shader code keeps its original permissive licenses (see [`LICENSES/`](LICENSES)):
- AMD FidelityFX FSR 1 and CAS: MIT.
- Qualcomm Snapdragon GSR 1: BSD-3-Clause.

TweenGlass is an independent project. It is not affiliated with Lossless Scaling, lsfg-vk, AMD or Qualcomm. lsfg-vk and Lossless Scaling have their own licenses and are not distributed here.
