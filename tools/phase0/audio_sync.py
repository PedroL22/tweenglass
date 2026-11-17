# SPDX-License-Identifier: GPL-3.0-or-later
"""A/V sync: delay the captured app's audio by the overlay's video latency.

The overlay shows the window ~1 content frame late (capture -> presenter ->
lsfg-vk). The app keeps playing its sound on time, so voices lead lips. We
route the app's audio streams through a PipeWire loopback (`pw-loopback
--delay`, exact: measured 40/100/200 ms requested -> 40/100/200 ms added)
whose output follows the default sink.

Streams are moved with the `target.object` metadata (what pavucontrol does)
and the metadata is removed again when frame generation stops. WirePlumber's
restore-stream would otherwise remember the virtual sink for that app, so
the saved target is also cleaned from its state if it shows up there.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

GENERIC = {"org", "com", "io", "net", "dev", "app", "desktop", "client", "browser", "www", "the",
           "kde", "gnome", "mozilla", "google", "freedesktop", "github", "flatpak", "stable", "beta", "nightly"}
WP_STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "wireplumber" / "stream-properties"


def video_latency_ms(base_fps: float) -> float:
    """Overlay video delay measured on screen (KWin screenshots, source vs.
    overlay): 38 ms at 30 fps, 48 ms at 24 fps, with lsfg-vk 2x/3x or off ->
    about one content frame + 5 ms."""
    fps = base_fps if base_fps >= 5 else 30.0
    return 1000.0 / fps + 5.0


def _run(cmd: list[str], timeout: float = 3.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:  # noqa: BLE001
        return ""


def _descendants(pid: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            stat = Path(f"/proc/{d}/stat").read_text()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(d))
    out, todo = {pid}, [pid]
    while todo:
        for c in children.get(todo.pop(), []):
            if c not in out:
                out.add(c)
                todo.append(c)
    return out


def _tokens(app_id: str) -> set[str]:
    parts = re.split(r"[.\-_\s]+", app_id.lower())
    return {p for p in parts if len(p) >= 3 and p not in GENERIC}


class AudioSync:
    def __init__(self, log=print):
        self.log = log
        self.proc: subprocess.Popen | None = None
        self.sink: str | None = None
        self.delay_ms = 0.0
        self.pid: int | None = None
        self.app_id = ""
        self.moved: dict[int, str] = {}  # pipewire object id -> application.name
        self.serial = 0
        self.available = bool(_run(["sh", "-c", "command -v pw-loopback pw-metadata pactl"]).count("\n") >= 3)

    # ---------------------------------------------------------------- loopback
    def _spawn(self, delay_ms: float) -> tuple[subprocess.Popen, str] | None:
        self.serial += 1
        name = f"tweenglass_avsync_{os.getpid()}_{self.serial}"
        proc = subprocess.Popen(
            ["pw-loopback", "-n", name,
             "--capture-props",
             f'media.class=Audio/Sink node.name={name} node.description="TweenGlass A/V sync" '
             "node.virtual=true priority.session=0 priority.driver=0",
             "--playback-props",
             f'node.name={name}_out node.description="TweenGlass A/V sync (output)" node.dont-move=false',
             "--delay", f"{max(0.0, delay_ms) / 1000:.4f}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):  # wait until the sink exists (usually ~50 ms)
            if name in _run(["pactl", "list", "short", "sinks"]):
                return proc, name
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        proc.terminate()
        self.log("[audio] pw-loopback did not start")
        return None

    def _streams(self) -> list[dict]:
        try:
            return json.loads(_run(["pactl", "-f", "json", "list", "sink-inputs"]) or "[]")
        except json.JSONDecodeError:
            return []

    @staticmethod
    def _pid_match(props: dict, pids: set[int]) -> bool:
        try:
            return int(props.get("application.process.id", -1)) in pids
        except ValueError:
            return False

    def _name_match(self, props: dict) -> bool:
        # Fallback for sandboxed apps (Flatpak), whose PIDs live in another namespace.
        names = " ".join(str(props.get(k, "")) for k in
                         ("application.process.binary", "application.name", "application.id")).lower()
        return any(t in names for t in _tokens(self.app_id))

    def _set_target(self, obj_id: int, sink: str | None) -> None:
        if sink:
            _run(["pw-metadata", str(obj_id), "target.object", sink])
        else:
            _run(["pw-metadata", "-d", str(obj_id), "target.object"])

    # ---------------------------------------------------------------- API
    def start(self, pid: int | None, app_id: str, delay_ms: float) -> bool:
        self.stop()
        if not self.available:
            return False
        self.pid, self.app_id = pid, app_id or ""
        spawned = self._spawn(delay_ms)
        if not spawned:
            return False
        self.proc, self.sink = spawned
        self.delay_ms = delay_ms
        self.poll()
        self.log(f"[audio] delaying {self.app_id or pid} by {delay_ms:.0f} ms ({len(self.moved)} stream(s))")
        return True

    def poll(self) -> None:
        """Route the app's new streams (each new video/tab may open one)."""
        if not self.sink:
            return
        pids = _descendants(self.pid) if self.pid else set()
        cands = []
        for s in self._streams():
            props = s.get("properties", {})
            if str(props.get("node.name", "")).startswith("tweenglass_avsync"):
                continue
            try:
                cands.append((int(props.get("object.id", s.get("index"))), props))
            except (TypeError, ValueError):
                continue
        # Prefer the window's own process tree; match by name only if that finds nothing.
        chosen = [c for c in cands if self._pid_match(c[1], pids)] or [c for c in cands if self._name_match(c[1])]
        live = set()
        for oid, props in chosen:
            live.add(oid)
            if oid not in self.moved:
                self._set_target(oid, self.sink)
                self.moved[oid] = str(props.get("application.name", ""))
        for gone in set(self.moved) - live:
            self.moved.pop(gone, None)

    def set_delay(self, delay_ms: float) -> None:
        """Swap in a loopback with the new delay, then retire the old one."""
        if not self.sink or abs(delay_ms - self.delay_ms) < 4:
            return
        spawned = self._spawn(delay_ms)
        if not spawned:
            return
        old_proc = self.proc
        self.proc, self.sink = spawned
        for oid in self.moved:
            self._set_target(oid, self.sink)
        self.delay_ms = delay_ms
        time.sleep(0.15)  # let the streams relink before the old sink disappears
        if old_proc:
            old_proc.terminate()
        self.log(f"[audio] delay now {delay_ms:.0f} ms")

    @property
    def streams(self) -> int:
        return len(self.moved)

    def stop(self) -> None:
        apps = set(self.moved.values())
        for oid in list(self.moved):
            self._set_target(oid, None)  # back to the default sink
        self.moved.clear()
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc, self.sink = None, None
        if apps:
            self._forget_targets()

    def _forget_targets(self) -> None:
        """Drop any `"target"` WirePlumber saved pointing at our (gone) sink."""
        try:
            text = WP_STATE.read_text()
        except OSError:
            return
        if "tweenglass_avsync" not in text:
            return
        out = []
        for line in text.splitlines():
            if "tweenglass_avsync" in line and "=" in line:
                key, _, val = line.partition("=")
                try:
                    data = json.loads(val)
                    data.pop("target", None)
                    line = f"{key}={json.dumps(data)}"
                except json.JSONDecodeError:
                    continue
            out.append(line)
        WP_STATE.write_text("\n".join(out) + "\n")
