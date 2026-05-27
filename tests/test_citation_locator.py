"""Pure-function tests for the citation locator pipeline (quote-based).

Footnotes the agent emits look like:

  [^a]: entry_id=<uuid>, quote="<verbatim 10-60 char excerpt>" - reason
  [^p]: entry_id=<uuid>, page=<n> - reason   # PDF only

`_LIVE_FOOTNOTE_RE` parses these. `_rewrite_footnotes_for_display` looks
up the entry name and emits

  [^a]: [name](entry:<uuid>?q=<urlencoded>) — reason
  [^p]: [name](entry:<uuid>?page=<n>) — reason

Legacy `lines=`/`section_id=` are still tolerated by the regex so old
sessions don't crash on replay/export, but they produce no query string.
"""
from __future__ import annotations

import asyncio
import sys
import urllib.parse
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
    # Group layout: 1=marker, 2=eid, 3=quote, 4=page, 5=reason
    eid = "12345678-1234-1234-1234-123456789012"
    cases = [
        # quote, simple
        (
            f'[^a]: entry_id={eid}, quote="合同第4.6条规定" - 论证 Y',
            ("a", eid, "合同第4.6条规定", None, "论证 Y"),
        ),
        # quote with embedded escaped double-quote
        (
            f'[^b]: entry_id={eid}, quote="he said \\"yes\\"" - r',
            ("b", eid, 'he said \\"yes\\"', None, "r"),
        ),
        # page (PDF case)
        (
            f"[^c]: entry_id={eid}, page=3 - reason",
            ("c", eid, None, "3", "reason"),
        ),
        # backticks around uuid + page
        (
            f"[^d]: entry_id=`{eid}`, page=`12` - r",
            ("d", eid, None, "12", "r"),
        ),
        # short hex prefix
        (
            '[^e]: entry_id=019e63b9, quote="第三条" - r',
            ("e", "019e63b9", "第三条", None, "r"),
        ),
        # bare entry_id, no locator, no reason
        (
            f"[^f]: entry_id={eid}",
            ("f", eid, None, None, None),
        ),
        # legacy lines= still parses (group 3/4 None) — tolerated for
        # historical sessions, but no query string is emitted downstream
        (
            f"[^g]: entry_id={eid}, lines=10-40 - r",
            ("g", eid, None, None, "r"),
        ),
        # legacy section_id= same: tolerated but no query string
        (
            f"[^h]: entry_id={eid}, section_id=s1 - r",
            ("h", eid, None, None, "r"),
        ),
        # legacy descriptive lines=
        (
            f"[^i]: entry_id={eid}, lines=合同第4.6条 - r",
            ("i", eid, None, None, "r"),
        ),
    ]
    for line, expected in cases:
        m = rt._LIVE_FOOTNOTE_RE.search(line)
        assert m, f"regex failed to match: {line!r}"
        got = m.groups()
        assert got == expected, f"\n line: {line!r}\n got:  {got}\n want: {expected}"
    print(f"[1] regex matched all {len(cases)} forms")


async def _check_rewrite():
    rt = _import_runtime()
    eid = "12345678-1234-1234-1234-123456789012"

    fake_entry = SimpleNamespace(id=eid, display_name="my-doc.md")
    fake_file = SimpleNamespace(id="file-1", description={})

    async def fake_list_live(db, ids):
        return [(fake_entry, fake_file)] if eid in ids else []

    async def fake_resolve_prefix(db, raw):
        if raw == eid:
            return eid, None
        cleaned = raw.replace("-", "").lower()
        if len(cleaned) >= 8 and eid.replace("-", "").startswith(cleaned):
            return eid, None
        return raw, f"no entry matches prefix {raw!r}"

    @asynccontextmanager
    async def fake_session_scope():
        yield None

    with patch.object(
        rt.entries_repo, "list_live_with_file_by_ids", new=fake_list_live,
    ), patch.object(
        rt.entries_repo, "resolve_entry_id_prefix", new=fake_resolve_prefix,
    ), patch.object(rt, "session_scope", new=fake_session_scope):
        # 1. quote → ?q=<urlencoded>
        out = await rt._rewrite_footnotes_for_display(
            f'body[^a]\n\n[^a]: entry_id={eid}, quote="合同第4.6条规定" - reason',
        )
        expected_q = urllib.parse.quote_plus("合同第4.6条规定")
        assert f"[my-doc.md](entry:{eid}?q={expected_q})" in out, out

        # 2. page → ?page=<n>
        out = await rt._rewrite_footnotes_for_display(
            f"body[^a]\n\n[^a]: entry_id={eid}, page=3 - reason",
        )
        assert f"[my-doc.md](entry:{eid}?page=3)" in out, out

        # 3. quote with escaped embedded double-quote — \" unescapes to "
        # before urlencoding so the GUI search target matches the source.
        out = await rt._rewrite_footnotes_for_display(
            f'body[^a]\n\n[^a]: entry_id={eid}, quote="he said \\"yes\\"" - r',
        )
        expected_q = urllib.parse.quote_plus('he said "yes"')
        assert f"entry:{eid}?q={expected_q}" in out, out

        # 4. legacy lines= → no query string (link opens file without jump)
        out = await rt._rewrite_footnotes_for_display(
            f"body[^a]\n\n[^a]: entry_id={eid}, lines=10-40 - r",
        )
        assert f"[my-doc.md](entry:{eid})" in out, out
        assert "?q=" not in out and "?line=" not in out and "?page=" not in out

        # 5. legacy section_id= → no query string
        out = await rt._rewrite_footnotes_for_display(
            f"body[^a]\n\n[^a]: entry_id={eid}, section_id=s1 - r",
        )
        assert f"[my-doc.md](entry:{eid})" in out, out
        assert "?q=" not in out

        # 6. bare entry_id, no locator → bare link
        out = await rt._rewrite_footnotes_for_display(
            f"body[^a]\n\n[^a]: entry_id={eid}",
        )
        assert f"[my-doc.md](entry:{eid})" in out, out

        # 7. 8-char prefix is promoted to full uuid in the link
        out = await rt._rewrite_footnotes_for_display(
            'body[^a]\n\n[^a]: entry_id=12345678, quote="abc" - r',
        )
        assert f"[my-doc.md](entry:{eid}?q=abc)" in out, out

        # 8. unresolvable prefix → "(entry … unavailable)" branch, no link
        out = await rt._rewrite_footnotes_for_display(
            'body[^a]\n\n[^a]: entry_id=deadbeef, quote="x" - r',
        )
        assert "(entry deadbeef unavailable)" in out, out
        assert "entry:deadbeef" not in out
        assert "my-doc.md" not in out

    print("[2] _rewrite_footnotes_for_display: quote/page/legacy/prefix all wire correctly")


def main():
    _check_regex()
    asyncio.run(_check_rewrite())
    print("\nALL CITATION LOCATOR CHECKS PASSED")


# pytest entry points
def test_regex():
    _check_regex()


def test_rewrite():
    asyncio.run(_check_rewrite())


if __name__ == "__main__":
    main()
