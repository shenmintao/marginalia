"""marginalia CLI rendering: glowpy-backed markdown + spinner.

Markdown is rendered by glowpy with the `claude-code` theme as the default,
plus a marginalia-flavoured override that keeps `[^a]` footnote refs as a
blue-bold tag (the agent uses these heavily and they need to pop visually).

Spinner / progress indicators are local, kb-lite-style.
"""
from __future__ import annotations

import itertools
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
from contextlib import contextmanager

from glowpy import ColorDepth, Theme, get_theme, render as _glow_render

# ---- ANSI codes (kept for spinner + commands.py imports) ------------------

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
ITALIC = "\x1b[3m"
UNDER = "\x1b[4m"

CYAN = "\x1b[36m"
GREEN = "\x1b[32m"
RED = "\x1b[31m"
BLUE = "\x1b[34m"
YELLOW = "\x1b[33m"
DIM_GREY = "\x1b[90m"

CLEAR_LINE = "\x1b[2K"
CR = "\r"


def _enable_windows_vt() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        for std_id in (-11, -12):
            handle = kernel32.GetStdHandle(std_id)
            if handle in (0, -1):
                continue
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                continue
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        return True
    except Exception:
        return False


def _supports_color() -> bool:
    if "NO_COLOR" in os.environ:
        return False
    if not sys.stdout.isatty():
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    if not _enable_windows_vt():
        return False
    return True


_COLOR = _supports_color()


# ---- Theme: claude-code with a marginalia footnote accent -----------------

def _build_theme() -> Theme:
    base = get_theme("claude-code")
    # Footnote refs/defs in marginalia are heavy-use citation markers — the
    # default italic-grey is too subtle. Use blue + bold so the eye picks them
    # out as a tag, matching the prior hand-rolled renderer's contract.
    base.footnote.color = "#7AB4E8"
    base.footnote.bold = True
    base.footnote.italic = False
    return base


_THEME = _build_theme()


def _theme_accent() -> str:
    """ANSI sequence for the theme's H1 colour — banner border + title use
    this so the box harmonises with the rest of glowpy's output. Falls back
    to standard blue if the theme didn't set one."""
    hex_color = _THEME.h1.color or "#BD93F9"
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"\x1b[38;2;{r};{g};{b}m"


# Spelled-out so the banner can call it without re-parsing every render
_THEME_ACCENT = _theme_accent()


# ---- markdown rendering ---------------------------------------------------

_TABLE_DELIMITER_CELL_RE = re.compile(r"^:?-{3,}:?$")
_ENTRY_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(entry:([^)]+)\)")
_RAW_ENTRY_FOOTNOTE_RE = re.compile(
    r"(^\[\^[^\]]+\]:\s*)entry_id\s*=\s*`?"
    r"([0-9a-fA-F][0-9a-fA-F\-]{6,35})`?"
    r"((?:\s*[,，]\s*[^-—–\n]+)*)"
    r"(?:\s*[-—–]\s*(.*))?$",
    re.MULTILINE,
)


def _split_table_cells(line: str) -> list[str]:
    if "|" not in line:
        return []
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def _is_table_header_line(line: str) -> bool:
    cells = _split_table_cells(line)
    return len(cells) >= 2 and any(cells)


def _is_table_delimiter_line(line: str) -> bool:
    cells = _split_table_cells(line)
    return (
        len(cells) >= 2
        and all(_TABLE_DELIMITER_CELL_RE.fullmatch(cell) for cell in cells)
    )


def _strip_blockquote_prefix(line: str) -> str:
    stripped = line.lstrip()
    if not stripped.startswith(">"):
        return line
    rest = stripped[1:]
    if rest.startswith(" "):
        rest = rest[1:]
    return rest


def _strip_fence_indent(line: str) -> str | None:
    text = line.rstrip("\r\n")
    idx = 0
    col = 0
    for ch in text:
        if ch == " ":
            idx += 1
            col += 1
        elif ch == "\t":
            idx += 1
            col += 4
        else:
            break
        if col >= 4:
            return None
    return text[idx:]


