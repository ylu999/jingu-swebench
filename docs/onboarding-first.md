# PRINCIPLE: Official Onboarding Before Adaptation

**Domain:** jingu-swebench / SWE-bench harness
**Scope:** applies before any modification to run_with_jingu_gate.py, test commands, or environment setup

---

## Definition

When interacting with any external system (benchmark, harness, library, environment),
the system MUST establish the official execution model BEFORE performing local adaptation.

```
onboarding(system) = complete
→ then: implementation allowed

onboarding(system) = incomplete
→ then: REJECT — output ONBOARDING_REQUIRED
```

---

## Required Conditions (onboarding is complete iff ALL are true)

| Condition | Check |
|-----------|-------|
| Official documentation identified | README + eval section read |
| Execution path confirmed | `MAP_REPO_VERSION_TO_SPECS[repo][version]["test_cmd"]` verified |
| Environment setup confirmed | `conda activate testbed` confirmed from harness |
| Evaluation method confirmed | Official harness path confirmed, not invented |
| Differences from prior assumptions explicitly listed | stated before coding |

---

## Failure Types

| Code | Description |
|------|-------------|
| `ONBOARDING_SKIPPED` | Implementation started without completing onboarding |
| `OFFICIAL_PATH_NOT_CONFIRMED` | Repo or version not found in official specs |
| `ASSUMED_ENV_BEHAVIOR` | Used `python` without verifying it's the testbed env |
| `OLD_EXPERIENCE_TRANSFER` | Applied pytest/pip-install patterns to a conda+runtests.py system |
| `CUSTOM_PATH_INVENTED` | Wrote custom test command without reading harness |
| `HARNESS_NOT_AVAILABLE` | `swebench.harness` import failed — harness not installed |

---

## Code Enforcement

`_check_onboarding(instance)` in `run_with_jingu_gate.py`:
- Verifies `repo` + `version` exist in `MAP_REPO_VERSION_TO_SPECS`
- Verifies `_build_test_command(instance)` produces a valid harness command
- Verifies `FAIL_TO_PASS` is defined

`_build_execution_model(instance)` in `run_with_jingu_gate.py`:
- Derives explicit `env`, `test`, `verify` model from harness
- Printed as `[execution-model]` before any agent run

---

## SWE-bench Specific: What the Official Path Looks Like

```
# Environment
source /opt/miniconda3/bin/activate && conda activate testbed && cd /testbed

# Test command (from harness)
MAP_REPO_VERSION_TO_SPECS[repo][version]["test_cmd"]
# e.g.: ./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1

# Directives (from harness)
get_test_directives(instance)
# e.g.: ['admin_inlines.tests', 'forms_tests.tests.test_media']
```

**Never invent any part of this.** Read it from the harness.

---

## Relationship

- `docs/cognitive-failure-cases.md` — CF-ENV-001: wrong python path, wrong test command
- `~/.claude/rules/onboarding-first.md` — behavioral constraint for Claude
- `~/.claude/rules/system-mental-model.md` SM1+SM2 — conceptual parent
