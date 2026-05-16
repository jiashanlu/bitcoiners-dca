# bitcoiners-dca — self-hostable DCA bot for UAE Bitcoiners
FROM python:3.11-slim

LABEL org.opencontainers.image.source=https://github.com/jiashanlu/bitcoiners-dca
LABEL org.opencontainers.image.description="Self-hosted DCA bot for UAE Bitcoiners"
LABEL org.opencontainers.image.licenses=MIT

WORKDIR /app

# System deps: ca-certificates for HTTPS exchange APIs; wget for the
# dashboard healthcheck used by docker-compose; gosu to drop privileges
# from the entrypoint after the chown step (small static binary, ~2MB).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates wget gosu \
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

# Entrypoint shim that chowns bind mounts then drops privs to `dca`.
COPY hosted/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Chown /app to the dca user. Without this, runtime writes to
# /app/.bitcoiners-dca-cache (Path.home() resolves to /app for the dca
# user per useradd -d /app above) hit PermissionError because all the
# COPY layers above ran as root. The entrypoint chowns bind-mounted
# subdirs (data, config, reports) on each start, but the top-level
# /app directory + image-layer files need a build-time chown.
RUN chown -R dca:dca /app

# Mount points: /app/config holds config.yaml; /app/data holds the
# SQLite event log; /app/reports holds generated tax CSVs.
VOLUME ["/app/config", "/app/data", "/app/reports"]

# Default to dry-run for safety; user overrides via env or by setting
# `dry_run: false` in their config.yaml.
ENV BITCOINERS_DCA_DRY_RUN=true

# NOTE: stay USER root through the entrypoint — it drops privileges to
# `dca` via gosu after the chown step. Switching USER here would skip
# that chown and break bind-mounted writes on first run.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["--help"]
