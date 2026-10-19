"""Run a validated suite through /eval/run, or validate offline with --dry-run."""

import argparse
import asyncio
import json
from pathlib import Path

import httpx

from evaluation.contracts import EvalRunInput


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path(__file__).resolve().parents[2] / "data/eval/golden_set_v1.json")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--rules-only", action="store_true")
    parser.add_argument("--case-id", help="Run one dialog case (intent cases excluded)")
    parser.add_argument("--dry-run", action="store_true", help="Validate without contacting services")
    parser.add_argument("--output", type=Path, help="Optional local report copy")
    args = parser.parse_args()
    payload = json.loads(args.dataset.read_text(encoding="utf-8"))
    if args.case_id:
        payload["intent_cases"] = []
        payload["dialog_cases"] = [case for case in payload.get("dialog_cases", []) if case.get("case_id") == args.case_id]
        if not payload["dialog_cases"]:
            parser.error("case-id 不存在")
    payload.update(repeats=args.repeats, judge_mode="rules" if args.rules_only else "llm",
                   record_metrics=False, persist_report=True)
    validated = EvalRunInput.model_validate(payload)
    case_ids = [case.case_id for case in validated.dialog_cases or []]
    if len(set(case_ids)) != len(case_ids):
        parser.error("dataset 中 case_id 重复或缺失")
    if args.dry_run:
        print(json.dumps({"dataset_version": validated.dataset_version,
                          "intent_cases": len(validated.intent_cases or []),
                          "dialog_cases": len(validated.dialog_cases or []),
                          "repeats": validated.repeats, "judge_mode": validated.judge_mode}, ensure_ascii=False, indent=2))
        return 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(3600.0, connect=10.0)) as client:
        response = await client.post(f"{args.url.rstrip('/')}/eval/run", json=validated.model_dump(exclude_none=True))
        response.raise_for_status()
        report = response.json()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ("run_id", "pass_rate", "stability", "release_gate", "artifact_path")}, ensure_ascii=False, indent=2))
    # Review and blocked both stop an unattended release. This does not deploy anything.
    return 0 if report.get("release_gate", {}).get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
