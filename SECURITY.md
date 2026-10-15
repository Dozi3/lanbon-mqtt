# Security policy

## Supported versions

Security fixes target the latest published release or release candidate.
Earlier local candidates and arbitrary modified builds are not maintained
release branches. This volunteer project does not promise a response deadline.

## Report a vulnerability privately

Use GitHub's **Security → Advisories → Report a vulnerability** for
[Dozi3/lanbon-mqtt](https://github.com/Dozi3/lanbon-mqtt/security/advisories/new).
Do not post a working exploit, credentials or private network details in a
public issue. If private reporting is unavailable, open an issue requesting a
private contact route without disclosing the vulnerability.

Include affected versions, the trust boundary involved, impact, prerequisites,
a minimal synthetic reproduction and any suggested mitigation. Use dummy
broker credentials and identities. Do not attach household backups, tokens,
private keys, full broker dumps or proprietary firmware.

## Trust boundaries

- The integration reuses Home Assistant MQTT. Configure broker authentication,
  permissions and certificate verification in that native integration.
- A broker publisher permitted to impersonate Lanbon topics is within the
  telemetry trust boundary. MQTT receipt is not physical attestation.
- Optional root metadata uses explicitly configured private IPv4 targets on
  fixed HTTP port 80, with identity checks and bounded responses. This optional
  device API does not provide encrypted transport.
- Actuator commands are qualified separately from observations and are sent
  with QoS 0 and retain false. Do not weaken these checks as a troubleshooting
  shortcut.
- Downloaded diagnostics redact identities and address-bearing fields, but
  review every attachment before sharing. Local entity attributes can contain
  actual device/network metadata.

For ordinary setup problems and compatibility questions, use
[Support](SUPPORT.md).
