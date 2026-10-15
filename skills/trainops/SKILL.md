---
name: trainops
description: 按需附加的 AutoDL 多实例训练计划账本与甘特图面板；依据目标自主优化调度，用 CLI 维护计划与余额结算，不替换原有角色。
---

# TrainOps

保留原有角色，仅在交办训练或计划任务时操作；仅加载能力不建表、不启动进程。账本只记计划，训练由你通过 SSH 执行。

## 自主调度

以用户目标、验收标准和明确硬约束为准，任务清单与叙述顺序不是僵硬脚本。根据真实依赖、资源、成本、耗时与实验结果自主优化调度：重排、拆并任务、调整实例分配，先验证高风险环节，避免 GPU 空等与重复实验。合理调整不必逐项请示，执行前用 CLI 同步计划并简述原因。

不为提速绕过依赖或降低验收要求。冗余、失效或低收益任务可提出替代方案；改变目标、放弃用户要求的实验、增加未授权费用或中断运行任务，先确认。见 [调度决策](references/cli.md#调度决策)。

## 安全边界

- `$PROJECT/.trainops/plan.json` 是唯一事实来源，仅用 CLI 修改（时价在面板登记），禁止直接 Edit/Write JSON；已有计划不得覆盖。恢复先读摘要，再核实远端进程与产物，不盲目重启。
- 编号固定：实例 `I01`、流程 `F01`、任务 `F01-T01`。只记任务名、实例、耗时、依赖、状态与真实进度；命令、日志路径、commit 留在远端，不写计划或反复贴回对话。
- 同一 GPU 同时只跑一个任务，`local` 可并行；依赖未完成不得启动。done 前核实进程退出与产物；关机须用户授权，最终关机须无后续复用（无卡转 GPU 除外）。
- 代码/环境放系统盘，数据/checkpoint/TensorBoard 放 `/root/autodl-tmp`，长期/共享产物放 `/root/autodl-fs`。数据盘关机保留，不随镜像保存，释放即丢；释放前先转存。
- 缺进度、耗时、费率或余额就保留待估，不猜数字、不补 0；可用时长只算现金，代金券另列。开机模式用 `--mode` 记，费率在面板登记。Token 只进请求头，不进计划或输出。

## 日常闭环

设 `CLI="python3 <本目录>/scripts/trainops.py -p <实验项目目录>"`，`-p` 不是本仓库。

```bash
$CLI show --brief
$CLI set F01-T01 --status running --brief
$CLI set F01-T01 --progress 40 --brief   # 真实观测
$CLI set F01-T01 --checked --brief       # 已巡检，无可靠百分比
$CLI set F01-T01 --status done --brief
```

- 需远端准备时，`autodl_mcp` 优先无卡了解项目/环境、配置改代码；就绪保存后经授权关机，再有卡开机（缺货后台重试），验 GPU 后训练。已有训练不重启；见 [开机模式](references/autodl.md#无卡准备与训练开机)。
- 建表/结构调整见 [CLI](references/cli.md)。`--brief` 随结构编辑附检查，问题须修、提醒须判读，不撤销已保存编辑；纯状态/进度更新后不必机械再跑 check + show。脚本门禁用 `check --strict`。
- 默认每 30 分钟真实 SSH 巡检（用户另定则遵从）；远端脚本先 `source /etc/network_turbo`，训练放 tmux/screen。`--checked` 只记巡检时间；未记录或超 1 小时标为未巡检。
- 摘要限量并注明省略数，不代表完整候选或无异常；按需用 show/check 看全量，日常不用 show --json、不重复贴计划。
- 配置见 [AutoDL](references/autodl.md)，异常/重跑/关机/交接见 [协议](references/protocol.md)，无需预读。monitor 只结算，不替代巡检、不自动关机；正常简报，完成/失败、资源风险或需决策时及时说明。
