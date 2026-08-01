from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .common import atomic_write_json, load_json, utc_now


CAPABILITY_SCHEMA = "pao.host-capability.v1"
RECEIPT_SCHEMA = "pao.host-execution-receipt.v1"

# Backward-compatible default adapter identity (Qwen was the first adapter).
ADAPTER_ID = "qwen_code"
QWEN_ADAPTER_ID = "qwen_code"
QWEN_RUNTIME_NAME = "Qwen Code"
REQUIRED_FLAGS = (
    "--bare",
    "--output-format",
    "--max-tool-calls",
    "--max-wall-time",
)
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class AdapterRejected(RuntimeError):
    def __init__(self, reason_codes: Sequence[str], message: str):
        super().__init__(message)
        self.reason_codes = sorted(set(reason_codes))


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_command(command: str) -> list[str]:
    candidate = Path(command).expanduser()
    resolved = candidate.resolve() if candidate.exists() else None
    if resolved is None:
        found = shutil.which(command)
        if found:
            resolved = Path(found).resolve()
    if resolved is None:
        raise AdapterRejected(["command_not_found"], f"command not found: {command}")
    suffix = resolved.suffix.lower()
    if suffix == ".ps1":
        pwsh = shutil.which("pwsh")
        if not pwsh:
            raise AdapterRejected(
                ["pwsh_not_found"], "pwsh is required to invoke a PowerShell adapter"
            )
        return [str(Path(pwsh).resolve()), "-NoProfile", "-File", str(resolved)]
    if suffix == ".py":
        return [sys.executable, str(resolved)]
    return [str(resolved)]


