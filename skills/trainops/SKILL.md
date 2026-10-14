---
name: trainops
description: 按需附加的多实例 GPU 训练管理能力：维护本地训练计划（并行流程、任务节点、依赖、预计完成时间与甘特图看板），执行结构性检查、AutoDL/SSH 训练、定时巡检汇报与安全关机。保留 agent 原有角色。
---

# TrainOps：可热插拔多实例训练管理

本能力附加于当前 agent，不替换原有角色。仅在用户明确交办训练或计划任务时执行操作。

## 1. 核心规则

- **本地计划账本**：计划保存在 `$PROJECT/.trainops/plan.json`，**仅用 CLI 修改，禁止直接 Edit/Write JSON**。
- **固定编号**：实例 `I01`、流程 `F01`、任务 `F01-T01` 编号固定，对话汇报与改计划均使用编号。
- **只填核心字段**：仅维护流程、任务名称、实例（`I01`/`local`）、预计分钟数、依赖、状态（`pending`/`running`/`done`/`failed`）与进度百分比（`0–100`）。计划表不记录命令、日志路径、commit 等过程性信息，这些留在对话与远端即可，不要写进计划。
- **结构性检查**：每次新建或调整计划后执行 `$CLI check`，确保流程连续、依赖闭合、实例与耗时完整。检查结果分两组：`issues` 是必须修的问题，`warnings` 只是提醒（例如相邻任务时间空隙偏大），不影响 `ok`；脚本化场景可用 `check --strict` 让存在问题时的退出码非 0。
- **远程执行与巡检**：SSH 远程命令前置 `source /etc/network_turbo`，后台会话（`tmux`/`screen`）运行训练；每 30 分钟（或整点/半点）真实巡检并用 `set --progress` 更新进度——`running` 任务超过 1 小时没更新进度会在 `show` 中标记 `未巡检`。跨流程确认无后续任务后再安全关闭对应 GPU 实例；实例上仍有 `running` 任务时 CLI 会拒绝把该实例标为 `off`/`unreachable`，需先核实远端并更新任务状态。

## 2. 精简 CLI 用法

设 `CLI="python3 <本目录>/scripts/trainops.py -p <实验项目目录>"`。**日常查看一律用文本 `show`，避免输出全量 JSON 占用上下文。**

### 1) 单次命令初始化完整计划（同流程自动串联）
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

### 2) 结构性检查与查看计划
```bash
$CLI check   # 检查流程连续性、依赖链、实例与耗时是否完整；问题与提醒分开列出
$CLI show    # 紧凑文本查看全局进度与预计完成时间；标出 可开工 / 未巡检 / 已超期
```

### 3) 更新状态、进度与修改计划（极简参数）
```bash
$CLI set F01-T01 --status running
$CLI set F01-T01 --progress 40
$CLI set F01-T01 --status done
$CLI set F02-T02 --instance I01 --deps F01-T02,F02-T01 --minutes 25
$CLI batch --task "F03:补充实验:I02:45" --set "F01-T01:done" --set "F02-T01:running:55"
$CLI batch --task "F01:评估:-:30:F01-T01"
```

任务写法为 `流程ID:任务名[:实例[:耗时[:依赖]]]`：中间字段不确定就用 `-` 跳过（上例是“F01 上 30 分钟的评估、依赖 F01-T01”），耗时写 `?` 表示待估；依赖逗号分隔，空字符串清空。

### 4) 启动本地甘特图面板
```bash
$CLI serve --port 8765   # 后台启动并记录 PID，访问 http://127.0.0.1:8765 查看各流程甘特图与总进度竖线
```

异常处理与恢复细节见 [references/protocol.md](references/protocol.md)（平时无需预读）。
