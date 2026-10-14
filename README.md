# TrainOps

给已有 agent 按需附加的**多实例训练管理能力**，不是新的主 agent 角色，也不是自动训练平台。

- 一次实验分配多个 AutoDL 实例，不同流程并行执行。
- 项目本地保存计划：稳定编号、跨流程依赖、认领和实例独占保护。
- 轻量只读 Web 页面：状态、实例占用、预计/实际完成时间、进度、运行记录。
- 训练管理规范：SSH / 代码版本 / 巡检 / 汇报 / 恢复 / 安全关机。
- **零第三方运行依赖**：Python 3.10+，Linux / macOS / WSL，无模型 API、npm、数据库或 CDN。

## 1. 加载能力：不换 agent，不改全局配置

最通用的方式，在任意能读文件并执行命令的 agent 会话中说：

> 请读取 `/你的路径/TrainOps/skills/trainops/SKILL.md` 并启用 TrainOps，保留你原有角色。实验项目是 `/你的路径/my-experiment`。先读取已有计划；当前只加载能力，不操作实例，具体训练任务和授权稍后提供。

完整可复制提示词见 [training-monitor.md](training-monitor.md)。启用后仍须提供真实实例 ID 与 SSH 别名的映射。任务执行由该 agent 现有的 SSH、MCP autodl、MCP trainpilot 和调度工具负责，本包不会自行连接它们。

**支持 Skill 的工具**也可把整个 `skills/trainops/` 目录复制或链接到其项目能力目录，不能只复制 SKILL.md（运行工具和页面也在包里）：

| 工具 | 常见项目目录 | 使用方式 |
|---|---|---|
| Claude Code | `<实验项目>/.claude/skills/trainops/` | 按工具的 Skill 加载机制启用，或直接要求读取 SKILL.md |
| 使用通用 Agent Skills 目录的工具 | `<实验项目>/.agents/skills/trainops/` | 以工具版本的目录发现机制为准 |
| Antigravity / 其他工具 | 使用其实际支持的 Skill 目录 | 不确定版本支持时，直接读取 Markdown，无需替换角色 |

Claude Code 项目内链接示例（替换两个绝对路径，确保目标不存在）：

```bash
mkdir -p /你的路径/my-experiment/.claude/skills
```

```bash
ln -s /你的路径/TrainOps/skills/trainops /你的路径/my-experiment/.claude/skills/trainops
```

能力可在对话中按需加载/停用；**安装目录是否无需重启就能被发现，取决于各工具自身**，本包不保证通用热重载。无需写入 `AGENTS.md` / `CLAUDE.md` / 全局配置。停用不会自动杀进程或关机，须先交代接管方式；新会话重新启用并读取计划即可恢复。

## 2. 先看双实例演示

在 TrainOps 仓库根目录运行：

```bash
python3 skills/trainops/scripts/trainops.py --project examples/demo-project serve
```

打开 **http://127.0.0.1:8765**。演示含两台占位实例、两条并行训练/评估流程和一条汇总流程；汇总依赖两边评估完成。有未知耗时节点，会显示待估。演示时间固定在样例生成时，之后可能显示“超时/需重估”，这是正常的陈旧观测提示。

想用当前时间生成演示，选择**没有现有计划**的新目录：

```bash
python3 skills/trainops/scripts/trainops.py --project /tmp/trainops-fresh-demo demo
```

```bash
python3 skills/trainops/scripts/trainops.py --project /tmp/trainops-fresh-demo serve --port 8766
```

`demo` 从不覆盖现存计划，**不会连接真实实例、启动训练或创建巡检**。页面服务前台运行，Ctrl+C 停止；agent 启动时应使用可管理的本地后台进程并记录 PID，不占住对话。

## 3. 建立自己的项目计划

下面从仓库根目录执行。`--project` 始终是**实验项目目录**，不是 Skill 所在目录。把 `/你的路径/my-experiment` 和实例占位值替换为实际值。文件写入该目录的 `.trainops/plan.json`，读取/展示不会修改计划。

### 初始化并登记实例

```bash
python3 skills/trainops/scripts/trainops.py --project /你的路径/my-experiment init --name "双实例对照实验"
```

```bash
python3 skills/trainops/scripts/trainops.py --project /你的路径/my-experiment instance add --name "基线 GPU" --autodl-id "用户指定的实例A-ID" --ssh-alias "gpu-a"
```

```bash
python3 skills/trainops/scripts/trainops.py --project /你的路径/my-experiment instance add --name "对照 GPU" --autodl-id "用户指定的实例B-ID" --ssh-alias "gpu-b"
```

