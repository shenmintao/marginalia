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
import { useEffect, useRef, useState } from "react";

import { health, resolveTauriBaseUrl } from "@/api/client";
import { useI18n } from "@/lib/i18n";
import { frontendLog, getTauriLogDir } from "@/lib/frontendLog";

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
  const [logDir, setLogDir] = useState("~/Marginalia/logs");
  const loggedFirstFailure = useRef(false);
  const loggedStale = useRef(false);
  const { t } = useI18n();

  useEffect(() => {
    let cancelled = false;
    const startedAt = Date.now();

    (async () => {
      // Make sure we know which backend URL to poll.
      // In browser dev this is a no-op and returns instantly.
      await resolveTauriBaseUrl();
      const dir = await getTauriLogDir();
      if (!cancelled) setLogDir(dir);

      while (!cancelled) {
        const attempt = withTimeout(health(), PER_ATTEMPT_TIMEOUT_MS);
        try {
          await attempt;
          frontendLog("info", "backend health check passed", {
            waitedMs: Date.now() - startedAt,
          });
          if (!cancelled) setReady(true);
          return;
        } catch (e: unknown) {
          if (cancelled) return;
          const message = e instanceof Error ? e.message : String(e);
          const elapsed = Date.now() - startedAt;
          setLastError(message);
          setWaitedMs(elapsed);
          if (!loggedFirstFailure.current) {
            loggedFirstFailure.current = true;
            frontendLog("warn", "backend health check failed", {
              waitedMs: elapsed,
              error: message,
            });
          }
          if (elapsed >= STALE_THRESHOLD_MS && !loggedStale.current) {
            loggedStale.current = true;
            frontendLog("error", "backend health check still failing", {
              waitedMs: elapsed,
              error: message,
            });
          }
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
        <p className="mt-4 text-sm font-medium">{t.backend.starting}</p>
        {!stale ? (
          <p className="mt-1 text-xs text-fg-subtle">
            {t.backend.waiting}
          </p>
        ) : (
          <div className="mt-3 space-y-1 text-xs text-fg-subtle">
            <p>
              {t.backend.slow(Math.round(waitedMs / 1000))}
            </p>
            <p>
              {t.backend.checkLog}{" "}
              <span className="break-all font-mono">{logDir}</span>.
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
