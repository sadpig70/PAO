# REVIEW — PAO Error / Exception / Edge-Case Handling

> Date: 2026-07-21 | Mode: PGF `review` (analyze + prioritize, no fixes) | Base: main `4742719`
> Method: 4 parallel review agents (transport/atomicity, OA state, LWAR/ADP loop, skill contracts),
> each returning structured findings; 12 high/medium findings cross-verified against source by the
> orchestrator (12/12 confirmed). Scope: `.agents/skills/pao-lwar/pao_runtime/*.py` (2663 LOC master)
> + both SKILL.md contracts and all 8 references.

## Systemic patterns (the headline — individual findings are instances of these)

### P1 — "Poison file": unguarded `load_json` in sweep loops, no quarantine → one bad file wedges a whole subsystem forever
The atomic write discipline (`temp → fsync → os.replace`) protects the system's OWN writes from tearing,
but there is NO defense against a single malformed JSON at a real path (crash of a non-atomic step, disk
fault, hand-edit, or the O9 two-writer race). A sweep loop calls `load_json` bare, the exception aborts the
loop, and the offending file is never quarantined — so it re-crashes the same command on every future run.
**`transport.claim_task` already does this correctly (`_reject_task` on `invalid_json`); the pattern is just
not applied uniformly.** Instances: T3, T7, O1, O2, O4, O10, L1.

### P2 — Non-atomic multi-step mutations leave an orphan/partial state that no recovery pass reconciles
`atomic_write_json` is per-file atomic; multi-file operations are not. A crash in the window leaves state that
recovery — keyed on ONE artifact — cannot see. Instances: T2 (claim: move→stamp→**lease**; crash before lease
→ task in `claimed/` with no lease → `expired_leases` never sees it → stuck forever), O3 (registry before
response marker → double-registration on retry), O7 (publish before ledger → untracked live task), O8
(dead_letter before ledger transition → ledger stuck non-terminal forever). L1 makes T2's window reachable
from the watcher via a raising `_auto_cancel`.

### P3 — Windows `os.replace` is never retried (the historically-observed WinError 5)
`atomic_write_json` (common.py:77) and every `.replace()` inherit an unretried replace. Any concurrent reader
or Defender scan on the destination → `PermissionError`, aborting the in-flight write. Mitigated
operationally (buses moved off AppData) but unmitigated in code. Instance: T1 (+ T6 archive replace).

### P4 — Contract silence on error branches → LLM improvises → defeats the honest-terminal & daemon guarantees
The contracts prescribe happy path + a few named failures; the rest is silent, and an LLM fills silence by
guessing. Instances: C1 (shutdown mid-task drops the held claim), C2 (no situation→status map for
timed_out/protocol_error/interrupted), C3 (no LWAR-side "never emit succeeded unverified" — the symmetric
rule to OA §4 is absent), C4 (unknown-event "fail closed" ambiguous vs daemon rule), C5–C11.

### P5 — Fail-open where fail-closed is required; false-green health
T4 (`path_within` OSError → treated as "not inside bus" → authority check PASSES the task), L8 (doctor accepts
py3.9 vs 3.13 target → false green), L2 (doctor crashes on a vanishing temp → false red), L9 (`status` mutates
the identity file as a read side-effect).

---

## HIGH findings (verified against source unless noted)

| id | location | defect | verified |
|----|----------|--------|----------|
| T1 | common.py:77 | `os.replace` no retry → PermissionError aborts every write under concurrent read/AV on Windows | ✓ |
| T2 | transport.py:289–319 | claim moves to `claimed/` + stamps token BEFORE lease write; crash/replace-fail in window → orphan invisible to lease-based recovery | ✓ |
| T3 | transport.py:399–401 | `expired_leases` bare `load_json`+`lease["expires_at"]`; one corrupt lease wedges ALL recovery for that LWAR | ✓ |
| O1 | registry.py:224–229 | `reconcile` no try around `process_*`; one bad request file wedges ALL registration+lifecycle forever (no quarantine) | ✓ |
| O2 | oa_cli.py:50–52 | corrupt/keyless `writer_lease.json` crashes `ensure_oa_writer` → wedges the ENTIRE mutation surface; unconditional rewrite at :66 never reached | ✓ |
| L1 | adp_watch.py:45–103 | try/except wraps only identity load; `claim_control`/`claim_task`/`write_heartbeat` bare → exception exits with traceback+code 1, NOT a dispatchable event; can strand a claim (P2) | ✓ |
| C1 | adp-loop.md:16–17 + SKILL §1.5 | shutdown branch says `return`; no instruction to submit a terminal result for a held claim first → silent claim abandonment that §1.5 forbids | ✓ |
| C2 | execute-complete.md:27 | 7 statuses listed, situation→status map for only 3; no rule for when to emit timed_out/protocol_error/interrupted → agent guesses, corrupting the honest-terminal signal | ✓ |
| C3 | execute-complete.md:7–12 | no LWAR-side "emit succeeded only when every criterion is independently verified"; the anti-fake-success guard exists only on the OA side | ✓ |
| C4 | adp-loop.md:61 + SKILL §1.9 | unknown-event "fail closed, report protocol_error" is ambiguous vs the daemon rule (may self-stop) and names no reporting channel when no task is held | ✓ (rated medium by orchestrator — "this slice" wording is partly clear) |

## MEDIUM findings (by pattern; locations exact, not all individually re-verified)