def _parse_fence_open(line: str) -> tuple[str, int, bool, bool] | None:
    trimmed = _strip_fence_indent(line)
    if trimmed is None:
        return None
    text = trimmed.lstrip()
    is_blockquoted = text.startswith(">")
    scan = _strip_blockquote_prefix(text) if is_blockquoted else text
    if not scan.startswith(("```", "~~~")):
        return None
    marker = scan[0]
    marker_len = len(scan) - len(scan.lstrip(marker))
    if marker_len < 3:
        return None
    info = scan[marker_len:].strip()
    is_markdown = info.lower() in ("md", "markdown")
    return marker, marker_len, is_blockquoted, is_markdown


def _is_fence_close(
    line: str, *, marker: str, marker_len: int, is_blockquoted: bool
) -> bool:
    trimmed = _strip_fence_indent(line)
    if trimmed is None:
        return False
    text = trimmed.lstrip()
    if is_blockquoted:
        if not text.startswith(">"):
            return False
        text = _strip_blockquote_prefix(text)
    if not text.startswith(marker * marker_len):
        return False
    close_len = len(text) - len(text.lstrip(marker))
    return close_len >= marker_len and not text[close_len:].strip()


def _markdown_lines_contain_table(
    lines: list[str], *, is_blockquoted: bool
) -> bool:
    previous: str | None = None
    for line in lines:
        text = line.rstrip("\r\n")
        if is_blockquoted:
            text = _strip_blockquote_prefix(text)
        text = text.strip()
        if not text:
            previous = None
            continue
        if (
            previous is not None
            and _is_table_header_line(previous)
            and not _is_table_delimiter_line(previous)
            and _is_table_delimiter_line(text)
        ):
            return True
        previous = text
    return False


def _unwrap_markdown_table_fences(md: str) -> str:
    """Unwrap ```md/```markdown fences only when they contain tables.

    Codex TUI does this before rendering agent output because models often
    wrap Markdown tables in Markdown fences, which would otherwise render as
    code instead of a table.
    """
    if "```" not in md and "~~~" not in md:
        return md

    lines = md.splitlines(keepends=True)
    out: list[str] = []
    changed = False
    i = 0
    while i < len(lines):
        parsed = _parse_fence_open(lines[i])
        if parsed is None:
            out.append(lines[i])
            i += 1
            continue

        marker, marker_len, is_blockquoted, is_markdown = parsed
        opening = lines[i]
        content: list[str] = []
        j = i + 1
        while j < len(lines) and not _is_fence_close(
            lines[j],
            marker=marker,
            marker_len=marker_len,
            is_blockquoted=is_blockquoted,
        ):
            content.append(lines[j])
            j += 1

        if j >= len(lines):
            out.append(opening)
            out.extend(content)
            break

        closing = lines[j]
        if is_markdown and _markdown_lines_contain_table(
            content, is_blockquoted=is_blockquoted
        ):
            out.extend(content)
            changed = True
        else:
            out.append(opening)
            out.extend(content)
            out.append(closing)
        i = j + 1

    return "".join(out) if changed else md


def _render_width(width: int | None, *, reserve_columns: int = 0) -> int:
    if width is not None:
        return max(0, int(width))
    if not sys.stdout.isatty():
        return 0
    try:
        columns = shutil.get_terminal_size(fallback=(100, 24)).columns
    except Exception:
        return 0
    return max(20, columns - max(0, reserve_columns))


def _entry_locator_suffix(target: str) -> str:
    entry_id, _, query = target.partition("?")
    short = entry_id.strip()[:8] or "unknown"
    parts = [f"entry {short}"]
    if query:
        params = urllib.parse.parse_qs(query, keep_blank_values=True)
        page = (params.get("page") or [""])[0]
        quote = (params.get("q") or [""])[0]
        if page:
            parts.append(f"page {page}")
        elif quote:
            compact = " ".join(quote.split())
            if len(compact) > 48:
                compact = compact[:45].rstrip() + "..."
            parts.append(f'q="{compact}"')
    return "; ".join(parts)


