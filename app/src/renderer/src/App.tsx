// SPDX-License-Identifier: GPL-3.0-or-later
import { EyeOff, Power } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useBackend } from "@/hooks/use-backend";
import { strings } from "../../shared/i18n";
import { DoctorPanel } from "./components/doctor-panel";
import { SettingsPanel } from "./components/settings-panel";
import { StatusCard } from "./components/status-card";
import { WindowList } from "./components/window-list";
import logo from "./logo.svg";

const t = strings(navigator.language);
const VERSION = "0.1.0";

export default function App() {
  const b = useBackend();
  const active = b.state?.active ?? false;

  return (
    <TooltipProvider>
      <div className="flex h-full flex-col">
        <header className="flex items-center gap-3 px-5 pt-5 pb-3">
          <img src={logo} alt="" className="size-9" draggable={false} />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <h1 className="text-base font-semibold tracking-tight">TweenGlass</h1>
              {active && <Badge className="bg-success/20 text-success">{t.active} · {b.settings.multiplier}x</Badge>}
            </div>
            <p className="text-xs text-muted-foreground">{t.appTagline}</p>
          </div>
          <Button variant="ghost" size="icon" className="size-8" title={t.hide} onClick={() => window.tweenglass.hide()}>
            <EyeOff className="size-4" />
          </Button>
          <Button variant="ghost" size="icon" className="size-8" title={t.quit} onClick={() => window.tweenglass.quit()}>
            <Power className="size-4" />
          </Button>
        </header>

        <Tabs defaultValue="home" className="flex min-h-0 flex-1 flex-col gap-0">
          <div className="px-5">
            <TabsList className="grid w-full grid-cols-3">
              <TabsTrigger value="home">{t.tabHome}</TabsTrigger>
              <TabsTrigger value="settings">{t.tabSettings}</TabsTrigger>
              <TabsTrigger value="doctor">{t.tabDoctor}</TabsTrigger>
            </TabsList>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-5 pt-4 pb-5">
            <TabsContent value="home" className="flex flex-col gap-3">
              <StatusCard b={b} t={t} />
              <WindowList b={b} t={t} />
            </TabsContent>
            <TabsContent value="settings">
              <SettingsPanel b={b} t={t} />
            </TabsContent>
            <TabsContent value="doctor">
              <DoctorPanel b={b} t={t} version={VERSION} />
            </TabsContent>
          </div>
        </Tabs>

        <footer className="border-t px-5 py-2.5 text-center text-[11px] text-muted-foreground">{t.closeToTray}</footer>
      </div>
    </TooltipProvider>
  );
}
