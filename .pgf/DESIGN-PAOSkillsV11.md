# DESIGN — PAOSkillsV11 (skills-first Protocol-Safe Revision)

> Date: 2026-07-17 | Base: working tree on `666d7f9` | Source of truth: `PAO_skills/` (plugin FROZEN)
> Input: `_workspace/PAO_Skills_upgrade_plan.md` (integrated two-agent review, 27 findings code-verified)
> Scope: Phase 0 + 1 + 2 + 4 of the upgrade plan (= skills v1.1). Phase 3 (authority bounds,
> artifact provenance, ValidationDecision) stays deferred to v1.2. OperationSoak and the
> plugin backport stay blocked gates outside this cycle.

## Decisions (resolved before implementation)

| # | Decision | Rationale |
|---|---|---|
| D1 | **LWAR expands to "Long-running Worker Agent Runtime"** | No expansion exists anywhere in the repo (verified). Matches README's "long-running AI runtimes" and the L-W-A-R letters. Flag to user in the final report for veto. |
| D2 | **Fencing key = `attempt` echo, `claim_token` = provenance only** | The lease file is deleted on submit, so collect cannot verify a token against it. `attempt` lives in the ledger (bumped by `recover`) and in the claimed task file (rewritten on requeue), so result-vs-ledger comparison is a true fencing check with zero new state. Full `leader_epoch`/`lease_id` machinery rejected in the plan. Legacy results without `attempt` skip the check (optional-first rollout). |
| D3 | **Single-writer OA identity = `PAO_OA_ID` env var** (fallback `oa-default`) | Each OA CLI call is a separate process, so PID cannot identify an OA session. The OA skill instructs the session to set one id; unmarked sessions share the default holder (backward compatible, protection activates when ids are set). Writer lease at `var/oa/writer_lease.json`, TTL 900s, refreshed by every mutating command; read-only commands never touch it. |
| D4 | **`interrupted` is recorded in the ledger by `recover`, not as a synthetic result file** | Writing fake results into `outgoing/` would collide with quarantine and duplicate-detection logic. The ledger entry gains an `interruption` record (`recorded_by: oa_reconciler`); result-status enum still gains `interrupted|timed_out|protocol_error` for LWAR-submitted terminals. |
| D5 | **Runtime master = `pao-lwar`; `pao-oa` is a mirror** | Two bundles carry byte-identical `pao_runtime/`, `scripts/`, `schemas/`. All edits land in `pao-lwar` first, then `PAO_skills/sync_bundles.py` mirrors them; a test enforces equality. |
| D6 | **Plugin gate tests: skip, not delete** | `test_standalone_skills.py` plugin→skills byte-gate and build-skills tests get `@unittest.skip("plugin frozen — PAO_skills is canonical; re-enable at backport")`. `test_plugin_packaging.py` switches from importing `pao_runtime.__version__` to reading the plugin's own `__init__.py`, so it keeps verifying the frozen artifact's internal consistency. |
| D7 | **Skills runtime version bumps 0.4.0 → 0.5.0** | Marks divergence from the frozen 0.4.0 plugin; the register version-handshake uses it. |
| D8 | **`--max-consecutive-errors` is an agent-side rule, not a watcher flag** | Watcher errors exit the slice immediately (exit 30); consecutive-error counting spans slices, which only the agent sees. Documented in adp-loop.md; only `--state-wait-backoff-max` is runtime code. |

## Gantree

