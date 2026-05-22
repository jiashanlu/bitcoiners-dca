# Hetzner Disaster-Recovery Runbook

Target RTO: ~45 min from "Hetzner is gone" to "tenant cycles resume."
Target RPO: ≤24h (last nightly Hetzner backup tarball on dockers-LXC).

This procedure restores prod DCA tenants onto a fresh Hetzner box when
the original is unrecoverable. For partial failures (one container
crashed, disk full, Caddy misconfigured) use the host-level fixes in
`HOSTED_DEPLOYMENT.md` instead.

## What you'll need

- Hetzner Cloud account access (current prod is CCX13 at `178.105.66.56`)
- SSH access to dockers-LXC (`192.168.4.151`) where the latest backup tarball lives
- The CRON_HEARTBEAT_SECRET + ADMIN_TG_* env vars from `infra/secrets.env`
- DNS access (Cloudflare) to repoint `*.bitcoiners.ae` tenant subdomains
- ~45 min of focused time

## 1 — Confirm the box is actually gone

Before any DR action, eliminate transient causes:

```bash
ping -c 5 178.105.66.56
ssh -i infra/jiashan_ai_ed25519 -o ConnectTimeout=10 root@178.105.66.56 'uptime'
```

If both fail: check Hetzner Cloud Console for power state. If "Running"
but unreachable, request a hardware-reboot via console and wait 5 min.
Only proceed to step 2 if Hetzner has confirmed loss OR more than 30
minutes of unreachability with no console response.

## 2 — Pick the freshest backup

```bash
ssh dockers-lxc 'ls -lt /opt/bitcoiners-backups/hetzner/*/bitcoiners-dca-hetzner-*.tar.gz | head -3'
```

Pick the most recent. If today's hasn't run yet:

```bash
ssh dockers-lxc '/usr/local/bin/backup_hetzner_tenants.sh'  # force a fresh pull — only works if old box was alive at last pull
```

