# 多实例训练异常与恢复参考

本文仅在遇到多实例资源冲突、会话恢复、训练异常或收尾交接时按需查阅，日常操作直接遵循 `SKILL.md` 即可。

## 1. 状态与依赖约束

| 状态 | 含义 | 允许转换 |
|---|---|---|
| `pending` | 待执行 | `running` / `blocked` / `cancelled`（CLI `set --status done` 自动完成启动与收尾） |
| `running` | 执行中，独占所属 GPU 实例 | `done` / `failed` / `blocked` / `cancelled` |
| `blocked` | 人工确认阻塞 | `pending` / `running` / `cancelled` |
| `done` | 进程已退出且产物已核实 | 重跑需先回退 `pending` |
| `failed` / `cancelled` | 失败或取消（不满足下游依赖） | 重新执行时改为 `pending` |

- 同一 GPU 实例同一时刻只允许一个 `running` 任务；`local` 本地节点不占 GPU，可并行。
- 日常更新只需维护 `--status` 与 `--progress`，无需填写命令、commit、日志路径、owner 等非核心字段。

## 2. 会话恢复与安全关机

1. **恢复原则**：新会话接管时先执行 `show` 查看现有计划，再通过 SSH 检查远端会话与训练进程状态，禁止未核实远端就直接重启训练或覆盖计划。
2. **远程环境**：每次远程 Bash 执行训练或准备脚本前先执行 `source /etc/network_turbo`。
3. **安全关机**：确认当前任务进程已退出、结果已保存，并检查计划表中**所有流程**无后续任务复用该实例且获用户授权后，再通过 MCP `autodl` 关机。
