/** User preferences persisted to localStorage.
 *
 *  Centralised so other components (StatusBar refresh cadence, upload
 *  default conflict policy) can subscribe instead of each reading raw
 *  localStorage with their own keys. */
import { create } from "zustand";

import type { OnConflict } from "@/types/api";

interface PrefsState {
  /** Default conflict policy used by uploads when the form doesn't set
   *  one explicitly. Server still owns the per-call decision. */
  defaultOnConflict: OnConflict;
  /** StatusBar polling interval in ms; clamped to [1000, 60000]. */
  statusPollMs: number;
  /** Auto-collapse sidebar on small windows. */
  compactSidebar: boolean;

  setDefaultOnConflict: (v: OnConflict) => void;
  setStatusPollMs: (v: number) => void;
  setCompactSidebar: (v: boolean) => void;
}

const KEY_CONFLICT = "marginalia.prefs.on_conflict";
const KEY_POLL = "marginalia.prefs.status_poll_ms";
const KEY_COMPACT = "marginalia.prefs.compact_sidebar";

function readOnConflict(): OnConflict {
  if (typeof localStorage === "undefined") return "rename";
  const v = localStorage.getItem(KEY_CONFLICT);
  return v === "rename" || v === "error" || v === "skip" ? v : "rename";
}

function readPollMs(): number {
  if (typeof localStorage === "undefined") return 4000;
  const raw = localStorage.getItem(KEY_POLL);
  const n = raw ? parseInt(raw, 10) : NaN;
  if (!Number.isFinite(n)) return 4000;
  return Math.min(60000, Math.max(1000, n));
}

function readCompact(): boolean {
  if (typeof localStorage === "undefined") return false;
  return localStorage.getItem(KEY_COMPACT) === "1";
}

export const usePrefs = create<PrefsState>((set) => ({
  defaultOnConflict: readOnConflict(),
  statusPollMs: readPollMs(),
  compactSidebar: readCompact(),
  setDefaultOnConflict: (v) => {
    localStorage.setItem(KEY_CONFLICT, v);
    set({ defaultOnConflict: v });
  },
  setStatusPollMs: (v) => {
    const clamped = Math.min(60000, Math.max(1000, Math.round(v)));
    localStorage.setItem(KEY_POLL, String(clamped));
    set({ statusPollMs: clamped });
  },
  setCompactSidebar: (v) => {
    localStorage.setItem(KEY_COMPACT, v ? "1" : "0");
    set({ compactSidebar: v });
  },
}));
