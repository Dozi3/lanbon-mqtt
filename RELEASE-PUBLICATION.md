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

Stable `0.3.0` promotes that accepted integration runtime with a stable manifest
version. The `v0.3.0-rc.4` tag and assets remain intact. Stable publication does
not expand the physical commissioning evidence in [TESTING.md](TESTING.md).

## Automatic maintenance

[Safe maintenance](https://github.com/Dozi3/lanbon-mqtt/actions/workflows/maintenance.yml)
runs daily at 04:47 UTC, after successful **Validate** runs, and manually through
**Run workflow**. Manual runs default to **dry run**. The job summary explains
what was eligible, blocked or changed. It uses GitHub's short-lived repository
token; no maintainer password or personal access token is stored.

### What can merge automatically

Only a genuine Dependabot PR updating the development **Ruff** pin to a newer
patch in the same major/minor series qualifies. The policy parses both
`pyproject.toml` and `uv.lock`, checks every other dependency and setting is
unchanged, and rejects extra files. A version label or PR title is insufficient.

The current head must have successful **Tests, lint and package**, **Hassfest**
and **HACS** checks from GitHub Actions, a matching successful **Validate** run,
and no pending or failed checks. The PR must be current with protected main.
Merges include the expected head SHA and use normal branch protections.

Ruff minor/major changes, Home Assistant/plugin upgrades, other dependencies,
device behavior, protocol, configuration, tests, packaging tools, security or
workflow changes require maintainer review. This deliberately treats an unknown
change as manual rather than guessing from its size.

### What can release automatically

After changes have reached main, the policy compares the whole unreleased diff
with the latest stable tag. Only documentation and eligible development files
qualify, and the integration runtime must be unchanged. A separate
`automation/release-X.Y.Z` PR increments the stable patch version and adds a
deterministic changelog section. It does not change device code.

The version PR receives explicitly dispatched full validation. It merges only
after the policy rechecks its exact generated content, bot author, base/head,
and successful checks. It then creates a matching immutable tag and explicitly
dispatches **Release** on that tag. Release repeats validation and compares the
archive with the tested artifact before publishing a stable release. Existing
tags are never moved or silently repackaged. Interrupted publication can be
retried on the same verified tag.

An unreleased runtime, workflow, test or packaging change blocks unattended
release until a maintainer performs a reviewed release. Documentation-only
maintenance releases may therefore have identical runtime files except for
their manifest version. Automated publication never enables HACS auto-update,
installs on a household server, or restarts Home Assistant.

### Permissions and stopping automation

The privileged maintenance job executes only the policy on protected main.
Candidate PR files are read as API data; their code and artifacts are never
executed with the write token. Permissions are limited to repository contents,
PRs, workflow dispatch and reading checks. The job never approves PR reviews or
bypasses protection. GitHub's combined **Allow GitHub Actions to create and
approve pull requests** setting is needed to create release PRs; the policy
only uses its creation capability.

To stop automatic merges and version PRs, open **Actions → Safe maintenance →
⋯ → Disable workflow**. Existing releases remain downloadable. Re-enable it
there when ready; inspect a manual dry run before resuming. The separate
**Release** workflow publishes only explicit version tags, and rejects branch
dispatches.

GitHub token pushes do not normally trigger another workflow, so the explicit
dispatches are required. See GitHub's [workflow triggering rules](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)
and [Dependabot automation guidance](https://docs.github.com/en/code-security/tutorials/secure-your-dependencies/automate-dependabot-with-actions).