def _replace_entry_link(match: re.Match[str]) -> str:
    label = match.group(1).strip()
    target = match.group(2).strip()
    return f"{label} ({_entry_locator_suffix(target)})"


def _replace_raw_entry_footnote(match: re.Match[str]) -> str:
    prefix = match.group(1)
    entry_id = match.group(2)
    fields = match.group(3) or ""
    reason = (match.group(4) or "").strip()
    parts = [f"entry {entry_id[:8]}"]
    page_match = re.search(
        r"page\s*=\s*`?([0-9]+(?:-[0-9]+)?)`?", fields, re.IGNORECASE
    )
    quote_match = re.search(r'quote\s*=\s*"((?:[^"\\]|\\.)*)"', fields)
    if page_match:
        parts.append(f"page {page_match.group(1)}")
    elif quote_match:
        quote = quote_match.group(1).replace(r"\"", '"').replace(r"\\", "\\")
        quote = " ".join(quote.split())
        if len(quote) > 48:
            quote = quote[:45].rstrip() + "..."
        parts.append(f'q="{quote}"')
    text = prefix + "; ".join(parts)
    if reason:
        text += f" — {reason}"
    return text


def _normalize_cli_footnotes(md: str) -> str:
    if "entry:" not in md and "entry_id" not in md:
        return md
    out = _ENTRY_LINK_RE.sub(_replace_entry_link, md)
    return _RAW_ENTRY_FOOTNOTE_RE.sub(_replace_raw_entry_footnote, out)


def render_markdown(
    md: str,
    *,
    width: int | None = None,
    reserve_columns: int = 0,
) -> str:
    """Return an ANSI-rendered version of `md` using the claude-code theme.

    When colour is unsupported (no TTY, NO_COLOR, TERM=dumb, or VT enable
    fails on Windows), we still run the layout pass but emit no SGR codes —
    callers like the table renderer rely on the visible structure (borders,
    indentation) regardless of whether colour is on."""
    try:
        depth = None if _COLOR else ColorDepth.NONE
        normalized = _normalize_cli_footnotes(_unwrap_markdown_table_fences(md))
        return _glow_render(
            normalized,
            theme=_THEME,
            hyperlinks=_COLOR,
            color_depth=depth,
            width=_render_width(width, reserve_columns=reserve_columns),
        ).rstrip("\n")
    except Exception:
        return md


def print_markdown(md: str) -> None:
    print(render_markdown(md))


def render_table(rows: list[str]) -> str:
    """Render a list of pipe-delimited markdown table rows.

    Kept for backward compat: a few callers and tests pass raw `| a | b |`
    lines without a separator row. We re-join them as a markdown fragment
    and let glowpy handle layout."""
    if not rows:
        return ""
    md = "\n".join(rows)
    # glowpy/markdown-it requires a separator row to recognise a table;
    # synthesise one from the first row's column count when missing.
    first = rows[0].strip()
    cols = max(1, first.count("|") - 1)
    sep = "|" + "|".join([" --- "] * cols) + "|"
    md_with_sep = rows[0] + "\n" + sep + "\n" + "\n".join(rows[1:])
    depth = None if _COLOR else ColorDepth.NONE
    out = _glow_render(
        md_with_sep, theme=_THEME, hyperlinks=False, color_depth=depth
    )
    return out.rstrip("\n")


# ---- startup banner (claude-code-style rounded box) ----------------------

# claude-code uses a rounded rectangle borrowing its theme `claude` color for
# the border and a small ASCII mascot inside. We borrow the structure but
# keep marginalia's own visual identity — a stack of dog-eared pages, since
# this is a personal library. The whole thing degrades to plain text when
# colour is off (NO_COLOR / pipes / Windows VT off).

