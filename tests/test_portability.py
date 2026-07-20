import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import re

from pao_helpers import PLUGIN, REPO, RUNTIME_HOME, PaoTestCase
from pao_runtime import __version__


class RootResolutionTests(PaoTestCase):
    def test_pao_root_env_resolves_bus_from_foreign_cwd(self):
        with tempfile.TemporaryDirectory() as bus_dir, tempfile.TemporaryDirectory() as work_dir:
            bus = Path(bus_dir)
            env = {"PAO_ROOT": str(bus), "PYTHONPATH": str(RUNTIME_HOME)}
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
                [sys.executable, str(RUNTIME_HOME / "scripts" / "pao.py"), "info"],
                cwd=work_dir,
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["version"], __version__)
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
            self.assertEqual(via_env["version"], __version__)
            _, via_flag = self.run_module(
                "pao_runtime.pao_cli", "info", "--root", str(bus),
                env={"PAO_ROOT": str(bus / "ignored")},
                expected=0,
            )
            self.assertEqual(via_flag["root_source"], "--root")

    def test_default_bus_is_dot_pao_under_cwd(self):
        # With no --root and no PAO_ROOT, the bus defaults to `.pao/` under the
        # working directory — never scattering state across the workspace root.
        with tempfile.TemporaryDirectory() as work_dir:
            env = {k: v for k, v in os.environ.items() if k != "PAO_ROOT"}
            env["PYTHONPATH"] = str(RUNTIME_HOME)
            completed = subprocess.run(
                [sys.executable, "-m", "pao_runtime.pao_cli", "info"],
                cwd=work_dir, check=False, capture_output=True, text=True, env=env,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["root_source"], "default_dot_pao")
            self.assertEqual(payload["root"], str((Path(work_dir) / ".pao").resolve()))
            # info alone must not create the bus; only mutating commands do.
            self.assertFalse((Path(work_dir) / ".pao").exists())

    def test_default_dot_pao_is_created_by_a_mutating_command(self):
        with tempfile.TemporaryDirectory() as work_dir:
            env = {k: v for k, v in os.environ.items() if k != "PAO_ROOT"}
            env["PYTHONPATH"] = str(RUNTIME_HOME)
            completed = subprocess.run(
                [sys.executable, "-m", "pao_runtime.lwar_cli", "register",
                 "--runtime-name", "T", "--model", "M", "--adapter-id", "t",
                 "--vendor-family", "v", "--interface", "cli"],
                cwd=work_dir, check=False, capture_output=True, text=True, env=env,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            # State landed under .pao/, not at the workspace root.
            self.assertTrue((Path(work_dir) / ".pao" / "control").is_dir())
            self.assertFalse((Path(work_dir) / "control").exists())
            self.assertFalse((Path(work_dir) / "mailbox").exists())


class InstallerTests(PaoTestCase):
    # install-skills targets the frozen plugin's thin-contract layout, so these
    # two tests deliberately run against the PLUGIN runtime, not RUNTIME_HOME.
    def test_install_skills_copies_contracts(self):
        with tempfile.TemporaryDirectory() as target_dir:
            target = Path(target_dir) / "skills"
            _, installed = self.run_module(
                "pao_runtime.pao_cli",
                "install-skills",
                "--source", str(PLUGIN / "skills"),
                "--target", str(target),
                env={"PYTHONPATH": str(PLUGIN)},
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
                "pao_runtime.pao_cli", "install-skills", "--target", str(target),
                env={"PYTHONPATH": str(PLUGIN)},
                expected=0,
            )
            self.assertEqual(installed["count"], 2)
            self.assertEqual(installed["source"], str((PLUGIN / "skills").resolve()))


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

        manifest = tomllib.loads((PLUGIN / "pyproject.toml").read_text(encoding="utf-8"))
        scripts = manifest["project"]["scripts"]
        self.assertEqual(
            set(scripts), {"pao", "pao-oa", "pao-lwar", "pao-adp-watch"}
        )
        for name, spec in scripts.items():
            module_name, function_name = spec.split(":")
            module = importlib.import_module(module_name)
            self.assertTrue(callable(getattr(module, function_name)), name)
        # The plugin is frozen: its pyproject must match its own bundled
        # runtime version, not the (possibly newer) canonical skills runtime.
        plugin_init = (PLUGIN / "pao_runtime" / "__init__.py").read_text(encoding="utf-8")
        plugin_version = re.search(r'^__version__ = "([^"]+)"$', plugin_init, flags=re.M).group(1)
        self.assertEqual(manifest["project"]["version"], plugin_version)


if __name__ == "__main__":
    unittest.main()
