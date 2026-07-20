# PAO command wrappers

These four thin wrappers (`pao.py`, `oa.py`, `lwar.py`, `adp_watch.py`) are the
entry points for the PAO tools. Each one bootstraps its own import path from the
bundle it lives in (`Path(__file__).resolve().parents[1]`), so no pip install,
no `PYTHONPATH`, and no plugin are required — the wrapper works from any working
directory.

Invoke them by the **absolute path of this bundle**. Follow the invocation
contract in the bundle's `SKILL.md` §0: replace `<PAO_SKILL>` with the absolute
path of the folder containing `SKILL.md`, then:

```bash
python "<PAO_SKILL>/scripts/pao.py"  --help
python "<PAO_SKILL>/scripts/oa.py"   --help
python "<PAO_SKILL>/scripts/lwar.py" --help
python "<PAO_SKILL>/scripts/adp_watch.py" --help
```

Bus root resolution for every command: explicit `--root` > `PAO_ROOT` env > a
`.pao/` folder under the current directory (the default — all PAO state stays in
one hidden folder; add `.pao/` to `.gitignore`). Set `PAO_ROOT` (or pass `--root`)
to use a central bus outside the project. Run with the current runtime's Python
(`python` and `python3` may differ). Diagnose version and root resolution with
`pao.py info`, and run `pao.py doctor --role oa|lwar` as a pre-flight.
