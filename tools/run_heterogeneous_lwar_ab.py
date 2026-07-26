#!/usr/bin/env python3
"""Run a real four-provider blind PAO LWAR comparison on an isolated bus."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
LWAR_SCRIPT = REPO / ".agents" / "skills" / "pao-lwar" / "scripts" / "lwar.py"
ADP_SCRIPT = REPO / ".agents" / "skills" / "pao-lwar" / "scripts" / "adp_watch.py"
OA_SCRIPT = REPO / ".agents" / "skills" / "pao-oa" / "scripts" / "oa.py"

TASKS = {
    "T1": {
        "prompt": (
            "Do not use tools. Return one JSON object and no prose. "
            "Five jobs A-E obey: B is before A; D is immediately before B; "
            "A is before C; E is after A and before C. "
            'Return {"answer":"<five letters in order>","reason":"<short proof>"}.'
        ),
        "expected": {"answer": "DBAEC"},
    },
    "T2": {
        "prompt": (
            "Do not use tools. Return one JSON object and no prose. Review: "
            "def reserve(item, qty, stock={}): "
            "stock.setdefault(item, 0); "
            "if stock[item] >= qty: stock[item] -= qty; "
            "return stock[item] >= 0. "
            "Choose every real defect ID from: "
            "B1 mutable default leaks state; "
            "B2 insufficient stock can still return true; "
            "B3 nonpositive qty is not rejected; "
            "B4 setdefault always raises KeyError; "
            "B5 subtraction is non-deterministic. "
            'Return {"defects":["sorted IDs"],"fix":"<short fix>"}.'
        ),
        "expected": {"defects": ["B1", "B2", "B3"]},
    },
    "T3": {
        "prompt": (
            "Do not use tools. Return one JSON object and no prose. "
            "Choose a subset with total cost <=10 and risk <=6 that maximizes "
            "value; tie-break by lower cost. Items are "
            "A(cost6,risk2,value9), B(4,4,8), C(5,3,8), "
            "D(3,2,5), E(2,1,3). "
            'Return {"selection":["sorted letters"],"value":<int>,'
            '"cost":<int>,"risk":<int>}.'
        ),
        "expected": {
            "selection": ["A", "B"],
            "value": 17,
            "cost": 10,
            "risk": 6,
        },
    },
}
DEFAULT_TASK_NAMES = tuple(TASKS)

ADAPTERS = {
    "LWAR1": {
        "runtime_name": "Codex CLI",
        "model": "gpt-5.6-sol",
        "adapter_id": "codex_cli",
        "vendor_family": "openai",
        "sequence": ["T1", "T2", "T3"],
    },
    "LWAR2": {
        "runtime_name": "Claude Code",
        "model": "claude-sonnet-5",
        "adapter_id": "claude_code",
        "vendor_family": "anthropic",
        "sequence": ["T2", "T3", "T1"],
    },
    "LWAR3": {
        "runtime_name": "Kimi Code CLI",
        "model": "kimi-for-coding",
        "adapter_id": "kimi_cli",
        "vendor_family": "moonshot",
        "sequence": ["T3", "T1", "T2"],
    },
    "LWAR4": {
        "runtime_name": "OpenCode",
        "model": "glm-4.7",
        "adapter_id": "opencode_zai",
        "vendor_family": "z_ai",
        "sequence": ["T1", "T3", "T2"],
    },
}

OPENCODE_VERIFICATION_PREAMBLE = (
    "Work privately and do not reveal reasoning. Before emitting the requested "
    "final JSON, independently verify every stated constraint, recompute every "
    "reported aggregate from the selected answer, and compare all alternatives "
    "when the problem has a finite search space. Revise the answer if any check "
    "fails. Preserve the requested JSON schema and output no prose.\n\n"
)
OPENCODE_FINITE_VERIFIER_VERSION = "finite-json-v2"


def parse_stdout_json(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError(f"command emitted no JSON object: {completed.stderr[-500:]}")


def run_pao(script: Path, *args: str, expected: int = 0) -> dict[str, Any]:
    env = {**os.environ, "PAO_OA_ID": "oa-heterogeneous-ab"}
    completed = subprocess.run(
        [sys.executable, str(script), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
        timeout=60,
    )
    if completed.returncode != expected:
        raise RuntimeError(
            f"{script.name} returned {completed.returncode}, expected {expected}: "
            f"{completed.stderr[-1000:]}{completed.stdout[-1000:]}"
        )
    return parse_stdout_json(completed)


def executable(name: str) -> str:
    candidates = [f"{name}.cmd", f"{name}.exe", name] if os.name == "nt" else [name]
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    raise RuntimeError(f"required runtime is not installed: {name}")


def extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group(1)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("provider response must be a JSON object")
    return value


def run_codex(prompt: str, work_dir: Path) -> dict[str, Any]:
    provider_dir = work_dir / "codex"
    provider_dir.mkdir(parents=True, exist_ok=True)
    shim = Path(executable("codex"))
    entrypoint = shim.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    if not entrypoint.is_file():
        raise RuntimeError(f"Codex entrypoint not found beside shim: {entrypoint}")
    command = [
        executable("node"),
        str(entrypoint),
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--model",
        "gpt-5.6-sol",
        "--json",
        "-C",
        str(provider_dir),
        prompt,
    ]
    return run_provider_command("codex", command, parse_codex, provider_dir)


def parse_codex(stdout: str, stderr: str = "") -> tuple[str, dict[str, Any]]:
    _ = stderr
    answer = ""
    usage: dict[str, Any] = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                answer = str(item.get("text", ""))
        elif event.get("type") == "turn.completed":
            usage = event.get("usage") or {}
    return answer, {"usage": usage, "telemetry_complete": bool(usage)}


def run_claude(prompt: str, work_dir: Path) -> dict[str, Any]:
    provider_dir = work_dir / "claude"
    provider_dir.mkdir(parents=True, exist_ok=True)
    command = [
        executable("claude"),
        "-p",
        "--model",
        "sonnet",
        "--output-format",
        "json",
        prompt,
    ]
    return run_provider_command("claude", command, parse_claude, provider_dir)


def parse_claude(stdout: str, stderr: str = "") -> tuple[str, dict[str, Any]]:
    _ = stderr
    value = json.loads(stdout)
    model_usage = value.get("modelUsage") or {}
    return str(value.get("result", "")), {
        "usage": value.get("usage") or {},
        "model_usage": model_usage,
        "actual_models": sorted(model_usage),
        "cost_usd": value.get("total_cost_usd"),
        "telemetry_complete": bool(value.get("usage")),
    }


def run_kimi(prompt: str, work_dir: Path) -> dict[str, Any]:
    provider_dir = work_dir / "kimi"
    provider_dir.mkdir(parents=True, exist_ok=True)
    command = [
        executable("kimi"),
        "--print",
        "--output-format",
        "stream-json",
        "--max-steps-per-turn",
        "1",
        "--work-dir",
        str(provider_dir),
        "--model",
        "kimi-code/kimi-for-coding",
        "-p",
        prompt,
    ]
    result = run_provider_command("kimi", command, parse_kimi, provider_dir)
    session_id = result["metrics"].pop("_session_id", None)
    if not result["ok"] or not session_id:
        return result
    try:
        with tempfile.TemporaryDirectory(dir=provider_dir) as temporary:
            archive = Path(temporary) / "session.zip"
            exported = subprocess.run(
                [
                    executable("kimi"),
                    "export",
                    session_id,
                    "--output",
                    str(archive),
                    "--yes",
                ],
                cwd=provider_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=60,
            )
            if exported.returncode != 0 or not archive.is_file():
                raise RuntimeError(f"export_exit_{exported.returncode}")
            usage = read_kimi_usage(archive)
        result["metrics"]["usage"] = usage
        result["metrics"]["telemetry_complete"] = bool(usage)
    except Exception as error:
        result["metrics"]["telemetry_error"] = str(error)
    return result


def parse_kimi(stdout: str, stderr: str = "") -> tuple[str, dict[str, Any]]:
    texts: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("role") != "assistant":
            continue
        for part in event.get("content") or []:
            if part.get("type") == "text":
                texts.append(str(part.get("text", "")))
    session_match = re.search(
        r"To resume this session:\s+kimi\s+-r\s+([0-9a-f-]{36})",
        stdout + "\n" + stderr,
        re.IGNORECASE,
    )
    return "".join(texts), {
        "usage": None,
        "telemetry_complete": False,
        "_session_id": session_match.group(1) if session_match else None,
    }


def read_kimi_usage(archive: Path) -> dict[str, Any] | None:
    """Read only current-session wire telemetry; never retain diagnostic logs."""
    latest = None
    with zipfile.ZipFile(archive) as bundle:
        with bundle.open("wire.jsonl") as wire:
            for encoded in wire:
                try:
                    event = json.loads(encoded.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                message = event.get("message") or {}
                if message.get("type") != "StatusUpdate":
                    continue
                token_usage = (message.get("payload") or {}).get("token_usage")
                if isinstance(token_usage, dict) and token_usage:
                    latest = token_usage
    return latest


def build_opencode_command(
    entrypoint: Path, provider_dir: Path, prompt: str
) -> list[str]:
    return [
        str(entrypoint),
        "run",
        "--pure",
        "--model",
        "zai-coding-plan/glm-4.7",
        "--variant",
        "high",
        "--format",
        "json",
        "--dir",
        str(provider_dir),
        OPENCODE_VERIFICATION_PREAMBLE + prompt,
    ]


def verify_finite_json_answer(prompt: str, answer: str) -> list[str]:
    """Return answer-key-free deterministic verification failures."""
    try:
        parsed = extract_json_object(answer)
    except Exception:
        return ["invalid_json"]
    optimization = re.search(
        r"total cost <=(\d+) and risk <=(\d+)", prompt
    )
    item_matches = re.findall(
        r"([A-Z])\(cost(\d+),risk(\d+),value(\d+)\)", prompt
    )
    if optimization and item_matches:
        cost_limit, risk_limit = map(int, optimization.groups())
        items = [
            {
                "name": name,
                "cost": int(cost),
                "risk": int(risk),
                "value": int(value),
            }
            for name, cost, risk, value in item_matches
        ]
        by_name = {item["name"]: item for item in items}
        selection = parsed.get("selection")
        failures = []
        if (
            not isinstance(selection, list)
            or any(name not in by_name for name in selection)
            or selection != sorted(set(selection))
        ):
            return ["invalid_selection"]
        selected = [by_name[name] for name in selection]
        totals = {
            "cost": sum(item["cost"] for item in selected),
            "risk": sum(item["risk"] for item in selected),
            "value": sum(item["value"] for item in selected),
        }
        if any(parsed.get(key) != value for key, value in totals.items()):
            failures.append("aggregate_mismatch")
        if totals["cost"] > cost_limit or totals["risk"] > risk_limit:
            failures.append("constraint_violation")
        feasible = []
        for count in range(1, len(items) + 1):
            for subset in itertools.combinations(items, count):
                cost = sum(item["cost"] for item in subset)
                risk = sum(item["risk"] for item in subset)
                if cost <= cost_limit and risk <= risk_limit:
                    feasible.append(
                        {
                            "selection": [item["name"] for item in subset],
                            "value": sum(item["value"] for item in subset),
                            "cost": cost,
                            "risk": risk,
                        }
                    )
        optimum = min(
            feasible,
            key=lambda item: (
                -item["value"],
                item["cost"],
                item["risk"],
                item["selection"],
            ),
        )
        if any(parsed.get(key) != value for key, value in optimum.items()):
            failures.append("selection_not_global_optimum")
        return sorted(set(failures))
    ordering = re.search(r"Arrange A-([A-Z])", prompt)
    if ordering:
        last = ord(ordering.group(1))
        expected_letters = [
            chr(code) for code in range(ord("A"), last + 1)
        ]
        answer_value = parsed.get("answer")
        if (
            not isinstance(answer_value, str)
            or sorted(answer_value) != expected_letters
        ):
            return ["invalid_ordering_alphabet"]
        positions = {
            letter: answer_value.index(letter) for letter in expected_letters
        }
        failures = []
        immediate = re.findall(
            r"([A-Z]) is immediately before ([A-Z])", prompt
        )
        before = re.findall(r"([A-Z]) is before ([A-Z])", prompt)
        assigned = re.findall(r"([A-Z]) is in position ([0-9]+)", prompt)
        not_adjacent = re.findall(
            r"([A-Z]) is not adjacent to ([A-Z])", prompt
        )
        if any(
            positions[right] != positions[left] + 1
            for left, right in immediate
        ):
            failures.append("ordering_constraint_violation")
        if any(positions[left] >= positions[right] for left, right in before):
            failures.append("ordering_constraint_violation")
        if any(
            positions[letter] != int(position) - 1
            for letter, position in assigned
        ):
            failures.append("ordering_constraint_violation")
        if any(
            abs(positions[left] - positions[right]) == 1
            for left, right in not_adjacent
        ):
            failures.append("ordering_constraint_violation")
        return sorted(set(failures))
    return []


def build_finite_correction_feedback(
    prompt: str, failures: list[str]
) -> str:
    """Build prompt-derived correction hints without exposing an answer key."""
    unique_failures = sorted(set(failures))
    details = []
    ordering = re.search(r"Arrange A-([A-Z])", prompt)
    if "invalid_ordering_alphabet" in unique_failures:
        if ordering:
            last = ordering.group(1)
            length = ord(last) - ord("A") + 1
            details.append(
                'For invalid_ordering_alphabet, the "answer" value must be '
                f"a string matching ^[A-{last}]{{{length}}}$ and must use "
                f"every letter A through {last} exactly once. Include no "
                "whitespace, punctuation, separators, or extra characters."
            )
        else:
            details.append(
                'For invalid_ordering_alphabet, the "answer" value must use '
                "exactly the alphabet and length required by the original "
                "prompt, with no whitespace or separators."
            )
    if "ordering_constraint_violation" in unique_failures:
        details.append(
            "For ordering_constraint_violation, re-check every position, "
            "before, immediately-before, and not-adjacent relation in the "
            "original prompt."
        )
    feedback = (
        "\n\nThe previous JSON failed these deterministic checks: "
        + ", ".join(unique_failures)
        + "."
    )
    if details:
        feedback += " Specific correction requirements: " + " ".join(details)
    return (
        feedback
        + " Re-solve the original problem without assuming the prior answer "
        "is correct, then return only the corrected requested JSON."
    )


def combine_provider_results(
    first: dict[str, Any], second: dict[str, Any]
) -> dict[str, Any]:
    """Preserve all call telemetry while returning the final answer."""
    return {
        **second,
        "duration_s": round(first["duration_s"] + second["duration_s"], 3),
        "metrics": {
            **second["metrics"],
            "verification_attempt_count": 2,
            "attempts": [
                {
                    "ok": first["ok"],
                    "duration_s": first["duration_s"],
                    "error": first["error"],
                    "metrics": first["metrics"],
                },
                {
                    "ok": second["ok"],
                    "duration_s": second["duration_s"],
                    "error": second["error"],
                    "metrics": second["metrics"],
                },
            ],
            "telemetry_complete": bool(
                first["metrics"].get("telemetry_complete")
                and second["metrics"].get("telemetry_complete")
            ),
        },
    }


def run_opencode(prompt: str, work_dir: Path) -> dict[str, Any]:
    provider_dir = work_dir / "opencode"
    provider_dir.mkdir(parents=True, exist_ok=True)
    shim = Path(executable("opencode"))
    entrypoint = shim.parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
    if not entrypoint.is_file():
        raise RuntimeError(f"OpenCode entrypoint not found beside shim: {entrypoint}")
    command = build_opencode_command(entrypoint, provider_dir, prompt)
    first = run_provider_command(
        "opencode", command, parse_opencode, provider_dir
    )
    if not first["ok"]:
        return first
    failures = verify_finite_json_answer(prompt, first["answer"])
    if not failures:
        return first
    feedback = build_finite_correction_feedback(prompt, failures)
    second = run_provider_command(
        "opencode",
        build_opencode_command(entrypoint, provider_dir, prompt + feedback),
        parse_opencode,
        provider_dir,
    )
    combined = combine_provider_results(first, second)
    if second["ok"]:
        remaining = verify_finite_json_answer(prompt, second["answer"])
        if remaining:
            combined["ok"] = False
            combined["error"] = (
                "deterministic_verification_failed:" + ",".join(remaining)
            )
    return combined


def parse_opencode(stdout: str, stderr: str = "") -> tuple[str, dict[str, Any]]:
    _ = stderr
    texts: list[str] = []
    usage: dict[str, Any] = {}
    cost: float | int | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = event.get("part") or {}
        if event.get("type") == "text":
            texts.append(str(part.get("text", "")))
        elif event.get("type") == "step_finish":
            usage = part.get("tokens") or {}
            cost = part.get("cost")
    return "".join(texts), {
        "usage": usage,
        "cost_usd": cost,
        "telemetry_complete": bool(usage),
    }


def run_provider_command(
    adapter: str,
    command: list[str],
    parser: Any,
    work_dir: Path,
) -> dict[str, Any]:
    started = time.monotonic()
    child_env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "NO_COLOR": "1",
    }
    try:
        completed = subprocess.run(
            command,
            cwd=work_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env,
            check=False,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return {
            "adapter": adapter,
            "ok": False,
            "duration_s": round(time.monotonic() - started, 3),
            "error": "timeout_180s",
            "answer": "",
            "metrics": {"telemetry_complete": False},
        }
    duration_s = round(time.monotonic() - started, 3)
    if completed.returncode != 0:
        return {
            "adapter": adapter,
            "ok": False,
            "duration_s": duration_s,
            "error": f"exit_{completed.returncode}:{completed.stderr[-500:]}",
            "answer": "",
            "metrics": {"telemetry_complete": False},
        }
    try:
        answer, metrics = parser(completed.stdout, completed.stderr)
    except Exception as error:
        return {
            "adapter": adapter,
            "ok": False,
            "duration_s": duration_s,
            "error": f"parse_error:{error}",
            "answer": completed.stdout[-2000:],
            "metrics": {"telemetry_complete": False},
        }
    return {
        "adapter": adapter,
        "ok": bool(answer.strip()),
        "duration_s": duration_s,
        "error": None if answer.strip() else "empty_answer",
        "answer": answer,
        "metrics": metrics,
    }


PROVIDER_RUNNERS = {
    "LWAR1": run_codex,
    "LWAR2": run_claude,
    "LWAR3": run_kimi,
    "LWAR4": run_opencode,
}


def run_provider_with_retry(
    runner: Any,
    prompt: str,
    work_dir: Path,
    max_attempts: int = 2,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    attempts = []
    final = None
    for _ in range(max_attempts):
        result = runner(prompt, work_dir)
        attempts.append(
            {
                "ok": result["ok"],
                "duration_s": result["duration_s"],
                "error": result["error"],
                "metrics": result["metrics"],
            }
        )
        final = result
        if result["ok"]:
            break
    assert final is not None
    final["duration_s"] = round(sum(attempt["duration_s"] for attempt in attempts), 3)
    final["started_at"] = started_at
    final["metrics"] = {
        **final["metrics"],
        "attempt_count": len(attempts),
        "attempts": attempts,
        "telemetry_complete": all(
            bool(attempt["metrics"].get("telemetry_complete"))
            for attempt in attempts
        ),
    }
    return final


def grade(task_name: str, answer: str) -> dict[str, Any]:
    try:
        parsed = extract_json_object(answer)
    except Exception as error:
        return {"score": 0, "valid_json": False, "reason": f"invalid_json:{error}"}
    expected = TASKS[task_name]["expected"]
    exact = all(parsed.get(key) == value for key, value in expected.items())
    return {
        "score": 1 if exact else 0,
        "valid_json": True,
        "reason": "exact_match" if exact else "objective_mismatch",
        "parsed": parsed,
    }


def reported_tokens(metrics: dict[str, Any]) -> int | None:
    attempts = metrics.get("attempts")
    if isinstance(attempts, list):
        values = [
            reported_tokens(attempt.get("metrics") or {})
            for attempt in attempts
        ]
        return sum(value for value in values if value is not None) if all(
            value is not None for value in values
        ) else None
    usage = metrics.get("usage")
    if not isinstance(usage, dict):
        return None
    if "total" in usage:
        return int(usage["total"])
    if "input_tokens" in usage:
        return sum(
            int(usage.get(key, 0) or 0)
            for key in (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
            )
        )
    if "input_other" in usage:
        return sum(
            int(usage.get(key, 0) or 0)
            for key in (
                "input_other",
                "input_cache_read",
                "input_cache_creation",
                "output",
            )
        )
    return None


def routing_upper_bound(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    tasks = sorted({record["task"] for record in records})
    aliases = sorted({record["alias"] for record in records})
    per_task = {}
    for task_name in tasks:
        correct = [
            record
            for record in records
            if record["task"] == task_name
            and record["grade"]["score"] == 1
            and record["provider"].get("reported_tokens") is not None
        ]
        if not correct:
            return None
        winner = min(correct, key=lambda record: record["provider"]["reported_tokens"])
        per_task[task_name] = {
            "alias": winner["alias"],
            "reported_tokens": winner["provider"]["reported_tokens"],
        }
    full_quality = []
    for alias in aliases:
        subset = [record for record in records if record["alias"] == alias]
        if len(subset) != len(tasks) or not all(
            record["grade"]["score"] == 1
            and record["provider"].get("reported_tokens") is not None
            for record in subset
        ):
            continue
        full_quality.append(
            {
                "alias": alias,
                "reported_tokens": sum(
                    record["provider"]["reported_tokens"] for record in subset
                ),
            }
        )
    if not full_quality:
        return None
    baseline = min(full_quality, key=lambda item: item["reported_tokens"])
    oracle_tokens = sum(item["reported_tokens"] for item in per_task.values())
    savings = 100 * (baseline["reported_tokens"] - oracle_tokens) / baseline[
        "reported_tokens"
    ]
    return {
        "quality_score": len(tasks),
        "quality_total": len(tasks),
        "best_full_quality_single": baseline,
        "posthoc_oracle": {
            "reported_tokens": oracle_tokens,
            "per_task": per_task,
        },
        "reported_token_savings_percent": round(savings, 1),
        "claim_scope": "posthoc_upper_bound_not_heldout_routing_proof",
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_task_suite(path: Path) -> dict[str, dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != "pao.benchmark-suite.v1":
        raise ValueError("task suite requires schema_version pao.benchmark-suite.v1")
    tasks = value.get("tasks")
    if not isinstance(tasks, dict) or not tasks:
        raise ValueError("task suite requires a non-empty tasks object")
    normalized = {}
    for name, task in tasks.items():
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            raise ValueError(f"invalid task name: {name}")
        if not isinstance(task, dict):
            raise ValueError(f"task must be an object: {name}")
        if not isinstance(task.get("prompt"), str) or not task["prompt"]:
            raise ValueError(f"task requires a prompt: {name}")
        if not isinstance(task.get("expected"), dict) or not task["expected"]:
            raise ValueError(f"task requires an expected object: {name}")
        task_class = task.get("task_class")
        if not isinstance(task_class, str) or re.fullmatch(r"[a-z][a-z0-9_-]*", task_class) is None:
            raise ValueError(f"task requires a valid task_class: {name}")
        normalized[name] = {
            "prompt": task["prompt"],
            "expected": task["expected"],
            "task_class": task_class,
        }
    return normalized


def build_assignment(task_names: list[str]) -> dict[str, list[str]]:
    if tuple(task_names) == DEFAULT_TASK_NAMES:
        return {alias: list(adapter["sequence"]) for alias, adapter in ADAPTERS.items()}
    assignment = {}
    for index, alias in enumerate(ADAPTERS):
        shift = index % len(task_names)
        assignment[alias] = task_names[shift:] + task_names[:shift]
    return assignment


def task_id(alias: str, task_name: str) -> str:
    return f"task-ab-{alias.lower()}-{task_name.lower()}"


def workflow_id(alias: str) -> str:
    return f"workflow-ab-{alias.lower()}"


def build_report(root: Path, records: list[dict[str, Any]], audit: dict[str, Any]) -> str:
    accepted = sum(record["grade"]["score"] for record in records)
    calls_ok = sum(bool(record["provider"]["ok"]) for record in records)
    all_telemetry = all(
        bool(record["provider"]["metrics"].get("telemetry_complete"))
        for record in records
    )
    if calls_ok == len(records):
        verdict = (
            "provider_model_heterogeneity_validated_quality_measured_"
            + ("token_efficiency_measured" if all_telemetry else "token_efficiency_partial")
        )
    else:
        verdict = "provider_model_heterogeneity_execution_partial"
    lines = [
        "# Heterogeneous LWAR Blind A/B Report",
        "",
        f"- Verdict: `{verdict}`",
        f"- Provider calls: {calls_ok}/{len(records)} completed",
        f"- Provider retries: {sum(record['provider']['metrics'].get('attempt_count', 1) - 1 for record in records)}",
        f"- Objective acceptance: {accepted}/{len(records)}",
        f"- Audit status: `{audit.get('status', audit.get('event', 'unknown'))}`",
        "- Assignment: fixed Latin-square order; prompts and grader were provider-neutral",
        "",
        "| Alias | Provider family | Model | Accepted | Mean latency (s) | Mean reported tokens |",
        "|---|---|---|---:|---:|---:|",
    ]
    for alias, adapter in ADAPTERS.items():
        subset = [record for record in records if record["alias"] == alias]
        score = sum(record["grade"]["score"] for record in subset)
        latency = sum(record["provider"]["duration_s"] for record in subset) / len(subset)
        telemetry = sum(
            bool(record["provider"]["metrics"].get("telemetry_complete"))
            for record in subset
        )
        token_values = [
            reported_tokens(record["provider"]["metrics"]) for record in subset
        ]
        mean_tokens = (
            f"{sum(token_values) / len(token_values):.0f}"
            if telemetry == len(subset) and all(value is not None for value in token_values)
            else f"partial ({telemetry}/{len(subset)})"
        )
        lines.append(
            f"| {alias} | {adapter['vendor_family']} | {adapter['model']} | "
            f"{score}/{len(subset)} | {latency:.3f} | {mean_tokens} |"
        )
    lines.append("")
    if all_telemetry:
        lines += [
            "Token telemetry was captured for all four runtimes. Kimi usage was",
            "read only from the current session's temporary export `wire.jsonl`;",
            "the diagnostic archive was deleted and never persisted.",
        ]
    else:
        lines += [
            "At least one runtime did not expose token usage, so a complete",
            "four-way token/cost comparison is not claimed.",
        ]
    routing = routing_upper_bound(records)
    if routing is not None:
        baseline = routing["best_full_quality_single"]
        oracle = routing["posthoc_oracle"]
        lines += [
            "",
            f"A post-hoc quality-preserving oracle kept {routing['quality_score']}/"
            f"{routing['quality_total']} accepted while reducing reported tokens "
            f"from {baseline['reported_tokens']} ({baseline['alias']}) to "
            f"{oracle['reported_tokens']} ({routing['reported_token_savings_percent']}%).",
            "This proves a routing opportunity, not held-out predictive routing.",
        ]
    lines += ["", f"Machine-readable evidence: `{(root / 'experiment.json').name}`"]
    return "\n".join(lines) + "\n"


def main() -> int:
    global TASKS
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--task-suite",
        type=Path,
        help="objective task catalog using schema pao.benchmark-suite.v1",
    )
    parser.add_argument(
        "--summarize-existing",
        action="store_true",
        help="refresh normalized token fields and report from an existing experiment.json",
    )
    parser.add_argument(
        "--canary-profile",
        type=Path,
        help="routing profile used to publish side-effect-free online shadow tasks",
    )
    parser.add_argument(
        "--canary-policy",
        type=Path,
        help="confidence canary policy paired with --canary-profile",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    if bool(args.canary_profile) != bool(args.canary_policy):
        raise SystemExit("--canary-profile and --canary-policy must be used together")
    canary_mode = args.canary_profile is not None
    canary_profile = args.canary_profile.resolve() if args.canary_profile else None
    canary_policy = args.canary_policy.resolve() if args.canary_policy else None
    if args.task_suite:
        TASKS = load_task_suite(args.task_suite.resolve())
    if canary_mode and any(not task.get("task_class") for task in TASKS.values()):
        raise SystemExit("canary evidence requires task_class on every task")
    assignment = build_assignment(list(TASKS))
    if args.summarize_existing:
        experiment_path = root / "experiment.json"
        if not experiment_path.is_file():
            raise SystemExit(f"experiment.json does not exist: {experiment_path}")
        payload = json.loads(experiment_path.read_text(encoding="utf-8"))
        for record in payload["records"]:
            record["provider"]["reported_tokens"] = reported_tokens(
                record["provider"]["metrics"]
            )
        payload["analysis"] = {
            "routing_upper_bound": routing_upper_bound(payload["records"])
        }
        write_json(experiment_path, payload)
        (root / "report.md").write_text(
            build_report(root, payload["records"], payload["audit_health"]),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "event": "heterogeneous_ab_summary_refreshed",
                    "root": str(root),
                    "report": str(root / "report.md"),
                },
                sort_keys=True,
            )
        )
        return 0
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"experiment root must be absent or empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    work_dir = root / "workspace"
    work_dir.mkdir()

    requests: dict[str, dict[str, Any]] = {}
    for alias, adapter in ADAPTERS.items():
        requested = run_pao(
            LWAR_SCRIPT,
            "register",
            "--runtime-name",
            adapter["runtime_name"],
            "--model",
            adapter["model"],
            "--adapter-id",
            adapter["adapter_id"],
            "--vendor-family",
            adapter["vendor_family"],
            "--interface",
            "cli",
            "--capability",
            "blind-evaluation",
            "--root",
            str(root),
        )
        requests[alias] = requested
        reconciled = run_pao(OA_SCRIPT, "reconcile", "--root", str(root))
        if reconciled.get("registrations") != 1:
            raise RuntimeError(f"expected one registration for {alias}: {reconciled}")

    identities: dict[str, dict[str, Any]] = {}
    if canary_mode:
        for alias in ADAPTERS:
            adopted = run_pao(
                LWAR_SCRIPT,
                "response",
                requests[alias]["request_id"],
                "--root",
                str(root),
            )
            identities[alias] = {
                "identity_file": adopted["identity_file"],
                "lwar_id": alias,
            }
            idle = run_pao(
                ADP_SCRIPT,
                "--identity-file",
                identities[alias]["identity_file"],
                "--interval",
                "0.01",
                "--timeout",
                "0.05",
                "--root",
                str(root),
                expected=10,
            )
            if idle.get("event") != "idle_timeout":
                raise RuntimeError(f"expected idle activation for {alias}: {idle}")

    published: dict[tuple[str, str], dict[str, Any]] = {}
    for alias in ADAPTERS:
        for priority, task_name in enumerate(assignment[alias], start=1):
            draft = {
                "task_id": task_id(alias, task_name),
                "workflow_id": workflow_id(alias),
                "goal": f"Solve blind benchmark {task_name}",
                "instructions": TASKS[task_name]["prompt"],
                "completion_criteria": [
                    "Return one JSON object",
                    "Match the objective answer key",
                ],
                "cwd": str(work_dir),
                "timeout_s": 180,
                "permissions": {
                    "read": [str(work_dir)],
                    "write": [] if canary_mode else [str(work_dir)],
                    "network": False if canary_mode else True,
                },
                "max_retries": 0,
                "priority": priority,
            }
            draft_path = root / "drafts" / f"{alias}-{task_name}.json"
            write_json(draft_path, draft)
            send_args = [
                "send",
                "--task-file",
                str(draft_path),
                "--root",
                str(root),
            ]
            if canary_mode:
                send_args += [
                    "--auto",
                    "--require-capability",
                    "blind-evaluation",
                    "--routing-profile",
                    str(canary_profile),
                    "--routing-class",
                    TASKS[task_name]["task_class"],
                    "--canary-policy",
                    str(canary_policy),
                    "--routing-shadow-lwar-id",
                    alias,
                ]
            else:
                send_args += ["--lwar-id", alias]
            published[(alias, task_name)] = run_pao(OA_SCRIPT, *send_args)
            if canary_mode:
                routing = published[(alias, task_name)]
                if routing.get("lwar_id") != alias or routing.get("routing_mode") != "shadow":
                    raise RuntimeError(
                        f"canary shadow routing invariant failed for {alias}: {routing}"
                    )

    records: list[dict[str, Any]] = []
    for round_index in range(len(TASKS)):
        deliveries: dict[str, dict[str, Any]] = {}
        grants: dict[str, dict[str, Any]] = {}
        for alias, adapter in ADAPTERS.items():
            task_name = assignment[alias][round_index]
            if round_index == 0 and not canary_mode:
                delivery = run_pao(
                    LWAR_SCRIPT,
                    "response",
                    requests[alias]["request_id"],
                    "--resident",
                    "--interval",
                    "0.01",
                    "--timeout",
                    "0.2",
                    "--lease-seconds",
                    "210",
                    "--root",
                    str(root),
                )
                identities[alias] = {
                    "identity_file": delivery["identity_file"],
                    "lwar_id": alias,
                }
            else:
                delivery = run_pao(
                    ADP_SCRIPT,
                    "--identity-file",
                    identities[alias]["identity_file"],
                    "--resident",
                    "--interval",
                    "0.01",
                    "--timeout",
                    "0.2",
                    "--lease-seconds",
                    "210",
                    "--root",
                    str(root),
                )
            if delivery.get("task_id") != published[(alias, task_name)]["task_id"]:
                raise RuntimeError(f"unexpected task order for {alias}: {delivery}")
            deliveries[alias] = delivery
            grants[alias] = run_pao(
                LWAR_SCRIPT,
                "begin",
                "--identity-file",
                identities[alias]["identity_file"],
                "--task-id",
                delivery["task_id"],
                "--claim-token",
                delivery["task"]["claim_token"],
                "--execution-id",
                delivery["execution_id"],
                "--invocation-id",
                delivery["invocation_id"],
                "--root",
                str(root),
            )

        provider_results: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {}
            for alias, adapter in ADAPTERS.items():
                task_name = assignment[alias][round_index]
                futures[
                    executor.submit(
                        run_provider_with_retry,
                        PROVIDER_RUNNERS[alias],
                        TASKS[task_name]["prompt"],
                        work_dir,
                    )
                ] = alias
            for future in as_completed(futures):
                alias = futures[future]
                try:
                    provider_results[alias] = future.result()
                except Exception as error:
                    provider_results[alias] = {
                        "adapter": ADAPTERS[alias]["adapter_id"],
                        "ok": False,
                        "started_at": datetime.now(timezone.utc).isoformat().replace(
                            "+00:00", "Z"
                        ),
                        "duration_s": 0,
                        "error": str(error),
                        "answer": "",
                        "metrics": {"telemetry_complete": False},
                    }

        for alias, adapter in ADAPTERS.items():
            task_name = assignment[alias][round_index]
            provider = provider_results[alias]
            outcome = grade(task_name, provider["answer"]) if provider["ok"] else {
                "score": 0,
                "valid_json": False,
                "reason": provider["error"],
            }
            raw_path = root / "raw" / alias / f"{task_name}.txt"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(provider["answer"], encoding="utf-8")
            digest = hashlib.sha256(provider["answer"].encode("utf-8")).hexdigest()
            result = {
                "status": "succeeded" if outcome["score"] == 1 else "failed",
                "summary": (
                    "Blind task passed objective grader"
                    if outcome["score"] == 1
                    else "Blind task did not pass objective grader"
                ),
                "evidence": {
                    "benchmark": task_name,
                    "score": outcome["score"],
                    "valid_json": outcome["valid_json"],
                    "response_sha256": digest,
                },
                "artifacts": [],
                "next_action": "validate",
                "exit_code": 0 if outcome["score"] == 1 else 1,
                "error": None if outcome["score"] == 1 else outcome["reason"],
            }
            result_path = root / "results" / alias / f"{task_name}.json"
            write_json(result_path, result)
            run_pao(
                LWAR_SCRIPT,
                "complete",
                "--identity-file",
                identities[alias]["identity_file"],
                "--task-id",
                deliveries[alias]["task_id"],
                "--claim-token",
                deliveries[alias]["task"]["claim_token"],
                "--execution-token",
                grants[alias]["execution_token"],
                "--result-file",
                str(result_path),
                "--root",
                str(root),
            )
            collected = run_pao(
                OA_SCRIPT,
                "collect",
                "--lwar-id",
                alias,
                "--archive",
                "--root",
                str(root),
            )
            if collected.get("count") != 1 or collected.get("quarantined"):
                raise RuntimeError(f"collection invariant failed for {alias}: {collected}")
            decision = "accepted" if outcome["score"] == 1 else "rejected"
            validation_args = [
                "validate",
                "--task-id",
                deliveries[alias]["task_id"],
                "--workflow-id",
                workflow_id(alias),
                "--record",
                "--decision",
                decision,
                "--reason",
                f"blind_objective_{outcome['reason']}",
                "--root",
                str(root),
            ]
            observed_tokens = reported_tokens(provider["metrics"])
            if canary_mode and observed_tokens is not None:
                validation_args += [
                    "--routing-reported-tokens",
                    str(observed_tokens),
                ]
            validation = run_pao(OA_SCRIPT, *validation_args)
            provider_evidence = {
                key: value for key, value in provider.items() if key != "answer"
            }
            provider_evidence["reported_tokens"] = reported_tokens(provider["metrics"])
            records.append(
                {
                    "alias": alias,
                    "task": task_name,
                    "task_class": TASKS[task_name].get("task_class"),
                    "provider": provider_evidence,
                    "grade": outcome,
                    "validation": {
                        "decision": decision,
                        "mechanical_passed": validation.get("mechanical_passed"),
                        "routing_observation": validation.get("routing_observation"),
                        "routing_observation_sha256": validation.get(
                            "routing_observation_sha256"
                        ),
                    },
                    "claim_token_sha256": hashlib.sha256(
                        deliveries[alias]["task"]["claim_token"].encode("utf-8")
                    ).hexdigest(),
                    "execution_id": deliveries[alias]["execution_id"],
                    "invocation_epoch": deliveries[alias]["invocation_epoch"],
                }
            )

    shutdowns = []
    for alias in ADAPTERS:
        run_pao(
            OA_SCRIPT,
            "control",
            "--lwar-id",
            alias,
            "--command",
            "shutdown",
            "--root",
            str(root),
        )
        shutdowns.append(
            run_pao(
                ADP_SCRIPT,
                "--identity-file",
                identities[alias]["identity_file"],
                "--interval",
                "0.01",
                "--timeout",
                "0.2",
                "--root",
                str(root),
                expected=20,
            )
        )
    audit = run_pao(OA_SCRIPT, "audit-health", "--root", str(root))
    payload = {
        "schema_version": "pao.heterogeneous-ab.v1",
        "assignment": assignment,
        "tasks": {
            name: {"task_class": task.get("task_class")}
            for name, task in TASKS.items()
        },
        "profiles": {
            alias: {
                "vendor_family": adapter["vendor_family"],
                "model": adapter["model"],
                "adapter_id": adapter["adapter_id"],
            }
            for alias, adapter in ADAPTERS.items()
        },
        "records": records,
        "shutdowns": shutdowns,
        "audit_health": audit,
        "analysis": {"routing_upper_bound": routing_upper_bound(records)},
        "canary_evidence": {
            "enabled": canary_mode,
            "profile": str(canary_profile) if canary_profile else None,
            "policy": str(canary_policy) if canary_policy else None,
            "routing_observations": sum(
                bool(record["validation"].get("routing_observation"))
                for record in records
            ),
        },
    }
    write_json(root / "experiment.json", payload)
    report = build_report(root, records, audit)
    (root / "report.md").write_text(report, encoding="utf-8")
    print(
        json.dumps(
            {
                "event": "heterogeneous_ab_completed",
                "root": str(root),
                "provider_calls": sum(record["provider"]["ok"] for record in records),
                "provider_calls_total": len(records),
                "accepted": sum(record["grade"]["score"] for record in records),
                "accepted_total": len(records),
                "audit_status": audit.get("status"),
                "routing_observations": payload["canary_evidence"][
                    "routing_observations"
                ],
                "report": str(root / "report.md"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
