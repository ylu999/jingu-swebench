# Smoke Test Runbook

## 标准工作流

```bash
# 1. 改代码 → commit → push
git add ... && git commit -m "..." && git push

# 2. Build 镜像（约 10 分钟）
python scripts/ops.py build

# 3. 启动 smoke test（自动尾随日志到结束）
./scripts/smoke-test.sh b3-smoke-$(date +%Y%m%d) django__django-11039 django__django-12470 django__django-10914

# 4. 对已跑完的任务查看日志
./scripts/logs.sh <task-id>
./scripts/logs.sh <task-id> '\[control-plane\]'
./scripts/logs.sh <task-id> 'cp-step|control-plane|STOPPING|verdict'
```

---

## Scripts

### `scripts/tail-logs.py` — 实时 tail（首选）

任何正在跑或刚启动的 task，直接 tail：

```bash
# 基础用法：去噪，全量显示
python scripts/tail-logs.py <task-id>

# 只看关键信号（启动 + CP + 报错）
python scripts/tail-logs.py <task-id> \
  --filter '\[entrypoint\]|\[preflight\]|\[init\]|\[jingu\]|ERROR|FAILED|\[control-plane\]|\[cp-step\]'

# 只看 control-plane verdict
python scripts/tail-logs.py <task-id> --filter '\[control-plane\]'

# 所有行（含 dockerd 噪音）
python scripts/tail-logs.py <task-id> --all

# 调整 poll 间隔（默认 5s）
python scripts/tail-logs.py <task-id> --interval 10
```

**行为**：
- log stream 未出现 → 等待（最多 3 min），task STOPPED 即 early-fail exit
- task STOPPED + stream 耗尽 → 自动退出（不需要 Ctrl-C）
- ERROR/Traceback 等行自动加 ⚠️ 标记
- 可 Ctrl-C 随时中断

### `scripts/smoke-test.sh` — 一键启动+尾随

- 启动 ECS 任务
- 等 task 到 RUNNING（最多 3 分钟），STOPPED 即 early-fail
- 等 log stream 出现（最多 2 分钟），STOPPED 即 early-fail
- 实时打印日志（过滤 dockerd/containerd 噪音）
- Task STOPPED + 无新日志 → 自动退出
- 打印 final status + control-plane summary

```bash
./scripts/smoke-test.sh <batch-name> <instance-id> [instance-id ...]

# 可选环境变量
MAX_ATTEMPTS=1 ./scripts/smoke-test.sh b3-quick django__django-12470
WORKERS=1 ./scripts/smoke-test.sh b3-quick django__django-12470
```

### `scripts/logs.sh` — 事后查日志

对已完成（或仍在跑）的任务，fetch 全量日志并过滤。

```bash
./scripts/logs.sh <task-id>                            # 全量（去除 dockerd 噪音）
./scripts/logs.sh <task-id> '\[control-plane\]'        # 只看 verdict
./scripts/logs.sh <task-id> 'cp-step|control-plane'    # cp-step + verdict
./scripts/logs.sh <task-id> 'STOPPING|task_success'    # 只看成功/停止
```

### `scripts/ops.py` — 底层工具

```bash
python scripts/ops.py build                    # rebuild + push 镜像
python scripts/ops.py status --task-id <id>   # 快速看 task 状态
python scripts/ops.py run ...                  # 手动启动（不尾随日志）
```

---

## CloudWatch 关键事实

| 项目 | 值 |
|------|-----|
| Log Group | `/ecs/jingu-swebench` |
| Log Stream | `runner/runner/<task-id>` |
| ECS Cluster | `jingu-swebench` |
| ECR | `235494812052.dkr.ecr.us-west-2.amazonaws.com/jingu-swebench:latest` |

**注意**：`ops.py logs` 命令历史上 stream name 写错（`ecs/runner/` 而不是 `runner/runner/`），
已修复。不要直接用旧的 `ops.py logs`，用 `logs.sh` 替代。

---

## Early Failure 判断

| 症状 | 原因 | 动作 |
|------|------|------|
| `smoke-test.sh` 报 "task already STOPPED" | 容器 crash，没跑起来 | `python scripts/ops.py status --task-id <id>` 看 exit code + reason |
| Log stream 2 分钟后仍不存在 | dockerd 启动失败 / entrypoint crash | CloudWatch 控制台直接搜 task-id |
| Task RUNNING 但无 `[jingu]` 日志 | 依赖下载慢 / dataset 下载慢 | 等 2-3 分钟再查 |
| `[jingu] ERROR` 出现 | 代码问题 | 看 traceback，修代码 |

---

## Control-Plane 关键日志格式

```
# 每个 agent step（有信号才打印）
[cp-step] instance=django__django-11039 attempt=1 signals=['patch'] no_progress:0 step:31 env_noise:False actionability:1 weak_progress:True

# 每个 attempt boundary（verify 后）
[control-plane] instance=django-11039 attempt=1 state=phase:OBSERVE step:254 no_progress:1 task_success:True
[control-plane] instance=django-11039 attempt=1 verdict=VerdictStop(type='STOP', reason='task_success')
[control-plane] instance=django-11039 STOPPING — reason=task_success
```

**B3.2 验证标准**：整个 attempt 过程中 `no_progress` 应保持 0，只在 verify boundary 才增加。
若 `no_progress` 在 step 阶段快速递增（每步+1），说明 `update_stagnation=False` 没生效。

---

## 历史失败案例

### 2026-04-04: ops.py logs 一直 hang

**症状**：`python scripts/ops.py logs --task-id ...` 跑了 30 分钟没输出，task 早已 STOPPED。

**根因**：
1. Stream name 写错：`ecs/runner/<id>` → 实际是 `runner/runner/<id>`，导致一直触发 `ResourceNotFoundException`
2. `while True` 循环里只打印 "waiting" 但不检查 task 是否已经 STOPPED
3. Claude 用 `TaskOutput(block=true, timeout=300000)` 等待，但 Bash 命令根本没退出

**修复**：
- `ops.py cmd_logs`：stream name 改为 `runner/runner/{task_id}`，加 2 分钟超时，加 task STOPPED 检测
- 新增 `scripts/smoke-test.sh`：完整生命周期管理，task STOPPED 即退出
- 新增 `scripts/logs.sh`：事后查日志，不会 hang

**教训**：不要用 `while True + sleep` 等待外部 API，总要有超时和 early exit 条件。
