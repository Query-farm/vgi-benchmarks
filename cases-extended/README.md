# cases-extended/

Benchmark definitions that aren't part of the default `cases/` set.

Move a file into `../cases/` to include it in `vgi-bench run`. They live here so
the active suite stays focused (currently just `scalar_multiply` and
`scalar_upper_case`) without losing the definitions of fixtures we've validated
but don't always want to spend wall-clock on.

| File | Function type | Notes |
|------|---------------|-------|
| `aggregate_sum.json` | aggregate | UPDATE + COMBINE + FINALIZE; output is one row per group |
| `aggregate_avg.json` | aggregate | same shape, different finalize |
| `table_sequence.json` | table | Server-to-client streaming, no input |
| `table_constant_columns.json` | table | Wider output schema, exercises Arrow batch framing |
| `table_in_out_echo.json` | table_in_out | Bidirectional streaming; transparent passthrough |
| `table_in_out_sum_all.json` | table_in_out | Reduces multiple input cols to one output |

Each one follows the same JSON schema as the active cases (see top-level
`README.md` "Adding a new case"). Validate after editing:

```bash
uv run vgi-bench validate
```
