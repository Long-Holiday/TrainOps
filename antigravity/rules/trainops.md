# TrainOps

本规则所在目录的上一级即 TrainOps 插件根。TrainOps 是按需使用的 **AutoDL 多实例训练管理能力**：本地计划账本（实例 / 流程 / 任务依赖 / 真实进度）+ 甘特图面板 + 余额结算。它本身不跑训练、不是服务，只记录与展示实验计划；保留原有角色，**"不建表、不启动进程"只约束加载阶段**（没有具体实验任务时不要创建计划文件）：**收到任务后按 SKILL.md 的开工顺序起面板（`serve`）并走无卡准备**，不直接申请有卡开机。

**能力契约（完整细则）：[`skills/trainops/SKILL.md`](../skills/trainops/SKILL.md)** —— 交办训练或计划任务时先读它并按其中的日常闭环操作；CLI 参数、AutoDL 配置与异常恢复按需读同目录 `references/`，不要一次全读。

三条不变量在任何情况下都成立：

1. `$PROJECT/.trainops/plan.json` 是唯一事实来源，只经 CLI 修改（时价例外：在 `serve` 面板的实例卡上登记）。
2. 缺数据就缺结论：算不出的数字不显示，不补 0。
3. 自主优化排程不必逐项请示；改变目标、放弃实验或增加费用前先确认。

CLI 与 `-p`（指向**实验项目目录**，不是插件根）：

```bash
python3 "<插件根>/skills/trainops/scripts/trainops.py" -p <实验项目目录> show --brief
```
