#!/usr/bin/env bash
#
# Nightly Hetzner-tenant backup — runs on home dockers-LXC (192.168.4.151).
# Hetzner can't reach the home LAN directly (no public IP on dockers-LXC),
# so this script PULLS from Hetzner over the public internet instead of
# Hetzner pushing.
#
# What it captures:
#   /opt/bitcoiners-dca/tenants/     — every tenant's config.yaml, .env
#                                       (Fernet-encrypted exchange creds),
#                                       SQLite trade DBs, reports
#   /etc/bitcoiners-dca/keys/        — license signing key (Ed25519).
#                                       This is the ONLY off-host copy.
#                                       Losing it = no new licenses can be
#                                       issued + existing tokens unverifiable.
#   /etc/bitcoiners-dca/provisioner.env — HMAC shared secret used by Vercel
#                                       to authenticate to the provisioner
#   /opt/caddy/Caddyfile             — reverse-proxy config (per-tenant
#                                       subdomain routing)
#   /opt/caddy/sites/                — per-tenant Caddyfile fragments
#
# Destination: /opt/bitcoiners-backups/hetzner/<YYYY-MM-DD>/ on
# dockers-LXC. Same retention story as the home tenants-LXC backup —
# 30 days retained.
#
# Install on dockers-LXC:
#   sudo install -m 0755 backup_hetzner_tenants.sh /usr/local/sbin/
#   sudo crontab -e
#     # 03:30 UTC daily (15 min after the home tenants-LXC backup, to
#     # avoid hammering dockers-LXC disk + Hetzner upload bandwidth
#     # simultaneously)
#     30 3 * * * /usr/local/sbin/backup_hetzner_tenants.sh >> /var/log/bitcoiners-dca-hetzner-backup.log 2>&1
#
# SSH key on dockers-LXC:
#   /root/.ssh/jiashan_ai_ed25519 (shared infra key). Already authorised
#   for root@178.105.66.56.

set -euo pipefail

HETZNER_HOST=root@178.105.66.56
SSH_KEY=/root/.ssh/jiashan_ai_ed25519
BACKUP_BASE=/opt/bitcoiners-backups/hetzner
DATE="$(date -u +%Y-%m-%d)"
TS="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
DEST_DIR="${BACKUP_BASE}/${DATE}"
ARCHIVE="${DEST_DIR}/bitcoiners-dca-hetzner-${TS}.tar.gz"
RETAIN_DAYS=30

log() { echo "[$(date -u +%FT%TZ) backup-hetzner-tenants] $*"; }

mkdir -p "${DEST_DIR}"
chmod 700 "${BACKUP_BASE}"

# Consistent SQLite snapshots BEFORE the tar. Tarring a live WAL database
# copies the -wal/-shm files at different instants than the main file —
# a torn snapshot that can fail integrity_check on restore (audit
# 2026-06-10 P2; the old --warning=no-file-changed comment claimed WAL
# made it restorable, which is only true if all three files are copied
# atomically — tar doesn't do that). sqlite3's Online Backup API
# (Connection.backup via each tenant's own container python) produces a
# point-in-time-consistent dca.db.snapshot INSIDE the data dir, which the
# tar below picks up. Restore procedure: prefer dca.db.snapshot when
# present. Skips (container down, no DB yet) keep the old behaviour for
# that tenant — partial beats nothing.
log "Creating consistent SQLite snapshots on Hetzner"
ssh -i "${SSH_KEY}" -o StrictHostKeyChecking=accept-new "${HETZNER_HOST}" '
  for d in /opt/bitcoiners-dca/tenants/*/; do
    t=$(basename "$d")
    c="bitcoiners-dca-${t}-daemon"
    if [ -f "${d}data/dca.db" ] && docker ps --format "{{.Names}}" | grep -qx "$c"; then
      docker exec "$c" python -c "
import sqlite3
src = sqlite3.connect(\"/app/data/dca.db\")
dst = sqlite3.connect(\"/app/data/dca.db.snapshot\")
src.backup(dst)
dst.close(); src.close()
" && echo "snapshot OK: $t" || echo "snapshot FAILED: $t (live file will be tarred as-is)" >&2
    fi
  done
' || log "WARNING: snapshot pass had failures — continuing with live files"

# Streaming tar over SSH — avoids creating intermediate files on Hetzner
# (Hetzner is a 40GB box; we don't want 50 GB of historical backups
# accumulating there).
#
# --ignore-failed-read: a permission glitch on one tenant dir shouldn't
# nuke the whole backup. Better partial than nothing.
log "Streaming tarball from Hetzner → ${ARCHIVE}"
if ssh -i "${SSH_KEY}" -o StrictHostKeyChecking=accept-new "${HETZNER_HOST}" \
    "tar -czf - --warning=no-file-changed --ignore-failed-read \
       -C / \
       opt/bitcoiners-dca/tenants \
       etc/bitcoiners-dca \
       opt/caddy/Caddyfile \
       opt/caddy/sites 2>/dev/null" \
    > "${ARCHIVE}.partial"; then
  mv "${ARCHIVE}.partial" "${ARCHIVE}"
  chmod 600 "${ARCHIVE}"
  ARCHIVE_SIZE=$(du -h "${ARCHIVE}" | cut -f1)
  log "Backup complete: ${ARCHIVE} (${ARCHIVE_SIZE})"
else
  log "Hetzner pull FAILED — partial file at ${ARCHIVE}.partial"
  exit 1
fi

# Sanity: did we get more than 1KB? Empty tarball would be ~45 bytes.
ARCHIVE_BYTES=$(stat -c%s "${ARCHIVE}")
if [[ "${ARCHIVE_BYTES}" -lt 1024 ]]; then
  log "WARN: archive is only ${ARCHIVE_BYTES} bytes — likely empty/broken"
  exit 1
fi

log "Prune archives older than ${RETAIN_DAYS}d"
find "${BACKUP_BASE}" -mindepth 2 -maxdepth 2 \
  -name 'bitcoiners-dca-hetzner-*.tar.gz' \
  -mtime "+${RETAIN_DAYS}" -delete

log "Done"
