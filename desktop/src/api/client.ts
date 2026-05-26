/** Typed fetch wrapper for the /v1/ API.
 *
 *  - The dev server proxies /v1/* to the Marginalia backend, so we use
 *    relative paths and the browser stays same-origin (no preflight).
 *  - In Tauri / production, baseUrl can be overridden via setBaseUrl()
 *    or VITE_API_BASE.
 *  - All errors surface as ApiError with status + parsed body so UI
 *    can show conflict details (e.g. display_name_conflict on upload).
 */
import type {
  ActiveTasks,
  ApiErrorBody,
  FileMetadata,
  Folder,
  FolderDetail,
  LlmSettings,
  OnConflict,
  RunningCount,
  SearchResult,
  ServerSettings,
  SessionInfo,
  SessionList,
  SessionTotals,
  SessionTranscript,
  UploadResult,
} from "@/types/api";

let _base = import.meta.env.VITE_API_BASE || "";

export function setBaseUrl(url: string) {
  _base = url.replace(/\/$/, "");
}
export function getBaseUrl() {
  return _base;
}

export class ApiError extends Error {
  status: number;
  body: ApiErrorBody | string | null;
  constructor(status: number, body: ApiErrorBody | string | null, msg: string) {
    super(msg);
    this.status = status;
    this.body = body;
  }
}

async function _request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const res = await fetch(_base + path, {
    ...init,
    headers: {
      ...(init.body && !(init.body instanceof FormData)
        ? { "Content-Type": "application/json" }
        : {}),
      ...(init.headers || {}),
    },
  });
  if (!res.ok) {
    let body: ApiErrorBody | string | null = null;
    const ct = res.headers.get("content-type") || "";
    try {
      body = ct.includes("application/json") ? await res.json() : await res.text();
    } catch {
      body = null;
    }
    const detail =
      typeof body === "object" && body && "detail" in body
        ? typeof body.detail === "string"
          ? body.detail
          : JSON.stringify(body.detail)
        : typeof body === "string"
          ? body
          : res.statusText;
    throw new ApiError(res.status, body, `${res.status} ${detail}`);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// ---- folders --------------------------------------------------------------

export const folders = {
  list: (parentId?: string | null) =>
    _request<{ folders: Folder[] }>(
      `/v1/folders${parentId ? `?parent_id=${encodeURIComponent(parentId)}` : ""}`,
    ),
  get: (id: string) => _request<FolderDetail>(`/v1/folders/${encodeURIComponent(id)}`),
  create: (name: string, parentId: string | null) =>
    _request<Folder>(`/v1/folders`, {
      method: "POST",
      body: JSON.stringify({ parent_id: parentId, name }),
    }),
  rename: (id: string, name: string) =>
    _request<Folder>(`/v1/folders/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ name }),
    }),
  move: (id: string, parentId: string | null) =>
    _request<Folder>(`/v1/folders/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({
        update_parent: true,
        parent_id: parentId === null ? "root" : parentId,
      }),
    }),
  delete: (id: string) =>
    _request<{ folder_id: string; deleted_at: string | null }>(
      `/v1/folders/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    ),
  downloadUrl: (id: string) =>
    `${_base}/v1/folders/${encodeURIComponent(id)}/download`,
};

// ---- file entries ---------------------------------------------------------

export const fileEntries = {
  metadata: (id: string) =>
    _request<FileMetadata>(`/v1/file-entries/${encodeURIComponent(id)}/metadata`),
  rename: (id: string, displayName: string, onConflict: OnConflict = "rename") =>
    _request(`/v1/file-entries/${encodeURIComponent(id)}/name`, {
      method: "PATCH",
      body: JSON.stringify({ display_name: displayName, on_conflict: onConflict }),
    }),
  move: (id: string, folderId: string, onConflict: OnConflict = "rename") =>
    _request(`/v1/file-entries/${encodeURIComponent(id)}/folder`, {
      method: "PATCH",
      body: JSON.stringify({ folder_id: folderId, on_conflict: onConflict }),
    }),
  setLifecycle: (id: string, lifecycle: string) =>
    _request(`/v1/file-entries/${encodeURIComponent(id)}/lifecycle`, {
      method: "PATCH",
      body: JSON.stringify({ lifecycle }),
    }),
  delete: (id: string, purgeAfterSeconds?: number) => {
    const q =
      purgeAfterSeconds !== undefined
        ? `?purge_after_seconds=${purgeAfterSeconds}`
        : "";
    return _request(`/v1/file-entries/${encodeURIComponent(id)}${q}`, {
      method: "DELETE",
    });
  },
  contentUrl: (id: string) =>
    `${_base}/v1/file-entries/${encodeURIComponent(id)}/content`,
  downloadUrl: (id: string) =>
    `${_base}/v1/file-entries/${encodeURIComponent(id)}/download`,
};

// ---- upload ---------------------------------------------------------------

export const uploads = {
  upload: async (
    file: File,
    dest: { remotePath: string } | { folderId: string },
    opts: {
      displayName?: string;
      onConflict?: OnConflict;
      onProgress?: (loaded: number, total: number) => void;
    } = {},
  ): Promise<UploadResult> => {
    const fd = new FormData();
    fd.append("file", file);
    const params = new URLSearchParams();
    if ("remotePath" in dest) params.set("remote_path", dest.remotePath);
    else params.set("folder_id", dest.folderId);
    if (opts.displayName) params.set("display_name", opts.displayName);
    if (opts.onConflict) params.set("on_conflict", opts.onConflict);

    // XHR for progress events; fetch doesn't expose upload progress yet.
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${_base}/v1/upload?${params.toString()}`);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && opts.onProgress) {
          opts.onProgress(e.loaded, e.total);
        }
      };
      xhr.onload = () => {
        const ct = xhr.getResponseHeader("content-type") || "";
        const body = ct.includes("application/json")
          ? safeParseJson(xhr.responseText)
          : xhr.responseText;
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(body as UploadResult);
        } else {
          const detail =
            (body && typeof body === "object" && "detail" in body
              ? JSON.stringify((body as ApiErrorBody).detail)
              : String(body)) || xhr.statusText;
          reject(new ApiError(xhr.status, body as ApiErrorBody, `${xhr.status} ${detail}`));
        }
      };
      xhr.onerror = () => reject(new ApiError(0, null, "network error"));
      xhr.send(fd);
    });
  },
};

