# LWAR Reference — Registration and Identity Adoption

Replace `<PAO_SKILL>` with this skill's folder (SKILL.md §0).

## Registration

Use only actual runtime metadata. Do not guess unknown values.

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

- When `event=identity_adopted`, the printed `identity_file` becomes the **only** valid identity input for later ADP calls.
- If the response is `pending`, do not treat the identity as approved; retry after OA reconciles.
- Never self-assign an `LWARn` before approval, and never accept a stale identity.
