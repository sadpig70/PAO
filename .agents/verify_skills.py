#!/usr/bin/env python3
"""Verification harness for the redesigned pg/pgf/pgxf skills (stdlib only, deterministic).

Tests (per DESIGN-skill-redesign.md acceptance_criteria):
  T1 self-consistency : every Gantree node line in each SKILL.md obeys that skill's own rules
                        (4-space indent, valid status code, CamelCase node, AI_ snake_case).
  T2 mode coverage    : pgf v2.6 re-tiering dropped NO original mode (names preserved).
  T3 regression       : a real existing PG doc (.pgf/DESIGN-SISAIHardening.md) stays valid under pg v1.4.

Run:  python _workspace/skills/verify_skills.py
Exit 0 iff all pass.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

# PG v1.4 (6) + PGF delegation (3)
VALID_STATUS = {"done", "in-progress", "designing", "blocked", "decomposed", "needs-verify",
                "delegated", "awaiting-return", "returned"}
NODE_RE = re.compile(r"^(?P<indent> *)(?P<name>[A-Za-z_][A-Za-z0-9_]*) // .*?\((?P<status>[a-z-]+)\)")
CAMEL_RE = re.compile(r"^[A-Z][A-Za-z0-9]*$|^[A-Z][A-Za-z0-9_]*$")  # CamelCase (allow AI_/under in PPR names)


def _code_blocks(md_text):
    """Yield the text of each fenced code block."""
    out, inside, buf = [], False, []
    for line in md_text.splitlines():
        if line.strip().startswith("```"):
            if inside:
                out.append("\n".join(buf)); buf = []
            inside = not inside
            continue
        if inside:
            buf.append(line)
    return out


def check_self_consistency(skill_path):
    problems = []
    text = open(skill_path, encoding="utf-8").read()
    for block in _code_blocks(text):
        for ln in block.splitlines():
            m = NODE_RE.match(ln)
            if not m:
                continue
            indent, name, status = m.group("indent"), m.group("name"), m.group("status")
            if name == "NodeName" or status == "status":   # syntax-template placeholder, not a real node
                continue
            if len(indent) % 4 != 0:
                problems.append(f"{os.path.basename(os.path.dirname(skill_path))}: indent not 4-multiple: {ln.strip()!r}")
            if status not in VALID_STATUS:
                problems.append(f"{os.path.basename(os.path.dirname(skill_path))}: bad status ({status}): {ln.strip()!r}")
            if not CAMEL_RE.match(name):
                problems.append(f"{os.path.basename(os.path.dirname(skill_path))}: node not CamelCase: {name}")
            depth = len(indent) // 4
            if depth > 5:
                problems.append(f"{os.path.basename(os.path.dirname(skill_path))}: depth>5: {ln.strip()!r}")
    return problems


def check_mode_coverage(pgf_path):
    """Every original mode keyword must still appear in the re-tiered v2.6 doc."""
    original = ["design", "plan", "execute", "verify", "full-cycle", "loop",
                "discover", "create", "micro", "review", "evolve", "delegate", "design --analyze"]
    text = open(pgf_path, encoding="utf-8").read()
    return [f"pgf: mode dropped in re-tiering: {m}" for m in original if m not in text]


def check_regression(pg_doc):
    """A real PG document must use only status codes pg v1.4 still defines."""
    if not os.path.exists(pg_doc):
        return [f"regression: {pg_doc} missing"]
    problems = []
    for ln in open(pg_doc, encoding="utf-8").read().splitlines():
        m = NODE_RE.match(ln)
        if m and m.group("status") not in VALID_STATUS:
            problems.append(f"regression: {os.path.basename(pg_doc)} uses status not in v1.4: {m.group('status')}")
    return problems


def main():
    all_problems = []
    print("== T1 self-consistency ==")
    for skill in ("pg", "pgf", "pgxf"):
        p = os.path.join(HERE, skill, "SKILL.md")
        probs = check_self_consistency(p)
        print(f"  {skill:5} {'OK' if not probs else 'FAIL (' + str(len(probs)) + ')'}")
        all_problems += probs

    print("== T2 mode coverage (pgf) ==")
    probs = check_mode_coverage(os.path.join(HERE, "pgf", "SKILL.md"))
    print(f"  pgf   {'OK' if not probs else 'FAIL'}")
    all_problems += probs

    print("== T3 regression (DESIGN-SISAIHardening under pg v1.4) ==")
    probs = check_regression(os.path.join(REPO, ".pgf", "DESIGN-SISAIHardening.md"))
    print(f"  doc   {'OK' if not probs else 'FAIL'}")
    all_problems += probs

    print()
    if all_problems:
        print(f"FAIL -- {len(all_problems)} problem(s):")
        for p in all_problems:
            print(f"  - {p}")
        return 1
    print("PASS -- all skill verification checks green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