```
PAOSkillsV11 // skills-first v1.1 — protocol-safe revision (in-progress) @v:1.1
    Phase0PremiseShift // premise flip enablement (in-progress)
        RepoDocsUpdate // AGENTS.md, CLAUDE.md point at PAO_skills (done)
        SyncTestGate // skip plugin byte-gate + build tests with reason (designing)
            # tests/test_standalone_skills.py: @unittest.skip on SyncGateTests;
            # keep StandaloneContractTests (skills-only) active
            # criteria: suite green with skips visible
        TestHarnessRetarget // helpers + suites run against skills runtime (designing)
            # pao_helpers: RUNTIME_HOME = REPO/"PAO_skills"/"pao-lwar"; sys.path + PYTHONPATH
            # test_adp_integration.py local PLUGIN → RUNTIME_HOME
            # test_portability.py scripts/PYTHONPATH → RUNTIME_HOME
            # test_plugin_packaging.py: read version by regex from PLUGIN copy (D6)
            # criteria: full suite imports/executes skills code only (except frozen-artifact checks)
        SyncBundlesTool // PAO_skills/sync_bundles.py mirror script (designing)
            # copy pao-lwar/{pao_runtime,scripts,schemas} → pao-oa, ignore __pycache__/*.pyc
            # stdlib only; prints copied dirs as JSON
    Phase1DocContract // documentation + contract symmetry (designing) @dep:Phase0PremiseShift
        OASchemasBundle // pao-oa gains schemas/ via mirror; SKILL §0 lists it (designing)
        SkillHeaderSync // v1.1 headers, argument-hints, definitions (designing)
            # OA hint: info | doctor | status | reconcile | send | collect | validate |
            #   workflow-status | recover | dead | control | prune
            # LWAR hint: info | doctor | register [number] | response | adp | status |
            #   on | drain | off | unregister
            # both SKILLs: Definitions block (PAO, OA, LWAR full name D1, ADP,
            #   TaskContract, ResultContract), single-host-bus note, python-launcher note
        OASkillRules // single-writer rule + read policy + doctor bootstrap (designing)
            # §: set PAO_OA_ID once per session; mutating commands hold writer lease;
            #   second OA becomes read-only observer
            # read policy: full read before first use per session; re-read on change
        LwarSkillRules // complete semantics + exhaustion handoff + symmetry (designing)
            # Rule 5 → "submit exactly one terminal result ... whenever the agent remains
            #   capable; crash is recovered by lease expiry + recover, never inferred success"
            # new rule: context-exhaustion handoff (drain → submit → off | re-register gen+1)
            # new rule: never expose provider names in artifacts/paths (symmetry)
            # heartbeat note: watcher emits automatically — no agent action
            # read policy same as OA
        ReferenceUpdates // 8 reference docs (designing)
            # adp-loop.md: unknown event/exit-code fail-closed row; state-wait backoff flag;
            #   agent-side consecutive-error cap; heartbeat automation note
            # execute-complete.md: status enum + attempt/claim_token echo + terminal semantics
            # lifecycle.md: exhaustion handoff procedure detail
            # register.md: runtime_version stamp note
            # reconcile.md: state transition table (requester/approver/preconditions);
            #   version-mismatch rejection
            # collect-validate.md: stale_attempt quarantine; succeeded-only acceptance
            # recover-maintain.md: interruption record semantics
            # publish.md: writer-lease mention
    Phase2Runtime // runtime hardening in pao-lwar master (designing) @dep:Phase1DocContract
        ResultStatusExtend // enum + interrupted recording (designing)
            # lwar_cli.complete: allow {succeeded,failed,blocked,cancelled,
            #   interrupted,timed_out,protocol_error}
            # oa_cli.recover: ledger entry gains "interruption" record (D4)
            # schemas/result.schema.json enum extended; + attempt, claim_token optional
        MinimalFencing // claim_token + attempt echo (designing) (D2)
            # transport.claim_task: generate claim_token; rewrite claimed file with it;
            #   store in lease
            # lwar_cli.complete: echo task attempt + claim_token into normalized result
            # oa_cli.collect: ledger.attempt vs result.attempt mismatch → quarantine
            #   "stale_attempt_result"
            # schemas: lease.schema.json + task claim_token note
        SingleWriterOA // writer lease enforcement (designing) (D3)
            # oa_cli: ensure_oa_writer(root) called by reconcile/send/control/collect/
            #   recover/dead --requeue/prune; FileLock + var/oa/writer_lease.json
        WatcherBackoff // --state-wait-backoff-max (designing) (D8)
            # adp_watch: when slot.state != on and flag set, poll sleep doubles up to cap;
            #   reset on state==on; default None = unchanged behavior
        RegisterVersionStamp // runtime_version handshake (designing)
            # lwar_cli.register request += runtime_version
            # registry.process_registration: present-and-mismatched → reject
            #   reason "runtime_version_mismatch"; absent → accept (legacy)
            # registration-request.schema.json += optional runtime_version
        DoctorCommand // pao doctor [--role oa|lwar] (designing)
            # checks: python >= 3.9; bundle files (pao_runtime modules, scripts, schemas,
            #   role references); root resolution + root not inside skill dir; bus writable;
            #   atomic os.replace works; registry parses; leftover .pao-*.tmp files
            # exit 0 healthy / 1 failed; JSON report per check
        VersionBump // __version__ = 0.5.0 in skills master (designing) (D7)
        MirrorBundles // run sync_bundles.py after all runtime edits (designing) @dep:ResultStatusExtend,MinimalFencing,SingleWriterOA,WatcherBackoff,RegisterVersionStamp,DoctorCommand,VersionBump
    Phase4Verify // tests + cross-verification (designing) @dep:Phase2Runtime
        SkillsInternalSyncTests // pao-oa ↔ pao-lwar byte equality (designing)
            # new class in test_standalone_skills.py: pao_runtime/scripts/schemas equal
        V11FeatureTests // new tests/test_skills_v11.py (designing)
            # extended status accepted end-to-end (timed_out)
            # stale-attempt result quarantined after recover bump (scenario 3/10 analogue)
            # interruption record present after recover (scenario 18 analogue)
            # writer lease: PAO_OA_ID=a holds, =b rejected, =a proceeds (scenario 7)
            # register version mismatch rejected fail-closed (scenario 12)
            # doctor healthy on real bundle / fails when root inside skill dir (scenario 14 analogue)
            # backoff flag: default behavior unchanged (state_wait still exit 10)
        FullSuite // python -m unittest discover -s tests (designing)
            # criteria: all green (skips only the two frozen-gate tests)
        CrossVerify // 3-perspective PGF verify (designing) @dep:FullSuite
            # acceptance: every node's criteria re-checked
            # quality: diff review for reuse/idiom (self, no /simplify budget)
            # architecture: DESIGN Gantree ↔ implementation match
```

