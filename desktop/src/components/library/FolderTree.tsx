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
import { useEffect, useState, useCallback } from "react";
import {
  ChevronDown, ChevronRight, Folder as FolderIcon, FolderOpen,
  FileText, Loader2, Plus, Upload as UploadIcon, Download, RefreshCw, Trash2,
  AlertTriangle,
} from "lucide-react";

import { folders, fileEntries, files, ApiError } from "@/api/client";
import type { Folder, FileEntrySummary } from "@/types/api";
import { cn } from "@/lib/utils";

export interface FileNode {
  kind: "file";
  entry: FileEntrySummary;
}
export interface FolderNode {
  kind: "folder";
  folder: Folder;
}
export type Node = FileNode | FolderNode;

interface Props {
  selectedEntryId: string | null;
  selectedFolderId: string | null;
  selectedFolderName: string | null;
  onSelectFile: (entry: FileEntrySummary) => void;
  onSelectFolder: (folder: Folder | null) => void;
  ingestingFileIds: Set<string>;
  refreshKey: number;
  onUploadHere: (folderId: string | null) => void;
  onNewFolderHere: (parentId: string | null) => void;
  onEntryDeleted: (entryId: string) => void;
  onFolderDeleted: (folderId: string) => void;
  onClearSelection: () => void;
}

export function FolderTree(props: Props) {
  const [roots, setRoots] = useState<Folder[] | null>(null);
  const [rootEntries, setRootEntries] = useState<FileEntrySummary[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [reprocessingAll, setReprocessingAll] = useState(false);

  const load = useCallback(() => {
    folders.list(null).then(
      (r) => { setRoots(r.folders); setRootEntries(r.entries ?? []); setErr(null); },
      (e) => setErr(e instanceof Error ? e.message : String(e)),
    );
  }, []);

  useEffect(() => { load(); }, [load, props.refreshKey]);

  const headerTarget = props.selectedFolderName ?? "root";
  const reprocessScope = props.selectedFolderId
    ? { folder_id: props.selectedFolderId } as const
    : { all: true } as const;
  const reprocessLabel = props.selectedFolderId
    ? `Re-run AI analysis on every file in "${props.selectedFolderName}" and its subfolders?`
    : `Re-run AI analysis on EVERY file in the library?\n\nThis clears existing summaries and tags, then re-ingests with the current LLM. Could take a while.`;

  const onReprocessScope = async () => {
    if (reprocessingAll) return;
    if (!confirm(reprocessLabel)) return;
    setReprocessingAll(true);
    try {
      const r = await files.reprocessBulk(reprocessScope);
      alert(
        `Queued ${r.task_ids.length} files for reprocessing.` +
          (r.reused_count ? ` ${r.reused_count} already in the queue.` : "") +
          (r.skipped_count ? ` ${r.skipped_count} skipped (deleted).` : ""),
      );
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      alert(`Bulk reprocess failed: ${msg}`);
    } finally {
      setReprocessingAll(false);
    }
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-center justify-between border-b border-border bg-bg-subtle px-3 py-2">
        <span className="text-xs font-medium text-fg-muted">Library</span>
        <div className="flex items-center gap-1">
          <button
            onClick={onReprocessScope}
            disabled={reprocessingAll}
            title={props.selectedFolderId
              ? `Reprocess "${headerTarget}" subtree`
              : "Reprocess entire library"}
            className="rounded p-1 text-fg-muted hover:bg-bg-muted hover:text-fg-base disabled:opacity-50"
          >
            {reprocessingAll
              ? <Loader2 size={13} className="animate-spin" />
              : <RefreshCw size={13} />}
          </button>
          <button
            onClick={() => props.onNewFolderHere(null)}
            title={`New folder in ${headerTarget}`}
            className="rounded p-1 text-fg-muted hover:bg-bg-muted hover:text-fg-base"
          >
            <Plus size={13} />
          </button>
          <button
            onClick={() => props.onUploadHere(null)}
            title={`Upload to ${headerTarget}`}
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
          <p className="px-2 text-xs text-fg-subtle">loading…</p>
        )}
        {roots && roots.length === 0 && rootEntries.length === 0 && (
          <p className="px-2 text-xs text-fg-subtle">Empty. Use the buttons above to create a folder or upload.</p>
        )}
        {roots && roots.map((f) => (
          <FolderRow
            key={f.id}
            folder={f}
            depth={0}
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
          />
        ))}
      </div>
    </div>
  );
}

