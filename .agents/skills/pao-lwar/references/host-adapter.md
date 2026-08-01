# Host Adapter Supervision Contract

This contract applies when a host runtime wraps PAO commands in tool calls that
may impose a maximum blocking duration shorter than the resident ADP lifetime.
A host timeout is an interruption of result delivery, not proof that the LWAR,
identity, claim, or task failed.

## Capability fence

Prompt instructions are not enforcement. Before a provider run that requires
zero tools or exact token telemetry, the host adapter MUST prove both properties
at its process boundary. The bundled Qwen supervisor provides that boundary:

```bash
python "<PAO_SKILL>/scripts/host_adapter.py" qwen-probe --live
python "<PAO_SKILL>/scripts/host_adapter.py" qwen-run \
  --task-file task.json \
  --prompt-file prompt.txt \
  --receipt-file receipt.json
```

The live probe is eligible only when Qwen supports `--bare`,
`--output-format json`, `--max-tool-calls 0`, and `--max-wall-time`, then
actually returns zero tool calls, exact input/output/total tokens, and at most
one provider call. A static probe without `--live` is intentionally ineligible.

`qwen-run` requires this exact TaskContract extension:

```json
{
  "adapter_options": {
    "host_contract": {
      "adapter_id": "qwen_code",
      "tool_policy": "deny_all",
      "token_telemetry": "exact_provider_report",
      "max_provider_calls": 1
    }
  }
}
```

The supervisor always adds `--bare --output-format json --max-tool-calls 0`,
checks provider-call and token totals against the terminal statistics, and
writes a `pao.host-execution-receipt.v1`. Missing telemetry, any tool call, a
second provider call, malformed output, timeout, non-zero exit, or task-contract
drift writes a rejected receipt and exits 4. Only an accepted receipt may feed
calibration or routing evidence.

## Kimi Code CLI adapter

A second supervisor enforces the same `deny_all` / `exact_provider_report`
contract for the Kimi Code CLI (`adapter_id: kimi_cli`, vendor `moonshot`,
model `kimi-code/kimi-for-coding`):

```bash
python "<PAO_SKILL>/scripts/host_adapter.py" kimi-probe --live
python "<PAO_SKILL>/scripts/host_adapter.py" kimi-run \
  --task-file task.json --prompt-file prompt.txt --receipt-file receipt.json
```

The task `host_contract` is identical except `adapter_id` is `kimi_cli`. The
supervisor always runs
`--print --output-format stream-json --max-steps-per-turn 1 --model kimi-code/kimi-for-coding`.
Tool discipline is structural: one step per turn cannot both call a tool and
emit a final answer, and any assistant non-text tool part or tool-role stream
event additionally rejects as `tool_call_observed`. Token telemetry is read only
from the current session's exported `wire.jsonl` (`kimi export SESSION --yes`):
the latest non-empty `StatusUpdate.token_usage` is folded into
input/output/total, where every component must be a non-negative integer whose
key starts with `input` or `output` (an unclassifiable key fails closed).

Two adapter-scoped limitations are recorded honestly and must be confirmed
before the limitation matters:

1. **No provider-call count.** Kimi `token_usage` exposes no per-call request
   count, so the receipt omits `provider_calls`; `max_provider_calls=1` is
   enforced structurally by the single turn, not observed from telemetry.
2. **Tool-signal token set is an assumption.** Tool detection matches content
   part types / event roles containing `tool` or `function`. A real Kimi
   tool-use sample must confirm this token set (and that legitimate non-text
   reasoning parts, if any, are not misread) before the first calibration call.

## Durable handles

Before invoking `lwar.py response REQUEST_ID --resident`, the adapter MUST retain:

- the exact registration `request_id` emitted by its own `register` call
- the exact explicit bus root, or the unchanged environment/cwd that resolves it
- the resident timing arguments it supplied

The `request_id` is the only pre-identity recovery handle. After any watcher
event is received, the event's absolute `identity_file` becomes the normal
identity handle. The adapter MUST NOT scan `var/identities/`, infer ownership
from filenames, or register again merely because a blocking call timed out.

## Timeout recovery

On a host-enforced timeout that discards resident stdout:

1. Do not interpret the timeout as task failure or submit a result.
2. Re-run the exact `lwar.py response REQUEST_ID --resident` command against the
   same bus root.
3. Let response replay reconstruct and verify the exact
   `(lwar_id, instance_id, generation)` identity.
4. Let ADP inspect only that identity's mailbox. If one unexpired leased claim
   exists, ADP emits it again as `task_received` with
   `recovered_claim: true`.
5. Call `lwar.py begin` with the event's `claim_token`, stable `execution_id`,
   and new `invocation_id`.
6. Execute only on `execution_began`. On `execution_fenced`, do not execute;
   another context already owns the claim.
7. Complete with both the unchanged `claim_token` and the granted
   `execution_token`.

The watcher checks a resumable claim before accepting new work. It does not
extend the lease, rotate the token, or manufacture a TaskContract. An expired
claim is left to OA `recover`; multiple active claims fail closed as ambiguous.
The final `complete` command remains fenced against any concurrent OA recovery,
so a superseded token cannot publish a second terminal result.

## Acceptance criteria

- replay never registers a new LWAR or increments generation
- replay reconstructs only the identity bound to the owned request id
- one live claim is redelivered with byte-identical `claim_token`
- every replay has a higher invocation epoch but the same stable `execution_id`
- delayed old invocations cannot begin after replay supersession
- concurrent/replayed begin calls expose only one execution token
- new tasks are not claimed while a resumable claim exists
- expired, mismatched, or ambiguous claims are never adopted speculatively
- capability discovery without a live probe is never execution-eligible
- any tool call or missing/inconsistent token telemetry rejects the host receipt
- exactly one terminal result reaches OA collection
