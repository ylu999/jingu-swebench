"""
Controlled verification — apply patch + run tests inside eval container.

Extracted from run_with_jingu_gate.py (p225-02).

Executes patch + runs tests inside the eval container.
run_controlled_verify is a PURE FUNCTION (no state of its own).
The scheduling / debounce / in-flight guard live in the caller.
"""
import os
import re
import time


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_f2p_class_labels(f2p_tests: list) -> list:
    """
    Extract unique class-level test labels from FAIL_TO_PASS entries.

    F2P format: 'test_method (module.submodule.ClassName)'
    Returns: ['module.submodule.ClassName', ...] (deduplicated, sorted)

    Django runtests.py accepts class-level labels, which is much narrower
    than module-level directives and avoids running hundreds of unrelated tests.
    """
    import re as _re
    classes = set()
    for entry in f2p_tests:
        m = _re.match(r'\w+\s+\((.+)\)', entry)
        if m:
            classes.add(m.group(1))
    return sorted(classes)


def _build_test_command(instance: dict, verify_scope: str = "module") -> tuple:
    """
    Build the test command for controlled verification.

    verify_scope:
      "targeted" — F2P class-level labels only (fast, for in-loop signal)
      "targeted+sentinel" — F2P classes + a few P2P classes (fast + regression detection)
      "module" — official directives (module-level, for eval alignment)

    Returns (cmd_string, actual_scope) where actual_scope reports what was actually used
    (may differ from verify_scope if targeted fell back to module).
    """
    from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
    from swebench.harness.test_spec.python import get_test_directives
    import json as _json

    repo = instance["repo"]
    version = instance["version"]
    specs = MAP_REPO_VERSION_TO_SPECS[repo][version]
    test_cmd = specs["test_cmd"]

    if verify_scope in ("targeted", "targeted+sentinel"):
        # Parse F2P test entries
        f2p_raw = instance.get("FAIL_TO_PASS", [])
        if isinstance(f2p_raw, str):
            try:
                f2p_raw = _json.loads(f2p_raw)
            except Exception:
                f2p_raw = []

        f2p_labels = _extract_f2p_class_labels(f2p_raw)

        # Add P2P sentinel classes for regression detection
        sentinel_labels = []
        if verify_scope == "targeted+sentinel":
            p2p_raw = instance.get("PASS_TO_PASS", [])
            if isinstance(p2p_raw, str):
                try:
                    p2p_raw = _json.loads(p2p_raw)
                except Exception:
                    p2p_raw = []
            p2p_labels = _extract_f2p_class_labels(p2p_raw)
            # Pick up to 3 P2P classes not already in F2P set
            f2p_set = set(f2p_labels)
            for lbl in p2p_labels:
                if lbl not in f2p_set:
                    sentinel_labels.append(lbl)
                    if len(sentinel_labels) >= 3:
                        break

        all_labels = f2p_labels + sentinel_labels

        # Always use targeted scope when labels are available.
        # No arbitrary class limit — timeout scales with class count instead.
        if all_labels:
            if len(all_labels) > 100:
                print(
                    f"    [controlled_verify] WARNING: targeted scope has {len(all_labels)} classes"
                    f" — timeout will scale accordingly",
                    flush=True,
                )
            labels_str = " ".join(all_labels)
            _actual = f"targeted(f2p={len(f2p_labels)},sentinel={len(sentinel_labels)},total={len(all_labels)})"
            print(
                f"    [controlled_verify] scope={_actual} classes={len(all_labels)}"
                f" f2p={len(f2p_labels)} sentinel={len(sentinel_labels)}",
                flush=True,
            )
            return (
                "source /opt/miniconda3/bin/activate && "
                "conda activate testbed && "
                f"cd /testbed && "
                f"{test_cmd} {labels_str} 2>&1"
            ), _actual
        # else: fall through to module-level with explanation
        _fallback_reason = "no_labels"
        _actual = f"module(fallback:{_fallback_reason})"
    else:
        _actual = "module"

    # Module-level: official directives (original behavior)
    directives = get_test_directives(instance)
    directives_str = " ".join(directives)

    return (
        "source /opt/miniconda3/bin/activate && "
        "conda activate testbed && "
        f"cd /testbed && "
        f"{test_cmd} {directives_str} 2>&1"
    ), _actual


