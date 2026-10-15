import copy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

OPEN = build_opener(ProxyHandler({})).open

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/trainops/scripts/trainops.py"
spec = importlib.util.spec_from_file_location("trainops", SCRIPT)
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)
NOW = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)


def base():
    plan = ops.new_plan("测试")
    ops.add_instance(plan, "GPU A", "test-a", "gpu-a")
    ops.add_instance(plan, "GPU B", "test-b", "gpu-b")
    ops.add_flow(plan, "基线")
    ops.add_flow(plan, "对照")
    return plan


# 开发机可能在仓库根放了自己的 .env（里面有真 token）：子进程一律把凭据来源指到不存在的文件，
# 需要 .env 的用例再显式传入临时文件，绝不读到开发者的真 token。
NO_ENV_FILE = str(ROOT / "tests" / "no-such-env-file")


def run_cli(project, *args, success=True, env=None):
    """跑一次 CLI 子进程，返回 CompletedProcess；success 用来声明期望的退出码是否为 0。"""
    environment = {**os.environ, "TRAINOPS_ENV": NO_ENV_FILE, "AUTODL_TOKEN": "", **(env or {})}
    result = subprocess.run([sys.executable, str(SCRIPT), "--project", str(project), *args],
                            text=True, capture_output=True, timeout=20, env=environment)
    if (result.returncode == 0) != success:
        raise AssertionError(f"退出码 {result.returncode}（期望 {'0' if success else '非 0'}）: "
                             f"{result.stdout}{result.stderr}")
    return result


def run_json(project, *args, success=True, env=None):
    result = run_cli(project, *args, success=success, env=env)
    return json.loads(result.stdout) if success and result.stdout.startswith("{") else result


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.plan = base()

    def test_stable_ids_after_rename_cancel_and_new_task(self):
        a = ops.add_task(self.plan, "F01", "A", "I01", 10)
        ops.set_task(self.plan, a["id"], {"name": "new", "status": "cancelled"}, now=NOW)
        b = ops.add_task(self.plan, "F01", "B", "I02", 20)
        self.assertEqual(a["id"], "F01-T01")
        self.assertEqual(b["id"], "F01-T02")
        ops.validate(self.plan)

    def test_cross_flow_dependencies_and_cycle(self):
        ops.add_task(self.plan, "F01", "A", "I01", 10)
        ops.add_task(self.plan, "F02", "B", "I02", 10, ["F01-T01"])
        ops.validate(self.plan)
        self.plan["tasks"][0]["deps"] = ["F02-T01"]
        with self.assertRaisesRegex(ops.PlanError, "环"):
            ops.validate(self.plan)

    def test_unknown_self_and_duplicate_deps(self):
        task = ops.add_task(self.plan, "F01", "A", "I01", 10)
        for deps in (["F09-T01"], ["F01-T01"], ["F02-T01", "F02-T01"]):
            task["deps"] = deps
            with self.subTest(deps=deps), self.assertRaises(ops.PlanError):
                ops.validate(self.plan)

    def test_invalid_numbers_and_dates(self):
        task = ops.add_task(self.plan, "F01", "A", "I01", 10)
        for value in (-1, float("nan"), float("inf"), True, "ten"):
            task["duration_minutes"] = value
            with self.subTest(value=value), self.assertRaises(ops.PlanError):
                ops.validate(self.plan)
        task["duration_minutes"] = 0
        ops.validate(self.plan)
        for value in ("2026-10-14", "2026-10-14T10:00:00", "invalid", 123):
            task["not_before"] = value
            with self.subTest(value=value), self.assertRaises(ops.PlanError):
                ops.validate(self.plan)
        task["not_before"] = "2026-10-14T10:00:00+08:00"
        task["progress"] = 101
        with self.assertRaises(ops.PlanError):
            ops.validate(self.plan)

    def test_duplicate_instance_mapping(self):
        for key in ("autodl_id", "ssh_alias"):
            plan = copy.deepcopy(self.plan)
            plan["instances"][1][key] = plan["instances"][0][key]
            with self.assertRaisesRegex(ops.PlanError, "重复"):
                ops.validate(plan)
        self.plan["instances"][0]["ssh_alias"] = "-oProxyCommand=bad"
        with self.assertRaises(ops.PlanError):
            ops.validate(self.plan)

    def test_running_requires_resource_and_satisfied_deps(self):
        a = ops.add_task(self.plan, "F01", "A", "I01", 10)
        b = ops.add_task(self.plan, "F02", "B", "I02", 10, [a["id"]])
        c = ops.add_task(self.plan, "F01", "C", None, 10)
        ops.set_task(self.plan, a["id"], {"status": "running"}, now=NOW)
        ops.validate(self.plan)
        ops.set_task(self.plan, a["id"], {"status": "cancelled"}, NOW)
        ops.set_task(self.plan, a["id"], {"status": "pending"}, NOW)
        ops.set_task(self.plan, c["id"], {"status": "running"}, NOW)
        with self.assertRaisesRegex(ops.PlanError, "实例"):
            ops.validate(self.plan)
        ops.set_task(self.plan, c["id"], {"status": "cancelled"}, NOW)
        ops.set_task(self.plan, c["id"], {"status": "pending"}, NOW)
        ops.set_task(self.plan, b["id"], {"status": "running"}, NOW)
        with self.assertRaisesRegex(ops.PlanError, "尚未完成"):
            ops.validate(self.plan)

    def test_resource_exclusivity_and_running_edit_lock(self):
        a = ops.add_task(self.plan, "F01", "A", "I01", 10)
        b = ops.add_task(self.plan, "F02", "B", "I01", 10)
        ops.set_task(self.plan, a["id"], {"status": "running"}, NOW)
        ops.validate(self.plan)
        with self.assertRaisesRegex(ops.PlanError, "运行中不能"):
            ops.set_task(self.plan, a["id"], {"instance": "I02"}, NOW)
        ops.set_task(self.plan, a["id"], {"progress": 5}, NOW)
        self.assertEqual(a["progress"], 5)
        ops.set_task(self.plan, b["id"], {"status": "running"}, NOW)
        with self.assertRaisesRegex(ops.PlanError, "占用"):
            ops.validate(self.plan)

    def test_not_before_and_actual_times(self):
        task = ops.add_task(self.plan, "F01", "A", "I01", 10, not_before=ops.stamp(NOW + timedelta(hours=1)))
        ops.set_task(self.plan, task["id"], {"status": "running"}, NOW)
        with self.assertRaisesRegex(ops.PlanError, "允许开始"):
            ops.validate(self.plan)
        task["not_before"] = None
        ops.set_task(self.plan, task["id"], {"status": "done"}, NOW - timedelta(minutes=1))
        with self.assertRaisesRegex(ops.PlanError, "结束时间"):
            ops.validate(self.plan)

    def test_done_cannot_bypass_running_and_retry_clears_times(self):
        task = ops.add_task(self.plan, "F01", "A", "local", 10)
        with self.assertRaisesRegex(ops.PlanError, "不允许"):
            ops.set_task(self.plan, task["id"], {"status": "done"}, now=NOW)
        ops.set_task(self.plan, task["id"], {"status": "running"}, NOW)
        ops.set_task(self.plan, task["id"], {"status": "failed"}, NOW)
        ops.set_task(self.plan, task["id"], {"status": "pending"}, now=NOW)
        for field in ("started_at", "finished_at", "checked_at", "progress", "progress_at"):
            self.assertIsNone(task[field])
        ops.validate(self.plan)

    def test_completed_dependency_cannot_be_reset_under_running_child(self):
        a = ops.add_task(self.plan, "F01", "A", "local", 10)
        b = ops.add_task(self.plan, "F02", "B", "I02", 10, [a["id"]])
        ops.set_task(self.plan, a["id"], {"status": "running"}, NOW - timedelta(minutes=20))
        ops.set_task(self.plan, a["id"], {"status": "done"}, NOW - timedelta(minutes=10))
        ops.set_task(self.plan, b["id"], {"status": "running"}, NOW)
        ops.set_task(self.plan, a["id"], {"status": "pending"}, now=NOW)
        with self.assertRaises(ops.PlanError):
            ops.validate(self.plan)

    def test_bad_schema_and_counters(self):
        # 开发阶段不做迁移：任何不等于当前版本的 schema 都直接硬拒并提示备份，绝不猜着读。
        for value in (1, 2, ops.SCHEMA_VERSION + 1):
            self.plan["schema_version"] = value
            with self.subTest(value=value), self.assertRaises(ops.PlanError) as error:
                ops.validate(self.plan)
            self.assertIn("不提供迁移", str(error.exception))
        # 类型都不对的值被更早的守卫拦下：同样是硬拒，但不该说"迁移"
        for value in (True, "1", None):
            self.plan["schema_version"] = value
            with self.subTest(value=value), self.assertRaises(ops.PlanError):
                ops.validate(self.plan)
        self.plan["schema_version"] = ops.SCHEMA_VERSION
        self.plan["next_instance"] = 1
        with self.assertRaises(ops.PlanError):
            ops.validate(self.plan)

    def test_monitor_block_is_validated_leniently(self):
        monitor = self.plan["monitor"]
        ops.validate(self.plan)  # 全新计划的 monitor 段必须合法
        for value in (0, 1441, "30", True):
            monitor["interval_minutes"] = value
            with self.subTest(interval=value), self.assertRaises(ops.PlanError):
                ops.validate(self.plan)
        monitor["interval_minutes"] = ops.INTERVAL_LIMITS[1]
        for key in ("last_run", "last_ok", "expires_at"):
            monitor[key] = "2026-10-14 10:00"  # 不带时区
            with self.subTest(key=key), self.assertRaises(ops.PlanError):
                ops.validate(self.plan)
            monitor[key] = None
        monitor["token_ready"] = "yes"
        with self.assertRaises(ops.PlanError):
            ops.validate(self.plan)
        monitor["token_ready"] = False
        # 结算快照：缺数据的键可以没有，但有的值必须可信（NaN 会让 json 落盘直接炸）
        monitor["estimate"] = {"computed_at": ops.stamp(NOW), "revision": 1}
        ops.validate(self.plan)
        for key in ("burn_rate", "hours_awake", "plan_gpu_hours", "plan_cost"):
            monitor["estimate"][key] = float("nan")
            with self.subTest(key=key), self.assertRaises(ops.PlanError):
                ops.validate(self.plan)
            del monitor["estimate"][key]
        monitor["estimate"]["covers_plan"] = "true"
        with self.assertRaises(ops.PlanError):
            ops.validate(self.plan)
        monitor["estimate"]["covers_plan"] = None
        del monitor["estimate"]["computed_at"]
        with self.assertRaises(ops.PlanError):
            ops.validate(self.plan)
        monitor["estimate"] = "oops"
        with self.assertRaises(ops.PlanError):
            ops.validate(self.plan)

    def test_price_and_disk_bounds(self):
        item = self.plan["instances"][0]
        for value in (-1, 0, "1.88", float("nan"), True):
            item["price"] = value
            with self.subTest(price=value), self.assertRaises(ops.PlanError):
                ops.validate(self.plan)
        item["price"] = None
        ops.validate(self.plan)
        for value in (-1, "40", float("inf")):
            item["disk"]["data_used_gb"] = value
            with self.subTest(disk=value), self.assertRaises(ops.PlanError):
                ops.validate(self.plan)
        item["disk"]["data_used_gb"] = None
        item["disk"]["data_used_gb"], item["disk"]["data_total_gb"] = 60, 50
        with self.assertRaisesRegex(ops.PlanError, "大于总容量"):
            ops.validate(self.plan)


