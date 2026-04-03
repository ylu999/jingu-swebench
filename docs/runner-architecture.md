# jingu-swebench Runner Architecture

## AWS Account

**Account**: `235494812052` (jingu dedicated)
**Region**: `us-west-2`

---

## Execution Model

All runs execute inside a Docker container launched on EC2 via SSM.

```
Local machine (boto3)
  -> SSM send_command -> EC2 instance
    -> docker run --privileged jingu-swebench:latest
      -> docker-entrypoint.sh (starts dockerd)
        -> run_with_jingu_gate.py
          -> mini-swe-agent (per instance, parallel workers)
            -> SWE-bench eval container (Docker-in-Docker)
```

**Why --privileged**: The container needs to start SWE-bench eval containers (Docker-in-Docker).
**Why EC2, not ECS**: ECS does not support privileged DinD at the required level.

---

## Infrastructure

| Resource | ID / Name | Notes |
|----------|-----------|-------|
| ECR | `235494812052.dkr.ecr.us-west-2.amazonaws.com/jingu-swebench:latest` | Runner image |
| ASG | `jingu-swebench-ecs-asg` | Build and run instances |
| Launch Template | `lt-024c610e94921a069` v2 | c5.9xlarge, builder AMI |
| Builder AMI | `ami-068cfa06f1b8dd28c` | Pre-installed: git, nodejs 18, npm, docker, aws cli, ssm agent |
| IAM Profile | `ecsInstanceRole` | Bedrock + ECR push + SSM permissions |

**ecsInstanceRole permissions:**
- `jingu-swebench-bedrock` (inline): bedrock:InvokeModel + InvokeModelWithResponseStream
- `jingu-swebench-ecr-push` (inline): ECR push/pull
- `AmazonSSMManagedInstanceCore` (managed): SSM
- `AmazonEC2ContainerServiceforEC2Role` (managed): ECS agent

Credentials are provided by EC2 instance metadata (IMDSv2), auto-rotated hourly. No manual config needed.

---

## Scripts Baked Into Image

Scripts are COPYed into the Docker image at build time. They are NOT loaded from git at runtime.
**After any script change: git push -> rebuild image -> push to ECR.**

Image path: `/app/scripts/`

Dockerfile COPY list:
- `run_with_jingu_gate.py`, `jingu_gate_bridge.py`, `retry_controller.py`
- `strategy_logger.py`, `aggregate_strategies.py`, `preflight.py`
- `patch_reviewer.py`, `patch_signals.py`, `declaration_extractor.py`, `cognition_check.py`
- `gate_runner.js`, `patch_admission_policy.js`

**Adding a new script: must also add to Dockerfile COPY list, then rebuild.**

---

## Model

**claude-sonnet-4-5** via Amazon Bedrock cross-region inference.

Default workers: 10 (conservative; Bedrock quota: 10k RPM / 5M TPM).

---

## Results Layout

```
/root/results/            (EC2 host, mounted as /app/results in container)
  <batch-name>.log        (nohup log)
  <batch-name>/
    jingu-predictions.jsonl   (or baseline-predictions.jsonl)
    run_report.json
    strategy_log.jsonl
    <instance_id>/
      traj.json
      patch.diff
      gate_log.json
```

---

## Monitoring

```bash
tail -f /root/results/<batch-name>.log
grep 'progress' /root/results/<batch-name>.log | tail -3
wc -l /root/results/strategy_log.jsonl
```

Completion signal: `[progress] N/N done` and `report saved -> /app/results/.../run_report.json`

---

## Common Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ModuleNotFoundError: No module named 'X'` | New script not in Dockerfile COPY | Add to COPY list, rebuild image |
| `[preflight] FAIL [node]` | `node_modules` missing from image | npm install + rebuild |
| Script change has no effect | Old image still running | git push + rebuild + push to ECR |
| `~/.aws/credentials` causes auth errors | Static tokens overriding instance profile | `mv ~/.aws/credentials ~/.aws/credentials.bak` |
