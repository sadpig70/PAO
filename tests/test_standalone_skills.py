import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pao_helpers import PLUGIN, REPO

SKILLS = REPO / "PAO_skills"
GENERATED = {
    "pao-oa": ("pao_runtime", "scripts"),
    "pao-lwar": ("pao_runtime", "scripts", "schemas"),
}
CANONICAL = {
    "pao_runtime": PLUGIN / "pao_runtime",
    "scripts": PLUGIN / "scripts",
    "schemas": PLUGIN / "skills" / "lwar-runtime" / "schemas",
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


def authored_bytes(skill_dir):
    files = {"SKILL.md": (skill_dir / "SKILL.md").read_bytes()}
    for path in (skill_dir / "references").glob("*.md"):
        files[f"references/{path.name}"] = path.read_bytes()
    return files


@unittest.skip(
    "plugin frozen — PAO_skills is the canonical source during the skills-first "
    "phase; the plugin→skills byte gate is re-enabled (direction re-decided) at backport"
)
class SyncGateTests(unittest.TestCase):
    def run_build(self, target, expected=0):
        completed = subprocess.run(
            [sys.executable, "-m", "pao_runtime.pao_cli", "build-skills", "--target", str(target)],
            cwd=REPO,
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(PLUGIN)},
        )
        if expected is not None:
            self.assertEqual(completed.returncode, expected, completed.stderr + completed.stdout)
        return completed

    def test_generated_trees_match_canonical_bytes(self):
        for skill, dirs in GENERATED.items():
            for name in dirs:
                canonical = tree_bytes(CANONICAL[name])
                bundled = tree_bytes(SKILLS / skill / name)
                self.assertEqual(set(canonical), set(bundled), f"{skill}/{name} file set")
                for rel, data in canonical.items():
                    self.assertEqual(data, bundled[rel], f"{skill}/{name}/{rel} bytes")

    def test_build_is_idempotent_and_never_touches_authored_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "PAO_skills"
            for skill in GENERATED:
                shutil.copytree(SKILLS / skill / "references", target / skill / "references")
                shutil.copy2(SKILLS / skill / "SKILL.md", target / skill / "SKILL.md")
            authored_before = {skill: authored_bytes(target / skill) for skill in GENERATED}
            self.run_build(target)
            first = {skill: tree_bytes(target / skill) for skill in GENERATED}
            self.run_build(target)
            second = {skill: tree_bytes(target / skill) for skill in GENERATED}
            self.assertEqual(first, second, "second build must be byte-identical")
            for skill in GENERATED:
                self.assertEqual(
                    authored_before[skill], authored_bytes(target / skill), f"{skill} authored files changed"
                )

    def test_build_refuses_unauthored_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            completed = self.run_build(Path(tmp) / "empty", expected=None)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("authored skill not found", completed.stderr)


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
