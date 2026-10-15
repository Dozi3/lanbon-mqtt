# Diagnostic entities and reported parameters

[Home](README.md) · [Use](docs/usage.md) · [Protocol](PROTOCOL.md)
· [Troubleshooting](docs/troubleshooting.md)

The integration exposes accepted device reports with their source and limits.
It does not turn unknown bytes into plausible physical measurements. Fields
appear only when their report family and identity have been qualified; a
catalogue entry does not mean every Lanbon model implements it.

## Entities and defaults

**Control status**, **Device details**, reported firmware and type information
are enabled by default. Most separate raw or frequently changing diagnostics
are registered disabled by default to limit recorder load. Enable a specific
sensor from its entity settings when you need it in a dashboard or automation.
The latest accepted detail remains available in Device details without
enabling every individual sensor.

Changed diagnostic snapshots are coalesced rather than written at packet
rate. The integration keeps bounded current observations, not an unbounded
packet history. Device/entity identities follow physical peer MAC and
function/channel, so a forwarding root or route change does not rename them.

| Source/family | HA exposure | Meaning and limits |
|---|---|---|
| Control policy | **Control status** with explanation, existing entities and qualified relay channels | Explains disabled/suppressed/waiting/unsupported/conflicting controls; `ready` is not physical delivery proof |
| Native output reports | Reported power, reported brightness, native output code and eligible controls | Device/root-reported output; remembered brightness is independent from power |
| Receipt/provenance | Last live report, source/confidence/current-generation and freshness attributes | Broker receipt, retained replay, root cache and physical device health remain distinct |
| Inventory | Type/model, raw output, flags, measurement triplets, auxiliary bytes and complete record | Root-cache snapshot; per-peer radio age is unknown |
| Firmware detail | Reported firmware, DeviceInfo version and raw firmware integer | Native firmware; distinct from the HTTP framework application version |
| Synchronization | Raw output, four auxiliary bytes and marker | Percentage interpretation requires a qualified dimmer profile; unknown bits remain raw |
| Energy report | Raw triplet and unitless decoder candidate; group metadata | Units, calibration and model capability are unverified; historical counter behavior resets hourly |
| Normal timers | Eight reported schedule sensors with full slot attributes | Enable, weekday mask, hour, full minute, raw action and extension bits; no execution proof |
| Delay timer | Remaining seconds at receipt and raw action | Reported counter; no locally simulated countdown |
| Green mode | Configured inactivity period | A setting, not remaining time or energy consumption |
| Night mode | Mode flags, configured start/end and raw schedule | Device clock/timezone correctness and physical effect remain unproven |
| Optional root HTTP metadata | Root address, self/parent/group identity, layer, parent RSSI and framework/build details | Requires an explicit verified root binding; bounded five-minute cadence |
| Optional child HTTP metadata | Parent identity, mesh layer, parent-link RSSI and framework/build details | Forwarded through a verified root; gateway address is not the child's own LAN address |
| Generic HTTP characteristics | Complete namespaced descriptor/value attributes | Generic `on` characteristic CID 0 is not Lanbon output and never qualifies controls/state |
| Transport/group | Capture status, group details, family counters and connection generation | `listening` describes broker capture, not each peer's health |

No qualified temperature, wattage or energy entity is created from unverified
measurement triplets. Unitless decoder candidates are deliberately labelled.
SDK RSSI fallback values are unavailable rather than treated as real signal
readings. TSF remains an opaque source clock, not UTC or proven uptime.

## Read device parameters

The **Read device parameters** diagnostic button sends four fixed read-only
parameter requests for supported, currently qualified devices. It requests
normal timer, delay, green mode and night mode settings. It does not modify or
activate them. Unresolved high bits and extensions remain raw rather than
being silently normalized into a valid time or action.

## Optional network settings

Enter optional root bindings through the native setup/Configure forms: select
a configured mesh, then its known private IPv4 address and physical root MAC.
No JSON is required. Only unique numeric RFC1918 IPv4 targets on fixed HTTP
port 80 are supported. Reads verify identity; they do not qualify actuator
type, route or physical state. Child metadata is off unless explicitly enabled.
See [setup](SETUP.md#6-optional-network-metadata).

## Download diagnostics for an issue

Open the **Lanbon MQTT** integration's menu under **Settings → Devices &
services** and select its diagnostics download action. The download redacts
household/device identities, addresses, names and raw identity-bearing fields.
Local HA device attributes can still contain your actual metadata.

Review the file and screenshots before sharing. Never attach broker passwords,
tokens, private keys, backups, `.storage` files or an unfiltered broker capture.
Keep a reproduction minimal and replace identities consistently with synthetic
ones. See [Support](SUPPORT.md) and the [security policy](SECURITY.md).

Unsupported models, historical curtain/IR/heater fields, uncaptured families
and encrypted mesh internals remain research gaps. UDP notices are partly
source-understood, but the integration has no UDP listener.
