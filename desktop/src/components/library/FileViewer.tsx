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
 *
 *  Deep-link locators (`{kind: "line"|"page", value}`) come from chat
 *  citations: text/markdown/code views scroll to the target line range
 *  and flash a highlight; PDF passes `#page=N` to the iframe so the
 *  browser PDF viewer jumps the page on load.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { FileText, Download, AlertCircle, Loader2 } from "lucide-react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { vscDarkPlus, prism } from "react-syntax-highlighter/dist/esm/styles/prism";

import { fileEntries } from "@/api/client";
import { MarkdownView } from "@/components/MarkdownView";
import type { FileMetadata } from "@/types/api";
import { useTheme } from "@/lib/theme";

export interface ViewerLocator {
  kind: "line" | "page";
  value: string;
}

interface Props {
  entryId: string;
  meta: FileMetadata | null;
  locator?: ViewerLocator | null;
  onLocatorConsumed?: () => void;
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

function parseLineRange(value: string): { start: number; end: number } | null {
  const m = /^(\d+)(?:-(\d+))?$/.exec(value.trim());
  if (!m) return null;
  const start = parseInt(m[1], 10);
  const end = m[2] ? parseInt(m[2], 10) : start;
  if (!Number.isFinite(start) || !Number.isFinite(end) || start < 1) return null;
  return { start, end: Math.max(start, end) };
}

export function FileViewer({ entryId, meta, locator, onLocatorConsumed }: Props) {
  const name = meta?.display_name || "";
  const kind = useMemo<Kind>(() => classifyByName(name), [name]);
  const contentUrl = fileEntries.contentUrl(entryId);
  const downloadUrl = fileEntries.downloadUrl(entryId);

  const lineLoc = locator?.kind === "line" ? parseLineRange(locator.value) : null;
  const pageLoc = locator?.kind === "page" ? parseInt(locator.value, 10) : null;
  // PDF reads its locator straight from the URL fragment, so consume it
  // immediately. Text-family viewers consume it after their scroll runs.
  useEffect(() => {
    if (kind === "pdf" && locator && onLocatorConsumed) onLocatorConsumed();
  }, [kind, locator, onLocatorConsumed]);

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
        {kind === "pdf" && (
          <PdfView
            url={contentUrl}
            page={Number.isFinite(pageLoc as number) ? (pageLoc as number) : null}
          />
        )}
        {kind === "image" && <ImageView url={contentUrl} />}
        {kind === "md" && (
          <MdView url={contentUrl} lineRange={lineLoc} onScrolled={onLocatorConsumed} />
        )}
        {kind === "text" && (
          <TextView url={contentUrl} lineRange={lineLoc} onScrolled={onLocatorConsumed} />
        )}
        {kind === "code" && (
          <CodeView
            url={contentUrl}
            lang={CODE_EXT_TO_LANG[(name.split(".").pop() || "").toLowerCase()] || "text"}
            lineRange={lineLoc}
            onScrolled={onLocatorConsumed}
          />
        )}
        {kind === "docx" && <DocxView url={contentUrl} />}
        {kind === "binary" && <BinaryView url={downloadUrl} name={name} />}
      </div>
    </div>
  );
}

