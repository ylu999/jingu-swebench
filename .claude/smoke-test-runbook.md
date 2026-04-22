# Jingu SWE-bench Runbook

唯一的操作手册。包含 build、launch、monitor、结果分析、实验对比。

---

## ⛔ BATCH GUARD — 最高优先级规则（Claude 必须遵守）

**跑 batch 是昂贵操作，必须严格按以下顺序，缺一步不能继续：**

### Step 1 — Smoke Test（强制，不可跳过）

每次 build 新镜像后，**必须先只跑 1 个 instance** 验证行为符合预期：

```bash
python scripts/ops.py smoke \
  --batch-name smoke-$(date +%Y%m%d-%H%M) \
  --instance-ids django__django-10097 \
  --mode jingu --max-attempts 2 \
  --runbook-ack
```

验证内容（看 log 确认）：
- `[preflight] ALL CHECKS PASSED` — 容器启动正常
- `[init] strategy_log_path=...` — 路径正确
- 目标 fix（如 `[principal_gate]`、`[cognition_check]`）的日志确实出现
- 没有 `Traceback` / `ModuleNotFoundError`

### Step 2 — 向用户汇报，等待批准（强制）

Smoke test 跑完后，Claude **必须**：
1. 向用户展示关键 log 片段（preflight、gate 触发情况）
2. 说明 smoke test 通过/失败的结论
3. **明确请求用户批准**才能 launch 大 batch

```
Claude: "Smoke test 通过。[preflight] ALL CHECKS PASSED，[principal_gate] 触发了 X 次。
         可以 launch batch-p11（30 instances）吗？"
```

**不得在用户未明确批准的情况下自行 launch 大 batch。**

### Step 3 — Launch Batch（需要 --confirmed flag）

用户批准后，`ops.py run/smoke` 超过 3 个 instance 时 **必须加 `--confirmed` flag**：

```bash
python scripts/ops.py smoke \
  --batch-name batch-p11-v2-contract \
  --instance-ids django__django-10097 django__django-10554 ... \
  --mode jingu --max-attempts 2 --confirmed --runbook-ack
```

`--confirmed` 是 code-level guard（超过 3 个 instance 必须）。
`--runbook-ack` 是 code-level guard（所有 launch 必须）——证明本 session 已读 runbook。

---

## CRITICAL: Build 流程（必读，违反必出错）

### 唯一正确的 build 命令

```bash
python scripts/ops.py build
```

**这条命令做三件事，缺一不可：**
1. Scale up ASG → 启动一台 EC2
2. EC2 上：`git pull` → `npm install`（如需）→ `docker build`
3. **`docker push` 到 ECR** → ECS 任务才能拿到新镜像
4. Scale down ASG

**禁止手动 SSM build：** 手动在 EC2 上 `docker build` 而不 `docker push` → ECS 用的仍是 ECR 旧镜像 → 代码改动不生效，且不报任何错误。这是历史上犯过的错误，不要再重复。

### Build 完成的验证

`ops.py build` 输出最后一行：
```
[ops] ECR latest: pushed=<timestamp> digest=sha256:...
```
`pushed` 时间戳必须 > 你的 `git push` 时间。如果时间对不上，build 没生效。

### 标准工作流