(If the old box is dead, you only have last night's snapshot.)

## 3 — Provision new Hetzner box

Hetzner Cloud Console → Create Server:
- Image: **Ubuntu 24.04**
- Type: **CCX13** (same as current prod)
- Location: Helsinki (same as current) or Falkenstein (slightly cheaper if you don't care)
- Networking: IPv4 only (we don't use v6 in DNS)
- SSH keys: select the `jiashan-ai` key already in your Hetzner account
- Name: `bitcoiners-dca-prod-2`

Wait ~60s for the box to come up. Note the new IPv4.

## 4 — Bootstrap the new box

```bash
NEW_IP=178.x.x.x  # from step 3

# First SSH — accept host key
ssh -i infra/jiashan_ai_ed25519 root@${NEW_IP} 'uname -a'

# Install Docker (Hetzner Ubuntu 24.04 doesn't ship with it)
ssh -i infra/jiashan_ai_ed25519 root@${NEW_IP} '
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
mkdir -p /etc/bitcoiners-dca /opt/bitcoiners-dca/tenants /opt/caddy
# Audit I-P0-3 follow-up: enable host firewall *before* the tarball
# lands so nothing publishes to the internet by accident.
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
# Daemon-level log rotation cap so a container leak can\\'t fill disk.
echo \\'{\"log-driver\":\"json-file\",\"log-opts\":{\"max-size\":\"50m\",\"max-file\":\"5\"}}\\' > /etc/docker/daemon.json
systemctl restart docker
'
```

## 5 — Restore the backup tarball

```bash
# From dockers-LXC to new Hetzner box, streaming over SSH:
ssh dockers-lxc 'cat /opt/bitcoiners-backups/hetzner/<DATE>/<TARBALL>' \
  | ssh -i infra/jiashan_ai_ed25519 root@${NEW_IP} 'tar xzvf - -C /'
```

The tarball lays files into `/opt/bitcoiners-dca/tenants/`,
`/etc/bitcoiners-dca/`, and `/opt/caddy/`.

Verify expected layout:
```bash
ssh root@${NEW_IP} 'ls /opt/bitcoiners-dca/tenants/ /etc/bitcoiners-dca/keys/ /opt/caddy/sites/'
```

## 6 — Restore the provisioner + Caddy

The provisioner image isn't in the tarball — it's built from the
`bitcoiners-dca` git repo. Pull the image used by the prior prod:

```bash
ssh root@${NEW_IP} '
# Provisioner image — match the tag from the prior compose
docker pull <registry>/bitcoiners-dca-provisioner:<tag>
docker pull caddy:2-alpine
'
```

Bring up Caddy first (so tenant containers can register routes when
they start):

```bash
ssh root@${NEW_IP} '
cd /opt/caddy
docker run -d --name caddy \
  --restart unless-stopped \
  -p 80:80 -p 443:443 \
  -v /opt/caddy:/etc/caddy \
  -v caddy_data:/data \
  -v caddy_config:/config \
  caddy:2-alpine
'
```

Then provisioner:
```bash
ssh root@${NEW_IP} '
cd /opt/bitcoiners-dca/hosted  # provisioner-service path
# Provisioner ENV: PROVISIONER_SHARED_SECRET + PROVISIONER_TENANT_HOSTNAME
docker compose -f provisioner-compose.yml up -d
'
```

## 7 — Bring each tenant container back

For each tenant directory under `/opt/bitcoiners-dca/tenants/`:

```bash
ssh root@${NEW_IP} '
for t in /opt/bitcoiners-dca/tenants/*/; do
  echo "Starting $t"
  cd "$t" && docker compose up -d
done
'
```

Wait ~30s and confirm both containers per tenant are `Up`:
```bash
ssh root@${NEW_IP} 'docker ps --format "{{.Names}} {{.Status}}" | grep bitcoiners-dca'
```

## 8 — Repoint DNS

In Cloudflare → DNS for `bitcoiners.ae`, change the A records for the
Hetzner-routed subdomains to the new IP:

- `<tenant-id>.tenants.bitcoiners.ae` → ${NEW_IP}

For prod (after 2026-05-15 cutover), `app.bitcoiners.ae` is on Vercel
not Hetzner, so it doesn't need to change.

TTL = 60s typically; expect ~2 min for global propagation.

## 9 — Verify each tenant cycle resumes

```bash
ssh root@${NEW_IP} 'docker logs --tail 30 bitcoiners-dca-<tenant>-daemon 2>&1 | grep -E "cycle|funding|heartbeat"'
```

Each tenant should be back to its normal cron rhythm within 1 hour of
its next scheduled cycle.

## 10 — Update memory + team-feed

```bash
bash infra/team_feed.sh forge "URGENT DR: restored Hetzner prod to ${NEW_IP} from <DATE> backup. RTO ~XX min. Tenants: <list>. DNS repointed."
```

Update `memory/prod_topology_post_cutover.md` with the new IP.

---

## Things that go wrong (and what to do)

- **Caddy fails to bind 80/443** — old container is still running on the
  ghost box (Hetzner sometimes leaves zombie images). `docker stop
  caddy && docker rm caddy` then re-`docker run`.

- **Tenant container starts but `/healthz` returns 503 forever** — the
  `DCA_SECRETS_KEY` Fernet key in `.env` doesn't match the encrypted
  rows in `data/dca.db`. The backup tarball must include BOTH the .env
  AND the dca.db from the same point in time. If they're mismatched
  (e.g. tarball is from before a key rotation), restore from an older
  backup OR recreate tenant from scratch via provisioner.

- **License signing key mismatch** — `/etc/bitcoiners-dca/keys/
  license_signing.pem` is in the tarball. If the bot image is also
  newer than the .pem, the embedded public key may not match. Rotation
  rule per `feedback_license_keypair`: keep .pem + LICENSE_PUBLIC_KEY_HEX
  in sync; for DR, prefer rolling back the bot image to match the .pem.

- **Provisioner can't reach tenants-lxc** — DR is on Hetzner, not
  tenants-lxc. provisioner_service expects host-mode networking for
  the tenants/ volume. Should work because everything is local to the
  Hetzner box.

## After DR is stable

1. Decommission the old Hetzner box if recoverable (don't keep two
   billing instances).
2. Re-run the backup smoke test: `ssh dockers-lxc /usr/local/sbin/restore_drill_weekly.sh`
3. Update Kuma monitors to point at the new IP if any health-check
   monitors are IP-pinned (they should be domain-pinned but verify).
4. Schedule a postmortem: what caused the failure, what's our new RPO
   target, do we need a hot standby vs continuing with cold-restore?
