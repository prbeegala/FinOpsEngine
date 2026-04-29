# FinOps Engine — tests

Stdlib-only engines, but tests use **pytest** (declared as an optional
dependency in `pyproject.toml`).

```pwsh
pip install -e ".[test]"
pytest
```

## Layout

```
tests/
├── conftest.py                       # imports engines by path; CSV diff helper
├── fixtures/
│   ├── rightsizing-peak/
│   │   ├── <scenario>.json           # input: VmRecord + canned az_metrics points
│   │   └── expected.csv              # one row per scenario, in deterministic order
│   └── context-enricher/
│       ├── input/                    # CSVs that mimic phase 1 / 2 outputs
│       └── expected/                 # snapshot of enriched.csv
├── test_rightsizing_peak.py          # parametrised over fixture JSONs
└── test_context_enricher.py          # E2E: runs main(), diffs output
```

## Adding a new fixture

The acceptance criterion on
[issue #18](https://github.com/prbeegala/FinOpsEngine/issues/18) is that
adding a new test case takes **just a `.json` and an `.expected.csv`** —
no Python edits.

### `rightsizing-peak`

1. Drop a new file under `tests/fixtures/rightsizing-peak/`, e.g.
   `vm-mem-spiky.json`:

   ```json
   {
     "vm": {
       "subscription_id": "00000000-0000-0000-0000-000000000001",
       "resource_group": "rg-test",
       "name": "vm-test-01",
       "resource_id": "/subscriptions/.../vm-test-01",
       "vm_size": "Standard_D4s_v5",
       "location": "uksouth",
       "power_state": "running"
     },
     "cpu_avg_points":  [{"average": 12.0}, {"average": 14.0}, ...],
     "cpu_max_points":  [{"maximum": 28.0}, {"maximum": 31.0}, ...],
     "mem_min_points":  [{"minimum": 9000000000}, ...],
     "expected_verdict": "DOWNSIZE_CANDIDATE",
     "expected_confidence": "HIGH",
     "expected_target_sku": "Standard_D2s_v5"
   }
   ```

2. The test discovers `*.json` files automatically and runs
   `analyse_vm()` against each, asserting the engine's verdict matches
   the expected fields.

### `context-enricher`

1. Drop new CSVs under `tests/fixtures/context-enricher/input/`.
2. Run the engine once manually to produce the expected output:
   ```pwsh
   python tools/context-enricher/context_enricher.py `
     --hidden-waste-csv tests/fixtures/context-enricher/input/hidden-waste.csv `
     --rightsizing-csv  tests/fixtures/context-enricher/input/rightsizing.csv `
     --out-dir tests/fixtures/context-enricher/expected/
   ```
3. Inspect the generated `enriched-<date>.csv` and rename it to
   `enriched.csv` (date stripped — the test ignores the date column).
4. Re-run `pytest`.

## Why this design

- **Engines are stdlib-only** — tests use pytest because tests aren't
  shipped, but engines themselves still install with zero pip deps.
- **Fixtures are JSON / CSV, not Python** — so non-Python contributors
  (FinOps analysts) can add coverage for new scenarios.
- **Snapshot diffs are column-aware** — `assert_csv_matches` points at
  the first differing cell with row index and column name. No 500-line
  text diffs.

## What's still missing

[Issue #18](https://github.com/prbeegala/FinOpsEngine/issues/18)'s
acceptance criterion is "at least one fixture per engine". This first
PR delivers **2 of 4**:

- ✅ `rightsizing-peak` — `analyse_vm` decision tree
- ✅ `context-enricher` — full E2E pipeline
- ⏳ `hidden-waste` — needs `az_rest` mock; tracked separately
- ⏳ `ri-coverage` — needs `az_rest` mock; tracked separately
