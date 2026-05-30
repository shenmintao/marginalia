/** Chat workbench — drives the SSE state machine in @/api/chatStream
 *  and renders each frame type as a turn entry:
 *
 *    conversation/plan/thinking → muted progress markers (collapsible)
 *    tool_call/tool_result      → grey blocks with payload preview
 *    answer                     → markdown body + footnote citations
 *    error                      → red banner inline
 *
 *  Layout: left rail with session list, right pane with conversation +
 *  composer. Clicking a session in the rail loads its transcript via
 *  GET /v1/sessions/{id}/messages and replays the turns read-only;
 *  sending a message into a loaded session resumes it. New chat opens
 *  a fresh session lazily on first send.
 */
import { useEffect, useRef, useState, useCallback } from "react";
import { Send, Square, Sparkles } from "lucide-react";

import { sessions } from "@/api/client";
import { streamChat } from "@/api/chatStream";
import type {
  ChatEvent, ReplayedTurn, ReplayedToolCall, ThinkingEventData,
} from "@/types/api";
import { TurnView, type Turn, type Step } from "@/components/TurnView";
import { SessionList } from "@/components/SessionList";
import { useChatSession } from "@/lib/chatSession";
import { cn } from "@/lib/utils";
import { useI18n, type I18nStrings } from "@/lib/i18n";

/** Module-level in-flight SSE streams.
 *  Not tied to component lifecycle so streams survive navigation and
 *  switching between chat sessions. */
interface LiveStream {
  abort: AbortController;
  generation: number;
  turnIdx: number;
  turns: Turn[];
}

const liveStreams = new Map<string, LiveStream>();

/** Monotonic counter used to ignore stale callbacks from replaced streams. */
let streamGeneration = 0;

