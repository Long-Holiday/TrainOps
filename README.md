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

**任务写法**：`流程ID:任务名[:实例[:耗时[:依赖]]]`。中间字段不确定时用 `-` 占位跳过，例如只想登记“F01 上耗时 30 分钟的评估、依赖 F01-T01”：

```bash
$CLI batch --task "F01:评估:-:30:F01-T01"
```

耗时写 `?` 表示待估（文本摘要显示“待估”，面板按默认 30 分钟占位绘制）。依赖用逗号分隔，空字符串表示清空；`--instance -` 等价于 `none`（不占实例，可并行）。

## 4. 命令速查

设 `CLI="python3 skills/trainops/scripts/trainops.py -p /你的路径/my-experiment"`；加 `--version` 可查看当前 schema 版本。

| 命令 | 说明 |
|---|---|
| `init --name ... [--instance ...] [--flow ...] [--task ...]` | 新建计划，支持一次传入全部实例、流程和任务（同流程自动串联）；已有计划时拒绝覆盖 |
| `demo [--force]` | 在空目录生成演示计划，不涉及任何远程操作；`--force` 用于重置演示目录或已被改动的计划 |
| `check [--strict]` | 结构性检查：返回问题（阻断性）与提醒（如相邻任务时间空隙过大）两组结果；`--strict` 在存在问题时不返回 0 退出码，便于脚本或 CI 拦截 |
| `show` / `show --json` | 紧凑文本摘要（推荐 agent 使用）或完整 JSON 快照；文本模式下标出 `可开工`（依赖已就绪）、`未巡检`（running 超过 1 小时未更新进度）与 `已超期` |
| `set <任务ID> [--status ...] [--progress ...] [--instance ...] [--deps ...] [--minutes ...]` | 更新单个任务（`task set` 的简写） |
| `task add --flow F01 --name 名称 [--instance I01] [--minutes 30] [--deps ...] [--not-before ...]` | 新增单个任务；`flow add --name ...`、`instance add` / `instance set I01 --status on --observed` 同理 |
| `batch [--instance ...] [--flow ...] [--task ...] [--set ...]` | 单次命令批量追加实例/流程/任务或批量更新任务进度与状态（`--set "F01-T01:done:100:备注"`） |
| `monitor --note ... [--schedule-id ...] [--expires-at ...]` | 登记外部调度器/巡检安排（仅记录，不创建定时任务） |
| `serve --port 8765` | 启动本地只读甘特图 Web 面板（仅监听 `127.0.0.1`，写入一律拒绝） |

几个容易被忽略的约束：同一实例同时只允许一个 `running` 任务（`local` 不占 GPU，可并行）；依赖未完成的任务不能被标记为 `running`；实例上仍有任务运行时不能把该实例标为 `off` / `unreachable`。

## 5. 测试

```bash
python3 -m unittest discover -s tests -v   # CLI / 计划账本 / 并发写 / 演示快照一致性
node tests/test_web.cjs                    # 面板渲染：甘特几何、滚动保持、逾期提示、XSS 转义
```
