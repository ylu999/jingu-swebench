"""step_sections — extracted step-level section functions.

p225-05: These functions were extracted verbatim from run_with_jingu_gate.py
to enable independent testing and gradual migration to JinguAgent hooks.

Functions:
    _step_observe          — Section 1: pure observation
    _step_verify_if_needed — Section 2: patch signal + inner-verify
    _step_cp_update_and_verdict — Section 3: control-plane update + verdict
    _step_check_structure  — Section 4: per-step structure validation
    _step_inject_phase     — Section 5: phase prefix injection
    _check_materialization_gate — Section 6: EXECUTE liveness gate
    PHASE_REQUIRED_FIELDS  — per-phase required field definitions
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from step_monitor_state import StepMonitorState

# ---------------------------------------------------------------------------
# Imports shared across sections — lazy-imported in original, kept lazy here.
# Only import what is needed at module level; heavy deps stay inside functions.
# ---------------------------------------------------------------------------
from dataclasses import dataclass, field as dc_field
from signal_extraction import _msg_has_env_mutation, _msg_has_signal
from control.reasoning_state import (
    decide_next,
    VerdictStop, VerdictRedirect, VerdictAdvance, VerdictContinue,
)
from control.swe_signal_adapter import extract_weak_progress
from gate_rejection import SDG_ENABLED as _SDG_ENABLED, build_repair_from_rejection as _build_sdg_repair
from failure_routing import route_failure as route_failure_p216, is_data_driven_routing_enabled
from strategy_prompts import get_strategy_prompt
from routing_decision import RoutingDecision
from step_monitor_state import StopExecution
from declaration_extractor import build_phase_record_from_structured, extract_phase_output


# ══════════════════════════════════════════════════════════════════════════════
# Phase 4: TransitionEvaluation — sole transition authority result type
#
# evaluate_transition() produces this. The advance handler ONLY consumes it.
# No gate logic in the handler. No re-decision. Only state commit + effects.
#
# verdict is the SOLE control signal. Four values:
#   "advance"  — all gates passed, commit phase transition
#   "retry"    — blocked, stay in current phase, agent retries
#   "redirect" — blocked, move to a different phase (next_phase)
#   "stop"     — terminal, stop the attempt
#
# routing is TELEMETRY ONLY — strategy prompt selection + logging.
# It never drives control flow. Handler reads verdict, not routing.
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class TransitionEvaluation:
    """Result of evaluate_transition() — the sole transition authority.

    Phase 4: unified verdict model. One verdict field drives all control.
    routing is telemetry-only (strategy prompt + explainability).
    """
    # The ONE control signal — "advance" | "retry" | "redirect" | "stop"
    verdict: str = "retry"
    # Target phase for redirect (only meaningful when verdict="redirect")
    next_phase: str = ""
    # Stop reason (only meaningful when verdict="stop")
    stop_reason: str = ""
    # Messages to inject into agent context (role=user)
    pending_messages: list = dc_field(default_factory=list)
    # Phase record (if admitted)
    phase_record: object = None
    phase_record_source: str = "none"
    # State mutations to apply (list of (attr, value) tuples or callables)
    state_mutations: list = dc_field(default_factory=list)
    # Whether principal gate already redirected phase (RETRYABLE with redirect)
    pg_redirected: bool = False
    # Verdict source attribution
    source: str = "default"
    reason: str = ""
    # Telemetry-only: strategy prompt selection + explainability.
    # Never drives control flow. Handler reads verdict, not routing.
    routing: "RoutingDecision | None" = None
    # Governance exception marker: True when verdict="advance" was produced
    # by tolerance exhaustion (force_advance), not by clean gate passage.
    # Audit-only — does NOT change control flow.
    tolerated: bool = False
    tolerated_gate: str = ""  # which gate was force-advanced (e.g. "design_gate")


# ══════════════════════════════════════════════════════════════════════════════
# P0.1: AdmissionResult — immediate admission at submit time
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class AdmissionResult:
    """Result of immediate admission at submit time."""
    admitted: bool = False
    record: object = None           # PhaseRecord if admitted
    source: str = ""                # "tool_submitted" | "structured" | ...
    retry_messages: list = dc_field(default_factory=list)
    stop: bool = False              # True = protocol violation
    stop_reason: str = ""
    extraction_telemetry: dict = dc_field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════════════
# Extracted: fake loop escalation check (Gate 10 core logic)
#
# Determines whether a fake_principal violation should trigger:
#   - "bypass": escalation limit reached → selective bypass, record PRESERVED
#   - "retry": below limit → record cleared, agent retries
#
# Extracted from evaluate_transition() for testability (10999 bug fix).
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class FakeLoopResult:
    """Result of fake loop escalation check."""
    action: str = "retry"  # "bypass" or "retry"
    bypassed_principals: list = dc_field(default_factory=list)
    loop_count: int = 0


def _handle_fake_loop_check(state, eval_phase: str, violation: str) -> FakeLoopResult:
    """Check fake loop count and decide bypass vs retry.

    On bypass: record is PRESERVED in state.phase_records.
    On retry: record for eval_phase is CLEARED from state.phase_records.

    Returns FakeLoopResult with action="bypass" or "retry".
    """
    from principal_gate import _FAKE_LOOP_LIMIT

    loop_key = (eval_phase, violation)
    state._retryable_loop_counts[loop_key] = (
        state._retryable_loop_counts.get(loop_key, 0) + 1
    )
    # Reset other loop counters
    for k in list(state._retryable_loop_counts):
        if k != loop_key:
            state._retryable_loop_counts[k] = 0
    loop_count = state._retryable_loop_counts[loop_key]

    if loop_count >= _FAKE_LOOP_LIMIT:
        # Escalation: selective bypass — record PRESERVED
        fake_principals = []
        if ":" in violation:
            fake_principals = [
                p.strip() for p in violation.split(":", 1)[1].split(",")
                if p.strip()
            ]
        state._bypassed_principals.update(fake_principals)
        state._retryable_loop_counts[loop_key] = 0
        state.pending_redirect_hint = ""
        return FakeLoopResult(
            action="bypass",
            bypassed_principals=sorted(state._bypassed_principals),
            loop_count=loop_count,
        )
    else:
        # Below limit: clear record for retry
        state.phase_records = [
            r for r in state.phase_records
            if r.phase.upper() != eval_phase
        ]
        return FakeLoopResult(
            action="retry",
            bypassed_principals=[],
            loop_count=loop_count,
        )


# ── Phase 4.5: Rejection Policy Table ─────────────────────────────────────
# Single source of truth for all rejection → verdict escalation.
#
# Each entry maps a blocked_reason to a sequence of verdicts by occurrence:
#   verdicts[0] = first rejection
#   verdicts[1] = second rejection (if different from first)
#   verdicts[-1] = final escalation (repeat for all subsequent)
#
# Verdict spec: (verdict_type, strategy_key, repair_hint)
#   verdict_type: "retry" | "redirect" | "stop" | "force_advance"
#   strategy_key: maps to strategy_prompts.py via get_strategy_prompt()
#   repair_hint: injected into agent context
#
# "force_advance" = admit despite gate failure (tolerance exhaustion).
# This replaces the scattered force_pass logic in _run_phase_gate.

_REJECTION_POLICY: dict[str, dict] = {
    "missing_phase_record_retry": {
        "verdicts": [
            ("retry", "submit_phase_record", "Call submit_phase_record with your findings before phase can advance."),
            ("retry", "submit_phase_record", "Call submit_phase_record with your findings before phase can advance."),
            ("stop", "submit_phase_record", "Agent never submitted phase record after max retries."),
        ],
    },
    "cognition_validation_failed": {
        "verdicts": [
            ("retry", "fix_cognition_errors", "Fix the validation errors reported and resubmit."),
            ("retry", "fix_cognition_errors", "Fix the validation errors reported and resubmit."),
            ("stop", "fix_cognition_errors", "Cognition validation failed after max retries."),
        ],
    },
    "analysis_gate_rejected": {
        "verdicts": [
            ("retry", "complete_causal_chain", "Strengthen root cause, causal chain, or alternatives as indicated."),
            ("retry", "complete_causal_chain", "Strengthen root cause, causal chain, or alternatives as indicated."),
            ("stop", "rethink_root_cause", "Analysis gate exhausted — fail closed."),
        ],
    },
    "design_gate_rejected": {
        "verdicts": [
            ("retry", "compare_alternatives", "Improve design comparison or constraint encoding as indicated."),
            ("retry", "compare_alternatives", "Improve design comparison or constraint encoding as indicated."),
            ("force_advance", "compare_alternatives", "Design gate tolerance exhausted — admitting."),
        ],
    },
    "decide_gate_rejected": {
        "verdicts": [
            ("retry", "rethink_root_cause", "Strengthen decision rationale as indicated."),
            ("retry", "rethink_root_cause", "Strengthen decision rationale as indicated."),
            ("force_advance", "rethink_root_cause", "Decide gate tolerance exhausted — admitting."),
        ],
    },
    "execute_gate_rejected": {
        "verdicts": [
            ("retry", "fix_execution_errors", "Fix the execution issues reported."),
            ("retry", "fix_execution_errors", "Fix the execution issues reported."),
            ("force_advance", "fix_execution_errors", "Execute gate tolerance exhausted — admitting."),
        ],
    },
    "judge_gate_rejected": {
        "verdicts": [
            ("retry", "verify_test_coverage", "Improve verification coverage as indicated."),
            ("retry", "verify_test_coverage", "Improve verification coverage as indicated."),
            ("force_advance", "verify_test_coverage", "Judge gate tolerance exhausted — admitting."),
        ],
    },
    "principal_gate_retryable": {
        "verdicts": [
            ("retry", "rethink_root_cause", "Fix principal violations as indicated."),
            ("retry", "rethink_root_cause", "Fix principal violations as indicated."),
            ("retry", "rethink_root_cause", "Fix principal violations as indicated."),
            # escalation handled by principal_gate's own loop_count mechanism
        ],
    },
    "fake_principal": {
        "verdicts": [
            ("retry", "gather_code_evidence", "Provide concrete evidence for declared principals."),
            ("retry", "gather_code_evidence", "Provide concrete evidence for declared principals."),
            ("retry", "gather_code_evidence", "Provide concrete evidence for declared principals."),
            # escalation (selective bypass) handled by _FAKE_LOOP_LIMIT mechanism
        ],
    },
}


def lookup_rejection_policy(reason: str, occurrence: int) -> tuple[str, str, str]:
    """Look up the verdict for a given rejection reason and occurrence count.

    Args:
        reason: blocked reason (may contain ":" suffix — prefix is used for lookup)
        occurrence: how many times this reason has been seen (0-based)

    Returns:
        (verdict_type, strategy_key, repair_hint)
        Falls back to ("retry", "rethink_root_cause", "") for unknown reasons.
    """
    _reason_key = reason.split(":")[0] if ":" in reason else reason
    entry = _REJECTION_POLICY.get(_reason_key)
    if not entry:
        return ("retry", "rethink_root_cause", "")
    verdicts = entry["verdicts"]
    # Clamp to last entry for occurrences beyond the list
    idx = min(occurrence, len(verdicts) - 1)
    return verdicts[idx]


def _route_blocked(eval_phase: str, reason: str, occurrence: int = 0) -> RoutingDecision:
    """Phase 4.5: produce a typed RoutingDecision for a blocked transition.

    P1.1: routing is now CONTROL, not just telemetry.
    Uses contract repair_target for cross-phase routing when available.
    Falls back to eval_phase (stay in current phase) when no repair_target.
    """
    _verdict_type, _strategy, _hint = lookup_rejection_policy(reason, occurrence)
    # P1.1: derive next_phase from contract repair_target
    _next_phase = eval_phase  # default: stay in current phase
    try:
        from subtype_contracts import get_repair_target
        _repair = get_repair_target(eval_phase)
        if _repair:
            _next_phase = _repair
    except Exception:
        pass
    return RoutingDecision(
        next_phase=_next_phase,
        strategy=_strategy,
        repair_hints=[_hint] if _hint else [],
        source="rejection_policy",
    )


def admit_phase_record(
    agent_self,
    *,
    state: "StepMonitorState",
    cp_state_holder: "list | None" = None,
    eval_phase: str,
    old_phase: str = "",
    latest_assistant_text: str = "",
) -> AdmissionResult:
    """P0.1: Immediate admission — validate and store record at submit time.

    Runs Gate 1 (Record Acquisition) and Gate 3 (Cognition Validation) from
    evaluate_transition(). Does NOT run phase-specific gates or Principal Gate.

    Returns AdmissionResult:
      admitted=True  → record appended to state.phase_records
      admitted=False → retry_messages populated for caller to inject
      stop=True      → protocol violation, caller should raise StopExecution
    """
    _result = AdmissionResult()
    _cp_s = cp_state_holder[0] if cp_state_holder is not None else state.cp_state

    # ── Gate 0: Routing enforcement (P0.2) ──────────────────────────────
    # Peek submitted record WITHOUT consuming. If routing set
    # required_next_phase, reject on mismatch (record stays unconsumed
    # so agent can re-examine and resubmit the correct phase).
    _model = getattr(agent_self, "model", None)
    _peeked = None
    if _model is not None and hasattr(_model, "_submitted_phase_record"):
        _peeked = _model._submitted_phase_record  # peek, not pop

    if _peeked is None:
        # No record submitted — caller handles this
        return _result

    if state.required_next_phase is not None:
        _submitted_phase = (_peeked.get("phase") or "").upper()
        _required = state.required_next_phase.upper()
        if _submitted_phase != _required:
            print(
                f"    [immediate-admission] ROUTING REJECT:"
                f" required={_required} submitted={_submitted_phase}"
                f" — record NOT consumed (agent can retry)",
                flush=True,
            )
            _result.admitted = False
            _result.retry_messages.append({
                "role": "user",
                "content": (
                    f"[ROUTING ENFORCEMENT]\n"
                    f"The system requires you to submit a {_required} record.\n"
                    f"You submitted {_submitted_phase}. This does not match.\n"
                    f"Return to {_required} phase and submit the correct record."
                ),
            })
            return _result
        else:
            # Match — clear the routing constraint
            print(
                f"    [immediate-admission] routing match:"
                f" required={_required} submitted={_submitted_phase}"
                f" — constraint cleared",
                flush=True,
            )
            state.required_next_phase = None

    # ── Gate 0.5: QJ corrective signal enforcement (P0.4) ────────────
    # If agent ignored a corrective QJ signal, soft-reject the submission.
    if getattr(state, '_qj_corrective_ignored', False):
        _last_qj = state.quick_judge_history[-1] if state.quick_judge_history else {}
        _test_info = _last_qj.get("target_test_id", "unknown")
        print(
            f"    [immediate-admission] Gate 0.5 REJECT:"
            f" corrective QJ ignored (test={_test_info})",
            flush=True,
        )
        # Clear flag so agent gets one more chance after seeing this message
        state._qj_corrective_ignored = False
        _result.admitted = False
        _result.retry_messages.append({
            "role": "user",
            "content": (
                f"[QJ SIGNAL IGNORED]\n"
                f"A quick judge test showed your patch has issues "
                f"(test: {_test_info}).\n"
                f"You must address this test result before proceeding.\n"
                f"Review the test output above and adjust your approach."
            ),
        })
        return _result

    # ── Gate 1: Record Acquisition ──────────────────────────────────────
    # Gate 0 passed (or no routing constraint). Now consume the record.
    _pr = None
    _pr_source = "none"
    _pr_foreign_phase = ""
    try:
        _tool_submitted = None
        if _model is not None and hasattr(_model, "pop_submitted_phase_record"):
            _tool_submitted = _model.pop_submitted_phase_record()

        if _tool_submitted is None:
            # Should not happen — we peeked a record above. Defensive.
            return _result

        _pr = build_phase_record_from_structured(
            _tool_submitted, str(old_phase) if old_phase else str(eval_phase)
        )
        _pr_source = "tool_submitted"
        if hasattr(state, "_extraction_tool_submitted"):
            state._extraction_tool_submitted += 1
        _declared_phase = (_tool_submitted.get("phase") or "").upper()
        _foreign = bool(_declared_phase and _declared_phase != eval_phase)
        print(
            f"    [immediate-admission] extraction_method=tool_submitted"
            f" fields={list(_tool_submitted.keys())}"
            f" phase={eval_phase}",
            flush=True,
        )
        if _foreign:
            _pr_foreign_phase = _declared_phase
            from canonical_symbols import ALL_PHASES as _PHASE_ORDER_CS
            try:
                _eval_idx = _PHASE_ORDER_CS.index(eval_phase)
                _decl_idx = _PHASE_ORDER_CS.index(_declared_phase)
                _align = "declared_ahead" if _decl_idx > _eval_idx else "declared_behind"
                _align_delta = _decl_idx - _eval_idx
            except (ValueError, AttributeError):
                _align = "unknown_phase"
                _align_delta = 0
            print(
                f"    [immediate-admission] foreign_phase_declared:"
                f" eval_phase={eval_phase} declared_phase={_declared_phase}"
                f" alignment={_align} delta={_align_delta}",
                flush=True,
            )

        # C-09: Parallel extract_phase_output() for unified telemetry
        try:
            _extraction_schema = None
            try:
                from jingu_onboard import onboard as _onboard_fn
                _gov = _onboard_fn()
                _extraction_schema = _gov.get_constrained_schema(eval_phase)
            except Exception:
                pass

            _schema_fields: list[str] = []
            if _extraction_schema is not None:
                _schema_props = _extraction_schema.get("properties", {})
                _schema_fields = [k for k in _schema_props if k not in ("phase", "subtype")]

            _epo_record, _epo_meta = extract_phase_output(
                tool_submitted=_tool_submitted,
                structured_parsed=None,
                agent_message=latest_assistant_text,
                phase=str(old_phase) if old_phase else str(eval_phase),
                schema_fields=_schema_fields,
            )

            if not hasattr(state, "extraction_telemetry"):
                state.extraction_telemetry = {}
            state.extraction_telemetry[eval_phase] = {
                "extraction_source": _epo_meta.source,
                "schema_field_count": len(_epo_meta.fields_in_schema),
                "extracted_count": len(_epo_meta.fields_extracted),
                "missing_count": len(_epo_meta.fields_missing),
                "fields_extracted": _epo_meta.fields_extracted,
                "fields_missing": _epo_meta.fields_missing,
            }
            _result.extraction_telemetry = state.extraction_telemetry[eval_phase]
            print(
                f"    [immediate-admission] extraction phase={eval_phase}"
                f" source={_epo_meta.source}"
                f" extracted={len(_epo_meta.fields_extracted)}"
                f" missing={len(_epo_meta.fields_missing)}",
                flush=True,
            )
        except Exception as _epo_exc:
            print(f"    [immediate-admission] telemetry error (non-fatal): {_epo_exc}", flush=True)

    except Exception as _pr_exc:
        print(f"    [immediate-admission] record acquisition error (non-fatal): {_pr_exc}", flush=True)
        return _result

    if _pr is None:
        return _result

    # ── Gate 3: Cognition Validation ────────────────────────────────────
    try:
        from cognition_prompts import COGNITION_EXECUTION_ENABLED as _COG_ENABLED
        if _COG_ENABLED:
            from cognition_prompts import CognitionLoader as _CogLoader
            from phase_validator import (
                validate_phase_record as _validate_pr,
                build_validation_feedback as _build_cog_feedback,
            )
            import json as _json_cog
            import os as _os_cog
            from pathlib import Path as _Path_cog
            _bundle_path_cog = _os_cog.environ.get(
                "JINGU_BUNDLE_PATH",
                str(_Path_cog(__file__).parent.parent / "bundle.json"),
            )
            with open(_bundle_path_cog) as _f_cog:
                _cog_bundle = _json_cog.load(_f_cog)
            _cog_loader = _CogLoader(_cog_bundle)
            _cog_errors = _validate_pr(_pr, _cog_loader)
            if _cog_errors:
                _cog_codes = [e.code for e in _cog_errors]
                print(
                    f"    [immediate-admission] cognition_validator REJECT errors={_cog_codes}",
                    flush=True,
                )
                _cog_feedback = _build_cog_feedback(_cog_errors, _pr, _cog_loader)
                _result.admitted = False
                _result.retry_messages.append({
                    "role": "user",
                    "content": (
                        f"[Cognition Validation Failed]\n\n"
                        f"{_cog_feedback}\n\n"
                        f"Fix the issues above and resubmit for phase {eval_phase}."
                    ),
                })
                return _result
            else:
                print(
                    f"    [immediate-admission] cognition_validator PASS phase={_pr.phase}"
                    f" subtype={_pr.subtype}",
                    flush=True,
                )
    except Exception as _cog_exc:
        print(f"    [immediate-admission] cognition_validator error (non-fatal): {_cog_exc}", flush=True)

    # ── Gate 4: ANALYZE Quality Gate (trajectory collapse prevention) ──
    # Reject ANALYZE submissions that lack grounded root cause, alternative
    # hypotheses, or evidence refs. Forces the agent to explore multiple
    # hypotheses before committing to a single analysis path.
    if eval_phase == "ANALYZE" and _pr is not None:
        _aq_failures: list[str] = []

        # Check 1: root_cause must reference code (file path or function name)
        _rc = getattr(_pr, "root_cause", "") or ""
        _rc_has_grounding = bool(
            _rc
            and len(_rc) >= 10
            and any(
                sig in _rc
                for sig in ("/", ".py", ".js", ".ts", "def ", "class ", "function ", "()", "::")
            )
        )
        if not _rc_has_grounding:
            _aq_failures.append(
                "ROOT_CAUSE lacks code grounding. "
                "Point to the exact file, function, or line causing the issue."
            )

        # Check 2: at least 1 alternative hypothesis
        _alt = getattr(_pr, "alternative_hypotheses", []) or []
        _alt_valid = [
            h for h in _alt
            if isinstance(h, dict) and (h.get("hypothesis") or h.get("description") or "").strip()
        ]
        if len(_alt_valid) < 1:
            _aq_failures.append(
                "No alternative hypotheses provided. "
                "You MUST consider at least 1 alternative cause and explain "
                "why it was ruled out. This prevents premature commitment to "
                "a single analysis path."
            )

        # Check 3: evidence_refs non-empty
        _ev = getattr(_pr, "evidence_refs", []) or []
        if len(_ev) < 1:
            _aq_failures.append(
                "No evidence references. Cite at least one file:line or test name "
                "that supports your analysis."
            )

        if _aq_failures:
            # Track rejection count to avoid infinite ANALYZE loops
            if not hasattr(state, "_analyze_quality_rejections"):
                state._analyze_quality_rejections = 0
            state._analyze_quality_rejections += 1

            # Allow max 2 rejections — after that, let it through to avoid deadlock
            if state._analyze_quality_rejections <= 2:
                _aq_msg = "\n".join(f"  - {f}" for f in _aq_failures)
                print(
                    f"    [immediate-admission] ANALYZE QUALITY REJECT"
                    f" ({state._analyze_quality_rejections}/2)"
                    f" failures={len(_aq_failures)}: {_aq_failures}",
                    flush=True,
                )
                _result.admitted = False
                _result.retry_messages.append({
                    "role": "user",
                    "content": (
                        f"[ANALYZE QUALITY GATE — REJECTED]\n\n"
                        f"Your analysis submission was rejected for quality issues:\n"
                        f"{_aq_msg}\n\n"
                        f"IMPORTANT: Do NOT proceed to a fix. Go back and deepen your "
                        f"analysis. Consider alternative root causes. Look at the code "
                        f"more carefully. What else could explain the test failure?\n\n"
                        f"Resubmit your ANALYZE record with the issues fixed."
                    ),
                })
                return _result
            else:
                print(
                    f"    [immediate-admission] ANALYZE QUALITY: bypassed"
                    f" (max rejections reached, allowing through)",
                    flush=True,
                )

    # ── Gate 5: DESIGN Quality Gate (structural redesign enforcement) ────
    # Reject DESIGN submissions that lack concrete solution structure.
    # Forces the agent to produce a real design before entering EXECUTE.
    if eval_phase == "DESIGN" and _pr is not None:
        _dq_failures: list[str] = []

        # Check 1: files_to_modify must be non-empty and contain file paths
        _ftm = getattr(_pr, "files_to_modify", []) or []
        if isinstance(_ftm, str):
            _ftm = [_ftm] if _ftm.strip() else []
        _ftm_valid = [
            f for f in _ftm
            if isinstance(f, str) and ("/" in f or ".py" in f or ".js" in f or ".ts" in f)
        ]
        if len(_ftm_valid) < 1:
            _dq_failures.append(
                "FILES_TO_MODIFY is empty or lacks file paths. "
                "You MUST specify at least one file path to modify (e.g. django/db/models/query.py)."
            )

        # Check 2: scope_boundary must be non-trivial
        _sb = getattr(_pr, "scope_boundary", "") or ""
        if isinstance(_sb, list):
            _sb = " ".join(str(x) for x in _sb)
        if len(str(_sb).strip()) < 10:
            _dq_failures.append(
                "SCOPE_BOUNDARY is missing or too brief. "
                "Describe what your fix will change and what it must NOT change. "
                "This prevents scope creep and unintended regressions."
            )

        # Check 3: solution approach must be present (in any of several possible fields)
        # Agent often puts approach info in scope_boundary (e.g. "SOLUTION APPROACH: ...").
        # Accept approach from any of these sources.
        _approach = (
            getattr(_pr, "solution_approach", "")
            or getattr(_pr, "design_comparison", "")
            or getattr(_pr, "approach", "")
            or ""
        )
        if isinstance(_approach, list):
            _approach = " ".join(str(x) for x in _approach)
        # Also check the raw submitted record for approach-like content
        if _tool_submitted is not None and len(str(_approach).strip()) < 10:
            for _ak in ("solution_approach", "approach", "design_comparison", "strategy"):
                _av = _tool_submitted.get(_ak, "")
                if isinstance(_av, str) and len(_av.strip()) >= 10:
                    _approach = _av
                    break
        # Fallback: if scope_boundary is substantial (>30 chars), agent likely
        # embedded approach info there. Accept it to avoid false rejections.
        if len(str(_approach).strip()) < 10:
            _sb_text = str(_sb).strip() if isinstance(_sb, str) else " ".join(str(x) for x in (_sb or []))
            if len(_sb_text) > 30:
                _approach = _sb_text  # scope_boundary doubles as approach

        if len(str(_approach).strip()) < 10:
            _dq_failures.append(
                "No SOLUTION APPROACH found. "
                "Describe HOW you will fix the issue — what logic changes, "
                "what conditions to add/remove, what the fix strategy is."
            )

        if _dq_failures:
            if not hasattr(state, "_design_quality_rejections"):
                state._design_quality_rejections = 0
            state._design_quality_rejections += 1

            if state._design_quality_rejections <= 2:
                _dq_msg = "\n".join(f"  - {f}" for f in _dq_failures)
                print(
                    f"    [immediate-admission] DESIGN QUALITY REJECT"
                    f" ({state._design_quality_rejections}/2)"
                    f" failures={len(_dq_failures)}: {_dq_failures}",
                    flush=True,
                )
                _result.admitted = False
                _result.retry_messages.append({
                    "role": "user",
                    "content": (
                        f"[DESIGN QUALITY GATE — REJECTED]\n\n"
                        f"Your design submission was rejected for quality issues:\n"
                        f"{_dq_msg}\n\n"
                        f"IMPORTANT: Do NOT jump straight to writing code. "
                        f"First, clearly specify:\n"
                        f"  1. Which files you will modify\n"
                        f"  2. What your scope boundary is (what changes, what must NOT change)\n"
                        f"  3. Your solution approach (what logic changes and why)\n\n"
                        f"Resubmit your DESIGN record with the issues fixed."
                    ),
                })
                return _result
            else:
                print(
                    f"    [immediate-admission] DESIGN QUALITY: bypassed"
                    f" (max rejections reached, allowing through)",
                    flush=True,
                )

    # ── Protocol validation: control fields must be present ──────────────
    # Protocol Compiler enforcement: all protocol_required fields must be in
    # the submitted record. Missing control field = rejection (not bypass).
    try:
        from protocol_compiler import validate_record_protocol, _get_protocol_specs
        _proto_specs = _get_protocol_specs()
        # Build a dict from the PhaseRecord for validation
        _proto_record = {}
        if _tool_submitted is not None:
            _proto_record = _tool_submitted
        elif _pr is not None:
            _proto_record = _pr.as_dict() if hasattr(_pr, "as_dict") else vars(_pr)
        _proto_missing = validate_record_protocol(_proto_record, eval_phase, _proto_specs)
        if _proto_missing:
            _proto_msg = ", ".join(_proto_missing)
            print(
                f"    [immediate-admission] PROTOCOL REJECT:"
                f" phase={eval_phase} missing_fields={_proto_missing}",
                flush=True,
            )
            # Build field-specific repair hints from protocol specs
            _field_hints = []
            _spec_map = {s.name: s for s in _proto_specs if s.phase == eval_phase}
            for _mf in _proto_missing:
                _fs = _spec_map.get(_mf)
                if _fs and _fs.prompt_instruction:
                    _field_hints.append(f"  - {_mf}: {_fs.prompt_instruction}")
                elif _fs and _fs.field_type == "enum" and _fs.enum_values:
                    _field_hints.append(f"  - {_mf}: choose one of [{', '.join(_fs.enum_values)}]")
                else:
                    _field_hints.append(f"  - {_mf}: required field, must be non-empty")
            _hint_block = "\n".join(_field_hints)
            _result.admitted = False
            _result.retry_messages.append({
                "role": "user",
                "content": (
                    f"[PROTOCOL VIOLATION — INCOMPLETE SUBMISSION]\n\n"
                    f"Your submission is missing required protocol fields: {_proto_msg}\n\n"
                    f"Fix these fields:\n{_hint_block}\n\n"
                    f"Resubmit with ALL required fields. Missing fields = REJECTED."
                ),
            })
            return _result
    except ImportError:
        pass  # protocol_compiler not available — skip (graceful degradation)

    # ── Admission success: append to state.phase_records ────────────────
    state.phase_records.append(_pr)
    _result.admitted = True
    _result.record = _pr
    _result.source = _pr_source
    print(
        f"    [immediate-admission] ADMITTED phase={eval_phase}"
        f" record_phase={_pr.phase} source={_pr_source}"
        f" subtype={_pr.subtype} principals={_pr.principals}",
        flush=True,
    )
    return _result


def evaluate_transition(
    agent_self,
    *,
    state: "StepMonitorState",
    cp_state_holder: "list | None",
    eval_phase: str,
    old_phase: str,
    latest_assistant_text: str,
) -> TransitionEvaluation:
    """Phase 3: sole transition authority.

    P0.1 cleanup: evaluate_transition() no longer acquires or validates records.
    Records are admitted at submit time by admit_phase_record().
    This function consumes the pre-admitted record and runs:
      1. Pre-admitted record lookup (from state.phase_records)
      2. Phase Completion Gate (defensive assertion — should not fire post-admission)
      3. Phase-specific gates (analysis, design, decide, execute, judge)
      4. Principal Gate
      5. Principal Inference
      6. Diagnostic extraction (telemetry only)

    Returns TransitionEvaluation — the advance handler ONLY consumes this,
    never re-evaluates gates.
    """
    _cp_s = cp_state_holder[0] if cp_state_holder is not None else state.cp_state
    result = TransitionEvaluation()

    # ══════════════════════════════════════════════════════════════════
    # Step 1: Lookup pre-admitted record from state.phase_records
    # Records are admitted by admit_phase_record() at submit time.
    # This is the ONLY acquisition path — no legacy pop/build/append.
    # ══════════════════════════════════════════════════════════════════
    _pr = None
    _pr_source = "none"
    _pr_foreign_phase = ""
    _diagnostic_pr = None
    for _existing in reversed(state.phase_records):
        if hasattr(_existing, 'phase') and _existing.phase.upper() == eval_phase:
            _pr = _existing
            _pr_source = "pre_admitted"
            break

    if _pr is not None:
        print(
            f"    [evaluate_transition] phase={eval_phase}"
            f" record_phase={_pr.phase} source=pre_admitted"
            f" subtype={_pr.subtype} principals={_pr.principals}",
            flush=True,
        )
    else:
        print(
            f"    [evaluate_transition] phase={eval_phase}"
            f" source=none — no pre-admitted record found"
            f" ASSERTION_FAILURE=should_not_happen_post_admission",
            flush=True,
        )

    # Diagnostic extraction (telemetry ONLY, never admission)
    _model = getattr(agent_self, "model", None)
    try:
        _accumulated = state._phase_accumulated_text.get(eval_phase, "")
        _extract_text = _accumulated if _accumulated.strip() else latest_assistant_text
        _structured_parsed = None
        _extraction_schema = None
        try:
            from jingu_onboard import onboard as _onboard_fn
            _gov = _onboard_fn()
            _extraction_schema = _gov.get_constrained_schema(eval_phase)
            _phase_hint = ""
            try:
                _cog = _gov.get_cognition(eval_phase)
                if _cog and _cog.success_criteria:
                    _phase_hint = "; ".join(_cog.success_criteria)
            except Exception:
                pass
            if _extraction_schema is not None and _model is not None:
                if hasattr(_model, "structured_extract"):
                    _structured_parsed = _model.structured_extract(
                        accumulated_text=_extract_text,
                        phase=eval_phase,
                        schema=_extraction_schema,
                        phase_hint=_phase_hint,
                    )
        except Exception as _se_exc:
            print(
                f"    [diagnostic] structured_extract error (non-fatal): {_se_exc}",
                flush=True,
            )

        # Plan-C: record structured_extract call in traj
        _extract_rec = getattr(_model, "_last_extract_record", None) if _model else None
        if _extract_rec is not None:
            try:
                result.pending_messages.append({
                    "role": "user",
                    "content": _extract_rec.extraction_prompt,
                    "extra": {
                        "type": "structured_extract_request",
                        "phase": eval_phase,
                        "schema_name": _extract_rec.schema_name,
                        "accumulated_text_chars": len(_accumulated) if _accumulated else 0,
                        "phase_hint": _extract_rec.phase_hint or "",
                        "timestamp": _extract_rec.timestamp_request,
                    },
                })
                result.pending_messages.append({
                    "role": "assistant",
                    "content": _extract_rec.response_raw or "",
                    "extra": {
                        "type": "structured_extract_response",
                        "phase": eval_phase,
                        "success": _extract_rec.success,
                        "fields": list((_extract_rec.response_parsed or {}).keys()),
                        "cost": _extract_rec.cost,
                        "error": _extract_rec.error,
                        "timestamp": _extract_rec.timestamp_response,
                    },
                })
            except Exception as _traj_exc:
                print(f"    [Plan-C] traj recording error (non-fatal): {_traj_exc}", flush=True)

        if _structured_parsed is not None:
            from declaration_extractor import extract_record_for_phase as _extract_for_phase
            _diagnostic_pr = build_phase_record_from_structured(
                _structured_parsed, str(old_phase)
            )
            if hasattr(state, "_extraction_structured"):
                state._extraction_structured += 1
            _acc_len = len(_accumulated) if _accumulated else 0
            print(
                f"    [diagnostic] method=structured"
                f" accumulated_chars={_acc_len}"
                f" fields={list(_structured_parsed.keys())}"
                f" DIAGNOSTIC_ONLY=true",
                flush=True,
            )
        else:
            try:
                from declaration_extractor import extract_record_for_phase as _extract_for_phase
                _diag_pr, _diag_declared, _diag_foreign = _extract_for_phase(
                    _extract_text, str(old_phase)
                )
                _diagnostic_pr = _diag_pr
            except Exception:
                pass
            if hasattr(state, "_extraction_no_schema") and _extraction_schema is None:
                state._extraction_no_schema += 1
            elif hasattr(state, "_extraction_regex_fallback"):
                state._extraction_regex_fallback += 1
            _acc_len = len(_accumulated) if _accumulated else 0
            print(
                f"    [diagnostic] method=regex"
                f" accumulated_chars={_acc_len}"
                f" DIAGNOSTIC_ONLY=true",
                flush=True,
            )

        if _diagnostic_pr is not None:
            if not hasattr(state, "diagnostic_phase_records"):
                state.diagnostic_phase_records = []
            state.diagnostic_phase_records.append(_diagnostic_pr)

        # C-09: extraction telemetry (uses diagnostic data, not admission data)
        try:
            _schema_fields: list[str] = []
            if _extraction_schema is not None:
                _schema_props = _extraction_schema.get("properties", {})
                _schema_fields = [k for k in _schema_props if k not in ("phase", "subtype")]

            _epo_record, _epo_meta = extract_phase_output(
                tool_submitted=None,  # already consumed by admission
                structured_parsed=_structured_parsed,
                agent_message=_extract_text,
                phase=str(old_phase),
                schema_fields=_schema_fields,
            )

            if not hasattr(state, "extraction_telemetry"):
                state.extraction_telemetry = {}
            state.extraction_telemetry[eval_phase] = {
                "extraction_source": _epo_meta.source,
                "schema_field_count": len(_epo_meta.fields_in_schema),
                "extracted_count": len(_epo_meta.fields_extracted),
                "missing_count": len(_epo_meta.fields_missing),
                "fields_extracted": _epo_meta.fields_extracted,
                "fields_missing": _epo_meta.fields_missing,
            }
            print(
                f"    [extraction] phase={eval_phase}"
                f" source={_epo_meta.source}"
                f" extracted={len(_epo_meta.fields_extracted)}"
                f" missing={len(_epo_meta.fields_missing)}",
                flush=True,
            )
        except Exception as _epo_exc:
            print(f"    [extraction] telemetry error (non-fatal): {_epo_exc}", flush=True)

    except Exception as _diag_exc:
        print(f"    [diagnostic] error (non-fatal): {_diag_exc}", flush=True)

    # ══════════════════════════════════════════════════════════════════
    # Gate 2: Phase Completion — defensive assertion
    # P0.1: Records are admitted at submit time. If evaluate_transition()
    # is called, admission should have already succeeded. This gate is
    # now a defensive assertion, not an active acquisition retry loop.
    # ══════════════════════════════════════════════════════════════════
    if _pr is None:
        # Should not happen post-admission — log and stop gracefully
        if hasattr(state, "_missing_submission_count"):
            state._missing_submission_count += 1
        _emit_decision(
            state, decision_type="gate_verdict", step_n=_cp_s.step_index,
            verdict="stop", reason="no_admitted_record_assertion_failure",
            signals={"phase": eval_phase},
        )
        print(
            f"    [phase_gate] ASSERTION FAILURE: evaluate_transition called"
            f" but no pre-admitted record found for {eval_phase}"
            f" — this should not happen post-admission; stopping",
            flush=True,
        )
        result.verdict = "stop"
        result.stop_reason = "no_admitted_record_assertion_failure"
        result.source = "assertion_failure"
        return result

    # Record available — set on result for downstream gates
    result.phase_record = _pr
    result.phase_record_source = _pr_source
    # Gate 3 (Cognition Validation) removed — already done in admit_phase_record()

    # ══════════════════════════════════════════════════════════════════
    # Gate 4-8: Phase-specific gates
    # ══════════════════════════════════════════════════════════════════

    # Helper: run a phase gate with policy-driven escalation
    def _run_phase_gate(
        gate_name: str,
        eval_fn,
        eval_args: dict,
        max_rejects: int,
        reject_counter_attr: str,
        phase_label: str,
    ) -> tuple[bool, bool]:  # (rejected, force_passed)
        """Run a phase-specific gate. Returns (rejected, force_passed).

        Phase 4.5: escalation behavior is driven by _REJECTION_POLICY.
        max_rejects is still the threshold count; the policy table determines
        what happens when that count is reached (stop vs force_advance).
        """
        try:
            _gate_verdict = eval_fn(**eval_args)
            _reject_count = getattr(state, reject_counter_attr, 0)
            print(
                f"    [{gate_name}] passed={_gate_verdict.passed}"
                f" failed_rules={_gate_verdict.failed_rules}"
                f" scores={_gate_verdict.scores}"
                f" rejects_so_far={_reject_count}",
                flush=True,
            )
            if not _gate_verdict.passed and _reject_count >= max_rejects:
                # Phase 4.5: consult policy for escalation verdict
                _reason_key = f"{phase_label.lower()}_gate_rejected"
                _esc_verdict, _esc_strategy, _esc_hint = lookup_rejection_policy(
                    _reason_key, _reject_count
                )
                if _esc_verdict == "stop":
                    _missing_rules = _gate_verdict.failed_rules or []
                    _emit_limit_triggered(
                        state, step_n=_cp_s.step_index,
                        limit_name=f"{gate_name}_exhausted",
                        configured_value=max_rejects, actual_value=_reject_count,
                        action_taken="admission_gate_stop",
                        source_file="step_sections.py", source_line=0,
                        reason=f"failed_rules={_gate_verdict.failed_rules} scores={_gate_verdict.scores}",
                    )
                    print(
                        f"    [{gate_name}] ADMISSION EXHAUSTED:"
                        f" rejects={_reject_count}/{max_rejects}"
                        f" failed_rules={_missing_rules}"
                        f" → fail-closed STOP (policy)",
                        flush=True,
                    )
                    result.verdict = "stop"
                    result.stop_reason = f"admission_gate_exhausted_{phase_label.lower()}"
                    result.source = "gate_rejection"
                    return (True, False)
                elif _esc_verdict == "force_advance":
                    print(
                        f"    [{gate_name}] FORCE_ADVANCE — max_rejects={max_rejects}"
                        f" reached, policy={_esc_strategy}, allowing advance",
                        flush=True,
                    )
                    _emit_limit_triggered(
                        state, step_n=_cp_s.step_index,
                        limit_name=f"{gate_name}_force_advance",
                        configured_value=max_rejects, actual_value=_reject_count,
                        action_taken="force_advance", source_file="step_sections.py", source_line=0,
                        reason=f"failed_rules={_gate_verdict.failed_rules} scores={_gate_verdict.scores}",
                    )
                    return (False, True)
            elif not _gate_verdict.passed:
                # REJECT: build feedback and return
                _sdg_repair_used = False
                if _SDG_ENABLED and getattr(_gate_verdict, "rejection", None):
                    try:
                        _sdg_content = _build_sdg_repair(_gate_verdict.rejection)
                        _sdg_content += f"\n\nFix only the failing fields. Do not rewrite fields already OK.\nStay in {phase_label} phase."
                        result.pending_messages.append({
                            "role": "user",
                            "content": _sdg_content,
                        })
                        _sdg_repair_used = True
                        print(f"    [{gate_name}] sdg_repair_used=true failures={len(_gate_verdict.rejection.failures)}", flush=True)
                    except Exception as _sdg_exc:
                        print(f"    [{gate_name}] sdg_repair error (fallback): {_sdg_exc}", flush=True)

                if not _sdg_repair_used:
                    _scores = _gate_verdict.scores
                    _pass_threshold = 0.5
                    _field_status = "\n".join(
                        f"- {k.upper()}: {'OK' if v >= _pass_threshold else 'MISSING'} (score={v:.1f})"
                        for k, v in _scores.items()
                    )
                    result.pending_messages.append({
                        "role": "user",
                        "content": (
                            f"[{gate_name} REJECT]\n"
                            f"{phase_label} gate result:\n"
                            f"{_field_status}\n\n"
                            f"Fix only the MISSING fields. Do not rewrite fields already OK.\n"
                            f"Stay in {phase_label} phase."
                        ),
                    })
                state.phase_records = [
                    r for r in state.phase_records
                    if r.phase.upper() != eval_phase
                ]
                if not hasattr(state, reject_counter_attr):
                    setattr(state, reject_counter_attr, 0)
                setattr(state, reject_counter_attr, getattr(state, reject_counter_attr, 0) + 1)
                _new_count = getattr(state, reject_counter_attr)
                print(f"    [{gate_name}] REJECT ({_new_count}/{max_rejects}) — redirecting to {phase_label}", flush=True)
                return (True, False)
        except Exception as _gate_exc:
            print(f"    [{gate_name}] error (non-fatal): {_gate_exc}", flush=True)
        return (False, False)

    # ── Analysis Gate ──
    _analysis_gate_rejected = False
    _analysis_gate_force_passed = False
    if eval_phase == "ANALYZE":
        from analysis_gate import evaluate_analysis as _eval_analysis
        _analysis_gate_rejected, _analysis_gate_force_passed = _run_phase_gate(
            gate_name="analysis_gate",
            eval_fn=_eval_analysis,
            eval_args={"phase_record": _pr, "structured_output": (_pr_source == "tool_submitted")},
            max_rejects=2,
            reject_counter_attr="analysis_gate_rejects",
            phase_label="ANALYZE",
        )
        if result.verdict == "stop":
            return result
        if _analysis_gate_rejected:
            result.source = "gate_rejection"
            result.reason = "analysis_gate_rejected"
            result.routing = _route_blocked(eval_phase, result.reason)
            # P1.1: redirect when routing says different phase, retry otherwise
            if result.routing.next_phase and result.routing.next_phase != eval_phase:
                result.verdict = "redirect"
                result.next_phase = result.routing.next_phase
            else:
                result.verdict = "retry"
            return result

    # ── Design Gate ──
    _design_gate_rejected = False
    _design_gate_force_passed = False
    if eval_phase == "DESIGN":
        from design_gate import evaluate_design as _eval_design
        _design_gate_rejected, _design_gate_force_passed = _run_phase_gate(
            gate_name="design_gate",
            eval_fn=_eval_design,
            eval_args={"phase_record": _pr},
            max_rejects=2,
            reject_counter_attr="design_gate_rejects",
            phase_label="DESIGN",
        )
        if result.verdict == "stop":
            return result
        if _design_gate_rejected:
            result.source = "gate_rejection"
            result.reason = "design_gate_rejected"
            result.routing = _route_blocked(eval_phase, result.reason)
            if result.routing.next_phase and result.routing.next_phase != eval_phase:
                result.verdict = "redirect"
                result.next_phase = result.routing.next_phase
            else:
                result.verdict = "retry"
            return result

    # ── Decide Gate ──
    _decide_gate_rejected = False
    _decide_gate_force_passed = False
    if eval_phase == "DECIDE":
        from decide_gate import evaluate_decide as _eval_decide
        _decide_gate_rejected, _decide_gate_force_passed = _run_phase_gate(
            gate_name="decide_gate",
            eval_fn=_eval_decide,
            eval_args={"phase_record": _pr},
            max_rejects=2,
            reject_counter_attr="decide_gate_rejects",
            phase_label="DECIDE",
        )
        if result.verdict == "stop":
            return result
        if _decide_gate_rejected:
            result.source = "gate_rejection"
            result.reason = "decide_gate_rejected"
            result.routing = _route_blocked(eval_phase, result.reason)
            if result.routing.next_phase and result.routing.next_phase != eval_phase:
                result.verdict = "redirect"
                result.next_phase = result.routing.next_phase
            else:
                result.verdict = "retry"
            return result

    # ── Execute Gate ──
    _execute_gate_rejected = False
    _execute_gate_force_passed = False
    if eval_phase == "EXECUTE":
        from execute_gate import evaluate_execute as _eval_execute
        _execute_gate_rejected, _execute_gate_force_passed = _run_phase_gate(
            gate_name="execute_gate",
            eval_fn=_eval_execute,
            eval_args={"phase_record": _pr},
            max_rejects=2,
            reject_counter_attr="execute_gate_rejects",
            phase_label="EXECUTE",
        )
        if result.verdict == "stop":
            return result
        if _execute_gate_rejected:
            result.source = "gate_rejection"
            result.reason = "execute_gate_rejected"
            result.routing = _route_blocked(eval_phase, result.reason)
            if result.routing.next_phase and result.routing.next_phase != eval_phase:
                result.verdict = "redirect"
                result.next_phase = result.routing.next_phase
            else:
                result.verdict = "retry"
            return result

    # ── Judge Gate ──
    _judge_gate_rejected = False
    _judge_gate_force_passed = False
    if eval_phase == "JUDGE":
        from judge_gate import evaluate_judge as _eval_judge
        _judge_gate_rejected, _judge_gate_force_passed = _run_phase_gate(
            gate_name="judge_gate",
            eval_fn=_eval_judge,
            eval_args={"phase_record": _pr},
            max_rejects=2,
            reject_counter_attr="judge_gate_rejects",
            phase_label="JUDGE",
        )
        if result.verdict == "stop":
            return result
        if _judge_gate_rejected:
            result.source = "gate_rejection"
            result.reason = "judge_gate_rejected"
            result.routing = _route_blocked(eval_phase, result.reason)
            if result.routing.next_phase and result.routing.next_phase != eval_phase:
                result.verdict = "redirect"
                result.next_phase = result.routing.next_phase
            else:
                result.verdict = "retry"
            return result

    # ══════════════════════════════════════════════════════════════════
    # Gate 9: Principal Gate
    # ══════════════════════════════════════════════════════════════════
    # Track which gate was force-advanced for audit
    _force_passed_gate = ""
    if _analysis_gate_force_passed:
        _force_passed_gate = "analysis_gate"
    elif _design_gate_force_passed:
        _force_passed_gate = "design_gate"
    elif _decide_gate_force_passed:
        _force_passed_gate = "decide_gate"
    elif _execute_gate_force_passed:
        _force_passed_gate = "execute_gate"
    elif _judge_gate_force_passed:
        _force_passed_gate = "judge_gate"
    _any_force_passed = (
        _analysis_gate_force_passed or _design_gate_force_passed
        or _decide_gate_force_passed or _execute_gate_force_passed
        or _judge_gate_force_passed
    )
    try:
        if _any_force_passed:
            raise RuntimeError("phase_gate FORCE_PASS, skipping principal gate to allow advance")
        if _pr is None:
            raise RuntimeError("phase_record unavailable, skipping principal gate")
        from principal_gate import (
            evaluate_admission as _eval_admission,
            get_principal_feedback as _get_pg_feedback,
        )
        from control.reasoning_state import set_principal_violation as _set_pv
        _obs_tool_sig = getattr(getattr(agent_self, "_jingu_monitor_state", None), "_observe_tool_signal", False)
        if eval_phase == "ANALYZE" and _pr is not None:
            _rc = getattr(_pr, "root_cause", "") or ""
            if _rc:
                state.last_analyze_root_cause = _rc
                print(f"    [phase_record] root_cause saved ({len(_rc)} chars)", flush=True)
            # P2: Save root_cause_location_files for scope consistency gate
            _rcf = getattr(_pr, "root_cause_location_files", None) or []
            if not _rcf and hasattr(_pr, "__dict__"):
                _rcf = _pr.__dict__.get("root_cause_location_files", []) or []
            if _rcf:
                state._analyze_root_cause_files = list(_rcf)
                print(f"    [phase_record] root_cause_location_files={_rcf}", flush=True)
            _rcs = getattr(_pr, "root_cause_scope_summary", None) or ""
            if not _rcs and hasattr(_pr, "__dict__"):
                _rcs = _pr.__dict__.get("root_cause_scope_summary", "") or ""
            if _rcs:
                state._analyze_scope_summary = _rcs
        _admission = _eval_admission(
            _pr, eval_phase,
            observe_tool_signal=_obs_tool_sig,
            last_analyze_root_cause=state.last_analyze_root_cause if eval_phase == "EXECUTE" else "",
            structured_output=(_pr_source == "tool_submitted"),
            loop_counts=state._retryable_loop_counts,
        )
        if _pr_foreign_phase:
            from canonical_symbols import ALL_PHASES as _phase_order_cs2
            _delta = abs(_phase_order_cs2.index(_pr_foreign_phase) - _phase_order_cs2.index(eval_phase)) if (_pr_foreign_phase in _phase_order_cs2 and eval_phase in _phase_order_cs2) else 0
            _foreign_reason = f"foreign_phase_declared:declared={_pr_foreign_phase},eval={eval_phase},delta={_delta}"
            _legacy = _admission.reasons_legacy
            if _foreign_reason not in _legacy:
                _admission.reasons.insert(0, _foreign_reason)
            _admission.reasons = [
                r for r in _admission.reasons
                if not (
                    (hasattr(r, "code") and r.code.startswith("MISSING_PRINCIPAL:"))
                    or (isinstance(r, str) and r.startswith("missing_required_principal"))
                )
            ]
            _non_foreign_reasons = [
                r for r in _admission.reasons
                if not (isinstance(r, str) and r == _foreign_reason)
            ]
            if not _non_foreign_reasons and _admission.status == "RETRYABLE":
                _admission.status = "ADMITTED"
        print(
            f"    [principal_gate] eval_phase={eval_phase} record_phase={_pr.phase}"
            f" admission={_admission.status} reasons={_admission.reasons_legacy}",
            flush=True,
        )
        # Telemetry
        if _admission.status == "ADMITTED":
            state._phase_record_admit_total += 1
        elif _admission.status == "ESCALATED":
            state._phase_record_admit_total += 1
            _esc = _admission.escalation
            if _esc:
                print(
                    f"    [principal_gate] ESCALATED:"
                    f" reason={_esc.reason.value} loop_key={_esc.loop_key}"
                    f" count={_esc.loop_count} action={_esc.action}"
                    f" bypassed={_esc.bypassed_principals}",
                    flush=True,
                )
                _emit_limit_triggered(
                    state, step_n=_cp_s.step_index,
                    limit_name=f"escalation_{_esc.reason.value}",
                    configured_value=_esc.loop_count, actual_value=_esc.loop_count,
                    action_taken=_esc.action, source_file="principal_gate.py", source_line=0,
                    reason=f"phase={_esc.loop_key[0]} violation={_esc.loop_key[1]}",
                )
                if _esc.loop_key in state._retryable_loop_counts:
                    state._retryable_loop_counts[_esc.loop_key] = 0
            else:
                print(
                    f"    [principal_gate] ESCALATED (no escalation info)",
                    flush=True,
                )
        elif _admission.status in ("RETRYABLE", "REJECTED"):
            state._phase_record_reject_total += 1

        if _admission.status in ("RETRYABLE", "REJECTED"):
            _pg_violation = _admission.reasons_legacy[0] if _admission.reasons else "admission_violation"
            _pg_feedback = _get_pg_feedback(_pg_violation)
            if getattr(_admission, "routing", None):
                _repair_phase = _admission.routing.next_phase
                _pg_guidance = "; ".join(_admission.routing.repair_hints) if _admission.routing.repair_hints else ""
            else:
                try:
                    from jingu_onboard import onboard as _onb_repair
                    _gov_repair = _onb_repair()
                    _route_obj = _gov_repair.get_route(str(_cp_s.phase), _pg_violation)
                    _repair_phase = _route_obj.next_phase if _route_obj else ""
                    _repair_hint = _gov_repair.get_repair_hint(str(_cp_s.phase), _pg_violation)
                    _pg_guidance = _repair_hint if _repair_hint else ""
                except Exception:
                    _repair_phase = ""
                    _pg_guidance = ""
            _repair_suffix = f" Repair phase: {_repair_phase}." if _repair_phase else ""
            _guidance_suffix = f" {_pg_guidance}" if _pg_guidance else ""
            if _SDG_ENABLED and getattr(_admission, "rejection", None):
                try:
                    _sdg_hint = _build_sdg_repair(_admission.rejection)
                    state.pending_redirect_hint = _sdg_hint
                    print(f"    [principal_gate] sdg_repair_used=true failures={len(_admission.rejection.failures)}", flush=True)
                except Exception as _sdg_exc:
                    print(f"    [principal_gate] sdg_repair error (fallback): {_sdg_exc}", flush=True)
                    state.pending_redirect_hint = (
                        f"[{_admission.status}:{_pg_violation}] {_pg_feedback}{_repair_suffix}{_guidance_suffix}"
                    )
            else:
                state.pending_redirect_hint = (
                    f"[{_admission.status}:{_pg_violation}] {_pg_feedback}{_repair_suffix}{_guidance_suffix}"
                )
            if is_data_driven_routing_enabled():
                try:
                    _p216_phase = eval_phase
                    _p216_principal = _pg_violation.split(":")[-1] if ":" in _pg_violation else _pg_violation
                    _p216_next, _p216_strategy = route_failure_p216(_p216_phase, _p216_principal)
                    _p216_prompt = get_strategy_prompt(_p216_strategy)
                    state.pending_redirect_hint = _p216_prompt + "\n\n" + state.pending_redirect_hint
                    print(
                        f"    [p216-routing] phase={_p216_phase} principal={_p216_principal}"
                        f" -> next={_p216_next} strategy={_p216_strategy}",
                        flush=True,
                    )
                except Exception as _p216_exc:
                    print(f"    [p216-routing] error (non-fatal): {_p216_exc}", flush=True)
            if cp_state_holder is not None:
                cp_state_holder[0] = _set_pv(cp_state_holder[0], _pg_violation)
                _cp_s = cp_state_holder[0]
            else:
                state.cp_state = _set_pv(state.cp_state, _pg_violation)
                _cp_s = state.cp_state

            if _admission.status == "REJECTED":
                _emit_decision(
                    state, decision_type="gate_verdict", step_n=_cp_s.step_index,
                    verdict="stop", reason="no_signal",
                    rule_violated=_admission.reasons_legacy[0] if _admission.reasons else None,
                )
                print(
                    f"    [principal_gate] REJECTED → VerdictStop"
                    f" reasons={_admission.reasons_legacy}",
                    flush=True,
                )
                result.verdict = "stop"
                result.stop_reason = "no_signal"
                result.source = "gate_rejection"
                result.reason = "principal_gate_rejected"
                return result
            else:
                # RETRYABLE
                state.phase_records = [
                    r for r in state.phase_records
                    if r.phase.upper() != eval_phase
                ]
                _loop_key = (eval_phase, _pg_violation)
                state._retryable_loop_counts[_loop_key] = (
                    state._retryable_loop_counts.get(_loop_key, 0) + 1
                )
                for _k in list(state._retryable_loop_counts):
                    if _k != _loop_key:
                        state._retryable_loop_counts[_k] = 0

                # Determine if principal gate triggers a redirect
                _pg_redirect_phase = ""
                if not state.early_stop_verdict:
                    _pv_verdict = decide_next(_cp_s)
                    print(
                        f"    [principal_gate] RETRYABLE → cognition_verdict={_pv_verdict.type}"
                        f" to={getattr(_pv_verdict, 'to', '')}",
                        flush=True,
                    )
                    if isinstance(_pv_verdict, VerdictRedirect):
                        _pg_redirect_phase = _pv_verdict.to
                        # P0.2: within-attempt routing enforcement
                        state.required_next_phase = _pg_redirect_phase
                        print(
                            f"    [routing-enforcement] principal gate redirect:"
                            f" required_next_phase={_pg_redirect_phase}",
                            flush=True,
                        )
                        result.pending_messages.append({
                            "role": "user",
                            "content": (
                                f"[Cognition gate RETRYABLE: {_pg_violation}] "
                                f"{_pg_feedback} "
                                f"{_pg_guidance} "
                                f"Return to phase {_pv_verdict.to} before proceeding."
                            ),
                        })
                        _model_redir = getattr(agent_self, "model", None)
                        if _model_redir is not None and hasattr(_model_redir, "set_force_phase_record"):
                            _model_redir.set_force_phase_record(True)
                        state.pending_redirect_hint = ""
                        result.pg_redirected = True

                # Phase 4: unified verdict — redirect or retry
                if _pg_redirect_phase:
                    result.verdict = "redirect"
                    result.next_phase = _pg_redirect_phase
                else:
                    result.verdict = "retry"
                result.source = "gate_rejection"
                result.reason = f"principal_gate_retryable:{_pg_violation}"
                # Telemetry: routing for strategy prompt selection
                if getattr(_admission, "routing", None):
                    result.routing = _admission.routing
                else:
                    result.routing = RoutingDecision(
                        next_phase=_repair_phase or eval_phase,
                        strategy="rethink_root_cause",
                        repair_hints=[_pg_guidance] if _pg_guidance else [],
                        source="principal_route",
                    )
                return result
    except Exception as _pg_exc:
        print(f"    [principal_gate] error={_pg_exc}", flush=True)

    # ══════════════════════════════════════════════════════════════════
    # Gate 10: Principal Inference (diagnostic + fake check)
    # ══════════════════════════════════════════════════════════════════
    try:
        if _any_force_passed:
            raise RuntimeError("phase_gate FORCE_PASS, skipping inference check")
        if _pr is None:
            raise RuntimeError("phase_record unavailable, skipping inference check")
        try:
            from principal_inference import run_inference as _run_inf
            from jingu_onboard import onboard as _onb_inf
            _gov_inf = _onb_inf()
            _inf_cfg = _gov_inf.get_phase_config(eval_phase)
            _inf_subtype = _inf_cfg.subtype if _inf_cfg else ""
            _inf_result = _run_inf(_pr, _inf_subtype)
            _inf_telem_parts = []
            for _pname, _pdetail in _inf_result.details.items():
                _inferred_flag = "\u2713" if _pname in _inf_result.present else "\u2717"
                _inf_telem_parts.append(
                    f"{_inferred_flag}{_pname}(score={_pdetail.score:.1f}"
                    f" signals={_pdetail.signals})"
                )
            print(
                f"    [principal_inference] subtype={_inf_subtype}"
                f" declared={[p.lower() for p in (_pr.principals or [])]}"
                f" inferred={_inf_result.present}",
                flush=True,
            )
            if _inf_telem_parts:
                print(
                    f"    [principal_inference] details: {' | '.join(_inf_telem_parts)}",
                    flush=True,
                )
        except Exception as _inf_telem_exc:
            print(f"    [principal_inference] telemetry error={_inf_telem_exc}", flush=True)
        from principal_gate import check_principal_inference as _check_pi
        _inf_violation = _check_pi(_pr, eval_phase)
        if _inf_violation and "fake_principal" in _inf_violation and state._bypassed_principals:
            _fake_names = [
                p.strip() for p in _inf_violation.split(":", 1)[1].split(",")
                if p.strip()
            ]
            _remaining = [p for p in _fake_names if p not in state._bypassed_principals]
            if _remaining:
                _inf_violation = f"fake_principal:{','.join(_remaining)}"
            else:
                _inf_violation = None
                print(
                    f"    [principal_inference] fake_principals_all_bypassed:"
                    f" bypassed={sorted(state._bypassed_principals)}",
                    flush=True,
                )
        if _inf_violation and "fake_principal" in _inf_violation:
            try:
                from jingu_onboard import onboard as _onb_inf_repair
                _gov_inf_repair = _onb_inf_repair()
                _inf_route = _gov_inf_repair.get_route(eval_phase, "fake_principal")
                _inf_repair = _inf_route.next_phase if _inf_route else eval_phase
                _inf_guidance = _gov_inf_repair.get_repair_hint(eval_phase, "fake_principal")
            except Exception:
                _inf_repair = ""
                _inf_guidance = ""
            _inf_repair_suffix = f" Repair phase: {_inf_repair}." if _inf_repair else ""

            # P1.4.b: Build actionable repair hint from inference signals
            _fake_detail_hints = []
            try:
                _fake_names_for_hint = [
                    p.strip() for p in _inf_violation.split(":", 1)[1].split(",")
                    if p.strip()
                ]
                for _fp_name in _fake_names_for_hint:
                    _fp_detail = _inf_result.details.get(_fp_name) if _inf_result else None
                    if _fp_detail and _fp_detail.explanation:
                        _fake_detail_hints.append(
                            f"  - {_fp_name}: {_fp_detail.explanation}"
                        )
            except Exception:
                pass
            _detail_block = ""
            if _fake_detail_hints:
                _detail_block = " Specific issues:\n" + "\n".join(_fake_detail_hints)

            state.pending_redirect_hint = (
                f"[RETRYABLE:{_inf_violation}] "
                f"Your declared principals are not supported by your reasoning. "
                f"Provide concrete evidence (file references, causal reasoning) "
                f"before declaring these principals.{_inf_repair_suffix} {_inf_guidance}"
                f"{_detail_block}"
            )
            print(
                f"    [principal_inference] FAKE_RETRYABLE: phase={eval_phase}"
                f" violation={_inf_violation} repair={_inf_repair}",
                flush=True,
            )
            _fl_result = _handle_fake_loop_check(state, eval_phase, _inf_violation)
            if _fl_result.action == "bypass":
                from routing_decision import EscalationReason, EscalationInfo
                _fi_esc = EscalationInfo(
                    reason=EscalationReason.FAKE_LOOP,
                    loop_key=(eval_phase, _inf_violation),
                    loop_count=_fl_result.loop_count,
                    action="selective_bypass",
                    bypassed_principals=_fl_result.bypassed_principals,
                )
                from principal_gate import _FAKE_LOOP_LIMIT
                _emit_limit_triggered(
                    state, step_n=_cp_s.step_index,
                    limit_name=f"escalation_{_fi_esc.reason.value}",
                    configured_value=_FAKE_LOOP_LIMIT, actual_value=_fl_result.loop_count,
                    action_taken=_fi_esc.action, source_file="principal_gate.py", source_line=0,
                    reason=f"phase={eval_phase} violation={_inf_violation} bypassed={_fi_esc.bypassed_principals}",
                )
                print(
                    f"    [principal_inference] ESCALATED(fake_loop):"
                    f" phase={eval_phase} violation={_inf_violation}"
                    f" count={_fl_result.loop_count} >= {_FAKE_LOOP_LIMIT}"
                    f" → bypassed_principals={_fi_esc.bypassed_principals}"
                    f" (selective bypass, record preserved, other principals still enforced)",
                    flush=True,
                )
            else:
                result.verdict = "retry"
                result.source = "gate_rejection"
                result.reason = f"fake_principal:{_inf_violation}"
                result.routing = RoutingDecision(
                    next_phase=eval_phase,
                    strategy="gather_code_evidence",
                    repair_hints=["Provide concrete evidence for declared principals."],
                    source="inference_route",
                )
                return result
        elif _inf_violation and "missing_required" in _inf_violation:
            pass
    except Exception as _pi_exc:
        print(f"    [principal_inference] check error={_pi_exc}", flush=True)

    # ══════════════════════════════════════════════════════════════════
    # Telemetry: diff_principals (diagnostic only)
    # ══════════════════════════════════════════════════════════════════
    try:
        if _pr is not None and not _any_force_passed:
            from principal_inference import run_inference, diff_principals
            from jingu_onboard import onboard as _onb_telem
            _gov_telem = _onb_telem()
            _pi_cfg = _gov_telem.get_phase_config(eval_phase)
            _pi_subtype = _pi_cfg.subtype if _pi_cfg else ""
            _inf_rich = run_inference(_pr, _pi_subtype)
            diff_principals(
                getattr(_pr, "principals", []) or [],
                _inf_rich,
                phase=eval_phase,
            )
    except Exception:
        pass

    # ══════════════════════════════════════════════════════════════════
    # ALL GATES PASSED — transition authorized
    # ══════════════════════════════════════════════════════════════════
    result.verdict = "advance"
    if _any_force_passed:
        result.source = "tolerance"
        result.reason = f"force_advance:{_force_passed_gate}"
        result.tolerated = True
        result.tolerated_gate = _force_passed_gate
    else:
        result.source = "admission"
        result.reason = "all_gates_passed"
    return result


# ── PR3: limit event unification helper ──────────────────────────────────────

def _emit_limit_triggered(
    state: "StepMonitorState",
    *,
    step_n: int,
    limit_name: str,
    configured_value: int | float,
    actual_value: int | float,
    action_taken: str,
    source_file: str,
    source_line: int,
    reason: str = "",
) -> None:
    """Emit to BOTH stdout ([limit-triggered] prefix) and decisions.jsonl."""
    print(
        f"    [limit-triggered] {limit_name}: configured={configured_value}"
        f" actual={actual_value} action={action_taken}"
        f" source={source_file}:{source_line}"
        f" reason={reason}",
        flush=True,
    )
    _emit_decision(
        state,
        decision_type="limit_triggered",
        step_n=step_n,
        verdict=action_taken,
        reason=f"{limit_name}: configured={configured_value} actual={actual_value} -- {reason}",
        signals={
            "limit_name": limit_name,
            "configured_value": configured_value,
            "actual_value": actual_value,
            "action_taken": action_taken,
            "source_file": source_file,
            "source_line": source_line,
        },
    )


# ── p230: decision provenance emission helper ────────────────────────────────

def _emit_decision(
    state: "StepMonitorState",
    *,
    decision_type: str,
    step_n: int,
    verdict: str,
    reason: str = "",
    rule_violated: str | None = None,
    signals: dict | None = None,
    phase_from: str | None = None,
    phase_to: str | None = None,
) -> None:
    """Emit a DecisionEvent to the attempt's decisions.jsonl. Never raises."""
    logger = getattr(state, "_decision_logger", None)
    if not logger:
        return
    try:
        from decision_logger import DecisionEvent
        logger.log(DecisionEvent(
            decision_type=decision_type,
            step_n=step_n,
            timestamp_ms=time.time() * 1000,
            verdict=verdict,
            rule_violated=rule_violated,
            signals_evaluated=signals,
            reason_text=reason,
            phase_from=phase_from,
            phase_to=phase_to,
        ))
    except Exception:
        pass  # logging must not crash the run


