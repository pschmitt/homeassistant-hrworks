"""HR WORKS configuration and defaults."""

from homeassistant.const import Platform

DOMAIN = "hrworks"
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.CALENDAR, Platform.BUTTON]
CONF_COMPANY = "company_id"
CONF_BD_URL = "browser_daemon_url"
CONF_BD_SECRET = "browser_daemon_secret"
CONF_BD_PROFILE = "browser_daemon_profile"
CONF_BD_NODE_GROUP = "browser_daemon_node_group"
CONF_RBW_ENTRY_ID = "rbw_entry_id"
CONF_RBW_ITEM_ID = "rbw_item_id"
# Keys of the removed SSH worker backend, dropped from old entries on migration.
LEGACY_KEYS = (
    "backend",
    "ssh_host",
    "ssh_port",
    "ssh_username",
    "ssh_key_path",
    "ssh_known_hosts",
    "worker_command",
)
CONF_PROFILE = "profile_id"
CONF_CODE = "code"
CONF_TOTP_URI = "totp_uri"
CONF_PAST_DAYS = "calendar_past_days"
CONF_FUTURE_DAYS = "calendar_future_days"
CONF_SICKNESS = "include_sickness"
CONF_PENDING = "include_pending"
CONF_WRITES = "enable_time_entry"
DEFAULT_SCAN_INTERVAL = 900
DEFAULT_PAST_DAYS = 365
DEFAULT_FUTURE_DAYS = 365
