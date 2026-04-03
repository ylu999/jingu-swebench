# jingu-swebench Runner Architecture

## AWS Account

**Account**: `235494812052` (jingu 专用账号)
**Region**: `us-west-2`

**不使用 `cloud` SSH alias** — 那台机器是 Amazon 内部 dev desktop（账号 `123367104812`），
不在 jingu 账号里，credentials 12h 过期，永远需要手动推。

---

## 两种运行模式

### Mode A — ECS (生产批量跑，不推荐用于 p169 实验)

ECS task 跑在 Docker container 里，但：
- container 内部需要 Docker daemon（testbed 是 Docker 容器）
- ECS 不支持 Docker-in-Docker
- **结论：ECS 不能跑 run_with_jingu_gate.py**

ASG `jingu-swebench-ecs-asg` + LT `jingu-swebench-ecs-lt` 仅用于 ECS worker，不用于 runner。

### Mode B — Runner EC2 (实验跑，当前推荐方案)

直接在 EC2 host 上跑 Python 脚本，EC2 有 Docker daemon，可以起 testbed container。

**需要的环境**：
- Python 3.12 + pip (mise 管理)
- Node.js 18 (gate_runner.js 需要)
- Docker (testbed container)
- jingu-swebench repo (`~/jingu-swebench/`)
- jingu-trust-gate (`~/jingu-swebench/jingu-trust-gate/`)
- boto3 + litellm + 其他依赖

**当前问题**：`ami-068cfa06f1b8dd28c` (jingu-swebench-builder-20260402) 上没有：
- mise / Python 3.12
- boto3
- node

需要基于现有 ECS 实例重建 AMI。

---

## Infrastructure 现状

| 资源 | ID / 名称 | 说明 |
|------|-----------|------|
| AMI (builder) | `ami-068cfa06f1b8dd28c` | jingu-swebench-builder-20260402，有 git/docker/aws-cli，**无 Python 3.12/boto3/node** |
| AMI (ECS) | `ami-060921e471f88bf4c` | ECS worker AMI，有 Python 3.9/Docker，**无 mise/boto3/node** |
| ASG | `jingu-swebench-ecs-asg` | ECS worker 用，LT=jingu-swebench-ecs-lt |
| LT (ECS) | `lt-024c610e94921a069` jingu-swebench-ecs-lt | c5.9xlarge, ECS AMI |
| LT (runner) | `lt-03cd70e0699fbcc90` jingu-swebench-runner-lt | c5.4xlarge, builder AMI，**AMI 需修** |
| IAM Profile | `ecsInstanceRole` | 两个 LT 都用，有 Bedrock + ECR + SSM 权限 |
| SG | `sg-098d7e41bdb28cd46` | 两个 LT 都用 |

---

## 正确的 Runner EC2 启动流程

### Step 1 — 准备好的 Runner AMI（一次性）

当前 `ami-068cfa06f1b8dd28c` 缺 Python 3.12 + boto3 + node。
需要起一台，手动装好，再 bake 成新 AMI，更新 runner LT。

```bash
# 起临时实例
INSTANCE_ID=$(aws ec2 run-instances \
  --region us-west-2 \
  --launch-template LaunchTemplateId=lt-03cd70e0699fbcc90 \
  --count 1 \
  --query 'Instances[0].InstanceId' --output text)

# 等 SSM ready
aws ec2 wait instance-running --region us-west-2 --instance-ids $INSTANCE_ID

# SSM 进去装环境（用 start-session 交互式，或 send-command 批量）
aws ssm start-session --region us-west-2 --target $INSTANCE_ID

# 在实例里：
# curl -fsSL https://mise.run | sh
# mise use --global python@3.12 node@18
# pip install boto3 litellm minisweagent
# git clone https://github.com/ylu999/jingu-swebench ~/jingu-swebench
# cd ~/jingu-swebench && npm install  # 安装 jingu-trust-gate

# bake AMI
NEW_AMI=$(aws ec2 create-image \
  --instance-id $INSTANCE_ID \
  --name "jingu-swebench-runner-$(date +%Y%m%d)" \
  --no-reboot \
  --region us-west-2 \
  --query 'ImageId' --output text)

# 更新 runner LT 到新 AMI
aws ec2 create-launch-template-version \
  --region us-west-2 \
  --launch-template-id lt-03cd70e0699fbcc90 \
  --source-version '$Latest' \
  --launch-template-data "{\"ImageId\":\"$NEW_AMI\"}"

# terminate 临时实例
aws ec2 terminate-instances --region us-west-2 --instance-ids $INSTANCE_ID
```

