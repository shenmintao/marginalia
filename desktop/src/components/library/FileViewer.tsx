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
 *  Deep-link locators (`{kind: "quote"|"line"|"page", value}`) come from
 *  chat citations:
 *    quote → walk the rendered DOM, surround the matching text with a
 *            <mark>, scroll it into view, then unwrap after a flash
 *    line  → text/code views scroll to the target line range; markdown
 *            uses ratio approximation (legacy; stable_context no longer
 *            asks the LLM to write `lines=`, but historical sessions and
 *            manual deep-links still resolve)
 *    page  → PDF iframe receives `#page=N`
 */
import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { FileText, Download, AlertCircle, Loader2 } from "lucide-react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { vscDarkPlus, prism } from "react-syntax-highlighter/dist/esm/styles/prism";

import { fileEntries } from "@/api/client";
import { MarkdownView } from "@/components/MarkdownView";
import type { FileMetadata } from "@/types/api";
import { useTheme } from "@/lib/theme";
import { useI18n } from "@/lib/i18n";

export interface ViewerLocator {
  kind: "quote" | "line" | "page";
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

const DOCX_ALLOWED_TAGS = new Set([
  "A", "B", "BLOCKQUOTE", "BR", "CODE", "DD", "DIV", "DL", "DT", "EM",
  "H1", "H2", "H3", "H4", "H5", "H6", "HR", "I", "IMG", "LI", "OL",
  "P", "PRE", "S", "SPAN", "STRONG", "SUB", "SUP", "TABLE", "TBODY",
  "TD", "TH", "THEAD", "TR", "U", "UL",
]);

const DOCX_DROP_TAGS = new Set([
  "AUDIO", "BUTTON", "CANVAS", "EMBED", "FORM", "IFRAME", "INPUT",
  "LINK", "MATH", "META", "OBJECT", "SCRIPT", "SELECT", "STYLE", "SVG",
  "TEMPLATE", "TEXTAREA", "VIDEO",
]);

const DOCX_ALLOWED_ATTRS: Record<string, Set<string>> = {
  A: new Set(["href", "title"]),
  IMG: new Set(["src", "alt", "title"]),
  TD: new Set(["colspan", "rowspan"]),
  TH: new Set(["colspan", "rowspan"]),
};

function sanitizeDocxHtml(html: string): string {
  const template = document.createElement("template");
  template.innerHTML = html;

  const isSafeUrl = (value: string, opts?: { image?: boolean }): boolean => {
    const image = Boolean(opts?.image);
    const trimmed = value.trim();
    if (!trimmed) return false;
    if (
      trimmed.startsWith("#") ||
      (trimmed.startsWith("/") && !trimmed.startsWith("//")) ||
      trimmed.startsWith("./") ||
      trimmed.startsWith("../")
    ) {
      return true;
    }
    try {
      const url = new URL(trimmed, window.location.origin);
      if (url.protocol === "http:" || url.protocol === "https:") {
        return true;
      }
      if (!image && (url.protocol === "mailto:" || url.protocol === "tel:")) {
        return true;
      }
      if (image && url.protocol === "data:") {
        return /^data:image\/(png|jpeg|jpg|gif|webp);base64,/i.test(trimmed);
      }
    } catch {
      return false;
    }
    return false;
  };

  const unwrap = (el: Element): void => {
    const parent = el.parentNode;
    if (!parent) return;
    while (el.firstChild) {
      parent.insertBefore(el.firstChild, el);
    }
    parent.removeChild(el);
  };

  const scrubChildren = (root: ParentNode): void => {
    for (const child of Array.from(root.childNodes)) {
      scrub(child);
    }
  };

  const scrub = (node: Node): void => {
    if (node.nodeType === Node.COMMENT_NODE) {
      node.parentNode?.removeChild(node);
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) {
      return;
    }
    const el = node as HTMLElement;
    scrubChildren(el);
    if (!DOCX_ALLOWED_TAGS.has(el.tagName)) {
      if (DOCX_DROP_TAGS.has(el.tagName)) {
        el.parentNode?.removeChild(el);
      } else {
        unwrap(el);
      }
      return;
    }
    const allowed = DOCX_ALLOWED_ATTRS[el.tagName] || new Set<string>();
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name.toLowerCase();
      if (name.startsWith("on") || name === "style" || name === "srcdoc") {
        el.removeAttribute(attr.name);
        continue;
      }
      if (!allowed.has(name)) {
        el.removeAttribute(attr.name);
        continue;
      }
      if (el.tagName === "A" && name === "href" && !isSafeUrl(attr.value)) {
        el.removeAttribute(attr.name);
      }
      if (el.tagName === "IMG" && name === "src" && !isSafeUrl(attr.value, { image: true })) {
        el.removeAttribute(attr.name);
      }
    }
    if (el.tagName === "A") {
      el.setAttribute("rel", "noreferrer");
    }
  };

