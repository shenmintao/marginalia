import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn/ui-style class merge. Tailwind + conditional classes without
 *  worrying about ordering: `cn("p-2", condition && "p-4")` keeps p-4. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Keys that look human-friendly when rendered as a label, in order
 *  of preference. Mirrors the CLI's payload_label heuristic. */
const LABEL_KEYS = ["entry_id", "file_id", "session_id", "conversation_id", "path"];

export function payloadLabel(p: unknown): string {
  if (!p || typeof p !== "object") return "";
  const obj = p as Record<string, unknown>;
  for (const k of LABEL_KEYS) {
    const v = obj[k];
    if (v) {
      const s = String(v);
      return `${k}=${s.length > 24 ? s.slice(0, 24) + "…" : s}`;
    }
  }
  return "";
}

export function shortDuration(seconds: number): string {
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds - m * 60);
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

/** Format bytes as a human-readable size. KB/MB/GB are 1024-based to
 *  match what users see in OS file managers on Windows/macOS. */
export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}
