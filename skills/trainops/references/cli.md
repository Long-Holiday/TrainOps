# CLI 用法

仅首次建表、批量编辑或查参数时读取；日常遵循 [入口](../SKILL.md)。设 `CLI="python3 <技能目录>/scripts/trainops.py -p <实验项目目录>"`。

实验记录与飞书：`experiment init/show/start/update/sync`，目标表、Run 字段、JSON 参数及补录见[实验日志](experiments.md)。普通 `set/batch` 不隐式同步飞书。

## 调度决策

用户交办的是目标与约束，不要求照抄任务描述或机械按列举顺序执行。agent 应持续判断更好的推进方式；以下调整在既有授权内可自主决定，无需逐项请示：

- 先识别验收标准、必须完成的实验、预算/资源/截止时间及用户明确指定的先后顺序；这些是硬约束。其余顺序按真实数据依赖、关键路径、资源可用性和已观测耗时优化，不把描述顺序自动当成业务依赖。
- 优先做能及早暴露环境、数据或方案问题的低成本验证；独立任务在已授权实例间并行，GPU 等待期间推进无需 GPU 的准备或分析，避免为了守原顺序付费空等。实例间共用的数据集与中间产物走共享的文件存储（`/root/autodl-fs` 对所有实例可见），不必重复上传或实例间互传，但共享盘上要按实例/任务分目录，别让并发写入互相覆盖。不擅自新增付费实例，也不把远端无卡任务冒充 `local`（本地任务）。
- 根据实验结果重排后续工作、拆分过大任务、合并不影响验收的重复步骤；已有可信产物可核实后复用。对失效或低收益任务说明判断与替代方案；放弃用户明确要求的实验、改变验收、扩大费用授权或中断运行任务须先确认，不以“优化”为由静默缩减范围。
- `show --brief` 只是限量候选，不是必须执行的队首命令；有省略或需比较其他候选时再用 `show` 查看完整任务。缺耗时/费率时可以依据已知依赖和资源决策，但不编造最优时长、节约金额或进度。
- 执行调整前用 CLI 同步实例、耗时与真实依赖；保留编号和历史。JSON 批量新增不隐含串联；仅简写模式在同流程同实例内默认串联，独立任务可显式留空依赖；不要为“优先级”伪造依赖、删除真实依赖或清除用户指定的最早开始限制。CLI 无法表达的调整应说明限制，不直接改 JSON 或虚构参数。
- 结构调整后检查最终问题并修正，再启动受影响任务；不要因为 CLI 已保存就视为排程可执行。运行中任务改实例/依赖/开始限制仍遵守 [恢复协议](protocol.md#状态与依赖)。被合并或取消的任务不冒充 done，受影响下游依赖须按真实产物关系核对；改变方案的理由在对话简述，不往计划塞调度长文。

## 单 agent 多机再平衡

由一个 agent 统一管理多台机器，不引入多 agent 抢任务、认领租约或常驻调度器。任务完成/失败、实例变为可用及例行巡检时执行下面的闭环；agent 未运行时不再平衡。现有 `set --instance` 只改变计划分配，不会修改远端队列或启动、停止训练。

1. **先收尾与看全局**：核实远端进程、产物，按真实结果更新任务及实验 Run；失败先按恢复协议处理，不能把失联视为已退出。读取 `show --brief` 检查全局待执行任务，有省略或需比较其他节点任务时用 `show` 看全量；不能因空闲实例自身队列为空就认定没有工作。
2. **筛选可搬任务**：只选尚未启动的 `pending`，依赖须已完成、最早开始限制须满足；不自动重试 failed 或解除 blocked。尊重用户指定节点、GPU 型号/显存、预算及资源授权，核实目标实例实际空闲、GPU 就绪、环境及数据/checkpoint 兼容。共享盘可见不等于本地数据或环境已就绪；为搬任务新增费用或中断已有任务仍须先确认。没有合格候选就保留待执行，不为占满 GPU 乱搬。
3. **排除远端抢先启动**：计划为 pending 不代表远端尚未启动。检查原机器的进程、tmux/screen、批量脚本或自动队列；若已排队，先安全暂停其待执行调度或撤下该项，保留其余任务，并确认该项未启动且不会再被旧队列拉起。不改写正在被队列执行的脚本来赌生效；不能安全撤下或无法确认时不改派，记录原因；已经启动则按 running 恢复，不能当作普通改派或擅停训练。
4. **同步计划并检查**：用现有命令改派，保留编号、真实依赖和历史；回执只代表已保存，处理结构检查中的问题后才启动。改派不会自动解除依赖：简写批量建表默认串联的边，只有核实仅表示原同机执行顺序、没有数据依赖也不是用户指定顺序时，才用 `--deps` 调整（多处调整用 `batch --dep` 一次提交，按最终拓扑校验）；保留真实依赖，不直接清空整条依赖链。
5. **按最新计划实际执行**：每次启动前重新读取最新计划，核对任务仍为 pending、实例分配、依赖和开始限制，并再次确认目标机器无冲突及两端没有该任务进程；不沿用开工时清单，也不让远端批量队列按旧分配继续启动。条件变化则重新决策，不执行过期安排。由同一个 agent 在目标机通过 SSH 启动，核实进程与 GPU 实际运行后再 `set --status running`、`experiment start`，使新 Run 对应实际执行实例；不能提前标 running 占位，不能覆盖旧 Run 的实例历史。启动失败或结果不确定先核实并走恢复协议，不盲目在另一台重试。

例如：I02 完成 v7、v8 后，从 I01 的待执行消融中选出已核实可搬的 F01-T05，撤下原队列项、确认两端状态及目标环境后：

```bash
$CLI set F01-T05 --instance I02 --brief
```

处理回执问题并重新读计划后，agent 才在 I02 实际启动训练、核实成功并登记状态与 Run。向用户简述改派原因、原/目标实例及实际启动结果；只改派尚未启动时明确说“已改派，未启动”。不添加 `reassign` / `claim` 命令，不把 SSH 命令、队列脚本或日志路径塞进计划。

## JSON 建表与批量修改（agent 首选）

直接用 stdin 提交，不需要人工编辑输入文件，更不能直接 Edit/Write `.agent/experiment.yaml`。也支持 `--from-json /路径/计划.json`，它是输入文档，不是账本或凭据配置。JSON 不与 `--name/--instance/--flow/--task/--set/--dep` 混用。

```bash
$CLI init --from-json - <<'JSON'
{
  "name": "基线实验",
  "instances": [{"key": "gpu", "name": "训练机", "autodl_id": "实例ID", "ssh_alias": "gpu-a"}],
  "flows": [{"key": "base", "name": "基线"}],
  "tasks": [
    {"key": "prepare", "flow": "base", "name": "准备数据", "instance": "local", "minutes": 10},
    {"key": "train", "flow": "base", "name": "训练", "instance": "gpu", "minutes": 120, "deps": ["prepare"]},
    {"key": "eval", "flow": "base", "name": "评估:完整指标", "instance": "gpu", "minutes": null, "deps": ["train"]}
  ]
}
JSON

# 已核实训练完成后，批量更新、改派并追加任务；只推进一个 revision
$CLI batch --from-json - --brief <<'JSON'
{
  "tasks": [{"key": "report", "flow": "F01", "name": "整理报告", "instance": "local", "minutes": 10, "deps": ["F01-T03"]}],
  "updates": [
    {"id": "F01-T02", "status": "done"},
    {"id": "F01-T03", "minutes": 15, "checked": true},
    {"id": "report", "note": "核对评估产物后执行"}
  ]
}
JSON
```

### JSON 字段与引用

| 部分 | 字段 |
|---|---|
| init 顶层 | `name` 必填；`instances`、`flows`、`tasks` 为可省略的数组 |
| batch 顶层 | `instances`、`flows`、`tasks`、`updates` 为可省略的数组，至少一项新增/更新；不修改计划名 |
| instances 新增项 | `name`、`autodl_id`、`ssh_alias` 必填；`key` 可选 |
| flows 新增项 | `name` 必填；`key` 可选 |
| tasks 新增项 | `flow`、`name` 必填；`key`、`instance`、`minutes`、`deps`、`not_before` 可选 |
| updates 项 | `id` 必填，另至少一项有效修改：`name`、`status`、`progress`、`instance`、`minutes`、`deps`、`not_before`、`note`、`checked` |

- `key` 是**本次请求内**的临时别名，所有种类之间也不能重复；不能用 `local` 或系统编号格式（如 `I01`、`F01`、`F01-T01`）。系统仍生成固定编号，不保存 key。默认 JSON 回执的 `aliases.instances/flows/tasks` 返回映射；batch 的映射在 `item.aliases`。后续请求使用真实编号，不重用上次 key。
- `flow`、`instance`、`deps` 和更新目标 `id` 可引用对应种类的本次 key 或系统编号；任务依赖允许前向引用、跨流程引用，数组不必按依赖排序。`instance: "local"` 为本地，`null` 为未分配。
- **JSON 不自动串联**：新增任务省略 `deps` 或写 `[]` 都表示无依赖；更新省略字段保持原值，`deps: []` 清空依赖。重复依赖、未知引用、错误种类和依赖环都拒绝，真实依赖不猜。
- `minutes` 是预计分钟数，`null` 为待估，数字 0 是已知零耗时；`progress` 为 0–100 或 `null`；`instance/minutes/progress/not_before` 可用 `null` 清空。`not_before` 非空值必须带时区，`note: ""` 清空备注。JSON 不使用 `?`、`-`、`none` 占位；数字不能写成字符串，布尔值不能当数字。
- `updates` 只修改任务；实例观测、流程改名仍用对应单项命令，时价与 Token 仍只在面板登记。未知字段、重复 JSON 对象字段、NaN/Infinity 及非法类型会报错，不静默忽略。
- 新增全部登记后解析引用，**结构字段先于状态更新**，最后按整批最终拓扑校验；同批状态时间一致，父任务完成与子任务开始可以同批提交，但必须先有真实观测。一个任务每批最多出现一次更新（即使用不同别名/编号也不能重复），不在同批串联停止、改派、重新启动等操作。
- 状态转换和运行中保护与 `set` 一致：pending/blocked → done 会补 running 转换，done 进度固定 100；running 任务不能改实例/依赖/开始限制，即使本项同时写 blocked 也不行。`checked: true` 记录真实巡检；修改 progress 或设 running/done 也记录巡检，纯改名/备注/结构修改不自动算巡检。
- 整批硬错误不保存、revision 不变；成功 batch 只推进一个版本，并保留实验配置、Run 历史和面板费率。不自动重写其他依赖，不下发训练。

### 只校验不保存

对同一条 init/batch 增加 `--dry-run` 即可（简写模式同样支持），例如：

```bash
$CLI init --from-json /路径/计划.json --dry-run --brief
$CLI batch --from-json /路径/修改.json --dry-run
```

预览执行同样的硬校验和结构检查，**不创建目录、锁或计划文件，不修改账本字节或 revision**；JSON 返回 `dry_run: true`、当前 `revision`、预计提交的 `proposed_revision` 与编号映射，brief 明示「未保存」。编号仅供预览、不保留，别的写入后可能变化。init 预览也拒绝覆盖已有计划。issues/warnings 沿用原结构诊断语义，不自动升级为硬错误；真实提交需要去掉 `--dry-run`，按最新账本重新校验。

## 简写建表（兼容快捷方式）

```bash
$CLI init --name "双实例对照实验" \
  --instance "基线GPU:autodl-id-a:gpu-a" --instance "对照GPU:autodl-id-b:gpu-b" \
  --flow "基线流程" --flow "对照流程" --flow "结果汇总" \
  --task "F01:基线训练:I01:120" \
  --task "F01:基线评估:I01:15" \
  --task "F02:对照训练:I02:90" \
  --task "F02:对照评估:I02:15" \
  --task "F03:对比报告:local:20:F01-T02,F02-T02" --brief
```

- 实例写法 `名称:autodl_id:ssh_alias`，编号依次为 I01、I02；流程依次为 F01、F02。
- 简写任务写法 `流程ID:任务名[:实例[:分钟数[:依赖]]]`。仅简写 init / batch 省略依赖字段时，自动依赖同流程、同已分配实例的最近任务（含 local）；不同实例、不同流程或未分配实例不自动连线。显式依赖列表优先，跨实例/跨流程的真实依赖需显式列出。
- 第五个依赖字段显式留空、填 `-` 或 `none` 表示独立任务，不做推断，例如 `--task "F01:独立训练:I02:?:-"`（尾随 `:` 也表示显式空）；单项 `task add` 不自动推断依赖。Flow 可有多个独立根节点及分支/汇合，只校验真实依赖的合法性，不要求串成一条链。init/batch 回执会列出 `自动串联` 的边；它们只表示同流程同实例的默认顺序，独立任务应显式写 `-`，不要先接受默认边再逐条清理。
- 中间字段用 `-` 跳过；耗时 `?` 表示待估，不能据此得出可靠 ETA/费用。依赖逗号分隔。不要在名称中使用分隔符 `:`，复杂名称用单项 `task add --name`。
- init 拒绝覆盖已有计划；没有计划时先等用户具体实验任务，不为“加载能力”建空账本。

## 简写更新与追加

```bash
$CLI set F02-T02 --instance I01 --deps F01-T02,F02-T01 --minutes 25 --brief
$CLI batch --task "F03:补充实验:I02:45" --set "F01-T01:done" --set "F02-T01:running:55" --brief
$CLI batch --task "F01:评估:-:30:F01-T01" --brief
$CLI batch --dep "F03-T04=F02-T06" --dep "F03-T07=F03-T03" --brief
$CLI task add --flow F01 --name "后处理" --instance local --minutes 10 --deps F01-T02 --brief
$CLI instance add --name "新增GPU" --autodl-id id-c --ssh-alias gpu-c --brief
$CLI instance set I01 --status on --mode no-gpu --observed --brief
```

- `set` 是 `task set` 简写，二者均支持 --brief；`flow add/set`、`instance add/set`、`wallet set` 同样支持。
- 批量更新写法 `任务ID:状态[:进度[:备注]]`；批量状态更新会登记巡检时间，须先真实观测，不把它当作自动巡检。
- 批量改依赖用 `--dep "任务ID=依赖列表"`，可重复；右侧为空、`none` 或 `-` 表示清空。整批只推进一个 revision，按最终状态校验（逐条编辑会撞上的中间环在这里不存在），只改点名的任务，不碰未点名的真实依赖；运行中任务仍拒绝改依赖。与 `--task`/`--set` 同批时按“新增任务 → 状态更新 → 依赖更新”执行。
- 单项任务更新的 `--deps ""` 清空依赖，`--instance none`（或 `-`）清空实例，`--minutes ?` 清空耗时，`--not-before none` 清空开始限制；最早开始时间必须是带时区的 ISO 时间。实例的 `--mode none` 表示开机模式未观测，不等于"有卡"。
- `local` 不占 GPU，可并行；`none` 表示未分配实例，不等同于本地。
- 重命名用 `flow set F01 --name ...` / `set F01-T01 --name ...`；编号保持不变。运行任务不能直接改实例/依赖/开始限制，恢复规则见 [协议](protocol.md#状态与依赖)。

## 输出与检查

- `show --brief`：当前运行、依赖就绪候选、待处置/待估、实例和问题/提醒。每组最多 3 条，长文本折成单行并标 `…`；总数和省略数在截断前计算。GPU 每实例仅列一个候选，local 不去重；依赖就绪不保证实例在线，执行前核实远端。
- `show`：完整文本计划，包含历史任务。`show --json`：完整机器快照；与 --brief 互斥。
- 写命令无 --brief 时保持原 JSON 回执；有 --brief 时输出版本、编号和本次更新。新增编号超限也会注明省略，可用 show 查询，不要重跑写命令取编号。
- brief 新建/结构编辑自动检查最终状态：JSON init、JSON batch 含新增或结构字段，以及简写 init、新增实例/流程/任务、batch 含新增项或 `--dep`，或 set 改实例/耗时/依赖/开始限制（含清空）。诊断对应同次保存，只增一个版本，不额外读盘；不写进账本。
- 纯状态/进度/巡检/备注/改名、实例观测/开机模式/磁盘、钱包及结算不自动做全计划结构检查；需要全局诊断时用 show --brief 或 check。
- 时价不在 CLI：`instance set I01 --mode no-gpu` 只登记"这台现在无卡"，费率由用户在面板（`serve`）的实例行上登记——无卡登记无卡费率、有卡登记 GPU 时价，两者互不覆盖。缺费率时少给结论，不补 0。
- Token 同样不在 CLI（没有 `--token`，也不读环境变量或文件）：用户在面板表头登记，只存在面板进程内存里，口径见 [配置](autodl.md#余额与定时结算)。余额只能由面板进程结算，或人工用 `wallet set` 登记。
- `serve` 本身是前台进程：常驻由起它的调用方以后台任务负责（别用 shell 的 `&`，父 shell 一退进程就没了），面板不自己后台化、不写 PID、不碰 systemd。口径与生命周期见 [面板](autodl.md#面板)。
- `check` 把必须修复的 issues 与提醒 warnings 分组；默认退出 0。`check --strict` 仅在 issues 非空时退出 1，warnings 不影响 ok。pending 任务的未知耗时（`?`）仅为 warning；其他非终态未知耗时、非终态未分配实例及空流程仍是 issue。brief 编辑发现问题仍是“已保存”，不撤销编辑、不改变成功退出码。硬校验拒绝的编辑不落盘。
- `--version` 查询 schema；版本不符时先备份并核对文件，开发阶段不自动迁移。