## PPR — load-bearing nodes

```python
def ensure_oa_writer(root: Path, ttl_s: int = 900) -> dict:
    """Single-writer guard for mutating OA commands (D3)."""
    oa_id = os.environ.get("PAO_OA_ID", "").strip() or "oa-default"
    path = root / "var" / "oa" / "writer_lease.json"
    with FileLock(path.parent / ".writer.lock"):
        if path.is_file():
            lease = load_json(path)
            if lease["oa_id"] != oa_id and parse_utc(lease["expires_at"]) > now():
                raise SystemExit(
                    f"another OA holds the writer lease: {lease['oa_id']} "
                    f"until {lease['expires_at']} — read-only observer mode"
                )
        atomic_write_json(path, {"schema_version": "pao.oa-writer-lease.v1",
                                 "oa_id": oa_id, "refreshed_at": utc_now(),
                                 "expires_at": now() + ttl_s})
    return {"oa_id": oa_id}

def collect_attempt_fence(entry: dict | None, result: dict) -> str | None:
    """Return quarantine reason when the result belongs to a superseded attempt (D2)."""
    if entry is None:
        return None
    ledger_attempt, result_attempt = entry.get("attempt"), result.get("attempt")
    if ledger_attempt is None or result_attempt is None:
        return None          # optional-first rollout: legacy results skip the fence
    return "stale_attempt_result" if int(result_attempt) != int(ledger_attempt) else None

def doctor(role: str | None, root: Path) -> int:
    checks = []
    checks += [python_version(), bundle_files(role), root_resolution(root)]
    checks += [root_outside_skill_dir(root), bus_writable_atomic(root)]
    checks += [registry_parses(root), no_leftover_tmp(root)]
    emit({"event": "doctor_report", "role": role, "checks": checks,
          "healthy": all(c["ok"] for c in checks)})
    return 0 if all(c["ok"] for c in checks) else 1
    # acceptance_criteria:
    #   - healthy on a fresh temp bus with the real bundle
    #   - fails (exit 1) when the bus root resolves inside the skill directory
```

## Constraints carried from the contract tests

- Authored docs must not contain `CLAUDE_PLUGIN_ROOT`, `PAO_HOME`, `$PAO_SKILL`, or a
  line-initial `python -m pao_runtime` (enforced by `StandaloneContractTests`).
- All bus writes stay `tmp → fsync → os.replace`; CLI-only mutation rule unchanged.
- No new third-party dependency; stdlib only.

## Design Review — Round 1 (two red-team agents, applied)

17 findings (protocol 7, feasibility 10). Dispositions — all Critical/P0 resolved, no open blockers:

