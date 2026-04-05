# jingu-swebench — Claude Instructions

## 跑 case 前必读（强制，无例外）

**任何涉及以下操作，必须先读完 `.claude/smoke-test-runbook.md` 再执行：**

- launch batch（smoke-test.sh / ops.py run）
- build 镜像
- monitor / tail 日志
- 查看或分析结果
- 重跑实验

**跑 case 前的强制检查顺序（缺一不可）：**
1. 代码改动已 commit + push
2. `python scripts/ops.py build`（git pull + docker build + ECR push）
3. **Scale up ASG**（`DesiredCapacity=1`）
4. 确认 ECS agent connected
5. **⛔ Smoke test 1 instance** — 确认新行为出现在 log，向用户汇报结果
6. **等用户明确批准** batch launch
7. Launch batch（加 `--confirmed` flag）
8. 跑完立即 scale down（`DesiredCapacity=0`）

```
STOP — 先读 .claude/smoke-test-runbook.md
STOP — 未经用户批准，不得 launch 超过 3 个 instance
```

不读 runbook 直接操作 = 必然犯错。历史案例：
- 手动 SSM build 没 push ECR → ECS 用旧镜像 → fix 不生效，无报错
- instance IDs 拼成单个字符串 → ValueError，batch 0 秒 exit=1
- 用 `ops.py logs` → stream name 写错，无限 hang
- **未 smoke test 直接 launch 30 instances → 浪费资源，修复未验证**（2026-04-05）

---

## Build 铁律（最容易犯的错）

**唯一正确命令：**
```bash
python scripts/ops.py build
```

**禁止：** 手动 SSM `docker build`（不含 ECR push，ECS 拿不到新镜像）

验证：`ops.py build` 输出 `[ops] ECR latest: pushed=<timestamp>`，时间戳必须 > 你的 git push 时间。

---

## 禁止 timeout（严令禁止）

**禁止对任何长时间运行的命令设置 `timeout` 参数。**

原因：命令 fail early 时仍要傻等到 timeout 才能知道，浪费时间。

**正确做法：长时间命令必须用 `run_in_background=true`**，完成后自动通知，不阻塞。

适用命令（必须 background）：
- `python scripts/ops.py build`（约 10 分钟）
- `./scripts/smoke-test.sh ...`（视实例数，可能 30+ 分钟）
- 任何 SSM 命令超过 30 秒的

```
# 错误（严禁）
Bash(command="python scripts/ops.py build", timeout=900000)

# 正确
Bash(command="python scripts/ops.py build", run_in_background=true)
```

---

## 日志监控铁律（禁止傻等 smoke-test.sh 输出）

**smoke-test.sh 背后是 ECS 云端任务，本地 shell 只是一个长轮询包装。**

### 正确流程

1. 用 `run_in_background=true` 启动 smoke-test.sh
2. 立刻从 background output 里拿 task_id：
   ```
   grep "task_id=" <output_file>
   ```
3. **直接查 CloudWatch** 监控日志 — 不要读 smoke-test.sh 的 background output：
   ```python
   python3 /tmp/jingu_logs.py <task_id>
   ```

### 禁止的错误模式

```
# 错误：读 background output file 等 smoke-test.sh 输出（包含大量 LiteLLM 噪音，且同步阻塞）
cat /tmp/claude-xxx/tasks/<background_id>.output

# 正确：拿到 task_id 后直接查 CloudWatch
grep "task_id=" /tmp/claude-xxx/tasks/<background_id>.output
python3 /tmp/jingu_logs.py <task_id>
```

### /tmp/jingu_logs.py（每次 session 需要创建）

```python
import boto3, time, sys
task_id = sys.argv[1]
client = boto3.client('logs', region_name='us-west-2')
ecs = boto3.client('ecs', region_name='us-west-2')
STREAM = f'runner/runner/{task_id}'
LOG_GROUP = '/ecs/jingu-swebench'
SIGNALS = ['[phase_record]','[principal_gate]','[principal_inference]','[phase_injection]',
           '[cp-step]','[cp] ','[inner-verify]','DONE','FAILED','ACCEPTED','REJECTED',
           '[step ','[jingu]','pee:True','[verify_gate]','[init]','Traceback','Error','STOPPED']
resp = client.get_log_events(logGroupName=LOG_GROUP, logStreamName=STREAM, limit=500, startFromHead=True)
events = resp['events']
token = resp.get('nextForwardToken')
for e in events:
    for line in e['message'].split('\t'):
        line = line.strip()
        if line and any(s in line for s in SIGNALS):
            print(line[:200])
print('=== polling 8 rounds x 25s ===', flush=True)
for rnd in range(8):
    time.sleep(25)
    tasks = ecs.describe_tasks(cluster='jingu-swebench', tasks=[task_id]).get('tasks', [])
    status = tasks[0].get('lastStatus', '?') if tasks else '?'
    resp = client.get_log_events(logGroupName=LOG_GROUP, logStreamName=STREAM, limit=300, nextToken=token)
    new_events = resp['events']
    token = resp.get('nextForwardToken')
    for e in new_events:
        for line in e['message'].split('\t'):
            line = line.strip()
            if line and any(s in line for s in SIGNALS):
                print(line[:200], flush=True)
    print(f'--- round {rnd+1}: +{len(new_events)} events, status={status} ---', flush=True)
    if status == 'STOPPED':
        break
```
