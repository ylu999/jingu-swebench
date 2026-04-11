"""
Jingu adapter — translates raw agent output + signals into Jingu-readable proposals.

Extracted from run_with_jingu_gate.py (p225-02).

Bridges runtime layer and governance layer. MUST NOT contain:
  - debounce / in-flight / stagnation state
  - loop control decisions
"""
import re


# ── Constants for principal violation detection ───────────────────────────────

# Enforced violation codes detectable from a cognition declaration (Python-side check)
# Mirrors ENFORCED_VIOLATION_CODES in retry_controller.py — keep in sync.
_LOCAL_PATH_PATTERNS = ("/root/", "/home/", "/Users/", "~/.claude", "/tmp/jingu")
_ENV_CHECK_KEYWORDS = (
    "env check", "smoke test", "activation proof", "preflight",
    "node_modules", "npm install", "pip install",
)
_FEEDBACK_KEYWORDS = (
    "verify", "check", "test", "observe", "measure", "confirm",
    "run", "result", "output", "pass", "fail",
)

_MAX_PYTEST_FEEDBACK_BYTES = 2048  # cap extracted content to ~2KB


# ── Principal violation extraction ────────────────────────────────────────────

def extract_principal_violation_codes(decl: dict | None) -> list[str]:
    """
    Lightweight Python-side detection of enforced-principal violations.

    Returns violation codes from ENFORCED_VIOLATION_CODES that are detectable
    from the cognition declaration alone — feeds into build_retry_plan() as
    principal_violation_codes for targeted hint injection.

    Checks:
      ENV_LEAKAGE_HARDCODE_PATH — P_DEBUG_ENV_INDEPENDENCE declared but no env
        validation evidence, OR evidence contains local path patterns
      PLAN_NO_FEEDBACK_LOOP — P_PLAN_CLOSE_THE_LOOP declared but no feedback
        evidence keywords
    """
    if not decl:
        return []
    codes: list[str] = []
    principals = decl.get("principals_used", decl.get("principals", []))
    evidence_items = decl.get("evidence", [])
    evidence_texts = [
        (e.get("content", "") if isinstance(e, dict) else str(e)).lower()
        for e in evidence_items
    ]
    combined_evidence = " ".join(evidence_texts)

    if "P_DEBUG_ENV_INDEPENDENCE" in principals:
        has_local_path = any(p.lower() in combined_evidence for p in _LOCAL_PATH_PATTERNS)
        has_env_check = any(kw in combined_evidence for kw in _ENV_CHECK_KEYWORDS)
        if has_local_path or not has_env_check:
            codes.append("ENV_LEAKAGE_HARDCODE_PATH")

    if "P_PLAN_CLOSE_THE_LOOP" in principals:
        has_feedback = any(kw in combined_evidence for kw in _FEEDBACK_KEYWORDS)
        if not has_feedback:
            codes.append("PLAN_NO_FEEDBACK_LOOP")

    return codes


# ── pytest output parser ──────────────────────────────────────────────────────

