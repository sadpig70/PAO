from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

from . import __version__
from .common import emit, resolve_root


SKILL_NAMES = ("oa-runtime", "lwar-runtime")
STANDALONE_SKILLS = ("pao-oa", "pao-lwar")
GENERATED_DIRS = ("pao_runtime", "scripts")


def default_skills_source() -> Path | None:
    """Locate the canonical skills directory next to the package.

    Valid for editable installs (`pip install -e PAO_HOME`) and in-repo runs;
    wheel installs must pass --source explicitly. Probes the plugin layout
    (`skills/`) first, then the pre-0.4 layout (`.agents/skills`).
    """
    repo = Path(__file__).resolve().parents[1]
    for candidate in (repo / "skills", repo / ".agents" / "skills"):
        if all((candidate / name).is_dir() for name in SKILL_NAMES):
            return candidate
    return None


def root_source(value: str | None) -> str:
    if value:
        return "--root"
    if os.environ.get("PAO_ROOT", "").strip():
        return "PAO_ROOT"
    return "cwd"


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


def command_install_skills(args: argparse.Namespace) -> int:
    if args.source:
        source = Path(args.source).expanduser().resolve()
    else:
        source = default_skills_source()
        if source is None:
            raise SystemExit(
                "skills source not found next to the package; pass --source PAO_HOME/skills"
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
                f"skill source missing: {skill_source} (canonical skills live in PAO_HOME/skills since 0.4)"
            )
        destination = target / name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(skill_source, destination)
        installed.append({"skill": name, "target": str(destination)})
    emit({"event": "skills_installed", "count": len(installed), "source": str(source), "skills": installed})
    return 0


def command_build_skills(args: argparse.Namespace) -> int:
    package_root = Path(__file__).resolve().parents[1]
    target = (
        Path(args.target).expanduser().resolve()
        if args.target
        else package_root.parent / "PAO_skills"
    )
    schemas_source = package_root / "skills" / "lwar-runtime" / "schemas"
    for required in (package_root / "pao_runtime", package_root / "scripts", schemas_source):
        if not required.is_dir():
            raise SystemExit(f"canonical source missing: {required}")
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")

    def replace(source: Path, destination: Path) -> None:
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination, ignore=ignore)

    built = []
    for skill in STANDALONE_SKILLS:
        skill_dir = target / skill
        if not (skill_dir / "SKILL.md").is_file():
            raise SystemExit(
                f"authored skill not found: {skill_dir / 'SKILL.md'} "
                "(build-skills only refreshes the generated layer of existing skills)"
            )
        for name in GENERATED_DIRS:
            replace(package_root / name, skill_dir / name)
        built.append({"skill": skill, "target": str(skill_dir)})
    replace(schemas_source, target / "pao-lwar" / "schemas")
    emit({"event": "standalone_skills_built", "count": len(built), "skills": built})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pao", description="PAO deployment and diagnostics tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    info = subparsers.add_parser("info", help="show runtime version and bus root resolution")
    info.add_argument("--root", default=None)
    info.set_defaults(handler=command_info)

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
        help="refresh the generated runtime layer (pao_runtime/, scripts/, schemas/) "
        "of the standalone PAO_skills skills from the canonical source",
    )
    build.add_argument("--target", default=None, help="standalone skills root (default: <package>/../../PAO_skills)")
    build.set_defaults(handler=command_build_skills)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
