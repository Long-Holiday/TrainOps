# TrainOps

给任意 agent 按需热插拔的**多实例训练管理能力**：本地持久化维护项目计划表、支持多并行训练流程与跨流程任务依赖、提供带总进度竖线的流程甘特图 Web 面板，并精简 agent CLI 操作以减少上下文占用与幻觉。

- **多实例并行**：一次实验可指定多个 AutoDL GPU 实例并行推进不同流程，单实例独占防冲突。
- **规律编号与依赖**：实例 `I01`、流程 `F01`、任务 `F01-T01` 编号稳定，支持同流程自动串联与跨流程依赖。
- **甘特图 Web 面板**：每个流程以紧凑连续甘特图展示各节点子进度与大致完成时间，并用**红色竖线标出当前甘特图的总流程进度**。
- **极简 Agent 操作**：单次命令完成建表/批量更新，只填核心字段（状态、进度、耗时、依赖），免填冗余记录，避免上下文过长产生幻觉。
- **AutoDL 存储与花销**：按三盘规范登记磁盘用量、TensorBoard 事件落数据盘；`monitor` 定时结算账户余额与「跑完剩余计划还需多少 GPU 小时 / 多少钱 / 余额够不够」。
- **零第三方依赖**：仅需 Python 3.10+ 标准库（Linux / macOS / WSL）。

## 1. 快速启用（任意 Agent 热插拔）

把下面的提示词复制给任意能读文件、能执行命令的 agent（把两处路径换成你自己的）：

> 启用 TrainOps 训练管理能力：请读取 `/你的路径/TrainOps/skills/trainops/SKILL.md`（无需预读其他参考文档），保留你原有角色。本次实验项目目录是 `/你的路径/my-experiment`。先用文本模式 `show` 查看现有计划；若尚无计划，接到具体实验任务后用 `init` 单次命令建立实例、并行流程与任务节点，执行 `check` 完成结构性检查，并用 `set` / `batch` 维护任务状态与进度。训练产物按 SKILL.md 的三盘规范落盘（高 IO 与 checkpoint 放 `/root/autodl-tmp`，需长期保留或跨实例共享的放 `/root/autodl-fs`，释放实例前先转存），TensorBoard 事件写数据盘再软链到 `/root/tf-logs`；巡检时一并登记实例时价与 `df -h` 用量（`instance set --price/--disk`），余额与「跑完剩余计划还需多少 GPU 小时 / 多少钱」交给 `monitor` 定时结算（token 放在 TrainOps 仓库根的 `.env`），余额不足以覆盖剩余计划时提醒我。需要可视化时后台启动 `serve` 本地甘特图面板。当前仅加载能力，具体实例映射与任务稍后提供。

## 2. 预览甘特图演示面板

```bash
python3 skills/trainops/scripts/trainops.py -p examples/demo-project serve --port 8765
```

浏览器打开 **http://127.0.0.1:8765** 查看各流程甘特图、任务子进度、流程总进度红线与余额结算结论。演示计划自带一份结算记录（数据是编造的演示值），所以开箱就能看到结算徽标；它超过 2 个周期（60 分钟）后自动回退为实时值并标注原因——这是设计好的降级，不是故障。

演示目录不要启动自动结算：`serve --interval` / `monitor` 会真的写入 `examples/demo-project/.trainops/plan.json`（那是版本库里的快照），需要真实结算时用自己的实验项目目录。

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
| `instance set I01 --price 1.88 --disk "data:40/50,fs:5/200"` | 登记 GPU 时价（元/小时）与各盘用量（GB）；`--disk` 逐盘合并，`data:none` 清空该盘 |
| `wallet fetch [--token ...] [--dry-run]` | 手动拉一次账户余额并写入计划（只读接口，不下发实例指令、不触发计费）；失败时退出码非 0 且计划文件不变。定时刷新余额用 `monitor` |
| `wallet set --assets 128.50 [--voucher 20]` | 无 token / 断网时手工登记余额（`none` 清空；无参数会被拒绝） |
| `monitor [--interval 30] [--once] [--expires-at ISO\|none] [--token ...] [--timeout 20] [--strict]` | **定时结算**：每轮刷新余额并重算「剩余计划还需多少 GPU 小时 / 多少钱 / 余额够不够」，结论连同结算时间写进计划；默认前台循环，`--once` 跑一轮即退出（cron），`--strict` 在最近一次结算有错时返回非 0 |
| `serve --port 8765 [--interval 30]` | 启动本地只读甘特图 Web 面板（仅监听 `127.0.0.1`，写入一律拒绝）；带 `--interval` 时同一进程内再跑一个结算线程 |

几个容易被忽略的约束：同一实例同时只允许一个 `running` 任务（`local` 不占 GPU，可并行）；依赖未完成的任务不能被标记为 `running`；实例上仍有任务运行时不能把该实例标为 `off` / `unreachable`。

