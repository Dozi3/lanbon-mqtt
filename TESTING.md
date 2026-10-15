# Verification and commissioning

[Home](README.md) · [Compatibility](docs/compatibility.md) · [Contributing](CONTRIBUTING.md)

## Run the software checks

Use the repository's locked development environment, separate from production
Home Assistant:

```sh
uv sync --locked
uv run --locked python -m pytest -q
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .
```

The lock targets Python 3.14 and Home Assistant 2026.10.1, with its matching
custom-component pytest plugin. Home Assistant, MQTT test support and lint/test
tools are development dependencies; this project has no installable runtime
dependencies, and the integration manifest also has no extra requirements.
MQTT tests need Mosquitto on `PATH` (or `MOSQUITTO_EXE` set to its executable)
and local loopback access. CI installs a real isolated broker and rejects
skipped tests. Check the workflows in `.github/workflows` for additional
public repository, Home Assistant/HACS validation and release checks. Current
results are visible under [GitHub Actions](https://github.com/Dozi3/lanbon-mqtt/actions).

The software suite covers protocol parsing, bounds, retained/current report
separation, observation leases, queries, discovery, native setup/options,
control ownership, exact routes, diagnostics redaction and optional metadata.
Do not point test fixtures at a household broker.

## Accepted control-runtime evidence

The control runtime accepted before publication (`0.3.0-rc.3`) passed:

| Layer | Result and scope |
|---|---|
| Software suite | **524 passed**, zero failures, errors or skips, including 19 new native option cases and 27 ownership/runtime cases |
| Independent runtime review | **13 additional checks passed**, separate from the 524-test count |
| Lint and formatting | Ruff checks passed for all 45 Python files in that candidate |
| Native HA/MQTT acceptance | **11 checks passed** on isolated HAOS/Core 2026.10.1 with a real broker and synthetic peers |
| Cold Core restart | Options and five synthetic controls recovered; **zero startup actuator commands** observed |
| Package/source integrity | All 18 accepted runtime files matched the frozen package and staged installation |
| Physical commissioning | One `0x4D` dimmer followed **25% → 60% → 100% → OFF** through the new Lanbon control, with fresh feedback and the user's physical confirmation |

The isolated native checks exercised existing functional MQTT entities plus a
configuration administration switch; the default preserve-existing policy;
opt-in dimmers and single/double relay routes; deferred state until feedback;
QoS 0/nonretained actuator commands; option/master-gate toggles; stable IDs;
and enabled Control status. Native metadata supplied routes, never output state.

A stale transient-route defect in the first offline candidate was reproduced
and fixed before deployment. Regressions cover unique retargeting, withdrawn
or disabled native entities, missing/zero-count subscriptions, contradicted
command aliases, immediate command-time revalidation, real feedback precedence
and nonpersistence of metadata-only routes.

## Public release evidence

Public `0.3.0-rc.4` retains that accepted control runtime and configuration schema
version 2. It adds public metadata, documentation, branding and repository
automation, plus portable/sanitized test infrastructure. Fresh repository CI
is evidence for the public revision; the historical counts above identify the
accepted runtime baseline and are not a claim that every future revision has
the same test count.

Release checks must validate the final combined source and archive. Neither
historical green tests nor a successful metadata validator substitutes for
fresh runtime checks when behavior changes.

Stable `0.3.0` promotes that same integration runtime; the manifest version is
the only changed integration file. It also updates development Ruff and adds
repository maintenance policy tests. Fresh CI on the stable tag verifies the
combined source, HACS/Hassfest metadata and release package. Maintenance policy
tests cover disallowed runtime/lockfile changes, author and check validation,
deterministic version PRs and dry-run mutation prevention.

The native setup wizard was also completed on an isolated Core 2026.10.1
installation using its on-screen fields and synthetic MQTT peers. A dimmer,
a two-gang relay and a single relay exposed four available controls. The
optional root form was captured as an unsaved draft; its example address was
never contacted. Five screenshots show that native UI, and the HACS example
is attributed to its official documentation.

The initial public HACS check and all 524 Linux software tests passed.
Hassfest's manifest-ordering finding was corrected, after which all three
public validation jobs passed. The development lock was then refreshed to the
supported Home Assistant 2026.10.1 dependency set, including PyJWT 2.15.1 and
cryptography 50.0.1, without overriding Home Assistant's declared requirements.
Consult the linked Actions runs for validation of the final release commit.

## Commission your own installation

After installation or a material update, use a safe output to verify on/off,
brightness where supported, fresh feedback, physical behavior and restoration
of its previous state. Check each relay gang independently before using it in
an automation. Keep a pre-update backup and follow [rollback guidance](docs/updates.md).

The single physical dimmer result does not commission all models, firmware,
loads or networks. Relay hardware acceptance, every installation method,
long-duration behavior and future Home Assistant versions are not established
by synthetic fixtures. Root-cache peer age, raw measurement units/calibration,
timer execution and device clock correctness remain unverified. Keep network
receipt, HA state and physical output as separate acceptance layers.
