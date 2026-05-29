/** Shared types mirroring the /v1/ JSON shapes. Keep in lockstep with
 *  src/marginalia/api/routes_*.py. When the backend changes a payload,
 *  update both — the typed client is the only thing keeping them honest.
 */

export type IngestStatus = "pending" | "processing" | "done" | "failed";

export interface FolderIngestSummary {
  total: number;
  pending: number;
  processing: number;
  done: number;
  failed: number;
  incomplete: number;
  status: IngestStatus | null;
}

export interface Folder {
  id: string;
  parent_id: string | null;
  name: string;
  created_at: string | null;
  updated_at: string | null;
  /** Recursive file ingest summary for this folder subtree. Present on
   *  folder-listing responses so collapsed rows can show unfinished work. */
  ingest_summary?: FolderIngestSummary | null;
}

export interface FolderDetail extends Folder {
  children: Folder[];
  entries: FileEntrySummary[];
}

export interface FolderListing {
  folders: Folder[];
  entries: FileEntrySummary[];
}

export interface FileEntrySummary {
  id: string;
  folder_id: string | null;
  file_id: string;
  display_name: string;
  lifecycle: string;
  /** File-side ingest state — pending | processing | done | failed.
   *  Sourced from the joined `files.ingest_status` so the row can
   *  paint a "failed" badge without a second round-trip. */
  ingest_status?: IngestStatus | null;
  ingest_error?: string | null;
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
  tags?: { name: string; facet?: string | null }[];
  extra?: string | null;
  related_entries?: RelatedEntry[];
  [key: string]: unknown;
}

/** Folder ancestor chain (root → leaf) for an entry, returned by
 *  GET /v1/file-entries/{id}/path. The Library tree consumes this
 *  to expand each ancestor in turn before selecting the file. */
export interface EntryPath {
  entry_id: string;
  display_name: string;
  folder_id: string | null;
  ancestors: { id: string; name: string }[];
}

export interface SessionInfo {
  session_id: string;
  started_at: string | null;
}

export interface SessionListEntry {
  session_id: string;
  started_at: string | null;
  ended_at: string | null;
  end_reason: string | null;
  preview: string;
  turn_count: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_tool_calls: number;
}

export interface SessionList {
  sessions: SessionListEntry[];
  limit: number;
  offset: number;
}

export interface ReplayedToolCall {
  name: string | null;
  arguments: Record<string, unknown>;
  /** Server-resolved one-line summary, mirrors the live SSE
   *  tool_call event. Names referenced as ids (entry/tag/folder/
   *  catalog) come back resolved so the GUI prints a readable label
   *  instead of a uuid. Optional for forward-compatibility with
   *  older transcripts. */
  display?: string | null;
  ok: boolean;
  error: string | null;
  duration_ms: number | null;
  /** One-line summary of the tool result, mirrors what the live SSE
   *  `tool_result` event carries in its `preview` field. Null when the
   *  call ran but produced no result body (legacy rows). */
  preview?: string | null;
}

export interface ReplayedTurn {
  conversation_id: string;
  turn_index: number;
  started_at: string | null;
  ended_at: string | null;
  user_message: string;
  agent_response: string | null;
  plan_text: string | null;
  tool_calls: ReplayedToolCall[];
  metrics: {
    tokens_in: number;
    tokens_out: number;
    cache_read: number;
    tool_calls: number;
    llm_calls: number;
    duration_ms: number;
  };
}

export interface SessionTranscript {
  session_id: string;
  started_at: string | null;
  ended_at: string | null;
  end_reason: string | null;
  turns: ReplayedTurn[];
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

export interface RecentTask {
  id: string;
  kind: string;
  status: "done" | "dead";
  label: string;
  file_id?: string | null;
  entry_id?: string | null;
  started_at: string | null;
  finished_at: string | null;
  last_error: string | null;
  duration_ms: number | null;
  tokens_in: number | null;
  tokens_out: number | null;
  cache_read: number | null;
  llm_calls: number | null;
}

export interface RecentTasks {
  items: RecentTask[];
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

// ---- settings -------------------------------------------------------------

export interface ServerSettings {
  app_env: string;
  marginalia_home: string;
  db_backend: string;
  storage_backend: string;
  worker_enabled: boolean;
  worker_batch_size: number;
  auto_lifecycle_enabled: boolean;
  default_on_conflict: string;
  agent_plan_max_tokens: number;
  agent_execute_max_tokens: number;
  agent_execute_max_turns: number;
  agent_final_answer_continue_turns: number;
  agent_final_answer_max_chars: number;
  llm_ingest_concurrency: number;
  vision_profile_configured: boolean;
}

export type LlmProfileName =
  | "default" | "chat" | "reflect" | "ingest" | "vision";

export interface LlmProfileResolved {
  provider: string | null;
  api_key: string | null;
  api_key_set: boolean;
  base_url: string | null;
  model: string | null;
}

export interface LlmSettings {
  profiles: Record<LlmProfileName, LlmProfileResolved>;
  overlay: Record<string, string | number | null>;
  defaults: {
    provider: string;
    model: string;
    base_url: string | null;
    api_key: string | null;
    api_key_set: boolean;
  };
}
