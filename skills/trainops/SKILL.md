---
name: trainops
description: 按需附加远程 GPU 训练管理能力：管理用户指定的多个 AutoDL 实例，维护并行实验流程、任务依赖和稳定编号的本地计划，展示预计完成时间，执行 SSH 训练、巡检、汇报与安全收尾。适用于安排实验、修改训练计划、查看进度、恢复或结束训练；不替换 agent 原有角色。
---

# TrainOps：可插拔训练管理能力

这是一项附加能力，不是新的主 agent 身份，不限制你的原有工具，也不是开始训练或操作实例的即时授权。只有用户明确启用或交办相关任务时才使用。具体实验协议、范围和授权以当次任务及项目配置为准。

## 加载与恢复

1. 首次使用先读本能力包的 [操作协议](references/protocol.md)。对本轮任务不需要的操作不要执行。
2. 确定两个绝对路径：`SKILL_DIR` 是本文件所在目录；`PROJECT` 是本次实验的本地项目目录。不要把能力包目录误当成实验项目。下文的变量需由当前 shell 显式赋值；不能假设跨命令保留。
3. 用本地工具查看计划（Python 3.10+，Linux/macOS/WSL）：

```bash
python3 "$SKILL_DIR/scripts/trainops.py" --project "$PROJECT" show --json
```

4. 首次无计划时用 `init --name "项目名称"` 创建；已有计划时不重建。先核对实例映射、任务 owner、远程进程、会话、代码版本、日志、检查点和已有巡检调度，不能仅凭计划里的 running 再次启动训练。
5. 若 MCP/SSH/调度工具不可用，只处理不依赖它们的本地工作，并明确记录阻塞。不要声称创建了训练、通知、巡检或完成了关机。

## 计划是共享工作账本

- 数据在 `$PROJECT/.trainops/plan.json`。**通过 CLI 增量修改，不用 Write/Edit 覆盖 JSON**；CLI 会在文件锁内重新读取、校验和原子保存。多个 agent 必须使用同一个本地项目目录，不能各维护一份副本。
- 实例 `I01`、流程 `F01`、任务 `F01-T01`。编号永不重排；引用与汇报必须带编号。先建流程和节点再执行，跨流程依赖使用完整任务编号。
- 用户明确提供每个 AutoDL ID 与 SSH 别名的映射，禁止猜测。每个任务指定 `I01` 等实例；本地任务显式指定 `local`；未分配的任务不能开始。
- 每条实例同一时刻只容许一个执行中任务；不同实例可以并行。需要前后顺序就写依赖，不靠列表位置暗示；汇总节点可以依赖多个流程的评估节点。
- 耗时有依据才填 `--minutes`，未知用 `?`；用户给定最早开始时间时用带时区的 `--not-before`。预计时间是动态估算，不是已建立的自动调度。
- 运行前以本次会话唯一名称认领：`task set F01-T01 --status running --owner agent-a-session-01`。这是本地执行锁，涵盖本节点的准备/启动/运行阶段，**并不意味着远程训练已启动**。收到占用错误就停止派发，不换个 owner 规避冲突。
- 认领成功后再操作远程；操作前仍需核实远程有无未登记进程。实际启动后登记 command、commit、session、log、output；只有观察到事实后才用 `--checked` 或填进度。
- 运行中的更新必须附相同 `--owner`。owner 是协作标记，不是安全认证；其他 agent 不应冒用。owner 失联时先核实远程，协调原 owner 停止工作，再明确接管。
- 节点变化、真实巡检和故障均及时落盘。完成必须先确认进程退出、产物可用，再标 done；不要将失败/取消依赖视作完成。修改计划时不悄悄删除依赖或减少实验范围。

## 常用操作

下列示例中的编号、耗时、owner 必须替换为当前计划的真实值。

登记实例、建立流程：

```bash
python3 "$SKILL_DIR/scripts/trainops.py" --project "$PROJECT" instance add --name "基线 GPU" --autodl-id "用户提供的实例ID" --ssh-alias "用户提供的SSH别名"
```

```bash
python3 "$SKILL_DIR/scripts/trainops.py" --project "$PROJECT" flow add --name "基线实验"
```

建立训练节点、指定跨流程依赖：

```bash
python3 "$SKILL_DIR/scripts/trainops.py" --project "$PROJECT" task add --flow F02 --name "对照评估" --instance I02 --minutes 20 --deps F02-T01,F01-T02
```

修改未运行节点（依赖列表整体替换；`--deps ""` 清空，`--instance none` 取消分配，`--minutes '?'` 改为待估）：

```bash
python3 "$SKILL_DIR/scripts/trainops.py" --project "$PROJECT" task set F02-T02 --instance I01 --deps F01-T03 --minutes 25
```

登记实际观测及运行证据（不要照抄示例制造观测）：

```bash
python3 "$SKILL_DIR/scripts/trainops.py" --project "$PROJECT" task set F01-T01 --owner agent-a-session-01 --progress 35 --checked --session "tmux:F01-T01" --log "runs/F01-T01/train.log" --note "已核实进程存活，日志持续更新"
```

运行记录的其他字段：`--command "实际命令" --commit "实际提交SHA" --output "实际产物目录"`。所有记录仅作数据保存，CLI 不执行这些命令。

确认完成后：

```bash
python3 "$SKILL_DIR/scripts/trainops.py" --project "$PROJECT" task set F01-T01 --owner agent-a-session-01 --status done --output "已核实的产物目录" --checked
```

展示页面（在可持续运行的本地进程/会话中启动，避免阻塞当前 agent；记录服务 PID，结束时只清理自己启动的服务）：

```bash
python3 "$SKILL_DIR/scripts/trainops.py" --project "$PROJECT" serve --port 8765
```

地址 `http://127.0.0.1:8765`。页面没有写操作，不执行 SSH、不自动启动依赖任务。只有开启该服务才会实时刷新，不把页面刷新当作巡检。

## 巡检和收尾

- 原生调度工具优先：默认每 30 分钟巡检，有重大节点立即额外汇报；用户明确指定整点/半点时按其要求执行。必须实际创建可执行任务，记录调度 ID、过期时间、是否依赖当前会话。若缺少调度能力，说明限制并确定替代方案，不忙循环、不虚构后台监控。
- 创建真实调度后用 `monitor --schedule-id "真实ID" --expires-at "带时区时间" --note "生命周期及巡检范围"` 登记。此命令**只是记账，不创建定时器**；无过期时间时省略该参数，在 note 解释。
- 汇报优先用已配置且目的地明确的 MCP trainpilot；无法使用时在当前会话报告。内容包含编号、实例、进度、指标来源、异常、预计时间及下一步，不编造缺失数据。
- 每台实例独立收尾：不能因一条流程结束就关闭仍服务其他流程的实例；先检查本项目所有相关节点和远程实际进程，确认检查点/结果保存以及用户的关机授权。
- 用户停用能力时，先交代仍运行的节点、实例和调度的负责人/接管方式，再停止新操作。**停用不等于取消训练、删除计划、停止远程进程或关机。** 未明确授权时保留资源并说明费用与无人监控风险。

更详细的多实例调度、恢复与安全边界见 [操作协议](references/protocol.md)。
