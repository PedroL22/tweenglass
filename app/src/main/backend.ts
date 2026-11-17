// SPDX-License-Identifier: GPL-3.0-or-later
// Spawns and supervises the capture backend (Python, JSON over stdio).

import { type ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline";
import type { BackendCommand, BackendEvent, Settings } from "../shared/protocol";

export function backendScript(appPath: string): string {
  if (process.env.TWEENGLASS_BACKEND) return process.env.TWEENGLASS_BACKEND;
  // Development layout: <repo>/app (this Electron app) and <repo>/tools/phase0.
  return resolve(appPath, "..", "tools", "phase0", "probe.py");
}

function pythonExe(): string {
  // The backend needs the system Python (python-gobject, GStreamer bindings),
  // never a project venv that may be first on PATH.
  for (const p of ["/usr/bin/python3", "/usr/local/bin/python3"]) if (existsSync(p)) return p;
  return "python3";
}

export function settingsArgs(s: Settings): string[] {
  const args = [
    "--multiplier", String(s.multiplier),
    "--flow-scale", String(s.flow_scale),
    "--toggle-keys", s.toggle_keys,
  ];
  if (s.performance) args.push("--performance");
  if (!s.hud) args.push("--no-hud");
  if (s.gpu !== null) args.push("--gpu", String(s.gpu));
  return args;
}

export class Backend {
  private proc: ChildProcessWithoutNullStreams | null = null;
  private stopping = false;
  private restarts = 0;
  private restartTimer: NodeJS.Timeout | null = null;

  constructor(
    private readonly script: string,
    private readonly getSettings: () => Settings,
    private readonly onEvent: (e: BackendEvent) => void,
    private readonly log: (line: string) => void,
  ) {}

  get alive(): boolean {
    return this.proc !== null && this.proc.exitCode === null;
  }

  start(): void {
    if (this.alive) return;
    this.stopping = false;
    const args = [this.script, "--ipc", ...settingsArgs(this.getSettings())];
    this.log(`spawn ${pythonExe()} ${args.join(" ")}`);
    const proc = spawn(pythonExe(), args, {
      cwd: join(this.script, ".."),
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.proc = proc;

    createInterface({ input: proc.stdout }).on("line", (line) => {
      if (!line.trim()) return;
      try {
        const ev = JSON.parse(line) as BackendEvent;
        if (ev.type === "hello") this.restarts = 0;
        this.onEvent(ev);
      } catch {
        this.log(`[backend:stdout] ${line}`);
      }
    });
    createInterface({ input: proc.stderr }).on("line", (line) => this.log(`[backend] ${line}`));

    proc.on("exit", (code) => {
      this.proc = null;
      const restarting = !this.stopping && this.restarts < 5;
      this.onEvent({ type: "backend-exit", code, restarting });
      if (restarting) {
        this.restarts += 1;
        this.restartTimer = setTimeout(() => this.start(), 1500 * this.restarts);
      }
    });
  }

  send(cmd: BackendCommand): void {
    if (!this.alive || !this.proc) return;
    this.proc.stdin.write(`${JSON.stringify(cmd)}\n`);
  }

  /** Ask the backend to quit (it tears down the overlay and KWin scripts), then kill if needed. */
  async stop(timeoutMs = 4000): Promise<void> {
    this.stopping = true;
    if (this.restartTimer) clearTimeout(this.restartTimer);
    const proc = this.proc;
    if (!proc || proc.exitCode !== null) return;
    await new Promise<void>((done) => {
      const timer = setTimeout(() => {
        proc.kill("SIGTERM");
        setTimeout(() => {
          if (proc.exitCode === null) proc.kill("SIGKILL");
          done();
        }, 1500);
      }, timeoutMs);
      proc.once("exit", () => {
        clearTimeout(timer);
        done();
      });
      this.send({ cmd: "quit" });
    });
  }
}
