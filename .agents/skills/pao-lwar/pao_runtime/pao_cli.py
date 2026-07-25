from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

from . import __version__
from .common import (
    atomic_write_json,
    emit,
    load_json,
    local_filesystem_status,
    resolve_root,
    safe_load_json,
)


SKILL_NAMES = ("pao-oa", "pao-lwar")

RUNTIME_MODULES = (
    "adp_watch.py",
    "audit.py",
    "canary_routing.py",
    "common.py",
    "contracts.py",
    "ledger.py",
    "lwar_cli.py",
    "oa_cli.py",
    "pao_cli.py",
    "presence.py",
    "predictive_routing.py",
    "registry.py",
    "routing.py",
    "transport.py",
)
WRAPPER_SCRIPTS = ("adp_watch.py", "lwar.py", "oa.py", "pao.py")
ROLE_REFERENCES = {
    "oa": {"reconcile.md", "publish.md", "collect-validate.md", "recover-maintain.md"},
    "lwar": {"register.md", "adp-loop.md", "execute-complete.md", "lifecycle.md"},
}
LEFTOVER_TMP_AGE_S = 60


def default_skills_source() -> Path | None:
    """Locate the canonical skills directory next to the package.

    Valid for the canonical standalone bundle layout. The bundle's parent is
    `.agents/skills`, containing exactly the `pao-oa` and `pao-lwar` channels.
    """
    bundle = Path(__file__).resolve().parents[1]
    for candidate in (bundle.parent, bundle / "skills", bundle / ".agents" / "skills"):
        if all((candidate / name).is_dir() for name in SKILL_NAMES):
            return candidate
    return None


def root_source(value: str | None) -> str:
    if value:
        return "--root"
    if os.environ.get("PAO_ROOT", "").strip():
        return "PAO_ROOT"
    return "default_dot_pao"


