# Troubleshooting and FAQ

[Home](../README.md) · [Setup](../SETUP.md) · [Support](../SUPPORT.md)

## Control status

Start with the physical device's **Control status** sensor. Its attributes
include an explanation, existing control entities and qualified relay channels.

| Status | Meaning | Next step |
|---|---|---|
| `ready` | Control prerequisites are met | Test a safe output and compare fresh feedback with physical behavior |
| `disabled` | Main control option is off | Enable **Enable supported light and switch controls** in Configure |
| `existing_controls_preserved` | Another integration already supplies a control | Use that entity, or explicitly enable **Add Lanbon controls alongside existing controls** |
| `mqtt_disconnected` | Native HA MQTT is disconnected | Restore the connection in the MQTT integration |
| `waiting_for_inventory` | A current supported type has not been qualified | Check selected mesh/profile and root-cache responses |
| `unsupported_type` | The inventoried type has no supported actuator profile | Keep diagnostics and report the model/type with sanitized evidence |
| `waiting_for_route` | No validated relay route is available | Enable its existing native MQTT entity or wait for native per-channel feedback |
| `partial_routes` | Only some relay channels have routes | Check feedback/configuration for each missing gang |
| `membership_conflict` | Device ownership is ambiguous between meshes | Resolve the conflicting mesh selection/evidence before control |

`ready` is not an assertion of physical reachability. Inspect the explanation
for ambiguous native routes; the integration refuses to choose between them.

## MQTT sees the device, but Lanbon has no toggle

Open the **Lanbon MQTT** device page, rather than the separate native **MQTT**
page. If an old control already exists, the default policy preserves it and
suppresses the duplicate. Enable both control options in **Lanbon MQTT →
Configure** to add separate Lanbon dimmers/switches. Existing entities are not
moved between device pages.

Type discovery, mesh membership and per-channel relay routes must still pass.
A retained discovery message or a generic router administration switch does
not qualify an actuator. Use Control status rather than deleting working
entities to force rediscovery.

## Automatic discovery finds nothing

The 35-second scan is passive. A quiet scan is not proof that no devices exist.
Check that devices publish to the broker configured in Home Assistant, that
broker permissions allow their reports, and that the native prefix matches.
Then enter the known mesh topic IDs manually. Do not use the root's physical
MAC unless it is also genuinely the mesh topic ID.

Home Assistant's MQTT **Listen to a topic** tool can inspect your own report
topics. Prefer a narrow scope. Do not publish guessed commands or disclose a
whole broker capture when asking for help. See the
[native MQTT testing documentation](https://www.home-assistant.io/integrations/mqtt/#testing-your-setup).

## Commands return, but the light does not change

MQTT publication is intent, not a delivery acknowledgement. Compare the
entity's report source/freshness with the physical output. Check broker ACLs,
root connection, selected mesh, actual device profile and qualified route.
Stop repeated commands if the load does not respond as expected. Do not turn
on retained actuator commands as a workaround; this integration publishes
actuator intents with QoS 0 and retain false.

## State is unknown, unavailable or different after restart

MQTT disconnection, stale state and missing route qualification are different
conditions. A device can have a usable control path while a particular field
has no fresh value. Retained replay does not become current feedback. After
startup, allow an accepted root-cache response or native report to arrive.

Root-cache state remains assumed because its peer age is unknown. Increasing
the freshness window merely keeps a value longer; it does not prove the lamp's
current output. See [reported state](usage.md#understand-reported-state).

## Firmware, timer or raw sensors are missing

Some fields exist only after the corresponding report has been accepted. Many
raw sensors default disabled. Check **Device details**, include disabled
entities in the entity list and enable only the sensors you need. A supported
device's **Read device parameters** button requests timer settings; it does
not configure them. Not every model implements every diagnostic family.

## Optional network metadata fails

Verify the selected mesh, the actual physical root MAC and the root's current
numeric private IPv4 address. MQTT mesh ID and physical root MAC may differ.
Only the qualified HTTP port 80 API is supported; redirects, hostnames, HTTPS
and alternate ports are rejected. A failed identity check must be resolved,
not bypassed. MQTT controls can continue without this optional metadata.

## Does this remove cloud access or require new firmware?

No firmware replacement or cloud-account operation is performed. The devices
must already support and use the qualified native MQTT profile. Their own
cloud behavior and device-side broker provisioning remain outside this project.

## Can I merge the MQTT and Lanbon devices?

The integration preserves existing device and entity identities. It does not
merge records, migrate history or rewrite automations. You can choose the
entities to show on a dashboard without changing registry ownership.

## I still need help

Follow [Support](../SUPPORT.md) and include a sanitized diagnostics download,
versions, type code, Control status and expected/observed behavior. Report
security issues privately using [SECURITY.md](../SECURITY.md).
