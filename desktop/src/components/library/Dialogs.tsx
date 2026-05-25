/** Two small modal dialogs used by the library page:
 *
 *    NewFolderDialog  — name input, creates under given parent
 *    UploadDialog     — file picker + drag-drop, uploads to given folder
 *                       with progress and conflict-handling
 */
import { useEffect, useRef, useState } from "react";
import { X, Upload, FolderPlus, Loader2 } from "lucide-react";

import { folders as foldersApi, uploads, ApiError } from "@/api/client";
import type { OnConflict } from "@/types/api";
import { cn } from "@/lib/utils";

export function NewFolderDialog({ parentId, parentName, onClose, onCreated }: {
  parentId: string | null;
  parentName: string;
  onClose: () => void;
  onCreated: () => void;
}) {
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
      title={<><FolderPlus size={14} /> New folder in <em className="font-mono">{parentName}</em></>}
      onClose={onClose}
    >
      <input
        ref={inputRef}
        value={name}
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") submit(); }}
        placeholder="Folder name"
        className="w-full rounded-md border border-border bg-bg-base px-3 py-2 text-sm outline-none focus:border-accent"
      />
      {err && <p className="mt-2 text-xs text-danger">{err}</p>}
      <div className="mt-4 flex justify-end gap-2">
        <button onClick={onClose}
                className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-bg-muted">
          Cancel
        </button>
        <button onClick={submit} disabled={busy || !name.trim()}
                className={cn(
                  "rounded-md px-3 py-1.5 text-sm font-medium",
                  busy || !name.trim()
                    ? "cursor-not-allowed bg-bg-muted text-fg-subtle"
                    : "bg-accent text-accent-fg hover:opacity-90",
                )}>
          {busy ? "Creating…" : "Create"}
        </button>
      </div>
    </ModalShell>
  );
}

interface UploadItem {
  file: File;
  loaded: number;
  status: "queued" | "uploading" | "done" | "error";
  err?: string;
  renamedTo?: string;
}

export function UploadDialog({ folderId, folderName, onClose, onUploaded }: {
  folderId: string | null;
  folderName: string;
  onClose: () => void;
  onUploaded: () => void;
}) {
  const [items, setItems] = useState<UploadItem[]>([]);
  const [conflict, setConflict] = useState<OnConflict>("rename");
  const [running, setRunning] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const addFiles = (fs: FileList | File[]) => {
    setItems((prev) => [
      ...prev,
      ...Array.from(fs).map<UploadItem>((file) => ({
        file, loaded: 0, status: "queued",
      })),
    ]);
  };

  const start = async () => {
    if (running) return;
    setRunning(true);
    let didUpload = false;
    for (let i = 0; i < items.length; i++) {
      if (items[i].status !== "queued") continue;
      setItems((p) => updateAt(p, i, { status: "uploading" }));
      try {
        const dest = folderId
          ? { folderId } as const
          : { remotePath: "/" } as const;
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
      title={<><Upload size={14} /> Upload to <em className="font-mono">{folderName}</em></>}
      onClose={onClose}
      wide
    >
      <div
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault(); setDragOver(false);
          if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
        }}
        onClick={() => fileInput.current?.click()}
        className={cn(
          "flex cursor-pointer flex-col items-center justify-center rounded-md border-2 border-dashed py-8 text-sm",
          dragOver
            ? "border-accent bg-accent-subtle text-accent"
            : "border-border bg-bg-base text-fg-muted hover:border-accent",
        )}
      >
        <Upload size={20} className="mb-2" />
        <p>Drop files here, or click to browse.</p>
        <input ref={fileInput} type="file" multiple className="hidden"
               onChange={(e) => e.target.files && addFiles(e.target.files)} />
      </div>

      <div className="mt-3 flex items-center gap-2 text-xs">
        <span className="text-fg-muted">On conflict:</span>
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
            {p}
          </button>
        ))}
      </div>

      {items.length > 0 && (
        <ul className="mt-3 max-h-64 space-y-1 overflow-y-auto rounded-md border border-border bg-bg-subtle p-2 text-xs">
          {items.map((it, i) => <UploadRow key={i} item={it} />)}
        </ul>
      )}

      <div className="mt-4 flex justify-end gap-2">
        <button onClick={onClose}
                className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-bg-muted">
          Close
        </button>
        <button
          onClick={start}
          disabled={running || items.every((it) => it.status !== "queued")}
          className={cn(
            "flex items-center gap-1 rounded-md px-3 py-1.5 text-sm font-medium",
            running || items.every((it) => it.status !== "queued")
              ? "cursor-not-allowed bg-bg-muted text-fg-subtle"
              : "bg-accent text-accent-fg hover:opacity-90",
          )}
        >
          {running && <Loader2 size={12} className="animate-spin" />}
          {running ? "Uploading…" : "Start"}
        </button>
      </div>
    </ModalShell>
  );
}

function UploadRow({ item }: { item: UploadItem }) {
  const pct = item.file.size > 0 ? Math.round((item.loaded / item.file.size) * 100) : 0;
  return (
    <li className="flex items-center gap-2">
      <span className="flex-1 truncate">{item.file.name}</span>
      <span className="w-12 text-right text-fg-subtle">
        {item.status === "uploading" ? `${pct}%`
         : item.status === "done" ? "✓"
         : item.status === "error" ? "!"
         : "—"}
      </span>
      {item.renamedTo && (
        <span className="truncate text-fg-subtle" title={`renamed → ${item.renamedTo}`}>
          → {item.renamedTo}
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

function ModalShell({ title, onClose, children, wide }: {
  title: React.ReactNode;
  onClose: () => void;
  children: React.ReactNode;
  wide?: boolean;
}) {
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
                  className="rounded-md p-1 text-fg-muted hover:bg-bg-muted">
            <X size={14} />
          </button>
        </header>
        <div className="p-4">{children}</div>
      </div>
    </div>
  );
}