# ── Per-step structure validation (p207-P2) ──────────────────────────────────
# Required structured fields per phase, derived from phase_prompt.py templates.
PHASE_REQUIRED_FIELDS: dict[str, list[str]] = {
    "UNDERSTAND": ["PROBLEM_STATEMENT", "EXPECTED_BEHAVIOR", "ACTUAL_BEHAVIOR", "SCOPE"],
    "OBSERVE":    ["EVIDENCE"],
    # ANALYZE: removed — agent submits via submit_phase_record tool call, not text
    # sections. The analysis_gate checks the tool call fields (root_cause,
    # causal_chain). Checking for text markers here actively conflicts with the
    # tool-call path by injecting "write ROOT_CAUSE:" hints.
    # DECIDE/EXECUTE/JUDGE: removed — structural gates check these phases
}


# ═══════════════════════════════════════════════════════════════════════════════
# Section 1: _step_observe
# ═══════════════════════════════════════════════════════════════════════════════

def _step_observe(agent_self, *, step_n: int, mode: str) -> tuple[str, str, bool]:
    """
    Section 1: pure observation — extract text, run cognition parse, detect env mutation.

    Returns (latest_assistant_text, snippet, env_error_detected).
    Side-effects: appends cognition violation feedback as user message (non-fatal).
    """
    latest_assistant_text = ""
    snippet = ""
    for msg in reversed(agent_self.messages):
        if msg.get("role") == "assistant":
            # Plan-C: skip structured_extract traj entries
            if msg.get("extra", {}).get("type", "").startswith("structured_extract_"):
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        content = c["text"]
                        break
            if isinstance(content, str):
                latest_assistant_text = content
                snippet = content.replace("\n", " ")[:80]
            break

    _state_ref = getattr(agent_self, "_jingu_monitor_state", None)
    if _state_ref is not None and latest_assistant_text:
        if latest_assistant_text != _state_ref._last_assistant_text:
            _state_ref._llm_step += 1
            _state_ref._last_assistant_text = latest_assistant_text
            _state_ref._observe_tool_signal = False

    if _state_ref is not None:
        for _msg in reversed(agent_self.messages):
            if _msg.get("role") == "assistant":
                # Plan-C: skip structured_extract traj entries
                if _msg.get("extra", {}).get("type", "").startswith("structured_extract_"):
                    continue
                _tcs = _msg.get("tool_calls", [])
                if _tcs:
                    _state_ref._observe_tool_signal = True
                elif _msg.get("extra", {}).get("actions"):
                    _state_ref._observe_tool_signal = True
                break

    print(f"    [step {step_n}] ${agent_self.cost:.2f}  {snippet}", flush=True)

    if mode == "jingu" and latest_assistant_text:
        try:
            from cognition_schema import check_step_cognition, format_violation_feedback
            _cog_record, _cog_violations = check_step_cognition(latest_assistant_text, step_n=step_n)
            if _cog_record is not None:
                _principal_str = " ".join(_cog_record.principals) if _cog_record.principals else "(none)"
                print(
                    f"    [cognition] step={step_n} phase={_cog_record.phase}"
                    f" principals=[{_principal_str}]"
                    f" evidence={len(_cog_record.evidence_refs)}"
                    f" violations={len(_cog_violations)}",
                    flush=True,
                )
                if _cog_violations:
                    _feedback = format_violation_feedback(_cog_violations, _cog_record)
                    _viol_codes = ":".join(sorted(v.code if hasattr(v, "code") else str(v) for v in _cog_violations))
                    _llm_step_id = _state_ref._llm_step if _state_ref is not None else step_n
                    _cog_key = f"{_llm_step_id}:cognition_violation:{_viol_codes}"
                    if _state_ref is None or _cog_key not in _state_ref._injected_signals:
                        if _state_ref is not None:
                            _state_ref._injected_signals.add(_cog_key)
                        print(f"    [cognition] VIOLATION — injecting feedback", flush=True)
                        agent_self.messages.append({"role": "user", "content": _feedback})
                    else:
                        print(f"    [cognition] VIOLATION — skipped=dedup key={_cog_key}", flush=True)
        except Exception as _cog_exc:
            print(f"    [cognition] parse error (non-fatal): {_cog_exc}", flush=True)

    env_error_detected = False
    for msg in reversed(agent_self.messages):
        if msg.get("role") == "assistant":
            # Plan-C: skip structured_extract traj entries
            if msg.get("extra", {}).get("type", "").startswith("structured_extract_"):
                continue
            has_mut, trigger = _msg_has_env_mutation(msg)
            if has_mut:
                env_error_detected = True
                print(
                    f"    [env-mutation] ENVIRONMENT_MUTATION_IN_AGENT_LOOP "
                    f"step={step_n} trigger={trigger!r} — "
                    f"agent is doing env work (pip/conda/setup.py). "
                    f"This belongs to infrastructure, not agent reasoning.",
                    flush=True,
                )
            break

    return latest_assistant_text, snippet, env_error_detected


