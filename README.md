# TrainOps

面向 **Codex、Claude Code 与 Antigravity CLI** 的插件形式的**多实例训练执行调度能力**：本地持久化维护项目计划表、支持多并行训练流程与跨流程任务依赖、提供以中性色为主、运行节点按实例身份色标识、按实例分组、原生滚动的执行队列 Web 面板（含流程进度与节点子进度），并精简 agent CLI 操作以减少上下文占用与幻觉。

- **资源来源与多实例并行**：用户须明确使用 AutoDL 实例，还是在 Vast.ai 上寻找实例；未明确时 agent 先询问，不默认平台。AutoDL 使用外部 `autodl_mcp`，Vast.ai 必须加载外部 `vastai:vastai` skill；搜索与租用授权分开。多台已授权 GPU 实例可并行推进不同流程，单实例独占防冲突。见 [Vast.ai 接入](skills/trainops/references/vastai.md)。
- **规律编号与依赖**：实例 `I01`、流程 `F01`、任务 `F01-T01` 编号稳定，支持 Flow 内并行 DAG、同流程同实例默认串联与显式跨流程依赖。
- **执行队列 Web 面板**：按实例横向展示执行中与待执行任务，虚线箭头表示同实例候选排队顺序，不代表真实前置依赖或已在远端启动。本地、未分配、已结束及需处理任务单独展示，不画串行连线；通过原生横向/纵向滚动查看。任务卡与箭头以中性色为主，执行中任务的左侧色条取所属实例的身份色，实例行用同一色条对应；完整显示任务名称、实例、流程、实际子进度与预计时间。流程完成度以一行纯文本读数展示「已完成 ÷ 未取消任务」。面板只读展示执行队列与进度。
- **自主优化调度**：agent 以目标、验收和硬约束为准，可按依赖、资源可用性、预计耗时与实验结果自主重排、拆并任务和调整实例分配，不机械照搬任务顺序；改变目标、放弃必需实验或超出资源授权时先确认。
- **单 agent 多机再平衡**：任务完成/失败、实例可用及巡检时检查全局待执行任务，借助现有 `set --instance` 改派合格任务；先排除原远端队列重复启动，再由同一个 agent 按最新计划在目标机实际启动并核实，不只更新面板。只搬尚未启动的任务，不绕过真实依赖或节点约束；agent 未运行时不再平衡，不新增常驻调度器。见[执行闭环](skills/trainops/references/cli.md#单-agent-多机再平衡)。
- **标准化 Agent 操作**：首选 `apply --from-json` 一次完成建表或增量更新，只填核心字段（状态、进度、耗时、依赖），临时 `key` 自动映射为稳定编号；需要时也可先读后写直接编辑账本并跑 `check --strict` 校验，避免上下文过长产生幻觉。
- **三端原生插件安装**：Codex、Claude Code 与 Antigravity CLI 各有原生清单，共用 `skills/trainops/` 中的能力契约、脚本、参考和面板，不维护多份规则。后台任务、定时巡检与 MCP 按[宿主能力适配](skills/trainops/references/hosts.md)执行。
- **无卡准备再训练**：通过外部 `autodl_mcp` 先无卡开机，完成项目/环境了解、配置与代码调整；准备就绪后关机，再按需正常开机或持续请求 GPU，验证就绪后正式训练，减少 GPU 空等。
- **AutoDL 存储与 TensorBoard**：按三盘规范登记磁盘用量，TensorBoard 事件落数据盘并持续核实写入，巡检时沿用项目已有的指标与记录方式。
- **飞书实验日志**：agent 巡检同时用 `lark-cli` 维护多维表格；复用用户指定/项目已有表，未指定则自动创建。项目配置、计划账本与 Run 历史统一存实验项目 `.agent/experiment.yaml`，不覆盖人工决策。见[实验日志](skills/trainops/references/experiments.md)。
- **执行与通知闭环**：MCP `trainpilot` 在固定巡检和重大节点即时推送训练状态，终态直接上报，独立于 `lark-cli` 实验日志；异常按实时协议取得决定，自行修复只做一次 ACK 恢复汇报；用户选择等待时保持现场、等直接对话，不从飞书或群消息拉取方案。取消任务同时停止本任务进程并按授权关机；完成/取消清理本任务不再需要的巡检与资源等待，汇报结果位置和实例最终状态，面板可保留。代码本地修改验证、提交推送后远端拉取核对。见[TrainPilot 接入](skills/trainops/references/trainpilot.md)与[执行与通知闭环](skills/trainops/references/execution.md)。
- **零第三方 Python 依赖**：仅需 Python 3.10+ 标准库（Linux / macOS / WSL）；飞书同步另需已安装并授权的 `lark-cli`。

## 1. 插件安装

支持 Codex、Claude Code 与 Antigravity CLI，仓库根就是插件根，三端共用 `skills/trainops/`。只需安装当前使用的客户端对应入口。安装后仍需在宿主中连接外部 `autodl_mcp`、`trainpilot`，并按需安装授权 `lark-cli` 与外部 Vast.ai skill；插件不携带凭据，也不自动配置这些服务。

### 1.1 Claude Code

在 Claude Code 中添加本地市场并安装：

```text
/plugin marketplace add <仓库路径>
/plugin install trainops@trainops-marketplace
```

也可以在终端执行：

```bash
claude plugin marketplace add <仓库路径>
claude plugin install trainops@trainops-marketplace
```

安装后重新加载插件或重启会话，以 `/trainops:trainops` 调用。仅本次会话试用可用 `claude --plugin-dir <仓库路径>`，不需要安装市场。

### 1.2 Codex

使用支持 `codex plugin` 的版本，在终端执行：

```bash
codex plugin marketplace add <仓库路径>
codex plugin add trainops@trainops-marketplace
codex plugin list --marketplace trainops-marketplace
```

重新打开会话，从插件技能列表选择 `trainops` 或在对话中要求使用 TrainOps。若当前客户端没有 `plugin` 子命令，先升级到支持插件的版本，或使用下述技能目录方式。清单位于 `.codex-plugin/plugin.json`，本地市场位于 `.agents/plugins/marketplace.json`。

Claude Code 与 Codex 都通过各自的市场入口安装整个插件根；更新仓库后使用客户端的市场刷新与插件更新/重新安装机制同步，已安装缓存不保证实时跟随本地编辑。

### 1.3 Antigravity CLI

```bash
agy plugin install <仓库路径>     # 整个插件根复制到 ~/.gemini/config/plugins/trainops/
agy plugin list                  # 确认识别到 trainops（skills 1 · rules 1）
```

装的是插件根的**副本**：改完仓库要再 `agy plugin install <仓库路径>` 同步一次；不想敲命令就在 CLI 里打开 `/plugin` → 「+ Install from local directory...」选同一个目录。开关与卸载按目录名走：`agy plugin disable trainops` / `enable` / `uninstall trainops`。

### 1.4 不装插件：挂到技能目录

只挂技能时不会加载 Antigravity 的规则入口。先创建所选目标的父目录，再选择对应宿主的一条软链命令：

```bash
ln -s <仓库路径>/skills/trainops ~/.claude/skills/trainops              # Claude Code 全局
ln -s <仓库路径>/skills/trainops ~/.agents/skills/trainops              # Codex 全局
ln -s <仓库路径>/skills/trainops ~/.gemini/config/skills/trainops       # Antigravity 全局
ln -s <仓库路径>/skills/trainops <实验项目>/.agents/skills/trainops      # Codex / Antigravity 项目内
ln -s <仓库路径>/skills/trainops <实验项目>/.claude/skills/trainops      # Claude Code 项目内
```

**用软链，别复制**：脚本靠自身位置回推 `references/` 与面板页面，软链能保住这条路。Windows 上 `git` 若没开符号链接，链会落成普通文件，改用 `mklink /D`，或者干脆走 1.5。

### 1.5 连目录都不想挂：直接指路

在所用客户端的会话里说一句即可（把两处路径换成你自己的）：

> 启用 TrainOps：读取 `<仓库路径>/skills/trainops/SKILL.md` 并保留原有角色。实验项目目录为 `<实验项目路径>`；当前仅加载能力，不创建计划、不启动进程。收到任务后按 SKILL.md 的开工顺序执行：先以后台常驻任务起任务执行队列面板（`serve`）并报出地址，先明确资源来源：使用我提供的 AutoDL 实例，或按我的约束在 Vast.ai 上寻找；我未明确时先询问。AutoDL 再走无卡准备（0.5 核 CPU、2GB 内存、无 GPU，仅做适配该资源的轻量准备），之后才按授权申请有卡开机；Vast.ai 先加载外部 `vastai:vastai` skill，尽量在本地完成准备，再搜索并按授权租用，不套用 AutoDL 无卡模式。以目标、验收标准和明确硬约束为准，在资源授权内自主优化调度，优先推进能验证关键假设、解除依赖或决定下一轮方向的任务，不必机械照搬我列出的步骤或顺序。我可能提供多台 AutoDL 实例及各自对应的 SSH 名称；先核实实例与 SSH 的对应关系、实际 GPU 可用性及已有任务，再按依赖、显存、环境、可用性和预计耗时动态分配。常规重排直接同步计划并简述理由；改变目标、放弃必需实验、超出资源授权或中断已有任务前先确认。其他参考按需读取。

### 1.6 两个容易踩的坑

- **面板要真常驻**：`serve` 是前台进程，必须用宿主的后台任务或持久执行会话起，并核实地址可访问；见[宿主能力适配](skills/trainops/references/hosts.md#面板后台任务)。别用短命 shell 的 `&` 或占住前台；退出宿主后不保证仍可访问，重开前先确认旧面板项目与端口。
- **两个版本号**：清单 `plugin.json` 里的 `version` 是**插件版本**（0.1.0），`trainops.py --version` 打的是**计划段 schema 版本**（3）。前者变了不影响账本，后者变了要先备份并核对文件。

## 2. 预览任务执行队列演示面板

```bash
python3 skills/trainops/scripts/trainops.py -p examples/demo-project serve --port 8765
```

浏览器打开 **http://127.0.0.1:8765** 查看**执行队列**：按当前实例分配和既有排程规则，将执行中任务与待执行实验横向排列，虚线箭头只表示同实例的候选先后顺序，不是模型产物依赖，也不表示任务已在远端启动。未知耗时仍显示候选队列，但不猜测完成时间；改派或状态变化后随快照重算。本地、未分配、已结束及需处理任务单独展示，不画串行排队连线。真实 `deps` 仍用于校验和排程，但不再提供依赖图视图，也不应把排队顺序写成硬依赖。

队列用原生横向/纵向滚动查看，执行中任务用所属实例的身份色标识，任务卡展示自己的名称、实例、流程、子进度与预计时间。队列上方是一行流程完成度读数。演示计划用于预览队列与进度，不连接服务器。

面板是**只读**的执行队列视图，不提供业务写入入口。

面板进程是**前台**进程：后台运行与日志由调用方管理，它不会自动后台化、不写 PID 文件、也不注册系统服务。**起面板的命令必须以后台任务的形式常驻**——用 harness 的后台任务机制起，不要用 shell 的 `&`，更不要把前台占住：那样面板是那次 shell 调用的子进程，父 shell 一收尾（命令返回、会话切换）它就被带走。后台任务活在 harness 进程里，harness 一退面板就没了，重开前先确认没有旧面板还在跑（端口被占就是旧面板还活着）。

## 3. 精简 CLI 用法

设 `CLI="python3 skills/trainops/scripts/trainops.py -p /你的路径/my-experiment"`。

### 标准化 JSON 输入（agent 首选）

首选 `apply --from-json`，无需拼接冒号字段或预先计算任务编号：目录里还没有计划时按 `name/instances/flows/tasks` 创建，已有计划时按 `instances/flows/tasks/updates` 增量更新，不覆盖原计划、不改计划名。也支持 `--from-json /路径/输入.json`；`--dry-run` 只校验和预览，不创建目录/锁/计划文件、不改变 revision。显式 `init --from-json` / `batch --from-json` 仍然可用，分别只创建、只增量。

```bash
$CLI apply --from-json - --brief <<'JSON'
{
  "name": "基线实验",
  "instances": [{"key": "gpu", "name": "训练机", "autodl_id": "实例ID", "ssh_alias": "gpu-a"}],
  "flows": [{"key": "base", "name": "基线"}],
  "tasks": [
    {"key": "prepare", "flow": "base", "name": "准备数据", "instance": "local", "minutes": 10},
    {"key": "train", "flow": "base", "name": "训练", "instance": "gpu", "minutes": 120, "deps": ["prepare"]},
    {"key": "eval", "flow": "base", "name": "评估", "instance": "gpu", "minutes": null, "deps": ["train"]}
  ]
}
JSON

# 核实训练完成后一次修改状态、预计耗时及巡检记录
$CLI apply --from-json - --brief <<'JSON'
{"updates": [{"id": "F01-T02", "status": "done"}, {"id": "F01-T03", "minutes": 15, "checked": true}]}
JSON
```

- `apply` 顶层为 `name/instances/flows/tasks/updates`；无计划时 `name` 必填且不能包含非空 `updates`，已有计划时 `name` 可省略或与当前同名，写不同名会被拒绝。`init` 顶层为 `name/instances/flows/tasks`；`batch` 为 `instances/flows/tasks/updates`。
- `apply` 回执的 `created`、`revision`、`aliases` 都在顶层；batch 的别名映射在 `item.aliases`。
- `key` 仅本次请求有效，回执 `aliases` 返回系统编号映射，后续用真实编号。`apply` 是增量操作，重复提交新增项会再次创建；修改已有任务用 updates 和真实 ID。允许依赖后面的任务或跨流程任务；名称可以含冒号。**JSON 不自动串联**，新增省略 deps 或写 `[]` 都是无依赖，更新 `deps: []` 表示清空。
- 更新省略字段保持原值；`instance/minutes/progress/not_before` 可用 `null` 清空或表示待估。数字使用 JSON 数字，不使用 `?`/`-`/`none`。同任务每批最多一项更新；运行中保护、状态机与真实观测要求不变。
- 真实写入持锁读取最新账本、按最终拓扑校验、一次原子提交；硬错误不保存，成功只推进一次 revision，保留 config/experiments 和无关字段。
- 也允许 agent 直接编辑 `.agent/experiment.yaml`：先读最新文档并备份，保留三段结构、编号、Run 历史与无关字段，避开面板或其他并发写入，用临时文件原子替换，改完 `check --strict` 通过后再启动受影响任务。CLI 自带锁、原子提交和自动编号，通常更省事；不得借直接编辑绕过运行中改派保护。

完整字段、空值和前向引用规则见 [JSON 用法](skills/trainops/references/cli.md#json-建表与批量修改agent-首选)，直接编辑规范见[直接编辑](skills/trainops/references/cli.md#直接编辑计划文件)。少量手动修改仍可用以下简写。

### 简写一次创建完整计划（兼容）

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
$CLI set F01-T01 --progress 800/7500 --brief  # 日志迭代数自动换算百分比
# 等价写法：$CLI set F01-T01 --iter 800 --max-iter 7500 --brief
$CLI set F01-T01 --checked --brief  # 已真实巡检，但没有可靠百分比
$CLI set F01-T01 --status done --brief

# 修改计划（改实例、依赖、预计耗时）
$CLI set F02-T02 --instance I01 --deps F01-T02,F02-T01 --minutes 25 --brief

# 批量追加或批量更新
$CLI batch --task "F03:消融补充:I02:40" --set "F01-T02:done" --set "F02-T01:running:60" --brief

# 一次原子改多条依赖（右侧为空表示清空；整批只推进一个版本，按最终拓扑校验）
$CLI batch --dep "F03-T04=F02-T06" --dep "F03-T07=F03-T03" --brief
```

**简写任务写法**（只有该模式会推断自动串联）：`流程ID:任务名[:实例[:耗时[:依赖]]]`。中间字段不确定时用 `-` 占位跳过，例如只想登记“F01 上耗时 30 分钟的评估、依赖 F01-T01”：

```bash
$CLI batch --task "F01:评估:-:30:F01-T01"
```

耗时写 `?` 表示待估（文本摘要与面板均显示待估，不猜测完成时间）。依赖用逗号分隔，空字符串表示清空；`--instance -` 等价于 `none`（未分配实例；本地并行任务应显式设为 `local`）。init/batch 回执会列出自动推断的 `自动串联` 边；它们只表示同流程同实例的默认执行顺序，独立任务应在第五个字段显式写 `-`。

### 低上下文工作方式

`--brief` 为显式选项，不改变默认完整文本/JSON 或脚本退出码：show 只看当前行动，写命令只看版本、编号和更新结果。每组最多 3 条，长文本标 `…`，同时报告总数/省略数；历史任务仍保留在账本。完整问题看 check，完整任务看 show。

brief 新增或结构编辑会随回执附带最终结构检查，问题/提醒不会撤销已保存修改；纯状态/进度/巡检更新成功后不必机械再跑 check + show。脚本门禁仍用 check --strict。详细参数和批量语法见 [CLI 参考](skills/trainops/references/cli.md)，配置见 [AutoDL 参考](skills/trainops/references/autodl.md)，异常见 [恢复协议](skills/trainops/references/protocol.md)，按需读取即可，不必加载本 README。

## 4. 命令速查

设 `CLI="python3 skills/trainops/scripts/trainops.py -p /你的路径/my-experiment"`；加 `--version` 可查看当前 schema 版本。

| 命令 | 说明 |
|---|---|
| `apply --from-json <path|-> [--dry-run]` | agent 首选；无计划时创建、已有计划时增量更新；具名字段、临时别名及前向依赖，一次原子提交；dry-run 不落盘、不保留编号 |
| `init --from-json <path|-> [--dry-run]` / `batch --from-json <path|-> [--dry-run]` | 兼容入口：init 只创建，batch 只增量；字段与校验规则同 apply |
| `init --name ... [--instance ...] [--flow ...] [--task ...]` | 新建计划，支持一次传入全部实例、流程和任务（仅同流程同实例默认串联，可显式留空依赖以并行）；已有计划时拒绝覆盖 |
| `demo [--force]` | 在空目录生成演示计划，不涉及任何远程操作；`--force` 用于重置演示目录或已被改动的计划 |
| `check [--strict]` | 结构性检查：返回问题（阻断性）与提醒（如 pending 任务耗时待估、依赖间时间空隙过大）两组结果；`--strict` 在存在问题时不返回 0 退出码，便于脚本或 CI 拦截 |
| `show --brief` / `show` / `show --json` | 当前行动摘要（推荐 agent 使用）、完整文本或完整 JSON；brief 分组限量并注明省略数，与 --json 互斥。`未巡检` 指未记录 checked_at 或超过 1 小时，`依赖就绪` 不保证远端在线 |
| `set <任务ID> [--status ...] [--progress ...] [--instance ...] [--deps ...] [--minutes ...]` | 更新单个任务（`task set` 的简写） |
| `task add --flow F01 --name 名称 [--instance I01] [--minutes 30] [--deps ...] [--not-before ...]` | 新增单个任务；`flow add --name ...`、`instance add` / `instance set I01 --status on --observed` 同理 |
| `batch [--instance ...] [--flow ...] [--task ...] [--set ...] [--dep ...]` | 单次命令批量追加实例/流程/任务、更新进度/状态或依赖（`--set "F01-T01:done:100:备注"`、`--dep "F03-T04=F02-T06"`）；依赖整批一次提交、按最终拓扑校验 |
| `instance set I01 --disk "data:40/50,fs:5/200"` | 登记各盘用量（GB）；`--disk` 逐盘合并，`data:none` 清空该盘 |
| `instance set I01 --mode no-gpu`（或 `gpu` / `none`） | 登记开机模式：无卡准备（含持续请求等待 GPU）记 `no-gpu`，正式有卡开机记 `gpu`，核实不了记 `none`（未观测） |
| `serve --port 8765` | 启动本地任务执行队列 Web 面板（仅监听 `127.0.0.1`）；面板只读，展示执行队列与进度。它是前台进程，**要常驻就由调用方以后台任务起**（别用 shell 的 `&`：父 shell 一退进程就被带走），CLI 自己不后台化、不写 PID、不碰 systemd |

几个容易被忽略的约束：同一实例同时只允许一个 `running` 任务（`local` 不占 GPU，可并行）；依赖未完成的任务不能被标记为 `running`；实例上仍有任务运行时不能把该实例标为 `off` / `unreachable`。

### 执行、通知与取消

- 训练通过 SSH 在 tmux/screen 等断线后可恢复的会话中运行；代码先本地修改验证，按授权提交推送，远端拉取核对 commit 后再启动。
- 新训练启动前必须实际执行 [TrainPilot 开工探针](skills/trainops/references/trainpilot.md#调用与开工探针)并判读返回；Lazy MCP 通过 `call_mcp_tool(ServerName="trainpilot", ToolName="…", Arguments={…})` 调用，不能因没有独立原生工具而跳过。
- MCP `trainpilot` 负责即时通知与按钮决定，终态直接 `report` completed/failed；异常修复验证后用一次 ACK 合并汇报，不重复 milestone。`lark-cli` 实验日志记录 Run 与证据，`synced to Feishu` 只代表写表成功。巡检按顺序更新计划/Run、实际调用 TrainPilot、最后对话总结；写表失败仍继续即时通知。
- 用户选择等待时保持现场与任务状态，结束轮询，把控制交还直接对话；不接收 `next_task_plans`、不调用 `ack_plan`，也不要求 `#fix` 或从群消息拉取方案。
- 取消任务并关机：停止本任务进程、保全必要产物、清理本任务的资源等待，按授权关机并核实物理终态；有其他任务共享实例时先说明冲突，不误停其他训练。
- 完成/取消后清理本任务不再需要的定时巡检与唤醒，汇报结果位置、实例最终状态和未决事项；任务面板可以保留。

## 5. AutoDL 存储与 TensorBoard

本节仅适用于 AutoDL，开机准备由外部工具执行，存储由账本记录和告警。Vast.ai 使用外部 `vastai:vastai` skill，按 [Vast.ai 参考](skills/trainops/references/vastai.md) 搜索和管理实例、配置实际存储与 TensorBoard；不套用下述无卡模式、三盘路径和共享盘。**面板不展示磁盘与 TensorBoard 信息**——这些是 agent 巡检时要盯的事，写在 `show`／`check` 里更有用。

### 无卡准备 → 有卡训练

多实例筛选优先只读 `list_instances(instance_uuids=[...])`，每批 1–100 个完整 UUID；支持去重、白名单校验和逐项错误，共享一次分页遍历、硬件查询最多 5 路并发，无需 Agent 另编排并发。逐项核实，失败项保留待核实，不当作实例不存在或立即重查整批。单实例、批量工具不可用或操作后定点复核用 `instance_status(instance_uuid)` 核实状态、模式及硬件规格：`gpu_name`（如 RTX 3090）、`gpu_memory_gb`（如 24.0，单卡显存，按 1024³ 字节换算）、`cpu_cores`（如 14，实例分配的 vCPU 数）。规格来自控制台“规格详情”的配置快照，关机时也可查询、无需 SSH，可用于开机前筛选候选实例；缺失或无效的可选字段返回 `null`，保留待核实、不补 0。快照不是 GPU 已分配或可训练的证明，也不能覆盖无卡模式的实际资源上限；后台开机重试仍只查运行状态，不额外请求硬件规格。

无卡模式开机是指**本次开机使用 0.5 核 CPU、2GB 内存、无 GPU 的配置**，不是保留原有 CPU/内存规格只去掉 GPU。准备工作须适配该资源上限：优先阅读代码、调整配置、检查路径与少量数据；限制安装/编译和数据加载的并发与内存占用。重型编译、全量数据预处理或大模型加载等超出资源的工作移到授权且资源充足的环境，或留到有卡阶段补做；不因无卡阶段资源不足就判定环境损坏，也不为完成这些检查反复触发 OOM。

通过 `autodl_mcp` 对普通按量实例默认采用：**`power_on_no_gpu` 无卡开机 → 了解项目/环境、完成配置与代码调整、执行无需 GPU 的检查 → 保存并结束准备进程 → 按授权 `switch_mode(instance_uuid, target_mode="gpu")`（内部关机、确认关机、再开机，缺货后台持续重试）→ 验证 GPU 与训练入口 → 正式训练**。开机与切换均返回 `job_id`，优先用只读 `wait_for_job(job_id, timeout_seconds=15)` 观察，`job_status(job_id)` 保留用于即时进度；不存在独立的“持续开机”工具。默认进入 `waiting_capacity` 即返回，即使指定 120 秒也不会硬等缺货；及时汇报 `phase`、`elapsed_seconds`、`attempts`、`suggested_next_poll_seconds` 并推进其他工作，后台任务耗时不是训练进度或预计有货时间；`suggested_next_poll_seconds` 是建议下次拉取状态前等待的秒数（缺货/网络退避时已计入最近一次尝试间隔，约跨两次尝试窗口；`null` 表示无待定等待或任务已结束），不是资源可用倒计时。缺货返回后按该秒数用宿主定时调度器登记一次性唤醒（Antigravity CLI 为 `schedule`，`DurationSeconds` 取建议值、至少 1 秒；登记后继续推进其他工作，没有可推进工作时结束本轮等定时通知，到点再 `job_status` 复核），不要用 shell `sleep` 盲等，不紧密重复默认调用。宿主没有定时能力时才在授权范围内用 `wait_for_job(job_id, timeout_seconds=15, return_on_capacity=False)`。`timeout_seconds` 支持 0–300 秒（0 为立即查询），按授权剩余等待时间和工具调用超时上限缩短；终态立即返回，等待超时返回当前快照。等待超时、连接断开或取消等待请求不会取消后台任务，不应因此重发开机；长轮询不增加上游轮询或开机请求。

`switch_mode` 不是热切换或可回滚事务；反向转无卡用 `target_mode="non_gpu"`，均须确认中断任务已获授权，已有授权覆盖则不重复询问。活动期间不另提交同实例开机或切换；失败可能已关机，先查真实状态；关机完成后在 `waiting_capacity` 等 GPU 时取消，`cancel_job` 会直接返回 `resulting_status: "shutdown"`，不必再调 `instance_status`。取消只停止后续尝试，不撤销已接受的电源请求；返回的 `resulting_status`、`resulting_start_mode`、`resulting_observed_at` 是取消后即时快照，稳定态可直接采用，过渡态（如 `starting`、`shutting_down`）或查询失败（`resulting_status_error`）仍需复查。切换成功后仍核实模式与 GPU 可用性。工具不支持时才分步 `power_off` → 查询确认关机 → `power_on`；已关机实例直接按所需模式开机，不重复切换。止付仍用 `power_off`，会先取消活动切换、防止后续自动拉起，不用切成无卡代替关机。

这是收到任务后的默认路线，不是"有时间才做"：agent 应先起任务执行队列面板让进度可见，再走无卡准备，**不该在没准备的情况下直接申请有卡开机让 GPU 空等**；只有用户明确要求持续请求，或已核实无需远端准备，才直接 `power_on`。无卡准备完成后为切换模式而关机属于准备流程，不是结束实验，仍须确认准备进程退出、配置与产物落盘并取得授权。

开关机及持续请求按用户授权执行；已有训练不为此重启。切换时的关机保留实例与后续计划，不是释放实例；持续请求已提交或 SSH 可达都不代表 GPU 已就绪，未就绪不得把训练标为 running。账本用 `--mode` 区分两种开机模式：无卡准备/持续请求等待记 `no-gpu`，正式有卡开机记 `gpu`，核实不了记 `none`（未观测）；切模式后按实际状态重新登记。工具参数以当前 MCP 说明为准，TrainOps 不实现开关机或自动抢卡。详见 [开机模式](skills/trainops/references/autodl.md#无卡准备与训练开机)。

### 多实例效率与资源占用

以尽快交付有效结果、减少 GPU 空转为目标。你提供的多台实例是候选资源池，agent 按已就绪任务、GPU/显存、环境、可用性及预计耗时调度；同一任务不同时向多台抢卡，不中断正常训练来追逐空闲 GPU。全部缺卡时复用已有开机 job、按退避唤醒，继续本地或适量无卡准备，无事可做就等待通知。

共享数据只下载/预处理一次，校验版本与完整性后只读复用；并发输出按任务/Run 隔离，避免读取半成品或并发改写共享环境。无卡实例低配，只为实际准备工作开机，重型工作使用授权且资源充足的环境。训练结束立即检查全局可接续任务，不等半小时汇报；没有可立即运行的任务时按已有授权保全后关机等待，确有轻量准备才切无卡。未来仍有任务不意味着 GPU 要一直开着，停机保留实例但不保证再次拿到 GPU；缺关机授权及时请求决策，不影响其他训练、不擅自释放实例。详见[按需用卡与空闲处置](skills/trainops/references/autodl.md#按需用卡与空闲处置)。

### 训练故障：自愈 5 次后先关机止付

训练或准备出问题（进程崩溃、OOM、依赖/数据缺失、SSH 断、盘满、GPU 不可用、checkpoint 损坏）时走这条固定路线：

1. **先报告**：说清现象与已核实的证据（进程、日志、`nvidia-smi`、盘用量、实例状态）以及影响哪个任务，不静默重试、不伪造进度。
2. **自愈**：定位原因后对症处理——调参数、补依赖、清盘、重下数据、重启训练进程；科学对照实验保留变量控制，工程修复可按根因成组调整相关配置，每次尝试后核实真实结果（进程在跑、GPU 在用、checkpoint 在写）。避免没有新证据的重复，瞬时故障可合理退避重试。**最多 5 次：5 次是上限不是配额，修好就继续训练。**
3. **第 5 次仍无法继续 → 先关机止付**：结束训练/准备进程、确认必要产物与日志已落盘，`power_off` 后用 `instance_status` 确认真已关机；任务按真实结果记 `failed`（在等你拍板时记 `blocked`），不标 done、不留 running。

这一步关机是你对「5 次自愈失败」给出的**预授权中间关机**，agent 不必再等一轮确认——它止的是资源空转，代价只是保留实例与后续任务，重新开机仍走原来的授权与无卡准备流程。已经明确无法继续的情形（实例不健康、长时间缺货、数据缺失、验收口径变化、需要额外资源授权）不必凑满 5 次，可直接止付关机并说明理由。**其它关口不变**：最终关机、释放实例、超出约定的持续请求等待仍须先获授权。细则见 [异常与恢复协议](skills/trainops/references/protocol.md#故障自愈与止付关机)。

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

## 6. 测试

```bash
python3 -m unittest discover -s tests -v   # 计划账本 / CLI / 并发写 / 演示快照一致性与插件清单
node tests/test_web.cjs                    # 面板渲染：实例执行队列/虚线排队箭头/运行任务实例色、服务端进度直传、滚动保持、逾期提示、XSS 转义
```

测试进程不碰网络。装了 `claude` 时，清单测试会用 `claude plugin validate --strict` 校验 Claude Code 插件与市场，并用临时 `HOME` / `CLAUDE_CONFIG_DIR` 安装验证；装了支持插件的 `codex` 时，会用临时 `HOME` / `CODEX_HOME` 从本地市场安装并核对技能和 CLI；装了 `agy` 时，会调 `agy plugin validate --strict` 并用临时 `HOME` 安装、核对技能与规则。缺少对应客户端时跳过，不碰开发机的插件配置。

## 7. 改这个仓库

这一节给维护者：怎么跑、数据怎么流、哪些约束不能破。用它的 agent 不需要读。

### 硬约束

- **零第三方依赖**：只用 Python 标准库；测试用 Node 内置模块（没有 `package.json`，不装 npm 包）。
- **计划写入口**：推荐 CLI（`apply --from-json` 等，统一走 `Store.change()`）。项目文件 `$PROJECT/.agent/experiment.yaml` 的 `plan` 段是唯一事实来源；agent 也可先读后写直接编辑，但必须保留三段结构、编号与无关字段，避开并发写入并原子替换，改完跑 `check --strict`，不得借编辑绕过运行中保护。面板只读，不提供计划写入口。
- **缺数据就缺结论**：算不出的数字（如缺预计耗时、缺完成时间）一律不显示，绝不用 0 顶替或自行估算。这条口径同时约束 CLI 文本、`/api/plan` 与面板三处。

### 常用命令

```bash
python3 -m unittest discover -s tests -v      # Python 测试（约 30 秒）
node tests/test_web.cjs                       # 面板渲染测试（无 npm 依赖）

# 跑单个模块 / 单个类
python3 -m unittest tests.test_trainops -v
python3 -m unittest tests.test_plugin -v      # 三端清单与隔离安装（按已安装客户端验证）

# 演示面板（自带演示计划，不连服务器）
python3 skills/trainops/scripts/trainops.py -p examples/demo-project serve --port 8765
```

CLI 的 `-p/--project` 指向**实验项目目录**，不是本仓库（`--version` 打当前 schema 版本）。容易踩的坑：

- 改了 `demo_plan()` 就要重新生成仓库里的演示快照，否则 `RepoSnapshotTests` 失败：
  `python3 skills/trainops/scripts/trainops.py -p examples/demo-project demo --force`

### 架构

数据流是单向的：`.agent/experiment.yaml` 的 `plan` 段 → CLI（`Store` → `forecast`）→ `show` 文本或 `/api/plan` JSON → 面板只渲染。`config` 与 `experiments` 两段由 `ExperimentLog` 维护，不进入面板快照。

`skills/trainops/scripts/` 按职责拆分，入口仍是 `trainops.py`（只做模块组装、计划写状态机与命令分派）：

| 模块 | 职责 |
|---|---|
| `trainops.py` | 入口与兼容层：计划状态机（`set_task`）、`demo_plan`、命令分派、`main`，并重导出其余模块的公开函数 |
| `trainops_base.py` | 常量、`PlanError`、时钟与通用校验 |
| `trainops_model.py` | 计划文档构造、`validate()`、实例/流程/任务操作 |
| `trainops_store.py` | `Store`、文件锁、原子写与整份文档校验 |
| `trainops_graph.py` | `forecast()`、`execution_queues()`、`flow_views()`、`snapshot()`、`check_structure()` |
| `trainops_web.py` | 本机面板 `Handler` 与只读快照服务 |
| `trainops_experiments.py` | 实验配置、Run 历史与飞书同步 |
| `trainops_json.py` | 严格结构化输入、请求内别名与两阶段批量应用（不直接读写账本） |
| `trainops_specs.py` / `trainops_cli.py` | CLI 简写解析与 argparse 命令树 |
| `trainops_brief.py` | CLI 的 brief 文本输出 |

- `Store`：唯一读写入口。`.agent/experiment.yaml` 是 JSON 兼容 YAML 的单个文档，含 `config` / `plan` / `experiments` 三段；`fcntl.flock` 排他锁 + 临时文件 `os.replace` + 文件与目录 fsync。计划读改写走 `store.change(operation)`，实验配置与 Run 历史走 `store.update(operation)`——后者只改自己那两段，不推进计划 `revision`。
- `validate()`：读取与写入两侧都跑，账本不变量集中在这里——编号与计数器一致性、`TRANSITIONS` 状态机、依赖闭环（Kahn）、`done` ⇒ 进度 100、同一 GPU 实例同时只能有一个 `running`。校验失败只报错，**从不静默修复**；schema 版本不符直接拒绝读（开发阶段无迁移，提示"备份并核对文件"）。
- `forecast()`：排程的唯一事实来源。每个 GPU 实例一条车道，`estimate.start/finish` 由依赖 + 车道 + `not_before` 推出；`running` 任务靠 `progress_at` 采样外推，进度 100% 或已超期即降级为"待估"。
- `execution_queues()`：复用排程遍历顺序，按实例输出候选执行队列；本地、未分配、已结束及需处理任务另行分组，不生成或改写 `deps`。
- `flow_views()`：生成流程完成计数、总进度、完成时间与是否可估计的结论。总进度是**已完成 ÷ 未取消任务**，全取消时为 null。
- `snapshot()`：`/api/plan` 与 `show --json` 共用派生快照，包含 `queues`、`flows[].view` 和 `tasks[].view.progress`，不再返回依赖图 `graph` 或布局几何。任务显示进度通过 `node_progress()` 统一取整（done 为 100、未知为 0），不覆盖账本原始 `tasks[].progress`，不改变持久化 schema。
- `Handler` 只读：只提供页面与 `/api/plan` 快照，其他路径返回 404，写动词未实现并返回 501。Host 白名单只放 127.0.0.1/localhost（防 DNS rebinding），保留禁止缓存、内容类型保护及 CSP 响应头。

实验日志另由 `ExperimentLog` / `experiment` 命令维护：同一个 `.agent/experiment.yaml` 里，`config` 段保存非敏感配置，`experiments` 段保存 Run 和同步状态（文件是 JSON 兼容 YAML 1.2，不支持任意 YAML）。项目路径复用 `Store.root`，与计划共用一把文件锁与同一套原子写；飞书通过参数数组调用 `lark-cli`，网络期间不持锁。同项目同步串行；按 Run ID 精确查询去重、内容指纹去重写、版本差异保留补录。普通计划命令不触发飞书。首次建表前落结果不确定标记，成功后立即保存目标；出错不另建表。测试使用 mock/临时假 CLI，不访问真实授权或飞书。

`skills/trainops/web/index.html`（单文件面板，无构建步骤）：

- **轻量静态布局**：只用原生 HTML/CSS/JS，不引外部字体/图片/图标库/JS 库，也不用 canvas 元素，不做持续动画。改完必须跑 `node tests/test_web.cjs`。
- **面板不做业务计算**：队列顺序、流程总进度、任务显示进度与完成计数由服务端 `/api/plan` 统一下发。前端按 `queues` 分组展示任务卡，不重排依赖、不推算 ETA。`when()` 仅负责格式化。
- **只有执行队列**：不保留依赖图或视图切换；每个任务只展示一次，卡片只放任务自己的编号、名称、实例、流程、子进度与预计时间（以及可选备注、排队原因），不嵌套子任务或外部引用列表。队列是原生滚动容器，自动刷新后保留滚动位置。
- **同一个数只画一处**：流程完成度只在队列上方以文字给出；任务子进度只在对应卡片显示细进度条与百分比，不另造第二套统计。
- **视觉口径是一张纸**：中性色纸面、白色节点、细横线分隔，不使用渐变、图例、药丸标签或装饰性卡片。颜色只留给状态与结论：状态点、任务节点左侧细线与错误提示；执行中节点的左侧细线改用所属实例的身份色（调色板避开状态色，尤其不占用已完成的绿色），实例行左侧用同一色条对应，不参与业务状态判断。其余交互一律墨色。沿用深浅主题。完整任务名称自然换行，不做省略或固定高度裁剪。
- **连线不测量 DOM**：任务卡使用原生横向 flex 排列，相邻的同实例候选任务之间插入固定宽度的虚线箭头；非串行分组不连线。不加入绝对定位布局、尺寸测量、布局观察器、碰撞检测或迭代避障。
- DOM 一律 `createElement` + `textContent`：项目文件里的文本是不可信输入（`test_web.cjs` 有 XSS 用例）。
- 每 10 秒轮询 `/api/plan`，`document.hidden` 时跳过；重绘必须保留队列的滚动位置。

### 文档也是产品

`SKILL.md` 是热插拔入口（外部 agent 读的那份）、`rules/trainops.md` 是 Antigravity 的规则入口、`references/protocol.md` 是异常与恢复的按需参考、`README.md`（本文件）面向用户。CLI 行为变了，这几处要同步。

外部 agent 反复踩过的默认路线，改入口时别丢（`test_plugin.SkillTests.test_entry_keeps_panel_resident_and_no_gpu_default` 等用例盯着）：

- **收到任务就要起面板**：`serve --port 8765`；必须**以后台任务常驻起**——用 harness 的后台任务机制，别用 shell 的 `&` 或前台，父 shell 一退面板就被带走。
- **先明确资源来源**：用户选择 AutoDL 实例或在 Vast.ai 寻找，未明确先询问；Vast.ai 加载外部 `vastai:vastai` skill，搜索不等于租用授权，不自动跨平台。
- **AutoDL 无卡准备是默认**：先 `power_on_no_gpu` 准备再申请有卡，只有用户明确要求持续请求或已核实无需准备才直接 `power_on`；Vast.ai 按自身资源流程准备和启动。
- **故障自愈有上限**：先报告再自愈，最多 5 次，仍无法继续即先关机止付（5 次自愈失败本身有预授权；TrainPilot 的“取消任务并关机”按钮也授权对应专属实例关机。见 [协议](skills/trainops/references/protocol.md#故障自愈与止付关机)）。
- **计划首选标准化 JSON**：`apply --from-json`，也允许按规范直接编辑并校验；不要为“只走 CLI”或上下文长度牺牲已有规则。

入口与规则保持简短、只做指针；细则留在 `SKILL.md` 与 `references/`，同一份约束不要在多处分叉。SKILL 以讲清合同为准，不设字符硬门槛。

### 插件打包

一份技能，三种宿主清单；Claude Code 与 Codex 各提供本地市场入口：

| 文件 | 作用 |
|---|---|
| `plugin.json` | 插件清单：`$schema` 指向 Antigravity 的 schema，`name` 决定装到 `~/.gemini/config/plugins/<name>/`，`version` 是插件版本 |
| `.claude-plugin/plugin.json` | Claude Code 清单，技能路径相对插件根 |
| `.claude-plugin/marketplace.json` | Claude Code 本地市场，`trainops` 指向仓库根 |
| `.codex-plugin/plugin.json` | Codex 清单，复用相同技能目录 |
| `.agents/plugins/marketplace.json` | Codex 本地市场，`trainops` 指向仓库根 |
| `rules/trainops.md` | Antigravity 规则入口（≤40 行）：只做指针 + 关键不变量，细则留在 `SKILL.md` 与 `references/` |
| `skills/trainops/` | 技能本体；不要移动这一层——脚本靠 `parents[3]` 回推插件根 |

三条不能破的规矩：

- **不要移动 `skills/trainops/`**：脚本靠 `parents[3]` 回推插件根，`references/` 与 `web/index.html` 也按自身位置找（`test_plugin` 有深度守卫）。入口文件里不写绝对路径。
- **入口文件只做指针**（≤40 行）：细则一律留在 `SKILL.md` 与 `references/`，同一份约束写两处必然漂移。
- **三端共用一份技能**：清单中的名称、版本、说明保持一致，技能路径统一指向插件根的 `skills/`；不要在清单目录里复制技能或脚本。宿主差异集中在 `references/hosts.md`，不新增会覆盖实验项目角色的根 `AGENTS.md` / `CLAUDE.md`。

### 约定

- 全仓库中文：文档、CLI 输出、`PlanError` 文案（写成可直接执行的建议）、测试断言消息、commit message（中文摘要 + 按子系统分条的 `-` 列表）。
- 失败必须原子：被拒绝的编辑不留下半个文件（有测试逐字节比对项目文件）。
- 计划表只记核心字段（状态、进度、实例、依赖、耗时）；命令、日志路径、commit 一律不进计划。
- 新增测试沿用既有约定：不碰网络也不碰开发机上的凭据；需要 `agy` 的用例用临时 `HOME`。


