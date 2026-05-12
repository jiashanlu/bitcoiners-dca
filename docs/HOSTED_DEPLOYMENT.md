# Hosted deployment playbook

How to run the bot AS A SERVICE for paying customers. Internal doc — not
shipped in the open-source release.

## Architecture

```
                      Cloudflare Tunnel
                            │
                            ▼
                    app.bitcoiners.ae
                            │
                            ▼
                      nginx (host)
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
        /alice-pro    /bob-business   /charlie-pro
              │             │             │
              ▼             ▼             ▼
        :8101 dash    :8102 dash    :8103 dash
              │             │             │
              ▼             ▼             ▼
        daemon-alice  daemon-bob    daemon-charlie
              │             │             │
              ▼             ▼             ▼
        SQLite DB     SQLite DB     SQLite DB
        (alice)       (bob)         (charlie)
```

Each customer gets:
- Two Docker containers (daemon + dashboard)
- Their own data volume at `tenants/<tenant_id>/`
- A unique localhost port for the dashboard (8100-8999 pool)
- A basic-auth-gated URL path under `app.bitcoiners.ae`
- A signed license token baked into their config

No customer ever sees another customer's data. The hosted infrastructure
runs ONE Docker engine and ONE nginx, but tenants are filesystem-
isolated.

## Prerequisites

Run-once setup on the host (Mac mini or dedicated VPS):

1. **License signing key.** Generate once, stash securely:
   ```bash
   python scripts/generate_license.py keygen \
     --out /opt/bitcoiners-dca/license_signing_key.pem
   chmod 600 /opt/bitcoiners-dca/license_signing_key.pem
   ```
   Replace the `LICENSE_PUBLIC_KEY_HEX` constant in `core/license.py`
   with the printed public key, rebuild the Docker image.

2. **Docker image published.** Build + push the image to a registry:
   ```bash
   docker buildx build \
     --platform linux/amd64,linux/arm64 \
     -t ghcr.io/jiashanlu/bitcoiners-dca:0.5.0 \
     -t ghcr.io/jiashanlu/bitcoiners-dca:latest \
     --push .
   ```

3. **nginx with htpasswd module** installed. On Ubuntu:
   ```bash
   apt install -y nginx apache2-utils
   ```

4. **Provision env vars** set on the host:
   ```bash
   export PROVISION_PRIVATE_KEY=/opt/bitcoiners-dca/license_signing_key.pem
   export PROVISION_IMAGE_TAG=ghcr.io/jiashanlu/bitcoiners-dca:0.5.0
   export PROVISION_BASE_DIR=/opt/bitcoiners-dca
   export PROVISION_NGINX_DIR=/etc/nginx/conf.d
   ```

5. **Cloudflare Tunnel ingress** routes `app.bitcoiners.ae` → host's nginx :80.
   The tenant URL path lives under that.

## Onboarding a new customer

```bash
# 1. Provision the tenant (creates dirs, issues license, renders nginx + compose)
hosted/provision.sh alice-pro alice@example.com pro

# 2. Customer pastes their API secrets into the tenant .env
$EDITOR /opt/bitcoiners-dca/tenants/alice-pro/.env

# 3. Set up basic-auth (one-time per tenant)
htpasswd -c /etc/nginx/.htpasswd-bitcoiners-alice-pro alice-pro

# 4. Start their containers
cd /opt/bitcoiners-dca/tenants/alice-pro
docker compose up -d

# 5. Reload nginx
nginx -t && systemctl reload nginx

# 6. Verify
curl -u alice-pro:<pw> https://app.bitcoiners.ae/alice-pro/healthz
```

Customer's dashboard is now at `https://app.bitcoiners.ae/alice-pro/`,
protected by HTTP Basic auth. Customer logs in with their tenant_id +
password and sees their own trades, balances, status, etc.

## Day-2 operations

### Renewing a license

