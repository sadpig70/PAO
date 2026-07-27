from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from .common import atomic_write_json, load_json, utc_now


CAPABILITY_SCHEMA = "pao.host-capability.v1"
RECEIPT_SCHEMA = "pao.host-execution-receipt.v1"
ADAPTER_ID = "qwen_code"
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
    try:
        return subprocess.run(
            [*command, *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as error:
        raise AdapterRejected(
            ["host_process_timeout"], f"host command timed out after {timeout_s}s"
        ) from error
    except OSError as error:
        raise AdapterRejected(
            ["host_process_start_failed"], f"host command could not start: {error}"
        ) from error


def inspect_qwen(command: Sequence[str], timeout_s: int = 20) -> dict[str, Any]:
    version_run = run_command(command, ["--version"], timeout_s)
    help_run = run_command(command, ["--help"], timeout_s)
    version = ANSI_RE.sub("", version_run.stdout).strip()
    help_text = ANSI_RE.sub("", help_run.stdout)
    missing = [flag for flag in REQUIRED_FLAGS if flag not in help_text]
    reasons = []
    if version_run.returncode != 0 or not version:
        reasons.append("version_probe_failed")
    if help_run.returncode != 0:
        reasons.append("help_probe_failed")
    if missing:
        reasons.append("required_flags_missing")
    return {
        "runtime_version": version or "unreported",
        "required_flags": {flag: flag not in missing for flag in REQUIRED_FLAGS},
        "static_eligible": not reasons,
        "reason_codes": sorted(reasons),
    }


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
    max_wall_time_s: int,
    process_timeout_s: int,
    max_provider_calls: int = 1,
) -> dict[str, Any]:
    completed = run_command(
        command, qwen_arguments(prompt, max_wall_time_s), process_timeout_s
    )
    return verify_qwen_output(
        completed, max_provider_calls=max_provider_calls
    )


def _host_requirement(task: dict[str, Any]) -> dict[str, Any]:
    adapter_options = task.get("adapter_options")
    contract = (
        adapter_options.get("host_contract")
        if isinstance(adapter_options, dict)
        else None
    )
    expected = {
        "adapter_id": ADAPTER_ID,
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
    command_name: str,
    command: Sequence[str],
    inspection: dict[str, Any],
    live: dict[str, Any] | None,
    live_error: AdapterRejected | None,
) -> dict[str, Any]:
    reasons = list(inspection["reason_codes"])
    if live is None:
        reasons.extend(
            live_error.reason_codes if live_error else ["live_probe_required"]
        )
    payload = {
        "schema_version": CAPABILITY_SCHEMA,
        "adapter_id": ADAPTER_ID,
        "runtime_name": "Qwen Code",
        "runtime_version": inspection["runtime_version"],
        "command": command_name,
        "discovered_at": utc_now(),
        "requirements": {
            "tool_policy": "deny_all",
            "token_telemetry": "exact_provider_report",
            "max_provider_calls": 1,
        },
        "enforcement": {
            "bare_mode": True,
            "max_tool_calls": 0,
            "output_format": "json",
            "required_flags": inspection["required_flags"],
        },
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


def command_probe(args: argparse.Namespace) -> int:
    try:
        command = resolve_command(args.command)
        inspection = inspect_qwen(command, args.probe_timeout_s)
        live = None
        live_error = None
        if args.live and inspection["static_eligible"]:
            try:
                live = execute_qwen(
                    command,
                    "Reply with exactly OK. Do not call tools.",
                    max_wall_time_s=args.max_wall_time_s,
                    process_timeout_s=args.process_timeout_s,
                )
            except AdapterRejected as error:
                live_error = error
        payload = capability_payload(
            args.command, command, inspection, live, live_error
        )
    except AdapterRejected as error:
        payload = {
            "schema_version": CAPABILITY_SCHEMA,
            "adapter_id": ADAPTER_ID,
            "runtime_name": "Qwen Code",
            "runtime_version": "unreported",
            "command": args.command,
            "discovered_at": utc_now(),
            "requirements": {
                "tool_policy": "deny_all",
                "token_telemetry": "exact_provider_report",
                "max_provider_calls": 1,
            },
            "enforcement": {
                "bare_mode": True,
                "max_tool_calls": 0,
                "output_format": "json",
                "required_flags": {flag: False for flag in REQUIRED_FLAGS},
            },
            "live_probe": None,
            "eligible": False,
            "reason_codes": error.reason_codes,
        }
        payload["contract_sha256"] = canonical_sha256(
            {key: payload[key] for key in ("adapter_id", "requirements", "eligible", "reason_codes")}
        )
    if args.output:
        atomic_write_json(Path(args.output).resolve(), payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["eligible"] else 4


def command_run(args: argparse.Namespace) -> int:
    task_path = Path(args.task_file).resolve()
    receipt_path = Path(args.receipt_file).resolve()
    task_id = "unbound"
    try:
        task = load_json(task_path)
        task_id = str(task.get("task_id", "unbound"))
        requirement = _host_requirement(task)
        prompt = Path(args.prompt_file).resolve().read_text(encoding="utf-8")
        if not prompt.strip():
            raise AdapterRejected(["prompt_empty"], "prompt file must not be empty")
        command = resolve_command(args.command)
        inspection = inspect_qwen(command, args.probe_timeout_s)
        if not inspection["static_eligible"]:
            raise AdapterRejected(
                inspection["reason_codes"], "Qwen static capability probe failed"
            )
        verified = execute_qwen(
            command,
            prompt,
            max_wall_time_s=args.max_wall_time_s,
            process_timeout_s=args.process_timeout_s,
            max_provider_calls=requirement["max_provider_calls"],
        )
        receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "adapter_id": ADAPTER_ID,
            "task_id": task_id,
            "created_at": utc_now(),
            "status": "accepted",
            "reason_codes": [],
            "runtime_version": inspection["runtime_version"],
            "model_ids": verified["model_ids"],
            "output": verified["output"],
            "usage": verified["usage"],
            "tool_calls": verified["tool_calls"],
            "provider_calls": verified["provider_calls"],
            "session_id": verified["session_id"],
            "raw_output_sha256": verified["raw_output_sha256"],
            "host_contract_sha256": canonical_sha256(requirement),
        }
        exit_code = 0
    except (OSError, ValueError, UnicodeError) as error:
        rejected = AdapterRejected(["input_invalid"], str(error))
        receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "adapter_id": ADAPTER_ID,
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
            "adapter_id": ADAPTER_ID,
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Machine-enforced host adapter contracts"
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    probe = subparsers.add_parser("qwen-probe")
    probe.add_argument("--command", default="qwen")
    probe.add_argument("--live", action="store_true")
    probe.add_argument("--output")
    probe.add_argument("--probe-timeout-s", type=int, default=20)
    probe.add_argument("--max-wall-time-s", type=int, default=60)
    probe.add_argument("--process-timeout-s", type=int, default=90)
    probe.set_defaults(handler=command_probe)

    run = subparsers.add_parser("qwen-run")
    run.add_argument("--command", default="qwen")
    run.add_argument("--task-file", required=True)
    run.add_argument("--prompt-file", required=True)
    run.add_argument("--receipt-file", required=True)
    run.add_argument("--probe-timeout-s", type=int, default=20)
    run.add_argument("--max-wall-time-s", type=int, default=300)
    run.add_argument("--process-timeout-s", type=int, default=330)
    run.set_defaults(handler=command_run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for name in ("probe_timeout_s", "max_wall_time_s", "process_timeout_s"):
        if getattr(args, name) < 1:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
