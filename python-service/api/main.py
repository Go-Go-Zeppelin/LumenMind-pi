"""LumenMind Python 基础设施服务。

Agent 运行时已迁移到 TypeScript/Pi；本服务继续承载记忆、知识库管理、
评测、Prometheus 和旧 FastAPI 兼容入口。
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
import uuid
from contextlib import asynccontextmanager
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
from memory.repository import CapacityError, ConflictError, LeaseError, RepositoryError

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


def _memory_config() -> Dict[str, Any]:
    return {
        "db_path": _project_path("MEMORY_DB_PATH", "data/memory/memory.sqlite3"),
        "namespace": os.getenv("MEMORY_NAMESPACE", "lumenmind").strip() or "lumenmind",
        "long_term_enabled": os.getenv("MEMORY_LONG_TERM_ENABLED", "true").lower() not in {"0", "false", "no", "off"},
        "context_max_chars": int(os.getenv("MEMORY_CONTEXT_MAX_CHARS", "24000")),
    }


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _memory, _knowledge, _intent_index, _tool_manager, _evaluator
    from evaluation.pi_evaluator import PiServiceEvaluator
    from mcp.knowledge_base import KnowledgeBase
    from mcp.intent_index import IntentEmbeddingIndex
    from mcp.tool_manager import MCPToolManager, Tool
    from memory.conversation_memory import MemoryManager

    initialize_business(database_path(), os.getenv("BUSINESS_SEED_DEMO", "false").lower() == "true")
    cfg = _llm_config()
    chroma_host = os.getenv("CHROMA_HOST", "localhost")
    chroma_port = int(os.getenv("CHROMA_PORT", "8001"))
    chroma_path = _project_path("CHROMA_PERSIST_DIRECTORY", "data/chroma")

    _memory = MemoryManager(
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        chroma_host=chroma_host,
        chroma_port=chroma_port,
        chroma_path=chroma_path,
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        model=cfg["model"],
        **_memory_config(),
    )
    _knowledge = KnowledgeBase(
        chroma_host=chroma_host,
        chroma_port=chroma_port,
        chroma_path=chroma_path,
    )
    _intent_index = IntentEmbeddingIndex(
        chroma_host=chroma_host,
        chroma_port=chroma_port,
        chroma_path=chroma_path,
    )
    _tool_manager = MCPToolManager(
        api_key=cfg["api_key"] or "not-configured",
        base_url=cfg["base_url"],
        model=cfg["model"],
    )

    def fallback(params: Dict[str, Any], _context: Any, error: str):
        return [{
            "title": "知识库降级结果",
            "content": f"知识库暂时不可用，未能检索“{params.get('query', '')}”。",
            "score": 0.0,
            "fallback": True,
            "error": error,
        }]

    _tool_manager.register(Tool(
        name="knowledge_search",
        description="搜索 LumenMind ChromaDB 知识库",
        handler=_knowledge.search_handler,
        schema={
            "type": "object",
            "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
            "required": ["query"],
        },
        cache_ttl=300.0,
        supports_rerank=True,
        fallback=fallback,
    ))
    _evaluator = PiServiceEvaluator(
        agent_service_url=_agent_url(),
        api_key=cfg["api_key"],
        model=cfg["model"],
        base_url=cfg["base_url"],
        baseline_path=_project_path("EVAL_BASELINE_PATH", "data/eval/baseline.json"),
    )
    logger.info("Python 基础设施已就绪：memory + knowledge + evaluation + management")
    yield
    if _memory is not None:
        await _memory.close()
    logger.info("Python 基础设施已关闭")


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
    conv_id: str
    request_id: str = Field(min_length=1)
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
    status: str = "COMPLETED"
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


@app.post("/memory/context")
async def memory_context(body: MemoryContextInput):
    if _memory is None:
        raise HTTPException(503, "记忆服务未就绪")
    context = await _memory.get_context(body.user_id, body.conv_id, query=body.query)
    if getattr(_memory, "_repo", None) is not None:
        snapshot = await _memory.get_snapshot(body.user_id, body.conv_id)
        result = _memory.snapshot_to_dict(snapshot)
        # 当前 request 的 user 原文由 begin 保存，但 prompt 只能在 agent.prompt()
        # 中出现一次；调用方传 request_id 时从快照移除该轮。
        if body.request_id:
            result["recent_messages"] = [m for m in result["recent_messages"] if m.get("request_id") != body.request_id]
        return result
    return {
        "prompt_text": context.to_prompt_text(),
        "recent_messages": [
            {
                "role": message.role.value,
                "content": message.content,
                "timestamp": message.timestamp.isoformat(),
                "metadata": message.metadata,
            }
            for message in context.recent_messages
        ],
        "relevant_history": context.relevant_history,
        "user_profile": context.user_profile,
        "summary": context.summary,
    }


@app.post("/memory/turn")
async def memory_turn(body: MemoryTurnInput, background_tasks: BackgroundTasks):
    if _memory is None:
        raise HTTPException(503, "记忆服务未就绪")
    if getattr(_memory, "_repo", None) is not None:
        request_id = body.request_id or str(uuid.uuid4())
        try:
            begin = await _memory.begin_turn(body.user_id, body.conv_id, request_id, body.message, body.idempotency_key)
            receipt = await _memory.finish_turn(body.user_id, body.conv_id, request_id, begin.lease_token, "COMPLETED", body.response)
            MEMORY_TURNS.inc()
            return {"status": "stored", "memory_status": "stored", "request_id": request_id, "conv_id": body.conv_id, "turn_seq": receipt.turn_seq, "source_seq": receipt.source_seq}
        except LeaseError as error:
            raise HTTPException(409, str(error)) from error
        except ConflictError as error:
            raise HTTPException(409, str(error)) from error
    from memory.conversation_memory import MsgRole
    await _memory.add_message(body.user_id, body.conv_id, MsgRole.USER, body.message)
    await _memory.add_message(body.user_id, body.conv_id, MsgRole.ASSISTANT, body.response)
    background_tasks.add_task(_memory.update_profile, body.user_id, body.conv_id)
    MEMORY_TURNS.inc()
    return {"status": "stored", "conv_id": body.conv_id, "memory_status": "stored"}


def _memory_error(error: Exception) -> HTTPException:
    if isinstance(error, (ConflictError, LeaseError)):
        return HTTPException(409, str(error))
    if isinstance(error, CapacityError):
        return HTTPException(409, {"code": "CAPACITY_EXCEEDED", "detail": str(error)})
    if isinstance(error, RepositoryError):
        return HTTPException(503, str(error))
    return HTTPException(500, str(error))


@app.post("/memory/turn/begin")
async def memory_turn_begin(body: MemoryBeginInput, idempotency_header: Optional[str] = Header(default=None, alias="Idempotency-Key")):
    if _memory is None or getattr(_memory, "_repo", None) is None:
        raise HTTPException(503, "SQLite 记忆主库未就绪")
    try:
        receipt = await _memory.begin_turn(body.user_id, body.conv_id, body.request_id, body.message, idempotency_header or body.idempotency_key)
        return {"status": receipt.status, "request_id": receipt.request_id, "conv_id": receipt.conv_id, "turn_seq": receipt.turn_seq, "source_seq": receipt.source_seq, "lease_token": receipt.lease_token, "lease_until": receipt.lease_until, "context_revision": receipt.context_revision}
    except Exception as error:
        raise _memory_error(error) from error


@app.post("/memory/turn/renew")
async def memory_turn_renew(body: MemoryRenewInput):
    if _memory is None or getattr(_memory, "_repo", None) is None:
        raise HTTPException(503, "SQLite 记忆主库未就绪")
    try:
        until = await _memory.renew_turn(body.user_id, body.conv_id, body.request_id, body.lease_token)
        return {"status": "renewed", "lease_until": until}
    except Exception as error:
        raise _memory_error(error) from error


@app.post("/memory/turn/finish")
async def memory_turn_finish(body: MemoryFinishInput):
    if _memory is None or getattr(_memory, "_repo", None) is None:
        raise HTTPException(503, "SQLite 记忆主库未就绪")
    try:
        receipt = await _memory.finish_turn(body.user_id, body.conv_id, body.request_id, body.lease_token, body.status, body.response)
        return {"status": receipt.status, "memory_status": "stored", "request_id": receipt.request_id, "conv_id": receipt.conv_id, "turn_seq": receipt.turn_seq, "source_seq": receipt.source_seq, "response": receipt.response}
    except Exception as error:
        raise _memory_error(error) from error


@app.post("/memory/flush")
async def memory_flush(body: MemoryFlushInput):
    """显式 flush 的确定性首期实现：从当前 user 原文写入的事实由管理接口维护。

    返回 PENDING/NOOP 等真实状态，绝不把后台任务排队描述成已记住。
    """
    if _memory is None or getattr(_memory, "_repo", None) is None:
        raise HTTPException(503, "SQLite 记忆主库未就绪")
    result = await _memory.flush(body.user_id, body.conv_id, body.request_id)
    return {"request_id": body.request_id or str(uuid.uuid4()), **result}


@app.get("/memory/facts")
async def memory_facts(user_id: str = "anonymous"):
    if _memory is None or getattr(_memory, "_repo", None) is None:
        raise HTTPException(503, "SQLite 记忆主库未就绪")
    return await _memory.list_facts(user_id)


@app.post("/memory/facts")
async def memory_add_fact(body: MemoryFactInput):
    if _memory is None or getattr(_memory, "_repo", None) is None:
        raise HTTPException(503, "SQLite 记忆主库未就绪")
    try:
        return await _memory.add_explicit_fact(body.user_id, body.category, body.content, body.fact_key, body.source_seqs, body.evidence)
    except Exception as error:
        raise _memory_error(error) from error


@app.delete("/memory/facts/{fact_id}")
async def memory_retract_fact(fact_id: str, user_id: str = "anonymous"):
    if _memory is None or getattr(_memory, "_repo", None) is None:
        raise HTTPException(503, "SQLite 记忆主库未就绪")
    try:
        return await _memory.retract_fact(user_id, fact_id)
    except Exception as error:
        raise _memory_error(error) from error


@app.post("/memory/clear")
async def memory_clear(body: MemoryFlushInput):
    if _memory is None or getattr(_memory, "_repo", None) is None:
        raise HTTPException(503, "SQLite 记忆主库未就绪")
    return await _memory.clear_facts(body.user_id)


@app.post("/memory/settings")
async def memory_settings(body: MemorySettingsInput):
    if _memory is None or getattr(_memory, "_repo", None) is None:
        raise HTTPException(503, "SQLite 记忆主库未就绪")
    return await _memory.set_long_term_enabled(body.user_id, body.enabled)


@app.delete("/memory/conversations/{conv_id}")
async def memory_delete_conversation(conv_id: str, user_id: str = "anonymous"):
    if _memory is None or getattr(_memory, "_repo", None) is None:
        raise HTTPException(503, "SQLite 记忆主库未就绪")
    try:
        return await _memory.delete_conversation(user_id, conv_id)
    except Exception as error:
        raise _memory_error(error) from error


@app.delete("/memory/all")
async def memory_delete_all(user_id: str = "anonymous"):
    if _memory is None or getattr(_memory, "_repo", None) is None:
        raise HTTPException(503, "SQLite 记忆主库未就绪")
    return await _memory.delete_all(user_id)


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