export function ChatPage() {
  const sessionId = useChatSession((s) => s.sessionId);
  const setSessionId = useChatSession((s) => s.setSessionId);
  const turns = useChatSession((s) => s.turns);
  const streaming = useChatSession((s) => s.streaming);
  const loading = useChatSession((s) => s.loading);
  const { setTurns, setStreaming, setLoading, reset } = useChatSession();
  const [input, setInput] = useState("");
  const [openErr, setOpenErr] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [refreshSignal, setRefreshSignal] = useState(0);
  const { t: i18n } = useI18n();

  const ensureSession = useCallback(
    async (initiatingMessage?: string): Promise<string> => {
      const sid = useChatSession.getState().sessionId;
      if (sid) return sid;
      const s = await sessions.open(initiatingMessage);
      setSessionId(s.session_id);
      setRefreshSignal((n) => n + 1);
      return s.session_id;
    },
    [setSessionId, setRefreshSignal],
  );

  useEffect(() => {
    if (!scrollRef.current) return;
    scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [turns]);

  // Mount-only: fetch transcript from the server (source of truth).
  // If streaming is active (SSE stream running in background), don't
  // overwrite — the live stream is authoritative for the in-flight turn.
  // Otherwise the server transcript is authoritative (e.g. stream
  // completed while the user was on another page).
  useEffect(() => {
    const { sessionId } = useChatSession.getState();
    if (!sessionId) return;
    const live = liveStreams.get(sessionId);
    if (live) {
      setTurns(live.turns);
      setStreaming(true);
      return;
    }
    let cancelled = false;
    setLoading(true);
    sessions.messages(sessionId)
      .then((transcript) => {
        if (cancelled) return;
        if (!useChatSession.getState().streaming) {
          setTurns(transcript.turns.map((rt) => replayedToTurn(rt, i18n)));
        }
      })
      .catch((e) => {
        if (cancelled) return;
        setOpenErr(e instanceof Error ? e.message : String(e));
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const send = useCallback(async () => {
    const q = input.trim();
    const { streaming: curStreaming } = useChatSession.getState();
    if (!q || curStreaming) return;

    let sid: string;
    let isFirstTurn = false;
    try {
      isFirstTurn = useChatSession.getState().sessionId === null;
      sid = await ensureSession(q);
    } catch (e) {
      setOpenErr(e instanceof Error ? e.message : String(e));
      return;
    }

    setOpenErr(null);
    setInput("");
    const ac = new AbortController();
    const gen = ++streamGeneration;
    const turnIdx = useChatSession.getState().turns.length;
    const live: LiveStream = {
      abort: ac,
      generation: gen,
      turnIdx,
      turns: [
        ...useChatSession.getState().turns,
        { query: q, steps: [], answer: null, error: null, done: false },
      ],
    };
    liveStreams.set(sid, live);
    setTurns(live.turns);
    setStreaming(true);

    try {
      await streamChat(sid, q, {
        signal: ac.signal,
        onEvent: (ev) => {
          const cur = liveStreams.get(sid);
          if (!cur || cur.generation !== gen) return;
          cur.turns = applyEventToTurnList(cur.turns, cur.turnIdx, ev, i18n);
          if (useChatSession.getState().sessionId === sid) {
            setTurns(cur.turns);
            setStreaming(true);
          }
          if (ev.type === "plan" && extractSessionNameFromPlan(ev.data)) {
            setRefreshSignal((n) => n + 1);
          }
        },
      });
    } catch (e) {
      if (!ac.signal.aborted) {
        const cur = liveStreams.get(sid);
        if (cur && cur.generation === gen) {
          cur.turns = updateTurn(cur.turns, cur.turnIdx, (t) => ({
            ...finishActiveThinking(t),
            error: e instanceof Error ? e.message : String(e),
            done: true,
          }));
          if (useChatSession.getState().sessionId === sid) setTurns(cur.turns);
        }
      }
    } finally {
      const cur = liveStreams.get(sid);
      if (cur && cur.generation === gen) {
        cur.turns = updateTurn(cur.turns, cur.turnIdx, (t) => ({
          ...finishActiveThinking(t),
          done: true,
        }));
        liveStreams.delete(sid);
        if (useChatSession.getState().sessionId === sid) {
          setTurns(cur.turns);
          setStreaming(false);
        }
      }
      if (cur && cur.generation === gen && isFirstTurn) setRefreshSignal((n) => n + 1);
    }
  }, [input, ensureSession, setTurns, setStreaming, i18n]);

  const stop = useCallback(() => {
    const sid = useChatSession.getState().sessionId;
    if (sid) {
      const live = liveStreams.get(sid);
      if (live) {
        live.abort.abort();
        live.turns = updateTurn(live.turns, live.turnIdx, (t) => ({
          ...finishActiveThinking(t),
          done: true,
        }));
        liveStreams.delete(sid);
        setTurns(live.turns);
      }
    }
    setStreaming(false);
  }, [setTurns, setStreaming]);

  const loadSession = useCallback(async (id: string) => {
    setLoading(true);
    setOpenErr(null);
    setSessionId(id);
    const live = liveStreams.get(id);
    if (live) {
      setTurns(live.turns);
      setStreaming(true);
      setLoading(false);
      return;
    }
    setStreaming(false);
    try {
      const transcript = await sessions.messages(id);
      if (useChatSession.getState().sessionId !== id) return;
      setTurns(transcript.turns.map((rt) => replayedToTurn(rt, i18n)));
    } catch (e) {
      if (useChatSession.getState().sessionId !== id) return;
      setOpenErr(e instanceof Error ? e.message : String(e));
    } finally {
      if (useChatSession.getState().sessionId === id) setLoading(false);
    }
  }, [setSessionId, setTurns, setStreaming, setLoading, i18n]);

  const newChat = useCallback(() => {
    const { sessionId: curSessionId } = useChatSession.getState();
    if (curSessionId) liveStreams.get(curSessionId)?.abort.abort();
    reset();
    setOpenErr(null);
    setInput("");
  }, [reset]);

  return (
    <div className="flex h-full overflow-hidden">
      <SessionList
        activeSessionId={sessionId}
        onSelect={loadSession}
        onNewChat={newChat}
        refreshSignal={refreshSignal}
      />
      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-6 py-6">
          <div className="mx-auto max-w-5xl">
            {openErr && (
              <div className="mb-4 rounded-md border border-danger/30 bg-danger/10 p-3 text-sm text-danger">
                {openErr}
              </div>
            )}
            {loading && (
              <div className="mb-4 text-sm text-fg-muted">{i18n.chat.loadingTranscript}</div>
            )}
            {!loading && turns.length === 0 && <ChatEmpty t={i18n} />}
            {turns.map((t, i) => (
              <TurnView key={i} turn={t} />
            ))}
          </div>
        </div>

        <div className="border-t border-border bg-bg-subtle px-6 py-3">
          <div className="mx-auto flex max-w-5xl items-end gap-2">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send();
                }
              }}
              placeholder={i18n.chat.inputPlaceholder}
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
                <Square size={13} fill="currentColor" /> {i18n.chat.stop}
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
                <Send size={13} /> {i18n.chat.send}
              </button>
            )}
          </div>
          <div className="mx-auto mt-1 max-w-3xl text-[11px] text-fg-subtle">
            {sessionId
              ? <>{i18n.chat.session} <span className="font-mono">{sessionId.slice(0, 8)}...</span></>
              : i18n.chat.sessionOpens}
          </div>
        </div>
      </div>
    </div>
  );
}

