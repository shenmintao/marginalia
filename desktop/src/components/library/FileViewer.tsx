import { useEffect, useMemo, useState } from "react";
import { FileText, Download, Cloud, Loader2 } from "lucide-react";

import { fileEntries, webdavSync } from "@/api/client";
import type { FileMetadata } from "@/types/api";
import { useI18n } from "@/lib/i18n";
import {
  ArchiveView,
  BinaryView,
  CodeView,
  EpubView,
  ExtractedMarkdownView,
  ImageView,
  MdView,
  OfficeDocumentView,
  PdfView,
  TextView,
} from "./FileViewerViews";

export interface ViewerLocator {
  kind: "quote" | "line" | "page";
  value: string;
}

interface Props {
  entryId: string;
  meta: FileMetadata | null;
  locator?: ViewerLocator | null;
  onLocatorConsumed?: () => void;
  onHydrated?: () => void;
}

type Kind = "pdf" | "image" | "md" | "text" | "code" | "docx" | "xlsx" | "pptx" | "epub" | "email" | "archive" | "binary";
type OfficeKind = "docx" | "xlsx" | "pptx";

const TEXT_EXT = new Set([
  "txt", "log", "csv", "tsv", "ini", "conf", "env", "sql", "rst",
]);
const CODE_EXT_TO_LANG: Record<string, string> = {
  ts: "typescript", tsx: "tsx", js: "javascript", jsx: "jsx",
  py: "python", rb: "ruby", go: "go", rs: "rust", java: "java",
  c: "c", h: "c", cpp: "cpp", hpp: "cpp",
  json: "json", yaml: "yaml", yml: "yaml", toml: "toml",
  html: "html", css: "css", scss: "scss",
  sh: "bash", bash: "bash", zsh: "bash", ps1: "powershell",
  md: "markdown",
};
const ARCHIVE_EXT = new Set([
  "zip", "tar", "tgz", "gz", "bz2", "xz", "lzma", "7z", "rar", "iso", "cab",
]);

