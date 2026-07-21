# PAO ADP Operations Overview

This document describes the operating model without duplicating the executable
contract. The canonical instructions are:

- OA: `.agents/skills/pao-oa/SKILL.md` and its bundled references
- LWAR/ADP: `.agents/skills/pao-lwar/SKILL.md` and its bundled references

## Runtime Model

The user starts one OA session and one or more LWAR sessions on the same local
bus, in either order. Each session receives only the instruction to read its role
skill and act in that role. OA publishes a short-TTL presence signal while it
supervises durable workflows; each LWAR can distinguish live, stale, missing,
and invalid OA presence, self-registers, adopts an approved `LWARn` identity,
and remains in its ADP watch/execute loop.

```text
OA: presence -> reconcile -> plan/publish -> monitor -> collect/validate -> recover
LWAR: oa-status -> register/adopt -> watch -> execute/submit -> watch -> shutdown/retire
```

All deterministic bus operations, exit-code reactions, controls, result states,
claim fencing, lifecycle transitions, and recovery rules live in the two role
skills. Operators and runtimes must not reconstruct those mutable commands from
this overview.

## Deployment Boundary

- Both roles use the same bus selected by `--root`, `PAO_ROOT`, or `<cwd>/.pao`.
- The bus must be on a single-host local filesystem.
- Task execution occurs in each TaskContract's own `cwd` and authority bounds.
- OA never drives a vendor CLI directly; LWARs receive work only through PAO.
- Mailbox, registry, identity, lease, and ledger files are never edited by hand.

## Operator Expectations

- OA approval is required before a registering runtime may claim `LWARn`.
- OA and LWAR startup order is independent; an LWAR waits when OA presence is
  absent or stale and never self-approves.
- LWAR ADP remains resident across idle watch slices.
- Every claimed task produces exactly one terminal result.
- OA validates completion criteria and evidence; process exit alone is not
  success.
- Expired or superseded claims are recovered and fenced by the bundled runtime.
- `shutdown` retains a resumable slot; `retire` completes the lifecycle through
  deregistration and returns the slot.

For execution, read the applicable role skill. No separate ADP bootstrap prompt
is valid.
