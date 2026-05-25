import { useEffect, useState } from "react";
import { Activity, Wifi, WifiOff } from "lucide-react";

import { health, tasks } from "@/api/client";
import { cn } from "@/lib/utils";

export function StatusBar() {
  const [online, setOnline] = useState<boolean | null>(null);
  const [storage, setStorage] = useState<string>("");
  const [busy, setBusy] = useState({ running: 0, pending: 0 });

  useEffect(() => {
    let cancelled = false;

    async function tick() {
      try {
        const h = await health();
        if (cancelled) return;
        setOnline(true);
        setStorage(h.storage_backend);
      } catch {
        if (!cancelled) setOnline(false);
      }
      try {
        const c = await tasks.runningCount();
        if (!cancelled) setBusy(c);
      } catch {
        /* keep last value */
      }
    }

    tick();
    const id = window.setInterval(tick, 4000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  const totalBusy = busy.running + busy.pending;

  return (
    <footer className="flex h-7 items-center justify-between border-t border-border bg-bg-subtle px-3 text-[11px] text-fg-muted">
      <div className="flex items-center gap-3">
        <span
          className={cn(
            "flex items-center gap-1",
            online === false && "text-danger",
          )}
        >
          {online === false ? <WifiOff size={11} /> : <Wifi size={11} />}
          {online === null
            ? "connecting…"
            : online
              ? `connected · ${storage}`
              : "backend offline"}
        </span>
      </div>
      <div className="flex items-center gap-1">
        <Activity
          size={11}
          className={cn(totalBusy > 0 && "text-accent animate-pulse-soft")}
        />
        {totalBusy > 0
          ? `${busy.running} running · ${busy.pending} pending`
          : "idle"}
      </div>
    </footer>
  );
}
