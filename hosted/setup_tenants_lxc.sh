#!/usr/bin/env bash
# Server-side bootstrap for the hosted-bot provisioning flow.
#
# Run ONCE on a dedicated Debian/Ubuntu tenants-LXC (NOT on dockers-LXC).
# Steps:
#   1. Clone bitcoiners-dca to /opt/bitcoiners-dca
#   2. Generate a fresh Ed25519 license signing keypair (rotates the
#      bootstrap public key baked into the bot image)
#   3. Build the bitcoiners-dca Docker image (used by all tenants)
#   4. Build + start the provisioner microservice container, binding 8500
#      on the host LAN interface
#
# After this, bitcoiners-app (running on a different LXC) calls
# http://<this-lxc-lan-ip>:8500/provision to spawn per-tenant bot stacks.
# Lock down inbound port 8500 + 8100-8999 to bitcoiners-app's source IP
# with ufw on this host.
#
# Idempotent — safe to re-run.

set -euo pipefail

# Credentials come from env. Never hardcode tokens here — earlier revs
# inlined the Gitea PAT and it ended up in git history. Run with:
#   GITEA_USER=jiashanlu GITEA_TOKEN=<pat> ./setup_tenants_lxc.sh
: "${GITEA_USER:?GITEA_USER required (e.g. jiashanlu)}"
: "${GITEA_TOKEN:?GITEA_TOKEN required (Gitea PAT with repo:read)}"
GITEA_HOST="${GITEA_HOST:-192.168.4.151:3005}"
GITEA_REPO="${GITEA_REPO:-jiashan-dev/bitcoiners-dca}"
REPO_URL="${REPO_URL:-http://${GITEA_USER}:${GITEA_TOKEN}@${GITEA_HOST}/${GITEA_REPO}.git}"
INSTALL_DIR="/opt/bitcoiners-dca"
KEYS_DIR="/etc/bitcoiners-dca/keys"
ENV_FILE="/etc/bitcoiners-dca/provisioner.env"
# LAN IP / hostname bitcoiners-app uses to reach this host. The provisioner
# returns it as `internal_host` so bitcoiners-app's dynamic proxy knows
# where to send dashboard requests. Defaults to the first interface IP.
TENANT_HOSTNAME="${TENANT_HOSTNAME:-$(hostname -I | awk '{print $1}')}"

log() { echo "[setup] $*" >&2; }
log "Tenant hostname (returned to bitcoiners-app): ${TENANT_HOSTNAME}"

# ─── 1. Clone or update repo ─────────────────────────────────────────────
if [[ -d "${INSTALL_DIR}/.git" ]]; then
  log "Updating ${INSTALL_DIR}"
  git -C "${INSTALL_DIR}" pull --ff-only
else
  log "Cloning bitcoiners-dca into ${INSTALL_DIR}"
  git clone "${REPO_URL}" "${INSTALL_DIR}"
fi

mkdir -p "${INSTALL_DIR}/tenants"
chmod 700 "${INSTALL_DIR}/tenants"

# ─── 2. License signing keypair ──────────────────────────────────────────
mkdir -p "${KEYS_DIR}"
chmod 700 "${KEYS_DIR}"
PRIVATE_KEY="${KEYS_DIR}/license_signing.pem"
PUBLIC_HEX_FILE="${KEYS_DIR}/license_signing.pub.hex"

if [[ -s "${PRIVATE_KEY}" ]]; then
  log "License signing key already exists at ${PRIVATE_KEY} — keeping"
  PUBLIC_HEX="$(cat "${PUBLIC_HEX_FILE}")"
else
  log "Generating new Ed25519 license signing keypair via python:3.12-slim"
  # Run keygen in an ephemeral container with cryptography installed.
  out=$(docker run --rm \
    -v "${INSTALL_DIR}:/work:ro" \
    -v "${KEYS_DIR}:/keys" \
    -w /work \
    python:3.12-slim \
    bash -c "pip install --quiet 'cryptography>=42' && \
      python scripts/generate_license.py keygen --out /keys/license_signing.pem 2>&1")
  echo "${out}"
  PUBLIC_HEX=$(echo "${out}" | grep -oE '[0-9a-f]{64}' | head -1)
  if [[ -z "${PUBLIC_HEX}" ]]; then
    log "ERROR: could not extract public hex from keygen output"
    exit 1
  fi
  echo "${PUBLIC_HEX}" > "${PUBLIC_HEX_FILE}"
  chmod 600 "${PRIVATE_KEY}"
  chmod 644 "${PUBLIC_HEX_FILE}"

  log "Patching LICENSE_PUBLIC_KEY_HEX in src/bitcoiners_dca/core/license.py"
  # Use python (in a container) for the patch — portable + clean.
  docker run --rm \
    -v "${INSTALL_DIR}:/work" \
    -e "PUBLIC_HEX=${PUBLIC_HEX}" \
    -w /work \
    python:3.12-slim \
    python -c "
