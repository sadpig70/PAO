# DF-01 Remediation Report — Identity-Bound Bus Root

Date: 2026-07-21
Runtime: PAO 0.7.1
Finding: adopted identity did not make a non-default bus self-locating

## Outcome

DF-01 is resolved. An adopted LWAR identity now binds to one canonical bus root.
Identity-bearing LWAR commands can cold-resume with only the absolute identity
file, while conflicting `--root` or `PAO_ROOT` values fail closed before the
conflicting bus is touched.

## Implemented Controls

```text
DF01Remediation // identity-bound bus root (done) @v:0.7.1
    PersistRoot // adoption writes absolute identity.bus_root (done)
    ResolveRoot // status/state/complete/ADP derive root from identity (done)
    LegacyResume // infer root from <root>/var/identities for old identities (done)
    ConflictFence // explicit/env mismatch fails before foreign-bus I/O (done)
    ContractDocs // skill, references, wrapper README, schema updated (done)
    Regression // focused, full-suite, live old-identity cold resume (done)
```

- `lwar response` persists the resolved absolute `bus_root` in every new identity.
- `lwar status`, `lwar state`, `lwar complete`, and `adp_watch` resolve from the
  identity when no root override is supplied.
- Existing identities without `bus_root` remain compatible when stored in their
  canonical `<root>/var/identities/` directory.
- A matching explicit/env root is allowed; a mismatch reports the identity-root
  conflict and performs no write against the conflicting location.
- Root resolution occurs before local-filesystem and transport initialization.

## Verification

- Focused ADP/LWAR integration: **9/9 passed**.
- Full repository suite: **100/100 passed** in 93.525 seconds.
- Bundle mirror: `python tools/sync_bundles.py --check` -> `in_sync: true`.
- Diff hygiene: `git diff --check` passed.
- Live post-fix reproduction used the original PAO 0.7.0 dogfood identity, which
  has no `bus_root`, on its non-default ignored bus:
  - `lwar status --identity-file <absolute>` with no root -> `lwar_status`, exit 0.
  - `adp_watch --identity-file <absolute>` with no root -> `idle_timeout`, exit 10.
  - No `PAO_ROOT` was present; the legacy canonical identity location supplied
    the correct bus.
- Conflict regressions cover both explicit `--root` and `PAO_ROOT`; ADP returns
  fatal exit 30 and the conflicting directory remains absent.

## Verdict

- DF-01: `passed`
- Self-hosted operational dogfooding after remediation: `passed`

## Next Highest-Priority Work

Run PAO 0.7.1 cross-runtime dogfooding with at least one independently hosted,
non-identical LWAR implementation. The current dogfood proved the OA/LWAR
protocol with multiple agents but not the vendor-neutral interoperability claim.
