import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pao_helpers import REPO

SKILLS = REPO / ".agents" / "skills"
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


class SkillsInternalSyncTests(unittest.TestCase):
    """pao-lwar is the runtime master; pao-oa must be a byte mirror.

    Guards against drift between the two bundled runtime copies while the
    skills are edited directly (sync via tools/sync_bundles.py).
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

    def test_default_role_invocation_is_autonomous(self):
        oa = (SKILLS / "pao-oa" / "SKILL.md").read_text(encoding="utf-8")
        lwar = (SKILLS / "pao-lwar" / "SKILL.md").read_text(encoding="utf-8")
        register = (SKILLS / "pao-lwar" / "references" / "register.md").read_text(
            encoding="utf-8"
        )

        for skill, text in (("pao-oa", oa), ("pao-lwar", lwar)):
            self.assertIn("### Default autonomous invocation", text, skill)
            self.assertRegex(text, r"executable `start`\s+command", skill)
            self.assertIn("ask for a second bootstrap prompt", text, skill)
            self.assertIn("complete operating contract", text, skill)
            self.assertIn("not a separate Python subcommand", text, skill)

        self.assertIn("mint a unique `oa-<random>` id yourself", oa)
        self.assertIn("If no goal was supplied, do not invent tasks", oa)
        self.assertIn("Presence expires after 90 seconds", oa)
        self.assertIn("writer lease is fencing, **not liveness**", oa)
        self.assertIn("No explicit identity handle → REGISTER fresh", lwar)
        self.assertIn("Never scan `var/identities/`", lwar)
        self.assertIn("lwar.py oa-status", lwar)
        self.assertIn("successful `control:retire`", lwar)
        self.assertIn("Registration remains order-independent", register)
        self.assertIn("`Unreported Runtime`", register)
        self.assertIn("`Unreported Model`", register)
        self.assertIn("`unreported_vendor`", register)
        self.assertRegex(register, r"`unreported_\*`\s+sentinels")

    def test_skill_markdown_links_resolve_inside_each_bundle(self):
        for skill in GENERATED:
            root = (SKILLS / skill).resolve()
            text = (root / "SKILL.md").read_text(encoding="utf-8")
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
                if target.startswith("#"):
                    continue
                resolved = (root / target).resolve()
                self.assertTrue(resolved.is_relative_to(root), f"{skill}: {target}")
                self.assertTrue(resolved.exists(), f"{skill}: {target}")

    def test_repository_bootstrap_note_does_not_duplicate_runtime_commands(self):
        text = (REPO / "docs" / "LWAR_ADP_Bootstrap.md").read_text(encoding="utf-8")
        self.assertNotIn("scripts/", text)
        self.assertNotIn("lwar-runtime", text)
        self.assertIn("sole operating prompts", text)

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