def parse_pytest_output(stdout: str, stderr: str = "") -> dict:
    """
    Parse pytest stdout/stderr for structured failure details.

    Returns dict with:
      - failing_tests: list[str]  — test names that FAILED
      - error_excerpts: list[str] — first N assertion/error messages
      - summary: str              — the "X failed, Y passed" summary line
      - partial: bool             — True if some tests passed and some failed

    Handles gracefully: empty input, non-pytest output, truncated output.
    """
    result: dict = {
        "failing_tests": [],
        "error_excerpts": [],
        "summary": "",
        "partial": False,
    }
    if not stdout and not stderr:
        return result

    combined = (stdout or "") + "\n" + (stderr or "")

    # 1. Extract FAILED test names from "FAILED path::Class::method" lines
    failed_pattern = re.compile(r'FAILED\s+(\S+)', re.MULTILINE)
    failed_matches = failed_pattern.findall(combined)
    if failed_matches:
        seen: set = set()
        for m in failed_matches:
            if m not in seen:
                seen.add(m)
                result["failing_tests"].append(m)

    # 2. Extract summary line: "= X failed, Y passed =" or similar
    summary_pattern = re.compile(
        r'={2,}\s*([\d\w\s,]+(?:failed|passed|error)[^\n]*?)\s*={2,}',
        re.MULTILINE,
    )
    summary_matches = summary_pattern.findall(combined)
    if summary_matches:
        result["summary"] = summary_matches[-1].strip()
        s = result["summary"].lower()
        if "passed" in s and "failed" in s:
            result["partial"] = True

    # 3. Extract error excerpts — assertion errors and typed exceptions
    error_patterns = [
        re.compile(r'((?:Assertion|Type|Value|Attribute|Key|Import|Runtime)Error[:\s].{10,200})', re.MULTILINE),
        re.compile(r'(assert\w*\s+.{10,200})', re.MULTILINE | re.IGNORECASE),
    ]
    excerpts: list = []
    for pat in error_patterns:
        for m in pat.findall(combined):
            cleaned = m.strip()[:300]
            if cleaned and cleaned not in excerpts:
                excerpts.append(cleaned)
            if len(excerpts) >= 3:
                break
        if len(excerpts) >= 3:
            break

    # 4. Fallback: "E " prefixed lines (pytest's error detail marker)
    if not excerpts:
        e_lines = re.findall(r'^E\s+(.+)$', combined, re.MULTILINE)
        for line in e_lines[:5]:
            cleaned = line.strip()[:200]
            if cleaned:
                excerpts.append(cleaned)

    result["error_excerpts"] = excerpts[:3]

    return result


# ── Execution feedback helpers ────────────────────────────────────────────────

def _get_cv_stdout(jingu_body: dict) -> tuple:
    """
    Extract stdout/stderr from the most recent controlled_fail_to_pass verify_history entry.
    Returns (stdout, stderr). Both empty if not available.
    """
    verify_history = jingu_body.get("verify_history", [])
    if not verify_history:
        return "", ""
    for entry in reversed(verify_history):
        if entry.get("kind") == "controlled_fail_to_pass":
            return entry.get("stdout", ""), entry.get("stderr", "")
    for entry in reversed(verify_history):
        if entry.get("kind") == "controlled_error":
            return entry.get("stdout", ""), entry.get("stderr", "")
    for entry in reversed(verify_history):
        if entry.get("stdout", ""):
            return entry.get("stdout", ""), entry.get("stderr", "")
    return "", ""


def _format_pytest_feedback(parsed: dict, controlled_passed: int, controlled_failed: int,
                            fail_to_pass_tests: list) -> str:
    """
    Format parsed pytest output into an actionable retry feedback string.
    Capped at ~2KB.
    """
    parts = []

    total = controlled_passed + controlled_failed
    if parsed["partial"]:
        parts.append(
            f"TEST RESULTS: {controlled_failed}/{total} FAIL_TO_PASS tests still failing "
            f"({controlled_passed} now pass — partial progress)."
        )
    else:
        parts.append(
            f"TEST RESULTS: {controlled_failed}/{total} FAIL_TO_PASS tests still failing."
        )

    if parsed["failing_tests"]:
        parts.append("Failing tests:")
        for t in parsed["failing_tests"][:8]:
            short = t.split("::")[-1] if "::" in t else t.split("/")[-1]
            parts.append(f"  - {short}  ({t})" if len(t) < 120 else f"  - {short}")
    elif fail_to_pass_tests:
        parts.append("Required FAIL_TO_PASS tests:")
        for t in fail_to_pass_tests[:6]:
            short = t.split(".")[-1] if "." in t else t
            parts.append(f"  - {short}")

    if parsed["error_excerpts"]:
        parts.append("Error details:")
        for i, exc in enumerate(parsed["error_excerpts"][:2]):
            parts.append(f"  [{i+1}] {exc}")

    if parsed["summary"]:
        parts.append(f"Summary: {parsed['summary']}")

    parts.append(
        "Action: read the failing test code, trace the expected behavior, "
        "and fix the root cause in your patch."
    )

    feedback = "\n".join(parts)
    if len(feedback) > _MAX_PYTEST_FEEDBACK_BYTES:
        feedback = feedback[:_MAX_PYTEST_FEEDBACK_BYTES - 20] + "\n[truncated]"
    return feedback


