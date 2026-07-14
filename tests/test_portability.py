import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pao_helpers import REPO, PaoTestCase


class RootResolutionTests(PaoTestCase):
    def test_pao_root_env_resolves_bus_from_foreign_cwd(self):
        with tempfile.TemporaryDirectory() as bus_dir, tempfile.TemporaryDirectory() as work_dir:
            bus = Path(bus_dir)
            env = {"PAO_ROOT": str(bus), "PYTHONPATH": str(REPO)}
            _, requested = self.run_module(
                "pao_runtime.lwar_cli",
                "register",
                "--runtime-name", "Portable",
                "--model", "Portable Model",
                "--adapter-id", "portable",
                "--vendor-family", "portable",
                "--interface", "cli",
                env=env, cwd=work_dir,
                expected=0,
            )
            self.run_module("pao_runtime.oa_cli", "reconcile", env=env, cwd=work_dir, expected=0)
            _, adopted = self.run_module(
                "pao_runtime.lwar_cli", "response", requested["request_id"],
                env=env, cwd=work_dir,
                expected=0,
            )
            self.assertEqual(adopted["lwar_id"], "LWAR1")
            self.assertTrue((bus / "var" / "registry" / "lwar_registry.json").is_file())
            self.assertTrue((bus / "mailbox" / "LWAR1").is_dir())
            self.assertFalse((Path(work_dir) / "var").exists())
            self.assertFalse((Path(work_dir) / "mailbox").exists())

    def test_script_wrappers_run_without_install_from_foreign_cwd(self):
        # No pip install, no PYTHONPATH: the scripts/*.py wrappers must
        # bootstrap their own import path from any working directory.
        with tempfile.TemporaryDirectory() as bus_dir, tempfile.TemporaryDirectory() as work_dir:
            env = {**os.environ, "PAO_ROOT": bus_dir}
            env.pop("PYTHONPATH", None)
            completed = subprocess.run(
                [sys.executable, str(REPO / "scripts" / "pao.py"), "info"],
                cwd=work_dir,
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["version"], "0.3.0")
            self.assertEqual(payload["root"], str(Path(bus_dir).resolve()))
            self.assertEqual(payload["root_source"], "PAO_ROOT")

    def test_explicit_root_beats_env(self):
        with tempfile.TemporaryDirectory() as real_dir:
            real = Path(real_dir)
            bogus = real / "never-used-env-root"
            env = {"PAO_ROOT": str(bogus)}
            self.run_module(
                "pao_runtime.lwar_cli",
                "register",
                "--runtime-name", "Explicit",
                "--model", "Explicit Model",
                "--adapter-id", "explicit",
                "--vendor-family", "explicit",
                "--interface", "cli",
                "--root", str(real),
                env=env,
                expected=0,
            )
            self.assertTrue((real / "control" / "registration" / "requests").is_dir())
            self.assertFalse(bogus.exists())

    def test_pao_info_reports_root_source(self):
        with tempfile.TemporaryDirectory() as bus_dir:
            bus = Path(bus_dir)
            _, via_env = self.run_module(
                "pao_runtime.pao_cli", "info", env={"PAO_ROOT": str(bus)}, expected=0
            )
            self.assertEqual(via_env["root_source"], "PAO_ROOT")
            self.assertEqual(via_env["root"], str(bus.resolve()))
            self.assertEqual(via_env["version"], "0.3.0")
            _, via_flag = self.run_module(
                "pao_runtime.pao_cli", "info", "--root", str(bus),
                env={"PAO_ROOT": str(bus / "ignored")},
                expected=0,
            )
            self.assertEqual(via_flag["root_source"], "--root")


class InstallerTests(PaoTestCase):
    def test_install_skills_copies_contracts(self):
        with tempfile.TemporaryDirectory() as target_dir:
            target = Path(target_dir) / "skills"
            _, installed = self.run_module(
                "pao_runtime.pao_cli",
                "install-skills",
                "--source", str(REPO / ".agents" / "skills"),
                "--target", str(target),
                expected=0,
            )
            self.assertEqual(installed["count"], 2)
            self.assertTrue((target / "oa-runtime" / "SKILL.md").is_file())
            self.assertTrue((target / "lwar-runtime" / "SKILL.md").is_file())
            self.assertTrue((target / "lwar-runtime" / "schemas" / "task.schema.json").is_file())
            self.assertTrue((target / "lwar-runtime" / "references" / "adp-contract.md").is_file())

    def test_install_skills_detects_default_source(self):
        with tempfile.TemporaryDirectory() as target_dir:
            target = Path(target_dir) / "skills"
            _, installed = self.run_module(
                "pao_runtime.pao_cli", "install-skills", "--target", str(target), expected=0
            )
            self.assertEqual(installed["count"], 2)
            self.assertEqual(installed["source"], str((REPO / ".agents" / "skills").resolve()))


class CwdGuardTests(PaoTestCase):
    def test_send_rejects_missing_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.register_lwar(root)
            draft = root / "bad_cwd.json"
            draft.write_text(
                json.dumps({"goal": "Run in a ghost workspace", "cwd": str(root / "does-not-exist")}),
                encoding="utf-8",
            )
            completed, _ = self.run_module(
                "pao_runtime.oa_cli",
                "send", "--lwar-id", "LWAR1", "--task-file", str(draft), "--root", str(root),
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("cwd does not exist", completed.stderr)


class PackagingTests(unittest.TestCase):
    def test_console_entry_points_are_importable(self):
        import tomllib

        manifest = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        scripts = manifest["project"]["scripts"]
        self.assertEqual(
            set(scripts), {"pao", "pao-oa", "pao-lwar", "pao-adp-watch"}
        )
        for name, spec in scripts.items():
            module_name, function_name = spec.split(":")
            module = importlib.import_module(module_name)
            self.assertTrue(callable(getattr(module, function_name)), name)
        self.assertEqual(manifest["project"]["version"], "0.3.0")


if __name__ == "__main__":
    unittest.main()
