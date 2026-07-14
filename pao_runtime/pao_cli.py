from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

from . import __version__
from .common import emit, resolve_root


SKILL_NAMES = ("oa-runtime", "lwar-runtime")


def default_skills_source() -> Path | None:
    """Locate the canonical skills directory next to the package.

    Valid for editable installs (`pip install -e PAO_HOME`) and in-repo runs;
    wheel installs must pass --source explicitly.
    """
    candidate = Path(__file__).resolve().parents[1] / ".agents" / "skills"
    return candidate if candidate.is_dir() else None


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
                "skills source not found next to the package; pass --source PAO_HOME/.agents/skills"
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
            raise SystemExit(f"skill source missing: {skill_source}")
        destination = target / name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(skill_source, destination)
        installed.append({"skill": name, "target": str(destination)})
    emit({"event": "skills_installed", "count": len(installed), "source": str(source), "skills": installed})
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
    install.add_argument("--source", default=None, help="skills directory (default: <package>/../.agents/skills)")
    install.add_argument("--target", default=None, help="destination (default: ~/.agents/skills)")
    install.set_defaults(handler=command_install_skills)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
