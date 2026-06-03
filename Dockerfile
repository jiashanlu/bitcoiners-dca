# bitcoiners-dca — self-hostable DCA bot for UAE Bitcoiners
FROM python:3.11-slim

LABEL org.opencontainers.image.source=https://github.com/jiashanlu/bitcoiners-dca
LABEL org.opencontainers.image.description="Self-hosted DCA bot for UAE Bitcoiners"
LABEL org.opencontainers.image.licenses=MIT

WORKDIR /app

# System deps: ca-certificates for HTTPS exchange APIs; wget for the
# dashboard healthcheck used by docker-compose. (gosu was only needed by
# the old root-entrypoint drop-priv shim; with `USER dca` below the
# container runs non-root from PID 1 and bind-mount ownership is handled
# by the provisioner — see hosted/provision.sh. Task #150.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates wget \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user with stable uid/gid so chowned bind-mount data
# survives image rebuilds and remains readable from host-side tools.
RUN groupadd -r -g 1001 dca && \
    useradd  -r -g dca -u 1001 -d /app -s /usr/sbin/nologin dca

COPY pyproject.toml ./
COPY src/ ./src/
COPY config.example.yaml ./

# Upgrade pip + setuptools + wheel BEFORE installing the app — the base
# python:3.11-slim ships older versions with HIGH-severity CVEs
# (jaraco.context path-traversal, wheel privilege-escalation). Pinning
# floors keeps the upgrade reproducible.
RUN pip install --no-cache-dir --upgrade 'pip>=24.3' 'setuptools>=78.0' 'wheel>=0.46.2' \
    && pip install --no-cache-dir -e .

# Chown /app to the dca user. Without this, runtime writes to
# /app/.bitcoiners-dca-cache (Path.home() resolves to /app for the dca
# user per useradd -d /app above) hit PermissionError because all the
# COPY layers above ran as root. Bind-mounted subdirs (data, config,
# reports) are chowned to 1001:1001 by the provisioner at provision time
# (hosted/provision.sh) — not at container start, since `USER dca` can't
# chown.
RUN chown -R dca:dca /app

# Mount points: /app/config holds config.yaml; /app/data holds the
# SQLite event log; /app/reports holds generated tax CSVs.
VOLUME ["/app/config", "/app/data", "/app/reports"]

# Default to dry-run for safety; user overrides via env or by setting
# `dry_run: false` in their config.yaml.
ENV BITCOINERS_DCA_DRY_RUN=true

# Run non-root from PID 1 (task #150). The container never touches root;
# `docker exec` also lands as `dca`. ENTRYPOINT is the CLI binary directly
# so the tenant compose `command:` (e.g. ["run","--config",...] /
# ["dashboard",...]) concatenates as before. Bind-mount dirs are pre-owned
# 1001:1001 by the provisioner.
USER dca
ENTRYPOINT ["bitcoiners-dca"]
CMD ["--help"]
