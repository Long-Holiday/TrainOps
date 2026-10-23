"""实验配置、Run 历史与飞书同步。

与计划共用 `.agent/experiment.yaml`，但只动 config / experiments 两段，不推进计划 revision。
"""

import copy
from contextlib import contextmanager
import hashlib
import json
import math
import re
import subprocess
import uuid

try:
    import fcntl
except ImportError:
    fcntl = None

from trainops_base import *
from trainops_model import default_config, default_state


# 实验日志与计划分段保存；飞书只是索引，不参与调度。
EXPERIMENT_FIELDS = {
    "run_id": ("Run ID", "text"), "project": ("项目", "text"),
    "flow": ("流程", "text"), "task": ("任务", "text"), "instance": ("实例", "text"),
    "intent": ("实验意图", "text"), "change": ("关键改动", "text"),
    "baseline": ("对照 Run", "text"), "status": ("运行状态", "text"),
    "started_at": ("开始时间", "text"), "finished_at": ("结束时间", "text"),
    "metric_name": ("主指标名", "text"), "metric_value": ("主指标值", "number"),
    "direction": ("指标方向", "text"), "dataset_version": ("数据版本", "text"),
    "eval_protocol": ("评测协议", "text"), "seed": ("Seed", "text"),
    "git_commit": ("Git Commit", "text"), "config_path": ("配置引用", "text"),
    "artifacts": ("产物与日志", "text"), "evidence": ("评测依据", "text"),
    "error": ("故障摘要", "text"), "suggestion": ("Agent 建议", "text"),
    "next_step": ("下一步", "text"),
}
EXPERIMENT_FIELD_DESCRIPTIONS = {
    "run_id": "Run 唯一标识；由 experiment start 生成或指定。",
    "project": "实验项目名；同一 Base 内用于区分项目。",
    "flow": "计划中的流程 ID；当前同步只写 ID。",
    "task": "任务 ID · 任务名；对应本地计划的 tasks。",
    "instance": "执行 Run 的实例 ID；当前同步只写 ID。",
    "intent": "本 Run 要验证的假设或目的。",
    "change": "相对基线或上一版的关键改动；只写摘要。",
    "baseline": "用于对比的 Run ID；应尽量填写。",
    "status": "running/done/failed/blocked/cancelled。",
    "started_at": "登记的开始时间（带时区 ISO 8601）；可在 start/update 显式传入真实时间。",
    "finished_at": "登记的结束时间（带时区 ISO 8601）；可在 update 显式传入真实时间。",
    "metric_name": "主评测指标名；建议带单位。",
    "metric_value": "真实评测产物中的主指标值；空 = 未测，不是 0。",
    "direction": "max 或 min；只描述主指标。",
    "dataset_version": "评测所用数据集版本；缺失时不要下跨 Run 可比结论。",
    "eval_protocol": "评测脚本/协议版本；缺失时不要下跨 Run 可比结论。",
    "seed": "训练随机种子；空表示未记录。",
    "git_commit": "代码 commit；有未提交修改时需另留配置快照/patch 证据。",
    "config_path": "配置文件或配置快照路径；Git 不干净时必须填写。",
    "artifacts": "checkpoint、日志、TensorBoard 等路径或链接；只放引用。",
    "evidence": "指标文件、评测命令输出等可信证据路径。",
    "error": "已核实的简短故障摘要；不要贴完整 stderr 或凭据。",
    "suggestion": "Agent 的结论/建议；与人工决策分开填写。",
    "next_step": "下一步动作或待办；结束后也应填写。",
}
RUN_STATUSES = ("running", "done", "failed", "blocked", "cancelled")
RUN_TEXT = {"intent", "change", "baseline", "metric_name", "dataset_version", "eval_protocol",
            "seed", "git_commit", "config_path", "artifacts", "evidence", "error", "suggestion", "next_step"}
RUN_UPDATE = RUN_TEXT | {"status", "metric_value", "direction", "checked_at",
                         "started_at", "finished_at", "extra_metrics"}


def reject_json_constant(value):
    raise ValueError("JSON 不接受非有限数字")


