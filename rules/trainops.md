---
trigger: always_on
---

# TrainOps

TrainOps 为 Antigravity CLI 提供实验计划、任务面板、Run 日志和周期汇报能力。收到具体实验任务时读取 [`skills/trainops/SKILL.md`](../skills/trainops/SKILL.md)，仅加载时不启动实验。

在目标、验收和资源授权内自主推进与优化，不逐项请示；改变目标、放弃必需实验或超出资源授权时请求决策。尽早以后台常驻任务起面板，默认先做必要的无卡准备；最多 5 次自愈仍无法继续则关机止付。

项目 `.agent/experiment.yaml` 是计划与 Run 的唯一事实来源，推荐 CLI 规范化创建/更新，也允许先读后写直接编辑并校验。面板候选队列不等于已执行，缺数据就缺结论，不补 0。

MCP trainpilot 负责通知和按钮决定，lark-cli 负责实验日志；用户通过直接对话安排任务和提供等待中的方案。代码修改、本地同步与远端拉取、训练核实和收尾见[执行与通知闭环](../skills/trainops/references/execution.md)。参数与故障细节按需读 references，不一次加载全部文档。
