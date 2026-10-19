"""Deterministic trace scoring, evidence grouping, and conservative release checks."""

import hashlib
import json
import math
from collections import defaultdict
from typing import Any, Dict, List

SCORER_VERSION = "agent-quality-v1"


def fingerprint(intent_cases, dialog_cases) -> str:
    payload = {"intents": intent_cases, "dialogs": dialog_cases}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def check_process(response: Dict[str, Any], trace: Dict[str, Any], expected: Dict[str, Any]) -> List[str]:
    failures = []
    if not trace:
        failures.append("trace_missing")
    if response.get("output_valid") is False:
        failures.append("output_contract_invalid")
    calls = trace.get("tool_calls", [])
    successful = {call.get("tool_name") for call in calls if call.get("success") and not call.get("is_error")}
    observed = {call.get("tool_name") for call in calls}
    for name in expected.get("required_tools", []):
        if name not in successful:
            failures.append(f"required_tool_missing:{name}")
    for name in expected.get("forbidden_tools", []):
        if name in observed:
            failures.append(f"forbidden_tool_used:{name}")
    for name, fields in expected.get("tool_inputs", {}).items():
        if not any(call.get("tool_name") == name and call.get("success") and not call.get("is_error")
                   and all(call.get("input", {}).get(key) == value for key, value in fields.items()) for call in calls):
            failures.append(f"tool_input_mismatch:{name}")
    order = expected.get("tool_order", [])
    if order:
        # Completion order is unreliable when tools run concurrently. Require start timestamps.
        if any(not call.get("started_at") for call in calls if call.get("tool_name") in order):
            failures.append("tool_order_unobservable")
        else:
            relevant = [call for call in sorted(calls, key=lambda call: call.get("started_at", ""))
                        if call.get("tool_name") in order]
            stages = [order.index(call["tool_name"]) for call in relevant]
            if set(stages) != set(range(len(order))) or stages != sorted(stages):
                failures.append("tool_order_mismatch")
            else:
                for previous, following in zip(order, order[1:]):
                    left = [call for call in relevant if call["tool_name"] == previous]
                    right = [call for call in relevant if call["tool_name"] == following]
                    if any(not call.get("finished_at") for call in left):
                        failures.append("tool_order_unobservable")
                        break
                    if max(call["finished_at"] for call in left) > min(call["started_at"] for call in right):
                        failures.append("tool_order_overlap")
                        break
    skills = set(trace.get("skills_used", []))
    for name in expected.get("required_skills", []):
        if name not in skills:
            failures.append(f"required_skill_missing:{name}")
    for name in expected.get("forbidden_skills", []):
        if name in skills:
            failures.append(f"forbidden_skill_used:{name}")
    for key, actual in (("expected_agent", response.get("agent_type")),
                        ("expected_escalated", response.get("escalated")),
                        ("require_knowledge", response.get("knowledge_used"))):
        if expected.get(key) is not None and expected[key] != actual:
            failures.append(f"{key}_mismatch")
    if not expected.get("allow_fallback", True) and (trace.get("fallback_used") or any(call.get("fallback_used") for call in calls)):
        failures.append("fallback_not_allowed")
    if expected.get("max_latency_ms") is not None:
        latency = response.get("latency_ms")
        if not isinstance(latency, (int, float)) or latency > expected["max_latency_ms"]:
            failures.append("latency_budget_exceeded")
    return failures


def wilson_interval(successes: int, total: int) -> List[float]:
    if not total:
        return [0.0, 1.0]
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return [round(max(0, center - margin), 4), round(min(1, center + margin), 4)]


def summarize_stability(sessions: List[Dict[str, Any]], repeats: int) -> Dict[str, Any]:
    grouped = defaultdict(list)
    for session in sessions:
        grouped[session["case_id"]].append(session)
    any_count = sum(any(row["passed"] for row in rows) for rows in grouped.values())
    all_count = sum(len(rows) == repeats and all(row["passed"] for row in rows) for rows in grouped.values())
    total = len(grouped)
    return {
        "case_total": total, "repeats": repeats,
        "at_least_once_success_rate": any_count / total if total else 0.0,
        "all_runs_success_rate": all_count / total if total else 0.0,
        "session_success_rate": sum(row["passed"] for row in sessions) / len(sessions) if sessions else 0.0,
        "all_runs_success_interval_95": wilson_interval(all_count, total),
        "interval_unit": "case (fixed evaluation set; not a production guarantee)",
        "cases": [{"case_id": key, "successes": sum(row["passed"] for row in rows), "runs": len(rows)}
                  for key, rows in grouped.items()],
    }


