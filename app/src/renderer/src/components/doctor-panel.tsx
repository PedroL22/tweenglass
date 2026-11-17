// SPDX-License-Identifier: GPL-3.0-or-later
import { CheckCircle2, Monitor, RefreshCw, XCircle } from "lucide-react";
import { useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { BackendView } from "@/hooks/use-backend";
import type { Strings } from "../../../shared/i18n";

function Check({ ok, label, detail }: { ok: boolean; label: string; detail?: string | null }) {
  return (
    <li className="flex items-start gap-3 py-1.5">
      {ok ? (
        <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" />
      ) : (
        <XCircle className="mt-0.5 size-4 shrink-0 text-destructive" />
      )}
      <div className="min-w-0">
        <div className="text-sm">{label}</div>
        {detail && (
          <div className="truncate text-xs text-muted-foreground" title={detail}>
            {detail}
          </div>
        )}
      </div>
    </li>
  );
}

export function DoctorPanel({ b, t, version }: { b: BackendView; t: Strings; version: string }) {
  const { send, backendAlive } = b;
  useEffect(() => {
    if (backendAlive) send({ cmd: "checks" });
  }, [backendAlive, send]);
  const c = b.checks;
  const dgpu = c?.gpus.find((g) => g.discrete);
  const isKde = (c?.desktop ?? "").toUpperCase().includes("KDE");

  return (
    <div className="flex flex-col gap-3">
      <Card className="gap-2 py-4">
        <CardHeader className="px-4">
          <CardTitle className="text-sm">{t.doctor}</CardTitle>
          <CardAction>
            <Button
              variant="ghost"
              size="icon"
              className="size-7"
              title={t.recheck}
              disabled={!backendAlive}
              onClick={() => send({ cmd: "checks" })}
            >
              <RefreshCw className="size-3.5" />
            </Button>
          </CardAction>
        </CardHeader>
        <CardContent className="px-4">
          {!c ? (
            <p className="text-sm text-muted-foreground">{t.starting}</p>
          ) : (
            <ul>
              <Check ok={!!c.lsfg_layer} label={t.checkLsfg} detail={c.lsfg_layer ?? t.notFound} />
              <Check ok={!!c.lossless_dll} label={t.checkDll} detail={c.lossless_dll ?? t.notFound} />
              <Check ok={!!dgpu} label={t.checkGpu} detail={dgpu?.name ?? c.gpus.map((g) => g.name).join(", ")} />
              <Check ok={isKde && c.wayland} label={t.checkSession} detail={`${c.desktop || "?"} · ${c.wayland ? "Wayland" : "X11"}`} />
              <Check ok={c.layer_shell} label={t.checkLayerShell} detail={c.layer_shell ? null : t.missing} />
              <Check ok={c.pipewire_capture} label={t.checkCapture} detail={c.pipewire_capture ? null : t.missing} />
              {c.display && (
                <li className="flex items-center gap-3 py-1.5 text-sm">
                  <Monitor className="size-4 text-muted-foreground" />
                  {t.display}: {c.display.width}×{c.display.height} @ {Math.round(c.display.hz)} Hz
                </li>
              )}
            </ul>
          )}
        </CardContent>
      </Card>
      <Card className="gap-2 py-4">
        <CardHeader className="px-4">
          <CardTitle className="text-sm">
            {t.about} · TweenGlass {version}
          </CardTitle>
        </CardHeader>
        <CardContent className="px-4 text-xs leading-relaxed text-muted-foreground">{t.aboutBody}</CardContent>
      </Card>
    </div>
  );
}
