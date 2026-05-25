"""marginalia CLI rendering: glowpy-backed markdown + spinner.

Markdown is rendered by glowpy with the `claude-code` theme as the default,
plus a marginalia-flavoured override that keeps `[^a]` footnote refs as a
blue-bold tag (the agent uses these heavily and they need to pop visually).

Spinner / progress indicators are local, kb-lite-style.
"""
from __future__ import annotations

import itertools
import os
import sys
import threading
import time
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

def render_markdown(md: str) -> str:
    """Return an ANSI-rendered version of `md` using the claude-code theme.

    When colour is unsupported (no TTY, NO_COLOR, TERM=dumb, or VT enable
    fails on Windows), we still run the layout pass but emit no SGR codes —
    callers like the table renderer rely on the visible structure (borders,
    indentation) regardless of whether colour is on."""
    try:
        depth = None if _COLOR else ColorDepth.NONE
        return _glow_render(
            md, theme=_THEME, hyperlinks=_COLOR, color_depth=depth
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
