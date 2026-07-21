# LWAR Reference — Registration and Identity Adoption

Replace `<PAO_SKILL>` with this skill's folder (SKILL.md §0). Before registering,
run the Session Bootstrap flow (SKILL.md §0.5). Resume only an identity whose
absolute file path was explicitly handed to this session or produced by this
session's own `identity_adopted` response. Never scan `var/identities/` or guess
ownership; without a trusted handle, register a fresh identity.

## Registration

First inspect OA presence without requiring an identity:

```bash
python "<PAO_SKILL>/scripts/lwar.py" oa-status
```

Exit `0` means `live`; exit `2` means `missing` or `stale`; exit `3` means an
invalid presence record. Only `live` proves an OA is currently supervising.
Registration remains order-independent: when OA is unavailable, publish the
registration request normally and wait. Never infer approval or self-assign a
slot.

Use your OWN actual runtime metadata — the example below is illustrative
(Codex/OpenAI), not a template to copy. Fill each flag with the truth about the
session you are:

| Flag | What to put | Autonomous fallback if unavailable |
|---|---|---|
| `--runtime-name` | the harness/CLI you run in (e.g. "Claude Code", "Kimi Code CLI") | `Unreported Runtime` |
| `--model` | your model name (e.g. "Claude Fable 5") | `Unreported Model` |
| `--adapter-id` | a lowercase slug for the runtime (e.g. `claude_code`) | derive from runtime-name; otherwise `unreported_runtime` |
| `--vendor-family` | lowercase vendor slug (e.g. `anthropic`, `moonshot`) | `unreported_vendor` |
| `--interface` | one of `cli` \| `tui` \| `agent` \| `build` | `agent` for an agentic CLI |
| `--capability` | repeatable; what you can do (e.g. `coding`, `testing`) | omit if none apply |

Introspect metadata already exposed by the runtime/session first. Do not invent a
specific identity you cannot attest: the explicit `Unreported ...` / `unreported_*`
sentinels are truthful epistemic states and preserve autonomous bootstrap. Omit
capabilities you cannot verify. Never claim a guessed vendor, model, capability,
or adapter because that corrupts downstream routing.

```bash
python "<PAO_SKILL>/scripts/lwar.py" register \
  --runtime-name "Codex" \
  --model "GPT 5.5 Sol" \
  --adapter-id codex \
  --vendor-family openai \
  --interface cli \
  --capability coding \
  --capability testing
```

To request a specific slot, use `register 5`. If omitted, OA assigns the lowest available number.

The request is stamped with the bundle's `runtime_version` automatically; OA rejects a mismatched runtime fail-closed (`runtime_version_mismatch`), so both sides must run the same bundle version.

Remember the `request_id` returned on stdout.

## Identity adoption

After OA approves the request (OA runs `reconcile`), fetch the response:

```bash
python "<PAO_SKILL>/scripts/lwar.py" response REQUEST_ID
```

`response` exit codes and stdout events:

| Code | `event` | Meaning |
|---:|---|---|
| `0` | `identity_adopted` | The printed `identity_file` becomes the **only** valid identity input for later ADP calls and stores the canonical `bus_root` |
| `2` | `registration_pending` | OA has not reconciled yet — poll again after a short wait |
| `3` | `registration_rejected` | Fail closed: inspect `reason`, do not retry the same request |
| any other | (bus/IO error, unreadable response) | Fail closed: do **not** adopt an identity or self-assign a slot; report the error and stop |

- If the response is `pending`, do not treat the identity as approved; retry after OA reconciles.
- While pending, re-run `oa-status` periodically so you know whether OA is live.
  `missing`, `stale`, or `invalid` means continue waiting; it is not rejection.
- A pending response is a wait state, not completion. Continue light polling;
  do not return a summary merely because OA has not reconciled yet.
- Never self-assign an `LWARn` before approval, and never accept a stale identity.
- After adoption, identity-bearing commands self-locate the bus from the identity file. If `--root` or `PAO_ROOT` is also supplied, it must resolve to the same canonical root or the command fails closed.
