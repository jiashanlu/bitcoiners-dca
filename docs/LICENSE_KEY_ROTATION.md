# License Signing Key — Rotation Runbook

> Status: drafted 2026-05-16 as part of audit follow-through. **Don't rotate**
> outside a deliberate maintenance window — every existing tenant's license
> token becomes invalid the moment the public key on the bot image changes,
> and the tenant downgrades to free until they get a re-signed token.

## The three-place coupling

The Ed25519 keypair lives in three different places and they MUST line up:

| Place | What's stored | Path / env |
|------|----------|----------|
| Tenants host (Hetzner) | **PRIVATE** key — used by provisioner to sign new tokens | `/etc/bitcoiners-dca/keys/license_signing.pem` |
| Bot image | **PUBLIC** key (hex) — burned at build time, used to verify tokens at boot | `LICENSE_PUBLIC_KEY_HEX` build-arg in `Dockerfile` → consumed by `src/bitcoiners_dca/core/license.py` |
| Webapp Vercel env | **PUBLIC** key (hex) — used by `/api/pro/*` requireProLicense() wrapper to verify Pro-tier tokens server-side | `LICENSE_PUBLIC_KEY_HEX` env var on Vercel project `bitcoiners-app` |

If any one of these drifts, all of the following break:

- Existing tenants reload → token signed by old key, bot image trusts new key → bot downgrades silently to free
- New tenants are signed with new key, but bot image still has old key in `LICENSE_PUBLIC_KEY_HEX` → same downgrade
- Pro API calls from bot to webapp → `requireProLicense()` rejects with 401, dashboard banner "Pro API unavailable"

## When to rotate

Legitimate reasons:

- Private key was leaked / suspected leaked (compromise)
- Annual hygiene rotation (calendar-driven, **not** required by anything today)
- Migrating to a stronger algorithm (Ed25519 → something later)

Bad reasons:

- "Just to clean up" → don't. Every rotation invalidates every active customer's
  token until you re-issue. The work is non-trivial; the upside is zero.

## The runbook

### Step 1 — generate a new keypair on the tenants host

SSH to the Hetzner host (or wherever `provisioner_service.py` runs):

```bash
ssh root@<hetzner-host>
cd /etc/bitcoiners-dca/keys
# Rename existing files instead of deleting — needed if rollback.
mv license_signing.pem license_signing.pem.old-$(date -u +%Y%m%d)

# Generate new Ed25519 keypair
python3 -c '
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
pk = Ed25519PrivateKey.generate()
pem = pk.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)
print(pem.decode())' > license_signing.pem
chmod 600 license_signing.pem
```

### Step 2 — extract the new public key hex

Still on the Hetzner host:

```bash
python3 -c '
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
with open("license_signing.pem", "rb") as f:
    pk = serialization.load_pem_private_key(f.read(), password=None)
pub = pk.public_key().public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw,
)
print(pub.hex())'
```

Copy the hex output (64 chars). This is the new `LICENSE_PUBLIC_KEY_HEX`.

### Step 3 — update Vercel env

In the `bitcoiners-app` Vercel project settings:

1. Settings → Environment Variables
2. Find `LICENSE_PUBLIC_KEY_HEX`
3. Update both Production and Preview values to the new hex
4. Redeploy (Vercel won't pick up env-var changes until the next deploy)

Verify post-deploy:

```bash
curl -s https://app.bitcoiners.ae/api/pro/route \
  -H "Content-Type: application/json" \
  -d '{"license":"<existing-old-token>"}' | jq .
# Should now return 401 — confirms the new public key rejects old tokens.
```

### Step 4 — rebuild the bot image with the new public key

```bash
# On the tenants host (or via CI):
cd /opt/bitcoiners-dca
docker build \
  --build-arg LICENSE_PUBLIC_KEY_HEX=<new-hex-from-step-2> \
  -t bitcoiners-dca:rotated-$(date -u +%Y%m%d) \
  .
# Tag as :latest so existing compose pulls pick it up
docker tag bitcoiners-dca:rotated-$(date -u +%Y%m%d) bitcoiners-dca:latest
```

(If you use Gitea CI: bump the `LICENSE_PUBLIC_KEY_HEX` secret in the workflow
env, push a tag-only commit, let CI rebuild + ship.)

### Step 5 — re-sign every existing tenant's token

For each tenant in `provisioned_containers` with `status='running'`:

```bash
# On the tenants host:
TENANT_ID="<email-slug-uuid8>"
NEW_TOKEN=$(curl -s -X POST http://localhost:8500/resign \
  -H "X-Provisioner-Secret: $PROVISIONER_SHARED_SECRET" \
  -H "Content-Type: application/json" \
  -d "{\"tenant_id\": \"$TENANT_ID\"}" | jq -r '.token')

# Manually update the tenant's config.yaml license.key to the new token,
# then restart the tenant compose to pick it up.
```

> **Note:** the `/resign` endpoint doesn't exist yet — this runbook documents
> the intended flow. Adding it is a future task; for now, rotation requires
> deleting + re-provisioning each tenant, which loses no data because the
> tenant data dirs are preserved.

### Step 6 — verify

For each tenant:

1. Hit `https://<tenant-domain>/healthz` — should still respond 200
2. Open the dashboard — Settings → License → tier badge should show its
   actual paid tier (not "free")
3. Check daemon logs: should NOT show `license verification failed`

### Step 7 — clean up

Once every tenant verifies green for 24 hours:

```bash
# On the tenants host:
mv /etc/bitcoiners-dca/keys/license_signing.pem.old-* /tmp/
# Hold them in /tmp for one more cycle, then `trash` them.
```

## Rollback

If something breaks mid-rotation, the fastest path is:

1. On tenants host: `mv license_signing.pem.old-<date> license_signing.pem`
2. On Vercel: revert `LICENSE_PUBLIC_KEY_HEX` to the prior value, redeploy.
3. On tenants host: `docker tag bitcoiners-dca:<prior-tag> bitcoiners-dca:latest`.
4. Recreate any tenants that already received new tokens.

The window for rollback is bounded by step 5 — once you start re-signing tokens,
each one is bound to the new key. That's why step 5 is per-tenant and atomic.
