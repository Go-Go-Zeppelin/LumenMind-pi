"""Isolated intent/retrieval experiments; never overwrite the archived benchmark.

Serve exposes only read-only evaluation endpoints. It seeds a fresh Chroma
directory, without starting memory workers or touching the business database.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx

from evaluation import benchmark_suite as suite


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def serve(args: argparse.Namespace) -> None:
    from fastapi import FastAPI
    from pydantic import BaseModel, Field
    import uvicorn
    from mcp.knowledge_base import KnowledgeBase
    from mcp.intent_index import IntentEmbeddingIndex

    storage = Path(args.chroma_path).resolve()
    if storage == (suite.ROOT / "data/chroma").resolve():
        raise ValueError("Use an isolated Chroma directory")
    kb = KnowledgeBase(chroma_host="127.0.0.1", chroma_port=1, chroma_path=str(storage),
                       retrieval_mode=args.retrieval_mode)
    index = IntentEmbeddingIndex(chroma_host="127.0.0.1", chroma_port=1, chroma_path=str(storage))
    app = FastAPI()

    class SearchInput(BaseModel):
        query: str = Field(min_length=1)
        top_k: int = Field(default=5, ge=1, le=20)

    # Avoid postponed local annotations: FastAPI must see the actual model.
    async def knowledge(body):
        return {"results": await kb.search_async(body.query, body.top_k)}

    async def intent(body):
        return {"candidates": await index.search_async(body.query, body.top_k)}

    knowledge.__annotations__["body"] = SearchInput
    intent.__annotations__["body"] = SearchInput
    app.post("/knowledge/search")(knowledge)
    app.post("/intent/embedding")(intent)

    @app.get("/health")
    def health():
        page = kb._collection.get(include=["documents", "metadatas"])
        corpus = sorted(zip(page["ids"], page["documents"], page["metadatas"]))
        return {"status": "ok", "fixture": "isolated-intent-retrieval", "chunks": kb.doc_count,
                "retrieval_mode": kb.retrieval_mode,
                "corpus_sha256": hashlib.sha256(json.dumps(corpus, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}

    uvicorn.run(app, host="127.0.0.1", port=args.port)


async def measure(args: argparse.Namespace) -> None:
    if args.intent_dataset:
        suite.INTENT_PATH = Path(args.intent_dataset).resolve()
    if args.retrieval_dataset:
        suite.RAG_RETRIEVAL_PATH = Path(args.retrieval_dataset).resolve()
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{args.pi_url}/health")
        response.raise_for_status()
        health = response.json()
        response = await client.get(f"{args.python_url}/health")
        response.raise_for_status()
        infrastructure = response.json()
    sources = ["agent-service/src/agents/intent-router.ts", "python-service/mcp/knowledge_base.py",
               "python-service/mcp/hybrid_retrieval.py", "python-service/mcp/intent_index.py",
               "python-service/evaluation/benchmark_suite.py", "python-service/evaluation/optimization_experiment.py"]
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(), "label": args.label,
        "methodology": {
            "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=suite.ROOT, text=True).strip(),
            "concurrency": args.concurrency,
            "infrastructure": infrastructure,
            "embedding_model": "Chroma default all-MiniLM-L6-v2 (unchanged)",
            "intent_chain": "rules only" if args.rules_only else "rule -> embedding candidates -> LLM",
            "model": health.get("model"), "api_key_configured": health.get("api_key_configured"),
            "datasets": {p.name: digest(p) for p in (suite.INTENT_PATH, suite.RAG_RETRIEVAL_PATH)},
            "sources": {p: digest(suite.ROOT / p) for p in sources if (suite.ROOT / p).exists()},
            "warning": "Existing synthetic cases are development regression data, not an independent human gold set.",
        },
    }
    if not args.retrieval_only:
        report["intent"] = await suite.run_intent_benchmark(
            args.pi_url, args.concurrency, use_llm=not args.rules_only,
            predictions_path=output / "intent_predictions.jsonl")
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if not args.intent_only:
        report["rag_retrieval"] = await suite.run_rag_retrieval(args.python_url, args.concurrency)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"report": str(output / "report.json"),
                      "intent": {k: report.get("intent", {}).get(k) for k in ("accuracy", "macro_f1", "errors")},
                      "retrieval": {k: {m: v for m, v in metrics.items() if m not in ("details", "failures")}
                                    for k, metrics in report.get("rag_retrieval", {}).get("k", {}).items()}},
                     ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fixture = sub.add_parser("serve")
    fixture.add_argument("--chroma-path", required=True)
    fixture.add_argument("--port", type=int, default=8100)
    fixture.add_argument("--retrieval-mode", choices=("dense", "sparse", "hybrid"), default="hybrid")
    run = sub.add_parser("measure")
    run.add_argument("--output-dir", required=True)
    run.add_argument("--label", required=True)
    run.add_argument("--intent-dataset")
    run.add_argument("--retrieval-dataset")
    run.add_argument("--pi-url", default="http://127.0.0.1:3100")
    run.add_argument("--python-url", default="http://127.0.0.1:8100")
    run.add_argument("--concurrency", type=int, default=6)
    run.add_argument("--rules-only", action="store_true")
    scope = run.add_mutually_exclusive_group()
    scope.add_argument("--intent-only", action="store_true")
    scope.add_argument("--retrieval-only", action="store_true")
    args = parser.parse_args()
    if args.command == "serve":
        serve(args)
    else:
        if args.concurrency < 1:
            parser.error("concurrency must be positive")
        asyncio.run(measure(args))


if __name__ == "__main__":
    main()
