---
name: trainops
description: 按需附加的多实例 GPU 训练管理能力：维护本地训练计划（并行流程、任务节点、依赖、预计完成时间与甘特图看板），执行结构性检查、AutoDL/SSH 训练、定时巡检汇报与安全关机。保留 agent 原有角色。
---

# TrainOps：可热插拔多实例训练管理

本能力附加于当前 agent，不替换原有角色。仅在用户明确交办训练或计划任务时执行操作。

## 1. 核心规则

- **本地计划账本**：计划保存在 `$PROJECT/.trainops/plan.json`，**仅用 CLI 修改，禁止直接 Edit/Write JSON**。
- **固定编号**：实例 `I01`、流程 `F01`、任务 `F01-T01` 编号固定，对话汇报与改计划均使用编号。
- **多实例与依赖**：用户明确提供实例 ID 与 SSH 别名映射，禁止猜测；同实例串行独占、不同实例并行，本地任务用 `local`；同流程批量建任务默认自动串联，跨流程依赖用 `--deps` 显式声明。
- **结构性检查流程**：每次新建或调整计划后，必须执行 `$CLI check` 进行结构性检查（校验各流程连续性、依赖链闭合、实例分配与预计耗时完整性），确保甘特图块连续无断裂。
- **远程执行规范**：SSH 远程命令前置 `source /etc/network_turbo`；代码在本地修改验证并推送指定 commit 后在服务器拉取；用 `tmux`/`screen` 后台会话运行训练并记录会话与日志路径。
- **定时巡检与安全收尾**：用宿主真实调度工具建立每 30 分钟（或整点/半点）巡检，并通过 MCP `trainpilot`（不可用则在当前会话）汇报；重大节点即时汇报。关机前须确认进程退出、产物已保存且**跨流程无其他待跑任务复用该实例**。

## 2. 精简 CLI 用法（防上下文膨胀）

设 `CLI="python3 <本目录>/scripts/trainops.py -p <实验项目目录>"`。**日常查看一律用文本 `show`，避免输出全量 JSON 占用上下文。**

### 1) 单次命令初始化完整计划（同流程未填依赖时自动串联前一节点）
```bash
$CLI init --name "双实例对照实验" \
  --instance "基线GPU:autodl-id-a:gpu-a" --instance "对照GPU:autodl-id-b:gpu-b" \
  --flow "基线流程" --flow "对照流程" --flow "结果汇总" \
  --task "F01:基线训练:I01:120" \
  --task "F01:基线评估:I01:15" \
  --task "F02:对照训练:I02:90" \
  --task "F02:对照评估:I02:15" \
  --task "F03:对比报告:local:20:F01-T02,F02-T02"
```

### 2) 结构性检查流程与查看计划
```bash
$CLI check   # 检查各流程连续性、依赖链、实例分配与耗时是否完整
$CLI show    # 紧凑文本查看全局进度与预计完成时间
```

### 3) 更新状态、进度与修改计划
```bash
$CLI set F01-T01 --status running --session "tmux:F01-T01" --log "runs/F01-T01/train.log" --command "python train.py"
$CLI set F01-T01 --progress 40 --note "epoch 8, loss=0.32"
$CLI set F01-T01 --status done --output "runs/F01-T01"
$CLI set F02-T02 --instance I01 --deps F01-T02,F02-T01 --minutes 25
$CLI batch --task "F03:补充实验:I02:45" --set "F01-T01:done" --set "F02-T01:running:55:训练正常"
```

### 4) 实例状态、巡检登记与甘特图面板
```bash
$CLI instance set I01 --status on --note "GPU 正常"
$CLI monitor --schedule-id "真实调度ID" --note "每30分钟巡检"
$CLI serve --port 8765   # 后台启动并记录 PID，访问 http://127.0.0.1:8765 查看各流程连续甘特图与进度竖线
```

异常处理与恢复细节见 [references/protocol.md](references/protocol.md)（平时无需预读）。
