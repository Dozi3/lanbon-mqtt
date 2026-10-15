# Compatibility and verification limits

[Home](../README.md) · [Verification](../TESTING.md) · [Protocol](../PROTOCOL.md)

## Required device profile

The supported profile is **legacy Lanbon Smart Living MQTT**. Devices must
already publish their native Lanbon binary reports and native light/switch
topics to a broker reachable by Home Assistant. The integration cannot turn
Tuya firmware or a cloud-only product into this profile. Product branding and
appearance do not establish compatibility.

The parser recognizes more diagnostic layouts than it permits actuators.
Only current, qualified inventory types below can create controls.

| Type | Family | Supported control | Physical acceptance |
|---|---|---|---|
| `0x4A` | Single relay | One independently routed switch | Software and synthetic MQTT checks; installation-specific physical test required |
| `0x4B` | Double relay | Two independently routed switches | Software and synthetic MQTT checks; installation-specific physical test required |
| `0x4C` | Triple relay | Three independently routed switches | Protocol/software coverage; installation-specific physical test required |
| `0x4D` | Dimmer | Native light with percentage brightness | One physical dimmer confirmed through the integration, including 25% → 60% → 100% → OFF |
| Other/unknown | Other models | Diagnostics where understood | No supported actuator profile |

Relays require a validated exact native route for each channel. An observed
feedback route qualifies it. When additional controls are explicitly enabled,
active native Home Assistant MQTT subscription metadata can qualify a route
after independent current inventory validates the type and membership. Metadata
does not supply output state and its route qualification is rechecked.

The physical dimmer result is evidence for that tested installation and
firmware, not a claim that every Lanbon dimmer, load or mesh has been commissioned.

## Home Assistant and broker

| Surface | Requirement or evidence |
|---|---|
| Minimum Home Assistant | 2026.9.3, as declared in `hacs.json` |
| Locked software test runtime | Python 3.14 / Home Assistant 2026.10.1 |
| Native runtime acceptance | Isolated HAOS/Core 2026.10.1 with real MQTT transport |
| Installation methods | HACS custom repository or component directory copied into Home Assistant's configuration |
| Broker | Native Home Assistant MQTT connection; device/root and HA must reach the same relevant topic space |
| Broker TLS/credentials | Managed by Home Assistant MQTT; device-side capabilities remain a separate requirement |
| Optional metadata transport | Explicitly bound private IPv4 root, fixed HTTP port 80 |

Native MQTT transport does not require Home Assistant OS, a Windows host,
Node-RED, the Mosquitto app or the author's other integrations. The isolated
HAOS runtime check is not a full installation matrix for every container,
broker implementation or future Home Assistant version.

## Evidence carried into the public releases

The preceding control-runtime candidate passed 524 software tests, 13 separate
independent runtime checks, 11 native HA/MQTT acceptance checks and a separate
cold Core restart check. Startup produced no actuator commands in that isolated
test. Physical feedback and software results are recorded separately.

Public `0.3.0-rc.4` packages that control runtime with public metadata,
documentation, branding and repository automation. It does not introduce a new
device protocol. See [TESTING.md](../TESTING.md) for the commands and the
distinction between inherited runtime evidence and fresh public packaging/CI.

Stable `0.3.0` promotes that same integration runtime. Its version label does
not extend the hardware or firmware compatibility established by those tests.

## Known boundaries

- Root-cache peer age and mesh radio reachability are unknown.
- Raw measurement triplets have unverified units and calibration; they are not
  exposed as qualified temperature, wattage or energy measurements.
- Timer reports do not prove execution or the device clock's correctness.
- Generic HTTP framework characteristics do not qualify actuator state.
- No UDP listener, cloud API, firmware updater or Wi-Fi provisioning is included.
- Encrypted internal mesh traffic is not decrypted by this integration.
- New model support requires evidence and tests; changing a type byte or topic
  pattern by guesswork is not a compatibility fix.
