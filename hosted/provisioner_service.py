"""
Provisioner microservice — HTTP front-end for hosted/provision.sh.

Runs as a systemd unit on the dockers-LXC host, listening on the shared
Docker network at `provisioner:8500`. Bitcoiners-app (running on the same
host, same network) posts here to spawn a per-tenant DCA bot stack.

This service is intentionally tiny:
  - One POST endpoint per lifecycle action
  - Token-based auth (shared secret in env)
  - Subprocess-runs the existing bash scripts; no business logic here

Why a separate service:
  - bitcoiners-app does not need access to the Docker socket
  - the license-signing private key lives on the host (this process), never
    in bitcoiners-app
  - provision/suspend/destroy can be rate-limited and audited centrally
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, EmailStr, Field

# ─── Config ──────────────────────────────────────────────────────────────

SHARED_SECRET = os.environ.get("PROVISIONER_SHARED_SECRET", "")
HOSTED_DIR = Path(os.environ.get("PROVISIONER_HOSTED_DIR", "/opt/bitcoiners-dca/hosted"))
PROVISION_SCRIPT = HOSTED_DIR / "provision.sh"
TENANTS_DIR = Path(os.environ.get("PROVISION_BASE_DIR", "/opt/bitcoiners-dca")) / "tenants"

TENANT_ID_RE = re.compile(r"^[a-z0-9-]{3,40}$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("provisioner")

app = FastAPI(title="bitcoiners-dca provisioner", version="1.0.0")

# ─── Auth ────────────────────────────────────────────────────────────────

def _require_secret(x_provisioner_secret: Optional[str]) -> None:
    if not SHARED_SECRET:
        raise HTTPException(500, "PROVISIONER_SHARED_SECRET not set on the service")
    if x_provisioner_secret != SHARED_SECRET:
        raise HTTPException(401, "bad shared secret")


# ─── Models ──────────────────────────────────────────────────────────────

class ProvisionRequest(BaseModel):
    tenant_id: str = Field(..., min_length=3, max_length=40)
    customer_email: EmailStr
    tier: str = Field(..., pattern="^(pro|business)$")


class ProvisionResponse(BaseModel):
    tenant_id: str
    container_name: str
    internal_host: str
    internal_port: int
    license_token: str


class LifecycleRequest(BaseModel):
    tenant_id: str = Field(..., min_length=3, max_length=40)


# ─── Endpoints ───────────────────────────────────────────────────────────

@app.get("/healthz")
def healthz() -> dict:
    return {
        "ok": True,
        "hosted_dir_exists": HOSTED_DIR.is_dir(),
        "provision_script_exists": PROVISION_SCRIPT.is_file(),
        "tenants_dir": str(TENANTS_DIR),
    }


@app.post("/provision", response_model=ProvisionResponse)
def provision(
    body: ProvisionRequest,
    x_provisioner_secret: Optional[str] = Header(default=None),
) -> ProvisionResponse:
    _require_secret(x_provisioner_secret)
    if not TENANT_ID_RE.match(body.tenant_id):
        raise HTTPException(400, "tenant_id must be lowercase alphanumeric + dashes")

    log.info(f"PROVISION tenant={body.tenant_id} email={body.customer_email} tier={body.tier}")

    cmd = [
        "bash",
        str(PROVISION_SCRIPT),
        body.tenant_id,
        body.customer_email,
        body.tier,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        log.error("provision.sh timed out")
        raise HTTPException(504, "provisioning script timed out")
    except subprocess.CalledProcessError as e:
        log.error(f"provision.sh exit {e.returncode}: {e.stderr}")
        raise HTTPException(500, f"provisioning failed: {e.stderr.strip()[:500]}")

    # Parse the printed output for port + license. provision.sh prints:
    #   "    Dashboard port: ${dash_port}"
    #   "    License: tier=…"  (no token in output — pull from config.yaml)
    dash_port = _parse_port(result.stdout)
    license_token = _read_license_token(body.tenant_id)
    if not dash_port:
        raise HTTPException(500, "provision script did not print a dashboard port")
    if not license_token:
        raise HTTPException(500, "could not read license token from tenant config")

    # Bring up the tenant compose stack
    tenant_dir = TENANTS_DIR / body.tenant_id
    try:
        subprocess.run(
            ["docker", "compose", "up", "-d"],
            cwd=tenant_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.CalledProcessError as e:
        log.error(f"docker compose up failed: {e.stderr}")
        raise HTTPException(500, f"compose up failed: {e.stderr.strip()[:500]}")

    return ProvisionResponse(
        tenant_id=body.tenant_id,
        container_name=f"bitcoiners-dca-{body.tenant_id}-dashboard",
        internal_host=f"bitcoiners-dca-{body.tenant_id}-dashboard",
        internal_port=8000,
        license_token=license_token,
    )


@app.post("/suspend")
def suspend(
    body: LifecycleRequest,
    x_provisioner_secret: Optional[str] = Header(default=None),
) -> dict:
    _require_secret(x_provisioner_secret)
    tenant_dir = TENANTS_DIR / body.tenant_id
    if not tenant_dir.is_dir():
        raise HTTPException(404, f"tenant {body.tenant_id} not provisioned")
    subprocess.run(
        ["docker", "compose", "stop"],
        cwd=tenant_dir,
        check=True,
        capture_output=True,
        timeout=60,
    )
    log.info(f"SUSPEND tenant={body.tenant_id}")
    return {"tenant_id": body.tenant_id, "status": "suspended"}


@app.post("/resume")
def resume(
    body: LifecycleRequest,
    x_provisioner_secret: Optional[str] = Header(default=None),
) -> dict:
    _require_secret(x_provisioner_secret)
    tenant_dir = TENANTS_DIR / body.tenant_id
    if not tenant_dir.is_dir():
        raise HTTPException(404, f"tenant {body.tenant_id} not provisioned")
    subprocess.run(
        ["docker", "compose", "start"],
        cwd=tenant_dir,
        check=True,
        capture_output=True,
        timeout=60,
    )
    log.info(f"RESUME tenant={body.tenant_id}")
    return {"tenant_id": body.tenant_id, "status": "running"}


@app.post("/destroy")
def destroy(
    body: LifecycleRequest,
    x_provisioner_secret: Optional[str] = Header(default=None),
) -> dict:
    """
    Tear down a tenant's containers. Does NOT delete tenant data — that
    requires a separate manual step so a billing dispute can't accidentally
    wipe a customer's trade history.
    """
    _require_secret(x_provisioner_secret)
    tenant_dir = TENANTS_DIR / body.tenant_id
    if not tenant_dir.is_dir():
        raise HTTPException(404, f"tenant {body.tenant_id} not provisioned")
    subprocess.run(
        ["docker", "compose", "down"],
        cwd=tenant_dir,
        check=True,
        capture_output=True,
        timeout=120,
    )
    log.info(f"DESTROY tenant={body.tenant_id} (data preserved at {tenant_dir})")
    return {"tenant_id": body.tenant_id, "status": "destroyed", "data_preserved_at": str(tenant_dir)}


# ─── Helpers ─────────────────────────────────────────────────────────────

def _parse_port(stdout: str) -> Optional[int]:
    for line in stdout.splitlines():
        if "Dashboard port:" in line:
            try:
                return int(line.split("Dashboard port:")[1].strip())
            except (ValueError, IndexError):
                pass
    return None


def _read_license_token(tenant_id: str) -> Optional[str]:
    """Lift the license.key from the rendered config.yaml."""
    cfg_path = TENANTS_DIR / tenant_id / "config" / "config.yaml"
    if not cfg_path.is_file():
        return None
    try:
        for line in cfg_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("key:"):
                # value may be quoted
                raw = line.split(":", 1)[1].strip()
                return raw.strip('"').strip("'") or None
    except OSError:
        return None
    return None
