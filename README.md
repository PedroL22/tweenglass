# TweenGlass

**Frame generation for any window on Linux.**

TweenGlass mirrors any window (a browser playing Netflix or YouTube, a video player, a game) into a Vulkan overlay. Because the overlay is presented from a Vulkan process, your installed [lsfg-vk](https://lsfg-vk.dev) layer can interpolate it, the same way Lossless Scaling works on Windows.

> Status: **pre-alpha / Phase 0**. The core pipeline is still being validated, and nothing here is usable yet.

## How it works

```
Any window ──(xdg-desktop-portal ScreenCast + PipeWire)──► TweenGlass renderer (Vulkan)
                                                              │  presents only new content frames
                                                              ▼
                                                     lsfg-vk layer (user-installed)
                                                              │
                                                              ▼
                                   always-on-top, click-through overlay at display refresh
```

- **UI:** Electron + React + shadcn/ui.
- **Renderer:** a separate Rust process (PipeWire → Vulkan → Wayland layer-shell). It is a separate process because lsfg-vk applies per process.

## Requirements

- Linux with a Wayland session. KDE Plasma comes first; Hyprland, Sway and COSMIC should work. GNOME support is planned.
- A Vulkan-capable GPU (any vendor).
- [lsfg-vk](https://lsfg-vk.dev) installed, and **Lossless Scaling** owned and installed through Steam.

TweenGlass does **not** include lsfg-vk or any Lossless Scaling files. It only detects and uses what you installed.

## Development

Phase 0 probe (validates capture, frame cadence and the lsfg-vk hook):

```sh
/usr/bin/python3 tools/phase0/probe.py --multiplier 2 --seconds 3600
```

- Put the source (e.g. the browser video) in **fullscreen**: the overlay covers the whole output and is click-through, so clicks only land in the right place when the source is 1:1 with the screen.
- Top-left counter: `base / output` fps. *Base* = new frames from the window; *output* = frames the overlay process actually handed to the compositor (lsfg-vk's generated frames included, counted from its Wayland buffer attaches).
- Stop: `Meta+Alt+Q` (KDE global shortcut, `--stop-keys` to change) or Ctrl+C in the terminal.
- `--sink mpv` keeps the old mpv window presenter (not click-through).

See [`docs/plan.md`](docs/plan.md) for the roadmap.

## License

[GPL-3.0-or-later](LICENSE). TweenGlass is an independent project. It is not affiliated with Lossless Scaling or lsfg-vk.
