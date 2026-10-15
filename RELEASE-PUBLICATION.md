# Release and publication process

[Home](README.md) · [Changelog](CHANGELOG.md) · [Verification](TESTING.md)

The public project is [Dozi3/lanbon-mqtt](https://github.com/Dozi3/lanbon-mqtt).
It uses one integration directory, `custom_components/lanbon_mqtt`, with
public documentation, issue tracking, maintainer metadata and local brand
assets. Distribution is under the [MIT License](LICENSE).

## Prepare a release

1. Finalize runtime changes and meaningful tests in a reviewed pull request.
2. Update the component manifest version and changelog. Declare any minimum
   Home Assistant change in `hacs.json` and migration/rollback documentation.
3. Run the locked suite, lint/format checks, Home Assistant/HACS validation,
   link checks and a secret/privacy review on the exact release revision.
4. For behavior or transport changes, complete isolated native HA/MQTT
   acceptance. Record physical tests separately from synthetic evidence.
5. Verify that the final archive contains only allowlisted package content,
   record its checksum, and confirm that its runtime matches the tested source.
6. Publish a matching Git tag and GitHub release. Mark release candidates as
   prereleases. Attach the generated package/checksum where the release
   workflow provides them, with verification results and known limitations.

Do not edit a published release in place to change runtime behavior. Publish a
new version. Never include household captures, backups, deployment credentials,
local environments or author-specific administration scripts.

## HACS distribution

Users can add this public repository as a **custom Integration repository**;
see [SETUP.md](SETUP.md). Default-catalogue inclusion is a separate HACS
submission and must not be implied by a custom-repository badge.

Current [HACS integration requirements](https://www.hacs.xyz/docs/publish/integration/)
include a single integration, the required manifest metadata and brand assets.
Local `brand/icon.png` assets are supported by the current requirements; an
external Home Assistant Brands submission is not required for that route.
Follow the [general publication requirements](https://www.hacs.xyz/docs/publish/start/)
and [HACS validation action](https://www.hacs.xyz/docs/publish/action/) when
checking a release. Recheck these requirements before a future catalogue
submission because they can change.

## First public candidate

`0.3.0-rc.4` packages the accepted `rc.3` control runtime with public-facing
metadata, docs, assets and automation. It retains schema version 2 and does
not itself update an existing household installation. The preceding
[runtime verification](TESTING.md) remains distinct from public packaging/CI,
HACS installation and other users' physical commissioning.
