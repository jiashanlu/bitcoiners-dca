#!/usr/bin/env python3
"""
License issuance tooling — Ed25519 signing for bitcoiners-dca Pro/Business tiers.

Three subcommands:

  keygen
    Generate a fresh Ed25519 keypair. Writes private key to a PEM file
    and prints the public hex (paste into core/license.py).

  issue
    Sign a license for a customer. Outputs the base64 token to paste
    into `license.key` in the customer's config.yaml.

  verify
    Decode + verify a token against a public key. Sanity check.

Usage:
  python scripts/generate_license.py keygen --out infra/license_signing_key.pem
  python scripts/generate_license.py issue \\
    --private-key infra/license_signing_key.pem \\
    --customer-id "ben+test@bitcoiners.ae" \\
    --tier pro \\
    --expires 2027-05-12
  python scripts/generate_license.py verify --token <token> --public-key-hex <hex>
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make this script runnable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bitcoiners_dca.core.license import (
    License,
    LicenseError,
    LicenseTier,
    generate_keypair,
    parse_license_token,
    sign_license,
)


def cmd_keygen(args) -> int:
    private_pem, public_hex = generate_keypair()
    out_path = Path(args.out)
    if out_path.exists() and not args.force:
        print(f"ERROR: {out_path} already exists. Pass --force to overwrite.")
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(private_pem)
    out_path.chmod(0o600)
    print(f"Private key written to: {out_path}")
    print(f"Permissions: 600")
    print()
    print("Public key (paste into src/bitcoiners_dca/core/license.py as")
    print("the value of LICENSE_PUBLIC_KEY_HEX, replacing the BOOTSTRAP")
    print("placeholder):")
    print()
    print(f"  {public_hex}")
    return 0


def cmd_issue(args) -> int:
    try:
        tier = LicenseTier(args.tier.lower())
    except ValueError:
        print(f"ERROR: invalid tier {args.tier!r}. Use free|pro|business.")
        return 1
    if tier == LicenseTier.FREE:
        print("Free tier doesn't need a license key — leave license.key blank.")
        return 0

    expires_at = None
    if args.expires:
        try:
            expires_at = datetime.fromisoformat(args.expires).replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"ERROR: --expires must be YYYY-MM-DD or full ISO 8601")
            return 1

    private_pem = Path(args.private_key).read_text()
    lic = License(
        tier=tier,
        customer_id=args.customer_id,
        issued_at=datetime.now(timezone.utc),
        expires_at=expires_at,
        notes=args.notes,
    )
    token = sign_license(lic, private_pem)
    print(f"License for {args.customer_id}")
    print(f"  Tier:       {tier.value}")
    print(f"  Issued at:  {lic.issued_at.isoformat()}")
    print(f"  Expires at: {expires_at.isoformat() if expires_at else 'never'}")
    print(f"  Notes:      {args.notes or '(none)'}")
    print()
    print("Token (paste into the customer's config.yaml under license.key):")
    print()
    print(f"  {token}")
    return 0


def cmd_verify(args) -> int:
    try:
        lic = parse_license_token(args.token, args.public_key_hex)
    except LicenseError as e:
        print(f"INVALID: {e}")
        return 2
    print(f"VALID license:")
    print(f"  Tier:        {lic.tier.value}")
    print(f"  Customer:    {lic.customer_id}")
    print(f"  Issued at:   {lic.issued_at.isoformat()}")
    print(f"  Expires at:  {lic.expires_at.isoformat() if lic.expires_at else 'never'}")
    print(f"  Expired now? {lic.is_expired}")
    print(f"  Notes:       {lic.notes or '(none)'}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().split('\n')[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_keygen = sub.add_parser("keygen", help="Generate a new Ed25519 keypair")
    p_keygen.add_argument("--out", required=True)
    p_keygen.add_argument("--force", action="store_true")
    p_keygen.set_defaults(func=cmd_keygen)

    p_issue = sub.add_parser("issue", help="Sign a license for a customer")
    p_issue.add_argument("--private-key", required=True)
    p_issue.add_argument("--customer-id", required=True)
    p_issue.add_argument("--tier", required=True, choices=["pro", "business"])
    p_issue.add_argument("--expires", help="YYYY-MM-DD (UTC) — omit for never")
    p_issue.add_argument("--notes", default="")
    p_issue.set_defaults(func=cmd_issue)

    p_verify = sub.add_parser("verify", help="Decode + verify a license token")
    p_verify.add_argument("--token", required=True)
    p_verify.add_argument("--public-key-hex", required=True)
    p_verify.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
