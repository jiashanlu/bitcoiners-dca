# bitcoiners-dca — self-hostable DCA bot for UAE Bitcoiners
FROM python:3.11-slim

LABEL org.opencontainers.image.source=https://github.com/jiashanlu/bitcoiners-dca
LABEL org.opencontainers.image.description="Self-hosted DCA bot for UAE Bitcoiners"
LABEL org.opencontainers.image.licenses=MIT

WORKDIR /app

# System deps: ca-certificates for HTTPS exchange APIs; wget for the dashboard
# healthcheck used by docker-compose.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates wget \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src/ ./src/
COPY config.example.yaml ./

# Upgrade pip + setuptools + wheel BEFORE installing the app — the base
# python:3.11-slim ships older versions with HIGH-severity CVEs
# (jaraco.context path-traversal, wheel privilege-escalation). Pinning
# floors keeps the upgrade reproducible.
RUN pip install --no-cache-dir --upgrade 'pip>=24.3' 'setuptools>=78.0' 'wheel>=0.46.2' \
    && pip install --no-cache-dir -e .

# Mount points: /app/config holds config.yaml (read-only); /app/data holds the
# SQLite event log; /app/reports holds generated tax CSVs.
VOLUME ["/app/config", "/app/data", "/app/reports"]

# Default to dry-run for safety; user overrides via env or by setting
# `dry_run: false` in their config.yaml.
ENV BITCOINERS_DCA_DRY_RUN=true

ENTRYPOINT ["bitcoiners-dca"]
CMD ["--help"]
