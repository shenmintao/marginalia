import { NavLink } from "react-router-dom";
import { BookOpen, MessageSquare, Search, Settings, Library } from "lucide-react";

import { APP_VERSION } from "@/lib/appVersion";
import { cn } from "@/lib/utils";
import { usePrefs } from "@/lib/prefs";
import { useI18n } from "@/lib/i18n";

interface Item {
  to: string;
  labelKey: "chat" | "library" | "search" | "settings";
  icon: typeof MessageSquare;
}

const ITEMS: Item[] = [
  { to: "/chat", labelKey: "chat", icon: MessageSquare },
  { to: "/library", labelKey: "library", icon: BookOpen },
  { to: "/search", labelKey: "search", icon: Search },
  { to: "/settings", labelKey: "settings", icon: Settings },
];

export function Sidebar() {
  const compact = usePrefs((s) => s.compactSidebar);
  const { t } = useI18n();
  return (
    <aside
      className={cn(
        "flex shrink-0 flex-col border-r border-border bg-bg-subtle",
        compact ? "w-14" : "w-56",
      )}
    >
      <div className={cn("flex items-center gap-2 py-4", compact ? "justify-center px-2" : "px-4")}>
        <div className="flex h-8 w-8 items-center justify-center rounded-md bg-accent text-accent-fg">
          <Library size={18} strokeWidth={2.2} />
        </div>
        {!compact && (
          <div className="flex flex-col leading-tight">
            <span className="text-sm font-semibold tracking-tight">{t.common.appName}</span>
            <span className="text-[11px] text-fg-subtle">{t.common.personalLibrary}</span>
          </div>
        )}
      </div>

      <nav className="flex flex-col gap-0.5 px-2">
        {ITEMS.map((it) => {
          const Icon = it.icon;
          const label = t.nav[it.labelKey];
          return (
            <NavLink
              key={it.to}
              to={it.to}
              title={compact ? label : undefined}
              className={({ isActive }) =>
                cn(
                  "flex items-center rounded-md text-sm transition-colors",
                  "hover:bg-bg-muted",
                  compact
                    ? "justify-center px-2 py-2"
                    : "gap-2.5 px-2.5 py-1.5",
                  isActive
                    ? "bg-bg-muted text-fg-base font-medium"
                    : "text-fg-muted",
                )
              }
            >
              <Icon size={16} strokeWidth={2} />
              {!compact && <span>{label}</span>}
            </NavLink>
          );
        })}
      </nav>

      {!compact && (
        <div className="mt-auto px-4 py-3 text-[11px] text-fg-subtle">
          v{APP_VERSION}
        </div>
      )}
    </aside>
  );
}
