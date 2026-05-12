#!/usr/bin/env bash
# Provision a new hosted tenant.
#
# Usage:
#   hosted/provision.sh <tenant_id> <customer_email> <tier>
#
# Where:
#   tenant_id      — short, alphanumeric, used in container + URL paths (ben-prod, alice-pro)
#   customer_email — customer-id baked into the license token
#   tier           — pro | business
#
# Side effects:
#   - Creates tenants/<tenant_id>/{config/,data/,reports/,.env}
#   - Writes a tier-appropriate config.yaml
#   - Issues a license token via scripts/generate_license.py
#   - Picks an unused localhost dashboard port
#   - Renders docker-compose.yml + nginx fragment from templates
#   - Prints next-step instructions
#
# Required env vars:
#   PROVISION_PRIVATE_KEY  — path to the license-signing private key PEM
#   PROVISION_IMAGE_TAG    — Docker image, e.g. ghcr.io/jiashanlu/bitcoiners-dca:0.5.0
#   PROVISION_BASE_DIR     — where tenants/ lives, e.g. /opt/bitcoiners-dca
#   PROVISION_NGINX_DIR    — where to drop nginx fragments, e.g. /etc/nginx/conf.d
#
# The script is idempotent for re-runs on the same tenant_id IF the data/
# directory already exists — it'll re-render templates without overwriting
# user data.

set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 <tenant_id> <customer_email> <tier>" >&2
  exit 1
fi

tenant_id="$1"
customer_email="$2"
tier="$3"

if ! [[ "$tenant_id" =~ ^[a-z0-9-]+$ ]]; then
  echo "tenant_id must be lowercase alphanumeric + dashes only" >&2
  exit 1
fi
case "$tier" in
  pro|business) ;;
  *) echo "tier must be 'pro' or 'business'" >&2; exit 1 ;;
esac

: "${PROVISION_PRIVATE_KEY:?need PROVISION_PRIVATE_KEY}"
: "${PROVISION_IMAGE_TAG:?need PROVISION_IMAGE_TAG}"
: "${PROVISION_BASE_DIR:?need PROVISION_BASE_DIR}"
: "${PROVISION_NGINX_DIR:?need PROVISION_NGINX_DIR}"

tenant_dir="${PROVISION_BASE_DIR}/tenants/${tenant_id}"
echo "==> Provisioning tenant '${tenant_id}' for ${customer_email} (tier=${tier})"
echo "    Base dir: ${tenant_dir}"

mkdir -p "${tenant_dir}"/{config,data,reports}
chmod 700 "${tenant_dir}"

# Pick an unused localhost port in 8100-8999. Skip anything already in use.
dash_port=""
for candidate in $(seq 8100 8999); do
  if ! ss -ltn "( sport = :${candidate} )" 2>/dev/null | grep -q LISTEN; then
    dash_port="${candidate}"
    break
  fi
done
if [[ -z "${dash_port}" ]]; then
  echo "no free port in 8100-8999" >&2
  exit 1
fi
echo "    Dashboard port: ${dash_port}"

# Issue a 1-year license token. Use GNU `date -d` (Linux) with a BSD `date -v`
# fallback for macOS dev environments.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
expires_iso="$(date -u -d '+1 year' +%Y-%m-%d 2>/dev/null || date -u -v+1y +%Y-%m-%d)"
token="$(
  python3 "${script_dir}/scripts/generate_license.py" issue \
    --private-key "${PROVISION_PRIVATE_KEY}" \
    --customer-id "${customer_email}" \
    --tier "${tier}" \
    --expires "${expires_iso}" \
    --notes "provisioned by hosted/provision.sh" 2>/dev/null \
    | tail -1 | tr -d ' '
)"
echo "    License: tier=${tier}, 1-year expiry"

# Skeleton config.yaml — customer edits exchanges + strategy.amount_aed
cat > "${tenant_dir}/config/config.yaml" <<YAML
# Tenant: ${tenant_id} · Customer: ${customer_email}
license:
  tier: ${tier}
  key: "${token}"

strategy:
  amount_aed: 500
  frequency: weekly
  day_of_week: monday
  time: "09:00"
  timezone: "Asia/Dubai"

exchanges:
  okx:
    enabled: false
    api_key_env: OKX_API_KEY
    api_secret_env: OKX_API_SECRET
    passphrase_env: OKX_API_PASSPHRASE
  bitoasis:
    enabled: false
    token_env: BITOASIS_API_TOKEN
  binance:
    enabled: false

execution:
  mode: maker_fallback

routing:
  enable_two_hop: true
  enable_cross_exchange_alerts: true

risk:
  max_consecutive_failures: 5

dry_run: true   # SAFETY — customer flips to false after their own audit
YAML

# Generate a fresh Fernet key for the dashboard's encrypted SecretStore
# (where customer-typed API credentials live). One per tenant, never
# leaves the tenant dir. Without this set, the Exchanges page returns
# "Secret store unavailable" and credential paste forms don't render.
fernet_key="$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")"

# Skeleton .env — customer fills in their API secrets via the dashboard;
# the env-var slots remain for self-hosters who prefer env-based config.
cat > "${tenant_dir}/.env" <<ENV
# Tenant API secrets. chmod 600. Never commit.
DCA_SECRETS_KEY=${fernet_key}
OKX_API_KEY=
OKX_API_SECRET=
OKX_API_PASSPHRASE=
BITOASIS_API_TOKEN=
BINANCE_API_KEY=
BINANCE_API_SECRET=
TG_BOT_TOKEN=
ENV
chmod 600 "${tenant_dir}/.env"

# Render docker-compose.yml for this tenant
export TENANT_ID="${tenant_id}"
export TENANT_DATA_DIR="${tenant_dir}"
export TENANT_DASH_PORT="${dash_port}"
export IMAGE_TAG="${PROVISION_IMAGE_TAG}"
envsubst < "${script_dir}/hosted/docker-compose.tenant.yml" > "${tenant_dir}/docker-compose.yml"

# Render nginx fragment IF the target dir exists. In the bitcoiners-app
# deployment, the per-tenant nginx routing is replaced by bitcoiners-app's
# dynamic /dca/console/[...path] proxy — no nginx needed. We still write
# the fragment if PROVISION_NGINX_DIR exists, for legacy nginx-fronted
# setups (the bare hosted/ deployment without bitcoiners-app).
auth_file="/etc/nginx/.htpasswd-bitcoiners-${tenant_id}"
export TENANT_AUTH_FILE="${auth_file}"
if [[ -d "${PROVISION_NGINX_DIR}" ]]; then
  envsubst < "${script_dir}/hosted/nginx.conf.template" \
    > "${PROVISION_NGINX_DIR}/bitcoiners-dca-${tenant_id}.conf"
  echo "    nginx fragment: ${PROVISION_NGINX_DIR}/bitcoiners-dca-${tenant_id}.conf"
else
  echo "    nginx fragment: skipped (no ${PROVISION_NGINX_DIR}; bitcoiners-app handles proxying)"
fi

echo
echo "==> Tenant '${tenant_id}' provisioned."
echo
echo "Next steps:"
echo "  1. Customer fills in ${tenant_dir}/.env with their API secrets"
echo "  2. cd ${tenant_dir} && docker compose up -d  (provisioner does this for you)"
echo
echo "Container DNS name: bitcoiners-dca-${tenant_id}-dashboard:8000"
echo "Local debug port:   127.0.0.1:${dash_port}"
