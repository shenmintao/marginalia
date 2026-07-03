import { useEffect, useRef, useState, type MutableRefObject, type ReactNode, type RefObject } from "react";
import { AlertCircle, Loader2 } from "lucide-react";

import { authHeaders, fileEntries, hasApiToken } from "@/api/client";
import type { FilePreviewText } from "@/types/api";
import { useI18n } from "@/lib/i18n";

export interface JumpProps {
  quote: string | null;
  lineRange: { start: number; end: number } | null;
  onScrolled?: () => void;
}

/** Resolve a backend content URL into something URL-based consumers
 *  (<img>, <iframe>, epubjs, @silurus/ooxml) can load when the API is
 *  token-protected: fetch the bytes with the Authorization header and
 *  hand back an object URL, revoked on unmount / url change. When no
 *  token is configured the original URL passes through untouched so the
 *  direct streaming path keeps working with zero overhead.
 *
 *  Pass `enabled: false` when the caller won't render the result (e.g.
 *  ImageView for formats it decodes itself) so token mode doesn't fetch
 *  the whole file into an unused blob. */
export function useAuthObjectUrl(
  url: string,
  enabled = true,
): { src: string | null; err: string | null } {
  const [state, setState] = useState<{ src: string | null; err: string | null }>(
    () => (!enabled || hasApiToken() ? { src: null, err: null } : { src: url, err: null }),
  );
  useEffect(() => {
    if (!enabled) {
      setState({ src: null, err: null });
      return;
    }
    if (!hasApiToken()) {
      setState({ src: url, err: null });
      return;
    }
    let cancelled = false;
    let objectUrl: string | null = null;
    setState({ src: null, err: null });
    fetch(url, { headers: authHeaders() })
      .then(async (r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
        const blob = await r.blob();
        objectUrl = URL.createObjectURL(blob);
        if (cancelled) {
          URL.revokeObjectURL(objectUrl);
          objectUrl = null;
          return;
        }
        setState({ src: objectUrl, err: null });
      })
      .catch((e) => {
        if (!cancelled) {
          setState({ src: null, err: e instanceof Error ? e.message : String(e) });
        }
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [url, enabled]);
  return state;
}

export function useTextResource(url: string, maxBytes = 2_000_000) {
  const [text, setText] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  useEffect(() => {
    let cancelled = false;
    setText(null); setErr(null); setTruncated(false);
    fetch(url, { headers: authHeaders() })
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

export function usePreviewText(entryId: string, maxChars = 2_000_000) {
  const [preview, setPreview] = useState<FilePreviewText | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    setPreview(null); setErr(null);
    fileEntries.previewText(entryId, maxChars)
      .then((result) => { if (!cancelled) setPreview(result); })
      .catch((e) => { if (!cancelled) setErr(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [entryId, maxChars]);
  return {
    text: preview?.text ?? null,
    err,
    truncated: preview?.truncated ?? false,
  };
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

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

export function clampInt(value: number, min: number, max: number): number {
  return Math.round(clamp(value, min, max));
}

export function isEditableEventTarget(target: EventTarget | null): boolean {
  const el = target instanceof HTMLElement ? target : null;
  return Boolean(el?.closest('input, textarea, select, [contenteditable="true"]'));
}

export interface ViewportZoomAnchor {
  pageIndex: number;
  relX: number;
  relY: number;
  clientX: number;
  clientY: number;
}

export const VIEWER_MIN_ZOOM = 0.45;
export const VIEWER_MAX_ZOOM = 2.5;
const VIEWER_ZOOM_STEPS = [0.45, 0.5, 0.67, 0.75, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5];

function nextViewportZoomStep(
  current: number,
  direction: -1 | 1,
  min = VIEWER_MIN_ZOOM,
  max = VIEWER_MAX_ZOOM,
): number {
  const steps = VIEWER_ZOOM_STEPS.filter((step) => step >= min && step <= max);
  if (direction > 0) {
    return steps.find((step) => step > current + 0.001) ?? max;
  }
  return [...steps].reverse().find((step) => step < current - 0.001) ?? min;
}

export function formatZoomPercent(zoom: number): string {
  return `${Math.round(zoom * 100)}%`;
}

export function useViewportWheelZoom(
  rootRef: RefObject<HTMLDivElement>,
  pagesRef: MutableRefObject<(HTMLDivElement | null)[]>,
  opts: {
    resetKey: string;
    min?: number;
    max?: number;
    applyZoom?: (zoom: number) => void;
    onWheelZoom?: () => void;
    onZoomingChange?: (zooming: boolean) => void;
    onZoomSettled?: (zoom: number) => void;
  },
) {
  const min = opts.min ?? VIEWER_MIN_ZOOM;
  const max = opts.max ?? VIEWER_MAX_ZOOM;
  const applyZoomRef = useRef(opts.applyZoom);
  const onWheelZoomRef = useRef(opts.onWheelZoom);
  const onZoomingChangeRef = useRef(opts.onZoomingChange);
  const onZoomSettledRef = useRef(opts.onZoomSettled);
  const zoomRef = useRef(1);
  const zoomingRef = useRef(false);
  const zoomFrameRef = useRef<number | null>(null);
  const pendingZoomRef = useRef(1);
  const pendingAnchorRef = useRef<ViewportZoomAnchor | null>(null);
  const zoomLabelFrameRef = useRef<number | null>(null);
  const zoomLabelValueRef = useRef(1);
  const zoomSettledRef = useRef<number | null>(null);
  const [zoom, setZoom] = useState(1);

  useEffect(() => {
    applyZoomRef.current = opts.applyZoom;
    onWheelZoomRef.current = opts.onWheelZoom;
    onZoomingChangeRef.current = opts.onZoomingChange;
    onZoomSettledRef.current = opts.onZoomSettled;
  });

  useEffect(() => {
    zoomRef.current = 1;
    pendingZoomRef.current = 1;
    pendingAnchorRef.current = null;
    zoomLabelValueRef.current = 1;
    zoomingRef.current = false;
    setZoom(1);
    applyZoomRef.current?.(1);
    onZoomingChangeRef.current?.(false);
    return () => {
      if (zoomFrameRef.current != null) {
        window.cancelAnimationFrame(zoomFrameRef.current);
        zoomFrameRef.current = null;
      }
      if (zoomLabelFrameRef.current != null) {
        window.cancelAnimationFrame(zoomLabelFrameRef.current);
        zoomLabelFrameRef.current = null;
      }
      if (zoomSettledRef.current != null) {
        window.clearTimeout(zoomSettledRef.current);
        zoomSettledRef.current = null;
      }
      if (zoomingRef.current) {
        zoomingRef.current = false;
        onZoomingChangeRef.current?.(false);
      }
    };
  }, [opts.resetKey]);

  const applyZoomValue = (next: number, anchor: ViewportZoomAnchor | null) => {
    const root = rootRef.current;
    if (!root) return;
    const applyZoom = applyZoomRef.current ?? ((value: number) => {
      applyVisualPageScale(pagesRef.current, value);
    });
    applyZoom(next);
    if (anchor) {
      const page = pagesRef.current[anchor.pageIndex];
      if (page) preserveViewportZoomAnchor(root, page, anchor);
    }
    scheduleViewportZoomLabel(zoomLabelFrameRef, zoomLabelValueRef, setZoom, next);
  };

  const setZoomTo = (value: number) => {
    const next = clamp(value, min, max);
    if (Math.abs(next - zoomRef.current) < 0.001) return;
    const root = rootRef.current;
    const anchor = root ? makeViewportCenterAnchor(root, pagesRef.current) : null;
    if (zoomFrameRef.current != null) {
      window.cancelAnimationFrame(zoomFrameRef.current);
      zoomFrameRef.current = null;
    }
    pendingAnchorRef.current = null;
    pendingZoomRef.current = next;
    zoomRef.current = next;
    if (!zoomingRef.current) {
      zoomingRef.current = true;
      onZoomingChangeRef.current?.(true);
    }
    applyZoomValue(next, anchor);
    if (zoomSettledRef.current != null) {
      window.clearTimeout(zoomSettledRef.current);
      zoomSettledRef.current = null;
    }
    window.requestAnimationFrame(() => {
      if (zoomingRef.current) {
        zoomingRef.current = false;
        onZoomingChangeRef.current?.(false);
      }
      onZoomSettledRef.current?.(zoomRef.current);
    });
  };

  const zoomByStep = (direction: -1 | 1) => {
    setZoomTo(nextViewportZoomStep(zoomRef.current, direction, min, max));
  };

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    // Match Chromium PDF viewer behavior: coalesce zoom work to frames,
    // then let the renderer catch up after input settles.
    const flushZoomFrame = () => {
      zoomFrameRef.current = null;
      const next = pendingZoomRef.current;
      const anchor = pendingAnchorRef.current;
      pendingAnchorRef.current = null;
      applyZoomValue(next, anchor);
    };
    const onWheel = (event: WheelEvent) => {
      if (!event.ctrlKey) return;
      event.preventDefault();
      const prev = zoomRef.current;
      const deltaY = normalizedWheelDeltaY(event, root);
      const factor = clamp(Math.exp(-deltaY * 0.001), 0.75, 1.333);
      const next = clamp(prev * factor, min, max);
      if (Math.abs(next - prev) < 0.001) return;
      const anchor = makeViewportZoomAnchor(
        pagesRef.current,
        event.clientX,
        event.clientY,
      );
      onWheelZoomRef.current?.();
      zoomRef.current = next;
      pendingZoomRef.current = next;
      pendingAnchorRef.current = anchor;
      if (!zoomingRef.current) {
        zoomingRef.current = true;
        onZoomingChangeRef.current?.(true);
      }
      if (zoomFrameRef.current == null) {
        zoomFrameRef.current = window.requestAnimationFrame(flushZoomFrame);
      }
      if (zoomSettledRef.current != null) {
        window.clearTimeout(zoomSettledRef.current);
      }
      zoomSettledRef.current = window.setTimeout(() => {
        zoomSettledRef.current = null;
        if (zoomFrameRef.current != null) {
          window.cancelAnimationFrame(zoomFrameRef.current);
          flushZoomFrame();
        }
        if (zoomingRef.current) {
          zoomingRef.current = false;
          onZoomingChangeRef.current?.(false);
        }
        onZoomSettledRef.current?.(zoomRef.current);
      }, 320);
    };
    root.addEventListener("wheel", onWheel, { passive: false });
    return () => root.removeEventListener("wheel", onWheel);
  }, [max, min, pagesRef, rootRef]);

  return { zoom, zoomRef, setZoom: setZoomTo, zoomIn: () => zoomByStep(1), zoomOut: () => zoomByStep(-1) };
}

export function useLineRatioJump(
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
// unwrap after a 1.6 s flash. First-version heuristic 鈥?only matches
// against a single text node, so quotes that get split by inline
// formatting (e.g. <strong> mid-sentence in DOCX) won't match.
// `content` is the source string we render 鈥?used as the dep so the
// effect re-runs once the DOM has the new content.
export function useQuoteJump(
  ref: RefObject<HTMLElement>,
  content: string | null,
  quote: string | null,
  onScrolled?: () => void,
  opts?: { allowMissing?: boolean; consumeOnMissing?: boolean },
) {
  const allowMissing = opts?.allowMissing ?? true;
  const consumeOnMissing = opts?.consumeOnMissing ?? true;
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
    // rAF to wait for layout to settle (OOXML text overlays, syntax
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
          onScrolled?.();
        } else if (allowMissing) {
          setHit("missing");
          if (consumeOnMissing) onScrolled?.();
        } else {
          setHit(null);
        }
      });
    });
    return () => {
      window.cancelAnimationFrame(handle1);
      if (handle2 != null) window.cancelAnimationFrame(handle2);
      if (cleanup != null) window.clearTimeout(cleanup);
      marks.forEach(unwrapMark);
    };
  }, [allowMissing, consumeOnMissing, content, quote, onScrolled, ref]);
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

export function normalizeSearchText(value: string): string {
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


export function ViewerToolbarButton({
  title,
  disabled,
  active,
  onClick,
  children,
}: {
  title: string;
  disabled?: boolean;
  active?: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      disabled={disabled}
      onClick={onClick}
      className={
        "inline-flex h-7 w-7 shrink-0 items-center justify-center rounded border text-fg-muted transition-colors " +
        (active
          ? "border-accent/50 bg-accent/10 text-accent"
          : "border-border hover:bg-bg-muted disabled:cursor-not-allowed disabled:opacity-40")
      }
    >
      {children}
    </button>
  );
}

export function LocatorBanner(
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


function scheduleViewportZoomLabel(
  frameRef: MutableRefObject<number | null>,
  valueRef: MutableRefObject<number>,
  setZoom: (value: number) => void,
  zoom: number,
) {
  valueRef.current = zoom;
  if (frameRef.current != null) return;
  frameRef.current = window.requestAnimationFrame(() => {
    frameRef.current = null;
    setZoom(valueRef.current);
  });
}

export function refreshVisualPageBase(page: HTMLDivElement): boolean {
  const wrapper = page.firstElementChild as HTMLElement | null;
  if (!wrapper) return false;
  const rect = wrapper.getBoundingClientRect();
  const transform = wrapper.style.transform;
  if (transform) wrapper.style.transform = "";
  const width = wrapper.offsetWidth || rect.width;
  const height = wrapper.offsetHeight || rect.height;
  wrapper.style.transform = transform;
  if (!width || !height) return false;
  page.dataset.visualBaseWidth = String(width);
  page.dataset.visualBaseHeight = String(height);
  page.style.overflow = "visible";
  wrapper.style.transformOrigin = "top center";
  wrapper.style.willChange = "transform";
  return true;
}

export function applyVisualPageScale(
  pages: (HTMLDivElement | null)[],
  zoom: number,
) {
  const safeZoom = clamp(zoom, VIEWER_MIN_ZOOM, VIEWER_MAX_ZOOM);
  for (const page of pages) {
    if (!page) continue;
    const wrapper = page.firstElementChild as HTMLElement | null;
    if (!wrapper) continue;
    let baseWidth = Number(page.dataset.visualBaseWidth || 0);
    let baseHeight = Number(page.dataset.visualBaseHeight || 0);
    if ((!baseWidth || !baseHeight) && refreshVisualPageBase(page)) {
      baseWidth = Number(page.dataset.visualBaseWidth || 0);
      baseHeight = Number(page.dataset.visualBaseHeight || 0);
    }
    if (!baseWidth || !baseHeight) continue;
    page.style.width = `${baseWidth * safeZoom}px`;
    page.style.height = `${baseHeight * safeZoom}px`;
    wrapper.style.transformOrigin = "top center";
    wrapper.style.transform = Math.abs(safeZoom - 1) < 0.001
      ? ""
      : `scale(${safeZoom})`;
  }
}

export function parseCssPixels(value: string): number {
  const match = /^([\d.]+)px$/.exec(value.trim());
  return match ? Number(match[1]) : 0;
}

function normalizedWheelDeltaY(event: WheelEvent, root: HTMLElement): number {
  if (event.deltaMode === 1) return event.deltaY * 16;
  if (event.deltaMode === 2) return event.deltaY * root.clientHeight;
  return event.deltaY;
}

function makeViewportZoomAnchor(
  pages: (HTMLDivElement | null)[],
  clientX: number,
  clientY: number,
): ViewportZoomAnchor | null {
  let bestIndex = -1;
  let bestScore = Number.POSITIVE_INFINITY;
  let bestRect: DOMRect | null = null;
  for (let i = 0; i < pages.length; i += 1) {
    const page = pages[i];
    if (!page) continue;
    const rect = page.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    const dy = clientY < rect.top ? rect.top - clientY : clientY > rect.bottom ? clientY - rect.bottom : 0;
    const dx = clientX < rect.left ? rect.left - clientX : clientX > rect.right ? clientX - rect.right : 0;
    const score = dy * 10 + dx;
    if (score < bestScore) {
      bestIndex = i;
      bestScore = score;
      bestRect = rect;
    }
  }
  if (bestIndex < 0 || !bestRect) return null;
  return {
    pageIndex: bestIndex,
    relX: clamp((clientX - bestRect.left) / bestRect.width, 0, 1),
    relY: clamp((clientY - bestRect.top) / bestRect.height, 0, 1),
    clientX,
    clientY,
  };
}

export function makeViewportCenterAnchor(
  root: HTMLDivElement,
  pages: (HTMLDivElement | null)[],
): ViewportZoomAnchor | null {
  const rect = root.getBoundingClientRect();
  return makeViewportZoomAnchor(
    pages,
    rect.left + rect.width / 2,
    rect.top + rect.height / 2,
  );
}

export function preserveViewportZoomAnchor(
  root: HTMLDivElement,
  page: HTMLDivElement,
  anchor: ViewportZoomAnchor,
) {
  const rect = page.getBoundingClientRect();
  const nextClientX = rect.left + anchor.relX * rect.width;
  const nextClientY = rect.top + anchor.relY * rect.height;
  root.scrollLeft += nextClientX - anchor.clientX;
  root.scrollTop += nextClientY - anchor.clientY;
}


export function ViewerLoading() {
  const { t } = useI18n();
  return (
    <div className="flex h-full items-center justify-center text-sm text-fg-muted">
      <Loader2 size={14} className="mr-2 animate-spin" /> {t.common.loading}
    </div>
  );
}
export function ViewerError({ msg }: { msg: string }) {
  return (
    <div className="flex h-full items-center justify-center p-4 text-sm text-danger">
      <AlertCircle size={14} className="mr-2" /> {msg}
    </div>
  );
}
export function TruncatedBanner() {
  const { t } = useI18n();
  return (
    <div className="border-b border-border bg-bg-subtle px-3 py-1 text-[11px] text-fg-subtle">
      {t.library.truncatedPreview}
    </div>
  );
}