_BANNER_BOX_TOP_L = "╭"
_BANNER_BOX_TOP_R = "╮"
_BANNER_BOX_BOT_L = "╰"
_BANNER_BOX_BOT_R = "╯"
_BANNER_BOX_H = "─"
_BANNER_BOX_V = "│"

_BANNER_BOX_ASCII = {
    _BANNER_BOX_TOP_L: "+", _BANNER_BOX_TOP_R: "+",
    _BANNER_BOX_BOT_L: "+", _BANNER_BOX_BOT_R: "+",
    _BANNER_BOX_H: "-", _BANNER_BOX_V: "|",
}


def _banner_glyphs() -> dict[str, str]:
    """Pick box-drawing chars the current stdout encoding can actually
    print. Modern terminals (Windows Terminal, iTerm, gnome-terminal)
    handle the rounded box; legacy cmd.exe under cp936/cp1252 can encode
    these to bytes via its codepage but the glyphs render as mojibake,
    so fall back to ASCII for anything other than UTF-8."""
    enc = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
    if enc in ("utf8", "utf16", "utf32") or enc.startswith("utf"):
        return {ch: ch for ch in _BANNER_BOX_ASCII}
    return dict(_BANNER_BOX_ASCII)

_BANNER_MASCOT = (
    "  .------.  ",
    "  |======|  ",
    "  |------|  ",
    "  '------'  ",
)


def _ansi_strip_len(s: str) -> int:
    """Visible length, ignoring ANSI SGR escapes — needed for box padding."""
    out = []
    skip = False
    for ch in s:
        if skip:
            if ch == "m":
                skip = False
            continue
        if ch == "\x1b":
            skip = True
            continue
        out.append(ch)
    # Treat box-drawing / CJK width as 1 here; banner content is ASCII.
    return len(out)


def render_banner(
    title: str,
    lines: list[str],
    *,
    width: int = 62,
) -> str:
    """Render a rounded-box banner with `title` in the top border and
    `lines` (already-coloured strings) inside. Mascot tucks against the
    right edge of the inner area when there's room."""
    color = _THEME_ACCENT if _COLOR else ""
    reset = RESET if _COLOR else ""
    g = _banner_glyphs()
    tl, tr = g[_BANNER_BOX_TOP_L], g[_BANNER_BOX_TOP_R]
    bl, br = g[_BANNER_BOX_BOT_L], g[_BANNER_BOX_BOT_R]
    h, v = g[_BANNER_BOX_H], g[_BANNER_BOX_V]

    # Top border: ╭─ <title> ──────╮
    title_text = f" {title} "
    title_inner = f"{BOLD}{title_text}{RESET}" if _COLOR else title_text
    fill = max(2, width - 2 - len(title_text) - 1)
    top = (
        f"{color}{tl}{h}{reset}"
        f"{title_inner}"
        f"{color}{h * fill}{tr}{reset}"
    )

    # Body rows: │  <line>                     <mascot row>  │
    inner_w = width - 4  # two side borders + one space padding each side
    mascot_w = len(_BANNER_MASCOT[0])
    body_rows: list[str] = []
    nrows = max(len(lines), len(_BANNER_MASCOT))
    for i in range(nrows):
        text = lines[i] if i < len(lines) else ""
        mascot = _BANNER_MASCOT[i] if i < len(_BANNER_MASCOT) else " " * mascot_w
        text_w = _ansi_strip_len(text)
        gap = max(1, inner_w - text_w - mascot_w)
        line = f"{text}{' ' * gap}{color}{mascot}{reset}"
        body_rows.append(
            f"{color}{v}{reset} {line} {color}{v}{reset}"
        )

    bottom = f"{color}{bl}{h * (width - 2)}{br}{reset}"
    return "\n".join([top, *body_rows, bottom])


def print_banner(title: str, lines: list[str], *, width: int = 62) -> None:
    print(render_banner(title, lines, width=width))


# ---- spinner (claude-code style: breathing star + rotating verb) ---------

