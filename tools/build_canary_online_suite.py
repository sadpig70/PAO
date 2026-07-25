#!/usr/bin/env python3
"""Build the deterministic objective suite used for online canary evidence."""

from __future__ import annotations

import itertools
import json
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
OUTPUT = REPO / "benchmarks" / "canary-online-suite-v1.json"

ORDERINGS = (
    "DBAEC",
    "CEBAD",
    "BADEC",
    "EDCBA",
    "ACEDB",
    "BDECA",
    "EABDC",
    "CDAEB",
    "DEBAC",
    "ABECD",
)

REVIEWS = (
    (
        "def append_log(item, log=[]): log.append(item); return len(log)",
        [
            "B1 mutable default shares history",
            "B2 return type changes nondeterministically",
            "B3 append always raises",
        ],
        ["B1"],
    ),
    (
        "def ratio(total, count): return total / count",
        [
            "B1 count zero is not handled",
            "B2 division always truncates",
            "B3 total must be a string",
        ],
        ["B1"],
    ),
    (
        "def lookup(rows, key): return [r for r in rows if r['id'] == key][0]",
        [
            "B1 a missing key raises IndexError",
            "B2 the comparison mutates rows",
            "B3 list comprehension is unordered",
        ],
        ["B1"],
    ),
    (
        "def enabled(value): return bool(value)",
        [
            "B1 the string 'false' is treated as true",
            "B2 bool cannot accept strings",
            "B3 true booleans become false",
        ],
        ["B1"],
    ),
    (
        "def debit(balance, amount): return balance - amount",
        [
            "B1 negative amount increases the balance",
            "B2 subtraction is nondeterministic",
            "B3 integer balance always overflows",
        ],
        ["B1"],
    ),
    (
        "def first_even(values): return next(v for v in values if v % 2 == 0)",
        [
            "B1 no even value raises StopIteration",
            "B2 modulo mutates values",
            "B3 even integers cannot be yielded",
        ],
        ["B1"],
    ),
    (
        "def normalized(name): return name.strip().lower()",
        [
            "B1 None input raises AttributeError",
            "B2 lower changes the source string",
            "B3 strip removes internal spaces",
        ],
        ["B1"],
    ),
    (
        "def page(items, size): return [items[i:i+size] for i in range(0, len(items), size)]",
        [
            "B1 nonpositive size is not rejected",
            "B2 slicing mutates items",
            "B3 range cannot use integers",
        ],
        ["B1"],
    ),
    (
        "def merge(base, update): base.update(update); return base",
        [
            "B1 the caller's base mapping is mutated",
            "B2 update discards all keys",
            "B3 return is always None",
        ],
        ["B1"],
    ),
    (
        "def parse_port(text): return int(text)",
        [
            "B1 range 1..65535 is not validated",
            "B2 int accepts no strings",
            "B3 valid ports always become floats",
        ],
        ["B1"],
    ),
)


def ordering_tasks() -> dict[str, dict]:
    tasks = {}
    for index, answer in enumerate(ORDERINGS, start=1):
        constraints = [
            f"{answer[position]} is immediately before {answer[position + 1]}"
            for position in range(len(answer) - 1)
        ]
        constraints = constraints[2:] + constraints[:2]
        tasks[f"CO{index:02d}"] = {
            "task_class": "constraint_ordering",
            "prompt": (
                "Do not use tools. Return one JSON object and no prose. Arrange A-E "
                "using all constraints: "
                + "; ".join(constraints)
                + '. Return {"answer":"<five letters in order>"}.'
            ),
            "expected": {"answer": answer},
        }
    return tasks


def review_tasks() -> dict[str, dict]:
    tasks = {}
    for index, (snippet, options, defects) in enumerate(REVIEWS, start=1):
        tasks[f"CR{index:02d}"] = {
            "task_class": "code_review",
            "prompt": (
                "Do not use tools. Return one JSON object and no prose. Review: "
                f"{snippet}. Choose every real defect ID from: "
                + "; ".join(options)
                + '. Return {"defects":["sorted IDs"]}.'
            ),
            "expected": {"defects": defects},
        }
    return tasks


def optimization_tasks() -> dict[str, dict]:
    tasks = {}
    for index in range(1, 11):
        items = []
        for offset, name in enumerate("ABCDE"):
            items.append(
                {
                    "name": name,
                    "cost": 2 + ((index + offset * 2) % 6),
                    "risk": 1 + ((index * 2 + offset) % 4),
                    "value": 4 + ((index * 3 + offset * 5) % 9),
                }
            )
        cost_limit = 11 + index % 3
        risk_limit = 6 + index % 2
        feasible = []
        for count in range(1, len(items) + 1):
            for subset in itertools.combinations(items, count):
                cost = sum(item["cost"] for item in subset)
                risk = sum(item["risk"] for item in subset)
                if cost <= cost_limit and risk <= risk_limit:
                    selection = [item["name"] for item in subset]
                    feasible.append(
                        {
                            "selection": selection,
                            "value": sum(item["value"] for item in subset),
                            "cost": cost,
                            "risk": risk,
                        }
                    )
        winner = min(
            feasible,
            key=lambda item: (
                -item["value"],
                item["cost"],
                item["risk"],
                item["selection"],
            ),
        )
        catalog = ", ".join(
            f"{item['name']}(cost{item['cost']},risk{item['risk']},value{item['value']})"
            for item in items
        )
        tasks[f"BO{index:02d}"] = {
            "task_class": "bounded_optimization",
            "prompt": (
                "Do not use tools. Return one JSON object and no prose. Choose a "
                f"subset with total cost <={cost_limit} and risk <={risk_limit} "
                "that maximizes value; tie-break by lower cost, then lower risk, "
                f"then lexicographically sorted selection. Items: {catalog}. "
                'Return {"selection":["sorted letters"],"value":<int>,'
                '"cost":<int>,"risk":<int>}.'
            ),
            "expected": winner,
        }
    return tasks


def build_suite() -> dict:
    tasks = {}
    tasks.update(ordering_tasks())
    tasks.update(review_tasks())
    tasks.update(optimization_tasks())
    return {
        "schema_version": "pao.benchmark-suite.v1",
        "suite_id": "canary-online-suite-v1",
        "claim_scope": "objective_side_effect_free_online_shadow_evidence",
        "tasks": tasks,
    }


def main() -> int:
    OUTPUT.write_text(
        json.dumps(build_suite(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps({"event": "canary_suite_built", "path": str(OUTPUT)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
