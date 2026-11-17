// SPDX-License-Identifier: GPL-3.0-or-later
import { Play, RefreshCw } from "lucide-react";
import { useEffect } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { BackendView } from "@/hooks/use-backend";
import { cn } from "@/lib/utils";
import type { Strings } from "../../../shared/i18n";
import type { WindowInfo } from "../../../shared/protocol";

const PALETTE = ["bg-sky-500/20 text-sky-300", "bg-emerald-500/20 text-emerald-300", "bg-violet-500/20 text-violet-300",
  "bg-amber-500/20 text-amber-300", "bg-rose-500/20 text-rose-300", "bg-teal-500/20 text-teal-300"];

const GENERIC = new Set(["client", "desktop", "app", "application", "browser"]);

function prettyApp(appId: string): string {
  // "com.spotify.Client" -> "Spotify", "google-chrome" -> "Google Chrome"
  const parts = appId.split(".").filter((p) => p && !GENERIC.has(p.toLowerCase()));
  const last = parts.pop() ?? appId;
  return last.replace(/[-_]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()) || "?";
}

function AppBadge({ appId, icon }: { appId: string; icon?: string | null }) {
  if (icon) {
    return (
      <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-accent/40 p-1.5">
        <img src={icon} alt="" className="size-full object-contain" draggable={false} />
      </div>
    );
  }
  const name = prettyApp(appId);
  const hue = [...appId].reduce((a, c) => a + c.charCodeAt(0), 0) % PALETTE.length;
  return (
    <div className={cn("flex size-9 shrink-0 items-center justify-center rounded-lg font-semibold", PALETTE[hue])}>
      {name.charAt(0)}
    </div>
  );
}

function isRunningOn(b: BackendView, w: WindowInfo): boolean {
  return !!b.state?.active && b.state.target?.app_id === w.app_id && b.state.target?.title === w.title;
}

export function WindowList({ b, t }: { b: BackendView; t: Strings }) {
  const { send, backendAlive } = b;
  // Refresh when the backend comes up, and every few seconds while visible.
  useEffect(() => {
    if (!backendAlive) return;
    send({ cmd: "list_windows" });
    const id = setInterval(() => {
      if (document.visibilityState === "visible") send({ cmd: "list_windows" });
    }, 4000);
    return () => clearInterval(id);
  }, [backendAlive, send]);

  return (
    <Card className="gap-3 py-4">
      <CardHeader className="px-4">
        <CardTitle className="text-sm">{t.windows}</CardTitle>
        <CardAction>
          <Button
            variant="ghost"
            size="icon"
            className="size-7"
            title={t.refresh}
            disabled={!backendAlive}
            onClick={() => send({ cmd: "list_windows" })}
          >
            <RefreshCw className="size-3.5" />
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="px-2">
        {b.windows.length === 0 ? (
          <p className="px-2 py-3 text-sm text-muted-foreground">{t.noWindows}</p>
        ) : (
          <ul className="flex flex-col">
            {b.windows.map((w) => {
              const running = isRunningOn(b, w);
              return (
                <li key={w.id} className="flex items-center gap-3 rounded-lg px-2 py-2 hover:bg-accent/60">
                  <AppBadge appId={w.app_id} icon={w.icon} />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm" title={w.title}>
                      {w.title || prettyApp(w.app_id)}
                    </div>
                    <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                      <span className="truncate">{prettyApp(w.app_id)}</span>
                      {w.fullscreen && <Badge variant="outline" className="px-1.5 py-0 text-[10px]">{t.fullscreen}</Badge>}
                      {w.minimized && <Badge variant="outline" className="px-1.5 py-0 text-[10px]">{t.minimized}</Badge>}
                    </div>
                  </div>
                  {running ? (
                    <Badge className="bg-success/20 text-success">{t.running}</Badge>
                  ) : (
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => send({ cmd: "activate", id: w.id, app_id: w.app_id, title: w.title })}
                    >
                      <Play className="size-3.5 fill-current" />
                      {t.activate}
                    </Button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
        <p className="px-2 pt-3 text-xs leading-relaxed text-muted-foreground">{t.tipFullscreen}</p>
      </CardContent>
    </Card>
  );
}
