# PAO command wrappers

These four thin wrappers (`pao.py`, `oa.py`, `lwar.py`, `adp_watch.py`) are the
entry points for the PAO tools. Each one bootstraps its own import path from the
bundle it lives in (`Path(__file__).resolve().parents[1]`), so no pip install,
no `PYTHONPATH`, and no plugin are required — the wrapper works from any working
directory.

Runtime v0.7.19 validates bundled JSON contracts at all trust boundaries. OA
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
