# TrainPilot MCP 监督闭环

开工、巡检、异常、飞书指令与完成/取消时读取。本接口说明核对自本机 `/home/default_user/TrainPilot` 的 `plugins/trainpilot/skills/trainpilot/SKILL.md`、`src/trainpilot/server/mcp_server.py` 与数据契约；路径仅为实现来源，不是安装或启动要求。TrainOps 不修改 TrainPilot、不启动本地代理或第二套监控器、不读取其凭据文件。

## 连接协议与任务绑定

- **先读取实时协议**：实际连接的 MCP 初始化 `instructions` 与工具描述是 TrainPilot 监督协议的权威来源，以下是当前实现的接入说明，不替代实时协议。客户端未暴露初始化 instructions 时，停止接入监督并要求修复/重连，不凭本文猜测协议，也不声称即时监控已建立；必要的进程/产物保护和止付不因此延迟。
- 从当前 MCP 工具列表定位工具，服务名前缀可能不同；工具缺失时说明连接阻塞，不擅自换服务器、安装 Python 服务或读取凭据兜底。
- 为每次实际 Run 绑定明确且唯一的 `task_id`（如项目标识 + Run ID），在本项目非敏感 `config.settings` 或远端可定位记录中保留与 TrainOps 任务/Run 的映射，只经现有 CLI 修改项目文件。恢复沿用同一 ID，新尝试不串用旧邮箱；多个项目都有 `F01-T01`，不能只用该编号作为全局 ID。
- `report`、`poll_instruction`、`ack_instruction`、`get_task_status` 每次显式传 `task_id`；省略会使用服务端默认值，不是训练机环境变量。`ack_plan` **没有 `task_id` 参数**，使用实际返回的 `plan_id`。
- 连接检查先 `get_task_status(task_id=...)`，不为测试连通随意创建任务。首报前 Task not found 正常；恢复时读取控制面状态及待处理指令，再 SSH 核实，控制面状态不代表进程已停止或 GPU 正常。

## 五个工具的分工

| 工具 | 输入与使用时机 | 必须处理的返回 |
|---|---|---|
| `get_task_status` | 显式 `task_id`；开工连通检查、恢复接管、状态有歧义时 | `success`、任务 `state`、`pending_instruction`、`awaiting_arrangement`；只读摘要不等于消费指令 |
| `report` | `task_id`、`message`、`event_type`；可传真实 `step`、`epoch`、`metrics`、1–3 句 `agent_note` | `success`、`state`、每次都有的 `next_task_plans`，alert 时的 `next_action` / guidance；不可只看发送返回而忽略任务计划 |
| `poll_instruction` | 同一 `task_id`，`wait_timeout` 适配宿主超时；异常决策等待 | `has_instruction`、`status`、`instruction_id`、`action`、`payload`；取到指令即消费，保存原始 ID/action 防止丢失或执行错项 |
| `ack_instruction` | 指令执行并验证后：`task_id`、原 `instruction_id`、原 `action`、`status="success"` 或 `"failed"`、`solution` | 检查 `success` 与 `state`；描述实际操作和证据，失败不伪报成功，不把 ACK 当作执行动作 |
| `ack_plan` | 下一任务计划处理后：原 `plan_id`、`status="done"` / `"failed"` / `"skipped"`、`note` | 检查 `success`；成功 ACK 前计划可能继续出现，按 `plan_id` 去重，不重复执行 |

充分利用指按场景使用全部工具，不是每轮机械调用五遍；正常巡检不额外空轮询，异常、恢复及收到计划时进入对应闭环。

## 状态上报与飞书送达边界