- **P1 poison-file:** T7 (transport.py:342/349/411/449 — find_claimed_task/read_heartbeat/list_dead), O4 (oa_cli.py:281/292 — collect aborts on one bad result), O10 (routing.py:28 + oa_cli.py:448 — one corrupt heartbeat kills all `--auto` routing + status), L2 (pao_cli.py:154–160 — doctor crashes on vanishing temp), L3/L4/L6 (lwar_cli.py:239/242/245/129 — complete/state/status/response traceback on missing/corrupt identity/result/response instead of a clean SystemExit).
- **P2 crash-window:** O3 (registry.py:125–147 — registration commits before response marker → double-registration or wrong retry verdict), O7 (oa_cli.py:204–205 — publish before ledger → untracked task can dead-letter with zero ledger trace), O8 (oa_cli.py:378–401 — dead_letter before transition → ledger stuck non-terminal, no pass reconciles `dead/`).
- **P3 Windows replace:** T6 (transport.py:357–370 — submit_result: non-FileNotFound archive failure after outgoing written → result emitted but claim+lease live → later requeue = duplicate execution).
- **Concurrency / lease:** T5 (common.py:223–238 — FileLock steals by mtime age with no PID-liveness check and `__exit__` deletes whoever's lock is present → broken mutual exclusion), O9 (oa_cli.py:46/52 — two OAs sharing id, or both default `oa-default`, both pass the lease → single-writer void in the DEFAULT config; :62 even sets `exclusive:false`), O5 (oa_cli.py:309–337 — `collect` without `--archive` re-collects same result every run → inflated counts, unbounded ledger history), O6 (oa_cli.py:202 + ledger.py:28 — re-send of a completed pinned task_id clobbers the completed ledger entry + re-executes).
- **P4 contract:** C5 (OA SKILL §1 — writer-lease rejection: no prescribed reaction → improvise/hand-edit), C6 (execute-complete.md:29/44 — only "superseded" handled; any other `complete` failure unspecified), C7 (LWAR SKILL §0.5 — bootstrap treats status exit 2 "registry unavailable" as "not present" → RE-REGISTERS, orphaning a valid identity), C8 (recover-maintain.md — a `blocked`-for-authority result is blind-requeued to dead-letter instead of re-planned), C9 (SKILL §1.3 vs §1.6 — terminator list mismatch: add_error omitted in §1.6), C10 (lifecycle.md:39 — context-exhaustion self-stop hinges on the same subjective "feeling finished" §1.3 bans).
- **P5 / other:** T4 (common.py:104–127 — path_within fail-open on resolve OSError → authority bypass), L5 (adp_watch.py:136 — no `interval <= timeout` guard → one slice sleeps far past its timeout, starving control messages), L7 (lwar_cli.py:127–149 — `state` does no slot/generation/transition-legality check).

## LOW findings
T8/T9 (transport prune stat TOCTOU; dead_letter can resurrect an archived task), O11/O12 (send int-coercion raw traceback; routing lwar_number int-parse on a foreign key), L8 (doctor py3.9 floor vs 3.13), L9 (`status` write side-effect), C11 (register.md `response` table has no "any other exit → fail closed" row).
Plus notes: atomic_write_json doesn't fsync the parent dir (durability, low on NTFS); fd leak if `os.fdopen` raises (common.py:219).

## Non-defects (explicitly cleared by the audit — delegation genuinely covers them)
Torn writes by the single writer (atomic temp+replace); cross-device EXDEV (all replaces stay within one dir);
claim/requeue/submit TOCTOU between LWAR and recover (os.replace + FileNotFoundError backoff); attempt-fence
monotonicity for stale results; artifact size-before-hash DoS guard; routing tie-break determinism + empty
candidate handling; symlink/`..`/case-insensitivity in path_within (correct except the OSError fail-open, T4);
generation-bump detection (watcher + status).

## Calibration / falsification
- **What makes most of P1/P2 dormant:** under strict single-OA + atomic-write operation on a non-Windows,
  never-hand-edited, short-lived bus, spontaneous corrupt files and crash-windows are rare. These are
  robustness gaps at the FAULT boundaries (crash, corrupt file, concurrency, Defender, hand-edit) — exactly
  what this review targeted, and exactly what a multi-hour soak daemon will eventually hit, but NOT everyday
  happy-path bugs.
- **What bites regardless of faults:** the P4 contract gaps (LLM improvises on every ambiguous run, not just
  under fault) and T1 (any concurrent reader on Windows). These have the highest expected impact.
- **Would-be counter-argument:** "single-writer lease + atomic writes already make the bus consistent." Rebuttal:
  the lease is void in the default config (O9), atomicity is per-file not per-operation (P2), and neither
  addresses a malformed file once it exists (P1) — the three compound.

## Recommended triage (if a fix cycle is authorized)
1. **P1 in one shot:** a `load_json_or_quarantine` helper applied to every sweep loop (mirror the existing
   `_reject_task`/`quarantine` pattern) — closes T3,T7,O1,O2,O4,O10,L1's parse paths at once. Highest
   value-to-effort: turns "wedged forever" into "skip + quarantine + continue".
2. **T1:** bounded retry/backoff around `os.replace` on `PermissionError` — one primitive, protects every writer.
3. **L1:** wrap the whole watcher slice body in one try/except emitting a `transient_error` vs `adp_error`
   event with an explicit re-loop-vs-stop action.
4. **P4 contract batch (C1–C4):** shutdown-with-held-claim rule, a situation→status table, an LWAR-side
   anti-fake-success rule, unknown-event clarified as slice-only. Cheap (doc edits), high behavioral impact.
5. **P2 reconciliation:** a sweeper for `claimed/` files with no lease (T2), and a `dead/`→ledger reconcile
   pass (O8); order-of-writes fix for O3/O7.
6. O9 (refuse/warn on `oa-default` writer lease), T5 (PID-liveness on lock steal), remaining mediums.
7. Low/polish batch last.
