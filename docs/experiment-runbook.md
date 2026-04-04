# Experiment Runbook — 3-Way Comparison

3-way comparison: **Official Verified** vs **Baseline** vs **Jingu**

---

## Instance Set (12 instances)

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

---

## Prerequisites

```bash
cd ~/jingu/repo/jingu-swebench

# 确认至少 1 台 c5.9xlarge ECS agent connected
python3 -c "
import boto3
ecs = boto3.client('ecs', region_name='us-west-2')
cis = ecs.describe_container_instances(cluster='jingu-swebench',
    containerInstances=ecs.list_container_instances(cluster='jingu-swebench')['containerInstanceArns'])['containerInstances']
for ci in cis:
    rem = {r['name']: r['integerValue'] for r in ci['remainingResources']}
    print(ci['ec2InstanceId'], 'connected='+str(ci['agentConnected']), 'cpu_free='+str(rem['CPU']))
"

# ECS agent 断连时重启（替换 <instance-id>）:
# python3 -c "import boto3; boto3.client('ssm','us-west-2').send_command(InstanceIds=['<instance-id>'], DocumentName='AWS-RunShellScript', Parameters={'commands':['sudo systemctl restart ecs']})"

# 代码有改动时 rebuild（约 10 分钟）:
# python scripts/ops.py build
```

---

## Launch

### Baseline
```bash
DATASET=Verified MODE=baseline MAX_ATTEMPTS=1 WORKERS=12 \
  ./scripts/smoke-test.sh exp-baseline-12-$(date +%Y%m%d) \
  django__django-11095 django__django-10097 django__django-13028 django__django-11433 \
  sympy__sympy-19346 sympy__sympy-13372 \
  sphinx-doc__sphinx-7757 sphinx-doc__sphinx-7910 \
  sphinx-doc__sphinx-10435 django__django-11820 django__django-12308 django__django-15695
```

### Jingu
```bash
DATASET=Verified MODE=jingu MAX_ATTEMPTS=1 WORKERS=12 \
  ./scripts/smoke-test.sh exp-jingu-12-$(date +%Y%m%d) \
  django__django-11095 django__django-10097 django__django-13028 django__django-11433 \
  sympy__sympy-19346 sympy__sympy-13372 \
  sphinx-doc__sphinx-7757 sphinx-doc__sphinx-7910 \
  sphinx-doc__sphinx-10435 django__django-11820 django__django-12308 django__django-15695
```

**smoke-test.sh 自动 tail 直到 task STOPPED，自动退出，无需手动。**

---

## Live Monitor（task 已在跑时）

```bash
# 实时 tail，task STOPPED 自动退出，无 timeout
python scripts/tail-logs.py <task-id>

# 只看关键信号
python scripts/tail-logs.py <task-id> --filter '\[jingu\]|\[control-plane\]|ACCEPTED|REJECTED|ERROR|Traceback'

# 查 task 当前状态（不 tail）
python scripts/ops.py status --task-id <task-id>
```

**禁止用 `ops.py logs`** — stream name 历史上写错，会无限 hang。

---

## 读结果

### 从 S3 下载
```bash
aws s3 sync s3://jingu-swebench-results/<batch-name>/ /tmp/<batch-name>/
python3 -c "import json; r=json.load(open('/tmp/<batch-name>/run_report.json')); print(json.dumps({k:v for k,v in r.items() if k!='instances'}, indent=2))"
```

### 3-way 对比表
```bash
python3 - <<'EOF'
import json, csv

with open('/Users/ysl/Downloads/claude-4-6-opus-mini-v2.csv') as f:
    official = {r['metadata.instance_id']: r for r in csv.DictReader(f)}

INSTANCES = [
    'django__django-11095','django__django-10097','django__django-13028','django__django-11433',
    'sympy__sympy-19346','sympy__sympy-13372',
    'sphinx-doc__sphinx-7757','sphinx-doc__sphinx-7910',
    'sphinx-doc__sphinx-10435','django__django-11820','django__django-12308','django__django-15695',
]

# 替换为实际 batch 名
BASELINE_DIR = '/tmp/exp-baseline-12-YYYYMMDD'
JINGU_DIR    = '/tmp/exp-jingu-12-YYYYMMDD'

def load_resolved(d):
    try:
        r = json.load(open(f'{d}/run_report.json'))
        return set(r.get('eval_results', {}).get('resolved_ids', []))
    except: return set()

b_res = load_resolved(BASELINE_DIR)
j_res = load_resolved(JINGU_DIR)

print(f"{'Instance':<45} Official  Baseline  Jingu")
print('-' * 75)
for iid in INSTANCES:
    off = '✅' if official.get(iid, {}).get('metadata.scores.resolved') == '1' else '❌'
    bas = '✅' if iid in b_res else '❌'
    jin = '✅' if iid in j_res else '❌'
    print(f'{iid:<45} {off:<9} {bas:<9} {jin}')

print()
print(f"Official: {sum(1 for i in INSTANCES if official.get(i,{}).get('metadata.scores.resolved')=='1')}/12")
print(f"Baseline: {len(b_res & set(INSTANCES))}/12")
print(f"Jingu:    {len(j_res & set(INSTANCES))}/12")
EOF
```

---

## Troubleshooting

| 症状 | 原因 | 动作 |
|------|------|------|
| `RESOURCE:CPU` | 没有空闲 c5.9xlarge | 检查 ECS agents，重启断连的 |
| `AGENT` not connected | ECS agent 断连 | SSM restart ecs |
| task 5 秒 exit=1 | 容器 crash | `python scripts/tail-logs.py <id> --all` 看 traceback |
| task RUNNING 无 `[jingu]` | dataset 下载慢 | 等 2–3 分钟 |
| instance IDs not found | dataset 传错（Lite vs Verified）| 检查 `DATASET=Verified` |

---

## Notes

- `WORKERS=12` 匹配 instance 数，1 worker per instance，约 5 分钟出结果
- `MAX_ATTEMPTS=1` 对齐官方 leaderboard
- Bedrock quota: 12 workers 远低于 40-worker 安全上限
- 结果自动上传 `s3://jingu-swebench-results/<batch-name>/`
- baseline 和 jingu 可以同时跑（两台 c5.9xlarge 各跑一个）
