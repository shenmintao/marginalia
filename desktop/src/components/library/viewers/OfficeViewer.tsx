import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Download,
  Maximize2,
  Printer,
  ZoomIn,
  ZoomOut,
} from "lucide-react";

import { maybeAuthDownload } from "@/api/client";
import { useI18n } from "@/lib/i18n";
import {
  VIEWER_MAX_ZOOM,
  VIEWER_MIN_ZOOM,
  ViewerError,
  ViewerLoading,
  ViewerToolbarButton,
  applyVisualPageScale,
  clamp,
  clampInt,
  formatZoomPercent,
  isEditableEventTarget,
  makeViewportCenterAnchor,
  normalizeSearchText,
  parseCssPixels,
  preserveViewportZoomAnchor,
  refreshVisualPageBase,
  useAuthObjectUrl,
  useQuoteJump,
  useViewportWheelZoom,
} from "./ViewerShared";
type OfficeKind = "docx" | "xlsx" | "pptx";
type SingleCanvasOoxmlKind = Exclude<OfficeKind, "docx">;
const OFFICE_VIEWER_LOAD_TIMEOUT_MS = 30_000;

interface OfficeViewerToolbarProps {
  ready: boolean;
  name: string;
  downloadUrl: string;
  positionInput: string;
  total: number;
  positionLabel?: string | null;
  inputLabel: string;
  prevTitle: string;
  nextTitle: string;
  prevIcon: ReactNode;
  nextIcon: ReactNode;
  canPrev: boolean;
  canNext: boolean;
  onPrev: () => void;
  onNext: () => void;
  onPositionInputChange: (value: string) => void;
  onCommitPositionInput: () => void;
  onResetPositionInput: () => void;
  zoomLabel: string;
  canZoomOut: boolean;
  canZoomIn: boolean;
  onZoomOut: () => void;
  onZoomIn: () => void;
  fitActive: boolean;
  onFitWidth: () => void;
  canPrint: boolean;
  onPrint: () => void;
}

function OfficeViewerToolbar({
  ready,
  name,
  downloadUrl,
  positionInput,
  total,
  positionLabel,
  inputLabel,
  prevTitle,
  nextTitle,
  prevIcon,
  nextIcon,
  canPrev,
  canNext,
  onPrev,
  onNext,
  onPositionInputChange,
  onCommitPositionInput,
  onResetPositionInput,
  zoomLabel,
  canZoomOut,
  canZoomIn,
  onZoomOut,
  onZoomIn,
  fitActive,
  onFitWidth,
  canPrint,
  onPrint,
}: OfficeViewerToolbarProps) {
  const { t } = useI18n();
  return (
    <div className="flex h-10 shrink-0 items-center gap-2 overflow-x-auto border-b border-border bg-bg px-3 text-xs text-fg-muted">
      <div className="flex items-center gap-1">
        <ViewerToolbarButton title={prevTitle} disabled={!canPrev} onClick={onPrev}>
          {prevIcon}
        </ViewerToolbarButton>
        <input
          value={positionInput}
          disabled={!ready || total <= 0}
          onChange={(event) => onPositionInputChange(event.target.value)}
          onFocus={(event) => event.currentTarget.select()}
          onBlur={onCommitPositionInput}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.currentTarget.blur();
            } else if (event.key === "Escape") {
              onResetPositionInput();
              event.currentTarget.blur();
            }
          }}
          inputMode="numeric"
          className="h-7 w-12 rounded border border-border bg-bg px-1.5 text-center text-xs text-fg-base tabular-nums outline-none focus:border-accent disabled:opacity-50"
          aria-label={inputLabel}
        />
        <span className="min-w-10 text-center tabular-nums">/ {total || "-"}</span>
        <ViewerToolbarButton title={nextTitle} disabled={!canNext} onClick={onNext}>
          {nextIcon}
        </ViewerToolbarButton>
        {positionLabel ? (
          <span className="ml-1 max-w-40 truncate text-fg-subtle" title={positionLabel}>
            {positionLabel}
          </span>
        ) : null}
      </div>
      <div className="h-5 w-px shrink-0 bg-border" />
      <div className="flex items-center gap-1">
        <ViewerToolbarButton title={t.viewer.zoomOut} disabled={!canZoomOut} onClick={onZoomOut}>
          <ZoomOut size={14} />
        </ViewerToolbarButton>
        <span className="min-w-14 text-center tabular-nums">{zoomLabel}</span>
        <ViewerToolbarButton title={t.viewer.zoomIn} disabled={!canZoomIn} onClick={onZoomIn}>
          <ZoomIn size={14} />
        </ViewerToolbarButton>
        <ViewerToolbarButton
          title={t.viewer.fitWidth}
          disabled={!ready}
          active={fitActive}
          onClick={onFitWidth}
        >
          <Maximize2 size={14} />
        </ViewerToolbarButton>
      </div>
      <div className="ml-auto flex items-center gap-1">
        <ViewerToolbarButton title={t.viewer.print} disabled={!canPrint} onClick={onPrint}>
          <Printer size={14} />
        </ViewerToolbarButton>
        <a
          href={downloadUrl}
          download={name}
          onClick={(e) => maybeAuthDownload(e, downloadUrl, name)}
          title={t.viewer.download}
          className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded border border-border text-fg-muted transition-colors hover:bg-bg-muted"
        >
          <Download size={14} />
        </a>
      </div>
    </div>
  );
}

type DocxDocumentInstance = import("@silurus/ooxml/docx").DocxDocument;
type PptxViewerInstance = import("@silurus/ooxml/pptx").PptxViewer;
type PptxPresentationInstance = import("@silurus/ooxml/pptx").PptxPresentation;
type PptxPresentationData = import("@silurus/ooxml/pptx").Presentation;
type PptxSlide = import("@silurus/ooxml/pptx").Slide;
type PptxTextBody = import("@silurus/ooxml/pptx").TextBody;
type PptxChartElement = import("@silurus/ooxml/pptx").ChartElement;
type XlsxViewerInstance = import("@silurus/ooxml/xlsx").XlsxViewer;
type OoxmlViewerInstance =
  | PptxViewerInstance
  | XlsxViewerInstance;