```bash
# 1. 改代码
git add ... && git commit -m "..." && git push

# 2. Build + push 到 ECR（约 10 分钟，自动 scale up → build → scale down）
python scripts/ops.py build

# 3. Scale up EC2（build 完后 ASG=0，跑 case 前必须手动 scale up）
#    baseline + jingu 并行 → 需要 2 台；只跑一批 → 1 台
python3 -c "
import boto3
asg = boto3.client('autoscaling', region_name='us-west-2')
asg.set_desired_capacity(AutoScalingGroupName='jingu-swebench-ecs-asg', DesiredCapacity=2)
print('ASG desired=2, waiting for instances...')
"
# 等 ECS agent 连上（约 60s），确认：
python3 -c "
import boto3
ecs = boto3.client('ecs', region_name='us-west-2')
arns = ecs.list_container_instances(cluster='jingu-swebench')['containerInstanceArns']
cis = ecs.describe_container_instances(cluster='jingu-swebench', containerInstances=arns)['containerInstances'] if arns else []
for ci in cis:
    rem = {r['name']: r['integerValue'] for r in ci['remainingResources']}
    print(ci['ec2InstanceId'], 'connected='+str(ci['agentConnected']), 'cpu_free='+str(rem.get('CPU',0)))
print(f'{len(cis)} instance(s) registered')
"

# 4. Launch（见下方 Launch 章节）

# 5. 跑完后 scale down（节省费用）
python3 -c "
import boto3
asg = boto3.client('autoscaling', region_name='us-west-2')
asg.set_desired_capacity(AutoScalingGroupName='jingu-swebench-ecs-asg', DesiredCapacity=0)
print('ASG desired=0')
"
```

**Scale 规则：**
- `ops.py build` 会自动 scale up → down，build 后 ASG=0
- 跑 case 前必须手动 scale up（build 不等于 case 环境就绪）
- baseline + jingu 并行 → `DesiredCapacity=2`；只跑一批 → `DesiredCapacity=1`
- 跑完立即 scale down，EC2 按小时计费

---

## Scale Up（Launch 前必做）

```python
import boto3, time

# Scale up
asg = boto3.client('autoscaling', region_name='us-west-2')
n = 2  # baseline + jingu 并行用 2；只跑一批用 1
asg.set_desired_capacity(AutoScalingGroupName='jingu-swebench-ecs-asg', DesiredCapacity=n)
print(f'ASG desired={n}')

# 等 ECS agent 连上（约 60s）
time.sleep(60)
ecs = boto3.client('ecs', region_name='us-west-2')
arns = ecs.list_container_instances(cluster='jingu-swebench')['containerInstanceArns']
cis = ecs.describe_container_instances(cluster='jingu-swebench', containerInstances=arns)['containerInstances'] if arns else []
for ci in cis:
    rem = {r['name']: r['integerValue'] for r in ci['remainingResources']}
    print(ci['ec2InstanceId'], 'connected='+str(ci['agentConnected']), 'cpu_free='+str(rem.get('CPU', 0)))
if not all(ci['agentConnected'] for ci in cis):
    print('WARNING: some agents not connected yet, wait another 30s and re-check')
```

## Scale Down（跑完立即执行）

```python
import boto3
asg = boto3.client('autoscaling', region_name='us-west-2')
asg.set_desired_capacity(AutoScalingGroupName='jingu-swebench-ecs-asg', DesiredCapacity=0)
print('ASG desired=0')
```

---

## Launch

### 环境变量

| 变量 | 默认值 | 可选值 |
|------|--------|--------|
| `DATASET` | `Verified` | `Lite` / `Verified` |
| `MODE` | `jingu` | `jingu` / `baseline` |
| `MAX_ATTEMPTS` | `2` | 任意正整数 |
| `WORKERS` | 实例数 | 任意正整数 |

### 启动命令

```bash
# 单实例 smoke（验证新镜像是否生效）
MAX_ATTEMPTS=1 ./scripts/smoke-test.sh smoke-$(date +%Y%m%d) django__django-11039

# baseline 模式
DATASET=Verified MODE=baseline MAX_ATTEMPTS=1 \
  ./scripts/smoke-test.sh exp-baseline-$(date +%Y%m%d) \
  django__django-11039 django__django-12470

# jingu 模式
DATASET=Verified MODE=jingu MAX_ATTEMPTS=1 \
  ./scripts/smoke-test.sh exp-jingu-$(date +%Y%m%d) \
  django__django-11039 django__django-12470
```

`smoke-test.sh` 自动 tail 到 task STOPPED，无需手动干预，无超时。

### baseline 和 jingu 可以同时跑

