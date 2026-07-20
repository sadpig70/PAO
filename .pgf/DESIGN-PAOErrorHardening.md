# DESIGN — PAO Error/Exception/Edge-Case Hardening

> Date: 2026-07-21 | Base: main `4742719` | Source: `.pgf/REVIEW-PAOErrorHandling.md` (41 findings, 5 patterns)
> Scope: resolve every finding except those consciously judged non-defects. Runtime edited in the
> `pao-lwar` master, mirrored to `pao-oa` via `tools/sync_bundles.py`. Contracts edited in place per skill.

## Non-defects (reviewed, NO change — with reason)
- **L8** (doctor py3.9 floor): the code is genuinely 3.9-safe — every module has `from __future__ import
  annotations`, and there is no `match`, runtime `|` union, or 3.9+-only method. `>= (3,9)` is correct;
  raising it would falsely reject a working interpreter. No pyproject `requires-python` exists to align to.

## Solution Gantree

```text
PAOErrorHardening
    S1_Foundation // common.py — primitives every module inherits
        safe_load_json(path)->dict|None // lenient read: None on missing/empty/corrupt/non-object [P1 core]
        quarantine_corrupt(path,reason) // best-effort move to <parent>/.corrupt/ + .error.json [P1]
        _replace_retry(src,dst) // os.replace with bounded PermissionError backoff (Windows) [T1/P3]
        atomic_write_json // route its os.replace through _replace_retry [T1]
        claim_file // route through _replace_retry (still returns False on FileNotFound) [T1]
        authority_denied_reason // resolve-failure → DENY (fail closed), not allow [T4/P5]
        FileLock // token+pid content; steal only if pid dead; __exit__ removes only own lock [T5]
    S2_Transport // transport.py — sweeps skip-and-quarantine; ordering; recovery
        expired_leases // safe_load + quarantine per lease; skip bad [T3/P1]
        find_claimed_task/claimed_task_for_lease/list_dead/read_heartbeat // safe_load, skip bad [T7,O10/P1]
        _auto_cancel // tombstone via safe_load ({} if unreadable) — existence is the signal [L1 root]
        submit_result // remove lease BEFORE archive; archive failure idempotent, never resurrects [T6]
        dead_letter // move-first (guard source exists), don't rewrite-then-move [T9]
        prune // stat() per-file try/except FileNotFoundError [T8]
        orphaned_claims(lwar_id,now,grace) // NEW: claimed/ files with no lease older than grace [T2]
    S3_OA // oa_cli.py + ledger.py + routing.py + registry.py
        ensure_oa_writer // safe_load lease; unreadable/keyless → treat absent + overwrite [O2/P1]
        ensure_oa_writer // warn when oa_id==oa-default (no exclusion) [O9]
        collect // safe_load per result; skip already-collected same file (no re-record); quarantine bad [O4,O5/P1]
        send // reject re-send of a terminal-state ledger entry [O6]; ledger.record_published BEFORE publish [O7]; int coercion → SystemExit [O11]
        recover // call orphaned_claims sweep [T2]; reconcile dead/ ledger entries to `dead` [O8]
        ledger.get/workflow_entries/record // safe_load, skip corrupt [P1]
        registry.reconcile // try/except per request → quarantine + continue [O1/P1]
        registry.process_registration // idempotent replay: instance_id already in a slot → reconstruct response, no 2nd alloc [O3]
        registry.process_lifecycle // idempotent replay: already-in-state or deregistered-with-tombstone → reconstruct accepted [O3]
        routing.auto_route // skip slot keys failing validate_lwar_id [O12]
    S4_LWAR // lwar_cli.py + adp_watch.py + pao_cli.py
        adp_watch.watch // wrap per-slice body; transient error → bounded retry (3) then adp_error; clamp sleep to deadline; reject interval>timeout [L1,L5]
        lwar complete/state/status/response // load guards → classified SystemExit, not traceback [L3,L4,L6]
        lwar status // read-only by default; --sync persists identity refresh [L9]
        lwar state // verify slot + generation + transition legality locally before emitting [L7]
        pao doctor // no_leftover_tmp: stat() per-file try/except [L2]
    S5_Contracts // both skills' authored docs
        adp-loop.md // shutdown-with-held-claim → submit terminal result first [C1]; unknown-event = slice-only, not ADP stop, name channel [C4]; 3-terminator consistency [C9]
        execute-complete.md // situation→status table [C2]; "succeeded only when every criterion verified" [C3]; complete-failure handling [C6]
        lifecycle.md // bootstrap exit-code map (2=wait,3=register,4=stale) [C7]; anchor exhaustion to objective signal [C10]
        register.md // response table "any other exit → fail closed" row [C11]
        pao-lwar SKILL // §1.6 terminator list ↔ §1.3 [C9]; §0.5 exit-code branch [C7]
        pao-oa SKILL // §1 writer-lease-rejection reaction [C5]
        recover-maintain.md // blocked-for-authority = re-plan, not blind requeue [C8]
    S6_SyncAndTest
        tools/sync_bundles.py // mirror master → pao-oa
        tests // add error/edge tests: poison-file skip, replace-retry, orphan-claim recovery, idempotent re-registration, collect-idempotency, terminal-resend reject, fail-closed authority, complete/status guards, doctor TOCTOU
```

