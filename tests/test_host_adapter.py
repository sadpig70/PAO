import contextlib
import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from pao_helpers import REPO

from pao_runtime.contracts import validate_contract
from pao_runtime.host_adapter import (
    AdapterRejected,
    kimi_arguments,
    main,
    normalize_kimi_usage,
    parse_kimi_stream,
    qwen_arguments,
    read_kimi_usage,
    verify_qwen_output,
)


def qwen_payload(*, tool_calls=0, total_tokens=15, provider_calls=1):
    return [
        {
            "type": "system",
            "subtype": "init",
            "model": "qwen/test",
            "qwen_code_version": "0.18.3",
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "session-test",
            "result": "EADBCF",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": total_tokens,
            },
            "stats": {
                "models": {
                    "qwen/test": {
                        "api": {"totalRequests": provider_calls},
                        "tokens": {"total": total_tokens},
                    }
                },
                "tools": {"totalCalls": tool_calls},
            },
        },
    ]


class HostAdapterUnitTests(unittest.TestCase):
    def completed(self, payload, returncode=0):
        return subprocess.CompletedProcess(
            args=["qwen"], returncode=returncode, stdout=json.dumps(payload), stderr=""
        )

    def test_required_qwen_arguments_are_machine_enforced(self):
        arguments = qwen_arguments("answer", 60)
        self.assertIn("--bare", arguments)
        self.assertEqual(arguments[arguments.index("--max-tool-calls") + 1], "0")
        self.assertEqual(arguments[arguments.index("--output-format") + 1], "json")
        self.assertEqual(arguments[arguments.index("--max-wall-time") + 1], "60")

    def test_exact_zero_tool_single_call_output_is_accepted(self):
        verified = verify_qwen_output(self.completed(qwen_payload()))
        self.assertEqual(verified["output"], "EADBCF")
        self.assertEqual(verified["usage"]["total_tokens"], 15)
        self.assertEqual(verified["tool_calls"], 0)
        self.assertEqual(verified["provider_calls"], 1)

    def test_any_tool_call_is_rejected(self):
        with self.assertRaises(AdapterRejected) as caught:
            verify_qwen_output(self.completed(qwen_payload(tool_calls=1)))
        self.assertIn("tool_call_observed", caught.exception.reason_codes)

    def test_missing_or_inconsistent_token_telemetry_is_rejected(self):
        payload = qwen_payload(total_tokens=16)
        with self.assertRaises(AdapterRejected) as caught:
            verify_qwen_output(self.completed(payload))
        self.assertIn("token_total_mismatch", caught.exception.reason_codes)

    def test_second_provider_call_is_rejected(self):
        with self.assertRaises(AdapterRejected) as caught:
            verify_qwen_output(self.completed(qwen_payload(provider_calls=2)))
        self.assertIn("provider_call_limit_exceeded", caught.exception.reason_codes)


