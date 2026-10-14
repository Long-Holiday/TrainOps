import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
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

    def test_running_requires_owner_resource_and_satisfied_deps(self):
        a = ops.add_task(self.plan, "F01", "A", "I01", 10)
        b = ops.add_task(self.plan, "F02", "B", "I02", 10, [a["id"]])
        with self.assertRaisesRegex(ops.PlanError, "owner"):
            ops.set_task(self.plan, a["id"], {"status": "running"}, now=NOW)
        a["status"] = "pending"
        ops.set_task(self.plan, b["id"], {"status": "running"}, "agent-b", NOW)
        with self.assertRaisesRegex(ops.PlanError, "尚未完成"):
            ops.validate(self.plan)

    def test_resource_exclusivity_and_owner(self):
        a = ops.add_task(self.plan, "F01", "A", "I01", 10)
        b = ops.add_task(self.plan, "F02", "B", "I01", 10)
        ops.set_task(self.plan, a["id"], {"status": "running"}, "agent-a", NOW)
        ops.validate(self.plan)
        with self.assertRaisesRegex(ops.PlanError, "认领"):
            ops.set_task(self.plan, a["id"], {"progress": 5}, "agent-b", NOW)
        with self.assertRaisesRegex(ops.PlanError, "运行中不能"):
            ops.set_task(self.plan, a["id"], {"instance": "I02"}, "agent-a", NOW)
        ops.set_task(self.plan, b["id"], {"status": "running"}, "agent-b", NOW)
        with self.assertRaisesRegex(ops.PlanError, "占用"):
            ops.validate(self.plan)

    def test_not_before_and_actual_times(self):
        task = ops.add_task(self.plan, "F01", "A", "I01", 10, not_before=ops.stamp(NOW + timedelta(hours=1)))
        ops.set_task(self.plan, task["id"], {"status": "running"}, "agent-a", NOW)
        with self.assertRaisesRegex(ops.PlanError, "允许开始"):
            ops.validate(self.plan)
        task["not_before"] = None
        ops.set_task(self.plan, task["id"], {"status": "done"}, "agent-a", NOW - timedelta(minutes=1))
        with self.assertRaisesRegex(ops.PlanError, "结束时间"):
            ops.validate(self.plan)

    def test_done_cannot_bypass_running_and_retry_clears_times(self):
        task = ops.add_task(self.plan, "F01", "A", "local", 10)
        with self.assertRaisesRegex(ops.PlanError, "不允许"):
            ops.set_task(self.plan, task["id"], {"status": "done"}, now=NOW)
        ops.set_task(self.plan, task["id"], {"status": "running"}, "agent", NOW)
        ops.set_task(self.plan, task["id"], {"status": "failed"}, "agent", NOW)
        ops.set_task(self.plan, task["id"], {"status": "pending"}, now=NOW)
        for field in ("started_at", "finished_at", "checked_at", "progress", "progress_at"):
            self.assertIsNone(task[field])
        self.assertEqual(task["owner"], "")
        ops.validate(self.plan)

    def test_completed_dependency_cannot_be_reset_under_running_child(self):
        a = ops.add_task(self.plan, "F01", "A", "local", 10)
        b = ops.add_task(self.plan, "F02", "B", "I02", 10, [a["id"]])
        ops.set_task(self.plan, a["id"], {"status": "running"}, "agent-a", NOW - timedelta(minutes=20))
        ops.set_task(self.plan, a["id"], {"status": "done"}, "agent-a", NOW - timedelta(minutes=10))
        ops.set_task(self.plan, b["id"], {"status": "running"}, "agent-b", NOW)
        ops.set_task(self.plan, a["id"], {"status": "pending"}, now=NOW)
        with self.assertRaises(ops.PlanError):
            ops.validate(self.plan)

    def test_bad_schema_and_counters(self):
        for value in (2, True, "1"):
            self.plan["schema_version"] = value
            with self.assertRaises(ops.PlanError):
                ops.validate(self.plan)
        self.plan["schema_version"] = 1
        self.plan["next_instance"] = 1
        with self.assertRaises(ops.PlanError):
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
                    ops.set_task(plan, parent["id"], {"status": "running"}, "a", NOW)
                    ops.set_task(plan, parent["id"], {"status": state}, "a", NOW)
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
        ops.set_task(self.plan, a["id"], {"status": "running"}, "a", NOW - timedelta(minutes=30))
        ops.set_task(self.plan, a["id"], {"progress": 50}, "a", NOW - timedelta(minutes=10))
        first = self.estimate()[a["id"]]["finish"]
        later = ops.forecast(self.plan, NOW + timedelta(minutes=5))[a["id"]]["finish"]
        self.assertEqual(first, ops.stamp(NOW + timedelta(minutes=10)))
        self.assertEqual(later, first)

    def test_overdue_and_100_percent_are_unknown_until_verified(self):
        a = ops.add_task(self.plan, "F01", "A", "I01", 10)
        ops.add_task(self.plan, "F02", "B", "I01", 10)
        ops.set_task(self.plan, a["id"], {"status": "running"}, "a", NOW - timedelta(minutes=15))
        view = self.estimate()
        self.assertIsNone(view[a["id"]]["finish"])
        self.assertTrue(view[a["id"]]["overdue"])
        self.assertIsNone(view["F02-T01"]["finish"])
        ops.set_task(self.plan, a["id"], {"progress": 100}, "a", NOW)
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


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.store = ops.Store(self.project)

    def command(self, *args, success=True):
        result = subprocess.run([sys.executable, str(SCRIPT), "--project", str(self.project), *args],
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return json.loads(result.stdout) if success and result.stdout.startswith("{") else result

    def setup_plan(self):
        self.command("init", "--name", "实验")
        self.command("instance", "add", "--name", "A", "--autodl-id", "test-a", "--ssh-alias", "gpu-a")
        self.command("flow", "add", "--name", "基线")
        self.command("task", "add", "--flow", "F01", "--name", "train", "--instance", "I01", "--minutes", "60")

    def test_end_to_end_commands_and_command_evidence(self):
        self.setup_plan()
        self.command("instance", "set", "I01", "--status", "on", "--observed")
        self.command("task", "set", "F01-T01", "--status", "running", "--owner", "agent-a",
                     "--command", "python train.py --flag", "--session", "tmux:F01-T01")
        self.command("task", "set", "F01-T01", "--owner", "agent-a", "--progress", "20", "--checked")
        view = self.command("show", "--json")
        self.assertEqual(view["tasks"][0]["command"], "python train.py --flag")
        self.assertEqual(view["tasks"][0]["progress"], 20)
        self.command("task", "set", "F01-T01", "--owner", "agent-a", "--status", "done", "--output", "runs/a")
        self.command("flow", "set", "F01", "--name", "改名")
        self.command("monitor", "--schedule-id", "test-cron", "--note", "仅登记")
        final = self.store.read()
        self.assertEqual(final["tasks"][0]["status"], "done")
        self.assertEqual(final["flows"][0]["id"], "F01")
        self.assertEqual(final["monitor"]["schedule_id"], "test-cron")

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

    def test_corrupt_plan_never_overwritten(self):
        self.setup_plan()
        self.store.path.write_text("not-json", encoding="utf-8")
        self.command("flow", "add", "--name", "lost", success=False)
        self.assertEqual(self.store.path.read_text(), "not-json")

    def test_running_mapping_and_unowned_edits_rejected(self):
        self.setup_plan()
        self.command("task", "set", "F01-T01", "--status", "running", "--owner", "a")
        self.command("instance", "set", "I01", "--ssh-alias", "different", success=False)
        self.command("task", "set", "F01-T01", "--progress", "30", success=False)

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

    def test_parallel_claim_only_one_owner_wins(self):
        self.setup_plan()
        processes = [subprocess.Popen([sys.executable, str(SCRIPT), "--project", str(self.project),
                     "task", "set", "F01-T01", "--status", "running", "--owner", f"agent-{i}"],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(8)]
        for process in processes:
            process.communicate(timeout=20)
        self.assertEqual(sum(p.returncode == 0 for p in processes), 1)
        self.assertEqual(self.store.read()["tasks"][0]["status"], "running")

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
        # Single-agent set without --owner or --checked
        self.command("set", "F01-T01", "--status", "running", "--progress", "40", "--note", "epoch 4")
        t1 = self.store.read()["tasks"][0]
        self.assertEqual(t1["owner"], "agent")
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
        for path in ("/.trainops/plan.json", "/../training-monitor.md", "/favicon.ico"):
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
