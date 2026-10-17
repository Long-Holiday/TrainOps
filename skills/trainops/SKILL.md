---
name: trainops
description: AutoDL 多实例训练计划、任务流程图、结算与飞书实验日志；按目标自主优化排程。
---

# TrainOps

保留原角色；仅加载时不建表、不启动进程，交办任务后执行下列流程。设 `CLI="python3 <本目录>/scripts/trainops.py -p <实验项目目录>"`。

## 开工顺序

1. **计划**：`show --brief`；已有计划先核实、不覆盖；无计划按目标、验收与授权范围创建，耗时待估、真实依赖不猜。用法见 [CLI](references/cli.md)。
2. **面板**：随即以 harness 的后台常驻任务起 `$CLI serve --port 8765 --interval 30`（别用 `&` 或前台），报出地址。端口占用先核实项目；非本项目则换端口，不杀其他面板。结算由该进程负责，不另起 monitor。
3. **日志初始化**：主动执行 `$CLI experiment init`，按[实验日志](references/experiments.md)复用指定/已有表，未指定则自动创建“<项目名> · 实验日志”Base 和“Run 记录”首表。检查并报告真实目标和字段核验结果，不能只描述步骤或等用户提醒；失败保留待补状态，不阻断后续工作。
4. **无卡准备**：默认 `power_on_no_gpu`，仅 **0.5 核 CPU、2GB 内存、无 GPU**；做轻量项目理解、配置与代码调整，限制并发/内存，重型编译、数据处理和模型加载移到授权且资源充足的环境或有卡阶段，不把资源不足误判为环境故障。准备就绪后按授权优先 `switch_mode` 转有卡（内部先关机并确认），不支持时分步关机再开机；用户要求持续请求或已核实无需准备才直接 `power_on`。不交付空等 GPU，不支持无卡则说明限制。细则见 [AutoDL](references/autodl.md)。
5. **训练与记录**：核实 GPU 就绪后训练；**先读后写**：写入实验日志前先 `experiment show`，只回顾最近的相关 Run（同任务最近一条、同流程/基线最近一次，通常 1–3 条，不必全量）与 pending，确认续跑、重跑还是新尝试。再以 `experiment start` 登记 Run；主指标外用 `extra_metrics` 记录多个真实指标，自动扩展数字列；恢复用 update，不重复 start，不静默覆盖旧证据。

## 自主调度

目标是**缩短假设到有效结果的迭代时间**，不是占满 GPU。围绕验收标准和预算、资源、先后顺序等硬约束，按真实依赖、成本、耗时与结果自主优化调度：优先关键假设、解锁依赖和高风险验证；每轮复盘，重排、拆并任务、调整实例，用 CLI 同步并简述原因，无需逐项请示。

多实例/SSH 是候选资源，不代表都有 GPU。多实例优先 `list_instances(instance_uuids=[...])` 批量筛选，单实例或操作后用 `instance_status` 定点核实模式及 `gpu_name`、`gpu_memory_gb`（单卡）、`cpu_cores`，关机也可查规格，无需为此开机或 SSH；`null` 保留待核实，快照规格不代表 GPU 就绪，也不改变无卡资源上限。核实映射、已有训练及环境/数据；缺卡时用其他合格实例推进，同时做无卡准备，不让单台等待阻塞全局。迁移先验环境、数据/checkpoint 兼容性，不重复启动或擅停训练。SSH 可达、开机请求提交均不等于 GPU 就绪；验证后才训练、标 running。开机或切换返回 `job_id` 后优先 `wait_for_job`（默认 15 秒，按授权剩余时间/调用超时缩短），缺货 `waiting_capacity` 默认立即返回；汇报 `phase`、`elapsed_seconds`、`attempts`、`suggested_next_poll_seconds`（建议下次拉取状态前等待的秒数，退避阶段已含重试窗口余量，不是资源可用倒计时）并推进其他任务。要等这台机器时，用宿主定时调度器按建议秒数挂起（Antigravity CLI 为 `schedule`，`DurationSeconds` 取建议值、至少 1 秒，`Prompt` 写明到点复核 `job_id`）；登记后继续推进其他任务，没有可推进工作时结束本轮等定时通知，到点再 `job_status` 复核；不要用 shell `sleep` 盲等，不紧密重复默认调用。宿主没有定时能力时，才在授权范围内用 `wait_for_job(..., return_on_capacity=False)`（单次 ≤300 秒）。`job_status` 只用于即时进度；等待超时或取消等待不取消后台任务；确需停止后续尝试时按授权 `cancel_job`，它返回取消后即时物理状态 `resulting_status`，稳定态可直接判读，过渡态或查询失败（`resulting_status_error`）再查 `instance_status`。不密集轮询或反复提交开机请求；记录实际分配、阻塞与待估，不把等待当进度。