  scrubChildren(template.content);
  return template.innerHTML;
}

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
  const { t } = useI18n();
  const name = meta?.display_name || "";
  const kind = useMemo<Kind>(() => classifyByName(name), [name]);
  const contentUrl = fileEntries.contentUrl(entryId);
  const downloadUrl = fileEntries.downloadUrl(entryId);

  const lineLoc = locator?.kind === "line" ? parseLineRange(locator.value) : null;
  const pageLoc = locator?.kind === "page" ? parseInt(locator.value, 10) : null;
  const quoteLoc = locator?.kind === "quote" ? locator.value : null;
  // PDF reads its locator straight from the URL fragment, so consume it
  // immediately. Text-family viewers consume it after their scroll runs.
  useEffect(() => {
    if (kind === "pdf" && locator && onLocatorConsumed) onLocatorConsumed();
  }, [kind, locator, onLocatorConsumed]);

  return (
    <div className="flex h-full min-w-0 flex-1 flex-col">
      <header className="flex shrink-0 items-center gap-2 border-b border-border bg-bg-subtle px-4 py-2 text-sm">
        <FileText size={14} className="text-fg-muted" />
        <span className="flex-1 truncate font-medium">{name || t.common.unset}</span>
        <a href={downloadUrl} download className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs hover:bg-bg-muted">
          <Download size={12} /> {t.library.download}
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
        {kind === "docx" && (
          <DocxView
            url={contentUrl}
            quote={quoteLoc}
            onScrolled={onLocatorConsumed}
          />
        )}
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
  //
  // Pin the page in a ref: FileViewer clears the locator immediately
  // after first commit, so on the next render `page` flips to null and
  // the iframe `src` would lose its `#page=N` fragment, reloading the
  // viewer back to page 1. Refresh the pin when `url` changes (different
  // file) or when a fresh non-null page arrives for the same file (the
  // user clicked a second citation into the same PDF).
  const pageRef = useRef<number | null>(page);
  const urlRef = useRef(url);
  if (urlRef.current !== url) {
    urlRef.current = url;
    pageRef.current = page;
  } else if (page != null) {
    pageRef.current = page;
  }
  const p = pageRef.current;
  const src = p ? `${url}#page=${p}` : url;
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
        const t = decodeTextBytes(slice, r.headers.get("content-type") || "");
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

function decodeTextBytes(buf: ArrayBuffer, contentType: string): string {
  const bytes = new Uint8Array(buf);
  if (bytes.length >= 3 && bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) {
    return stripLeadingBom(new TextDecoder("utf-8").decode(bytes.subarray(3)));
  }
  if (bytes.length >= 2 && bytes[0] === 0xff && bytes[1] === 0xfe) {
    return stripLeadingBom(decodeWith("utf-16le", bytes.subarray(2)) ?? "");
  }
  if (bytes.length >= 2 && bytes[0] === 0xfe && bytes[1] === 0xff) {
    return stripLeadingBom(decodeWith("utf-16be", bytes.subarray(2)) ?? "");
  }

  const charset = parseCharset(contentType);
  if (charset) {
    const decoded = decodeWith(charset, bytes);
    if (decoded != null) return stripLeadingBom(decoded);
  }

  const utf8 = decodeUtf8Strict(bytes);
  if (utf8 != null) return stripLeadingBom(utf8);

  const utf16 = decodeLikelyUtf16(bytes);
  if (utf16 != null) return stripLeadingBom(utf16);

  // Common for Chinese Markdown edited by Windows-era tools. Only tried
  // after strict UTF-8 fails, so normal UTF-8 content keeps the fast path.
  const gb18030 = decodeWith("gb18030", bytes);
  if (gb18030 != null) return stripLeadingBom(gb18030);

  return stripLeadingBom(new TextDecoder("utf-8", { fatal: false }).decode(bytes));
}

function parseCharset(contentType: string): string | null {
  const m = /(?:^|;)\s*charset\s*=\s*"?([^";]+)"?/i.exec(contentType);
  if (!m) return null;
  const label = m[1].trim().toLowerCase();
  return label === "utf8" ? "utf-8" : label;
}

