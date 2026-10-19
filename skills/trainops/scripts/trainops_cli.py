"""argparse 命令树。参数值解析在 trainops_specs，业务分派留在入口模块。"""

import argparse

from trainops_base import *
from trainops_specs import dependencies, disk_arg, duration, interval_arg, money_arg, nullable, progress_arg


def parser():
    root = argparse.ArgumentParser(description="TrainOps：本地训练计划账本与执行队列面板")
    root.add_argument("--version", action="version", version=f"%(prog)s schema {SCHEMA_VERSION}")
    root.add_argument("-p", "--project", default=".", help="使用项目目录，不是能力包目录")
    commands = root.add_subparsers(dest="operation_name", required=True)
    experiment = commands.add_parser("experiment", help="实验项目配置、Run 记录与飞书同步")
    actions = experiment.add_subparsers(dest="action", required=True)
    setup = actions.add_parser("init", help="复用指定/已有表；未指定则自动创建")
    setup.add_argument("--url", help="真实飞书表链接（包含 table ID）")
    setup.add_argument("--json", default="{}", help="合并 project/defaults/lark/settings 非敏感配置")
    actions.add_parser("show", help="读取项目配置、运行历史和待同步状态")
    start = actions.add_parser("start", help="登记一次实际运行并尝试同步；重跑新建 Run")
    start.add_argument("--task", required=True)
    start.add_argument("--run-id", help="可选稳定 Run ID，恢复同 ID 不重复创建")
    start.add_argument("--json", default="{}", help="实验意图、改动、评测依据等 JSON 对象")
    update = actions.add_parser("update", help="增量更新 Run 并尝试同步；不改计划")
    update.add_argument("run_id")
    update.add_argument("--json", required=True)
    retry = actions.add_parser("sync", help="补同步指定或全部 Run；相同内容不写入")
    retry.add_argument("run_id", nargs="?")
    init = commands.add_parser("init", help="新建计划（可同时批量传入实例、流程、任务）")
    init.add_argument("--name")
    init.add_argument("--instance", action="append", default=[], help="名称:autodl_id:ssh_alias")
    init.add_argument("--flow", action="append", default=[], help="流程名称")
    init.add_argument("--task", action="append", default=[], help="F01:任务名[:实例[:分钟数[:依赖]]]")
    batch = commands.add_parser("batch", help="单次命令批量新增实例/流程/任务或更新状态")
    batch.add_argument("--instance", action="append", default=[], help="名称:autodl_id:ssh_alias")
    batch.add_argument("--flow", action="append", default=[], help="流程名称")
    batch.add_argument("--task", action="append", default=[], help="F01:任务名[:实例[:分钟数[:依赖]]]")
    batch.add_argument("--set", dest="updates", action="append", default=[], help="F01-T01:状态[:进度[:备注]]")
    batch.add_argument("--dep", dest="dep_updates", action="append", default=[],
                       help="任务ID=依赖列表（逗号分隔；右侧为空/none/- 清空），可重复")
    for target in (init, batch):
        target.add_argument("--from-json", metavar="PATH|-", help="结构化 JSON；- 从标准输入读取，不与简写字段混用")
        target.add_argument("--dry-run", action="store_true", help="仅校验和预览，不写文件；编号不保留")
    apply = commands.add_parser(
        "apply", help="创建或更新计划（只写计划账本，不执行训练）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=("一次创建或增量修改计划：\n"
                     "  无计划 → 按 JSON 新建（必须有 name，不能带 updates）\n"
                     "  有计划 → 按 batch 语义原子新增/更新（name 可省略；若提供必须与当前名称一致，不会改名）\n"
                     "key 只在本次请求内生效，不按 key 持久匹配：重复提交新增项会再次创建，已有对象用 updates。\n"
                     "只记录计划，不执行或停止训练，也不改 config/experiments/wallet。"),
        epilog=("JSON 键：name（仅新建生效）、instances、flows、tasks、updates；\n"
                "字段、别名与校验规则同 init/batch --from-json。\n"
                "--dry-run 不创建目录、锁或文件，编号不保留。"))
    apply.add_argument("--from-json", metavar="PATH|-", required=True,
                       help="结构化 JSON；- 从标准输入读取")
    apply.add_argument("--dry-run", action="store_true", help="仅校验和预览，不写文件；编号不保留")
    demo = commands.add_parser("demo", help="在空目录生成演示计划，无远程操作")
    demo.add_argument("--force", action="store_true", help="覆盖已有计划（仅演示数据，慎用）")
    check = commands.add_parser("check", help="执行结构性检查（依赖拓扑、实例分配与耗时就绪情况）")
    check.add_argument("--strict", action="store_true", help="存在结构问题时以退出码 1 结束，便于脚本判断")
    show = commands.add_parser("show", help="读取最新计划和时间估算")
    show_output = show.add_mutually_exclusive_group()
    show_output.add_argument("--json", action="store_true")
    show_output.add_argument("--brief", action="store_true", help="只看当前行动摘要，分组限量并注明省略数")
    serve = commands.add_parser("serve", help="仅本机面板（可写处是登记时价与 Token），Ctrl+C 停止")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--interval", type=interval_arg, default=None,
                       help="顺带在这个进程里跑自动结算（分钟）；不传则只做面板")
    instance = commands.add_parser("instance").add_subparsers(dest="action", required=True)
    instance_add = instance.add_parser("add")
    instance_add.add_argument("--name", required=True)
    instance_add.add_argument("--autodl-id", required=True)
    instance_add.add_argument("--ssh-alias", required=True)
    instance_set = instance.add_parser("set")
    instance_set.add_argument("id")
    instance_set.add_argument("--name")
    instance_set.add_argument("--autodl-id")
    instance_set.add_argument("--ssh-alias")
    instance_set.add_argument("--status", choices=INSTANCE_STATUSES)
    instance_set.add_argument("--mode", type=nullable, default=argparse.SUPPRESS,
                              help="开机模式：gpu 有卡 / no-gpu 无卡准备（含持续请求等待）；none 表示未观测")
    instance_set.add_argument("--note")
    instance_set.add_argument("--observed", action="store_true", help="记录本次真实观测时间")
    instance_set.add_argument("--disk", type=disk_arg, default=argparse.SUPPRESS,
                              help="如 \"data:40/50,fs:5/200\"：逐盘合并，none 清空该盘")
    flow = commands.add_parser("flow").add_subparsers(dest="action", required=True)
    flow_add = flow.add_parser("add")
    flow_add.add_argument("--name", required=True)
    flow_set = flow.add_parser("set")
    flow_set.add_argument("id")
    flow_set.add_argument("--name", required=True)
    task = commands.add_parser("task").add_subparsers(dest="action", required=True)
    task_add = task.add_parser("add")
    task_add.add_argument("--flow", required=True)
    task_add.add_argument("--name", required=True)
    add_task_args(task_add, is_set=False)
    task_set = task.add_parser("set")
    task_set.add_argument("id")
    task_set.add_argument("--name")
    add_task_args(task_set, is_set=True)
    quick_set = commands.add_parser("set", help="task set 的简写命令")
    quick_set.add_argument("id")
    quick_set.add_argument("--name")
    add_task_args(quick_set, is_set=True)
    wallet = commands.add_parser("wallet", help="账户余额：手工登记（自动结算由面板进程负责）")
    wallet_actions = wallet.add_subparsers(dest="action", required=True)
    wallet_set = wallet_actions.add_parser("set", help="面板没开或需要人工核对时登记余额")
    wallet_set.add_argument("--assets", type=money_arg, default=argparse.SUPPRESS, help="余额（元）；none 清空")
    wallet_set.add_argument("--voucher", type=money_arg, default=argparse.SUPPRESS, help="代金券（元）；单独展示不计入可用时长")
    for target in (init, batch, apply, instance_add, instance_set, flow_add, flow_set, task_add,
                   task_set, quick_set, wallet_set):
        target.add_argument("--brief", action="store_true", help="输出紧凑文本回执，不改变保存及退出码语义")
    return root



def add_task_args(target, is_set=False):
    default = argparse.SUPPRESS if is_set else None
    target.add_argument("--instance", type=nullable, default=default, help="I01 / local / none（- 同 none）")
    target.add_argument("--minutes", dest="duration_minutes", type=duration, default=default, help="预计耗时；? 表示待估")
    target.add_argument("--deps", type=dependencies, default=default, help="逗号分隔；空字符串清空")
    target.add_argument("--not-before", type=nullable, default=default, help="带时区时间；none 清空")
    if is_set:
        target.add_argument("--status", choices=STATUSES)
        progress = target.add_mutually_exclusive_group()
        progress.add_argument("--progress", type=progress_arg, help="百分比 0–100 或 当前迭代数/总迭代数，如 800/7500")
        progress.add_argument("--iter", dest="iteration", type=int, help="当前迭代数，须同时提供 --max-iter")
        target.add_argument("--max-iter", type=int, help="总迭代数，与 --iter 配合使用")
        target.add_argument("--checked", action="store_true", help="记录实际巡检时间")
        target.add_argument("--note")


__all__ = ["parser", "add_task_args"]
