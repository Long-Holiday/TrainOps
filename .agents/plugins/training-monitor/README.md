# training-monitor

面向 Antigravity CLI（`agy`）的**模型训练监控者**角色插件。把一台 AutoDL GPU 上的一次训练任务，从开机、同步代码、启动训练，到巡检汇报、保存检查点、关机，整个生命周期交给一个专职角色。

## 提供什么

| 组件 | 路径 | 作用 |
| --- | --- | --- |
| Agent | `agents/training-monitor.md` | 模型训练监控者角色，**仅作为主 agent** 使用（`mainAgent: true` / `subagent: false`） |

通过 `/agents` 面板选择，或用 `agy --agent training-monitor` 启动会话，即由它担任本次对话的主 agent。它不会被 `invoke_subagent` 调用。

## 安装与启用

插件位于本仓库的 `.agents/`（agy 的 workspace customization root），在 TrainOps 目录下会被自动发现。

- **已开启的会话**：新建的插件目录要**重启 `agy`** 才会被发现。
- **启用/停用**：用 `/plugin`，或 `agy plugin enable training-monitor` / `agy plugin disable training-monitor`。发现到的插件默认启用。

## 使用前需要具备

角色依赖以下能力，缺一不可（角色自身会先做可用性检查）：

1. **MCP `autodl`** —— 实例开机、状态检查、关机。
2. **MCP `trainpilot`** —— 汇报任务状态。
3. **SSH 免密别名** —— 连接 GPU 服务器控制训练进程、查日志。

前两项配置在本机全局 `~/.gemini/config/mcp_config.json`。插件**不附带 `mcp_config.json`**：MCP server 名字全局唯一，在插件里重复声明同一个 `autodl` / `trainpilot` 会与用户自己的配置冲突。

## 使用方式

```bash
agy --agent training-monitor
```

或先启动 `agy`，再用 `/agents` 切换。然后直接给任务描述：

```
开机 autodl-gpu-01，拉取 main 分支最新代码，用 configs/exp3.yaml 启动训练并每半小时汇报
```

角色要求实例 ID 与 SSH 别名必须由用户明确给出——上下文里没有时它会先问你，不会猜。

## 关于 `tools` 字段

`agents/training-monitor.md` 必须显式列出工具，**不能省略**。省略该字段时，agent 只拿到只读与通信类工具，无法执行任何命令，SSH / git / 远程 Bash 全部失效——主 agent 与子代理皆然。

工具名必须用 Antigravity 自己的（`view_file`、`run_command`、`grep_search`…），不是 Claude Code 的 `Read` / `Bash`。写错或不存在的名字会让 `agy` 报 `unknown component ... not found in registry` 并拒绝启动。

当前使用的 24 个：

```
view_file  read_file  write_to_file  edit_file  replace_file_content
multi_replace_file_content  run_command  grep_search  find_by_name
list_dir  search_web  read_url_content  ask_question  ask_permission
ask_custom_permission  generate_image  invoke_subagent  define_subagent
manage_task  manage_subagents  send_message  schedule  wait  search_marketplace
```

其中 `list_dir` + `grep_search` 就是取文件搜索的全部手段——Antigravity 没有独立的 glob 工具。

要增删工具，改 `tools` 数组。`agy plugin validate` **不检查**工具名，只有运行时会查：

```bash
agy --agent training-monitor --print="x" 2>&1 | grep 'not found in registry'
```

## 与原始文档的关系

本插件由仓库根目录的 `training-monitor.md` 转换而来。原文档是带 `trigger: always_on` frontmatter 的**常驻规则**——每次对话都生效，工具集跟随主 agent、无需声明。

现在它是**主 agent 角色**：只有被选为主 agent 时才生效，并且必须显式声明工具集。

如果要的是「插件启用即常驻、工具集不受限」的旧行为，把正文原样挪到 `rules/AGENTS.md` 即可——插件内 `rules/` 下的规则在插件启用期间始终生效，且不带 frontmatter，不存在工具白名单问题。两种形态对应不同需求，按需选一。
