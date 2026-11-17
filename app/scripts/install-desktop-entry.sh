#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Install a user-level launcher (app menu entry) for the development build.
#   scripts/install-desktop-entry.sh            # app menu
#   scripts/install-desktop-entry.sh --autostart  # also start hidden in the tray at login
set -euo pipefail

app_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
electron="$app_dir/node_modules/electron/dist/electron"
[[ -x "$electron" ]] || { echo "Electron not installed: run 'bun install' in $app_dir" >&2; exit 1; }
[[ -f "$app_dir/out/main/index.js" ]] || { echo "Not built: run 'bun run build' in $app_dir" >&2; exit 1; }

id="dev.tweenglass.TweenGlass"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$apps"

entry() {
  cat <<EOF
[Desktop Entry]
Type=Application
Name=TweenGlass
GenericName=Frame generation
GenericName[pt_BR]=Geração de quadros
Comment=Frame generation for any window, powered by lsfg-vk
Comment[pt_BR]=Geração de quadros para qualquer janela, com o lsfg-vk
Exec="$electron" "$app_dir" $1
Icon=$app_dir/resources/icon.png
Terminal=false
Categories=AudioVideo;Video;
Keywords=lsfg;interpolation;lossless scaling;fps;smooth;
StartupWMClass=dev.tweenglass.TweenGlass
SingleMainWindow=true
EOF
}

entry "" > "$apps/$id.desktop"
echo "installed $apps/$id.desktop"

if [[ "${1:-}" == "--autostart" ]]; then
  auto="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
  mkdir -p "$auto"
  { entry "--hidden"; echo "X-GNOME-Autostart-enabled=true"; } > "$auto/$id.desktop"
  echo "installed $auto/$id.desktop"
fi

command -v update-desktop-database >/dev/null && update-desktop-database "$apps" 2>/dev/null || true
