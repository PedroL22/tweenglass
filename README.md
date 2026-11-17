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

### App (Electron + React + shadcn/ui)

```sh
cd app
bun install
bun run build
scripts/install-desktop-entry.sh        # adds "TweenGlass" to the app menu (--autostart: also start in the tray at login)
```

Open **TweenGlass** from the app menu. It lives in the tray. Closing the window keeps it running, so the shortcut keeps working.

- **Home:** status, live `original → interpolated` fps, and the list of open windows. Press **Activate** on one, or click a window and press `Ctrl+Alt+S`.
- **Settings:** multiplier (with a recommendation from your refresh rate and the measured fps), flow scale, performance mode, on-screen fps counter, GPU, and shortcut. Changes apply live.
- **Diagnostics:** lsfg-vk layer, Lossless.dll (any Steam library), dedicated GPU, Plasma/Wayland, gtk4-layer-shell, PipeWire capture, and display mode.

`bun run dev` starts it with hot reload. The UI drives the capture backend (`tools/phase0/probe.py --ipc`) over JSON lines on stdio. The protocol is in `app/src/shared/protocol.ts`. Settings live in `~/.config/TweenGlass/settings.json` and the log in `~/.config/TweenGlass/tweenglass.log`.

### Capture backend / probe (CLI)

Phase 0 probe (validates capture, frame cadence and the lsfg-vk hook):

```sh
/usr/bin/python3 tools/phase0/probe.py --multiplier 2
```

- Runs in the background until you quit it. Focus any window and press **`Ctrl+Alt+S`** to turn frame generation on for it; press again to turn it off (KDE global shortcuts, `--toggle-keys` / `--stop-keys` to change). No picker dialog: the active window is pre-selected through the portal's restore data. `--now` starts at once with the last window instead.
- Put the source (e.g. the browser video) in **fullscreen**: the overlay covers the whole output and is click-through, so clicks only land in the right place when the source is 1:1 with the screen.
- Top-left counter: `base / output` fps. *Base* = new frames from the window; *output* = frames the overlay process actually handed to the compositor (lsfg-vk's generated frames included, counted from its Wayland buffer attaches).
- Quit: `Meta+Alt+Q` or Ctrl+C in the terminal.
- `--sink mpv` keeps the old mpv window presenter (not click-through).

See [`docs/plan.md`](docs/plan.md) for the roadmap.

## License

[GPL-3.0-or-later](LICENSE). TweenGlass is an independent project. It is not affiliated with Lossless Scaling or lsfg-vk.