依次得到 `I01`、`I02`。登记实例不等于开机；状态默认“未观测”，真实检查后使用 `instance set I01 --status on --observed`。

### 建流程、任务和汇合依赖

```bash
python3 skills/trainops/scripts/trainops.py --project /你的路径/my-experiment flow add --name "基线"
```

```bash
python3 skills/trainops/scripts/trainops.py --project /你的路径/my-experiment flow add --name "对照"
```

```bash
python3 skills/trainops/scripts/trainops.py --project /你的路径/my-experiment flow add --name "汇总"
```

```bash
python3 skills/trainops/scripts/trainops.py --project /你的路径/my-experiment task add --flow F01 --name "基线训练" --instance I01 --minutes 120
```

```bash
python3 skills/trainops/scripts/trainops.py --project /你的路径/my-experiment task add --flow F01 --name "基线评估" --instance I01 --minutes 15 --deps F01-T01
```

```bash
python3 skills/trainops/scripts/trainops.py --project /你的路径/my-experiment task add --flow F02 --name "对照训练" --instance I02 --minutes 90
```

```bash
python3 skills/trainops/scripts/trainops.py --project /你的路径/my-experiment task add --flow F02 --name "对照评估" --instance I02 --minutes 15 --deps F02-T01
```

```bash
python3 skills/trainops/scripts/trainops.py --project /你的路径/my-experiment task add --flow F03 --name "比较指标与报告" --instance local --minutes 20 --deps F01-T02,F02-T02
```

训练在两台实例上并行，评估接在各自训练后，汇总等待两边。任务粒度以可独立交接为准，不必把每个 shell 命令都建节点。

### 查看和修改

```bash
python3 skills/trainops/scripts/trainops.py --project /你的路径/my-experiment show
```

```bash
python3 skills/trainops/scripts/trainops.py --project /你的路径/my-experiment show --json
```

```bash
python3 skills/trainops/scripts/trainops.py --project /你的路径/my-experiment serve --port 8765
```

也可直接对 agent 说：

> 将 F02-T02 换到 I01，改为依赖 F01-T02 和 F02-T01，预计耗时 25 分钟。先修改计划，不启动训练。

对应命令：

```bash
python3 skills/trainops/scripts/trainops.py --project /你的路径/my-experiment task set F02-T02 --instance I01 --deps F01-T02,F02-T01 --minutes 25
```

`--deps` **替换整份依赖列表**，不自动追加。想只追加时，agent 必须先读当前依赖并合并；多个 agent 对同一未运行节点的同一字段同时修改时，最后一次有效更新生效，应协调同节点修改。

## 4. 执行、认领和巡检记账

### 认领节点

```bash
python3 skills/trainops/scripts/trainops.py --project /你的路径/my-experiment task set F01-T01 --status running --owner agent-a-session-01
```

CLI 原子检查依赖、not_before、实例独占和 owner。认领后的 running 涵盖该节点的准备/执行阶段，**不证明远程训练已启动**；`started_at` 是节点执行开始。owner 必须是本次会话唯一名称，同一实例上第二个 running 任务会被拒绝。

实际启动并核实后记录（以下也是示例，不会执行 `--command` 的内容）：

```bash
python3 skills/trainops/scripts/trainops.py --project /你的路径/my-experiment task set F01-T01 --owner agent-a-session-01 --command "python train.py --config configs/base.yaml" --commit "实际提交SHA" --session "tmux:F01-T01" --log "runs/F01-T01/train.log" --output "runs/F01-T01" --checked
```

### 记录真实巡检

```bash
python3 skills/trainops/scripts/trainops.py --project /你的路径/my-experiment task set F01-T01 --owner agent-a-session-01 --progress 35 --checked --note "已核实 GPU 占用、进程与持续更新的日志"
```

默认约每 30 分钟巡检，重大节点即时报告。agent 必须通过其真实调度工具创建任务，核实会话/过期限制，再登记：

```bash
python3 skills/trainops/scripts/trainops.py --project /你的路径/my-experiment monitor --schedule-id "真实调度ID" --expires-at "2026-10-21T18:00:00+08:00" --note "项目巡检；此处填写实际生命周期限制及 owner"
```

**monitor 只是记账，不会创建定时器**。页面每 10 秒读取本地 JSON，也不会 SSH 巡检、续期调度或通知。缺少持续调度能力时，agent 必须明确说出限制，而非声称后台监控已建立。

### 完成、失败和恢复

