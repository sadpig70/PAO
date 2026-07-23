# PAO command wrappers

These four thin wrappers (`pao.py`, `oa.py`, `lwar.py`, `adp_watch.py`) are the
entry points for the PAO tools. Each one bootstraps its own import path from the
bundle it lives in (`Path(__file__).resolve().parents[1]`), so no pip install,
no `PYTHONPATH`, and no plugin are required — the wrapper works from any working
directory.

Runtime v0.7.32 validates bundled JSON contracts at all trust boundaries. OA
mutations require `PAO_OA_ID` and refresh a short-TTL presence signal independently
of the writer lease on a fixed-rate 25-second target with a 30-second hard-latest
contract. A separate command mutex serializes complete OA mutations even when
multiple processes reuse the same OA id. POSIX and Windows PID checks prevent
live-lock theft and reclaim a dead holder after the stale threshold. LWARs inspect presence with `oa-status`; clean one-time workers
return their slots through `retire`. `complete` requires the exact claim token
emitted in `task_received`. The default LWAR ADP uses a resident watcher that
crosses idle slice boundaries internally, keeping heartbeat fresh without
depending on agent turn scheduling.
`lwar.py response REQUEST_ID --resident` performs identity adoption and resident
watcher entry in one Python process. Adoption publishes a `starting` heartbeat;
OA distinguishes that bounded startup phase from a watcher that was active and
later became stale. OA can explicitly reclaim an overdue orphaned startup slot
only through identity-fenced `recover --reap-startup`; active mailbox work
blocks the reclaim. Accepted startup-reap audit events use deterministic keys,
so crash replay restores missing audit steps without duplicating committed ones.
Repeated active-log failures also deduplicate the degraded spool under its own
lock. If a process stops after active-log flush but before spool deletion,
recovery filters already-committed keys and promotes each event only once.
Both active-log and degraded-spool appends cross an `fsync` durability barrier
before success or spool deletion. An active `fsync` failure therefore preserves
the deterministic event in the durable degraded spool for later recovery.
Rotated-segment pruning uses the same audit-then-degraded lock order as append.
It retains an old segment when its deterministic key is still represented in
the degraded spool, then allows pruning after a healthy replay clears the spool.
Deterministic key scans require a complete readable snapshot of every active
and rotated segment. Any read or decode failure defers active append and retains
the event in the degraded spool until a later complete scan can converge once.
Strict JSONL scans distinguish valid unkeyed objects from malformed evidence.
Only a non-terminated malformed tail in the mutable active log or degraded
spool is auto-repaired: its raw bytes are durably quarantined under `.corrupt/`
before truncation. Other malformed lines remain fail-closed for operator repair.
`oa.py audit-health` is a read-only diagnostic for segment validity, blocked
replay, pending degraded records, quarantined fragments, and exact
`repair_candidates` containing each segment SHA-256 and malformed line set. It
never acquires writer/audit locks or changes bus files; blocked health exits
with code 2.
`oa.py audit-repair` is the only supported repair for terminated, interior, or
rotated corruption. It requires the diagnosed segment SHA-256 and every
malformed 1-based line number, rejects drift or valid-line deletion, durably
preserves the original under `.corrupt/`, and atomically installs a fully
validated replacement under the OA writer and audit locks.
Before replacement it also writes a durable `.repairs/` receipt binding the
operator intent, original/candidate hashes, line set, and backup. Receipt phases
`prepared -> replaced -> committed` let the exact command resume after a
process stop before replacement, after replacement, or around the deterministic
audit append. A mismatched receipt or target digest remains fail-closed.
`oa.py prune` removes an old repair receipt and its original backup only when
the receipt is committed, its deterministic audit key is still present, the
backup still matches, and the target is provably repaired (or the degraded
spool was consumed). Prepared, replaced, invalid, drifted, missing, or otherwise
ambiguous evidence is retained. The command reports receipt and backup removals
separately.
Before removing either file, pruning writes a strict transaction tombstone
under `.repair-prune/`. It removes the receipt, atomically stages the matching
backup on the same filesystem, advances the tombstone from `authorized` to
`backup_staged`, removes the staged backup, and removes the tombstone last. A
later `prune` resumes an authorized transaction at any process-stop boundary,
independently of its new cutoff. Malformed, conflicting, missing, or drifted
transaction state remains untouched.
`oa.py audit-health` inspects these tombstones without locks or writes. It
reports each transaction as `resumable` only when its strict binding and file
topology match a supported crash boundary; every malformed, missing, drifted,
or conflicting state is `blocked` with stable `reason_codes`. Aggregate
`resumable_retention_count` and `blocked_retention_count` raise overall health
to `attention` without changing keyed-append blocking semantics.
Rotated audit pruning loads every retention tombstone before deleting any
segment. It preserves both tombstone-named rotated repair targets and every
rotated segment carrying a referenced repair audit key. A malformed,
unreadable, or non-file tombstone cancels the entire rotated prune pass.
Protection ends only after the retention transaction removes its tombstone.
Every age-eligible rotated segment must also parse completely as
JSON-object JSONL before deletion. Unreadable, malformed, non-object,
metadata-inaccessible, or unlink-failed segments remain in place.
`oa.py prune` reports `audit_segments_removed`, `audit_segments_protected`, and
`audit_segments_blocked`; only successful removals contribute to `total`.
Its `audit_segment_outcomes` gives every counted segment a bus-root-relative
`path`, one `removed|protected|blocked` status, and stable `reason_codes`.
Removed segments use `valid_expired`; protection identifies
`retention_target`, `retention_audit_key`, or `degraded_replay_key`; blocked
outcomes identify dependency-snapshot, read/parse, metadata, disappearance, or
unlink failures. The outcome count always equals the sum of the three aggregate
counts, and OA writes the same list into the `pruned` audit event.
Before any `valid_expired` segment is deleted, pruning writes one strict
`.rotated-prune/<run_id>.json` receipt containing the original cutoff, all
decisions, and exact SHA-256/byte witnesses for authorized removals. A later
run resumes the sole pending receipt before classifying new candidates.
Authorized files already absent after a process stop count as completed;
present files are fingerprint-checked again, and drift becomes
`segment_drifted` without deletion. The `pruned` event uses the receipt's
deterministic `rotated-prune:<run_id>` key. The receipt is removed only after
that key is confirmed in the complete audit snapshot. If append degrades,
`audit_prune_audit_committed=false` and the receipt remains for exact replay.
`oa.py audit-health` inspects `.rotated-prune/` without locks or writes. It
reports `rotated_prune_receipts`, `resumable_rotated_prune_count`, and
`blocked_rotated_prune_count`. Valid prepared/applied crash states with
matching or authorized-absent targets are resumable. Invalid schema,
unexpected or multiple entries, unreadable/non-file targets, fingerprint
drift, an incomplete audit snapshot, and a deletion target present after
`applied` are stable reason-coded blocked states. Either class raises health to
`attention` without changing `keyed_append_blocked`.
`oa.py audit-prune-resolve` is the only supported resolution when a target
reappears after a receipt reached `applied`. It requires the exact run ID,
receipt SHA-256, segment name, segment SHA-256, and explicit
`preserve-recreated` decision. Before changing the receipt it writes a strict
fingerprint-bound `.rotated-preserve/` marker. The outcome becomes
`operator_preserved_recreated_segment`, the original `pruned` event is
recovered under its run key, and a second deterministic resolution event
records the operator decision. Receipt completion requires both keys plus the
unchanged marker and target. Future pruning classifies the segment as
`operator_preserved_target`. Exact retry converges after either the marker or
receipt write; invalid/multiple receipts and every fingerprint mismatch are
refused.
`oa.py audit-health` also snapshots `.rotated-preserve/` strictly read-only.
It exposes `rotated_preservations` plus protected/blocked counts. A marker is
`protected` only when its target fingerprint and both the original prune and
resolution audit keys match. Orphaned markers, target drift, duplicate target
claims, invalid entries, and missing audit bindings are stable reason-coded
`blocked` states. Either class raises health to `attention` without changing
`keyed_append_blocked`; preserve blocked evidence for operator repair.
`oa.py audit-preserve-release` retires one valid protection under exact run,
segment, marker SHA-256, target SHA-256, and explicit `release-protection`
fences. A strict deterministic release event must be committed before the
runtime revalidates and unlinks only the marker. Audit failure retains the
marker. Event-first and post-unlink retries converge exactly, including strict
event-payload validation. Release never deletes or changes the segment; a
later normal prune may remove it.
`oa.py audit-health` groups committed release evidence by deterministic key
without locks, tail repair, or writes. It exposes `preservation_releases` plus
completed/resumable/blocked counts. A valid event with no marker is
`completed`; an exact protected marker still present is event-first
`resumable`. Duplicate events, key/payload conflict, marker fingerprint drift,
and blocked marker bindings are reason-coded `blocked`. Completed history is
informational; resumable or blocked evidence raises health to `attention`
without changing `keyed_append_blocked`.

Invoke them by the **absolute path of this bundle**. Follow the invocation
contract in the bundle's `SKILL.md` §0: replace `<PAO_SKILL>` with the absolute
path of the folder containing `SKILL.md`, then:

```bash
python "<PAO_SKILL>/scripts/pao.py"  --help
python "<PAO_SKILL>/scripts/oa.py"   --help
python "<PAO_SKILL>/scripts/lwar.py" --help
python "<PAO_SKILL>/scripts/adp_watch.py" --help
```

Before identity adoption, root resolution is explicit `--root` > `PAO_ROOT` >
`<cwd>/.pao`. Adopted identity-bearing LWAR commands derive the canonical bus
from the identity file; an explicit/env mismatch fails closed. Run with the current runtime's Python
(`python` and `python3` may differ). Diagnose version and root resolution with
`pao.py info`, and run `pao.py doctor --role oa|lwar` as a pre-flight.
`doctor` fails closed for remote/UNC bus roots because the transport requires
single-host local-filesystem atomic rename semantics.
