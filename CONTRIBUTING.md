# Contributing

Bug reports, documentation fixes, translations and evidence-backed device
support are welcome. Follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Report a problem

Search existing [issues](https://github.com/Dozi3/lanbon-mqtt/issues) first.
Include Home Assistant and integration versions, reported type/firmware,
Control status, a minimal reproduction, expected behavior and actual behavior.
Use the issue templates where available. Follow [Support](SUPPORT.md) for
sanitizing evidence; vulnerabilities use [private security reporting](SECURITY.md).

Do not attach raw household captures, credentials, HA backups, `.storage`
files or unreviewed screenshots. Replace peer/group/address identities
consistently with synthetic values while preserving packet structure.

## Develop locally

Use the checked-in Python requirements and `uv.lock`; do not install test
dependencies into a production Home Assistant environment.

```sh
uv sync --locked
uv run --locked python -m pytest -q
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .
```

The project currently targets Python 3.14 and a pinned Home Assistant test
runtime. See [TESTING.md](TESTING.md) for native acceptance and remaining
hardware limits. Check the workflow files for additional repository and
release validation commands used by CI.

## Submit a pull request

1. Make a focused branch from the default branch.
2. Describe the user-visible problem and the resulting behavior.
3. Add meaningful regression coverage for behavioral changes and update the
   relevant user/protocol documentation.
4. Run the affected tests, then the full relevant checks. Include their results
   and any checks you could not perform.
5. Keep runtime, tests and release metadata consistent. Do not include generated
   caches, local environments, captured household data or deployment scripts.

Preserve entity unique IDs, existing automation references, retained/live
separation and command-versus-feedback behavior. New models or packet fields
need traceable structure and semantic evidence. Keep uncertain fields raw;
do not assign a physical unit or actuator meaning from a plausible number.

Test real transport/configuration changes in an isolated Home Assistant and
broker first. State separately what software tests, MQTT receipt, HA state
and physical behavior establish. Obtain the installation owner's authorization
before tests that operate real devices.

Contributions are made under this repository's [MIT License](LICENSE). Do not
submit code or assets you do not have permission to contribute. There is no
requirement to upload proprietary firmware or application packages.
