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
  --instance-ids django__django-11019 django__django-11048 django__django-11087 \
                 django__django-11099 django__django-11133 django__django-11163 \
                 django__django-11239 django__django-11422 django__django-11564 \
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
  --instance-ids django__django-11019 django__django-11048 django__django-11087 \
                 django__django-11099 django__django-11133 django__django-11163 \
                 django__django-11239 django__django-11422 django__django-11564 \
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

Selected from SWE-bench Lite django subset: FTP=60-150, ps_len<=2500, single-file logic bugs.

```
django__django-11019   django__django-11048   django__django-11087
django__django-11099   django__django-11133   django__django-11163
django__django-11239   django__django-11422   django__django-11564
django__django-11905
```

Decision rule: if jingu delta >= 2 instances on calibration set, expand to 30-instance run.

## Architecture

```
run_with_jingu_gate.py
  mini-swe-agent (jingu-swebench.yaml config)
    agent produces patch
  jingu-trust-gate (B1 gate)
    gate_runner.js + patch_admission_policy.js
  retry_controller.py   structured hint on failure
  strategy_logger.py    JSONL log per attempt
  _run_official_evaluation() -> swebench.harness.run_evaluation
```

Config layer: `config/jingu-swebench.yaml` is baked into the Docker image at
`/usr/local/lib/python3.12/site-packages/minisweagent/config/benchmarks/jingu-swebench.yaml`.
Enforces FORBIDDEN ACTIONS (pip install, running tests, reproduction scripts).

## Build and deploy

See [docs/build-and-deploy.md](docs/build-and-deploy.md).
