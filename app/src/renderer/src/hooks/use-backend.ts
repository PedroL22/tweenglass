import { useCallback, useEffect, useState } from "react";
import { type BackendCommand, type BackendEvent, DEFAULT_SETTINGS, type Settings, type Snapshot } from "../../../shared/protocol";

const EMPTY: Snapshot = {
  ready: null,
  state: null,
  stats: null,
  checks: null,
  windows: [],
  settings: DEFAULT_SETTINGS,
  backendAlive: false,
};

export interface BackendView extends Snapshot {
  error: string | null;
  restarting: boolean | null;
  send: (cmd: BackendCommand) => void;
  update: (patch: Partial<Settings>) => void;
}

function reduce(prev: Snapshot, ev: BackendEvent): Snapshot {
  switch (ev.type) {
    case "hello":
      return { ...prev, backendAlive: true };
    case "ready":
      return { ...prev, ready: ev };
    case "state":
      return { ...prev, state: ev, stats: ev.active ? prev.stats : null, settings: ev.settings };
    case "stats":
      return { ...prev, stats: ev };
    case "windows":
      return { ...prev, windows: ev.items };
    case "checks": {
      const { type: _t, ...checks } = ev;
      return { ...prev, checks };
    }
    case "backend-exit":
      return { ...prev, backendAlive: false, state: null, stats: null };
    default:
      return prev;
  }
}

/** Mirrors the main process' snapshot and applies backend events as they arrive. */
export function useBackend(): BackendView {
  const [snap, setSnap] = useState<Snapshot>(EMPTY);
  const [error, setError] = useState<string | null>(null);
  const [restarting, setRestarting] = useState<boolean | null>(null);

  useEffect(() => {
    let alive = true;
    void window.tweenglass.snapshot().then((s) => {
      if (alive) setSnap(s);
    });
    const off = window.tweenglass.onEvent((ev) => {
      if (ev.type === "error") {
        setError(ev.message);
        return;
      }
      if (ev.type === "hello") setRestarting(null);
      if (ev.type === "backend-exit") setRestarting(ev.restarting);
      setSnap((prev) => reduce(prev, ev));
    });
    return () => {
      alive = false;
      off();
    };
  }, []);

  const send = useCallback((cmd: BackendCommand) => {
    setError(null);
    void window.tweenglass.send(cmd);
  }, []);

  const update = useCallback(
    (patch: Partial<Settings>) => {
      setSnap((prev) => ({ ...prev, settings: { ...prev.settings, ...patch } }));
      send({ cmd: "set", ...patch });
    },
    [send],
  );

  return { ...snap, error, restarting, send, update };
}