两台 c5.9xlarge 各跑一个，互不干扰。在两个终端分别启动即可。

---

## 12-Instance 对比实验（3-way: Official vs Baseline vs Jingu）

### Instance Set

```
# Resolved by official (8)
django__django-11095
django__django-10097
django__django-13028
django__django-11433
sympy__sympy-19346
sympy__sympy-13372
sphinx-doc__sphinx-7757
sphinx-doc__sphinx-7910

# Unresolved by official (4)
sphinx-doc__sphinx-10435
django__django-11820
django__django-12308
django__django-15695
```

Official reference: `~/Downloads/claude-4-6-opus-mini-v2.csv` (378/500 resolved)

### Launch（两个终端并行）

**Terminal 1 — baseline:**
```bash
cd ~/jingu/repo/jingu-swebench
DATASET=Verified MODE=baseline MAX_ATTEMPTS=1 WORKERS=12 \
  ./scripts/smoke-test.sh exp-baseline-12-$(date +%Y%m%d) \
  django__django-11095 django__django-10097 django__django-13028 django__django-11433 \
  sympy__sympy-19346 sympy__sympy-13372 \
  sphinx-doc__sphinx-7757 sphinx-doc__sphinx-7910 \
  sphinx-doc__sphinx-10435 django__django-11820 django__django-12308 django__django-15695
```

**Terminal 2 — jingu:**
```bash
cd ~/jingu/repo/jingu-swebench
DATASET=Verified MODE=jingu MAX_ATTEMPTS=1 WORKERS=12 \
  ./scripts/smoke-test.sh exp-jingu-12-$(date +%Y%m%d) \
  django__django-11095 django__django-10097 django__django-13028 django__django-11433 \
  sympy__sympy-19346 sympy__sympy-13372 \
  sphinx-doc__sphinx-7757 sphinx-doc__sphinx-7910 \
  sphinx-doc__sphinx-10435 django__django-11820 django__django-12308 django__django-15695
```

---

## Live Monitor

`smoke-test.sh` 已经内置 tail，不需要额外 monitor。

task 已在跑时，用 `tail-logs.py`：

```bash
# LLM 每个 step 实时输出（snippet 前 80 字符）+ 关键信号
python scripts/tail-logs.py <task-id> \
  --filter '\[step|\[jingu\]|\[control-plane\]|\[cp-step\]|ACCEPTED|FAILED|ERROR|Traceback'

# 只看 LLM step（每步说了什么）
python scripts/tail-logs.py <task-id> --filter '\[step'

# 全量（去 dockerd 噪音）— 最详细
python scripts/tail-logs.py <task-id>

# 自动轮询 signal 日志（推荐，每30s拉一次，task STOPPED 自动停）
python scripts/ops.py peek --task-id <task-id>

# 单次快照
python scripts/ops.py peek --task-id <task-id> --once

# 查状态（不 tail）
python scripts/ops.py status --task-id <task-id>
```

**LLM step 输出格式：**
```
    [step 12] $0.14  I need to look at the test file to understand what's expected...
    [step 13] $0.18  Let me check the implementation in models.py...
```
每行：step 编号、累计花费、LLM 当前 step 说的前 80 字符。实时 flush，无延迟。

**禁止用 `ops.py logs`** — stream name 历史上写错，会无限 hang。

---

## 查看进度（Per-Instance 追踪）

所有历史数据统一存储在 `s3://jingu-swebench-results/pipeline-results/instances/`。

```bash
# repo 维度汇总表（ran / accepted / not accepted / eval resolved）
python scripts/ops.py summary

# batch 历史表（resolved rate 趋势）
python scripts/ops.py history

# 新 batch 跑完后，同步 traj + eval 数据进 per-instance records
python scripts/ops.py backfill
# 只处理指定 batch：
python scripts/ops.py backfill --batches batch-p26-xxx
```

