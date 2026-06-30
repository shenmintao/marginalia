type FrontendLogLevel = "debug" | "info" | "warn" | "error";
type InvokeFn = <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>;

const FALLBACK_LOG_DIR = "~/Marginalia/logs";

let installed = false;
let invokeLoader: Promise<InvokeFn | null> | null = null;
let logQueue: Promise<void> = Promise.resolve();
let logDirPromise: Promise<string> | null = null;

function isTauri(): boolean {
  if (typeof window === "undefined") return false;
  return Boolean(
    (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ ||
      (window as unknown as { __TAURI__?: unknown }).__TAURI__,
  );
}

async function loadInvoke(): Promise<InvokeFn | null> {
  if (!isTauri()) return null;
  if (!invokeLoader) {
    invokeLoader = import("@tauri-apps/api/core")
      .then((m) => m.invoke as InvokeFn)
      .catch(() => null);
  }
  return invokeLoader;
}

export function installFrontendErrorLogging(): void {
  if (installed || typeof window === "undefined") return;
  installed = true;

  frontendLog("info", "frontend boot", {
    location: {
      protocol: window.location.protocol,
      pathname: window.location.pathname,
      hash: window.location.hash ? window.location.hash.split("?")[0] : "",
    },
    userAgent: navigator.userAgent,
  });

  window.addEventListener("error", (event) => {
    frontendLog("error", "uncaught window error", {
      message: event.message,
      source: event.filename,
      line: event.lineno,
      column: event.colno,
      error: describeError(event.error),
    });
  });

  window.addEventListener("unhandledrejection", (event) => {
    frontendLog("error", "unhandled promise rejection", {
      reason: describeError(event.reason),
    });
  });
}

export function frontendLog(
  level: FrontendLogLevel,
  message: string,
  details?: unknown,
): void {
  if (!isTauri()) return;
  const line =
    details === undefined
      ? message
      : `${message} ${truncate(serializeDetails(details), 4_000)}`;

  logQueue = logQueue
    .then(async () => {
      const invoke = await loadInvoke();
      if (!invoke) return;
      await invoke<void>("append_frontend_log", {
        level,
        message: truncate(line, 8_000),
      });
    })
    .catch(() => {
      // There is no useful fallback in a packaged webview if IPC logging fails.
    });
}

export async function getTauriLogDir(): Promise<string> {
  if (!isTauri()) return FALLBACK_LOG_DIR;
  if (!logDirPromise) {
    logDirPromise = (async () => {
      const invoke = await loadInvoke();
      if (!invoke) return FALLBACK_LOG_DIR;
      try {
        const dir = await invoke<string>("logs_dir");
        return dir || FALLBACK_LOG_DIR;
      } catch {
        return FALLBACK_LOG_DIR;
      }
    })();
  }
  return logDirPromise;
}

export function describeError(value: unknown): unknown {
  if (value instanceof Error) {
    return {
      name: value.name,
      message: value.message,
      stack: value.stack ? truncate(value.stack, 4_000) : undefined,
    };
  }
  if (typeof value === "string") return truncate(value, 1_000);
  if (value === null || value === undefined) return value;
  if (typeof value === "number" || typeof value === "boolean") return value;
  return truncate(serializeDetails(value), 2_000);
}

function serializeDetails(details: unknown): string {
  const seen = new WeakSet<object>();
  try {
    return JSON.stringify(details, (_key, value: unknown) => {
      if (value instanceof Error) return describeError(value);
      if (typeof value === "bigint") return value.toString();
      if (typeof value === "object" && value !== null) {
        if (seen.has(value)) return "[Circular]";
        seen.add(value);
      }
      return value;
    });
  } catch {
    return String(details);
  }
}

function truncate(value: string, maxLength: number): string {
  if (value.length <= maxLength) return value;
  return `${value.slice(0, maxLength)}...`;
}
