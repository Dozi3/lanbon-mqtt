# Changelog

Changes are listed by release. Release candidates are prereleases and should
be commissioned on a safe output before broader use.

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
