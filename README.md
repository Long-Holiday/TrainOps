# TrainOps

给任意 agent 按需热插拔的**多实例训练管理能力**：本地持久化维护项目计划表、支持多并行训练流程与跨流程任务依赖、提供带总进度竖线的流程甘特图 Web 面板，并精简 agent CLI 操作以减少上下文占用与幻觉。

- **多实例并行**：一次实验可指定多个 AutoDL GPU 实例并行推进不同流程，单实例独占防冲突。
- **规律编号与依赖**：实例 `I01`、流程 `F01`、任务 `F01-T01` 编号稳定，支持同流程自动串联与跨流程依赖。
- **甘特图 Web 面板**：每个流程以紧凑连续甘特图展示各节点子进度与大致完成时间，并用**红色竖线标出当前甘特图的总流程进度**。
- **自主优化调度**：agent 以目标、验收和硬约束为准，可按依赖、资源、成本与实验结果自主重排、拆并任务和调整实例分配，不机械照搬任务顺序；改变目标、放弃必需实验或增加未授权费用时先确认。
- **极简 Agent 操作**：单次命令完成建表/批量更新，只填核心字段（状态、进度、耗时、依赖），免填冗余记录，避免上下文过长产生幻觉。
- **一份技能，多家 agent 插件**：仓库本身就是插件目录（Agent Plugins 1.0.0 + Agent Skills），Claude Code、Codex、Antigravity、Gemini CLI 用原生命令装，其余 agent 一条软链即可；各家清单只是指针，没有第二份会漂移的规则。
- **无卡准备再训练**：通过外部 `autodl_mcp` 先无卡开机，完成项目/环境了解、配置与代码调整；准备就绪后关机，再按需正常开机或持续请求 GPU，验证就绪后正式训练，减少 GPU 空等。
- **AutoDL 存储与花销**：按三盘规范登记磁盘用量、TensorBoard 事件落数据盘；`monitor` 定时结算账户余额与「跑完剩余计划还需多少 GPU 小时 / 多少钱 / 余额够不够」。
- **零第三方依赖**：仅需 Python 3.10+ 标准库（Linux / macOS / WSL）。

## 1. 安装到各 Agent

本仓库本身就是插件目录，同时满足两套公开标准：**Agent Plugins 1.0.0**（根目录 `plugin.json` + `skills/`）与 **Agent Skills**（`skills/trainops/SKILL.md`）。技能只有一份，各家清单和下面的入口文件都只是**指向它**——不存在第二份会漂移的规则。按你的 agent 选一种装法。

### 1.1 有插件机制的：用原生命令装

| Agent | 安装命令 | 说明 |
|---|---|---|
| **Claude Code** | `/plugin marketplace add <仓库路径>` 然后 `/plugin install trainops@trainops`；已发布到 GitHub 时可一条命令：`/plugin install trainops --marketplace Long-Holiday/TrainOps` | 装的是仓库副本（`~/.claude/plugins/cache/trainops/trainops/<版本>/`），改完仓库要 `claude plugin update trainops` 重新同步 |
| **Codex CLI** | `codex plugin marketplace add <仓库路径>` 然后 `codex plugin add trainops@trainops` | 复制到 `$CODEX_HOME/plugins/cache/trainops/`；装完要新开一个会话才生效 |
| **Antigravity CLI** | `agy plugin install <仓库路径>/antigravity` | 复制到 `~/.gemini/config/plugins/trainops/`；技能经 `antigravity/skills` 软链一并复制 |
| **Gemini CLI** | `gemini extensions link <仓库路径>` | 读仓库根的 `gemini-extension.json` 与 `GEMINI.md`（薄入口，指向技能） |

Claude Code、Codex、Antigravity 三者已用官方校验器实测（`claude plugin validate` / `codex plugin list` / `agy plugin validate`）；Gemini CLI 本机未安装，扩展清单按官方格式写但**未实测**。Codex 与 Antigravity 都能直接读本仓库的 `.claude-plugin/marketplace.json`，不需要第二份市场清单。

### 1.2 只有技能目录的：软链一次，覆盖多家

`.agents/skills/` 是跨 agent 的共享技能目录，Windsurf/Cascade、Cursor、Copilot、Gemini CLI、Cline、opencode、Amp 等都会扫（各家的目录以各自官方文档为准）；它们也有自己的目录，按需选一行：

```bash
ln -s <仓库路径>/skills/trainops ~/.agents/skills/trainops            # 用户级，所有工作区生效
ln -s <仓库路径>/skills/trainops <实验项目>/.devin/skills/trainops     # Windsurf/Cascade 项目级（.windsurf/skills/ 同样读）
ln -s <仓库路径>/skills/trainops <实验项目>/.github/skills/trainops    # Copilot 项目级
ln -s <仓库路径>/skills/trainops ~/.claude/skills/trainops            # Claude Code 不用插件时
```

