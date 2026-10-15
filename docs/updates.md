# Updates, rollback and removal

[Home](../README.md) · [Setup](../SETUP.md) · [Troubleshooting](troubleshooting.md)

## Before a version change

1. Read the target release notes, compatibility requirements and known issues.
2. Record the installed Lanbon version and any changed options. Take note of
   entities used by dashboards and automations.
3. Create a Home Assistant backup containing the configuration and relevant
   broker/app data. Verify completion, download a recovery copy, and keep its
   encryption recovery key separately where required.
4. Choose a time when you can restart Home Assistant and safely check one
   output. Keep a copy of the last working release.

Home Assistant's [backup documentation](https://www.home-assistant.io/common-tasks/general/#backups)
explains the native backup and recovery process. External brokers/databases
may need separate backups; do not assume a Home Assistant backup includes them.

## Update through HACS

Open **HACS → Lanbon MQTT**, select the offered update or use
**⋮ → Redownload**, and choose the intended published version. For release
candidates, select the `rc` version explicitly; **Show beta versions** may be
needed where HACS offers that setting. Restart Home Assistant after the files
have been replaced.

Do not delete/re-add the Lanbon configuration entry to perform a normal update.
Existing physical peer/channel/function unique IDs are intended to remain
stable. Review any release-specific migration notes before changing versions.

After restart, check that native MQTT and Lanbon MQTT are loaded, Control
status is sensible, reports are arriving, and your existing entities and
automations still refer to the intended controls. Test a safe output and
restore its previous state. Do not infer physical success from a command
return alone.

## Roll back an update

If Home Assistant still runs and the release notes allow the downgrade:

1. In **HACS → Lanbon MQTT → ⋮ → Redownload**, choose the last known working
   version and restart Home Assistant.
2. Check the version, MQTT connection, options, entity IDs and reports again.
3. Repeat a safe physical control test and restore the original state.

Replacing code alone does not reverse a configuration/storage migration. If the
release changed storage incompatibly, follow that release's migration rollback
instructions or restore your pre-update backup. The first public `rc.4`
packages the accepted `rc.3` runtime and retains configuration schema version 2;
its public metadata and documentation do not require a runtime migration.

If Core cannot start, use your installation's supported recovery/file-access
method to restore the exact previous `custom_components/lanbon_mqtt` directory,
then restart. Restore the pre-update Home Assistant backup if code rollback is
insufficient. A full restore can overwrite unrelated changes made since the
backup, so account for them first. Do not edit Home Assistant's internal
`.storage` files while Core is running.

Save sanitized logs and diagnostics for an issue after recovering service.
Avoid repeated production source edits while trying different fixes; use a
tagged package with a clear path back.

## Temporarily stop Lanbon MQTT

Disable the Lanbon MQTT configuration entry from its integration menu and
reload/restart as requested. This stops the custom integration's subscriptions,
cache requests, optional metadata reads and controls. Existing native MQTT
entities belong to their own configuration and can remain in use.

## Uninstall

1. Review dashboards, automations and helpers that use **Lanbon-owned** entities.
   Repoint or remove those references intentionally.
2. Remove the **Lanbon MQTT** configuration entry under **Settings → Devices &
   services**. This removes its configured devices/entities through HA's normal
   lifecycle; do not use deletion as an update technique.
3. In HACS, remove the **Lanbon MQTT** repository/download. For a manual install,
   remove only `config/custom_components/lanbon_mqtt` using your supported file
   access method.
4. Restart Home Assistant.

Uninstalling Lanbon MQTT does not rewrite device firmware, reset the devices,
remove broker retained messages, or remove independently configured native
MQTT controls. Remove those separately only if that is your intention. Keep
your backup until you have confirmed the result.