**per-instance record 结构：**
```json
{
  "instance_id": "django__django-10097",
  "last_batch": "batch-p25-b10",
  "last_commit": "5bee637a1b36",
  "accepted": true,
  "eval_resolved": true,
  "runs": [
    {"batch": "...", "git_commit": "...", "accepted": true, "eval_resolved": true},
    ...
  ]
}
```

**Eval 数据来源：**
- 新 pipeline 跑完 → `docker-entrypoint.sh` 生成 `eval_results.json`（含 `resolved_ids`/`unresolved_ids`）→ 上传 S3
- `cmd_pipeline` Step 3 自动读取 → 写入 per-instance `eval_resolved=true/false`
- `cmd_backfill` 也会尝试读取 `eval-<batch>/eval_results.json`
- 历史 batch（无 `eval_results.json`）的 eval 数据为 `null`（只有 batch 级别的 `_KNOWN_EVAL_RESULTS`）

---

## 读结果

### 从 S3 下载

```bash
aws s3 sync s3://jingu-swebench-results/<batch-name>/ /tmp/<batch-name>/
python3 -c "
import json
r = json.load(open('/tmp/<batch-name>/run_report.json'))
print(json.dumps({k: v for k, v in r.items() if k != 'instances'}, indent=2))
"
```

### 3-way 对比表

```python
import json, csv

with open('/Users/ysl/Downloads/claude-4-6-opus-mini-v2.csv') as f:
    official = {r['metadata.instance_id']: r for r in csv.DictReader(f)}

INSTANCES = [
    'django__django-11095', 'django__django-10097', 'django__django-13028', 'django__django-11433',
    'sympy__sympy-19346', 'sympy__sympy-13372',
    'sphinx-doc__sphinx-7757', 'sphinx-doc__sphinx-7910',
    'sphinx-doc__sphinx-10435', 'django__django-11820', 'django__django-12308', 'django__django-15695',
]

# 替换为实际 batch 名（date +%Y%m%d 部分）
DATE = 'YYYYMMDD'
BASELINE_DIR = f'/tmp/exp-baseline-12-{DATE}'
JINGU_DIR    = f'/tmp/exp-jingu-12-{DATE}'

def load_resolved(d):
    try:
        r = json.load(open(f'{d}/run_report.json'))
        return set(r.get('eval_results', {}).get('resolved_ids', []))
    except:
        return set()

b_res = load_resolved(BASELINE_DIR)
j_res = load_resolved(JINGU_DIR)

print(f"{'Instance':<45} Official  Baseline  Jingu")
print('-' * 75)
for iid in INSTANCES:
    off = 'Y' if official.get(iid, {}).get('metadata.scores.resolved') == '1' else 'N'
    bas = 'Y' if iid in b_res else 'N'
    jin = 'Y' if iid in j_res else 'N'
    print(f'{iid:<45} {off:<9} {bas:<9} {jin}')

print()
print(f"Official: {sum(1 for i in INSTANCES if official.get(i,{}).get('metadata.scores.resolved')=='1')}/12")
print(f"Baseline: {len(b_res & set(INSTANCES))}/12")
print(f"Jingu:    {len(j_res & set(INSTANCES))}/12")
```

---

## Early Failure 快速诊断

| 症状 | 根因 | 动作 |
|------|------|------|
| `smoke-test.sh` 报 "task already STOPPED" | 容器 crash | `python scripts/ops.py status --task-id <id>` 看 exit code |
| task 5s exit=1 | entrypoint crash / ModuleNotFoundError | `python scripts/ops.py peek --task-id <id> --all --once` 看 traceback |
| `ModuleNotFoundError: No module named 'X'` | 新 script 没加进 Dockerfile COPY，或 build 没用 `ops.py build` | 检查 Dockerfile COPY 清单，重新 `ops.py build` |
| sympy/sphinx 报 `'accepted'` KeyError | onboarding-fail 返回 dict 缺 accepted key（已在 commit 21adb7c 修复） | 确认用的是修复后的镜像（`ops.py build` 时间 > commit 21adb7c 时间） |
| 代码改了但行为没变 | 手动 SSM build 没 push 到 ECR | 必须用 `python scripts/ops.py build`，不要手动 build |
| `RESOURCE:CPU` | 没有空闲 c5.9xlarge | 检查 ECS agent 状态，必要时重启 |
| ECS agent 断连 | agent 进程挂了 | `boto3 ssm.send_command(['sudo systemctl restart ecs'])` |
| instance IDs not found | dataset 传错（Lite vs Verified）| 确认命令里有 `DATASET=Verified` |
| task RUNNING 无 `[jingu]` 日志 | dataset 下载慢 | 等 2-3 分钟再查 |

