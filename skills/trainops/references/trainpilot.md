# TrainPilot 通知与决定

训练通知、异常按钮或恢复接管时读取。工具名、参数和协议以已连接服务的 instructions / 工具描述为准；不读取其凭据文件，不另起通知服务。尚未更新的服务可能仍带旧功能，先核实实际工具契约，不把旧协议下的操作当作新协议已生效。

## 调用与开工探针

Antigravity 的 Lazy MCP 工具通过宿主 `call_mcp_tool` 转发；没有独立原生 `report` 命令不等于工具不可用。下例中 `ServerName="trainpilot"` 是客户端配置别名，服务自报名称是 `TrainPilot`；若本机别名不同，按实际配置替换。`ToolName` 使用服务注册的 `get_task_status` / `report`。包装器参数名及 `Arguments` 的类型以宿主实时 schema 为准，要求字符串时将参数对象序列化为 JSON，不猜原生工具完整限定名。

**开工前必须实际执行探针**，不能只在对话中确认“工具可用”。优先查询本次真实 Run 绑定的 task_id，不创建测试任务、不为探针虚构训练进展：

```python
call_mcp_tool(
    ServerName="trainpilot",
    ToolName="get_task_status",
    Arguments={"task_id": "project-exp01-run01"}
)
```

先判读宿主/MCP 错误（含 `isError`），再解析结构化结果或 content 中的业务 JSON。已有任务返回 `success=true`；新 Run 首报前明确返回 `success=false`、`error="Task 'project-exp01-run01' not found"` 也证明查询已到达服务端，可通过连接探针。其他业务错误、连接/授权失败或无法解析的返回均未通过，先解决或取得用户明确同意的替代通知方案，再启动新训练。查询成功仅证明 MCP 查询可用，不证明通知送达或训练正常；接管已有训练不因探针失败停进程，安全处置和关机止付继续执行。

也可用**真实开工事件** `report(event_type="milestone")` 作探针，需业务 `success=true`；此时只描述实际完成的准备，不提前宣称训练已启动。`report` 会创建/更新控制面任务、追加事件并异步发送卡片，不能当无副作用的连接测试。每次调用都显式传绑定的 task_id，避免多个 Run 落到服务端默认任务。

正常巡检必须实际执行以下调用结构，不能只把代码块贴到对话里。下列 task_id、步数、消息和 loss 仅为语法示例，执行时替换为真实 Run 与观测值；未知的 step/epoch/metrics 省略：

```python
call_mcp_tool(
    ServerName="trainpilot",
    ToolName="report",
    Arguments={
        "task_id": "project-exp01-run01",
        "event_type": "milestone",
        "step": 800,
        "message": "训练正常推进至 800 步，日志持续更新",
        "metrics": {"loss": 0.4502}
    }
)
```

`report` 必填 `message`；可选 `event_type`（milestone / alert / completed / failed）、`task_id`、`step`、`epoch`、`metrics`、`agent_note`。`success=true` 只表示事件已记录，飞书送达未确认时如实说明；查询状态位于 `task.state`，report 状态位于顶层 `state`。调用超时结果不明时先核对事件与状态，不盲目重发制造重复卡片。

巡检依次更新 TrainOps 计划/Run、调用 TrainPilot、最后对话总结。`lark-cli` 写多维表格是被动档案留痕，TrainPilot 发卡片是主动即时通知，飞书凭据与调用逻辑独立；`synced to Feishu` 不能替代 `report`。写表失败仍执行即时通知；终态与修复回执按下文执行，避免重复通知。

## 绑定与状态

为每次 Run 使用项目范围唯一的 task_id，保存 Run 与控制面 ID 的非敏感映射；新尝试使用新 ID，接管沿用原 ID。`get_task_status` 查询记录，不代表 GPU 进程的事实。首报前 Task not found 正常，不为测试连接制造任务。

正常巡检和重大节点使用 `report`，指标只传真实观测。`completed` / `failed` 直接报告终态，不再先 milestone 再重报终态；事件已记录与飞书已送达按实际返回区分，异步发送未确认时不宣称用户已收到。通知失败保留待补事项，不阻断产物保护和止付。

## 异常与直接对话

异常用 `report(event_type="alert")` 报告现象、证据和受影响 Run。实际进程由 agent 检查和保护，控制面 WAITING 不代表进程已停止。服务的普通告警自动自愈窗口按当前协议处理；轮询超时本身不是决定。

- **自行解决**：通过 `poll_instruction` 取得决定，核实后执行已授权修复；最多 5 次的累计上限不因新指令或接管重置。`ack_instruction` 报告修复动作和结果，作为这次修复的唯一恢复汇报，不再追加重复 milestone。
- **等待用户**：`poll_instruction` 返回 awaiting_direct_conversation=true 后保持当前现场与任务状态，结束轮询，把控制交还用户直接对话；不拉取群消息、不要求 #fix，不设置等待安排超时后自动修复。其他实例上的独立工作和必要费用保护仍可继续。用户直接给出方案后执行、验证，以 `ack_instruction(action="self_resolve", solution=实际处理)` 回执，省略 instruction_id；不把用户的沉默当成许可。
- **取消任务并关机**：取消对应计划任务、停止本任务进程并保全必要产物，清理本任务的资源等待，调用 AutoDL 关机并核实物理终态。若有其他任务共享该实例，先说明冲突，不能误停其他训练；结果未知或关机失败如实回执，不能报取消关机成功。该按钮代表对对应任务取消及其专属实例关机的授权。

领取决定后的回执使用实际返回的 instruction_id/action；直接对话恢复按上面的无 instruction_id 路线处理。执行并验证后才报告 success，失败则报告 failed。取消的成功回执保持终态并直接发送取消结果，不再追加 report failed，不把任务重置为 RUNNING。具体字段以实时工具说明为准。

## 任务来源

新任务、后续实验与修复方案由用户直接与 agent 对话交办。TrainPilot 不接收或返回飞书下一步计划，agent 不调用 ack_plan，也不维护第二份远端任务队列。TrainOps 计划保留真实依赖，Run 日志保留尝试与结果。
