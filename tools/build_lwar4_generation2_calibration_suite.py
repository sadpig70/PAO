#!/usr/bin/env python3
"""Build the sealed provider-neutral LWAR4 generation-2 calibration suite."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

if __package__:
    from tools.build_lwar4_reset_requalification_suite import (
        build_suite,
        canonical_sha256,
        prior_prompt_hashes,
        prompt_sha256,
    )
else:
    from build_lwar4_reset_requalification_suite import (
        build_suite,
        canonical_sha256,
        prior_prompt_hashes,
        prompt_sha256,
    )


REPO = Path(__file__).resolve().parents[1]
TARGET = REPO / "benchmarks" / "lwar4-generation2-calibration-suite-v1.json"
PREDECESSOR = REPO / "benchmarks" / "lwar4-reset-requalification-evidence-v2.json"


def build_generation2_suite() -> dict[str, Any]:
    suite = build_suite(campaign_version=3, seed_offset=2000)
    predecessor = json.loads(PREDECESSOR.read_text(encoding="utf-8"))
    prior_hashes, prior_sources = prior_prompt_hashes(excluded_path=TARGET)
    campaign_hashes = {
        prompt_sha256(task["prompt"]) for task in suite["tasks"].values()
    }
    overlap = campaign_hashes & prior_hashes
    if overlap:
        raise RuntimeError(f"generation-2 calibration prompt overlap: {sorted(overlap)}")
    suite["suite_id"] = "lwar4-generation2-calibration-suite-v1"
    suite["claim_scope"] = "preregistered_new_provider_generation_calibration"
    suite["sealed_at"] = "2026-07-26T10:50:00Z"
    suite["max_campaign_executions"] = 1
    suite["provider_receives_expected_answer"] = False
    suite["prompt_overlap_with_prior"] = 0
    suite["prior_suite_sha256"] = prior_sources
    suite["target"] = {
        "lwar_id": "LWAR4",
        "required_generation": 2,
        "replacement_profile_rule": "must_differ_from_retired_generation_1",
        "identity_and_adapter_binding": "pending_before_provider_execution",
    }
    suite["predecessor_evidence"] = {
        "path": PREDECESSOR.name,
        "sha256": canonical_sha256(predecessor),
        "verdict": predecessor["verdict"],
        "reuse_allowed": False,
    }
    return suite


def main() -> int:
    suite = build_generation2_suite()
    TARGET.write_text(
        json.dumps(suite, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "event": "generation2_calibration_suite_built",
                "path": str(TARGET.relative_to(REPO)).replace("\\", "/"),
                "sha256": canonical_sha256(suite),
                "tasks": len(suite["tasks"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