def _check_onboarding(instance: dict) -> tuple[bool, str]:
    """
    ONBOARDING_FIRST enforcement gate.

    Verifies the instance can be run via the official SWE-bench harness path
    before any agent execution begins. Prevents OFFICIAL_PATH_NOT_CONFIRMED and
    ASSUMED_ENV_BEHAVIOR failure classes.

    Returns (ok, reason).
    """
    if not instance.get("repo") or not instance.get("version"):
        return False, "MISSING_REPO_OR_VERSION"

    try:
        from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
        repo = instance["repo"]
        version = instance["version"]
        if repo not in MAP_REPO_VERSION_TO_SPECS:
            return False, f"OFFICIAL_PATH_NOT_CONFIRMED: repo '{repo}' not in harness specs"
        if version not in MAP_REPO_VERSION_TO_SPECS[repo]:
            return False, f"OFFICIAL_PATH_NOT_CONFIRMED: version '{version}' not in harness specs for {repo}"
    except ImportError as e:
        return False, f"HARNESS_NOT_AVAILABLE: {e}"

    try:
        cmd, _ = _build_test_command(instance, verify_scope="module")
        if "conda activate testbed" not in cmd:
            return False, "ASSUMED_ENV_BEHAVIOR: test command missing 'conda activate testbed'"
    except Exception as e:
        return False, f"TEST_COMMAND_BUILD_FAILED: {e}"

    if not instance.get("FAIL_TO_PASS"):
        return False, "NO_FAIL_TO_PASS_DEFINED"

    return True, "OK"


def _build_execution_model(instance: dict) -> dict:
    """
    Derive the explicit execution model from the official SWE-bench harness.

    This is the ground truth for what will actually run — not inferred from
    prior experience. Printed as [execution-model] before any agent run.
    """
    from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
    from swebench.harness.test_spec.python import get_test_directives

    repo = instance["repo"]
    version = instance["version"]
    specs = MAP_REPO_VERSION_TO_SPECS[repo][version]

    return {
        "repo": repo,
        "version": version,
        "env": {
            "conda_env": "testbed",
            "workdir": "/testbed",
            "activate": "source /opt/miniconda3/bin/activate && conda activate testbed",
        },
        "test": {
            "runner": "runtests.py" if "runtests.py" in specs["test_cmd"] else "pytest",
            "test_cmd": specs["test_cmd"],
            "directives": get_test_directives(instance),
        },
        "verify": {
            "mode": "controlled",
            "source": "swebench_harness",
        },
    }


def _print_execution_model(model: dict) -> None:
    """Print execution model to stdout (visible in log as [execution-model] block)."""
    print("[execution-model]")
    print(f"  repo: {model['repo']}  version: {model['version']}")
    print(f"  env: conda_env={model['env']['conda_env']}  workdir={model['env']['workdir']}")
    print(f"  test.runner: {model['test']['runner']}")
    print(f"  test.cmd: {model['test']['test_cmd']}")
    print(f"  test.directives: {model['test']['directives']}")
    print(f"  verify: mode={model['verify']['mode']}  source={model['verify']['source']}")


def _parse_test_output_counts(output: str) -> tuple[int, int]:
    """
    Parse passed/failed counts from test output.
    Returns (passed, failed). Both -1 if unparseable.
    """
    # pytest: "3 passed, 2 failed"
    m_pass = re.search(r'(\d+) passed', output)
    m_fail = re.search(r'(\d+) failed', output)
    if m_pass or m_fail:
        passed = int(m_pass.group(1)) if m_pass else 0
        failed = int(m_fail.group(1)) if m_fail else 0
        return passed, failed
    # unittest: "Ran N tests ... OK" or "FAILED (failures=K)"
    ran_m = re.search(r'Ran (\d+) tests? in', output)
    if ran_m:
        total = int(ran_m.group(1))
        fail_m = re.search(r'FAILED \((?:failures=(\d+))?(?:,\s*)?(?:errors=(\d+))?\)', output)
        if fail_m:
            f = int(fail_m.group(1) or 0)
            e = int(fail_m.group(2) or 0)
            return max(0, total - f - e), f + e
        return total, 0  # OK
    # Error exit with no parseable output
    return -1, -1


