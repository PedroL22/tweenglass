// SPDX-License-Identifier: GPL-3.0-or-later
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import type { BackendView } from "@/hooks/use-backend";
import { fmt, type Strings } from "../../../shared/i18n";
import type { Settings } from "../../../shared/protocol";
import { Keys } from "./status-card";

function Row({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div className="space-y-0.5">
        <Label className="text-sm">{title}</Label>
        {hint && <p className="text-xs leading-snug text-muted-foreground">{hint}</p>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

/** Highest of 2–4 that the display can show for a given content fps. */
export function recommend(baseFps: number, hz: number): 2 | 3 | 4 {
  return Math.max(2, Math.min(4, Math.floor((hz + 0.5) / baseFps))) as 2 | 3 | 4;
}

const MODS: Record<string, string> = { Control: "Ctrl", Alt: "Alt", Shift: "Shift", Meta: "Meta" };

function comboFromEvent(e: KeyboardEvent): string | null {
  if (e.key in MODS) return null;
  let key: string | null = null;
  if (/^Key[A-Z]$/.test(e.code)) key = e.code.slice(3);
  else if (/^Digit\d$/.test(e.code)) key = e.code.slice(5);
  else if (/^F\d{1,2}$/.test(e.code)) key = e.code;
  if (!key) return null;
  const mods = [e.ctrlKey && "Ctrl", e.altKey && "Alt", e.shiftKey && "Shift", e.metaKey && "Meta"].filter(Boolean);
  return [...mods, key].join("+");
}

function ShortcutRecorder({ value, onChange, t }: { value: string; onChange: (v: string) => void; t: Strings }) {
  const [recording, setRecording] = useState(false);
  const [warn, setWarn] = useState(false);

  useEffect(() => {
    if (!recording) return;
    const onKey = (e: KeyboardEvent) => {
      e.preventDefault();
      if (e.key === "Escape") {
        setRecording(false);
        return;
      }
      const combo = comboFromEvent(e);
      if (!combo) return;
      if (!(e.ctrlKey || e.altKey || e.metaKey)) {
        setWarn(true);
        return;
      }
      setRecording(false);
      setWarn(false);
      onChange(combo);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [recording, onChange]);

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center gap-2">
        {recording ? (
          <span className="animate-pulse text-xs text-primary">{t.shortcutRecording}</span>
        ) : (
          <Keys combo={value} />
        )}
        <Button size="sm" variant="outline" onClick={() => setRecording((r) => !r)}>
          {t.shortcutChange}
        </Button>
      </div>
      {warn && <span className="text-xs text-destructive">{t.shortcutNeedsModifier}</span>}
    </div>
  );
}

export function SettingsPanel({ b, t }: { b: BackendView; t: Strings }) {
  const s = b.settings;
  const [flow, setFlow] = useState(s.flow_scale);
  useEffect(() => setFlow(s.flow_scale), [s.flow_scale]);
  const hz = b.checks?.display?.hz ?? null;
  const base = b.stats?.base ?? 0;
  const gpus = b.checks?.gpus ?? [];

  return (
    <Card className="py-4">
      <CardContent className="flex flex-col gap-5 px-4">
        <div className="space-y-2">
          <Row title={t.multiplier} hint={t.multiplierHint}>
            <ToggleGroup
              type="single"
              variant="outline"
              size="sm"
              value={String(s.multiplier)}
              onValueChange={(v) => v && b.update({ multiplier: Number(v) as Settings["multiplier"] })}
            >
              {[2, 3, 4].map((m) => (
                <ToggleGroupItem key={m} value={String(m)} className="px-3 font-mono">
                  {m}x
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          </Row>
          {hz && (
            <p className="text-xs text-primary/90">
              {b.state?.active && base >= 5
                ? fmt(t.recommendLive, { m: recommend(base, hz), base: Math.round(base), hz: Math.round(hz) })
                : fmt(t.recommendIdle, { hz: Math.round(hz), m30: recommend(30, hz), m60: recommend(60, hz) })}
            </p>
          )}
        </div>
        <Separator />
        <div className="space-y-3">
          <Row title={t.flowScale} hint={t.flowScaleHint}>
            <span className="font-mono text-sm tabular-nums">{Math.round(flow * 100)}%</span>
          </Row>
          <Slider
            min={0.25}
            max={1}
            step={0.05}
            value={[flow]}
            onValueChange={([v]) => setFlow(v)}
            onValueCommit={([v]) => b.update({ flow_scale: Math.round(v * 100) / 100 })}
          />
        </div>
        <Separator />
        <Row title={t.performance} hint={t.performanceHint}>
          <Switch checked={s.performance} onCheckedChange={(v) => b.update({ performance: v })} />
        </Row>
        <Row title={t.hud} hint={t.hudHint}>
          <Switch checked={s.hud} onCheckedChange={(v) => b.update({ hud: v })} />
        </Row>
        <Separator />
        <div className="space-y-2">
          <div className="space-y-0.5">
            <Label className="text-sm">{t.gpu}</Label>
            <p className="text-xs leading-snug text-muted-foreground">{t.gpuHint}</p>
          </div>
          <Select
            value={s.gpu === null ? "auto" : String(s.gpu)}
            onValueChange={(v) => b.update({ gpu: v === "auto" ? null : Number(v) })}
          >
            <SelectTrigger size="sm" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="auto">
                {t.gpuAuto}
                {gpus.find((g) => g.discrete) ? ` · ${gpus.find((g) => g.discrete)?.name.replace(/\(R\)|\(TM\)/g, "")}` : ""}
              </SelectItem>
              {gpus.map((g) => (
                <SelectItem key={g.index} value={String(g.index)}>
                  {g.name.replace(/\(R\)|\(TM\)/g, "")}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <Separator />
        <div className="space-y-1">
          <Row title={t.shortcut} hint={t.shortcutHint}>
            <ShortcutRecorder value={s.toggle_keys} onChange={(v) => b.update({ toggle_keys: v })} t={t} />
          </Row>
          {b.ready?.altgr_keys && (
            <p className="text-xs text-muted-foreground">{fmt(t.shortcutAltGr, { keys: b.ready.altgr_keys })}</p>
          )}
        </div>
        <p className="text-xs text-muted-foreground">{t.appliesNextTime}</p>
      </CardContent>
    </Card>
  );
}