# ═══════════════════════════════════════════════════════════════════════════════
# Section 2: _step_verify_if_needed
# ═══════════════════════════════════════════════════════════════════════════════

def _step_verify_if_needed(
    agent_self,
    *,
    state: "StepMonitorState",
    verify_debounce_s: float,
    cp_state_holder: list | None = None,
) -> bool:
    """
    Section 2: patch signal detection + conditional quick judge dispatch.

    Returns step_patch_non_empty (True if agent has a real, non-empty patch).
    Side-effects: may run a synchronous quick judge (max 30s) and set
    state._pending_quick_judge_message for the caller to inject.
    """
    import hashlib as _hl
    import subprocess as _sp_iv

    step_patch_non_empty = False
    for msg in reversed(agent_self.messages):
        if msg.get("role") == "assistant":
            # Plan-C: skip structured_extract traj entries
            if msg.get("extra", {}).get("type", "").startswith("structured_extract_"):
                continue
            if not _msg_has_signal(msg):
                break
            step_patch_non_empty = True
            cid = state.container_id
            if not cid:
                break
            _base_commit = state.instance.get("base_commit", "HEAD")
            _git_diff_result = _sp_iv.run(
                ["docker", "exec", "-w", "/testbed", cid, "git", "diff", _base_commit],
                capture_output=True, text=True, timeout=30,
            )
            _raw_diff = _git_diff_result.stdout if _git_diff_result.returncode == 0 else ""
            current_patch = (_raw_diff.strip() + "\n") if _raw_diff.strip() else ""
            if not current_patch:
                step_patch_non_empty = False
                break

            # E1: Quick Judge — target-aware corrective signal
            patch_hash = _hl.md5(current_patch.encode()).hexdigest()[:16]
            # Pass real phase from cp_state_holder (state.cp_state may be stale)
            _real_phase = None
            if cp_state_holder:
                _real_phase = getattr(cp_state_holder[0], 'phase', None)
            if state.should_trigger_quick_judge(patch_hash, current_phase=_real_phase):
                try:
                    from quick_judge import run_quick_judge, format_agent_message, QuickJudgeResult

                    # Extract changed files from patch
                    changed_files = [
                        line[6:].strip()
                        for line in current_patch.splitlines()
                        if line.startswith("+++ b/")
                    ]

                    # Reconstruct previous result for direction comparison
                    prev_result = None
                    if state.quick_judge_history:
                        prev = state.quick_judge_history[-1]
                        prev_result = QuickJudgeResult(
                            step=prev.get("step", 0),
                            target_test_id=prev.get("target_test_id", ""),
                            target_status=prev.get("target_status", "unknown"),
                            tests_targeted=prev.get("tests_targeted", 0),
                            tests_passed=prev.get("tests_passed", 0),
                            tests_failed=prev.get("tests_failed", 0),
                            tests_error=prev.get("tests_error", 0),
                            failing_test_names=prev.get("failing_test_names", []),
                            elapsed_ms=prev.get("elapsed_ms", 0),
                            direction=prev.get("direction", "first_signal"),
                        )

                    # Canonical step identity: use state._llm_step (monotonic per step)
                    _canonical_step = state._llm_step
                    print(
                        f"    [quick-judge] triggering at step={_canonical_step} "
                        f"(patch changed, container={cid[:12]}...)",
                        flush=True,
                    )

                    qj_result = run_quick_judge(
                        patch=current_patch,
                        instance=state.instance,
                        container_id=cid,
                        changed_files=changed_files,
                        previous_result=prev_result,
                        step=_canonical_step,
                        priority_sentinel_tests=getattr(state, 'priority_sentinel_tests', None) or None,
                    )

                    # Record in telemetry (target-aware + sentinel fields)
                    state.record_quick_judge(_canonical_step, {
                        "step": _canonical_step,
                        "tier": "quick",
                        "trigger_source": "automatic_patch_detected",
                        "target_test_id": qj_result.target_test_id,
                        "target_status": qj_result.target_status,
                        "signal_kind": qj_result.signal_kind,
                        "corrective": qj_result.corrective,
                        "command_scope": qj_result.command_scope,
                        "tests_targeted": qj_result.tests_targeted,
                        "tests_passed": qj_result.tests_passed,
                        "tests_failed": qj_result.tests_failed,
                        "tests_error": qj_result.tests_error,
                        "failing_test_names": qj_result.failing_test_names,
                        "elapsed_ms": qj_result.elapsed_ms,
                        "direction": qj_result.direction,
                        "patch_hash": patch_hash,
                        "invoked": True,
                        "acknowledged": None,
                        "effective": None,
                        "sentinel_tests_run": qj_result.sentinel_tests_run,
                        "sentinel_tests_passed": qj_result.sentinel_tests_passed,
                        "sentinel_tests_failed": qj_result.sentinel_tests_failed,
                        "regression_detected": qj_result.regression_detected,
                        "regression_test_names": qj_result.regression_test_names,
                        "f2p_targeted": qj_result.f2p_targeted,
                        "f2p_passed": qj_result.f2p_passed,
                        "f2p_failed": qj_result.f2p_failed,
                        "f2p_coverage": qj_result.f2p_coverage,
                    })

                    # Format message for agent injection (consumed by jingu_agent.py)
                    state._pending_quick_judge_message = format_agent_message(qj_result)

                    # P0.3: QJ result triggers EXECUTE → JUDGE advance
                    if qj_result.target_status in ("pass", "fail"):
                        state._qj_advance_ready = True
                        print(
                            f"    [qj-advance] QJ result available:"
                            f" target_status={qj_result.target_status}"
                            f" → EXECUTE->JUDGE trigger armed",
                            flush=True,
                        )

                except Exception as _qj_exc:
                    print(f"    [quick-judge] ERROR (non-fatal): {_qj_exc}", flush=True)

            break

    return step_patch_non_empty