type PptxPresentationInternal = {
  _presentation?: PptxPresentationData | null;
};

type PptxViewerInternal = {
  engine?: PptxPresentationInstance | null;
};

interface PptxSlideSearchEntry {
  text: string;
  normalized: string;
}

interface DocxTextRunInfo {
  text: string;
  x: number;
  y: number;
  w: number;
  h: number;
  fontSize: number;
  font: string;
}

const DOCX_BASE_WIDTH = 960;
const DOCX_MIN_ZOOM = 0.45;
const DOCX_MAX_ZOOM = 2.5;
const DOCX_VIEWPORT_SIDE_PADDING = 48;

type DocxFitMode = "none" | "width";

function roundDocxRenderZoom(zoom: number): number {
  return Math.round(clamp(zoom, DOCX_MIN_ZOOM, DOCX_MAX_ZOOM) * 100) / 100;
}

function docxFitWidthZoom(root: HTMLDivElement): number {
  const available = Math.max(240, root.clientWidth - DOCX_VIEWPORT_SIDE_PADDING);
  return roundDocxRenderZoom(available / DOCX_BASE_WIDTH);
}

function waitForNextFrame(): Promise<void> {
  return new Promise((resolve) => window.requestAnimationFrame(() => resolve()));
}

export function OfficeDocumentView({ url, format, name, downloadUrl, quote, page, onScrolled }: {
  url: string;
  format: OfficeKind;
  name: string;
  downloadUrl: string;
  quote: string | null;
  page: number | null;
  onScrolled?: () => void;
}) {
  // Token-protected backends reject the ooxml loaders' internal fetch;
  // route the bytes through an object URL in that case (direct URL
  // otherwise).
  const { src, err } = useAuthObjectUrl(url);
  if (err) return <ViewerError msg={err} />;
  if (!src) return <ViewerLoading />;
  if (format === "docx") {
    return (
      <DocxScrollView
        url={src}
        name={name}
        downloadUrl={downloadUrl}
        quote={quote}
        onScrolled={onScrolled}
      />
    );
  }
  return (
    <OoxmlView
      url={src}
      format={format}
      name={name}
      downloadUrl={downloadUrl}
      quote={quote}
      page={format === "pptx" ? page : null}
      onScrolled={onScrolled}
    />
  );
}
function DocxScrollView({ url, name, downloadUrl, quote, onScrolled }: {
  url: string;
  name: string;
  downloadUrl: string;
  quote: string | null;
  onScrolled?: () => void;
}) {
  const { t } = useI18n();
  const scrollRef = useRef<HTMLDivElement>(null);
  const docRef = useRef<DocxDocumentInstance | null>(null);
  const pageRefs = useRef<(HTMLDivElement | null)[]>([]);
  const rasterZoomRef = useRef(1);
  const docxZoomingRef = useRef(false);
  const quoteRef = useRef<string | null>(quote);
  const [ready, setReady] = useState(false);
  const [rendering, setRendering] = useState(false);
  const [renderedPageCount, setRenderedPageCount] = useState(0);
  const [err, setErr] = useState<string | null>(null);
  const [pageCount, setPageCount] = useState(0);
  const [currentPage, setCurrentPage] = useState(0);
  const [pageInput, setPageInput] = useState("1");
  const [fitMode, setFitMode] = useState<DocxFitMode>("none");
  const [renderRequestZoom, setRenderRequestZoom] = useState(1);
  const [renderKey, setRenderKey] = useState(0);

  const updateCurrentPageFromViewport = () => {
    const root = scrollRef.current;
    if (!root) return;
    setCurrentPage(nearestDocxPage(root, pageRefs.current));
  };

  const zoom = useViewportWheelZoom(scrollRef, pageRefs, {
    resetKey: `docx:${url}`,
    min: DOCX_MIN_ZOOM,
    max: DOCX_MAX_ZOOM,
    applyZoom: (value) => applyDocxViewportZoom(
      pageRefs.current,
      value,
      rasterZoomRef.current,
    ),
    onWheelZoom: () => setFitMode("none"),
    onZoomingChange: (zooming) => {
      docxZoomingRef.current = zooming;
      if (!zooming) {
        window.requestAnimationFrame(updateCurrentPageFromViewport);
      }
    },
    onZoomSettled: (value) => {
      const next = roundDocxRenderZoom(value);
      setRenderRequestZoom((prev) => Math.abs(prev - next) < 0.005 ? prev : next);
    },
  });

  const setManualZoom = (value: number) => {
    setFitMode("none");
    zoom.setZoom(roundDocxRenderZoom(value));
  };

  const applyFitWidth = () => {
    const root = scrollRef.current;
    if (!root) return;
    setFitMode("width");
    zoom.setZoom(docxFitWidthZoom(root));
  };

  const toggleFitWidth = () => {
    if (fitMode === "width") {
      setManualZoom(1);
    } else {
      applyFitWidth();
    }
  };

  const commitPageInput = () => {
    if (pageCount <= 0) return;
    const page = clampInt(parseInt(pageInput, 10), 1, pageCount);
    if (!Number.isFinite(page)) {
      setPageInput(String(currentPage + 1));
      return;
    }
    setPageInput(String(page));
    scrollDocxPage(pageRefs.current, page - 1);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (isEditableEventTarget(event.target)) return;
    if (event.ctrlKey || event.metaKey) {
      if (event.key === "+" || event.key === "=") {
        event.preventDefault();
        setFitMode("none");
        zoom.zoomIn();
      } else if (event.key === "-" || event.key === "_") {
        event.preventDefault();
        setFitMode("none");
        zoom.zoomOut();
      } else if (event.key === "0") {
        event.preventDefault();
        setManualZoom(1);
      }
      return;
    }
    if (event.altKey || !ready || pageCount <= 0) return;
    if (event.key === "PageUp") {
      event.preventDefault();
      scrollDocxPage(pageRefs.current, currentPage - 1);
    } else if (event.key === "PageDown" || event.key === " ") {
      event.preventDefault();
      scrollDocxPage(pageRefs.current, currentPage + 1);
    } else if (event.key === "Home") {
      event.preventDefault();
      scrollDocxPage(pageRefs.current, 0);
    } else if (event.key === "End") {
      event.preventDefault();
      scrollDocxPage(pageRefs.current, pageCount - 1);
    }
  };

  useEffect(() => {
    quoteRef.current = quote;
  }, [quote]);

  const initialRenderComplete = pageCount > 0 && renderedPageCount >= pageCount;
  const quoteState = useQuoteJump(
    scrollRef,
    ready && renderKey > 0 ? `docx:${url}:${renderKey}` : null,
    quote,
    onScrolled,
    { allowMissing: initialRenderComplete, consumeOnMissing: initialRenderComplete },
  );

  useEffect(() => {
    let cancelled = false;
    const timeout = window.setTimeout(() => {
      if (!cancelled) setErr(t.viewer.officeTimeout("DOCX"));
    }, OFFICE_VIEWER_LOAD_TIMEOUT_MS);
    setReady(false);
    setRendering(false);
    setErr(null);
    setPageCount(0);
    setCurrentPage(0);
    setRenderedPageCount(0);
    setRenderRequestZoom(1);
    setRenderKey(0);
    rasterZoomRef.current = 1;
    pageRefs.current = [];
    docRef.current?.destroy();
    docRef.current = null;

    (async () => {
      try {
        const { DocxDocument } = await import("@silurus/ooxml/docx");
        const doc = await DocxDocument.load(url);
        if (cancelled) {
          doc.destroy();
          return;
        }
        docRef.current = doc;
        setPageCount(doc.pageCount);
        setReady(true);
        window.clearTimeout(timeout);
      } catch (error) {
        if (!cancelled) {
          setErr(error instanceof Error ? error.message : String(error));
        }
        window.clearTimeout(timeout);
      }
    })();

    return () => {
      cancelled = true;
      window.clearTimeout(timeout);
      docRef.current?.destroy();
      docRef.current = null;
    };
  }, [url]);

  useEffect(() => {
    setPageInput(pageCount > 0 ? String(currentPage + 1) : "");
  }, [currentPage, pageCount]);

  useEffect(() => {
    if (fitMode !== "width") return;
    const root = scrollRef.current;
    if (!root) return;
    let frame: number | null = null;
    const sync = () => {
      frame = null;
      zoom.setZoom(docxFitWidthZoom(root));
    };
    const schedule = () => {
      if (frame != null) return;
      frame = window.requestAnimationFrame(sync);
    };
    const resizeObserver = typeof ResizeObserver !== "undefined"
      ? new ResizeObserver(schedule)
      : null;
    resizeObserver?.observe(root);
    window.addEventListener("resize", schedule);
    schedule();
    return () => {
      resizeObserver?.disconnect();
      window.removeEventListener("resize", schedule);
      if (frame != null) window.cancelAnimationFrame(frame);
    };
  }, [fitMode]);
  useEffect(() => {
    const root = scrollRef.current;
    if (!root) return;
    let frame: number | null = null;
    const update = () => {
      frame = null;
      if (docxZoomingRef.current) return;
      updateCurrentPageFromViewport();
    };
    const schedule = () => {
      if (frame != null) return;
      frame = window.requestAnimationFrame(update);
    };
    update();
    root.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    return () => {
      root.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
      if (frame != null) window.cancelAnimationFrame(frame);
    };
  }, [pageCount]);

  useEffect(() => {
    if (!ready || pageCount <= 0) return;
    const doc = docRef.current;
    if (!doc) return;
    let cancelled = false;
    const handle = window.requestAnimationFrame(() => {
      setRendering(true);
      void (async () => {
        const root = scrollRef.current;
        const renderAnchor = root ? makeViewportCenterAnchor(root, pageRefs.current) : null;
        try {
          for (let i = 0; i < pageCount; i += 1) {
            if (cancelled) return;
            const pageEl = pageRefs.current[i];
            if (!pageEl) continue;
            const rendered = await renderDocxPage(
              pageEl,
              doc,
              i,
              renderRequestZoom,
              () => zoom.zoomRef.current,
              () => cancelled,
            );
            if (!rendered) return;
            const completedThrough = i + 1;
            setRenderedPageCount((prev) => Math.max(prev, completedThrough));
            if (quoteRef.current || completedThrough === 1 || completedThrough === pageCount) {
              setRenderKey((n) => n + 1);
            }
            if (completedThrough < pageCount) {
              await waitForNextFrame();
            }
          }
          if (!cancelled) {
            rasterZoomRef.current = renderRequestZoom;
            applyDocxViewportZoom(pageRefs.current, zoom.zoomRef.current, rasterZoomRef.current);
            if (root && renderAnchor) {
              const page = pageRefs.current[renderAnchor.pageIndex];
              if (page) preserveViewportZoomAnchor(root, page, renderAnchor);
            }
            setRenderKey((n) => n + 1);
            setRendering(false);
          }
        } catch (error) {
          if (!cancelled) {
            setErr(error instanceof Error ? error.message : String(error));
            setRendering(false);
          }
        }
      })();
    });
    return () => {
      cancelled = true;
      window.cancelAnimationFrame(handle);
    };
  }, [ready, pageCount, renderRequestZoom]);

  const canPrev = ready && currentPage > 0;
  const canNext = ready && pageCount > 0 && currentPage < pageCount - 1;
  const canZoomOut = ready && zoom.zoom > DOCX_MIN_ZOOM + 0.001;
  const canZoomIn = ready && zoom.zoom < DOCX_MAX_ZOOM - 0.001;
  const zoomLabel = formatZoomPercent(zoom.zoom);

  return (
    <div
      className="flex h-full min-h-0 flex-col bg-bg-subtle"
      tabIndex={0}
      onKeyDown={handleKeyDown}
    >
      <OfficeViewerToolbar
        ready={ready}
        name={name}
        downloadUrl={downloadUrl}
        positionInput={pageInput}
        total={pageCount}
        inputLabel={t.viewer.pageNumber}
        prevTitle={t.viewer.prevPage}
        nextTitle={t.viewer.nextPage}
        prevIcon={<ChevronUp size={14} />}
        nextIcon={<ChevronDown size={14} />}
        canPrev={canPrev}
        canNext={canNext}
        onPrev={() => scrollDocxPage(pageRefs.current, currentPage - 1)}
        onNext={() => scrollDocxPage(pageRefs.current, currentPage + 1)}
        onPositionInputChange={setPageInput}
        onCommitPositionInput={commitPageInput}
        onResetPositionInput={() => setPageInput(String(currentPage + 1))}
        zoomLabel={zoomLabel}
        canZoomOut={canZoomOut}
        canZoomIn={canZoomIn}
        onZoomOut={() => { setFitMode("none"); zoom.zoomOut(); }}
        onZoomIn={() => { setFitMode("none"); zoom.zoomIn(); }}
        fitActive={fitMode === "width"}
        onFitWidth={toggleFitWidth}
        canPrint={ready && renderedPageCount > 0}
        onPrint={() => printRenderedCanvasPages(pageRefs.current, name || "document")}
      />
      <div ref={scrollRef} className="relative min-h-0 flex-1 overflow-auto">
        {quoteState.banner}
        <div className="flex min-h-full w-full flex-col items-center gap-4 p-4">
          {Array.from({ length: pageCount }, (_, i) => (
            <div
              key={i}
              ref={(el) => { pageRefs.current[i] = el; }}
              className="flex min-h-24 justify-center"
            />
          ))}
        </div>
        {(!ready || (rendering && renderedPageCount === 0)) && !err && (
          <div className="absolute inset-0 z-20 bg-bg/70">
            <ViewerLoading />
          </div>
        )}
        {err && (
          <div className="absolute inset-0 z-20 bg-bg">
            <ViewerError msg={err} />
          </div>
        )}
      </div>
    </div>
  );
}
async function renderDocxPage(
  pageEl: HTMLDivElement,
  doc: DocxDocumentInstance,
  pageIndex: number,
  renderZoom: number,
  getViewportZoom: () => number,
  shouldCancel?: () => boolean,
): Promise<boolean> {
  const wrapper = document.createElement("div");
  wrapper.className = "relative inline-block align-top bg-white shadow-sm";
  const canvas = document.createElement("canvas");
  canvas.className = "block bg-white";
  const textLayer = document.createElement("div");
  textLayer.style.cssText =
    "position:absolute;top:0;left:0;width:100%;height:100%;overflow:hidden;pointer-events:none;user-select:text;-webkit-user-select:text;";
  wrapper.appendChild(canvas);
  wrapper.appendChild(textLayer);

  const runs: DocxTextRunInfo[] = [];
  const dpr = window.devicePixelRatio || 1;
  await doc.renderPage(canvas, pageIndex, {
    width: DOCX_BASE_WIDTH * renderZoom,
    dpr,
    onTextRun: (run: DocxTextRunInfo) => runs.push(run),
  });
  if (shouldCancel?.()) return false;
  buildDocxTextLayer(textLayer, canvas, runs);
  if (shouldCancel?.()) return false;
  const rasterWidth = parseCssPixels(canvas.style.width) || canvas.width / dpr;
  const rasterHeight = parseCssPixels(canvas.style.height) || canvas.height / dpr;
  const previousBaseWidth = Number(pageEl.dataset.docxBaseWidth || 0);
  const previousBaseHeight = Number(pageEl.dataset.docxBaseHeight || 0);
  const logicalWidth = previousBaseWidth || rasterWidth / renderZoom;
  const logicalHeight = previousBaseHeight || rasterHeight / renderZoom;
  pageEl.dataset.docxBaseWidth = String(logicalWidth);
  pageEl.dataset.docxBaseHeight = String(logicalHeight);
  pageEl.dataset.docxRasterZoom = String(renderZoom);
  pageEl.style.overflow = "visible";
  wrapper.style.transformOrigin = "top center";
  wrapper.style.willChange = "transform";
  setDocxPageScale(pageEl, wrapper, logicalWidth, logicalHeight, getViewportZoom(), renderZoom);
  pageEl.replaceChildren(wrapper);
  return true;
}
function buildDocxTextLayer(
  textLayer: HTMLDivElement,
  canvas: HTMLCanvasElement,
  runs: DocxTextRunInfo[],
) {
  textLayer.replaceChildren();
  textLayer.style.width = canvas.style.width || `${canvas.width}px`;
  textLayer.style.height = canvas.style.height || `${canvas.height}px`;
  for (const run of runs) {
    const span = document.createElement("span");
    span.textContent = run.text;
    span.style.cssText =
      `position:absolute;left:${run.x}px;top:${run.y}px;` +
      `font:${run.font};line-height:${run.h}px;letter-spacing:0;` +
      "white-space:pre;color:transparent;cursor:text;pointer-events:all;";
    textLayer.appendChild(span);
  }
}