class ForecastTests(unittest.TestCase):
    def setUp(self):
        self.plan = base()

    def estimate(self):
        ops.validate(self.plan)
        return ops.forecast(self.plan, NOW)

    def test_parallel_instances_and_serial_lane(self):
        ops.add_task(self.plan, "F01", "A", "I01", 60)
        ops.add_task(self.plan, "F02", "B", "I02", 30)
        ops.add_task(self.plan, "F02", "C", "I01", 10)
        view = self.estimate()
        self.assertEqual(view["F01-T01"]["finish"], ops.stamp(NOW + timedelta(minutes=60)))
        self.assertEqual(view["F02-T01"]["finish"], ops.stamp(NOW + timedelta(minutes=30)))
        self.assertEqual(view["F02-T02"]["start"], view["F01-T01"]["finish"])

    def test_dependency_scheduled_before_lower_id_on_same_lane(self):
        ops.add_task(self.plan, "F01", "child", "I01", 10, ["F02-T01"])
        ops.add_task(self.plan, "F02", "parent", "I01", 30)
        view = self.estimate()
        self.assertEqual(view["F02-T01"]["start"], ops.stamp(NOW))
        self.assertEqual(view["F01-T01"]["start"], ops.stamp(NOW + timedelta(minutes=30)))

    def test_join_waits_for_both_flows(self):
        ops.add_task(self.plan, "F01", "A", "I01", 60)
        ops.add_task(self.plan, "F02", "B", "I02", 30)
        ops.add_task(self.plan, "F01", "join", "local", 10, ["F01-T01", "F02-T01"])
        self.assertEqual(self.estimate()["F01-T02"]["finish"], ops.stamp(NOW + timedelta(minutes=70)))

    def test_unknown_duration_propagates_dependency_and_lane(self):
        ops.add_task(self.plan, "F01", "unknown", "I01", None)
        ops.add_task(self.plan, "F01", "child", "I02", 10, ["F01-T01"])
        ops.add_task(self.plan, "F02", "same lane", "I01", 10)
        ops.add_task(self.plan, "F02", "other lane", "I02", 10)
        view = self.estimate()
        self.assertIsNone(view["F01-T01"]["finish"])
        self.assertIsNone(view["F01-T02"]["finish"])
        self.assertIsNone(view["F02-T01"]["finish"])
        self.assertIsNotNone(view["F02-T02"]["finish"])

    def test_failed_cancelled_blocked_are_not_completed_deps(self):
        for state in ("failed", "cancelled", "blocked"):
            with self.subTest(state=state):
                plan = base()
                parent = ops.add_task(plan, "F01", "A", "I01", 10)
                ops.add_task(plan, "F02", "B", "I02", 10, [parent["id"]])
                if state == "failed":
                    ops.set_task(plan, parent["id"], {"status": "running"}, NOW)
                    ops.set_task(plan, parent["id"], {"status": state}, NOW)
                else:
                    ops.set_task(plan, parent["id"], {"status": state}, now=NOW)
                ops.validate(plan)
                self.assertIsNone(ops.forecast(plan, NOW)["F02-T01"]["finish"])

    def test_not_before_and_unassigned(self):
        ops.add_task(self.plan, "F01", "A", "I01", 10, not_before=ops.stamp(NOW + timedelta(hours=2)))
        ops.add_task(self.plan, "F02", "B", None, 10)
        view = self.estimate()
        self.assertEqual(view["F01-T01"]["start"], ops.stamp(NOW + timedelta(hours=2)))
        self.assertFalse(view["F01-T01"]["ready"])
        self.assertIsNone(view["F02-T01"]["finish"])
        self.assertFalse(view["F02-T01"]["ready"])

    def test_running_progress_is_sampled_not_recomputed_from_now(self):
        a = ops.add_task(self.plan, "F01", "A", "I01", 120)
        ops.set_task(self.plan, a["id"], {"status": "running"}, NOW - timedelta(minutes=30))
        ops.set_task(self.plan, a["id"], {"progress": 50}, NOW - timedelta(minutes=10))
        first = self.estimate()[a["id"]]["finish"]
        later = ops.forecast(self.plan, NOW + timedelta(minutes=5))[a["id"]]["finish"]
        self.assertEqual(first, ops.stamp(NOW + timedelta(minutes=10)))
        self.assertEqual(later, first)

    def test_overdue_and_100_percent_are_unknown_until_verified(self):
        a = ops.add_task(self.plan, "F01", "A", "I01", 10)
        ops.add_task(self.plan, "F02", "B", "I01", 10)
        ops.set_task(self.plan, a["id"], {"status": "running"}, NOW - timedelta(minutes=15))
        view = self.estimate()
        self.assertIsNone(view[a["id"]]["finish"])
        self.assertTrue(view[a["id"]]["overdue"])
        self.assertIsNone(view["F02-T01"]["finish"])
        ops.set_task(self.plan, a["id"], {"progress": 100}, NOW)
        self.assertIsNone(self.estimate()[a["id"]]["finish"])

    def test_local_tasks_parallel_and_ready_is_not_auto_started(self):
        ops.add_task(self.plan, "F01", "A", "local", 10)
        ops.add_task(self.plan, "F02", "B", "local", 10)
        view = self.estimate()
        self.assertEqual(view["F01-T01"]["start"], view["F02-T01"]["start"])
        self.assertTrue(view["F01-T01"]["ready"])
        self.assertEqual(self.plan["tasks"][0]["status"], "pending")

    def test_demo_snapshot(self):
        plan = ops.demo_plan(NOW)
        ops.validate(plan)
        view = ops.snapshot(plan, NOW)
        self.assertEqual(len(view["tasks"]), 7)
        self.assertTrue(all("estimate" in t for t in view["tasks"]))
        self.assertFalse(any("estimate" in t for t in plan["tasks"]))


