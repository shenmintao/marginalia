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
 *  which leave fingerprints in `<MARGINALIA_HOME>/logs/backend.log`.
 *
 *  When the Rust shell reports a doomed startup (spawn failed, the
 *  configured port is occupied, or the sidecar process exited) via the
 *  `backend_status` command, we stop spinning and render an explicit
 *  error screen with the log path plus Retry / Quit actions. */
import { useEffect, useRef, useState } from "react";

import { health, resetResolvedBaseUrl, resolveTauriBaseUrl } from "@/api/client";
import { useI18n } from "@/lib/i18n";
import { frontendLog, getTauriLogDir } from "@/lib/frontendLog";

const POLL_INTERVAL_MS = 300;
const PER_ATTEMPT_TIMEOUT_MS = 1500;
const STALE_THRESHOLD_MS = 8000;

interface Props {
  children: React.ReactNode;
}

interface BackendStatusInfo {
  state: string;
  message: string | null;
}

function isTauri(): boolean {
  if (typeof window === "undefined") return false;
  return Boolean(
    (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ ||
      (window as unknown as { __TAURI__?: unknown }).__TAURI__,
  );
}

async function fetchBackendStatus(): Promise<BackendStatusInfo | null> {
  if (!isTauri()) return null;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    return await invoke<BackendStatusInfo>("backend_status");
  } catch {
    // Older shells without the command — keep the plain polling UX.
    return null;
  }
}

export function BackendGate({ children }: Props) {
  const [ready, setReady] = useState(false);
  const [waitedMs, setWaitedMs] = useState(0);
  const [lastError, setLastError] = useState<string | null>(null);
  const [fatal, setFatal] = useState<BackendStatusInfo | null>(null);
  const [retryNonce, setRetryNonce] = useState(0);
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
          // Ask the shell whether startup is doomed (spawn error, port
          // conflict, dead child) — no point spinning on /health then.
          const status = await fetchBackendStatus();
          if (cancelled) return;
          if (status && (status.state === "error" || status.state === "exited")) {
            frontendLog("error", "backend startup failed", {
              state: status.state,
              message: status.message,
            });
            setFatal(status);
            return;
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
  }, [retryNonce]);

  if (ready) return <>{children}</>;

  if (fatal) {
    return (
      <div className="flex h-full w-full items-center justify-center bg-bg-base text-fg-base">
        <div className="max-w-md text-center">
          <p className="text-sm font-medium text-danger">{t.backend.failedTitle}</p>
          <div className="mt-3 space-y-2 text-xs text-fg-subtle">
            <p>{t.backend.failedBody}</p>
            {fatal.message && (
              <p className="break-all rounded border border-border bg-bg-subtle p-2 text-left font-mono text-[11px]">
                {fatal.message}
              </p>
            )}
            <p>
              {t.backend.failedLogHint}{" "}
              <span className="break-all font-mono">{logDir}</span>.
            </p>
          </div>
          <div className="mt-4 flex justify-center gap-2">
            <button
              type="button"
              onClick={() => {
                setFatal(null);
                setWaitedMs(0);
                setLastError(null);
                loggedFirstFailure.current = false;
                loggedStale.current = false;
                void (async () => {
                  try {
                    const { invoke } = await import("@tauri-apps/api/core");
                    await invoke("restart_backend");
                  } catch {
                    /* browser dev / older shell: just resume polling */
                  }
                  // The respawned sidecar binds a fresh ephemeral port;
                  // drop the cached base URL so the poll loop re-asks
                  // the shell instead of hammering the dead old port.
                  resetResolvedBaseUrl();
                  setRetryNonce((n) => n + 1);
                })();
              }}
              className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-accent-fg hover:opacity-90"
            >
              {t.backend.retry}
            </button>
            <button
              type="button"
              onClick={() => {
                void (async () => {
                  try {
                    const { invoke } = await import("@tauri-apps/api/core");
                    await invoke("quit_app");
                  } catch {
                    window.close();
                  }
                })();
              }}
              className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-bg-muted"
            >
              {t.backend.quit}
            </button>
          </div>
        </div>
      </div>
    );
  }

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
