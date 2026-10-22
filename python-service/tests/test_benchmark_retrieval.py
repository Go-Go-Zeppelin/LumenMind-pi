"""Partial title coverage must remain visible even when Hit@K passes."""
import asyncio

from evaluation import benchmark_suite


def test_partial_recall_is_preserved_and_reported_as_incomplete(monkeypatch):
    cases = [{"id": "single", "question": "one", "relevant_titles": ["A"]},
             {"id": "multi", "question": "two", "relevant_titles": ["A", "B"]}]
    monkeypatch.setattr(benchmark_suite, "_jsonl_read", lambda _: cases)

    async def search(*_args, **_kwargs):
        return {"results": [{"title": "A"}, {"title": "A"}]}

    monkeypatch.setattr(benchmark_suite, "_post_with_retries", search)
    report = asyncio.run(benchmark_suite.run_rag_retrieval("http://unused", 2))
    for metrics in report["k"].values():
        assert metrics["hit_at_k"] == 1.0
        assert metrics["recall_at_k"] == 0.75
        assert metrics["all_relevant_at_k"] == 0.5
        assert [row["id"] for row in metrics["failures"]] == ["multi"]
        assert len(metrics["details"]) == 2