class RepoSnapshotTests(unittest.TestCase):
    """仓库里提交的演示快照必须和 demo_plan() 结构一致，防止测试与演示数据悄悄分叉。"""

    def test_committed_demo_snapshot_matches_demo_plan(self):
        committed = json.loads((ROOT / "examples/demo-project/.trainops/plan.json").read_text(encoding="utf-8"))
        fresh = ops.demo_plan(NOW)
        fields = ("id", "flow", "name", "instance", "deps", "duration_minutes", "status", "progress", "note")
        hint = ("演示快照已过期：python3 skills/trainops/scripts/trainops.py "
                "-p examples/demo-project demo --force")
        self.assertEqual([[t[f] for f in fields] for t in committed["tasks"]],
                         [[t[f] for f in fields] for t in fresh["tasks"]], hint)
        # 时价与三盘用量也是演示数据的一部分：不比对就看不到它们的漂移（时间戳除外）。
        instance_fields = ("id", "name", "status", "note", "price", "disk")
        self.assertEqual([{f: i[f] for f in instance_fields} for i in committed["instances"]],
                         [{f: i[f] for f in instance_fields} for i in fresh["instances"]], hint)
        self.assertEqual({k: v for k, v in committed["wallet"].items() if k != "observed_at"},
                         {k: v for k, v in fresh["wallet"].items() if k != "observed_at"}, hint)
        self.assertEqual([f["name"] for f in committed["flows"]],
                         [f["name"] for f in fresh["flows"]], hint)
        self.assertEqual(committed["schema_version"], ops.SCHEMA_VERSION, hint)
        # 演示快照带一份自动结算记录，面板因此开箱就能看到结算徽标：比对静态字段，
        # 时间戳与指纹每次都不同，只比对结论的键集合（键必须和实时口径一致）。
        monitor_fields = ("interval_minutes", "expires_at", "last_error", "token_ready")
        self.assertEqual({f: committed["monitor"][f] for f in monitor_fields},
                         {f: fresh["monitor"][f] for f in monitor_fields}, hint)
        self.assertEqual(sorted(committed["monitor"]["estimate"]),
                         sorted(fresh["monitor"]["estimate"]), hint)


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.store = ops.Store(self.project)

    def command(self, *args, success=True, env=None):
        return run_json(self.project, *args, success=success, env=env)

    def setup_plan(self):
        self.command("init", "--name", "实验")
        self.command("instance", "add", "--name", "A", "--autodl-id", "test-a", "--ssh-alias", "gpu-a")
        self.command("flow", "add", "--name", "基线")
        self.command("task", "add", "--flow", "F01", "--name", "train", "--instance", "I01", "--minutes", "60")

    def test_end_to_end_commands(self):
        self.setup_plan()
        self.command("instance", "set", "I01", "--status", "on", "--observed")
        self.command("task", "set", "F01-T01", "--status", "running")
        self.command("task", "set", "F01-T01", "--progress", "20", "--checked")
        view = self.command("show", "--json")
        self.assertEqual(view["tasks"][0]["progress"], 20)
        self.assertIsNotNone(view["tasks"][0]["checked_at"])
        self.assertNotIn("owner", view["tasks"][0])
        self.command("task", "set", "F01-T01", "--status", "done")
        self.command("flow", "set", "F01", "--name", "改名")
        final = self.store.read()
        self.assertEqual(final["tasks"][0]["status"], "done")
        self.assertEqual(final["flows"][0]["id"], "F01")

    def test_show_marks_ready_overdue_and_stale(self):
        self.setup_plan()
        self.command("instance", "add", "--name", "B", "--autodl-id", "test-b", "--ssh-alias", "gpu-b")
        self.command("task", "set", "F01-T01", "--status", "done")
        # 依赖已完成 + 实例空闲 = 可开工（实例必须已分配，未分配的任务只提示补实例）
        self.command("task", "add", "--flow", "F01", "--name", "评估", "--instance", "I01",
                     "--deps", "F01-T01", "--minutes", "15")
        self.command("task", "add", "--flow", "F01", "--name", "长跑", "--instance", "I02", "--minutes", "60")
        # 直接写入一个 3 小时前开始、2 小时前最后巡检的 running 任务：既超期也未巡检
        plan = self.store.read()
        long_run = plan["tasks"][2]
        plan["tasks"][2] = long_run | {"status": "running", "progress": 20, "deps": [],
                                       "started_at": ops.stamp(ops.utcnow() - timedelta(hours=3)),
                                       "progress_at": ops.stamp(ops.utcnow() - timedelta(hours=3)),
                                       "checked_at": ops.stamp(ops.utcnow() - timedelta(hours=2))}
        self.store.write(plan)
        text = self.command("show").stdout
        self.assertIn("可开工", text)
        self.assertIn("已超期", text)
        self.assertIn("未巡检", text)
        self.assertIn("[running 20% 已超期 未巡检]", text)

    def test_clear_fields_and_rejected_edit_is_atomic(self):
        self.setup_plan()
        self.command("task", "add", "--flow", "F01", "--name", "child", "--instance", "local", "--deps", "F01-T01")
        self.command("task", "set", "F01-T02", "--deps", "", "--instance", "none", "--minutes", "?",
                     "--not-before", "none", "--note=--instance")
        task = self.store.read()["tasks"][1]
        self.assertEqual(task["deps"], [])
        self.assertIsNone(task["instance"])
        original = self.store.path.read_bytes()
        self.command("task", "set", "F01-T01", "--deps", "F01-T01", success=False)
        self.assertEqual(original, self.store.path.read_bytes())

    def test_nan_negative_and_duplicate_instance_rejected(self):
        self.setup_plan()
        original = self.store.path.read_bytes()
        for value in ("nan", "inf", "-2"):
            self.command("task", "set", "F01-T01", "--minutes", value, success=False)
        self.command("task", "set", "F01-T01", "--progress", "nan", success=False)
        self.command("instance", "add", "--name", "duplicate", "--autodl-id", "test-a", "--ssh-alias", "gpu-new", success=False)
        self.assertEqual(original, self.store.path.read_bytes())

    def test_init_and_demo_never_overwrite_existing(self):
        self.setup_plan()
        original = self.store.path.read_bytes()
        self.command("init", "--name", "overwrite", success=False)
        self.command("demo", success=False)
        self.assertEqual(original, self.store.path.read_bytes())
        # demo --force 是唯一的覆盖入口，且只重置为演示数据
        self.command("demo", "--force")
        plan = self.store.read()
        self.assertEqual((len(plan["tasks"]), plan["revision"]), (7, 1))

    def test_corrupt_plan_never_overwritten(self):
        self.setup_plan()
        self.store.path.write_text("not-json", encoding="utf-8")
        self.command("flow", "add", "--name", "lost", success=False)
        self.assertEqual(self.store.path.read_text(), "not-json")

    def test_running_mapping_locked_but_progress_open(self):
        self.setup_plan()
        self.command("task", "set", "F01-T01", "--status", "running")
        self.command("instance", "set", "I01", "--ssh-alias", "different", success=False)
        self.command("task", "set", "F01-T01", "--progress", "30")
        self.assertEqual(self.store.read()["tasks"][0]["progress"], 30)

    def test_parallel_process_updates_do_not_lose_tasks(self):
        self.setup_plan()
        processes = [subprocess.Popen([sys.executable, str(SCRIPT), "--project", str(self.project),
                     "task", "add", "--flow", "F01", "--name", f"parallel-{i}", "--instance", "local"],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(12)]
        for process in processes:
            out, err = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, out + err)
        plan = self.store.read()
        self.assertEqual(len(plan["tasks"]), 13)
        self.assertEqual(len({t["id"] for t in plan["tasks"]}), 13)
        self.assertEqual(plan["revision"], 16)

    def test_parallel_running_sets_are_serialized(self):
        self.setup_plan()
        processes = [subprocess.Popen([sys.executable, str(SCRIPT), "--project", str(self.project),
                     "task", "set", "F01-T01", "--status", "running"],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(8)]
        for process in processes:
            out, err = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, out + err)
        plan = self.store.read()
        self.assertEqual(plan["tasks"][0]["status"], "running")
        self.assertEqual(plan["revision"], 12)  # 4 次建表 + 8 次更新，一次都没丢

    def test_streamlined_init_batch_and_quick_set(self):
        out = self.command(
            "init", "--name", "极简实验",
            "--instance", "GPU-A:id-a:gpu-a",
            "--instance", "GPU-B:id-b:gpu-b",
            "--flow", "基线", "--flow", "对照",
            "--task", "F01:基线训练:I01:60",
            "--task", "F01:基线评估:I01:15:F01-T01",
            "--task", "F02:对照训练:I02:45:F01-T01",
        )
        self.assertEqual(out["instances"], ["I01", "I02"])
        self.assertEqual(out["flows"], ["F01", "F02"])
        self.assertEqual(len(out["tasks"]), 3)
        # 单条 set 命令即可开工并记录进度
        self.command("set", "F01-T01", "--status", "running", "--progress", "40", "--note", "epoch 4")
        t1 = self.store.read()["tasks"][0]
        self.assertEqual(t1["progress"], 40)
        self.assertIsNotNone(t1["checked_at"])
        check_res = self.command("check")
        self.assertTrue(check_res["ok"])
        # Batch update & direct pending->done transition
        self.command("batch", "--set", "F01-T01:done", "--set", "F02-T01:running:25:ok")
        plan = self.store.read()
        self.assertEqual(plan["tasks"][0]["status"], "done")
        self.assertEqual(plan["tasks"][2]["status"], "running")
        self.assertEqual(plan["tasks"][2]["progress"], 25)
        self.command("set", "F01-T02", "--status", "done")
        self.assertEqual(self.store.read()["tasks"][1]["status"], "done")

    def test_task_fields_stay_minimal(self):
        self.setup_plan()
        self.assertEqual(set(self.store.read()["tasks"][0]), {
            "id", "flow", "name", "instance", "deps", "duration_minutes", "not_before", "status",
            "progress", "started_at", "finished_at", "checked_at", "progress_at", "note"})

    def test_instance_cannot_be_marked_off_while_task_runs(self):
        self.setup_plan()
        self.command("task", "set", "F01-T01", "--status", "running")
        self.command("instance", "set", "I01", "--status", "off", success=False)
        self.command("task", "set", "F01-T01", "--status", "failed")
        self.command("instance", "set", "I01", "--status", "off")

    def test_task_spec_placeholder_skips_middle_fields(self):
        self.setup_plan()
        self.command("batch", "--task", "F01:评估:-:30:F01-T01")
        task = self.store.read()["tasks"][1]
        self.assertIsNone(task["instance"])
        self.assertEqual(task["duration_minutes"], 30)
        self.assertEqual(task["deps"], ["F01-T01"])

    def test_check_issues_fail_and_strict_sets_exit_code(self):
        self.setup_plan()
        self.command("task", "add", "--flow", "F01", "--name", "待估任务", "--instance", "local")
        result = self.command("check")
        self.assertFalse(result["ok"])
        self.assertTrue(any("未设置预计耗时" in item for item in result["issues"]))
        strict = self.command("check", "--strict", success=False)
        self.assertFalse(json.loads(strict.stdout)["ok"])
        self.command("task", "set", "F01-T02", "--minutes", "5", "--deps", "F01-T01")
        self.command("task", "set", "F01-T01", "--status", "done")
        self.command("task", "set", "F01-T02", "--status", "done")
        self.assertTrue(self.command("check")["ok"])

    def test_check_time_gap_is_warning_not_issue(self):
        self.setup_plan()
        self.command("task", "add", "--flow", "F01", "--name", "第二步", "--instance", "local",
                     "--minutes", "5", "--deps", "F01-T01")
        self.command("task", "set", "F01-T01", "--status", "done")
        self.command("task", "set", "F01-T02", "--status", "done")
        plan = self.store.read()
        plan["tasks"][0].update(started_at="2026-10-14T09:50:00+00:00", finished_at="2026-10-14T10:00:00+00:00")
        plan["tasks"][1].update(started_at="2026-10-14T10:30:00+00:00", finished_at="2026-10-14T10:35:00+00:00")
        self.store.write(plan)
        result = self.command("check")
        self.assertTrue(result["ok"])
        self.assertIn("时间空隙", result["warnings"][0])
        # compact() 会丢掉空列表，所以结构完好时输出里没有 issues 键
        self.assertNotIn("issues", result)


class DiskAndPriceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.store = ops.Store(self.project)
        self.command("init", "--name", "实验")
        self.command("instance", "add", "--name", "A", "--autodl-id", "test-a", "--ssh-alias", "gpu-a")

    def command(self, *args, success=True):
        return run_json(self.project, *args, success=success)

    def instance(self):
        return self.store.read()["instances"][0]

    def test_disk_merges_per_mount(self):
        self.command("instance", "set", "I01", "--disk", "data:40/50,fs:5/200")
        item = self.instance()
        self.assertEqual(item["disk"]["data_used_gb"], 40)
        self.assertEqual(item["disk"]["data_total_gb"], 50)
        self.assertEqual(item["disk"]["fs_total_gb"], 200)
        self.assertIsNone(item["disk"]["system_used_gb"])
        self.assertIsNotNone(item["observed_at"])
        self.command("instance", "set", "I01", "--disk", "data:44/50")
        item = self.instance()
        self.assertEqual(item["disk"]["data_used_gb"], 44)
        self.assertEqual(item["disk"]["fs_used_gb"], 5, "没提到的盘不能被静默抹掉")

    def test_disk_none_clears_only_that_mount(self):
        self.command("instance", "set", "I01", "--disk", "sys:12/30,data:40/50")
        self.command("instance", "set", "I01", "--disk", "data:none")
        item = self.instance()
        self.assertIsNone(item["disk"]["data_used_gb"])
        self.assertIsNone(item["disk"]["data_total_gb"])
        self.assertEqual(item["disk"]["system_used_gb"], 12)

    def test_bad_disk_specs_are_rejected_without_writing(self):
        before = self.store.path.read_bytes()
        for spec in ("data:40", "gpu:1/2", "data:40/x", "data:50/40", "data:-1/50", ""):
            with self.subTest(spec=spec):
                self.command("instance", "set", "I01", "--disk", spec, success=False)
                self.assertEqual(before, self.store.path.read_bytes())

    def test_price_is_a_quote_not_an_observation(self):
        self.command("instance", "set", "I01", "--price", "1.88")
        item = self.instance()
        self.assertEqual(item["price"], 1.88)
        self.assertIsNone(item["observed_at"], "时价是标价，不该刷新观测时间")
        for value in ("0", "-1", "nan"):
            with self.subTest(price=value):
                self.command("instance", "set", "I01", "--price", value, success=False)
        self.command("instance", "set", "I01", "--price", "none")
        self.assertIsNone(self.instance()["price"])


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.plan = base()
        for item in self.plan["instances"]:
            item["price"] = 2.0

    def test_hours_use_cash_only_and_skip_unknown_pieces(self):
        self.plan["wallet"].update(assets=100.0, voucher=500.0, observed_at=ops.stamp(NOW))
        view = ops.budget(self.plan, NOW)
        # 500 元代金券不该让 100 元现金变成 300 小时
        self.assertEqual(view["assets"], 100.0)
        self.assertEqual(view["voucher"], 500.0)
        self.assertNotIn("burn_rate", view, "没有开机实例就不该有烧钱速度")
        self.assertNotIn("hours_awake", view)

    def test_burn_rate_needs_every_awake_price(self):
        self.plan["wallet"]["assets"] = 100.0
        self.plan["instances"][0]["status"] = "on"
        self.plan["instances"][1].update(status="on", price=None)
        view = ops.budget(self.plan, NOW)
        self.assertNotIn("burn_rate", view, "缺一台时价时，烧钱速度必须是残缺的而不是缩小")
        self.plan["instances"][1]["price"] = 3.0
        view = ops.budget(self.plan, NOW)
        self.assertEqual(view["burn_rate"], 5.0)
        self.assertEqual(view["hours_awake"], 20.0)
        self.plan["instances"][0]["status"] = "off"
        self.assertEqual(ops.budget(self.plan, NOW)["burn_rate"], 3.0)

    def test_plan_cost_follows_lane_end_and_refuses_partial_sums(self):
        self.plan["wallet"]["assets"] = 100.0
        ops.add_task(self.plan, "F01", "训练", "I01", 120)
        ops.add_task(self.plan, "F02", "对照", "I02", 60)
        view = ops.budget(self.plan, NOW)
        # 两台实例各跑各的：2h×2 元 + 1h×2 元 = 6 元，而不是把耗时相加
        self.assertEqual(view["plan_gpu_hours"], 3.0)
        self.assertEqual(view["plan_cost"], 6.0)
        self.assertTrue(view["covers_plan"])
        unknown = ops.add_task(self.plan, "F01", "待估", "I01", None)
        view = ops.budget(self.plan, NOW)
        self.assertNotIn("plan_gpu_hours", view, "有任务完成时间待估时不能给出部分求和")
        self.assertNotIn("plan_cost", view)
        self.assertNotIn("covers_plan", view)
        self.plan["tasks"][2]["status"] = "cancelled"
        self.plan["tasks"][2].update(started_at=ops.stamp(NOW), finished_at=ops.stamp(NOW))
        self.assertIn("plan_cost", ops.budget(self.plan, NOW))

    def test_short_balance_is_reported_as_not_covering(self):
        self.plan["wallet"]["assets"] = 1.0
        ops.add_task(self.plan, "F01", "训练", "I01", 600)
        view = ops.budget(self.plan, NOW)
        self.assertEqual(view["plan_cost"], 20.0)
        self.assertFalse(view["covers_plan"])


class CheckWarningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.store = ops.Store(self.project)
        self.command("init", "--name", "实验")
        self.command("instance", "add", "--name", "A", "--autodl-id", "test-a", "--ssh-alias", "gpu-a")
        self.command("flow", "add", "--name", "基线")
        self.command("task", "add", "--flow", "F01", "--name", "train", "--instance", "I01", "--minutes", "60")

    def command(self, *args, success=True):
        return run_json(self.project, *args, success=success)

    def test_no_observation_means_no_new_warning(self):
        result = self.command("check")
        self.assertNotIn("warnings", result)

    def test_disk_price_and_coverage_warnings(self):
        self.command("instance", "set", "I01", "--status", "on", "--disk", "data:48/50")
        self.command("wallet", "set", "--assets", "100")
        result = self.command("check")
        text = " ".join(result["warnings"])
        self.assertIn("数据盘已用 48/50G", text)
        self.assertIn("未登记时价", text)
        self.assertNotIn("不足以", text, "缺时价时算不出计划花费，不能凭空说余额不够")
        self.command("instance", "set", "I01", "--price", "2")
        self.command("task", "set", "F01-T01", "--minutes", "6000")
        result = self.command("check")
        text = " ".join(result["warnings"])
        self.assertIn("不足以覆盖", text)
        self.assertNotIn("系统盘", " ".join(result["warnings"]))
        self.assertTrue(result["ok"], "这些只是提醒，不能把结构检查判成失败")

    def test_full_disk_warning_appears_in_show_text(self):
        self.command("instance", "set", "I01", "--disk", "sys:29/30,data:10/50")
        text = self.command("show").stdout
        self.assertIn("系统 29/30G!", text)
        self.assertIn("数据 10/50G", text)
        self.assertIn("盘 ", text)


