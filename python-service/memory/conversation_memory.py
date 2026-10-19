"""Authoritative SQLite memory protocol and legacy Redis/Chroma adapters.

New deployments save turns and durable jobs in SQLite. Model-driven fact
extraction, rolling summaries and episodic synchronization are separate modules;
this manager never substitutes keyword matches for an authoritative Judge.
"""
import hashlib
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

from anthropic import AsyncAnthropic

from core.llm_utils import extract_text_content
from .context_builder import ContextBudget, build_context
from .config import MemorySettings
from .metrics import observe_context
from .models import MemorySnapshot, TurnReceipt
from .repository import CapacityError, ConflictError, LeaseError, MemoryRepository

logger = logging.getLogger(__name__)


class MsgRole(Enum):
    USER      = "user"
    ASSISTANT = "assistant"
    SYSTEM    = "system"


@dataclass
class Message:
    role:       MsgRole
    content:    str
    timestamp:  datetime = field(default_factory=datetime.now)
    metadata:   Dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryContext:
    """传给 Agent 的完整上下文。"""
    recent_messages:  List[Message]   # 工作记忆：最近对话
    relevant_history: List[str]       # 情景记忆：语义相关的历史片段
    user_profile:     Dict[str, Any]  # 用户画像：偏好、常用实体
    summary:          str             # 当前会话摘要（压缩后）

    @staticmethod
    def _clean(text: str) -> str:
        """移除 Unicode 代理字符，防止编码错误。"""
        return text.encode("utf-8", errors="ignore").decode("utf-8")

    def to_prompt_text(self) -> str:
        """将记忆上下文格式化为 LLM 可用的文本。"""
        parts = []
        if self.summary:
            parts.append(f"[会话摘要]\n{self._clean(self.summary)}")
        if self.relevant_history:
            parts.append("[相关历史]\n" + "\n".join(f"- {self._clean(h)}" for h in self.relevant_history[:3]))
        if self.user_profile:
            parts.append(f"[用户画像]\n{json.dumps(self.user_profile, ensure_ascii=True)}")
        if self.recent_messages:
            parts.append("[最近对话]")
            for m in self.recent_messages:
                parts.append(f"{m.role.value}: {self._clean(m.content)}")
        return "\n\n".join(parts)


