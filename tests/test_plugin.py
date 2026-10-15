"""插件清单与入口文件的结构检查。

这些清单不在计划账本的数据流上，CLI 的输出路径碰不到它们，所以单独盯：
各 agent 的清单必须同源同版本、引用的路径必须存在且不越出插件根，
入口文件（AGENTS.md / GEMINI.md / Antigravity 规则）只做指针，不复制细则。

外部校验器（claude / codex / agy）装了就用它验一遍，没装跳过而不是失败。
"""

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills/trainops"
SKILL = SKILL_DIR / "SKILL.md"
SCRIPT = SKILL_DIR / "scripts/trainops.py"

AGENT_PLUGINS_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
ANTIGRAVITY_SCHEMA = "https://antigravity.google/schemas/v1/plugin.json"
# Agent Plugins 1.0.0 的清单是封闭 schema：多一个键就该被发现，而不是被静默忽略。
AGENT_PLUGINS_KEYS = {"$schema", "name", "version", "description", "author", "homepage",
                      "repository", "license", "keywords", "extensions"}
PLUGIN_NAME = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
PLUGIN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ANTIGRAVITY_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")
# Claude Code 的清单键里有这几个是"路径"：相对插件根、必须以 ./ 开头、必须留在插件根内。
CLAUDE_COMPONENT_KEYS = ("skills", "commands", "agents", "hooks", "outputStyles", "workflows")
ENTRY_FILES = (ROOT / "AGENTS.md", ROOT / "GEMINI.md", ROOT / "antigravity/rules/trainops.md")
ENTRY_LIMIT = 40  # 入口文件只做指针，细则留在 SKILL.md；超了说明有人在写第二份契约
# 三条不变量在入口文件里必须逐字一致，否则同一个约束会长出三种说法。
INVARIANTS = ("是唯一事实来源，只经 CLI 修改", "缺数据就缺结论", "改变目标、放弃实验或增加费用前先确认")
# 本机安装的 Claude Code 会提示插件根的 CLAUDE.md 不作为上下文加载：这是预期行为
# （CLAUDE.md 是本仓库的开发约定），不是清单错误；除它以外出现任何警告都要报出来。
KNOWN_CLAUDE_WARNINGS = ("CLAUDE.md at the plugin root",)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def frontmatter(path):
    """只解析 SKILL.md 顶部的简单 frontmatter：这里是 name/description 级别的键值，不引 YAML。"""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise AssertionError(f"{path} 缺少 frontmatter 起始行 ---")
    keys = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return keys
        key, sep, value = line.partition(":")
        if sep:
            keys[key.strip()] = value.strip().strip("'\"")
    raise AssertionError(f"{path} 的 frontmatter 没有结束行 ---")


def slug(heading):
    """按 GitHub 的口径粗算锚点：小写、去掉标点、空格转连字符；中文按原样保留。"""
    text = heading.strip().lower()
    text = re.sub(r"[^\w一-鿿\s-]", "", text)
    return re.sub(r"\s+", "-", text)


def links(path):
    """列出正文里的相对链接（含 #锚点），跳过外部 URL 与代码块里的内容。"""
    body = Path(path).read_text(encoding="utf-8")
    body = re.sub(r"```.*?```", "", body, flags=re.S)
    body = re.sub(r"`[^`]*`", "", body)
    return [target for target in re.findall(r"\]\(([^)\s]+)\)", body)
            if not re.match(r"^[a-z]+://|^mailto:|^#", target)]