function applyDocxViewportZoom(
  pages: (HTMLDivElement | null)[],
  viewportZoom: number,
  fallbackRasterZoom: number,
) {
  for (const page of pages) {
    if (!page) continue;
    const baseWidth = Number(page.dataset.docxBaseWidth || 0);
    const baseHeight = Number(page.dataset.docxBaseHeight || 0);
    const pageRasterZoom = Number(page.dataset.docxRasterZoom || fallbackRasterZoom || 1);
    const wrapper = page.firstElementChild as HTMLElement | null;
    if (!baseWidth || !baseHeight || !pageRasterZoom || !wrapper) continue;
    setDocxPageScale(page, wrapper, baseWidth, baseHeight, viewportZoom, pageRasterZoom);
  }
}

function setDocxPageScale(
  page: HTMLDivElement,
  wrapper: HTMLElement,
  baseWidth: number,
  baseHeight: number,
  viewportZoom: number,
  rasterZoom: number,
) {
  const safeViewportZoom = clamp(viewportZoom, DOCX_MIN_ZOOM, DOCX_MAX_ZOOM);
  const safeRasterZoom = clamp(rasterZoom, DOCX_MIN_ZOOM, DOCX_MAX_ZOOM);
  const transformScale = safeViewportZoom / safeRasterZoom;
  page.style.width = `${baseWidth * safeViewportZoom}px`;
  page.style.height = `${baseHeight * safeViewportZoom}px`;
  wrapper.style.transformOrigin = "top center";
  wrapper.style.transform = Math.abs(transformScale - 1) < 0.001
    ? ""
    : `scale(${transformScale})`;
}


