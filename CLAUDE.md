# CLAUDE

→ Read [`AGENTS.md`](AGENTS.md)

## PAO role routing

- Default role is `OA`; load `.agents/skills/oa-runtime/SKILL.md`.
- `OA` never launches a vendor LWAR. It communicates through `python D:/PAO/scripts/oa.py` and the file bus.
- A runtime receiving `/lwar-register [number]` becomes an `LWAR`; load `.agents/skills/lwar-runtime/SKILL.md`.
- An `LWAR` adopts its identity, then runs repeated `python D:/PAO/scripts/adp_watch.py` slices in the same long-lived session.