function decodeWith(label: string, bytes: Uint8Array): string | null {
  try {
    return new TextDecoder(label, { fatal: false }).decode(bytes);
  } catch {
    return null;
  }
}

function decodeUtf8Strict(bytes: Uint8Array): string | null {
  for (let trim = 0; trim <= 3 && trim < bytes.length; trim += 1) {
    try {
      const view = trim === 0 ? bytes : bytes.subarray(0, bytes.length - trim);
      return new TextDecoder("utf-8", { fatal: true }).decode(view);
    } catch {
      /* A truncated preview can cut a multi-byte char; trim and retry. */
    }
  }
  return null;
}

function decodeLikelyUtf16(bytes: Uint8Array): string | null {
  const sample = bytes.subarray(0, Math.min(bytes.length, 1024));
  if (sample.length < 8) return null;
  let evenNulls = 0;
  let oddNulls = 0;
  for (let i = 0; i < sample.length; i += 1) {
    if (sample[i] !== 0) continue;
    if (i % 2 === 0) evenNulls += 1;
    else oddNulls += 1;
  }
  const pairs = Math.floor(sample.length / 2);
  if (oddNulls > pairs / 3) return decodeWith("utf-16le", bytes);
  if (evenNulls > pairs / 3) return decodeWith("utf-16be", bytes);
  return null;
}

function stripLeadingBom(text: string): string {
  return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
}

interface JumpProps {
  quote: string | null;
  lineRange: { start: number; end: number } | null;
  onScrolled?: () => void;
}

function MdView({ url, quote, lineRange, onScrolled }:
  { url: string } & JumpProps,
) {
  const { text, err } = useTextResource(url);
  const containerRef = useRef<HTMLDivElement>(null);
  const quoteState = useQuoteJump(containerRef, text, quote, onScrolled);
  // Quote takes precedence; lineRange only fires when no quote.
  const { flashKey: lineFlash } = useLineRatioJump(
    containerRef, text, quote ? null : lineRange, onScrolled,
  );
  if (err) return <ViewerError msg={err} />;
  if (text === null) return <ViewerLoading />;
  return (
    <div className="h-full overflow-auto px-6 py-4" ref={containerRef}>
      <div className="mx-auto max-w-3xl">
        {quoteState.banner}
        {quoteState.banner == null && lineFlash != null && lineRange && (
          <LocatorBanner kind="line" range={lineRange} />
        )}
        <MarkdownView content={text} />
      </div>
    </div>
  );
}

function TextView({ url, quote, lineRange, onScrolled }:
  { url: string } & JumpProps,
) {
  const { text, err, truncated } = useTextResource(url);
  if (err) return <ViewerError msg={err} />;
  if (text === null) return <ViewerLoading />;
  return (
    <PlainTextLines
      text={text}
      quote={quote}
      lineRange={lineRange}
      truncated={truncated}
      onScrolled={onScrolled}
    />
  );
}

