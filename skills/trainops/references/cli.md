# CLI 用法

仅首次建表、批量编辑或查参数时读取；日常遵循 [入口](../SKILL.md)。设 `CLI="python3 <技能目录>/scripts/trainops.py -p <实验项目目录>"`。

## 建表

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
- 任务写法 `流程ID:任务名[:实例[:分钟数[:依赖]]]`。批量新增时未指定依赖会自动依赖同流程上一个任务；跨流程依赖需显式列出。
- 中间字段用 `-` 跳过；耗时 `?` 表示待估，不能据此得出可靠 ETA/费用。依赖逗号分隔。不要在名称中使用分隔符 `:`，复杂名称用单项 `task add --name`。
- init 拒绝覆盖已有计划；没有计划时先等用户具体实验任务，不为“加载能力”建空账本。

## 更新与追加

```bash
$CLI set F02-T02 --instance I01 --deps F01-T02,F02-T01 --minutes 25 --brief
$CLI batch --task "F03:补充实验:I02:45" --set "F01-T01:done" --set "F02-T01:running:55" --brief
$CLI batch --task "F01:评估:-:30:F01-T01" --brief
$CLI task add --flow F01 --name "后处理" --instance local --minutes 10 --deps F01-T02 --brief
$CLI instance add --name "新增GPU" --autodl-id id-c --ssh-alias gpu-c --brief
$CLI instance set I01 --status on --observed --brief
```

- `set` 是 `task set` 简写，二者均支持 --brief；`flow add/set`、`instance add/set`、`wallet fetch/set` 同样支持。
- 批量更新写法 `任务ID:状态[:进度[:备注]]`；批量状态更新会登记巡检时间，须先真实观测，不把它当作自动巡检。
- 单项任务更新的 `--deps ""` 清空依赖，`--instance none`（或 `-`）清空实例，`--minutes ?` 清空耗时，`--not-before none` 清空开始限制；最早开始时间必须是带时区的 ISO 时间。
- `local` 不占 GPU，可并行；`none` 表示未分配实例，不等同于本地。
- 重命名用 `flow set F01 --name ...` / `set F01-T01 --name ...`；编号保持不变。运行任务不能直接改实例/依赖/开始限制，恢复规则见 [协议](protocol.md#状态与依赖)。

## 输出与检查

- `show --brief`：当前运行、依赖就绪候选、待处置/待估、实例和问题/提醒。每组最多 3 条，长文本折成单行并标 `…`；总数和省略数在截断前计算。GPU 每实例仅列一个候选，local 不去重；依赖就绪不保证实例在线，执行前核实远端。
- `show`：完整文本计划，包含历史任务。`show --json`：完整机器快照；与 --brief 互斥。
- 写命令无 --brief 时保持原 JSON 回执；有 --brief 时输出版本、编号和本次更新。新增编号超限也会注明省略，可用 show 查询，不要重跑写命令取编号。
- brief 新建/结构编辑自动检查最终状态：init、新增实例/流程/任务、batch 含新增项，或 set 改实例/耗时/依赖/开始限制（含清空）。诊断对应同次保存，只增一个版本，不额外读盘；不写进账本。
- 纯状态/进度/巡检/备注/改名、实例观测/时价/磁盘、钱包及结算不自动做全计划结构检查；需要全局诊断时用 show --brief 或 check。
- `check` 把必须修复的 issues 与提醒 warnings 分组；默认退出 0。`check --strict` 仅在 issues 非空时退出 1，warnings 不影响 ok。brief 编辑发现问题仍是“已保存”，不撤销编辑、不改变成功退出码。硬校验拒绝的编辑不落盘。
- `--version` 查询 schema；版本不符时先备份并核对文件，开发阶段不自动迁移。