确认目标进程退出、产物/检查点可用后：

```bash
python3 skills/trainops/scripts/trainops.py --project /你的路径/my-experiment task set F01-T01 --owner agent-a-session-01 --status done --checked --output "已核实的产物目录"
```

状态：`pending / running / blocked / done / failed / cancelled`。不能直接 pending → done；人工跳过用 cancelled，但取消不满足下游依赖。重跑失败/完成/取消节点先设 pending；依赖它的 running/done 节点存在时会拒绝重置，应先处理下游影响。运行中不能直接更换实例、owner 或依赖，先核实进程停止、原 owner 停止操作后再标 blocked。完整流程见 [操作协议](skills/trainops/references/protocol.md)。

恢复时读取同一计划并核实远程进程，不重新 init，不把记录丢失误当“从未启动”。共享实例是否关机以跨流程计划和远程实际工作共同判断，不只看当前任务结束。

## 5. CLI 速查

所有命令前置 `python3 <能力包>/scripts/trainops.py --project <实验目录>`，参数帮助使用 `--help`。

| 命令 | 作用 |
|---|---|
| `init --name ...` | 建空计划，已有文件拒绝覆盖 |
| `instance add --name ... --autodl-id ... --ssh-alias ...` | 实例映射，编号 I01 起 |
| `instance set I01 --status on --observed --note ...` | 记录真实实例观测，支持改标签 |
| `flow add --name ...` / `flow set F01 --name ...` | 增加/改名流程，不改编号 |
| `task add --flow F01 --name ... --instance I01 --minutes 60 --deps ...` | 增加节点 |
| `task set F01-T01 ...` | 增量修改字段、认领、状态、观测和证据 |
| `show` / `show --json` | 文本或 JSON，含实时派生时间 |
| `monitor --schedule-id ... --expires-at ... --note ...` | 登记外部真实巡检调度信息 |
| `serve --port 8765` | 只监听 127.0.0.1，只读页面/API |
| `demo` | 在无计划目录生成演示，不执行远程操作 |

任务可用选项：`--name`、`--instance I01/local/none`、`--minutes 60/'?'`、`--deps "编号,编号"`、`--not-before "带时区时间"/none`、`--status`、`--owner`、`--progress 0..100`、`--checked`、`--note`、`--command`、`--commit`、`--session`、`--log`、`--output`。

清空：`--deps ""`、`--instance none`、`--minutes '?'`、`--not-before none`；字符串证据用空字符串清空。时间必须带时区，比如 `2026-10-14T18:00:00+08:00`。没有硬删除命令，取消保留编号。

## 6. 时间估算和边界

- 根据总耗时、not_before、实际开始、带采样时间的进度、依赖和同实例队列估算；不同实例并行，本地节点不占 GPU。
- 同实例队列按依赖就绪时间、编号确定顺序；需要明确调序就添加依赖，不靠改名或表格排序。
- 未分配实例、未知耗时、失败/取消/阻塞依赖显示待估，不当作零；运行超出预测要求重估。进度 100% 不自动标 done。
- `estimate.ready` 只是本地依赖/时间/已登记占用就绪，不证明实例开机或远程无其他任务。
- 未记录的开机等待/传输等不在 ETA 中，应建节点或计入耗时。进度推算假设速度大致稳定，不能当准确承诺。
- 文件锁保护读取—修改—校验—写入，原子替换避免半文件；损坏/未知版本文件拒绝覆盖。owner 是协作标记，不是权限认证。
- 仅支持**同一台机器、本地文件系统**的共享计划；不同机器/网络盘不是分布式调度。原生 Windows 用 WSL。
- 默认本机服务无登录，不要代理到公网/监听共享地址；计划可能包含私有路径/命令，不写密钥。服务不提供修改/执行接口，也不暴露项目目录。

## 文件与验证

```text
training-monitor.md                  简洁通用启用提示
skills/trainops/SKILL.md              能力入口
skills/trainops/references/protocol.md 多实例操作协议
skills/trainops/scripts/trainops.py    标准库计划 CLI / 只读 HTTP
skills/trainops/web/index.html         无构建中文页面
examples/demo-project/.trainops/plan.json 演示计划
tests/test_trainops.py                标准库测试
```

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

覆盖依赖环、稳定编号、状态与资源约束、并行/串行/未知时间估算、原子保存、并发更新/认领、CLI 和只读 HTTP。真实 AutoDL/SSH/MCP 巡检由使用该能力的 agent 在实际任务授权后核验，本仓库测试不操作 GPU。
