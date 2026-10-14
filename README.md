# TrainOps

给任意 agent 按需热插拔的**多实例训练管理能力**：本地持久化维护项目计划表、支持多并行训练流程与跨流程任务依赖、提供带总进度竖线的流程甘特图 Web 面板，并精简 agent CLI 操作以减少上下文占用与幻觉。

- **多实例并行**：一次实验可指定多个 AutoDL GPU 实例并行推进不同流程，单实例独占防冲突。
- **规律编号与依赖**：实例 `I01`、流程 `F01`、任务 `F01-T01` 编号稳定，支持同流程自动串联与跨流程依赖。
- **甘特图 Web 面板**：每个流程以紧凑连续甘特图展示各节点子进度与大致完成时间，并用**红色竖线标出当前甘特图的总流程进度**。
- **极简 Agent 操作**：单次命令完成建表/批量更新，只填核心字段（状态、进度、耗时、依赖），免填冗余记录，避免上下文过长产生幻觉。
- **零第三方依赖**：仅需 Python 3.10+ 标准库（Linux / macOS / WSL）。

## 1. 快速启用（任意 Agent 热插拔）

在任意能读文件并执行命令的 agent 会话中输入（完整提示词见 [training-monitor.md](training-monitor.md)）：

> 请读取 `/你的路径/TrainOps/skills/trainops/SKILL.md` 并启用 TrainOps，保留你原有角色。实验项目目录是 `/你的路径/my-experiment`。先用 `show` 查看现有计划；当前只加载能力，不操作实例。

## 2. 预览甘特图演示面板

```bash
python3 skills/trainops/scripts/trainops.py -p examples/demo-project serve --port 8765
```

浏览器打开 **http://127.0.0.1:8765** 查看各流程甘特图、任务子进度与流程总进度红线。

## 3. 精简 CLI 用法

设 `CLI="python3 skills/trainops/scripts/trainops.py -p /你的路径/my-experiment"`。

### 一次命令创建完整计划并做结构性检查

```bash
$CLI init --name "双实例对照实验" \
  --instance "基线GPU:autodl-a:gpu-a" --instance "对照GPU:autodl-b:gpu-b" \
  --flow "基线" --flow "对照" --flow "汇总" \
  --task "F01:基线训练:I01:120" \
  --task "F01:基线评估:I01:15" \
  --task "F02:对照训练:I02:90" \
  --task "F02:对照评估:I02:15" \
  --task "F03:比较指标与报告:local:20:F01-T02,F02-T02"

$CLI check
$CLI show
```

### 更新状态、进度与修改计划

```bash
# 启动、更新进度、标记完成（无需填写额外字段）
$CLI set F01-T01 --status running
$CLI set F01-T01 --progress 35
$CLI set F01-T01 --status done

# 修改计划（改实例、依赖、预计耗时）
$CLI set F02-T02 --instance I01 --deps F01-T02,F02-T01 --minutes 25

# 批量追加或批量更新
$CLI batch --task "F03:消融补充:I02:40" --set "F01-T02:done" --set "F02-T01:running:60"
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

## 5. 测试

```bash
python3 -m unittest discover -s tests -v
node tests/test_web.cjs
```