function ChatEmpty({ t }: { t: I18nStrings }) {
  return (
    <div className="flex h-full min-h-[40vh] flex-col items-center justify-center text-center">
      <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-accent-subtle text-accent">
        <Sparkles size={22} />
      </div>
      <h2 className="text-lg font-semibold">{t.chat.emptyTitle}</h2>
      <p className="mt-1 max-w-md text-sm text-fg-muted">
        {t.chat.emptyBody}
      </p>
      <div className="mt-4 flex flex-wrap justify-center gap-2 text-xs text-fg-subtle">
        <kbd className="rounded border border-border bg-bg-subtle px-1.5 py-0.5">{t.chat.enter}</kbd> {t.chat.enterHint}
        <kbd className="rounded border border-border bg-bg-subtle px-1.5 py-0.5">{t.chat.shiftEnter}</kbd> {t.chat.shiftEnterHint}
      </div>
    </div>
  );
}

function updateTurn(prev: Turn[], idx: number, fn: (t: Turn) => Turn): Turn[] {
  const next = [...prev];
  if (next[idx]) next[idx] = fn(next[idx]);
  return next;
}

function applyEventToTurnList(
  prev: Turn[],
  idx: number,
  ev: ChatEvent,
  t: I18nStrings,
): Turn[] {
  return updateTurn(prev, idx, (turn) => {
    switch (ev.type) {
      case "conversation":
        return {
          ...turn,
          conversationId: typeof ev.data === "string" ? ev.data : extractId(ev.data, "conversation_id"),
        };
      case "planning":
        return appendStep(turn, "planning", t.chat.planning);
      case "plan": {
        const noPlan = noPlanBody(ev.data);
        if (noPlan !== null) {
          return appendStep(turn, "plan", t.chat.noPlan, { plan: [noPlan] });
        }
        return appendStep(turn, "plan", t.chat.planReady, { plan: planLines(ev.data) });
      }
      case "thinking":
        return appendStep(
          finishActiveThinking(turn),
          "thinking",
          thinkingLabel(ev.data, t),
          { startedAtMs: Date.now() },
        );
      case "tool_call": {
        const baseTurn = finishActiveThinking(turn);
        const d = (ev.data && typeof ev.data === "object")
          ? (ev.data as {
              name?: string;
              tool_call_id?: string;
              arguments?: Record<string, unknown>;
              display?: string;
              entry_names?: Record<string, string>;
              tag_names?: Record<string, string>;
            })
          : {};
        const args = d.arguments || {};
        const label = d.display
          ? `${t.chat.calling} ${d.display}`
          : formatToolCall(d.name || t.chat.tool, args, t);
        return appendStep(
          baseTurn, "tool_call", label,
          {
            args,
            toolName: d.name,
            toolCallId: d.tool_call_id,
            entryNames: d.entry_names,
            tagNames: d.tag_names,
          },
        );
      }
      case "tool_result": {
        const d = (ev.data && typeof ev.data === "object")
          ? (ev.data as {
              ok?: boolean;
              tool_call_id?: string;
              duration_ms?: number;
              error?: string;
              preview?: string;
            })
          : {};
        return markResult(
          turn,
          d.tool_call_id,
          d.ok === false ? "failed" : "ok",
          d.duration_ms,
          d.error,
          d.preview,
        );
      }
      case "answer":
        return {
          ...finishActiveThinking(turn),
          answer: typeof ev.data === "string" ? ev.data : ev.raw,
        };
      case "done": {
        const d = (ev.data && typeof ev.data === "object")
          ? (ev.data as Turn["metrics"])
          : undefined;
        return { ...finishActiveThinking(turn), metrics: d, done: true };
      }
      case "error":
        return {
          ...finishActiveThinking(turn),
          error: typeof ev.data === "string" ? ev.data : ev.raw,
          done: true,
        };
      default:
        return turn;
    }
  });
}

function thinkingLabel(data: unknown, t: I18nStrings): string {
  if (!data || typeof data !== "object") return t.chat.thinking;
  const d = data as ThinkingEventData;
  const round = Number(d.round);
  const limit = Number(d.limit);
  if (!Number.isFinite(round) || round <= 0) return t.chat.thinking;
  if (!Number.isFinite(limit) || limit <= 0) {
    return `${t.chat.thinking} (${round})`;
  }
  return `${t.chat.thinking} (${round}/${limit})`;
}

function noPlanBody(data: unknown): string | null {
  if (typeof data !== "string") return null;
  const text = data.trim();
  if (!text.startsWith("NO_PLAN:")) return null;
  return text.slice("NO_PLAN:".length).trim();
}

function planLines(data: unknown): string[] {
  const text = typeof data === "string" ? data.trim() : "";
  return text.split("\n").map((line) => line.trim()).filter(Boolean);
}

