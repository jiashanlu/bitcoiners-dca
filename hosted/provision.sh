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
# The bot containers run as the non-root `dca` user (uid/gid 1001 — see the
# bot Dockerfile, task #150). Pre-own the bind-mounted state dirs so the
# daemon/dashboard can write SQLite, secrets, config edits + tax CSVs.
# Numeric 1001:1001 (the host has no `dca` named user — only the image does;
# the kernel stores the numeric id, which is what matters). Idempotent.
chown -R 1001:1001 "${tenant_dir}"/{config,data,reports}

# Pick an unused localhost port in 8100-8999.
#
# IMPORTANT: `ss -ltn` only sees sockets inside THIS process's network
# namespace. When provision.sh runs inside the bitcoiners-provisioner
# container, ss does NOT see host-bound tenant dashboard ports → every
# new tenant picked 8100, colliding with the first tenant.
#
# Fix: parse the assigned port out of every existing tenant's
# docker-compose.yml. The compose ports line is:
#     - "0.0.0.0:${TENANT_DASH_PORT}:8000"
# We grep that and collect all assigned ports, plus check ss for any
# host-process sockets in our namespace just in case. Re-running for an
# existing tenant reuses its already-assigned port (idempotent).
existing_port=""
if [[ -f "${tenant_dir}/docker-compose.yml" ]]; then
  existing_port="$(grep -oE '0\.0\.0\.0:[0-9]+:8000' "${tenant_dir}/docker-compose.yml" | head -1 | cut -d: -f2 || true)"
fi
if [[ -n "${existing_port}" ]]; then
  dash_port="${existing_port}"
  echo "    Dashboard port (reused): ${dash_port}"
else
  # Build a set of all already-assigned ports across all tenants on disk.
  assigned_ports="$(grep -rhoE '0\.0\.0\.0:[0-9]+:8000' "${PROVISION_BASE_DIR}/tenants" 2>/dev/null | cut -d: -f2 | sort -u || true)"
  dash_port=""
  for candidate in $(seq 8100 8999); do
    # Skip if any existing tenant compose claims this port.
    if echo "${assigned_ports}" | grep -qx "${candidate}"; then
      continue
    fi
    # Also skip if a process in our own namespace is bound to it.
    if ss -ltn "( sport = :${candidate} )" 2>/dev/null | grep -q LISTEN; then
      continue
    fi
    dash_port="${candidate}"
    break
  done
  if [[ -z "${dash_port}" ]]; then
    echo "no free port in 8100-8999" >&2
    exit 1
  fi
  echo "    Dashboard port: ${dash_port}"
fi

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
# The non-root bot (uid 1001) reads this via compose env_file — it must own it.
chown 1001:1001 "${tenant_dir}/.env"

# Render docker-compose.yml for this tenant
export TENANT_ID="${tenant_id}"
export TENANT_DATA_DIR="${tenant_dir}"
export TENANT_DASH_PORT="${dash_port}"
export IMAGE_TAG="${PROVISION_IMAGE_TAG}"
# Audit B-P1-6 2026-05-21: the bot dashboard's CF gate refuses any
# CF-Access-authenticated request where the email doesn't match this
# value — defence against a mis-scoped CF Access policy granting
# cross-tenant access.
export TENANT_OWNER_EMAIL="${customer_email}"
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

# Caddy per-tenant route (Hetzner deployment). The dashboard joins the shared
# `tenants` docker network (see docker-compose.tenant.yml) so Caddy can reach
# it as bitcoiners-dca-<id>-dashboard:8000. We write the per-tenant site block
# into Caddy's sites dir (mounted rw into THIS provisioner container as
# PROVISION_CADDY_SITES_DIR) and hot-reload Caddy over the docker socket.
# Skipped on home/LXC setups that don't set PROVISION_CADDY_SITES_DIR (the
# nginx fragment above handles those).
caddy_sites_dir="${PROVISION_CADDY_SITES_DIR:-}"
caddy_container="${PROVISION_CADDY_CONTAINER:-caddy}"
if [[ -n "${caddy_sites_dir}" && -d "${caddy_sites_dir}" ]]; then
  subdomain_base="${PROVISION_TENANT_SUBDOMAIN_BASE:-tenants.bitcoiners.ae}"
  # tenant_id is validated `^[a-z0-9-]{3,40}$` upstream → safe to interpolate.
  printf '%s.%s {\n    reverse_proxy http://bitcoiners-dca-%s-dashboard:8000 {\n        header_up X-Forwarded-Proto https\n    }\n}\n' \
    "${tenant_id}" "${subdomain_base}" "${tenant_id}" \
    > "${caddy_sites_dir}/${tenant_id}.caddy"
  if docker exec "${caddy_container}" caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
    echo "    caddy route: ${tenant_id}.${subdomain_base} (reloaded)"
  else
    echo "    caddy route written but reload FAILED — run: docker exec ${caddy_container} caddy reload --config /etc/caddy/Caddyfile" >&2
  fi
else
  echo "    caddy route: skipped (no PROVISION_CADDY_SITES_DIR set)"
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
