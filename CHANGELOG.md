# Changelog

## 1.4.1 — 2026-07-26

- Treat an alias/class circuit reset as a promotion epoch watermark.
- Exclude observations at or before `reset_at` from that alias/class's later
  promotion statistics as well as circuit refresh.
- Require fresh ordinary shadow evidence before a reset candidate can return
  to `confidence_qualified_live`.
- Add a sealed LWAR4 reset/requalification campaign with paired recovery,
  conditional reset, post-reset Wilson requalification, and one production
  canary.

## 1.0.0 — 2026-07-25

PAO v1.0.0 is the first finite skills-only production release. Operation
requires only the self-contained `pao-oa` and `pao-lwar` skill folders; no pip
package or plugin is required.

### Release guarantees

- autonomous OA and LWAR bootstrap from their own `SKILL.md`
- order-independent dynamic LWAR registration
- identity, generation, registry, attempt, and claim provenance fencing
- semantic OA acceptance before workflow dependency release
- content-addressed artifact snapshots with authority bounds
- crash-consistent audit, repair, retention, and preservation-release flows
- Windows and Ubuntu verification with byte-identical runtime bundles
- protected pull requests and live repository-policy drift auditing
- durable, deduplicated credential lifecycle issue escalation

### Breaking protocol boundary

PAO v1.0.0 closes the optional-first compatibility window. It rejects:

- registration requests without an exact `runtime_version`
- task records without registry, attempt, and explicit permission contracts
- result records without workflow, registry, attempt, and claim provenance
- legacy string artifacts and unknown task/result contract fields

Use a fresh bus for v1, or intentionally retire the pre-v1 bus after preserving
required evidence. `pao doctor` reports detected pre-v1 protocol records through
the `v1_bus_contract` check. The runtime never silently rewrites historical bus
records.

### Credential operations

The repository policy audit accepts only a repository-scoped fine-grained PAT.
Its audit job remains read-only. A separate least-privilege job uses the
short-lived workflow token to open, deduplicate, update, and close a managed
credential lifecycle issue without exposing the audit PAT.