| ID | Finding | Disposition |
|---|---|---|
| PR-1 (P1) | `requeue_claimed` recreates an already-archived claimed file (submit vs recover TOCTOU) → real result quarantined AND duplicate execution | **Fix in MinimalFencing**: `requeue_claimed` claims via `os.replace(claimed→incoming)` FIRST (FileNotFoundError → return None, no ledger bump), then rewrites incoming with the bumped attempt. `submit_result` catches FileNotFoundError on archive and fails with a clear "claim_superseded" error. |
| PR-2 (P1) | `attempt` rewinds on dead-requeue (reset to 1) → superseded result can later match the fence | **Fix**: dead-requeue continues the counter (`attempt+1`), never resets. recover-maintain.md updated; monotonic attempt is the fence key. |
| PR-3 (P2) | absent `runtime_version` + absent `attempt` = silent fence/handshake bypass | Accepted for 0.5.x freeze window (single host); documented in reconcile.md as legacy-accept with planned rejection. |
| PR-4 (P2) | writer lease must not gate read-only paths (`dead` listing) | `ensure_oa_writer` called only inside mutating branches: reconcile, send, control, collect, recover, `dead --requeue`, prune. |
| PR-5 (P2) | `oa-default` fallback = invisible zero exclusion | Lease records `exclusive: bool`; SKILL mandates setting `PAO_OA_ID` per session; behavior documented. |
| PR-6 (P2) | TTL-only exclusion for long commands; PPR datetime bug | TTL documented best-effort; expires_at/refreshed_at stored as ISO-Z strings, compared via `parse_utc`. |
| PR-7 (P2) | doctor tmp-scan false-fails on in-flight writes | Only `.pao-*.tmp` older than 60s counts as leftover. |
| FE-1 (P0) | `PackagingTests.test_console_entry_points_are_importable` asserts frozen pyproject version == imported `__version__` | Test reads the PLUGIN bundle's own `__init__.py` version by regex for the equality assert. |
| FE-2 (P0) | `InstallerTests` default-source detection impossible from the skills bundle | Both InstallerTests run with explicit `env={"PYTHONPATH": str(PLUGIN)}` — they verify the frozen plugin artifact. |
| FE-3 (P0) | `PLUGIN` symbol must survive in pao_helpers (3 modules import it) | Keep `PLUGIN`; add `RUNTIME_HOME = REPO/"PAO_skills"/"pao-lwar"`; only RUNTIME_HOME goes on sys.path/PYTHONPATH default. |
| FE-4 (P1) | Five hardcoded PLUGIN literals in test_portability with divergent correct targets | Enumerated: `:18` env → RUNTIME_HOME; `:49` wrapper script → RUNTIME_HOME; `:106/:123` install source → stays PLUGIN (+ PLUGIN env per FE-2); `:148` pyproject → stays PLUGIN with regex version per FE-1. |
| FE-5 (P1) | version-handshake baseline unpinned | Baseline = registry-side `pao_runtime.__version__` (same bundle post-mirror); feature test: absent field → accepted (legacy). |
| FE-6 (P1) | pao_cli path math (`skills/lwar-runtime`) is wrong inside the skills bundle | doctor derives bundle root as `Path(__file__).resolve().parents[1]`, schemas at `<bundle>/schemas`. build-skills untouched (skipped tests). |
| FE-7 (P2) | ensure_oa_writer PPR not runnable (`now()`, datetime+int) | Same fix as PR-6. |
| FE-8 (P2) | new docs may trip StandaloneContractTests token bans | All doc examples use `python "<PAO_SKILL>/scripts/pao.py" doctor` form; forbidden tokens (`PAO_HOME`, `CLAUDE_PLUGIN_ROOT`, `$PAO_SKILL`, line-initial `python -m pao_runtime`) linted before commit. |
| FE-9 (P2) | internal-sync test scope + stale pao-oa SKILL §0 sentence | Equality test hardcodes the three dirs; OASchemasBundle edits the §0 bundle sentence explicitly. |
| FE-10 (P2) | doctor python-version check advisory-only | Kept as advisory (still catches 3.8 launches that get far enough to run it). |

Cleared by review (no change): initial-claim claim_token rewrite atomicity; extended status enum vs existing closed-set branches; runtime_version reject leaves no partial state (guard before mutation block); writer lease vs existing tests (shared `oa-default`); result/lease new keys vs existing assertions; `sync_bundles.py` placement at PAO_skills root.

## Risks

| Risk | Mitigation |
|---|---|
| Writer lease breaks existing tests calling OA without PAO_OA_ID | Shared `oa-default` fallback keeps them green; enforcement activates only with distinct ids |
| Claimed-file rewrite (claim_token) races with recover's requeue | Rewrite happens between claim_file (atomic move) and return — the file is exclusively ours until the lease exists; recover only acts on expired leases |
| Result normalized fields drift from schema doc | Schema files updated in the same node; feature test asserts submitted result carries attempt/claim_token |
| Mirror forgotten after a runtime edit | SkillsInternalSyncTests fails the suite on any drift |
```
