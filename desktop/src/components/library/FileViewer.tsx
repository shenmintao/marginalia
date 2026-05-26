/** Renders the body of a file entry. Tier 1 (browser-native) + DOCX:
 *
 *    PDF   → <iframe> the download URL; browser's PDF reader takes over
 *    image → <img>
 *    md    → react-markdown
 *    text  → react-syntax-highlighter (or <pre> for very large files)
 *    docx  → mammoth.js converts to HTML in the worker thread
 *    other → "Preview not available — open in default app" + download link
 *
 *  We rely on the entry's display_name extension and metadata.mime_type
 *  to decide. Both are best-effort — Marginalia's mime detection is
 *  loose, so we fall back to extension when in doubt.
 */
import { useEffect, useMemo, useState } from "react";
import { FileText, Download, AlertCircle, Loader2 } from "lucide-react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { vscDarkPlus, prism } from "react-syntax-highlighter/dist/esm/styles/prism";

import { fileEntries } from "@/api/client";
import { MarkdownView } from "@/components/MarkdownView";
import type { FileMetadata } from "@/types/api";
import { useTheme } from "@/lib/theme";

interface Props {
  entryId: string;
  meta: FileMetadata | null;
}

type Kind = "pdf" | "image" | "md" | "text" | "code" | "docx" | "binary";

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

function classifyByName(name: string): Kind {
  const ext = (name.split(".").pop() || "").toLowerCase();
  if (ext === "pdf") return "pdf";
  if (["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"].includes(ext)) return "image";
  if (ext === "md" || ext === "markdown") return "md";
  if (ext === "docx") return "docx";
  if (CODE_EXT_TO_LANG[ext]) return "code";
  if (TEXT_EXT.has(ext)) return "text";
  return "binary";
}

export function FileViewer({ entryId, meta }: Props) {
  const name = meta?.display_name || "";
  const kind = useMemo<Kind>(() => classifyByName(name), [name]);
  const contentUrl = fileEntries.contentUrl(entryId);
  const downloadUrl = fileEntries.downloadUrl(entryId);

  return (
    <div className="flex h-full min-w-0 flex-1 flex-col">
      <header className="flex shrink-0 items-center gap-2 border-b border-border bg-bg-subtle px-4 py-2 text-sm">
        <FileText size={14} className="text-fg-muted" />
        <span className="flex-1 truncate font-medium">{name || "—"}</span>
        <a href={downloadUrl} download className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs hover:bg-bg-muted">
          <Download size={12} /> Download
        </a>
      </header>
      <div className="flex-1 overflow-hidden">
        {kind === "pdf" && <PdfView url={contentUrl} />}
        {kind === "image" && <ImageView url={contentUrl} />}
        {kind === "md" && <MdView url={contentUrl} />}
        {kind === "text" && <TextView url={contentUrl} />}
        {kind === "code" && (
          <CodeView url={contentUrl}
                    lang={CODE_EXT_TO_LANG[(name.split(".").pop() || "").toLowerCase()] || "text"} />
        )}
        {kind === "docx" && <DocxView url={contentUrl} />}
        {kind === "binary" && <BinaryView url={downloadUrl} name={name} />}
      </div>
    </div>
  );
}

function PdfView({ url }: { url: string }) {
  return <iframe src={url} className="h-full w-full border-0" title="pdf" />;
}

function ImageView({ url }: { url: string }) {
  return (
    <div className="flex h-full items-center justify-center overflow-auto bg-bg-subtle p-4">
      <img src={url} className="max-h-full max-w-full object-contain" alt="" />
    </div>
  );
}

function useTextResource(url: string, maxBytes = 2_000_000) {
  const [text, setText] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  useEffect(() => {
    let cancelled = false;
    setText(null); setErr(null); setTruncated(false);
    fetch(url)
      .then(async (r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
        const buf = await r.arrayBuffer();
        const slice = buf.byteLength > maxBytes ? buf.slice(0, maxBytes) : buf;
        const t = new TextDecoder("utf-8", { fatal: false }).decode(slice);
        if (!cancelled) {
          setText(t);
          setTruncated(buf.byteLength > maxBytes);
        }
      })
      .catch((e) => { if (!cancelled) setErr(e.message); });
    return () => { cancelled = true; };
  }, [url, maxBytes]);
  return { text, err, truncated };
}

function MdView({ url }: { url: string }) {
  const { text, err } = useTextResource(url);
  if (err) return <ViewerError msg={err} />;
  if (text === null) return <ViewerLoading />;
  return (
    <div className="h-full overflow-auto px-6 py-4">
      <div className="mx-auto max-w-3xl">
        <MarkdownView content={text} />
      </div>
    </div>
  );
}

function TextView({ url }: { url: string }) {
  const { text, err, truncated } = useTextResource(url);
  if (err) return <ViewerError msg={err} />;
  if (text === null) return <ViewerLoading />;
  return (
    <div className="h-full overflow-auto px-4 py-3">
      {truncated && <TruncatedBanner />}
      <pre className="whitespace-pre-wrap break-all font-mono text-xs">{text}</pre>
    </div>
  );
}

function CodeView({ url, lang }: { url: string; lang: string }) {
  const { text, err, truncated } = useTextResource(url);
  const { effective } = useTheme();
  if (err) return <ViewerError msg={err} />;
  if (text === null) return <ViewerLoading />;
  return (
    <div className="h-full overflow-auto">
      {truncated && <TruncatedBanner />}
      <SyntaxHighlighter
        language={lang}
        style={effective === "dark" ? vscDarkPlus : prism}
        customStyle={{ margin: 0, padding: "12px 16px", fontSize: 12 }}
        showLineNumbers
        wrapLongLines={false}
      >
        {text}
      </SyntaxHighlighter>
    </div>
  );
}

function DocxView({ url }: { url: string }) {
  const [html, setHtml] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    setHtml(null); setErr(null);
    (async () => {
      try {
        const buf = await (await fetch(url)).arrayBuffer();
        const mammoth = await import("mammoth");
        const r = await mammoth.convertToHtml({ arrayBuffer: buf });
        if (!cancelled) setHtml(r.value);
      } catch (e) {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => { cancelled = true; };
  }, [url]);
  if (err) return <ViewerError msg={err} />;
  if (html === null) return <ViewerLoading />;
  return (
    <div className="h-full overflow-auto px-6 py-4">
      <div className="prose-marginalia mx-auto max-w-3xl"
           dangerouslySetInnerHTML={{ __html: html }} />
    </div>
  );
}

function BinaryView({ url, name }: { url: string; name: string }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center text-sm text-fg-muted">
      <FileText size={32} className="text-fg-subtle" />
      <p>Preview not available for this file type.</p>
      <a href={url} download={name}
         className="flex items-center gap-1 rounded-md border border-border bg-bg-subtle px-3 py-1.5 text-xs hover:bg-bg-muted">
        <Download size={12} /> Download
      </a>
    </div>
  );
}

function ViewerLoading() {
  return (
    <div className="flex h-full items-center justify-center text-sm text-fg-muted">
      <Loader2 size={14} className="mr-2 animate-spin" /> loading…
    </div>
  );
}
function ViewerError({ msg }: { msg: string }) {
  return (
    <div className="flex h-full items-center justify-center p-4 text-sm text-danger">
      <AlertCircle size={14} className="mr-2" /> {msg}
    </div>
  );
}
function TruncatedBanner() {
  return (
    <div className="border-b border-border bg-bg-subtle px-3 py-1 text-[11px] text-fg-subtle">
      File truncated to first 2 MB for preview. Download for full content.
    </div>
  );
}
