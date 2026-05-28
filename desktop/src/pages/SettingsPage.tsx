/** Settings page — three sections.
 *
 *  1. Connection: API base URL (client-side, persisted to localStorage)
 *  2. Preferences: theme, default conflict policy, status-bar polling
 *  3. Server status (read-only) + LLM profile editor (writable overlay)
 *
 *  Server state (server + llm) lives on the page so Preferences and the
 *  Server-status card stay in lockstep — picking a conflict policy in
 *  Preferences updates the read-only line below without a re-fetch.
 *
 *  Sections 1-2 work without a backend. Section 3 calls /v1/settings/*
 *  and shows a friendly empty state if the server is offline. */
import { useEffect, useState } from "react";
import { Save, Sun, Moon, Monitor } from "lucide-react";

import { setBaseUrl, getBaseUrl, settings as settingsApi } from "@/api/client";
import { LlmProfileEditor } from "@/components/LlmProfileEditor";
import { usePrefs } from "@/lib/prefs";
import { useTheme } from "@/lib/theme";
import { cn } from "@/lib/utils";
import type { LlmSettings, OnConflict, ServerSettings } from "@/types/api";

const STORAGE_KEY = "marginalia.api_base";

interface ServerCtx {
  server: ServerSettings | null;
  llm: LlmSettings | null;
  err: string | null;
  setLlm: (next: LlmSettings) => void;
  setDefaultConflict: (v: OnConflict) => Promise<void>;
  setServerNumber: (
    field:
      | "agent_plan_max_tokens"
      | "agent_execute_max_tokens"
      | "agent_execute_max_turns"
      | "worker_batch_size"
      | "llm_ingest_concurrency",
    v: number,
  ) => Promise<void>;
}

export function SettingsPage() {
  const ctx = useServerCtx();
  return (
    <div className="h-full overflow-y-auto px-8 py-8">
      <div className="mx-auto max-w-2xl space-y-6">
        <h1 className="text-xl font-semibold">Settings</h1>

        <ConnectionSection />
        <PreferencesSection ctx={ctx} />
        <ServerSection ctx={ctx} />
      </div>
    </div>
  );
}

