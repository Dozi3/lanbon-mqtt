# Installation

The complete illustrated instructions are in **[Install and configure Lanbon
MQTT](../SETUP.md)**.

Start with a current backup, connect Home Assistant's native MQTT integration,
then add `https://github.com/Dozi3/lanbon-mqtt` in **HACS → ⋮ → Custom
repositories** with type **Integration**. Download a tagged release, restart
Home Assistant, and add **Lanbon MQTT** under **Settings → Devices & services**.

The setup guide covers every native field, mesh discovery/manual entry,
controls alongside existing entities, and optional root metadata. It requires
no YAML or JSON configuration. Devices must already use the supported legacy
Smart Living MQTT profile and publish to your reachable broker.

Also see [compatibility](compatibility.md), [troubleshooting](troubleshooting.md)
and [updates/rollback](updates.md).
