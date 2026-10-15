"""Constants for the Lanbon MQTT integration."""

from homeassistant.const import Platform

DOMAIN = "lanbon_mqtt"
NAME = "Lanbon MQTT"
PLATFORMS = [Platform.SENSOR, Platform.LIGHT, Platform.SWITCH, Platform.BUTTON]
CONF_GROUP_ID = "group_id"
CONF_GROUP_IDS = "groups"
CONF_DISCOVERY_PREFIX = "discovery_prefix"
CONF_MAX_REPORT_AGE = "max_report_age"
CONF_QUERY_INTERVAL = "query_interval"
CONF_QUERY_ON_START = "query_on_start"
CONF_ENABLE_CONTROLS = "enable_controls"
CONF_ALLOW_EXISTING_CONTROLS = "allow_existing_controls"
CONF_PROTOCOL_PROFILE = "protocol_profile"
CONF_ROOT_BINDINGS = "root_bindings"
CONF_METADATA_CHILDREN = "metadata_children"
DEFAULT_DISCOVERY_PREFIX = "homeassistant"
DEFAULT_MAX_REPORT_AGE = 300
DEFAULT_QUERY_INTERVAL = 300
DEFAULT_QUERY_ON_START = True
DEFAULT_ENABLE_CONTROLS = True
DEFAULT_ALLOW_EXISTING_CONTROLS = False
DEFAULT_PROTOCOL_PROFILE = "legacy_smartliving_mqtt_v1"
BROKER_UNIQUE_ID = "ha_mqtt"
DISCOVERY_SECONDS = 35
QUERY_TIMEOUT = 10
DIAGNOSTIC_INTERVAL = 60
MAX_DEVICES = 256
MAX_PACKET_BYTES = 8192
