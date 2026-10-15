# 异常与恢复协议

仅资源冲突、失败重跑、会话恢复、关机/交接或结算故障时读取。正常操作见 [入口](../SKILL.md)，参数见 [CLI](cli.md)，初次配置见 [AutoDL](autodl.md)。

## 状态与依赖

| 状态 | 含义 | 允许转换 |
|---|---|---|
| pending | 待执行 | running / blocked / cancelled（set --status done 自动完成启动与收尾，仍须核实产物） |
| running | 执行中，独占 GPU | done / failed / blocked / cancelled |
| blocked | 人工确认阻塞 | pending / running / cancelled |
| done | 进程已退出且产物核实 | 重跑先回退 pending |
| failed / cancelled | 失败/取消，不满足下游依赖 | 重试先改 pending |

同一 GPU 同时只允许一个 running，local 可并行；依赖未完成不能启动。运行中改实例/依赖/最早开始限制前，先核实远程进程已停止，再转为 blocked 或结束。计划不记录命令、commit、日志路径。

未巡检依据 checked_at：未记录或距最近巡检超过 1 小时即标记；--progress 会自动记录巡检，没有可靠百分比则只用 --checked。progress_at 是 ETA 的进度采样时刻，单独巡检不应伪造新进度。已超期或进度 100% 的运行任务完成时间回退待估，先核实进程/产物，不直接当作 done。

## 恢复与关机

1. 新会话先 show --brief；有省略或异常时按需 show/check，再 SSH 核实远端会话、进程、checkpoint，禁止盲目重启或覆盖账本。
2. 远程执行训练/准备脚本先 source /etc/network_turbo，训练放 tmux/screen。摘要里的“依赖就绪”只是计划候选，不保证实例在线。
3. 关机前确认进程退出、结果保存，检查所有流程无后续复用该实例且获用户授权，再通过已有 AutoDL 工具执行。TrainOps 本身不下发关机指令。
4. CLI 拒绝把仍有 running 的实例标为 off/unreachable；先核实远端并更新任务状态，不为绕过校验随意改状态。释放实例前还须转存并核实数据盘产物。

## 磁盘处置

SSH 用 df -h 检查三盘，必要时只查看相关目录占用，登记方法见 [配置](autodl.md#三盘与用量)。用量达到 90% 时需处理；清理前核实目标，不删除唯一 checkpoint 或其他实验产物。

| 满盘位置 | 处置方向 |
|---|---|
| 系统盘 | 核实缓存占用，将数据/checkpoint 移至数据盘；避免清理破坏训练环境 |
| 数据盘 | 先转存必要产物，按保留策略清理中间权重或扩容；扩容涉及费用先确认 |
| 文件存储 | 检查长期产物与小文件/inode，用量计费；不要当临时盘 |

## 结算恢复

- Token 配置与只读接口口径见 [配置](autodl.md#余额与定时结算)。没有 Token 不算 monitor 故障：仍用已有余额结算并标明未配置；明确联网的 wallet fetch 才报错。
- 网络/API 失败：last_error 保存单行原因，保留上一份结论，不打断循环；check/show --brief 报提醒。monitor --once --strict 返回非零用于告警。修复后 monitor --once --brief 立即重算。
- 人工顶替用 wallet set --assets/--voucher，汇报注明人工登记；无参数会被拒绝。不要用 wallet fetch 代替重算，它只更新余额。
- 新鲜结算若标记“结算后计划有变更”，不能当作新计划费用；超过 2 周期会退回实时值。缺字段是数据不足，不是零花费或余额足够。
- schema 版本不符或文件损坏时先备份并核对文件，禁止直接编辑 JSON“修复”；开发阶段没有自动迁移。不要用 demo --force 覆盖真实计划。
