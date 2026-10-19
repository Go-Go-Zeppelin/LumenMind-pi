"""持久记忆层的轻量数据模型。

这些模型只描述 HTTP/存储协议，不携带模型调用逻辑，便于在离线测试中
使用 fake judge/summary 组件验证事务语义。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class MemoryScope:
    namespace: str
    user_id: str


@dataclass
class TurnReceipt:
    request_id: str
    namespace: str
    user_id: str
    conv_id: str
    turn_seq: int
    source_seq: int
    status: str
    lease_token: str
    context_revision: int
    memory_status: str = "stored"
    response: Optional[str] = None
    lease_until: Optional[str] = None


@dataclass
class MemorySnapshot:
    schema_version: int
    status: str
    namespace: str
    user_id: str
    conv_id: str
    fact_revision: int
    context_revision: int
    erase_epoch: int
    summary_generation: int
    summary: Dict[str, Any] | str = field(default_factory=dict)
    facts: List[Dict[str, Any]] = field(default_factory=list)
    episodes: List[Dict[str, Any]] = field(default_factory=list)
    recent_messages: List[Dict[str, Any]] = field(default_factory=list)
    routing_context: str = ""
    prompt_text: str = ""
    context_truncated: bool = False

