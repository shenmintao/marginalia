from __future__ import annotations

from types import SimpleNamespace

from marginalia.pipelines.text import TextPipeline


def test_heading_read_matches_chapter_prefix_from_sections() -> None:
    body = "\n".join([
        "# 第十章 商业",
        "商业正文",
        "# 第十一章 市场中的信贷",
        "信贷正文",
        "# 第十二章 走向现代银行业",
        "银行正文",
    ])
    file_row = SimpleNamespace(
        description={
            "sections": [
                {
                    "id": "s11",
                    "title": "第十一章 市场中的信贷",
                    "anchor": {"unit": "lines", "value": "3-4"},
                },
            ],
        },
    )

    result = TextPipeline()._slice(
        body=body,
        args={"heading": "第十一章", "max_chars": 1000},
        file_row=file_row,
    )

    assert result.error is None
    assert "信贷正文" in result.text
    assert result.extras["section_id"] == "s11"


def test_heading_read_falls_back_to_markdown_heading_scan() -> None:
    body = "\n".join([
        "# 第十章 商业",
        "商业正文",
        "# 第十一章 市场中的信贷",
        "信贷正文",
        "更多信贷正文",
        "# 第十二章 走向现代银行业",
        "银行正文",
    ])
    file_row = SimpleNamespace(
        description={
            "sections": [
                {
                    "id": "s10",
                    "title": "第十章 商业",
                    "anchor": {"unit": "lines", "value": "1-2"},
                },
            ],
        },
    )

    result = TextPipeline()._slice(
        body=body,
        args={"heading": "第十一章", "max_chars": 1000},
        file_row=file_row,
    )

    assert result.error is None
    assert "# 第十一章 市场中的信贷" in result.text
    assert "更多信贷正文" in result.text
    assert "# 第十二章" not in result.text
    assert result.extras["located_via"] == "body-heading-scan"