def experiment_object(text):
    try:
        value = json.loads(text, parse_constant=reject_json_constant)
    except (ValueError, TypeError) as exc:
        raise PlanError("实验数据必须是 JSON 对象") from exc
    if not isinstance(value, dict):
        raise PlanError("实验数据必须是 JSON 对象")
    return value


def reject_experiment_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in {"token", "accesstoken", "refreshtoken", "appsecret", "clientsecret",
                              "autodltoken", "authorization", "password", "privatekey", "devicecode"}:
                raise PlanError("实验配置与记录不接受凭据；飞书认证由 lark-cli 管理，AutoDL Token 只走面板")
            reject_experiment_secrets(item)
    elif isinstance(value, list):
        for item in value:
            reject_experiment_secrets(item)


def validate_extra_metrics(metrics, declarations=False):
    reject_experiment_secrets(metrics)
    if not isinstance(metrics, dict):
        raise PlanError("extra_metrics 必须是指标名到数字或 null 的对象")
    for name, value in metrics.items():
        if (not isinstance(name, str) or not name.strip() or name != name.strip()
                or len(name) > 100 or any(ord(c) < 32 or ord(c) == 127 for c in name)):
            raise PlanError("额外指标名必须为 1–100 字符，不含首尾空白或控制字符")
        if declarations and value is not None:
            raise PlanError("配置 extra_metrics 只接受 null 预声明，实际测量值请写入 Run")
        try:
            valid = value is None or (not isinstance(value, bool) and isinstance(value, (int, float))
                                       and math.isfinite(value))
        except OverflowError:
            valid = False
        if not valid:
            raise PlanError("额外指标必须是有限数字或 null")


def validate_experiment_config(config):
    reject_experiment_secrets(config)
    if not isinstance(config, dict) or config.get("schema_version") != CONFIG_VERSION:
        raise PlanError(f"实验配置 schema_version 必须为 {CONFIG_VERSION}，未覆盖原文件")
    if not isinstance(config.get("project"), str) or not config["project"].strip():
        raise PlanError("实验配置缺少项目名")
    target = config.get("lark", {})
    if not isinstance(target, dict):
        raise PlanError("lark 配置必须为对象")
    for key in ("base_token", "table_id", "url"):
        if key in target and (not isinstance(target[key], str) or not target[key].strip()):
            raise PlanError(f"lark.{key} 必须是非空字符串")
    mapping = target.get("fields", {})
    if not isinstance(mapping, dict) or any(key not in EXPERIMENT_FIELDS or not isinstance(val, str)
                                           or not val.strip() for key, val in mapping.items()):
        raise PlanError("字段映射只能将已知实验字段映射为非空的真实字段名或 ID")
    names = [mapping.get(key, name) for key, (name, _) in EXPERIMENT_FIELDS.items()]
    if len(names) != len(set(names)) or "人工决策" in names or any(name.startswith("extra_metrics.") for name in names):
        raise PlanError("字段映射不能重复或映射到人工决策列、额外指标列")
    validate_extra_metrics(config.get("extra_metrics", {}), declarations=True)
    defaults = config.get("defaults", {})
    validate_run_patch(defaults, defaults_only=True)
    return config