---

## Dockerfile COPY 清单（updated for p225-p235）

```dockerfile
COPY scripts/run_with_jingu_gate.py \
     scripts/jingu_gate_bridge.py \
     scripts/retry_controller.py \
     scripts/strategy_logger.py \
     scripts/aggregate_strategies.py \
     scripts/preflight.py \
     scripts/patch_reviewer.py \
     scripts/patch_signals.py \
     scripts/declaration_extractor.py \
     scripts/cognition_check.py \
     scripts/cognition_schema.py \
     scripts/gate_runner.js \
     scripts/patch_admission_policy.js \
     scripts/subtype_contracts.py \
     scripts/phase_prompt.py \
     scripts/principal_gate.py \
     scripts/principal_inference.py \
     scripts/phase_record.py \
     scripts/in_loop_judge.py \
     scripts/verification_evidence.py \
     scripts/governance_pack.py \
     scripts/governance_runtime.py \
     scripts/swebench_failure_reroute_pack.py \
     scripts/unresolved_case_classifier.py \
     scripts/phase_record_pack.py \
     scripts/failure_classifier.py \
     scripts/repair_prompts.py \
     scripts/analysis_gate.py \
     scripts/gate_rejection.py \
     scripts/failure_routing.py \
     scripts/extract_failure_events.py \
     scripts/compute_routing_stats.py \
     scripts/suggest_routing.py \
     scripts/strategy_prompts.py \
     scripts/check_onboarding.py \
     scripts/phase_validator.py \
     scripts/phase_schemas.py \
     scripts/cognition_prompts.py \
     scripts/jingu_onboard.py \
     # p225: decomposition (CRITICAL runtime imports)
     scripts/step_monitor_state.py \
     scripts/signal_extraction.py \
     scripts/controlled_verify.py \
     scripts/jingu_adapter.py \
     # p225: core agent + step processing
     scripts/jingu_agent.py \
     scripts/step_sections.py \
     # p240: multi-candidate direction selection
     scripts/candidate_selection.py \
     # p228-p235: visibility + replay
     scripts/step_event_emitter.py \
     scripts/decision_logger.py \
     scripts/checkpoint.py \
     scripts/replay_engine.py \
     scripts/replay_cli.py \
     scripts/replay_traj.py \
     scripts/traj_diff.py \
     scripts/prompt_regression.py \
     /app/scripts/
COPY scripts/control/ /app/scripts/control/
COPY scripts/cognition_contracts/ /app/scripts/cognition_contracts/
COPY python/jingu_loader/ /app/python/jingu_loader/
COPY bundle.json /app/bundle.json
```

新增 script 必须同时加入这个清单，否则容器里 `ModuleNotFoundError`。

---

## 基础信息

| 项目 | 值 |
|------|-----|
| ECS Cluster | `jingu-swebench` |
| ECR | `235494812052.dkr.ecr.us-west-2.amazonaws.com/jingu-swebench:latest` |
| Log Group | `/ecs/jingu-swebench` |
| Log Stream | `runner/runner/<task-id>` |
| Region | `us-west-2` |
| EC2 type | `c5.9xlarge`（36 vCPU，72 GB RAM）|
| ASG | `jingu-swebench-ecs-asg` |
