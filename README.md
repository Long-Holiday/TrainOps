# TrainOps

给任意 agent 按需热插拔的**多实例训练管理能力**：本地持久化维护项目计划表、支持多并行训练流程与跨流程任务依赖、提供带进度竖线的流程甘特图 Web 面板，并大幅精简 agent CLI 操作以减少上下文占用与幻觉。

- **多实例并行**：一次实验可指定多个 AutoDL GPU 实例并行推进不同流程，单实例独占防冲突。
- **规律编号与依赖**：实例 `I01`、流程 `F01`、任务 `F01-T01` 编号稳定，支持同流程/跨流程依赖与预计完成时间推算。
- **甘特图 Web 面板**：每个流程以甘特图展示任务时间轴、大致完成时间，并用**竖线标出任务进度**。
- **极简 Agent 操作**：单次命令即可完成建表/批量更新，默认紧凑单行与文本输出，无需繁琐参数，避免上下文过长产生幻觉。
- **零第三方依赖**：仅需 Python 3.10+ 标准库（Linux / macOS / WSL）。

## 1. 快速启用（任意 Agent 热插拔）

在任意能读文件并执行命令的 agent 会话中输入（完整提示词见 [training-monitor.md](training-monitor.md)）：

> 请读取 `/你的路径/TrainOps/skills/trainops/SKILL.md` 并启用 TrainOps，保留你原有角色。实验项目目录是 `/你的路径/my-experiment`。先用 `show` 查看现有计划；当前只加载能力，不操作实例。

支持 Skill 目录的工具也可直接软链 `skills/trainops`：

```bash
mkdir -p /你的路径/my-experiment/.claude/skills
ln -s /你的路径/TrainOps/skills/trainops /你的路径/my-experiment/.claude/skills/trainops
```

## 2. 预览甘特图演示面板

在仓库根目录启动自带的双实例并行演示项目：

```bash
python3 skills/trainops/scripts/trainops.py -p examples/demo-project serve --port 8765
```

浏览器打开 **http://127.0.0.1:8765**，即可看到每个流程的甘特图、各任务大致完成时间以及标示任务进度的竖线。

也可在空目录生成基于当前时间的演示计划：

```bash
python3 skills/trainops/scripts/trainops.py -p /tmp/trainops-demo demo
python3 skills/trainops/scripts/trainops.py -p /tmp/trainops-demo serve --port 8766
```

## 3. 精简 CLI 用法

设 `CLI="python3 skills/trainops/scripts/trainops.py -p /你的路径/my-experiment"`。

### 一次命令创建完整计划（实例 + 流程 + 任务 + 依赖）

无需拆成十几次命令，一条 `init` 即可完成：

```bash
$CLI init --name "双实例对照实验" \
  --instance "基线GPU:autodl-a:gpu-a" --instance "对照GPU:autodl-b:gpu-b" \
  --flow "基线" --flow "对照" --flow "汇总" \
  --task "F01:基线训练:I01:120" \
  --task "F01:基线评估:I01:15:F01-T01" \
  --task "F02:对照训练:I02:90" \
  --task "F02:对照评估:I02:15:F02-T01" \
  --task "F03:比较指标与报告:local:20:F01-T02,F02-T02"
```

### 查看计划（紧凑文本，节省上下文）

```bash
$CLI show
```

需要完整结构化快照时使用 `$CLI show --json`。

### 更新状态、进度与修改计划

直接使用顶层简写命令 `set`（无需手动传 `--owner` 或 `--checked`，系统自动维护）：

```bash
# 启动任务并记录会话与日志
$CLI set F01-T01 --status running --session "tmux:F01-T01" --log "runs/F01-T01/train.log"

# 巡检更新进度（甘特图进度竖线随之移动，并根据采样速度动态更新预计完成时间）
$CLI set F01-T01 --progress 35 --note "GPU 正常，epoch 7"

# 标记完成
$CLI set F01-T01 --status done --output "runs/F01-T01"

# 修改计划（改实例、依赖、预计耗时）
$CLI set F02-T02 --instance I01 --deps F01-T02,F02-T01 --minutes 25
```

### 批量追加或批量更新（`batch`）

```bash
$CLI batch --task "F03:消融补充:I02:40:F03-T01" --set "F01-T02:done" --set "F02-T01:running:60:loss收敛"
```

## 4. 命令速查

| 命令 | 说明 |
|---|---|
| `init --name ... [--instance ...] [--flow ...] [--task ...]` | 新建计划，支持一次传入全部实例、流程和任务（同流程自动串联） |
| `check` | 结构性检查（校验流程连续性、依赖链闭合、实例与预计耗时完整性） |
| `batch [--instance ...] [--flow ...] [--task ...] [--set ...]` | 单次命令批量追加实例/流程/任务或批量更新任务进度与状态 |
| `set <任务ID> [--status ...] [--progress ...] [--instance ...] [--deps ...] [--minutes ...]` | 更新单个任务（`task set` 的简写） |
| `show` / `show --json` | 紧凑文本摘要（推荐 agent 使用）或完整 JSON 快照 |
| `serve --port 8765` | 启动本地只读甘特图 Web 面板（仅监听 `127.0.0.1`） |
| `instance add/set`、`flow add/set`、`task add/set`、`monitor` | 分步细粒度管理命令（完全向后兼容） |

## 5. 目录结构与测试

```text
training-monitor.md                       通用热插拔启用提示词
skills/trainops/SKILL.md                  Agent 核心精简操作说明
skills/trainops/references/protocol.md    异常与恢复按需参考
skills/trainops/scripts/trainops.py       零依赖计划管理 CLI 与本地 Web 服务
skills/trainops/web/index.html            流程甘特图与进度竖线看板
examples/demo-project/.trainops/plan.json 双实例演示计划
tests/test_trainops.py                    Python 单元测试
tests/test_web.cjs                        Web 甘特图与进度竖线测试
```

运行全部测试：

```bash
python3 -m unittest discover -s tests -v
node tests/test_web.cjs
```