def build_execution_feedback(
    jingu_body: dict,
    fail_to_pass_tests: list[str],
    patch_fp: dict,
) -> str:
    """
    Build a structured retry hint from execution signal — deterministic, no LLM.

    Converts: test_results + patch fingerprint → actionable hint for attempt 2.
    Three layers: summary → failing tests → example failure excerpt.
    p207-P3: when controlled_verify stdout is available, parses pytest output
    for specific failing test names and error messages.
    """
    test_results = jingu_body.get("test_results", {})
    tests_ran = test_results.get("ran_tests", False)
    excerpt = test_results.get("excerpt", "")

    # p201: trust hierarchy — check controlled_passed (trust=100) BEFORE last_passed (trust=30).
    # controlled_passed is the official FAIL_TO_PASS harness result — ground truth.
    # last_passed is a heuristic scan of agent tool output — low trust, may be wrong.
    controlled_passed = test_results.get("controlled_passed")
    controlled_failed = test_results.get("controlled_failed")
    if controlled_passed is not None and controlled_failed is not None:
        print(f"[bef] branch=controlled controlled_passed={controlled_passed} controlled_failed={controlled_failed}")
        tests_str = ", ".join(fail_to_pass_tests[:4])
        if controlled_failed == 0:
            # Official tests passed — strong SUBMIT signal
            return (
                f"Official FAIL_TO_PASS tests PASSED ({controlled_passed} passed, 0 failed). "
                f"Your patch is correct. Submit immediately. "
                f"Required tests: {tests_str}"
            )
        else:
            # Official tests failed — p207-P3: parse pytest stdout for targeted feedback
            cv_stdout, cv_stderr = _get_cv_stdout(jingu_body)
            if cv_stdout or cv_stderr:
                parsed = parse_pytest_output(cv_stdout, cv_stderr)
                if parsed["failing_tests"] or parsed["error_excerpts"]:
                    print(f"[bef] p207-P3: parsed {len(parsed['failing_tests'])} failing tests, "
                          f"{len(parsed['error_excerpts'])} error excerpts from cv stdout")
                    return _format_pytest_feedback(parsed, controlled_passed, controlled_failed,
                                                   fail_to_pass_tests)
            # Fallback: generic HARD_FAIL with counts (no parseable pytest output)
            return (
                f"Official FAIL_TO_PASS tests FAILED: "
                f"{controlled_passed} passed, {controlled_failed} failed. "
                f"Your patch does not fix the required tests. "
                f"Required tests: {tests_str}. "
                f"Revisit your analysis — the root cause fix is incomplete."
            )

    # No controlled_verify result available — fall back to agent-heuristic (trust=30).
    # NOTE: agent-run tests are LOW TRUST. A failing agent test does NOT mean your patch is wrong.
    print(f"[bef] branch=last_passed_fallback tests_ran={tests_ran} last_passed={test_results.get('last_passed')}")
    test_passed = test_results.get("last_passed")

    if not tests_ran:
        return (
            "Previous attempt submitted without running tests. "
            "Run the required tests FIRST, verify they pass, then submit."
        )

    if test_passed:
        # Agent's own tests passed but official FAIL_TO_PASS not yet verified (low trust).
        tests_str = ", ".join(fail_to_pass_tests[:4])
        return (
            f"Previous attempt's tests passed (agent-run, not official verification). "
            f"Ensure these specific FAIL_TO_PASS tests pass: {tests_str}. "
            f"If they already pass, submit immediately."
        )

    # Tests failed — build structured feedback
    parts = ["Previous attempt failed tests.\n"]

    # Layer 1: extract failure/error counts from excerpt
    failures = 0
    errors = 0
    if excerpt:
        fm = re.search(r'(\d+) failure', excerpt)
        em = re.search(r'(\d+) error', excerpt)
        if fm:
            failures = int(fm.group(1))
        if em:
            errors = int(em.group(1))
    if failures or errors:
        parts.append(f"Test results: {failures} failure(s), {errors} error(s)\n")

    # Layer 2: failing test names from FAIL_TO_PASS (most relevant signal)
    if fail_to_pass_tests:
        tests_str = "\n".join(f"  - {t.split('.')[-1]}" for t in fail_to_pass_tests[:6])
        parts.append(f"Tests that must pass:\n{tests_str}\n")

    # Layer 3: compress excerpt to most useful part
    # pytest output: errors/failures section is most useful, summary line is at end
    if excerpt:
        # Try to extract the failure section (between === FAILURES === and === short test summary ===)
        fail_section = re.search(
            r'(={3,} FAILURES ={3,}.*?)(?:={3,}|$)', excerpt, re.DOTALL
        )
        if fail_section:
            parts.append(f"Failure detail:\n{fail_section.group(1)[:600]}\n")
        else:
            # Fallback: last 400 chars of excerpt (usually has summary)
            useful = excerpt[-400:].strip()
            if useful:
                parts.append(f"Test output tail:\n{useful}\n")

    # Files changed (to surface if agent went to wrong files)
    files = patch_fp.get("files", []) if patch_fp else []
    if files:
        parts.append(f"Files you changed: {files}\n")

    parts.append(
        "You must: fix the underlying logic (not just suppress warnings or add code). "
        "Run the failing tests and verify they pass before submitting."
    )

    return "\n".join(parts)


