import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Download, ZoomIn, ZoomOut } from "lucide-react";

import {
  ViewerError,
  ViewerLoading,
  ViewerToolbarButton,
  clampInt,
} from "./ViewerShared";

type EpubBookInstance = import("epubjs").Book;
type EpubRenditionInstance = import("epubjs").Rendition;
type EpubLocation = import("epubjs").Location;
type EpubSearchMatch = { cfi?: unknown; excerpt?: unknown };
interface EpubSectionSearchable {
  linear?: boolean;
  load: (request?: unknown) => Promise<unknown>;
  search?: (query: string, maxSeqEle?: number) => EpubSearchMatch[];
  find?: (query: string) => EpubSearchMatch[];
  unload?: () => void;
}
const EPUB_MIN_FONT = 80;
const EPUB_MAX_FONT = 160;
const EPUB_FONT_STEP = 10;
const EPUB_LOCATION_CHARS = 1000;
const EPUB_QUOTE_CANDIDATE_MAX = 80;

interface EpubProgress {
  atStart: boolean;
  atEnd: boolean;
  currentPage: number | null;
  totalPages: number | null;
}

function epubProgress(book: EpubBookInstance | null, loc: EpubLocation | null | undefined): EpubProgress {
  const totalPages = book ? book.locations.length() : 0;
  const progress: EpubProgress = {
    atStart: Boolean(loc?.atStart),
    atEnd: Boolean(loc?.atEnd),
    currentPage: null,
    totalPages: totalPages > 0 ? totalPages : null,
  };
  const cfi = loc?.start?.cfi;
  if (!book || !cfi || totalPages <= 0) return progress;
  const rawLocation = book.locations.locationFromCfi(cfi) as unknown;
  const locationIndex = typeof rawLocation === "number"
    ? rawLocation
    : Number(rawLocation);
  if (!Number.isFinite(locationIndex) || locationIndex < 0) return progress;
  const clamped = clampInt(locationIndex, 0, Math.max(0, totalPages - 1));
  progress.currentPage = clamped + 1;
  return progress;
}

function epubQuoteCandidates(quote: string): string[] {
  const normalized = quote.replace(/\s+/g, " ").trim();
  const raw = quote.trim();
  const candidates = [
    raw,
    normalized,
    normalized.slice(0, EPUB_QUOTE_CANDIDATE_MAX).trim(),
  ].filter((value) => value.length >= 4);
  return [...new Set(candidates)];
}

async function findEpubQuoteCfi(
  book: EpubBookInstance,
  quote: string,
): Promise<string | null> {
  const candidates = epubQuoteCandidates(quote);
  if (candidates.length === 0) return null;
  await book.ready;

  const sections: EpubSectionSearchable[] = [];
  const spine = book.spine as unknown as {
    each: (fn: (section: EpubSectionSearchable) => void) => void;
  };
  spine.each((section) => {
    if (section.linear !== false) sections.push(section);
  });

  const request = (book as unknown as { request?: unknown }).request;
  for (const section of sections) {
    let loaded = false;
    try {
      await section.load(request);
      loaded = true;
      for (const candidate of candidates) {
        const matches = section.search
          ? section.search(candidate, 8)
          : section.find?.(candidate) || [];
        const cfi = matches.find((match) => typeof match.cfi === "string")?.cfi;
        if (typeof cfi === "string" && cfi) return cfi;
      }
    } catch {
      continue;
    } finally {
      if (loaded) section.unload?.();
    }
  }
  return null;
}