function FolderRow({
  folder, depth,
  selectedEntryId, selectedFolderId, selectedFolderName,
  onSelectFile, onSelectFolder,
  ingestingFileIds,
  refreshKey,
  onUploadHere, onNewFolderHere,
  onEntryDeleted, onFolderDeleted,
  onClearSelection,
}: { folder: Folder; depth: number } & Props) {
  const [open, setOpen] = useState(false);
  const [children, setChildren] = useState<Folder[] | null>(null);
  const [entries, setEntries] = useState<FileEntrySummary[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const loadDetail = useCallback(() => {
    setLoading(true);
    folders.get(folder.id).then(
      (d) => { setChildren(d.children); setEntries(d.entries); setLoading(false); },
      () => setLoading(false),
    );
  }, [folder.id]);

  useEffect(() => {
    if (open) loadDetail();
  }, [open, loadDetail, refreshKey]);

  const isSelected = selectedFolderId === folder.id;
  const indent = { paddingLeft: 8 + depth * 12 };

  const onDeleteFolder = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (deleting) return;
    if (!confirm(
      `Delete folder "${folder.name}" and everything inside it?\n\n` +
      `Subfolders and files are moved to trash and purged after 7 days.`,
    )) return;
    setDeleting(true);
    try {
      await folders.delete(folder.id);
      onFolderDeleted(folder.id);
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : String(err);
      alert(`Delete failed: ${msg}`);
      setDeleting(false);
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
          <span className="truncate">{folder.name}</span>
        </button>
        <div className="hidden items-center gap-0.5 group-hover:flex">
          <button
            onClick={(e) => { e.stopPropagation(); onNewFolderHere(folder.id); }}
            title="New subfolder"
            className="rounded p-0.5 text-fg-subtle hover:bg-bg-base hover:text-fg-base"
          >
            <Plus size={11} />
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); onUploadHere(folder.id); }}
            title="Upload here"
            className="rounded p-0.5 text-fg-subtle hover:bg-bg-base hover:text-fg-base"
          >
            <UploadIcon size={11} />
          </button>
          <button
            onClick={onDeleteFolder}
            disabled={deleting}
            title="Delete folder"
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
              onSelectFile={onSelectFile}
              onSelectFolder={onSelectFolder}
              ingestingFileIds={ingestingFileIds}
              refreshKey={refreshKey}
              onUploadHere={onUploadHere}
              onNewFolderHere={onNewFolderHere}
              onEntryDeleted={onEntryDeleted}
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
            />
          ))}
        </div>
      )}
    </div>
  );
}

function FileRow({ entry, depth, selected, ingesting, onClick, onDeleted }: {
  entry: FileEntrySummary; depth: number; selected: boolean;
  ingesting: boolean; onClick: () => void;
  onDeleted: (entryId: string) => void;
}) {
  const [reprocessing, setReprocessing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const failed = entry.ingest_status === "failed";
  const onReprocess = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (reprocessing || ingesting) return;
    const prompt = failed
      ? `Retry AI analysis on "${entry.display_name}"?\n\nThe previous ingest failed. This re-runs with the current LLM.`
      : `Re-run AI analysis on "${entry.display_name}"?\n\nThis clears the existing summary and tags, then re-ingests with the current LLM.`;
    if (!confirm(prompt)) {
      return;
    }
    setReprocessing(true);
    try {
      await files.reprocess(entry.file_id);
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : String(err);
      alert(`Reprocess failed: ${msg}`);
    } finally {
      setReprocessing(false);
    }
  };
  const onDelete = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (deleting) return;
    if (!confirm(`Delete "${entry.display_name}"?\n\nThe file is moved to trash and purged after 7 days.`)) {
      return;
    }
    setDeleting(true);
    try {
      await fileEntries.delete(entry.id);
      onDeleted(entry.id);
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : String(err);
      alert(`Delete failed: ${msg}`);
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
          <AlertTriangle
            size={11}
            className="shrink-0 text-danger"
            aria-label="ingest failed"
          />
        )}
      </button>
      {ingesting && <Loader2 size={11} className="shrink-0 animate-spin text-fg-subtle" />}
      <button
        onClick={onReprocess}
        disabled={reprocessing || ingesting}
        title={failed ? "Retry AI analysis (previous run failed)" : "Re-run AI analysis"}
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
        title="Download"
        className="hidden shrink-0 rounded p-0.5 text-fg-subtle hover:bg-bg-base hover:text-fg-base group-hover:flex"
      >
        <Download size={11} />
      </a>
      <button
        onClick={onDelete}
        disabled={deleting}
        title="Delete"
        className="hidden shrink-0 rounded p-0.5 text-fg-subtle hover:bg-bg-base hover:text-danger group-hover:flex disabled:opacity-50"
      >
        {deleting
          ? <Loader2 size={11} className="animate-spin" />
          : <Trash2 size={11} />}
      </button>
    </div>
  );
}
