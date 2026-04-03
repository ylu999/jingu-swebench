#!/usr/bin/env python3
"""
Fast local unit test for extract_test_counts().

Tests against real excerpt samples observed in p179-batch-10b traj files.
Run: python3 scripts/test_extract_test_counts.py

All cases pass in < 1 second. No EC2, no Docker, no batch needed.
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from run_with_jingu_gate import extract_test_counts


def make_jb(excerpt="", exit_code=None, ran_tests=True):
    return {"test_results": {"excerpt": excerpt, "exit_code": exit_code, "ran_tests": ran_tests}}


CASES = [
    # description, jingu_body, expected

    # pytest
    ("pytest: 3 passed",                make_jb("3 passed in 0.12s"),                   3),
    ("pytest: 0 passed (fail only)",    make_jb("FAILED (failures=1)"),                 0),

    # unittest Ran N tests
    ("unittest ran_ok: 28",
     make_jb("test_foo ... ok\n\n------\nRan 28 tests in 0.155s\n\nOK\n"),             28),
    ("unittest ran_fail: 28 - 1 fail",
     make_jb("test_foo ... FAIL\n\n------\nRan 28 tests in 0.155s\n\nFAILED (failures=1)\n"), 27),
    ("unittest ran_fail: 10 - 2 errors",
     make_jb("Ran 10 tests in 1.2s\n\nFAILED (errors=2)\n"),                            8),
    ("unittest ran_fail: 10 - 1 fail + 1 error",
     make_jb("Ran 10 tests in 0.5s\n\nFAILED (failures=1, errors=1)\n"),                8),

    # unittest minimal OK
    ("unittest minimal OK",
     make_jb("test_a ... ok\n\n------\nRan 1 test in 0.002s\n\nOK\n"),                  1),

    # real batch-10b: django__django-12184
    ("batch-10b 12184: 28 tests OK",
     make_jb(
         "test_converter_reverse ... ok\ntest_invalid_converter ... ok\n"
         "\n----------------------------------------------------------------------\n"
         "Ran 28 tests in 0.155s\n\nOK\n"),                                            28),

    # real batch-10b: django__django-10914
    ("batch-10b 10914: FAILED (failures=1)",
     make_jb(
         "Permission errors are not swallowed ... FAIL\n"
         "\n------\nRan 1 test in 0.002s\n\nFAILED (failures=1)\n"),                    0),

    # real batch-10b: django__django-11815 (ALL TESTS PASSED + checkmarks)
    ("batch-10b 11815: ALL TESTS PASSED + 2 checkmarks",
     make_jb(
         "\u2713 Binary Enum uses name-based access\n"
         "======================================================================\n"
         "\u2713 ALL TESTS PASSED - The fix is working correctly!\n"
         "======================================================================\n"),   2),

    # real batch-10b: django__django-12453 (Test passed!)
    ("batch-10b 12453: Test passed!",
     make_jb("Testing error handling with invalid data...\nTest passed!\n"),             1),

    # non-test excerpt (code diff / source): use exit_code fallback
    ("non-test excerpt, exit_code=0",
     make_jb("   274\t        if len(self.data) == 1:\n", exit_code=0),                  1),
    ("non-test excerpt, exit_code=1",
     make_jb("   274\t        if len(self.data) == 1:\n", exit_code=1),                  0),
    ("non-test excerpt, exit_code=None",
     make_jb("   274\t        if len(self.data) == 1:\n", exit_code=None),              -1),

    # empty excerpt
    ("empty excerpt, no exit_code",   make_jb("", exit_code=None),                     -1),
    ("empty excerpt, exit_code=0",    make_jb("", exit_code=0),                         1),
    ("empty excerpt, exit_code=1",    make_jb("", exit_code=1),                         0),

    # custom PASS:/FAIL: markers
    ("custom PASS: x3",
     make_jb("PASS: String path works\nPASS: Callable path works\nPASS: No regression\n"), 3),
    ("custom 2 PASS + 1 FAIL",
     make_jb("PASS: Test 1\nPASS: Test 2\nFAIL: Test 3\n"),                              1),
]


def run():
    ok = fail = 0
    for desc, jb, expected in CASES:
        got = extract_test_counts(jb)
        if got == expected:
            ok += 1
            print(f"  PASS  {desc}")
        else:
            fail += 1
            excerpt = jb.get("test_results", {}).get("excerpt", "")[:80]
            print(f"  FAIL  {desc}")
            print(f"        expected={expected}  got={got}  excerpt={repr(excerpt)}")
    print(f"\n{'='*50}")
    print(f"  {ok}/{ok+fail} passed" + (f"  ({fail} FAILED)" if fail else ""))
    return fail == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)


def test_controlled_verify_priority():
    """extract_test_counts should use controlled_verify result when available."""
    from run_with_jingu_gate import extract_test_counts

    # controlled_verify takes priority over excerpt
    jb_with_cv = {
        "test_results": {
            "excerpt": "FAILED (failures=5)",  # would parse as 0
            "exit_code": 1,
            "ran_tests": True,
            "controlled_passed": 3,  # promoted from controlled_verify
        },
        "controlled_verify": {
            "verification_kind": "controlled_fail_to_pass",
            "tests_passed": 3,
            "tests_failed": 2,
            "exit_code": 1,
        },
    }
    got = extract_test_counts(jb_with_cv)
    assert got == 3, f"expected 3 (controlled_verify), got {got}"
    print("  PASS  controlled_verify takes priority over excerpt (3 vs would-be 0)")

    # controlled_error falls through to excerpt parsing
    jb_cv_error = {
        "test_results": {
            "excerpt": "3 passed in 0.1s",
            "exit_code": 0,
            "ran_tests": True,
        },
        "controlled_verify": {
            "verification_kind": "controlled_error",
            "tests_passed": -1,
            "tests_failed": -1,
        },
    }
    got2 = extract_test_counts(jb_cv_error)
    assert got2 == 3, f"expected 3 (excerpt fallback), got {got2}"
    print("  PASS  controlled_error falls through to excerpt parsing (got 3)")

    print("\n==================================================")
    print("  controlled_verify priority tests: 2/2 passed")


if __name__ == "__main__" and "test_controlled_verify_priority" not in __import__("sys").argv:
    pass