class HostAdapterCliTests(unittest.TestCase):
    def write_fake_qwen(self, root: Path, payload) -> Path:
        path = root / "fake_qwen.py"
        path.write_text(
            "\n".join(
                [
                    "import json, sys",
                    "if '--version' in sys.argv:",
                    "    print('0.18.3')",
                    "elif '--help' in sys.argv:",
                    "    print('--bare --output-format --max-tool-calls --max-wall-time')",
                    "else:",
                    f"    print({json.dumps(json.dumps(payload))})",
                ]
            ),
            encoding="utf-8",
        )
        return path

    def task(self):
        return {
            "task_id": "task-host-contract",
            "adapter_options": {
                "host_contract": {
                    "adapter_id": "qwen_code",
                    "tool_policy": "deny_all",
                    "token_telemetry": "exact_provider_report",
                    "max_provider_calls": 1,
                }
            },
        }

    def test_live_probe_and_run_write_valid_contracts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = self.write_fake_qwen(root, qwen_payload())
            capability = root / "capability.json"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(
                    [
                        "qwen-probe",
                        "--command",
                        str(fake),
                        "--live",
                        "--output",
                        str(capability),
                    ]
                )
            self.assertEqual(code, 0, output.getvalue())
            capability_payload = json.loads(capability.read_text(encoding="utf-8"))
            validate_contract(
                capability_payload, "host-capability.schema.json"
            )
            self.assertTrue(capability_payload["eligible"])

            task = root / "task.json"
            prompt = root / "prompt.txt"
            receipt = root / "receipt.json"
            task.write_text(json.dumps(self.task()), encoding="utf-8")
            prompt.write_text("Return the ordering only.", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(
                    [
                        "qwen-run",
                        "--command",
                        str(fake),
                        "--task-file",
                        str(task),
                        "--prompt-file",
                        str(prompt),
                        "--receipt-file",
                        str(receipt),
                    ]
                )
            self.assertEqual(code, 0, output.getvalue())
            receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
            validate_contract(
                receipt_payload, "host-execution-receipt.schema.json"
            )
            self.assertEqual(receipt_payload["status"], "accepted")
            self.assertEqual(receipt_payload["tool_calls"], 0)

    def test_missing_task_contract_writes_rejection_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = self.write_fake_qwen(root, qwen_payload())
            task = root / "task.json"
            prompt = root / "prompt.txt"
            receipt = root / "receipt.json"
            task.write_text(
                json.dumps({"task_id": "task-no-host-contract"}), encoding="utf-8"
            )
            prompt.write_text("Return the ordering only.", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "qwen-run",
                        "--command",
                        str(fake),
                        "--task-file",
                        str(task),
                        "--prompt-file",
                        str(prompt),
                        "--receipt-file",
                        str(receipt),
                    ]
                )
            self.assertEqual(code, 4)
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            validate_contract(payload, "host-execution-receipt.schema.json")
            self.assertEqual(payload["status"], "rejected")
            self.assertIn("host_contract_missing", payload["reason_codes"])

    def test_unexpected_task_contract_field_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = self.write_fake_qwen(root, qwen_payload())
            task_payload = self.task()
            task_payload["adapter_options"]["host_contract"]["advisory_override"] = True
            task = root / "task.json"
            prompt = root / "prompt.txt"
            receipt = root / "receipt.json"
            task.write_text(json.dumps(task_payload), encoding="utf-8")
            prompt.write_text("Return the ordering only.", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "qwen-run",
                        "--command",
                        str(fake),
                        "--task-file",
                        str(task),
                        "--prompt-file",
                        str(prompt),
                        "--receipt-file",
                        str(receipt),
                    ]
                )
            self.assertEqual(code, 4)
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertIn(
                "host_contract_unexpected_fields", payload["reason_codes"]
            )


class KimiHostAdapterUnitTests(unittest.TestCase):
    def test_kimi_arguments_deny_tools_and_stream_json(self):
        arguments = kimi_arguments("answer", Path("work"))
        self.assertEqual(
            arguments[arguments.index("--max-steps-per-turn") + 1], "1"
        )
        self.assertEqual(arguments[arguments.index("--output-format") + 1], "stream-json")
        self.assertEqual(arguments[arguments.index("-p") + 1], "answer")

    def test_parse_stream_extracts_text_and_session(self):
        stdout = "\n".join(
            [
                json.dumps({"role": "assistant", "content": [{"type": "text", "text": "EAD"}]}),
                json.dumps({"role": "assistant", "content": [{"type": "text", "text": "BCF"}]}),
                "To resume this session: kimi -r 12345678-1234-1234-1234-123456789abc",
            ]
        )
        answer, session, tool, nontext = parse_kimi_stream(stdout)
        self.assertEqual(answer, "EADBCF")
        self.assertEqual(session, "12345678-1234-1234-1234-123456789abc")
        self.assertFalse(tool)
        self.assertEqual(nontext, [])

    def test_parse_stream_flags_tool_content_part(self):
        stdout = json.dumps(
            {"role": "assistant", "content": [{"type": "tool_use", "name": "python"}]}
        )
        _, _, tool, nontext = parse_kimi_stream(stdout)
        self.assertTrue(tool)
        self.assertIn("tool_use", nontext)

    def test_parse_stream_flags_tool_role_event(self):
        stdout = json.dumps({"role": "tool", "content": [{"type": "text", "text": "x"}]})
        _, _, tool, _ = parse_kimi_stream(stdout)
        self.assertTrue(tool)

    def test_normalize_usage_folds_input_components(self):
        usage = normalize_kimi_usage(
            {"input_other": 100, "input_cache_read": 200, "output": 30}
        )
        self.assertEqual(usage, {"input_tokens": 300, "output_tokens": 30, "total_tokens": 330})

    def test_normalize_usage_rejects_unclassified_key(self):
        with self.assertRaises(AdapterRejected) as caught:
            normalize_kimi_usage({"input_other": 1, "mystery": 2})
        self.assertIn("unclassified_token_component", caught.exception.reason_codes)

    def test_normalize_usage_rejects_absent_or_noninteger(self):
        with self.assertRaises(AdapterRejected):
            normalize_kimi_usage({})
        with self.assertRaises(AdapterRejected) as caught:
            normalize_kimi_usage({"input_other": "10"})
        self.assertIn("exact_token_telemetry_missing", caught.exception.reason_codes)

    def test_read_usage_takes_latest_nonempty_wire_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "session.zip"
            events = [
                {"message": {"type": "StatusUpdate", "payload": {"token_usage": None}}},
                {"message": {"type": "StatusUpdate", "payload": {"token_usage": {"input_other": 5, "output": 2}}}},
            ]
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr(
                    "wire.jsonl", "".join(json.dumps(e) + "\n" for e in events)
                )
                bundle.writestr("logs/kimi.log", "ignored")
            self.assertEqual(read_kimi_usage(archive), {"input_other": 5, "output": 2})