function CodeView({ url, lang, quote, lineRange, onScrolled }:
  { url: string; lang: string } & JumpProps,
) {
  const { text, err, truncated } = useTextResource(url);
  const { effective } = useTheme();
  const containerRef = useRef<HTMLDivElement>(null);
  const [lineFlash, setLineFlash] = useState<number | null>(null);
  const quoteState = useQuoteJump(containerRef, text, quote, onScrolled);

  useEffect(() => {
    if (quote) return;  // quote path owns the highlight
    if (!text || !lineRange) return;
    const root = containerRef.current;
    if (!root) return;
    const lineEls = root.querySelectorAll<HTMLElement>(".react-syntax-highlighter-line-number");
    const target = lineEls[lineRange.start - 1]?.parentElement;
    if (target) {
      target.scrollIntoView({ block: "center", behavior: "smooth" });
      setLineFlash(Date.now());
      onScrolled?.();
      const t = window.setTimeout(() => setLineFlash(null), 1600);
      return () => window.clearTimeout(t);
    }
  }, [text, lineRange, quote, onScrolled]);

  if (err) return <ViewerError msg={err} />;
  if (text === null) return <ViewerLoading />;
  return (
    <div className="h-full overflow-auto" ref={containerRef}>
      {truncated && <TruncatedBanner />}
      {quoteState.banner}
      {quoteState.banner == null && lineFlash != null && lineRange && (
        <LocatorBanner kind="line" range={lineRange} />
      )}
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
  text, quote, lineRange, truncated, onScrolled,
}: {
  text: string;
  quote: string | null;
  lineRange: { start: number; end: number } | null;
  truncated: boolean;
  onScrolled?: () => void;
}) {
  const lines = useMemo(() => text.split("\n"), [text]);
  const containerRef = useRef<HTMLDivElement>(null);
  const targetRef = useRef<HTMLDivElement>(null);
  const [lineFlash, setLineFlash] = useState<number | null>(null);
  const quoteState = useQuoteJump(containerRef, text, quote, onScrolled);

  useEffect(() => {
    if (quote) return;
    if (!lineRange || !targetRef.current) return;
    targetRef.current.scrollIntoView({ block: "center", behavior: "smooth" });
    setLineFlash(Date.now());
    onScrolled?.();
    const t = window.setTimeout(() => setLineFlash(null), 1600);
    return () => window.clearTimeout(t);
  }, [lineRange, quote, onScrolled]);

  return (
    <div className="h-full overflow-auto" ref={containerRef}>
      {truncated && <TruncatedBanner />}
      {quoteState.banner}
      {quoteState.banner == null && lineFlash != null && lineRange && (
        <LocatorBanner kind="line" range={lineRange} />
      )}
      <pre className="whitespace-pre-wrap break-all px-4 py-3 font-mono text-xs">
        {lines.map((ln, i) => {
          const lineNo = i + 1;
          const inRange =
            !quote && lineRange && lineNo >= lineRange.start && lineNo <= lineRange.end;
          const isStart = !quote && lineRange && lineNo === lineRange.start;
          return (
            <div
              key={i}
              ref={isStart ? targetRef : undefined}
              className={
                inRange && lineFlash != null
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
// per-line wrappers like the plain-text viewer does. Approximate via
// scrollHeight ratio. Kept for legacy `?line=` deep-links; quote jump
// is preferred (and stable_context no longer asks the LLM for line=).
function useLineRatioJump(
  ref: RefObject<HTMLDivElement>,
  text: string | null,
  lineRange: { start: number; end: number } | null,
  onScrolled?: () => void,
) {
  const [flashKey, setFlashKey] = useState<number | null>(null);
  useEffect(() => {
    if (!text || !lineRange || !ref.current) return;
    const total = text.split("\n").length || 1;
    const ratio = Math.max(0, (lineRange.start - 1) / total);
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
  }, [text, lineRange, onScrolled, ref]);
  return { flashKey };
}

// Quote-based jump: walk the rendered text DOM, surround the first text
// node containing the quote with a <mark>, scroll it into view, then
// unwrap after a 1.6 s flash. First-version heuristic — only matches
// against a single text node, so quotes that get split by inline
// formatting (e.g. <strong> mid-sentence in DOCX) won't match.
// `content` is the source string we render — used as the dep so the
// effect re-runs once the DOM has the new content.
function useQuoteJump(
  ref: RefObject<HTMLElement>,
  content: string | null,
  quote: string | null,
  onScrolled?: () => void,
) {
  const [hit, setHit] = useState<"found" | "missing" | null>(null);
  useEffect(() => {
    if (!quote || !content || !ref.current) {
      setHit(null);
      return;
    }
    const root = ref.current;
    let marks: HTMLElement[] = [];
    let cleanup: number | null = null;
    let handle2: number | null = null;
    // rAF to wait for layout to settle (mammoth-rendered HTML, syntax
    // highlighting, KaTeX). Two frames is enough for prism + react-markdown.
    const handle1 = window.requestAnimationFrame(() => {
      handle2 = window.requestAnimationFrame(() => {
        marks = highlightQuoteInDom(root, quote);
        if (marks.length > 0) {
          marks[0].scrollIntoView({ block: "center", behavior: "smooth" });
          setHit("found");
          cleanup = window.setTimeout(() => {
            marks.forEach(unwrapMark);
          }, 1600);
        } else {
          setHit("missing");
        }
        onScrolled?.();
      });
    });
    return () => {
      window.cancelAnimationFrame(handle1);
      if (handle2 != null) window.cancelAnimationFrame(handle2);
      if (cleanup != null) window.clearTimeout(cleanup);
      marks.forEach(unwrapMark);
    };
  }, [content, quote, onScrolled, ref]);
  const banner = quote && hit === "found"
    ? <LocatorBanner kind="quote" quote={quote} />
    : quote && hit === "missing"
      ? <LocatorBanner kind="quote-missing" quote={quote} />
      : null;
  return { banner };
}

function highlightQuoteInDom(root: HTMLElement, quote: string): HTMLElement[] {
  const exact = highlightExactQuoteInDom(root, quote);
  if (exact.length > 0) return exact;
  return highlightNormalizedQuoteInDom(root, quote);
}

function highlightExactQuoteInDom(root: HTMLElement, quote: string): HTMLElement[] {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const tn = node as Text;
    const text = tn.data;
    const idx = text.indexOf(quote);
    if (idx === -1) continue;
    const range = document.createRange();
    range.setStart(tn, idx);
    range.setEnd(tn, idx + quote.length);
    const mark = document.createElement("mark");
    mark.className = "rounded bg-accent/30 px-0.5";
    range.surroundContents(mark);
    return [mark];
  }
  return [];
}

interface SearchChar {
  node: Text;
  offset: number;
}

interface TextSegment {
  node: Text;
  start: number;
  end: number;
}

function highlightNormalizedQuoteInDom(root: HTMLElement, quote: string): HTMLElement[] {
  const needle = normalizeSearchText(quote);
  if (needle.length < 3) return [];
  const index = buildDomSearchIndex(root);
  const start = index.text.indexOf(needle);
  if (start === -1) return [];
  const end = start + needle.length;
  const segments = segmentsForMatch(index.map.slice(start, end));
  const marks: HTMLElement[] = [];
  for (let i = segments.length - 1; i >= 0; i -= 1) {
    const mark = wrapTextSegment(segments[i]);
    if (mark) marks.unshift(mark);
  }
  return marks;
}

function buildDomSearchIndex(root: HTMLElement): { text: string; map: SearchChar[] } {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let text = "";
  const map: SearchChar[] = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const tn = node as Text;
    for (let i = 0; i < tn.data.length; i += 1) {
      const normalized = normalizeSearchChar(tn.data[i]);
      for (const ch of normalized) {
        text += ch;
        map.push({ node: tn, offset: i });
      }
    }
  }
  return { text, map };
}

function normalizeSearchText(value: string): string {
  let out = "";
  for (let i = 0; i < value.length; i += 1) {
    out += normalizeSearchChar(value[i]);
  }
  return out;
}

function normalizeSearchChar(value: string): string {
  let out = "";
  for (const ch of value.normalize("NFKC").toLowerCase()) {
    if (/[\p{L}\p{N}_]/u.test(ch)) out += ch;
  }
  return out;
}

function segmentsForMatch(chars: SearchChar[]): TextSegment[] {
  const segments: TextSegment[] = [];
  for (const ch of chars) {
    const last = segments[segments.length - 1];
    if (last && last.node === ch.node && ch.offset <= last.end) {
      last.end = Math.max(last.end, ch.offset + 1);
      continue;
    }
    segments.push({ node: ch.node, start: ch.offset, end: ch.offset + 1 });
  }
  return segments;
}

function wrapTextSegment(segment: TextSegment): HTMLElement | null {
  if (segment.start >= segment.end) return null;
  try {
    const range = document.createRange();
    range.setStart(segment.node, segment.start);
    range.setEnd(segment.node, segment.end);
    const mark = document.createElement("mark");
    mark.className = "rounded bg-accent/30 px-0.5";
    range.surroundContents(mark);
    return mark;
  } catch {
    return null;
  }
}

function unwrapMark(mark: HTMLElement | null) {
  if (!mark || !mark.parentNode) return;
  const parent = mark.parentNode;
  while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
  parent.removeChild(mark);
  // Re-merge adjacent text nodes to keep the DOM tidy.
  parent.normalize?.();
}

function LocatorBanner(
  props:
    | { kind: "line"; range: { start: number; end: number } }
    | { kind: "quote"; quote: string }
    | { kind: "quote-missing"; quote: string },
) {
  const { t } = useI18n();
  if (props.kind === "line") {
    const span = props.range.start === props.range.end
      ? t.library.line(props.range.start)
      : t.library.lines(props.range.start, props.range.end);
    return (
      <div className="sticky top-0 z-10 border-b border-accent/30 bg-accent/10 px-3 py-1 text-[11px] font-mono text-accent">
        {t.library.jumpedLine(span)}
      </div>
    );
  }
  const head = props.quote.length > 30 ? props.quote.slice(0, 30) + "..." : props.quote;
  if (props.kind === "quote-missing") {
    return (
      <div className="sticky top-0 z-10 border-b border-warning/30 bg-warning/10 px-3 py-1 text-[11px] text-warning">
        {t.library.quoteMissing(head)}
      </div>
    );
  }
  return (
    <div className="sticky top-0 z-10 border-b border-accent/30 bg-accent/10 px-3 py-1 text-[11px] text-accent">
      {t.library.quoteJumped(head)}
    </div>
  );
}

function DocxView({ url, quote, onScrolled }: {
  url: string;
  quote: string | null;
  onScrolled?: () => void;
}) {
  const [html, setHtml] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const quoteState = useQuoteJump(containerRef, html, quote, onScrolled);
  useEffect(() => {
    let cancelled = false;
    setHtml(null); setErr(null);
    (async () => {
      try {
        const buf = await (await fetch(url)).arrayBuffer();
        const mammoth = await import("mammoth");
        const r = await mammoth.convertToHtml({ arrayBuffer: buf });
        if (!cancelled) setHtml(sanitizeDocxHtml(r.value));
      } catch (e) {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => { cancelled = true; };
  }, [url]);
  if (err) return <ViewerError msg={err} />;
  if (html === null) return <ViewerLoading />;
  return (
    <div className="h-full overflow-auto px-6 py-4" ref={containerRef}>
      {quoteState.banner}
      <div className="prose-marginalia mx-auto max-w-3xl"
           dangerouslySetInnerHTML={{ __html: html }} />
    </div>
  );
}

function BinaryView({ url, name }: { url: string; name: string }) {
  const { t } = useI18n();
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center text-sm text-fg-muted">
      <FileText size={32} className="text-fg-subtle" />
      <p>{t.library.previewUnavailable}</p>
      <a href={url} download={name}
         className="flex items-center gap-1 rounded-md border border-border bg-bg-subtle px-3 py-1.5 text-xs hover:bg-bg-muted">
        <Download size={12} /> {t.library.download}
      </a>
    </div>
  );
}

function ViewerLoading() {
  const { t } = useI18n();
  return (
    <div className="flex h-full items-center justify-center text-sm text-fg-muted">
      <Loader2 size={14} className="mr-2 animate-spin" /> {t.common.loading}
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
  const { t } = useI18n();
  return (
    <div className="border-b border-border bg-bg-subtle px-3 py-1 text-[11px] text-fg-subtle">
      {t.library.truncatedPreview}
    </div>
  );
}
