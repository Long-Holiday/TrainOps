# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 仓库概览

TrainOps 是给任意 agent 热插拔的**多实例 GPU 训练管理能力包**（面向 AutoDL）：本地计划账本 + 只读甘特图面板 + 技能文档。它本身不跑训练、也不是服务，只负责记录与展示实验计划。

三条硬约束贯穿全仓：

- **零第三方依赖**：只用 Python 标准库；测试用 Node 内置模块（没有 package.json，不装 npm 包）。
- **计划文件只能过 CLI**：`$PROJECT/.trainops/plan.json` 是唯一事实来源，禁止用 Edit/Write 直接改。
- **缺数据就缺结论**：算不出的数字（缺时价、缺耗时、缺余额）一律不显示，绝不用 0 顶替或自行估算。这条口径同时约束 CLI 文本、`/api/plan` 与面板三处。

## 常用命令

```bash
python3 -m unittest discover -s tests -v      # Python 测试（约 30 秒）
node tests/test_web.cjs                       # 面板渲染测试（无 npm 依赖）

# 跑单个用例 / 单个类
python3 -m unittest discover -s tests -k wallet
python3 -m unittest tests.test_trainops.BudgetTests.test_hours_use_cash_only_and_skip_unknown_pieces -v

# 演示面板（自带演示计划与结算记录，不连服务器）
python3 skills/trainops/scripts/trainops.py -p examples/demo-project serve --port 8765
```

CLI 的 `-p/--project` 指向**实验项目目录**，不是本仓库（`--version` 打当前 schema 版本）。

两个容易踩的坑：

- 改了 `demo_plan()` 就要重新生成仓库里的演示快照，否则 `RepoSnapshotTests` 失败：
  `python3 skills/trainops/scripts/trainops.py -p examples/demo-project demo --force`
- 别在 `examples/demo-project` 上跑 `monitor` 或 `serve --interval`：它们会真的改写版本库里的那份快照。

## 架构

数据流是单向的：`plan.json` → CLI（`Store` → `forecast`/`budget`/`settlement`）→ `show` 文本或 `/api/plan` JSON → 面板只渲染。

### `skills/trainops/scripts/trainops.py`（全部逻辑，单文件）

- `Store`：唯一写入口。`fcntl.flock` 排他锁 + 临时文件 `os.replace` + 文件与目录 fsync；所有读改写都走 `store.change(operation)`。
- `validate()`：读取与写入两侧都跑，账本不变量集中在这里——编号与计数器一致性、`TRANSITIONS` 状态机、依赖闭环（Kahn）、`done` ⇒ 进度 100、同一 GPU 实例同时只能有一个 `running`。校验失败只报错，**从不静默修复**；schema 版本不符直接拒绝读（开发阶段无迁移，提示"备份并核对文件"）。
- `forecast()`：排程的唯一事实来源。每个 GPU 实例一条车道，`estimate.start/finish` 由依赖 + 车道 + `not_before` 推出；`running` 任务靠 `progress_at` 采样外推，进度 100% 或已超期即降级为"待估"。
- `budget()` / `estimate_view()` / `settlement()`：共用一套口径。可用时长只按现金余额 `assets` 算，代金券单独展示；结算结论超过 2 个周期未更新就回退为实时值并说明原因，同一屏不出现两套数字。
- `plan_fingerprint()`：用参与计算的字段做哈希，判断"结算之后计划有没有变过"。不要改用 `revision`——monitor 每轮都自增它，会永远显示"有变更"。
- `cycle()`：一轮结算。联网在文件锁**之外**，落盘只走一次 `store.change()`；失败只写 `monitor.last_error`，不打断循环、不动其余数据。
- `Handler`：只读 HTTP，仅 `/`（面板）与 `/api/plan`（`snapshot()`）；写动词一律 405，Host 白名单只放 127.0.0.1/localhost（防 DNS rebinding）。
- 唯一的外部调用是 AutoDL 只读余额接口（POST 无请求体、拒绝重定向以免转发 `Authorization`）。返回值是千分之一元整数，单位对不上就报错，而不是写错 1000 倍。

### `skills/trainops/web/index.html`（单文件面板，无构建步骤）

- **零运行时开销**：只用静态 CSS，不引外部字体/图片/图标库/JS 库/canvas；唯一的动画是 `prefers-reduced-motion` 守卫下运行中状态的脉冲点。改完必须跑 `node tests/test_web.cjs`。
- **面板不做业务计算**：金额、结算整句文案、甘特条几何全部由服务端在 `/api/plan` 里算好，前端只负责打点。条形几何以服务端 `estimate` 为准是硬要求（有测试盯着时间轴、ETA 文案与红色总进度线三者一致）。
- DOM 一律 `createElement` + `textContent`：计划文件里的文本是不可信输入（`test_web.cjs` 有 XSS 用例）。
- 每 10 秒轮询 `/api/plan`，`document.hidden` 时跳过；重绘必须保留各流程甘特的横向滚动位置。

### 文档也是产品

`SKILL.md` 是热插拔入口（外部 agent 读的那份）、`references/protocol.md` 是异常与恢复的按需参考、`README.md` 面向用户。CLI 行为变了，这几处要同步。

## 约定

- 全仓库中文：文档、CLI 输出、`PlanError` 文案（写成可直接执行的建议）、测试断言消息、commit message（中文摘要 + 按子系统分条的 `-` 列表，结尾带 Co-Authored-By）。
- 失败必须原子：被拒绝的编辑不留下半个文件（有测试逐字节比对计划文件）。
- Token 只进请求头：绝不写进计划文件、stdout/stderr 或异常文本。优先级 `--token` > 环境变量 `AUTODL_TOKEN` > 仓库根 `.env`。
- 计划表只记核心字段（状态、进度、实例、依赖、耗时）；命令、日志路径、commit 一律不进计划。
- 新增测试沿用既有约定：子进程一律把 `TRAINOPS_ENV` 指向不存在的文件，绝不读开发机上的真 token。
