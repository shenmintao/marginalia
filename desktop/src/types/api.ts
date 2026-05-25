/** Shared types mirroring the /v1/ JSON shapes. Keep in lockstep with
 *  src/marginalia/api/routes_*.py. When the backend changes a payload,
 *  update both — the typed client is the only thing keeping them honest.
 */

export interface Folder {
  id: string;
  parent_id: string | null;
  name: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface FolderDetail extends Folder {
  children: Folder[];
  entries: FileEntrySummary[];
}

export interface FileEntrySummary {
  id: string;
  folder_id: string | null;
  file_id: string;
  display_name: string;
  lifecycle: string;
  created_at?: string | null;
}

export interface UploadResult {
  file_id: string;
  entry_id: string;
  folder_id: string;
  display_name: string;
  deduped: boolean;
  auto_renamed: boolean;
  skipped: boolean;
}

export interface SearchResult {
  q: string;
  count: number;
  entries: SearchEntry[];
}

export interface SearchEntry {
  entry_id: string;
  display_name: string;
  folder_path?: string;
  summary?: string | null;
  score?: number;
  related_entries?: RelatedEntry[];
}

export interface RelatedEntry {
  entry_id: string;
  display_name: string;
  score: number;
  visit_count?: number;
  direct_edge_weight?: number;
}

export interface FileMetadata {
  entry_id: string;
  display_name: string;
  folder_id: string | null;
  folder_path?: string;
  size_bytes?: number;
  mime_type?: string | null;
  lifecycle: string;
  summary?: string | null;
  tags?: string[];
  related_entries?: RelatedEntry[];
  [key: string]: unknown;
}

export interface SessionInfo {
  session_id: string;
  started_at: string | null;
}

export interface SessionTotals {
  session_id: string;
  ended_at: string | null;
  end_reason: string | null;
  totals: {
    turn_count: number;
    input_tokens: number;
    output_tokens: number;
    tool_calls: number;
    llm_calls: number;
  };
}

export interface RunningCount {
  running: number;
  pending: number;
}

export interface ActiveTask {
  id: string;
  kind: string;
  label: string;
  file_id?: string | null;
  entry_id?: string | null;
  attempts: number;
  age_s: number;
}

export interface ActiveTasks {
  running: ActiveTask[];
  pending: ActiveTask[];
}

export type OnConflict = "rename" | "error" | "skip";

/** SSE event names emitted by POST /v1/chat/{session_id}.
 *  Order in a typical turn: conversation → planning → plan → thinking
 *  → (tool_call → tool_result)* → answer → done. `error` may
 *  interrupt at any time. */
export type ChatEventType =
  | "conversation"
  | "planning"
  | "plan"
  | "thinking"
  | "tool_call"
  | "tool_result"
  | "answer"
  | "error"
  | "done";

export interface ChatEvent<T = unknown> {
  type: ChatEventType;
  data: T;
  raw: string;
}

export interface ConversationEventData {
  conversation_id: string;
}

export interface PlanEventData {
  steps: string[];
}

export interface ToolCallEventData {
  name: string;
  arguments: Record<string, unknown>;
  tool_call_id?: string;
}

export interface ToolResultEventData {
  tool_call_id?: string;
  name?: string;
  result?: unknown;
  ok?: boolean;
  duration_ms?: number;
}

export interface AnswerEventData {
  text: string;
  citations?: Array<{
    marker: string;
    entry_id: string;
    display_name?: string;
  }>;
  usage?: {
    input_tokens?: number;
    output_tokens?: number;
    tool_calls?: number;
    llm_calls?: number;
    duration_ms?: number;
  };
}

export interface ApiErrorBody {
  detail?: string | Record<string, unknown>;
}