def validate_run_patch(patch, defaults_only=False):
    reject_experiment_secrets(patch)
    allowed = {"metric_name", "direction", "dataset_version", "eval_protocol"} if defaults_only else RUN_UPDATE
    if not isinstance(patch, dict):
        raise PlanError("实验数据必须是 JSON 对象")
    unknown = set(patch) - allowed
    if unknown:
        invalid_fields = ", ".join(sorted(repr(key) for key in unknown))
        allowed_fields = ", ".join(sorted(allowed))
        raise PlanError(f"实验记录含未知或不可修改字段: {invalid_fields}；允许字段: [{allowed_fields}]")
    if "extra_metrics" in patch:
        validate_extra_metrics(patch["extra_metrics"])
    for key, value in patch.items():
        if key in RUN_TEXT and value is not None and (not isinstance(value, str) or len(value) > 8000):
            raise PlanError(f"{key} 必须为简短文本或 null（最多 8000 字符）")
        if key == "status" and value not in RUN_STATUSES:
            raise PlanError("实验状态必须是 running/done/failed/blocked/cancelled")
        if key == "direction" and value not in (None, "max", "min"):
            raise PlanError("指标方向必须为 max/min 或 null，不可猜测")
        if key == "metric_value" and value is not None and (isinstance(value, bool)
                or not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise PlanError("主指标必须是有限数字或 null")
        if key in ("checked_at", "started_at", "finished_at") and value is not None:
            date(value)
        if key == "started_at" and value is None:
            raise PlanError("started_at 不能为空")


def validate_experiment_state(state):
    if (not isinstance(state, dict) or state.get("schema_version") != EXPERIMENT_STATE_VERSION
            or not isinstance(state.get("runs"), dict)):
        raise PlanError("实验运行记录格式错误；未覆盖")
    for run_id, record in state["runs"].items():
        if (not isinstance(record, dict) or record.get("run_id") != run_id
                or isinstance(record.get("version"), bool)
                or not isinstance(record.get("version"), int) or record["version"] < 1
                or not isinstance(record.get("sync", {}), dict)
                or record.get("status") not in RUN_STATUSES
                or not all(key in record for key in ("task_id", "started_at", "finished_at"))):
            raise PlanError("实验运行记录损坏；未覆盖")
        validate_run_patch({key: record[key] for key in RUN_UPDATE if key in record})
    return state


class LarkNotStarted(PlanError):
    """CLI 进程根本未启动，确定没有创建远端资源。"""


class ExperimentLog:
    """实验配置与 Run 历史，和计划共用 .agent/experiment.yaml。

    config / experiments 两段由这里维护；任何写都走 store.update()，只改这两段、
    不会推进计划 revision。实验同步另用一把 sync 锁串行化，网络期间不持有文件锁。
    """

    def __init__(self, store):
        self.store = store
        self.directory = store.directory
        self.config_path = store.path
        self.state_path = store.path

    @contextmanager
    def lock(self, name="data"):
        if name == "data":
            with self.store.lock():
                yield
            return
        if fcntl is None:
            raise PlanError("实验日志需要 POSIX 文件锁")
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "experiment-sync.lock").open("a", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PlanError("实验同步已在进行，请稍后补录") from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def config(self):
        document = self.store.read_document()
        if document.get("config") is None:
            raise PlanError("尚无实验配置，请先 experiment init")
        return validate_experiment_config(document["config"])

    def state(self):
        document = self.store.read_document()
        return validate_experiment_state(document.get("experiments", default_state()))

    def save_config(self, config):
        def operation(document):
            document["config"] = validate_experiment_config(config)
        self.store.update(operation)
        return config

    def call(self, command, **options):
        return self.invoke(["base", command], options)

    def invoke(self, command, options):
        argv = ["lark-cli", *command, "--as", "user", "--format", "json"]
        for key, value in options.items():
            argv.extend(["--" + key.replace("_", "-"),
                         json.dumps(value, ensure_ascii=False, allow_nan=False) if isinstance(value, (dict, list)) else str(value)])
        try:
            process = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            raise LarkNotStarted("未找到 lark-cli；请安装并完成用户授权后补同步") from None
        except (subprocess.TimeoutExpired, OSError, UnicodeError):
            raise PlanError("lark-cli 超时或无法执行；远端结果可能不确定，请先核实后补同步") from None
        # 不输出/保存 stderr 或 error JSON：可能含鉴权链接、凭据或用户数据。
        if process.returncode:
            raise PlanError(f"lark-cli 调用失败（退出码 {process.returncode}）；请检查授权、权限或网络后补同步")
        try:
            result = json.loads(process.stdout)
        except (ValueError, TypeError):
            raise PlanError("lark-cli 返回非 JSON；远端结果可能不确定，请先核实") from None
        if not isinstance(result, dict) or result.get("ok") is not True or result.get("code", 0) not in (0, "0"):
            raise PlanError("lark-cli 返回失败；请核实授权、权限与目标表，不会另建表")
        data = result.get("data", result)
        if not isinstance(data, dict):
            raise PlanError("lark-cli 返回数据格式不支持")
        return data

    def metric_names(self, config, state):
        names = set(config.get("extra_metrics", {}))
        for record in state["runs"].values():
            names.update(record.get("extra_metrics", {}))
        return sorted(names)

    def fields(self, config, metrics=()):
        target = config["lark"]
        if not target.get("base_token") or not target.get("table_id"):
            raise PlanError("实验表尚未确定；请 experiment init --url 指定真实表链接")
        actual = []
        offset = 0
        while True:
            result = self.call("+field-list", base_token=target["base_token"], table_id=target["table_id"],
                               offset=offset, limit=200)
            page = result.get("fields", result.get("items"))
            if not isinstance(page, list):
                raise PlanError("无法识别字段列表，未写入飞书")
            actual.extend(page)
            if not result.get("has_more"):
                break
            if not page:
                raise PlanError("字段分页无进展，未写入飞书")
            offset += len(page)
        mapping = target.get("fields", {})
        for key, (name, kind) in EXPERIMENT_FIELDS.items():
            wanted = mapping.get(key, name)
            matches = [field for field in actual if wanted in
                       (field.get("name"), field.get("id"), field.get("field_name"), field.get("field_id"))]
            if len(matches) != 1 or matches[0].get("type") not in (kind, 1 if kind == "text" else 2):
                raise PlanError(f"实验字段 {key} 缺失或类型不兼容，请核对字段映射；不会修改原表")
            actual_name = matches[0].get("name", matches[0].get("field_name", ""))
            if actual_name == "人工决策" or actual_name.startswith("extra_metrics."):
                raise PlanError("字段映射不能指向人工决策列或额外指标列，包括它的字段 ID")

        missing = []
        for metric in metrics:
            name = "extra_metrics." + metric
            matches = [field for field in actual if field.get("name", field.get("field_name")) == name]
            if matches and (len(matches) != 1 or matches[0].get("type") not in ("number", 2)):
                raise PlanError(f"额外指标列 {name} 类型不兼容或重名，未写入飞书")
            if not matches:
                missing.append({"name": name, "type": "number",
                                "description": "额外评测指标；空 = 未测，不是 0。"})
        if missing:
            self.call("+field-create", base_token=target["base_token"], table_id=target["table_id"], json=missing)

    def init(self, url=None, settings=None):
        plan = self.store.read()
        settings = settings or {}
        if set(settings) - {"project", "defaults", "lark", "settings", "extra_metrics"}:
            raise PlanError("init 只接受 project/defaults/lark/settings/extra_metrics 配置")
        with self.lock("sync"):
            document = self.store.read_document()
            config = document.get("config")
            config = copy.deepcopy(validate_experiment_config(config)) if config is not None \
                else default_config(plan["name"])
            for key, value in settings.items():
                if isinstance(value, dict) and isinstance(config.get(key), dict):
                    config[key].update(value)
                else:
                    config[key] = value
            validate_experiment_config(config)
            metrics = self.metric_names(config, validate_experiment_state(document.get("experiments", default_state())))
            if url:
                result = self.call("+url-resolve", url=url)
                config["lark"].update({key: result[key] for key in ("base_token", "table_id") if result.get(key)})
                if not result.get("base_token") or not result.get("table_id"):
                    raise PlanError("链接未解析出 Base 和数据表，请提供含 table ID 的链接；不会猜选或新建")
                config["lark"]["url"] = url
            elif not config["lark"].get("base_token"):
                if config["lark"].get("table_id") or config["lark"].get("url"):
                    raise PlanError("表配置不完整；请用 init --url 解析目标，不会另建表")

                def mark_uncertain(document):
                    state = validate_experiment_state(document.get("experiments", default_state()))
                    if state.get("creation_uncertain"):
                        raise PlanError("上次建表结果不确定；请在飞书核实并用 init --url 绑定，禁止盲目重建")
                    document["config"] = validate_experiment_config(config)
                    state["creation_uncertain"] = True
                    document["experiments"] = state
                self.store.update(mark_uncertain)
                try:
                    result = self.call(
                        "+base-create", name=f"{config['project']} · 实验日志", table_name="Run 记录",
                        fields=[{"name": name, "type": kind,
                                 "description": EXPERIMENT_FIELD_DESCRIPTIONS[key]}
                                for key, (name, kind) in EXPERIMENT_FIELDS.items()]
                        + [{"name": "人工决策", "type": "select", "multiple": False,
                            "options": [{"name": "待评审", "hue": "Orange", "lightness": "Light"},
                                        {"name": "采纳", "hue": "Green", "lightness": "Light"},
                                        {"name": "拒绝", "hue": "Red", "lightness": "Light"},
                                        {"name": "需复跑", "hue": "Blue", "lightness": "Light"},
                                        {"name": "无需决策", "hue": "Gray", "lightness": "Light"}],
                            "description": "仅人工填写，CLI 不同步。"},
                           {"name": "决策人", "type": "user", "multiple": False,
                            "description": "人工决策的负责人；仅人工填写。"},
                           {"name": "决策时间", "type": "datetime",
                            "style": {"format": "yyyy-MM-dd HH:mm"},
                            "description": "人工决策时间；仅人工填写。"},
                           {"name": "创建时间", "type": "created_at",
                            "style": {"format": "yyyy-MM-dd HH:mm"},
                            "description": "系统字段：记录创建时间。"},
                           {"name": "最后更新时间", "type": "updated_at",
                            "style": {"format": "yyyy-MM-dd HH:mm"},
                            "description": "系统字段：记录最后一次被写入的时间。"}]
                        + [{"name": "extra_metrics." + name, "type": "number",
                            "description": "额外评测指标；空 = 未测，不是 0。"} for name in metrics])
                except LarkNotStarted:
                    def clear_uncertain(document):
                        state = validate_experiment_state(document.get("experiments", default_state()))
                        state.pop("creation_uncertain", None)
                        document["experiments"] = state
                    self.store.update(clear_uncertain)
                    raise
                base = result.get("base", result)
                base_token = base.get("base_token") or result.get("base_token") or base.get("app_token")
                table = result.get("table", {})
                table_id = result.get("table_id") or base.get("table_id") or table.get("table_id") or table.get("id")
                if not base_token:
                    raise PlanError("创建结果缺少 Base ID；请核实飞书后用 init --url 绑定")
                config["lark"].update({"base_token": base_token})
                if table_id:
                    config["lark"]["table_id"] = table_id
                remote_url = base.get("url") or result.get("url")
                if remote_url:
                    config["lark"]["url"] = remote_url
                # 即使字段核验失败也保留刚创建的目标，防止下一次重复建表。
                self.save_config(config)
                if not table_id:
                    raise PlanError("已保存 Base ID，但创建结果缺少表 ID；请用 init --url 绑定，不会重复建表")
            self.save_config(config)
            self.fields(config, metrics)

            def finalize(document):
                document["config"] = validate_experiment_config(config)
                state = validate_experiment_state(document.get("experiments", default_state()))
                state.pop("creation_uncertain", None)
                document["experiments"] = state
            self.store.update(finalize)
            return {"config": config, "verified": True, "ok": True}

    def run(self, task_id=None, run_id=None, patch=None):
        patch = patch or {}
        validate_run_patch(patch)

        def operation(document):
            nonlocal run_id
            plan = document["plan"]
            config = document.get("config")
            config = validate_experiment_config(config) if config is not None else {"defaults": {}}
            state = validate_experiment_state(document.get("experiments", default_state()))
            if task_id is not None:
                task = find(plan, "tasks", task_id)
                active = [r for r in state["runs"].values() if r["task_id"] == task_id
                          and r["status"] in ("running", "blocked")]
                if run_id and run_id in state["runs"]:
                    existing = state["runs"][run_id]
                    if existing["task_id"] != task_id:
                        raise PlanError("Run ID 已被另一任务使用")
                    return copy.deepcopy(existing)
                if active:
                    raise PlanError(f"任务已有活动 Run {active[0]['run_id']}；恢复请 update，重跑先结束旧 Run")
                run_id = run_id or uuid.uuid4().hex
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
                    raise PlanError("Run ID 只能使用 1–100 位字母、数字、下划线或连字符")
                record = {"run_id": run_id, "task_id": task_id, "instance": task["instance"],
                          "flow": task["flow"], "task": task["name"], "status": "running",
                          "started_at": stamp(), "finished_at": None,
                          "version": 0, **config.get("defaults", {})}
                state["runs"][run_id] = record
            else:
                if run_id not in state["runs"]:
                    raise PlanError("找不到 Run，请先 start 或核实 ID")
                record = state["runs"][run_id]
                if record["status"] in ("done", "failed", "cancelled") and patch.get("status") in ("running", "blocked"):
                    raise PlanError("已结束 Run 不能重新启动；重跑应新建 Run")
            merged = copy.deepcopy(patch)
            if "extra_metrics" in patch:
                merged["extra_metrics"] = {**record.get("extra_metrics", {}), **patch["extra_metrics"]}
            record.update(merged)
            if (record.get("started_at") and record.get("finished_at")
                    and date(record["finished_at"]) < date(record["started_at"])):
                raise PlanError("结束时间不能早于开始时间")
            if any(value is not None for value in record.get("extra_metrics", {}).values()) and not record.get("evidence"):
                raise PlanError("额外指标需要可信评测 evidence；不从训练 loss 推测")
            if record.get("metric_value") is not None and (not record.get("metric_name") or not record.get("evidence")):
                raise PlanError("主指标需要 metric_name 与可信评测 evidence；不从训练 loss 推测")
            if record["status"] in ("done", "failed", "cancelled") and not record["finished_at"]:
                record["finished_at"] = stamp()
            record["checked_at"] = patch.get("checked_at") or stamp()
            record["version"] += 1
            document["experiments"] = state
            return copy.deepcopy(record)

        return self.store.update(operation)

    def projection(self, config, record):
        values = {key: record.get(key) for key in EXPERIMENT_FIELDS}
        values["project"] = config["project"]
        values["task"] = f"{record['task_id']} · {record['task']}"
        mapping = config["lark"].get("fields", {})
        return {**{mapping.get(key, name): values[key] for key, (name, _) in EXPERIMENT_FIELDS.items()},
                **{"extra_metrics." + key: value for key, value in record.get("extra_metrics", {}).items()}}

    def sync(self, run_id=None):
        reports = []
        with self.lock("sync"):
            document = self.store.read_document()
            if document.get("config") is None:
                raise PlanError("尚无实验配置，请先 experiment init")
            config = validate_experiment_config(document["config"])
            state = validate_experiment_state(document.get("experiments", default_state()))
            if run_id is not None and run_id not in state["runs"]:
                raise PlanError("找不到 Run")
            records = copy.deepcopy([r for key, r in state["runs"].items() if run_id is None or key == run_id])
            try:
                self.fields(config, self.metric_names(config, state))
            except PlanError as exc:
                def save_error(document):
                    latest = validate_experiment_state(document.get("experiments", default_state()))
                    for record in records:
                        latest["runs"][record["run_id"]]["sync"] = {"error": str(exc)}
                    document["experiments"] = latest
                self.store.update(save_error)
                return {"ok": False, "error": str(exc), "runs": [
                    {"run_id": record["run_id"], "status": "pending"} for record in records]}
            target = {key: config["lark"][key] for key in ("base_token", "table_id")}
            for record in records:
                payload = self.projection(config, record)
                fingerprint = hashlib.sha256(json.dumps([target, payload], sort_keys=True, ensure_ascii=False,
                                                        allow_nan=False).encode()).hexdigest()
                previous = record.get("sync", {})
                if previous.get("fingerprint") == fingerprint and not previous.get("error"):
                    reports.append({"run_id": record["run_id"], "status": "unchanged"})
                    continue
                outcome = {"target": target, "sent_version": record["version"]}
                try:
                    found = []
                    offset = 0
                    while True:
                        result = self.call("+record-list", **target, offset=offset, limit=200,
                                           filter_json={"logic": "and", "conditions": [[config["lark"].get("fields", {}).get(
                                               "run_id", "Run ID"), "==", record["run_id"]]]})
                        if "record_id_list" in result:
                            ids = result["record_id_list"]
                            if not isinstance(ids, list) or any(not isinstance(rid, str) or not rid for rid in ids):
                                raise PlanError("无法识别记录 ID 列表，未创建记录")
                            # JSON 矩阵的 data 是单元格二维数组；去重只需服务端精确筛选返回的 ID。
                            page = [{"record_id": rid} for rid in ids]
                        else:
                            page = result.get("records", result.get("items", result.get("data")))
                        if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                            raise PlanError("无法识别记录列表，未创建记录")
                        found.extend(page)
                        if len(found) > 1:
                            raise PlanError("同一 Run ID 存在多条飞书记录，请人工处理冲突")
                        if not result.get("has_more"):
                            break
                        if not page:
                            raise PlanError("记录分页无进展，未创建记录")
                        offset += len(page)
                    options = {**target, "json": payload}
                    if found:
                        record_id = found[0].get("record_id") or found[0].get("id")
                        if not isinstance(record_id, str) or not record_id:
                            raise PlanError("查询结果缺少 record ID，未写入")
                        options["record_id"] = record_id
                    result = self.call("+record-upsert", **options)
                    remote = result.get("record", result)
                    if not isinstance(remote, dict):
                        raise PlanError("无法识别写入结果，请核实后补录")
                    ids = remote.get("record_id_list")
                    if "record_id_list" in remote and (not isinstance(ids, list) or len(ids) != 1
                            or not isinstance(ids[0], str) or not ids[0]):
                        raise PlanError("写入结果 record ID 列表无效，请核实后补录")
                    record_id = (remote.get("record_id") or remote.get("id")
                                 or (ids[0] if ids else None) or options.get("record_id"))
                    if not isinstance(record_id, str) or not record_id or result.get("ignored_fields"):
                        raise PlanError("写入结果缺少 record ID 或字段被忽略，请核实后补录")
                    outcome.update(record_id=record_id, fingerprint=fingerprint, synced_at=stamp())
                    status = "synced"
                except PlanError as exc:
                    outcome["error"] = str(exc)
                    status = "pending"

                def save_outcome(document):
                    latest = validate_experiment_state(document.get("experiments", default_state()))
                    latest["runs"][record["run_id"]]["sync"] = outcome
                    document["experiments"] = latest
                self.store.update(save_outcome)
                reports.append({"run_id": record["run_id"], "status": status,
                                **({"error": outcome["error"]} if "error" in outcome else {})})
        return {"ok": all(r["status"] != "pending" for r in reports), "runs": reports}

    def show(self):
        document = self.store.read_document()
        config = document.get("config")
        config = validate_experiment_config(config) if config is not None else None
        state = validate_experiment_state(document.get("experiments", default_state()))
        records = copy.deepcopy(state["runs"])
        for record in records.values():
            if config is None:
                record["pending"] = True
                continue
            digest = hashlib.sha256(json.dumps([
                {key: config["lark"].get(key) for key in ("base_token", "table_id")},
                self.projection(config, record)], sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
            record["pending"] = record.get("sync", {}).get("fingerprint") != digest or bool(record.get("sync", {}).get("error"))
        return {"config": config, "runs": records,
                "creation_uncertain": state.get("creation_uncertain", False)}


def experiment_dispatch(args, store, log_cls=None):
    log = (log_cls or ExperimentLog)(store)
    if args.action == "init":
        return log.init(args.url, experiment_object(args.json))
    if args.action == "show":
        return log.show()
    if args.action == "sync":
        return log.sync(args.run_id)
    patch = experiment_object(args.json)
    record = log.run(task_id=args.task if args.action == "start" else None, run_id=args.run_id, patch=patch)
    try:
        result = log.sync(record["run_id"])
    except PlanError as exc:
        result = {"ok": False, "error": str(exc)}
    return {"run": record, "local_saved": True, "sync": result, "ok": result["ok"]}


__all__ = [
    "EXPERIMENT_FIELDS", "EXPERIMENT_FIELD_DESCRIPTIONS", "RUN_STATUSES", "RUN_TEXT", "RUN_UPDATE", "LarkNotStarted",
    "ExperimentLog", "experiment_dispatch", "experiment_object", "reject_experiment_secrets",
    "validate_extra_metrics", "validate_experiment_config", "validate_run_patch",
    "validate_experiment_state",
]