# ── Patch validation / scoring ────────────────────────────────────────────────

def normalize_patch(patch_text: str) -> str:
    """Pad truncated hunks so git apply does not fail with 'corrupt patch'.

    LLMs sometimes omit the last 1-2 trailing context lines of a hunk.
    git apply counts lines strictly against the @@ header count; a short hunk
    causes 'corrupt patch at line N'.  We detect each hunk's claimed line count
    and append missing blank context lines (' ') at the end of short hunks.
    """
    lines = patch_text.splitlines()
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r'^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@', line)
        if m:
            old_count = int(m.group(1)) if m.group(1) is not None else 1
            new_count = int(m.group(2)) if m.group(2) is not None else 1
            result.append(line)
            i += 1
            old_seen = new_seen = 0
            hunk_lines = []
            while i < len(lines):
                nl = lines[i]
                if re.match(r'^(@@ |diff --git |--- )', nl) or nl.startswith('+++ '):
                    break
                hunk_lines.append(nl)
                if nl.startswith('+') and not nl.startswith('+++'):
                    new_seen += 1
                elif nl.startswith('-') and not nl.startswith('---'):
                    old_seen += 1
                else:
                    old_seen += 1
                    new_seen += 1
                i += 1
            old_missing = old_count - old_seen
            new_missing = new_count - new_seen
            pad = max(old_missing, new_missing)
            for _ in range(pad):
                hunk_lines.append(' ')
            result.extend(hunk_lines)
        else:
            result.append(line)
            i += 1
    normalized = '\n'.join(result)
    if not normalized.endswith('\n'):
        normalized += '\n'
    return normalized


def jingu_structural_check(patch_text: str) -> dict:
    """Check patch has --- / +++ / @@ markers."""
    if not patch_text or len(patch_text.strip()) < 10:
        return {"pass": False, "code": "EMPTY_PATCH", "message": "Patch is empty"}
    if not re.search(r'^(---|[+]{3}|@@)', patch_text, re.MULTILINE):
        return {"pass": False, "code": "PARSE_FAILED", "message": "No diff markers found"}
    return {"pass": True, "code": "ACCEPTED"}

def score_patch(patch_text: str) -> float:
    """Score: prefer small, single-file patches."""
    lines = patch_text.splitlines()
    files = sum(1 for l in lines if l.startswith("+++ b/"))
    changed = sum(1 for l in lines if l.startswith(("+", "-")) and not l.startswith(("+++", "---")))
    score = 1000.0 - files * 50
    return score


