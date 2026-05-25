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
  FileText, Loader2, Plus, Upload as UploadIcon, Download,
} from "lucide-react";

import { folders, fileEntries } from "@/api/client";
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
}

export function FolderTree(props: Props) {
  const [roots, setRoots] = useState<Folder[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(() => {
    folders.list(null).then(
      (r) => { setRoots(r.folders); setErr(null); },
      (e) => setErr(e instanceof Error ? e.message : String(e)),
    );
  }, []);

  useEffect(() => { load(); }, [load, props.refreshKey]);

  const headerTarget = props.selectedFolderName ?? "root";

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-center justify-between border-b border-border bg-bg-subtle px-3 py-2">
        <span className="text-xs font-medium text-fg-muted">Library</span>
        <div className="flex items-center gap-1">
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
      <div className="flex-1 overflow-y-auto px-1 py-2 text-sm">
        {err && <p className="px-2 text-xs text-danger">{err}</p>}
        {roots === null && !err && (
          <p className="px-2 text-xs text-fg-subtle">loading…</p>
        )}
        {roots && roots.length === 0 && (
          <p className="px-2 text-xs text-fg-subtle">No folders yet.</p>
        )}
        {roots && roots.map((f) => (
          <FolderRow
            key={f.id}
            folder={f}
            depth={0}
            {...props}
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
}: { folder: Folder; depth: number } & Props) {
  const [open, setOpen] = useState(false);
  const [children, setChildren] = useState<Folder[] | null>(null);
  const [entries, setEntries] = useState<FileEntrySummary[] | null>(null);
  const [loading, setLoading] = useState(false);

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
            />
          ))}
        </div>
      )}
    </div>
  );
}

function FileRow({ entry, depth, selected, ingesting, onClick }: {
  entry: FileEntrySummary; depth: number; selected: boolean;
  ingesting: boolean; onClick: () => void;
}) {
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
      </button>
      {ingesting && <Loader2 size={11} className="shrink-0 animate-spin text-fg-subtle" />}
      <a
        href={fileEntries.downloadUrl(entry.id)}
        download={entry.display_name}
        onClick={(e) => e.stopPropagation()}
        title="Download"
        className="hidden shrink-0 rounded p-0.5 text-fg-subtle hover:bg-bg-base hover:text-fg-base group-hover:flex"
      >
        <Download size={11} />
      </a>
    </div>
  );
}