**单 agent 多机再平衡**：任务完成/失败、实例变为可用及例行巡检时，先核实并更新真实状态，再查看全局待执行任务；不能只看空闲实例原有队列。优先将其他实例上尚未启动、依赖就绪且满足开始限制的 `pending` 任务调往合格空闲实例；保留用户指定节点与资源约束，blocked/failed 不自动当候选。每次启动前重新读取最新计划，不沿用旧清单；原远端队列先撤下待执行项并确认未启动，无法确认则不改派。用 `set <任务> --instance <实例> --brief` 同步分配、处理检查结果后，由同一个 agent 在目标机实际启动并核实成功，再登记 running 和实际 Run；不能只改面板就算完成。细则见 [多机再平衡](references/cli.md#单-agent-多机再平衡)。TrainOps 不自动下发训练，agent 未运行时不再平衡，不新增常驻调度器。

不绕过依赖或降低验收；低收益任务可提替代方案，**改变目标、放弃指定实验、增加未授权费用或中断任务须先确认**。见 [调度决策](references/cli.md#调度决策)。

## 安全边界

- 唯一事实来源是项目 `.agent/experiment.yaml` 的 `plan` 段，只经 CLI/面板时价修改；禁止直接 Edit/Write JSON/YAML，恢复先核实、不盲目重启。
- 编号固定：`I01`、`F01`、`F01-T01`。计划只记任务名、实例、耗时、依赖、状态/进度；命令、日志、commit 留远端。
- 同一 GPU 单任务，`local` 可并行；依赖未完成不得启动，done 前核实进程退出与产物。关机须用户授权，最终关机还须无后续复用；无卡转有卡的中间关机按开工顺序与协议执行。
- 故障先报现象与证据，再定位、自愈（一次改一个变量，不重复动作），最多 5 次；仍无法继续即先关机止付（已预授权），记 `failed`/`blocked` 而非 done，随后报告等决策。见 [协议](references/protocol.md)。
- 系统盘放代码/环境，`/root/autodl-tmp` 放数据/checkpoint/TensorBoard，`/root/autodl-fs` 放长期产物与跨实例交接；后者所有实例共享，分目录防覆盖。数据盘不随镜像保存，释放即丢，释放前先转存。
- 缺进度、耗时、费率或余额保留待估，不猜数、不补 0；可用时长只算现金，代金券另列。`--mode` 记开机模式，费率与 Token 只在面板登记，Token 只存面板进程内存。

## 日常闭环

- `show --brief` → 核实后 `set <任务> --status running` → `--progress 40`（真实观测）或 `--checked` → 核实后 done；写命令带 `--brief`。
- 结构编辑回执附检查：修 issues、判读 warnings，不撤销已保存编辑；纯状态更新不必再跑 check + show。摘要有省略，按需 show/check 看全量。多条依赖变更用 `batch --dep "任务=依赖列表"` 一次提交；回执会标出自动串联边，独立任务建表时用 `-` 显式留空。
- 默认每 30 分钟 SSH 巡检（用户另定则遵从）；`--checked` 只记巡检，未记或超 1 小时即未巡检。
- 巡检及完成/失败后先 `experiment show` 读取当前 Run、最近记录与 pending，再 `experiment update`，以 `experiment sync` 补录；恢复/更新不新建重复 Run，不覆盖旧证据；飞书失败不阻断训练与止付。
