"""Pure-function tests for the citation locator pipeline.

The end-to-end agent test (test_agent_e2e.py) needs a real LLM stub and
takes ~30s. These tests pin the two pieces that decide whether deep-link
citations work at all — both pure functions:

  1. _LIVE_FOOTNOTE_RE: parses agent-emitted footnote defs
       [^a]: entry_id=<uuid>, lines=10-40 - reason
     into (marker, eid, lines_loc, page_loc, reason). Regression here
     produces empty links so chat citations stop scrolling.

  2. _capture_locators(): sniffs read_files tool calls and remembers the
     latest segment locator per entry. Used as the C-style fallback when
     the agent forgets to write `lines=` / `page=`.

  3. _rewrite_footnotes_for_display(): the wiring of the two — explicit
     locator wins, cache fills in, both missing leaves the link plain.
     We patch the entry-name DB lookup to keep the test pure-Python.
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _import_runtime():
    """Test runs from a checkout, not an installed package."""
    src = Path(__file__).resolve().parent.parent / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from marginalia.agent import runtime  # noqa: WPS433
    return runtime


def _check_regex():
    rt = _import_runtime()
    cases = [
        # (line, expected_groups[1..])
        (
            "[^a]: entry_id=12345678-1234-1234-1234-123456789012, lines=10-40 - reason",
            ("a", "12345678-1234-1234-1234-123456789012", "10-40", None, "reason"),
        ),
        (
            "[^b]: entry_id=12345678-1234-1234-1234-123456789012, page=3 - reason",
            ("b", "12345678-1234-1234-1234-123456789012", None, "3", "reason"),
        ),
        (
            "[^c]: entry_id=12345678-1234-1234-1234-123456789012, lines=42 - single",
            ("c", "12345678-1234-1234-1234-123456789012", "42", None, "single"),
        ),
        # legacy section_id still parses but contributes no locator
        (
            "[^d]: entry_id=12345678-1234-1234-1234-123456789012, section_id=s1 - reason",
            ("d", "12345678-1234-1234-1234-123456789012", None, None, "reason"),
        ),
        # bare entry_id, no locator, no reason
        (
            "[^e]: entry_id=12345678-1234-1234-1234-123456789012",
            ("e", "12345678-1234-1234-1234-123456789012", None, None, None),
        ),
        # backticks around uuid + lines (models often inline-code these)
        (
            "[^f]: entry_id=`12345678-1234-1234-1234-123456789012`, lines=`5-9` - r",
            ("f", "12345678-1234-1234-1234-123456789012", "5-9", None, "r"),
        ),
    ]
    for line, expected in cases:
        m = rt._LIVE_FOOTNOTE_RE.search(line)
        assert m, f"regex failed to match: {line!r}"
        got = m.groups()
        assert got == expected, f"\n line: {line!r}\n got:  {got}\n want: {expected}"
    print(f"[1] regex matched all {len(cases)} forms")


def _check_capture_locators():
    rt = _import_runtime()
    locators: dict[str, dict] = {}

    # text/markdown: start_line + end_line
    tc = SimpleNamespace(name="read_files", arguments={
        "requests": [{
            "entry_id": "ent-A",
            "reads": [{"start_line": 10, "end_line": 40}],
        }],
    })
    rt._capture_locators(tc, locators)
    assert locators["ent-A"] == {"kind": "line", "value": "10-40"}, locators

    # PDF: page
    tc2 = SimpleNamespace(name="read_files", arguments={
        "requests": [{"entry_id": "ent-B", "reads": [{"page": 7}]}],
    })
    rt._capture_locators(tc2, locators)
    assert locators["ent-B"] == {"kind": "page", "value": "7"}

    # multiple reads on same entry: latest wins (closest to citation in attention)
    tc3 = SimpleNamespace(name="read_files", arguments={
        "requests": [{
            "entry_id": "ent-A",
            "reads": [
                {"start_line": 1, "end_line": 5},
                {"start_line": 80, "end_line": 100},
            ],
        }],
    })
    rt._capture_locators(tc3, locators)
    assert locators["ent-A"] == {"kind": "line", "value": "80-100"}

    # single line (no end_line): emits "42" not "42-42"
    tc4 = SimpleNamespace(name="read_files", arguments={
        "requests": [{"entry_id": "ent-C", "reads": [{"start_line": 42}]}],
    })
    rt._capture_locators(tc4, locators)
    assert locators["ent-C"] == {"kind": "line", "value": "42"}

    # non-read_files tool calls leave the cache alone
    before = dict(locators)
    rt._capture_locators(
        SimpleNamespace(name="search_journal", arguments={"query": "q"}),
        locators,
    )
    assert locators == before

    # malformed args don't blow up
    rt._capture_locators(
        SimpleNamespace(name="read_files", arguments=None),
        locators,
    )
    rt._capture_locators(
        SimpleNamespace(name="read_files", arguments={"requests": "nope"}),
        locators,
    )
    print("[2] _capture_locators handles all four pipelines + malformed input")


async def _check_rewrite():
    rt = _import_runtime()
    eid = "12345678-1234-1234-1234-123456789012"

    # Stub the DB lookup so this test is pure
    fake_entry = SimpleNamespace(id=eid, display_name="my-doc.md")

    async def fake_list_live(db, ids):
        return [(fake_entry, None)] if eid in ids else []

    @asynccontextmanager
    async def fake_session_scope():
        yield None

    with patch.object(
        rt.entries_repo, "list_live_with_file_by_ids", new=fake_list_live,
    ), patch.object(rt, "session_scope", new=fake_session_scope):
        # 1. agent emitted explicit lines= -> link carries ?line=10-40
        out = await rt._rewrite_footnotes_for_display(
            f"answer body[^a]\n\n[^a]: entry_id={eid}, lines=10-40 - reason",
            locators=None,
        )
        assert f"[my-doc.md](entry:{eid}?line=10-40)" in out, out

        # 2. no explicit locator, but cache has one -> fallback fills it
        out = await rt._rewrite_footnotes_for_display(
            f"body[^a]\n\n[^a]: entry_id={eid} - reason",
            locators={eid: {"kind": "line", "value": "5-7"}},
        )
        assert f"[my-doc.md](entry:{eid}?line=5-7)" in out, out

        # 3. neither -> bare link, no querystring
        out = await rt._rewrite_footnotes_for_display(
            f"body[^a]\n\n[^a]: entry_id={eid} - reason",
            locators={},
        )
        assert f"[my-doc.md](entry:{eid})" in out, out
        assert "?line=" not in out

        # 4. explicit lines= beats cache (agent's intent wins)
        out = await rt._rewrite_footnotes_for_display(
            f"body[^a]\n\n[^a]: entry_id={eid}, lines=10-40 - r",
            locators={eid: {"kind": "page", "value": "9"}},
        )
        assert f"entry:{eid}?line=10-40" in out, out
        assert "?page=" not in out

        # 5. page= locator
        out = await rt._rewrite_footnotes_for_display(
            f"body[^a]\n\n[^a]: entry_id={eid}, page=3 - r",
            locators=None,
        )
        assert f"entry:{eid}?page=3" in out, out

    print("[3] _rewrite_footnotes_for_display: explicit > cache > bare, page+line both work")


def main():
    _check_regex()
    _check_capture_locators()
    asyncio.run(_check_rewrite())
    print("\nALL CITATION LOCATOR CHECKS PASSED")


# pytest entry points
def test_regex():
    _check_regex()


def test_capture_locators():
    _check_capture_locators()


def test_rewrite():
    asyncio.run(_check_rewrite())


if __name__ == "__main__":
    main()
