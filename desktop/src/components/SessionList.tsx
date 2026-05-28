/** Left rail for ChatPage — lists recent sessions, lets the user click
 *  one to load its transcript into the workbench, and starts a fresh
 *  one with "+ New chat".
 *
 *  Reads via:
 *    GET /v1/sessions               (sessions.list)
 *    GET /v1/sessions/{id}/messages (sessions.messages)
 *
 *  The list refreshes when `refreshSignal` changes — ChatPage bumps
 *  it when a new session opens and again when the planner writes the
 *  final title, so the entry appears immediately and then gets renamed.
 */
import { useCallback, useEffect, useState } from "react";
import { Plus, MessageSquare, Loader2, Lock, Trash2 } from "lucide-react";

import { sessions as sessionsApi } from "@/api/client";
import type { SessionListEntry } from "@/types/api";
import { cn } from "@/lib/utils";
import { useI18n, type I18nStrings } from "@/lib/i18n";

interface Props {
  activeSessionId: string | null;
  onSelect: (sessionId: string) => void;
  onNewChat: () => void;
  refreshSignal: number;
}

export function SessionList({
  activeSessionId, onSelect, onNewChat, refreshSignal,
}: Props) {
  const [entries, setEntries] = useState<SessionListEntry[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const { t, localeTag } = useI18n();

  useEffect(() => {
    let cancelled = false;
    sessionsApi
      .list(50)
      .then((r) => { if (!cancelled) setEntries(r.sessions); })
      .catch((e) => { if (!cancelled) setErr(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [refreshSignal]);

  const handleDelete = useCallback(async (entry: SessionListEntry) => {
    const label = entry.preview ? `"${entry.preview.slice(0, 60)}"` : t.common.emptyName;
    if (!confirm(t.chat.deleteConfirm(label))) return;

    setDeletingId(entry.session_id);
    setErr(null);
    try {
      await sessionsApi.delete(entry.session_id);
      setEntries((prev) =>
        prev ? prev.filter((s) => s.session_id !== entry.session_id) : prev,
      );
      if (entry.session_id === activeSessionId) onNewChat();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setDeletingId(null);
    }
  }, [activeSessionId, onNewChat, t]);

  return (
    <aside className="flex h-full w-60 shrink-0 flex-col border-r border-border bg-bg-subtle">
      <div className="border-b border-border p-3">
        <button
          onClick={onNewChat}
          className="flex w-full items-center justify-center gap-1.5 rounded-md border border-border bg-bg-base px-3 py-2 text-sm hover:bg-bg-muted"
        >
          <Plus size={13} /> {t.chat.newChat}
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-2 py-2">
        {entries === null && !err && (
          <div className="flex items-center gap-2 px-2 py-3 text-xs text-fg-muted">
            <Loader2 size={12} className="animate-spin" /> {t.common.loading}
          </div>
        )}
        {err && (
          <div className="rounded-md border border-danger/30 bg-danger/10 p-2 text-xs text-danger">
            {err}
          </div>
        )}
        {entries && entries.length === 0 && (
          <div className="px-2 py-3 text-xs text-fg-subtle">
            {t.chat.noSessions}
          </div>
        )}
        {entries && entries.map((s) => (
          <SessionRow
            key={s.session_id}
            entry={s}
            active={s.session_id === activeSessionId}
            deleting={deletingId === s.session_id}
            onClick={() => onSelect(s.session_id)}
            onDelete={() => handleDelete(s)}
            t={t}
            localeTag={localeTag}
          />
        ))}
      </div>
    </aside>
  );
}

function SessionRow({
  entry, active, deleting, onClick, onDelete, t, localeTag,
}: {
  entry: SessionListEntry;
  active: boolean;
  deleting: boolean;
  onClick: () => void;
  onDelete: () => void;
  t: I18nStrings;
  localeTag: string;
}) {
  const closed = entry.ended_at !== null;
  const preview = entry.preview || t.common.emptyName;
  const when = entry.started_at ? formatRelative(entry.started_at, t, localeTag) : "";

  return (
    <div
      className={cn(
        "group relative mb-0.5 flex items-start gap-2 rounded-md px-2 py-1.5 text-xs transition-colors",
        active
          ? "bg-accent-subtle text-accent"
          : "text-fg-muted hover:bg-bg-muted hover:text-fg-base",
      )}
    >
      <button
        onClick={onClick}
        className="flex min-w-0 flex-1 items-start gap-2 text-left"
        title={preview}
      >
        <MessageSquare size={12} className="mt-0.5 shrink-0" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1">
            <div className="truncate font-medium">{preview}</div>
            {closed && <Lock size={9} className="shrink-0 text-fg-subtle" />}
          </div>
          <div className="mt-0.5 flex items-center gap-2 text-[10.5px] text-fg-subtle">
            <span>{when}</span>
            <span>·</span>
            <span>{t.chat.turn(entry.turn_count)}</span>
          </div>
        </div>
      </button>
      <button
        onClick={(e) => { e.stopPropagation(); onDelete(); }}
        disabled={deleting}
        title={t.chat.deleteSessionTitle}
        aria-label={t.chat.deleteSessionTitle}
        className={cn(
          "shrink-0 self-center rounded p-1 text-fg-subtle transition-opacity",
          "hover:bg-bg-base hover:text-danger",
          "opacity-0 group-hover:opacity-100 focus:opacity-100",
          deleting && "opacity-100",
        )}
      >
        {deleting
          ? <Loader2 size={11} className="animate-spin" />
          : <Trash2 size={11} />}
      </button>
    </div>
  );
}

function formatRelative(iso: string, t: I18nStrings, localeTag: string): string {
  const timestamp = new Date(iso).getTime();
  if (Number.isNaN(timestamp)) return "";
  const diffSec = (Date.now() - timestamp) / 1000;
  if (diffSec < 60) return t.time.justNow;
  if (diffSec < 3600) return t.time.minutesAgo(Math.floor(diffSec / 60));
  if (diffSec < 86400) return t.time.hoursAgo(Math.floor(diffSec / 3600));
  if (diffSec < 86400 * 7) return t.time.daysAgo(Math.floor(diffSec / 86400));
  return new Date(iso).toLocaleDateString(localeTag);
}
