"""滚动摘要候选的边界校验。模型调用由上层注入。"""
from __future__ import annotations

import json
from typing import Any, Dict


SUMMARY_FIELDS = ("user_goal", "constraints", "confirmed_facts", "progress", "open_questions", "next_step")


def validate_summary(value: Any, max_chars: int = 4_000) -> Dict[str, Any]:
    if isinstance(value, str):
        try: value = json.loads(value)
        except json.JSONDecodeError: raise ValueError("摘要不是合法 JSON")
    if not isinstance(value, dict) or set(value) != set(SUMMARY_FIELDS):
        raise ValueError("摘要字段不完整或包含额外字段")
    if any(not isinstance(value[field], (str, list)) for field in SUMMARY_FIELDS):
        raise ValueError("摘要字段类型不合法")
    rendered = json.dumps(value, ensure_ascii=False)
    if not rendered.strip() or len(rendered) > max_chars: raise ValueError("摘要为空或超出字符预算")
    return value

