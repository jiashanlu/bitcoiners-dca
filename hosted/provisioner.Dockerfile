# Provisioner microservice image.
#
# Runs the FastAPI app from hosted/provisioner_service.py. Needs the Docker
# socket bind-mounted at /var/run/docker.sock and the docker CLI installed
# (so it can `docker compose up` on tenant directories).
#
# Build:
#   docker build -f hosted/provisioner.Dockerfile -t bitcoiners-provisioner:latest .
#
# Compose runs it as a sibling of bitcoiners-app on the `bitcoiners-app`
# network — see hosted/docker-compose.provisioner.yml.

FROM python:3.12-slim AS base

# System packages:
#   - docker-ce-cli + docker-compose-plugin: for `docker compose up`
#   - gettext-base:                          envsubst (rendering tenant templates)
#   - iproute2:                              `ss` for port detection in provision.sh
#   - bash:                                  provision.sh uses bashisms
#   - curl/ca-certificates:                  healthcheck + general HTTPS
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl gnupg bash \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
       | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       docker-ce-cli docker-compose-plugin gettext-base iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps:
#   - FastAPI/Uvicorn/Pydantic: the service itself
#   - cryptography:             scripts/generate_license.py imports it via
#                               bitcoiners_dca.core.license; provision.sh
#                               shells out to that script per tenant
#   - pyyaml + jsonschema:      provision.sh doesn't strictly need these,
#                               but importing bitcoiners_dca.core.license
#                               pulls them in transitively
RUN pip install --no-cache-dir \
    "fastapi==0.115.*" \
    "uvicorn[standard]==0.32.*" \
    "pydantic[email]==2.10.*" \
    "cryptography>=42,<46"

COPY hosted/provisioner_service.py /app/provisioner_service.py

EXPOSE 8500

CMD ["uvicorn", "provisioner_service:app", "--host", "0.0.0.0", "--port", "8500"]