import os, re, sys, pathlib
p = pathlib.Path('src/bitcoiners_dca/core/license.py')
src = p.read_text()
new, n = re.subn(r'\"[0-9a-f]{64}\"', f'\"{os.environ[\"PUBLIC_HEX\"]}\"', src, count=1)
if n == 0:
    print('ERROR: no 64-char hex literal found in license.py', file=sys.stderr)
    sys.exit(1)
p.write_text(new)
print(f'license.py patched: {os.environ[\"PUBLIC_HEX\"][:16]}...')
"
fi

# ─── 3. Provisioner env ──────────────────────────────────────────────────
if [[ ! -f "${ENV_FILE}" ]]; then
  log "Generating provisioner env"
  SECRET=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 40)
  cat > "${ENV_FILE}" <<EOF
PROVISIONER_SHARED_SECRET=${SECRET}
PROVISIONER_TENANT_HOSTNAME=${TENANT_HOSTNAME}
EOF
  chmod 600 "${ENV_FILE}"
  log "Provisioner env written to ${ENV_FILE}"
  log
  log "Add this to bitcoiners-app .env (on the OTHER LXC):"
  log "  PROVISIONER_URL=http://${TENANT_HOSTNAME}:8500"
  log "  PROVISIONER_SHARED_SECRET=${SECRET}"
else
  log "Provisioner env already exists at ${ENV_FILE} — keeping"
  # If TENANT_HOSTNAME isn't yet in the env file, append it.
  if ! grep -q '^PROVISIONER_TENANT_HOSTNAME=' "${ENV_FILE}"; then
    echo "PROVISIONER_TENANT_HOSTNAME=${TENANT_HOSTNAME}" >> "${ENV_FILE}"
    log "Appended PROVISIONER_TENANT_HOSTNAME=${TENANT_HOSTNAME}"
  fi
fi

# ─── 4. Build images ─────────────────────────────────────────────────────
cd "${INSTALL_DIR}"

log "Building bitcoiners-dca:latest (tenant bot image)"
docker build -t bitcoiners-dca:latest .

log "Building bitcoiners-provisioner:latest"
docker build -f hosted/provisioner.Dockerfile -t bitcoiners-provisioner:latest .

# ─── 5. Pre-create the external `tenants` network ──────────────────────
# docker-compose.provisioner.yml attaches the provisioner to this network
# so that on Hetzner-style hosts, the Caddy reverse-proxy (which lives on
# `tenants`) can reach the provisioner at `bitcoiners-provisioner:8500`.
# Idempotent — `network create` is a no-op if it already exists.
log "Ensuring 'tenants' docker network exists"
docker network inspect tenants >/dev/null 2>&1 || docker network create tenants

# ─── 6. Start provisioner ────────────────────────────────────────────────
log "Starting provisioner container"
docker compose -f "${INSTALL_DIR}/hosted/docker-compose.provisioner.yml" up -d

log "Waiting for provisioner to come up"
for i in {1..20}; do
  if docker exec bitcoiners-provisioner curl -fsS http://127.0.0.1:8500/healthz >/dev/null 2>&1; then
    log "Provisioner healthy"
    break
  fi
  sleep 2
done

log "Setup complete."
log
log "Next steps:"
log "  1. Lock down ufw on this LXC:"
log "       ufw allow from <bitcoiners-app-ip> to any port 8500"
log "       ufw allow from <bitcoiners-app-ip> to any port 8100:8999"
log "       ufw default deny incoming && ufw enable"
log "  2. On bitcoiners-app LXC, add to .env:"
log "       PROVISIONER_URL=http://${TENANT_HOSTNAME}:8500"
log "       PROVISIONER_SHARED_SECRET=$(grep '^PROVISIONER_SHARED_SECRET=' ${ENV_FILE} | cut -d= -f2)"
log "  3. Restart bitcoiners-app container"
log "  4. Smoke test: curl from bitcoiners-app to http://${TENANT_HOSTNAME}:8500/healthz"