# ═══════════════════════════════════════════════════════════════════════════════
# Section 3: _step_cp_update_and_verdict
# ═══════════════════════════════════════════════════════════════════════════════

def _step_cp_update_and_verdict(
    agent_self,
    *,
    state: "StepMonitorState",
    cp_state_holder: "list | None",
    env_error_detected: bool,
    step_patch_non_empty: bool,
    latest_assistant_text: str,
) -> None:
    """
    Section 3: control-plane state update + verdict decision + verdict actions.

    Side-effects:
    - Updates cp_state / cp_state_holder[0] via update_cp_with_step_signals
    - Sets state.early_stop_verdict + agent n_calls on VerdictStop
    - Sets state.pending_redirect_hint on VerdictRedirect
    - Advances cp_state phase + appends phase_records on VerdictAdvance
    - Logs per-step cp telemetry
    """
    _pee, _pee_reason = state.update_cp_with_step_signals(
        env_error_detected=env_error_detected,
        patch_non_empty=step_patch_non_empty,
        cp_state_holder=cp_state_holder,
    )
    _cp_s = cp_state_holder[0] if cp_state_holder is not None else state.cp_state
    _step_signals_present = bool(env_error_detected or step_patch_non_empty)
    _weak_progress = extract_weak_progress(
        env_error_detected=env_error_detected,
        patch_non_empty=step_patch_non_empty,
        latest_tests_passed=state.latest_tests_passed(),
    )
    if _step_signals_present or _weak_progress:
        _pee_str = f"True({_pee_reason})" if _pee else "False"
        print(
            f"    [cp-step] instance={state.instance_id} attempt={state.attempt}"
            f" signals={[k for k,v in [('env',env_error_detected),('patch',step_patch_non_empty)] if v]}"
            f" no_progress:{_cp_s.no_progress_steps} step:{_cp_s.step_index}"
            f" env_noise:{_cp_s.env_noise} actionability:{_cp_s.actionability}"
            f" weak_progress:{_weak_progress} pee:{_pee_str}",
            flush=True,
        )

    # ══════════════════════════════════════════════════════════════════
    # Phase Submission Enforcement — Checkpoint Escalation
    #
    # Four-level escalation for agents that don't call submit_phase_record:
    #   Level 1 (soft):     reminder message
    #   Level 2 (hard):     warning message + force armed (continuous)
    #   Level 3 (terminal): protocol_violation → STOP
    #
    # Phase-specific deadlines (steps before hard enforcement):
    #   OBSERVE=15, ANALYZE=12, DECIDE=8, EXECUTE=10, DESIGN=10
    # Resets when agent submits a phase record or phase changes.
    # ══════════════════════════════════════════════════════════════════
    _current_phase_str = str(_cp_s.phase).upper()
    _PHASE_DEADLINES = {
        "UNDERSTAND": 8, "OBSERVE": 15, "ANALYZE": 12, "DECIDE": 8,
        "DESIGN": 10, "EXECUTE": 10, "JUDGE": 8,
    }
    _DEFAULT_DEADLINE = 12
    _phase_deadline = _PHASE_DEADLINES.get(_current_phase_str, _DEFAULT_DEADLINE)
    _CHECKPOINT_SOFT = max(5, _phase_deadline - 3)   # 3 steps before deadline
    _CHECKPOINT_HARD = _phase_deadline                # deadline = continuous force
    _CHECKPOINT_TERMINAL = _phase_deadline + 3        # deadline + 3 = STOP

    # Detect phase change → reset counter
    if _current_phase_str != state._last_submission_phase:
        state._steps_without_submission = 0
        state._submission_escalation_level = 0
        state._last_submission_phase = _current_phase_str

    # Check if agent submitted a phase record this step (peek, don't consume)
    _model_peek = getattr(agent_self, "model", None)
    _has_pending_submission = (
        _model_peek is not None
        and hasattr(_model_peek, "_submitted_phase_record")
        and _model_peek._submitted_phase_record is not None
    )
    # P0.1: immediate admission — initialized before branch so it's always available
    _admission_result = None

    if _has_pending_submission:
        state._steps_without_submission = 0
        state._submission_escalation_level = 0

        # P0.1: immediate admission — validate and store at submit time
        _admission_result = admit_phase_record(
            agent_self,
            state=state,
            cp_state_holder=cp_state_holder,
            eval_phase=_current_phase_str,
            old_phase=_current_phase_str,
            latest_assistant_text=latest_assistant_text,
        )
        if _admission_result.stop:
            state.early_stop_verdict = VerdictStop(reason=_admission_result.stop_reason)
            raise StopExecution(_admission_result.stop_reason)
        elif not _admission_result.admitted:
            for _retry_msg in _admission_result.retry_messages:
                agent_self.messages.append(_retry_msg)
            print(f"    [immediate-admission] REJECTED phase={_current_phase_str}", flush=True)
        else:
            state._last_admitted_phase = _current_phase_str
            print(f"    [immediate-admission] ADMITTED phase={_current_phase_str}"
                  f" source={_admission_result.source}", flush=True)

            # ── L4: Phase Lifecycle — protocol-driven routing (ANALYZE-only) ──
            # When ANALYZE admission succeeds, use route_from_phase_result()
            # instead of decide_next() heuristic. This makes phase completion
            # and routing a single protocol-driven path.
            if _current_phase_str == "ANALYZE":
                try:
                    from phase_lifecycle import build_phase_result_from_admission

                    # Build the admitted record dict from tool submission
                    _admitted_dict = None
                    if _model_peek is not None and hasattr(_model_peek, "_submitted_phase_record"):
                        # Record was already consumed by admit_phase_record.
                        # Use the last phase_record from state (just appended).
                        _last_pr = state.phase_records[-1] if state.phase_records else None
                        if _last_pr is not None and hasattr(_last_pr, "phase") and _last_pr.phase.upper() == "ANALYZE":
                            _admitted_dict = _last_pr.as_dict() if hasattr(_last_pr, "as_dict") else vars(_last_pr)

                    _phase_result = build_phase_result_from_admission(
                        phase="ANALYZE",
                        admitted_record=_admitted_dict,
                        admission_source=_admission_result.source,
                    )
                    _routing = _phase_result.routing

                    if _routing is not None:
                        if _routing.retry_current:
                            # Protocol says retry — inject hint
                            agent_self.messages.append({
                                "role": "user",
                                "content": f"[PROTOCOL ROUTING] {_routing.retry_hint}",
                            })
                            print(
                                f"    [phase-lifecycle] ANALYZE retry:"
                                f" reason={_routing.reason}",
                                flush=True,
                            )
                        elif _routing.next_phase and _routing.source == "protocol":
                            # Protocol-driven advance: store on state for verdict override
                            state._protocol_next_phase = _routing.next_phase
                            state._protocol_routing_reason = _routing.reason
                            print(
                                f"    [phase-lifecycle] ANALYZE → {_routing.next_phase}"
                                f" source=protocol reason={_routing.reason}",
                                flush=True,
                            )
                except Exception as _pl_exc:
                    print(f"    [phase-lifecycle] error (non-fatal): {_pl_exc}", flush=True)
    else:
        state._steps_without_submission += 1

    # Escalation logic (4 levels: none → soft → hard → terminal)
    if _current_phase_str in ("UNDERSTAND",):
        pass  # Skip enforcement for UNDERSTAND phase
    elif state._steps_without_submission >= _CHECKPOINT_TERMINAL:
        # Level 3: TERMINAL — protocol violation → STOP
        if state._submission_escalation_level < 3:
            state._submission_escalation_level = 3
            state.early_stop_verdict = VerdictStop(
                reason=f"step_governance_timeout_{_current_phase_str.lower()}",
            )
            print(
                f"    [step-governance] TERMINAL: phase={_current_phase_str}"
                f" steps_without_submission={state._steps_without_submission}"
                f" deadline={_phase_deadline} → STOP",
                flush=True,
            )
    elif state._steps_without_submission >= _CHECKPOINT_HARD:
        # Level 2: HARD — warning (once) + continuous force (every step)
        if state._submission_escalation_level < 2:
            state._submission_escalation_level = 2
            agent_self.messages.append({
                "role": "user",
                "content": (
                    f"[PHASE CHECKPOINT — HARD WARNING]\n"
                    f"You have been in {_current_phase_str} for {state._steps_without_submission} steps "
                    f"without submitting a phase record.\n"
                    f"The system REQUIRES you to call submit_phase_record to complete this phase.\n"
                    f"Your next response MUST include a submit_phase_record call with your "
                    f"{_current_phase_str} findings. You will not be able to use other tools "
                    f"until you submit."
                ),
            })
            print(
                f"    [phase_submission_enforcement] CHECKPOINT HARD:"
                f" phase={_current_phase_str} steps={state._steps_without_submission}"
                f" deadline={_phase_deadline} → force armed + warning injected",
                flush=True,
            )
        # Re-arm force on EVERY step after deadline (continuous lock-out)
        if _model_peek is not None and hasattr(_model_peek, "set_force_phase_record"):
            _model_peek.set_force_phase_record(True)
            state._phase_record_force_total += 1
    elif state._steps_without_submission >= _CHECKPOINT_SOFT:
        # Level 1: SOFT — reminder (once)
        if state._submission_escalation_level < 1:
            state._submission_escalation_level = 1
            agent_self.messages.append({
                "role": "user",
                "content": (
                    f"[PHASE CHECKPOINT — REMINDER]\n"
                    f"You have been in {_current_phase_str} for {state._steps_without_submission} steps. "
                    f"Remember to call submit_phase_record when you have enough findings. "
                    f"Phase completion requires a submitted record."
                ),
            })
            print(
                f"    [phase_submission_enforcement] CHECKPOINT SOFT:"
                f" phase={_current_phase_str} steps={state._steps_without_submission}"
                f" → reminder injected",
                flush=True,
            )

    _step_verdict = decide_next(_cp_s)

    # ── P1-min: Repeated patch detection (within-attempt) ────────────
    # If agent writes the same patch content 3+ times in one attempt,
    # override verdict to VerdictStop(repeated_patch). This prevents
    # "write same patch → verify fails → write same patch" loops that
    # burn steps without progress.
    _REPEATED_PATCH_LIMIT = 3
    if step_patch_non_empty and hasattr(agent_self, "model"):
        try:
            _env = getattr(agent_self, "environment", None)
            if _env is not None:
                _diff = _env.communicate("cd /testbed && git diff 2>/dev/null || true")
                if _diff and _diff.strip():
                    import hashlib
                    _ph = hashlib.md5(_diff.strip().encode()).hexdigest()[:12]
                    state._patch_hash_counts[_ph] = state._patch_hash_counts.get(_ph, 0) + 1
                    _ph_count = state._patch_hash_counts[_ph]
                    if _ph_count >= _REPEATED_PATCH_LIMIT:
                        print(
                            f"    [p1-min] repeated_patch: hash={_ph}"
                            f" count={_ph_count} limit={_REPEATED_PATCH_LIMIT}"
                            f" → VerdictStop(repeated_patch)",
                            flush=True,
                        )
                        _step_verdict = VerdictStop(reason="repeated_patch", source="repeated_patch")
                    elif _ph_count >= 2:
                        print(
                            f"    [p1-min] repeated_patch warning: hash={_ph}"
                            f" count={_ph_count}/{_REPEATED_PATCH_LIMIT}",
                            flush=True,
                        )
                    # P2: Extract patch_files deterministically from git diff
                    _patch_files = []
                    try:
                        _diff_names = _env.communicate(
                            "cd /testbed && git diff --name-only 2>/dev/null || true"
                        )
                        if _diff_names and _diff_names.strip():
                            _patch_files = [
                                f.strip() for f in _diff_names.strip().split("\n")
                                if f.strip()
                            ]
                            state._last_patch_files = _patch_files
                    except Exception:
                        pass
        except Exception:
            pass  # non-critical — don't crash on hash check failure

    # ── P2: Scope consistency gate (ANALYZE→EXECUTE file-scope) ───────
    # If ANALYZE declared root_cause_location_files AND EXECUTE has a patch,
    # check for zero overlap. Zero overlap = execution_scope_drift → RETRYABLE.
    # First violation → route to DECIDE. Repeated → route to ANALYZE.
    _P2_SCOPE_GATE_MAX = 2  # max scope drift violations before giving up
    if (step_patch_non_empty
            and _current_phase_str == "EXECUTE"
            and state._analyze_root_cause_files
            and state._last_patch_files
            and not isinstance(_step_verdict, VerdictStop)):
        try:
            # Normalize: strip leading paths to get relative file paths
            def _norm_file(f: str) -> str:
                """Normalize to bare relative path (strip /testbed/ prefix)."""
                f = f.strip()
                for _prefix in ("/testbed/", "a/", "b/"):
                    if f.startswith(_prefix):
                        f = f[len(_prefix):]
                return f

            _analyze_files_norm = {_norm_file(f) for f in state._analyze_root_cause_files}
            _patch_files_norm = {_norm_file(f) for f in state._last_patch_files}

            _overlap = _analyze_files_norm & _patch_files_norm
            if not _overlap:
                # Zero overlap → scope drift detected
                state._scope_drift_count += 1
                _drift_n = state._scope_drift_count
                print(
                    f"    [P2-scope-gate] DRIFT DETECTED ({_drift_n}/{_P2_SCOPE_GATE_MAX}):"
                    f" analyze_files={sorted(_analyze_files_norm)}"
                    f" patch_files={sorted(_patch_files_norm)}"
                    f" overlap=NONE",
                    flush=True,
                )
                if _drift_n <= _P2_SCOPE_GATE_MAX:
                    # Route back: first → DECIDE, repeated → ANALYZE
                    import dataclasses as _dc_p2
                    _target_phase = "DECIDE" if _drift_n == 1 else "ANALYZE"
                    _cp_p2 = cp_state_holder[0] if cp_state_holder else state.cp_state
                    _cp_p2_new = _dc_p2.replace(
                        _cp_p2, phase=_target_phase, no_progress_steps=0
                    )
                    if cp_state_holder:
                        cp_state_holder[0] = _cp_p2_new
                    else:
                        state.cp_state = _cp_p2_new
                    state._execute_entry_step = -1

                    _repair_hint = (
                        f"[SCOPE DRIFT] Your patch modifies {sorted(_patch_files_norm)} "
                        f"but ANALYZE identified root cause in {sorted(_analyze_files_norm)}. "
                        f"Either: (1) revise the patch to target the analyzed files, or "
                        f"(2) go back to ANALYZE and update root_cause_location_files "
                        f"with evidence justifying a different scope."
                    )
                    agent_self.messages.append({
                        "role": "user",
                        "content": _repair_hint,
                    })
                    _step_verdict = VerdictContinue()
                    print(
                        f"    [P2-scope-gate] → routed to {_target_phase}",
                        flush=True,
                    )
            else:
                if state._scope_drift_count > 0:
                    print(
                        f"    [P2-scope-gate] OK: overlap={sorted(_overlap)}"
                        f" (previous drift count={state._scope_drift_count})",
                        flush=True,
                    )
        except Exception as _p2_exc:
            print(f"    [P2-scope-gate] error (non-fatal): {_p2_exc}", flush=True)

    # ── Phase 2: Submission-triggered advance ─────────────────────────
    # P0.1: Now driven by immediate admission result, not raw submission peek.
    # Only advance if the record was actually admitted (Gates 1+3 passed).
    # EXECUTE excluded: EXECUTE→JUDGE driven by verify signal (task_success).
    # UNDERSTAND excluded: no contract, no submission expected.
    from canonical_symbols import ALL_PHASES as _all_ph
    _IMMEDIATE_ADVANCE_PHASES = frozenset(_all_ph) - {"UNDERSTAND", "EXECUTE", "JUDGE"}
    if (isinstance(_step_verdict, VerdictContinue)
            and _admission_result is not None
            and _admission_result.admitted
            and _current_phase_str in _IMMEDIATE_ADVANCE_PHASES):

        # L4: ANALYZE uses protocol-driven routing (phase_lifecycle)
        _protocol_next = getattr(state, "_protocol_next_phase", None)
        if _current_phase_str == "ANALYZE" and _protocol_next:
            _protocol_reason = getattr(state, "_protocol_routing_reason", "")
            _step_verdict = VerdictAdvance(
                to=_protocol_next, source="protocol", reason=_protocol_reason,
            )
            # Clear one-shot protocol routing state
            state._protocol_next_phase = None
            state._protocol_routing_reason = ""
            print(
                f"    [admission-advance] PROTOCOL-DRIVEN:"
                f" phase={_current_phase_str} to={_protocol_next}"
                f" reason={_protocol_reason}",
                flush=True,
            )
        else:
            # Non-ANALYZE phases: existing heuristic advance
            from control.reasoning_state import _ADVANCE_TABLE as _adv_tbl
            _submission_next = _adv_tbl.get(_current_phase_str)
            if _submission_next is not None:
                _step_verdict = VerdictAdvance(to=_submission_next, source="admission", reason="submission_triggered")
                print(
                    f"    [admission-advance] submission-triggered:"
                    f" phase={_current_phase_str} to={_submission_next}",
                    flush=True,
                )

    # ── Phase 2b-pre: P1.3' EXECUTE → ANALYZE redirect on wrong direction ──
    # Before QJ-triggered EXECUTE→JUDGE, check if QJ signals show wrong
    # patch direction. If so, redirect to ANALYZE for re-analysis instead
    # of advancing to JUDGE with a wrong patch.
    # Safety: max 1 redirect per attempt (detect_qj_wrong_direction checks flag).
    if (isinstance(_step_verdict, VerdictContinue)
            and _current_phase_str == "EXECUTE"):
        _wd_redirect, _wd_reason = state.detect_qj_wrong_direction()
        if _wd_redirect:
            state._execute_analyze_redirect_used = True
            state._qj_advance_ready = False  # cancel any pending QJ→JUDGE advance
            _step_verdict = VerdictRedirect(
                to="ANALYZE", source="qj_wrong_direction",
                reason="execute_wrong_direction",
            )
            # Set routing enforcement so agent MUST submit ANALYZE record
            state.required_next_phase = "ANALYZE"
            print(
                f"    [P1.3'] EXECUTE→ANALYZE redirect triggered:"
                f" {_wd_reason}",
                flush=True,
            )

    # ── Phase 2b: QJ-triggered EXECUTE → JUDGE advance (P0.3) ───────
    # When quick-judge returns target_status pass/fail, arm the flag.
    # Consume it here to trigger EXECUTE → JUDGE transition.
    if (isinstance(_step_verdict, VerdictContinue)
            and _current_phase_str == "EXECUTE"
            and state._qj_advance_ready):
        _step_verdict = VerdictAdvance(to="JUDGE", source="quick_judge", reason="qj_result_available")
        state._qj_advance_ready = False
        print(f"    [qj-advance] EXECUTE->JUDGE triggered by quick judge result", flush=True)

    # ── RC-1: Fail-closed admission ──────────────────────────────────
    # VerdictAdvance requires an admitted phase record. If agent hasn't
    # submitted one yet, suppress VerdictAdvance → VerdictContinue.
    # This prevents the "advance attempted → no record → retry loop"
    # pattern that caused phase_records_count=0 across all unresolved cases.
    if isinstance(_step_verdict, VerdictAdvance) and not _has_pending_submission:
        print(
            f"    [rc1-admission] VerdictAdvance suppressed:"
            f" phase={_current_phase_str} to={_step_verdict.to}"
            f" reason=no_pending_phase_record"
            f" steps_without_submission={state._steps_without_submission}",
            flush=True,
        )
        _step_verdict = VerdictContinue(source="default", reason="rc1_no_submission")

    _verdict_source = getattr(_step_verdict, "source", "unknown")
    _verdict_to_log = f"step={_cp_s.step_index} verdict={_step_verdict.type} source={_verdict_source}"
    if hasattr(_step_verdict, "to") and _step_verdict.to is not None:
        _verdict_to_log += f" to={_step_verdict.to}"
    if hasattr(_step_verdict, "reason") and _step_verdict.reason:
        _verdict_to_log += f" reason={_step_verdict.reason}"
    print(f"    [cp] {_verdict_to_log}", flush=True)

    if isinstance(_step_verdict, VerdictStop):
        state.early_stop_verdict = _step_verdict
        _emit_decision(
            state, decision_type="gate_verdict", step_n=_cp_s.step_index,
            verdict="stop", reason=getattr(_step_verdict, "reason", ""),
        )
        print(
            f"    [cp] VerdictStop enforcement: raising StopExecution({_step_verdict.reason})"
            f" — immediate interrupt, no phase injection",
            flush=True,
        )
        raise StopExecution(_step_verdict.reason)

    elif isinstance(_step_verdict, VerdictRedirect):
        _emit_decision(
            state, decision_type="gate_verdict", step_n=_cp_s.step_index,
            verdict="redirect", reason=getattr(_step_verdict, "reason", ""),
            phase_to=getattr(_step_verdict, "to", None),
        )
        _EXECUTE_REDIRECT_LIMIT = 3
        if _step_verdict.reason == "execute_no_progress":
            _exec_key = ("EXECUTE", "execute_no_progress")
            state._retryable_loop_counts[_exec_key] = (
                state._retryable_loop_counts.get(_exec_key, 0) + 1
            )
            _exec_redirect_count = state._retryable_loop_counts[_exec_key]
            print(
                f"    [cp] execute_no_progress_redirect count={_exec_redirect_count}"
                f" limit={_EXECUTE_REDIRECT_LIMIT}",
                flush=True,
            )
            if _exec_redirect_count > _EXECUTE_REDIRECT_LIMIT:
                _emit_limit_triggered(
                    state, step_n=_cp_s.step_index,
                    limit_name="execute_redirect_limit",
                    configured_value=_EXECUTE_REDIRECT_LIMIT, actual_value=_exec_redirect_count,
                    action_taken="stop", source_file="step_sections.py", source_line=379,
                    reason="execute_no_progress_loop_exceeded",
                )
                _emit_decision(
                    state, decision_type="gate_verdict", step_n=_cp_s.step_index,
                    verdict="stop", reason="execute_no_progress_loop_exceeded",
                    signals={"redirect_count": _exec_redirect_count, "limit": _EXECUTE_REDIRECT_LIMIT},
                )
                print(
                    f"    [cp] execute_no_progress loop exceeded limit={_EXECUTE_REDIRECT_LIMIT}"
                    f" → VerdictStop(no_signal) [attempt-terminal, will retry]",
                    flush=True,
                )
                state.early_stop_verdict = VerdictStop(reason="no_signal")
                raise StopExecution("no_signal")
        else:
            _exec_key = ("EXECUTE", "execute_no_progress")
            state._retryable_loop_counts[_exec_key] = 0

        state.pending_redirect_hint = f"[REDIRECT:{_step_verdict.to}] {_step_verdict.reason}"
        # Phase Submission Enforcement: enriched redirect with cognition context
        _redirect_content = ""
        if _step_verdict.reason == "execute_wrong_direction":
            # P1.3': EXECUTE→ANALYZE redirect — patch direction wrong
            _last_rc = state.last_analyze_root_cause or ""
            _qj_summary = ""
            if state.quick_judge_history:
                _recent_qj = state.quick_judge_history[-1]
                _qj_summary = (
                    f" Quick judge shows target_status={_recent_qj.get('target_status', '?')},"
                    f" direction={_recent_qj.get('direction', '?')}."
                )
            _redirect_content = (
                f"[EXECUTE → ANALYZE: WRONG PATCH DIRECTION]\n"
                f"Your patch is not fixing the target test.{_qj_summary}\n"
                f"The target test still fails after multiple patch iterations.\n\n"
                f"Your previous root cause analysis may be incorrect or incomplete.\n"
                f"Return to ANALYZE phase and:\n"
                f"1. Re-read the failing test to understand EXACTLY what it expects\n"
                f"2. Identify what your patch is NOT addressing\n"
                f"3. Consider alternative root causes\n"
                f"4. Submit a new ANALYZE record with revised root_cause"
            )
            if _last_rc:
                _redirect_content += (
                    f"\n\nYour previous root cause was: \"{_last_rc[:200]}\"\n"
                    f"This analysis led to a patch that does not pass the test. Revise it."
                )
        elif _step_verdict.reason == "execute_no_progress":
            _last_rc = state.last_analyze_root_cause or ""
            _rc_hint = ""
            if _last_rc:
                _rc_hint = (
                    f" Your analysis identified: \"{_last_rc[:200]}\". "
                    f"Edit the specific file/function from your analysis directly. "
                    f"If your analysis was wrong, return to ANALYZE with new evidence."
                )
            _exec_steps = state._steps_without_submission
            _redirect_content = (
                f"[EXECUTE STALL — NO PROGRESS]\n"
                f"You have spent {_exec_steps} steps in EXECUTE without writing any file changes.\n"
                f"You must either:\n"
                f"1. Write a code change NOW (use the bash tool to edit a file)\n"
                f"2. If you don't know what to change, return to ANALYZE{_rc_hint}"
            )
        else:
            _redirect_content = (
                f"[Control-plane redirect: {_step_verdict.reason}] "
                f"Re-examine your environment assumptions. "
                f"Transition to phase {_step_verdict.to} before patching."
            )
        agent_self.messages.append({
            "role": "user",
            "content": _redirect_content,
        })
        state.pending_redirect_hint = ""

    elif isinstance(_step_verdict, VerdictAdvance):
        # ══════════════════════════════════════════════════════════════════
        # Phase 3: Advance handler — ONLY consumes evaluate_transition() result.
        # No gate logic here. No re-decision. Only state commit + effects.
        # ══════════════════════════════════════════════════════════════════
        _emit_decision(
            state, decision_type="gate_verdict", step_n=_cp_s.step_index,
            verdict="advance", reason=getattr(_step_verdict, "reason", ""),
            phase_to=getattr(_step_verdict, "to", None),
        )
        _old_phase = _cp_s.phase
        _new_phase = _step_verdict.to
        _eval_phase = str(_old_phase).upper()

        # ── L4 Invariant: ANALYZE protocol completeness check ────────────
        # When advancing FROM ANALYZE, verify admitted record has valid
        # repair_strategy_type. This is the runtime invariant that catches
        # any residual bypass of the protocol.
        _routing_source = getattr(_step_verdict, "source", "unknown")
        if _eval_phase == "ANALYZE":
            _analyze_protocol_ok = False
            _analyze_strategy = ""
            for _pr_check in reversed(state.phase_records):
                if hasattr(_pr_check, "phase") and _pr_check.phase.upper() == "ANALYZE":
                    _rst = getattr(_pr_check, "repair_strategy_type", "") or ""
                    if _rst.strip():
                        _analyze_protocol_ok = True
                        _analyze_strategy = _rst.strip()
                    break
            if _analyze_protocol_ok:
                print(
                    f"    [protocol-invariant] ANALYZE→{_new_phase} OK:"
                    f" strategy={_analyze_strategy}"
                    f" routing_source={_routing_source}",
                    flush=True,
                )
            else:
                import os as _os_inv
                _strict = _os_inv.environ.get("JINGU_PROTOCOL_STRICT", "").lower() in ("1", "true", "yes")
                print(
                    f"    [protocol-invariant] WARNING: ANALYZE→{_new_phase}"
                    f" WITHOUT valid repair_strategy_type!"
                    f" routing_source={_routing_source}"
                    f" strict={_strict}",
                    flush=True,
                )
                if _strict:
                    raise RuntimeError(
                        f"[PROTOCOL INVARIANT VIOLATION] Advancing from ANALYZE"
                        f" to {_new_phase} without repair_strategy_type."
                        f" routing_source={_routing_source}"
                    )
        else:
            print(
                f"    [protocol-invariant] {_eval_phase}→{_new_phase}"
                f" routing_source={_routing_source}",
                flush=True,
            )

        try:
            from declaration_extractor import _extract_phase_from_message as _epfm, _PHASE_NORM as _pnorm
            _adv_declared_raw = _epfm(latest_assistant_text)
            _adv_declared = _pnorm.get(_adv_declared_raw, _adv_declared_raw) if _adv_declared_raw else "none"
        except Exception:
            _adv_declared = "unknown"
        print(
            f"    [cp] phase_advance from={_old_phase} to={_step_verdict.to}"
            f" agent_declared={_adv_declared}"
            f" routing_source={_routing_source}",
            flush=True,
        )
        _emit_decision(
            state, decision_type="phase_advance", step_n=_cp_s.step_index,
            verdict="advance", phase_from=str(_old_phase), phase_to=str(_step_verdict.to),
        )

        # ── Phase 3: evaluate_transition() is the SOLE transition authority ──
        _transition = evaluate_transition(
            agent_self,
            state=state,
            cp_state_holder=cp_state_holder,
            eval_phase=_eval_phase,
            old_phase=str(_old_phase),
            latest_assistant_text=latest_assistant_text,
        )

        # ── Phase 4: Apply transition result — verdict-driven, single authority ──
        # Inject pending messages from gate evaluation
        for _msg in _transition.pending_messages:
            agent_self.messages.append(_msg)

        _tv = _transition.verdict  # the ONE control signal
        _rt = _transition.routing  # telemetry only

        if _tv == "stop":
            state.early_stop_verdict = VerdictStop(reason=_transition.stop_reason)
            raise StopExecution(_transition.stop_reason)

        elif _tv == "advance":
            import dataclasses as _dc_adv
            if cp_state_holder is not None:
                cp_state_holder[0] = _dc_adv.replace(
                    cp_state_holder[0], phase=_new_phase, no_progress_steps=0, phase_steps=0
                )
            else:
                state.cp_state = _dc_adv.replace(
                    state.cp_state, phase=_new_phase, no_progress_steps=0, phase_steps=0
                )
            if _transition.tolerated:
                # Governance exception: advance despite gate failure
                if not hasattr(state, "_force_advance_count"):
                    state._force_advance_count = {}
                _fa_gate = _transition.tolerated_gate
                state._force_advance_count[_fa_gate] = (
                    state._force_advance_count.get(_fa_gate, 0) + 1
                )
                print(
                    f"    [Plan-A] phase_advance TOLERATED: {_old_phase} → {_new_phase}"
                    f" gate={_fa_gate} (governance exception — gate rejected but policy allowed advance)",
                    flush=True,
                )
                _emit_decision(
                    state, decision_type="gate_verdict", step_n=_cp_s.step_index,
                    verdict="tolerated_advance",
                    reason=_transition.reason,
                    signals={"tolerated_gate": _fa_gate, "force_advance_counts": dict(state._force_advance_count)},
                )
            else:
                print(
                    f"    [Plan-A] phase_advance COMMITTED: {_old_phase} → {_new_phase}",
                    flush=True,
                )

        elif _tv == "redirect":
            import dataclasses as _dc_redir
            _redir_target = _transition.next_phase
            if cp_state_holder is not None:
                cp_state_holder[0] = _dc_redir.replace(
                    cp_state_holder[0], phase=_redir_target, phase_steps=0
                )
            else:
                state.cp_state = _dc_redir.replace(
                    state.cp_state, phase=_redir_target, phase_steps=0
                )
            # P1.1: inject redirect reason + repair hints into agent context
            _redir_hints = "; ".join(_rt.repair_hints) if _rt and _rt.repair_hints else ""
            _redir_msg = (
                f"[GATE REDIRECT: {_transition.reason}]\n"
                f"Your {_eval_phase} submission did not pass the quality gate.\n"
                f"The system is redirecting you to {_redir_target} phase."
            )
            if _redir_hints:
                _redir_msg += f"\nRepair guidance: {_redir_hints}"
            agent_self.messages.append({"role": "user", "content": _redir_msg})
            print(
                f"    [Plan-A] phase_advance BLOCKED + REDIRECT:"
                f" {_old_phase} → {_redir_target}"
                f" source={_transition.source} reason={_transition.reason}"
                f" routing={_rt.strategy if _rt else 'none'}",
                flush=True,
            )
            _emit_decision(
                state, decision_type="gate_redirect", step_n=_cp_s.step_index,
                verdict="redirect",
                reason=_transition.reason,
                phase_from=_old_phase, phase_to=_redir_target,
                signals={
                    "source": _transition.source,
                    "strategy": _rt.strategy if _rt else None,
                    "repair_hints": _rt.repair_hints if _rt else [],
                },
            )

        else:
            # verdict == "retry" — blocked, stay in current phase
            # P1.1: inject repair hints into agent context so retry is informed
            _retry_hints = "; ".join(_rt.repair_hints) if _rt and _rt.repair_hints else ""
            _retry_msg = (
                f"[GATE RETRY: {_transition.reason}]\n"
                f"Your {_eval_phase} submission did not pass the quality gate.\n"
                f"Stay in {_eval_phase} phase and resubmit with improvements."
            )
            if _retry_hints:
                _retry_msg += f"\nRepair guidance: {_retry_hints}"
            agent_self.messages.append({"role": "user", "content": _retry_msg})
            print(
                f"    [Plan-A] phase_advance BLOCKED: {_old_phase} → {_new_phase}"
                f" source={_transition.source} reason={_transition.reason}"
                f" routing={_rt.strategy if _rt else 'none'}"
                f" route_to={_rt.next_phase if _rt else _eval_phase}",
                flush=True,
            )
            _emit_decision(
                state, decision_type="gate_retry", step_n=_cp_s.step_index,
                verdict="retry",
                reason=_transition.reason,
                phase_from=_eval_phase, phase_to=_eval_phase,
                signals={
                    "source": _transition.source,
                    "strategy": _rt.strategy if _rt else None,
                    "repair_hints": _rt.repair_hints if _rt else [],
                },
            )