def command_info(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    skills = default_skills_source()
    emit(
        {
            "event": "pao_info",
            "version": __version__,
            "root": str(root),
            "root_source": root_source(args.root),
            "package_dir": str(Path(__file__).resolve().parent),
            "skills_source": str(skills) if skills else None,
            "registry_exists": (root / "var" / "registry" / "lwar_registry.json").is_file(),
        }
    )
    return 0


def _check(name: str, ok: bool, detail: object = None) -> dict:
    entry: dict = {"check": name, "ok": bool(ok)}
    if detail is not None:
        entry["detail"] = detail
    return entry


def _legacy_bus_records(root: Path) -> list[str]:
    """Return pre-v1 protocol records that require an intentional fresh bus."""
    legacy = []

    registration_paths = [
        *root.glob("control/registration/requests/*.json"),
        *root.glob("control/registration/archive/*.json"),
    ]
    for path in sorted(registration_paths):
        payload = safe_load_json(path)
        if payload is not None and "runtime_version" not in payload:
            legacy.append(f"{path.relative_to(root).as_posix()}:missing_runtime_version")

    task_patterns = (
        "mailbox/*/incoming/*.json",
        "mailbox/*/claimed/*.json",
        "mailbox/*/dead/*.json",
        "mailbox/*/archive/tasks/*.json",
    )
    for pattern in task_patterns:
        for path in sorted(root.glob(pattern)):
            if path.name.endswith(".error.json"):
                continue
            payload = safe_load_json(path)
            if payload is None or payload.get("schema_version") != "pao.task.v1":
                continue
            missing = [
                key
                for key in ("registry_version", "attempt", "permissions")
                if key not in payload
            ]
            if path.parent.name in {"claimed", "tasks"} and "claim_token" not in payload:
                missing.append("claim_token")
            permissions = payload.get("permissions")
            if isinstance(permissions, dict):
                missing.extend(
                    f"permissions.{key}"
                    for key in ("read", "write", "network")
                    if key not in permissions
                )
            if missing:
                legacy.append(
                    f"{path.relative_to(root).as_posix()}:missing_{','.join(sorted(set(missing)))}"
                )

    result_patterns = (
        "mailbox/*/outgoing/*.json",
        "mailbox/*/archive/results/*.json",
    )
    for pattern in result_patterns:
        for path in sorted(root.glob(pattern)):
            payload = safe_load_json(path)
            if payload is None or payload.get("schema_version") != "pao.result.v1":
                continue
            missing = [
                key
                for key in ("workflow_id", "registry_version", "attempt", "claim_token")
                if not payload.get(key)
            ]
            if any(not isinstance(item, dict) for item in payload.get("artifacts", [])):
                missing.append("snapshot_artifact")
            if missing:
                legacy.append(
                    f"{path.relative_to(root).as_posix()}:missing_{','.join(sorted(set(missing)))}"
                )
    return legacy


def command_doctor(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    package_dir = Path(__file__).resolve().parent
    bundle = package_dir.parent  # the skill folder containing this runtime
    checks = []

    checks.append(
        _check("python_version", sys.version_info >= (3, 9), sys.version.split()[0])
    )
    missing_modules = [name for name in RUNTIME_MODULES if not (package_dir / name).is_file()]
    checks.append(_check("runtime_modules", not missing_modules, missing_modules or None))
    missing_scripts = [
        name for name in WRAPPER_SCRIPTS if not (bundle / "scripts" / name).is_file()
    ]
    checks.append(_check("wrapper_scripts", not missing_scripts, missing_scripts or None))
    # The two standalone skills keep schemas/ and references/ at bundle root.
    schemas = bundle / "schemas"
    checks.append(
        _check(
            "schemas_present",
            schemas.is_dir() and any(schemas.glob("*.schema.json")),
            str(schemas),
        )
    )
    schema_errors = []
    if schemas.is_dir():
        for schema in sorted(schemas.glob("*.schema.json")):
            try:
                load_json(schema)
            except Exception as error:
                schema_errors.append(f"{schema.name}:{error}")
    checks.append(_check("schemas_parse", not schema_errors, schema_errors or None))
    if args.role:
        references = bundle / "references"
        if references.is_dir():
            present = {path.name for path in references.glob("*.md")}
            expected = ROLE_REFERENCES[args.role]
            checks.append(
                _check("role_references", expected <= present, sorted(expected - present) or None)
            )
        else:
            checks.append(_check("role_references", False, str(references)))

    inside_skill = root == bundle or bundle in root.parents
    checks.append(_check("root_outside_skill_dir", not inside_skill, str(root)))
    filesystem_local, filesystem_detail = local_filesystem_status(root)
    checks.append(_check("local_filesystem", filesystem_local, filesystem_detail))

    if inside_skill:
        # Never write probe files into the skill bundle itself.
        checks.append(_check("bus_writable_atomic", False, "skipped — root resolves inside the skill directory"))
    else:
        probe = root / "var" / f".doctor-{os.getpid()}.probe.json"
        renamed = probe.with_suffix(".renamed.json")
        try:
            atomic_write_json(probe, {"probe": True})
            os.replace(probe, renamed)
            renamed.unlink()
            checks.append(_check("bus_writable_atomic", True))
        except OSError as error:
            checks.append(_check("bus_writable_atomic", False, str(error)))
        finally:
            probe.unlink(missing_ok=True)
            renamed.unlink(missing_ok=True)

    registry_path = root / "var" / "registry" / "lwar_registry.json"
    if registry_path.is_file():
        try:
            load_json(registry_path)
            checks.append(_check("registry_parses", True))
        except Exception as error:
            checks.append(_check("registry_parses", False, str(error)))
    else:
        checks.append(_check("registry_parses", True, "absent (fresh bus)"))

    legacy_records = _legacy_bus_records(root)
    checks.append(
        _check(
            "v1_bus_contract",
            not legacy_records,
            legacy_records or "no pre-v1 protocol records",
        )
    )

    # Only aged temp files count: an in-flight atomic write briefly creates
    # a .pao-*.tmp on a perfectly healthy bus.
    cutoff = time.time() - LEFTOVER_TMP_AGE_S
    leftovers = []
    for path in root.rglob(".pao-*.tmp"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                leftovers.append(str(path))
        except FileNotFoundError:
            # An in-flight atomic write deleted its temp between glob and stat
            # on a healthy busy bus — not a leftover, keep scanning.
            continue
    checks.append(_check("no_leftover_tmp", not leftovers, leftovers or None))

    healthy = all(entry["ok"] for entry in checks)
    emit(
        {
            "event": "doctor_report",
            "version": __version__,
            "role": args.role,
            "root": str(root),
            "bundle": str(bundle),
            "healthy": healthy,
            "checks": checks,
        }
    )
    return 0 if healthy else 1


def command_install_skills(args: argparse.Namespace) -> int:
    if args.source:
        source = Path(args.source).expanduser().resolve()
    else:
        source = default_skills_source()
        if source is None:
            raise SystemExit(
                "skills source not found next to the bundle; pass --source <skills-directory>"
            )
    if not source.is_dir():
        raise SystemExit(f"skills source is not a directory: {source}")
    target = (
        Path(args.target).expanduser().resolve()
        if args.target
        else Path.home() / ".agents" / "skills"
    )
    installed = []
    for name in SKILL_NAMES:
        skill_source = source / name
        if not skill_source.is_dir():
            raise SystemExit(
                f"skill source missing: {skill_source} (canonical skills live in .agents/skills)"
            )
        destination = target / name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(skill_source, destination)
        installed.append({"skill": name, "target": str(destination)})
    emit({"event": "skills_installed", "count": len(installed), "source": str(source), "skills": installed})
    return 0


def command_build_skills(args: argparse.Namespace) -> int:
    # Retired: .agents/skills is the single canonical channel; pao-lwar is the
    # runtime master. Building skills FROM anywhere else would clobber it.
    raise SystemExit(
        "build-skills is retired: .agents/skills is the canonical source. "
        "To sync the master runtime into the pao-oa mirror, run "
        "tools/sync_bundles.py from the repository root."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pao", description="PAO deployment and diagnostics tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    info = subparsers.add_parser("info", help="show runtime version and bus root resolution")
    info.add_argument("--root", default=None)
    info.set_defaults(handler=command_info)

    doctor = subparsers.add_parser(
        "doctor",
        help="pre-flight health checks for the bundle and the bus root; "
        "exit 1 means do not start registration, publishing, or ADP",
    )
    doctor.add_argument("--role", choices=("oa", "lwar"), default=None)
    doctor.add_argument("--root", default=None)
    doctor.set_defaults(handler=command_doctor)

    install = subparsers.add_parser(
        "install-skills",
        help="copy the OA and LWAR skill contracts to a global skills directory "
        "(identical to copying them by hand)",
    )
    install.add_argument("--source", default=None, help="skills directory (default: <package>/../skills)")
    install.add_argument("--target", default=None, help="destination (default: ~/.agents/skills)")
    install.set_defaults(handler=command_install_skills)

    build = subparsers.add_parser(
        "build-skills",
        help="retired (fails closed): .agents/skills is canonical since 0.5.0 — "
        "use tools/sync_bundles.py instead",
    )
    build.add_argument("--target", default=None, help=argparse.SUPPRESS)
    build.set_defaults(handler=command_build_skills)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
