---
name: trainops
description: 按需附加的 AutoDL 多实例训练计划账本与只读甘特图；用 CLI 维护任务、查看行动摘要和余额结算，不替换 agent 原有角色。
---

# TrainOps

保留原有角色，仅在用户交办训练或计划任务时操作。仅加载能力时不建表、不启动进程。账本只记录计划；训练仍由你通过 SSH 执行。

## 安全边界

- `$PROJECT/.trainops/plan.json` 是唯一事实来源，仅用 CLI 修改，禁止直接 Edit/Write JSON；已有计划不得覆盖。恢复会话先看摘要，再核实远端进程与产物，不盲目重启训练。
- 使用固定编号：实例 `I01`、流程 `F01`、任务 `F01-T01`。只维护任务名、实例、耗时、依赖、状态与真实进度；命令、日志路径、commit 留在远端脚本等处，不写进计划，也不反复贴回对话。
- 同一 GPU 同时只跑一个任务；`local` 可并行。依赖未完成不得启动。标记 done 前核实进程退出与产物；关机前检查所有流程无后续复用并取得用户授权。
- 代码/环境放系统盘；数据、checkpoint、TensorBoard 事件放 `/root/autodl-tmp`；长期或跨实例产物放 `/root/autodl-fs`。数据盘关机保留，但不随镜像保存、释放实例即丢；释放前先转存。
- 缺进度、耗时、时价或余额就保留待估，不猜数字、不用 0 顶替。可用时长只算现金，代金券另列。Token 只进请求头，不进计划或输出。

## 日常闭环

设 `CLI="python3 <本目录>/scripts/trainops.py -p <实验项目目录>"`，`-p` 不是 TrainOps 仓库目录。

```bash
$CLI show --brief                       # 接管/决策时读行动摘要；完整明细才用 show
$CLI set F01-T01 --status running --brief
$CLI set F01-T01 --progress 40 --brief   # 只登记真实观测
$CLI set F01-T01 --checked --brief       # 已真实巡检，但没有可靠百分比
$CLI set F01-T01 --status done --brief
$CLI batch --set "F01-T01:done" --set "F02-T01:running:55" --brief
```

- 首次建表、追加任务或调整依赖/实例/耗时：按需读 [CLI 用法](references/cli.md)。`--brief` 结构编辑会随成功回执附带检查；问题须修，提醒须判读，但均不撤销已保存编辑。纯状态/进度更新成功后不必机械再跑 check + show。脚本门禁仍用 `check --strict`。
- 默认每 30 分钟真实 SSH 巡检（用户另定周期则遵从）；远程训练/准备脚本先 `source /etc/network_turbo`，训练放 tmux/screen。`--checked` 只记巡检时间，不替你检查远端；未记录或超过 1 小时会标为未巡检。
- 摘要按组限量并注明省略数，不等于无异常；需完整任务看 `show`，完整问题/提醒看 `check`。日常不用 `show --json`，不要重复汇报全计划。
- 配置存储、TensorBoard、余额或面板时读 [AutoDL 配置](references/autodl.md)；异常、重跑、关机/交接时读 [恢复协议](references/protocol.md)。平时无需预读参考。
- 余额结算交给 monitor 定时进程；它不做 SSH 巡检或自动关机。按用户约定汇报，例行正常巡检简报，完成/失败、资源风险或需用户决策时及时说明。
