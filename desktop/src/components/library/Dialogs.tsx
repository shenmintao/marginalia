/** Two small modal dialogs used by the library page:
 *
 *    NewFolderDialog  — name input, creates under given parent
 *    UploadDialog     — file picker + drag-drop, uploads to given folder
 *                       with progress and conflict-handling. Dropping a
 *                       folder walks the directory tree (webkitGetAsEntry)
 *                       and recreates the subfolder structure under the
 *                       target via /v1/folders before uploading each file.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Filter, X, Upload, Download, FolderPlus, Loader2 } from "lucide-react";

import { folders as foldersApi, uploads, ApiError, settings as settingsApi, webdavSync } from "@/api/client";
import type { OnConflict, WebDavPlanItem, WebDavPlanResult, WebDavSyncLast } from "@/types/api";
import { cn, formatBytes } from "@/lib/utils";
import { useI18n } from "@/lib/i18n";

export function NewFolderDialog({ parentId, parentName, onClose, onCreated }: {
  parentId: string | null;
  parentName: string;
  onClose: () => void;
  onCreated: () => void;
}) {
  const { t } = useI18n();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => { inputRef.current?.focus(); }, []);

  const submit = async () => {
    const v = name.trim();
    if (!v) return;
    setBusy(true); setErr(null);
    try {
      await foldersApi.create(v, parentId);
      onCreated();
      onClose();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <ModalShell
      title={<><FolderPlus size={14} /> {t.library.newFolderIn(parentName)}</>}
      onClose={onClose}
    >
      <input
        ref={inputRef}
        value={name}
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") submit(); }}
        placeholder={t.dialogs.folderName}
        className="w-full rounded-md border border-border bg-bg-base px-3 py-2 text-sm outline-none focus:border-accent"
      />
      {err && <p className="mt-2 text-xs text-danger">{err}</p>}
      <div className="mt-4 flex justify-end gap-2">
        <button onClick={onClose}
                className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-bg-muted">
          {t.common.cancel}
        </button>
        <button onClick={submit} disabled={busy || !name.trim()}
                className={cn(
                  "rounded-md px-3 py-1.5 text-sm font-medium",
                  busy || !name.trim()
                    ? "cursor-not-allowed bg-bg-muted text-fg-subtle"
                    : "bg-accent text-accent-fg hover:opacity-90",
                )}>
          {busy ? t.common.creating : t.common.create}
        </button>
      </div>
    </ModalShell>
  );
}

interface UploadItem {
  file: File;
  /** Path segments relative to the dropped root, e.g. ["sub", "deep"]
   *  for a file at <drop>/sub/deep/file.md. Empty for plain file drops. */
  relDirs: string[];
  loaded: number;
  selected: boolean;
  status: "queued" | "uploading" | "done" | "error";
  err?: string;
  renamedTo?: string;
}

type UploadCategory =
  | "documents"
  | "pdfs"
  | "images"
  | "archives"
  | "audio"
  | "videos"
  | "unknown";

interface UploadGroup {
  category: UploadCategory;
  files: number;
  bytes: number;
  selectedFiles: number;
  selectedBytes: number;
  extensions: {
    ext: string;
    files: number;
    bytes: number;
    selectedFiles: number;
    selectedBytes: number;
  }[];
}

const CATEGORY_ORDER: UploadCategory[] = [
  "documents",
  "pdfs",
  "images",
  "archives",
  "audio",
  "videos",
  "unknown",
];
const DEFAULT_INCLUDED_CATEGORIES: UploadCategory[] = CATEGORY_ORDER.filter(
  (c) => c !== "videos",
);

