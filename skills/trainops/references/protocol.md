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
- 依赖未达成的任务不能被标记为 `running`；`running` 期间只允许改状态、进度与备注，改实例/依赖/最早开始时间必须先结束该任务。
- 日常更新只需维护 `--status` 与 `--progress`（外加可选 `--note`）；计划表本身不记录命令、commit、日志路径等过程性信息，这些留在对话与远端。
- `show` 中的 `未巡检` 表示 `running` 任务超过 1 小时未更新进度，`可开工` 表示依赖已就绪可以启动，`已超期` 表示超过了预计完成时间、需要重新估计。

## 2. 会话恢复与安全关机

1. **恢复原则**：新会话接管时先执行 `show` 查看现有计划，再通过 SSH 检查远端会话与训练进程状态，禁止未核实远端就直接重启训练或覆盖计划。
2. **远程环境**：每次远程 Bash 执行训练或准备脚本前先执行 `source /etc/network_turbo`。
3. **安全关机**：确认当前任务进程已退出、结果已保存，并检查计划表中**所有流程**无后续任务复用该实例且获用户授权后，再通过 MCP `autodl` 关机。CLI 侧的兜底：该实例仍有 `running` 任务时，`instance set --status off/unreachable` 会被直接拒绝，必须先核实远端并更新任务状态。

## 3. 磁盘巡检与满盘处置

```bash
ssh <别名> 'df -h / /root/autodl-tmp /root/autodl-fs && du -sh /root/autodl-tmp/* 2>/dev/null | sort -h | tail'
```

```bash
python3 <本目录>/scripts/trainops.py -p <项目> instance set I01 --disk "sys:12/30,data:44/50,fs:5/200"
```

判读与处置：

| 现象 | 处置 |
|---|---|
| 系统盘 ≥90% | 环境/缓存挤占系统盘：`conda clean -a`、清 pip 缓存、把数据集与 checkpoint 移到数据盘；系统盘满会导致 SSH 与训练直接失败 |
| 数据盘 ≥90% | 删中间权重、保留最后一个 checkpoint，或把产物转存到文件存储/本地；数据盘扩容按天计费，先清理再考虑扩 |
| 文件存储 ≥90% | 只留最终产物；文件存储 20G 以上按量计费，且 inode 数量有上限（小文件多更容易先耗尽） |
| 释放实例前 | 数据盘不随镜像保存、释放即丢：先把要保留的产物 `rsync` 到 `/root/autodl-fs` 或下载回本地，再释放 |

登记后再看 `$CLI check`：`warnings` 里会出现"某盘已用 ≥90%，请先清理或扩容再开新任务"，`show` 的实例行会带 `!`。

## 4. 余额、自动结算与接口降级

- 联网只做一件只读的事：拉账户余额，不下发实例指令、不触发计费。`wallet fetch` 手动拉一次；`monitor`（或 `serve --interval`）按周期自动结算并把结论写进计划。
- Token 来源优先级：`--token` > 环境变量 `AUTODL_TOKEN` > TrainOps 仓库根的 `.env`（模板 `.env.example`，`chmod 600`；`TRAINOPS_ENV` 可指定别的文件）。**token 只进请求头**，绝不写入 `plan.json`、stdout/stderr 或异常文本。
- 没有 token：不算故障——`monitor` 照常记录结算时刻与"未配置 Token"，其余结论用计划里已有的余额算；`wallet fetch` 这类明确要联网的命令才会报错退出。
- 网络不通 / 接口报错：`monitor` 把原因压成一行写进 `plan["monitor"]["last_error"]`，**不退出循环、不改动计划其余部分、保留上一份结论**；`check` 会把它列进 `warnings`，`monitor --once --strict` 退出码非 0 便于 cron 告警。想手工顶一段时间，用 `wallet set --assets <余额> --voucher <代金券>`（无参数的 `wallet set` 会被拒绝，不会写坏数据），并在汇报里说明该数字是手工填的。
- 结算新鲜度：结论超过 2 个结算周期未更新就视为过期，`show` 与面板回退为实时值并说明原因（"已过期"／"未自动结算"）；计划在结算之后被改动时额外标注"结算后计划有变更"。**同一屏不要出现两套数字**，也不要把过期结论当现状汇报。
- 换取数据前先确认单位：官方约定余额与代金券都是"千分之一元"的整数，CLI 已做 ÷1000；若接口返回非整数会直接报错而不是写进计划。
- 结论缺省不是故障：任一开机实例缺时价（`instance set I01 --price 1.88`）或任一任务完成时间待估时，预算与结算里会少给可用时长/计划花费——补数据即可，不要自行估算。
