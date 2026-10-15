---
name: trainops
description: 按需附加的多实例 GPU 训练管理能力：维护本地训练计划（并行流程、任务节点、依赖、预计完成时间与甘特图看板），执行结构性检查、AutoDL/SSH 训练、定时巡检汇报、余额与剩余开销自动结算与安全关机。保留 agent 原有角色。
---

# TrainOps：可热插拔多实例训练管理

本能力附加于当前 agent，不替换原有角色。仅在用户明确交办训练或计划任务时执行操作。

## 1. 核心规则

- **本地计划账本**：计划保存在 `$PROJECT/.trainops/plan.json`，**仅用 CLI 修改，禁止直接 Edit/Write JSON**。
- **固定编号**：实例 `I01`、流程 `F01`、任务 `F01-T01` 编号固定，对话汇报与改计划均使用编号。
- **只填核心字段**：仅维护流程、任务名称、实例（`I01`/`local`）、预计分钟数、依赖、状态（`pending`/`running`/`done`/`failed`）与进度百分比（`0–100`）。计划表不记录命令、日志路径、commit 等过程性信息，这些留在对话与远端即可，不要写进计划。
- **结构性检查**：每次新建或调整计划后执行 `$CLI check`，确保流程连续、依赖闭合、实例与耗时完整。检查结果分两组：`issues` 是必须修的问题，`warnings` 只是提醒（相邻任务时间空隙偏大、某块盘已用 ≥90%、开机实例缺时价、余额不足以覆盖剩余计划、自动结算失败或有实例开机却没在结算），不影响 `ok`；脚本化场景可用 `check --strict` 让存在问题时的退出码非 0。
- **远程执行与巡检**：SSH 远程命令前置 `source /etc/network_turbo`，后台会话（`tmux`/`screen`）运行训练；每 30 分钟（或整点/半点）真实巡检并用 `set --progress` 更新进度——`running` 任务超过 1 小时没更新进度会在 `show` 中标记 `未巡检`。跨流程确认无后续任务后再安全关闭对应 GPU 实例；实例上仍有 `running` 任务时 CLI 会拒绝把该实例标为 `off`/`unreachable`，需先核实远端并更新任务状态。
- **存储、日志与花销同样要盯**：训练产物按第 3 节的三盘规范落盘（放错盘=丢数据），TensorBoard 事件按第 4 节的"数据盘 + 软链"约定写入（写系统盘=挤爆/丢环境），余额与剩余开销按第 5 节定时结算。巡检时顺手看一眼 `$CLI show` 里的 `盘 …`、`预算` 与 `结算` 三处，带 `!` 的盘、不足的余额和失败/过期的结算都要当场处置。

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
$CLI serve --port 8765               # 后台启动并记录 PID，访问 http://127.0.0.1:8765 查看各流程甘特图与总进度竖线
$CLI serve --port 8765 --interval 30 # 同一个进程里再跑一个结算线程（见第 5 节）
```

面板只做只读展示（各流程甘特图 + 余额/可用时长/结算结论），**不展示磁盘条与 TensorBoard 链接**：容量与日志路径是 agent 巡检时要盯的事，不是给面板看的事。

计划文件必须严格是当前 schema（`$CLI --version` 可查）：版本不符时的提示是"请备份并核对文件"，项目仍在开发阶段，不做自动迁移，也不会替你覆盖。

## 3. 数据存储规范（AutoDL 三盘）

| 路径 | 特性 | 放什么 |
|---|---|---|
| `/` 系统盘 | 一般速度；关机不丢，**随镜像保存** | 代码、conda 环境、配置脚本。容量最小（通常 30G）且与系统共用 |
| `/root/autodl-tmp` 数据盘 | 快；关机不丢，**不随镜像保存**，释放实例即丢失 | 训练数据、checkpoint、TensorBoard 事件、高 IO 中间产物 |
| `/root/autodl-fs` 文件存储 | 一般速度；多实例同步共享，不受开关机与保存镜像影响 | 需要跨实例共享或长期保留的产物（最终权重、评测结果、数据集） |

- **释放实例前，必须把要保留的产物同步到 `/root/autodl-fs` 或下载到本地**：数据盘关机不丢，但释放实例会丢，且不随镜像保存。
- 开新任务前先看 `$CLI show` 的实例行（`盘 系统 12/30G·数据 40/50G·存储 5/200G`）：带 `!` 表示该盘已用 ≥90%，先清理或扩容再启动训练；`check` 也会把它列进 `warnings`。
- 磁盘用量从 SSH 取 `df -h / /root/autodl-tmp /root/autodl-fs`，再登记：

```bash
$CLI instance set I01 --disk "sys:12/30,data:40/50,fs:5/200"
```

`--disk` 按盘逐项合并（没提到的盘保持原值，不会被清空），盘名取 `sys`/`data`/`fs`，`data:none` 清空该盘。文件存储超过 20G 起按量计费，别把临时文件长期堆在那里。

## 4. TensorBoard 约定（数据盘 + 软链）

事件文件一律写数据盘，再用软链挂到内置 TensorBoard 读取的目录，避免把系统盘写满：

```bash
TB=/root/autodl-tmp/tf-logs/<项目名>          # 每个任务一个 run：$TB/<任务ID>，例如 $TB/F01-T02
ln -sfn "$TB" /root/tf-logs/<项目名>          # 内置 TensorBoard 免启动即可读到
tensorboard --logdir "$TB" &                  # 需要单独进程时（默认端口 6006）
```

- 每个任务一个 run 目录，用计划里的任务编号命名，便于按任务对比曲线。
- 需要换 logdir 时按官方流程重启：`ps -ef | grep tensorboard | awk '{print $2}' | xargs kill -9`，再 `tensorboard --port 6007 --logdir <目录>`，从 AutoPanel 的自定义服务进入。
- 需要公网直链时用 `tensorboard --port 6006 --host 0.0.0.0`（平台对外只映射 6006 / 6008）。
- 计划表不记录 TensorBoard 路径或链接：路径留在对话与远端脚本里。

## 5. 余额、可用时长与自动结算

```bash
$CLI monitor --once                            # 结算一轮：刷新余额 + 重算剩余计划的 GPU 小时与花费，结论写进计划
$CLI monitor                                   # 前台循环，每 --interval 分钟（默认 30）结算一轮
$CLI monitor --once --strict                   # 最近一次结算有错时退出码非 0，供 cron / 巡检告警
$CLI wallet fetch                              # 只拉一次余额（不更新结算记录）
$CLI wallet set --assets 128.50 --voucher 20   # 无 token / 断网时的人工登记
$CLI instance set I01 --price 1.88             # GPU 时价（元/小时），按实例规格登记；不构成一次观测
$CLI show                                      # 末尾"预算"与"结算"两行给出结论
```

- Token 放 **TrainOps 仓库根目录的 `.env`**（`AUTODL_TOKEN=...`，模板 `.env.example`，建议 `chmod 600`），也可用环境变量或 `--token` 临时覆盖；**Token 绝不写进计划文件、输出或异常文本**。
- **结算要定时跑起来**，否则 `show`／面板只能给实时值：长任务用 `$CLI monitor`（或 `serve --interval`）常驻，短任务与巡检用 cron 调 `monitor --once --strict`。日志、开机实例与结算进程同时停摆是最危险的组合——有实例开机计费却没有结算在跑时，`check` 会提醒。
- 结算结论包含：跑完剩余计划还需多少 GPU 小时、预估花费、余额够不够，外加结算时刻。它随计划变化自动过期：超过 2 个周期未更新就回退为实时值并说明原因，**同一屏不会出现两套互相矛盾的数字**。
- 可用时长只按**现金余额** `assets` 计算，代金券单独展示并标注"不计入可用时长"——把代金券算进去会得出乐观的错误结论。
- **任一开机实例缺时价、或任一任务完成时间待估时，对应结论直接不显示**——不要替它猜，先把缺的数据补齐。
- 结算失败（断网、token 失效）只记进计划的 `last_error`，不打断循环也不影响其它数据；`check` 会把它列为提醒，处理完用 `monitor --once` 立刻重算。
- 余额快见底或不足以覆盖剩余计划时，主动提醒用户充值或缩减计划，不要等训练跑到一半被欠费关机。

异常处理与恢复细节见 [references/protocol.md](references/protocol.md)（平时无需预读）。
