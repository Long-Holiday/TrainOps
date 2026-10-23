"""`.agent/experiment.yaml` 的加锁读写与整份文档校验。

CLI 与面板经由这里读写；也允许备份后直接编辑这份 JSON 兼容 YAML（保留三段），
但 CLI 仍会在锁内校验整份文档，非法内容不会被自动导入或静默修复。
"""

from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import tempfile

try:
    import fcntl
except ImportError:
    fcntl = None

from trainops_base import *
from trainops_experiments import validate_experiment_config, validate_experiment_state
from trainops_model import new_document, validate


def sync_directory(directory):
    """os.replace 之后 fsync 目录，让改名本身也落盘；个别文件系统不支持时忽略。"""
    try:
        handle = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


def validate_document(document):
    if not isinstance(document, dict) or type(document.get("schema_version")) is not int:
        raise PlanError("项目配置格式/版本不支持；请备份并核对文件，不会自动覆盖")
    if document["schema_version"] != FILE_VERSION:
        raise PlanError(f"项目配置 schema {document['schema_version']} 与当前 {FILE_VERSION} 不符"
                        "（开发阶段不提供迁移）；请备份并核对文件，不会自动覆盖")
    if not isinstance(document.get("plan"), dict):
        raise PlanError("项目配置缺少 plan 段；可先备份，再按 config/plan/experiments 三段修复，或重新 init")
    if "config" not in document or "experiments" not in document:
        raise PlanError("项目配置缺少 config 或 experiments 段；可先备份补齐三段，或重新 init")
    config = document["config"]
    if config is not None:
        validate_experiment_config(config)
    validate_experiment_state(document["experiments"])
    validate(document["plan"])


class Store:
    """`.agent/experiment.yaml` 的加锁读写入口。

    计划、实验配置与 Run 历史在同一个文件里，所以也只有一把文件锁：任何写路径都走
    lock() → 读整份文档 → 只改自己那一段 → 原子替换。计划命令只动 plan 段；
    experiment 命令只动 config/experiments 段，因此不会推进计划的 revision。
    也允许备份后直接编辑：提交前仍会整份校验，非法文档不会被自动导入。
    """

    def __init__(self, project):
        self.root = Path(project).expanduser().resolve()
        self.directory = self.root / ".agent"
        self.path = self.directory / "experiment.yaml"
        self.lock_path = self.directory / "experiment.lock"

    @contextmanager
    def lock(self):
        if fcntl is None:
            raise PlanError("需要 POSIX 文件锁；Windows 请在 WSL 中运行")
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def read_document(self):
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise PlanError("尚无项目配置，请先 init 或在空目录运行 demo") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PlanError(f"项目配置损坏，未覆盖: {exc}") from exc
        if isinstance(document, dict) and "plan" not in document:
            raise PlanError("项目配置缺少 plan 段；可先备份，再按 config/plan/experiments 三段修复，或重新 init")
        validate_document(document)
        return document

    def read(self):
        return self.read_document()["plan"]

    def write_document(self, document):
        validate_document(document)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                             prefix=".experiment-", suffix=".tmp", delete=False) as handle:
                path = Path(handle.name)
                json.dump(document, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(path, self.path)
            sync_directory(self.directory)
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    def write(self, plan):
        """只替换 plan 段；配置与 Run 历史原样保留。"""
        with self.lock():
            document = self.read_document() if self.path.exists() else new_document(plan)
            document["plan"] = plan
            self.write_document(document)

    def create(self, plan, force=False, dry_run=False):
        # 预览不碰 lock()，否则即使不保存也会创建目录和锁文件。
        with nullcontext() if dry_run else self.lock():
            if self.path.exists():
                if not force:
                    raise PlanError("计划已存在，不会覆盖；请使用原计划、指定另一个目录，或对演示数据用 demo --force")
                document = self.read_document()
            else:
                document = new_document(plan)
            plan["revision"] = 0 if dry_run else 1
            document["plan"] = plan
            if dry_run:
                validate_document(document)
            else:
                self.write_document(document)
        return plan

    def apply(self, create, operation, dry_run=False):
        """无计划则创建、已存在则更新；存在性判断与提交在同一把锁内。

        create() 只在文件不存在时调用并返回全新计划；operation(plan, created) 复用
        调用方的两阶段编辑逻辑。dry-run 不碰 lock()、不建目录、不写文件，与
        change()/create() 的预览语义一致。
        """
        if dry_run:
            if self.path.exists():
                document = self.read_document()
                plan = document["plan"]
                created = False
            else:
                plan = create()
                document = new_document(plan)
                created = True
            item = operation(plan, created)
            validate_document(document)
            return {"revision": plan["revision"], "item": item, "created": created}
        with self.lock():
            if self.path.exists():
                document = self.read_document()
                plan = document["plan"]
                plan["revision"] += 1
                created = False
            else:
                plan = create()
                document = new_document(plan)
                plan["revision"] = 1
                created = True
            item = operation(plan, created)
            if not created:
                plan["updated_at"] = stamp()
            self.write_document(document)
            return {"revision": plan["revision"], "item": item, "created": created}

    def update(self, operation):
        """只改 plan 以外的段（config / experiments），不推进计划 revision。"""
        with self.lock():
            document = self.read_document()
            result = operation(document)
            self.write_document(document)
            return result

    def change(self, operation, dry_run=False):
        """预览只读原子快照，不保留编号；真实提交仍在同一把锁里重新读写。"""
        with nullcontext() if dry_run else self.lock():
            document = self.read_document()
            plan = document["plan"]
            # 先自增再执行，让操作读到本次提交的 revision。
            if not dry_run:
                plan["revision"] += 1
            result = operation(plan)
            if dry_run:
                validate_document(document)
            else:
                plan["updated_at"] = stamp()
                self.write_document(document)
            return {"revision": plan["revision"], "item": result}