function nearestDocxPage(
  root: HTMLDivElement,
  pages: (HTMLDivElement | null)[],
): number {
  const rootRect = root.getBoundingClientRect();
  const center = rootRect.top + rootRect.height / 2;
  let best = 0;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (let i = 0; i < pages.length; i += 1) {
    const page = pages[i];
    if (!page) continue;
    const rect = page.getBoundingClientRect();
    const pageCenter = rect.top + rect.height / 2;
    const distance = Math.abs(pageCenter - center);
    if (distance < bestDistance) {
      best = i;
      bestDistance = distance;
    }
  }
  return best;
}

function scrollDocxPage(
  pages: (HTMLDivElement | null)[],
  pageIndex: number,
) {
  const target = pages[clampInt(pageIndex, 0, Math.max(0, pages.length - 1))];
  target?.scrollIntoView({ block: "start", behavior: "smooth" });
}


function printRenderedCanvasPages(
  pages: (HTMLDivElement | null)[],
  title: string,
) {
  const canvases = pages
    .map((page) => page?.querySelector("canvas") ?? null)
    .filter((canvas): canvas is HTMLCanvasElement => canvas != null);
  if (canvases.length === 0) return;
  const printWindow = window.open("", "_blank", "width=960,height=720");
  if (printWindow) {
    writePrintDocument(printWindow.document, title, canvases, () => {
      printWindow.focus();
      printWindow.print();
    });
    return;
  }
  // Packaged Tauri webviews silently drop window.open() popups; fall
  // back to printing from a hidden same-document iframe instead.
  const frame = document.createElement("iframe");
  frame.style.cssText =
    "position:fixed;right:0;bottom:0;width:0;height:0;border:0;visibility:hidden;";
  document.body.appendChild(frame);
  const doc = frame.contentDocument;
  const win = frame.contentWindow;
  if (!doc || !win) {
    frame.remove();
    return;
  }
  writePrintDocument(doc, title, canvases, () => {
    win.focus();
    win.print();
    // Leave the frame around long enough for the print dialog to
    // consume its content, then clean up.
    window.setTimeout(() => frame.remove(), 60_000);
  });
}

