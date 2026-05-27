/** Splash that blocks the main app until the Python sidecar's /health
 *  responds 200. Without it, the webview mounts faster than the sidecar
 *  binds its port, the first /v1/* call fires into the void, and React
 *  Query / pages render their "Failed to fetch" empty states — confusing
 *  on a fresh launch where the backend is still warming up.
 *
 *  Cadence: poll every 300ms, with a 1500ms per-attempt timeout. Most
 *  cold starts settle in 1-3s; in the happy path (backend already up,
 *  e.g. `pnpm dev` against running uvicorn) the first poll succeeds and
 *  the splash flashes for ~50ms.
 *
 *  After STALE_THRESHOLD_MS we widen the splash to surface what's wrong
 *  — usually a missing python runtime or a port collision, both of
 *  which leave fingerprints in `<MARGINALIA_HOME>/logs/backend.log`. */
import { useEffect, useState } from "react";

import { health, resolveTauriBaseUrl } from "@/api/client";

const POLL_INTERVAL_MS = 300;
const PER_ATTEMPT_TIMEOUT_MS = 1500;
const STALE_THRESHOLD_MS = 8000;

interface Props {
  children: React.ReactNode;
}

export function BackendGate({ children }: Props) {
  const [ready, setReady] = useState(false);
  const [waitedMs, setWaitedMs] = useState(0);
  const [lastError, setLastError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const startedAt = Date.now();

    (async () => {
      // Make sure we know which port the sidecar bound before we poll.
      // In browser dev this is a no-op and returns instantly.
      await resolveTauriBaseUrl();

      while (!cancelled) {
        const attempt = withTimeout(health(), PER_ATTEMPT_TIMEOUT_MS);
        try {
          await attempt;
          if (!cancelled) setReady(true);
          return;
        } catch (e: unknown) {
          if (cancelled) return;
          setLastError(e instanceof Error ? e.message : String(e));
          setWaitedMs(Date.now() - startedAt);
          await sleep(POLL_INTERVAL_MS);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  if (ready) return <>{children}</>;

  const stale = waitedMs >= STALE_THRESHOLD_MS;
  return (
    <div className="flex h-full w-full items-center justify-center bg-bg-base text-fg-base">
      <div className="max-w-md text-center">
        <div className="mx-auto h-8 w-8 animate-spin rounded-full border-2 border-border border-t-accent" />
        <p className="mt-4 text-sm font-medium">Starting Marginalia…</p>
        {!stale ? (
          <p className="mt-1 text-xs text-fg-subtle">
            Waiting for the local backend to come up.
          </p>
        ) : (
          <div className="mt-3 space-y-1 text-xs text-fg-subtle">
            <p>
              The backend hasn't responded in {Math.round(waitedMs / 1000)}s.
              It usually starts within a few seconds.
            </p>
            <p>
              If this persists, check{" "}
              <span className="font-mono">~/Marginalia/logs/backend.log</span>.
            </p>
            {lastError && (
              <p className="font-mono text-[10px] opacity-70">{lastError}</p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error("timeout")), ms);
    p.then(
      (v) => {
        clearTimeout(t);
        resolve(v);
      },
      (e) => {
        clearTimeout(t);
        reject(e);
      },
    );
  });
}
