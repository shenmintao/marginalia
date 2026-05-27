import { useEffect, useRef, useState } from "react";
import { Activity, Wifi, WifiOff } from "lucide-react";

import { health, tasks } from "@/api/client";
import { ActivityPopover } from "@/components/ActivityPopover";
import { usePrefs } from "@/lib/prefs";
import { cn } from "@/lib/utils";

export function StatusBar() {
  const [online, setOnline] = useState<boolean | null>(null);
  const [storage, setStorage] = useState<string>("");
  const [busy, setBusy] = useState({ running: 0, pending: 0 });
  const [popoverOpen, setPopoverOpen] = useState(false);
  const popoverRef = useRef<HTMLDivElement>(null);
  const pollMs = usePrefs((s) => s.statusPollMs);

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
    const id = window.setInterval(tick, pollMs);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [pollMs]);

  // Click-outside to close — the popover sits absolutely above the
  // footer, so any click outside the wrapper should dismiss it.
  useEffect(() => {
    if (!popoverOpen) return;
    function onDown(ev: MouseEvent) {
      if (!popoverRef.current) return;
      if (!popoverRef.current.contains(ev.target as Node)) {
        setPopoverOpen(false);
      }
    }
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [popoverOpen]);

  const totalBusy = busy.running + busy.pending;

  return (
    <footer className="relative flex h-7 items-center justify-between border-t border-border bg-bg-subtle px-3 text-[11px] text-fg-muted">
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
      <div ref={popoverRef}>
        <button
          onClick={() => setPopoverOpen((o) => !o)}
          className={cn(
            "flex items-center gap-1 rounded px-1 py-0.5",
            "hover:bg-bg-muted hover:text-fg-base",
            popoverOpen && "bg-bg-muted text-fg-base",
          )}
          title="Show task activity"
        >
          <Activity
            size={11}
            className={cn(totalBusy > 0 && "text-accent animate-pulse-soft")}
          />
          {totalBusy > 0
            ? `${busy.running} running · ${busy.pending} pending`
            : "idle"}
        </button>
        <ActivityPopover open={popoverOpen} pollMs={pollMs} />
      </div>
    </footer>
  );
}