def run_command(
    command: Sequence[str], arguments: Sequence[str], timeout_s: int
) -> subprocess.CompletedProcess[str]:
    # A wide COLUMNS keeps boxed CLI help (e.g. Kimi) from truncating long flag
    # names, so static flag discovery is not defeated by the terminal width.
    env = {**os.environ, "COLUMNS": "200"}
    try:
        return subprocess.run(
            [*command, *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        raise AdapterRejected(
            ["host_process_timeout"], f"host command timed out after {timeout_s}s"
        ) from error
    except OSError as error:
        raise AdapterRejected(
            ["host_process_start_failed"], f"host command could not start: {error}"
        ) from error


def _static_inspection(
    command: Sequence[str], required_flags: Sequence[str], timeout_s: int
) -> dict[str, Any]:
    version_run = run_command(command, ["--version"], timeout_s)
    help_run = run_command(command, ["--help"], timeout_s)
    version = ANSI_RE.sub("", version_run.stdout).strip()
    help_text = ANSI_RE.sub("", help_run.stdout)
    missing = [flag for flag in required_flags if flag not in help_text]
    reasons = []
    if version_run.returncode != 0 or not version:
        reasons.append("version_probe_failed")
    if help_run.returncode != 0:
        reasons.append("help_probe_failed")
    if missing:
        reasons.append("required_flags_missing")
    return {
        "runtime_version": version or "unreported",
        "required_flags": {flag: flag not in missing for flag in required_flags},
        "static_eligible": not reasons,
        "reason_codes": sorted(reasons),
    }


# ---------------------------------------------------------------------------
# Qwen Code adapter
# ---------------------------------------------------------------------------


def inspect_qwen(command: Sequence[str], timeout_s: int = 20) -> dict[str, Any]:
    return _static_inspection(command, REQUIRED_FLAGS, timeout_s)


def qwen_arguments(prompt: str, max_wall_time_s: int) -> list[str]:
    return [
        "--bare",
        "--prompt",
        prompt,
        "--output-format",
        "json",
        "--max-tool-calls",
        "0",
        "--max-wall-time",
        str(max_wall_time_s),
    ]


def _result_record(stdout: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        decoded = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise AdapterRejected(
            ["invalid_json_output"], "Qwen output is not one complete JSON document"
        ) from error
    records = decoded if isinstance(decoded, list) else [decoded]
    if not records or any(not isinstance(item, dict) for item in records):
        raise AdapterRejected(
            ["invalid_json_output"], "Qwen output must contain JSON object records"
        )
    results = [item for item in records if item.get("type") == "result"]
    if len(results) != 1:
        raise AdapterRejected(
            ["terminal_result_count_invalid"],
            f"expected one terminal result record, observed {len(results)}",
        )
    return records, results[0]


def verify_qwen_output(
    completed: subprocess.CompletedProcess[str],
    *,
    max_provider_calls: int = 1,
) -> dict[str, Any]:
    reason_codes: list[str] = []
    try:
        records, terminal = _result_record(completed.stdout)
    except AdapterRejected as error:
        if completed.returncode != 0:
            error.reason_codes.append("host_process_nonzero")
            error.reason_codes = sorted(set(error.reason_codes))
        raise

    if completed.returncode != 0:
        reason_codes.append("host_process_nonzero")
    if terminal.get("subtype") != "success" or terminal.get("is_error") is not False:
        reason_codes.append("terminal_result_not_success")

    usage = terminal.get("usage")
    token_keys = ("input_tokens", "output_tokens", "total_tokens")
    if not isinstance(usage, dict) or any(
        not isinstance(usage.get(key), int) or usage[key] < 0 for key in token_keys
    ):
        reason_codes.append("exact_token_telemetry_missing")
        normalized_usage = None
    else:
        normalized_usage = {key: usage[key] for key in token_keys}
        if usage["input_tokens"] + usage["output_tokens"] != usage["total_tokens"]:
            reason_codes.append("token_total_mismatch")

    stats = terminal.get("stats")
    tools = stats.get("tools") if isinstance(stats, dict) else None
    if not isinstance(tools, dict) or not isinstance(tools.get("totalCalls"), int):
        reason_codes.append("tool_telemetry_missing")
        tool_calls = None
    else:
        tool_calls = tools["totalCalls"]
        if tool_calls != 0:
            reason_codes.append("tool_call_observed")

    models = stats.get("models") if isinstance(stats, dict) else None
    provider_calls = 0
    model_ids: list[str] = []
    stats_total = 0
    if not isinstance(models, dict) or not models:
        reason_codes.append("provider_call_telemetry_missing")
        provider_calls_value: int | None = None
    else:
        provider_call_valid = True
        stats_token_valid = True
        for model_id, model_stats in models.items():
            if not isinstance(model_id, str) or not isinstance(model_stats, dict):
                provider_call_valid = False
                stats_token_valid = False
                continue
            model_ids.append(model_id)
            api = model_stats.get("api")
            tokens = model_stats.get("tokens")
            if not isinstance(api, dict) or not isinstance(api.get("totalRequests"), int):
                provider_call_valid = False
            else:
                provider_calls += api["totalRequests"]
            if not isinstance(tokens, dict) or not isinstance(tokens.get("total"), int):
                stats_token_valid = False
            else:
                stats_total += tokens["total"]
        provider_calls_value = provider_calls if provider_call_valid else None
        if not provider_call_valid:
            reason_codes.append("provider_call_telemetry_missing")
        elif provider_calls > max_provider_calls:
            reason_codes.append("provider_call_limit_exceeded")
        if (
            normalized_usage is not None
            and stats_token_valid
            and stats_total != normalized_usage["total_tokens"]
        ):
            reason_codes.append("provider_token_total_mismatch")

    if reason_codes:
        raise AdapterRejected(
            reason_codes,
            "Qwen execution failed the machine-enforced host contract",
        )
    return {
        "output": terminal.get("result", ""),
        "usage": normalized_usage,
        "tool_calls": tool_calls,
        "provider_calls": provider_calls_value,
        "model_ids": sorted(model_ids),
        "session_id": terminal.get("session_id"),
        "raw_output_sha256": hashlib.sha256(
            completed.stdout.encode("utf-8")
        ).hexdigest(),
        "records": len(records),
    }


def execute_qwen(
    command: Sequence[str],
    prompt: str,
    *,
    work_dir: Path | None = None,
    max_wall_time_s: int,
    process_timeout_s: int,
    max_provider_calls: int = 1,
) -> dict[str, Any]:
    _ = work_dir  # Qwen runs statelessly; the shared adapter signature carries work_dir.
    completed = run_command(
        command, qwen_arguments(prompt, max_wall_time_s), process_timeout_s
    )
    return verify_qwen_output(
        completed, max_provider_calls=max_provider_calls
    )


def qwen_enforcement(inspection: dict[str, Any]) -> dict[str, Any]:
    return {
        "bare_mode": True,
        "max_tool_calls": 0,
        "output_format": "json",
        "required_flags": inspection["required_flags"],
    }


# ---------------------------------------------------------------------------
# Kimi Code CLI adapter
# ---------------------------------------------------------------------------

KIMI_ADAPTER_ID = "kimi_cli"
KIMI_RUNTIME_NAME = "Kimi Code CLI"
KIMI_MODEL = "kimi-code/kimi-for-coding"
KIMI_REQUIRED_FLAGS = (
    "--print",
    "--output-format",
    "--max-steps-per-turn",
    "--work-dir",
    "--model",
)
# ASSUMPTION (confirm against a real Kimi tool-use sample): any assistant content
# part type or event role containing one of these substrings marks a tool signal.
KIMI_TOOL_SIGNAL_TOKENS = ("tool", "function")
KIMI_SESSION_RE = re.compile(
    r"To resume this session:\s+kimi\s+-r\s+([0-9a-f-]{36})", re.IGNORECASE
)


def inspect_kimi(command: Sequence[str], timeout_s: int = 20) -> dict[str, Any]:
    return _static_inspection(command, KIMI_REQUIRED_FLAGS, timeout_s)


def kimi_arguments(prompt: str, work_dir: Path, max_steps_per_turn: int = 1) -> list[str]:
    return [
        "--print",
        "--output-format",
        "stream-json",
        "--max-steps-per-turn",
        str(max_steps_per_turn),
        "--work-dir",
        str(work_dir),
        "--model",
        KIMI_MODEL,
        "-p",
        prompt,
    ]


def _kimi_tool_signal(role: Any, part_type: Any) -> bool:
    token = str(part_type or "").lower()
    if any(marker in token for marker in KIMI_TOOL_SIGNAL_TOKENS):
        return True
    return str(role or "").lower() == "tool"


def parse_kimi_stream(
    stdout: str, stderr: str = ""
) -> tuple[str, str | None, bool, list[str]]:
    """Return (answer, session_id, tool_observed, nontext_part_types)."""
    texts: list[str] = []
    tool_observed = False
    nontext_types: set[str] = set()
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        role = event.get("role")
        if _kimi_tool_signal(role, event.get("type")):
            tool_observed = True
        if role != "assistant":
            continue
        for part in event.get("content") or []:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type == "text":
                texts.append(str(part.get("text", "")))
                continue
            nontext_types.add(str(part_type))
            if _kimi_tool_signal(role, part_type):
                tool_observed = True
    session = KIMI_SESSION_RE.search(stdout + "\n" + stderr)
    return "".join(texts), (session.group(1) if session else None), tool_observed, sorted(
        nontext_types
    )


def read_kimi_usage(archive: Path) -> dict[str, Any] | None:
    """Read only current-session wire token telemetry; never diagnostic logs."""
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


def normalize_kimi_usage(token_usage: Any) -> dict[str, int]:
    """Fold Kimi component token counts into input/output/total integers.

    Kimi reports split components (e.g. ``input_other``, ``input_cache_read``,
    ``output``) with no explicit total. Every component must be a non-negative
    integer classifiable as input or output; an unclassifiable key fails closed
    so an unrecognised telemetry shape is never silently undercounted.
    """
    if not isinstance(token_usage, dict) or not token_usage:
        raise AdapterRejected(
            ["exact_token_telemetry_missing"], "Kimi token_usage is absent"
        )
    input_tokens = 0
    output_tokens = 0
    for key, value in token_usage.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise AdapterRejected(
                ["exact_token_telemetry_missing"],
                f"non-integer Kimi token component: {key}",
            )
        classifier = str(key).lower()
        if classifier.startswith("input"):
            input_tokens += value
        elif classifier.startswith("output"):
            output_tokens += value
        else:
            raise AdapterRejected(
                ["unclassified_token_component"],
                f"unclassified Kimi token component: {key}",
            )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def execute_kimi(
    command: Sequence[str],
    prompt: str,
    *,
    work_dir: Path | None = None,
    max_wall_time_s: int,
    process_timeout_s: int,
    max_provider_calls: int = 1,
) -> dict[str, Any]:
    _ = max_wall_time_s  # Kimi has no wall-time flag; process_timeout_s bounds it.
    _ = max_provider_calls  # Kimi token_usage does not expose a provider-call count.
    if work_dir is None:
        raise AdapterRejected(["work_dir_required"], "Kimi execution requires a work_dir")
    work_dir = Path(work_dir)
    completed = run_command(
        command, kimi_arguments(prompt, work_dir), process_timeout_s
    )
    reason_codes: list[str] = []
    answer, session_id, tool_observed, _nontext = parse_kimi_stream(
        completed.stdout, completed.stderr
    )
    if completed.returncode != 0:
        reason_codes.append("host_process_nonzero")
    if tool_observed:
        reason_codes.append("tool_call_observed")
    if not answer.strip():
        reason_codes.append("empty_answer")

    usage: dict[str, int] | None = None
    if session_id is None:
        reason_codes.append("session_id_missing")
        reason_codes.append("exact_token_telemetry_missing")
    else:
        try:
            with tempfile.TemporaryDirectory(dir=str(work_dir)) as temporary:
                archive = Path(temporary) / "session.zip"
                exported = run_command(
                    command,
                    ["export", session_id, "--output", str(archive), "--yes"],
                    process_timeout_s,
                )
                if exported.returncode != 0 or not archive.is_file():
                    reason_codes.append("telemetry_export_failed")
                else:
                    usage = normalize_kimi_usage(read_kimi_usage(archive))
        except AdapterRejected as error:
            reason_codes.extend(error.reason_codes)
        except (OSError, zipfile.BadZipFile, KeyError) as error:
            reason_codes.append("telemetry_export_failed")
            _ = error

    if reason_codes:
        raise AdapterRejected(
            sorted(set(reason_codes)),
            "Kimi execution failed the machine-enforced host contract",
        )
    return {
        "output": answer,
        "usage": usage,
        "tool_calls": 0,
        "provider_calls": None,
        "model_ids": [KIMI_MODEL],
        "session_id": session_id,
        "raw_output_sha256": hashlib.sha256(
            completed.stdout.encode("utf-8")
        ).hexdigest(),
        "records": None,
    }


def kimi_enforcement(inspection: dict[str, Any]) -> dict[str, Any]:
    return {
        "bare_mode": False,
        "max_tool_calls": 0,
        "output_format": "stream-json",
        "max_steps_per_turn": 1,
        "required_flags": inspection["required_flags"],
    }


# ---------------------------------------------------------------------------
# Adapter registry and shared probe/run contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterSpec:
    adapter_id: str
    runtime_name: str
    default_command: str
    required_flags: tuple[str, ...]
    inspect: Callable[[Sequence[str], int], dict[str, Any]]
    execute: Callable[..., dict[str, Any]]
    enforcement: Callable[[dict[str, Any]], dict[str, Any]]
    live_prompt: str = "Reply with exactly OK. Do not call tools."


QWEN_SPEC = AdapterSpec(
    adapter_id=QWEN_ADAPTER_ID,
    runtime_name=QWEN_RUNTIME_NAME,
    default_command="qwen",
    required_flags=REQUIRED_FLAGS,
    inspect=inspect_qwen,
    execute=execute_qwen,
    enforcement=qwen_enforcement,
)

KIMI_SPEC = AdapterSpec(
    adapter_id=KIMI_ADAPTER_ID,
    runtime_name=KIMI_RUNTIME_NAME,
    default_command="kimi",
    required_flags=KIMI_REQUIRED_FLAGS,
    inspect=inspect_kimi,
    execute=execute_kimi,
    enforcement=kimi_enforcement,
)

ADAPTERS = {spec.adapter_id: spec for spec in (QWEN_SPEC, KIMI_SPEC)}


def _host_requirement(task: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    adapter_options = task.get("adapter_options")
    contract = (
        adapter_options.get("host_contract")
        if isinstance(adapter_options, dict)
        else None
    )
    expected = {
        "adapter_id": adapter_id,
        "tool_policy": "deny_all",
        "token_telemetry": "exact_provider_report",
        "max_provider_calls": 1,
    }
    if not isinstance(contract, dict):
        raise AdapterRejected(
            ["host_contract_missing"], "task adapter_options.host_contract is required"
        )
    mismatches = []
    for key, value in expected.items():
        observed = contract.get(key)
        if key == "max_provider_calls":
            if (
                not isinstance(observed, int)
                or isinstance(observed, bool)
                or observed != value
            ):
                mismatches.append(key)
        elif observed != value:
            mismatches.append(key)
    unexpected = sorted(set(contract) - set(expected))
    if mismatches:
        raise AdapterRejected(
            [f"host_contract_{key}_mismatch" for key in mismatches],
            f"task host contract mismatch: {', '.join(mismatches)}",
        )
    if unexpected:
        raise AdapterRejected(
            ["host_contract_unexpected_fields"],
            f"task host contract has unexpected fields: {', '.join(unexpected)}",
        )
    return expected


def capability_payload(
    spec: AdapterSpec,
    command_name: str,
    command: Sequence[str],
    inspection: dict[str, Any],
    live: dict[str, Any] | None,
    live_error: AdapterRejected | None,
) -> dict[str, Any]:
    _ = command
    reasons = list(inspection["reason_codes"])
    if live is None:
        reasons.extend(
            live_error.reason_codes if live_error else ["live_probe_required"]
        )
    payload = {
        "schema_version": CAPABILITY_SCHEMA,
        "adapter_id": spec.adapter_id,
        "runtime_name": spec.runtime_name,
        "runtime_version": inspection["runtime_version"],
        "command": command_name,
        "discovered_at": utc_now(),
        "requirements": {
            "tool_policy": "deny_all",
            "token_telemetry": "exact_provider_report",
            "max_provider_calls": 1,
        },
        "enforcement": spec.enforcement(inspection),
        "live_probe": (
            {
                "provider_calls": live["provider_calls"],
                "tool_calls": live["tool_calls"],
                "usage": live["usage"],
                "model_ids": live["model_ids"],
                "raw_output_sha256": live["raw_output_sha256"],
            }
            if live is not None
            else None
        ),
        "eligible": not reasons,
        "reason_codes": sorted(set(reasons)),
    }
    payload["contract_sha256"] = canonical_sha256(
        {
            "adapter_id": payload["adapter_id"],
            "runtime_version": payload["runtime_version"],
            "requirements": payload["requirements"],
            "enforcement": payload["enforcement"],
            "eligible": payload["eligible"],
            "reason_codes": payload["reason_codes"],
        }
    )
    return payload


def _rejected_capability(spec: AdapterSpec, command_name: str, reason_codes: Sequence[str]) -> dict[str, Any]:
    payload = {
        "schema_version": CAPABILITY_SCHEMA,
        "adapter_id": spec.adapter_id,
        "runtime_name": spec.runtime_name,
        "runtime_version": "unreported",
        "command": command_name,
        "discovered_at": utc_now(),
        "requirements": {
            "tool_policy": "deny_all",
            "token_telemetry": "exact_provider_report",
            "max_provider_calls": 1,
        },
        "enforcement": spec.enforcement(
            {"required_flags": {flag: False for flag in spec.required_flags}}
        ),
        "live_probe": None,
        "eligible": False,
        "reason_codes": sorted(set(reason_codes)),
    }
    payload["contract_sha256"] = canonical_sha256(
        {key: payload[key] for key in ("adapter_id", "requirements", "eligible", "reason_codes")}
    )
    return payload


def command_probe(args: argparse.Namespace) -> int:
    spec: AdapterSpec = args.spec
    try:
        command = resolve_command(args.command)
        inspection = spec.inspect(command, args.probe_timeout_s)
        live = None
        live_error = None
        if args.live and inspection["static_eligible"]:
            with tempfile.TemporaryDirectory() as temporary:
                try:
                    live = spec.execute(
                        command,
                        spec.live_prompt,
                        work_dir=Path(temporary),
                        max_wall_time_s=args.max_wall_time_s,
                        process_timeout_s=args.process_timeout_s,
                    )
                except AdapterRejected as error:
                    live_error = error
        payload = capability_payload(
            spec, args.command, command, inspection, live, live_error
        )
    except AdapterRejected as error:
        payload = _rejected_capability(spec, args.command, error.reason_codes)
    if args.output:
        atomic_write_json(Path(args.output).resolve(), payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["eligible"] else 4


def command_run(args: argparse.Namespace) -> int:
    spec: AdapterSpec = args.spec
    task_path = Path(args.task_file).resolve()
    receipt_path = Path(args.receipt_file).resolve()
    task_id = "unbound"
    try:
        task = load_json(task_path)
        task_id = str(task.get("task_id", "unbound"))
        requirement = _host_requirement(task, spec.adapter_id)
        prompt = Path(args.prompt_file).resolve().read_text(encoding="utf-8")
        if not prompt.strip():
            raise AdapterRejected(["prompt_empty"], "prompt file must not be empty")
        command = resolve_command(args.command)
        inspection = spec.inspect(command, args.probe_timeout_s)
        if not inspection["static_eligible"]:
            raise AdapterRejected(
                inspection["reason_codes"],
                f"{spec.runtime_name} static capability probe failed",
            )
        with tempfile.TemporaryDirectory() as temporary:
            verified = spec.execute(
                command,
                prompt,
                work_dir=Path(temporary),
                max_wall_time_s=args.max_wall_time_s,
                process_timeout_s=args.process_timeout_s,
                max_provider_calls=requirement["max_provider_calls"],
            )
        receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "adapter_id": spec.adapter_id,
            "task_id": task_id,
            "created_at": utc_now(),
            "status": "accepted",
            "reason_codes": [],
            "runtime_version": inspection["runtime_version"],
            "model_ids": verified["model_ids"],
            "output": verified["output"],
            "usage": verified["usage"],
            "tool_calls": verified["tool_calls"],
            "session_id": verified["session_id"],
            "raw_output_sha256": verified["raw_output_sha256"],
            "host_contract_sha256": canonical_sha256(requirement),
        }
        if verified["provider_calls"] is not None:
            receipt["provider_calls"] = verified["provider_calls"]
        exit_code = 0
    except (OSError, ValueError, UnicodeError) as error:
        rejected = AdapterRejected(["input_invalid"], str(error))
        receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "adapter_id": spec.adapter_id,
            "task_id": task_id,
            "created_at": utc_now(),
            "status": "rejected",
            "reason_codes": rejected.reason_codes,
            "error": str(rejected),
        }
        exit_code = 4
    except AdapterRejected as error:
        receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "adapter_id": spec.adapter_id,
            "task_id": task_id,
            "created_at": utc_now(),
            "status": "rejected",
            "reason_codes": error.reason_codes,
            "error": str(error),
        }
        exit_code = 4
    atomic_write_json(receipt_path, receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return exit_code


def _add_probe(subparsers: Any, name: str, spec: AdapterSpec) -> None:
    probe = subparsers.add_parser(name)
    probe.add_argument("--command", default=spec.default_command)
    probe.add_argument("--live", action="store_true")
    probe.add_argument("--output")
    probe.add_argument("--probe-timeout-s", type=int, default=20)
    probe.add_argument("--max-wall-time-s", type=int, default=60)
    probe.add_argument("--process-timeout-s", type=int, default=90)
    probe.set_defaults(handler=command_probe, spec=spec)


def _add_run(subparsers: Any, name: str, spec: AdapterSpec) -> None:
    run = subparsers.add_parser(name)
    run.add_argument("--command", default=spec.default_command)
    run.add_argument("--task-file", required=True)
    run.add_argument("--prompt-file", required=True)
    run.add_argument("--receipt-file", required=True)
    run.add_argument("--probe-timeout-s", type=int, default=20)
    run.add_argument("--max-wall-time-s", type=int, default=300)
    run.add_argument("--process-timeout-s", type=int, default=330)
    run.set_defaults(handler=command_run, spec=spec)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Machine-enforced host adapter contracts"
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)
    _add_probe(subparsers, "qwen-probe", QWEN_SPEC)
    _add_run(subparsers, "qwen-run", QWEN_SPEC)
    _add_probe(subparsers, "kimi-probe", KIMI_SPEC)
    _add_run(subparsers, "kimi-run", KIMI_SPEC)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for name in ("probe_timeout_s", "max_wall_time_s", "process_timeout_s"):
        if getattr(args, name) < 1:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
