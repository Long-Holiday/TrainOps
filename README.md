# TrainOps

面向 **Antigravity CLI** 的插件形式的**多实例训练管理能力**：本地持久化维护项目计划表、支持多并行训练流程与跨流程任务依赖、提供中性色、按网格排布、原生滚动的单张任务依赖主图 Web 面板（含流程进度与节点子进度），并精简 agent CLI 操作以减少上下文占用与幻觉。

- **多实例并行**：一次实验可指定多个 AutoDL GPU 实例并行推进不同流程，单实例独占防冲突。
- **规律编号与依赖**：实例 `I01`、流程 `F01`、任务 `F01-T01` 编号稳定，支持 Flow 内并行 DAG、同流程同实例默认串联与显式跨流程依赖。
- **任务流程图 Web 面板**：整份计划只有一张有向无环主图，所有流程的任务都在这张图里。节点按列/行网格排布，跨层依赖在中间层插入不可见路点，服务端用重心排序与局部相邻交换减少连线交叉；不显示开始节点，无前置任务就是最左侧一列的起点。画布是原生滚动容器，支持横向/纵向滚动查看大图。节点与箭头统一为中性色，颜色只留给任务状态；每个任务只画成一个叶子矩形节点，不嵌套子任务，完整显示任务名称、实例、流程、实际子进度与预计时间。流程完成度以一行纯文本读数展示「已完成 ÷ 未取消任务」。仅展示现有依赖，不提供条件分支执行；面板上可登记实例时价与开发者 Token。
- **自主优化调度**：agent 以目标、验收和硬约束为准，可按依赖、资源、成本与实验结果自主重排、拆并任务和调整实例分配，不机械照搬任务顺序；改变目标、放弃必需实验或增加未授权费用时先确认。
- **单 agent 多机再平衡**：任务完成/失败、实例可用及巡检时检查全局待执行任务，借助现有 `set --instance` 改派合格任务；先排除原远端队列重复启动，再由同一个 agent 按最新计划在目标机实际启动并核实，不只更新面板。只搬尚未启动的任务，不绕过真实依赖或节点约束；agent 未运行时不再平衡，不新增常驻调度器。见[执行闭环](skills/trainops/references/cli.md#单-agent-多机再平衡)。
- **极简 Agent 操作**：单次命令完成建表/批量更新，只填核心字段（状态、进度、耗时、依赖），免填冗余记录，避免上下文过长产生幻觉。
- **只做 Antigravity CLI 插件**：仓库根就是插件根（`plugin.json` 清单 + `rules/` 规则入口 + `skills/trainops/` 能力契约），一条 `agy plugin install` 装完即用；不提供别家 agent 的清单，也不维护第二份会漂移的规则。
- **无卡准备再训练**：通过外部 `autodl_mcp` 先无卡开机，完成项目/环境了解、配置与代码调整；准备就绪后关机，再按需正常开机或持续请求 GPU，验证就绪后正式训练，减少 GPU 空等。
- **AutoDL 存储与花销**：按三盘规范登记磁盘用量、TensorBoard 事件落数据盘；面板进程按周期结算账户余额与「跑完剩余计划还需多少 GPU 小时 / 多少钱 / 余额够不够」，开发者 Token 在面板上登记。
- **飞书实验日志**：agent 巡检同时用 `lark-cli` 维护多维表格；复用用户指定/项目已有表，未指定则自动创建。项目配置、计划账本与 Run 历史统一存实验项目 `.agent/experiment.yaml`，不覆盖人工决策。见[实验日志](skills/trainops/references/experiments.md)。
- **零第三方 Python 依赖**：仅需 Python 3.10+ 标准库（Linux / macOS / WSL）；飞书同步另需已安装并授权的 `lark-cli`。

## 1. 安装到 Antigravity CLI

本插件**只服务 Antigravity CLI**，仓库根就是插件根：根目录 `plugin.json`（清单）+ `rules/trainops.md`（规则入口：开工顺序与三条不变量）+ `skills/trainops/`（完整能力契约）。规则只做指针，细则只有 `SKILL.md` 一份——不存在第二份会漂移的规则。

### 1.1 装成插件（推荐）

```bash
agy plugin install <仓库路径>     # 整个插件根复制到 ~/.gemini/config/plugins/trainops/
agy plugin list                  # 确认识别到 trainops（skills 1 · rules 1）
```

装的是插件根的**副本**：改完仓库要再 `agy plugin install <仓库路径>` 同步一次；不想敲命令就在 CLI 里打开 `/plugin` → 「+ Install from local directory...」选同一个目录。开关与卸载按目录名走：`agy plugin disable trainops` / `enable` / `uninstall trainops`。

### 1.2 不装插件：把技能挂进 Antigravity 的技能目录

Antigravity 也直接扫技能目录（全局 `~/.gemini/config/skills/<名称>/`，项目内 `.agents/skills/<名称>/`），挂一次就能用：

```bash
ln -s <仓库路径>/skills/trainops ~/.gemini/config/skills/trainops        # 全局，所有工作区生效
ln -s <仓库路径>/skills/trainops <实验项目>/.agents/skills/trainops       # 只在这个项目里生效
```

**用软链，别复制**：脚本靠自身位置回推 `references/` 与面板页面，软链能保住这条路（凭据不跟仓库走，见 1.4）。Windows 上 `git` 若没开符号链接，链会落成普通文件，改用 `mklink /D`，或者干脆走 1.3。

### 1.3 连目录都不想挂：直接指路

在 Antigravity 会话里说一句即可（把两处路径换成你自己的）：

> 启用 TrainOps：读取 `<仓库路径>/skills/trainops/SKILL.md`，保留原有角色。实验项目目录为 `<实验项目路径>`。当前仅加载能力，不创建计划、不启动进程；收到任务后按 SKILL.md 的开工顺序执行：先以后台常驻任务起任务流程图面板（`serve`）并报出地址，再走无卡准备（本次开机配置为 0.5 核 CPU、2GB 内存、无 GPU，仅做适配该资源的轻量准备），之后才按授权申请有卡开机。以目标、验收标准和明确硬约束为准，自主优化调度，不必机械照搬我列出的步骤或顺序。动态调度的最终目的是加快实验迭代：在预算、验收标准和资源授权范围内，缩短从实验假设到可用于下一轮决策的有效结果的时间，而不是单纯追求 GPU 利用率或并行任务数量。优先推进能验证关键假设、解除后续依赖或决定下一轮实验方向的任务；每轮结果出来后及时复盘并调整后续安排，避免为了占满资源而运行低价值或重复实验。我可能提供多个 AutoDL 实例及各自对应的 SSH 名称；这些实例是候选资源，不保证当前都有空闲 GPU。先核实实例与 SSH 的对应关系、实际 GPU 可用性及已有任务，再按依赖、GPU 型号/显存、环境与数据就绪情况、成本和预计耗时动态调整实验顺序、并行度与实例分配。某台暂时缺卡时，优先用已就绪且满足要求的实例推进可执行实验，同时完成不依赖 GPU 的准备，不让单台缺卡阻塞整个计划；资源变化后继续重排，迁移前核实环境、数据和 checkpoint 兼容性，不重复启动或擅自中断已有训练。SSH 可达或开机请求已提交不代表 GPU 就绪，验证通过后才启动训练并标记 running。常规优化直接同步计划并简述理由，改变目标、放弃必需实验、增加未授权费用、中断已有任务或超出约定的缺卡等待范围时先确认。其他参考按需读取。

### 1.4 两个容易踩的坑

- **凭据在哪**：开发者 Token **只在面板上填，并且只存在面板进程的内存里**（表头「Token」按钮，见 §5）。不读环境变量、不读任何文件，CLI 也没有 `--token`——凭据从不跟着仓库走。这一点在装成插件时尤其要紧：Antigravity 会把整个插件根复制到 `~/.gemini/config/plugins/trainops/`，写进仓库的任何东西都会跟着这份副本走，重装一次就换掉。面板进程一退（重启、换端口重开都算）就要重新填；没填时本地命令照常可用，只是余额和「够不够跑完」缺结论。
- **两个版本号**：清单 `plugin.json` 里的 `version` 是**插件版本**（0.1.0），`trainops.py --version` 打的是**计划段 schema 版本**（3）。前者变了不影响账本，后者变了要先备份并核对文件。

## 2. 预览任务流程图演示面板

```bash
python3 skills/trainops/scripts/trainops.py -p examples/demo-project serve --port 8765
```

浏览器打开 **http://127.0.0.1:8765** 查看整份计划唯一的一张任务依赖主图：节点按网格排布，箭头表示前置依赖，服务端已尽量减少相邻层交叉；不显示开始节点，无前置任务就是最左侧的起点。画布用原生横向/纵向滚动查看，节点与箭头统一为中性色，任务节点只装自己的名称、实例、流程、子进度与预计时间。主图上方是一行流程完成度读数，页面顶部是余额与结算结论。演示计划自带一份结算记录（数据是编造的演示值），所以开箱就能看到结算记录；它超过 2 个周期（60 分钟）后自动回退为实时值并标注原因——这是设计好的降级，不是故障。

面板上能改的只有两处，其余一律是只读的：**时价**——点实例行右侧的「时价」，填当前档位的费率（无卡开机填无卡费率，有卡填 GPU 时价），保存后顶部的「现价可跑 X 小时」立刻重算；点「清空时价」则整块结论消失——缺数据就缺结论，不会显示成 0。**Token**——点表头的「Token」填开发者 Token，只存在这个面板进程的内存里，登记后当场结算一轮（见 §5）。

演示目录不要启动自动结算：`serve --interval` 会真的写入 `examples/demo-project/.agent/experiment.yaml`（那是版本库里的快照），需要真实结算时用自己的实验项目目录。

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

`--brief` 为显式选项，不改变默认完整文本/JSON 或脚本退出码：show 只看当前行动，写命令只看版本、编号和更新结果。每组最多 3 条，长文本标 `…`，同时报告总数/省略数；历史任务仍保留在账本。完整问题看 check，完整任务看 show。

brief 新增或结构编辑会随回执附带最终结构检查，问题/提醒不会撤销已保存修改；纯状态/进度/巡检更新成功后不必机械再跑 check + show。脚本门禁仍用 check --strict。详细参数和批量语法见 [CLI 参考](skills/trainops/references/cli.md)，配置见 [AutoDL 参考](skills/trainops/references/autodl.md)，异常见 [恢复协议](skills/trainops/references/protocol.md)，按需读取即可，不必加载本 README。

## 4. 命令速查

设 `CLI="python3 skills/trainops/scripts/trainops.py -p /你的路径/my-experiment"`；加 `--version` 可查看当前 schema 版本。

| 命令 | 说明 |
|---|---|
| `init --name ... [--instance ...] [--flow ...] [--task ...]` | 新建计划，支持一次传入全部实例、流程和任务（仅同流程同实例默认串联，可显式留空依赖以并行）；已有计划时拒绝覆盖 |
| `demo [--force]` | 在空目录生成演示计划，不涉及任何远程操作；`--force` 用于重置演示目录或已被改动的计划 |
| `check [--strict]` | 结构性检查：返回问题（阻断性）与提醒（如 pending 任务耗时待估、依赖间时间空隙过大）两组结果；`--strict` 在存在问题时不返回 0 退出码，便于脚本或 CI 拦截 |
| `show --brief` / `show` / `show --json` | 当前行动摘要（推荐 agent 使用）、完整文本或完整 JSON；brief 分组限量并注明省略数，与 --json 互斥。`未巡检` 指未记录 checked_at 或超过 1 小时，`依赖就绪` 不保证远端在线 |
| `set <任务ID> [--status ...] [--progress ...] [--instance ...] [--deps ...] [--minutes ...]` | 更新单个任务（`task set` 的简写） |
| `task add --flow F01 --name 名称 [--instance I01] [--minutes 30] [--deps ...] [--not-before ...]` | 新增单个任务；`flow add --name ...`、`instance add` / `instance set I01 --status on --observed` 同理 |
| `batch [--instance ...] [--flow ...] [--task ...] [--set ...]` | 单次命令批量追加实例/流程/任务或批量更新任务进度与状态（`--set "F01-T01:done:100:备注"`） |
| `instance set I01 --disk "data:40/50,fs:5/200"` | 登记各盘用量（GB）；`--disk` 逐盘合并，`data:none` 清空该盘 |
| `instance set I01 --mode no-gpu`（或 `gpu` / `none`） | 登记开机模式：无卡准备（含持续请求等待 GPU）记 `no-gpu`，正式有卡开机记 `gpu`，核实不了记 `none`（未观测）。时价不在 CLI，见下方「余额与可用时长」 |
| `wallet set --assets 128.50 [--voucher 20]` | 面板没开或要人工核对时手工登记余额（`none` 清空；无参数会被拒绝）。余额没有 CLI 自动拉取：结算由面板进程负责 |
| `serve --port 8765 [--interval 30]` | 启动本地任务流程图 Web 面板（仅监听 `127.0.0.1`）；面板上**能写的只有实例时价与开发者 Token**，其余路径与写动词一律拒绝；带 `--interval` 时同一进程内再跑一个结算线程。它是前台进程，**要常驻就由调用方以后台任务起**（别用 shell 的 `&`：父 shell 一退进程就被带走），CLI 自己不后台化、不写 PID、不碰 systemd |

几个容易被忽略的约束：同一实例同时只允许一个 `running` 任务（`local` 不占 GPU，可并行）；依赖未完成的任务不能被标记为 `running`；实例上仍有任务运行时不能把该实例标为 `off` / `unreachable`。

## 5. AutoDL 存储、TensorBoard 与余额

项目只面向 AutoDL，开机准备由外部工具执行，存储与花销由账本记录和告警。**面板不展示磁盘与 TensorBoard 信息**——这些是 agent 巡检时要盯的事，写在 `show`／`check` 里更有用。

### 无卡准备 → 有卡训练

多实例筛选优先只读 `list_instances(instance_uuids=[...])`，每批 1–100 个完整 UUID；支持去重、白名单校验和逐项错误，共享一次分页遍历、硬件查询最多 5 路并发，无需 Agent 另编排并发。逐项核实，失败项保留待核实，不当作实例不存在或立即重查整批。单实例、批量工具不可用或操作后定点复核用 `instance_status(instance_uuid)` 核实状态、模式、计费类型及硬件规格：`gpu_name`（如 RTX 3090）、`gpu_memory_gb`（如 24.0，单卡显存，按 1024³ 字节换算）、`cpu_cores`（如 14，实例分配的 vCPU 数）。规格来自控制台“规格详情”的配置快照，关机时也可查询、无需 SSH，可用于开机前筛选候选实例；缺失或无效的可选字段返回 `null`，保留待核实、不补 0。快照不是 GPU 已分配或可训练的证明，也不能覆盖无卡模式的实际资源上限；后台开机重试仍只查运行状态，不额外请求硬件规格。

无卡模式开机是指**本次开机使用 0.5 核 CPU、2GB 内存、无 GPU 的配置**，不是保留原有 CPU/内存规格只去掉 GPU。准备工作须适配该资源上限：优先阅读代码、调整配置、检查路径与少量数据；限制安装/编译和数据加载的并发与内存占用。重型编译、全量数据预处理或大模型加载等超出资源的工作移到授权且资源充足的环境，或留到有卡阶段补做；不因无卡阶段资源不足就判定环境损坏，也不为完成这些检查反复触发 OOM。

通过 `autodl_mcp` 对普通按量实例默认采用：**`power_on_no_gpu` 无卡开机 → 了解项目/环境、完成配置与代码调整、执行无需 GPU 的检查 → 保存并结束准备进程 → 按授权 `switch_mode(instance_uuid, target_mode="gpu")`（内部关机、确认关机、再开机，缺货后台持续重试）→ 验证 GPU 与训练入口 → 正式训练**。开机与切换均返回 `job_id`，优先用只读 `wait_for_job(job_id, timeout_seconds=15)` 观察，`job_status(job_id)` 保留用于即时进度；不存在独立的“持续开机”工具。默认进入 `waiting_capacity` 即返回，即使指定 120 秒也不会硬等缺货；及时汇报 `phase`、`elapsed_seconds`、`attempts`、`next_retry_in_seconds` 并推进其他工作，后台任务耗时不是训练进度或预计有货时间；`next_retry_in_seconds` 是距下次后台重试的剩余秒数（`null` 表示无待定重试或任务已结束），不是资源可用倒计时。确需继续等待且在授权范围内时用 `wait_for_job(job_id, timeout_seconds=15, return_on_capacity=False)`，不紧密重复默认调用。`timeout_seconds` 支持 0–300 秒（0 为立即查询），按授权剩余等待时间和工具调用超时上限缩短；终态立即返回，等待超时返回当前快照。等待超时、连接断开或取消等待请求不会取消后台任务，不应因此重发开机；长轮询不增加上游轮询或开机请求。

`switch_mode` 不是热切换或可回滚事务；反向转无卡用 `target_mode="non_gpu"`，均须确认中断任务和计费影响已获授权，已有授权覆盖则不重复询问。活动期间不另提交同实例开机或切换；失败可能已关机，先查真实状态。切换成功后仍核实模式与 GPU 可用性。工具不支持时才分步 `power_off` → 查询确认关机 → `power_on`；已关机实例直接按所需模式开机，不重复切换。止付仍用 `power_off`，会先取消活动切换、防止后续自动拉起，不用切成无卡代替关机。

这是收到任务后的默认路线，不是"有时间才做"：agent 应先起任务流程图面板让进度可见，再走无卡准备，**不该在没准备的情况下直接申请有卡开机让 GPU 空等**；只有用户明确要求持续请求，或已核实无需远端准备，才直接 `power_on`。无卡准备完成后为切换模式而关机属于准备流程，不是结束实验，仍须确认准备进程退出、配置与产物落盘并取得授权。

开关机及持续请求按用户授权执行；已有训练不为此重启。切换时的关机保留实例与后续计划，不是释放实例；持续请求已提交或 SSH 可达都不代表 GPU 已就绪，未就绪不得把训练标为 running。无卡不等于免费：账本用 `--mode` 区分两种开机模式，无卡按无卡费率（默认 0.1 元/小时）、有卡按 GPU 时价，两个档位各自登记、互不覆盖；不以 0 或另一档位的价格代替。MCP 当前 `price: null` 及其原因表示缺少可信报价，不代表免费，不覆盖面板已有费率，也不据此作成本最优结论。工具参数以当前 MCP 说明为准，TrainOps 不实现开关机或自动抢卡。详见 [开机模式与费用边界](skills/trainops/references/autodl.md#无卡准备与训练开机)。

### 训练故障：自愈 5 次后先关机止付

训练或准备出问题（进程崩溃、OOM、依赖/数据缺失、SSH 断、盘满、GPU 不可用、checkpoint 损坏）时走这条固定路线：

1. **先报告**：说清现象与已核实的证据（进程、日志、`nvidia-smi`、盘用量、实例状态）以及影响哪个任务，不静默重试、不伪造进度。
2. **自愈**：定位原因后对症处理——调参数、补依赖、清盘、重下数据、重启训练进程；一次只改一个变量，每次核实真实结果（进程在跑、GPU 在用、checkpoint 在写），同一个动作不重复第二遍。**最多 5 次：5 次是上限不是配额，修好就继续训练。**
3. **第 5 次仍无法继续 → 先关机止付**：结束训练/准备进程、确认必要产物与日志已落盘，`power_off` 后用 `instance_status` 确认真已关机；任务按真实结果记 `failed`（在等你拍板时记 `blocked`），不标 done、不留 running。

这一步关机是你对「5 次自愈失败」给出的**预授权中间关机**，agent 不必再等一轮确认——它止的是空转计费，代价只是保留实例与后续任务，重新开机仍走原来的授权与无卡准备流程。已经明确无法继续的情形（实例不健康、长时间缺货、数据缺失、验收口径变化、要加钱）不必凑满 5 次，可直接止付关机并说明理由。**其它关口不变**：最终关机、释放实例、超出约定的持续请求等待仍须先获授权。细则见 [异常与恢复协议](skills/trainops/references/protocol.md#故障自愈与止付关机)。

### 三盘规范（详见 [AutoDL 配置](skills/trainops/references/autodl.md#三盘与用量)）

| 路径 | 特性 | 放什么 |
|---|---|---|
| `/` 系统盘 | 关机不丢、随镜像保存、容量最小 | 代码、conda 环境、脚本 |
| `/root/autodl-tmp` 数据盘 | 快、关机不丢、**不随镜像保存、释放实例即丢** | 数据集、checkpoint、TB 事件、高 IO 产物 |
| `/root/autodl-fs` 文件存储 | **同一账号下所有实例共享同一份**、不受开关机与镜像影响 | 需长期保留或跨实例共享的产物 |

文件存储 `/root/autodl-fs` 对**所有实例是共享的**：任一实例写入，其余实例立刻可见，所以跨实例交接（只需上传一次的数据集、对照实验共用的输入、需要汇总的评测结果）直接落在它上面，不必实例间互传；代价是共享盘上的同名路径会真的撞在一起，多实例并发写要按实例/任务编号分目录（如 `/root/autodl-fs/<项目名>/F01-T02/`），清理前也要确认不是别的实例正在读写的产物。

`instance set I01 --disk "sys:12/30,data:44/50,fs:5/200"` 登记用量后，`show` 的实例行会显示 `盘 系统 12/30G·数据 44/50G`，已用 ≥90% 的盘带 `!`，`check` 同时给出提醒。

### TensorBoard（数据盘 + 软链）

```bash
TB=/root/autodl-tmp/tf-logs/<项目名>     # 每任务一个 run：$TB/F01-T02
ln -sfn "$TB" /root/tf-logs/<项目名>     # 内置 TensorBoard 免启动即可读
```

换 logdir 前先定位并核实本项目服务进程，按授权停止后重启，不批量杀掉其他实验的 TensorBoard；端口与访问方式见 [TensorBoard 配置](skills/trainops/references/autodl.md#tensorboard)。

### 余额与可用时长

Token 在**面板**上登记，不在 CLI：打开 `serve` 后点表头的「Token」，粘贴控制台 → 设置 → 开发者Token。它只存在面板进程的内存里——不落盘、不进项目文件、不进日志或异常文本，面板重启要重新填。

```bash
$CLI show                                # 预算行：余额、可跑小时、计划剩余 GPU 小时与花费、够不够
$CLI wallet set --assets 128.50          # 面板没开或要人工核对时手工登记余额
```

时价也只在面板上登记：点实例行右侧的「时价」按钮。无卡开机（含持续请求等待 GPU）登记的是无卡费率，GPU 模式开机后登记的是 GPU 时价——两个档位各存各的，切模式不会互相覆盖；开机模式本身由 agent 用 `instance set I01 --mode ...` 登记。

可用时长只按现金余额 `assets` 计算，代金券单独展示；任一开机实例缺当前档位的费率或任一任务完成时间待估时，对应结论**不显示**而不是给出残缺数字。面板顶部只呈现这些算得出的结论：余额、代金券、可跑小时、"够不够跑完"，外加一块结算记录（文案由服务端写好，面板不做任何计算）。

### 自动结算（余额与剩余计划开销定时重算）

结算**只由面板进程做**，没有第二个常驻结算进程：起面板时带 `--interval`，它就每 N 分钟刷新一次余额并重算结论；在面板上登记 Token 的那一刻也会立刻结算一轮。

```bash
$CLI serve --interval 30 --port 8765   # 一个进程管两件事：看板 + 结算
```

面板在跑，结算就在跑；它一退，余额就不再刷新：超过 2 个周期没有新结算就回退为实时值并说明原因，**同一屏不会出现两套互相矛盾的数字**。结算失败（断网、Token 失效）只记进计划的 `last_error`，不打断结算，面板进程下一轮自己重试；`check` 会把它当成提醒报出来。面板重启后 Token 要重新登记，否则结算写着"未配置 Token"继续跑——不报错，只是少给余额结论。

面板是**前台**进程：后台运行与日志由调用方管理，它不会自动后台化、不写 PID 文件、也不注册系统服务。**起面板的命令必须以后台任务的形式常驻**——用你所在 harness 的后台任务机制起，不要用 shell 的 `&`，更不要把前台占住：那样面板是那次 shell 调用的子进程，父 shell 一收尾（命令返回、会话切换）它就被带走，结算同时停摆。反过来也要清楚它的寿命上限：后台任务活在 harness 进程里，harness 一退面板就没了，重开前先确认没有旧面板还在跑（端口被占就是旧面板还活着），重开后 Token 要重新登记。

## 6. 测试

```bash
python3 -m unittest discover -s tests -v   # 计划账本 / CLI / 并发写 / 自动结算 / 余额接口 / 演示快照一致性与插件清单
node tests/test_web.cjs                    # 面板渲染：原生滚动画布/网格节点/无开始节点/直线箭头/中性色、服务端几何原样打点、滚动保持、余额与结算块、逾期提示、XSS 转义
```

测试进程不碰网络：需要余额接口的用例把 `TRAINOPS_API_BASE` 指到本地假服务，凭据直接传参（没有任何文件或环境变量可读，也就没有"别读到开发机真 token"这回事）。装了 `agy` 时，清单测试会调它的官方校验器（`agy plugin validate --strict`）复验一遍，并用临时 `HOME` 真装一次插件、核对技能与规则都到位——两条都不会碰你本机的 Antigravity 配置，`agy` 没装则跳过。

## 7. 改这个仓库

这一节给维护者：怎么跑、数据怎么流、哪些约束不能破。用它的 agent 不需要读。

### 硬约束

- **零第三方依赖**：只用 Python 标准库；测试用 Node 内置模块（没有 `package.json`，不装 npm 包）。
- **写计划只有两条入口**：CLI，以及面板的时价接口（`POST /api/instance-price`，只能改时价）。两条都走 `Store.change()`；项目文件 `$PROJECT/.agent/experiment.yaml` 的 `plan` 段是唯一事实来源，禁止用 Edit/Write 直接改。面板另有一个 `POST /api/token`，只把开发者 Token 写进面板进程内存——不经过 `Store`，也不落任何文件。
- **缺数据就缺结论**：算不出的数字（缺费率、缺耗时、缺余额）一律不显示，绝不用 0 顶替或自行估算。这条口径同时约束 CLI 文本、`/api/plan` 与面板三处。

### 常用命令

```bash
python3 -m unittest discover -s tests -v      # Python 测试（约 30 秒）
node tests/test_web.cjs                       # 面板渲染测试（无 npm 依赖）

# 跑单个用例 / 单个类
python3 -m unittest discover -s tests -k wallet
python3 -m unittest tests.test_trainops.BudgetTests.test_hours_use_cash_only_and_skip_unknown_pieces -v
python3 -m unittest tests.test_plugin -v      # 插件清单（装了 agy 时顺带跑官方校验器）

# 演示面板（自带演示计划与结算记录，不连服务器）
python3 skills/trainops/scripts/trainops.py -p examples/demo-project serve --port 8765
```

CLI 的 `-p/--project` 指向**实验项目目录**，不是本仓库（`--version` 打当前 schema 版本）。两个容易踩的坑：

- 改了 `demo_plan()` 就要重新生成仓库里的演示快照，否则 `RepoSnapshotTests` 失败：
  `python3 skills/trainops/scripts/trainops.py -p examples/demo-project demo --force`
- 别在 `examples/demo-project` 上跑 `serve --interval`：结算会真的改写版本库里的那份快照。

### 架构

数据流是单向的：`.agent/experiment.yaml` 的 `plan` 段 → CLI（`Store` → `forecast`/`budget`/`settlement`）→ `show` 文本或 `/api/plan` JSON → 面板只渲染。`config` 与 `experiments` 两段由 `ExperimentLog` 维护，不进入面板快照。

`skills/trainops/scripts/` 按职责拆分，入口仍是 `trainops.py`（只做模块组装、计划写状态机与命令分派）：

| 模块 | 职责 |
|---|---|
| `trainops.py` | 入口与兼容层：计划状态机（`set_task`）、`demo_plan`、命令分派、`main`，并重导出其余模块的公开函数 |
| `trainops_base.py` | 常量、`PlanError`、时钟与通用校验 |
| `trainops_model.py` | 计划文档构造、`validate()`、实例/流程/任务操作 |
| `trainops_store.py` | `Store`、文件锁、原子写与整份文档校验 |
| `trainops_graph.py` | `forecast()`、`layout()`、`budget()`、`settlement()`、`snapshot()`、`check_structure()` |
| `trainops_web.py` | 本机面板 `Handler`、`Credential`、AutoDL 余额与结算线程 |
| `trainops_experiments.py` | 实验配置、Run 历史与飞书同步 |
| `trainops_specs.py` / `trainops_cli.py` | CLI 简写解析与 argparse 命令树 |
| `trainops_brief.py` | CLI 的 brief 文本输出 |

- `Store`：唯一读写入口。`.agent/experiment.yaml` 是 JSON 兼容 YAML 的单个文档，含 `config` / `plan` / `experiments` 三段；`fcntl.flock` 排他锁 + 临时文件 `os.replace` + 文件与目录 fsync。计划读改写走 `store.change(operation)`，实验配置与 Run 历史走 `store.update(operation)`——后者只改自己那两段，不推进计划 `revision`。
- `validate()`：读取与写入两侧都跑，账本不变量集中在这里——编号与计数器一致性、`TRANSITIONS` 状态机、依赖闭环（Kahn）、`done` ⇒ 进度 100、同一 GPU 实例同时只能有一个 `running`。校验失败只报错，**从不静默修复**；schema 版本不符直接拒绝读（开发阶段无迁移，提示"备份并核对文件"）。
- `forecast()`：排程的唯一事实来源。每个 GPU 实例一条车道，`estimate.start/finish` 由依赖 + 车道 + `not_before` 推出；`running` 任务靠 `progress_at` 采样外推，进度 100% 或已超期即降级为"待估"。
- `layout()`：生成整份计划唯一的主图与各流程完成结论。返回值是 `(graph, views)`：`graph.nodes` 中每个任务含 `id/layer/row/progress`；`graph.edges` 含 `source/target/waypoints`，跨层依赖在中间层插入不可见路点，拆成只连接相邻层的线段；`columns/rows` 是画布网格尺寸。列由依赖最长路径决定，行由重心排序与局部相邻交换减少交叉；交换时只计算受影响两层的局部交叉数，不再为每个候选重算整张图。不生成开始节点。`view.progress/finish/known` 与完成计数保持原口径。总进度是**已完成 ÷ 未取消任务**，全取消时为 null。没有时间轴、占位耗时或条形几何；层级不代表时间、资源分配或可执行性。
- `budget()` / `estimate_view()` / `settlement()`：共用一套口径，并支持调用方传入已算好的 `estimates` / 预算视图；`snapshot()` 与每轮结算只排程、只算预算一次。可用时长只按现金余额 `assets` 算，代金券单独展示；费率按开机模式取——无卡（`mode=no-gpu`，含持续请求等待 GPU）按 `no_gpu_price`，有卡按 `price`，模式未观测时按 `price`（宁可高估花费），费率缺失则整块不下结论。结算结论超过 2 个周期未更新就回退为实时值并说明原因，同一屏不出现两套数字。
- `plan_fingerprint()`：用参与计算的字段做哈希，判断"结算之后计划有没有变过"。不要改用 `revision`——结算每轮都自增它，会永远显示"有变更"。
- `Credential`：开发者 Token 只活在这里——面板是唯一入口，值只存在面板进程内存，进程一退就没了，重开面板要重填。它不落盘、不进计划、不进日志，`status()` 只外露"配没配、什么时候配的"。
- `cycle()`：一轮结算，token 由调用方从 `Credential` 现取（没有环境变量、没有文件、没有 `--token`）。联网在文件锁**之外**，落盘只走一次 `store.change()`；失败只写 `monitor.last_error`，不打断结算、不动其余数据。
- `Handler`：可写的只有两个端点——`POST /api/instance-price` 与 `POST /api/token`；其余路径与写动词一律 404/405。Host 白名单只放 127.0.0.1/localhost（防 DNS rebinding），另加 Origin 同源与 `application/json` 两道关。`/api/plan` 与登记响应都带 `credentials` 块，但只有布尔与登记时间，没有值。
- AutoDL 外部调用是只读余额接口（POST 无请求体、拒绝重定向以免转发 `Authorization`）。返回值是千分之一元整数，单位对不上就报错，而不是写错 1000 倍。

实验日志另由 `ExperimentLog` / `experiment` 命令维护：同一个 `.agent/experiment.yaml` 里，`config` 段保存非敏感配置，`experiments` 段保存 Run 和同步状态（文件是 JSON 兼容 YAML 1.2，不支持任意 YAML）。项目路径复用 `Store.root`，与计划共用一把文件锁与同一套原子写；飞书通过参数数组调用 `lark-cli`，网络期间不持锁。同项目同步串行；按 Run ID 精确查询去重、内容指纹去重写、版本差异保留补录。普通计划命令和余额结算不触发飞书。首次建表前落结果不确定标记，成功后立即保存目标；出错不另建表。测试使用 mock/临时假 CLI，不访问真实授权或飞书。

`skills/trainops/web/index.html`（单文件面板，无构建步骤）：

- **轻量静态布局**：只用原生 HTML/CSS/JS，不引外部字体/图片/图标库/JS 库，也不用 canvas 元素，不做持续动画。改完必须跑 `node tests/test_web.cjs`。
- **面板不做业务计算**：金额、结算文案、主图节点的列/行、跨层路点、画布网格尺寸、流程总进度、任务进度与完成计数由服务端 `/api/plan` 统一下发（`layout()` + `snapshot()`）。前端只把行列与固定间距映射为画布坐标，不重排依赖、不推算 ETA。`when()` 与 `money()` 仅负责格式化。
- **只有一张主图**：`plan.graph` 是整份计划唯一的有向无环图，不显示开始节点，无前置任务就是最左侧一列的起点；流程不再各自出图，只在主图上方以一行纯文本给出完成数与总进度。所有节点按服务端下发的列/行绝对定位在同一块画布上；画布是原生滚动容器，自动刷新后保留滚动位置。每个任务节点都是叶子，只放任务自己的编号、名称、实例、流程、子进度与预计时间（以及可选备注），不嵌套子任务或外部引用列表。
- **同一个数只画一处**：流程完成度只在主图上方以文字给出；任务子进度只在对应节点显示细进度条与百分比，不另造第二套统计。
- **视觉口径是一张纸**：中性色纸面、白色节点、细横线分隔，不使用实例配色、渐变、图例、药丸标签或装饰性卡片。颜色只留给状态与结论：状态点、任务节点左侧细线、余额结论与错误提示；其余交互一律墨色。沿用深浅主题。完整任务名称自然换行，不做省略或固定高度裁剪。
- **连线不测量 DOM**：服务端负责分层、插入跨层路点并减少相邻层交叉；前端用固定像素网格（列距 380px、行距 210px、节点宽 280px）把 `layer/row` 映射为坐标，每段连线是一条旋转的 1.5px 中性色细线，最后一段带箭头。不要加入尺寸测量、布局观察器、碰撞检测或迭代避障；改固定间距时同步修改 CSS、渲染坐标与测试。
- DOM 一律 `createElement` + `textContent`：项目文件里的文本是不可信输入（`test_web.cjs` 有 XSS 用例）。
- 每 10 秒轮询 `/api/plan`，`document.hidden` 时跳过；重绘必须保留画布的滚动位置。
- **两个弹窗**（时价、Token）都挂在 `#modal`（不在重绘范围内），否则轮询会冲掉正在输入的值。

### 文档也是产品

`SKILL.md` 是热插拔入口（外部 agent 读的那份）、`rules/trainops.md` 是 Antigravity 的规则入口、`references/protocol.md` 是异常与恢复的按需参考、`README.md`（本文件）面向用户。CLI 行为变了，这几处要同步。

外部 agent 反复踩过的两条默认路线，改入口时别丢（`test_plugin.SkillTests.test_entry_keeps_task_startup_order` 盯着）：

- **收到任务就要起面板**：`serve --port 8765 --interval 30`（一个进程顺带结算，也是唯一的结算方式）；必须**以后台任务常驻起**——用 harness 的后台任务机制，别用 shell 的 `&` 或前台，父 shell 一退面板就被带走。
- **无卡准备是默认**：先 `power_on_no_gpu` 准备再申请有卡，只有用户明确要求持续请求或已核实无需准备才直接 `power_on`。
- **故障自愈有上限**：先报告再自愈，最多 5 次，仍无法继续即先关机止付（唯一预授权的关机情形，见 [协议](skills/trainops/references/protocol.md#故障自愈与止付关机)）。

`SKILL.md` 有 2000 字符预算（`test_trainops` 的入口用例；2026-10-16 由 1800 放宽，当时只剩 4 字符，新规则只能靠削减别处硬挤），新约束先看能不能挤进这个预算，别靠加长入口来写第二份细则。

### 插件打包

一份技能、一份清单，只有 Antigravity 一家：

| 文件 | 作用 |
|---|---|
| `plugin.json` | 插件清单：`$schema` 指向 Antigravity 的 schema，`name` 决定装到 `~/.gemini/config/plugins/<name>/`，`version` 是插件版本 |
| `rules/trainops.md` | 规则入口（≤40 行）：只做指针 + 三条不变量，细则留在 `SKILL.md` 与 `references/` |
| `skills/trainops/` | 技能本体；不要移动这一层——脚本靠 `parents[3]` 回推插件根 |

三条不能破的规矩：

- **不要移动 `skills/trainops/`**：脚本靠 `parents[3]` 回推插件根，`references/` 与 `web/index.html` 也按自身位置找（`test_plugin` 有深度守卫）。入口文件里不写绝对路径。
- **入口文件只做指针**（≤40 行）：细则一律留在 `SKILL.md` 与 `references/`，同一份约束写两处必然漂移。
- **别再加别家的清单**（`.claude-plugin/`、`.codex-plugin/`、`gemini-extension.json`、根 `AGENTS.md` 等）：本插件只服务 Antigravity CLI，`test_plugin.ManifestTests.test_only_antigravity_packaging_exists` 会直接判失败。

### 约定

- 全仓库中文：文档、CLI 输出、`PlanError` 文案（写成可直接执行的建议）、测试断言消息、commit message（中文摘要 + 按子系统分条的 `-` 列表）。
- 失败必须原子：被拒绝的编辑不留下半个文件（有测试逐字节比对项目文件）。
- Token 只进请求头：绝不写进项目文件、stdout/stderr 或异常文本。它**只有面板一个入口**（`POST /api/token`），只存在面板进程内存里——不要给它加环境变量、配置文件或 CLI 参数（插件被复制/远程安装后，跟着仓库走的凭据文件都会丢）。
- 计划表只记核心字段（状态、进度、实例、依赖、耗时）；命令、日志路径、commit 一律不进计划。
- 新增测试沿用既有约定：不碰网络也不碰开发机上的凭据——需要余额接口的用例把 `TRAINOPS_API_BASE` 指到本地假服务，Token 直接作为参数传给 `cycle()`；需要 `agy` 的用例用临时 `HOME`。


