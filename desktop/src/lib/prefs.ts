/** User preferences persisted to localStorage.
 *
 *  Centralised so other components (StatusBar refresh cadence, sidebar
 *  layout) can subscribe instead of each reading raw localStorage with
 *  their own keys. */
import { create } from "zustand";

export type LanguagePreference = "auto" | "en" | "zh";

interface PrefsState {
  /** StatusBar polling interval in ms; clamped to [1000, 60000]. */
  statusPollMs: number;
  /** Auto-collapse sidebar on small windows. */
  compactSidebar: boolean;
  /** UI language. auto follows navigator.language. */
  language: LanguagePreference;

  setStatusPollMs: (v: number) => void;
  setCompactSidebar: (v: boolean) => void;
  setLanguage: (v: LanguagePreference) => void;
}

const KEY_POLL = "marginalia.prefs.status_poll_ms";
const KEY_COMPACT = "marginalia.prefs.compact_sidebar";
const KEY_LANGUAGE = "marginalia.prefs.language";

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

function readLanguage(): LanguagePreference {
  if (typeof localStorage === "undefined") return "auto";
  const raw = localStorage.getItem(KEY_LANGUAGE);
  return raw === "en" || raw === "zh" || raw === "auto" ? raw : "auto";
}

export const usePrefs = create<PrefsState>((set) => ({
  statusPollMs: readPollMs(),
  compactSidebar: readCompact(),
  language: readLanguage(),
  setStatusPollMs: (v) => {
    const clamped = Math.min(60000, Math.max(1000, Math.round(v)));
    localStorage.setItem(KEY_POLL, String(clamped));
    set({ statusPollMs: clamped });
  },
  setCompactSidebar: (v) => {
    localStorage.setItem(KEY_COMPACT, v ? "1" : "0");
    set({ compactSidebar: v });
  },
  setLanguage: (v) => {
    localStorage.setItem(KEY_LANGUAGE, v);
    set({ language: v });
  },
}));