def _parse_f2p_p2p(
    output: str,
    fail_to_pass: list,
    pass_to_pass: list,
) -> tuple[int, int, int, int]:
    """
    Parse test output against FAIL_TO_PASS and PASS_TO_PASS test lists.

    Matches individual test results in the output against the official test lists
    to compute eval-aligned metrics, exactly like SWE-bench grading.

    Returns (f2p_passed, f2p_failed, p2p_passed, p2p_failed).

    BUG-10 fix: this replaces the old approach of just counting total pass/fail,
    which couldn't distinguish F2P from P2P and produced false positives/negatives.
    """
    import json as _json

    # Parse JSON-encoded test lists if needed
    if isinstance(fail_to_pass, str):
        try:
            fail_to_pass = _json.loads(fail_to_pass)
        except Exception:
            fail_to_pass = []
    if isinstance(pass_to_pass, str):
        try:
            pass_to_pass = _json.loads(pass_to_pass)
        except Exception:
            pass_to_pass = []

    if not output:
        # No output at all — F2P conservatively failed, P2P unknown (assume passed)
        return 0, len(fail_to_pass), len(pass_to_pass), 0

    # Build sets of test identifiers for matching
    f2p_set = set(fail_to_pass)
    p2p_set = set(pass_to_pass)

    # Extract test results from output.
    # Django uses unittest format: "test_name (module.Class) ... ok/FAIL/ERROR"
    # pytest uses: "path::test_name PASSED/FAILED"
    passed_tests = set()
    failed_tests = set()

    for line in output.split("\n"):
        line = line.strip()
        # Django unittest format: "test_method (module.ClassName) ... ok"
        # or "test_method (module.ClassName) ... FAIL"
        m_django = re.match(
            r"^(\w+)\s+\(([^)]+)\)\s+\.\.\.\s+(ok|FAIL|ERROR)", line
        )
        if m_django:
            test_name = m_django.group(1)
            test_class = m_django.group(2)
            status = m_django.group(3)
            # Build full test ID: "test_name (module.Class)"
            full_id = f"{test_name} ({test_class})"
            if status == "ok":
                passed_tests.add(full_id)
            else:
                failed_tests.add(full_id)
            continue

        # Django verbose: "test_method (module.ClassName)\nDescription ... ok"
        m_django_desc = re.match(
            r"^(.+?)\s+\.\.\.\s+(ok|FAIL|ERROR)$", line
        )
        if m_django_desc:
            test_id = m_django_desc.group(1).strip()
            status = m_django_desc.group(2)
            if status == "ok":
                passed_tests.add(test_id)
            else:
                failed_tests.add(test_id)
            continue

        # pytest format: "path/test.py::test_name PASSED/FAILED"
        m_pytest = re.match(r"^(.+?)\s+(PASSED|FAILED|ERROR)", line)
        if m_pytest:
            test_id = m_pytest.group(1).strip()
            status = m_pytest.group(2)
            if status == "PASSED":
                passed_tests.add(test_id)
            else:
                failed_tests.add(test_id)

    # Match against F2P and P2P lists
    f2p_passed = 0
    f2p_failed = 0
    for t in f2p_set:
        if t in passed_tests:
            f2p_passed += 1
        elif t in failed_tests:
            f2p_failed += 1
        else:
            # Test not found in output — count as failed (conservative)
            f2p_failed += 1

    p2p_passed = 0
    p2p_failed = 0
    for t in p2p_set:
        if t in passed_tests:
            p2p_passed += 1
        elif t in failed_tests:
            p2p_failed += 1
        else:
            # Test not found in output — count as passed (conservative for P2P,
            # since missing from output often means the test module wasn't in directives)
            p2p_passed += 1

    return f2p_passed, f2p_failed, p2p_passed, p2p_failed


