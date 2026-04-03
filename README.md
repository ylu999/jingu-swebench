# jingu-swebench

Measures jingu's improvement over baseline on SWE-bench Lite.

## What this does

Runs the same LLM (claude-sonnet-4-5 via Bedrock) in two modes on the same SWE-bench instances:

- **baseline**: 2 attempts, no gate, attempt 2 gets no structured hint (truly naive)
- **jingu**: 2 attempts, B1 trust gate + structured retry with failure evidence

Resolved rate is measured via `python -m swebench.harness.run_evaluation` (official harness).
Delta = jingu resolved rate − baseline resolved rate.

## Running a calibration experiment

### Prerequisites

Runs in Docker container on EC2 via SSM. See [docs/build-and-deploy.md](docs/build-and-deploy.md).

### Baseline run

```bash
docker run --rm --privileged \
  -v /root/results:/app/results \
  -e AWS_DEFAULT_REGION=us-west-2 \
  jingu-swebench:latest \
  --instance-ids django__django-11019 django__django-11039 django__django-11049 \
                 django__django-11099 django__django-11133 django__django-11179 \
                 django__django-11422 django__django-11564 django__django-11620 \
                 django__django-11905 \
  --mode baseline --max-attempts 2 --workers 5 \
  --output /app/results/calib-v1-baseline \
  --run-eval --run-id calib-v1-baseline
```

### Jingu run

Same instances, same budget, mode=jingu:

```bash
docker run --rm --privileged \
  -v /root/results:/app/results \
  -e AWS_DEFAULT_REGION=us-west-2 \
  jingu-swebench:latest \
  --instance-ids django__django-11019 django__django-11039 django__django-11049 \
                 django__django-11099 django__django-11133 django__django-11179 \
                 django__django-11422 django__django-11564 django__django-11620 \
                 django__django-11905 \
  --mode jingu --max-attempts 2 --workers 5 \
  --output /app/results/calib-v1-jingu \
  --run-eval --run-id calib-v1-jingu
```

### Read results

```bash
for mode in baseline jingu; do
  echo "=== $mode ==="
  python3 -c "
import json, sys
r = json.load(open('/root/results/calib-v1-$mode/run_report.json'))
e = r.get('eval_results', {})
a = r.get('attempt_stats', {})
print('resolved:', e.get('resolved_rate'), f'({e.get(\"resolved_count\")}/{e.get(\"total\")})')
if mode == 'jingu':
    print('rescued_rate:', a.get('rescued_rate'))
    print('failure_breakdown:', r.get('failure_breakdown'))
"
done
```

## CLI reference

```
run_with_jingu_gate.py
  --instance-ids ID [ID ...]   SWE-bench Lite instance IDs
  --mode {jingu,baseline}      default: jingu
  --max-attempts N             default: 2
  --workers N                  default: 10
  --output DIR                 output directory
  --run-eval                   run official harness after inference
  --run-id STR                 run identifier (required with --run-eval)
```

## Output files

```
<output>/
  jingu-predictions.jsonl      (or baseline-predictions.jsonl)
  run_report.json              summary: attempt_stats, failure_breakdown, eval_results
  <instance_id>/
    traj.json                  full agent trajectory
    patch.diff                 accepted patch
    gate_log.json              gate decision per attempt
```

`run_report.json` key fields:
```json
{
  "mode": "jingu",
  "run_id": "calib-v1-jingu",
  "attempt_stats": {
    "attempt1_accepted": 6,
    "attempt2_rescued": 2,
    "rescued_rate": 0.50
  },
  "failure_breakdown": {"no_test_progress": 3, "wrong_direction": 2},
  "eval_results": {
    "resolved_count": 8,
    "total": 10,
    "resolved_rate": 0.80,
    "resolved_ids": ["django__django-11019", "..."]
  }
}
```

## Calibration instance set (10 instances)

Selected from SWE-bench Lite django subset: FTP=1-2, ps_len<=2500, single-file logic bugs.
All IDs verified present in SWE-bench/SWE-bench_Lite (300 instances).

```
django__django-11019   django__django-11039   django__django-11049
django__django-11099   django__django-11133   django__django-11179
django__django-11422   django__django-11564   django__django-11620
django__django-11905
```

Decision rule: if jingu delta >= 2 instances on calibration set, expand to 30-instance run.

## Architecture

### What mini-swe-agent does (one attempt)

mini-swe-agent is a bash-only agent with linear message history (source: mini-swe-agent README +
`default.py`). Each call to `process_instance()` runs one attempt:

```
process_instance(instance, output_dir, config, progress)
  get_sb_environment()        # pull SWE-bench Docker image, start container
  DefaultAgent.run(problem_statement)
    add_messages(system, user)        # rendered from jingu-swebench.yaml templates
    while True:
      query()                         # call LLM -> bash command
      execute_actions()               # docker exec in container, append result to messages
      if messages[-1].role == "exit": break
    return {"exit_status": ..., "submission": <git diff>}
  write preds.json
```

The agent reads code, runs bash, produces a patch. It does not see test results — test execution
is the harness's job, not the agent's.

### Where jingu hooks in

Jingu's retry loop wraps `process_instance`. It does not modify the agent's internal loop:

```
for attempt in 1..max_attempts:
    # inject hint into instance_template before this attempt
    config["agent"]["instance_template"] += hint_from_last_failure

    process_instance(instance, attempt_dir, config, progress)
    #   ^--- DefaultAgent.run() is monkey-patched here to run controlled_verify
    #        on the same container BEFORE it is destroyed (mid-run signal only)

    patch = read from traj.json

    # jingu gate (B1): structural patch evaluation, not test execution
    gate_result = evaluate_patch(patch, traj)
    if gate_result.accepted: break

    # build hint for next attempt from gate failure classification
    # + controlled_verify output (test counts, failing test names)
    last_failure = retry_controller(failure_class, exec_feedback)

best patch -> predictions.jsonl
_run_official_evaluation() -> python -m swebench.harness.run_evaluation  # final score only
```

**controlled_verify** (`run_controlled_verify`): orchestrator runs `git apply` + FAIL_TO_PASS
tests via `docker exec` on the already-running container. Used to build the retry hint
(failure counts, test names). It is mid-run signal only — not the official score.

**Official score**: `swebench.harness.run_evaluation` runs after all inference is done,
on a fresh container per instance. Its resolved_rate is the benchmark number.

### Prompt injection

`AgentConfig` (mini-swe-agent) has only `system_template` and `instance_template`.
There is no `instance_template_extra` field. We append directly to `instance_template`
before each attempt (`run_with_jingu_gate.py:1441`):

```python
config["agent"]["instance_template"] = (
    config["agent"]["instance_template"] + "\n\n" + "\n\n".join(extra_parts)
)
```

`extra_parts` contains: DECLARATION PROTOCOL + FAIL_TO_PASS test list + previous failure hint.

### Config layer

`config/jingu-swebench.yaml` is baked into the Docker image at
`/usr/local/lib/python3.12/site-packages/minisweagent/config/benchmarks/jingu-swebench.yaml`.
Fork of the official `swebench.yaml`. Adds FORBIDDEN ACTIONS block (pip install, running tests,
reproduction scripts). Agent sees these constraints for the full attempt.

## Build and deploy

See [docs/build-and-deploy.md](docs/build-and-deploy.md).