function PdfView({ url, page }: { url: string; page: number | null }) {
  // The PDF Open Parameters spec lets us append `#page=N` to scroll the
  // browser viewer to a 1-indexed page. Works in Chrome, Firefox, and
  // Edge's built-in viewers — Safari historically ignores it but degrades
  // to "open at page 1", which is acceptable.
  const src = page ? `${url}#page=${page}` : url;
  return <iframe src={src} className="h-full w-full border-0" title="pdf" />;
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

interface LineJumpProps {
  lineRange: { start: number; end: number } | null;
  onScrolled?: () => void;
}

function MdView({ url, lineRange, onScrolled }:
  { url: string } & LineJumpProps,
) {
  const { text, err } = useTextResource(url);
  const { ref, flashKey } = useLineJumpForPlainBlock(text, lineRange, onScrolled);
  if (err) return <ViewerError msg={err} />;
  if (text === null) return <ViewerLoading />;
  return (
    <div className="h-full overflow-auto px-6 py-4" ref={ref}>
      <div className="mx-auto max-w-3xl">
        {flashKey != null && <LocatorBanner range={lineRange!} />}
        <MarkdownView content={text} />
      </div>
    </div>
  );
}

function TextView({ url, lineRange, onScrolled }:
  { url: string } & LineJumpProps,
) {
  const { text, err, truncated } = useTextResource(url);
  if (err) return <ViewerError msg={err} />;
  if (text === null) return <ViewerLoading />;
  return (
    <PlainTextLines
      text={text}
      lineRange={lineRange}
      truncated={truncated}
      onScrolled={onScrolled}
    />
  );
}

function CodeView({ url, lang, lineRange, onScrolled }:
  { url: string; lang: string } & LineJumpProps,
) {
  const { text, err, truncated } = useTextResource(url);
  const { effective } = useTheme();
  const containerRef = useRef<HTMLDivElement>(null);
  const [flashKey, setFlashKey] = useState<number | null>(null);

  useEffect(() => {
    if (!text || !lineRange) return;
    // SyntaxHighlighter wraps each line with showLineNumbers in a
    // `.linenumber` span. The line number value isn't a DOM id, so we
    // count children of the rendered code block and scroll the matching
    // index into view.
    const root = containerRef.current;
    if (!root) return;
    const lineEls = root.querySelectorAll<HTMLElement>(".react-syntax-highlighter-line-number");
    // showLineNumbers gives one .linenumber per line — its parent is the
    // line wrapper. Scroll the wrapper at index (start-1).
    const target = lineEls[lineRange.start - 1]?.parentElement;
    if (target) {
      target.scrollIntoView({ block: "center", behavior: "smooth" });
      setFlashKey(Date.now());
      onScrolled?.();
      const t = window.setTimeout(() => setFlashKey(null), 1600);
      return () => window.clearTimeout(t);
    }
  }, [text, lineRange, onScrolled]);

  if (err) return <ViewerError msg={err} />;
  if (text === null) return <ViewerLoading />;
  return (
    <div className="h-full overflow-auto" ref={containerRef}>
      {truncated && <TruncatedBanner />}
      {flashKey != null && lineRange && <LocatorBanner range={lineRange} />}
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

function PlainTextLines({
  text, lineRange, truncated, onScrolled,
}: {
  text: string;
  lineRange: { start: number; end: number } | null;
  truncated: boolean;
  onScrolled?: () => void;
}) {
  const lines = useMemo(() => text.split("\n"), [text]);
  const containerRef = useRef<HTMLDivElement>(null);
  const targetRef = useRef<HTMLDivElement>(null);
  const [flashKey, setFlashKey] = useState<number | null>(null);

  useEffect(() => {
    if (!lineRange || !targetRef.current) return;
    targetRef.current.scrollIntoView({ block: "center", behavior: "smooth" });
    setFlashKey(Date.now());
    onScrolled?.();
    const t = window.setTimeout(() => setFlashKey(null), 1600);
    return () => window.clearTimeout(t);
  }, [lineRange, onScrolled]);

  return (
    <div className="h-full overflow-auto" ref={containerRef}>
      {truncated && <TruncatedBanner />}
      {flashKey != null && lineRange && <LocatorBanner range={lineRange} />}
      <pre className="whitespace-pre-wrap break-all px-4 py-3 font-mono text-xs">
        {lines.map((ln, i) => {
          const lineNo = i + 1;
          const inRange =
            lineRange && lineNo >= lineRange.start && lineNo <= lineRange.end;
          const isStart = lineRange && lineNo === lineRange.start;
          return (
            <div
              key={i}
              ref={isStart ? targetRef : undefined}
              className={
                inRange && flashKey != null
                  ? "-mx-1 rounded bg-accent/15 px-1 transition-colors duration-1000"
                  : undefined
              }
            >
              {ln || "​"}
            </div>
          );
        })}
      </pre>
    </div>
  );
}

// Markdown reflows source lines, so we can't pin to "line 42" with
// per-line wrappers like the plain-text viewer does. Instead we approximate:
// scroll the container by the fraction (start / total) of its scrollHeight
// after the markdown renders. Inaccurate, but more useful than no jump,
// and we flag the range in a banner so the reader knows where to look.
function useLineJumpForPlainBlock(
  text: string | null,
  lineRange: { start: number; end: number } | null,
  onScrolled?: () => void,
) {
  const ref = useRef<HTMLDivElement>(null);
  const [flashKey, setFlashKey] = useState<number | null>(null);
  useEffect(() => {
    if (!text || !lineRange || !ref.current) return;
    const total = text.split("\n").length || 1;
    const ratio = Math.max(0, (lineRange.start - 1) / total);
    // Wait one rAF so KaTeX / syntax highlighting finishes layout, then
    // scroll. Without this, scrollHeight is the pre-render height.
    const handle = window.requestAnimationFrame(() => {
      const el = ref.current;
      if (!el) return;
      el.scrollTop = ratio * el.scrollHeight;
      setFlashKey(Date.now());
      onScrolled?.();
    });
    const t = window.setTimeout(() => setFlashKey(null), 1600);
    return () => {
      window.cancelAnimationFrame(handle);
      window.clearTimeout(t);
    };
  }, [text, lineRange, onScrolled]);
  return { ref, flashKey };
}

function LocatorBanner({ range }: { range: { start: number; end: number } }) {
  const span = range.start === range.end
    ? `line ${range.start}`
    : `lines ${range.start}–${range.end}`;
  return (
    <div className="sticky top-0 z-10 border-b border-accent/30 bg-accent/10 px-3 py-1 text-[11px] font-mono text-accent">
      jumped to {span}
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