export function EpubView({ url, name, downloadUrl, quote, page, onScrolled }: {
  url: string;
  name: string;
  downloadUrl: string;
  quote: string | null;
  page: number | null;
  onScrolled?: () => void;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const pageInputRef = useRef<HTMLInputElement>(null);
  const bookRef = useRef<EpubBookInstance | null>(null);
  const renditionRef = useRef<EpubRenditionInstance | null>(null);
  const appliedLocatorRef = useRef<string | null>(null);
  const [ready, setReady] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [fontSize, setFontSize] = useState(100);
  const [pageInput, setPageInput] = useState("");
  const [location, setLocation] = useState<EpubProgress>({
    atStart: true,
    atEnd: false,
    currentPage: null,
    totalPages: null,
  });

  const updateLocation = useCallback((loc: EpubLocation | null | undefined) => {
    const progress = epubProgress(bookRef.current, loc);
    setLocation(progress);
    if (document.activeElement !== pageInputRef.current) {
      setPageInput(progress.currentPage == null ? "" : String(progress.currentPage));
    }
  }, []);

  const goToPage = useCallback(async (rawPage: number) => {
    const book = bookRef.current;
    const rendition = renditionRef.current;
    const total = location.totalPages;
    if (!book || !rendition || !total) return;
    const targetPage = clampInt(rawPage, 1, total);
    const cfi = book.locations.cfiFromLocation(targetPage - 1) as unknown;
    if (typeof cfi !== "string" || !cfi || cfi === "-1") return;
    setPageInput(String(targetPage));
    await rendition.display(cfi);
    updateLocation(rendition.currentLocation() as unknown as EpubLocation | null);
  }, [location.totalPages, updateLocation]);

  const goToQuote = useCallback(async (rawQuote: string): Promise<boolean> => {
    const book = bookRef.current;
    const rendition = renditionRef.current;
    if (!book || !rendition) return false;
    const cfi = await findEpubQuoteCfi(book, rawQuote);
    if (!cfi) return false;
    await rendition.display(cfi);
    updateLocation(rendition.currentLocation() as unknown as EpubLocation | null);
    return true;
  }, [updateLocation]);

  const commitPageInput = useCallback(() => {
    const parsed = parseInt(pageInput, 10);
    if (!Number.isFinite(parsed)) {
      setPageInput(location.currentPage == null ? "" : String(location.currentPage));
      return;
    }
    void goToPage(parsed);
  }, [goToPage, location.currentPage, pageInput]);

  useEffect(() => {
    let cancelled = false;
    const markReady = () => {
      if (!cancelled) setReady(true);
    };
    setReady(false);
    setErr(null);
    setPageInput("");
    setLocation({
      atStart: true,
      atEnd: false,
      currentPage: null,
      totalPages: null,
    });
    const host = hostRef.current;
    host?.replaceChildren();

    (async () => {
      try {
        const { default: ePub } = await import("epubjs");
        if (cancelled || !hostRef.current) return;
        const book = ePub(url, { openAs: "epub" });
        const rendition = book.renderTo(hostRef.current, {
          width: "100%",
          height: "100%",
          spread: "none",
          flow: "paginated",
        });
        bookRef.current = book;
        renditionRef.current = rendition;
        rendition.themes.fontSize(`${fontSize}%`);
        rendition.on("rendered", markReady);
        rendition.on("displayed", markReady);
        rendition.on("relocated", (loc: EpubLocation) => {
          if (cancelled) return;
          markReady();
          updateLocation(loc);
        });
        await rendition.display();
        markReady();
        updateLocation(rendition.currentLocation() as unknown as EpubLocation | null);
        void book.ready.then(async () => {
          if (cancelled) return;
          (book.locations as unknown as { pause?: number }).pause = 0;
          await book.locations.generate(EPUB_LOCATION_CHARS);
          if (cancelled || renditionRef.current !== rendition) return;
          updateLocation(rendition.currentLocation() as unknown as EpubLocation | null);
        }).catch((error: unknown) => {
          if (!cancelled) setErr(error instanceof Error ? error.message : String(error));
        });
      } catch (error) {
        if (!cancelled) setErr(error instanceof Error ? error.message : String(error));
      }
    })();

    return () => {
      cancelled = true;
      try {
        renditionRef.current?.destroy();
      } catch {
        /* Best-effort cleanup for third-party viewer teardown. */
      }
      try {
        bookRef.current?.destroy();
      } catch {
        /* Best-effort cleanup for third-party viewer teardown. */
      }
      renditionRef.current = null;
      bookRef.current = null;
      hostRef.current?.replaceChildren();
    };
  }, [updateLocation, url]);

  useEffect(() => {
    if (!ready) return;
    if (quote?.trim()) {
      const key = `${url}:q:${quote}`;
      if (appliedLocatorRef.current === key) return;
      appliedLocatorRef.current = key;
      void goToQuote(quote).then((found) => {
        if (found) onScrolled?.();
      });
      return;
    }
    if (!location.totalPages || page == null || page < 1) return;
    const key = `${url}:${page}:${location.totalPages}`;
    if (appliedLocatorRef.current === key) return;
    appliedLocatorRef.current = key;
    void goToPage(page).then(onScrolled);
  }, [goToPage, goToQuote, location.totalPages, onScrolled, page, quote, ready, url]);

  useEffect(() => {
    renditionRef.current?.themes.fontSize(`${fontSize}%`);
  }, [fontSize]);

  useEffect(() => {
    const host = hostRef.current;
    const rendition = renditionRef.current;
    if (!host || !rendition) return;
    const resize = () => rendition.resize(host.clientWidth, host.clientHeight);
    const observer = typeof ResizeObserver !== "undefined"
      ? new ResizeObserver(resize)
      : null;
    observer?.observe(host);
    window.addEventListener("resize", resize);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", resize);
    };
  }, [ready]);

  const canZoomOut = ready && fontSize > EPUB_MIN_FONT;
  const canZoomIn = ready && fontSize < EPUB_MAX_FONT;
  const totalPages = location.totalPages ?? 0;
  const canJump = ready && totalPages > 0;

  return (
    <div className="flex h-full min-h-0 flex-col bg-bg-subtle">
      <div className="flex h-10 shrink-0 items-center gap-2 overflow-x-auto border-b border-border bg-bg px-3 text-xs text-fg-muted">
        <div className="flex items-center gap-1">
          <ViewerToolbarButton
            title="Previous page"
            disabled={!ready || location.atStart}
            onClick={() => void renditionRef.current?.prev()}
          >
            <ChevronLeft size={14} />
          </ViewerToolbarButton>
          <input
            ref={pageInputRef}
            value={pageInput}
            disabled={!canJump}
            onChange={(event) => setPageInput(event.target.value)}
            onFocus={(event) => event.currentTarget.select()}
            onBlur={commitPageInput}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.currentTarget.blur();
              } else if (event.key === "Escape") {
                setPageInput(location.currentPage == null ? "" : String(location.currentPage));
                event.currentTarget.blur();
              }
            }}
            inputMode="numeric"
            className="h-7 w-12 rounded border border-border bg-bg px-1.5 text-center text-xs text-fg-base tabular-nums outline-none focus:border-accent disabled:opacity-50"
            aria-label="EPUB page"
          />
          <span className="min-w-10 text-center tabular-nums">/ {totalPages || "-"}</span>
          <ViewerToolbarButton
            title="Next page"
            disabled={!ready || location.atEnd}
            onClick={() => void renditionRef.current?.next()}
          >
            <ChevronRight size={14} />
          </ViewerToolbarButton>
        </div>
        <div className="h-5 w-px shrink-0 bg-border" />
        <div className="flex items-center gap-1">
          <ViewerToolbarButton
            title="Decrease text size"
            disabled={!canZoomOut}
            onClick={() => setFontSize((value) => clampInt(value - EPUB_FONT_STEP, EPUB_MIN_FONT, EPUB_MAX_FONT))}
          >
            <ZoomOut size={14} />
          </ViewerToolbarButton>
          <span className="min-w-14 text-center tabular-nums">{fontSize}%</span>
          <ViewerToolbarButton
            title="Increase text size"
            disabled={!canZoomIn}
            onClick={() => setFontSize((value) => clampInt(value + EPUB_FONT_STEP, EPUB_MIN_FONT, EPUB_MAX_FONT))}
          >
            <ZoomIn size={14} />
          </ViewerToolbarButton>
        </div>
        <div className="ml-auto flex items-center gap-1">
          <a
            href={downloadUrl}
            download={name}
            title="Download"
            className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded border border-border text-fg-muted transition-colors hover:bg-bg-muted"
          >
            <Download size={14} />
          </a>
        </div>
      </div>
      <div className="relative min-h-0 flex-1 bg-bg">
        <div ref={hostRef} className="h-full w-full" />
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
  );
}
