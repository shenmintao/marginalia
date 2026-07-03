"""Run an LLM-supplied regex over text in a separate, killable process.

Why a whole process and not a thread: CPython's ``re`` engine holds the GIL
for the duration of a single ``match``/``search`` call, so a catastrophically
backtracking pattern (e.g. ``(a+)+$`` against a long line) cannot be
pre-empted — ``asyncio.to_thread`` does NOT free the event loop, and a Python
thread cannot be cancelled. The only dependency-free way to bound wall-clock
time is to run the scan in a child process we can ``terminate()``.

The ``spawn`` start method is used unconditionally: it is the only method on
Windows / macOS-default, and it avoids inheriting the parent's asyncio loop
and open sqlite handles that ``fork`` would duplicate. Consequently the worker
callables here are module-level (picklable by qualified name) and this module
imports nothing heavy — the child re-imports only ``marginalia.agent`` (an
empty package) plus stdlib, so a scan costs a fast interpreter start, not the
~1s import of the whole tool suite.
"""
from __future__ import annotations

import multiprocessing as mp
import re
from typing import Any, Callable

# One shared spawn context. Constructing it is cheap and starts nothing.
_CTX = mp.get_context("spawn")

# Wall-clock budget for a single scan. Generous enough for a real multi-MB log
# scanned with a sane pattern, short enough that a wedged process is reaped
# quickly while the event loop stays fully responsive throughout.
DEFAULT_TIMEOUT_SECONDS = 10.0

# Belt-and-suspenders join timeout when reaping the child.
_JOIN_TIMEOUT = 5.0


class RegexScanTimeout(Exception):
    """The scan exceeded its wall-clock budget; its process was killed."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        super().__init__(
            f"regex timed out after {seconds:g}s — simplify the pattern "
            "(avoid nested quantifiers like (a+)+ that backtrack "
            "catastrophically)"
        )


class RegexScanError(RuntimeError):
    """The scan process crashed, or the worker raised before returning."""


# --- module-level workers (must be picklable by qualified name) -------------

def match_flags(source: str, flags: int, lines: list[str]) -> set[int]:
    """Return the set of indices of ``lines`` matched by the pattern."""
    pat = re.compile(source, flags)
    return {i for i, line in enumerate(lines) if pat.search(line)}


def match_captures(
    source: str, flags: int, lines: list[str], group_name: str,
) -> list[list[str | None]]:
    """Per line, the captured group value of every ``finditer`` match.

    A ``None`` entry means the match did not expose the requested capture
    group (mirrors query_log's top_values semantics)."""
    pat = re.compile(source, flags)
    return [
        [_capture_value(m, group_name) for m in pat.finditer(line)]
        for line in lines
    ]


def _capture_value(match: "re.Match[str]", group_name: str) -> str | None:
    if group_name:
        try:
            return match.group(group_name)
        except (IndexError, KeyError):
            return None
    if match.lastgroup:
        return match.group(match.lastgroup)
    if match.lastindex:
        return match.group(1)
    return None


# --- process manager --------------------------------------------------------

def _child_entry(worker: Callable[..., Any], args: tuple[Any, ...], conn) -> None:
    try:
        conn.send(("ok", worker(*args)))
    except BaseException as exc:  # noqa: BLE001 - relayed to the parent
        conn.send(("err", f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()


def run_scan(
    worker: Callable[..., Any],
    args: tuple[Any, ...],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    """Run ``worker(*args)`` in a spawned process, killing it after ``timeout``.

    Blocking — call via ``asyncio.to_thread`` so the event loop stays live.
    Returns the worker's (picklable) result. Raises :class:`RegexScanTimeout`
    on overrun (the process is terminated) or :class:`RegexScanError` if the
    process died or the worker raised.
    """
    recv_conn, send_conn = _CTX.Pipe(duplex=False)
    proc = _CTX.Process(target=_child_entry, args=(worker, args, send_conn))
    proc.start()
    # Only the child writes; drop the parent's write end so ``recv``/``poll``
    # sees EOF promptly if the child dies without sending a result.
    send_conn.close()
    try:
        if recv_conn.poll(timeout):
            try:
                status, payload = recv_conn.recv()
            except EOFError:
                proc.join(_JOIN_TIMEOUT)
                raise RegexScanError(
                    f"regex scan process exited unexpectedly "
                    f"(code {proc.exitcode})"
                ) from None
            proc.join(_JOIN_TIMEOUT)
            if status == "err":
                raise RegexScanError(payload)
            return payload
        # No result within budget: still grinding (kill it) or already dead.
        if proc.is_alive():
            proc.terminate()
            proc.join(_JOIN_TIMEOUT)
            raise RegexScanTimeout(timeout)
        proc.join(_JOIN_TIMEOUT)
        raise RegexScanError(
            f"regex scan process exited unexpectedly (code {proc.exitcode})"
        )
    finally:
        recv_conn.close()
        if proc.is_alive():
            proc.terminate()
            proc.join(_JOIN_TIMEOUT)


# --- convenience wrappers used by the tools ---------------------------------

def run_match_flags(
    source: str,
    flags: int,
    lines: list[str],
    *,
    is_regex: bool,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> set[int]:
    """Matched-line indices. A caller-supplied regex (``is_regex``) runs in a
    killable subprocess; an escaped literal cannot backtrack, so it runs
    in-thread."""
    if is_regex:
        return run_scan(match_flags, (source, flags, lines), timeout=timeout)
    return match_flags(source, flags, lines)


def run_match_captures(
    source: str,
    flags: int,
    lines: list[str],
    group_name: str,
    *,
    is_regex: bool,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[list[str | None]]:
    """Per-line captured group values. ``finditer`` can backtrack, so a
    caller-supplied regex runs in a killable subprocess."""
    if is_regex:
        return run_scan(
            match_captures, (source, flags, lines, group_name), timeout=timeout
        )
    return match_captures(source, flags, lines, group_name)
