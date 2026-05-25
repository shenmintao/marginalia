/** Chat workbench — drives the SSE state machine in @/api/chatStream
 *  and renders each frame type as a turn entry:
 *
 *    conversation/plan/thinking → muted progress markers (collapsible)
 *    tool_call/tool_result      → grey blocks with payload preview
 *    answer                     → markdown body + footnote citations
 *    error                      → red banner inline
 *
 *  The UI mirrors the CLI's render contract (cli/render.py) so a
 *  user moving between CLI and GUI sees the same conceptual model:
 *  planning → thinking → tools → answer.
 */
import { useEffect, useRef, useState, useCallback } from "react";
import { Send, Square, Sparkles } from "lucide-react";

import { sessions } from "@/api/client";
import { streamChat } from "@/api/chatStream";
import type { ChatEvent } from "@/types/api";
import { TurnView, type Turn, type StepKind } from "@/components/TurnView";
import { cn } from "@/lib/utils";

export function ChatPage() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [openErr, setOpenErr] = useState<string | null>(null);

  const ensureSession = useCallback(async (): Promise<string> => {
    if (sessionId) return sessionId;
    const s = await sessions.open();
    setSessionId(s.session_id);
    return s.session_id;
  }, [sessionId]);

  useEffect(() => {
    if (!scrollRef.current) return;
    scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [turns]);

  const send = useCallback(async () => {
    const q = input.trim();
    if (!q || streaming) return;

    let sid: string;
    try {
      sid = await ensureSession();
    } catch (e) {
      setOpenErr(e instanceof Error ? e.message : String(e));
      return;
    }

    setOpenErr(null);
    setInput("");
    const turnIdx = turns.length;
    setTurns((prev) => [...prev, { query: q, steps: [], answer: null, error: null, done: false }]);
    setStreaming(true);

    const ac = new AbortController();
    abortRef.current = ac;

    try {
      await streamChat(sid, q, {
        signal: ac.signal,
        onEvent: (ev) => applyEvent(setTurns, turnIdx, ev),
      });
    } catch (e) {
      if (!ac.signal.aborted) {
        setTurns((prev) => updateTurn(prev, turnIdx, (t) => ({
          ...t, error: e instanceof Error ? e.message : String(e), done: true,
        })));
      }
    } finally {
      abortRef.current = null;
      setStreaming(false);
      setTurns((prev) => updateTurn(prev, turnIdx, (t) => ({ ...t, done: true })));
    }
  }, [input, streaming, ensureSession, turns.length]);

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  return (
    <div className="flex h-full flex-col">
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-6 py-6">
        <div className="mx-auto max-w-3xl">
          {openErr && (
            <div className="mb-4 rounded-md border border-danger/30 bg-danger/10 p-3 text-sm text-danger">
              {openErr}
            </div>
          )}
          {turns.length === 0 && <ChatEmpty />}
          {turns.map((t, i) => (
            <TurnView key={i} turn={t} />
          ))}
        </div>
      </div>

      <div className="border-t border-border bg-bg-subtle px-6 py-3">
        <div className="mx-auto flex max-w-3xl items-end gap-2">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            placeholder="Ask the librarian…  (Enter to send · Shift+Enter for newline)"
            rows={1}
            className={cn(
              "flex-1 resize-none rounded-md border border-border bg-bg-base px-3 py-2 text-sm",
              "outline-none transition-colors focus:border-accent",
              "placeholder:text-fg-subtle",
              "max-h-40",
            )}
          />
          {streaming ? (
            <button
              onClick={stop}
              className="flex h-9 items-center gap-1 rounded-md border border-border bg-bg-elevated px-3 text-sm hover:bg-bg-muted"
            >
              <Square size={13} fill="currentColor" /> Stop
            </button>
          ) : (
            <button
              onClick={send}
              disabled={!input.trim()}
              className={cn(
                "flex h-9 items-center gap-1.5 rounded-md px-3 text-sm font-medium transition-colors",
                input.trim()
                  ? "bg-accent text-accent-fg hover:opacity-90"
                  : "cursor-not-allowed bg-bg-muted text-fg-subtle",
              )}
            >
              <Send size={13} /> Send
            </button>
          )}
        </div>
        <div className="mx-auto mt-1 max-w-3xl text-[11px] text-fg-subtle">
          {sessionId
            ? <>session <span className="font-mono">{sessionId.slice(0, 8)}…</span></>
            : "session opens on first message"}
        </div>
      </div>
    </div>
  );
}

function ChatEmpty() {
  return (
    <div className="flex h-full min-h-[40vh] flex-col items-center justify-center text-center">
      <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-accent-subtle text-accent">
        <Sparkles size={22} />
      </div>
      <h2 className="text-lg font-semibold">Ask the librarian</h2>
      <p className="mt-1 max-w-md text-sm text-fg-muted">
        The investigator agent reads its journal, gathers context from your
        library, and answers with citations.
      </p>
      <div className="mt-4 flex flex-wrap justify-center gap-2 text-xs text-fg-subtle">
        <kbd className="rounded border border-border bg-bg-subtle px-1.5 py-0.5">Enter</kbd> send
        <kbd className="rounded border border-border bg-bg-subtle px-1.5 py-0.5">Shift+Enter</kbd> newline
      </div>
    </div>
  );
}

