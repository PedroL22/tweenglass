// SPDX-License-Identifier: GPL-3.0-or-later
import { AlertTriangle, Layers, Loader2, Square } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Kbd, KbdGroup } from "@/components/ui/kbd";
import type { BackendView } from "@/hooks/use-backend";
import { fmt, type Strings } from "../../../shared/i18n";

export function Keys({ combo }: { combo: string }) {
  return (
    <KbdGroup>
      {combo.split("+").map((k) => (
        <Kbd key={k}>{k}</Kbd>
      ))}
    </KbdGroup>
  );
}

function Fps({ value, label, accent }: { value: number | null; label: string; accent?: boolean }) {
  return (
    <div className="flex flex-col items-center gap-0.5">
      <span
        className={
          accent
            ? "font-mono text-4xl font-semibold tabular-nums text-primary"
            : "font-mono text-4xl font-semibold tabular-nums"
        }
      >
        {value === null ? "–" : Math.round(value)}
      </span>
      <span className="text-[11px] uppercase tracking-wider text-muted-foreground">{label}</span>
    </div>
  );
}

export function StatusCard({ b, t }: { b: BackendView; t: Strings }) {
  const active = b.state?.active ?? false;
  const keys = b.ready?.toggle_keys ?? b.settings.toggle_keys;

  let body: React.ReactNode;
  if (!b.backendAlive) {
    body = (
      <div className="flex items-center gap-3 py-2 text-sm text-muted-foreground">
        {b.restarting === false ? (
          <AlertTriangle className="size-5 text-destructive" />
        ) : (
          <Loader2 className="size-5 animate-spin" />
        )}
        <div>
          {b.restarting === null ? (
            t.starting
          ) : (
            <>
              <div className="text-foreground">{t.backendDown}</div>
              <div>{b.restarting ? t.backendRestarting : t.backendGaveUp}</div>
            </>
          )}
        </div>
      </div>
    );
  } else if (active) {
    const s = b.stats;
    const idle = s !== null && s.base < 1;
    body = (
      <div className="flex flex-col gap-4">
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="relative flex size-2.5">
                <span className="absolute inline-flex size-full animate-ping rounded-full bg-success opacity-60" />
                <span className="relative inline-flex size-2.5 rounded-full bg-success" />
              </span>
              <span className="font-medium">{t.active}</span>
              <Badge variant="secondary">{b.settings.multiplier}x</Badge>
            </div>
            <div className="mt-1 truncate text-sm text-muted-foreground" title={b.state?.target?.title ?? ""}>
              {b.state?.target?.title || b.state?.target?.app_id}
            </div>
          </div>
          <Button variant="outline" size="sm" onClick={() => b.send({ cmd: "deactivate" })}>
            <Square className="size-3.5 fill-current" />
            {t.stop}
          </Button>
        </div>
        <div className="flex items-center justify-around rounded-lg bg-background/60 py-4">
          <Fps value={s?.base ?? null} label={t.base} />
          <span className="text-2xl text-muted-foreground">→</span>
          <Fps value={s?.output ?? null} label={t.output} accent />
        </div>
        {(s === null || idle) && (
          <p className="text-center text-xs text-muted-foreground">{s === null ? t.noFramesYet : t.paused}</p>
        )}
      </div>
    );
  } else {
    body = (
      <div className="flex items-start gap-3">
        <div className="rounded-lg bg-primary/15 p-2 text-primary">
          <Layers className="size-5" />
        </div>
        <div className="space-y-1.5">
          <div className="font-medium">{t.ready}</div>
          <div className="flex flex-wrap items-center gap-1.5 text-sm text-muted-foreground">
            {b.ready?.shortcuts_ok === false ? (
              t.shortcutsMissing
            ) : (
              <>
                {t.readyHint} <Keys combo={keys} /> {t.readyHintOr}
              </>
            )}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <Card className="gap-0 py-4">
        <CardContent className="px-4">{body}</CardContent>
      </Card>
      {b.ready && b.ready.conflicts.length > 0 && (
        <Alert variant="destructive">
          <AlertTriangle />
          <AlertTitle>{fmt(t.conflictTitle, { keys: b.ready.toggle_keys })}</AlertTitle>
          <AlertDescription>{fmt(t.conflictBody, { apps: b.ready.conflicts.join("; ") })}</AlertDescription>
        </Alert>
      )}
      {b.error && (
        <Alert variant="destructive">
          <AlertTriangle />
          <AlertDescription className="break-words">{b.error}</AlertDescription>
        </Alert>
      )}
    </div>
  );
}
