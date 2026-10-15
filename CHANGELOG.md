# Changelog

## 0.3.1

- Maintenance release containing validated documentation and/or Ruff patch updates.
- Device control runtime and configuration schema are unchanged.

Changes are listed by release. Commission a safe output after installation or
an update. Release candidates are explicitly marked as prereleases.

## 0.3.0 — 2026-10-15

First stable public release. Promotes the accepted `0.3.0-rc.4` integration
runtime without changing device behavior, entity identities or storage schema.

- Merge the verified Dependabot Ruff development-tooling update.
- Retain the reviewed fix for byte-identical Windows/Linux release archives.
- Add conservative automatic maintenance: verified Ruff patch updates and
  separate, tested patch-release PRs for documentation/development-only changes.
- Keep runtime, protocol, Home Assistant fixture and workflow changes under
  maintainer review; publishing does not update anyone's Home Assistant.
- Preserve the previous release candidate and its immutable version tag.

## 0.3.0-rc.4 — 2026-10-15

First public GitHub/HACS custom-repository package.

- Publish installation, configuration, usage, troubleshooting and rollback
  documentation, with isolated synthetic Home Assistant examples.
- Add public maintainer/documentation/issue metadata, local brand assets,
  repository policies, contribution guidance and automated validation.
- Provide a reproducible public release package and an MIT distribution license.
- Keep the accepted `0.3.0-rc.3` control runtime and schema version 2; this is a
  publication/packaging change, not a new device protocol or household update.

## 0.3.0-rc.3 — local acceptance candidate

- Add native **Add Lanbon controls alongside existing controls**, off by
  default, while preserving existing controls and entity identities.
- Add enabled per-device **Control status** with actionable ownership/route
  explanations.
- Permit independently validated active native MQTT metadata to qualify exact
  relay routes in the opt-in mode; recheck transient routes before commands.
- Exclude configuration/diagnostic administration switches from functional
  control ownership.
- Cover stale-route withdrawal/retargeting and feedback precedence with
  regression tests.

The runtime passed 524 software tests, 13 separate independent runtime checks,
11 isolated HA/MQTT acceptance checks and a separate cold-restart check. One
physical `0x4D` dimmer followed the new control's 25% → 60% → 100% → OFF sequence.
See [verification](TESTING.md) for scope and limits.

Earlier candidates were local research/acceptance packages and were not public
HACS releases.