function updateTurn(prev: Turn[], idx: number, fn: (t: Turn) => Turn): Turn[] {
  const next = [...prev];
  if (next[idx]) next[idx] = fn(next[idx]);
  return next;
}

function applyEvent(
  setTurns: React.Dispatch<React.SetStateAction<Turn[]>>,
  idx: number,
  ev: ChatEvent,
) {
  setTurns((prev) => updateTurn(prev, idx, (t) => {
    switch (ev.type) {
      case "conversation":
        return {
          ...t,
          conversationId: typeof ev.data === "string" ? ev.data : extractId(ev.data, "conversation_id"),
        };
      case "planning":
        return appendStep(t, "planning", "planning the investigation…");
      case "plan": {
        const text = typeof ev.data === "string" ? ev.data : "";
        const steps = text.trim().split("\n").filter(Boolean);
        return appendStep(t, "plan", "plan ready", { plan: steps });
      }
      case "thinking":
        return appendStep(t, "thinking", "thinking…");
      case "tool_call": {
        // Server emits: {"name": ..., "arguments": {...}, "display": "...",
        //               "entry_names": {"<uuid>": "<name>", ...},
        //               "tag_names":   {"<uuid>": "<name>", ...}}
        // `display` is a kb-lite-style one-liner (e.g. "read_files paper.pdf
        // pages 5-7"); fall back to a local formatter for older backends.
        const d = (ev.data && typeof ev.data === "object")
          ? (ev.data as {
              name?: string;
              arguments?: Record<string, unknown>;
              display?: string;
              entry_names?: Record<string, string>;
              tag_names?: Record<string, string>;
            })
          : {};
        const args = d.arguments || {};
        const label = d.display
          ? `calling ${d.display}`
          : formatToolCall(d.name || "tool", args);
        return appendStep(
          t, "tool_call", label,
          {
            args,
            toolName: d.name,
            entryNames: d.entry_names,
            tagNames: d.tag_names,
          },
        );
      }
      case "tool_result": {
        const d = (ev.data && typeof ev.data === "object")
          ? (ev.data as {
              ok?: boolean;
              duration_ms?: number;
              error?: string;
              preview?: string;
            })
          : {};
        return markLastResult(
          t,
          d.ok === false ? "failed" : "ok",
          d.duration_ms,
          d.error,
          d.preview,
        );
      }
      case "answer":
        // Server emits the answer as a plain string (not an object).
        return { ...t, answer: typeof ev.data === "string" ? ev.data : ev.raw };
      case "done": {
        // {tokens_in, tokens_out, cache_read, tool_calls, llm_calls,
        //  duration_ms, truncated}
        const d = (ev.data && typeof ev.data === "object")
          ? (ev.data as Turn["metrics"])
          : undefined;
        return { ...t, metrics: d, done: true };
      }
      case "error":
        return { ...t, error: typeof ev.data === "string" ? ev.data : ev.raw, done: true };
      default:
        return t;
    }
  }));
}

// Mirrors cli/commands.py:_format_tool_call so CLI and GUI render the
// same call signature: `search_metadata(q="raft", limit=10)` instead of
// a raw JSON dump. Truncates long values + the overall arg list so the
// row stays one line in the trace.
function formatToolCall(name: string, args: Record<string, unknown>): string {
  const keys = Object.keys(args);
  if (keys.length === 0) return `calling ${name}`;
  const parts: string[] = [];
  for (const k of keys) {
    const v = args[k];
    let s = typeof v === "string" ? v : JSON.stringify(v);
    if (s.length > 24) s = s.slice(0, 21) + "...";
    parts.push(`${k}=${s}`);
  }
  let inner = parts.join(", ");
  if (inner.length > 60) inner = inner.slice(0, 57) + "...";
  return `calling ${name}(${inner})`;
}

function extractId(data: unknown, key: string): string | undefined {
  if (data && typeof data === "object" && key in data) {
    const v = (data as Record<string, unknown>)[key];
    return typeof v === "string" ? v : undefined;
  }
  return undefined;
}

function appendStep(
  t: Turn, kind: StepKind, label: string,
  extra?: {
    args?: Record<string, unknown>;
    plan?: string[];
    toolName?: string;
    entryNames?: Record<string, string>;
    tagNames?: Record<string, string>;
  },
): Turn {
  return {
    ...t,
    steps: [...t.steps, {
      kind,
      label,
      args: extra?.args,
      plan: extra?.plan,
      toolName: extra?.toolName,
      entryNames: extra?.entryNames,
      tagNames: extra?.tagNames,
      result: undefined,
      durationMs: undefined,
    }],
  };
}

function markLastResult(
  t: Turn,
  result: "ok" | "failed",
  durationMs?: number,
  error?: string,
  resultPreview?: string,
): Turn {
  if (t.steps.length === 0) return t;
  const steps = [...t.steps];
  for (let i = steps.length - 1; i >= 0; i--) {
    if (steps[i].kind === "tool_call" && !steps[i].result) {
      steps[i] = { ...steps[i], result, durationMs, error, resultPreview };
      break;
    }
  }
  return { ...t, steps };
}
