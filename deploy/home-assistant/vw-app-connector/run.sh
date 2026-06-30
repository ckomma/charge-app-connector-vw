#!/usr/bin/with-contenv sh
set -eu

options=/data/options.json

opt() {
    jq -r --arg key "$1" --arg default "$2" '.[$key] // $default' "$options"
}

export ADB_SERIAL="$(opt adb_serial "")"
export ADB_MODE="$(opt adb_mode "auto")"
export ADB_WIFI_ADDRESS="$(opt adb_wifi_address "")"
export LISTEN_ADDRESS="$(opt listen_address "0.0.0.0")"
export PORT="$(opt port "9920")"
export CHARGING_INTERVAL_SECONDS="$(opt charging_interval_seconds "300")"
export IDLE_INTERVAL_SECONDS="$(opt idle_interval_seconds "900")"
export DETAIL_INTERVAL_SECONDS="$(opt detail_interval_seconds "43200")"
export LOCATION_INTERVAL_SECONDS="$(opt location_interval_seconds "14400")"
export BACKGROUND_MIN_INTERVAL_SECONDS="$(opt background_min_interval_seconds "300")"
export BACKGROUND_ERROR_RETRY_SECONDS="$(opt background_error_retry_seconds "900")"
export BACKGROUND_DAILY_LIMIT="$(opt background_daily_limit "180")"
export ACTION_MIN_INTERVAL_SECONDS="$(opt action_min_interval_seconds "60")"
export ACTION_DAILY_LIMIT="$(opt action_daily_limit "20")"
export RATE_LIMIT_COOLDOWN_SECONDS="$(opt rate_limit_cooldown_seconds "43200")"
export USAGE_STATE_FILE=/data/usage.json
export CACHE_STATE_DIR=/data/cache
export DIAGNOSTICS_DIR=/data/diagnostics
export APP_PACKAGE="$(opt app_package "com.volkswagen.weconnect")"
export MAPS_PACKAGE="$(opt maps_package "com.google.android.apps.maps")"
export VERIFIED_APP_VERSION="$(opt verified_app_version "3.63.2")"
export APP_START_WAIT_SECONDS="$(opt app_start_wait_seconds "8")"
export DETAIL_WAIT_SECONDS="$(opt detail_wait_seconds "3")"
export UI_UPDATE_TIMEOUT_SECONDS="$(opt ui_update_timeout_seconds "8")"
export SLEEP_AFTER_OPERATION="$(opt sleep_after_operation "true")"
export API_KEY="$(opt api_key "")"
export VW_SPIN="$(opt vw_spin "")"

export MQTT_HOST="$(opt mqtt_host "")"
export MQTT_PORT="$(opt mqtt_port "1883")"
export MQTT_USERNAME="$(opt mqtt_username "")"
export MQTT_PASSWORD="$(opt mqtt_password "")"
export MQTT_TOPIC_PREFIX="$(opt mqtt_topic_prefix "vw_app_connector")"
export MQTT_DISCOVERY_PREFIX="$(opt mqtt_discovery_prefix "homeassistant")"
export MQTT_CLIENT_ID="$(opt mqtt_client_id "vw-app-connector")"
export MQTT_TLS="$(opt mqtt_tls "false")"

mkdir -p /data/cache /data/diagnostics
if [ -d /addon_configs/local_vw_app_connector/.android ]; then
    export HOME=/addon_configs/local_vw_app_connector
else
    export HOME=/data
fi
mkdir -p "$HOME/.android"
chmod 700 "$HOME/.android"

adb start-server || true
exec python3 /app/vw_app_connector.py
