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

# Issue a 1-year license token
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
token="$(
  python3 "${script_dir}/scripts/generate_license.py" issue \
    --private-key "${PROVISION_PRIVATE_KEY}" \
    --customer-id "${customer_email}" \
    --tier "${tier}" \
    --expires "$(date -u -v+1y +%Y-%m-%d)" \
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

# Skeleton .env — customer fills in their API secrets
cat > "${tenant_dir}/.env" <<'ENV'
# Tenant API secrets. chmod 600. Never commit.
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

# Render nginx fragment (basic auth file is the customer's responsibility to set
# with `htpasswd -c <file> <user>` before reloading nginx)
auth_file="/etc/nginx/.htpasswd-bitcoiners-${tenant_id}"
export TENANT_AUTH_FILE="${auth_file}"
envsubst < "${script_dir}/hosted/nginx.conf.template" \
  > "${PROVISION_NGINX_DIR}/bitcoiners-dca-${tenant_id}.conf"

echo
echo "==> Tenant '${tenant_id}' provisioned."
echo
echo "Next steps:"
echo "  1. Customer fills in ${tenant_dir}/.env with their API secrets"
echo "  2. Set their basic-auth password:"
echo "       htpasswd -c ${auth_file} ${tenant_id}"
echo "  3. cd ${tenant_dir} && docker compose up -d"
echo "  4. nginx -t && systemctl reload nginx"
echo "  5. Verify: curl -u ${tenant_id}:<pw> https://app.bitcoiners.ae/${tenant_id}/healthz"
echo
echo "Dashboard URL: https://app.bitcoiners.ae/${tenant_id}/"
