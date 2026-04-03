"""
cdp_gate_report.py — CDP v1 Gate Report (p169)

Runs the full CDP v1 chain (p170–p174 logic ported to Python) against a
results directory and answers 4 quantitative questions:

  Q1  Coverage:      declaration rate + CDP chain admission rate
  Q2  Rejection:     top failure codes from p171/p172/p173 chain
  Q3  Precision:     sample table for human spot-check (reject + admit)
  Q4  Behavioral Δ: attempts, invalid_output, retry_trigger rates

Usage:
  python3 scripts/cdp_gate_report.py --results results/p169-taxonomy-20260402 \\
      [--baseline results/baseline-batch] \\
      [--output results/cdp-report-20260402.json] \\
      [--sample 15]

Output:
  Prints the report to stdout.
  Writes JSON to --output if specified.
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from declaration_extractor import extract_declaration, extract_last_agent_message
from patch_signals import extract_patch_signals


# ── CDP v1 taxonomy port (p170) ───────────────────────────────────────────────
# Mirrors taxonomy.ts: type → {required_principals, forbidden_principals, subtypes}

CDP_TYPES = {
    "understanding": {
        "subtypes": ["codebase_reading", "dependency_analysis", "context_gathering"],
        "required": [],
        "forbidden": ["scope_control", "minimal_change", "execution_first"],
    },
    "observation": {
        "subtypes": ["log_reading", "test_output_analysis", "error_parsing", "state_inspection"],
        "required": ["evidence_based", "no_hallucination"],
        "forbidden": ["scope_control", "minimal_change", "execution_first"],
    },
    "analysis": {
        "subtypes": ["root_cause_analysis", "impact_analysis", "pattern_recognition"],
        "required": ["causality"],
        "forbidden": ["scope_control", "minimal_change", "execution_first"],
    },
    "diagnosis": {
        "subtypes": ["bug_localization", "regression_identification", "failure_classification"],
        "required": ["evidence_based", "causality"],
        "forbidden": ["scope_control", "minimal_change", "execution_first"],
    },
    "decision": {
        "subtypes": ["approach_selection", "tradeoff_evaluation", "risk_assessment"],
        "required": ["explicit_assumption"],
        "forbidden": ["scope_control", "minimal_change", "execution_first"],
    },
    "design": {
        "subtypes": ["solution_design", "api_design", "schema_design"],
        "required": ["constraint_awareness"],
        "forbidden": ["scope_control", "minimal_change", "execution_first"],
    },
    "planning": {
        "subtypes": ["task_breakdown", "dependency_ordering", "effort_estimation"],
        "required": ["completeness"],
        "forbidden": ["scope_control", "minimal_change", "execution_first"],
    },
    "execution": {
        "subtypes": ["code_change", "test_writing", "refactoring", "configuration_change"],
        "required": ["scope_control", "minimal_change"],
        "forbidden": ["hypothesis_testing", "causality"],
    },
    "validation": {
        "subtypes": ["test_run", "regression_check", "evidence_verification"],
        "required": ["execution_first", "consistency_check"],
        "forbidden": ["causality", "hypothesis_testing"],
    },
}

CDP_ALL_PRINCIPALS = {
    "evidence_based", "no_hallucination", "constraint_awareness", "scope_control",
    "consistency_check", "execution_first", "minimal_change", "causality",
    "hypothesis_testing", "completeness", "risk_awareness", "explicit_assumption",
}


# ── p171 Declaration Validator (Python port) ─────────────────────────────────

class DeclError:
    INVALID_TYPE               = "INVALID_TYPE"
    TYPE_SUBTYPE_MISMATCH      = "TYPE_SUBTYPE_MISMATCH"
    UNKNOWN_PRINCIPAL          = "UNKNOWN_PRINCIPAL"
    MISSING_REQUIRED_PRINCIPAL = "MISSING_REQUIRED_PRINCIPAL"
    FORBIDDEN_PRINCIPAL_USED   = "FORBIDDEN_PRINCIPAL_USED"


def validate_declaration(decl_type: str, subtype: str | None, principals: list[str]) -> list[str]:
    """
    Port of validateDeclaration() from declaration-validator.ts (p171).
    Returns list of DeclError codes (empty = valid).
    """
    errors = []
    policy = CDP_TYPES.get(decl_type)
    if not policy:
        errors.append(DeclError.INVALID_TYPE)
        return errors  # short-circuit

    if subtype is not None and subtype not in policy["subtypes"]:
        errors.append(DeclError.TYPE_SUBTYPE_MISMATCH)

    forbidden = set(policy["forbidden"])
    for p in principals:
        if p not in CDP_ALL_PRINCIPALS:
            errors.append(DeclError.UNKNOWN_PRINCIPAL)
        elif p in forbidden:
            errors.append(DeclError.FORBIDDEN_PRINCIPAL_USED)

    declared = set(principals)
    for req in policy["required"]:
        if req not in declared:
            errors.append(DeclError.MISSING_REQUIRED_PRINCIPAL)

    return errors


# ── Traj processing ───────────────────────────────────────────────────────────

def extract_attempt_count(traj: dict) -> int:
    history = traj.get("history", [])
    attempts = {
        item.get("attempt", 1)
        for item in history
        if isinstance(item, dict) and "attempt" in item
    }
    return max(attempts) if attempts else 1


def extract_invalid_output_count(traj: dict) -> int:
    history = traj.get("history", [])
    return sum(
        1 for item in history
        if isinstance(item, dict) and item.get("verdict") == "invalid_output"
    )


def process_traj(traj_path: Path) -> dict:
    try:
        traj = json.loads(traj_path.read_text())
    except Exception as e:
        return {"instance_id": traj_path.parent.name, "error": str(e)}

    instance_id = traj_path.parent.name
    messages    = traj.get("messages", [])
    info        = traj.get("info", {})
    patch       = info.get("submission", "") or ""

    last_msg   = extract_last_agent_message(messages)
    raw_decl   = extract_declaration(last_msg)

    has_patch  = bool(patch.strip())
    has_decl   = bool(raw_decl.get("type"))
    decl_type  = raw_decl.get("type", "").lower() if has_decl else None
    principals = [p.lower() for p in raw_decl.get("principals", [])] if has_decl else []

    decl_errors: list[str] = []
    if has_decl and decl_type:
        decl_errors = validate_declaration(decl_type, None, principals)

    admitted = has_decl and len(decl_errors) == 0

    signals      = extract_patch_signals(patch) if has_patch else []
    patch_lines  = len(patch.splitlines()) if patch else 0
    attempts     = extract_attempt_count(traj)
    inv_outputs  = extract_invalid_output_count(traj)

    return {
        "instance_id":    instance_id,
        "has_patch":      has_patch,
        "has_decl":       has_decl,
        "decl_type":      decl_type,
        "principals":     principals,
        "decl_errors":    decl_errors,
        "admitted":       admitted,
        "patch_signals":  signals,
        "patch_lines":    patch_lines,
        "attempts":       attempts,
        "invalid_outputs": inv_outputs,
    }


# ── Report ────────────────────────────────────────────────────────────────────

def make_bar(rate: float, width: int = 25) -> str:
    filled = int(rate * width)
    return "█" * filled + "░" * (width - filled)


def generate_report(
    results: list[dict],
    baseline_results: list[dict] | None,
    sample_n: int,
) -> dict:
    total = len(results)
    if total == 0:
        return {"error": "no results"}

    # Q1
    has_patch_count = sum(1 for r in results if r.get("has_patch"))
    has_decl_count  = sum(1 for r in results if r.get("has_decl"))
    admitted_count  = sum(1 for r in results if r.get("admitted"))
    rejected_count  = sum(1 for r in results if r.get("has_decl") and not r.get("admitted"))

    decl_rate      = has_decl_count / total
    admitted_rate  = admitted_count / total
    rejection_rate = rejected_count / has_decl_count if has_decl_count else 0

    # Q2
    error_counter: Counter = Counter()
    for r in results:
        for e in r.get("decl_errors", []):
            error_counter[e] += 1

    # Q3 — sample
    rejected_sample = [r for r in results if r.get("has_decl") and not r.get("admitted")][:sample_n // 2]
    admitted_sample = [r for r in results if r.get("admitted")][: sample_n - len(rejected_sample)]

    # Q4
    avg_attempts   = sum(r.get("attempts", 1) for r in results) / total
    avg_inv_out    = sum(r.get("invalid_outputs", 0) for r in results) / total
    retry_rate     = sum(1 for r in results if r.get("attempts", 1) > 1) / total

    baseline_delta = None
    if baseline_results:
        bt = len(baseline_results)
        b_avg_att = sum(r.get("attempts", 1) for r in baseline_results) / bt
        b_avg_inv = sum(r.get("invalid_outputs", 0) for r in baseline_results) / bt
        b_retry   = sum(1 for r in baseline_results if r.get("attempts", 1) > 1) / bt
        baseline_delta = {
            "attempts_delta":   round(avg_attempts - b_avg_att, 2),
            "inv_output_delta": round(avg_inv_out - b_avg_inv, 2),
            "retry_rate_delta": round(retry_rate - b_retry, 3),
        }

    # Judgment
    if decl_rate < 0.50:
        judgment = "TUNE_PROMPT"
        judgment_reason = f"Only {decl_rate:.0%} declarations — agent not consistently following CDP protocol"
    elif decl_rate < 0.80:
        judgment = "PARTIAL_ADOPTION"
        judgment_reason = f"{decl_rate:.0%} declarations — protocol partially working, prompt needs refinement"
    elif rejection_rate > 0.70:
        judgment = "GATE_TOO_STRICT"
        judgment_reason = (
            f"Gate rejects {rejection_rate:.0%} of declarations — "
            "taxonomy or required_principals may be misconfigured"
        )
    elif admitted_rate >= 0.60:
        judgment = "GATE_EFFECTIVE"
        judgment_reason = (
            f"{decl_rate:.0%} declare, {admitted_rate:.0%} admitted — "
            "proceed to Lane B (p164/p165)"
        )
    else:
        judgment = "INVESTIGATE"
        judgment_reason = (
            f"{decl_rate:.0%} declare, {admitted_rate:.0%} admitted, "
            f"{rejection_rate:.0%} rejection — review Q3 sample"
        )

    return {
        "run_date": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "q1_coverage": {
            "total":          total,
            "has_patch":      has_patch_count,
            "has_decl":       has_decl_count,
            "admitted":       admitted_count,
            "rejected":       rejected_count,
            "decl_rate":      round(decl_rate, 3),
            "admitted_rate":  round(admitted_rate, 3),
            "rejection_rate": round(rejection_rate, 3),
        },
        "q2_rejection_dist": {
            "total_errors": sum(error_counter.values()),
            "by_code": {
                code: {
                    "count": count,
                    "rate":  round(count / has_decl_count, 3) if has_decl_count else 0,
                }
                for code, count in error_counter.most_common()
            },
        },
        "q3_precision_sample": {
            "rejected": [
                {
                    "instance_id": r["instance_id"],
                    "type":        r.get("decl_type"),
                    "principals":  r.get("principals", []),
                    "errors":      r.get("decl_errors", []),
                    "signals":     r.get("patch_signals", []),
                    "patch_lines": r.get("patch_lines", 0),
                }
                for r in rejected_sample
            ],
            "admitted": [
                {
                    "instance_id": r["instance_id"],
                    "type":        r.get("decl_type"),
                    "principals":  r.get("principals", []),
                    "signals":     r.get("patch_signals", []),
                    "patch_lines": r.get("patch_lines", 0),
                }
                for r in admitted_sample
            ],
        },
        "q4_behavior": {
            "avg_attempts":    round(avg_attempts, 2),
            "avg_inv_outputs": round(avg_inv_out, 2),
            "retry_rate":      round(retry_rate, 3),
            "baseline_delta":  baseline_delta,
        },
        "judgment": judgment,
        "judgment_reason": judgment_reason,
        "instances": results,
    }


def print_report(report: dict) -> None:
    sep = "=" * 60

    print(f"\n{sep}")
    print(f"  CDP GATE REPORT  —  {report['run_date'][:10]}")
    print(f"  Total instances: {report['total']}")
    print(sep)

    q1 = report["q1_coverage"]
    total = report["total"]
    print("\n  Q1  COVERAGE")
    print(f"  {'With patch':30s}  {q1['has_patch']:4d}  ({q1['has_patch']/total:.0%})")
    print(f"  {'Declaration present':30s}  {q1['has_decl']:4d}  ({q1['decl_rate']:.0%})  {make_bar(q1['decl_rate'])}")
    print(f"  {'Admitted (p171 valid)':30s}  {q1['admitted']:4d}  ({q1['admitted_rate']:.0%})  {make_bar(q1['admitted_rate'])}")
    print(f"  {'Rejected':30s}  {q1['rejected']:4d}  ({q1['rejection_rate']:.0%} of declared)")

    q2 = report["q2_rejection_dist"]
    print(f"\n  Q2  REJECTION DISTRIBUTION  (of {q1['has_decl']} declared instances)")
    if not q2["by_code"]:
        print("  (no rejections)")
    else:
        for code, info in q2["by_code"].items():
            print(f"  {code:35s}  {info['count']:4d}  ({info['rate']:.0%})  {make_bar(info['rate'], 20)}")

    q3 = report["q3_precision_sample"]
    print(f"\n  Q3  PRECISION SAMPLE  (spot-check these manually)")

    if q3["rejected"]:
        print(f"\n  — REJECTED ({len(q3['rejected'])} samples) —")
        for s in q3["rejected"]:
            err_str = ", ".join(s["errors"][:2])
            pri_str = ", ".join(s["principals"][:3]) or "(none)"
            sig_str = ", ".join(s["signals"]) or "-"
            print(f"    {s['instance_id']:42s}  type={s['type']}  err=[{err_str}]")
            print(f"    {'':42s}  principals=[{pri_str}]  signals=[{sig_str}]  lines={s['patch_lines']}")

    if q3["admitted"]:
        print(f"\n  — ADMITTED ({len(q3['admitted'])} samples) —")
        for s in q3["admitted"]:
            pri_str = ", ".join(s["principals"][:3]) or "(none)"
            sig_str = ", ".join(s["signals"]) or "-"
            print(f"    {s['instance_id']:42s}  type={s['type']}")
            print(f"    {'':42s}  principals=[{pri_str}]  signals=[{sig_str}]  lines={s['patch_lines']}")

    q4 = report["q4_behavior"]
    print(f"\n  Q4  BEHAVIORAL METRICS")
    print(f"  {'Avg attempts':30s}  {q4['avg_attempts']:.2f}")
    print(f"  {'Avg invalid_outputs':30s}  {q4['avg_inv_outputs']:.2f}")
    print(f"  {'Retry rate':30s}  {q4['retry_rate']:.0%}")
    if q4.get("baseline_delta"):
        d = q4["baseline_delta"]
        sign = lambda x: ("+" if x >= 0 else "") + str(x)
        print(f"  {'vs baseline (Δ):':30s}  attempts {sign(d['attempts_delta'])}  "
              f"inv_out {sign(d['inv_output_delta'])}  retry {sign(d['retry_rate_delta'])}")

    print(f"\n{sep}")
    print(f"  JUDGMENT: {report['judgment']}")
    print(f"  {report['judgment_reason']}")

    next_steps = {
        "GATE_EFFECTIVE":   "→ Lane B: p164 no-signal-detector, p165 control-loop-principles",
        "TUNE_PROMPT":      "→ Update system prompt to reinforce FIX_TYPE + PRINCIPALS declaration",
        "GATE_TOO_STRICT":  "→ Review required_principals in CDP_TYPES (taxonomy.ts)",
        "PARTIAL_ADOPTION": "→ Strengthen prompt, re-run with same instances",
        "INVESTIGATE":      "→ Inspect Q3 rejected samples for common pattern",
    }
    step = next_steps.get(report["judgment"], "→ Review results")
    print(f"  {step}")
    print(sep)


def main() -> None:
    parser = argparse.ArgumentParser(description="CDP Gate Report — p169")
    parser.add_argument("--results",  required=True, help="Results dir (contains *.traj.json files)")
    parser.add_argument("--baseline", default=None,  help="Baseline results dir for Q4 delta")
    parser.add_argument("--output",   default=None,  help="Write JSON report to this path")
    parser.add_argument("--sample",   type=int, default=10, help="Q3 sample size (default 10)")
    args = parser.parse_args()

    results_dir = Path(args.results)
    traj_files  = sorted(results_dir.rglob("*.traj.json"))
    if not traj_files:
        print(f"No *.traj.json files found under {results_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Processing {len(traj_files)} trajectories...", flush=True)
    results = [process_traj(p) for p in traj_files]
    results = [r for r in results if "error" not in r]

    baseline_results = None
    if args.baseline:
        b_files = sorted(Path(args.baseline).rglob("*.traj.json"))
        if b_files:
            print(f"Processing {len(b_files)} baseline trajectories...", flush=True)
            baseline_results = [process_traj(p) for p in b_files]
            baseline_results = [r for r in baseline_results if "error" not in r]

    report = generate_report(results, baseline_results, args.sample)
    print_report(report)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        slim = {k: v for k, v in report.items() if k != "instances"}
        out_path.write_text(json.dumps(slim, indent=2))
        print(f"\nReport written: {out_path}")


if __name__ == "__main__":
    main()
