"""Constants for PAYBACK Deutschland integration."""

DOMAIN = "payback"

CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_SESSION_COOKIE = "session_cookie"
CONF_AUTO_ACTIVATE_COUPONS = "auto_activate_coupons"
CONF_UPDATE_INTERVAL = "update_interval"
# The API host requires HTTP Basic auth with credentials taken from the official
# app, plus a "principal" identifying the client application.
CONF_BASIC_AUTH_USERNAME = "basic_auth_username"
CONF_BASIC_AUTH_CREDENTIAL = "basic_auth_credential"
CONF_PRINCIPAL = "principal"
# An already-authenticated token; when set, no login request is made at all.
CONF_ACCESS_TOKEN = "access_token"

DEFAULT_UPDATE_INTERVAL = 12  # Hours
MIN_UPDATE_INTERVAL = 2  # Minimum allowed update interval in hours