export function UploadDialog({
  folderId,
  folderName,
  onClose,
  onUploaded,
}: {
  folderId: string | null;
  folderName: string;
  onClose: () => void;
  onUploaded: () => void;
}) {
  const { t } = useI18n();
  const [items, setItems] = useState<UploadItem[]>([]);
  const [conflict, setConflict] = useState<OnConflict>("rename");
  const [running, setRunning] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [filterOpen, setFilterOpen] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  // Seed conflict policy from the server's current default so what the
  // dialog opens with matches what Settings → Default conflict policy
  // shows. Failure (server offline, etc.) just leaves the "rename"
  // initial value alone.
  useEffect(() => {
    let cancelled = false;
    settingsApi.server().then(
      (s) => {
        if (cancelled) return;
        const v = s.default_on_conflict as OnConflict;
        if (v === "rename" || v === "error" || v === "skip") setConflict(v);
      },
      () => {},
    );
    return () => {
      cancelled = true;
    };
  }, []);

  const addItems = (next: UploadItem[]) => {
    setItems((prev) => [...prev, ...next]);
  };

  const uploadPlan = useMemo(
    () => summarizeUploadPlan(items),
    [items],
  );

  const hasQueuedSelected = items.some(
    (it) => it.status === "queued" && it.selected,
  );

  const toggleAll = () => {
    if (running) return;
    const nextSelected = !items.every((item) => item.selected);
    setItems((prev) => prev.map((item) => (
      item.status === "queued" ? { ...item, selected: nextSelected } : item
    )));
  };

  const toggleCategory = (category: UploadCategory) => {
    if (running) return;
    const group = uploadPlan.groups.find((g) => g.category === category);
    const nextSelected = !group || group.selectedFiles < group.files;
    setItems((prev) => prev.map((item) => (
      item.status === "queued" && categoryForFile(item.file) === category
        ? { ...item, selected: nextSelected }
        : item
    )));
  };

  const toggleExtension = (category: UploadCategory, ext: string) => {
    if (running) return;
    const matching = items.filter(
      (item) => categoryForFile(item.file) === category && extensionForFile(item.file) === ext,
    );
    const selectedCount = matching.filter((item) => item.selected).length;
    const nextSelected = selectedCount < matching.length;
    setItems((prev) => prev.map((item) => (
      item.status === "queued"
      && categoryForFile(item.file) === category
      && extensionForFile(item.file) === ext
        ? { ...item, selected: nextSelected }
        : item
    )));
  };

  const toggleItem = (index: number) => {
    if (running || items[index]?.status !== "queued") return;
    setItems((prev) => updateAt(prev, index, { selected: !prev[index].selected }));
  };

  const onPickFiles = (fs: FileList | File[]) => {
    addItems(Array.from(fs).map((file) => ({
      file,
      relDirs: [],
      loaded: 0,
      selected: DEFAULT_INCLUDED_CATEGORIES.includes(categoryForFile(file)),
      status: "queued" as const,
    })));
  };

  const onDrop = async (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault(); setDragOver(false);
    const dt = e.dataTransfer;
    // Prefer items[] — it can expose folder entries via webkitGetAsEntry.
    // Fall back to dt.files for browsers/contexts where items isn't there.
    const entries: FileSystemEntry[] = [];
    if (dt.items && dt.items.length) {
      for (let i = 0; i < dt.items.length; i++) {
        const it = dt.items[i];
        if (it.kind !== "file") continue;
        const ent = (it as DataTransferItem).webkitGetAsEntry?.();
        if (ent) entries.push(ent);
      }
    }
    if (entries.length === 0) {
      if (dt.files.length) onPickFiles(dt.files);
      return;
    }
    setScanning(true);
    try {
      const flat: UploadItem[] = [];
      for (const ent of entries) {
        await walkEntry(ent, [], flat);
      }
      addItems(flat);
    } finally {
      setScanning(false);
    }
  };

  const start = async () => {
    if (running) return;
    setRunning(true);
    let didUpload = false;
    // Cache of "path-from-target → folder_id" so we mkdir each subfolder
    // exactly once even if many files share it. Empty key === target itself.
    const folderCache = new Map<string, string | null>();
    folderCache.set("", folderId);
    for (let i = 0; i < items.length; i++) {
      if (items[i].status !== "queued") continue;
      if (!items[i].selected) continue;
      setItems((p) => updateAt(p, i, { status: "uploading" }));
      try {
        const targetFolderId = await mkdirP(folderCache, items[i].relDirs);
        const dest = targetFolderId
          ? { folderId: targetFolderId } as const
          : { remotePath: "/" + items[i].file.name } as const;
        const res = await uploads.upload(items[i].file, dest, {
          onConflict: conflict,
          onProgress: (loaded) => setItems((p) => updateAt(p, i, { loaded })),
        });
        setItems((p) => updateAt(p, i, {
          status: "done",
          loaded: items[i].file.size,
          renamedTo: res.auto_renamed ? res.display_name : undefined,
        }));
        didUpload = true;
      } catch (e) {
        const msg = e instanceof ApiError
          ? `${e.status} ${typeof e.body === "object" && e.body && "detail" in e.body
              ? JSON.stringify(e.body.detail) : e.message}`
          : (e instanceof Error ? e.message : String(e));
        setItems((p) => updateAt(p, i, { status: "error", err: msg }));
      }
    }
    setRunning(false);
    if (didUpload) onUploaded();
  };

  return (
    <ModalShell
      title={<><Upload size={14} /> {t.library.uploadTo(folderName)}</>}
      onClose={onClose}
      wide
    >
      <div
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
        onClick={() => fileInput.current?.click()}
        className={cn(
          "flex cursor-pointer flex-col items-center justify-center rounded-md border-2 border-dashed py-8 text-sm",
          dragOver
            ? "border-accent bg-accent-subtle text-accent"
            : "border-border bg-bg-base text-fg-muted hover:border-accent",
        )}
      >
        <Upload size={20} className="mb-2" />
        <p>{scanning ? t.dialogs.scanningFolder : t.dialogs.uploadDrop}</p>
        <input ref={fileInput} type="file" multiple className="hidden"
               onChange={(e) => e.target.files && onPickFiles(e.target.files)} />
      </div>

      <div className="mt-3 flex items-center gap-2 text-xs">
        <span className="text-fg-muted">{t.dialogs.onConflict}</span>
        {(["rename", "skip", "error"] as OnConflict[]).map((p) => (
          <button key={p}
                  onClick={() => setConflict(p)}
                  disabled={running}
                  className={cn(
                    "rounded-md border px-2 py-0.5",
                    conflict === p
                      ? "border-accent bg-accent-subtle text-accent"
                      : "border-border text-fg-muted hover:bg-bg-muted",
                  )}>
            {t.dialogs.conflict[p]}
          </button>
        ))}
      </div>
      <p className="mt-3 text-xs leading-5 text-fg-muted">
        {t.dialogs.uploadAnalysisHint}
      </p>

      {items.length > 0 && (
        <div className="mt-3 rounded-md border border-border bg-bg-subtle text-xs">
          <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
            <label className="flex cursor-pointer items-center gap-1.5 text-fg-base">
              <input
                type="checkbox"
                checked={items.length > 0 && items.every((item) => item.selected)}
                disabled={running}
                onChange={toggleAll}
                className="accent-accent"
              />
              <span>{t.dialogs.uploadFilterTitle}</span>
            </label>
            <span className="text-fg-muted">
              {t.dialogs.uploadPlan(uploadPlan.selectedFiles, uploadPlan.skippedFiles)}
              {" / "}
              {formatBytes(uploadPlan.selectedBytes)}
            </span>
            <div className="ml-auto flex flex-wrap items-center gap-2">
              {uploadPlan.groups.map((group) => (
                <CategoryToggle
                  key={group.category}
                  group={group}
                  disabled={running}
                  onToggle={toggleCategory}
                />
              ))}
              <div className="relative">
                <button
                  type="button"
                  onClick={() => setFilterOpen((open) => !open)}
                  disabled={running}
                  title={t.dialogs.uploadFilterTitle}
                  className={cn(
                    "rounded p-1 text-fg-muted hover:bg-bg-muted hover:text-fg-base",
                    filterOpen && "bg-bg-muted text-fg-base",
                  )}
                >
                  <Filter size={14} />
                </button>
                {filterOpen && (
                  <UploadFilterPanel
                    groups={uploadPlan.groups}
                    disabled={running}
                    onToggleCategory={toggleCategory}
                    onToggleExtension={toggleExtension}
                  />
                )}
              </div>
            </div>
          </div>
          {uploadPlan.skippedFiles > 0 && (
            <div className="border-b border-border px-3 py-1.5 text-fg-muted">
              {t.dialogs.uploadSkippedSummary(
                uploadPlan.skippedFiles,
                formatBytes(uploadPlan.skippedBytes),
              )}
            </div>
          )}
          <div className="max-h-72 overflow-y-auto">
            <table className="w-full table-fixed border-collapse">
              <thead className="sticky top-0 bg-bg-subtle text-left text-[11px] text-fg-subtle">
                <tr className="border-b border-border">
                  <th className="w-8 px-3 py-1.5">
                    <span className="sr-only">select</span>
                  </th>
                  <th className="px-1 py-1.5 font-medium">{t.dialogs.uploadFile}</th>
                  <th className="w-20 px-2 py-1.5 font-medium">{t.dialogs.uploadType}</th>
                  <th className="w-20 px-2 py-1.5 text-right font-medium">
                    {t.dialogs.uploadSize}
                  </th>
                  <th className="w-20 px-2 py-1.5 text-right font-medium">
                    {t.dialogs.uploadStatus}
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((it, i) => (
                  <UploadRow
                    key={i}
                    item={it}
                    index={i}
                    running={running}
                    onToggle={toggleItem}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="mt-4 flex justify-end gap-2">
        <button onClick={onClose}
                className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-bg-muted">
          {t.common.close}
        </button>
        <button
          onClick={start}
          disabled={running || !hasQueuedSelected}
          className={cn(
            "flex items-center gap-1 rounded-md px-3 py-1.5 text-sm font-medium",
            running || !hasQueuedSelected
              ? "cursor-not-allowed bg-bg-muted text-fg-subtle"
              : "bg-accent text-accent-fg hover:opacity-90",
          )}
        >
          {running && <Loader2 size={12} className="animate-spin" />}
          {running ? t.dialogs.uploading : t.dialogs.start}
        </button>
      </div>
    </ModalShell>
  );
}

export function WebDavSyncDialog({
  mode,
  onClose,
  onDone,
}: {
  mode: "upload" | "download";
  onClose: () => void;
  onDone: () => void | Promise<void>;
}) {
  const { t } = useI18n();
  const [plan, setPlan] = useState<WebDavPlanResult | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [runningStatus, setRunningStatus] = useState<WebDavSyncLast | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const title = mode === "upload"
    ? t.library.webdavUploadSyncTitle
    : t.library.webdavDownloadSyncTitle;
  const TitleIcon = mode === "upload" ? Upload : Download;

  const refreshPlan = async () => {
    setLoading(true);
    setErr(null);
    try {
      const nextPlan = mode === "upload"
        ? await webdavSync.uploadPlan()
        : await webdavSync.downloadPlan();
      setPlan(nextPlan);
      setSelected(new Set(nextPlan.items.map((item) => item.entry_id)));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refreshPlan();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode]);

  useEffect(() => {
    if (!running) return;
    let cancelled = false;
    const tick = () => webdavSync.status().then(
      (status) => {
        if (!cancelled) setRunningStatus(status.last ?? null);
      },
      () => {},
    );
    tick();
    const handle = window.setInterval(tick, 1000);
    return () => {
      cancelled = true;
      window.clearInterval(handle);
    };
  }, [running]);

  const selectedBytes = useMemo(() => {
    if (!plan) return 0;
    return plan.items
      .filter((item) => selected.has(item.entry_id))
      .reduce((sum, item) => sum + (item.size_bytes ?? 0), 0);
  }, [plan, selected]);
  const allSelected = Boolean(plan?.items.length) && selected.size === plan?.items.length;

  const toggleAll = () => {
    if (!plan || running) return;
    setSelected(allSelected ? new Set() : new Set(plan.items.map((item) => item.entry_id)));
  };

  const toggleOne = (entryId: string) => {
    if (running) return;
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(entryId)) next.delete(entryId);
      else next.add(entryId);
      return next;
    });
  };

  const run = async () => {
    if (running || selected.size === 0) return;
    setRunning(true);
    setRunningStatus(null);
    setErr(null);
    try {
      const entryIds = [...selected];
      if (mode === "upload") await webdavSync.publishSelected(entryIds);
      else await webdavSync.downloadSelected(entryIds);
      await onDone();
      await refreshPlan();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
      setRunningStatus(null);
    }
  };

  return (
    <ModalShell
      title={<><TitleIcon size={14} /> {title}</>}
      onClose={onClose}
      wide
    >
      <div className="mb-3 rounded-md border border-border bg-bg-subtle px-3 py-2 text-xs">
        <div className="grid gap-1 md:grid-cols-2">
          <KvLine label={t.library.webdavRemoteUpdated} value={remoteUpdated(plan, t)} />
          <KvLine label={t.library.webdavRemoteSnapshot} value={plan?.snapshot_id || t.common.unset} mono />
        </div>
      </div>

      {err && <p className="mb-3 rounded-md bg-danger/10 px-3 py-2 text-xs text-danger">{err}</p>}
      {loading && <p className="text-sm text-fg-muted">{t.common.loading}</p>}
      {!loading && plan && (
        <>
          <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-fg-muted">
            <label className="flex cursor-pointer items-center gap-1.5 text-fg-base">
              <input
                type="checkbox"
                checked={allSelected}
                disabled={running || plan.items.length === 0}
                onChange={toggleAll}
                className="accent-accent"
              />
              <span>{t.library.webdavSelectedSummary(selected.size, plan.items.length, formatBytes(selectedBytes))}</span>
            </label>
          </div>
          {running && (
            <div className="mb-2 rounded-md border border-border bg-bg-subtle px-3 py-2 text-xs text-fg-muted">
              {webdavProgressText(runningStatus, mode, t)}
            </div>
          )}
          <div className="max-h-[52vh] overflow-y-auto rounded-md border border-border">
            {plan.items.length === 0 ? (
              <div className="px-3 py-8 text-center text-sm text-fg-muted">
                {mode === "upload" ? t.library.webdavNoUploadItems : t.library.webdavNoDownloadItems}
              </div>
            ) : (
              <table className="w-full table-fixed border-collapse text-xs">
                <thead className="sticky top-0 bg-bg-subtle text-left text-[11px] text-fg-subtle">
                  <tr className="border-b border-border">
                    <th className="w-8 px-3 py-1.5"><span className="sr-only">select</span></th>
                    <th className="px-1 py-1.5 font-medium">{t.dialogs.uploadFile}</th>
                    <th className="w-24 px-2 py-1.5 font-medium">{t.library.webdavReason}</th>
                    <th className="w-24 px-2 py-1.5 text-right font-medium">{t.dialogs.uploadSize}</th>
                    <th className="w-36 px-2 py-1.5 font-medium">{t.library.webdavUpdated}</th>
                  </tr>
                </thead>
                <tbody>
                  {plan.items.map((item) => (
                    <WebDavPlanRow
                      key={item.entry_id}
                      item={item}
                      selected={selected.has(item.entry_id)}
                      disabled={running}
                      onToggle={toggleOne}
                    />
                  ))}
                </tbody>
              </table>
            )}
          </div>
          <div className="mt-4 flex justify-end gap-2">
            <button
              onClick={onClose}
              disabled={running}
              className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-bg-muted disabled:opacity-50"
            >
              {t.common.close}
            </button>
            <button
              onClick={run}
              disabled={running || selected.size === 0}
              className={cn(
                "flex items-center gap-1 rounded-md px-3 py-1.5 text-sm font-medium",
                running || selected.size === 0
                  ? "cursor-not-allowed bg-bg-muted text-fg-subtle"
                  : "bg-accent text-accent-fg hover:opacity-90",
              )}
            >
              {running && <Loader2 size={12} className="animate-spin" />}
              {mode === "upload" ? t.library.webdavUploadSelected : t.library.webdavDownloadSelected}
            </button>
          </div>
        </>
      )}
    </ModalShell>
  );
}

function KvLine({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex min-w-0 items-center gap-2">
      <span className="shrink-0 text-fg-subtle">{label}</span>
      <span className={cn("min-w-0 truncate text-fg-base", mono && "font-mono")}>{value}</span>
    </div>
  );
}

function remoteUpdated(
  plan: WebDavPlanResult | null,
  t: ReturnType<typeof useI18n>["t"],
): string {
  return plan?.remote_updated_at
    ? new Date(plan.remote_updated_at).toLocaleString()
    : t.common.unset;
}

function webdavProgressText(
  status: WebDavSyncLast | null,
  mode: "upload" | "download",
  t: ReturnType<typeof useI18n>["t"],
): string {
  const fallback = mode === "upload"
    ? t.library.webdavUploadRunning
    : t.library.webdavPullRunning;
  if (!status) return fallback;
  if (status.status !== "running") return fallback;
  const parts = [t.library.webdavSyncPhase(status.phase || "running")];
  if (status.selected_entries != null) {
    parts.push(t.library.webdavProgressEntries(status.selected_entries));
  }
  if (status.total_blobs != null) {
    parts.push(t.library.webdavProgressBlobs(
      status.processed_blobs ?? ((status.uploaded_blobs ?? 0) + (status.skipped_blobs ?? 0)),
      status.total_blobs,
      status.uploaded_blobs ?? 0,
      status.skipped_blobs ?? 0,
    ));
  }
  if (status.total_metadata_files != null) {
    parts.push(t.library.webdavProgressMetadata(
      status.uploaded_metadata_files ?? 0,
      status.total_metadata_files,
    ));
  }
  return parts.join(" · ");
}

function WebDavPlanRow({
  item,
  selected,
  disabled,
  onToggle,
}: {
  item: WebDavPlanItem;
  selected: boolean;
  disabled: boolean;
  onToggle: (entryId: string) => void;
}) {
  const { t } = useI18n();
  return (
    <tr className="border-b border-border last:border-0">
      <td className="px-3 py-1.5">
        <input
          type="checkbox"
          checked={selected}
          disabled={disabled}
          onChange={() => onToggle(item.entry_id)}
          className="accent-accent"
        />
      </td>
      <td className="min-w-0 px-1 py-1.5">
        <div className="truncate" title={item.display_name}>{item.display_name}</div>
        <div className="truncate text-[11px] text-fg-subtle" title={item.folder_path || "/"}>
          {item.folder_path || "/"}
        </div>
      </td>
      <td className="px-2 py-1.5 text-fg-muted">{t.library.webdavPlanReason(item.reason)}</td>
      <td className="px-2 py-1.5 text-right text-fg-muted">{formatBytes(item.size_bytes ?? 0)}</td>
      <td className="px-2 py-1.5 text-fg-muted">
        {item.updated_at ? new Date(item.updated_at).toLocaleString() : "-"}
      </td>
    </tr>
  );
}

function CategoryToggle({
  group,
  disabled,
  onToggle,
}: {
  group: UploadGroup;
  disabled: boolean;
  onToggle: (category: UploadCategory) => void;
}) {
  const { t } = useI18n();
  const checked = group.selectedFiles > 0;
  return (
    <label
      className={cn(
        "flex cursor-pointer items-center gap-1.5 whitespace-nowrap text-fg-muted",
        checked && "text-fg-base",
      )}
      title={t.dialogs.uploadCategorySummary(group.files, formatBytes(group.bytes))}
    >
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={() => onToggle(group.category)}
        className="accent-accent"
      />
      <span>{t.dialogs.uploadCategories[group.category]}</span>
    </label>
  );
}

function UploadFilterPanel({
  groups,
  disabled,
  onToggleCategory,
  onToggleExtension,
}: {
  groups: UploadGroup[];
  disabled: boolean;
  onToggleCategory: (category: UploadCategory) => void;
  onToggleExtension: (category: UploadCategory, ext: string) => void;
}) {
  const { t } = useI18n();
  return (
    <div
      className={cn(
        "absolute right-0 top-full z-50 mt-2 w-[360px] max-w-[calc(100vw-48px)]",
        "rounded-md border border-border bg-bg-elevated p-3 shadow-xl",
      )}
    >
      <div className="space-y-3">
        {groups.map((group) => (
          <div key={group.category}>
            <div className="mb-1.5 flex items-center justify-between gap-2">
              <span className="font-medium text-fg-base">
                {t.dialogs.uploadCategories[group.category]}
              </span>
              <button
                type="button"
                disabled={disabled}
                onClick={() => onToggleCategory(group.category)}
                className="rounded border border-border px-2 py-0.5 text-[11px] text-fg-muted hover:border-accent hover:text-accent disabled:opacity-50"
              >
                {group.selectedFiles === group.files
                  ? t.dialogs.uploadSelectNone
                  : t.dialogs.uploadSelectAll}
              </button>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {group.extensions.map((ext) => {
                const selected = ext.selectedFiles > 0;
                return (
                  <button
                    key={ext.ext}
                    type="button"
                    disabled={disabled}
                    onClick={() => onToggleExtension(group.category, ext.ext)}
                    title={
                      selected
                        ? t.dialogs.uploadExcludeExtension(ext.ext)
                        : t.dialogs.uploadIncludeExtension(ext.ext)
                    }
                    className={cn(
                      "rounded border px-2 py-1 text-[11px] disabled:opacity-50",
                      selected
                        ? "border-accent/70 bg-accent-subtle text-accent"
                        : "border-border bg-bg-muted text-fg-subtle",
                    )}
                  >
                    {ext.ext} {ext.files}
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function UploadRow({
  item,
  index,
  running,
  onToggle,
}: {
  item: UploadItem;
  index: number;
  running: boolean;
  onToggle: (index: number) => void;
}) {
  const { t } = useI18n();
  const pct = item.file.size > 0 ? Math.round((item.loaded / item.file.size) * 100) : 0;
  const prefix = item.relDirs.length ? item.relDirs.join("/") + "/" : "";
  const category = categoryForFile(item.file);
  const ext = extensionForFile(item.file);
  const skipped = item.status === "queued" && !item.selected;
  return (
    <tr className={cn("border-b border-border last:border-0", skipped && "text-fg-subtle")}>
      <td className="px-3 py-1.5">
        <input
          type="checkbox"
          checked={item.selected}
          disabled={running || item.status !== "queued"}
          onChange={() => onToggle(index)}
          className="accent-accent"
        />
      </td>
      <td className="min-w-0 px-1 py-1.5">
        <div className="truncate" title={prefix + item.file.name}>
          {prefix && <span className="text-fg-subtle">{prefix}</span>}
          {item.file.name}
        </div>
        {item.renamedTo && (
          <div className="truncate text-[11px] text-fg-subtle" title={item.renamedTo}>
            {t.dialogs.renamedTo(item.renamedTo)}
          </div>
        )}
        {item.err && (
          <div className="truncate text-[11px] text-danger" title={item.err}>
            {item.err}
          </div>
        )}
      </td>
      <td className="px-2 py-1.5 text-fg-muted" title={t.dialogs.uploadCategories[category]}>
        {ext}
      </td>
      <td className="px-2 py-1.5 text-right text-fg-muted">
        {formatBytes(item.file.size)}
      </td>
      <td className="px-2 py-1.5 text-right text-fg-muted">
        {item.status === "uploading" ? `${pct}%`
         : item.status === "done" ? "done"
         : item.status === "error" ? "!"
         : skipped ? t.dialogs.skipped
         : "-"}
      </td>
    </tr>
  );
}

function updateAt<T>(arr: T[], i: number, patch: Partial<T>): T[] {
  const next = [...arr];
  next[i] = { ...next[i], ...patch };
  return next;
}

function summarizeUploadPlan(items: UploadItem[]) {
  const groups = new Map<UploadCategory, UploadGroup>();
  let selectedFiles = 0;
  let selectedBytes = 0;
  let skippedFiles = 0;
  let skippedBytes = 0;

  for (const category of CATEGORY_ORDER) {
    groups.set(category, {
      category,
      files: 0,
      bytes: 0,
      selectedFiles: 0,
      selectedBytes: 0,
      extensions: [],
    });
  }

  const extMaps = new Map<UploadCategory, Map<string, {
    files: number;
    bytes: number;
    selectedFiles: number;
    selectedBytes: number;
  }>>();
  for (const item of items) {
    const category = categoryForFile(item.file);
    const ext = extensionForFile(item.file);
    const group = groups.get(category)!;
    group.files += 1;
    group.bytes += item.file.size;
    if (!extMaps.has(category)) extMaps.set(category, new Map());
    const extMap = extMaps.get(category)!;
    const extStat = extMap.get(ext) ?? {
      files: 0,
      bytes: 0,
      selectedFiles: 0,
      selectedBytes: 0,
    };
    extStat.files += 1;
    extStat.bytes += item.file.size;

    if (item.selected) {
      selectedFiles += 1;
      selectedBytes += item.file.size;
      group.selectedFiles += 1;
      group.selectedBytes += item.file.size;
      extStat.selectedFiles += 1;
      extStat.selectedBytes += item.file.size;
    } else {
      skippedFiles += 1;
      skippedBytes += item.file.size;
    }
    extMap.set(ext, extStat);
  }

  for (const [category, extMap] of extMaps) {
    const group = groups.get(category)!;
    group.extensions = [...extMap.entries()]
      .map(([ext, stat]) => ({ ext, ...stat }))
      .sort((a, b) => b.files - a.files || a.ext.localeCompare(b.ext));
  }

  return {
    groups: CATEGORY_ORDER.map((category) => groups.get(category)!)
      .filter((group) => group.files > 0),
    selectedFiles,
    selectedBytes,
    skippedFiles,
    skippedBytes,
  };
}

function categoryForFile(file: File): UploadCategory {
  const mime = (file.type || "").toLowerCase();
  const ext = extensionForFile(file);
  if (mime === "application/pdf" || ext === ".pdf") return "pdfs";
  if (mime.startsWith("video/") || VIDEO_EXTENSIONS.has(ext)) return "videos";
  if (mime.startsWith("audio/") || AUDIO_EXTENSIONS.has(ext)) return "audio";
  if (mime.startsWith("image/") || IMAGE_EXTENSIONS.has(ext)) return "images";
  if (ARCHIVE_EXTENSIONS.has(ext) || ARCHIVE_MIMES.has(mime)) return "archives";
  if (
    mime.startsWith("text/")
    || DOCUMENT_EXTENSIONS.has(ext)
    || DOCUMENT_MIMES.has(mime)
  ) {
    return "documents";
  }
  return "unknown";
}

function extensionForFile(file: File): string {
  const idx = file.name.lastIndexOf(".");
  if (idx <= 0 || idx === file.name.length - 1) return "(none)";
  return file.name.slice(idx).toLowerCase();
}

const VIDEO_EXTENSIONS = new Set([
  ".3gp", ".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg",
  ".mpg", ".webm", ".wmv",
]);
const AUDIO_EXTENSIONS = new Set([
  ".aac", ".aiff", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".opus",
  ".wav", ".wma",
]);
const IMAGE_EXTENSIONS = new Set([
  ".avif", ".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png",
  ".svg", ".tif", ".tiff", ".webp",
]);
const ARCHIVE_EXTENSIONS = new Set([
  ".7z", ".bz2", ".gz", ".rar", ".tar", ".tgz", ".xz", ".zip",
]);
const DOCUMENT_EXTENSIONS = new Set([
  ".bat", ".c", ".cpp", ".csv", ".docx", ".eml", ".epub", ".go", ".h", ".hpp",
  ".htm", ".html", ".java", ".js", ".json", ".jsx", ".kt", ".log",
  ".md", ".msg", ".odf", ".ods", ".odt", ".php", ".pptm", ".pptx", ".ps1",
  ".py", ".rb", ".rs", ".rst", ".rtf", ".scala", ".sh", ".sql",
  ".swift", ".tex", ".toml", ".ts", ".tsx", ".txt", ".xls", ".xlsm", ".xlsx",
  ".xml", ".yaml", ".yml",
]);
const ARCHIVE_MIMES = new Set([
  "application/gzip",
  "application/vnd.rar",
  "application/x-7z-compressed",
  "application/x-bzip2",
  "application/x-tar",
  "application/zip",
]);
const DOCUMENT_MIMES = new Set([
  "application/epub+zip",
  "application/json",
  "application/rtf",
  "application/vnd.ms-excel",
  "application/vnd.ms-outlook",
  "application/vnd.ms-excel.sheet.macroenabled.12",
  "application/vnd.ms-powerpoint.presentation.macroenabled.12",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/xml",
  "message/rfc822",
]);

function ModalShell({ title, onClose, children, wide }: {
  title: React.ReactNode;
  onClose: () => void;
  children: React.ReactNode;
  wide?: boolean;
}) {
  const { t } = useI18n();
  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/40"
         onClick={onClose}>
      <div
        onClick={(e) => e.stopPropagation()}
        className={cn(
          "flex max-h-[calc(100vh-32px)] flex-col overflow-hidden rounded-lg border border-border bg-bg-elevated shadow-2xl",
          wide
            ? "w-[900px] max-w-[calc(100vw-32px)]"
            : "w-[360px] max-w-[calc(100vw-32px)]",
        )}
      >
        <header className="flex items-center justify-between border-b border-border px-4 py-2.5 text-sm font-medium">
          <span className="flex items-center gap-2">{title}</span>
          <button onClick={onClose}
                  title={t.common.close}
                  className="rounded-md p-1 text-fg-muted hover:bg-bg-muted">
            <X size={14} />
          </button>
        </header>
        <div className="overflow-y-auto p-4">{children}</div>
      </div>
    </div>
  );
}

// ---- folder-drop helpers --------------------------------------------------

/** Recursively flatten a dropped FileSystemEntry into UploadItems whose
 *  relDirs reflects the path within the dropped tree. The dropped folder's
 *  own name is the first segment (so a folder "notes/a.md" dropped on
 *  target T becomes T/notes/a.md), matching what users intuitively expect. */
async function walkEntry(
  entry: FileSystemEntry,
  parentDirs: string[],
  out: UploadItem[],
): Promise<void> {
  if ((entry as FileSystemFileEntry).isFile) {
    const file = await fileFromEntry(entry as FileSystemFileEntry);
    out.push({
      file,
      relDirs: parentDirs,
      loaded: 0,
      selected: DEFAULT_INCLUDED_CATEGORIES.includes(categoryForFile(file)),
      status: "queued",
    });
    return;
  }
  if ((entry as FileSystemDirectoryEntry).isDirectory) {
    const dir = entry as FileSystemDirectoryEntry;
    const reader = dir.createReader();
    // readEntries returns children in chunks; loop until empty.
    const children: FileSystemEntry[] = [];
    while (true) {
      const chunk = await new Promise<FileSystemEntry[]>((res, rej) =>
        reader.readEntries((r) => res(r), (e) => rej(e)),
      );
      if (chunk.length === 0) break;
      children.push(...chunk);
    }
    const nextParents = [...parentDirs, dir.name];
    for (const child of children) {
      await walkEntry(child, nextParents, out);
    }
  }
}

function fileFromEntry(entry: FileSystemFileEntry): Promise<File> {
  return new Promise((resolve, reject) => entry.file(resolve, reject));
}

/** Create folders for relDirs under the cached target, returning the
 *  leaf folder_id (or null if relDirs is empty AND target itself is null,
 *  meaning the upload should go to root via remote_path). */
async function mkdirP(
  cache: Map<string, string | null>,
  relDirs: string[],
): Promise<string | null> {
  if (relDirs.length === 0) return cache.get("") ?? null;
  let key = "";
  let parentId: string | null = cache.get("") ?? null;
  for (const seg of relDirs) {
    key = key ? `${key}/${seg}` : seg;
    if (cache.has(key)) {
      parentId = cache.get(key)!;
      continue;
    }
    parentId = await ensureFolder(seg, parentId);
    cache.set(key, parentId);
  }
  return parentId;
}

/** Idempotent folder create: tries POST /v1/folders, on 409 fetches the
 *  existing one out of the parent listing. */
async function ensureFolder(name: string, parentId: string | null): Promise<string> {
  try {
    const f = await foldersApi.create(name, parentId);
    return f.id;
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) {
      const body = typeof e.body === "object" && e.body && "detail" in e.body
        ? (e.body.detail as { existing_id?: string } | undefined)
        : undefined;
      if (body?.existing_id) return body.existing_id;
      // Fallback: list and find by name.
      const listing = await foldersApi.list(parentId ?? null);
      const hit = listing.folders.find((f) => f.name === name);
      if (hit) return hit.id;
    }
    throw e;
  }
}