class WalletAPITests(unittest.TestCase):
    """余额接口用本地假服务验证：成功、各类失败、以及 token 绝不外泄。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.store = ops.Store(self.project)
        ops.Store(self.project).create(ops.demo_plan(NOW))
        self.requests = []
        tests = self

        class Handler(BaseHTTPRequestHandler):
            status = 200
            body = json.dumps({"code": "Success", "data": {"assets": 128500, "voucher_balance": 20000}})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                tests.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                       "body": self.rfile.read(length)})
                payload = self.body.encode()
                self.send_response(self.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.handler = Handler
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.env = {"AUTODL_TOKEN": "secret-token-value",
                    "TRAINOPS_API_BASE": f"http://127.0.0.1:{self.server.server_port}",
                    "TRAINOPS_ENV": NO_ENV_FILE}

    def invoke(self, *args, success=True, env=None):
        return run_cli(self.project, *args, success=success, env={**self.env, **(env or {})})

    def command(self, *args, success=True, env=None):
        result = self.invoke(*args, success=success, env=env)
        return json.loads(result.stdout) if success and result.stdout.startswith("{") else result

    def test_fetch_stores_balance_and_never_leaks_token(self):
        result = self.command("wallet", "fetch")
        self.assertTrue(result["saved"])
        self.assertEqual(self.requests[0]["path"], "/api/v1/dev/wallet/balance")
        self.assertEqual(self.requests[0]["auth"], "secret-token-value", "必须用原始 token，不能加 Bearer 前缀")
        self.assertEqual(self.requests[0]["body"], b"")
        wallet = self.store.read()["wallet"]
        self.assertEqual(wallet["assets"], 128.5)
        self.assertEqual(wallet["voucher"], 20.0)
        self.assertIsNotNone(wallet["observed_at"])
        for leak in (result, self.project.joinpath(".trainops/plan.json").read_text(encoding="utf-8")):
            self.assertNotIn("secret-token-value", json.dumps(leak, ensure_ascii=False))

    def test_dry_run_reports_without_writing(self):
        before = self.store.path.read_bytes()
        result = self.command("wallet", "fetch", "--dry-run")
        self.assertFalse(result["saved"])
        self.assertEqual(result["wallet"]["assets"], 128.5)
        self.assertEqual(before, self.store.path.read_bytes())

    def test_failures_are_reported_and_leave_plan_byte_identical(self):
        cases = ((401, json.dumps({"code": "Fail", "msg": "token 无效"}), "HTTP 401"),
                 (200, json.dumps({"code": "Fail", "msg": "余额查询失败", "request_id": "req-1"}), "余额查询失败"),
                 (200, "not json at all", "JSON"),
                 (200, json.dumps({"code": "Success", "data": {}}), "assets"),
                 (200, json.dumps({"code": "Success", "data": {"assets": 12.345}}), "整数"))
        before = self.store.path.read_bytes()
        for status, body, expected in cases:
            with self.subTest(status=status, body=body[:40]):
                self.handler.status, self.handler.body = status, body
                result = self.invoke("wallet", "fetch", success=False)
                self.assertIn(expected, result.stderr)
                self.assertNotIn("secret-token-value", result.stderr + result.stdout)
                self.assertEqual(before, self.store.path.read_bytes(), "失败时计划必须逐字节不变")

    def test_connection_errors_are_translated_not_raised_raw(self):
        def boom(*args, **kwargs):
            raise URLError(ConnectionRefusedError(111, "Connection refused"))

        original, ops.opener = ops.opener, boom
        try:
            with self.assertRaisesRegex(ops.PlanError, "无法连接"):
                ops.api_post("/api/v1/dev/wallet/balance", "secret-token-value")
        finally:
            ops.opener = original

    def test_unreachable_api_does_not_block_local_edits(self):
        # 接口不可达时：退出码非 0、计划逐字节不变，本地命令照常可用
        before = self.store.path.read_bytes()
        result = self.command("wallet", "fetch", success=False,
                              env={"TRAINOPS_API_BASE": "http://127.0.0.1:9"})
        self.assertTrue(result.stderr.startswith("TrainOps: "), result.stderr)
        self.assertNotIn("secret-token-value", result.stderr)
        self.assertEqual(before, self.store.path.read_bytes())
        self.command("wallet", "set", "--assets", "12.5", "--voucher", "3")
        self.command("flow", "set", "F01", "--name", "改名")
        self.assertIn("改名", self.command("show").stdout)

    def test_wallet_set_manual_path(self):
        self.command("wallet", "set", "--assets", "12.5", "--voucher", "3")
        wallet = self.store.read()["wallet"]
        self.assertEqual((wallet["assets"], wallet["voucher"]), (12.5, 3))
        self.assertIsNotNone(wallet["observed_at"])
        # 不带任何字段时拒绝：既不改数据，也不假装成功
        self.command("wallet", "set", success=False)
        self.assertEqual(self.store.read()["wallet"]["voucher"], 3)
        self.command("wallet", "set", "--assets", "none")
        self.assertIsNone(self.store.read()["wallet"]["assets"])
        self.assertEqual(self.store.read()["wallet"]["voucher"], 3)
        self.command("wallet", "set", "--assets", "1", "--voucher", "none")
        self.assertIsNone(self.store.read()["wallet"]["voucher"])

    def test_missing_token_is_explained_not_crashed(self):
        env = {"AUTODL_TOKEN": "", "TRAINOPS_ENV": str(self.project / "missing.env")}
        result = self.command("wallet", "fetch", success=False, env=env)
        self.assertIn("AUTODL_TOKEN", result.stderr)
        self.assertIn(".env", result.stderr, "报错要直接说清 token 该放哪")
        self.assertEqual(self.requests, [], "没有 token 就不该发出请求")

    def test_env_file_token_permissions_and_precedence(self):
        path = self.project / "env-file"
        path.write_text("# 注释与空行都要忽略\n\nexport AUTODL_TOKEN='from-env-file'\nAUTODL_API=nope\n",
                        encoding="utf-8")
        os.chmod(path, 0o644)
        env = {"AUTODL_TOKEN": "", "TRAINOPS_ENV": str(path)}
        self.command("wallet", "fetch", env=env)
        self.assertEqual(self.requests[-1]["auth"], "from-env-file", "成对引号要剥掉，整行注释要忽略")
        # 权限过松只提醒一次，不阻断
        self.assertIn("chmod 600", self.invoke("wallet", "fetch", env=env).stderr)
        os.chmod(path, 0o600)
        self.assertNotIn("chmod 600", self.invoke("wallet", "fetch", env=env).stderr)
        # 环境变量优先于 .env；--token 优先级最高；两者都不进计划文件
        env["AUTODL_TOKEN"] = "from-environ"
        self.command("wallet", "fetch", env=env)
        self.assertEqual(self.requests[-1]["auth"], "from-environ")
        self.command("wallet", "fetch", "--token", "explicit-token", env=env)
        self.assertEqual(self.requests[-1]["auth"], "explicit-token")
        plan_text = self.store.path.read_text(encoding="utf-8")
        for secret in ("from-env-file", "from-environ", "explicit-token"):
            self.assertNotIn(secret, plan_text)

    def test_env_file_is_parsed_without_stripping_inline_comments(self):
        # token 里可能带 #：只有整行注释才算注释，行内的 # 必须原样保留
        path = self.project / "tricky.env"
        path.write_text('AUTODL_TOKEN=abc#def\n', encoding="utf-8")
        self.assertEqual(ops.load_env(path)["AUTODL_TOKEN"], "abc#def")
        self.assertEqual(ops.load_env(self.project / "nope.env"), {})
        broken = self.project / "broken.env"
        broken.write_bytes(b"AUTODL_TOKEN=\xff\xfe\n")
        with self.assertRaises(ops.PlanError):
            ops.load_env(broken)
        # 别的键写了也不生效：解析层照收，取 token 时只认 AUTODL_TOKEN
        only = self.project / "only.env"
        only.write_text("SOME_OTHER_TOKEN=xxx\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"AUTODL_TOKEN": "", "TRAINOPS_ENV": str(only)}):
            with self.assertRaises(ops.PlanError):
                ops.resolve_token()


class MonitorTests(unittest.TestCase):
    """自动结算：联网在文件锁之外，失败只记录不退出，结论与结算时间写回计划。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.store = ops.Store(self.project)
        # 用真实当前时刻生成演示计划：cycle() 按真实时钟判断"跑没跑完"，固定时刻的计划会整片逾期
        self.store.create(ops.demo_plan())
        self.requests = []
        self.arrived = None
        self.slow = None
        tests = self

        class Handler(BaseHTTPRequestHandler):
            status = 200
            body = json.dumps({"code": "Success", "data": {"assets": 52000, "voucher_balance": 8000}})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                tests.requests.append({"auth": self.headers.get("Authorization"), "body": self.rfile.read(length)})
                if tests.slow is not None:
                    tests.arrived.set()
                    tests.slow.wait(10)  # 卡住联网，用来验证结算没有攥着文件锁
                payload = self.body.encode()
                self.send_response(self.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.env = {"AUTODL_TOKEN": "settle-token",
                    "TRAINOPS_API_BASE": f"http://127.0.0.1:{self.server.server_port}"}

    def command(self, *args, success=True, env=None):
        return run_json(self.project, *args, success=success, env={**self.env, **(env or {})})

    def parse(self, *args):
        return ops.parser().parse_args(["-p", str(self.project), "monitor", *args])

    def in_process(self):
        """测试进程里直接跑 cycle()/run_monitor() 时，凭据与接口地址也得由本用例说了算。"""
        return mock.patch.dict(os.environ, {**self.env, "TRAINOPS_ENV": NO_ENV_FILE})

    def monitor(self):
        return self.store.read()["monitor"]

    def test_once_settles_balance_and_writes_conclusion(self):
        result = self.command("monitor", "--once", "--interval", "15")
        self.assertEqual(result["monitor"]["interval_minutes"], 15)
        self.assertEqual(result["wallet"]["assets"], 52.0)
        self.assertEqual(self.requests[0]["auth"], "settle-token")
        monitor = self.monitor()
        self.assertEqual(monitor["last_error"], "")
        self.assertTrue(monitor["token_ready"])
        self.assertEqual(monitor["last_ok"], monitor["last_run"])
        estimate = monitor["estimate"]
        self.assertEqual(estimate["plan_hash"], ops.plan_fingerprint(self.store.read()))
        for key in ("burn_rate", "hours_awake", "plan_gpu_hours", "plan_cost", "covers_plan"):
            self.assertIn(key, estimate)
        # show 与面板读的就是这份结论，且明确标出结算时间
        self.assertFalse(result["settlement"]["live"])
        self.assertIn("自动结算于", result["settlement"]["text"])
        self.assertIn("自动结算于", self.command("show").stdout)
        # token 绝不外泄：计划文件、stdout、stderr 里都不许出现
        for leak in (result, self.command("show"), self.store.path.read_text(encoding="utf-8")):
            self.assertNotIn("settle-token", json.dumps(leak, ensure_ascii=False, default=str))

    def test_failure_is_recorded_without_killing_the_schedule(self):
        self.command("monitor", "--once")
        good = self.store.read()
        self.handler_status(401, json.dumps({"code": "Fail", "msg": "token 无效"}))
        result = self.command("monitor", "--once")
        self.assertIn("HTTP 401", result["monitor"]["last_error"])
        after = self.store.read()
        monitor = after["monitor"]
        self.assertEqual(monitor["last_ok"], good["monitor"]["last_ok"], "失败不该刷新成功时间")
        self.assertEqual(monitor["estimate"], good["monitor"]["estimate"], "失败必须保留上一份结论")
        # 失败那一轮确实落盘了（revision 前进），只是没被记成成功；last_run 秒级精度，不去比它
        self.assertGreater(after["revision"], good["revision"])
        for key in ("instances", "flows", "tasks", "wallet"):
            self.assertEqual(after[key], good[key], f"失败只能动 monitor 段，不能动 {key}")
        # show 把失败标出来，但结论仍然可读；--strict 让 cron 能告警
        self.assertIn("上次结算失败", self.command("show").stdout)
        self.command("monitor", "--once", "--strict", success=False)

    def handler_status(self, status, body):
        handler = self.server.RequestHandlerClass
        handler.status, handler.body = status, body

    def test_unreachable_api_keeps_process_alive_and_plan_intact(self):
        self.command("monitor", "--once", env={"TRAINOPS_API_BASE": "http://127.0.0.1:9"})
        # 具体文案随环境而异（直连是"无法连接"，经代理是 HTTP 5xx），只要求失败被记下
        self.assertTrue(self.monitor()["last_error"], "连不上接口必须记进 last_error")
        self.assertEqual(self.store.read()["wallet"]["assets"], 128.5, "连不上时钱包保持原值")
        # 本地命令照常可用，计划结构没被破坏
        self.command("flow", "set", "F01", "--name", "改名")
        self.assertIn("改名", self.command("show").stdout)

    def test_missing_token_is_not_a_failure(self):
        env = {"AUTODL_TOKEN": "", "TRAINOPS_ENV": str(self.project / "missing.env")}
        result = self.command("monitor", "--once", env=env)
        self.assertEqual(self.requests, [], "没有 token 就不该发请求")
        # 没配 token 不是故障：compact() 会省掉空的 last_error，这里按"没有错误"读
        self.assertEqual(result["monitor"].get("last_error", ""), "")
        self.assertFalse(result["monitor"]["token_ready"])
        self.assertEqual(self.monitor()["last_error"], "", "计划里也不该留下错误")
        self.assertIn("未配置 Token", result["settlement"]["text"])
        # 结论仍然算得出来（用计划里已有的余额），--strict 也不会因此一直红
        self.assertIsNotNone(result["monitor"]["estimate"])
        self.command("monitor", "--once", "--strict", env=env)

    def test_stale_settlement_falls_back_to_live_values(self):
        self.command("monitor", "--once", "--interval", "1")
        plan = self.store.read()
        plan["monitor"]["estimate"]["computed_at"] = ops.stamp(NOW - timedelta(minutes=10))
        view = ops.settlement(plan, NOW)
        self.assertTrue(view["live"], "超过 2 个周期就回退实时值")
        self.assertIn("已过期", view["text"])
        # 计划改过之后必须标明这份结论不再代表当前计划
        plan["monitor"]["estimate"]["computed_at"] = ops.stamp(NOW)
        self.assertFalse(ops.settlement(plan, NOW)["changed"])
        ops.add_task(plan, "F01", "新任务", "I01", 10)
        view = ops.settlement(plan, NOW)
        self.assertTrue(view["changed"])
        self.assertIn("结算后计划有变更", view["text"])
        # 结算进程自己更新 last_run 不算"计划有变更"，否则会一直误报
        plan["monitor"]["last_run"] = ops.stamp(NOW + timedelta(minutes=1))
        self.assertTrue(ops.settlement(plan, NOW)["changed"], "任务改动才是真改动")
        clean = self.store.read()
        clean["monitor"]["last_run"] = ops.stamp(NOW + timedelta(minutes=1))
        self.assertFalse(ops.settlement(clean, NOW)["changed"])

    def test_interval_bounds_and_expiry_are_enforced(self):
        for value in ("0", "1441", "abc", "-5"):
            with self.subTest(interval=value):
                result = run_cli(self.project, "monitor", "--once", "--interval", value,
                                 success=False, env=self.env)
                self.assertIn("结算周期", result.stderr)
        for value in ("2026-10-14 10:00", "soon"):
            with self.subTest(expires=value):
                result = run_cli(self.project, "monitor", "--once", "--expires-at", value,
                                 success=False, env=self.env)
                self.assertIn("expires-at", result.stderr)

    def test_loop_sleeps_one_interval_and_stops_at_expiry(self):
        class Stop(Exception):
            pass

        slept = []

        def fake_sleep(seconds):
            slept.append(seconds)
            raise Stop

        soon = ops.stamp(ops.utcnow() + timedelta(minutes=1))
        with self.in_process():
            # 下一轮会越过 --expires-at：跑完这一轮就退出，不再睡
            args = self.parse("--interval", "15", "--expires-at", soon)
            self.assertIsNone(ops.run_monitor(self.store, args, sleep=slept.append, monotonic=lambda: 0.0))
            self.assertEqual(slept, [], "快到 --expires-at 就不该再睡")
            self.assertEqual(self.monitor()["expires_at"], soon)
            # 没有到期时间：睡满一个周期（扣掉本轮联网耗时），周期按计划里的值走
            args = self.parse("--interval", "15")
            with self.assertRaises(Stop):
                ops.run_monitor(self.store, args, sleep=fake_sleep, monotonic=lambda: 0.0)
            self.assertEqual(slept, [15 * 60])
            # --expires-at none 清空到期时间，循环继续
            args = self.parse("--expires-at", "none")
            with self.assertRaises(Stop):
                ops.run_monitor(self.store, args, sleep=fake_sleep, monotonic=lambda: 0.0)
            self.assertIsNone(self.monitor()["expires_at"])

    def test_cycle_does_not_hold_the_lock_while_online(self):
        """联网在锁外：慢接口期间本地写照常完成，两边的改动都留得住。"""
        report = {}
        self.arrived = threading.Event()  # 假接口收到请求
        self.slow = threading.Event()     # 由本用例放行，让接口一直卡着
        thread = threading.Thread(target=lambda: report.update(
            ops.cycle(self.store, interval=60, token="settle-token")), daemon=True)
        with self.in_process():
            thread.start()
            self.assertTrue(self.arrived.wait(5), "假接口没被调用")
            self.command("flow", "set", "F01", "--name", "并发改名")  # 联网还没返回就写入
            self.slow.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        final = self.store.read()
        self.assertEqual(final["flows"][0]["name"], "并发改名", "结算不能覆盖并发写")
        self.assertEqual(final["monitor"]["interval_minutes"], 60)
        self.assertEqual(final["wallet"]["assets"], 52.0, "结算自己的结果也要留住")
        self.assertEqual(report["monitor"]["last_error"], "", report)


class MoneyTests(unittest.TestCase):
    def test_milli_to_yuan_rejects_unit_drift(self):
        self.assertEqual(ops.milli_to_yuan(128500, "余额"), 128.5)
        self.assertEqual(ops.milli_to_yuan(0, "余额"), 0)
        for value in (12.5, -1, float("nan"), True, "100", None):
            with self.subTest(value=value), self.assertRaises(ops.PlanError):
                ops.milli_to_yuan(value, "余额")

    def test_balance_amounts_requires_assets(self):
        self.assertEqual(ops.balance_amounts({"data": {"assets": 1000, "voucher_balance": None}}),
                         {"assets": 1.0, "voucher": None})
        for payload in ({}, {"data": None}, {"data": {}}, {"data": {"voucher_balance": 1}}):
            with self.subTest(payload=payload), self.assertRaises(ops.PlanError):
                ops.balance_amounts(payload)

    def test_redirect_with_credentials_is_refused(self):
        with self.assertRaises(ops.PlanError):
            ops.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example/")


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ops.Store(self.temp.name)
        self.store.create(ops.demo_plan(NOW))
        self.server = ops.server(self.store, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def test_page_and_snapshot(self):
        with OPEN(self.url) as response:
            page = response.read().decode()
            self.assertIn("TrainOps", page)
            self.assertIn("gantt", page)
            self.assertIn("progress-line", page)
            self.assertIn("textContent", page)
            self.assertNotIn("innerHTML", page)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        with OPEN(self.url + "/api/plan") as response:
            plan = json.load(response)
            self.assertEqual(len(plan["tasks"]), 7)
            self.assertIn("estimate", plan["tasks"][0])

    def test_arbitrary_paths_and_writes_rejected(self):
        for path in ("/.trainops/plan.json", "/../README.md", "/favicon.ico"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                OPEN(self.url + path)
            self.assertEqual(error.exception.code, 404)
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            with self.subTest(method=method), self.assertRaises(HTTPError) as error:
                OPEN(Request(self.url + "/api/plan", data=b"{}", method=method))
            self.assertEqual(error.exception.code, 405)

    def test_external_host_is_rejected(self):
        request = Request(self.url + "/api/plan", headers={"Host": "external.example"})
        with self.assertRaises(HTTPError) as error:
            OPEN(request)
        self.assertEqual(error.exception.code, 403)

    def test_corruption_is_reported_not_hidden(self):
        self.store.path.write_text("oops")
        with self.assertRaises(HTTPError) as error:
            OPEN(self.url + "/api/plan")
        self.assertEqual(error.exception.code, 500)
        self.assertIn("损坏", json.load(error.exception)["error"])


if __name__ == "__main__":
    unittest.main()