function classifyByName(name: string): Kind {
  const lower = name.toLowerCase();
  const ext = (name.split(".").pop() || "").toLowerCase();
  if (ext === "pdf") return "pdf";
  if (["avif", "png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "tif", "tiff", "heic", "heif"].includes(ext)) return "image";
  if (ext === "md" || ext === "markdown") return "md";
  if (ext === "docx") return "docx";
  if (ext === "xlsx" || ext === "xlsm") return "xlsx";
  if (ext === "pptx" || ext === "pptm") return "pptx";
  if (ext === "epub") return "epub";
  if (ext === "eml" || ext === "msg") return "email";
  if (
    ARCHIVE_EXT.has(ext)
    || [".tar.gz", ".tar.bz2", ".tar.xz"].some((suffix) => lower.endsWith(suffix))
  ) return "archive";
  if (CODE_EXT_TO_LANG[ext]) return "code";
  if (TEXT_EXT.has(ext)) return "text";
  return "binary";
}

function isOfficeKind(kind: Kind): kind is OfficeKind {
  return kind === "docx" || kind === "xlsx" || kind === "pptx";
}

function parseLineRange(value: string): { start: number; end: number } | null {
  const m = /^(\d+)(?:-(\d+))?$/.exec(value.trim());
  if (!m) return null;
  const start = parseInt(m[1], 10);
  const end = m[2] ? parseInt(m[2], 10) : start;
  if (!Number.isFinite(start) || !Number.isFinite(end) || start < 1) return null;
  return { start, end: Math.max(start, end) };
}

export function FileViewer({ entryId, meta, locator, onLocatorConsumed, onHydrated }: Props) {
  const { t } = useI18n();
  const [hydrating, setHydrating] = useState(false);
  const [hydrateError, setHydrateError] = useState<string | null>(null);
  const name = meta?.display_name || "";
  const kind = useMemo<Kind>(() => classifyByName(name), [name]);
  const contentUrl = fileEntries.contentUrl(entryId);
  const downloadUrl = fileEntries.downloadUrl(entryId);
  const remote = meta?.webdav_remote;
  const needsHydrate = Boolean(remote && !remote.hydrated);

  const lineLoc = locator?.kind === "line" ? parseLineRange(locator.value) : null;
  const pageLoc = locator?.kind === "page" ? parseInt(locator.value, 10) : null;
  const quoteLoc = locator?.kind === "quote" ? locator.value : null;

  useEffect(() => {
    if (kind === "pdf" && locator && onLocatorConsumed) onLocatorConsumed();
  }, [kind, locator, onLocatorConsumed]);

  return (
    <div className="flex h-full min-w-0 flex-1 flex-col">
      <header className="flex shrink-0 items-center gap-2 border-b border-border bg-bg-subtle px-4 py-2 text-sm">
        <FileText size={14} className="text-fg-muted" />
        <span className="flex-1 truncate font-medium">{name || t.common.unset}</span>
        {needsHydrate ? (
          <button
            type="button"
            disabled={hydrating}
            onClick={async () => {
              setHydrating(true);
              setHydrateError(null);
              try {
                await webdavSync.hydrate(entryId);
                onHydrated?.();
              } catch (e) {
                setHydrateError(e instanceof Error ? e.message : String(e));
              } finally {
                setHydrating(false);
              }
            }}
            className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs hover:bg-bg-muted disabled:opacity-50"
          >
            {hydrating ? <Loader2 size={12} className="animate-spin" /> : <Cloud size={12} />}
            {t.library.hydrateFromWebDav}
          </button>
        ) : (
          <a href={downloadUrl} download className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs hover:bg-bg-muted">
            <Download size={12} /> {t.library.download}
          </a>
        )}
      </header>
      <div className="flex-1 overflow-hidden">
        {needsHydrate && (
          <div className="flex h-full items-center justify-center p-8 text-center">
            <div className="max-w-sm">
              <Cloud className="mx-auto h-8 w-8 text-fg-muted" />
              <h2 className="mt-3 text-sm font-semibold">{t.library.remoteFileTitle}</h2>
              <p className="mt-1 text-sm text-fg-muted">{t.library.remoteFileBody}</p>
              {hydrateError && <p className="mt-3 text-xs text-danger">{hydrateError}</p>}
            </div>
          </div>
        )}
        {!needsHydrate && (
          <>
        {kind === "pdf" && (
          <PdfView
            url={contentUrl}
            page={Number.isFinite(pageLoc as number) ? (pageLoc as number) : null}
          />
        )}
        {kind === "image" && <ImageView url={contentUrl} name={name} sizeBytes={meta?.size_bytes} />}
        {kind === "md" && (
          <MdView
            url={contentUrl}
            quote={quoteLoc}
            lineRange={lineLoc}
            onScrolled={onLocatorConsumed}
          />
        )}
        {kind === "text" && (
          <TextView
            url={contentUrl}
            quote={quoteLoc}
            lineRange={lineLoc}
            onScrolled={onLocatorConsumed}
          />
        )}
        {kind === "code" && (
          <CodeView
            url={contentUrl}
            lang={CODE_EXT_TO_LANG[(name.split(".").pop() || "").toLowerCase()] || "text"}
            quote={quoteLoc}
            lineRange={lineLoc}
            onScrolled={onLocatorConsumed}
          />
        )}
        {isOfficeKind(kind) && (
          <OfficeDocumentView
            url={contentUrl}
            format={kind}
            name={name}
            downloadUrl={downloadUrl}
            quote={quoteLoc}
            page={Number.isFinite(pageLoc as number) ? (pageLoc as number) : null}
            onScrolled={onLocatorConsumed}
          />
        )}
        {kind === "epub" && (
          <EpubView
            url={contentUrl}
            name={name}
            downloadUrl={downloadUrl}
            page={Number.isFinite(pageLoc as number) ? (pageLoc as number) : null}
            onScrolled={onLocatorConsumed}
          />
        )}
        {kind === "email" && (
          <ExtractedMarkdownView
            entryId={entryId}
            quote={quoteLoc}
            lineRange={lineLoc}
            onScrolled={onLocatorConsumed}
          />
        )}
        {kind === "archive" && <ArchiveView url={downloadUrl} name={name} />}
        {kind === "binary" && <BinaryView url={downloadUrl} name={name} />}
          </>
        )}
      </div>
    </div>
  );
}
