# Umbrel app exports — runs before docker-compose to populate env vars.
# Currently we don't need any custom exports; the app reads its own config
# from ${APP_DATA_DIR}/config/config.yaml and secrets from ${APP_DATA_DIR}/.env.
#
# Umbrel automatically provides:
#   APP_DATA_DIR    — host path for persistent data
#   APP_DOMAIN      — *.local Tor / mDNS domain
#   APP_HIDDEN_SERVICE_BITCOINERS_DCA — Tor onion address (if Tor exposure enabled)
