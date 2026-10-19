"""用户画像提取的离线契约测试。

测试不启动 Redis/ChromaDB，也不调用真实 Anthropic API，重点验证：
- LLM 输出解析的健壮性；
- 画像字段归一化、去重和限长；
- 用户级单文档的合并更新与 upsert；
- 旧版按会话写入画像的兼容读取。
"""

import asyncio
import json
import sys
import types
from typing import Any, Dict, List, Optional

try:  # pragma: no cover - 完整依赖环境走真实包，CI 最小依赖走下面的 stub
    import chromadb  # noqa: F401
except ImportError:  # pragma: no cover
    chromadb = types.ModuleType("chromadb")
    chromadb.Settings = type("Settings", (), {})
    sys.modules["chromadb"] = chromadb

try:  # pragma: no cover
    import redis.asyncio  # noqa: F401
except ImportError:  # pragma: no cover
    if "redis" not in sys.modules:
        sys.modules["redis"] = types.ModuleType("redis")
    redis_async = types.ModuleType("redis.asyncio")
    redis_async.from_url = lambda *args, **kwargs: None
    sys.modules["redis.asyncio"] = redis_async
    sys.modules["redis"].asyncio = redis_async  # type: ignore[attr-defined]

from memory.conversation_memory import MemoryManager


def raw_message(role: str, content: str) -> str:
    return json.dumps({
        "role": role,
        "content": content,
        "ts": "2026-01-01T00:00:00",
        "metadata": {},
    })


class FakeRedis:
    """最小 Redis 工作记忆：只实现 update_profile 会调用的 lrange。"""

    def __init__(self, messages: Optional[List[str]] = None):
        # Redis lpush 语义：最新在前
        self.messages = messages or []

    async def lrange(self, _key: str, _start: int, _end: int) -> List[str]:
        return self.messages


class FakeMessages:
    def __init__(self, output: str):
        self.output = output
        self.calls: List[Dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text=self.output)]
        )


class FakeAnthropic:
    def __init__(self, output: str):
        self.messages = FakeMessages(output)


class FakeProfileCollection:
    """用 dict 模拟 ChromaDB collection，支持 get(ids=...)/get(where=...) 和 upsert。"""

    def __init__(self, records: Optional[Dict[str, Any]] = None):
        self.records = records or {}
        self.upsert_calls: List[Dict[str, Any]] = []
        self.get_calls: List[Dict[str, Any]] = []

    def get(self, **kwargs: Any) -> Dict[str, Any]:
        self.get_calls.append(kwargs)
        ids = kwargs.get("ids")
        if ids is not None:
            selected = [(item_id, self.records[item_id]) for item_id in ids if item_id in self.records]
        else:
            where = kwargs.get("where") or {}
            selected = [
                (item_id, record)
                for item_id, record in self.records.items()
                if all(record[1].get(key) == value for key, value in where.items())
            ]
        return {
            "ids": [item_id for item_id, _ in selected],
            "documents": [record[0] for _, record in selected],
            "metadatas": [record[1] for _, record in selected],
        }

    def upsert(self, **kwargs: Any) -> None:
        self.upsert_calls.append(kwargs)
        for item_id, document, metadata in zip(
            kwargs["ids"], kwargs["documents"], kwargs["metadatas"]
        ):
            self.records[item_id] = (document, metadata)


def make_manager(
    messages: Optional[List[str]] = None,
    llm_output: str = '{"preferences": ["喜欢邮件通知"], "entities": {"产品": ["无线键盘"]}}',
    records: Optional[Dict[str, Any]] = None,
) -> MemoryManager:
    manager = MemoryManager.__new__(MemoryManager)
    manager._model = "test-model"  # type: ignore[attr-defined]
    manager._redis = FakeRedis(messages)  # type: ignore[attr-defined]
    manager._client = FakeAnthropic(llm_output)  # type: ignore[attr-defined]
    manager._profile = FakeProfileCollection(records)  # type: ignore[attr-defined]
    return manager


def test_parse_profile_json_accepts_plain_fence_and_prose():
    parse = MemoryManager._parse_profile_json

    assert parse('{"preferences": ["标准 JSON"], "entities": {}}')["preferences"] == ["标准 JSON"]
    assert parse('```json\n{"preferences": ["代码块"]}\n```')["preferences"] == ["代码块"]
    assert parse('结果如下：{"preferences": ["带前后缀"]}，请查收。')["preferences"] == ["带前后缀"]
    assert parse("抱歉，我无法返回 JSON") is None
    assert parse(None) is None