# ═══════════════════════════════════════════════════════════════════════════════
# Section 4: _step_check_structure
# ═══════════════════════════════════════════════════════════════════════════════

def _step_check_structure(
    agent_self,
    *,
    cp_state_holder: "list | None",
    state: "StepMonitorState",
    latest_assistant_text: str,
) -> None:
    """
    p207-P2: per-step structure validation with correction hints.

    After each agent step, check if the current phase's required structured fields
    are present in the agent's latest output. If missing, inject a SOFT correction
    hint into the next step's context.

    Design decisions:
    - Check ONLY the current phase's required fields (not all fields)
    - Correction is a HINT (soft), not a REJECT (hard)
    - Only inject correction ONCE per missing field per attempt (don't spam)
    - Uses state._injected_signals with key "structure:<FIELD>" for dedup
    - Log format: [structure-check] phase=X missing=FIELD injected=true|dedup

    Does not block execution. Exception-safe — failure must not crash main flow.
    """
    try:
        _cp_s = cp_state_holder[0] if cp_state_holder is not None else state.cp_state
        _phase = str(_cp_s.phase).upper()

        required = PHASE_REQUIRED_FIELDS.get(_phase)
        if not required:
            return

        if not latest_assistant_text:
            return

        missing: list[str] = []
        for field in required:
            if f"{field}:" not in latest_assistant_text and f"{field.lower()}:" not in latest_assistant_text:
                _dedup_key = f"structure:{field}"
                if _dedup_key not in state._injected_signals:
                    missing.append(field)

        if not missing:
            print(f"    [structure-check] phase={_phase} all_present=true", flush=True)
            return

        _MANDATORY_FIELDS = {
            ("ANALYZE", "ROOT_CAUSE"): (
                "WARNING: Your analysis MUST include ROOT_CAUSE: with a specific file and line. "
                "Without this, your fix will be unfocused."
            ),
            ("EXECUTE", "PLAN"): (
                "WARNING: Your execution MUST include PLAN: listing specific changes. "
                "Without this, your patch may be incomplete."
            ),
        }
        _missing_str = ", ".join(missing)
        _hint_parts = [
            f"[STRUCTURE HINT] Your {_phase} output is missing required fields: {_missing_str}.",
        ]
        _has_mandatory = False
        for field in missing:
            _mandatory_msg = _MANDATORY_FIELDS.get((_phase, field))
            if _mandatory_msg:
                _hint_parts.append(_mandatory_msg)
                _has_mandatory = True
            else:
                _hint_parts.append(f"  {field}:")
                _hint_parts.append(f"  <your {field.lower().replace('_', ' ')} here>")
        if not _has_mandatory:
            _hint_parts.insert(1, "Please include these fields in your next response using the format:")

        _hint = "\n".join(_hint_parts)

        agent_self.messages.append({"role": "user", "content": _hint})
        for field in missing:
            _dedup_key = f"structure:{field}"
            state._injected_signals.add(_dedup_key)
            print(f"    [structure-check] phase={_phase} missing={field} injected=true", flush=True)

    except Exception as _exc:
        print(f"    [structure-check] error (non-fatal): {_exc}", flush=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Section 5: _step_inject_phase
# ═══════════════════════════════════════════════════════════════════════════════

def _step_inject_phase(agent_self, *, cp_state_holder: "list | None", state: "StepMonitorState") -> None:
    """
    Section p189: inject current phase as a user message prefix.
    Also consumes state.pending_redirect_hint — any hint set during this step
    is injected now so the agent sees it at the start of the next step.
    Exception-safe — injection failure must not crash main flow.
    """
    try:
        _hint = state.pending_redirect_hint
        if _hint:
            agent_self.messages.append({"role": "user", "content": _hint})
            print(f"    [phase_injection] redirect_hint injected=true", flush=True)
            state.pending_redirect_hint = ""
    except Exception:
        pass

    try:
        from phase_prompt import build_phase_prefix as _build_phase_prefix
        _cp_s = cp_state_holder[0] if cp_state_holder is not None else state.cp_state
        _phase_str = str(_cp_s.phase)
        # P1.3' fix: when required_next_phase is set (e.g. EXECUTE->ANALYZE redirect),
        # inject the TARGET phase's prompt, not the current phase's prompt.
        # Without this, agent gets contradictory signals: redirect says "go to ANALYZE"
        # but prompt says "write the patch" (EXECUTE).
        if state.required_next_phase is not None:
            _phase_str = state.required_next_phase.upper()
        _phase_prefix = _build_phase_prefix(_phase_str)
        if _phase_prefix:
            _phase_content = _phase_prefix.rstrip("\n")
            _phase_key = f"{state._llm_step}:phase_prefix:{_phase_str}"
            if _phase_key not in state._injected_signals:
                state._injected_signals.add(_phase_key)
                agent_self.messages.append({"role": "user", "content": _phase_content})
                print(f"    [phase_injection] phase={_phase_str} injected=true", flush=True)
            else:
                print(f"    [phase_injection] phase={_phase_str} skipped=dedup", flush=True)
    except Exception as _phase_exc:
        print(f"    [phase_injection] error (non-fatal): {_phase_exc}", flush=True)

    # P2: Inject ANALYZE-confirmed file scope into DECIDE/EXECUTE prompts
    try:
        _cp_p2inj = cp_state_holder[0] if cp_state_holder is not None else state.cp_state
        _phase_p2inj = str(_cp_p2inj.phase).upper()
        if state.required_next_phase is not None:
            _phase_p2inj = state.required_next_phase.upper()
        if _phase_p2inj in ("DECIDE", "EXECUTE", "DESIGN") and state._analyze_root_cause_files:
            _scope_key = f"{state._llm_step}:scope_constraint:{_phase_p2inj}"
            if _scope_key not in state._injected_signals:
                state._injected_signals.add(_scope_key)
                _files_str = "\n".join(f"  - {f}" for f in state._analyze_root_cause_files)
                _scope_msg = (
                    f"[ANALYZE-confirmed root-cause files]\n{_files_str}\n"
                )
                if state._analyze_scope_summary:
                    _scope_msg += f"Scope: {state._analyze_scope_summary}\n"
                _scope_msg += (
                    "\nYou must keep the patch consistent with this scope. "
                    "If you believe additional files are necessary, explicitly "
                    "justify them with new evidence before submitting."
                )
                agent_self.messages.append({"role": "user", "content": _scope_msg})
                print(
                    f"    [P2-scope-inject] phase={_phase_p2inj}"
                    f" files={state._analyze_root_cause_files}",
                    flush=True,
                )
    except Exception as _p2_inj_exc:
        print(f"    [P2-scope-inject] error (non-fatal): {_p2_inj_exc}", flush=True)

    # Plan-B: set current phase + schema on model for submit_phase_record tool
    try:
        _model = getattr(agent_self, "model", None)
        if _model is not None and hasattr(_model, "set_current_phase"):
            _cp_s_b = cp_state_holder[0] if cp_state_holder is not None else state.cp_state
            _phase_b = str(_cp_s_b.phase).upper()
            # P1.3' fix: same override for plan-b schema injection
            if state.required_next_phase is not None:
                _phase_b = state.required_next_phase.upper()
            _schema_b = None
            try:
                from jingu_onboard import onboard as _onboard_b
                _gov_b = _onboard_b()
                _schema_b = _gov_b.get_constrained_schema(_phase_b)
            except Exception:
                pass
            _model.set_current_phase(_phase_b, _schema_b)
            print(
                f"    [plan-b] set_current_phase={_phase_b}"
                f" schema_available={_schema_b is not None}",
                flush=True,
            )
    except Exception as _pb_exc:
        print(f"    [plan-b] set_current_phase error (non-fatal): {_pb_exc}", flush=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Section 6: _check_materialization_gate
# ═══════════════════════════════════════════════════════════════════════════════

def _check_materialization_gate(
    agent_self,
    *,
    cp_state_holder: "list | None",
    state: "StepMonitorState",
    patch_non_empty: bool,
) -> None:
    """
    p25 Materialization Gate Layer 1 (in-loop liveness, K=2):
    Once EXECUTE phase is entered, agent MUST write a patch within 2 steps.
    If no write happens in K steps, inject a strong forcing hint.

    Also runs retroactive analysis gate on first EXECUTE entry.
    Exception-safe — failure must not crash main flow.
    """
    _mat_phase = str((cp_state_holder[0] if cp_state_holder else state.cp_state).phase).upper()
    _mat_step = getattr(agent_self, "n_calls", -1)
    if _mat_phase == "EXECUTE":
        if state._execute_entry_step < 0:
            state._execute_entry_step = _mat_step
            state._execute_write_seen = False
            print(f"    [mat-gate] EXECUTE entered at step={_mat_step}", flush=True)

            # Retroactive analysis gate
            _retro_ag_max = 2
            _retro_ag_count = state.analysis_gate_rejects
            _last_analyze_pr = next(
                (r for r in reversed(state.phase_records)
                 if getattr(r, 'phase', '').upper() == 'ANALYZE'),
                None,
            )
            if _last_analyze_pr is not None:
                try:
                    from analysis_gate import evaluate_analysis as _retro_eval
                    _retro_verdict = _retro_eval(_last_analyze_pr)
                    print(
                        f"    [mat-gate] retroactive_analysis_gate"
                        f" passed={_retro_verdict.passed}"
                        f" failed_rules={_retro_verdict.failed_rules}"
                        f" scores={_retro_verdict.scores}"
                        f" rejects_so_far={_retro_ag_count}",
                        flush=True,
                    )
                    if not _retro_verdict.passed and _retro_ag_count < _retro_ag_max:
                        import dataclasses as _dc_retro
                        _cp_ref = cp_state_holder[0] if cp_state_holder else state.cp_state
                        _cp_ref_new = _dc_retro.replace(
                            _cp_ref, phase="ANALYZE", no_progress_steps=0
                        )
                        if cp_state_holder:
                            cp_state_holder[0] = _cp_ref_new
                        else:
                            state.cp_state = _cp_ref_new
                        state._execute_entry_step = -1
                        state.analysis_gate_rejects += 1

                        _retro_feedback_injected = False
                        if _SDG_ENABLED and getattr(_retro_verdict, "rejection", None):
                            try:
                                _retro_sdg = _build_sdg_repair(_retro_verdict.rejection)
                                _retro_sdg += (
                                    "\n\nFix only the failing fields. Do not rewrite fields already OK."
                                    "\nStay in ANALYZE phase."
                                )
                                agent_self.messages.append({"role": "user", "content": _retro_sdg})
                                _retro_feedback_injected = True
                                print(
                                    f"    [mat-gate] retroactive_analysis_gate"
                                    f" sdg_repair_used=true"
                                    f" failures={len(_retro_verdict.rejection.failures)}",
                                    flush=True,
                                )
                            except Exception as _retro_sdg_exc:
                                print(
                                    f"    [mat-gate] retroactive_analysis_gate"
                                    f" sdg_repair error: {_retro_sdg_exc}",
                                    flush=True,
                                )

                        if not _retro_feedback_injected:
                            _retro_reasons = "; ".join(_retro_verdict.reasons)
                            agent_self.messages.append({
                                "role": "user",
                                "content": (
                                    f"[analysis_gate REJECT — retroactive check at EXECUTE entry]\n"
                                    f"Your ANALYZE phase output did not pass quality checks:\n"
                                    f"{_retro_reasons}\n\n"
                                    f"Return to ANALYZE and address the failing checks before proceeding to EXECUTE."
                                ),
                            })

                        print(
                            f"    [mat-gate] retroactive_analysis_gate REJECT"
                            f" ({state.analysis_gate_rejects}/{_retro_ag_max})"
                            f" — redirecting to ANALYZE",
                            flush=True,
                        )
                    elif not _retro_verdict.passed:
                        print(
                            f"    [mat-gate] retroactive_analysis_gate FORCE_PASS"
                            f" — max_rejects={_retro_ag_max} reached",
                            flush=True,
                        )
                except Exception as _retro_exc:
                    print(
                        f"    [mat-gate] retroactive_analysis_gate error (non-fatal):"
                        f" {_retro_exc}",
                        flush=True,
                    )
        if patch_non_empty:
            state._execute_write_seen = True
        _MAT_GATE_K = 2
        _steps_since_entry = _mat_step - state._execute_entry_step
        if _steps_since_entry >= _MAT_GATE_K and not state._execute_write_seen:
            _emit_decision(
                state, decision_type="materialization_gate", step_n=_mat_step,
                verdict="force",
                reason=f"no_patch_after_{_steps_since_entry}_steps_in_EXECUTE",
                signals={"steps_since_entry": _steps_since_entry, "K": _MAT_GATE_K},
            )
            print(
                f"    [mat-gate] FORCE: {_steps_since_entry} steps in EXECUTE, no patch written",
                flush=True,
            )
            agent_self.messages.append({
                "role": "user",
                "content": (
                    "[MATERIALIZATION GATE] You have been in the EXECUTE phase for "
                    f"{_steps_since_entry} steps without writing any code. "
                    "You MUST write a patch NOW. "
                    "Do not read more files. Do not analyze further. "
                    "Write the fix to the file immediately using str_replace or write_file."
                ),
            })
    else:
        if state._execute_entry_step >= 0:
            state._execute_entry_step = -1
            state._execute_write_seen = False
