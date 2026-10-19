"""LumenMind Python 基础设施服务。

Agent 运行时已迁移到 TypeScript/Pi；本服务继续承载记忆、知识库管理、
评测、Prometheus 和旧 FastAPI 兼容入口。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import sqlite3
import sys
import uuid
from contextlib import asynccontextmanager, suppress
from typing import Any, Dict, List, Optional

_ROOT = str(pathlib.Path(__file__).parent.parent.resolve())
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from pydantic import BaseModel, Field
from evaluation.contracts import EvalRunInput
from business.api import router as business_router
from business.store import database_path, initialize as initialize_business
from memory.config import MemorySettings
from memory.context_builder import ContextBudgetError
from memory.repository import CapacityError, ConflictError, LeaseError, RepositoryError
from memory.worker import MemoryWorker

load_dotenv(pathlib.Path(_ROOT).parent / ".env")
load_dotenv()
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_memory = None
_knowledge = None
_intent_index = None
_tool_manager = None
_evaluator = None

MEMORY_TURNS = Counter("lumenmind_memory_turns_total", "写入 Python 记忆层的对话轮数")
KNOWLEDGE_SEARCHES = Counter("lumenmind_knowledge_searches_total", "知识库检索次数")
EVALUATIONS = Counter("lumenmind_evaluations_total", "Pi Agent 评测运行次数")


def _llm_config() -> Dict[str, Any]:
    return {
        "api_key": os.getenv("ANTHROPIC_API_KEY", ""),
        "base_url": os.getenv("ANTHROPIC_BASE_URL", "").strip() or None,
        "model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6").strip(),
    }


def _agent_url() -> str:
    return os.getenv("PI_AGENT_URL", "http://localhost:3000").rstrip("/")


def _project_path(name: str, default: str) -> str:
    value = pathlib.Path(os.getenv(name, default)).expanduser()
    if not value.is_absolute():
        value = pathlib.Path(_ROOT).parent / value
    return str(value.resolve())


def _memory_config() -> MemorySettings:
    return MemorySettings.from_env(pathlib.Path(_ROOT).parent)


def _initialize_optional_services(cfg: Dict[str, Any], chroma_host: str, chroma_port: int, chroma_path: str) -> None:
    """Knowledge and embeddings are independent of SQLite memory availability."""
    global _knowledge, _intent_index, _tool_manager, _evaluator
    try:
        from mcp.knowledge_base import KnowledgeBase
        _knowledge = KnowledgeBase(chroma_host=chroma_host, chroma_port=chroma_port, chroma_path=chroma_path)
    except Exception:
        _knowledge = None
        logger.exception("Knowledge service unavailable")
    try:
        from mcp.intent_index import IntentEmbeddingIndex
        _intent_index = IntentEmbeddingIndex(chroma_host=chroma_host, chroma_port=chroma_port, chroma_path=chroma_path)
    except Exception:
        _intent_index = None
        logger.exception("Intent embedding service unavailable")
    try:
        from mcp.tool_manager import MCPToolManager, Tool
        _tool_manager = MCPToolManager(api_key=cfg["api_key"] or "not-configured", base_url=cfg["base_url"], model=cfg["model"])
        if _knowledge is not None:
            def fallback(params: Dict[str, Any], _context: Any, error: str):
                return [{"title": "知识库降级结果", "content": f"知识库暂时不可用，未能检索“{params.get('query', '')}”。", "score": 0.0, "fallback": True, "error": error}]
            _tool_manager.register(Tool(
                name="knowledge_search", description="搜索 LumenMind ChromaDB 知识库",
                handler=_knowledge.search_handler,
                schema={"type": "object", "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}}, "required": ["query"]},
                cache_ttl=300.0, supports_rerank=True, fallback=fallback,
            ))
    except Exception:
        _tool_manager = None
        logger.exception("Knowledge tool manager unavailable")
    try:
        from evaluation.pi_evaluator import PiServiceEvaluator
        _evaluator = PiServiceEvaluator(
            agent_service_url=_agent_url(), api_key=cfg["api_key"], model=cfg["model"], base_url=cfg["base_url"],
            baseline_path=_project_path("EVAL_BASELINE_PATH", "data/eval/baseline.json"),
        )
    except Exception:
        _evaluator = None
        logger.exception("Evaluation service unavailable")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _memory
    from memory.conversation_memory import MemoryManager
    from memory.fact_service import FactService, FactServiceSettings
    from memory.summary_service import SummaryService, SummaryServiceSettings
    from memory.episodic_index import EpisodicIndexService
    from memory.model_client import MemoryModelClient

    # Invalid settings fail startup; an unavailable database degrades only memory.
    settings = _memory_config()
    initialize_business(database_path(), os.getenv("BUSINESS_SEED_DEMO", "false").lower() == "true")
    cfg = _llm_config()
    worker = None
    worker_task = None
    try:
        _memory = await asyncio.to_thread(
            MemoryManager, db_path=settings.db_path, namespace=settings.namespace,
            long_term_enabled=settings.long_term_enabled, context_max_chars=settings.context_max_chars,
            lease_seconds=settings.lease_seconds, settings=settings,
        )
    except (sqlite3.Error, OSError, RepositoryError):
        _memory = None
        logger.exception("SQLite memory unavailable; memory endpoints will return 503")
    if _memory is not None:
        _memory.fact_service = FactService(_memory._repo,
            MemoryModelClient(**cfg, timeout_seconds=settings.model_timeout_seconds, max_input_chars=settings.model_max_input_chars, context_window_tokens=settings.model_context_window_tokens),
            FactServiceSettings(min_messages=settings.extract_min_messages,
                max_wait_seconds=settings.extract_max_wait_seconds, max_messages=settings.extract_max_messages,
                max_chars=settings.extract_max_chars, lease_seconds=settings.job_lease_seconds))
        _memory._repo.job_max_attempts = settings.job_max_attempts
        _memory._repo.fact_max_count = settings.fact_max_count
        _memory._repo.fact_max_chars = settings.fact_max_chars
        _memory._repo.fact_block_max_chars = settings.fact_block_max_chars
        _memory.summary_service = SummaryService(_memory._repo,
            MemoryModelClient(**cfg, timeout_seconds=settings.model_timeout_seconds, purpose="summary", max_input_chars=settings.model_max_input_chars, context_window_tokens=settings.model_context_window_tokens),
            SummaryServiceSettings(trigger_chars=settings.summary_trigger_chars, trigger_turns=settings.summary_trigger_turns,
                recent_min_turns=settings.recent_min_turns, max_summary_chars=settings.summary_max_chars,
                lease_seconds=settings.job_lease_seconds, max_attempts=settings.job_max_attempts))
        services = [_memory.fact_service, _memory.summary_service]
        if settings.episodic_enabled:
            def create_episode_client():
                import chromadb
                return chromadb.HttpClient(host=os.getenv("CHROMA_HOST", "localhost"), port=int(os.getenv("CHROMA_PORT", "8001")),
                    settings=chromadb.Settings(anonymized_telemetry=False))
            _memory.index_service = EpisodicIndexService(_memory._repo, client_factory=create_episode_client)
            services.append(_memory.index_service)
        worker = MemoryWorker(_memory._repo, settings.recovery_poll_seconds, services=services)
        worker_task = asyncio.create_task(worker.run(), name="memory-lease-recovery")
    _app.state.memory_worker = worker
    _app.state.memory_worker_task = worker_task
    try:
        await asyncio.to_thread(
            _initialize_optional_services, cfg, os.getenv("CHROMA_HOST", "localhost"),
            int(os.getenv("CHROMA_PORT", "8001")), _project_path("CHROMA_PERSIST_DIRECTORY", "data/chroma"),
        )
        logger.info("Python infrastructure started; SQLite memory=%s", "ready" if _memory else "unavailable")
        yield
    finally:
        if worker is not None:
            worker.stop()
        if worker_task is not None:
            try:
                await asyncio.wait_for(worker_task, timeout=10)
            except asyncio.TimeoutError:
                worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await worker_task
        if _memory is not None:
            if _memory.fact_service is not None:
                await _memory.fact_service.close()
            if _memory.index_service is not None:
                await _memory.index_service.close()
            if _memory.summary_service is not None:
                await _memory.summary_service.close()
            await _memory.close()
            _memory = None
        logger.info("Python infrastructure stopped")


app = FastAPI(
    title="LumenMind Python Infrastructure",
    version="3.1.0",
    lifespan=lifespan,
    docs_url="/docs",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(business_router)


class ChatInput(BaseModel):
    message: str
    user_id: str = "anonymous"
    conv_id: Optional[str] = None


class MemoryContextInput(BaseModel):
    user_id: str = "anonymous"
    conv_id: str
    query: str = ""
    request_id: Optional[str] = None


class MemoryTurnInput(BaseModel):
    user_id: str = "anonymous"
    conv_id: str
    message: str
    response: str
    request_id: Optional[str] = None
    idempotency_key: Optional[str] = None


class MemoryBeginInput(BaseModel):
    user_id: str = "anonymous"
    conv_id: Optional[str] = Field(default=None, min_length=1, max_length=256)
    request_id: str = Field(min_length=1, max_length=256)
    message: str = Field(min_length=1, max_length=20000)
    idempotency_key: Optional[str] = None


class MemoryRenewInput(BaseModel):
    user_id: str = "anonymous"
    conv_id: str
    request_id: str
    lease_token: str


class MemoryFinishInput(BaseModel):
    user_id: str = "anonymous"
    conv_id: str
    request_id: str
    lease_token: str
    status: str = Field(default="COMPLETED", pattern="^(COMPLETED|FAILED|CANCELLED)$")
    response: Optional[str] = None


class MemoryFlushInput(BaseModel):
    user_id: str = "anonymous"
    conv_id: str
    request_id: Optional[str] = None


class MemoryFactInput(BaseModel):
    user_id: str = "anonymous"
    category: str
    content: str = Field(min_length=1, max_length=300)
    fact_key: Optional[str] = None
    source_seqs: List[int] = Field(default_factory=list)
    evidence: str = ""


class MemorySettingsInput(BaseModel):
    user_id: str = "anonymous"
    enabled: bool


class KnowledgeSearchInput(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=10)


class IntentEmbeddingInput(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=3, ge=1, le=10)


class DocInput(BaseModel):
    title: str
    content: str


class BatchDocInput(BaseModel):
    documents: List[DocInput]


async def _proxy(method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.request(method, f"{_agent_url()}{path}", json=payload)
        if response.status_code >= 400:
            raise HTTPException(response.status_code, response.text[:1000])
        return response.json()
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(503, f"Pi Agent 服务不可用: {error}") from error


@app.get("/health")
async def health():
    if _memory is None or _knowledge is None:
        raise HTTPException(503, "基础设施未就绪")
    return {
        "status": "ok",
        "service": "python-infrastructure",
        "memory": "ready",
        "knowledge_chunks": await _knowledge.doc_count_async(),
        "agent_runtime": "external-pi",
    }


# 兼容旧调用方：FastAPI 的 /chat 继续存在，但不再运行 Python Agent。
@app.post("/chat")
async def chat_proxy(body: ChatInput):
    return await _proxy("POST", "/chat", body.model_dump())


@app.get("/skills")
async def skills_proxy():
    return await _proxy("GET", "/skills")


@app.post("/skills/reload")
async def skills_reload_proxy():
    return await _proxy("POST", "/skills/reload")


@app.post("/intent")
async def intent_proxy(body: Dict[str, Any]):
    """兼容入口：转发 Pi 的无副作用意图识别接口。"""
    return await _proxy("POST", "/intent", body)


@app.post("/intent/embedding")
async def intent_embedding(body: IntentEmbeddingInput):
    """返回语义相近的候选意图；最终意图仍由 Pi Router/LLM 决定。"""
    if _intent_index is None:
        raise HTTPException(503, "意图 Embedding 索引未就绪")
    return {
        "query": body.query,
        "candidates": await _intent_index.search_async(body.query, body.top_k),
    }


@app.get("/trace/tool/{request_id}")
async def tool_trace_proxy(request_id: str):
    """兼容入口：按 request_id 查询 Pi 最近请求的工具轨迹。"""
    return await _proxy("GET", f"/trace/tool/{request_id}")


@app.get("/trace/tools")
async def recent_tool_traces_proxy(limit: int = 20):
    """兼容入口：查询 Pi 最近请求的工具轨迹。"""
    return await _proxy("GET", f"/trace/tools?limit={max(1, min(200, limit))}")


def _memory_error(error: Exception) -> HTTPException:
    if isinstance(error, HTTPException):
        return error
    if isinstance(error, (ConflictError, LeaseError)):
        return HTTPException(409, str(error))
    if isinstance(error, CapacityError):
        return HTTPException(409, {"code": "CAPACITY_EXCEEDED", "detail": str(error)})
    if isinstance(error, (sqlite3.Error, OSError, RepositoryError)):
        logger.warning("Memory storage unavailable: %s", error)
        return HTTPException(503, "SQLite 记忆主库暂时不可用")
    if isinstance(error, ContextBudgetError):
        return HTTPException(413, str(error))
    if isinstance(error, (ValueError, TypeError)):
        return HTTPException(422, str(error))
    if isinstance(error, NotImplementedError):
        return HTTPException(501, str(error))
    logger.exception("Unexpected memory error", exc_info=error)
    return HTTPException(500, "记忆服务内部错误")


def _require_memory():
    if _memory is None or getattr(_memory, "_repo", None) is None:
        raise HTTPException(503, "SQLite 记忆主库未就绪")
    return _memory


async def _memory_call(method: str, *args, **kwargs):
    manager = _require_memory()
    try:
        return await getattr(manager, method)(*args, **kwargs)
    except Exception as error:
        raise _memory_error(error) from error


def _receipt_dict(receipt) -> Dict[str, Any]:
    return {
        "status": receipt.status, "memory_status": receipt.memory_status,
        "request_id": receipt.request_id, "conv_id": receipt.conv_id,
        "turn_seq": receipt.turn_seq, "source_seq": receipt.source_seq,
        "lease_token": receipt.lease_token, "lease_until": receipt.lease_until,
        "context_revision": receipt.context_revision, "response": receipt.response,
        "replayed": getattr(receipt, "replayed", False),
    }


@app.post("/memory/context")
async def memory_context(body: MemoryContextInput):
    manager = _require_memory()
    try:
        snapshot = await manager.get_snapshot(body.user_id, body.conv_id, body.request_id, body.query)
        return manager.snapshot_to_dict(snapshot)
    except Exception as error:
        raise _memory_error(error) from error


@app.post("/memory/turn")
async def memory_turn(body: MemoryTurnInput, background_tasks: BackgroundTasks):
    """Compatibility writer; errors must never become a successful stored receipt."""
    request_id = body.request_id or str(uuid.uuid4())
    begin = await _memory_call("begin_turn", body.user_id, body.conv_id, request_id, body.message, body.idempotency_key)
    receipt = await _memory_call("finish_turn", body.user_id, begin.conv_id, begin.request_id, begin.lease_token, "COMPLETED", body.response)
    if not getattr(receipt, "replayed", False):
        MEMORY_TURNS.inc()
    return {**_receipt_dict(receipt), "status": "stored", "retry_deduplication": bool(body.request_id or body.idempotency_key)}


@app.post("/memory/turn/begin")
async def memory_turn_begin(body: MemoryBeginInput, idempotency_header: Optional[str] = Header(default=None, alias="Idempotency-Key")):
    if idempotency_header and body.idempotency_key and idempotency_header != body.idempotency_key:
        raise HTTPException(422, "Idempotency-Key header and body must match")
    receipt = await _memory_call("begin_turn", body.user_id, body.conv_id, body.request_id, body.message, idempotency_header or body.idempotency_key)
    return _receipt_dict(receipt)


@app.post("/memory/turn/renew")
async def memory_turn_renew(body: MemoryRenewInput):
    until = await _memory_call("renew_turn", body.user_id, body.conv_id, body.request_id, body.lease_token)
    return {"status": "renewed", "lease_until": until}


@app.post("/memory/turn/finish")
async def memory_turn_finish(body: MemoryFinishInput):
    receipt = await _memory_call("finish_turn", body.user_id, body.conv_id, body.request_id, body.lease_token, body.status, body.response)
    if not getattr(receipt, "replayed", False):
        MEMORY_TURNS.inc()
    return _receipt_dict(receipt)


@app.post("/memory/flush")
async def memory_flush(body: MemoryFlushInput):
    """Bounded flush; source material is loaded from the durable current request."""
    result = await _memory_call("flush", body.user_id, body.conv_id, body.request_id)
    return {"request_id": body.request_id or str(uuid.uuid4()), **result}


@app.get("/memory/facts")
async def memory_facts(user_id: str = "anonymous"):
    return await _memory_call("list_facts", user_id)


@app.post("/memory/facts")
async def memory_add_fact(body: MemoryFactInput):
    _require_memory()
    raise HTTPException(501, "Direct ACTIVE writes are disabled; flush a persisted user request instead")


@app.delete("/memory/facts/{fact_id}")
async def memory_retract_fact(fact_id: str, user_id: str = "anonymous", request_id: Optional[str] = None):
    operation_id = request_id or str(uuid.uuid4())
    result = await _memory_call("retract_fact", user_id, fact_id, request_id=operation_id)
    return {"request_id": operation_id, **result}


@app.post("/memory/clear")
async def memory_clear(body: MemoryFlushInput):
    operation_id = body.request_id or str(uuid.uuid4())
    result = await _memory_call("clear_facts", body.user_id, request_id=operation_id)
    return {"request_id": operation_id, **result}


@app.post("/memory/settings")
async def memory_settings(body: MemorySettingsInput):
    return await _memory_call("set_long_term_enabled", body.user_id, body.enabled)


@app.delete("/memory/conversations/{conv_id}")
async def memory_delete_conversation(conv_id: str, user_id: str = "anonymous"):
    return await _memory_call("delete_conversation", user_id, conv_id)


@app.get("/memory/status")
async def memory_status():
    from memory.metrics import memory_metrics_snapshot
    manager = _require_memory()
    try:
        return await asyncio.to_thread(memory_metrics_snapshot, manager._repo)
    except Exception as error:
        raise _memory_error(error) from error


@app.get("/memory/cleanup")
async def memory_cleanup(user_id: str = "anonymous"):
    manager = _require_memory()
    try:
        return await asyncio.to_thread(manager._repo.index_cleanup_progress, user_id)
    except Exception as error:
        raise _memory_error(error) from error


@app.delete("/memory/all")
async def memory_delete_all(user_id: str = "anonymous"):
    return await _memory_call("delete_all", user_id)


async def _knowledge_search(query: str, top_k: int) -> Dict[str, Any]:
    if _knowledge is None:
        raise HTTPException(503, "知识库未就绪")
    KNOWLEDGE_SEARCHES.inc()
    rewrite = os.getenv("KNOWLEDGE_QUERY_REWRITE", "false").lower() in {"1", "true", "yes", "on"}
    if rewrite and _tool_manager is not None:
        result = await _tool_manager.search_with_rewrite("knowledge_search", query, top_k=top_k)
        return {
            "query": query,
            "results": result.data if isinstance(result.data, list) else [],
            "reranked": result.reranked,
            "cached": result.cached,
        }
    return {
        "query": query,
        "results": await _knowledge.search_async(query, top_k),
        "reranked": False,
        "cached": False,
    }


@app.post("/knowledge/search")
async def knowledge_search(body: KnowledgeSearchInput):
    return await _knowledge_search(body.query, body.top_k)


@app.post("/search")
async def legacy_search(query: str, top_k: int = 5):
    return await _knowledge_search(query, max(1, min(10, top_k)))


@app.post("/knowledge/add")
async def add_knowledge(body: BatchDocInput):
    if _knowledge is None:
        raise HTTPException(503, "知识库未就绪")
    count = await _knowledge.add_documents_async([item.model_dump() for item in body.documents])
    total = await _knowledge.doc_count_async()
    return {"message": f"成功写入 {count} 个文档片段（重复文档会更新）", "added_chunks": count, "total_chunks": total}


@app.post("/knowledge/upload")
async def upload_knowledge(file: UploadFile = File(...)):
    if _knowledge is None:
        raise HTTPException(503, "知识库未就绪")
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "文件大小超过 10MB 限制")
    text = content.decode("utf-8", errors="ignore")
    filename = file.filename or "unknown"
    if filename.lower().endswith(".json"):
        try:
            documents = json.loads(text)
        except json.JSONDecodeError as error:
            raise HTTPException(400, f"JSON 解析失败: {error}") from error
        if not isinstance(documents, list):
            raise HTTPException(400, "JSON 文件必须是 [{title, content}, ...] 数组")
    else:
        documents = [{"title": filename.rsplit(".", 1)[0], "content": text}]
    count = await _knowledge.add_documents_async(documents)
    total = await _knowledge.doc_count_async()
    return {"message": f"文件 {filename} 写入成功（重复文档会更新）", "added_chunks": count, "total_chunks": total}


@app.get("/knowledge/stats")
async def knowledge_stats():
    if _knowledge is None:
        raise HTTPException(503, "知识库未就绪")
    return {"total_chunks": await _knowledge.doc_count_async()}


@app.post("/eval/run")
async def run_eval(body: Optional[EvalRunInput] = None):
    if _evaluator is None:
        raise HTTPException(503, "评测器未就绪")
    from evaluation.pi_evaluator import DEFAULT_DIALOG_CASES, DEFAULT_INTENT_CASES, IntentTestCase

    if body is None:
        intent_cases = DEFAULT_INTENT_CASES
        dialog_cases = DEFAULT_DIALOG_CASES
        record_metrics = True
    else:
        # 传入自定义评测集时，未提供的部分默认为空，避免只想评意图却自动跑默认对话集。
        intent_cases = (
            [IntentTestCase(**item.model_dump()) for item in body.intent_cases]
            if body.intent_cases is not None else []
        )
        dialog_cases = (
            [item.model_dump(exclude_none=True) for item in body.dialog_cases]
            if body.dialog_cases is not None else []
        )
        record_metrics = body.record_metrics
    if record_metrics:
        EVALUATIONS.inc()
    options = ({"repeats": body.repeats, "judge_mode": body.judge_mode,
                "dataset_version": body.dataset_version, "persist_report": body.persist_report}
               if body is not None else {})
    try:
        report = await _evaluator.run(intent_cases, dialog_cases, **options)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return _evaluator.serialize(report)


@app.get("/monitor")
async def monitor_summary():
    agent_health: Dict[str, Any]
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{_agent_url()}/health")
            response.raise_for_status()
            agent_health = response.json()
    except Exception as error:
        agent_health = {"status": "unavailable", "error": str(error)}
    knowledge_chunks = await _knowledge.doc_count_async() if _knowledge else 0
    return {
        "pi_agent": agent_health,
        "python_infrastructure": {
            "memory_turns": MEMORY_TURNS._value.get(),
            "knowledge_searches": KNOWLEDGE_SEARCHES._value.get(),
            "evaluations": EVALUATIONS._value.get(),
            "knowledge_chunks": knowledge_chunks,
        },
        "tool_stats": _tool_manager.get_stats() if _tool_manager else {},
    }


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    uvicorn.run(
        "api.main:app",
        host=os.getenv("PYTHON_HOST", "0.0.0.0"),
        port=int(os.getenv("PYTHON_PORT", "8000")),
        reload=os.getenv("APP_ENV") == "development",
    )
