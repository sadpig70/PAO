# PAO ADP command wrappers

These wrappers are the canonical entry points for the PAO tools; they bootstrap their own import path from the parent `PAO_plugin/` directory.

```bash
python PAO_plugin/scripts/oa.py --help
python PAO_plugin/scripts/lwar.py --help
python PAO_plugin/scripts/adp_watch.py --help
```

Run commands from the repository root and pass `--root .` when an explicit root is useful; in operation mode prefix with `$PAO_HOME` (the `PAO_plugin` directory) instead.