// Map a server-replayed turn into the in-flight `Turn` shape so the
// existing TurnView renders historical sessions identically to live
// ones. Plan_text becomes a synthetic plan step; tool_calls each
// become tool_call steps with their result already marked.
function replayedToTurn(rt: ReplayedTurn, t: I18nStrings): Turn {
  const steps: Step[] = [];
  if (rt.plan_text) {
    const noPlan = noPlanBody(rt.plan_text);
    if (noPlan !== null) {
      steps.push({
        kind: "plan", label: t.chat.noPlan, plan: [noPlan],
      });
    } else {
      const lines = planLines(rt.plan_text);
      steps.push({ kind: "plan", label: t.chat.planReady, plan: lines });
    }
  }
  for (const tc of rt.tool_calls) {
    steps.push(replayedToolCallStep(tc, t));
  }
  return {
    query: rt.user_message,
    conversationId: rt.conversation_id,
    steps,
    answer: rt.agent_response,
    metrics: rt.metrics,
    error: null,
    done: rt.ended_at !== null,
  };
}

function replayedToolCallStep(tc: ReplayedToolCall, t: I18nStrings): Step {
  const name = tc.name || t.chat.tool;
  // Replay payload now carries a server-resolved `display` string, just
  // like the live SSE event does. Prefer that over the raw uuid args.
  const label = tc.display
    ? `${t.chat.calling} ${tc.display}`
    : formatToolCall(name, tc.arguments, t);
  return {
    kind: "tool_call",
    label,
    toolName: name,
    args: tc.arguments,
    result: tc.ok ? "ok" : "failed",
    durationMs: tc.duration_ms ?? undefined,
    error: tc.error ?? undefined,
    resultPreview: tc.preview ?? undefined,
  };
}

function formatToolCall(
  name: string,
  args: Record<string, unknown>,
  t: I18nStrings,
): string {
  const keys = Object.keys(args);
  if (keys.length === 0) return `${t.chat.calling} ${name}`;
  const parts: string[] = [];
  for (const k of keys) {
    const v = args[k];
    let s = typeof v === "string" ? v : JSON.stringify(v);
    if (s.length > 24) s = s.slice(0, 21) + "...";
    parts.push(`${k}=${s}`);
  }
  let inner = parts.join(", ");
  if (inner.length > 60) inner = inner.slice(0, 57) + "...";
  return `${t.chat.calling} ${name}(${inner})`;
}

function extractId(data: unknown, key: string): string | undefined {
  if (data && typeof data === "object" && key in data) {
    const v = (data as Record<string, unknown>)[key];
    return typeof v === "string" ? v : undefined;
  }
  return undefined;
}

function extractSessionNameFromPlan(data: unknown): string | null {
  const text = typeof data === "string" ? data : "";
  for (const line of text.split("\n").reverse()) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const prefix = "Session name:";
    if (!trimmed.toLowerCase().startsWith(prefix.toLowerCase())) return null;
    const name = trimmed.slice(prefix.length).trim().replace(/^["'`]+|["'`]+$/g, "");
    return name || null;
  }
  return null;
}

function appendStep(
  t: Turn, kind: Step["kind"], label: string,
  extra?: {
    args?: Record<string, unknown>;
    plan?: string[];
    toolName?: string;
    toolCallId?: string;
    entryNames?: Record<string, string>;
    tagNames?: Record<string, string>;
    startedAtMs?: number;
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
      toolCallId: extra?.toolCallId,
      entryNames: extra?.entryNames,
      tagNames: extra?.tagNames,
      result: undefined,
      startedAtMs: extra?.startedAtMs,
      durationMs: undefined,
    }],
  };
}

function finishActiveThinking(t: Turn): Turn {
  let changed = false;
  const nowMs = Date.now();
  const steps = t.steps.map((step) => {
    if (step.kind !== "thinking" || step.startedAtMs == null || step.durationMs != null) {
      return step;
    }
    changed = true;
    return { ...step, durationMs: Math.max(0, nowMs - step.startedAtMs) };
  });
  return changed ? { ...t, steps } : t;
}

function markResult(
  t: Turn,
  toolCallId: string | undefined,
  result: "ok" | "failed",
  durationMs?: number,
  error?: string,
  resultPreview?: string,
): Turn {
  if (t.steps.length === 0) return t;
  const steps = [...t.steps];
  // Pair by tool_call_id when present (parallel-safe). Fall back to the
  // last unfinished tool_call step for legacy/replay frames that don't
  // carry an id.
  let target = -1;
  if (toolCallId) {
    for (let i = steps.length - 1; i >= 0; i--) {
      if (steps[i].kind === "tool_call" && steps[i].toolCallId === toolCallId) {
        target = i;
        break;
      }
    }
  }
  if (target === -1) {
    for (let i = steps.length - 1; i >= 0; i--) {
      if (steps[i].kind === "tool_call" && !steps[i].result) {
        target = i;
        break;
      }
    }
  }
  if (target === -1) return t;
  steps[target] = { ...steps[target], result, durationMs, error, resultPreview };
  return { ...t, steps };
}