class ManifestTests(unittest.TestCase):
    def test_agent_plugins_manifest_is_closed_and_named(self):
        manifest = load(ROOT / "plugin.json")
        extra = set(manifest) - AGENT_PLUGINS_KEYS
        self.assertFalse(extra, f"plugin.json 出现 Agent Plugins 1.0.0 之外的键：{sorted(extra)}")
        self.assertEqual(manifest["$schema"], AGENT_PLUGINS_SCHEMA,
                         "插件根的 $schema 必须精确等于 Agent Plugins 1.0.0 的地址，写错会被拒绝加载")
        name = manifest["name"]
        self.assertRegex(name, PLUGIN_NAME, "插件名要满足 Agent Plugins 的命名规则")
        self.assertLessEqual(len(name), 64, "插件名最长 64 字符")

    def test_skills_directory_only_exposes_immediate_children(self):
        skills = ROOT / "skills"
        self.assertTrue(skills.is_dir(), "Agent Plugins 规定技能放 skills/<名称>/SKILL.md")
        found = sorted(child.name for child in skills.iterdir() if (child / "SKILL.md").is_file())
        self.assertEqual(found, ["trainops"], "skills/ 的直接子目录才是技能，深层目录不会被搜索")
        self.assertTrue((skills / "trainops/SKILL.md").is_file(), "SKILL.md 必须是普通文件而不是链接")

    def test_claude_manifest_paths_stay_inside_plugin_root(self):
        manifest = load(ROOT / ".claude-plugin/plugin.json")
        self.assertRegex(manifest["name"], PLUGIN_ID, "Claude Code 的插件名只能用字母数字与 . _ -")
        for key in CLAUDE_COMPONENT_KEYS:
            for value in (manifest[key] if isinstance(manifest.get(key), list) else [manifest.get(key)]):
                if value is None:
                    continue
                self.assertTrue(value.startswith("./"), f"{key} 的路径必须以 ./ 开头：{value}")
                target = (ROOT / value).resolve()
                self.assertTrue(target.is_relative_to(ROOT), f"{key} 的路径越出了插件根：{value}")
                self.assertTrue(target.exists(), f"{key} 指向的路径不存在：{value}")

    def test_claude_marketplace_entry_matches_manifest(self):
        market = load(ROOT / ".claude-plugin/marketplace.json")
        self.assertTrue(market["name"] and market["owner"]["name"], "market 缺少名称或维护者")
        self.assertTrue(market["plugins"], "market 至少要有本插件这一条")
        manifest = load(ROOT / ".claude-plugin/plugin.json")
        for entry in market["plugins"]:
            self.assertRegex(entry["name"], PLUGIN_ID, "条目名只能用字母数字与 . _ -")
            self.assertTrue(entry["source"].startswith("./"), "相对源必须以 ./ 开头")
            self.assertTrue((ROOT / entry["source"]).resolve().is_dir(), f"源目录不存在：{entry['source']}")
            self.assertEqual(entry.get("version"), manifest["version"],
                             "条目版本与 plugin.json 不一致：官方只给警告，这里直接判失败")

    def test_codex_manifest_declares_skills(self):
        manifest = load(ROOT / ".codex-plugin/plugin.json")
        self.assertRegex(manifest["name"], PLUGIN_NAME, "Codex 清单的插件名要满足同一条命名规则")
        self.assertTrue((ROOT / manifest["skills"]).is_dir(), "Codex 清单里的 skills 路径必须存在")

    def test_gemini_extension_points_at_context_file(self):
        manifest = load(ROOT / "gemini-extension.json")
        self.assertTrue(re.fullmatch(r"[a-z0-9-]+", manifest["name"]), "Gemini 扩展名要小写加连字符")
        self.assertTrue((ROOT / manifest["contextFileName"]).is_file(),
                        "contextFileName 指向的文件必须存在，否则扩展只挂个空壳")

    def test_antigravity_manifest_and_skill_link(self):
        manifest = load(ROOT / "antigravity/plugin.json")
        self.assertEqual(manifest["$schema"], ANTIGRAVITY_SCHEMA,
                         "Antigravity 的 $schema 与 Agent Plugins 不兼容，必须分开两个清单")
        self.assertRegex(manifest["name"], ANTIGRAVITY_NAME)
        skills = ROOT / "antigravity/skills"
        self.assertTrue(skills.is_symlink(), "antigravity/skills 必须是指向 ../skills 的软链，不能复制一份技能")
        self.assertEqual(os.readlink(skills), "../skills", "软链要写相对路径，跟着仓库走")
        self.assertTrue((skills / "trainops/SKILL.md").is_file(), "软链没解析到技能：Windows 下 git 可能把它落成普通文件")

    def test_every_manifest_shares_one_version(self):
        versions = {path: load(ROOT / path).get("version") for path in
                    ("plugin.json", ".claude-plugin/plugin.json", ".codex-plugin/plugin.json",
                     "gemini-extension.json")}
        versions[".claude-plugin/marketplace.json"] = load(ROOT / ".claude-plugin/marketplace.json")["plugins"][0]["version"]
        self.assertEqual(len(set(versions.values())), 1, f"各清单版本号必须逐字相同：{versions}")
        self.assertIsNotNone(versions["plugin.json"], "插件版本不能为空")


