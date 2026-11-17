// SPDX-License-Identifier: GPL-3.0-or-later
// Resolve a window's app id to its icon (freedesktop .desktop + icon theme), as a data URL.

import { existsSync, readdirSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { extname, join } from "node:path";

const home = homedir();
const DATA_DIRS = [
  join(home, ".local/share"),
  join(home, ".local/share/flatpak/exports/share"),
  "/var/lib/flatpak/exports/share",
  ...(process.env.XDG_DATA_DIRS ?? "/usr/local/share:/usr/share").split(":"),
].filter(Boolean);

const THEMES = ["breeze-dark", "breeze", "hicolor", "Papirus-Dark", "Papirus", "Adwaita"];
const SIZES = ["scalable", "256x256", "128x128", "96x96", "64x64", "48x48", "32x32"];
const cache = new Map<string, string | null>();

function desktopIconName(appId: string): string | null {
  const candidates = [appId, appId.toLowerCase()];
  for (const dir of DATA_DIRS) {
    for (const name of candidates) {
      const file = join(dir, "applications", `${name}.desktop`);
      if (existsSync(file)) {
        const m = /^Icon=(.+)$/m.exec(readFileSync(file, "utf8"));
        if (m) return m[1].trim();
      }
    }
  }
  return null;
}

function findIconFile(name: string): string | null {
  if (name.startsWith("/")) return existsSync(name) ? name : null;
  for (const dir of DATA_DIRS) {
    for (const theme of THEMES) {
      const base = join(dir, "icons", theme);
      if (!existsSync(base)) continue;
      for (const size of SIZES) {
        for (const sub of ["apps", "applications"]) {
          for (const ext of [".svg", ".png"]) {
            const f = join(base, size, sub, name + ext);
            if (existsSync(f)) return f;
          }
        }
      }
      // breeze uses <category>/<size>/ layout
      for (const sub of ["apps/48", "apps/64", "apps/32"]) {
        const f = join(base, sub, `${name}.svg`);
        if (existsSync(f)) return f;
      }
    }
    const pix = join(dir, "pixmaps");
    if (existsSync(pix)) {
      const hit = readdirSync(pix).find((f) => f.startsWith(`${name}.`));
      if (hit) return join(pix, hit);
    }
  }
  return null;
}

export function iconFor(appId: string): string | null {
  if (cache.has(appId)) return cache.get(appId) ?? null;
  let url: string | null = null;
  try {
    const name = desktopIconName(appId) ?? appId;
    const file = findIconFile(name) ?? findIconFile(appId.toLowerCase());
    if (file) {
      const ext = extname(file).toLowerCase();
      const mime = ext === ".svg" ? "image/svg+xml" : ext === ".png" ? "image/png" : null;
      if (mime) url = `data:${mime};base64,${readFileSync(file).toString("base64")}`;
    }
  } catch {
    url = null;
  }
  cache.set(appId, url);
  return url;
}
