# CLAUDE

→ Read [`AGENTS.md`](AGENTS.md)

## PAO role routing

- Default role is `OA`; load `PAO_skills/pao-oa/SKILL.md`.
- `OA` never launches a vendor LWAR. It communicates through `python PAO_skills/pao-oa/scripts/oa.py` and the file bus.
- A runtime receiving `/lwar-register [number]` becomes an `LWAR`; load `PAO_skills/pao-lwar/SKILL.md`.
- An `LWAR` adopts its identity, then runs repeated `python PAO_skills/pao-lwar/scripts/adp_watch.py` slices in the same long-lived session.
