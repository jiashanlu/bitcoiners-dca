# Dockerfile non-root hardening plan (#150)

Status: drafted 2026-05-15 — needs Ben review before merge.

## Goal

Run the bitcoiners-dca bot container as a non-root user (defense-in-depth).
If a dependency or exchange-adapter vulnerability is ever exploited, the
attacker is confined to a non-root uid inside the container instead of
having root + docker-socket access on the tenants host.

## Why this is non-trivial

The current `Dockerfile` runs everything as root. Production tenants
bind-mount three directories from the host:

```
/opt/bitcoiners-dca/tenants/<id>/config   →  /app/config  (ro)
/opt/bitcoiners-dca/tenants/<id>/data     →  /app/data    (rw — SQLite event log)
/opt/bitcoiners-dca/tenants/<id>/reports  →  /app/reports (rw — tax CSV exports)
```

On the host, these directories are currently owned by `root:root`.
Switching the container user to a non-root uid (say `dca:dca` with
uid 1001/gid 1001) breaks SQLite immediately:

```
sqlite3.OperationalError: attempt to write a readonly database
```

…because the bind-mounted `/app/data` is still root-owned at the host
filesystem layer, and the container user cannot write to it.

## Two viable approaches

### Approach A — entrypoint chown (recommended)

Image layout:

```dockerfile
# ...existing build steps unchanged...

# Create non-root user with a stable uid/gid we can match on the host.
RUN groupadd -r -g 1001 dca && \
    useradd  -r -g dca -u 1001 -d /app -s /usr/sbin/nologin dca

# gosu is a static drop-priv tool — smaller and safer than `su` for
# Dockerfiles. Used by the entrypoint after chowning bind mounts.
RUN apt-get update && \
    apt-get install -y --no-install-recommends gosu && \
    rm -rf /var/lib/apt/lists/*

COPY hosted/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# NOTE: stay USER root through the entrypoint — it drops privs after
# the chown. ENTRYPOINT becomes the chown+gosu shim.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["bitcoiners-dca", "--help"]
```

```bash
#!/bin/bash
# hosted/entrypoint.sh — runs as root, chowns mounts, drops privs.
set -e
chown -R dca:dca /app/data /app/reports 2>/dev/null || true
# config stays root-owned ro; we don't write to it from the daemon.
exec gosu dca:dca "$@"
```

**Pros**
- Idempotent: re-chowning at every startup is cheap (~ms per file).
- Works with existing bind-mount paths — no host-side changes needed.
- Drops privileges atomically before any business logic runs.

**Cons**
- Entrypoint runs briefly as root. Mitigation is small surface area
  (chown + gosu exec, 2 lines).
- Adds `gosu` to the image (~2MB).

### Approach B — host-side chown + USER directly

Skip the entrypoint shim. Just:

```dockerfile
RUN groupadd -r -g 1001 dca && useradd -r -g dca -u 1001 -d /app dca
USER dca
```

…and chown the host directories once during provisioning:

```bash
# In hosted/provision.sh after `docker compose up -d`:
chown -R 1001:1001 "${TENANT_DATA_DIR}/data" "${TENANT_DATA_DIR}/reports"
```

**Pros**
- Cleaner Dockerfile, no entrypoint indirection.
- Image stays minimal (no gosu).

**Cons**
- One-time host-side migration: every existing tenant directory must
  be chowned, or the daemon crashes on next restart with
  `OperationalError: attempt to write a readonly database`.
- Provisioner must be updated to chown at creation AND on resume.
- If anyone manually edits files on the host, they'll re-introduce
  root ownership. A surprising failure mode.

## Recommendation

**Approach A** — entrypoint shim. Per-startup chown is cheap and
self-healing; no out-of-band host migration needed; matches how nearly
every off-the-shelf "run-as-non-root" Docker image works (Postgres,
Redis, MariaDB all do this).

## Rollout

1. Ship Approach A on `dev` first; verify the home dev tenant still
   runs cycles correctly after the swap.
2. Smoke test the data-dir chown path: `docker exec -u root <container>
   touch /app/data/probe && docker exec <container> stat /app/data/probe`
   → should show `1001:1001`.
3. Manually recreate the home dev tenant once (`docker compose up -d
   --force-recreate`) so the new entrypoint runs end-to-end.
4. Promote to `main`; CI ships to Hetzner. Existing prod tenants stay
   on their currently-running image until operator-initiated recreate
   (same convention as every other image push). When Ben (or Stripe
   resume) recreates a tenant, it picks up the hardened image.

## Provisioner Dockerfile

Intentionally left as root. Rationale:

The provisioner mounts `/var/run/docker.sock` so it can `docker compose
up` tenant stacks. Anything that can talk to the docker socket is
effectively root on the host (it can launch privileged containers).
Switching the provisioner to a non-root user is security theater
unless we also remove socket access — which would defeat its purpose.

Worth doing later: switch the provisioner to a **socket-proxy** model
(e.g. tecnativa/docker-socket-proxy) so it only sees a narrowed
container-management API. That's a separate, bigger piece of work.