```bash
# Generate a new token with a fresh expiry
TOKEN=$(python scripts/generate_license.py issue \
  --private-key /opt/bitcoiners-dca/license_signing_key.pem \
  --customer-id alice@example.com \
  --tier pro \
  --expires 2028-05-12 | tail -1 | tr -d ' ')

# Update the tenant's config.yaml — replace license.key with $TOKEN
$EDITOR /opt/bitcoiners-dca/tenants/alice-pro/config/config.yaml

# Restart only their daemon
docker compose -f /opt/bitcoiners-dca/tenants/alice-pro/docker-compose.yml restart daemon
```

### Suspending a customer

Easiest: shut down their containers.

```bash
cd /opt/bitcoiners-dca/tenants/alice-pro
docker compose down
```

The data stays on disk. To reactivate: `docker compose up -d`.

### Downgrading a customer to free

Re-issue their license with `--tier free` (no, wait — there's no free token,
just no token). Set `license.tier: free` in their config, restart daemon.
Premium features auto-disable, customer's account keeps running on the
limited free-tier capabilities.

### Deleting a customer

```bash
docker compose -f /opt/bitcoiners-dca/tenants/alice-pro/docker-compose.yml down
rm /etc/nginx/conf.d/bitcoiners-dca-alice-pro.conf
rm /etc/nginx/.htpasswd-bitcoiners-alice-pro
nginx -t && systemctl reload nginx
# Preserve their data for 30 days in case of dispute / re-onboarding
mv /opt/bitcoiners-dca/tenants/alice-pro \
   /opt/bitcoiners-dca/tenants/_deleted/alice-pro-$(date -u +%Y%m%d)
```

### Updating the bot version across all tenants

```bash
# 1. Build + push the new image (e.g. 0.6.0)
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/jiashanlu/bitcoiners-dca:0.6.0 --push .

# 2. For each tenant, rewrite the compose file with the new tag and recreate
for tenant_dir in /opt/bitcoiners-dca/tenants/*/; do
  sed -i 's|bitcoiners-dca:0.5.0|bitcoiners-dca:0.6.0|g' \
    "${tenant_dir}/docker-compose.yml"
  docker compose -f "${tenant_dir}/docker-compose.yml" up -d
done
```

Roll back: replay the sed with the old tag and `up -d` again.

## Monitoring

Per-host:

```bash
# All running tenants
docker ps --filter "label=bitcoiners-dca.tenant" --format "table {{.Names}}\t{{.Status}}"

# Aggregate logs (paged)
docker logs --tail 50 -f bitcoiners-dca-alice-pro-daemon

# All tenants' cycle counts
for t in /opt/bitcoiners-dca/tenants/*/; do
  echo "$(basename $t): $(sqlite3 $t/data/dca.db 'SELECT count(*) FROM trades' 2>/dev/null)"
done
```

Per-tenant SLOs (informal):
- Dashboard p99 < 500ms
- Daemon health check passing every 5 min
- DCA cycle success rate > 99% over 30 days

## Backups

`data/dca.db` is the only stateful piece per tenant. Daily encrypted
backup to NAS / cloud:

```bash
# crontab: nightly at 03:00
0 3 * * * for t in /opt/bitcoiners-dca/tenants/*/; do
  tenant=$(basename $t)
  sqlite3 $t/data/dca.db ".backup /backups/dca/${tenant}-$(date +\%F).db"
  age -r $BACKUP_PUBKEY \
    /backups/dca/${tenant}-$(date +\%F).db \
    > /backups/dca/${tenant}-$(date +\%F).db.age
  rm /backups/dca/${tenant}-$(date +\%F).db
done
```

## Pricing & billing

Out-of-scope for this doc. Use Stripe (or BTCPay if accepting Bitcoin)
to handle subscriptions externally. The bot itself doesn't care about
billing — the license token expiry is the enforcement point. When a
subscription lapses, simply don't issue a renewal token; the customer's
key expires and the bot auto-downgrades to free.
