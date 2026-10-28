# Vast.ai 资源与执行

仅用户明确选择在 Vast.ai 上寻找实例时读取。先加载外部 `vastai:vastai` skill，使用其当前命令与参数；本文件补充 TrainOps 的计划、执行和授权边界。该技能须在运行 TrainOps 的宿主中可用，本插件不内置副本、不安装 CLI、不管理平台凭据。

## 搜索与租用

用户须明确选择 AutoDL 实例或在 Vast.ai 寻找；来源不明先询问，已有明确选择不重复询问。选择 Vast.ai 后按训练所需 GPU 型号/数量、单卡显存、CPU/内存、磁盘、镜像与 CUDA 兼容性、地区及用户给定预算搜索；可从项目推断技术需求，未知项说明依据，不擅自放宽硬约束。搜索不等于授权租用，创建前须明确资源数量、规格、磁盘、预算或费用上限及使用范围；已有授权覆盖则直接执行。

只用 PATH 上的 `vastai`，解析输出加 `--raw`；不绕过 CLI 直调 HTTP API。无匹配按外部技能报告 `No offers matched those filters.`，提出具体可放宽条件并等用户选择，不自动跨平台或扩大预算。命令失败如实报告退出码与 stderr，不换 CLI 或静默重试绕过。

尽量先在本地或已有授权环境完成代码、配置与数据准备，再租 GPU；Vast.ai 不套用 AutoDL 的 `power_on_no_gpu`、`switch_mode`、0.5 核/2GB 无卡配置或缺卡 job。确需租用后才能准备的工作，说明准备项并减少空转。创建前核实账户已登记实际 SSH 公钥；按外部技能使用明确的 `--disk N --ssh --direct --cancel-unavail`，记录返回的真实实例 ID，不把 offer ID 当实例 ID。

## 实例、计划与训练

通过 `vastai show instance <id> --raw` 核实 `actual_status`、`intended_status`、`cur_state`、`next_state`，用返回的 SSH 信息核对实际连接目标；`running` 后仍须 SSH 验证 `nvidia-smi`、框架 GPU、环境和数据，才启动训练并登记 running。实例列表使用 `show instances-v1 --limit N`，N 为 1–25，避免交互分页。等待用宿主一次性唤醒并设授权时间边界，不盲目循环；失败状态按外部技能诊断，停止无效轮询，销毁仍受下述授权限制。SSH 失败先取 `vastai logs <id>`，再依据证据修复。

沿用现有账本，不新增 provider / vastai_id 字段或 CLI 参数：`autodl_id` 是兼容字段，Vast.ai 实例用 `vastai-<真实实例ID>` 登记，实例名称标明 Vast.ai，`ssh_alias` 对应已核实的实际目标；调用平台命令时从该映射取真实数字 ID，不能传 `I01` 或带前缀的值，也不能传给 AutoDL MCP。例如：

```json
{"instances": [{"key": "gpu", "name": "Vast.ai 训练机", "autodl_id": "vastai-12345", "ssh_alias": "vast-gpu-a"}]}
```

示例 ID 不得用于实际操作。仅搜索到报价时不登记成已租实例；资源未取得可先保留未分配任务。模式按真实观测登记 `gpu` 或 `none`，不伪造 `no-gpu`。停机与失联仍按真实状态映射 `off` / `unreachable`，不能把平台字符串直接写入账本状态。

训练、本地代码同步、TrainPilot 探针、Run 留痕与巡检仍按 [执行闭环](execution.md)；其中 AutoDL MCP、UUID、开机 job 与无卡切换仅用于 AutoDL，Vast.ai 用本节实例映射与平台状态检查替代。`source /etc/network_turbo` 是 AutoDL 专用初始化，Vast.ai 不执行；按实际镜像和项目环境初始化。

## 存储与 TensorBoard

先核实镜像工作目录、真实挂载、容量与数据保留边界，不假定存在 `/root/autodl-tmp`、`/root/autodl-fs` 或内置 TensorBoard 入口。Vast.ai 不提供跨实例共享卷；数据按版本复制到各实例，或使用用户已配置并授权的外部对象存储，复制完成后核实完整性，不读取半成品，不宣称所有实例自动共享文件。产物按任务/Run 隔离。

项目已有 TensorBoard 配置仍须启用，并核实事件持续写入；事件目录选已核实的充足磁盘，按实际服务端口与映射配置访问，优先 SSH 隧道，不套用 AutoDL 端口或软链规则。凭据不进入计划、Run、命令 JSON 或飞书记录，不新增凭据文件、环境变量管理或计费面板。

## 故障与收尾

故障按 [恢复协议](protocol.md) 先报告、按证据修复，最多 5 次；止付预授权只覆盖停止保留实例：保全产物后执行 `vastai stop instance <id>` 并用 `show instance` 核实已停止，不把 stop 当作释放或全部费用归零。不能沿用 AutoDL `power_off` / `instance_status` 命令；恢复按授权 `start instance` 后重新核实 GPU，不走无卡切换。

完成/取消按 [收尾](execution.md#完成或取消收尾) 清理本任务宿主巡检、等待、重试与复制任务；Vast.ai 没有 AutoDL `job_id` / `cancel_job`，应核对实际记录的平台实例 ID 和操作，清理宿主任务不等于停止实例。没有可接续任务时按已有授权保全后停止；有共享任务先核实冲突。

`destroy` 会永久删除实例及未转存数据，须取得明确销毁授权并验证必要产物已转存；“取消任务并关机”和五次自愈止付只授权 stop，不能据此自动 destroy，即使外部技能建议失败后销毁也遵守此边界。确已授权时必须使用 `vastai destroy instance <id> -y` 并核实结果。停止失败或实例失联时如实说明状态、产物风险及需要的决策，不擅自销毁。最终汇报实例真实状态、产物位置、调度与等待清理结果及未决事项。
