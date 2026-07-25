# Predictive Routing Evidence

## Question

Can PAO choose a quality-qualified lower-token LWAR before execution and retain
the quality of the calibration-selected best single LWAR on unseen tasks?

## Protocol

- Providers: four real provider/model families behind `LWAR1` through `LWAR4`
- Calibration: six tasks, two per class, 24 provider observations
- Held-out: nine preregistered tasks, three per class
- Classes: constraint ordering, code review, bounded optimization
- Route binding: all nine receipts and the calibration profile SHA-256 were
  persisted before the earliest held-out provider call
- Evaluation: a 36-call full panel supplied counterfactual scores; only the
  nine precommitted selected calls count toward routed-policy tokens
- Experimental gate: two observations per alias and class, zero permitted
  empirical quality drop

The benchmark catalogs and reproducible tools are:

- `benchmarks/predictive-router-calibration.json`
- `benchmarks/predictive-router-heldout.json`
- `tools/run_heterogeneous_lwar_ab.py`
- `tools/run_predictive_lwar_router.py`

## Result

| Policy | Accepted | Reported tokens |
|---|---:|---:|
| Calibration-selected single `LWAR1` | 9/9 | 170,998 |
| Precommitted predictive router | 7/9 | 128,631 |
| Post-hoc quality-preserving oracle | 9/9 | 112,744 |

The router reduced reported tokens by 24.8%, close to the earlier 25.6%
post-hoc opportunity, but it did not preserve quality. `LWAR4` failed H7 by
violating the cost limit and H8 by missing the lower-risk tie-break. `LWAR1`
passed both and all other held-out tasks.

All 36 provider calls completed, PAO audit health was healthy, all mailboxes
drained, 36 results were archived, and all four shutdown controls were
consumed. Provider-native token telemetry was complete for 35/36 calls. One
failed Kimi attempt emitted a Windows encoding error before usage telemetry;
its retry succeeded. Router and baseline token totals are complete.

## Verdict and safety response

`heldout_predictive_routing_not_validated`

The pre-execution routing and receipt contracts work, and the token opportunity
is real. Two observations per class were not enough to establish
quality-preserving generalization. The production profile compiler therefore
defaults to five observations per eligible alias and class, requires balanced
support across profile-known eligible aliases, and falls back to the global
calibration quality leader below that gate.

This is an empirical nine-task result, not a population-level guarantee.
Reported tokens are normalized provider-native telemetry and are not equivalent
to monetary cost.
