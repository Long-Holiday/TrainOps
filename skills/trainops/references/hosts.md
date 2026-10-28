# 宿主能力适配

TrainOps 的计划、脚本、面板和训练合同由三个宿主共用；安装插件不会安装外部 MCP、Vast.ai skill 或 lark-cli。开工先核实当前宿主实际暴露的工具及参数，不从其他宿主照搬工具名，不读取凭据文件。

## 技能入口与路径

- **Claude Code**：安装后以 `/trainops:trainops` 显式调用技能，也可按技能描述自动加载。
- **Codex**：从已安装插件的技能列表选择 `trainops`，或在对话中要求使用 TrainOps；名称以当前客户端展示为准。
- **Antigravity CLI**：规则入口指向同一份 `skills/trainops/SKILL.md`。

`<技能目录>` 是本次加载的 SKILL.md 所在目录，插件安装后可能位于客户端缓存中；据此定位 `scripts/trainops.py`，不要假定当前目录就是 TrainOps 仓库。`-p` 始终指向实验项目目录。

## 面板后台任务

`serve` 是前台服务，由宿主管理后台生命周期。Claude Code 使用 Bash 的 `run_in_background=true`；Codex 使用实际提供的持久执行会话机制，工具返回活动 session 后保留其标识并核实面板可访问；Antigravity 使用其后台任务机制。不要把一次短命 shell 的 `&` 当作后台常驻。宿主没有可持续运行的机制时说明限制，并与用户确认由独立终端运行面板等可用方案；不能宣称面板已常驻。退出宿主后不保证服务继续运行，重开前核实旧服务与项目归属。

## 定时巡检与资源等待

训练启动后实际登记整点/半点巡检，用户另定周期则遵从。先核实已有调度，记录任务标识、时区、关联训练任务与取消方式；不重复登记，不声称退出宿主后仍会触发。

- **Antigravity CLI**：周期巡检用 `schedule(CronExpression="0,30 * * * *", Prompt=巡检指令, IsDaemon=false)`；一次性资源等待用 `schedule(DurationSeconds=建议秒数, Prompt=复核指令, IsDaemon=false)`，秒数至少为 1。
- **Claude Code**：实际工具可用时，用 `CronCreate` 建立 `0,30 * * * *` 的周期任务；用 `CronList` 核对、`CronDelete` 清理。一次性唤醒按实际 schema 设置时间和非周期选项，核对时区与精度。工具是否可用及存活时间以当前会话为准。
- **Codex**：核实当前客户端是否提供定时任务或自动化工具，按其实际 schema 创建、查询和取消。不能假定 Codex CLI 自带 `schedule` 或 `CronCreate`，也不把后台 shell 会话当作唤醒 agent 的定时器。

没有定时能力时明确说明，并取得用户同意的可用监控方案后再声称建立持续监控；不新增 TrainOps 常驻调度器。AutoDL 缺卡等待可按[AutoDL 参考](autodl.md#无卡准备与训练开机)使用只读 `wait_for_job` 的有限等待，不以 shell `sleep` 或密集轮询代替调度。

每次巡检的 Prompt 必须包含以下实际动作：

1. 先检查任务是否已结束或用户已要求停止/取消；若是，执行[收尾](execution.md#完成或取消收尾)，取消本调度及关联资源请求，不再开机、启动训练或续建巡检。
2. SSH 与所选平台工具检查实例、训练进程、GPU 和日志，取得真实进度与指标。
3. 更新 TrainOps 计划和真实 Run；写表失败也继续通知。
4. 通过本宿主实际暴露的 MCP 工具调用 TrainPilot `report`，传已绑定的 task_id 与真实事件并判读返回；终态直接 completed/failed，修复用一次 ack_instruction，不重复通知。写表或对话不能替代实际调用。
5. 对话总结状态、结果与方向，推进下一任务。

执行检查与汇报分开：按预计结束、故障和缺卡退避安排更早的一次性唤醒。完成/取消时实际查询并取消本任务的调度，核实清理结果；面板可保留。

## MCP 与外部技能

Claude Code 和 Codex 使用已连接 MCP 的实际工具；有延迟工具发现机制时先发现所需工具，再按返回的 schema 调用。Antigravity 的 Lazy MCP 用 `call_mcp_tool` 转发。`trainpilot` 和 `autodl_mcp` 是配置别名，不能假定完整限定工具名或包装器跨宿主通用。TrainPilot 探针、参数与返回判读见[调用与开工探针](trainpilot.md#调用与开工探针)。

Vast.ai 必须先加载已安装的外部 Vast.ai 技能（通常为 `vastai:vastai`，以宿主实际名称为准），不可用时报告阻塞；插件安装本身不提供资源操作授权。
