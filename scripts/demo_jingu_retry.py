"""Demo: Jingu retry in action on a real SWE-bench instance.

Shows the 3-step flow:
1. Attempt 1 fails (incomplete fix)
2. Jingu analyzes failure and routes retry
3. Attempt 2 succeeds (fully resolved)

Usage:
    python scripts/demo_jingu_retry.py                     # default: django-11477
    python scripts/demo_jingu_retry.py --instance django__django-11477
    python scripts/demo_jingu_retry.py --batch best-config-v1
"""

import argparse
import json
import subprocess
import sys
import textwrap

S3_BUCKET = "jingu-swebench-results"

# Pre-baked demo data for django-11477 (Jingu-only resolved on BOTH models)
DEMO_11477 = {
    "instance_id": "django__django-11477",
    "problem": "Django URL optional parameters: reverse() fails with optional named groups",
    "a1_patch": textwrap.dedent("""\
        diff --git a/django/urls/resolvers.py b/django/urls/resolvers.py
        --- a/django/urls/resolvers.py
        +++ b/django/urls/resolvers.py
        @@ -610,6 +610,11 @@ class URLResolver:
                 possibilities = self.reverse_dict.getlist(lookup_view)
        +        # Filter out None values from kwargs for optional parameters
        +        if kwargs:
        +            kwargs = {k: v for k, v in kwargs.items() if v is not None}
        +
                 for possibility, pattern, defaults, converters in possibilities:
    """),
    "a1_test_results": {
        "f2p_passed": 1,
        "f2p_total": 3,
        "f2p_failing": [
            "test_two_variable_at_start_of_path_pattern",
            "test_re_path_with_optional_parameter",
        ],
    },
    "failure_type": "incomplete_fix",
    "failure_routing": {
        "next_phase": "DESIGN",
        "repair_goal": "Refine the design to cover remaining failing scenarios.",
    },
    "a2_patch": textwrap.dedent("""\
        diff --git a/django/urls/resolvers.py b/django/urls/resolvers.py
        --- a/django/urls/resolvers.py
        +++ b/django/urls/resolvers.py
        @@ -153,7 +153,7 @@ class RegexPattern(CheckURLMixin):
                 # If there are any named groups, use those as kwargs, ignoring
                 # non-named groups. Otherwise, pass all non-named arguments as
                 # positional arguments.
        -            kwargs = match.groupdict()
        +            kwargs = {k: v for k, v in match.groupdict().items() if v is not None}
                 args = () if kwargs else match.groups()
                 return path[match.end():], args, kwargs
    """),
    "a2_test_results": {
        "f2p_passed": 3,
        "f2p_total": 3,
        "f2p_failing": [],
        "eval_resolved": True,
    },
}


def print_step(n, title, color="\033[1m"):
    reset = "\033[0m"
    print(f"\n{'='*60}")
    print(f"{color}[STEP {n}] {title}{reset}")
    print(f"{'='*60}\n")


def print_field(label, value, indent=2):
    prefix = " " * indent
    print(f"{prefix}\033[36m{label}:\033[0m {value}")


def run_demo(data):
    instance = data["instance_id"]

    print(f"\n\033[1;33m{'='*60}")
    print(f"  Jingu Retry Demo: {instance}")
    print(f"{'='*60}\033[0m")
    print(f"\n  Problem: {data['problem']}")
    print(f"  This instance is resolved by Jingu on BOTH Sonnet 4.5 and 4.6")
    print(f"  (not resolved by model-only on either model)")

    # Step 1: Attempt 1 fails
    print_step(1, "Attempt 1 — INCOMPLETE FIX", "\033[1;31m")
    print("  Agent patches URLResolver.reverse() to filter None kwargs:")
    print()
    for line in data["a1_patch"].strip().split("\n"):
        if line.startswith("+"):
            print(f"  \033[32m{line}\033[0m")
        elif line.startswith("-"):
            print(f"  \033[31m{line}\033[0m")
        else:
            print(f"  {line}")
    print()
    tr = data["a1_test_results"]
    print_field("Tests", f"f2p = {tr['f2p_passed']}/{tr['f2p_total']} passed")
    print_field("Failing", ", ".join(tr["f2p_failing"]))
    print_field("Verdict", "\033[31mNOT RESOLVED\033[0m — patch fixes reverse() but not forward matching")

    # Step 2: Jingu Analysis
    print_step(2, "Jingu Failure Analysis + Routing", "\033[1;33m")
    print_field("Failure type", f"\033[33m{data['failure_type']}\033[0m")
    print_field("Analysis",
                "1/3 tests pass → patch is partially correct, but misses 2 test scenarios")
    fr = data["failure_routing"]
    print_field("Routing decision", f"→ {fr['next_phase']} phase")
    print_field("Repair goal", fr["repair_goal"])
    print_field("Execution feedback",
                "Injected: failing test names + output excerpt from Attempt 1")
    print()
    print("  \033[2mThe agent now knows:\033[0m")
    print("    - Its reverse() fix was partial (1/3 tests)")
    print("    - test_re_path_with_optional_parameter still fails")
    print("    - It needs to also handle forward matching in RegexPattern.match()")

    # Step 3: Attempt 2 succeeds
    print_step(3, "Attempt 2 — RESOLVED", "\033[1;32m")
    print("  Agent patches RegexPattern.match() to filter None from groupdict:")
    print()
    for line in data["a2_patch"].strip().split("\n"):
        if line.startswith("+"):
            print(f"  \033[32m{line}\033[0m")
        elif line.startswith("-"):
            print(f"  \033[31m{line}\033[0m")
        else:
            print(f"  {line}")
    print()
    tr2 = data["a2_test_results"]
    print_field("Tests", f"f2p = {tr2['f2p_passed']}/{tr2['f2p_total']} passed")
    print_field("Verdict", "\033[32mRESOLVED\033[0m")

    # Summary
    print(f"\n{'='*60}")
    print("\033[1m  Summary\033[0m")
    print(f"{'='*60}")
    print()
    print("  Without Jingu: Agent submits the A1 patch (1/3 tests). NOT RESOLVED.")
    print("  With Jingu:    Failure detected → routed to DESIGN → A2 fixes the")
    print("                 root cause (forward matching). RESOLVED.")
    print()
    print("  This pattern repeats across the benchmark:")
    print("    Sonnet 4.5: 16/30 → 19/30 (+3 with Jingu)")
    print("    Sonnet 4.6: 19/30 → 22/30 (+3 with Jingu)")
    print("    Opus 4.6:          23/30 (ceiling)")
    print()


def main():
    parser = argparse.ArgumentParser(description="Demo Jingu retry on SWE-bench")
    parser.add_argument("--instance", default="django__django-11477")
    parser.add_argument("--batch", default="best-config-v1")
    args = parser.parse_args()

    if args.instance == "django__django-11477":
        run_demo(DEMO_11477)
    else:
        print(f"Demo data not available for {args.instance}.")
        print("Currently supported: django__django-11477")
        sys.exit(1)


if __name__ == "__main__":
    main()