- 正常巡检、开机/同步/启动/阶段/评估/恢复/关机等节点用 `report(event_type="milestone")`；`metrics` 仅传真实观测值，未知留空，不把训练 loss 冒充最终评测。
- NaN/Inf、loss 爆炸、OOM、意外退出及阻断训练的错误，**修复、重启或放弃前先 `report(event_type="alert")`**。不能用 milestone 掩盖异常，也不能用 failed 替代仍需决策的 alert。
- `report(success=true)` 表示控制面事件已记录；飞书通知异步发送，**不等于已送达**。汇报应写“已上报，飞书送达未确认”，只有独立送达证据才可确认收到；失败保留待补事项，不忙循环、不重复发同一未解决 alert。
- 当前实现仅 `milestone` / `alert` 进入通知卡发送分支，`completed` / `failed` 仅记录终态。正常完成时在标终态前，先用 milestone 发送最终结果位置、资源清理和实例状态摘要，处理其返回计划，再 `report(event_type="completed")`；取消/明确失败用 `failed`，不伪造成 completed。处理停止指令时按下节先 ACK 再 report failed。
- 终态之后才完成的关机/清理不能再发 milestone 把同一邮箱重置为 RUNNING；尽量在终态前完成收尾通知。确需终态后追加推送时按实时协议处理，当前实现可先 milestone 发送事实摘要、随即重报同一终态；明确中间状态仅为通知写入，不表示训练恢复，处理两次 report 的计划且不重复执行。不得只发 completed/failed 就宣称用户已收到最终通知。

## 异常决策、执行与回执

1. **先告警并保护现场**：report alert；在可行时安全暂停 GPU 作业，已退出则如实说明。控制面 WAITING 不会自动暂停进程，agent 必须核实真实进程状态。
2. **等待真实指令**：同 task_id 调用 `poll_instruction` 直到 `has_instruction=true`。长轮询超时不是恢复许可，不重复上报 alert，不自行推断服务端超时自动自愈已发生。用户选择等待安排时可长期 WAITING，继续等待，不能当成服务故障或擅自恢复。控制面进入 COMPLETED/FAILED 时停止轮询。等待期间仍遵守实例计费和保护边界，多机任务不因一台的人工决策等待而被遗忘。
3. **按 action 执行**：`self_resolve` 才进入已有自愈流程，一次只改一个变量、核实结果，最多 5 次；`custom` 按 `payload['solution']` 的人工方案执行，改代码仍走本地同步；`stop_training` 停止对应作业并确认进程退出、保全产物。涉及超授权费用、目标变更或其他任务资源冲突先核实授权，不把任意文本当作无限权限；未知 action 不猜测执行。
4. **核实后 ACK**：使用收到的 instruction_id/action，成功才 ack success，否则 ack failed，solution 说明实际做过的操作、证据和失败原因。当前成功 self_resolve/custom ACK 会异步发送恢复卡，不必为重复卡重报同一进度；仍维护真实计划和 Run。`stop_training` 在 ACK 后必须 `report(event_type="failed")`，再按收尾协议清理资源并确保最终摘要推送与终态一致。
5. **与五次止付的关系**：实时 TrainPilot 协议决定何时允许修复；“先报告再自愈”具体为 alert → poll_instruction → 执行获准修复 → 核实 → ACK，而不是一发告警就修。5 次是上限，不因每次指令或会话恢复而重置；仍无法继续按既有预授权先关机止付再报告，不能借通知等待无限付费空转，也不把轮询超时算作一次修复。用户明确等待安排时不擅自恢复；费用影响超出既有授权则汇报并请求决策。TrainPilot 不可用时明确通知/决策链路受阻，继续适用既有安全保护及止付授权，不声称收到不存在的指令。

## 下一任务计划

- **检查每次 report 的 `next_task_plans`**，包括 milestone、alert、completed、failed；空数组表示本次没有待办。计划绑定该 task_id，按返回的 plan_id 去重，重复出现是未 ACK 的同一计划，不是新的执行请求。
- 未解决 alert 优先；下一任务在当前 Run 结束后的安全点推进，不在训练 step 中途、告警未解决前执行。先核实任务内容、验收、预算、目标机器和真实依赖，用现有 CLI 维护计划后由 agent 实际执行，不绕过依赖或擅自停掉现有训练。
- 不把“收到计划”“记入面板”当作做完；执行并核实后 `ack_plan(status="done", note=实际结果)`，执行失败为 failed，已明确不适用或用户取消才 skipped；暂时等待资源、缺授权或待确认时保留 pending 并报告，不提前 ACK 吞掉计划。
- 接到后续计划不代表继续沿用旧 Run 邮箱；新 Run 按绑定规则建独立 task_id。收尾前检查是否影响实例复用和共享巡检，既不误关仍需使用的实例，也不无限保留无授权的付费资源。计划 ACK 失败先核实，不重复已完成动作，不改变实验日志人工决策列。