function writePrintDocument(
  doc: Document,
  title: string,
  canvases: HTMLCanvasElement[],
  onReady: () => void,
) {
  doc.open();
  doc.write("<!doctype html><html><head><title></title></head><body></body></html>");
  doc.close();
  doc.title = title;
  const style = doc.createElement("style");
  style.textContent =
    "@page{margin:12mm}body{margin:0;background:#fff}" +
    "img{display:block;max-width:100%;height:auto;margin:0 auto 12mm;page-break-after:always}" +
    "img:last-child{page-break-after:auto;margin-bottom:0}";
  doc.head.appendChild(style);
  let remaining = canvases.length;
  const maybePrint = () => {
    remaining -= 1;
    if (remaining > 0) return;
    window.setTimeout(onReady, 50);
  };
  for (const canvas of canvases) {
    const img = doc.createElement("img");
    img.onload = maybePrint;
    img.onerror = maybePrint;
    img.src = canvas.toDataURL("image/png");
    doc.body.appendChild(img);
  }
}
function OoxmlView({ url, format, name, downloadUrl, quote, page, onScrolled }: {
  url: string;
  format: SingleCanvasOoxmlKind;
  name: string;
  downloadUrl: string;
  quote: string | null;
  page: number | null;
  onScrolled?: () => void;
}) {
  const { t } = useI18n();
  const scrollRef = useRef<HTMLDivElement>(null);
  const hostRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const pageRefs = useRef<(HTMLDivElement | null)[]>([]);
  const thumbnailCanvasRefs = useRef<(HTMLCanvasElement | null)[]>([]);
  const thumbnailButtonRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const viewerRef = useRef<OoxmlViewerInstance | null>(null);
  const [ready, setReady] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [position, setPosition] = useState({ current: 0, total: 0 });
  const [positionInput, setPositionInput] = useState("1");
  const [sheetNames, setSheetNames] = useState<string[]>([]);
  const [fitMode, setFitMode] = useState<DocxFitMode>("none");
  const [renderKey, setRenderKey] = useState(0);
  const [pptxSlideSearch, setPptxSlideSearch] = useState<PptxSlideSearchEntry[] | null>(null);
  const zoom = useViewportWheelZoom(scrollRef, pageRefs, {
    resetKey: `${format}:${url}`,
    applyZoom: (value) => {
      if (format === "xlsx") {
        (viewerRef.current as XlsxViewerInstance | null)?.setScale(value);
      } else {
        applyVisualPageScale(pageRefs.current, value);
      }
    },
    onWheelZoom: () => setFitMode("none"),
  });
  const pptxQuoteSlide = useMemo<number | null | undefined>(() => {
    if (format !== "pptx" || !quote) return null;
    if (!pptxSlideSearch) return undefined;
    return findPptxQuoteSlide(pptxSlideSearch, quote);
  }, [format, pptxSlideSearch, quote]);
  const pptxQuoteReady = format !== "pptx" || !quote || pptxQuoteSlide !== undefined;
  const quoteJumpContent = format !== "xlsx" && ready && renderKey > 0 && (
    format !== "pptx" || !quote || (
      pptxQuoteReady && (pptxQuoteSlide == null || pptxQuoteSlide === position.current)
    )
  )
    ? `${format}:${url}:${position.current}:${renderKey}`
    : null;
  const quoteState = useQuoteJump(
    hostRef,
    quoteJumpContent,
    quote,
    onScrolled,
    format === "pptx"
      ? { allowMissing: pptxQuoteReady, consumeOnMissing: pptxQuoteReady }
      : undefined,
  );

  const goToPosition = (index: number) => {
    if (!ready || position.total <= 0) return;
    void ooxmlGoTo(format, viewerRef.current, clampInt(index, 0, position.total - 1));
  };

  useEffect(() => {
    if (format !== "pptx" || quote || page == null || !ready || position.total <= 0) return;
    const target = clampInt(page - 1, 0, position.total - 1);
    if (target === position.current) {
      onScrolled?.();
      return;
    }
    let cancelled = false;
    void ooxmlGoTo(format, viewerRef.current, target).then(() => {
      if (!cancelled) onScrolled?.();
    });
    return () => { cancelled = true; };
  }, [format, onScrolled, page, position.current, position.total, quote, ready]);

  useEffect(() => {
    if (format !== "pptx" || !ready || !quote || pptxQuoteSlide == null || position.total <= 0) return;
    const target = clampInt(pptxQuoteSlide, 0, position.total - 1);
    if (target === position.current) return;
    void ooxmlGoTo(format, viewerRef.current, target);
  }, [format, position.current, position.total, pptxQuoteSlide, quote, ready]);

  const setManualZoom = (value: number) => {
    setFitMode("none");
    zoom.setZoom(value);
  };

  const applyFitWidth = () => {
    const root = scrollRef.current;
    if (!root) return;
    setFitMode("width");
    zoom.setZoom(ooxmlFitWidthZoom(root));
  };

  const toggleFitWidth = () => {
    if (fitMode === "width") setManualZoom(1);
    else applyFitWidth();
  };

  const commitPositionInput = () => {
    if (position.total <= 0) return;
    const parsed = parseInt(positionInput, 10);
    if (!Number.isFinite(parsed)) {
      setPositionInput(String(position.current + 1));
      return;
    }
    const next = clampInt(parsed, 1, position.total);
    setPositionInput(String(next));
    goToPosition(next - 1);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (isEditableEventTarget(event.target)) return;
    if (event.ctrlKey || event.metaKey) {
      if (event.key === "+" || event.key === "=") {
        event.preventDefault();
        setFitMode("none");
        zoom.zoomIn();
      } else if (event.key === "-" || event.key === "_") {
        event.preventDefault();
        setFitMode("none");
        zoom.zoomOut();
      } else if (event.key === "0") {
        event.preventDefault();
        setManualZoom(1);
      }
      return;
    }
    if (event.altKey || !ready || position.total <= 0) return;
    if (event.key === "PageUp" || (format === "pptx" && event.key === "ArrowLeft")) {
      event.preventDefault();
      goToPosition(position.current - 1);
    } else if (event.key === "PageDown" || event.key === " " || (format === "pptx" && event.key === "ArrowRight")) {
      event.preventDefault();
      goToPosition(position.current + 1);
    } else if (event.key === "Home") {
      event.preventDefault();
      goToPosition(0);
    } else if (event.key === "End") {
      event.preventDefault();
      goToPosition(position.total - 1);
    }
  };

  useEffect(() => {
    let cancelled = false;
    let reportedError = false;
    const timeout = window.setTimeout(() => {
      reportedError = true;
      if (!cancelled) setErr(t.viewer.officeTimeout(format.toUpperCase()));
    }, OFFICE_VIEWER_LOAD_TIMEOUT_MS);
    const rafs: number[] = [];
    setReady(false);
    setErr(null);
    setPosition({ current: 0, total: 0 });
    setPositionInput("1");
    setSheetNames([]);
    setFitMode("none");
    setRenderKey(0);
    setPptxSlideSearch(null);
    thumbnailCanvasRefs.current = [];
    thumbnailButtonRefs.current = [];

    const reportError = (error: unknown) => {
      reportedError = true;
      window.clearTimeout(timeout);
      if (!cancelled) {
        setErr(error instanceof Error ? error.message : String(error));
      }
    };
    const refreshZoomGeometry = () => {
      const page = pageRefs.current[0];
      if (!page) return;
      if (format !== "xlsx") {
        refreshVisualPageBase(page);
        applyVisualPageScale(pageRefs.current, zoom.zoomRef.current);
      } else {
        (viewerRef.current as XlsxViewerInstance | null)?.setScale(zoom.zoomRef.current);
      }
    };
    const noteRendered = () => {
      const handle = window.requestAnimationFrame(() => {
        if (!cancelled) {
          refreshZoomGeometry();
          setRenderKey((n) => n + 1);
        }
      });
      rafs.push(handle);
    };

    (async () => {
      try {
        const host = hostRef.current;
        if (!host) return;
        if (format === "pptx") {
          const canvas = ensureOoxmlCanvas(host, canvasRef.current);
          if (!canvas) return;
          const { PptxViewer } = await import("@silurus/ooxml/pptx");
          if (cancelled) return;
          const viewer = new PptxViewer(canvas, {
            enableTextSelection: true,
            onError: reportError,
            onSlideChange: (index, total) => {
              if (cancelled) return;
              setPosition({ current: index, total });
              noteRendered();
            },
          });
          viewerRef.current = viewer;
          await viewer.load(url);
          if (!cancelled) {
            const presentation = (viewer as unknown as PptxViewerInternal).engine;
            setPptxSlideSearch(presentation ? buildPptxSlideSearchIndex(presentation) : []);
          }
        } else {
          host.replaceChildren();
          const { XlsxViewer } = await import("@silurus/ooxml/xlsx");
          if (cancelled) return;
          const viewer = new XlsxViewer(host, {
            showZoomSlider: false,
            zoomMin: VIEWER_MIN_ZOOM,
            zoomMax: VIEWER_MAX_ZOOM,
            onError: reportError,
            onReady: (names) => {
              if (!cancelled) {
                setSheetNames(names);
                setPosition({ current: 0, total: names.length });
                noteRendered();
              }
            },
            onSheetChange: (index, total) => {
              if (!cancelled) {
                setPosition({ current: index, total });
                noteRendered();
              }
            },
          });
          viewerRef.current = viewer;
          await viewer.load(url);
          viewer.setScale(zoom.zoomRef.current);
        }
        if (!cancelled && !reportedError) {
          window.clearTimeout(timeout);
          setReady(true);
        }
      } catch (error) {
        reportError(error);
      }
    })();

    return () => {
      cancelled = true;
      window.clearTimeout(timeout);
      rafs.forEach((handle) => window.cancelAnimationFrame(handle));
      const viewer = viewerRef.current;
      viewerRef.current = null;
      try {
        viewer?.destroy();
      } catch {
        /* Best-effort cleanup for third-party viewer teardown. */
      }
      const host = hostRef.current;
      const canvas = canvasRef.current;
      if (format === "xlsx") {
        host?.replaceChildren();
      } else if (host && canvas && !host.contains(canvas)) {
        host.appendChild(canvas);
      }
    };
  }, [format, url]);

  useEffect(() => {
    setPositionInput(position.total > 0 ? String(position.current + 1) : "");
  }, [position.current, position.total]);

  useEffect(() => {
    if (fitMode !== "width") return;
    const root = scrollRef.current;
    if (!root) return;
    let frame: number | null = null;
    const sync = () => {
      frame = null;
      zoom.setZoom(ooxmlFitWidthZoom(root));
    };
    const schedule = () => {
      if (frame != null) return;
      frame = window.requestAnimationFrame(sync);
    };
    const resizeObserver = typeof ResizeObserver !== "undefined"
      ? new ResizeObserver(schedule)
      : null;
    resizeObserver?.observe(root);
    window.addEventListener("resize", schedule);
    schedule();
    return () => {
      resizeObserver?.disconnect();
      window.removeEventListener("resize", schedule);
      if (frame != null) window.cancelAnimationFrame(frame);
    };
  }, [fitMode]);

  useEffect(() => {
    if (format !== "pptx" || !ready || position.total <= 0) return;
    let cancelled = false;
    let presentation: PptxPresentationInstance | null = null;
    (async () => {
      try {
        const { PptxPresentation } = await import("@silurus/ooxml/pptx");
        presentation = await PptxPresentation.load(url);
        if (cancelled) return;
        for (let i = 0; i < position.total; i += 1) {
          if (cancelled) return;
          const canvas = thumbnailCanvasRefs.current[i];
          if (!canvas) continue;
          await presentation.renderSlide(canvas, i, {
            width: 112,
            dpr: Math.min(window.devicePixelRatio || 1, 2),
          });
        }
      } catch {
        /* Thumbnail rendering is auxiliary; keep the main slide viewer usable. */
      }
    })();
    return () => {
      cancelled = true;
      presentation?.destroy();
    };
  }, [format, ready, position.total, url]);

  useEffect(() => {
    if (format !== "pptx") return;
    thumbnailButtonRefs.current[position.current]?.scrollIntoView({ block: "nearest" });
  }, [format, position.current]);

  const canPrev = ready && position.total > 0 && position.current > 0;
  const canNext = ready && position.total > 0 && position.current < position.total - 1;
  const canZoomOut = ready && zoom.zoom > VIEWER_MIN_ZOOM + 0.001;
  const canZoomIn = ready && zoom.zoom < VIEWER_MAX_ZOOM - 0.001;
  const isPptx = format === "pptx";
  const positionLabel = format === "xlsx" ? sheetNames[position.current] ?? null : null;

  return (
    <div
      className="flex h-full min-h-0 flex-col bg-bg-subtle"
      tabIndex={0}
      onKeyDown={handleKeyDown}
    >
      <OfficeViewerToolbar
        ready={ready}
        name={name}
        downloadUrl={downloadUrl}
        positionInput={positionInput}
        total={position.total}
        positionLabel={positionLabel}
        inputLabel={isPptx ? t.viewer.slideNumber : t.viewer.sheetNumber}
        prevTitle={isPptx ? t.viewer.prevSlide : t.viewer.prevSheet}
        nextTitle={isPptx ? t.viewer.nextSlide : t.viewer.nextSheet}
        prevIcon={isPptx ? <ChevronLeft size={14} /> : <ChevronUp size={14} />}
        nextIcon={isPptx ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
        canPrev={canPrev}
        canNext={canNext}
        onPrev={() => goToPosition(position.current - 1)}
        onNext={() => goToPosition(position.current + 1)}
        onPositionInputChange={setPositionInput}
        onCommitPositionInput={commitPositionInput}
        onResetPositionInput={() => setPositionInput(String(position.current + 1))}
        zoomLabel={formatZoomPercent(zoom.zoom)}
        canZoomOut={canZoomOut}
        canZoomIn={canZoomIn}
        onZoomOut={() => { setFitMode("none"); zoom.zoomOut(); }}
        onZoomIn={() => { setFitMode("none"); zoom.zoomIn(); }}
        fitActive={fitMode === "width"}
        onFitWidth={toggleFitWidth}
        canPrint={ready && renderKey > 0}
        onPrint={() => printRenderedCanvasPages(pageRefs.current, name || (isPptx ? "presentation" : "workbook"))}
      />
      <div className="flex min-h-0 flex-1">
        {isPptx && (
          <aside className="hidden w-36 shrink-0 overflow-y-auto border-r border-border bg-bg px-2 py-3 md:block">
            <div className="space-y-2">
              {Array.from({ length: position.total }, (_, i) => {
                const active = i === position.current;
                return (
                  <button
                    key={i}
                    ref={(el) => { thumbnailButtonRefs.current[i] = el; }}
                    type="button"
                    onClick={() => goToPosition(i)}
                    className={
                      "block w-full rounded border p-1 text-left transition-colors " +
                      (active
                        ? "border-accent/60 bg-accent/10 text-accent"
                        : "border-border bg-bg-subtle text-fg-muted hover:bg-bg-muted")
                    }
                    title={t.viewer.slide(i + 1)}
                  >
                    <div className="flex aspect-video w-full items-center justify-center overflow-hidden bg-white shadow-sm">
                      <canvas
                        ref={(el) => { thumbnailCanvasRefs.current[i] = el; }}
                        className="block max-h-full max-w-full"
                      />
                    </div>
                    <div className="mt-1 text-center text-[11px] tabular-nums">{i + 1}</div>
                  </button>
                );
              })}
            </div>
          </aside>
        )}
        <div ref={scrollRef} className="relative min-h-0 flex-1 overflow-auto">
          {quoteState.banner}
          <div className={format === "xlsx"
            ? "flex min-h-full w-full p-4"
            : "flex min-h-full w-full justify-center p-4"}
          >
            <div
              ref={(el) => { pageRefs.current[0] = el; }}
              className={format === "xlsx" ? "flex min-h-[560px] w-full" : "inline-flex justify-center"}
            >
              <div
                ref={hostRef}
                className={
                  format === "xlsx"
                    ? "min-h-[560px] w-full min-w-[760px] bg-white shadow-sm"
                    : "inline-block"
                }
              >
                {format !== "xlsx" && (
                  <canvas
                    ref={canvasRef}
                    className="block bg-white shadow-sm"
                    style={{ width: "960px" }}
                  />
                )}
              </div>
            </div>
          </div>
          {!ready && !err && (
            <div className="absolute inset-0 z-20 bg-bg/80">
              <ViewerLoading />
            </div>
          )}
          {err && (
            <div className="absolute inset-0 z-20 bg-bg">
              <ViewerError msg={err} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
function ensureOoxmlCanvas(
  host: HTMLDivElement,
  canvas: HTMLCanvasElement | null,
): HTMLCanvasElement | null {
  if (!canvas) return null;
  if (!host.contains(canvas)) host.appendChild(canvas);
  return canvas;
}

function ooxmlFitWidthZoom(root: HTMLDivElement): number {
  const available = Math.max(240, root.clientWidth - DOCX_VIEWPORT_SIDE_PADDING);
  return Math.round(clamp(available / DOCX_BASE_WIDTH, VIEWER_MIN_ZOOM, VIEWER_MAX_ZOOM) * 100) / 100;
}

function buildPptxSlideSearchIndex(presentation: PptxPresentationInstance): PptxSlideSearchEntry[] {
  const data = (presentation as unknown as PptxPresentationInternal)._presentation;
  if (data?.slides?.length) {
    return data.slides.map((slide, index) => {
      const text = pptxSlideSearchText(slide, presentation.getNotes(index));
      return { text, normalized: normalizeSearchText(text) };
    });
  }
  return Array.from({ length: presentation.slideCount }, (_, index) => {
    const text = presentation.getNotes(index) ?? "";
    return { text, normalized: normalizeSearchText(text) };
  });
}

function pptxSlideSearchText(slide: PptxSlide, notes: string | null): string {
  const parts: string[] = [];
  for (const element of slide.elements) {
    if (element.type === "shape") {
      appendPptxTextBody(parts, element.textBody);
    } else if (element.type === "table") {
      for (const row of element.rows) {
        for (const cell of row.cells) appendPptxTextBody(parts, cell.textBody);
      }
    } else if (element.type === "chart") {
      appendPptxChartText(parts, element);
    }
  }
  appendPptxText(parts, slide.notes ?? notes);
  for (const comment of slide.comments ?? []) appendPptxText(parts, comment.text);
  return parts.join("\n");
}

function appendPptxTextBody(parts: string[], body: PptxTextBody | null): void {
  for (const paragraph of body?.paragraphs ?? []) {
    let text = "";
    for (const run of paragraph.runs) {
      if (run.type === "text") text += run.text;
      else if (run.type === "break") text += "\n";
    }
    appendPptxText(parts, text);
  }
}

function appendPptxChartText(parts: string[], chart: PptxChartElement): void {
  appendPptxText(parts, chart.title);
  appendPptxText(parts, chart.catAxisTitle);
  appendPptxText(parts, chart.valAxisTitle);
  appendPptxText(parts, chart.secondaryValAxis?.title);
  for (const category of chart.categories) appendPptxText(parts, category);
  for (const series of chart.series) {
    appendPptxText(parts, series.name);
    for (const category of series.categories ?? []) appendPptxText(parts, category);
    for (const label of series.dataLabelOverrides ?? []) appendPptxText(parts, label.text);
  }
}

function appendPptxText(parts: string[], value: string | null | undefined): void {
  const text = value?.trim();
  if (text) parts.push(text);
}

function findPptxQuoteSlide(index: PptxSlideSearchEntry[], quote: string): number | null {
  const exact = quote.trim();
  if (exact) {
    for (let i = 0; i < index.length; i += 1) {
      if (index[i].text.includes(exact)) return i;
    }
  }
  const needle = normalizeSearchText(quote);
  if (needle.length < 3) return null;
  for (let i = 0; i < index.length; i += 1) {
    if (index[i].normalized.includes(needle)) return i;
  }
  return null;
}
async function ooxmlGoTo(
  format: SingleCanvasOoxmlKind,
  viewer: OoxmlViewerInstance | null,
  index: number,
): Promise<void> {
  if (!viewer) return;
  if (format === "pptx") await (viewer as PptxViewerInstance).goToSlide(index);
  else await (viewer as XlsxViewerInstance).goToSheet(index);
}
