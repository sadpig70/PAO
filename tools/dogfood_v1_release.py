#!/usr/bin/env python3
"""Run a clean-copy, two-LWAR PAO v1 release-candidate dogfood."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]


def run_json(
    script: Path,
    *args: str,
    expected: int | tuple[int, ...] = 0,
    env: dict[str, str],
) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(script), *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    accepted = (expected,) if isinstance(expected, int) else expected
    if completed.returncode not in accepted:
        raise RuntimeError(
            f"{script.name} returned {completed.returncode}, expected {accepted}: "
            f"{completed.stderr}{completed.stdout}"
        )
    return json.loads(completed.stdout) if completed.stdout.strip() else {}


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def register(
    lwar: Path,
    oa: Path,
    bus: Path,
    env: dict[str, str],
    suffix: str,
    capability: str,
) -> dict[str, Any]:
    request = run_json(
        lwar,
        "register",
        "--runtime-name",
        f"Dogfood Runtime {suffix}",
        "--model",
        f"Dogfood Model {suffix}",
        "--adapter-id",
        f"dogfood_{suffix.lower()}",
        "--vendor-family",
        "dogfood",
        "--interface",
        "agent",
        "--capability",
        capability,
        "--root",
        str(bus),
        env=env,
    )
    run_json(oa, "reconcile", "--root", str(bus), env=env)
    return run_json(
        lwar,
        "response",
        request["request_id"],
        "--root",
        str(bus),
        env=env,
    )


def execute_task(
    *,
    oa: Path,
    lwar: Path,
    watch: Path,
    bus: Path,
    env: dict[str, str],
    identity: dict[str, Any],
    draft: Path,
    result: Path,
) -> str:
    run_json(
        oa,
        "send",
        "--lwar-id",
        identity["lwar_id"],
        "--task-file",
        str(draft),
        "--root",
        str(bus),
        env=env,
    )
    event = run_json(
        watch,
        "--identity-file",
        identity["identity_file"],
        "--interval",
        "0.01",
        "--timeout",
        "0.5",
        "--root",
        str(bus),
        env=env,
    )
    run_json(
        lwar,
        "complete",
        "--identity-file",
        identity["identity_file"],
        "--task-id",
        event["task_id"],
        "--claim-token",
        event["task"]["claim_token"],
        "--result-file",
        str(result),
        "--root",
        str(bus),
        env=env,
    )
    run_json(oa, "collect", "--root", str(bus), env=env)
    run_json(
        oa,
        "validate",
        "--task-id",
        event["task_id"],
        "--record",
        "--decision",
        "accepted",
        "--reason",
        "release evidence verified",
        "--root",
        str(bus),
        env=env,
    )
    return event["task_id"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)

    temporary = tempfile.TemporaryDirectory()
    base = Path(temporary.name)
    installed = base / "skills"
    shutil.copytree(REPO / ".agents" / "skills" / "pao-oa", installed / "pao-oa")
    shutil.copytree(REPO / ".agents" / "skills" / "pao-lwar", installed / "pao-lwar")
    bus = base / "project" / ".pao"
    work = base / "project" / "work"
    work.mkdir(parents=True)

    oa = installed / "pao-oa" / "scripts" / "oa.py"
    oa_pao = installed / "pao-oa" / "scripts" / "pao.py"
    lwar = installed / "pao-lwar" / "scripts" / "lwar.py"
    lwar_pao = installed / "pao-lwar" / "scripts" / "pao.py"
    watch = installed / "pao-lwar" / "scripts" / "adp_watch.py"
    env = {**os.environ, "PAO_OA_ID": "oa-v1-dogfood"}

    run_json(oa_pao, "doctor", "--role", "oa", "--root", str(bus), env=env)
    run_json(lwar_pao, "doctor", "--role", "lwar", "--root", str(bus), env=env)

    # LWAR-first: publish registration before the OA appears.
    first_request = run_json(
        lwar,
        "register",
        "--runtime-name",
        "Dogfood Runtime A",
        "--model",
        "Dogfood Model A",
        "--adapter-id",
        "dogfood_a",
        "--vendor-family",
        "dogfood",
        "--interface",
        "agent",
        "--capability",
        "coding",
        "--root",
        str(bus),
        env=env,
    )
    run_json(oa, "presence", "--root", str(bus), env=env)
    run_json(oa, "reconcile", "--root", str(bus), env=env)
    first = run_json(
        lwar,
        "response",
        first_request["request_id"],
        "--root",
        str(bus),
        env=env,
    )

    # OA-first: reconcile a second registration while presence is live.
    second = register(lwar, oa, bus, env, "B", "testing")
    identities = {first["lwar_id"]: first, second["lwar_id"]: second}
    if sorted(identities) != ["LWAR1", "LWAR2"]:
        raise RuntimeError(f"unexpected dynamic allocation: {sorted(identities)}")

    for identity in identities.values():
        run_json(
            watch,
            "--identity-file",
            identity["identity_file"],
            "--interval",
            "0.01",
            "--timeout",
            "0.05",
            "--root",
            str(bus),
            expected=10,
            env=env,
        )

    up_draft = write_json(
        base / "upstream.json",
        {
            "task_id": "task-v1-upstream",
            "workflow_id": "workflow-v1-release",
            "goal": "produce verified upstream evidence",
            "completion_criteria": ["upstream evidence is present"],
            "cwd": str(work),
        },
    )
    up_result = write_json(
        base / "upstream-result.json",
        {
            "status": "succeeded",
            "summary": "upstream complete",
            "evidence": {"verified": True},
            "artifacts": [],
            "exit_code": 0,
        },
    )
    upstream = execute_task(
        oa=oa,
        lwar=lwar,
        watch=watch,
        bus=bus,
        env=env,
        identity=identities["LWAR1"],
        draft=up_draft,
        result=up_result,
    )

    down_draft = write_json(
        base / "downstream.json",
        {
            "task_id": "task-v1-downstream",
            "workflow_id": "workflow-v1-release",
            "goal": "consume accepted upstream evidence",
            "depends_on": [upstream],
            "completion_criteria": ["dependency was accepted"],
            "cwd": str(work),
        },
    )
    down_result = write_json(
        base / "downstream-result.json",
        {
            "status": "succeeded",
            "summary": "downstream complete",
            "evidence": {"dependency_accepted": True},
            "artifacts": [],
            "exit_code": 0,
        },
    )
    downstream = execute_task(
        oa=oa,
        lwar=lwar,
        watch=watch,
        bus=bus,
        env=env,
        identity=identities["LWAR2"],
        draft=down_draft,
        result=down_result,
    )

    oa_doctor = run_json(oa_pao, "doctor", "--role", "oa", "--root", str(bus), env=env)
    lwar_doctor = run_json(
        lwar_pao, "doctor", "--role", "lwar", "--root", str(bus), env=env
    )
    print(
        json.dumps(
            {
                "event": "pao_v1_dogfood_passed",
                "runtime_version": oa_doctor["version"],
                "installed_skills": ["pao-oa", "pao-lwar"],
                "lwar_ids": sorted(identities),
                "bootstrap_orders": ["lwar_before_oa", "oa_before_lwar"],
                "tasks": [upstream, downstream],
                "semantic_dependency": "accepted_then_released",
                "oa_doctor": oa_doctor["healthy"],
                "lwar_doctor": lwar_doctor["healthy"],
            },
            sort_keys=True,
        )
    )
    if args.keep:
        temporary._finalizer.detach()
        print(json.dumps({"kept_root": str(base)}, sort_keys=True))
    else:
        temporary.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
