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
import { X, Upload, FolderPlus, Loader2 } from "lucide-react";

import { folders as foldersApi, uploads, ApiError, settings as settingsApi } from "@/api/client";
import type { OnConflict } from "@/types/api";
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
  extensions: { ext: string; files: number; bytes: number }[];
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
const DEFAULT_INCLUDED_CATEGORIES = CATEGORY_ORDER.filter((c) => c !== "videos");

export function UploadDialog({ folderId, folderName, onClose, onUploaded }: {
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
  const [includedCategories, setIncludedCategories] = useState<Set<UploadCategory>>(
    () => new Set(DEFAULT_INCLUDED_CATEGORIES),
  );
  const [excludedExtensions, setExcludedExtensions] = useState<Set<string>>(
    () => new Set(),
  );
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
    () => summarizeUploadPlan(items, includedCategories, excludedExtensions),
    [items, includedCategories, excludedExtensions],
  );

  const hasQueuedSelected = items.some(
    (it) => it.status === "queued"
      && shouldUploadItem(it, includedCategories, excludedExtensions),
  );

  const toggleCategory = (category: UploadCategory) => {
    if (running) return;
    setIncludedCategories((prev) => {
      const next = new Set(prev);
      if (next.has(category)) next.delete(category);
      else next.add(category);
      return next;
    });
  };

  const toggleExtension = (ext: string) => {
    if (running) return;
    setExcludedExtensions((prev) => {
      const next = new Set(prev);
      if (next.has(ext)) next.delete(ext);
      else next.add(ext);
      return next;
    });
  };

  const onPickFiles = (fs: FileList | File[]) => {
    addItems(Array.from(fs).map((file) => ({
      file, relDirs: [], loaded: 0, status: "queued" as const,
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
      if (!shouldUploadItem(items[i], includedCategories, excludedExtensions)) continue;
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
        <div className="mt-3 rounded-md border border-border bg-bg-subtle p-3 text-xs">
          <div className="flex items-center justify-between gap-3">
            <span className="font-medium text-fg-base">{t.dialogs.uploadFilterTitle}</span>
            <span className="shrink-0 text-fg-muted">
              {t.dialogs.uploadPlan(uploadPlan.selectedFiles, uploadPlan.skippedFiles)}
            </span>
          </div>
          {uploadPlan.skippedFiles > 0 && (
            <p className="mt-1 text-fg-muted">
              {t.dialogs.uploadSkippedSummary(
                uploadPlan.skippedFiles,
                formatBytes(uploadPlan.skippedBytes),
              )}
            </p>
          )}
          <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2">
            {uploadPlan.groups.map((group) => (
              <FilterGroupCard
                key={group.category}
                group={group}
                checked={includedCategories.has(group.category)}
                excludedExtensions={excludedExtensions}
                disabled={running}
                onToggleCategory={toggleCategory}
                onToggleExtension={toggleExtension}
              />
            ))}
          </div>
        </div>
      )}

      {items.length > 0 && (
        <ul className="mt-3 max-h-64 space-y-1 overflow-y-auto rounded-md border border-border bg-bg-subtle p-2 text-xs">
          {items.map((it, i) => (
            <UploadRow
              key={i}
              item={it}
              skipped={
                it.status === "queued"
                && !shouldUploadItem(it, includedCategories, excludedExtensions)
              }
            />
          ))}
        </ul>
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

function FilterGroupCard({
  group,
  checked,
  excludedExtensions,
  disabled,
  onToggleCategory,
  onToggleExtension,
}: {
  group: UploadGroup;
  checked: boolean;
  excludedExtensions: Set<string>;
  disabled: boolean;
  onToggleCategory: (category: UploadCategory) => void;
  onToggleExtension: (ext: string) => void;
}) {
  const { t } = useI18n();
  return (
    <div className="rounded-md border border-border bg-bg-base p-2">
      <label className="flex cursor-pointer items-start gap-2">
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled}
          onChange={() => onToggleCategory(group.category)}
          className="mt-0.5 accent-accent"
        />
        <span className="min-w-0 flex-1">
          <span className="block font-medium text-fg-base">
            {t.dialogs.uploadCategories[group.category]}
          </span>
          <span className="block text-fg-muted">
            {t.dialogs.uploadCategorySummary(group.files, formatBytes(group.bytes))}
          </span>
        </span>
      </label>
      {group.extensions.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {group.extensions.map((ext) => {
            const excluded = excludedExtensions.has(ext.ext);
            return (
              <button
                key={ext.ext}
                type="button"
                disabled={disabled}
                onClick={() => onToggleExtension(ext.ext)}
                title={
                  excluded
                    ? t.dialogs.uploadIncludeExtension(ext.ext)
                    : t.dialogs.uploadExcludeExtension(ext.ext)
                }
                className={cn(
                  "rounded border px-1.5 py-0.5 text-[11px]",
                  excluded
                    ? "border-danger/50 bg-danger/10 text-danger"
                    : "border-border text-fg-muted hover:border-accent hover:text-accent",
                )}
              >
                {ext.ext} {ext.files}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function UploadRow({ item, skipped }: { item: UploadItem; skipped: boolean }) {
  const { t } = useI18n();
  const pct = item.file.size > 0 ? Math.round((item.loaded / item.file.size) * 100) : 0;
  const prefix = item.relDirs.length ? item.relDirs.join("/") + "/" : "";
  return (
    <li className="flex items-center gap-2">
      <span className="flex-1 truncate" title={prefix + item.file.name}>
        {prefix && <span className="text-fg-subtle">{prefix}</span>}
        {item.file.name}
      </span>
      <span className="w-12 text-right text-fg-subtle">
        {item.status === "uploading" ? `${pct}%`
         : item.status === "done" ? "done"
         : item.status === "error" ? "!"
         : skipped ? t.dialogs.skipped
         : "-"}
      </span>
      {item.renamedTo && (
        <span className="truncate text-fg-subtle" title={t.dialogs.renamedTo(item.renamedTo)}>
          {t.dialogs.renamedTo(item.renamedTo)}
        </span>
      )}
      {item.err && (
        <span className="truncate text-danger" title={item.err}>{item.err}</span>
      )}
    </li>
  );
}

function updateAt<T>(arr: T[], i: number, patch: Partial<T>): T[] {
  const next = [...arr];
  next[i] = { ...next[i], ...patch };
  return next;
}

function summarizeUploadPlan(
  items: UploadItem[],
  includedCategories: Set<UploadCategory>,
  excludedExtensions: Set<string>,
) {
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

  const extMaps = new Map<UploadCategory, Map<string, { files: number; bytes: number }>>();
  for (const item of items) {
    const category = categoryForFile(item.file);
    const ext = extensionForFile(item.file);
    const group = groups.get(category)!;
    group.files += 1;
    group.bytes += item.file.size;
    if (!extMaps.has(category)) extMaps.set(category, new Map());
    const extMap = extMaps.get(category)!;
    const extStat = extMap.get(ext) ?? { files: 0, bytes: 0 };
    extStat.files += 1;
    extStat.bytes += item.file.size;
    extMap.set(ext, extStat);

    if (shouldUploadItem(item, includedCategories, excludedExtensions)) {
      selectedFiles += 1;
      selectedBytes += item.file.size;
      group.selectedFiles += 1;
      group.selectedBytes += item.file.size;
    } else {
      skippedFiles += 1;
      skippedBytes += item.file.size;
    }
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

function shouldUploadItem(
  item: UploadItem,
  includedCategories: Set<UploadCategory>,
  excludedExtensions: Set<string>,
): boolean {
  const category = categoryForFile(item.file);
  const ext = extensionForFile(item.file);
  return includedCategories.has(category) && !excludedExtensions.has(ext);
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
  ".bat", ".c", ".cpp", ".csv", ".doc", ".docx", ".go", ".h", ".hpp",
  ".htm", ".html", ".java", ".js", ".json", ".jsx", ".kt", ".log",
  ".md", ".odf", ".ods", ".odt", ".php", ".ppt", ".pptx", ".ps1",
  ".py", ".rb", ".rs", ".rst", ".rtf", ".scala", ".sh", ".sql",
  ".swift", ".tex", ".toml", ".ts", ".tsx", ".txt", ".xls", ".xlsx",
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
  "application/json",
  "application/msword",
  "application/rtf",
  "application/vnd.ms-excel",
  "application/vnd.ms-powerpoint",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/xml",
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
          "rounded-lg border border-border bg-bg-elevated shadow-2xl",
          wide ? "w-[480px]" : "w-[360px]",
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
        <div className="p-4">{children}</div>
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
    out.push({ file, relDirs: parentDirs, loaded: 0, status: "queued" });
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
