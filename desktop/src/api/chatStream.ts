/** SSE consumer for POST /v1/chat/{session_id}.
 *
 *  EventSource doesn't support POST bodies, so we use fetch() with a
 *  manual reader and parse SSE frames ourselves. The CLI does the same
 *  thing in Python (cli/client.py); this is the JS counterpart.
 *
 *  Frame format the server emits (sse_starlette default):
 *      event: <type>\n
 *      data: <payload>\n
 *      \n
 *  Payloads are typically JSON, sometimes plain strings (errors). We
 *  pass each frame through onEvent with the raw text so callers can
 *  decide how to decode per type.
 */
import type { ChatEvent, ChatEventType } from "@/types/api";
import { getBaseUrl } from "@/api/client";

export interface ChatStreamOptions {
  signal?: AbortSignal;
  onEvent: (ev: ChatEvent) => void;
  onError?: (err: unknown) => void;
}

export async function streamChat(
  sessionId: string,
  query: string,
  opts: ChatStreamOptions,
): Promise<void> {
  const url = `${getBaseUrl()}/v1/chat/${encodeURIComponent(sessionId)}`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify({ query }),
    signal: opts.signal,
  });
  if (!res.ok || !res.body) {
    let detail = `${res.status}`;
    try { detail = (await res.text()) || detail; } catch { /* ignore */ }
    throw new Error(`chat stream failed: ${detail}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // Split on the SSE frame delimiter (blank line). \r\n\r\n covers
    // proxies that normalise newlines.
    let idx: number;
    while (
      (idx = indexOfDelim(buffer)) !== -1
    ) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx).replace(/^(\r?\n){2}/, "");
      const ev = parseFrame(frame);
      if (ev) opts.onEvent(ev);
    }
  }

  // Flush any trailing frame (rare; servers usually end with \n\n).
  if (buffer.trim()) {
    const ev = parseFrame(buffer);
    if (ev) opts.onEvent(ev);
  }
}

function indexOfDelim(s: string): number {
  const a = s.indexOf("\n\n");
  const b = s.indexOf("\r\n\r\n");
  if (a === -1) return b;
  if (b === -1) return a;
  return Math.min(a, b);
}

const KNOWN_EVENTS: ReadonlySet<ChatEventType> = new Set([
  "conversation",
  "planning",
  "plan",
  "thinking",
  "tool_call",
  "tool_result",
  "answer",
  "error",
  "done",
]);

function parseFrame(frame: string): ChatEvent | null {
  // "event:" / "data:" / ":" comments. Multiple `data:` lines are
  // concatenated with newlines per the SSE spec.
  let evType = "message";
  const dataLines: string[] = [];
  for (const line of frame.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    if (colon === -1) continue;
    const field = line.slice(0, colon);
    const value = line.slice(colon + 1).replace(/^ /, "");
    if (field === "event") evType = value;
    else if (field === "data") dataLines.push(value);
  }
  if (dataLines.length === 0 && evType === "message") return null;
  const raw = dataLines.join("\n");
  const type = (KNOWN_EVENTS.has(evType as ChatEventType) ? evType : "message") as ChatEventType;
  return { type, data: tryJson(raw), raw };
}

function tryJson(s: string): unknown {
  if (!s) return s;
  const t = s.trim();
  if (!t) return s;
  if (t[0] !== "{" && t[0] !== "[" && t[0] !== '"') return s;
  try { return JSON.parse(s); } catch { return s; }
}
