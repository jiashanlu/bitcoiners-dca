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
# IP/hostname bitcoiners-app should reach the tenant dashboard on. On a
# dedicated tenants-LXC this is the LXC's LAN IP. Required.
TENANT_HOSTNAME = os.environ.get("PROVISIONER_TENANT_HOSTNAME", "")

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

    # Args are passed as a list (no shell=True), and each arg is already
    # validated upstream: tenant_id by TENANT_ID_RE, customer_email by
    # Pydantic's EmailStr, tier by the {pro,business} switch in provision.sh.
    # No shell interpolation happens at this layer; semgrep's
    # "tainted-env-args" rule is a false positive against this call site.
    cmd = [
        "bash",
        str(PROVISION_SCRIPT),
        body.tenant_id,
        body.customer_email,
        body.tier,
    ]
    try:
        # Args validated upstream; see note above. Suppress semgrep flag at the
        # caller — the rule fires on the first arg reference (line of `cmd,`).
        result = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
            cmd,  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
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

    if not TENANT_HOSTNAME:
        raise HTTPException(500, "PROVISIONER_TENANT_HOSTNAME not configured")

    return ProvisionResponse(
        tenant_id=body.tenant_id,
        container_name=f"bitcoiners-dca-{body.tenant_id}-dashboard",
        # internal_host is the address bitcoiners-app uses to reach the
        # dashboard. On a dedicated tenants-LXC, that's the LXC's LAN IP +
        # the host-bound TENANT_DASH_PORT (not the container's port 8000).
        internal_host=TENANT_HOSTNAME,
        internal_port=dash_port,
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


@app.post("/resign")
def resign(
    body: LifecycleRequest,
    x_provisioner_secret: Optional[str] = Header(default=None),
) -> dict:
    """Re-issue a license token for an existing tenant.

    Used during license-key rotation (see docs/LICENSE_KEY_ROTATION.md).
    Extracts the customer email + tier from the tenant's current config,
    signs a fresh 1-year token with whichever private key the provisioner
    process holds NOW, and writes the new token back to config.yaml's
    `license.key`. The daemon hot-reloads config on every cycle, so no
    docker restart is needed — the next cycle picks up the new token.

    Idempotent: re-running on the same tenant re-issues again with a
    fresh issued_at timestamp. That's fine; verification is signature-
    based, not history-based.
    """
    _require_secret(x_provisioner_secret)
    if not TENANT_ID_RE.match(body.tenant_id):
        raise HTTPException(400, "tenant_id must be lowercase alphanumeric + dashes")

    tenant_dir = TENANTS_DIR / body.tenant_id
    cfg_path = tenant_dir / "config" / "config.yaml"
    if not cfg_path.is_file():
        raise HTTPException(404, f"tenant {body.tenant_id} not provisioned")

    # Extract customer_email from the `# Tenant: <id> · Customer: <email>`
    # comment provision.sh writes at the top of every config.yaml. Failing
    # to find it is fatal — without an email we'd sign a token to "unknown"
    # which the bot would reject as malformed.
    customer_email: Optional[str] = None
    tier: Optional[str] = None
    try:
        contents = cfg_path.read_text()
    except OSError as e:
        raise HTTPException(500, f"cannot read tenant config: {e}")

    customer_match = re.search(
        r"#\s*Tenant:\s*\S+\s*·\s*Customer:\s*(\S+)", contents
    )
    if customer_match:
        customer_email = customer_match.group(1).strip()
    # Tier lives under the `license:` block; conservative regex grabs the
    # first `tier: <value>` line.
    tier_match = re.search(r"^\s*tier:\s*(\w+)\s*$", contents, re.MULTILINE)
    if tier_match:
        tier = tier_match.group(1).strip()

    if not customer_email or not tier:
        raise HTTPException(
            500,
            "cannot parse customer_email or tier from tenant config; "
            "rotation needs manual intervention",
        )
    if tier not in ("pro", "business"):
        raise HTTPException(400, f"tier {tier!r} not signable (only pro/business)")

    private_key_path = os.environ.get("PROVISION_PRIVATE_KEY", "")
    if not private_key_path or not Path(private_key_path).is_file():
        raise HTTPException(500, "PROVISION_PRIVATE_KEY env var missing or path invalid")

    # 1-year expiry, matching provision.sh.
    expires_iso = subprocess.check_output(
        ["bash", "-c", "date -u -d '+1 year' +%Y-%m-%d 2>/dev/null || date -u -v+1y +%Y-%m-%d"],
        text=True,
    ).strip()

    cmd = [
        "python3",
        str(HOSTED_DIR.parent / "scripts" / "generate_license.py"),
        "issue",
        "--private-key", private_key_path,
        "--customer-id", customer_email,
        "--tier", tier,
        "--expires", expires_iso,
        "--notes", f"resigned by /resign endpoint",
    ]
    try:
        result = subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
            cmd,  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
            capture_output=True, text=True, check=True, timeout=30,
        )
    except subprocess.CalledProcessError as e:
        raise HTTPException(500, f"generate_license.py failed: {e.stderr.strip()[:500]}")
    # Token is the last non-blank line of stdout (provision.sh uses the
    # same parsing trick).
    lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    if not lines:
        raise HTTPException(500, "generate_license.py produced no output")
    new_token = lines[-1]

    # Write the new token back. Replace the existing `key:` line under
    # the `license:` block. Bounded to one substitution to avoid clobbering
    # any future nested `key:` (e.g. exchange API keys).
    new_contents, n_subs = re.subn(
        r"(^\s*license:\s*\n(?:.*\n)*?\s*key:\s*)\".*?\"",
        r'\1"' + new_token + '"',
        contents,
        count=1,
        flags=re.MULTILINE,
    )
    if n_subs != 1:
        raise HTTPException(
            500,
            "could not locate license.key in tenant config; manual edit required",
        )
    try:
        cfg_path.write_text(new_contents)
    except OSError as e:
        raise HTTPException(500, f"cannot write tenant config: {e}")

    log.info(f"RESIGN tenant={body.tenant_id} tier={tier} customer={customer_email}")
    return {
        "tenant_id": body.tenant_id,
        "status": "resigned",
        "tier": tier,
        "customer_email": customer_email,
        "expires_iso": expires_iso,
        "token": new_token,
    }


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