def test_normalize_profile_cleans_and_caps():
    normalized = MemoryManager._normalize_profile({
        "preferences": [" 喜欢邮件 ", "喜欢邮件", 42, "", None, "x" * 200],
        "entities": {
            "产品": ["键盘", "键盘", ""],
            "问题类型": "退款",
            "空": [],
        },
        "ignored": "不会进入画像",
    })

    assert normalized["preferences"] == ["喜欢邮件", "42", "x" * MemoryManager.PROFILE_MAX_ITEM_CHARS]
    assert normalized["entities"] == {"产品": ["键盘"], "问题类型": ["退款"]}
    assert "ignored" not in normalized


def test_update_profile_merges_existing_profile_and_upserts_single_user_doc():
    legacy = json.dumps({"preferences": ["偏好旧"], "entities": {"产品": ["鼠标"]}})
    manager = make_manager(
        messages=[
            raw_message("assistant", "好的，已经记录"),
            raw_message("user", "以后尽量晚上联系我，不要在上午打电话"),
        ],
        llm_output='{"preferences": ["偏好旧", "偏好新"], "entities": {"产品": ["无线键盘"]}}',
        records={"u1_profile_c1": (legacy, {"user_id": "u1", "conv_id": "c1", "ts": "2026-01-01T00:00:00"})},
    )

    asyncio.run(manager.update_profile("u1", "c1"))

    assert len(manager._profile.upsert_calls) == 1  # type: ignore[attr-defined]
    call = manager._profile.upsert_calls[0]  # type: ignore[attr-defined]
    assert call["ids"] == ["u1_profile"]
    assert call["metadatas"][0]["user_id"] == "u1"
    assert call["metadatas"][0]["conv_id"] == "c1"

    stored = json.loads(call["documents"][0])
    assert stored["preferences"] == ["偏好旧", "偏好新"]
    assert stored["entities"] == {"产品": ["无线键盘"]}

    prompt = manager._client.messages.calls[0]["messages"][0]["content"]  # type: ignore[attr-defined]
    assert "偏好旧" in prompt
    assert "以后尽量晚上联系我" in prompt


def test_update_profile_keeps_old_profile_when_llm_output_is_not_json():
    old = json.dumps({"preferences": ["旧偏好"], "entities": {}})
    manager = make_manager(
        messages=[raw_message("user", "你好")],
        llm_output="抱歉，我无法返回 JSON",
        records={"u1_profile_c1": (old, {"user_id": "u1", "conv_id": "c1", "ts": "2026-01-01T00:00:00"})},
    )

    asyncio.run(manager.update_profile("u1", "c1"))

    assert manager._profile.upsert_calls == []  # type: ignore[attr-defined]
    assert manager._profile.records["u1_profile_c1"][0] == old  # type: ignore[attr-defined]


def test_update_profile_skips_empty_normalized_profile():
    manager = make_manager(
        messages=[raw_message("user", "你好")],
        llm_output='{"preferences": [], "entities": {}}',
    )

    asyncio.run(manager.update_profile("u1", "c1"))

    assert manager._profile.upsert_calls == []  # type: ignore[attr-defined]


def test_get_profile_prefers_user_level_document_over_legacy_conversations():
    aggregate = json.dumps({"preferences": ["聚合画像"], "entities": {}})
    newer_legacy = json.dumps({"preferences": ["旧会话画像"], "entities": {}})
    manager = make_manager(records={
        "u1_profile": (aggregate, {"user_id": "u1", "conv_id": "__aggregate__", "ts": "2026-01-01T00:00:00"}),
        "u1_profile_c2": (newer_legacy, {"user_id": "u1", "conv_id": "c2", "ts": "2026-02-01T00:00:00"}),
    })

    profile = asyncio.run(manager._get_profile("u1"))

    assert profile["preferences"] == ["聚合画像"]


def test_get_profile_falls_back_to_latest_legacy_record():
    older = json.dumps({"preferences": ["旧会话"], "entities": {}})
    newer = json.dumps({"preferences": ["新会话"], "entities": {}})
    manager = make_manager(records={
        "u1_profile_c1": (older, {"user_id": "u1", "conv_id": "c1", "ts": "2026-01-01T00:00:00"}),
        "u1_profile_c2": (newer, {"user_id": "u1", "conv_id": "c2", "ts": "2026-02-01T00:00:00"}),
    })

    profile = asyncio.run(manager._get_profile("u1"))

    assert profile["preferences"] == ["新会话"]
