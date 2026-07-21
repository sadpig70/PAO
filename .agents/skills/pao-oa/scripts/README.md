# PAO command wrappers

These four thin wrappers (`pao.py`, `oa.py`, `lwar.py`, `adp_watch.py`) are the
entry points for the PAO tools. Each one bootstraps its own import path from the
bundle it lives in (`Path(__file__).resolve().parents[1]`), so no pip install,
no `PYTHONPATH`, and no plugin are required — the wrapper works from any working
directory.

Runtime v0.7.2 validates bundled JSON contracts at all trust boundaries. OA
mutations require `PAO_OA_ID` and refresh a short-TTL presence signal independently
of the writer lease. LWARs inspect it with `oa-status`; clean one-time workers
return their slots through `retire`. `complete` requires the exact claim token
emitted in `task_received`.

Invoke them by the **absolute path of this bundle**. Follow the invocation
contract in the bundle's `SKILL.md` §0: replace `<PAO_SKILL>` with the absolute
path of the folder containing `SKILL.md`, then:

```bash
python "<PAO_SKILL>/scripts/pao.py"  --help
python "<PAO_SKILL>/scripts/oa.py"   --help
python "<PAO_SKILL>/scripts/lwar.py" --help
python "<PAO_SKILL>/scripts/adp_watch.py" --help
```

Before identity adoption, root resolution is explicit `--root` > `PAO_ROOT` >
`<cwd>/.pao`. Adopted identity-bearing LWAR commands derive the canonical bus
from the identity file; an explicit/env mismatch fails closed. Run with the current runtime's Python
(`python` and `python3` may differ). Diagnose version and root resolution with
`pao.py info`, and run `pao.py doctor --role oa|lwar` as a pre-flight.
`doctor` fails closed for remote/UNC bus roots because the transport requires
single-host local-filesystem atomic rename semantics.
