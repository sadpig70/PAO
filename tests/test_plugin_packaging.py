import json
import re
import unittest

from pao_helpers import PLUGIN, REPO


def read(path):
    return path.read_text(encoding="utf-8")


def plugin_runtime_version():
    # The plugin is frozen while PAO_skills is canonical: these tests verify the
    # frozen artifact's INTERNAL consistency, so the version comes from the
    # plugin's own bundled runtime, never from the importable canonical runtime.
    match = re.search(
        r'^__version__ = "([^"]+)"$', read(PLUGIN / "pao_runtime" / "__init__.py"), flags=re.M
    )
    return match.group(1)


class PluginManifestTests(unittest.TestCase):
    def test_plugin_manifest_identity_and_version(self):
        manifest = json.loads(read(PLUGIN / ".claude-plugin" / "plugin.json"))
        self.assertEqual(manifest["name"], "pao")
        self.assertRegex(manifest["name"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
        self.assertEqual(manifest["version"], plugin_runtime_version())

    def test_pyproject_version_matches_runtime(self):
        match = re.search(r'^version = "([^"]+)"$', read(PLUGIN / "pyproject.toml"), flags=re.M)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), plugin_runtime_version())

    def test_marketplace_lists_repo_root_plugin(self):
        market = json.loads(read(REPO / ".claude-plugin" / "marketplace.json"))
        entries = {plugin["name"]: plugin for plugin in market["plugins"]}
        self.assertIn("pao", entries)
        self.assertEqual(entries["pao"]["source"], "./PAO_plugin")


class PluginLayoutTests(unittest.TestCase):
    def test_skills_ship_exactly_once_at_plugin_location(self):
        for name in ("oa-runtime", "lwar-runtime"):
            self.assertTrue((PLUGIN / "skills" / name / "SKILL.md").is_file(), name)
            self.assertFalse((REPO / "skills" / name).exists(), name)
            self.assertFalse((REPO / ".agents" / "skills" / name).exists(), name)

    def test_lwar_supporting_files_ship_with_the_skill(self):
        lwar = PLUGIN / "skills" / "lwar-runtime"
        self.assertTrue((lwar / "references" / "adp-contract.md").is_file())
        self.assertGreaterEqual(len(list((lwar / "schemas").glob("*.schema.json"))), 15)

    def test_command_aliases_cover_documented_lwar_commands(self):
        expected = {
            "oa",
            "lwar-register",
            "lwar-adp",
            "lwar-status",
            "lwar-on",
            "lwar-drain",
            "lwar-off",
            "lwar-unregister",
        }
        present = {path.stem for path in (PLUGIN / "commands").glob("*.md")}
        self.assertTrue(expected <= present, expected - present)
        for stem in expected:
            self.assertRegex(read(PLUGIN / "commands" / f"{stem}.md"), r"(?m)^description:")


class SkillContractTests(unittest.TestCase):
    def test_skill_examples_never_rely_on_module_invocation(self):
        # Plugin installs have no pip and an arbitrary cwd: every executable
        # example must use the self-bootstrapping wrapper scripts.
        for name in ("oa-runtime", "lwar-runtime"):
            text = read(PLUGIN / "skills" / name / "SKILL.md")
            self.assertNotRegex(text, r"(?m)^\s*python -m pao_runtime", name)
            self.assertIn("${CLAUDE_PLUGIN_ROOT}", text, name)
            self.assertIn("PAO_HOME", text, name)

    def test_dev_skills_stay_harness_agnostic(self):
        for path in (REPO / ".agents" / "skills").rglob("*.md"):
            self.assertNotIn("CLAUDE_PLUGIN_ROOT", read(path), path)


if __name__ == "__main__":
    unittest.main()
