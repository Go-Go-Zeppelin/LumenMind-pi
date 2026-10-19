"""长期事实 Judge 协议的纯校验部分。

模型只提出差异，服务端验证 schema、来源和目标归属后才允许 repository
提交。这样助手回复、摘要和工具结果无法成为事实抽取素材。
"""
from __future__ import annotations

from typing import Any, Dict, List, Set
import re

ALLOWED_ACTIONS = {"ADD", "SUPERSEDE", "RETRACT", "CLEAR", "NOOP"}
ALLOWED_CATEGORIES = {"preference", "stable_context"}
ALLOWED_KEYS = {"contact.channel", "response.detail", "device.os"}


_SENSITIVE_VALUE = re.compile(
    r"(?i)(?:password|passcode|api[ _-]?key|(?:access|refresh)[ _-]?token|private[ _-]?key|"
    r"密码|验证码|私钥|密钥|令牌)\s*(?:[:：=]|是|为|\bis\b)\s*[:：=]?\s*[^\s，。；,;]{3,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\bsk-[A-Za-z0-9_-]{12,}"
    r"|\bAKIA[A-Z0-9]{16}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bxox[baprs]-[A-Za-z0-9-]{12,}"
    r"|(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"
)


def contains_sensitive(text: str) -> bool:
    """Policy filter for credential values; never return or log matched text."""
    return bool(_SENSITIVE_VALUE.search(text))


class FactDecisionError(ValueError):
    pass


def validate_decisions(payload: Any, user_source: Dict[int, str], active_fact_ids: Set[str]) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload.get("schema_version") != 1 or set(payload) != {"schema_version", "decisions"}:
        raise FactDecisionError("事实决策 schema 不匹配")
    decisions = payload["decisions"]
    if not isinstance(decisions, list): raise FactDecisionError("decisions 必须是数组")
    if not decisions or len(decisions)>100: raise FactDecisionError("决策为空或超出批量限制")
    if any(not isinstance(item, dict) for item in decisions): raise FactDecisionError("决策项必须是对象")
    if any(set(item) - {"action", "target_fact_id", "category", "fact_key", "content", "source_seqs", "evidence"} for item in decisions):
        raise FactDecisionError("决策包含未允许字段")
    if any(item.get("action") == "NOOP" for item in decisions) and len(decisions) != 1:
        raise FactDecisionError("NOOP 不能和变更动作混用")
    if any(not isinstance(item.get("action"),str) for item in decisions):
        raise FactDecisionError("动作必须为字符串")
    targets = [item.get("target_fact_id") for item in decisions if item.get("action") in {"SUPERSEDE", "RETRACT"}]
    if any(not isinstance(target, str) for target in targets):
        raise FactDecisionError("目标事实 ID 必须为字符串")
    if len(targets) != len(set(targets)):
        raise FactDecisionError("同批重复操作目标事实")
    if any(item.get("action") == "CLEAR" for item in decisions):
        if sum(item.get("action") == "CLEAR" for item in decisions) != 1 or any(item.get("action") not in {"CLEAR", "ADD"} for item in decisions):
            raise FactDecisionError("CLEAR 仅可与 ADD 同批")
    required = {
        "NOOP": {"action"},
        "CLEAR": {"action", "source_seqs", "evidence"},
        "RETRACT": {"action", "target_fact_id", "source_seqs", "evidence"},
        "ADD": {"action", "category", "content", "source_seqs", "evidence"},
        "SUPERSEDE": {"action", "target_fact_id", "category", "content", "source_seqs", "evidence"},
    }
    for item in decisions:
        action = item.get("action")
        if action not in ALLOWED_ACTIONS: raise FactDecisionError("未知事实动作")
        allowed = required[action] | ({"fact_key"} if action in {"ADD", "SUPERSEDE"} else set())
        if not required[action].issubset(item) or not set(item).issubset(allowed):
            raise FactDecisionError("动作字段不完整或包含无关字段")
        if action in {"ADD", "SUPERSEDE"}:
            if not isinstance(item.get("category"),str) or item.get("category") not in ALLOWED_CATEGORIES or not isinstance(item.get("content"), str) or not item["content"].strip() or len(item["content"]) > 300:
                raise FactDecisionError("事实类别或正文不合法")
            if contains_sensitive(item["content"]):
                raise FactDecisionError("SENSITIVE_CONTENT")
            if item.get("fact_key") is not None and (not isinstance(item.get("fact_key"),str) or item.get("fact_key") not in ALLOWED_KEYS):
                raise FactDecisionError("事实键不在允许集合")
        if action in {"SUPERSEDE", "RETRACT"} and item.get("target_fact_id") not in active_fact_ids:
            raise FactDecisionError("目标事实不属于当前 scope 或已失效")
        if action in {"ADD", "SUPERSEDE", "RETRACT", "CLEAR"}:
            seqs = item.get("source_seqs")
            evidence = item.get("evidence")
            if not isinstance(seqs, list) or not seqs or any(type(x) is not int or x not in user_source for x in seqs):
                raise FactDecisionError("事实来源必须来自本批 user 消息")
            if isinstance(evidence,str) and contains_sensitive(evidence):
                raise FactDecisionError("SENSITIVE_CONTENT")
            if not isinstance(evidence, str) or not evidence.strip() or not all(evidence.strip() in user_source[x] for x in seqs):
                raise FactDecisionError("证据片段无法对应 user 原文")
    return decisions

