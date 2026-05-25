import { NavLink } from "react-router-dom";
import { BookOpen, MessageSquare, Search, Settings, Library } from "lucide-react";

import { cn } from "@/lib/utils";

interface Item {
  to: string;
  label: string;
  icon: typeof MessageSquare;
}

const ITEMS: Item[] = [
  { to: "/chat", label: "Chat", icon: MessageSquare },
  { to: "/library", label: "Library", icon: BookOpen },
  { to: "/search", label: "Search", icon: Search },
  { to: "/settings", label: "Settings", icon: Settings },
];

export function Sidebar() {
  return (
    <aside className="flex w-56 shrink-0 flex-col border-r border-border bg-bg-subtle">
      <div className="flex items-center gap-2 px-4 py-4">
        <div className="flex h-8 w-8 items-center justify-center rounded-md bg-accent text-accent-fg">
          <Library size={18} strokeWidth={2.2} />
        </div>
        <div className="flex flex-col leading-tight">
          <span className="text-sm font-semibold tracking-tight">Marginalia</span>
          <span className="text-[11px] text-fg-subtle">personal library</span>
        </div>
      </div>

      <nav className="flex flex-col gap-0.5 px-2">
        {ITEMS.map((it) => {
          const Icon = it.icon;
          return (
            <NavLink
              key={it.to}
              to={it.to}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-sm transition-colors",
                  "hover:bg-bg-muted",
                  isActive
                    ? "bg-bg-muted text-fg-base font-medium"
                    : "text-fg-muted",
                )
              }
            >
              <Icon size={16} strokeWidth={2} />
              <span>{it.label}</span>
            </NavLink>
          );
        })}
      </nav>

      <div className="mt-auto px-4 py-3 text-[11px] text-fg-subtle">
        v0.1.0 — local-first
      </div>
    </aside>
  );
}