## 5. AutoDL 存储、TensorBoard 与余额

项目只面向 AutoDL，因此把三件账外的事也纳入同一套记录与告警。**面板不展示磁盘与 TensorBoard 信息**——这些是 agent 巡检时要盯的事，写在 `show`／`check` 里更有用。

### 三盘规范（`SKILL.md` 第 3 节，巡检时逐条对照）

| 路径 | 特性 | 放什么 |
|---|---|---|
| `/` 系统盘 | 关机不丢、随镜像保存、容量最小 | 代码、conda 环境、脚本 |
| `/root/autodl-tmp` 数据盘 | 快、关机不丢、**不随镜像保存、释放实例即丢** | 数据集、checkpoint、TB 事件、高 IO 产物 |
| `/root/autodl-fs` 文件存储 | 跨实例共享、不受开关机与镜像影响 | 需长期保留或跨实例共享的产物 |

`instance set I01 --disk "sys:12/30,data:44/50,fs:5/200"` 登记用量后，`show` 的实例行会显示 `盘 系统 12/30G·数据 44/50G`，已用 ≥90% 的盘带 `!`，`check` 同时给出提醒。

### TensorBoard（数据盘 + 软链）

```bash
TB=/root/autodl-tmp/tf-logs/<项目名>     # 每任务一个 run：$TB/F01-T02
ln -sfn "$TB" /root/tf-logs/<项目名>     # 内置 TensorBoard 免启动即可读
```

换 logdir 按官方流程重启：`ps -ef | grep tensorboard | awk '{print $2}' | xargs kill -9`，再 `tensorboard --port 6007 --logdir <目录>`；要公网直链则 `--port 6006 --host 0.0.0.0`（平台只映射 6006 / 6008）。

### 余额与可用时长

Token 放在 **TrainOps 仓库根目录的 `.env`**（模板见 [.env.example](.env.example)，`chmod 600`）：

```bash
cp .env.example .env && chmod 600 .env   # 填入 AUTODL_TOKEN=...（控制台 → 设置 → 开发者Token）
$CLI wallet fetch                        # 只读接口，token 不进计划文件、不进输出、不进异常文本
$CLI instance set I01 --price 1.88       # 时价决定"余额能跑多久"
$CLI show                                # 预算行：余额、可跑小时、计划剩余 GPU 小时与花费、够不够
```

优先级为 `--token` > 环境变量 `AUTODL_TOKEN` > `.env`（`TRAINOPS_ENV` 可把 `.env` 指到别处）；文件缺失不算错误，没配 token 时本地命令照常可用，只有联网命令会提示怎么配。

可用时长只按现金余额 `assets` 计算，代金券单独展示；任一开机实例缺时价或任一任务完成时间待估时，对应结论**不显示**而不是给出残缺数字。面板顶部只呈现这些算得出的结论：余额、代金券、可跑小时、"够不够跑完"，外加一块结算记录（文案由服务端写好，面板不做任何计算）。

### 自动结算（余额与剩余计划开销定时重算）

按需选一种方式让它定期跑（三种都是同一个 `cycle()`：联网在文件锁之外，落盘只写一次）：

```bash
$CLI monitor                        # ① 前台循环，每 --interval 分钟结算一轮，Ctrl+C 退出
$CLI monitor --once                 # ② 跑一轮即退出：交给 cron / 系统定时器 / agent 巡检
$CLI serve --interval 30 --port 8765  # ③ 面板进程内顺带结算，一个进程管两件事
```

```cron
*/30 * * * * cd /你的路径/TrainOps && python3 skills/trainops/scripts/trainops.py -p /你的路径/my-experiment monitor --once --strict
```

每轮把结论写进计划：`余额能跑多久`、`跑完剩余计划还需多少 GPU 小时`、`预估花费`、`余额够不够`，外加结算时刻。`show` 与面板直接读这份结论并标注"自动结算于 X"；超过 2 个周期没有新结算就回退为实时值并说明原因，**同一屏不会出现两套互相矛盾的数字**。结算失败（断网、token 失效）只记进计划的 `last_error`，不打断循环也不改动其余数据，`check` 会把它当成提醒报出来，`--strict` 让 cron 能据此告警。

`--expires-at ISO` 用于给循环加个自动退出时间（避免留下遗忘的守护进程），`--expires-at none` 清空；手动改计划后想立刻刷新结论用 `monitor --once`，不要用 `wallet fetch` —— 后者只更新余额，不动结算记录。

## 6. 测试

```bash
python3 -m unittest discover -s tests -v   # 计划账本 / CLI / 并发写 / 自动结算 / 余额接口 / 演示快照一致性
node tests/test_web.cjs                    # 面板渲染：甘特几何、滚动保持、余额与结算块、逾期提示、XSS 转义
```

测试进程一律把 `.env` 指向不存在的路径，绝不会读到开发机上的真 token。
