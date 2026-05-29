/** Activity popover anchored to the StatusBar busy indicator.
 *
 *  Top section: live running + pending tasks (kind · label · age).
 *  Bottom section: recently-finished tasks with per-run usage.
 *
 *  Polls /v1/tasks/active and /v1/tasks/recent on the same cadence as
 *  the StatusBar itself; opens above the footer so it doesn't obscure
 *  the main content area.
 */
import { useEffect, useState } from "react";
import { CheckCircle2, XCircle, Loader2 } from "lucide-react";

import { tasks } from "@/api/client";
import type { ActiveTask, RecentTask } from "@/types/api";
import { cn } from "@/lib/utils";
import { useI18n, type I18nStrings } from "@/lib/i18n";

interface Props {
  open: boolean;
  pollMs: number;
}

export function ActivityPopover({ open, pollMs }: Props) {
  const [active, setActive] = useState<{ running: ActiveTask[]; pending: ActiveTask[] }>(
    { running: [], pending: [] },
  );
  const [recent, setRecent] = useState<RecentTask[]>([]);
  const [loading, setLoading] = useState(false);
  const { t } = useI18n();

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    async function tick() {
      setLoading(true);
      try {
        const [a, r] = await Promise.all([tasks.active(), tasks.recent(20)]);
        if (cancelled) return;
        setActive(a);
        setRecent(r.items);
      } catch {
        /* keep last value */
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    tick();
    const id = window.setInterval(tick, pollMs);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [open, pollMs]);

  if (!open) return null;

  const totals = recent.reduce(
    (acc, r) => {
      acc.tokens_in += r.tokens_in ?? 0;
      acc.tokens_out += r.tokens_out ?? 0;
      acc.cache_read += r.cache_read ?? 0;
      acc.llm_calls += r.llm_calls ?? 0;
      acc.duration_ms += r.duration_ms ?? 0;
      return acc;
    },
    { tokens_in: 0, tokens_out: 0, cache_read: 0, llm_calls: 0, duration_ms: 0 },
  );
  const cachePct = totals.tokens_in > 0
    ? Math.round((totals.cache_read / totals.tokens_in) * 100)
    : 0;
  const recentSummary = t.activity.recentSummary(
    recent.length,
    fmtDuration(totals.duration_ms),
    fmtTokens(totals.tokens_in),
    fmtTokens(totals.tokens_out),
    cachePct,
    totals.llm_calls,
  );

  return (
    <div
      className={cn(
        "absolute bottom-7 right-3 z-50 max-h-[60vh]",
        "w-[500px] max-w-[calc(100vw-24px)]",
        "overflow-hidden rounded-md border border-border bg-bg-elevated shadow-lg",
        "flex flex-col text-xs",
      )}
    >
      <div className="border-b border-border bg-bg-subtle px-3 py-2">
        <div className="flex items-center justify-between">
          <span className="font-medium text-fg-base">{t.activity.title}</span>
          {loading && <Loader2 size={11} className="animate-spin text-fg-subtle" />}
        </div>
        {recent.length > 0 && (
          <div
            className="mt-1 truncate whitespace-nowrap font-mono text-[10.5px] text-fg-subtle"
            title={recentSummary}
          >
            {recentSummary}
          </div>
        )}
      </div>

      <div className="flex-1 overflow-y-auto">
        {(active.running.length > 0 || active.pending.length > 0) && (
          <Section title={t.activity.inFlight}>
            {active.running.map((t) => (
              <ActiveRow key={t.id} task={t} state="running" />
            ))}
            {active.pending.map((t) => (
              <ActiveRow key={t.id} task={t} state="pending" />
            ))}
          </Section>
        )}

        <Section title={t.activity.recent}>
          {recent.length === 0 ? (
            <div className="px-3 py-3 text-fg-subtle">{t.activity.noTasks}</div>
          ) : (
            recent.map((t) => <RecentRow key={t.id} task={t} />)
          )}
        </Section>
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="sticky top-0 border-b border-border bg-bg-subtle px-3 py-1 font-mono text-[10px] uppercase tracking-wide text-fg-subtle">
        {title}
      </div>
      {children}
    </div>
  );
}

function ActiveRow({ task, state }: { task: ActiveTask; state: "running" | "pending" }) {
  const { t } = useI18n();
  return (
    <div className="flex items-center gap-2 border-b border-border px-3 py-1.5">
      {state === "running"
        ? <Loader2 size={11} className="shrink-0 animate-spin text-accent" />
        : <span className="block h-1.5 w-1.5 shrink-0 rounded-full bg-fg-subtle" />}
      <span className="shrink-0 font-medium text-fg-base" title={task.kind}>
        {taskKindLabel(task.kind, t)}
      </span>
      <span className="truncate text-fg-muted">{task.label}</span>
      <span className="ml-auto shrink-0 font-mono text-fg-subtle">{fmtAge(task.age_s)}</span>
    </div>
  );
}

function RecentRow({ task }: { task: RecentTask }) {
  const { t } = useI18n();
  const ok = task.status === "done";
  const hasUsage = (task.tokens_in ?? 0) > 0 || (task.tokens_out ?? 0) > 0;
  const cachePct = task.tokens_in
    ? Math.round(((task.cache_read ?? 0) / task.tokens_in) * 100)
    : 0;
  const usageLine = [
    t.chat.tokens(fmtTokens(task.tokens_in ?? 0), fmtTokens(task.tokens_out ?? 0)),
    task.tokens_in ? t.activity.cache(cachePct) : null,
    task.llm_calls ? t.activity.llm(task.llm_calls) : null,
  ].filter(Boolean).join(" · ");
  return (
    <div className="border-b border-border px-3 py-1.5">
      <div className="flex items-center gap-2">
        {ok
          ? <CheckCircle2 size={11} className="shrink-0 text-accent" />
          : <XCircle size={11} className="shrink-0 text-danger" />}
        <span className="shrink-0 font-medium text-fg-base" title={task.kind}>
          {taskKindLabel(task.kind, t)}
        </span>
        <span className="min-w-0 flex-1 truncate text-fg-muted">{task.label}</span>
        {task.duration_ms != null && (
          <span className="shrink-0 font-mono text-fg-subtle">
            {fmtDuration(task.duration_ms)}
          </span>
        )}
      </div>
      {hasUsage && (
        <div
          className="ml-[19px] mt-0.5 truncate whitespace-nowrap font-mono text-[10.5px] text-fg-subtle"
          title={usageLine}
        >
          {usageLine}
        </div>
      )}
      {!ok && task.last_error && (
        <div
          className="ml-[19px] mt-0.5 truncate text-[10.5px] text-danger"
          title={task.last_error}
        >
          {task.last_error}
        </div>
      )}
    </div>
  );
}

function taskKindLabel(kind: string, t: I18nStrings): string {
  return (t.activity.taskKind as Record<string, string>)[kind] ?? kind;
}

function fmtTokens(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

function fmtDuration(ms: number): string {
  const s = ms / 1000;
  if (s < 1) return `${Math.round(ms)}ms`;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const rs = Math.round(s - m * 60);
  if (m < 60) return `${m}m ${rs}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function fmtAge(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h`;
}