function useServerCtx(): ServerCtx {
  const [server, setServer] = useState<ServerSettings | null>(null);
  const [llm, setLlm] = useState<LlmSettings | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [s, l] = await Promise.all([
          settingsApi.server(),
          settingsApi.llm(),
        ]);
        if (cancelled) return;
        setServer(s);
        setLlm(l);
        setErr(null);
      } catch (e: unknown) {
        if (cancelled) return;
        setErr(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const setDefaultConflict = async (v: OnConflict) => {
    if (!server || server.default_on_conflict === v) return;
    const prev = server;
    setServer({ ...server, default_on_conflict: v });
    try {
      await settingsApi.updateLlm({ default_on_conflict: v });
    } catch {
      setServer(prev); // roll back the optimistic update
    }
  };

  const setServerNumber = async (
    field:
      | "agent_plan_max_tokens"
      | "agent_execute_max_tokens"
      | "agent_execute_max_turns"
      | "worker_batch_size"
      | "llm_ingest_concurrency",
    v: number,
  ) => {
    if (!server || server[field] === v) return;
    const prev = server;
    setServer({ ...server, [field]: v });
    try {
      await settingsApi.updateLlm({ [field]: v });
    } catch {
      setServer(prev);
    }
  };

  return { server, llm, err, setLlm, setDefaultConflict, setServerNumber };
}

// ---- Connection ------------------------------------------------------------

function ConnectionSection() {
  const [base, setBase] = useState(
    () => localStorage.getItem(STORAGE_KEY) || getBaseUrl(),
  );
  const [savedAt, setSavedAt] = useState<number | null>(null);

  const save = () => {
    const v = base.trim().replace(/\/$/, "");
    if (v) localStorage.setItem(STORAGE_KEY, v);
    else localStorage.removeItem(STORAGE_KEY);
    setBaseUrl(v);
    setSavedAt(Date.now());
  };

  return (
    <Section title="Connection" subtitle="How the GUI reaches the Marginalia backend.">
      <label className="block text-sm font-medium">API base URL</label>
      <p className="mt-1 text-xs text-fg-subtle">
        Leave empty to use the dev proxy (recommended in browser). Set to
        <span className="mx-1 font-mono">http://host:8000</span>
        when connecting to a remote server.
      </p>
      <div className="mt-3 flex gap-2">
        <input
          value={base}
          onChange={(e) => setBase(e.target.value)}
          placeholder="(empty = same-origin / proxy)"
          className="flex-1 rounded-md border border-border bg-bg-base px-3 py-1.5 font-mono text-sm outline-none focus:border-accent"
        />
        <button
          onClick={save}
          className={cn(
            "flex items-center gap-1.5 rounded-md bg-accent px-3 text-sm font-medium text-accent-fg hover:opacity-90",
          )}
        >
          <Save size={13} /> Save
        </button>
      </div>
      {savedAt && (
        <p className="mt-2 text-xs text-fg-subtle">
          Saved · {new Date(savedAt).toLocaleTimeString()}
        </p>
      )}
    </Section>
  );
}

// ---- Preferences -----------------------------------------------------------

function PreferencesSection({ ctx }: { ctx: ServerCtx }) {
  const { mode, setMode } = useTheme();
  const prefs = usePrefs();
  const { server, setDefaultConflict, setServerNumber } = ctx;

  return (
    <Section title="Preferences" subtitle="Local appearance and defaults.">
      <div className="space-y-5">
        <Row label="Theme">
          <div className="flex gap-1">
            {(["light", "dark", "system"] as const).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                className={cn(
                  "flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs",
                  mode === m
                    ? "border-accent bg-accent/10 text-accent"
                    : "border-border bg-bg-base hover:bg-bg-subtle",
                )}
              >
                {m === "light" && <Sun size={11} />}
                {m === "dark" && <Moon size={11} />}
                {m === "system" && <Monitor size={11} />}
                {m}
              </button>
            ))}
          </div>
        </Row>

        <Row
          label="Default conflict policy"
          hint="Used when uploading a file whose name already exists in the target folder. Stored on the server so the CLI and GUI agree."
        >
          <select
            value={server?.default_on_conflict ?? "rename"}
            disabled={!server}
            onChange={(e) => setDefaultConflict(e.target.value as OnConflict)}
            className="rounded border border-border bg-bg-base px-2 py-1 text-sm disabled:opacity-50"
          >
            <option value="rename">rename (append a suffix)</option>
            <option value="error">error (reject the upload)</option>
            <option value="skip">skip (keep existing)</option>
          </select>
        </Row>

        <Row
          label="Agent token budget"
          hint="Max tokens per agent step (plan / execute). Bump these for long-context models that emit large single responses."
        >
          <div className="flex items-center gap-1.5">
            <NumberInput
              value={server?.agent_plan_max_tokens}
              disabled={!server}
              min={1}
              max={200000}
              step={128}
              className="w-20"
              onCommit={(v) => setServerNumber("agent_plan_max_tokens", v)}
            />
            <span className="text-xs text-fg-subtle">/</span>
            <NumberInput
              value={server?.agent_execute_max_tokens}
              disabled={!server}
              min={1}
              max={200000}
              step={128}
              className="w-20"
              onCommit={(v) => setServerNumber("agent_execute_max_tokens", v)}
            />
          </div>
        </Row>

        <Row
          label="Agent execute turn budget"
          hint="Max tool-using rounds per question. Higher = deeper investigations at the cost of more LLM calls. The agent gets a wrap-up nudge when the last 1/3 of the budget is reached."
        >
          <NumberInput
            value={server?.agent_execute_max_turns}
            disabled={!server}
            min={3}
            max={100}
            step={1}
            className="w-16"
            onCommit={(v) => setServerNumber("agent_execute_max_turns", v)}
          />
        </Row>

        <Row
          label="Concurrent ingest tasks"
          hint="Maximum background tasks the worker runs at once. Lower this when ingesting large scanned PDFs."
        >
          <NumberInput
            value={server?.worker_batch_size}
            disabled={!server}
            min={1}
            max={32}
            step={1}
            className="w-16"
            onCommit={(v) => setServerNumber("worker_batch_size", v)}
          />
        </Row>

        <Row
          label="Ingest LLM concurrency"
          hint="Parallel LLM calls for long text/PDF chunks and scanned-PDF OCR pages. Lower it if your provider rate-limits."
        >
          <NumberInput
            value={server?.llm_ingest_concurrency}
            disabled={!server}
            min={1}
            max={32}
            step={1}
            className="w-16"
            onCommit={(v) => setServerNumber("llm_ingest_concurrency", v)}
          />
        </Row>

        <Row
          label="Status bar refresh"
          hint="How often the bottom bar polls /health and the running task count."
        >
          <select
            value={prefs.statusPollMs}
            onChange={(e) => prefs.setStatusPollMs(parseInt(e.target.value, 10))}
            className="rounded border border-border bg-bg-base px-2 py-1 text-sm"
          >
            <option value={2000}>2 s</option>
            <option value={4000}>4 s (default)</option>
            <option value={10000}>10 s</option>
            <option value={30000}>30 s</option>
            <option value={60000}>60 s</option>
          </select>
        </Row>

        <Row label="Compact sidebar" hint="Show icon-only navigation on the left.">
          <input
            type="checkbox"
            checked={prefs.compactSidebar}
            onChange={(e) => prefs.setCompactSidebar(e.target.checked)}
            className="h-4 w-4 accent-accent"
          />
        </Row>
      </div>
    </Section>
  );
}

