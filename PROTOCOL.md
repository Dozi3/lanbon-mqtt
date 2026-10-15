# Lanbon MQTT protocol contract

[Home](README.md) · [Diagnostics](DIAGNOSTICS.md) · [Verification](TESTING.md)

The runtime supports an explicitly accepted legacy Smart Living MQTT profile.
Type knowledge, packet structure, current report receipt and physical output are
separate facts. Unknown semantics remain raw. Runtime discovery and control use
MQTT; HTTP, firmware replacement, other vendor families and UDP transport are
not implied by the offline research helpers.

`P` is the configured native prefix, `G` a mesh/BSSID group and `D` a physical
peer MAC. Group identity is not a physical root identity. Offsets are zero-based.

## Topics and discovery

| Topic | Role |
|---|---|
| `P/G/light/D/state` | Native ON/OFF feedback |
| `P/G/light/D/brightnessState` | Native reported percentage |
| `P/G/switch/D-NN/state` | Independent relay channel feedback |
| `P/G/light/D/set` | Qualified dimmer or observed single-relay power command |
| `P/G/light/D/brightnessSet` | Qualified dimmer percentage command |
| `P/G/switch/D-NN/set` | Observed relay channel command |
| `lanbon/a/MAC:G/s2m/cmdStatusSynchronous` | Binary reported output |
| `lanbon/a/MAC:G/s2m/cmdStatusGet` | Root-cached inventory response |
| `lanbon/a/L8devElecsumReport` | Global mesh-group/peer-list structure |
| `lanbon/a/L8devCompleteDetailReport` | Global firmware metadata, without G |

Automatic setup listens for 35 seconds to the global energy topic, native
light/switch configuration and state hints, native brightness, and a-route
sync/inventory replies. It retains at most 16 groups and 16 peer hints per group,
with an 8192-byte packet bound. Current global energy evidence ranks above
binary group evidence, native hints and retained hints. Discovery suggests
groups for user selection; it sends no query and grants no actuator permission.

Optional native MQTT discovery accepts a constrained config topic with Lanbon
manufacturer or unique-ID evidence, then asks for confirmation before scanning.
Quiet discovery can finish through manual group entry. Global detail reports
alone cannot assign a device to a group.

Runtime subscriptions cover selected `P/G/#`, a/b binary group scopes, and global
energy/detail reports. A b-route message is not silently treated as an a-route
reply. Commands are classified independently and cannot become feedback.

## Packet layouts

### Status synchronization

The 11-byte source layout is output1 + auxiliary4 + peer MAC6. Captured newer
12-byte frames add a final candidate type marker. Preserve the first byte,
auxiliaries and marker exactly. Default parsing does not normalize sync power
or brightness. A known inventory type and explicit selected sync profile are
required before interpreting a dimmer's low seven bits. The high bit remains
unqualified metadata. Relay action-mask variants require native per-channel
feedback; an arbitrary sync byte cannot overwrite every gang.

### All-status inventory

One declared-total byte precedes 20-byte records:

| Record offset | Bytes | Field |
|---:|---:|---|
| 0 | 6 | Peer MAC |
| 6 | 1 | Type |
| 7 | 1 | Raw output |
| 8 | 2 | Raw little-endian flags |
| 10 | 3 | Temperature-like triplet |
| 13 | 3 | Power-like triplet |
| 16 | 4 | Model-dependent auxiliary bytes |

The selected `stock_44` producer profile allows at most 44 records per chunk.
The declared total repeats across chunks. A partial chunk is not a complete
inventory and missing peers are not deletion evidence. Misalignment, impossible
counts and duplicate peers are rejected. Specified-status records rearrange the
same fields as type/output/flags/triplets/auxiliary/MAC; their shape does not make
the associated query safe.

Events carry `observation_source: root_cache_response`, with no peer age, health
claim or nonce. Supplied root context may identify a root record; array position
and G cannot do so. Historical child serialization omits flags, so child zeros do
not prove timers, locks or other features are disabled. Raw flags are retained.

For inventory type `0x4D`, only the full byte 0–100 is a percentage candidate.
Bytes such as 128 or 228 are not masked into plausible inventory brightness.
Inventory `0x4A/0x4B/0x4C` low bits describe one/two/three relay gangs. High
change/action bits remain diagnostic until the installed event profile is known.

### Global energy and detail metadata