class SkillTests(unittest.TestCase):
    def test_entry_keeps_task_startup_order(self):
        """入口要写死开工顺序：起面板 + 默认无卡准备。

        这两步曾整体缺失：agent 装完插件后直接请求有卡开机，用户看不到面板也没有无卡准备。
        删掉任一条都算回归，所以在这里钉住（正文长度预算见 test_trainops 的入口用例）。
        """
        body = SKILL.read_text(encoding="utf-8")
        self.assertIn("## 开工顺序", body, "入口缺少开工顺序，agent 会跳过面板与无卡准备")
        self.assertIn("serve --port 8765 --interval 30", body, "入口必须给出面板启动命令")
        self.assertIn("power_on_no_gpu", body, "入口必须把无卡准备写成默认路线")

    def test_frontmatter_matches_directory_and_limits(self):
        meta = frontmatter(SKILL)
        self.assertEqual(meta["name"], SKILL_DIR.name, "技能名必须与目录名一致，否则客户端找不到")
        self.assertRegex(meta["name"], PLUGIN_NAME, "技能名只允许小写字母数字与单连字符")
        self.assertLessEqual(len(meta["name"]), 64)
        self.assertTrue(meta["description"].strip(), "description 是技能被检索到的唯一依据，不能为空")
        self.assertLessEqual(len(meta["description"]), 1024, "description 上限 1024 字符")

    def test_links_and_anchors_resolve(self):
        for path in [SKILL, *sorted((SKILL_DIR / "references").glob("*.md")), ROOT / "README.md",
                     *ENTRY_FILES]:
            for target in links(path):
                name, _, anchor = target.partition("#")
                target_path = (path.parent / name).resolve() if name else path
                self.assertTrue(target_path.exists(), f"{path.relative_to(ROOT)} 的链接指向不存在的位置：{target}")
                if anchor:
                    headings = re.findall(r"^#{1,6}\s+(.+)$", target_path.read_text(encoding="utf-8"), re.M)
                    self.assertIn(anchor, [slug(h) for h in headings],
                                  f"{path.relative_to(ROOT)} 的锚点找不到对应标题：{target}")

    def test_script_depth_is_stable(self):
        """脚本与仓库根之间固定三层：env_path() 靠这个深度找仓库根 .env，挪动就会静默丢 token。"""
        spec = importlib.util.spec_from_file_location("trainops_layout", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(SCRIPT.resolve().parents[3], ROOT,
                         "trainops.py 必须位于 <仓库根>/skills/trainops/scripts/ 下")
        self.assertEqual(module.env_path(), ROOT / ".env", "默认凭据文件必须是仓库根的 .env")


class EntryFileTests(unittest.TestCase):
    def test_entry_files_are_thin_pointers(self):
        for path in ENTRY_FILES:
            body = path.read_text(encoding="utf-8")
            where = path.relative_to(ROOT)
            self.assertIn("skills/trainops/SKILL.md", body, f"{where} 必须指向能力契约 SKILL.md")
            self.assertLessEqual(len(body.splitlines()), ENTRY_LIMIT,
                                 f"{where} 太长：入口文件只做指针，细则写在 SKILL.md 里")
            self.assertNotRegex(body, r"/home/|/Users/|[A-Z]:\\|/tmp/",
                                f"{where} 里不能出现绝对路径，否则换台机器就失效")
            for phrase in INVARIANTS:
                self.assertIn(phrase, body, f"{where} 缺少不变量「{phrase}」：三处写法必须一致")


@unittest.skipUnless(shutil.which("claude"), "未安装 Claude Code，跳过官方校验器")
class ClaudeValidatorTests(unittest.TestCase):
    def test_official_validator_reports_no_errors(self):
        result = subprocess.run([shutil.which("claude"), "plugin", "validate", "--json", str(ROOT)],
                                text=True, capture_output=True, timeout=120)
        report = json.loads(result.stdout)
        problems = [f"{item['file']}: {w['message']}"
                    for item in report.get("contents", []) for w in item.get("errors", [])]
        self.assertEqual(problems, [], "claude plugin validate 报告了错误")
        warnings = [w["message"] for item in report.get("contents", []) for w in item.get("warnings", [])]
        unexpected = [w for w in warnings if not any(known in w for known in KNOWN_CLAUDE_WARNINGS)]
        self.assertEqual(unexpected, [], "claude plugin validate 出现了预期之外的警告")


@unittest.skipUnless(shutil.which("agy"), "未安装 Antigravity CLI，跳过官方校验器")
class AntigravityValidatorTests(unittest.TestCase):
    def test_official_validator_accepts_plugin(self):
        result = subprocess.run([shutil.which("agy"), "plugin", "validate", str(ROOT / "antigravity")],
                                text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, f"agy plugin validate 失败：{result.stdout}{result.stderr}")
        self.assertIn("[ok]", result.stdout)
        self.assertIn("1 processed", result.stdout, "Antigravity 没识别到软链过来的技能")


@unittest.skipUnless(shutil.which("codex"), "未安装 Codex CLI，跳过官方校验器")
class CodexValidatorTests(unittest.TestCase):
    def test_local_marketplace_install_without_errors(self):
        """用临时 CODEX_HOME 装一遍：不碰用户自己的 Codex 配置，也不改本仓库。"""
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "CODEX_HOME": home}
            codex = shutil.which("codex")
            added = subprocess.run([codex, "plugin", "marketplace", "add", str(ROOT)],
                                   text=True, capture_output=True, timeout=180, env=env)
            self.assertEqual(added.returncode, 0, f"注册本地 marketplace 失败：{added.stdout}{added.stderr}")
            installed = subprocess.run([codex, "plugin", "add", "trainops@trainops"],
                                       text=True, capture_output=True, timeout=180, env=env)
            self.assertEqual(installed.returncode, 0, f"安装插件失败：{installed.stdout}{installed.stderr}")
            listed = subprocess.run([codex, "plugin", "list", "--json"],
                                    text=True, capture_output=True, timeout=120, env=env)
            plugins = json.loads(listed.stdout)["installed"]
            self.assertTrue(any(item["name"] == "trainops" and item["enabled"] for item in plugins),
                            f"Codex 没有把插件标为已启用：{plugins}")


if __name__ == "__main__":
    unittest.main()