function safeParseJson(s: string): unknown {
  try { return JSON.parse(s); } catch { return s; }
}

// ---- search / discover ----------------------------------------------------

export const search = {
  query: (q: string, limit = 25) =>
    _request<SearchResult>(
      `/v1/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),
  discover: (entryId: string, topK = 8) =>
    _request<{
      seed_entry_id: string;
      count: number;
      results: Array<{
        entry_id: string;
        display_name: string;
        score: number;
        visit_count: number;
        direct_edge_weight: number;
      }>;
    }>(`/v1/discover/${encodeURIComponent(entryId)}?top_k=${topK}`),
};

// ---- sessions / chat ------------------------------------------------------

export const sessions = {
  open: (initiatingMessage?: string) =>
    _request<SessionInfo>(`/v1/sessions`, {
      method: "POST",
      body: JSON.stringify({ initiating_user_message: initiatingMessage || null }),
    }),
  close: (id: string) =>
    _request<SessionTotals>(`/v1/sessions/${encodeURIComponent(id)}/close`, {
      method: "POST",
    }),
  list: (limit = 50, offset = 0) =>
    _request<SessionList>(
      `/v1/sessions?limit=${limit}&offset=${offset}`,
    ),
  messages: (id: string) =>
    _request<SessionTranscript>(
      `/v1/sessions/${encodeURIComponent(id)}/messages`,
    ),
};

// ---- tasks ----------------------------------------------------------------

export const tasks = {
  runningCount: () => _request<RunningCount>(`/v1/tasks/running-count`),
  active: () => _request<ActiveTasks>(`/v1/tasks/active`),
};

// ---- exports --------------------------------------------------------------

export const exports_ = {
  latest: () =>
    _request<{
      conversation_id: string;
      session_id: string;
      started_at: string | null;
      ended_at: string | null;
      user_message_preview: string;
    }>(`/v1/conversations/latest`),
  zipUrl: (conversationId: string) =>
    `${_base}/v1/conversations/${encodeURIComponent(conversationId)}/export`,
  markdownUrl: (conversationId: string) =>
    `${_base}/v1/conversations/${encodeURIComponent(conversationId)}/export.md`,
};

// ---- health ---------------------------------------------------------------

export const health = () =>
  _request<{ status: string; storage_backend: string }>(`/health`);

// ---- settings -------------------------------------------------------------

export const settings = {
  server: () => _request<ServerSettings>(`/v1/settings/server`),
  llm: () => _request<LlmSettings>(`/v1/settings/llm`),
  /** Patch the LLM overlay. Pass `null` for a field to clear that
   *  override and fall back to the .env / default. */
  updateLlm: (patch: Record<string, string | number | null>) =>
    _request<LlmSettings>(`/v1/settings/llm`, {
      method: "PUT",
      body: JSON.stringify({ patch, replace: false }),
    }),
};
