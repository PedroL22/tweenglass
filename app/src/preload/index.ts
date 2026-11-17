// SPDX-License-Identifier: GPL-3.0-or-later
import { contextBridge, ipcRenderer, type IpcRendererEvent } from "electron";
import type { BackendCommand, BackendEvent, TweenGlassApi } from "../shared/protocol";

const api: TweenGlassApi = {
  snapshot: () => ipcRenderer.invoke("tg:snapshot"),
  send: (cmd: BackendCommand) => ipcRenderer.invoke("tg:send", cmd),
  onEvent: (listener) => {
    const handler = (_e: IpcRendererEvent, ev: BackendEvent) => listener(ev);
    ipcRenderer.on("backend:event", handler);
    return () => ipcRenderer.removeListener("backend:event", handler);
  },
  hide: () => ipcRenderer.invoke("tg:hide"),
  quit: () => ipcRenderer.invoke("tg:quit"),
};

contextBridge.exposeInMainWorld("tweenglass", api);
