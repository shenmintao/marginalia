from __future__ import annotations

import pytest

from marginalia.agent.tools import ToolContext
from marginalia.agent.tools.recall_knowledge import (
    apply_rerank_hits,
    score_recall_entries,
    select_evidence_entry_ids,
    select_quota_entry_ids,
)
from marginalia.semantic.rerank import RerankHit


@pytest.mark.asyncio
async def test_recall_knowledge_overfetches_before_final_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import marginalia.agent.tools.recall_knowledge as module

    seen_limits: list[int] = []

    async def fake_search_metadata(db, ctx, args):  # noqa: ANN001
        seen_limits.append(int(args["limit"]))
        return {
            "count": 2,
            "entries": [
                {"entry_id": "b", "display_name": "b.txt"},
                {"entry_id": "a", "display_name": "a.txt"},
            ],
        }

    async def fake_search_journal(db, args, *, match):  # noqa: ANN001
        return {"count": 0, "notes": []}

    async def fake_expansion(db, anchor_entry_ids, *, limit):  # noqa: ANN001
        return []

    monkeypatch.setattr(module, "search_metadata", fake_search_metadata)
    monkeypatch.setattr(module, "run_search_journal", fake_search_journal)
    monkeypatch.setattr(module, "_one_hop_expansion_ids", fake_expansion)

    result = await module.recall_knowledge(
        None,
        ToolContext(session_id="s", conversation_id="c"),
        {"text": "query", "limit": 1},
    )

    assert seen_limits == [module.MAX_LIMIT]
    assert result["fetch_limit"] == module.MAX_LIMIT
    assert result["limit"] == 1
    assert result["candidate_entry_ids"] == ["b"]


@pytest.mark.asyncio
async def test_recall_knowledge_skips_semantic_without_embedding_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    import marginalia.agent.tools.recall_knowledge as module
    from marginalia.config import get_settings

    monkeypatch.setenv("MARGINALIA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SEMANTIC_RECALL_ENABLED", "true")
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    get_settings.cache_clear()  # type: ignore[attr-defined]

    async def fake_search_metadata(db, ctx, args):  # noqa: ANN001
        return {"count": 0, "entries": []}

    async def fake_search_journal(db, args, *, match):  # noqa: ANN001
        return {"count": 0, "notes": []}

    async def fake_expansion(db, anchor_entry_ids, *, limit):  # noqa: ANN001
        return []

    async def fail_semantic(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("semantic recall should not run without EMBEDDING_API_KEY")

    monkeypatch.setattr(module, "search_metadata", fake_search_metadata)
    monkeypatch.setattr(module, "run_search_journal", fake_search_journal)
    monkeypatch.setattr(module, "_one_hop_expansion_ids", fake_expansion)
    monkeypatch.setattr(module, "semantic_entry_rows", fail_semantic)

    try:
        result = await module.recall_knowledge(
            None,
            ToolContext(session_id="s", conversation_id="c"),
            {"text": "query"},
        )
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]

    assert "semantic" not in result["trace"]
    assert "semantic_error" not in result["trace"]


def test_recall_score_prefers_overlap_with_field_match() -> None:
    ranked = score_recall_entries(
        [
            {
                "entry_id": "lex",
                "display_name": "general.txt",
                "summary": "A generic note about consensus.",
                "matched_by": ["metadata_text"],
                "lexical_rank": 1,
                "rrf_score": 1 / 61,
                "rank_score": 2,
            },
            {
                "entry_id": "both",
                "display_name": "raft-consensus.txt",
                "summary": "Raft consensus uses leader election.",
                "matched_by": ["metadata_text", "semantic"],
                "lexical_rank": 3,
                "semantic_rank": 2,
                "rrf_score": (1 / 63) + (1 / 62),
                "rank_score": 1,
            },
        ],
        text_terms=["Raft consensus leader election"],
    )

    assert ranked[0]["entry_id"] == "both"
    assert ranked[0]["score_components"]["field_match"] > 0
    assert ranked[0]["score_components"]["source_overlap"] > 0


def test_recall_evidence_selection_uses_source_quotas_then_fills() -> None:
    rows = [
        {"entry_id": f"both{i}", "matched_by": ["metadata_text", "semantic"]}
        for i in range(1, 6)
    ]
    rows.extend(
        {"entry_id": f"tag{i}", "matched_by": ["metadata_tags"]}
        for i in range(1, 6)
    )
    rows.extend(
        {"entry_id": f"lex{i}", "matched_by": ["metadata_text"]}
        for i in range(1, 6)
    )
    rows.extend(
        {"entry_id": f"sem{i}", "matched_by": ["semantic"]}
        for i in range(1, 6)
    )

    selected = select_quota_entry_ids(rows, 10)

    assert selected[:4] == ["both1", "both2", "both3", "both4"]
    assert selected[4:6] == ["tag1", "tag2"]
    assert selected[6:8] == ["lex1", "lex2"]
    assert selected[8:] == ["sem1", "sem2"]


def test_recall_evidence_selection_can_use_ranked_order_without_quota() -> None:
    rows = [
        {"entry_id": "sem1", "matched_by": ["semantic"]},
        {"entry_id": "sem2", "matched_by": ["semantic"]},
        {"entry_id": "lex1", "matched_by": ["metadata_text"]},
    ]

    assert select_evidence_entry_ids(rows, 2, strategy="rerank") == ["sem1", "sem2"]


def test_apply_rerank_hits_reorders_only_reranked_window() -> None:
    rows = [
        {
            "entry_id": f"e{i}",
            "score_components": {},
        }
        for i in range(5)
    ]

    reranked = apply_rerank_hits(
        rows,
        [
            RerankHit(index=2, score=0.9, rank=1),
            RerankHit(index=0, score=0.5, rank=2),
            RerankHit(index=1, score=0.1, rank=3),
        ],
        top_n=3,
    )

    assert [row["entry_id"] for row in reranked] == ["e2", "e0", "e1", "e3", "e4"]
    assert reranked[0]["rerank_rank"] == 1
    assert reranked[0]["score_components"]["rerank"] == 0.9