DIAGNOSTICS = {
    "intent_mismatch": ("意图错误", ["intent_router", "memory_context"], "核对标注与上下文，再调整意图规则或路由示例", "algorithm"),
    "required_tool_missing": ("关键工具未成功调用", ["agent_executor", "skill_workflow"], "检查工具暴露与流程前置条件，并补充工具失败分支", "engineering"),
    "tool_input_mismatch": ("工具参数错误", ["entity_extraction", "agent_executor"], "对照 Trace 入参与用例期望修正参数构造", "engineering"),
    "required_skill_missing": ("Skill 未触发", ["skill_loader", "intent_router"], "检查角色限制、关键词和提示词预算", "engineering"),
    "trace_missing": ("执行证据缺失", ["trace_collection"], "检查请求 ID、Trace 接口与采集时序", "engineering"),
    "output_contract_invalid": ("输出契约失败", ["agent_executor"], "检查契约校验与修复回合日志", "engineering"),
    "execution_failed": ("运行失败", ["agent_service", "infrastructure"], "根据异常与工具返回确认故障环节", "engineering"),
    "judge_failed": ("裁判不可用", ["evaluation_judge"], "检查模型配置和返回格式后重新评测", "engineering"),
    "unverified_financial_promise": ("未经核验的资金承诺", ["response_generation", "billing_skill"], "核对业务边界，约束资金承诺并增加回归用例", "business"),
    "claimed_completed_operation": ("无依据的完成声明", ["response_generation", "skill_workflow"], "将声明与实际工具能力对齐；无写入工具时只能解释或转人工", "engineering"),
    "sensitive_information": ("敏感信息风险", ["response_generation", "guardrail"], "复核请求敏感凭证的上下文并修正安全约束", "engineering"),
    "outcome_not_met": ("会话未达成目标", ["dialog_strategy", "response_generation"], "对照整段会话目标修正澄清和推进策略", "algorithm"),
    "outcome_criteria_missing": ("会话验收标准缺失", ["evaluation_dataset"], "由业务明确会话目标，再补充最终结果规则或语义验收标准", "business"),
    "outcome_not_evaluated": ("会话目标未完成验收", ["evaluation_judge"], "开启语义评分或恢复裁判后重跑目标验收，不能用字面条件代替显式语义目标", "engineering"),
    "require_knowledge_mismatch": ("检索证据未满足要求", ["knowledge_search", "agent_executor"], "检查是否真实调用检索、是否只有降级结果及知识库可用性", "engineering"),
    "tool_order_mismatch": ("工具顺序偏离流程", ["agent_executor", "skill_workflow"], "将前置校验与后续操作的依赖关系写入流程，并验证调用时间", "engineering"),
    "tool_order_overlap": ("有依赖的工具被并行调用", ["agent_executor", "skill_workflow"], "要求依赖后续工具等待前置工具返回，再检查开始结束时间", "engineering"),
}


def cluster_findings(results, sessions) -> List[Dict[str, Any]]:
    grouped = {}
    rows = [(result.test_id, result.metadata) for result in results]
    rows += [(row["session_id"], row) for row in sessions if row.get("failures")]
    for test_id, metadata in rows:
        for failure in metadata.get("failures", []):
            kind = failure.split(":", 1)[0]
            phenomenon, modules, action, owner = DIAGNOSTICS.get(kind, (
                "验收条件未满足", ["response_generation", "skill_workflow"],
                "对照判定依据和执行轨迹复核，再确定责任模块", "engineering"))
            key = (failure, metadata.get("category", "general"))
            if key not in grouped:
                grouped[key] = {
                    "issue_id": f"issue-{len(grouped) + 1:03d}", "failure_type": failure,
                    "phenomenon": phenomenon, "category": key[1], "priority": metadata.get("priority", "P1"),
                    "candidate_modules": modules, "root_cause_status": "needs_evidence_review",
                    "owner_role": owner, "action": action,
                    "acceptance": "修复后重跑 evidence 中的用例及同场景回归集，验证过程和会话验收均通过",
                    "evidence": [],
                }
            issue = grouped[key]
            if metadata.get("priority") == "P0":
                issue["priority"] = "P0"
            issue["evidence"].append({"test_id": test_id, "case_id": metadata.get("case_id"),
                                      "request_id": metadata.get("request_id"), "reason": failure})
    for issue in grouped.values():
        issue["occurrence_count"] = len(issue["evidence"])
        issue["affected_case_count"] = len({row["case_id"] for row in issue["evidence"]})
    return list(grouped.values())


def release_gate(results, sessions, stability, baseline_comparison) -> Dict[str, Any]:
    blocked, review = [], []
    if not results:
        blocked.append("no_evaluation_data")
    if any(not result.passed and result.test_id == "intent_recognition" for result in results):
        blocked.append("intent_accuracy_below_threshold")
    if any(result.metadata.get("rule_violations") for result in results):
        blocked.append("business_rule_violation")
    if any(not row["passed"] and row["priority"] == "P0" for row in sessions):
        blocked.append("p0_session_failed")
    if sessions and stability["all_runs_success_rate"] < 0.9:
        blocked.append("all_runs_success_rate_below_0.9")
    if baseline_comparison.get("status") == "comparable" and baseline_comparison.get("regressions"):
        blocked.append("baseline_regression")
    if any(result.metadata.get("human_review") for result in results) or any(row.get("human_review") for row in sessions):
        review.append("manual_review_required")
    if any(result.metadata.get("judge_status") == "rules_only" for result in results):
        review.append("semantic_quality_not_evaluated")
    if sessions and stability["repeats"] < 3:
        review.append("fewer_than_3_repetitions")
    if baseline_comparison.get("status") != "comparable":
        review.append("no_comparable_baseline")
    return {"status": "blocked" if blocked else "review" if review else "passed",
            "blocked_reasons": blocked, "review_reasons": review,
            "regression_significance": "not_tested",
            "scope": "offline checks; requires online canary validation before production release"}
