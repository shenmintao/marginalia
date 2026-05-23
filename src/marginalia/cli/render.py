"""Minimal markdown→ANSI renderer in Claude Code's spirit.

Rules:
  - Headings (#  ##  ###) → reverse video on the title text only,
    flushed left, single line. No big bordered blocks.
  - Code fences ``` → indented block, dim color, no boxes.
  - Inline `code` → cyan.
  - **bold** / __bold__ → bold. *italic* / _italic_ → italic.
  - Lists ('- ' / '* ' / '1. ') → keep as-is, lightly indent nested.
  - Blockquotes (> ) → vertical bar prefix in dim.
  - Footnote refs `[^a]` → kept literal (Marginalia agent uses these).
  - Links `[text](url)` → text underlined; url shown afterwards in dim.
  - Tables → aligned column output with dim borders.
  - Hr (---) → dim '────'.

Designed to be readable in monospace terminals. No third-party dependency.

Spinner / progress indicators inspired by claw-code's `render.rs`.
"""
from __future__ import annotations

import itertools
import os
import re
import sys
import threading
import time
from contextlib import contextmanager

# ---- ANSI codes ------------------------------------------------------------

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
ITALIC = "\x1b[3m"
UNDER = "\x1b[4m"
REV = "\x1b[7m"

CYAN = "\x1b[36m"
GREEN = "\x1b[32m"
RED = "\x1b[31m"
BLUE = "\x1b[34m"
YELLOW = "\x1b[33m"
DIM_GREY = "\x1b[90m"

CLEAR_LINE = "\x1b[2K"
CR = "\r"


def _supports_color() -> bool:
    if "NO_COLOR" in os.environ:
        return False
    if not sys.stdout.isatty():
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    return True


_COLOR = _supports_color()


def _wrap(s: str, *codes: str) -> str:
    if not _COLOR or not s:
        return s
    return "".join(codes) + s + RESET


