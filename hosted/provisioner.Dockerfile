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

# docker CLI + compose plugin (apt-get version is fine for our use)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
       | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       docker-ce-cli docker-compose-plugin gettext-base \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps — keep minimal. We don't import the full bitcoiners-dca
# package here; provision.sh handles the bot-side work.
RUN pip install --no-cache-dir \
    "fastapi==0.115.*" \
    "uvicorn[standard]==0.32.*" \
    "pydantic[email]==2.10.*"

COPY hosted/provisioner_service.py /app/provisioner_service.py

EXPOSE 8500

CMD ["uvicorn", "provisioner_service:app", "--host", "0.0.0.0", "--port", "8500"]
