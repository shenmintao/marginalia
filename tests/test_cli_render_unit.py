from __future__ import annotations

from marginalia.cli.render import render_markdown


def test_render_markdown_unwraps_markdown_fenced_table() -> None:
    md = "```markdown\n| A | B |\n|---|---|\n| 1 | 2 |\n```\n"

    out = render_markdown(md)

    assert "```" not in out
    assert "|---|---|" not in out
    assert "\u2502" in out
    assert "A" in out and "B" in out and "1" in out and "2" in out


def test_render_markdown_unwraps_markdown_fenced_table_without_outer_pipes() -> None:
    md = "```md\nA | B\n--- | ---\nleft | right\n```\n"

    out = render_markdown(md)

    assert "```" not in out
    assert "--- | ---" not in out
    assert "\u2502" in out
    assert "left" in out and "right" in out


def test_render_markdown_keeps_non_table_markdown_fence_as_code() -> None:
    md = "```markdown\n**not a table**\n```\n"

    out = render_markdown(md)

    assert "**not a table**" in out


def test_render_markdown_shortens_resolved_entry_footnote_links() -> None:
    md = (
        "Claim[^a].\n\n"
        "[^a]: [report.md]"
        "(entry:019e5493-fca4-7524-b8d0-3c36885b1241?page=3)"
        " — evidence\n"
    )

    out = render_markdown(md)

    assert "Claim[1]." in out
    assert "report.md (entry 019e5493; page 3)" in out
    assert "entry:019e5493-fca4-7524-b8d0-3c36885b1241" not in out


def test_render_markdown_shortens_raw_entry_id_footnotes() -> None:
    md = (
        "Claim[^a].\n\n"
        '[^a]: entry_id=019e5493-fca4-7524-b8d0-3c36885b1241, '
        'quote="important clause" - evidence\n'
    )

    out = render_markdown(md)

    assert "Claim[1]." in out
    assert 'entry 019e5493; q="important clause"' in out
    assert "entry_id=" not in out
