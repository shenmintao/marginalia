"""parse_tagged tolerates a single trailing unclosed tag.

Why: reasoning models occasionally hit `max_tokens` mid-`<summary>`,
leaving the closing `</summary>` off the wire. Strict matching threw
the partial body away and the PDF pipeline raised
``produced empty summary`` — re-billing the whole run for what was a
serviceable (if shorter) answer. The parser now recovers from
open-tag-to-EOF for the rightmost unclosed tag.

Run:
    .venv/Scripts/python tests/test_tagged_response_unit.py
"""
from __future__ import annotations

from marginalia.llm.tagged_response import parse_tagged


def test_well_formed_tags() -> None:
    text = (
        "<summary>two-sentence prose.</summary>\n"
        "<description>fuller prose.</description>\n"
        "<tags>\ntopic: llm\n</tags>\n"
    )
    out = parse_tagged(text)
    assert out["summary"] == "two-sentence prose.", out
    assert out["description"] == "fuller prose.", out
    assert "topic: llm" in out["tags"], out
    print("[1] well-formed tags parsed")


def test_repeated_tag_last_wins() -> None:
    text = "<summary>draft</summary>\n<summary>final</summary>"
    assert parse_tagged(text)["summary"] == "final"
    print("[2] repeated tag — last occurrence wins")


def test_truncated_summary_recovered() -> None:
    text = (
        "<summary>本文件为劳动人事争议仲裁申请书，申请人要求被申请人支付绩效"
    )
    out = parse_tagged(text)
    assert "summary" in out, f"truncated summary lost entirely: {out!r}"
    assert "劳动人事争议" in out["summary"], out
    print("[3] truncated <summary> body recovered to EOF")


def test_truncated_after_complete_blocks() -> None:
    text = (
        "<summary>finished sentence.</summary>\n"
        "<description>also done.</description>\n"
        "<tags>\ntopic: foo\nform: pa"
    )
    out = parse_tagged(text)
    assert out["summary"] == "finished sentence."
    assert out["description"] == "also done."
    assert "topic: foo" in out["tags"], out
    print("[4] earlier complete blocks preserved when later block is truncated")


def test_no_tags_returns_empty() -> None:
    assert parse_tagged("") == {}
    assert parse_tagged("just prose, nothing tagged") == {}
    print("[5] tagless input → ")


def main() -> None:
    test_well_formed_tags()
    test_repeated_tag_last_wins()
    test_truncated_summary_recovered()
    test_truncated_after_complete_blocks()
    test_no_tags_returns_empty()
    print("\nALL TAGGED-RESPONSE PARSER CHECKS PASSED")


if __name__ == "__main__":
    main()