class MemoryManager:
    """SQLite memory facade; optional legacy adapters are initialized without db_path."""

    WORKING_MAX   = 20    # 工作记忆最大条数，超过则触发压缩
    COMPRESS_AT   = 15    # 达到此条数时压缩，保留摘要 + 最近 5 条
    HISTORY_TOP_K = 5     # 情景记忆检索返回条数

    # 用户画像提取参数：控制模型成本，同时防止脏数据或无限膨胀。
    PROFILE_WINDOW             = 10    # 单次提取最多参考最近多少条工作记忆
    PROFILE_MAX_DIALOGUE_CHARS = 6000  # 单次提取对话文本上限
    PROFILE_MAX_PREFERENCES    = 50    # preferences 最大条数
    PROFILE_MAX_ENTITY_KEYS    = 20    # entities 最大键数
    PROFILE_MAX_ENTITY_VALUES  = 50    # 每个 entity 键最大值数量
    PROFILE_MAX_ITEM_CHARS     = 120   # 单条偏好/实体的最大字符数

    def __init__(
        self,
        redis_url:    str = "redis://localhost:6379/0",
        chroma_host:  str = "localhost",
        chroma_port:  int = 8000,
        chroma_path:  str = "./data/chroma",
        api_key:      str = "",
        base_url:     Optional[str] = None,
        model:        str = "claude-3-5-sonnet-20241022",
        db_path:      Optional[str] = None,
        namespace:    str = "lumenmind",
        long_term_enabled: bool = True,
        context_max_chars: int = 24000,
        lease_seconds: int = 90,
        settings: Optional[MemorySettings] = None,
    ):
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = None
        self._model  = model
        self.namespace = namespace.strip() or "lumenmind"
        self.long_term_enabled = bool(long_term_enabled)
        self.context_max_chars = max(1000, int(context_max_chars))
        self.lease_seconds = lease_seconds
        self.settings = settings
        self.fact_service = None
        self.summary_service = None
        self.index_service = None
        self._repo = MemoryRepository(db_path, self.namespace, long_term_enabled=self.long_term_enabled) if db_path else None
        self._redis = None
        self._episodic = None
        self._profile = None
        # The authoritative SQLite path requires neither a model client nor
        # Redis/Chroma packages, network connections or embedding initialization.
        if self._repo is not None:
            return
        import chromadb
        import redis.asyncio as redis

        self._client = AsyncAnthropic(**kwargs)
        self._redis = redis.from_url(redis_url, decode_responses=True)

        # ChromaDB：优先连接独立服务（docker compose 模式），连不上则降级为本地嵌入式
        try:
            # HttpClient 默认也会初始化 ChromaDB telemetry；显式关闭避免 posthog 兼容性错误日志。
            chroma = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            chroma.heartbeat()  # 测试连接
            logger.info(f"ChromaDB 已连接: {chroma_host}:{chroma_port}")
        except Exception:
            logger.info(f"ChromaDB 服务不可用，使用本地嵌入式模式: {chroma_path}")
            chroma = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )

        # 情景记忆：存储历史对话片段
        self._episodic = chroma.get_or_create_collection("episodic")
        # 用户画像：存储提炼出的偏好和实体
        self._profile  = chroma.get_or_create_collection("user_profile")

    # ── SQLite authority / begin-finish protocol ───────────────────────────

    async def begin_turn(
        self, user_id: str, conv_id: Optional[str], request_id: str, message: str,
        idempotency_key: Optional[str] = None, lease_seconds: Optional[int] = None,
    ) -> TurnReceipt:
        """在一个 SQLite 短事务内分配 source_seq/turn_seq 并保存 user 原文。"""
        if self._repo is None:
            # 旧部署仍可使用 Redis 适配器；新部署始终传入 db_path。
            raise RuntimeError("SQLite memory repository is not configured")
        return await asyncio.to_thread(
            self._repo.begin_turn, self._safe_text(user_id), self._safe_text(conv_id) if conv_id else None,
            self._safe_text(request_id), self._safe_text(message), idempotency_key, lease_seconds or self.lease_seconds,
        )

    async def renew_turn(self, user_id: str, conv_id: str, request_id: str, lease_token: str, lease_seconds: Optional[int] = None) -> str:
        if self._repo is None:
            raise RuntimeError("SQLite memory repository is not configured")
        return await asyncio.to_thread(self._repo.renew_turn, self._safe_text(user_id), self._safe_text(conv_id), self._safe_text(request_id), lease_token, lease_seconds or self.lease_seconds)

    async def finish_turn(self, user_id: str, conv_id: str, request_id: str, lease_token: str, status: str = "COMPLETED", response: Optional[str] = None) -> TurnReceipt:
        if self._repo is None:
            raise RuntimeError("SQLite memory repository is not configured")
        return await asyncio.to_thread(self._repo.finish_turn, self._safe_text(user_id), self._safe_text(conv_id), self._safe_text(request_id), lease_token, status, response)

    async def get_snapshot(self, user_id: str, conv_id: str, request_id: Optional[str] = None, query: str = "") -> MemorySnapshot:
        if self._repo is None:
            raise RuntimeError("SQLite memory repository is not configured")
        candidate_ids = await self.index_service.search_candidates(user_id, query) if self.index_service is not None else []
        snapshot = await asyncio.to_thread(
            self._repo.get_snapshot, self._safe_text(user_id), self._safe_text(conv_id),
            exclude_request_id=request_id, candidate_ids=candidate_ids,
        )
        if self._safe_text(user_id) in {"anonymous", "demo", ""} or not self.long_term_enabled:
            snapshot.facts = []
            snapshot.episodes = []
            snapshot.routing_context = ""
        return snapshot

    async def list_facts(self, user_id: str) -> Dict[str, Any]:
        if self._safe_text(user_id) in {"anonymous", "demo", ""}:
            return {"facts": [], "revision": 0, "enabled": False, "status": "DISABLED"}
        if self._repo is None:
            return {"facts": [], "revision": 0, "enabled": False}
        return await asyncio.to_thread(self._repo.list_facts, self._safe_text(user_id))

    async def add_explicit_fact(self, *args, **kwargs) -> Dict[str, Any]:
        """Active facts may only be committed by a validated source-backed Judge."""
        raise NotImplementedError("Direct ACTIVE writes are disabled; flush a persisted user request instead")

    async def retract_fact(self, user_id: str, fact_id: str, request_id: Optional[str] = None) -> Dict[str, Any]:
        if self._safe_text(user_id) in {"anonymous", "demo", ""}:
            return {"status": "DISABLED", "mutated": False}
        if self._repo is None:
            raise RuntimeError("SQLite memory repository is not configured")
        return await asyncio.to_thread(self._repo.retract_fact, self._safe_text(user_id), self._safe_text(fact_id), request_id=request_id)

    async def clear_facts(self, user_id: str, request_id: Optional[str] = None) -> Dict[str, Any]:
        if self._safe_text(user_id) in {"anonymous", "demo", ""}:
            return {"status": "DISABLED", "mutated": False}
        if self._repo is None:
            raise RuntimeError("SQLite memory repository is not configured")
        return await asyncio.to_thread(self._repo.clear_facts, self._safe_text(user_id), request_id=request_id)

    async def flush(self, user_id: str, conv_id: str = "", request_id: Optional[str] = None) -> Dict[str, Any]:
        if self._safe_text(user_id) in {"anonymous", "demo", ""} or not self.long_term_enabled:
            return {"status": "DISABLED", "covered_source_seq": 0, "revision": 0, "applied": 0, "mutated": False}
        if self._repo is None:
            return {"status": "UNAVAILABLE", "covered_source_seq": 0, "revision": 0, "applied": 0, "mutated": False}
        if self.fact_service is not None:
            return await self.fact_service.flush(self._safe_text(user_id), request_id)
        return await asyncio.to_thread(self._repo.flush_user_messages, self._safe_text(user_id))

    async def set_long_term_enabled(self, user_id: str, enabled: bool) -> Dict[str, Any]:
        if self._repo is None:
            raise RuntimeError("SQLite memory repository is not configured")
        if enabled and not self.long_term_enabled:
            return {"status": "DISABLED", "enabled": False, "mutated": False}
        return await asyncio.to_thread(self._repo.set_long_term_enabled, self._safe_text(user_id), bool(enabled))

    async def delete_conversation(self, user_id: str, conv_id: str) -> Dict[str, Any]:
        if self._repo is None:
            raise RuntimeError("SQLite memory repository is not configured")
        return await asyncio.to_thread(self._repo.delete_conversation, self._safe_text(user_id), self._safe_text(conv_id))

    async def delete_all(self, user_id: str) -> Dict[str, Any]:
        if self._repo is None:
            raise RuntimeError("SQLite memory repository is not configured")
        return await asyncio.to_thread(self._repo.delete_all, self._safe_text(user_id))

    def snapshot_to_dict(self, snapshot: MemorySnapshot) -> Dict[str, Any]:
        """Render one filtered snapshot with the shared whole-turn budgeter."""
        raw = {
            "schema_version": 2,
            "status": snapshot.status,
            "snapshot": {
                "fact_revision": snapshot.fact_revision,
                "context_revision": snapshot.context_revision,
                "erase_epoch": snapshot.erase_epoch,
                "summary_generation": snapshot.summary_generation,
            },
            "blocks": {"facts": snapshot.facts, "episodes": snapshot.episodes, "summary": snapshot.summary},
            "routing_context": snapshot.routing_context,
            "recent_messages": snapshot.recent_messages,
            "context_truncated": snapshot.context_truncated,
        }
        settings = self.settings
        result = build_context(raw, ContextBudget(
            total_chars=self.context_max_chars,
            summary_chars=settings.summary_max_chars if settings else 4000,
            facts_chars=settings.fact_block_max_chars if settings else 3000,
            episodes_chars=settings.episode_block_max_chars if settings else 3000,
            recent_min_turns=settings.recent_min_turns if settings else 3,
        ))
        result.update({"schema_version": 2, "status": snapshot.status, "snapshot": raw["snapshot"]})
        result["relevant_history"] = [item.get("summary", "") for item in result["blocks"]["episodes"]]
        result["user_profile"] = {
            "preferences": [item["content"] for item in result["blocks"]["facts"] if item.get("category") == "preference"],
            "entities": {},
        }
        observe_context(result["budget"]["rendered_chars"], result["budget"]["context_truncated"])
        return result

    # ── 写入 ──────────────────────────────────────────────────────────────────

    async def add_message(
        self,
        user_id: str,
        conv_id: str,
        role:    MsgRole,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """将一条消息写入工作记忆，超阈值时自动压缩。"""
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        clean_metadata = {
            self._safe_text(k): self._safe_metadata_value(v)
            for k, v in (metadata or {}).items()
        }
        msg = Message(role=role, content=self._safe_text(content), metadata=clean_metadata)
        key = self._wm_key(user_id, conv_id)

        payload = json.dumps({
            "role":      msg.role.value,
            "content":   msg.content,
            "ts":        msg.timestamp.isoformat(),
            "metadata":  msg.metadata,
        })
        # 列表追加与 TTL 在同一 Redis 事务中完成，避免只写入一半或 TTL 漂移。
        if hasattr(self._redis, "pipeline"):
            pipeline = self._redis.pipeline(transaction=True)
            pipeline.lpush(key, payload)
            pipeline.expire(key, 86400)
            await pipeline.execute()
        else:  # 最小 fake Redis 或旧适配器
            await self._redis.lpush(key, payload)
            await self._redis.expire(key, 86400)

        # 只在 assistant 完成后检查，避免 user/assistant 轮次被压缩拆开。
        if role is MsgRole.ASSISTANT and await self._redis.llen(key) >= self.COMPRESS_AT:
            await self._compress(user_id, conv_id)

    async def update_profile(self, user_id: str, conv_id: str) -> None:
        """
        从当前工作记忆中提炼用户偏好，合并进用户级画像。

        与旧实现相比：
          - 读取已有画像并一起交给 LLM，避免最近窗口未提到的偏好被覆盖丢失；
          - 对画像做归一化、去重、限长，防止脏数据或无限膨胀；
          - 写入 user 级单文档并使用 upsert，读取不再依赖 limit=1 的偶然顺序。
        """
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        messages = await self._get_working_memory(user_id, conv_id)
        if not messages:
            return

        text = self._safe_text(
            "\n".join(f"{m.role.value}: {m.content}" for m in messages[-self.PROFILE_WINDOW:])
        )[-self.PROFILE_MAX_DIALOGUE_CHARS:]
        existing = await self._get_profile(user_id)
        prompt = self._build_profile_prompt(text, existing)

        try:
            resp = await self._client.messages.create(
                model=self._model, max_tokens=512, temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = extract_text_content(resp.content)
            profile_data = self._parse_profile_json(raw)
            if profile_data is None:
                logger.warning(f"用户画像提取结果不是 JSON，保留旧画像: {user_id}")
                return

            profile_data = self._normalize_profile(profile_data)
            if not profile_data["preferences"] and not profile_data["entities"]:
                logger.info(f"用户画像没有有效偏好/实体，跳过写入: {user_id}")
                return

            doc_id = self._profile_doc_id(user_id)
            doc_text = self._safe_text(json.dumps(profile_data, ensure_ascii=False))
            await asyncio.to_thread(
                self._profile.upsert,
                ids=[doc_id],
                documents=[doc_text],
                metadatas=[{
                    "user_id": user_id,
                    "conv_id": conv_id,
                    "ts": datetime.now().isoformat(),
                    "schema_version": 2,
                }],
            )
            logger.info(f"用户画像已更新: {user_id}")
        except Exception as ex:
            logger.warning(f"更新用户画像失败: {ex}")

    @classmethod
    def _build_profile_prompt(cls, dialogue: str, existing: Dict[str, Any]) -> str:
        """构造“已有画像 + 新增对话”的合并更新提示词。"""
        existing_text = json.dumps(existing, ensure_ascii=False) if existing else "（暂无）"
        return f"""你负责维护客服系统中的用户长期画像。请结合已有画像和新增对话，输出更新后的画像 JSON。

更新规则：
1. 只保留长期有效、可复用的偏好和关键实体；忽略寒暄、一次性操作和与客服无关的信息。
2. 已有画像中没有被新对话否定的信息应保留；新对话与旧画像冲突时，以新对话为准。
3. 不要编造对话中没有出现的信息。
4. preferences 是最多 {cls.PROFILE_MAX_PREFERENCES} 条简洁字符串。
5. entities 是对象，值为字符串数组；最多 {cls.PROFILE_MAX_ENTITY_KEYS} 个键，每个键最多 {cls.PROFILE_MAX_ENTITY_VALUES} 条。

已有画像:
{existing_text}

新增对话:
{dialogue}

只返回一个 JSON 对象，不要输出 Markdown 或额外解释。返回格式:
{{"preferences": ["..."], "entities": {{"产品": [], "问题类型": []}}}}"""

    @classmethod
    def _parse_profile_json(cls, raw: Any) -> Optional[Dict[str, Any]]:
        """从 LLM 输出中稳健地提取 JSON 对象。"""
        text = cls._safe_text(raw).strip()
        if not text:
            return None

        candidates = [text]
        if "```" in text:
            for index, fragment in enumerate(text.split("```")):
                if index % 2 == 1:
                    fragment = fragment.strip()
                    if fragment.lower().startswith("json"):
                        fragment = fragment[4:]
                    candidates.append(fragment.strip())
        start, end = text.find("{"), text.rfind("}") + 1
        if 0 <= start < end:
            candidates.append(text[start:end])

        for candidate in candidates:
            if not candidate:
                continue
            try:
                value = json.loads(candidate)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if isinstance(value, dict):
                return value
        return None

    @classmethod
    def _normalize_profile(cls, data: Any) -> Dict[str, Any]:
        """统一画像结构：只保留 preferences 和 entities，并去重、去空、限长。"""
        if not isinstance(data, dict):
            return {"preferences": [], "entities": {}}

        preferences = cls._clean_string_list(
            data.get("preferences"), cls.PROFILE_MAX_PREFERENCES
        )
        entities: Dict[str, List[str]] = {}
        raw_entities = data.get("entities")
        if isinstance(raw_entities, dict):
            for raw_key, raw_values in raw_entities.items():
                key = cls._safe_text(raw_key).strip()
                if not key:
                    continue
                values = cls._clean_string_list(raw_values, cls.PROFILE_MAX_ENTITY_VALUES)
                if values:
                    entities[key] = values
                if len(entities) >= cls.PROFILE_MAX_ENTITY_KEYS:
                    break

        return {"preferences": preferences, "entities": entities}

    @classmethod
    def _clean_string_list(cls, value: Any, limit: int) -> List[str]:
        """把可能是字符串/数组的画像字段清洗成去重后的字符串数组。"""
        if isinstance(value, str):
            candidates: List[Any] = [value]
        elif isinstance(value, (list, tuple, set)):
            candidates = list(value)
        else:
            candidates = []

        cleaned: List[str] = []
        seen = set()
        for item in candidates:
            if isinstance(item, bool) or not isinstance(item, (str, int, float)):
                continue
            text = cls._safe_text(item).strip()[:cls.PROFILE_MAX_ITEM_CHARS].strip()
            if not text or text in seen:
                continue
            seen.add(text)
            cleaned.append(text)
            if len(cleaned) >= limit:
                break
        return cleaned

    @staticmethod
    def _profile_doc_id(user_id: str) -> str:
        """用户级画像文档 ID，避免同一用户多个会话各自覆盖。"""
        return f"{user_id}_profile"

    # ── 读取 ──────────────────────────────────────────────────────────────────

    async def get_context(self, user_id: str, conv_id: str, query: str = "") -> MemoryContext:
        """
        构建完整的记忆上下文。

        query 用于从情景记忆中检索语义相关的历史片段。
        """
        # 新库是权威读取路径；anonymous 仅保留当前会话原文，不做跨会话事实检索。
        if self._repo is not None:
            snapshot = await self.get_snapshot(user_id, conv_id)
            recent = [Message(
                role=MsgRole(item["role"]), content=self._safe_text(item.get("content", "")),
                metadata={"source_seq": item.get("source_seq"), "turn_seq": item.get("turn_seq")},
            ) for item in snapshot.recent_messages]
            if user_id in {"anonymous", "demo", ""}:
                snapshot.facts = []
                snapshot.episodes = []
            rendered = self.snapshot_to_dict(snapshot)
            return MemoryContext(
                recent_messages=recent,
                relevant_history=rendered.get("relevant_history", []),
                user_profile=rendered.get("user_profile", {}),
                summary=json.dumps(snapshot.summary, ensure_ascii=False) if isinstance(snapshot.summary, dict) else self._safe_text(snapshot.summary),
            )

        # 1. 工作记忆（当前会话最近消息）
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        query = self._safe_text(query)

        recent = await self._get_working_memory(user_id, conv_id)

        # 2. 情景记忆（跨会话语义检索）
        history = await self._search_episodic(user_id, query or (recent[-1].content if recent else ""))

        # 3. 用户画像
        profile = await self._get_profile(user_id)

        # 4. 会话摘要（如果已压缩过）
        summary = await self._redis.get(self._summary_key(user_id, conv_id)) or ""

        return MemoryContext(
            recent_messages=recent,
            relevant_history=history,
            user_profile=profile,
            summary=summary,
        )

    # ── 压缩（防止 context 爆炸）─────────────────────────────────────────────

    async def _compress(self, user_id: str, conv_id: str) -> None:
        """
        工作记忆压缩：
          1. 用 LLM 对旧消息生成摘要
          2. 摘要存 Redis（覆盖旧摘要）
          3. 旧消息存入情景记忆（ChromaDB）供跨会话检索
          4. 工作记忆只保留最近 5 条
        """
        messages = await self._get_working_memory(user_id, conv_id)
        if len(messages) < self.COMPRESS_AT:
            return

        to_compress = messages[:-5]   # 保留最近 5 条
        keep        = messages[-5:]
        # 不在 user/assistant 轮次中间切断：若切点落在 user 后，
        # 将该 user 留在工作区，等待对应 assistant 一起提交。
        if to_compress and to_compress[-1].role is MsgRole.USER:
            keep = messages[-6:]
            to_compress = messages[:-6]
        if not to_compress:
            return

        # LLM 摘要
        text = self._safe_text("\n".join(f"{m.role.value}: {m.content}" for m in to_compress))
        old_summary = await self._redis.get(self._summary_key(user_id, conv_id)) or ""
        prompt = self._safe_text("请重写一个不超过 4000 字的结构化会话摘要，保留当前目标、约束、进展和待确认问题。旧摘要：\n" + old_summary + "\n新增完整历史：\n" + text)
        try:
            resp = await self._client.messages.create(
                model=self._model, max_tokens=256, temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            summary = self._safe_text(extract_text_content(resp.content)).strip()
        except Exception as ex:
            logger.warning("摘要生成失败，保留原文和旧摘要: %s", ex)
            return
        if not summary or len(summary) > 4000:
            logger.warning("摘要为空或超出预算，保留原文")
            return

        # 存摘要到 Redis
        skey = self._summary_key(user_id, conv_id)
        await self._redis.setex(skey, 86400, summary)

        # 旧消息存入情景记忆
        await self._store_episodic(user_id, conv_id, text, summary)

        # 重置工作记忆为最近 5 条
        key = self._wm_key(user_id, conv_id)
        if hasattr(self._redis, "pipeline"):
            pipe = self._redis.pipeline(transaction=True)
            pipe.delete(key)
            # lpush chronological keep，读取时 reversed 后仍为旧到新。
            for m in keep:
                pipe.lpush(key, json.dumps({
                    "role": m.role.value, "content": m.content,
                    "ts": m.timestamp.isoformat(), "metadata": m.metadata,
                }))
            pipe.expire(key, 86400)
            await pipe.execute()
        else:
            await self._redis.delete(key)
            for m in keep:
                await self._redis.lpush(key, json.dumps({
                    "role": m.role.value, "content": m.content,
                    "ts": m.timestamp.isoformat(), "metadata": m.metadata,
                }))
            await self._redis.expire(key, 86400)
        logger.info(f"工作记忆压缩完成: {user_id}/{conv_id}，摘要 {len(summary)} 字")

    # ── 内部辅助 ──────────────────────────────────────────────────────────────

    async def _get_working_memory(self, user_id: str, conv_id: str) -> List[Message]:
        key  = self._wm_key(user_id, conv_id)
        raws = await self._redis.lrange(key, 0, self.WORKING_MAX - 1)
        msgs = []
        for raw in reversed(raws):  # Redis lpush 最新在前，reversed 还原时序
            d = json.loads(raw)
            msgs.append(Message(
                role=MsgRole(d["role"]),
                content=d["content"],
                timestamp=datetime.fromisoformat(d["ts"]),
                metadata=d.get("metadata", {}),
            ))
        return msgs

    async def _search_episodic(self, user_id: str, query: str) -> List[str]:
        """语义检索情景记忆。ChromaDB 内置 embedding，不依赖外部 API。"""
        query_text = self._safe_text(query).strip()
        if not query_text:
            return []
        try:
            # 直接传 query_texts，ChromaDB 内置模型自动生成向量做匹配
            results = await asyncio.to_thread(
                self._episodic.query,
                query_texts=[query_text],
                n_results=self.HISTORY_TOP_K,
                where={"user_id": self._safe_text(user_id), "namespace": getattr(self, "namespace", "lumenmind")},
            )
            docs = results["documents"][0] if results["documents"] else []
            return [self._safe_text(doc) for doc in docs if isinstance(doc, str) and doc.strip()]
        except Exception as ex:
            logger.warning(f"情景记忆检索失败: {ex}")
            return []

    async def _store_episodic(self, user_id: str, conv_id: str, text: str, summary: str) -> None:
        """将压缩后的对话片段存入情景记忆。ChromaDB 内置 embedding，不依赖外部 API。"""
        try:
            user_id = self._safe_text(user_id)
            conv_id = self._safe_text(conv_id)
            text = self._safe_text(text)
            summary = self._safe_text(summary)
            doc_id = hashlib.md5(f"{user_id}{conv_id}{time.time()}".encode()).hexdigest()
            # 直接传 documents，ChromaDB 内置模型自动生成 embedding
            await asyncio.to_thread(
                self._episodic.add,
                ids=[doc_id],
                documents=[summary],
                metadatas=[{"user_id": user_id, "namespace": getattr(self, "namespace", "lumenmind"), "conv_id": conv_id,
                            "ts": datetime.now().isoformat(), "full_text": self._safe_text(text[:500])}],
            )
        except Exception as ex:
            logger.warning(f"存储情景记忆失败: {ex}")

    async def _get_profile(self, user_id: str) -> Dict[str, Any]:
        """
        读取用户级画像。

        优先读取 v2 的单用户文档；若尚未写入，则兼容旧版按会话写入的记录，
        按 metadata.ts 选择最新一条，保证灰度升级期间仍能读到画像。
        """
        user_id = self._safe_text(user_id)
        try:
            results = await asyncio.to_thread(
                self._profile.get, ids=[self._profile_doc_id(user_id)]
            )
            for document in results.get("documents") or []:
                parsed = self._parse_profile_json(document)
                if parsed is not None:
                    return self._normalize_profile(parsed)

            # 兼容旧版：每个 conv_id 一个文档，没有排序时 limit=1 不代表最新。
            legacy = await asyncio.to_thread(self._profile.get, where={"user_id": user_id})
            documents = legacy.get("documents") or []
            metadatas = legacy.get("metadatas") or []
            candidates: List[tuple] = []
            for index, document in enumerate(documents):
                parsed = self._parse_profile_json(document)
                if parsed is None:
                    continue
                metadata = metadatas[index] if index < len(metadatas) and isinstance(metadatas[index], dict) else {}
                timestamp = self._safe_text(metadata.get("ts", ""))
                candidates.append((timestamp, parsed))
            if not candidates:
                return {}
            candidates.sort(key=lambda item: item[0], reverse=True)
            return self._normalize_profile(candidates[0][1])
        except Exception as ex:
            logger.warning(f"读取用户画像失败: {ex}")
            return {}

    async def close(self) -> None:
        """Close the configured authority and any optional legacy clients."""
        if self._redis is not None:
            await self._redis.aclose()
        if self._client is not None:
            await self._client.close()
        if self._repo is not None:
            self._repo.close()

    def _scope_cache_key(self, kind: str, user_id: str, conv_id: str) -> str:
        # 规范化 JSON 后摘要，避免 user_id/conv_id 中的分隔符造成碰撞。
        encoded = json.dumps([getattr(self, "namespace", "lumenmind"), self._safe_text(user_id), self._safe_text(conv_id)], ensure_ascii=False, separators=(",", ":"))
        return f"{kind}:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"

    def _wm_key(self, user_id: str, conv_id: str) -> str:
        return self._scope_cache_key("wm", user_id, conv_id)

    def _summary_key(self, user_id: str, conv_id: str) -> str:
        return self._scope_cache_key("summary", user_id, conv_id)

    @staticmethod
    def _safe_text(value: Any) -> str:
        """转成 ChromaDB 可接受的普通 UTF-8 字符串。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")

    @classmethod
    def _safe_metadata_value(cls, value: Any) -> Any:
        """递归清洗 metadata，避免 Redis/ChromaDB 后续读写遇到非法 UTF-8。"""
        if isinstance(value, str):
            return cls._safe_text(value)
        if isinstance(value, dict):
            return {cls._safe_text(k): cls._safe_metadata_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._safe_metadata_value(v) for v in value]
        return value
