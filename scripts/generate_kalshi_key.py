#!/usr/bin/env python3
"""Generate an RSA-2048 key pair for Kalshi API authentication.

Usage:
    uv run python scripts/generate_kalshi_key.py [--output kalshi_private.pem]

The private key is saved locally (keep it secret, never commit it).
The public key is printed to stdout for you to paste into Kalshi's API settings.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Kalshi RSA-2048 key pair")
    parser.add_argument(
        "--output",
        default="kalshi_private.pem",
        help="Path to save the private key PEM file (default: kalshi_private.pem)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing private key without prompting",
    )
    args = parser.parse_args()

    out_path = Path(args.output)

    if out_path.exists() and not args.force:
        answer = input(f"⚠  '{out_path}' already exists. Overwrite? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

    # Generate RSA-2048 key pair
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    # Serialize private key to PEM (unencrypted — protect it with filesystem permissions)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    out_path.write_bytes(private_pem)
    out_path.chmod(0o600)  # owner read/write only

    # Serialize public key to PEM for Kalshi registration
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    print(f"\n✓  Private key saved to: {out_path}")
    print("   File permissions set to 600 (owner read/write only).")
    print("   Never commit this file — add it to .gitignore.\n")
    print("=" * 60)
    print("Public key to register at https://kalshi.com/settings/api")
    print("=" * 60)
    print(public_pem)
    print("=" * 60)
    print("\nNext steps:")
    print("  1. Go to https://kalshi.com/settings/api")
    print("  2. Click 'Add API Key', paste the public key above")
    print("  3. Copy the API Key ID shown by Kalshi")
    print(f"  4. Add to your .env file:")
    print(f"       KALSHI_API_KEY=<paste-key-id-here>")
    print(f"       KALSHI_PRIVATE_KEY_PATH={out_path.resolve()}")
    print("  5. Test: uv run python scripts/test_kalshi_connection.py\n")


if __name__ == "__main__":
    main()