# ---- inline rendering ------------------------------------------------------

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__")
_ITALIC = re.compile(r"(?<![*\w])\*([^*]+)\*(?!\w)|(?<![_\w])_([^_]+)_(?!\w)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _render_inline(text: str) -> str:
    text = _INLINE_CODE.sub(lambda m: _wrap(m.group(1), CYAN), text)
    text = _BOLD.sub(lambda m: _wrap(m.group(1) or m.group(2), BOLD), text)
    text = _ITALIC.sub(lambda m: _wrap(m.group(1) or m.group(2), ITALIC), text)
    text = _LINK.sub(
        lambda m: _wrap(m.group(1), UNDER) + " " + _wrap(f"({m.group(2)})", DIM),
        text,
    )
    return text


# ---- block rendering ------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_HR = re.compile(r"^\s*([-*_])\s*\1\s*\1\s*$")
_FENCE = re.compile(r"^\s*```([^\s`]*)\s*$")
_BLOCKQUOTE = re.compile(r"^>\s?(.*)$")


def render_markdown(md: str) -> str:
    """Return an ANSI-coloured rendering of `md`. Always returns a string;
    when colour is unsupported the markup is stripped to plain text."""
    out: list[str] = []
    in_fence = False
    fence_lang: str | None = None
    table_buf: list[str] = []
    raw_iter = iter(md.splitlines())
    for raw in raw_iter:
        if in_fence:
            if _FENCE.match(raw):
                in_fence = False
                fence_lang = None
                continue
            out.append(_wrap("    " + raw, DIM_GREY))
            continue

        m = _FENCE.match(raw)
        if m:
            in_fence = True
            fence_lang = m.group(1) or None
            if fence_lang:
                out.append(_wrap(f"  ┄ {fence_lang}", DIM))
            continue

        # detect markdown table block: line starts with `|` and we can read a
        # contiguous run of `|`-prefixed lines.
        if _looks_like_table_row(raw):
            table_buf.append(raw)
            continue
        elif table_buf:
            # flush accumulated table
            out.append(render_table(table_buf))
            table_buf.clear()
            # fall through to handle current `raw` normally

        m = _HEADING.match(raw)
        if m:
            level = len(m.group(1))
            title = m.group(2)
            if level == 1:
                rendered = _wrap(f" {title} ", REV, BOLD)
            elif level == 2:
                rendered = _wrap(f" {title} ", REV)
            else:
                rendered = _wrap(title, BOLD)
            out.append("")
            out.append(rendered)
            continue

        m = _BLOCKQUOTE.match(raw)
        if m:
            inner = _render_inline(m.group(1))
            out.append(_wrap("│ ", DIM_GREY) + inner)
            continue

        if _HR.match(raw):
            out.append(_wrap("────────────────────────────────────────", DIM_GREY))
            continue

        if not raw.strip():
            out.append("")
            continue

        out.append(_render_inline(raw))

    if table_buf:
        out.append(render_table(table_buf))

    return "\n".join(out)


def print_markdown(md: str) -> None:
    print(render_markdown(md))


# ---- table rendering ------------------------------------------------------

_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*[:\- ]+\s*(\|\s*[:\- ]+\s*)*\|?\s*$")


def _looks_like_table_row(line: str) -> bool:
    """A markdown table row starts and ends with `|`."""
    return bool(_TABLE_ROW_RE.match(line))


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def render_table(rows: list[str]) -> str:
    """Render markdown table lines as aligned columns with dim borders.

    The second row (if a `--- | --- | ---` separator) is consumed as the
    header rule but not emitted; the first row becomes a bold header."""
    if not rows:
        return ""
    parsed: list[list[str]] = []
    has_header = False
    for i, line in enumerate(rows):
        if i == 1 and _TABLE_SEPARATOR_RE.match(line):
            has_header = True
            continue
        parsed.append(_split_row(line))
    if not parsed:
        return ""
    n_cols = max(len(r) for r in parsed)
    for r in parsed:
        while len(r) < n_cols:
            r.append("")
    widths = [
        max(_visible_len(r[c]) for r in parsed)
        for c in range(n_cols)
    ]
    sep = _wrap("│", DIM_GREY)

    def _fmt_row(cells: list[str], *, bold: bool = False) -> str:
        formatted: list[str] = []
        for c, cell in enumerate(cells):
            pad = widths[c] - _visible_len(cell)
            inner = cell + " " * pad
            if bold:
                inner = _wrap(inner, BOLD)
            formatted.append(" " + inner + " ")
        return sep + sep.join(formatted) + sep

    border_chars = "─" * (sum(widths) + n_cols * 3 + 1)
    border = _wrap(border_chars, DIM_GREY)

    out_lines = [border]
    for i, row in enumerate(parsed):
        out_lines.append(_fmt_row(row, bold=(has_header and i == 0)))
        if has_header and i == 0:
            out_lines.append(border)
    out_lines.append(border)
    return "\n".join(out_lines)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible_len(s: str) -> int:
    """Length of `s` excluding ANSI escape sequences."""
    return len(_ANSI_ESCAPE_RE.sub("", s))


# ---- spinner --------------------------------------------------------------

SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


class Spinner:
    """A simple animated spinner for "agent is working" indication.

    Use as a context manager:
        with Spinner("调用 search_journal..."):
            await tool.handler(...)

    Or manually:
        sp = Spinner("...").start()
        sp.update("changed label")
        sp.finish("done")           # green ✓
        sp.fail("error message")    # red ✗

    No-op when stdout is not a TTY (so piped output stays clean).
    """

    def __init__(self, label: str = "") -> None:
        self._label = label
        self._frames = itertools.cycle(SPINNER_FRAMES)
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._enabled = (
            sys.stdout.isatty()
            and "NO_COLOR" not in os.environ
            and os.environ.get("TERM", "") != "dumb"
        )

    def update(self, label: str) -> None:
        self._label = label

    def start(self) -> "Spinner":
        if not self._enabled or self._thread is not None:
            return self
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        while self._stop_event is not None and not self._stop_event.is_set():
            frame = next(self._frames)
            sys.stdout.write(f"{CR}{CLEAR_LINE}{BLUE}{frame} {self._label}{RESET}")
            sys.stdout.flush()
            time.sleep(0.08)

    def _stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=0.2)
        self._stop_event = None
        self._thread = None

    def finish(self, label: str | None = None) -> None:
        self._stop()
        if not self._enabled:
            return
        msg = label if label is not None else self._label
        sys.stdout.write(f"{CR}{CLEAR_LINE}{GREEN}✓ {msg}{RESET}\n")
        sys.stdout.flush()

    def fail(self, label: str | None = None) -> None:
        self._stop()
        if not self._enabled:
            return
        msg = label if label is not None else self._label
        sys.stdout.write(f"{CR}{CLEAR_LINE}{RED}✗ {msg}{RESET}\n")
        sys.stdout.flush()

    def __enter__(self) -> "Spinner":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            self.fail(str(exc) if exc else None)
        else:
            self.finish()


@contextmanager
def spinner(label: str):
    """Functional shortcut: `with spinner('working...'): ...`."""
    sp = Spinner(label).start()
    try:
        yield sp
        sp.finish()
    except Exception as exc:
        sp.fail(str(exc))
        raise
