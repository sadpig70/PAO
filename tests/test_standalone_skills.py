import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pao_helpers import PLUGIN, REPO

SKILLS = REPO / "PAO_skills"
GENERATED = {
    "pao-oa": ("pao_runtime", "scripts", "schemas"),
    "pao-lwar": ("pao_runtime", "scripts", "schemas"),
}
REFERENCES = {
    "pao-oa": {"reconcile.md", "publish.md", "collect-validate.md", "recover-maintain.md"},
    "pao-lwar": {"register.md", "adp-loop.md", "execute-complete.md", "lifecycle.md"},
}


def tree_bytes(root):
    files = {}
    for path in root.rglob("*"):
        if path.is_dir() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


class PluginMirrorTests(unittest.TestCase):
    """PAO_skills/pao-lwar is the canonical runtime master; the plugin's
    runtime layer is a generated mirror (sync_bundles.py --to-plugin)."""

    MASTER = SKILLS / "pao-lwar"
    PLUGIN_MIRROR = {
        "pao_runtime": PLUGIN / "pao_runtime",
        "scripts": PLUGIN / "scripts",
        "schemas": PLUGIN / "skills" / "lwar-runtime" / "schemas",
    }

    def test_plugin_runtime_mirrors_the_master_bytes(self):
        for name, mirrored in self.PLUGIN_MIRROR.items():
            master = tree_bytes(self.MASTER / name)
            mirror = tree_bytes(mirrored)
            self.assertEqual(set(master), set(mirror), f"plugin/{name} file set")
            for rel, data in master.items():
                self.assertEqual(data, mirror[rel], f"plugin/{name}/{rel} bytes")

    def test_build_skills_fails_closed(self):
        # The old plugin->skills direction would clobber the canonical source.
        completed = subprocess.run(
            [sys.executable, "-m", "pao_runtime.pao_cli", "build-skills"],
            cwd=REPO,
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(PLUGIN)},
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("canonical source", completed.stderr)


class SkillsInternalSyncTests(unittest.TestCase):
    """pao-lwar is the runtime master; pao-oa must be a byte mirror.

    Guards against drift between the two bundled runtime copies while the
    skills are edited directly (sync via PAO_skills/sync_bundles.py).
    """

    MIRRORED = ("pao_runtime", "scripts", "schemas")

    def test_bundles_carry_identical_runtime_bytes(self):
        for name in self.MIRRORED:
            master = tree_bytes(SKILLS / "pao-lwar" / name)
            mirror = tree_bytes(SKILLS / "pao-oa" / name)
            self.assertEqual(set(master), set(mirror), f"pao-oa/{name} file set")
            for rel, data in master.items():
                self.assertEqual(data, mirror[rel], f"pao-oa/{name}/{rel} bytes")


class StandaloneContractTests(unittest.TestCase):
    def test_frontmatter_names_match_folders(self):
        for skill in GENERATED:
            text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
            match = re.search(r"(?m)^name: (\S+)$", text)
            self.assertIsNotNone(match, skill)
            self.assertEqual(match.group(1), skill)
            self.assertRegex(match.group(1), r"^[a-z0-9]+(-[a-z0-9]+)*$")

    def test_reference_documents_exist(self):
        for skill, expected in REFERENCES.items():
            present = {path.name for path in (SKILLS / skill / "references").glob("*.md")}
            self.assertEqual(expected, present, skill)

    def test_authored_files_stay_plugin_and_home_agnostic(self):
        for skill in GENERATED:
            files = [SKILLS / skill / "SKILL.md"]
            files += sorted((SKILLS / skill / "references").glob("*.md"))
            for path in files:
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("CLAUDE_PLUGIN_ROOT", text, path)
                self.assertNotIn("PAO_HOME", text, path)
                self.assertNotRegex(text, r"(?m)^\s*python -m pao_runtime", path)
                self.assertNotIn("$PAO_SKILL", text, path)  # placeholder is <PAO_SKILL>, never shell-expandable

    def test_functional_smoke_binds_to_the_bundle(self):
        for skill in GENERATED:
            with tempfile.TemporaryDirectory() as work_dir:
                env = {**os.environ}
                env.pop("PYTHONPATH", None)
                completed = subprocess.run(
                    [sys.executable, str(SKILLS / skill / "scripts" / "pao.py"), "info"],
                    cwd=work_dir,
                    check=False,
                    capture_output=True,
                    text=True,
                    env=env,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                payload = json.loads(completed.stdout)
                expected_package = (SKILLS / skill / "pao_runtime").resolve()
                self.assertEqual(Path(payload["package_dir"]).resolve(), expected_package, skill)


if __name__ == "__main__":
    unittest.main()
