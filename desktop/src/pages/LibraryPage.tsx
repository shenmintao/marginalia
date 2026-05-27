/** Library = a personal file cabinet with metadata.
 *
 *  Three-region layout:
 *    Tree (left, fixed-ish width)  | Viewer (center, fluid) | Meta (right, collapsible)
 *
 *  - Tree merges folders and files (folders expand, files leaf-select)
 *  - Viewer renders the selected file (PDF iframe / image / md / code / docx)
 *  - Meta panel shows entry's display_name, lifecycle, summary, tags, related
 *  - Background ingest tasks are reflected by spinners on the matching file rows
 *    via a single 4 s poll of /v1/tasks/active
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Inbox } from "lucide-react";

import { fileEntries, tasks } from "@/api/client";
import type { ActiveTasks, FileEntrySummary, FileMetadata, Folder } from "@/types/api";
import { FolderTree } from "@/components/library/FolderTree";
import { FileViewer } from "@/components/library/FileViewer";
import { MetaPanel } from "@/components/library/MetaPanel";
import { NewFolderDialog, UploadDialog } from "@/components/library/Dialogs";

export function LibraryPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [selectedEntry, setSelectedEntry] = useState<FileEntrySummary | null>(null);
  const [selectedFolder, setSelectedFolder] = useState<Folder | null>(null);
  const [meta, setMeta] = useState<FileMetadata | null>(null);
  const [metaLoading, setMetaLoading] = useState(false);
  const [metaOpen, setMetaOpen] = useState(true);
  const [refreshKey, setRefreshKey] = useState(0);
  // Folder ids that should be force-expanded on the next render — set
  // when arriving from search/chat with `?entry=<id>` so each ancestor
  // opens in turn. Cleared once consumed.
  const [expandPath, setExpandPath] = useState<string[]>([]);
  // Pending entry id we've been told to land on but haven't yet
  // resolved into a FileEntrySummary (the tree fills that in once it
  // loads the leaf folder's contents).
  const [pendingEntryId, setPendingEntryId] = useState<string | null>(null);
  // Optional position locator that travelled in on the same ?entry=
  // deep-link (`?q=<text>`, `?line=10-40`, or `?page=3`). FileViewer
  // reads this and scrolls/jumps/highlights once the file renders.
  const [pendingLocator, setPendingLocator] = useState<
    { kind: "quote" | "line" | "page"; value: string } | null
  >(null);

  const [newFolderUnder, setNewFolderUnder] = useState<{ id: string | null; name: string } | null>(null);
  const [uploadInto, setUploadInto] = useState<{ id: string | null; name: string } | null>(null);

  const [active, setActive] = useState<ActiveTasks | null>(null);
  const ingestingFileIds = useMemo<Set<string>>(() => {
    const set = new Set<string>();
    if (!active) return set;
    for (const t of active.running) if (t.file_id) set.add(t.file_id);
    for (const t of active.pending) if (t.file_id) set.add(t.file_id);
    return set;
  }, [active]);

  useEffect(() => {
    let cancelled = false;
    const tick = () => tasks.active().then(
      (r) => { if (!cancelled) setActive(r); },
      () => {},
    );
    tick();
    const handle = window.setInterval(tick, 4000);
    return () => { cancelled = true; window.clearInterval(handle); };
  }, []);

  // ?entry=<id> deep-link: ask the backend for the folder ancestor
  // chain, hand the ids to FolderTree as expandPath so each parent
  // opens, and remember the entry id so the tree can complete the
  // selection once the leaf folder's contents come back.
  // ?q=, ?line=, ?page= ride along on the same URL — captured into
  // pendingLocator and consumed by FileViewer once the file loads.
  useEffect(() => {
    const entryId = searchParams.get("entry");
    if (!entryId) return;
    const quoteParam = searchParams.get("q");
    const lineParam = searchParams.get("line");
    const pageParam = searchParams.get("page");
    let cancelled = false;
    fileEntries.path(entryId).then(
      (p) => {
        if (cancelled) return;
        setExpandPath(p.ancestors.map((a) => a.id));
        setPendingEntryId(p.entry_id);
        if (quoteParam) setPendingLocator({ kind: "quote", value: quoteParam });
        else if (lineParam) setPendingLocator({ kind: "line", value: lineParam });
        else if (pageParam) setPendingLocator({ kind: "page", value: pageParam });
        else setPendingLocator(null);
        // Drop the query params so a later navigation back to /library
        // (without them) doesn't keep retriggering the expansion.
        const next = new URLSearchParams(searchParams);
        next.delete("entry");
        next.delete("q");
        next.delete("line");
        next.delete("page");
        setSearchParams(next, { replace: true });
      },
      () => {
        if (!cancelled) {
          const next = new URLSearchParams(searchParams);
          next.delete("entry");
          next.delete("q");
          next.delete("line");
          next.delete("page");
          setSearchParams(next, { replace: true });
        }
      },
    );
    return () => { cancelled = true; };
  }, [searchParams, setSearchParams]);

  useEffect(() => {
    if (!selectedEntry) { setMeta(null); return; }
    // Reset meta immediately so stale metadata from a previous file
    // doesn't cause wrong classification (e.g. PDF iframe loading a
    // docx URL triggers a browser download).
    setMeta(null);
    let cancelled = false;
    setMetaLoading(true);
    fileEntries.metadata(selectedEntry.id)
      .then((m) => { if (!cancelled) setMeta(m); })
      .catch(() => { if (!cancelled) setMeta(null); })
      .finally(() => { if (!cancelled) setMetaLoading(false); });
    return () => { cancelled = true; };
  }, [selectedEntry]);

  const onSelectFile = useCallback((entry: FileEntrySummary) => {
    setSelectedEntry(entry);
    setSelectedFolder(null);
  }, []);
  const onSelectFolder = useCallback((folder: Folder | null) => {
    setSelectedFolder(folder);
    setSelectedEntry(null);
    setMeta(null);
  }, []);

  const triggerRefresh = useCallback(() => setRefreshKey((k) => k + 1), []);

  const clearSelection = useCallback(() => {
    setSelectedEntry(null);
    setSelectedFolder(null);
    setMeta(null);
  }, []);

  // The Library header buttons live on the tree, but uploading to "root"
  // when the user has clearly picked a folder is rarely what they want.
  // Bias header actions toward the current selection; fall back to root.
  const headerTargetFolderId = selectedFolder?.id ?? null;
  const headerTargetName = selectedFolder?.name ?? "root";

  return (
    <div className="flex h-full">
      <div className="w-72 shrink-0 border-r border-border bg-bg-base">
        <FolderTree
          selectedEntryId={selectedEntry?.id || null}
          selectedFolderId={selectedFolder?.id || null}
          selectedFolderName={selectedFolder?.name || null}
          onSelectFile={onSelectFile}
          onSelectFolder={onSelectFolder}
          ingestingFileIds={ingestingFileIds}
          refreshKey={refreshKey}
          expandPath={expandPath}
          pendingEntryId={pendingEntryId}
          onPendingEntryResolved={() => setPendingEntryId(null)}
          onUploadHere={(id) => setUploadInto({
            id: id ?? headerTargetFolderId,
            name: id
              ? (selectedFolder?.id === id ? selectedFolder.name : "…")
              : headerTargetName,
          })}
          onNewFolderHere={(id) => setNewFolderUnder({
            id: id ?? headerTargetFolderId,
            name: id
              ? (selectedFolder?.id === id ? selectedFolder.name : "…")
              : headerTargetName,
          })}
          onEntryDeleted={(id) => {
            if (selectedEntry?.id === id) {
              setSelectedEntry(null);
              setMeta(null);
            }
          }}
          onFolderDeleted={(id) => {
            if (selectedFolder?.id === id) {
              setSelectedFolder(null);
            }
            triggerRefresh();
          }}
          onClearSelection={clearSelection}
        />
      </div>

      <main className="flex flex-1 overflow-hidden bg-bg-base">
        {selectedEntry ? (
          <FileViewer
            entryId={selectedEntry.id}
            meta={meta}
            locator={pendingLocator}
            onLocatorConsumed={() => setPendingLocator(null)}
          />
        ) : (
          <EmptyViewer folder={selectedFolder} onClick={clearSelection} />
        )}
        <MetaPanel
          meta={meta}
          loading={metaLoading}
          open={metaOpen}
          onToggle={() => setMetaOpen((o) => !o)}
        />
      </main>

      {newFolderUnder && (
        <NewFolderDialog
          parentId={newFolderUnder.id}
          parentName={newFolderUnder.name}
          onClose={() => setNewFolderUnder(null)}
          onCreated={triggerRefresh}
        />
      )}
      {uploadInto && (
        <UploadDialog
          folderId={uploadInto.id}
          folderName={uploadInto.name}
          onClose={() => setUploadInto(null)}
          onUploaded={triggerRefresh}
        />
      )}
    </div>
  );
}

function EmptyViewer({ folder, onClick }: { folder: Folder | null; onClick: () => void }) {
  return (
    <div
      onClick={onClick}
      className="flex flex-1 cursor-default flex-col items-center justify-center text-center"
    >
      <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-bg-subtle text-fg-muted">
        <Inbox size={20} />
      </div>
      <h2 className="text-base font-medium">
        {folder ? folder.name : "Library"}
      </h2>
      <p className="mt-1 max-w-md text-sm text-fg-muted">
        {folder
          ? "Select a file from the tree to preview it. Click here to deselect."
          : "Select or create a folder to start. Files appear under their folder."}
      </p>
    </div>
  );
}
