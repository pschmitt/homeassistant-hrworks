"""HR WORKS configuration and defaults."""

from homeassistant.const import Platform

DOMAIN = "hrworks"
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.CALENDAR, Platform.BUTTON]
CONF_COMPANY = "company_id"
CONF_SSH_HOST = "ssh_host"
CONF_SSH_PORT = "ssh_port"
CONF_SSH_USERNAME = "ssh_username"
CONF_SSH_KEY_PATH = "ssh_key_path"
CONF_SSH_KNOWN_HOSTS = "ssh_known_hosts"
CONF_WORKER_COMMAND = "worker_command"
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
