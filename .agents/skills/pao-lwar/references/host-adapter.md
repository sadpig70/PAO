# Host Adapter Supervision Contract

This contract applies when a host runtime wraps PAO commands in tool calls that
may impose a maximum blocking duration shorter than the resident ADP lifetime.
A host timeout is an interruption of result delivery, not proof that the LWAR,
identity, claim, or task failed.

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
- exactly one terminal result reaches OA collection