**用软链，别复制**：脚本靠自身路径回推仓库根来读 `.env` 与 `references/`，软链能保住这条路；复制出来的副本会丢掉凭据（见 1.4）。Windows 上 `git` 若没开符号链接，链会落成普通文件，改用 `mklink /D` 或干脆走 1.3。

### 1.3 通用兜底：任何能读文件、能执行命令的 agent

把下面的提示词复制给 agent（把两处路径换成你自己的）：

> 启用 TrainOps：读取 `<仓库路径>/skills/trainops/SKILL.md`，保留原有角色。实验项目目录为 `<实验项目路径>`。当前仅加载能力，不创建计划、不启动进程；收到任务后，以目标、验收标准和明确硬约束为准，自主优化调度，不必机械照搬我列出的步骤或顺序；常规优化直接同步计划并简述理由，改变目标、放弃必需实验或增加未授权费用时先确认。其他参考按需读取。

### 1.4 两个容易踩的坑

- **凭据在哪**：Token 从**仓库根的 `.env`** 读。有插件机制的 agent 基本都是**复制**插件（Claude Code 到 `~/.claude/plugins/cache/`、Codex 到 `$CODEX_HOME/plugins/cache/`、Antigravity 到 `~/.gemini/config/plugins/`，Gemini CLI 的 `extensions link` 是软链不受影响）：从本地目录安装会连 `.env` 一起复制（介意多留一份凭据，就用 `TRAINOPS_ENV=<仓库路径>/.env` 或环境变量 `AUTODL_TOKEN` 指回仓库），从 GitHub 等远程源安装则没有 `.env`，必须用这两种方式之一。没配 token 时本地命令照常可用，只有联网的 `wallet fetch` / `monitor` 会提示怎么配。
- **两个版本号**：各清单里的 `version` 是**插件版本**（0.1.0），`trainops.py --version` 打的是**计划文件 schema 版本**（3）。前者变了不影响账本，后者变了要先备份并核对文件。


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
  --task "F03:比较指标与报告:local:20:F01-T02,F02-T02" --brief

$CLI show --brief  # 需要决策时看摘要，建表回执已含结构检查
```

### 更新状态、进度与修改计划

```bash
# 启动、更新进度、标记完成（无需填写额外字段）
$CLI set F01-T01 --status running --brief
$CLI set F01-T01 --progress 35 --brief
$CLI set F01-T01 --checked --brief  # 已真实巡检，但没有可靠百分比
$CLI set F01-T01 --status done --brief

# 修改计划（改实例、依赖、预计耗时）
$CLI set F02-T02 --instance I01 --deps F01-T02,F02-T01 --minutes 25 --brief

