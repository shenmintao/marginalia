/** Single-pane folder + file tree.
 *
 *  Folders expand on chevron click; files are leaf nodes that select on
 *  click. Folders also select on click (showing the empty viewer +
 *  "select a file" hint). Uses the existing `folders.list` and
 *  `folders.get` endpoints — children are fetched lazily.
 *
 *  Background activity (ingest tasks) lights up an `<Loader2>` next to
 *  any file row whose file_id matches an entry in the active-tasks set.
 */
import { useEffect, useState, useCallback, useMemo, useRef } from "react";
import {
  ChevronDown, ChevronRight, Folder as FolderIcon, FolderOpen,
  FileText, Loader2, Plus, Upload as UploadIcon, Download, RefreshCw, Trash2,
  AlertTriangle, CircleDashed,
} from "lucide-react";

import { folders, fileEntries, files, ApiError } from "@/api/client";
import type { Folder, FolderIngestSummary, FileEntrySummary } from "@/types/api";
import { cn } from "@/lib/utils";
import { useI18n, type I18nStrings } from "@/lib/i18n";

export interface FileNode {
  kind: "file";
  entry: FileEntrySummary;
}
export interface FolderNode {
  kind: "folder";
  folder: Folder;
}
export type Node = FileNode | FolderNode;
export interface FolderActionTarget {
  id: string | null;
  name: string;
}

interface Props {
  selectedEntryId: string | null;
  selectedFolderId: string | null;
  selectedFolderName: string | null;
  selectedFolderFailedCount: number | null;
  onSelectFile: (entry: FileEntrySummary) => void;
  onSelectFolder: (folder: Folder | null) => void;
  ingestingFileIds: Set<string>;
  refreshKey: number;
  /** Force-expand this folder ancestor chain (root → leaf). Each row
   *  whose id appears here opens itself and forwards the *remainder*
   *  of the chain to its children — so a click on a search hit walks
   *  the tree open one level at a time. */
  expandPath?: string[];
  /** When set, the leaf folder selects this file once its contents
   *  load. Cleared via `onPendingEntryResolved` so the same path
   *  doesn't keep re-selecting on subsequent re-renders. */
  pendingEntryId?: string | null;
  onPendingEntryResolved?: () => void;
  onUploadHere: (target: FolderActionTarget | null) => void;
  onNewFolderHere: (target: FolderActionTarget | null) => void;
  onEntryDeleted: (entryId: string) => void;
  onFolderDeleted: (folderId: string) => void;
  onClearSelection: () => void;
}

