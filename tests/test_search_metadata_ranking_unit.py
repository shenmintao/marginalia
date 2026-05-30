from __future__ import annotations

from types import SimpleNamespace

from marginalia.agent.tools.search_metadata import (
    _metadata_rank_score,
    _rank_terms,
)


def test_metadata_rank_score_prefers_specific_text_hits() -> None:
    terms = _rank_terms(["1/2000 in UK have abnormal PrP positivity."])
    relevant_entry = SimpleNamespace(
        id="relevant",
        display_name="13734012.txt",
        extra="",
    )
    relevant_file = SimpleNamespace(
        summary="UK appendix survey found abnormal prion protein positivity.",
        extra="retrieval_terms: abnormal PrP positivity 493 per million 1/2000 UK",
        description={"sections": [{"title": "Results", "key_terms": ["PrP"]}]},
        original_ext=".txt",
    )
    stale_noise_entry = SimpleNamespace(
        id="noise",
        display_name="older-but-recent.txt",
        extra="",
    )
    stale_noise_file = SimpleNamespace(
        summary="A general biomedical paper.",
        extra="retrieval_terms: study evidence cohort",
        description={},
        original_ext=".txt",
    )

    relevant = _metadata_rank_score(
        entry=relevant_entry,
        file_row=relevant_file,
        query_terms=terms,
        requested_tags=set(),
        entry_tags=[],
    )
    noise = _metadata_rank_score(
        entry=stale_noise_entry,
        file_row=stale_noise_file,
        query_terms=terms,
        requested_tags=set(),
        entry_tags=[],
    )

    assert relevant > noise
