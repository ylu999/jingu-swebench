# Task: Governance Activation — Stage 1 (Structured Output for ANALYZE)

## Design Doc
`docs/design-governance-activation.md` (v2)

## Goal
Eliminate Pattern A failures (4/12 unresolved — protocol_violation_missing_phase_record)
and improve Pattern C (5/12 — shallow analysis passes gate) by making the agent
reliably submit structured phase records.

## Critical Finding from Code Analysis

The `submit_phase_record` tool is ALREADY registered on every query (JinguModel._query()
always adds it alongside BASH_TOOL). The agent sees it. But:

- 10999 attempt_1: 75 steps, OBSERVE the entire time, `phase_records_count=0` until step 41
- Agent CAN submit (it did at step 41 for OBSERVE), but is inconsistent
- Agent never submitted ANALYZE record → attempt ended with wrong patch
- 10999 attempt_2: 2 steps in ANALYZE, 0 phase records → protocol violation → STOP

**Root cause: agent prefers bash over submit_phase_record. The tool is optional (no
tool_choice forcing), and the agent treats it as low priority.**

## Architecture Understanding

```
JinguModel._query():
  tools = [BASH_TOOL, submit_phase_record_tool]  # BOTH always present
  # No tool_choice forcing — agent picks freely
  # Agent almost always picks bash

JinguModel._parse_actions():
  - submit_phase_record calls → stored in self._submitted_phase_record
  - bash calls → returned as normal actions
  - Agent can call BOTH in same step

step_sections.py:
  _tool_submitted = model.pop_submitted_phase_record()
  if _tool_submitted:
    _pr = build_phase_record_from_structured(...)  # ADMITTED
  else:
    # No admitted record → gate can't evaluate → eventually protocol violation
```

## Implementation Plan

### Change 1: Phase-boundary tool_choice forcing

At phase evaluation points (when system detects agent should transition), force
`tool_choice` to `submit_phase_record` for the NEXT query.

This is NOT forcing it on every step (that would prevent bash usage). Instead:
- Agent uses bash freely during exploration
- When the system's phase heuristic suggests transition time (e.g., enough steps
  in OBSERVE, agent has written analysis text), the NEXT step forces tool_choice
- Agent MUST submit phase record, then returns to free tool choice

**Implementation approach**: Add a flag on JinguModel:

```python
class JinguModel(LitellmModel):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._force_phase_record_next = False  # Set by step_sections

    def set_force_phase_record(self, force: bool = True):
        self._force_phase_record_next = force

    def _query(self, messages, **kwargs):
        # ... build tools as before ...
        extra_kwargs = {}
        if self._force_phase_record_next and phase_tool:
            extra_kwargs["tool_choice"] = {
                "type": "function",
                "function": {"name": "submit_phase_record"}
            }
            self._force_phase_record_next = False  # One-shot

        return litellm.completion(
            model=self.config.model_name,
            messages=messages,
            tools=tools,
            **(self.config.model_kwargs | kwargs | extra_kwargs),
        )
```

**Trigger conditions** (in step_sections.py):
- Protocol violation retry: already tells agent to submit, now also force tool_choice
- Phase heuristic: after N steps in a phase without submission, force next step
- Post-redirect: after any redirect, force submission on the re-entry step

### Change 2: Reduce phase_record_submission wait (quick win)

Current: system waits until phase boundary heuristic triggers, THEN starts
3-retry countdown. With 75 steps in OBSERVE, the boundary detection is too late.

Add an earlier nudge: after MAX_STEPS_WITHOUT_RECORD steps in any phase,
inject a reminder + set force flag:

```python
_MAX_STEPS_WITHOUT_RECORD = 15  # ~15 steps without submitting → nudge
if steps_in_phase >= _MAX_STEPS_WITHOUT_RECORD and not _has_record_for_phase:
    model.set_force_phase_record(True)
    agent_self.messages.append({
        "role": "user",
        "content": (
            f"[PHASE CHECKPOINT] You have been in {phase} for {steps_in_phase} steps "
            f"without submitting a phase record. Call submit_phase_record now with your "
            f"current findings. You can continue working after submission."
        ),
    })
```

### Change 3: Remove analysis_gate force_pass

Replace force_pass with redirect to OBSERVE:

```python
# In step_sections.py around line 866:
if not _analysis_verdict.passed and _ag_reject_count >= _AG_MAX_REJECTS:
    _observe_redirects = getattr(state, '_analysis_observe_redirects', 0)
    if _observe_redirects < 2:
        state._analysis_observe_redirects = _observe_redirects + 1
        # Redirect to OBSERVE with specific missing signals
        _missing = _analysis_verdict.failed_rules
        agent_self.messages.append({
            "role": "user",
            "content": (
                f"[ANALYSIS INCOMPLETE] Your analysis failed these checks: {_missing}. "
                f"Return to OBSERVE and gather more evidence. Specifically look for:\n"
                + ("\n".join(f"- {r}" for r in _missing)) +
                f"\nThen re-enter ANALYZE with stronger evidence."
            ),
        })
        # Reset phase to OBSERVE
        import dataclasses as _dc
        _cp_ref = cp_state_holder[0] if cp_state_holder else state.cp_state
        cp_new = _dc.replace(_cp_ref, phase="OBSERVE", no_progress_steps=0)
        if cp_state_holder:
            cp_state_holder[0] = cp_new
        else:
            state.cp_state = cp_new
        state._execute_entry_step = -1
        _analysis_gate_rejected = True
    else:
        # After 2 observe redirects, force_pass with warning
        _analysis_gate_force_passed = True
```

### Change 4: Enrich execute_no_progress redirect

Include root_cause from last ANALYZE record:

```python
# In execute_no_progress redirect:
_last_rc = state.last_analyze_root_cause or ""
_hint_extra = ""
if _last_rc:
    _hint_extra = f" Your analysis identified: {_last_rc[:200]}. Edit that specific location."
redirect_hint = (
    f"execute_no_progress: {steps} steps in EXECUTE without writing a file.{_hint_extra}"
)
```

## Files to Modify

| File | Change | Risk |
|------|--------|------|
| `mini-swe-agent/jingu_model.py` | Add `set_force_phase_record()` + tool_choice forcing | Medium — changes LLM query |
| `scripts/step_sections.py` | Phase checkpoint nudge; force_pass → redirect; enriched hints | Medium — core control flow |

## Smoke Test Plan

1. Build image with changes
2. Run django__django-11095 (known resolved) — verify no regression
3. Run django__django-10999 (Pattern A at ANALYZE) — verify:
   - Agent submits ANALYZE phase record (no protocol violation)
   - Record contains root_cause, evidence
4. Run django__django-11292 (Pattern C, force_pass) — verify:
   - Analysis gate detects weak analysis
   - Redirects to OBSERVE instead of force_pass
   - Agent gathers more evidence before re-entering ANALYZE

## Open Question (resolved)

Q: Does `submit_phase_record` coexist with bash tools?
A: YES. `JinguModel._query()` always provides `[BASH_TOOL, phase_tool]`. Agent
can call both in the same step. `_parse_actions()` intercepts submit_phase_record
and returns only bash actions for execution. No conflict.

Q: Why doesn't the agent call it?
A: No `tool_choice` forcing. Agent defaults to bash. The prompt says "call
submit_phase_record" but the model treats it as a suggestion, not a requirement.
Fix: force tool_choice at key moments (protocol retry, phase checkpoint).
