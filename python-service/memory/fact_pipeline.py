"""长期事实 Judge 协议的纯校验部分。

模型只提出差异，服务端验证 schema、来源和目标归属后才允许 repository
提交。这样助手回复、摘要和工具结果无法成为事实抽取素材。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Set

ALLOWED_ACTIONS = {"ADD", "SUPERSEDE", "RETRACT", "CLEAR", "NOOP"}
ALLOWED_CATEGORIES = {"preference", "stable_context"}
ALLOWED_KEYS = {"contact.channel", "response.detail", "device.os"}


class FactDecisionError(ValueError):
    pass


def validate_decisions(payload: Any, user_source: Dict[int, str], active_fact_ids: Set[str]) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or set(payload) != {"schema_version", "decisions"}:
        raise FactDecisionError("事实决策 schema 不匹配")
    decisions = payload["decisions"]
    if not isinstance(decisions, list): raise FactDecisionError("decisions 必须是数组")
    if not decisions: return []
    if any(not isinstance(item, dict) for item in decisions): raise FactDecisionError("决策项必须是对象")
    if any(set(item) - {"action", "target_fact_id", "category", "fact_key", "content", "source_seqs", "evidence"} for item in decisions):
        raise FactDecisionError("决策包含未允许字段")
    if any(item.get("action") == "NOOP" for item in decisions) and len(decisions) != 1:
        raise FactDecisionError("NOOP 不能和变更动作混用")
    for item in decisions:
        action = item.get("action")
        if action not in ALLOWED_ACTIONS: raise FactDecisionError("未知事实动作")
        if action in {"ADD", "SUPERSEDE"}:
            if item.get("category") not in ALLOWED_CATEGORIES or not isinstance(item.get("content"), str) or not item["content"].strip() or len(item["content"]) > 300:
                raise FactDecisionError("事实类别或正文不合法")
            if item.get("fact_key") is not None and item.get("fact_key") not in ALLOWED_KEYS:
                raise FactDecisionError("事实键不在允许集合")
        if action in {"SUPERSEDE", "RETRACT"} and item.get("target_fact_id") not in active_fact_ids:
            raise FactDecisionError("目标事实不属于当前 scope 或已失效")
        if action in {"ADD", "SUPERSEDE", "RETRACT", "CLEAR"}:
            seqs = item.get("source_seqs")
            evidence = item.get("evidence")
            if not isinstance(seqs, list) or not seqs or any(not isinstance(x, int) or x not in user_source for x in seqs):
                raise FactDecisionError("事实来源必须来自本批 user 消息")
            if not isinstance(evidence, str) or not evidence.strip() or not any(evidence.strip() in user_source[x] for x in seqs):
                raise FactDecisionError("证据片段无法对应 user 原文")
    return decisions