export function FolderTree(props: Props) {
  const [roots, setRoots] = useState<Folder[] | null>(null);
  const [rootEntries, setRootEntries] = useState<FileEntrySummary[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [reprocessingAll, setReprocessingAll] = useState(false);
  const [reprocessingFailed, setReprocessingFailed] = useState(false);
  const { t } = useI18n();

  const load = useCallback(() => {
    folders.list(null).then(
      (r) => { setRoots(r.folders); setRootEntries(r.entries ?? []); setErr(null); },
      (e) => setErr(e instanceof Error ? e.message : String(e)),
    );
  }, []);

  useEffect(() => { load(); }, [load, props.refreshKey]);

  // Root-level entries: if we're navigating to an entry that lives in
  // the root (empty ancestor chain), the leaf is here, not in any
  // FolderRow — match against the root entries we already have.
  useEffect(() => {
    if (!props.pendingEntryId) return;
    const expanding = props.expandPath && props.expandPath.length > 0;
    if (expanding) return;
    const hit = rootEntries.find((e) => e.id === props.pendingEntryId);
    if (hit) {
      props.onSelectFile(hit);
      props.onPendingEntryResolved?.();
    }
  }, [rootEntries, props.pendingEntryId, props.expandPath, props.onSelectFile, props]);

  const headerTarget = props.selectedFolderName ?? t.library.root;
  const reprocessScope = props.selectedFolderId
    ? { folder_id: props.selectedFolderId } as const
    : { all: true } as const;
  const reprocessLabel = props.selectedFolderId
    ? t.library.reprocessFolderConfirm(props.selectedFolderName ?? headerTarget)
    : t.library.reprocessAllConfirm;
  const rootFailedCount = useMemo(() => {
    const folderFailures = (roots ?? []).reduce(
      (sum, folder) => sum + (folder.ingest_summary?.failed ?? 0),
      0,
    );
    const rootEntryFailures = rootEntries.filter((e) => e.ingest_status === "failed").length;
    return folderFailures + rootEntryFailures;
  }, [roots, rootEntries]);
  const scopeFailedCount = props.selectedFolderId
    ? props.selectedFolderFailedCount ?? 0
    : rootFailedCount;
  const failedScope = props.selectedFolderId
    ? { folder_id: props.selectedFolderId, status: "failed" } as const
    : { status: "failed" } as const;
  const failedLabel = props.selectedFolderId
    ? t.library.reprocessFailedFolderConfirm(headerTarget, scopeFailedCount)
    : t.library.reprocessFailedAllConfirm(scopeFailedCount);

  const onReprocessScope = async () => {
    if (reprocessingAll) return;
    if (!confirm(reprocessLabel)) return;
    setReprocessingAll(true);
    try {
      const r = await files.reprocessBulk(reprocessScope);
      alert(
        t.library.queuedReprocess(r.task_ids.length, r.reused_count, r.skipped_count),
      );
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      alert(t.library.bulkReprocessFailed(msg));
    } finally {
      setReprocessingAll(false);
    }
  };

  const onReprocessFailedScope = async () => {
    if (reprocessingFailed) return;
    if (!confirm(failedLabel)) return;
    setReprocessingFailed(true);
    try {
      const r = await files.reprocessBulk(failedScope);
      alert(
        t.library.queuedReprocess(r.task_ids.length, r.reused_count, r.skipped_count),
      );
      load();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      alert(t.library.bulkReprocessFailed(msg));
    } finally {
      setReprocessingFailed(false);
    }
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-center justify-between border-b border-border bg-bg-subtle px-3 py-2">
        <span className="text-xs font-medium text-fg-muted">{t.library.title}</span>
        <div className="flex items-center gap-1">
          {scopeFailedCount > 0 && (
            <button
              onClick={onReprocessFailedScope}
              disabled={reprocessingFailed}
              title={props.selectedFolderId
                ? t.library.reprocessFailedFolderTitle(headerTarget, scopeFailedCount)
                : t.library.reprocessFailedAllTitle(scopeFailedCount)}
              className="rounded p-1 text-danger hover:bg-bg-muted hover:text-danger disabled:opacity-50"
            >
              {reprocessingFailed
                ? <Loader2 size={13} className="animate-spin" />
                : <AlertTriangle size={13} />}
            </button>
          )}
          <button
            onClick={onReprocessScope}
            disabled={reprocessingAll}
            title={props.selectedFolderId
              ? t.library.reprocessFolderTitle(headerTarget)
              : t.library.reprocessAllTitle}
            className="rounded p-1 text-fg-muted hover:bg-bg-muted hover:text-fg-base disabled:opacity-50"
          >
            {reprocessingAll
              ? <Loader2 size={13} className="animate-spin" />
              : <RefreshCw size={13} />}
          </button>
          <button
            onClick={() => props.onNewFolderHere(null)}
            title={t.library.newFolderIn(headerTarget)}
            className="rounded p-1 text-fg-muted hover:bg-bg-muted hover:text-fg-base"
          >
            <Plus size={13} />
          </button>
          <button
            onClick={() => props.onUploadHere(null)}
            title={t.library.uploadTo(headerTarget)}
            className="rounded p-1 text-fg-muted hover:bg-bg-muted hover:text-fg-base"
          >
            <UploadIcon size={13} />
          </button>
        </div>
      </div>
      <div
        className="flex-1 overflow-y-auto px-1 py-2 text-sm"
        onClick={(e) => {
          // Click on bare scroll area (not a row) clears selection.
          if (e.target === e.currentTarget) props.onClearSelection();
        }}
      >
        {err && <p className="px-2 text-xs text-danger">{err}</p>}
        {roots === null && !err && (
          <p className="px-2 text-xs text-fg-subtle">{t.common.loading}</p>
        )}
        {roots && roots.length === 0 && rootEntries.length === 0 && (
          <p className="px-2 text-xs text-fg-subtle">{t.library.emptyTree}</p>
        )}
        {roots && roots.map((f) => (
          <FolderRow
            key={f.id}
            folder={f}
            depth={0}
            onFolderReprocessed={load}
            {...props}
          />
        ))}
        {rootEntries.map((e) => (
          <FileRow
            key={e.id}
            entry={e}
            depth={0}
            selected={props.selectedEntryId === e.id}
            ingesting={props.ingestingFileIds.has(e.file_id)}
            onClick={() => props.onSelectFile(e)}
            onDeleted={(id) => { load(); props.onEntryDeleted(id); }}
            onReprocessed={load}
            t={t}
          />
        ))}
      </div>
    </div>
  );
}

function FolderRow({
  folder, depth,
  selectedEntryId, selectedFolderId, selectedFolderName, selectedFolderFailedCount,
  onSelectFile, onSelectFolder,
  ingestingFileIds,
  refreshKey,
  expandPath, pendingEntryId, onPendingEntryResolved,
  onUploadHere, onNewFolderHere,
  onEntryDeleted, onFolderDeleted,
  onClearSelection,
  onFolderReprocessed,
}: { folder: Folder; depth: number; onFolderReprocessed: () => void } & Props) {
  const [open, setOpen] = useState(false);
  const [children, setChildren] = useState<Folder[] | null>(null);
  const [entries, setEntries] = useState<FileEntrySummary[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [reprocessingFolder, setReprocessingFolder] = useState(false);
  const [reprocessingFailedFolder, setReprocessingFailedFolder] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const loadedRef = useRef(false);
  const { t } = useI18n();

  const loadDetail = useCallback((showSpinner = !loadedRef.current) => {
    if (showSpinner) setLoading(true);
    return folders.get(folder.id).then(
      (d) => {
        loadedRef.current = true;
        setChildren(d.children);
        setEntries(d.entries);
        setLoading(false);
      },
      () => setLoading(false),
    );
  }, [folder.id]);

  const refreshAfterSubtreeReprocess = useCallback(() => {
    onFolderReprocessed();
    if (loadedRef.current) {
      void loadDetail(false);
    }
  }, [loadDetail, onFolderReprocessed]);

  // If this folder sits on the active expandPath, force it open and
  // forward the remainder of the chain to descendants. The first id
  // in the chain is the next ancestor to expand, so a match means
  // "we are that ancestor."
  const onPath = (expandPath?.[0] === folder.id);
  useEffect(() => {
    if (onPath && !open) setOpen(true);
  }, [onPath, open]);

  useEffect(() => {
    if (open) loadDetail();
  }, [open, loadDetail, refreshKey]);

  // Once this folder is the leaf of the expandPath (i.e. expandPath
  // ends here) and its contents have loaded, finalize the deep-link
  // by selecting the pending entry.
  const isLeaf = onPath && (expandPath?.length === 1);
  useEffect(() => {
    if (!isLeaf || !pendingEntryId || entries === null) return;
    const hit = entries.find((e) => e.id === pendingEntryId);
    if (hit) {
      onSelectFile(hit);
      onPendingEntryResolved?.();
    }
  }, [isLeaf, pendingEntryId, entries, onSelectFile, onPendingEntryResolved]);

  const childExpandPath = onPath ? expandPath!.slice(1) : expandPath;

  const isSelected = selectedFolderId === folder.id;
  const folderFailed = (folder.ingest_summary?.failed ?? 0) > 0;
  const indent = { paddingLeft: 8 + depth * 12 };

  const onDeleteFolder = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (deleting) return;
    if (!confirm(t.library.deleteFolderConfirm(folder.name))) return;
    setDeleting(true);
    try {
      await folders.delete(folder.id);
      onFolderDeleted(folder.id);
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : String(err);
      alert(t.library.deleteFailed(msg));
      setDeleting(false);
    }
  };

  const onReprocessFolder = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (reprocessingFolder) return;
    if (!confirm(t.library.reprocessFolderConfirm(folder.name))) return;
    setReprocessingFolder(true);
    try {
      const r = await files.reprocessBulk({ folder_id: folder.id });
      alert(
        t.library.queuedReprocess(r.task_ids.length, r.reused_count, r.skipped_count),
      );
      refreshAfterSubtreeReprocess();
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : String(err);
      alert(t.library.bulkReprocessFailed(msg));
    } finally {
      setReprocessingFolder(false);
    }
  };

  const onReprocessFailedFolder = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (reprocessingFailedFolder) return;
    const failedCount = folder.ingest_summary?.failed ?? 0;
    if (!confirm(t.library.reprocessFailedFolderConfirm(folder.name, failedCount))) return;
    setReprocessingFailedFolder(true);
    try {
      const r = await files.reprocessBulk({ folder_id: folder.id, status: "failed" });
      alert(
        t.library.queuedReprocess(r.task_ids.length, r.reused_count, r.skipped_count),
      );
      refreshAfterSubtreeReprocess();
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : String(err);
      alert(t.library.bulkReprocessFailed(msg));
    } finally {
      setReprocessingFailedFolder(false);
    }
  };

  return (
    <div>
      <div
        className={cn(
          "group flex items-center gap-1 rounded-md py-1 pr-1",
          isSelected ? "bg-accent-subtle text-accent" : "hover:bg-bg-muted",
        )}
        style={indent}
      >
        <button
          onClick={(e) => { e.stopPropagation(); setOpen((o) => !o); }}
          className="shrink-0 text-fg-muted"
        >
          {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        </button>
        <button
          onClick={() => onSelectFolder(folder)}
          className="flex flex-1 items-center gap-1.5 truncate text-left"
        >
          {open
            ? <FolderOpen size={13} className="text-fg-muted" />
            : <FolderIcon size={13} className="text-fg-muted" />}
          <span className="min-w-0 flex-1 truncate">{folder.name}</span>
          <FolderIngestBadge summary={folder.ingest_summary} t={t} />
        </button>
        <div
          className={cn(
            "items-center gap-0.5",
            folderFailed || reprocessingFolder || reprocessingFailedFolder
              ? "flex"
              : "hidden group-hover:flex",
          )}
        >
          {folderFailed && (
            <button
              onClick={onReprocessFailedFolder}
              disabled={reprocessingFailedFolder}
              title={t.library.reprocessFailedFolderTitle(
                folder.name,
                folder.ingest_summary?.failed ?? 0,
              )}
              className="rounded p-0.5 text-danger hover:bg-bg-base hover:text-danger disabled:opacity-50"
            >
              {reprocessingFailedFolder
                ? <Loader2 size={11} className="animate-spin" />
                : <AlertTriangle size={11} />}
            </button>
          )}
          <button
            onClick={onReprocessFolder}
            disabled={reprocessingFolder}
            title={t.library.reprocessFolderTitle(folder.name)}
            className="rounded p-0.5 text-fg-subtle hover:bg-bg-base hover:text-fg-base disabled:opacity-50"
          >
            {reprocessingFolder
              ? <Loader2 size={11} className="animate-spin" />
              : <RefreshCw size={11} />}
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              onNewFolderHere({ id: folder.id, name: folder.name });
            }}
            title={t.library.newSubfolder}
            className="rounded p-0.5 text-fg-subtle hover:bg-bg-base hover:text-fg-base"
          >
            <Plus size={11} />
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              onUploadHere({ id: folder.id, name: folder.name });
            }}
            title={t.library.uploadHere}
            className="rounded p-0.5 text-fg-subtle hover:bg-bg-base hover:text-fg-base"
          >
            <UploadIcon size={11} />
          </button>
          <button
            onClick={onDeleteFolder}
            disabled={deleting}
            title={t.library.deleteFolder}
            className="rounded p-0.5 text-fg-subtle hover:bg-bg-base hover:text-danger disabled:opacity-50"
          >
            {deleting
              ? <Loader2 size={11} className="animate-spin" />
              : <Trash2 size={11} />}
          </button>
        </div>
      </div>
      {open && (
        <div>
          {loading && (
            <div style={{ paddingLeft: 8 + (depth + 1) * 12 }}
                 className="py-1 text-xs text-fg-subtle">…</div>
          )}
          {children?.map((c) => (
            <FolderRow
              key={c.id}
              folder={c}
              depth={depth + 1}
              selectedEntryId={selectedEntryId}
              selectedFolderId={selectedFolderId}
              selectedFolderName={selectedFolderName}
              selectedFolderFailedCount={selectedFolderFailedCount}
              onSelectFile={onSelectFile}
              onSelectFolder={onSelectFolder}
              ingestingFileIds={ingestingFileIds}
              refreshKey={refreshKey}
              expandPath={childExpandPath}
              pendingEntryId={pendingEntryId}
              onPendingEntryResolved={onPendingEntryResolved}
              onUploadHere={onUploadHere}
              onNewFolderHere={onNewFolderHere}
              onEntryDeleted={onEntryDeleted}
              onFolderReprocessed={refreshAfterSubtreeReprocess}
              onFolderDeleted={(id) => { loadDetail(); onFolderDeleted(id); }}
              onClearSelection={onClearSelection}
            />
          ))}
          {entries?.map((e) => (
            <FileRow
              key={e.id}
              entry={e}
              depth={depth + 1}
              selected={selectedEntryId === e.id}
              ingesting={ingestingFileIds.has(e.file_id)}
              onClick={() => onSelectFile(e)}
              onDeleted={(id) => { loadDetail(); onEntryDeleted(id); }}
              onReprocessed={refreshAfterSubtreeReprocess}
              t={t}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function FolderIngestBadge({ summary, t }: {
  summary?: FolderIngestSummary | null;
  t: I18nStrings;
}) {
  if (!summary || summary.total <= 0 || summary.incomplete <= 0) return null;

  const failed = summary.failed > 0;
  const processing = !failed && summary.processing > 0;
  const count = failed ? summary.failed : summary.incomplete;
  const label = failed
    ? t.library.folderFailedBadge(summary.failed)
    : t.library.folderUnfinishedBadge(summary.incomplete);
  const title = t.library.folderIngestSummary(
    summary.total,
    summary.done,
    summary.pending,
    summary.processing,
    summary.failed,
  );

  return (
    <span
      title={title}
      aria-label={label}
      className={cn(
        "inline-flex h-4 shrink-0 items-center gap-0.5 rounded border px-1 text-[10px] leading-none tabular-nums",
        failed
          ? "border-danger/30 bg-danger/10 text-danger"
          : "border-border bg-bg-muted text-fg-muted",
      )}
    >
      {failed ? (
        <AlertTriangle size={10} />
      ) : processing ? (
        <Loader2 size={10} className="animate-spin" />
      ) : (
        <CircleDashed size={10} />
      )}
      <span>{count}</span>
    </span>
  );
}

function FileRow({
  entry, depth, selected, ingesting, onClick, onDeleted, onReprocessed, t,
}: {
  entry: FileEntrySummary; depth: number; selected: boolean;
  ingesting: boolean; onClick: () => void;
  onDeleted: (entryId: string) => void;
  onReprocessed: () => void;
  t: I18nStrings;
}) {
  const [reprocessing, setReprocessing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const failed = entry.ingest_status === "failed";
  const failureTitle = failed && entry.ingest_error
    ? t.library.ingestFailedReason(entry.ingest_error)
    : t.library.ingestFailed;
  const blockedByIngest = ingesting && !failed;
  const reprocessTitle = failed
    ? `${t.library.retryAnalysisTitle}\n${failureTitle}`
    : t.library.reprocessAnalysisTitle;
  const onReprocess = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (reprocessing || blockedByIngest) return;
    const prompt = failed
      ? t.library.retryAnalysisConfirm(entry.display_name)
      : t.library.reprocessFileConfirm(entry.display_name);
    if (!confirm(prompt)) {
      return;
    }
    setReprocessing(true);
    try {
      await files.reprocess(entry.file_id);
      onReprocessed();
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : String(err);
      alert(t.library.reprocessFailed(msg));
    } finally {
      setReprocessing(false);
    }
  };
  const onDelete = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (deleting) return;
    if (!confirm(t.library.deleteFileConfirm(entry.display_name))) {
      return;
    }
    setDeleting(true);
    try {
      await fileEntries.delete(entry.id);
      onDeleted(entry.id);
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : String(err);
      alert(t.library.deleteFailed(msg));
      setDeleting(false);
    }
  };
  return (
    <div
      style={{ paddingLeft: 8 + depth * 12 + 14 }}
      className={cn(
        "group flex w-full items-center gap-1.5 rounded-md py-1 pr-1",
        selected ? "bg-accent-subtle text-accent" : "hover:bg-bg-muted",
      )}
    >
      <button
        onClick={onClick}
        className="flex flex-1 items-center gap-1.5 truncate text-left"
      >
        <FileText size={12} className="shrink-0 text-fg-subtle" />
        <span className="flex-1 truncate">{entry.display_name}</span>
        {failed && (
          <span
            className="shrink-0 text-danger"
            title={failureTitle}
            aria-label={failureTitle}
          >
            <AlertTriangle size={11} />
          </span>
        )}
      </button>
      {blockedByIngest && (
        <Loader2 size={11} className="shrink-0 animate-spin text-fg-subtle" />
      )}
      <button
        onClick={onReprocess}
        disabled={reprocessing || blockedByIngest}
        title={reprocessTitle}
        className={cn(
          "shrink-0 rounded p-0.5 disabled:opacity-50",
          failed
            ? "flex text-danger hover:bg-bg-base hover:text-danger"
            : "hidden text-fg-subtle hover:bg-bg-base hover:text-fg-base group-hover:flex",
        )}
      >
        {reprocessing
          ? <Loader2 size={11} className="animate-spin" />
          : <RefreshCw size={11} />}
      </button>
      <a
        href={fileEntries.downloadUrl(entry.id)}
        download={entry.display_name}
        onClick={(e) => e.stopPropagation()}
        title={t.library.download}
        className="hidden shrink-0 rounded p-0.5 text-fg-subtle hover:bg-bg-base hover:text-fg-base group-hover:flex"
      >
        <Download size={11} />
      </a>
      <button
        onClick={onDelete}
        disabled={deleting}
        title={t.common.delete}
        className="hidden shrink-0 rounded p-0.5 text-fg-subtle hover:bg-bg-base hover:text-danger group-hover:flex disabled:opacity-50"
      >
        {deleting
          ? <Loader2 size={11} className="animate-spin" />
          : <Trash2 size={11} />}
      </button>
    </div>
  );
}