# ── Main function ─────────────────────────────────────────────────────────────

def run_controlled_verify(
    patch_text: str,
    instance: dict,
    container_id: str,
    timeout_s: int | None = None,
    apply_test_patch: bool = True,
    verify_scope: str = "targeted+sentinel",
) -> dict:
    """
    Orchestrator-controlled verification: apply patch + run FAIL_TO_PASS tests.

    apply_test_patch=True (default): applies test_patch for eval-aligned F2P/P2P.
        Use for final verification / eval metrics.
    apply_test_patch=False: skips test_patch, runs against existing test suite.
        Use for inner-verify (agent-visible signal, no oracle).

    Uses the already-running swebench container (same image agent used, no re-pull needed).
    Runs specified tests directly via docker exec, returns structured results.

    Returns a dict with:
      verification_kind: "controlled_fail_to_pass" | "controlled_no_tests" | "controlled_error"
      tests_passed: int (-1 if unknown)
      tests_failed: int (-1 if unknown)
      exit_code: int
      elapsed_ms: float
      output_tail: str  (last 500 chars of test output for debugging)
      error: str (if verification_kind == "controlled_error")

    This is the PRIMARY signal source for tests_passed_after.
    extract_test_counts() is the fallback for when controlled verify is unavailable.
    """
    import subprocess as _sp
    import tempfile as _tf

    t0 = time.monotonic()

    fail_to_pass = instance.get("FAIL_TO_PASS", [])
    # SWE-bench dataset stores FAIL_TO_PASS as a JSON-encoded string, not a list.
    # Parse it here so len() and iteration work correctly.
    if isinstance(fail_to_pass, str):
        import json as _json
        try:
            fail_to_pass = _json.loads(fail_to_pass)
        except Exception:
            fail_to_pass = []
    if not fail_to_pass:
        return {
            "verification_kind": "controlled_no_tests",
            "tests_passed": -1, "tests_failed": -1,
            "exit_code": -1, "elapsed_ms": 0.0, "output_tail": "",
            "stdout": "", "stderr": "",
        }

    if not patch_text or not patch_text.strip():
        return {
            "verification_kind": "controlled_error",
            "tests_passed": 0, "tests_failed": len(fail_to_pass),
            "exit_code": 1, "elapsed_ms": 0.0, "output_tail": "",
            "stdout": "", "stderr": "",
            "error": "no patch to apply",
        }

    # git apply requires patch to end with newline; .strip() upstream may remove it
    if not patch_text.endswith("\n"):
        patch_text = patch_text + "\n"

    try:
        # Step 1: write patch to a temp file inside container
        with _tf.NamedTemporaryFile(suffix=".patch", delete=False, mode="w") as f:
            f.write(patch_text)
            host_patch_path = f.name

        # Copy patch into container
        cp_result = _sp.run(
            ["docker", "cp", host_patch_path, f"{container_id}:/tmp/jingu_verify.patch"],
            capture_output=True, text=True, timeout=30,  # Bug F fix (p20): 10s too short under load
        )
        os.unlink(host_patch_path)
        if cp_result.returncode != 0:
            return {
                "verification_kind": "controlled_error",
                "tests_passed": -1, "tests_failed": -1,
                "exit_code": -1, "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
                "output_tail": "", "error": f"docker cp failed: {cp_result.stderr[:200]}",
                "stdout": (cp_result.stdout or "")[:10240],
                "stderr": (cp_result.stderr or "")[:10240],
            }

        # Step 2: reset to clean base_commit state before applying patch.
        # The patch is "git diff {base_commit}" — all changes since base.
        # To apply it cleanly we need to be at exactly base_commit.
        # Strategy: stash everything (including untracked), hard reset to
        # base_commit, apply patch, run tests, then restore original state.
        # Note: reset affects the container's working tree while agent is running.
        # The stash pop at the end restores agent's files after verify completes.
        _base_c = instance.get("base_commit", "HEAD")
        _sp.run(
            ["docker", "exec", "-w", "/testbed", container_id,
             "bash", "-c", "git stash --include-untracked -q 2>/dev/null || true"],
            capture_output=True, text=True, timeout=15,
        )
        _sp.run(
            ["docker", "exec", "-w", "/testbed", container_id,
             "bash", "-c", f"git reset --hard {_base_c} -q 2>/dev/null || true"],
            capture_output=True, text=True, timeout=15,
        )

        # Step 3: apply model patch (git apply in testbed)
        apply_result = _sp.run(
            ["docker", "exec", "-w", "/testbed", container_id,
             "bash", "-c", "git apply /tmp/jingu_verify.patch 2>&1"],
            capture_output=True, text=True, timeout=30,
        )
        if apply_result.returncode != 0:
            # Restore agent's working state before returning error
            _sp.run(
                ["docker", "exec", "-w", "/testbed", container_id,
                 "bash", "-c", f"git reset --hard {_base_c} -q 2>/dev/null; git stash pop -q 2>/dev/null || true"],
                capture_output=True, text=True, timeout=15,
            )
            return {
                "verification_kind": "controlled_error",
                "tests_passed": 0, "tests_failed": len(fail_to_pass),
                "exit_code": apply_result.returncode,
                "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
                "output_tail": apply_result.stdout[-300:],
                "error": f"git apply failed: {apply_result.stdout[:200]}",
                "stdout": (apply_result.stdout or "")[:10240],
                "stderr": (apply_result.stderr or "")[:10240],
            }

        # Step 3b: apply test_patch (CRITICAL for eval alignment — BUG-10 fix)
        # Official SWE-bench eval applies the test_patch which contains new/modified
        # test cases (the FAIL_TO_PASS tests). Without this, controlled_verify runs
        # against the OLD test suite and produces false positives/negatives.
        # v2: only apply when apply_test_patch=True (final eval). Inner-verify uses
        # apply_test_patch=False to keep signals agent-visible (no oracle).
        _test_patch = instance.get("test_patch", "") if apply_test_patch else ""
        if _test_patch and _test_patch.strip():
            # Get test files from test_patch for later reset
            import re as _re_mod
            _test_files = _re_mod.findall(r"diff --git a/.* b/(.*)", _test_patch)

            # First reset test files to base_commit state (same as official eval)
            if _test_files:
                _test_files_str = " ".join(_test_files)
                _sp.run(
                    ["docker", "exec", "-w", "/testbed", container_id,
                     "bash", "-c", f"git checkout {_base_c} {_test_files_str} 2>/dev/null || true"],
                    capture_output=True, text=True, timeout=15,
                )

            # Write test_patch to container and apply
            with _tf.NamedTemporaryFile(suffix=".patch", delete=False, mode="w") as _tp_f:
                _tp_f.write(_test_patch)
                _tp_host = _tp_f.name
            _sp.run(
                ["docker", "cp", _tp_host, f"{container_id}:/tmp/jingu_test.patch"],
                capture_output=True, text=True, timeout=30,
            )
            os.unlink(_tp_host)
            _tp_apply = _sp.run(
                ["docker", "exec", "-w", "/testbed", container_id,
                 "bash", "-c", "git apply -v /tmp/jingu_test.patch 2>&1"],
                capture_output=True, text=True, timeout=30,
            )
            if _tp_apply.returncode != 0:
                print(f"[controlled_verify] WARNING: test_patch apply failed: "
                      f"{_tp_apply.stdout[:200]}", flush=True)
                # Continue anyway — some test_patches may partially apply

        # Step 4: run tests using targeted or module scope
        test_cmd, _actual_scope = _build_test_command(instance, verify_scope=verify_scope)

        # Dynamic timeout: auto-scale based on scope when timeout_s is None
        if timeout_s is None:
            if "targeted" in _actual_scope:
                # Count classes from scope string: targeted(f2p=N,sentinel=M,total=T)
                import re as _re_to
                _m_total = _re_to.search(r'total=(\d+)', _actual_scope)
                _n_classes = int(_m_total.group(1)) if _m_total else 10
                # base 30s + 1.5s per class, cap 300s
                timeout_s = min(300, 30 + int(_n_classes * 1.5))
            else:
                # module-level fallback: generous timeout
                timeout_s = 120
        print(f"    [verify] scope={_actual_scope}  timeout={timeout_s}s", flush=True)

        test_result = _sp.run(
            ["docker", "exec", container_id, "bash", "-c", test_cmd],
            capture_output=True, text=True, timeout=timeout_s,
        )
        output = (test_result.stdout or "") + (test_result.stderr or "")
        # Extract FAIL/ERROR lines for meaningful output_tail instead of
        # raw tail (which is often just Django DB creation messages).
        _fail_lines = []
        _summary_line = ""
        for _line in output.split("\n"):
            _ls = _line.strip()
            if re.match(r"^\w+\s+\([^)]+\)\s+\.\.\.\s+(FAIL|ERROR)", _ls):
                _fail_lines.append(_ls)
            elif _ls.startswith("FAILED (") or _ls.startswith("Ran "):
                _summary_line = _ls
        if _fail_lines:
            output_tail = "\n".join(_fail_lines[-10:])  # up to 10 FAIL lines
            if _summary_line:
                output_tail = output_tail + "\n" + _summary_line
        else:
            output_tail = output[-500:]

        # Step 5: parse results from output
        passed, failed = _parse_test_output_counts(output)
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)

        # Step 6: parse F2P/P2P results for eval-aligned verdict
        f2p_pass, f2p_fail, p2p_pass, p2p_fail = _parse_f2p_p2p(
            output, fail_to_pass, instance.get("PASS_TO_PASS", [])
        )

        # Rollback: reset to base_commit then restore agent's working state via stash pop
        _sp.run(
            ["docker", "exec", "-w", "/testbed", container_id,
             "bash", "-c", f"git reset --hard {_base_c} -q 2>/dev/null; git stash pop -q 2>/dev/null || true"],
            capture_output=True, text=True, timeout=15,
        )

        # Compute eval-aligned resolved status
        f2p_total = f2p_pass + f2p_fail
        p2p_total = p2p_pass + p2p_fail
        f2p_rate = f2p_pass / f2p_total if f2p_total > 0 else 1.0
        p2p_rate = p2p_pass / p2p_total if p2p_total > 0 else 1.0
        eval_resolved = (f2p_rate == 1.0 and p2p_rate == 1.0)

        return {
            "verification_kind": "controlled_fail_to_pass",
            "tests_passed": passed,
            "tests_failed": failed,
            "exit_code": test_result.returncode,
            "elapsed_ms": elapsed_ms,
            "output_tail": output_tail,
            "stdout": (test_result.stdout or "")[:10240],
            "stderr": (test_result.stderr or "")[:10240],
            # BUG-10 fix: eval-aligned fields
            "f2p_passed": f2p_pass,
            "f2p_failed": f2p_fail,
            "p2p_passed": p2p_pass,
            "p2p_failed": p2p_fail,
            "eval_resolved": eval_resolved,
            "verify_scope": _actual_scope,
        }

    except _sp.TimeoutExpired:
        return {
            "verification_kind": "controlled_error",
            "tests_passed": -1, "tests_failed": -1,
            "exit_code": -1,
            "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
            "output_tail": "", "error": f"controlled verify timed out (scope={_actual_scope})",
            "stdout": "", "stderr": "",
            "verify_scope": _actual_scope,
        }
    except Exception as e:
        return {
            "verification_kind": "controlled_error",
            "tests_passed": -1, "tests_failed": -1,
            "exit_code": -1,
            "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
            "output_tail": "", "error": str(e)[:200],
            "stdout": "", "stderr": "",
        }
