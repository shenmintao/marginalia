from __future__ import annotations

from marginalia.eval.core import (
    _eval_entry_sort_key,
    _merge_eval_entries,
    _report_compare_result,
    _select_quota_evidence_ids,
    format_report_compare_result,
    report_compare_to_dict,
)


def test_eval_hybrid_merge_uses_rrf_overlap() -> None:
    entry_map: dict[str, dict] = {}

    _merge_eval_entries(
        entry_map,
        [
            {"entry_id": "lex1", "display_name": "lex1"},
            {"entry_id": "both", "display_name": "both"},
        ],
        "metadata_text",
    )
    _merge_eval_entries(
        entry_map,
        [
            {"entry_id": "sem1", "display_name": "sem1"},
            {"entry_id": "both", "display_name": "both"},
        ],
        "semantic",
    )

    ranked = sorted(entry_map.values(), key=_eval_entry_sort_key)

    assert ranked[0]["entry_id"] == "both"
    assert ranked[0]["lexical_rank"] == 2
    assert ranked[0]["semantic_rank"] == 2


def test_eval_evidence_selection_uses_quotas_then_fills() -> None:
    rows = [
        {"entry_id": f"both{i}", "matched_by": ["metadata_text", "semantic"]}
        for i in range(1, 6)
    ]
    rows.extend(
        {"entry_id": f"lex{i}", "matched_by": ["metadata_text"]}
        for i in range(1, 6)
    )
    rows.extend(
        {"entry_id": f"sem{i}", "matched_by": ["semantic"]}
        for i in range(1, 6)
    )

    selected = _select_quota_evidence_ids(rows, 10)

    assert selected[:4] == ["both1", "both2", "both3", "both4"]
    assert selected[4:8] == ["lex1", "lex2", "lex3", "lex4"]
    assert selected[8:] == ["sem1", "sem2"]


def test_report_compare_result_aggregates_pairwise_metrics() -> None:
    result = _report_compare_result(
        name="tiny",
        queries_total=2,
        queries_skipped=0,
        timeout_seconds=300.0,
        concurrency=2,
        total_elapsed_ms=1234,
        per_query=[
            {
                "timed_out": False,
                "rag": {
                    "answer_cites_relevant": True,
                    "label_correct": True,
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_read_tokens": 3,
                        "cache_creation_tokens": 0,
                    },
                },
                "react": {
                    "answer_cites_relevant": False,
                    "label_correct": True,
                    "tool_calls": 3,
                    "llm_calls": 4,
                    "usage": {
                        "input_tokens": 20,
                        "output_tokens": 8,
                        "cache_read_tokens": 7,
                        "cache_creation_tokens": 0,
                    },
                },
                "judge": {
                    "winner": "rag",
                    "usage": {
                        "input_tokens": 6,
                        "output_tokens": 2,
                        "cache_read_tokens": 0,
                        "cache_creation_tokens": 0,
                    },
                },
            },
            {
                "timed_out": True,
                "rag": {
                    "answer_cites_relevant": False,
                    "label_correct": False,
                    "usage": {},
                },
                "react": {
                    "answer_cites_relevant": True,
                    "label_correct": True,
                    "tool_calls": 1,
                    "llm_calls": 2,
                    "usage": {},
                },
                "judge": {
                    "winner": "react",
                    "error": None,
                    "usage": {},
                },
            },
        ],
    )

    as_dict = report_compare_to_dict(result)
    text = format_report_compare_result(result)

    assert result.rag_wins == 1
    assert result.react_wins == 1
    assert result.ties == 0
    assert result.timed_out == 1
    assert result.rag_citation_hit_rate == 0.5
    assert result.react_citation_hit_rate == 0.5
    assert result.rag_label_accuracy == 0.5
    assert result.react_label_accuracy == 1.0
    assert result.avg_react_tool_calls == 2.0
    assert result.avg_react_llm_calls == 3.0
    assert as_dict["usage"]["total_input_tokens"] == 36
    assert "judge_wins: rag=1 react=1 ties=0" in text
