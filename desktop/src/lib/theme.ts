/** Theme store (light/dark/system) persisted to localStorage.
 *  Mirrors the user's OS preference by default; a manual toggle
 *  overrides until the user clicks "system" again. */
import { create } from "zustand";

type ThemeMode = "light" | "dark" | "system";

interface ThemeState {
  mode: ThemeMode;
  effective: "light" | "dark";
  setMode: (m: ThemeMode) => void;
  init: () => () => void;
}

const STORAGE_KEY = "marginalia.theme";

function systemPrefersDark(): boolean {
  return typeof window !== "undefined"
    && window.matchMedia
    && window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function applyTheme(mode: ThemeMode): "light" | "dark" {
  const effective: "light" | "dark" =
    mode === "system" ? (systemPrefersDark() ? "dark" : "light") : mode;
  if (typeof document !== "undefined") {
    document.documentElement.classList.toggle("dark", effective === "dark");
  }
  return effective;
}

export const useTheme = create<ThemeState>((set, get) => ({
  mode:
    (typeof localStorage !== "undefined"
      && (localStorage.getItem(STORAGE_KEY) as ThemeMode | null))
    || "system",
  effective: "light",
  setMode: (m) => {
    if (typeof localStorage !== "undefined") localStorage.setItem(STORAGE_KEY, m);
    set({ mode: m, effective: applyTheme(m) });
  },
  init: () => {
    set({ effective: applyTheme(get().mode) });
    if (typeof window === "undefined" || !window.matchMedia) return () => {};
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => {
      if (get().mode === "system") {
        set({ effective: applyTheme("system") });
      }
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  },
}));