# 批量追加或批量更新
$CLI batch --task "F03:消融补充:I02:40" --set "F01-T02:done" --set "F02-T01:running:60" --brief
```

**任务写法**：`流程ID:任务名[:实例[:耗时[:依赖]]]`。中间字段不确定时用 `-` 占位跳过，例如只想登记“F01 上耗时 30 分钟的评估、依赖 F01-T01”：

```bash
$CLI batch --task "F01:评估:-:30:F01-T01"
```

耗时写 `?` 表示待估（文本摘要显示“待估”，面板按默认 30 分钟占位绘制）。依赖用逗号分隔，空字符串表示清空；`--instance -` 等价于 `none`（未分配实例；本地并行任务应显式设为 `local`）。

### 低上下文工作方式

`--brief` 为显式选项，不改变默认完整文本/JSON 或脚本退出码：show 只看当前行动，写命令只看版本、编号和更新结果，monitor --once 只看一份结算结论。每组最多 3 条，长文本标 `…`，同时报告总数/省略数；历史任务仍保留在账本。完整问题看 check，完整任务看 show。

brief 新增或结构编辑会随回执附带最终结构检查，问题/提醒不会撤销已保存修改；纯状态/进度/巡检更新成功后不必机械再跑 check + show。脚本门禁仍用 check --strict。详细参数和批量语法见 [CLI 参考](skills/trainops/references/cli.md)，配置见 [AutoDL 参考](skills/trainops/references/autodl.md)，异常见 [恢复协议](skills/trainops/references/protocol.md)，目标 agent 按需读取即可，不必加载本 README 或开发用 CLAUDE.md。

## 4. 命令速查

设 `CLI="python3 skills/trainops/scripts/trainops.py -p /你的路径/my-experiment"`；加 `--version` 可查看当前 schema 版本。

| 命令 | 说明 |
|---|---|
| `init --name ... [--instance ...] [--flow ...] [--task ...]` | 新建计划，支持一次传入全部实例、流程和任务（同流程自动串联）；已有计划时拒绝覆盖 |
| `demo [--force]` | 在空目录生成演示计划，不涉及任何远程操作；`--force` 用于重置演示目录或已被改动的计划 |
| `check [--strict]` | 结构性检查：返回问题（阻断性）与提醒（如相邻任务时间空隙过大）两组结果；`--strict` 在存在问题时不返回 0 退出码，便于脚本或 CI 拦截 |
| `show --brief` / `show` / `show --json` | 当前行动摘要（推荐 agent 使用）、完整文本或完整 JSON；brief 分组限量并注明省略数，与 --json 互斥。`未巡检` 指未记录 checked_at 或超过 1 小时，`依赖就绪` 不保证远端在线 |
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

项目只面向 AutoDL，开机准备由外部工具执行，存储与花销由账本记录和告警。**面板不展示磁盘与 TensorBoard 信息**——这些是 agent 巡检时要盯的事，写在 `show`／`check` 里更有用。

### 无卡准备 → 有卡训练

通过 `autodl_mcp` 对普通按量实例默认采用：**`power_on_no_gpu` 无卡开机 → 了解项目/环境、完成配置与代码调整、执行无需 GPU 的检查 → 保存并结束准备进程 → `power_off` 并查询确认关机 → `power_on` 有卡开机（缺货后台持续重试）→ 验证 GPU 与训练入口 → 正式训练**。两种开机均返回 `job_id`，用 `job_status` 等待确认；不存在独立的“持续开机”工具。

开关机及持续请求按用户授权执行；已有训练不为此重启。切换时的关机保留实例与后续计划，不是释放实例；持续请求已提交或 SSH 可达都不代表 GPU 已就绪，未就绪不得把训练标为 running。无卡不等于免费，现有账本不区分两种开机模式、不能同时计算两种费率；不适用的时价先清空，不以 0 或旧费率代替，正式 GPU 开机后核实时价再结算。工具参数以当前 MCP 说明为准，TrainOps 不实现开关机或自动抢卡。详见 [开机模式与费用边界](skills/trainops/references/autodl.md#无卡准备与训练开机)。

### 三盘规范（详见 [AutoDL 配置](skills/trainops/references/autodl.md#三盘与用量)）

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

换 logdir 前先定位并核实本项目服务进程，按授权停止后重启，不批量杀掉其他实验的 TensorBoard；端口与访问方式见 [TensorBoard 配置](skills/trainops/references/autodl.md#tensorboard)。

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
$CLI monitor --once --brief         # ② 跑一轮即退出：交给 cron / 系统定时器 / agent 巡检
$CLI serve --interval 30 --port 8765  # ③ 面板进程内顺带结算，一个进程管两件事
```

```cron
*/30 * * * * cd /你的路径/TrainOps && python3 skills/trainops/scripts/trainops.py -p /你的路径/my-experiment monitor --once --strict
```

monitor --brief 仅与 --once 同用；--strict 保留原退出码语义。monitor 只结算余额，不检查远端训练、不自动关机；真实 SSH 巡检和用户约定汇报仍须执行，正常时简报，不每轮重复贴计划。serve 与 monitor 均为前台进程，后台运行/日志由调用方管理，不会自动后台化或登记 PID。

每轮把结论写进计划：`余额能跑多久`、`跑完剩余计划还需多少 GPU 小时`、`预估花费`、`余额够不够`，外加结算时刻。`show` 与面板直接读这份结论并标注"自动结算于 X"；超过 2 个周期没有新结算就回退为实时值并说明原因，**同一屏不会出现两套互相矛盾的数字**。结算失败（断网、token 失效）只记进计划的 `last_error`，不打断循环也不改动其余数据，`check` 会把它当成提醒报出来，`--strict` 让 cron 能据此告警。

`--expires-at ISO` 用于给循环加个自动退出时间（避免留下遗忘的守护进程），`--expires-at none` 清空；手动改计划后想立刻刷新结论用 `monitor --once`，不要用 `wallet fetch` —— 后者只更新余额，不动结算记录。

## 6. 测试

```bash
python3 -m unittest discover -s tests -v   # 计划账本 / CLI / 并发写 / 自动结算 / 余额接口 / 演示快照一致性与插件清单
node tests/test_web.cjs                    # 面板渲染：甘特几何、滚动保持、余额与结算块、逾期提示、XSS 转义
```

测试进程一律把 `.env` 指向不存在的路径，绝不会读到开发机上的真 token。清单测试会在各 agent CLI（`claude` / `codex` / `agy`）存在时调用它们的官方校验器复验一遍，未安装则跳过（`codex` 用临时 `CODEX_HOME`，不碰你本机的配置）。