# Mirrors claude-code/components/Spinner/utils.ts:getDefaultCharacters().
# `*` instead of `✳` on non-darwin keeps cmd.exe / Windows Terminal happy.
_BASE_FRAMES = ("·", "✢", "*", "✶", "✻", "✽")
SPINNER_FRAMES = _BASE_FRAMES + tuple(reversed(_BASE_FRAMES))

# Subset of claude-code's SPINNER_VERBS (constants/spinnerVerbs.ts) — enough
# variety that the spinner doesn't feel canned, short enough that the eye
# doesn't need to chase a long word as it cycles.
SPINNER_VERBS = (
    "Brewing", "Cooking", "Crafting", "Composing", "Computing",
    "Considering", "Contemplating", "Crunching", "Deliberating",
    "Deciphering", "Distilling", "Forging", "Mulling", "Musing",
    "Pondering", "Processing", "Reasoning", "Reflecting", "Resolving",
    "Synthesizing", "Thinking", "Weaving", "Working", "Wrangling",
)
_VERB_TICK_S = 3.0  # rotate verb every ~3s while spinning


def short_duration(seconds: float) -> str:
    """`Nms` / `X.Ys` / `XmYs` / `XhYm`."""
    if seconds < 1:
        return f"{int(seconds * 1000)}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    m = int(seconds // 60)
    s = int(seconds - m * 60)
    if m < 60:
        return f"{m}m {s}s"
    h = m // 60
    m = m % 60
    return f"{h}h {m}m"


class Spinner:
    """Animates one indented step line.

    Render pattern:
      while running:   `  ⠋ <label>  3.1s`        (BLUE spinner + DIM elapsed)
      after finish():  `  <label>  3.1s`          (whole line dim, kept in scrollback)
      after fail():    `  ✗  <label>  3.1s`        (RED marker, message kept)

    No-op when stdout is not a TTY so piped output stays clean.
    """

    def __init__(self, label: str = "", indent: int = 2) -> None:
        self._label = label
        self._indent = " " * indent
        self._frames = itertools.cycle(SPINNER_FRAMES)
        self._verbs = itertools.cycle(SPINNER_VERBS)
        self._verb = next(self._verbs)
        self._verb_at = time.monotonic()
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._t0 = time.monotonic()
        self._committed = False
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
            now = time.monotonic()
            if now - self._verb_at >= _VERB_TICK_S:
                self._verb = next(self._verbs)
                self._verb_at = now
            elapsed = short_duration(now - self._t0)
            label = self._label or f"{self._verb}…"
            sys.stdout.write(
                f"{CR}{CLEAR_LINE}{self._indent}{BLUE}{frame}{RESET} "
                f"{label}  {DIM}{elapsed}{RESET}"
            )
            sys.stdout.flush()
            time.sleep(0.12)

    def _stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=0.2)
        self._stop_event = None
        self._thread = None

    def _commit(self, marker: str, color: str | None, label: str | None) -> None:
        if self._committed:
            return
        self._committed = True
        self._stop()
        if not self._enabled:
            return
        msg = label if label is not None else self._label
        elapsed = short_duration(time.monotonic() - self._t0)
        if color is None:
            line = f"{DIM}{self._indent}{msg}  {elapsed}{RESET}"
        else:
            line = (
                f"{self._indent}{color}{marker}{RESET} {msg}  "
                f"{DIM}{elapsed}{RESET}"
            )
        sys.stdout.write(f"{CR}{CLEAR_LINE}{line}\n")
        sys.stdout.flush()

    def finish(self, label: str | None = None) -> None:
        self._commit("✓", GREEN, label)

    def fail(self, label: str | None = None) -> None:
        self._commit("✗", RED, label)

    def __enter__(self) -> "Spinner":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            self.fail(str(exc) if exc else None)
        else:
            self.finish()


@contextmanager
def spinner(label: str):
    sp = Spinner(label).start()
    try:
        yield sp
        sp.finish()
    except Exception as exc:
        sp.fail(str(exc))
        raise