Energy structure: count1 + timezone2 + group6, then count records of MAC6 +
measurement3. Bytes 3–8 are BSSID/mesh group, **not** the forwarding root MAC.
Timezone bytes are raw fields, not fixed `01 00` magic. The selected source
profile allows up to 98 peer records per packet. Shape, group and duplicate
validation precede use. This report can propose G and peers without identifying
a physical root, type, command route, metering capability or fresh RF state.

Detail structure: `01 08 count`, thirteen zero bytes, then count records of MAC6
plus BE16 firmware integer. Version arithmetic is major × 10000 + minor × 100 +
patch. This global packet has no G and only supplements independently associated
peers. Device/firmware metadata alone does not authorize controls.

Numeric triplets use BE16 plus hundredths. The selected APK temperature
candidate is `(BE16 - 32767) + fraction/100`; fractions above 99 yield no value.
An older C encoder complements negative hundredths, so negative-profile choice
remains explicit and unqualified. Units, calibration and model eligibility are
not established by arithmetic. No default temperature, wattage or energy entity
is created. Auxiliary bytes, including constant extension patterns, stay raw.

## Queries, control construction and state policy

The automatic cache query is nonretained opcode `0x11` with eight-byte
body `11 00 00 00 00 00 00 00`, on `lanbon/a/MAC:G/m2s/cmdQuery`. It is bounded
by HA's processed-subscription barrier, connection generation, one outstanding
request per group, rate limits and a 10-second response window. Source-profile read-only behavior
does not make its cached peer rows fresh or provide a nonce. Opcode `0x12` may
beep or display a notice and is not a discovery/read fallback.
The processed-subscription event is not proof of broker ACL acceptance or peer
health; a missing response remains an explicit timeout.

Actuator commands use only qualified native routes, QoS 0 and retain false.
Publication is intent, not state confirmation. Direct reports arriving after a
query starts win. Root-cache state stays assumed and has a separate lease from
direct reports. Retained/unknown-retain values never become live state. Unknown
state after lease expiry is distinct from a broker or ownership failure.

Pure API:

```python
parse_message(topic, payload, *, root_mac=None, qualified_sync=False,
              inventory_profile="stock_44") -> dict | None
interpret_output(device_type, status_raw, *, context="inventory", qualified=False) -> dict
control_profile(device_type) -> dict | None
native_control_message(group_id, device_id, device_type, action, value=None,
                       channel=None, discovery_prefix="homeassistant", *,
                       observed_native_base=None, component=None) -> (topic, bytes)
```

`native_control_message` validates scope, physical peer, known type, channel and
value. Runtime relay routes require native feedback or, in the explicit
additional-controls mode, exact active Home Assistant MQTT subscription
metadata independently qualified by current inventory, type and membership.
Metadata-derived routes require enabled functional native entities, positive
subscription counts, exact nonwildcard state topics and consistent command
aliases when present. They are transient, requalified at startup and rechecked
before commands; withdrawal or ambiguity rejects the command. Real native
feedback takes precedence and metadata never becomes output state.
Single-relay suffixless light routes and numbered switch routes are preserved.
Dimmer brightness accepts integer 0–100.
The helper never publishes. Its caller owns membership/profile confirmation,
transport, connection generations and feedback acceptance.

## Parameter and network diagnostics

The manual device parameter button constructs exactly four eight-byte reads:
`A0/A1/A2/A3 + physical MAC (6 bytes) + 00`, QoS 0, retain false. Normal timer
replies contain eight three-byte slots plus the peer MAC; optional IR extension
bytes remain raw. Delay is a remaining-at-receipt value; green mode is a
configured inactivity period. Night mode preserves both mode flags, packed
hours and full minute bytes. Invalid or unresolved high bits are retained and
never silently normalized into a valid time. Timer reports do not change output
state and do not assert that a timer will fire.

The optional HTTP transport uses fixed numeric private IPv4 targets, port 80,
no redirects, bounded JSON and explicit self/group/header membership checks.
Root and child framework metadata keep separate identity provenance. All child
requests still go through the verified root. HTTP never qualifies a Lanbon type,
native command route or physical state. Mlink application version is separate
from native Lanbon firmware. TSF remains an opaque source clock; it is not UTC
or proven uptime. RSSI SDK fallback values are unavailable rather than real
signal readings. No UDP listener, cloud API or generic HTTP command is enabled.

The offline translator accepts JSON Lines containing `topic` and `payload_hex`,
with optional boolean `retained` and receipt metadata. It selects no installed
sync profile; extra input flags cannot authorize normalization. A supported
layout is not a qualified model or a physical-state assertion.
