# 实验日志与飞书同步

实际实验任务的开工、训练启动、SSH 巡检、完成/失败与会话恢复时读取。本能力通过外部 **`lark-cli`**（不是 `lark_cli` 或旧 bitable API）维护多维表格；不靠面板刷新，不新增常驻监控器，不自动运行训练。设 `CLI="python3 <技能目录>/scripts/trainops.py -p <实验项目目录>"`。

## 一个文件，三段数据

`.agent/experiment.yaml` 是实验项目唯一的持久文件，随**实验项目**保存，不在插件安装目录：

- `plan`：计划与任务状态的唯一事实来源，只经原 CLI / 面板时价入口修改。
- `config`：项目名、表格定位、字段映射、主指标方向与其他非敏感项目配置；首次 `experiment init` 前为 `null`。
- `experiments`：本地 Run 历史及同步状态；飞书只是它的检索索引，不反向驱动计划。日志命令只改这一段，不推进计划 `revision`，也不向计划塞指标、命令、日志或 commit。

文件使用 **JSON 兼容 YAML 1.2**：内容是缩进 JSON（合法 YAML 子集），由 Python 标准库读写。首版不支持缩进式 YAML、注释、锚点或自定义标签；不支持/损坏时拒绝读取，不覆盖原文件。用命令维护，无需手填：

```json
{
  "schema_version": 1,
  "config": {
    "schema_version": 1,
    "project": "项目名",
    "lark": {
      "base_token": "真实返回的 Base 资源 ID",
      "table_id": "真实返回的数据表 ID",
      "url": "真实表链接",
      "fields": {"intent": "实验意图"}
    },
    "defaults": {"metric_name": "accuracy", "direction": "max", "dataset_version": "v1", "eval_protocol": "eval-v1"},
    "settings": {"monitor_minutes": 30}
  },
  "plan": {"schema_version": 3, "name": "项目名", "revision": 1, "...": "计划字段"},
  "experiments": {"schema_version": 1, "runs": {}}
}
```

示例中主指标与版本仅演示格式，不当作真实实验默认值。`settings` 可保存其他项目级信息，但 TrainOps 不会自动把它解释为调度参数；用户指定巡检间隔后仍需 agent 实际安排巡检。`defaults` 仅在创建新 Run 时复制，不改历史。

**不存凭据**：`base_token` 是资源标识，不是 access token；access/refresh token、app secret、SSH 密钥、授权 device code 不得进入配置、记录、命令 JSON 或飞书单元格。飞书登录与权限交给 `lark-cli`，不复制它的凭据。AutoDL Token 仍只由面板登记并保存在进程内存。字段名防误存校验不能识别自由文本中的所有秘密，agent 上传前必须检查摘要与引用，不上传完整日志、命令中的密码或私密 URL。

## 开工和表格选择

收到实际任务后按入口先核实计划、后台常驻起面板，再初始化日志；仅加载插件时不创建任何文件或飞书表。

```bash
# 用户指定表：解析真实 Base / table ID，验证字段，再保存项目配置
$CLI experiment init --url 'https://<飞书域名>/base/<资源标识>?table=<数据表ID>'

# 未指定：已有配置则验证复用；没有配置则自动创建“<项目> · 实验日志”及初始表
$CLI experiment init

# 合并项目默认值与其他非敏感项目设置；保留无关已有键
$CLI experiment init --json '{"defaults":{"metric_name":"accuracy","direction":"max"},"settings":{"monitor_minutes":30}}'
$CLI experiment show
```

用户新指定的表优先于历史配置；只提供 Base 链接但没有 table ID 时要求确定目标表，不猜选。身份默认显式 `--as user`，不会自动切换 bot。首次使用需 `lark-cli` 已安装并完成用户授权；未授权或权限失败先报告，继续本地留痕，不能误判成“用户没指定”反复建表。

自动建表成功即保存真实标识，后续字段核验失败也不重建。创建超时或响应无法确认时，本地记录 `creation_uncertain`：先在飞书核实，找到已创建表后用 `init --url` 绑定，**禁止盲目重试创建**。该标记不可直接编辑清除；若未能定位目标，报告并等用户决定。

默认字段按 TrainOps 流程组织：Run ID、项目、流程、任务、实例、实验意图、关键改动、对照 Run、运行状态、起止时间、主指标名/值、指标方向、数据版本、评测协议、Seed、Git Commit、配置引用、产物与日志、评测依据、故障摘要、Agent 建议、下一步，另有人工决策列。主指标值为 number，其余为 text。真实路径只作为引用，不伪造为浏览器可访问 URL。

复用已有表时先检查字段结构；缺列/类型不匹配会明确报错，不删除或修改原列。可映射同类型的真实字段名或 ID：

```bash
$CLI experiment init --json '{"lark":{"fields":{"intent":"研究目的","metric_value":"fld实际指标字段ID"}}}'
```

字段映射只能使用已知逻辑字段（见 `EXPERIMENT_FIELDS` / 命令实现），不得把 agent 字段指向人工维护列；每个映射必须唯一。用户指定的表需有这些实验字段或相应映射，不能把模板 CSV 的 19 列直接当作本项目表结构。