// ---- Server status + LLM editor --------------------------------------------

function ServerSection({ ctx }: { ctx: ServerCtx }) {
  const { server, llm, err, setLlm } = ctx;

  if (err) {
    return (
      <Section title="Server" subtitle="Live state of the running backend.">
        <p className="text-sm text-danger">Backend unreachable — {err}</p>
      </Section>
    );
  }

  if (!server || !llm) {
    return (
      <Section title="Server" subtitle="Live state of the running backend.">
        <p className="text-sm text-fg-subtle">Loading…</p>
      </Section>
    );
  }

  return (
    <>
      <Section title="Server status" subtitle="Read-only — set via .env or via Preferences above.">
        <dl className="grid grid-cols-[1fr_2fr] gap-x-4 gap-y-2 text-sm">
          <Kv k="App env" v={server.app_env} />
          <Kv k="Marginalia home" v={server.marginalia_home} mono />
          <Kv k="Database backend" v={server.db_backend} />
          <Kv k="Storage backend" v={server.storage_backend} />
          <Kv k="Worker enabled" v={server.worker_enabled ? "yes" : "no"} />
          {server.worker_batch_size != null && (
            <Kv k="Concurrent ingest tasks" v={String(server.worker_batch_size)} />
          )}
          <Kv k="Auto lifecycle" v={server.auto_lifecycle_enabled ? "enabled" : "disabled"} />
          <Kv k="Default conflict policy" v={server.default_on_conflict} />
          <Kv
            k="Agent token budget (plan/exec)"
            v={`${server.agent_plan_max_tokens.toLocaleString()} / ${server.agent_execute_max_tokens.toLocaleString()}`}
          />
          <Kv k="Agent execute turns" v={String(server.agent_execute_max_turns)} />
          <Kv k="Ingest LLM concurrency" v={String(server.llm_ingest_concurrency)} />
          <Kv
            k="Vision profile"
            v={visionConfigured(llm) ? "configured" : "not set (image OCR skipped)"}
          />
        </dl>
      </Section>

      <Section
        title="LLM profiles"
        subtitle="Per-task model overrides. Empty fields inherit the default profile."
      >
        <LlmProfileEditor data={llm} onChange={setLlm} />
      </Section>
    </>
  );
}

// ---- shared bits -----------------------------------------------------------

function Section({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-md border border-border bg-bg-subtle p-4">
      <h2 className="text-sm font-semibold">{title}</h2>
      {subtitle && <p className="mt-0.5 text-xs text-fg-subtle">{subtitle}</p>}
      <div className="mt-4">{children}</div>
    </section>
  );
}

function Row({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium">{label}</div>
        {hint && <div className="mt-0.5 text-xs text-fg-subtle">{hint}</div>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function Kv({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <>
      <dt className="text-fg-muted">{k}</dt>
      <dd className={cn("truncate", mono && "font-mono text-xs")}>{v}</dd>
    </>
  );
}

// Local-state number input that commits on blur / Enter, so the server
// only sees the final value (PUT per keystroke would spam the overlay
// with intermediate junk like 1, 12, 124, ...). Out-of-range values
// roll back to the last accepted server value.
function NumberInput({
  value,
  disabled,
  min,
  max,
  step,
  className,
  onCommit,
}: {
  value: number | undefined;
  disabled?: boolean;
  min: number;
  max: number;
  step: number;
  className?: string;
  onCommit: (v: number) => void;
}) {
  const [draft, setDraft] = useState<string>(value != null ? String(value) : "");
  useEffect(() => {
    setDraft(value != null ? String(value) : "");
  }, [value]);

  const commit = () => {
    const n = parseInt(draft, 10);
    if (!Number.isFinite(n) || n < min || n > max || n === value) {
      setDraft(value != null ? String(value) : "");
      return;
    }
    onCommit(n);
  };

  return (
    <input
      type="number"
      min={min}
      max={max}
      step={step}
      value={draft}
      disabled={disabled}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
      }}
      className={cn(
        "rounded border border-border bg-bg-base px-2 py-1 text-right font-mono text-xs disabled:opacity-50",
        className,
      )}
    />
  );
}

// vision_profile_configured from /v1/settings/server is captured at first
// fetch and goes stale after the user edits the overlay. The /v1/settings/llm
// response always reflects current overlay+env state, so derive from there.
function visionConfigured(llm: LlmSettings): boolean {
  const v = llm.profiles.vision;
  return Boolean(v?.api_key_set || v?.base_url || v?.model);
}
