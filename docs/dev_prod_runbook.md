# Dev / Prod runbook — bitcoiners-dca + bitcoiners-app

Current as of 2026-05-15 (post-Vercel-cutover, post-Hetzner-migration).

## Topology

| Domain | Host | Branch | What runs there |
|---|---|---|---|
| `bitcoiners.ae` (marketing) | Vercel project `bitcoiners-ae` | main | Next.js static site |
| `app.bitcoiners.ae` (webapp PROD) | Vercel project `bitcoiners-app` | main | Next.js webapp + Neon DB |
| `provisioner.bitcoiners.ae` | Hetzner CCX13 (178.105.66.56) | main | FastAPI provisioner behind Caddy |
| Tenant containers (PROD) | Hetzner CCX13 | (image only) | per-customer bot stacks |
| `dev-app.bitcoiners.ae` | home dockers-LXC (192.168.4.151:3197) | dev | dev Next.js webapp |
| Dev tenant containers | home tenants-LXC (192.168.4.160) | dev | replicated benbois session |

## How code flows from git to running container

### bitcoiners-app (Next.js)

`git push origin main` → Vercel auto-deploys to `app.bitcoiners.ae`.

`git push origin dev` → Gitea CI rebuilds the `bitcoiners-app:dev` Docker image on home dockers-LXC and recreates the `bitcoiners-app-dev` container (port 3197). `dev-app.bitcoiners.ae` serves it via CF Tunnel.

There's still a `bitcoiners-app:main` container on home (port 3097) as warm fallback for the Vercel cutover (keep until 2026-05-16 then deprecate).

### bitcoiners-dca (Python bot)

`git push origin main` → Gitea CI (`.gitea/workflows/build-image.yml`):
1. Build `bitcoiners-dca:latest` + `bitcoiners-provisioner:latest`.
2. SSH to Hetzner (178.105.66.56), `docker load` both images.
3. `cd /opt/bitcoiners-dca && git fetch && git reset --hard origin/main` so the host repo (provision.sh, compose templates) matches the build.
4. `docker compose up -d --force-recreate` the provisioner container.
5. Existing tenant containers stay on their currently-running image — they're not auto-recreated. Operator does that explicitly.

`git push origin dev` → same workflow, target host is home tenants-LXC (192.168.4.160), git branch is `dev`.

## Common operations

### Deploy a normal change

Commit on `dev`, push, watch CI. If green and not touching billing/auth/DNS, fast-forward to `main`:

```bash
cd ~/.openclaw/workspace/bitcoiners-dca
# (work on dev branch)
git push origin dev
# wait for green CI on dev
git checkout main && git merge --ff-only dev && git push origin main
```

The `feedback_auto_merge_green_prs` rule applies: green CI + no HIGH+ security findings + no auth/billing/DNS touched → ok to fast-forward without asking.

### Recreate a prod tenant after a new image lands

CI never auto-recreates customer bots. To recreate Ben's benbois tenant on Hetzner after a code change:

```bash
ssh -i ~/.openclaw/workspace/infra/jiashan_ai_ed25519 root@178.105.66.56 \
  'cd /opt/bitcoiners-dca/tenants/benbois-ae0e0001 && docker compose up -d --force-recreate'
```

Brief downtime (~5s daemon + dashboard restart). Trade history + config preserved (volume-mounted).

### Test a change on dev before promoting

1. Push to `dev` branch only.
2. Home CI rebuilds → home dev tenant gets new image.
3. Pause prod tenant from `app.bitcoiners.ae/dca/console` (Ben must do this manually).
4. Resume dev tenant from `dev-app.bitcoiners.ae/dca/console`.
5. Test. Pause dev when done; resume prod.
6. Fast-forward `main` to release.

### Rollback Vercel deploy

```bash
# List recent deployments
curl -H "Authorization: Bearer $VERCEL_TOKEN" \
  "https://api.vercel.com/v6/deployments?projectId=prj_UfrowWfi8JnimFmGO548T3Lxd8Vu&limit=5"

# Promote an earlier deployment to production
curl -X POST -H "Authorization: Bearer $VERCEL_TOKEN" \
  "https://api.vercel.com/v13/deployments/<dep_id>/promote"
```

### Rollback bitcoiners-dca on Hetzner

CI ships `:latest`. Rollback = redeploy an earlier image. Quick path:

```bash
# Re-trigger CI from a known-good commit
cd ~/.openclaw/workspace/bitcoiners-dca
git push origin <known-good-sha>:main --force-with-lease  # nuclear; needs Ben's OK
```

Safer path: keep the previous `:git-XXXXXXX` tag on Hetzner.

```bash
ssh root@178.105.66.56 'docker tag bitcoiners-dca:git-XXXXXXX bitcoiners-dca:latest && \
  cd /opt/bitcoiners-dca/tenants/<tenant_id> && docker compose up -d --force-recreate'
```

### Rollback app.bitcoiners.ae DNS to home

CF DNS record id `3e2ddc1ea444bf0dbec1f3a0c124d752` was originally `CNAME 53bbb430-...cfargotunnel.com, proxied=true`. To revert:

```bash
curl -X PUT -H "Authorization: Bearer $CF_API_TOKEN" \
  "https://api.cloudflare.com/client/v4/zones/87c401019a01a79a454cd337c80551ad/dns_records/3e2ddc1ea444bf0dbec1f3a0c124d752" \
  -d '{"type":"CNAME","name":"app.bitcoiners.ae","content":"53bbb430-5baf-4a6f-9e89-ebb1843aaf2d.cfargotunnel.com","proxied":true,"ttl":1}'
```

Home `bitcoiners-app:3097` (warm fallback) takes traffic immediately.

## Secrets

- All credentials live in `~/.openclaw/workspace/infra/secrets.env`.
- Vercel env vars are managed via the Vercel dashboard (or API with `VERCEL_TOKEN`).
- Gitea secrets (`DEPLOY_SSH_KEY`) are managed in the Gitea repo settings UI.
- Never commit secrets. The repo has gitleaks pre-commit + Gitea CI semgrep + trivy-fs scans.

## Network notes

- Caddy on Hetzner serves on the `tenants` docker network. The provisioner container must attach to BOTH `hosted_default` (its compose default) AND `tenants` for `provisioner.bitcoiners.ae` to reach it. The compose file (`hosted/docker-compose.provisioner.yml`) handles this; both hosts pre-create the `tenants` network in their bootstrap script.
- Home tenants-LXC firewall (ufw) restricts ports 8500 + 8100–8999 to bitcoiners-app's source IP (192.168.4.151) only.
- Vercel functions reach Hetzner provisioner via the public `https://provisioner.bitcoiners.ae` URL (Caddy + Let's Encrypt + `PROVISIONER_SHARED_SECRET` HMAC).