def extract_jingu_body(traj: dict, patch_text: str, problem_statement: str = "") -> dict:
    """
    Derive structured jingu_body from traj messages — no LLM call needed.

    jingu_body schema v0: deterministic extraction from observable agent behavior.
    Used by jingu-trust-gate B1+ as structured evidence for admission decisions.
    """
    messages = traj.get("messages", [])
    info = traj.get("info", {})
    exit_status = info.get("exit_status", "")

    # Files read and written — parse from tool call content
    files_read: set[str] = set()
    files_written: set[str] = set()
    test_ran = False
    last_test_passed: bool | None = None
    last_test_excerpt = ""
    tool_calls_made = 0

    # Write signals: collected from multiple sources
    # 1. Patch is ground truth — if patch touches a file, agent wrote it
    for line in (patch_text or "").splitlines():
        if line.startswith("+++ b/"):
            fp = line[6:].strip()
            if fp:
                files_written.add(fp)

    for msg in messages:
        role = msg.get("role", "")
        extra = msg.get("extra", {})
        actions = extra.get("actions", []) if role == "assistant" else []
        for action in actions:
            tool_calls_made += 1
            # Actions may be dicts (structured tool calls) or strings (bash commands)
            if isinstance(action, dict):
                tool_name = action.get("tool", action.get("name", ""))
                tool_input = action.get("input", action.get("arguments", {}))
                # Structured tool calls: look for path/file fields
                path_val = ""
                if isinstance(tool_input, dict):
                    path_val = (tool_input.get("path") or tool_input.get("file_path")
                                or tool_input.get("filename") or "")
                if path_val and ("/" in path_val or path_val.endswith(".py")):
                    write_tools = {"edit_file", "write_file", "create_file",
                                   "str_replace_editor", "str_replace", "apply_patch",
                                   "bash_write", "patch"}
                    read_tools  = {"open_file", "view_file", "read_file",
                                   "str_replace_editor_view", "cat"}
                    if any(t in tool_name.lower() for t in write_tools):
                        files_written.add(path_val)
                    elif any(t in tool_name.lower() for t in read_tools):
                        files_read.add(path_val)
            else:
                # String action (bash command) — limited heuristic, patch is authoritative
                action_str = str(action)
                if any(kw in action_str for kw in ("open_file", "view_file", "cat ")):
                    parts = action_str.split()
                    for i, p in enumerate(parts):
                        if p in ("open_file", "view_file") and i + 1 < len(parts):
                            path_candidate = parts[i + 1].strip("'\"")
                            if "/" in path_candidate or path_candidate.endswith(".py"):
                                files_read.add(path_candidate)

        # Detect test results from tool outputs
        if role == "tool":
            content = str(msg.get("content", ""))
            if any(kw in content for kw in ("PASSED", "FAILED", "passed", "failed", "ERROR", "error")):
                test_ran = True
                if "FAILED" in content or "failed" in content.lower() or "ERROR" in content:
                    last_test_passed = False
                else:
                    last_test_passed = True
                # Extract from <output> tag if present; take last 1500 chars (summary is at end)
                out_match = re.search(r'<output>(.*?)</output>', content, re.DOTALL)
                raw_out = out_match.group(1) if out_match else content
                last_test_excerpt = raw_out[-1500:]

    # Patch summary from patch structure
    patch_lines = patch_text.splitlines() if patch_text else []
    patch_files_changed = sum(1 for l in patch_lines if l.startswith("+++ b/"))
    patch_hunks = sum(1 for l in patch_lines if l.startswith("@@"))
    patch_lines_added = sum(1 for l in patch_lines if l.startswith("+") and not l.startswith("+++"))
    patch_lines_removed = sum(1 for l in patch_lines if l.startswith("-") and not l.startswith("---"))

    return {
        "schema_version": "jingu-body-v0",
        "exit_status": exit_status,
        "problem_understanding": (problem_statement or info.get("problem_statement", ""))[:300],
        "tool_calls_made": tool_calls_made,
        "files_read": sorted(files_read)[:20],
        "files_written": sorted(files_written)[:10],
        "test_results": {
            "ran_tests": test_ran,
            "last_passed": last_test_passed,
            "excerpt": last_test_excerpt,
        },
        "patch_summary": {
            "files_changed": patch_files_changed,
            "hunks": patch_hunks,
            "lines_added": patch_lines_added,
            "lines_removed": patch_lines_removed,
        },
    }
