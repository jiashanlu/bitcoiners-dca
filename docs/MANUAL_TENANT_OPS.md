# Manual Tenant Ops

Procedures for adding, modifying, or destroying a DCA tenant outside
the normal Stripe → webhook → provisioner-tick flow.

When to use this:
- VIP onboarding (you want to comp someone)
- Stripe payment failed but you want to provision anyway (manual deal)
- A `provisioning_jobs` row is stuck in `failed` after retries exhausted
- Bulk-spinning test tenants for development

For the happy-path (paid customer → automatic tenant), this is
unnecessary. The webhook handler enqueues the job, cron picks it up.

---

## Provision a tenant manually

Two ways: call the provisioner HTTP API, or run `provision.sh` directly.

### Option A: HTTP API call

```bash
PROVISIONER_URL=http://178.105.66.56:8500   # Hetzner prod; for dev use http://192.168.4.160:8500
PROVISIONER_SECRET=$(grep ^PROVISIONER_SHARED_SECRET /opt/bitcoiners-app-infra/.env | cut -d= -f2)

curl -sS -X POST "${PROVISIONER_URL}/provision" \
  -H "Content-Type: application/json" \
  -H "X-Provisioner-Secret: ${PROVISIONER_SECRET}" \
  -d '{
    "tenant_id": "displayname-randomsuffix",
    "owner_email": "customer@example.com",
    "tier": "pro"
  }'
```

Watch the response for `{ ok: true, internal_host, internal_port }`.

### Option B: Direct script invocation on Hetzner

```bash
ssh root@178.105.66.56
cd /opt/bitcoiners-dca/hosted
./provision.sh <tenant-id> <owner-email> <tier>

# Verify
docker ps --format "{{.Names}}" | grep <tenant-id>
```

## Insert the provisioned_containers + subscriptions rows manually

After either provisioning method, you still need to write the DB rows
that tell bitcoiners-app this tenant exists. The provisioner doesn't
do that — it only spawns the container.

```sql
-- On the target Neon (prod) or local Postgres (dev):

INSERT INTO subscriptions (
  user_id, product, tier, status, provider,
  stripe_customer_id, stripe_subscription_id, stripe_price_id,
  current_period_start, current_period_end,
  cancel_at_period_end, created_at, updated_at
) VALUES (
  '<user-id>', 'dca', 'pro', 'active', 'manual',
  NULL, NULL, NULL,   -- no Stripe IDs for manual
  now(), now() + interval '1 year',  -- "lifetime comp" = year-long
  false, now(), now()
);

INSERT INTO provisioned_containers (
  user_id, product, tenant_id, container_name,
  internal_host, internal_port, status,
  license_token, created_at, updated_at
) VALUES (
  '<user-id>', 'dca', '<tenant-id>',
  'bitcoiners-dca-<tenant-id>-dashboard',
  '<tenant-id>.tenants.bitcoiners.ae', 443, 'running',
  '<license-token-from-provisioner-response>',
  now(), now()
);
```

The license token comes from the provisioner's response — it's
Ed25519-signed against the publisher's private key.

## Suspend a tenant manually

```bash
ssh root@178.105.66.56 '
cd /opt/bitcoiners-dca/tenants/<tenant-id>
docker compose stop
'

# Then mark in DB
psql "${NEON_MAIN_POOLED}" -c "
UPDATE provisioned_containers
SET status='suspended', updated_at=now()
WHERE tenant_id='<tenant-id>';
"
```

## Resume a suspended tenant

```bash
ssh root@178.105.66.56 '
cd /opt/bitcoiners-dca/tenants/<tenant-id>
docker compose up -d
'

psql "${NEON_MAIN_POOLED}" -c "
UPDATE provisioned_containers
SET status='running', updated_at=now()
WHERE tenant_id='<tenant-id>';
"
```

## Destroy a tenant (irreversible — data is gone)

```bash
# Confirm you've got a backup of this tenant's data dir if there's any chance you'll want it back
ssh root@178.105.66.56 'ls -la /opt/bitcoiners-dca/tenants/<tenant-id>/data/'

# Stop + remove containers + remove volumes
ssh root@178.105.66.56 '
cd /opt/bitcoiners-dca/tenants/<tenant-id>
docker compose down -v
'

# Delete the directory
ssh root@178.105.66.56 'rm -rf /opt/bitcoiners-dca/tenants/<tenant-id>'

# Remove Caddy route
ssh root@178.105.66.56 '
rm /opt/caddy/sites/<tenant-id>.caddy
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
'

# DB rows — soft-delete or hard-delete?
# Soft (recommended): set status='destroyed' so the dispute trail survives
# Hard: DELETE FROM provisioned_containers WHERE tenant_id='<tenant-id>';
```

## List all tenants on a host

```bash
ssh root@178.105.66.56 'ls /opt/bitcoiners-dca/tenants/'
ssh root@178.105.66.56 'docker ps --format "{{.Names}} {{.Status}}" | grep bitcoiners-dca'
```

Cross-reference with DB:
```bash
psql "${NEON_MAIN_POOLED}" -c "
SELECT user_id, tenant_id, status, internal_host, created_at
FROM provisioned_containers
WHERE product='dca'
ORDER BY created_at DESC;
"
```

Any tenant on disk but not in DB = orphan from a failed provisioning
or manual cleanup that didn't reach the DB. Worth investigating.

## Common pitfalls

- **Tenant ID character set**: must match `^[a-z0-9-]{3,40}$`. Capital
  letters or underscores will be rejected by the provisioner.
- **License token vs API tier**: the DB `tier` field controls the
  /api/pro/* gate on bitcoiners-app; the license token controls what
  the bot enables locally. Both must match — set both to `pro` or
  both to `business`. A mismatch causes "I'm Pro on the website but
  multi-exchange routing doesn't work in my bot."
- **Internal host port**: the bot tenant containers listen on port
  8100+ per the provisioner port-allocation. Don't manually set port
  in DB — let the provisioner's response carry it.
- **Stripe customer ID null on manual-provisioned tenants**: this is
  intentional. The Customer Portal route handles this case — manual
  customers get pointed at `support@bitcoiners.ae` for billing.