## 运行与巡检闭环

Run 对应**一次实际训练/评测尝试**，不是一次会话或一次巡检。与任务关联，但不复制出另一套计划状态机。普通 `set` / `batch` 不会暗中发飞书请求。

```bash
# 核实 GPU、真正启动训练后，仍按现有流程更新任务；再记录 Run
$CLI set F01-T01 --status running --brief
$CLI experiment start --task F01-T01 --run-id baseline-attempt-1 \
  --json '{"intent":"验证基线","change":"原始配置","git_commit":"实际commit","config_path":"ssh-a:/root/autodl-fs/project/config.lock.yaml"}'

# SSH 巡检后先记真实进度；没可靠百分比则只用 --checked
$CLI set F01-T01 --progress 40 --brief
$CLI experiment update baseline-attempt-1 --json '{"next_step":"继续训练，结束后评测"}'

# 核实进程退出、产物与评测证据后，再标任务完成和记录最终指标
$CLI set F01-T01 --status done --brief
$CLI experiment update baseline-attempt-1 \
  --json '{"status":"done","metric_name":"accuracy","metric_value":0.837,"direction":"max","evidence":"ssh-a:/root/autodl-fs/project/metrics.json","artifacts":"ssh-a:/root/autodl-fs/project/checkpoint.pt","suggestion":"建议复现"}'

# 故障先报告，再记失败/阻塞；重启训练新建 Run，不覆盖旧 attempt
$CLI experiment update baseline-attempt-1 --json '{"status":"failed","error":"已核实的简短故障摘要"}'

# 补同步某个或所有 Run（不启动或重启训练）
$CLI experiment sync baseline-attempt-1
$CLI experiment sync
$CLI experiment show
```

例子是独立操作示例，不能把“完成后改失败”当正常流水线；实际状态一律以观测为准。未提供 `--run-id` 时自动生成 UUID；显式 ID 可跨会话复用，重复 start 同任务同 ID 返回旧 Run，不启动新 attempt、不应用新 JSON。一个任务同时只能有一个 running/blocked Run；恢复用 update，重跑先结束旧 Run，再 start 新 ID。

`start` / `update` **先原子本地落盘，再尝试同步**；`local_saved: true` 与 `ok: false` 表示本地已保存但飞书未成功，退出码为 1，不能误报为全部成功。即使还没完成飞书配置也允许先留本地记录。`show` 会标出 `pending`，`sync` 可补录。更新只合并明确提供的字段，`null` 才清空；身份字段不可改，已结束 Run 不能重新转 running/blocked。结束后可补充可信评测结果。

允许增量字段：`intent/change/baseline/status/metric_name/metric_value/direction/dataset_version/eval_protocol/seed/git_commit/config_path/artifacts/evidence/error/suggestion/next_step/checked_at`。文本字段最多 8000 字符，seed 用文本；方向仅 `max/min`，缺失留空；指标必须为有限数字并有 `metric_name` 和可信 `evidence`。主指标来自真实评测产物，不从滚动训练 loss 推测；Git 有未提交修改时须在配置/产物引用中留实际配置快照及 patch 证据，不能声称 commit 足以复现。

每次 SSH 巡检后更新关键变化并补同步。仅 checked_at/版本变化不触发飞书写入，不按 step 刷表。训练结束但评测未完成时指标留空；缺数据不补零。首版不自动计算改善率或替换 baseline；不可比或方向/协议未知时不下改善结论，Agent 建议与人工决策分开。

## 同步、恢复与安全优先级

- 只使用 `lark-cli base +...` shortcut，通过参数数组与超时执行；写前核实字段及类型。JSON 是顶层字段映射，不套旧 API 的 `fields` 外壳。
- `+record-upsert` 没有业务键去重：不带 record ID 必定创建。同步始终先按 Run ID 精确查询，零条才创建、一条带真实 ID 更新、多条报冲突，不随意覆盖。分页必须读完。
- 记录保存目标表、record ID、内容指纹与发送版本；切换表不能复用旧表的 record ID。创建响应丢失后先查再更新，避免重复行。相同业务内容跳过写入。
- 同项目同步串行；网络不持有计划或数据写锁。同步期间有新观测，旧响应只确认旧快照，新内容仍待同步。不承诺跨机器并发唯一性，重复记录需核实处理。
- 人工决策列不在自动投影中，连默认“待确认”也不反复写。记录只传脱敏摘要与必要引用，原始 CLI stderr/鉴权 JSON 不落盘。
- 新会话：先 `show --brief` 并 SSH 核实进程/产物，再 `experiment show` 恢复旧 Run 和 `sync` 待补录项，不重建表、不盲目重启训练。
- 飞书缺权限/超时/限流：如实报告本地已存、远端待补录，下一次巡检再补；不增加常驻重试线程、不忙循环、不静默升级身份或放宽授权。
- **安全处理优先**：飞书故障不计入训练五次自愈，不阻断故障报告、修复或关机止付；必要时先处置训练与费用，再补记录。面板未开或退出也不影响实验日志 CLI。
