# AutoDL 配置

仅配置存储、TensorBoard、余额结算或面板时读取；异常恢复见 [协议](protocol.md)。设 `CLI="python3 <技能目录>/scripts/trainops.py -p <实验项目目录>"`。

## 三盘与用量

| 路径 | 保留边界 | 内容 |
|---|---|---|
| `/` 系统盘 | 关机保留，随镜像保存；容量小且与系统共用 | 代码、conda 环境、配置脚本 |
| `/root/autodl-tmp` 数据盘 | 快；关机保留，不随镜像保存，释放实例即丢 | 数据集、checkpoint、TensorBoard 事件、高 IO 临时产物 |
| `/root/autodl-fs` 文件存储 | 多实例共享，不受开关机/保存镜像影响 | 最终权重、评测结果、需共享或长期保留的数据 |

释放实例前先将保留产物同步到文件存储或下载本地，并核实转存结果。不要把临时产物长期堆在按量计费的文件存储。

```bash
ssh <别名> 'df -h / /root/autodl-tmp /root/autodl-fs'
$CLI instance set I01 --disk "sys:12/30,data:40/50,fs:5/200" --brief
```

用量单位 GB。--disk 逐盘合并，没提到的盘不动；`data:none` 清空该盘。用量达到 90% 时 show 的盘行带 `!`，check/show --brief 有提醒；先核实、清理或扩容再开任务。满盘处置见 [协议](protocol.md#磁盘处置)。

## TensorBoard

事件文件写数据盘，用软链接入内置读取目录，不要写满系统盘：

```bash
TB=/root/autodl-tmp/tf-logs/<项目名>
mkdir -p "$TB" /root/tf-logs
ln -sfn "$TB" /root/tf-logs/<项目名>
# 每个任务独立 run，例如 "$TB/F01-T02"
tensorboard --logdir "$TB"  # 确需独立服务时，由调用方管理后台进程
```

换 logdir 前先定位并核实本项目 TensorBoard 进程，按授权停止后重启；不要批量杀掉所有 TensorBoard。自定义服务可用 `--port 6007` 从 AutoPanel 进入；需要公网直链时使用 `--port 6006 --host 0.0.0.0`（平台映射 6006/6008），确认用户允许对外访问。计划不记录 TensorBoard 路径/链接，保存在远端脚本中。

## 余额与定时结算

Token 放 TrainOps 仓库根 `.env`（模板 `.env.example`，建议 chmod 600）。优先级 `--token` > 环境变量 `AUTODL_TOKEN` > `.env`；`TRAINOPS_ENV` 可指定其他文件。Token 只进请求头，不写进计划、输出或异常文本。

```bash
$CLI instance set I01 --price 1.88 --brief     # 元/小时；登记标价不等于真实观测
$CLI monitor --once --brief                   # 一轮余额刷新与结算
$CLI monitor --once --strict --brief          # 最近结算失败时退出 1；无 Token 不算失败
$CLI wallet fetch --brief                     # 只刷新余额，不重算结算
$CLI wallet set --assets 128.50 --voucher 20 --brief  # 断网时人工登记，汇报须注明
```

- 余额与代金券分别保存；可用时长只按现金算。缺时价、耗时或余额时少给对应结论，不补零、不猜数。官方余额接口使用千分之一元整数，换算由 CLI 完成，agent 不重复计算。
- monitor --once 的紧凑回执只显示一套结算结论、余额观测信息和错误，不回显内部哈希/重复字段。--brief 只用于 --once；默认 JSON 输出保持不变。
- 结算结论不超过 2 个周期时使用该快照；结算后计划变化会标明 changed，而不是悄悄当作新计划。超过 2 个周期回退实时值并注明过期。不要同时汇报快照与实时两套计划费用。
- 联网失败只写 last_error、保留上一份结论，不打断循环；处理后用 monitor --once 重算。余额不足时提醒用户充值或调整计划，不擅自停训练。恢复细节见 [协议](protocol.md#结算恢复)。

长任务从下列方式选一种定期结算，避免重复常驻；配置一次后不必主 agent 每轮重读/复述：

```bash
$CLI monitor --interval 30                    # 前台循环，调用方管理后台会话/日志
$CLI monitor --expires-at <带时区ISO时间>      # 到点自动退出；none 清空
$CLI serve --port 8765 --interval 30           # 面板进程内顺带结算
```

也可交给系统 cron/定时器调用 `monitor --once --strict --brief`。定时器只保证结算；monitor 不做 SSH 巡检、不执行训练、不自动关机，主 agent 的真实巡检和用户约定汇报不能被替代。正常巡检简报即可，完成/失败、异常风险、需决策时及时说明；不要每轮贴完整计划或训练日志。

## 只读面板

`serve --port 8765` 在前台监听 http://127.0.0.1:8765；需要持续运行时由调用方管理后台进程，不会自动后台化或登记 PID。页面展示甘特图、预算与结算，不展示磁盘条或 TensorBoard 链接。

不要在仓库演示项目上启动 monitor 或 serve --interval，否则会改写版本库快照；真实实验用自己的项目目录。
