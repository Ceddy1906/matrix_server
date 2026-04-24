"""Constants for Matrix Messenger integration."""
DOMAIN = "matrix_messenger"

CONF_HOMESERVER = "homeserver"
CONF_AUTH_METHOD = "auth_method"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_ACCESS_TOKEN = "access_token"
CONF_DEVICE_ID = "device_id"
CONF_DEVICE_NAME = "device_name"
CONF_ROOMS = "rooms"
CONF_ENABLE_SYNC = "enable_sync"

AUTH_METHOD_PASSWORD = "password"
AUTH_METHOD_TOKEN = "token"

DEFAULT_DEVICE_NAME = "Home Assistant Matrix Messenger"
DEFAULT_SYNC_INTERVAL = 5
DEFAULT_QUESTION_TIMEOUT = 1800

EVENT_MATRIX_RESPONSE = "matrix_messenger_response"