### Step 2 — 每次跑 batch 的流程

```bash
# 1. 起 runner 实例
INSTANCE_ID=$(aws ec2 run-instances \
  --region us-west-2 \
  --launch-template LaunchTemplateId=lt-03cd70e0699fbcc90 \
  --count 1 \
  --query 'Instances[0].InstanceId' --output text)

aws ec2 wait instance-running --region us-west-2 --instance-ids $INSTANCE_ID
echo "Instance: $INSTANCE_ID"

# 2. 等 SSM agent ready (约 60s)
sleep 60

# 3. 验证 credentials (instance profile 自动提供，无需任何手动操作)
aws ssm send-command \
  --region us-west-2 \
  --instance-ids $INSTANCE_ID \
  --document-name "AWS-RunShellScript" \
  --parameters '{"commands":["python3 -c \"import boto3; print(boto3.client(\\\"sts\\\",region_name=\\\"us-west-2\\\").get_caller_identity()[\\\"Arn\\\"])\""]}'

# 4. pull latest scripts
aws ssm send-command \
  --region us-west-2 \
  --instance-ids $INSTANCE_ID \
  --document-name "AWS-RunShellScript" \
  --parameters '{"commands":["cd ~/jingu-swebench && git pull origin main"]}'

# 5. launch batch
CMD_ID=$(aws ssm send-command \
  --region us-west-2 \
  --instance-ids $INSTANCE_ID \
  --document-name "AWS-RunShellScript" \
  --timeout-seconds 7200 \
  --parameters '{"commands":["cd ~/jingu-swebench && python3 scripts/run_with_jingu_gate.py --instance-ids django__django-10914 django__django-11910 --output ~/results/p169-treatment --workers 5 > ~/results/p169.log 2>&1"]}' \
  --query 'Command.CommandId' --output text)

# 6. 查进度
aws ssm get-command-invocation \
  --region us-west-2 \
  --command-id $CMD_ID \
  --instance-id $INSTANCE_ID \
  --query '{Status:Status,Out:StandardOutputContent}' --output json

# 7. 跑完后 terminate
aws ec2 terminate-instances --region us-west-2 --instance-ids $INSTANCE_ID
```

---

## IAM — ecsInstanceRole 权限

```
inline: jingu-swebench-bedrock   → bedrock:InvokeModel + InvokeModelWithResponseStream
inline: jingu-swebench-ecr-push  → ECR push/pull (us-west-2 repository)
managed: AmazonSSMManagedInstanceCore        → SSM agent (start-session / send-command)
managed: AmazonEC2ContainerServiceforEC2Role → ECS agent
managed: InfoSecHostMonitoringPolicy-DO-NOT-DELETE
```

credentials 由 EC2 metadata service (IMDSv2) 自动提供，每小时轮转，**永不过期**。
boto3 默认 credential chain 会自动使用，无需任何配置。

---

## 常见问题

**Q: credentials 过期**
A: 如果在 jingu 账号 EC2 上出现，说明 `~/.aws/credentials` 里有旧的静态 token 覆盖了 instance profile。
删掉或 rename：`mv ~/.aws/credentials ~/.aws/credentials.bak`

**Q: gate_runner.js 找不到模块**
A: `JINGU_TRUST_GATE_DIST` 未设置，fallback 到本地路径。
已在 `jingu_gate_bridge.py` 里修复：自动 fallback 到 `~/jingu-swebench/jingu-trust-gate/dist/src`

**Q: SSM InvalidInstanceId**
A: SSM agent 还没 ready，等 60s 再试。