## Key decisions
- **D1 skip-and-continue is the P1 contract:** a corrupt file must never abort a sweep. Quarantine-move is
  best-effort on top; if the move fails, still continue. Mirrors the existing `claim_control`/`_reject_task`
  discipline already in transport.
- **D2 T2 via a recovery sweep, not a claim reorder:** reordering claim/lease writes trades one orphan window
  for another; a `claimed/`-with-no-lease sweep in `recover` (grace 120s to not race a live in-flight claim)
  is strictly additive and provably reclaims the orphan.
- **D3 O3 via instance_id idempotency:** the identity model is one instance_id ⇒ one slot, so "instance_id
  already registered" is the safe idempotent replay signal — reconstruct the response, never double-allocate.
- **D4 fail-closed authority (T4):** an unverifiable path (resolve OSError) is denied, never allowed — the
  opposite of today. Defense-in-depth must not weaken to "couldn't check, so fine".
- **D5 status read-only default (L9):** `--sync` restores the identity-refresh write; registry_version is not
  a validated field, so a slightly stale local copy is harmless and the surprising read-side write is gone.
- **D6 backward compatibility:** every change is additive or a strictly-safer failure path; no bus schema
  changes, no CLI removals. Existing 69 tests must stay green; new tests cover the new paths.

## Verify outcome (2026-07-21)
- **Tests:** 85/85 green (69 pre-existing + 16 new in `tests/test_error_hardening.py`; 1 pre-existing test,
  `test_recollect_without_archive_is_idempotent`, updated — it had encoded the O5 bug). Both doctors healthy.
  Real E2E through the skill scripts (register→reconcile→adopt→send→watch→complete→collect→validate→recover→
  doctor) all green.
- **Adversarial verification (2 independent subagents):**
  - Runtime red-team found ONE real regression I introduced — **F1**: the first T6 attempt retired the lease
    *before* writing `outgoing`, which (combined with the new orphaned_claims sweep) opened a duplicate-
    execution window. **Fixed**: publish `outgoing` FIRST so `result_exists()` guards every recovery path,
    then retire the lease (keeps T6 idempotency). Plus **F2** (low): the O5 skip blocked a later `--archive`
    cleanup — fixed. The other 13 change-categories were verified defect-free.
  - Contract review found 4 consistency nits (C9 terminator wording, shutdown status label, an interrupted-row
    scope, pseudocode completeness) — all **fixed**. C1–C10 gaps confirmed closed; no forbidden strings.
- **Non-defect L8** left unchanged as designed (code is 3.9-safe).
```
