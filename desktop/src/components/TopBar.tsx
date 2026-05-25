import { Moon, Sun, MonitorSmartphone } from "lucide-react";

import { useTheme } from "@/lib/theme";
import { cn } from "@/lib/utils";

export function TopBar() {
  const { mode, setMode } = useTheme();

  return (
    <header className="flex h-12 items-center justify-between border-b border-border bg-bg-base/80 px-4 backdrop-blur">
      <div className="flex items-center gap-2 text-sm text-fg-muted">
        <span className="font-medium text-fg-base">Marginalia</span>
      </div>

      <div className="flex items-center gap-1 rounded-md border border-border bg-bg-subtle p-0.5">
        <ThemeBtn current={mode} mode="light" onClick={() => setMode("light")}>
          <Sun size={14} />
        </ThemeBtn>
        <ThemeBtn current={mode} mode="system" onClick={() => setMode("system")}>
          <MonitorSmartphone size={14} />
        </ThemeBtn>
        <ThemeBtn current={mode} mode="dark" onClick={() => setMode("dark")}>
          <Moon size={14} />
        </ThemeBtn>
      </div>
    </header>
  );
}

function ThemeBtn({
  current, mode, onClick, children,
}: {
  current: string; mode: string;
  onClick: () => void; children: React.ReactNode;
}) {
  const active = current === mode;
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex h-6 w-6 items-center justify-center rounded transition-colors",
        active
          ? "bg-bg-elevated text-fg-base shadow-sm"
          : "text-fg-subtle hover:text-fg-base",
      )}
      title={mode}
    >
      {children}
    </button>
  );
}
