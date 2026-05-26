/** Left rail for ChatPage — lists recent sessions, lets the user click
 *  one to load its transcript into the workbench, and starts a fresh
 *  one with "+ New chat".
 *
 *  Reads via:
 *    GET /v1/sessions               (sessions.list)
 *    GET /v1/sessions/{id}/messages (sessions.messages)
 *
 *  The list refreshes when `refreshSignal` changes — ChatPage bumps
 *  it after the first turn of a new session lands so the entry shows
 *  up without a full reload.
 */
import { useEffect, useState } from "react";
import { Plus, MessageSquare, Loader2, Lock } from "lucide-react";

import { sessions as sessionsApi } from "@/api/client";
import type { SessionListEntry } from "@/types/api";
import { cn } from "@/lib/utils";

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

  useEffect(() => {
    let cancelled = false;
    sessionsApi
      .list(50)
      .then((r) => { if (!cancelled) setEntries(r.sessions); })
      .catch((e) => { if (!cancelled) setErr(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [refreshSignal]);

  return (
    <aside className="flex h-full w-60 shrink-0 flex-col border-r border-border bg-bg-subtle">
      <div className="border-b border-border p-3">
        <button
          onClick={onNewChat}
          className="flex w-full items-center justify-center gap-1.5 rounded-md border border-border bg-bg-base px-3 py-2 text-sm hover:bg-bg-muted"
        >
          <Plus size={13} /> New chat
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-2 py-2">
        {entries === null && !err && (
          <div className="flex items-center gap-2 px-2 py-3 text-xs text-fg-muted">
            <Loader2 size={12} className="animate-spin" /> loading…
          </div>
        )}
        {err && (
          <div className="rounded-md border border-danger/30 bg-danger/10 p-2 text-xs text-danger">
            {err}
          </div>
        )}
        {entries && entries.length === 0 && (
          <div className="px-2 py-3 text-xs text-fg-subtle">
            No sessions yet. Send a message to start one.
          </div>
        )}
        {entries && entries.map((s) => (
          <SessionRow
            key={s.session_id}
            entry={s}
            active={s.session_id === activeSessionId}
            onClick={() => onSelect(s.session_id)}
          />
        ))}
      </div>
    </aside>
  );
}

function SessionRow({
  entry, active, onClick,
}: { entry: SessionListEntry; active: boolean; onClick: () => void }) {
  const closed = entry.ended_at !== null;
  const preview = entry.preview || "(empty session)";
  const when = entry.started_at ? formatRelative(entry.started_at) : "";

  return (
    <button
      onClick={onClick}
      className={cn(
        "group mb-0.5 flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-xs transition-colors",
        active
          ? "bg-accent-subtle text-accent"
          : "text-fg-muted hover:bg-bg-muted hover:text-fg-base",
      )}
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
          <span>{entry.turn_count} turn{entry.turn_count === 1 ? "" : "s"}</span>
        </div>
      </div>
    </button>
  );
}

function formatRelative(iso: string): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const diffSec = (Date.now() - t) / 1000;
  if (diffSec < 60) return "just now";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
  if (diffSec < 86400 * 7) return `${Math.floor(diffSec / 86400)}d ago`;
  return new Date(iso).toLocaleDateString();
}