class KimiHostAdapterCliTests(unittest.TestCase):
    ASSISTANT_TEXT = [{"role": "assistant", "content": [{"type": "text", "text": "EADBCF"}]}]
    TOOL_STREAM = [{"role": "assistant", "content": [{"type": "tool_use", "name": "python"}]}]
    USAGE = {"input_other": 100, "input_cache_read": 200, "output": 30}

    def write_fake_kimi(self, root: Path, stream_events, usage) -> Path:
        path = root / "fake_kimi.py"
        script = f"""
import json, sys, zipfile
argv = sys.argv[1:]
if "--version" in argv:
    print("kimi 1.2.3"); sys.exit(0)
if "--help" in argv:
    print("--print --output-format --max-steps-per-turn --work-dir --model -p"); sys.exit(0)
if argv and argv[0] == "export":
    out = argv[argv.index("--output") + 1]
    with zipfile.ZipFile(out, "w") as z:
        event = {{"message": {{"type": "StatusUpdate", "payload": {{"token_usage": {json.dumps(usage)}}}}}}}
        z.writestr("wire.jsonl", json.dumps(event) + "\\n")
    sys.exit(0)
for event in {json.dumps(stream_events)}:
    print(json.dumps(event))
print("To resume this session: kimi -r 12345678-1234-1234-1234-123456789abc")
"""
        path.write_text(script, encoding="utf-8")
        return path

    def task(self):
        return {
            "task_id": "task-kimi-gen3",
            "adapter_options": {
                "host_contract": {
                    "adapter_id": "kimi_cli",
                    "tool_policy": "deny_all",
                    "token_telemetry": "exact_provider_report",
                    "max_provider_calls": 1,
                }
            },
        }

    def run_kimi_cli(self, root: Path, fake: Path) -> dict:
        task = root / "task.json"
        prompt = root / "prompt.txt"
        receipt = root / "receipt.json"
        task.write_text(json.dumps(self.task()), encoding="utf-8")
        prompt.write_text("Return the ordering only.", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(
                [
                    "kimi-run",
                    "--command",
                    str(fake),
                    "--task-file",
                    str(task),
                    "--prompt-file",
                    str(prompt),
                    "--receipt-file",
                    str(receipt),
                ]
            )
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        validate_contract(payload, "host-execution-receipt.schema.json")
        return code, payload

    def test_probe_and_run_accept_native_answer_with_exact_telemetry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = self.write_fake_kimi(root, self.ASSISTANT_TEXT, self.USAGE)
            capability = root / "capability.json"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(
                    ["kimi-probe", "--command", str(fake), "--live", "--output", str(capability)]
                )
            self.assertEqual(code, 0, output.getvalue())
            payload = json.loads(capability.read_text(encoding="utf-8"))
            validate_contract(payload, "host-capability.schema.json")
            self.assertTrue(payload["eligible"])
            self.assertEqual(payload["adapter_id"], "kimi_cli")

            code, receipt = self.run_kimi_cli(root, fake)
            self.assertEqual(code, 0)
            self.assertEqual(receipt["status"], "accepted")
            self.assertEqual(receipt["adapter_id"], "kimi_cli")
            self.assertEqual(receipt["usage"]["total_tokens"], 330)
            self.assertEqual(receipt["tool_calls"], 0)

    def test_run_rejects_tool_use(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = self.write_fake_kimi(root, self.TOOL_STREAM, self.USAGE)
            code, receipt = self.run_kimi_cli(root, fake)
            self.assertEqual(code, 4)
            self.assertEqual(receipt["status"], "rejected")
            self.assertIn("tool_call_observed", receipt["reason_codes"])


class HostAdapterBundleSurfaceTests(unittest.TestCase):
    def test_wrapper_is_shipped_from_runtime_master(self):
        wrapper = (
            REPO / ".agents" / "skills" / "pao-lwar" / "scripts" / "host_adapter.py"
        )
        self.assertTrue(wrapper.is_file())


if __name__ == "__main__":
    unittest.main()
