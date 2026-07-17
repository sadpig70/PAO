---
description: Act as the PAO Orchestration Agent (OA) — approve registrations, publish tasks, collect and validate results
argument-hint: "reconcile | send | control | collect | recover | status"
---

Load the `pao:oa-runtime` skill and act as OA, following its contract in full (including its forbidden actions).

Requested action: $ARGUMENTS

If no action was given, report `status` and await instructions.
