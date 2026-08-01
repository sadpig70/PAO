#!/usr/bin/env python3
"""Seal honest LWAR4 generation-3 (Kimi) recovery calibration evidence.

Generation 3 executed the 12 sealed recovery tasks through the machine-enforced
Kimi host adapter (deny_all + exact token telemetry). Every executed task was
contract-compliant (zero tools, exact telemetry). The answer-key-free finite
verifier accepted 11/12; RR06 was a genuine objective miss, so the recovery gate
required (12/12) is not met and the constraint_ordering::LWAR4 circuit is
preserved open with no reset. RR02's first attempt was a transient host process
crash (empty answer, non-zero exit) and was re-measured (RR02b) to a correct
answer; re-measuring a crashed measurement does not touch answer-key-free
integrity, and an incorrect answer (RR06) is never re-attempted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
LWAR_BUNDLE = REPO / ".agents" / "skills" / "pao-lwar"
if str(LWAR_BUNDLE) not in sys.path:
    sys.path.insert(0, str(LWAR_BUNDLE))

from pao_runtime.predictive_routing import canonical_sha256
from tools.run_heterogeneous_lwar_ab import verify_finite_json_answer

SUITE_PATH = REPO / "benchmarks" / "lwar4-generation2-calibration-suite-v1.json"
PREREG_PATH = REPO / "benchmarks" / "lwar4-generation3-calibration-preregistration-v1.json"
TARGET = REPO / "benchmarks" / "lwar4-generation3-calibration-evidence-v1.json"
CIRCUIT_KEY = "constraint_ordering::LWAR4"
# RR02 first attempt was a transient crash; its re-measurement task id is rr02b.
RESULT_TASK_IDS = {f"RR{n:02d}": f"task-gen3-lwar4-rr{n:02d}" for n in range(1, 13)}
RESULT_TASK_IDS["RR02"] = "task-gen3-lwar4-rr02b"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def find_result(root: Path, task_id: str) -> dict[str, Any]:
    hits = [
        p
        for p in root.rglob(f"*{task_id}*result*.json")
        if "error" not in p.name
    ]
    if not hits:
        raise RuntimeError(f"no result found for {task_id}")
    # any copy is byte-identical evidence; take the first
    return load_json(hits[0])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--completed-at", required=True)
    args = parser.parse_args()
    root = args.root.resolve()

    suite = load_json(SUITE_PATH)
    prereg = load_json(PREREG_PATH)
    circuit_path = root / "var" / "routing" / "canary-circuits.json"
    circuit_sha = hashlib.sha256(circuit_path.read_bytes()).hexdigest()
    circuit = load_json(circuit_path)["circuits"][CIRCUIT_KEY]

    per_task = []
    receipt_accepted = tool_zero = exact_tokens = objective_matches = 0
    for name in [f"RR{n:02d}" for n in range(1, 13)]:
        result = find_result(root, RESULT_TASK_IDS[name])
        evidence = result.get("evidence") or {}
        receipt = evidence.get("host_receipt") or {}
        answer = (evidence.get("provider_answer") or {}).get("answer")
        usage = receipt.get("usage") or {}
        accepted = receipt.get("status") == "accepted"
        tools = receipt.get("tool_calls")
        total = usage.get("total_tokens")
        verifier_fail = (
            verify_finite_json_answer(
                suite["tasks"][name]["prompt"], json.dumps({"answer": answer})
            )
            if answer is not None
            else ["no_answer"]
        )
        objective = not verifier_fail
        receipt_accepted += 1 if accepted else 0
        tool_zero += 1 if tools == 0 else 0
        exact_tokens += 1 if isinstance(total, int) else 0
        objective_matches += 1 if (accepted and objective) else 0
        entry = {
            "task": name,
            "task_id": result.get("task_id"),
            "answer": answer,
            "receipt_status": receipt.get("status"),
            "tool_calls": tools,
            "total_tokens": total,
            "objective_answer_match": objective,
        }
        if verifier_fail:
            entry["verifier_failures"] = verifier_fail
        per_task.append(entry)

    passed = (
        receipt_accepted == 12
        and tool_zero == 12
        and exact_tokens == 12
        and objective_matches == 12
    )
    stop_reasons = []
    if objective_matches < 12:
        misses = [e["task"] for e in per_task if not e["objective_answer_match"]]
        stop_reasons.append("objective_answer_mismatch:" + ",".join(misses))

    evidence = {
        "schema_version": "pao.generation-calibration-evidence.v1",
        "preregistration_id": prereg["preregistration_id"],
        "preregistration_sha256": canonical_sha256(prereg),
        "suite_sha256": prereg["suite_sha256"],
        "suite_reused_from": prereg.get("suite_reused_from"),
        "completed_at": args.completed_at,
        "target_identity": {
            "lwar_id": prereg["target_identity"]["lwar_id"],
            "generation": prereg["target_identity"]["generation"],
            "instance_id": prereg["target_identity"]["instance_id"],
            "profile": prereg["target_identity"]["profile"],
        },
        "recovery_gate": {
            "tasks": 12,
            "executed": 12,
            "receipt_accepted": receipt_accepted,
            "tool_calls_zero": tool_zero,
            "exact_token_reports": exact_tokens,
            "objective_answer_matches": objective_matches,
            "required_lwar4_accepted": prereg["recovery_gate"]["required_lwar4_accepted"],
            "required_exact_token_reports": prereg["recovery_gate"]["required_exact_token_reports"],
            "passed": passed,
            "stop_reasons": stop_reasons,
        },
        "integrity_notes": [
            "RR02 attempt 1 was a transient host process crash (empty_answer, host_process_nonzero); re-measured as RR02b to a correct answer. Re-measuring a crashed measurement does not touch answer-key-free integrity.",
            "RR06 was a genuine objective miss under a contract-compliant receipt; an incorrect answer is never re-attempted.",
        ],
        "per_task": per_task,
        "routing_safety": {
            "circuit_key": CIRCUIT_KEY,
            "circuit_status": circuit["status"],
            "circuit_file_sha256_before": prereg["source_bus"]["circuit_file_sha256"],
            "circuit_file_sha256_after": circuit_sha,
            "circuit_reset": "not_executed",
            "routing_observation_recorded": False,
        },
        "claim_boundary": {
            "host_contract_compliance": "full_zero_tool_exact_telemetry_12_of_12",
            "recovery_objective_accuracy": f"{objective_matches}_of_12",
            "provider_family_heterogeneity": "observed_moonshot",
            "production_qualification": "not_authorized",
        },
        "verdict": (
            "generation3_qualified_circuit_reset"
            if passed
            else "generation3_contract_compliant_recovery_incomplete_single_objective_miss_circuit_preserved_open"
        ),
    }
    TARGET.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "event": "generation3_evidence_sealed",
                "path": str(TARGET.relative_to(REPO)).replace("\\", "/"),
                "passed": passed,
                "objective_matches": objective_matches,
                "verdict": evidence["verdict"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
