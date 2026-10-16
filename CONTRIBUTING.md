# Contributing

Adapters for more tools are the most useful contribution.

## Add an adapter (about 30 lines)

An adapter reads a tool's logs and yields `Usage` objects (see `spendguard/core.py`).

1. Add `read_<tool>(dirs) -> Iterator[Usage]` to `spendguard/adapters.py`. Look at `read_claude` as the model. Normalise the timestamp to an aware datetime with `parse_ts`, and keep token counts as non-negative ints.
2. **De-duplicate** if the tool logs one response several times. Check with a real log: count raw lines versus unique ids.
3. Register it in `load_records` in `spendguard/cli.py`.
4. Add a test in `tests/test_spendguard.py` with a small fixture that includes noise (bad JSON, other event types).
5. Mention the tool in the README.

Only send logs or fixtures you are allowed to share, and strip any prompts or code. spendguard only needs timestamps, model names and token counts.

## Rules

- Standard library only. No dependencies.
- `python -m unittest discover -s tests -v` must pass.
- Never make unpriced models count as free silently. They must show up as unpriced.
