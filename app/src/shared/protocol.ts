// SPDX-License-Identifier: GPL-3.0-or-later
// JSON-over-stdio protocol between the UI and the capture backend
// (tools/phase0/probe.py --ipc). One JSON object per line in each direction.

export interface Settings {
  multiplier: 2 | 3 | 4;
  flow_scale: number; // 0.25 – 1.0
  performance: boolean;
  hud: boolean;
  gpu: number | null; // Vulkan device index, null = discrete GPU
  toggle_keys: string; // KDE shortcut, e.g. "Ctrl+Alt+S"
}

export const DEFAULT_SETTINGS: Settings = {
  multiplier: 2,
  flow_scale: 1,
  performance: false,
  hud: true,
  gpu: null,
  toggle_keys: "Ctrl+Alt+S",
};

export interface WindowInfo {
  id: string;
  app_id: string;
  title: string;
  fullscreen: boolean;
  minimized: boolean;
  active: boolean;
  /** data: URL resolved by the main process from the app's .desktop file */
  icon?: string | null;
}

export interface Gpu {
  index: number;
  name: string;
  vendor: string;
  device: string;
  discrete: boolean;
}

export interface Checks {
  lsfg_layer: string | null;
  lossless_dll: string | null;
  gpus: Gpu[];
  desktop: string;
  wayland: boolean;
  layer_shell: boolean;
  pipewire_capture: boolean;
  display: { width: number; height: number; hz: number } | null;
}

export interface StateEvent {
  type: "state";
  active: boolean;
  reason: string | null;
  target: { app_id: string | null; title: string | null } | null;
  settings: Settings;
}

export interface ReadyEvent {
  type: "ready";
  toggle_keys: string;
  altgr_keys: string | null;
  shortcuts_ok: boolean;
  conflicts: string[];
}

export interface StatsEvent {
  type: "stats";
  base: number;
  output: number;
  capture: number;
  reconnects: number;
}

export type BackendEvent =
  | { type: "hello"; protocol: number }
  | ReadyEvent
  | StateEvent
  | StatsEvent
  | { type: "windows"; items: WindowInfo[] }
  | ({ type: "checks" } & Checks)
  | { type: "error"; message: string }
  | { type: "backend-exit"; code: number | null; restarting: boolean };

export type BackendCommand =
  | { cmd: "list_windows" }
  | { cmd: "activate"; id: string; app_id: string; title: string }
  | { cmd: "deactivate" }
  | ({ cmd: "set" } & Partial<Settings>)
  | { cmd: "checks" }
  | { cmd: "state" }
  | { cmd: "quit" };

/** Last value of each event kind, so a freshly (re)loaded window can render at once. */
export interface Snapshot {
  ready: ReadyEvent | null;
  state: StateEvent | null;
  stats: StatsEvent | null;
  checks: Checks | null;
  windows: WindowInfo[];
  settings: Settings;
  backendAlive: boolean;
}

export interface TweenGlassApi {
  snapshot(): Promise<Snapshot>;
  send(cmd: BackendCommand): Promise<void>;
  onEvent(listener: (event: BackendEvent) => void): () => void;
  hide(): Promise<void>;
  quit(): Promise<void>;
}
