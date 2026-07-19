# LWAR Reference — Registration and Identity Adoption

Replace `<PAO_SKILL>` with this skill's folder (SKILL.md §0). Before registering,
run the Session Bootstrap flow (SKILL.md §0.5): if you already hold a valid
identity file whose slot is still in the registry, **resume it instead of
registering again**.

## Registration

Use your OWN actual runtime metadata — the example below is illustrative
(Codex/OpenAI), not a template to copy. Fill each flag with the truth about the
session you are:

| Flag | What to put | If unknown |
|---|---|---|
| `--runtime-name` | the harness/CLI you run in (e.g. "Claude Code", "Kimi Code CLI") | ask the user |
| `--model` | your model name (e.g. "Claude Fable 5") | ask the user |
| `--adapter-id` | a lowercase slug for the runtime (e.g. `claude_code`) | derive from runtime-name |
| `--vendor-family` | lowercase vendor slug (e.g. `anthropic`, `moonshot`) | ask the user |
| `--interface` | one of `cli` \| `tui` \| `agent` \| `build` | `agent` for an agentic CLI |
| `--capability` | repeatable; what you can do (e.g. `coding`, `testing`) | omit if none apply |

Do not invent values you cannot attest — a wrong model/vendor label corrupts the
registry and any downstream (harness × model) matching. If you genuinely do not
know a required value, stop and ask the user rather than guessing.

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
| `0` | `identity_adopted` | The printed `identity_file` becomes the **only** valid identity input for later ADP calls |
| `2` | `registration_pending` | OA has not reconciled yet — poll again after a short wait |
| `3` | `registration_rejected` | Fail closed: inspect `reason`, do not retry the same request |

- If the response is `pending`, do not treat the identity as approved; retry after OA reconciles.
- Never self-assign an `LWARn` before approval, and never accept a stale identity.
