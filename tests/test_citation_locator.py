"""Pure-function tests for the citation locator pipeline (quote-based).

Footnotes the agent emits look like:

  [^a]: entry_id=<uuid>, quote="<verbatim 10-60 char excerpt>", page=<n> - reason

`_LIVE_FOOTNOTE_RE` parses these. The LLM is instructed to write both
`quote=` and `page=` whenever it can — `_rewrite_footnotes_for_display`
then resolves entry_id to the live `File` row and lets the file's actual
type pick the locator:

  - PDF (`mime_type == "application/pdf"` or `original_ext == "pdf"`):
    `?page=N`, preferably by locating the quote in extracted text and
    using that physical page; fallback to page if quote lookup misses.
  - text-shaped (`kind in {text, code, log, docx}` or text/code/docx ext):
    `?q=<urlencoded quote>` if quote present, bare otherwise.
  - everything else (image, table, audio, ...): bare link.

Resolved form:

  [^a]: [name](entry:<uuid>?q=<urlencoded>) — reason
  [^a]: [name](entry:<uuid>?page=<n>) — reason
  [^a]: [name](entry:<uuid>) — reason

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
        # `+` cancatenated quotes: extra `+ "..."` segments tolerated
        # but ignored — only the first quote is captured. The agent is
        # instructed not to write this, but if it slips through we still
        # render a working link to the first quote rather than leaking
        # the raw footnote definition to the user.
        (
            f'[^j]: entry_id={eid}, quote="第一段证据" + "第二段证据" - 拼接示例',
            ("j", eid, "第一段证据", None, "拼接示例"),
        ),
        # full-width parenthetical annotation after a page= value:
        # `page=54（第54页）`. Tolerated, the annotation is consumed,
        # group 4 still captures the digits.
        (
            f"[^k]: entry_id={eid}, page=54（第54页） - 注释示例",
            ("k", eid, None, "54", "注释示例"),
        ),
        # ASCII parenthetical after page=:
        (
            f"[^l]: entry_id={eid}, page=3 (table 2) - r",
            ("l", eid, None, "3", "r"),
        ),
        # page=N/A is tolerated and treated as no page. The prompt forbids
        # this, but older/live turns may contain it.
        (
            f'[^n]: entry_id={eid}, quote="abc", page=N/A - r',
            ("n", eid, "abc", None, "r"),
        ),
        # full-width comma as field separator (LLM occasionally writes 中文 comma).
        (
            f"[^m]: entry_id={eid}，page=7 - r",
            ("m", eid, None, "7", "r"),
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
    # The locator the backend emits depends on the entry's file type:
    # text/code/log/docx → ?q=, PDF → ?page=, everything else → bare. Tests
    # mutate `fake_file_attrs` before each call to set the type for that
    # case. Default = markdown-shaped text file.
    fake_file_attrs = {
        "id": "file-1",
        "mime_type": "text/markdown",
        "original_ext": "md",
        "kind": "text",
        "description": {},
    }

    def set_file(*, mime_type=None, original_ext=None, kind=None):
        fake_file_attrs["mime_type"] = mime_type
        fake_file_attrs["original_ext"] = original_ext
        fake_file_attrs["kind"] = kind

    async def fake_list_live(db, ids):
        return [(fake_entry, SimpleNamespace(**fake_file_attrs))] if eid in ids else []

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
        # 1. text file + quote → ?q=<urlencoded>
        set_file(mime_type="text/markdown", original_ext="md", kind="text")
        out = await rt._rewrite_footnotes_for_display(
            f'body[^a]\n\n[^a]: entry_id={eid}, quote="合同第4.6条规定" - reason',
        )
        expected_q = urllib.parse.quote_plus("合同第4.6条规定")
        assert f"[my-doc.md](entry:{eid}?q={expected_q})" in out, out

        # 2. PDF + page → ?page=<n>
        set_file(mime_type="application/pdf", original_ext="pdf", kind="text")
        out = await rt._rewrite_footnotes_for_display(
            f"body[^a]\n\n[^a]: entry_id={eid}, page=3 - reason",
        )
        assert f"[my-doc.md](entry:{eid}?page=3)" in out, out

        # 3. text + quote with escaped embedded double-quote — \" unescapes
        # to " before urlencoding so the GUI search target matches source.
        set_file(mime_type="text/markdown", original_ext="md", kind="text")
        out = await rt._rewrite_footnotes_for_display(
            f'body[^a]\n\n[^a]: entry_id={eid}, quote="he said \\"yes\\"" - r',
        )
        expected_q = urllib.parse.quote_plus('he said "yes"')
        assert f"entry:{eid}?q={expected_q}" in out, out

        # 4. text + quote + page=N/A: tolerate placeholder page and still use
        # the quote locator.
        out = await rt._rewrite_footnotes_for_display(
            f'body[^a]\n\n[^a]: entry_id={eid}, quote="abc", page=N/A - r',
        )
        assert f"[my-doc.md](entry:{eid}?q=abc)" in out, out
        assert "entry_id=" not in out, out
        assert "page=N/A" not in out, out

        # 4. legacy lines= → no query string (link opens file without jump)
        set_file(mime_type="text/markdown", original_ext="md", kind="text")
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

        # 9. PDF + both quote and page → backend picks page (PDF iframe
        # only honours #page=N; the quote is dropped because the viewer
        # can't text-search). The LLM is now told to write both fields
        # whenever it can — the type dispatcher is what makes that safe.
        set_file(mime_type="application/pdf", original_ext="pdf", kind="text")
        out = await rt._rewrite_footnotes_for_display(
            f'body[^a]\n\n[^a]: entry_id={eid}, page=4, quote="abc" - r',
        )
        assert f"[my-doc.md](entry:{eid}?page=4)" in out, out
        assert "?q=" not in out

        # 10. same PDF, fields in opposite order.
        out = await rt._rewrite_footnotes_for_display(
            f'body[^a]\n\n[^a]: entry_id={eid}, quote="abc", page=4 - r',
        )
        assert f"[my-doc.md](entry:{eid}?page=4)" in out, out
        assert "?q=" not in out

        # 11. PDF + both fields + quote locator hit: backend uses the
        # physical page found from extracted text, not the LLM's page
        # number. This avoids cover/toc printed-page offsets.
        async def fake_locate_pdf_quote_page(file, quote, *, pages_cache=None):
            assert quote == "printed page one"
            return 6

        with patch.object(
            rt, "_locate_pdf_quote_page", new=fake_locate_pdf_quote_page,
        ):
            set_file(mime_type="application/pdf", original_ext="pdf", kind="text")
            out = await rt._rewrite_footnotes_for_display(
                f'body[^a]\n\n[^a]: entry_id={eid}, quote="printed page one", page=1 - r',
            )
            assert f"[my-doc.md](entry:{eid}?page=6)" in out, out
            assert "?page=1" not in out

        # 12. text file + `+`-concatenated quotes: URL gets the first
        # quote, the second segment is silently dropped.
        set_file(mime_type="text/markdown", original_ext="md", kind="text")
        out = await rt._rewrite_footnotes_for_display(
            f'body[^a]\n\n[^a]: entry_id={eid}, quote="A段" + "B段" - r',
        )
        expected_q = urllib.parse.quote_plus("A段")
        assert f"[my-doc.md](entry:{eid}?q={expected_q})" in out, out
        assert "B段" not in out

        # 13. PDF + page with full-width parenthetical annotation: the
        # annotation is dropped; the link uses the digits.
        set_file(mime_type="application/pdf", original_ext="pdf", kind="text")
        out = await rt._rewrite_footnotes_for_display(
            f"body[^a]\n\n[^a]: entry_id={eid}, page=54（第54页） - r",
        )
        assert f"[my-doc.md](entry:{eid}?page=54)" in out, out

        # 14. PDF + ONLY quote (no page): backend cannot honour the
        # quote on a PDF iframe, so falls back to a bare link rather
        # than emitting a useless ?q= URL.
        set_file(mime_type="application/pdf", original_ext="pdf", kind="text")
        out = await rt._rewrite_footnotes_for_display(
            f'body[^a]\n\n[^a]: entry_id={eid}, quote="abc" - r',
        )
        assert f"[my-doc.md](entry:{eid})" in out, out
        assert "?q=" not in out and "?page=" not in out

        # 15. text file + page (LLM helpfully wrote a page number for a
        # markdown file): page is meaningless on text, backend falls
        # back to quote if present, bare otherwise. Here quote present.
        set_file(mime_type="text/markdown", original_ext="md", kind="text")
        out = await rt._rewrite_footnotes_for_display(
            f'body[^a]\n\n[^a]: entry_id={eid}, quote="abc", page=4 - r',
        )
        assert f"[my-doc.md](entry:{eid}?q=abc)" in out, out
        assert "?page=" not in out

        # 16. image / scan kind: even if LLM wrote a quote, the GUI has
        # no DOM to search, so the link is bare.
        set_file(mime_type="image/jpeg", original_ext="jpg", kind="image")
        out = await rt._rewrite_footnotes_for_display(
            f'body[^a]\n\n[^a]: entry_id={eid}, quote="abc" - r',
        )
        assert f"[my-doc.md](entry:{eid})" in out, out
        assert "?q=" not in out and "?page=" not in out

        # 17. table kind (xlsx): no in-page search, bare link.
        set_file(mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", original_ext="xlsx", kind="table")
        out = await rt._rewrite_footnotes_for_display(
            f'body[^a]\n\n[^a]: entry_id={eid}, quote="abc" - r',
        )
        assert f"[my-doc.md](entry:{eid})" in out, out
        assert "?q=" not in out

        # 18. code kind: in-page text search works, ?q= emitted.
        set_file(mime_type="text/x-python", original_ext="py", kind="code")
        out = await rt._rewrite_footnotes_for_display(
            f'body[^a]\n\n[^a]: entry_id={eid}, quote="def foo" - r',
        )
        expected_q = urllib.parse.quote_plus("def foo")
        assert f"[my-doc.md](entry:{eid}?q={expected_q})" in out, out

        # 19. docx kind: FileViewer renders DOCX into searchable HTML.
        set_file(
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            original_ext="docx",
            kind="docx",
        )
        out = await rt._rewrite_footnotes_for_display(
            f'body[^a]\n\n[^a]: entry_id={eid}, quote="contract clause" - r',
        )
        expected_q = urllib.parse.quote_plus("contract clause")
        assert f"[my-doc.md](entry:{eid}?q={expected_q})" in out, out

    print("[2] _rewrite_footnotes_for_display: type-aware dispatcher routes quote/page/bare correctly")


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
