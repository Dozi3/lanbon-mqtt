## Change

Describe the problem and the resulting behavior. Link any related issue.

## Verification

- [ ] `uv sync --locked --group dev`
- [ ] `uv run --locked ruff check .`
- [ ] `uv run --locked ruff format --check .`
- [ ] `uv run --locked pytest -q` (including the local Mosquitto wire test)
- [ ] `uv run --locked python tools/build_release.py`
- [ ] Relevant documentation and synthetic examples are updated.

Describe any hardware testing, including model/firmware and what was physically observed.
Distinguish device feedback from physical operation. State anything not tested.

## Compatibility

Does this change entity IDs, saved settings, topic routing, broker access, commands,
or state interpretation? Explain migration and rollback where relevant.

Do not include credentials, real device identifiers, private addresses or raw captures.
