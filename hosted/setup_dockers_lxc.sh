#!/usr/bin/env bash
# Server-side bootstrap for the hosted-bot provisioning flow.
#
# Run ONCE on the dockers-LXC host (192.168.4.151) to:
#   1. Clone bitcoiners-dca to /opt/bitcoiners-dca
#   2. Generate a fresh Ed25519 license signing keypair
#   3. Build the bitcoiners-dca Docker image (used by all tenants)
#   4. Build + start the provisioner microservice container
#   5. Ensure the bitcoiners-app Docker network exists
#
# After this, bitcoiners-app can call http://provisioner:8500/provision to
# spawn per-tenant bot stacks.
#
# Idempotent — safe to re-run.

set -euo pipefail

REPO_URL="${REPO_URL:-http://jiashanlu:508108b684b57110796c3e641d286e100695e25b@192.168.4.151:3005/jiashan-dev/bitcoiners-dca.git}"
INSTALL_DIR="/opt/bitcoiners-dca"
KEYS_DIR="/etc/bitcoiners-dca/keys"
ENV_FILE="/etc/bitcoiners-dca/provisioner.env"
NETWORK_NAME="bitcoiners-app"

log() { echo "[setup] $*" >&2; }

# ─── 1. Network ──────────────────────────────────────────────────────────
log "Ensuring Docker network '${NETWORK_NAME}' exists"
docker network inspect "${NETWORK_NAME}" >/dev/null 2>&1 \
  || docker network create "${NETWORK_NAME}"

# ─── 2. Clone or update repo ─────────────────────────────────────────────
if [[ -d "${INSTALL_DIR}/.git" ]]; then
  log "Updating ${INSTALL_DIR}"
  git -C "${INSTALL_DIR}" pull --ff-only
else
  log "Cloning bitcoiners-dca into ${INSTALL_DIR}"
  git clone "${REPO_URL}" "${INSTALL_DIR}"
fi

mkdir -p "${INSTALL_DIR}/tenants"
chmod 700 "${INSTALL_DIR}/tenants"

# ─── 3. License signing keypair ──────────────────────────────────────────
mkdir -p "${KEYS_DIR}"
chmod 700 "${KEYS_DIR}"
PRIVATE_KEY="${KEYS_DIR}/license_signing.pem"
PUBLIC_HEX_FILE="${KEYS_DIR}/license_signing.pub.hex"

if [[ -s "${PRIVATE_KEY}" ]]; then
  log "License signing key already exists at ${PRIVATE_KEY} — keeping"
  PUBLIC_HEX="$(cat "${PUBLIC_HEX_FILE}")"
else
  log "Generating new Ed25519 license signing keypair via python:3.12-slim"
  # dockers-LXC is Alpine without python — run keygen inside an ephemeral
  # python container that has cryptography installed.
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

  # Patch the public key into license.py so the rebuilt bot image
  # verifies against the freshly-generated private key. Idempotent —
  # we only edit the inside of the LICENSE_PUBLIC_KEY_HEX = (...) block.
  log "Patching LICENSE_PUBLIC_KEY_HEX in src/bitcoiners_dca/core/license.py"
  # Use sed for the patch — no Python on dockers-LXC. We replace the first
  # 64-char hex literal in license.py. That's the bootstrap LICENSE_PUBLIC_KEY_HEX.
  LICENSE_PY="${INSTALL_DIR}/src/bitcoiners_dca/core/license.py"
  if ! grep -qE '"[0-9a-f]{64}"' "${LICENSE_PY}"; then
    log "ERROR: no 64-char hex literal found in license.py"
    exit 1
  fi
  # Use a tab as sed delimiter so the hex value (which never contains tabs)
  # doesn't collide with the slash that sed uses by default.
  sed -i -e "0,/\"[0-9a-f]\\{64\\}\"/s/\"[0-9a-f]\\{64\\}\"/\"${PUBLIC_HEX}\"/" "${LICENSE_PY}"
  if ! grep -q "\"${PUBLIC_HEX}\"" "${LICENSE_PY}"; then
    log "ERROR: license.py patch did not take effect"
    exit 1
  fi
  log "license.py patched"
fi

# ─── 4. Provisioner shared secret ────────────────────────────────────────
if [[ ! -f "${ENV_FILE}" ]]; then
  log "Generating provisioner shared secret"
  SECRET=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 40)
  cat > "${ENV_FILE}" <<EOF
PROVISIONER_SHARED_SECRET=${SECRET}
EOF
  chmod 600 "${ENV_FILE}"
  log "Shared secret written to ${ENV_FILE}"
  log "Add this to bitcoiners-app .env:"
  log "  PROVISIONER_URL=http://provisioner:8500"
  log "  PROVISIONER_SHARED_SECRET=${SECRET}"
else
  log "Provisioner env already exists at ${ENV_FILE} — keeping"
fi

# ─── 5. Build images ─────────────────────────────────────────────────────
cd "${INSTALL_DIR}"

log "Building bitcoiners-dca:latest (tenant bot image)"
docker build -t bitcoiners-dca:latest .

log "Building bitcoiners-provisioner:latest"
docker build -f hosted/provisioner.Dockerfile -t bitcoiners-provisioner:latest .

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
log "Next steps in bitcoiners-app:"
log "  1. Add PROVISIONER_URL and PROVISIONER_SHARED_SECRET to .env"
log "  2. Restart bitcoiners-app: docker restart bitcoiners-app"
log "  3. Test provision: curl from bitcoiners-app into http://provisioner:8500/healthz"
