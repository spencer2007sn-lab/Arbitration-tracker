#!/usr/bin/env python3
"""Test Kalshi API credentials and connectivity.

Usage:
    uv run python scripts/test_kalshi_connection.py

Reads KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH from .env (or environment),
signs a request with RSA-PSS, and attempts to fetch one market from Kalshi.
Reports success or a clear actionable error message.
"""
from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv
import os

load_dotenv()

BASE_URL = "https://api.kalshi.com/trade-api/v2"


def _sign(private_key, method: str, path: str) -> tuple[str, str]:
    ts = str(int(time.time() * 1000))
    message = (ts + method.upper() + path).encode()
    sig = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return ts, base64.b64encode(sig).decode()


def main() -> None:
    api_key = os.environ.get("KALSHI_API_KEY", "").strip()
    pem_path_str = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "kalshi_private.pem").strip()
    pem_path = Path(pem_path_str)

    print("\nKalshi API Connection Test")
    print("=" * 40)

    errors: list[str] = []

    # --- Check API key ---
    if not api_key:
        errors.append("KALSHI_API_KEY is not set in .env")
        print("✗  KALSHI_API_KEY: not set")
    elif api_key == "your-api-key-uuid-here":
        errors.append("KALSHI_API_KEY still has the placeholder value")
        print("✗  KALSHI_API_KEY: still has placeholder value")
    else:
        print(f"✓  KALSHI_API_KEY: {api_key[:8]}…")

    # --- Check PEM file ---
    if not pem_path.exists():
        errors.append(
            f"PEM file not found: '{pem_path}'\n"
            "  → Run: uv run python scripts/generate_kalshi_key.py"
        )
        print(f"✗  PEM file: not found at '{pem_path}'")
    else:
        print(f"✓  PEM file: found at '{pem_path}'")
        try:
            pem_bytes = pem_path.read_bytes()
            private_key = serialization.load_pem_private_key(pem_bytes, password=None)
            print("✓  PEM file: loaded and parsed OK")
        except Exception as e:
            errors.append(f"PEM file could not be parsed: {e}")
            print(f"✗  PEM file: parse error — {e}")
            private_key = None

    if errors:
        print("\nFailed — fix the following before running the scanner:\n")
        for i, err in enumerate(errors, 1):
            print(f"  {i}. {err}")
        print()
        sys.exit(1)

    # --- Attempt live API call ---
    print("\nAttempting live API call…")
    path = "/trade-api/v2/markets"
    ts, sig = _sign(private_key, "GET", path)
    headers = {
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
    }

    try:
        resp = httpx.get(
            f"{BASE_URL}/markets",
            params={"limit": 1, "status": "open"},
            headers=headers,
            timeout=10.0,
        )
    except httpx.ConnectError as e:
        print(f"✗  Network error: {e}")
        print("   Check your internet connection and that api.kalshi.com is reachable.")
        sys.exit(1)
    except httpx.TimeoutException:
        print("✗  Request timed out (10s). Kalshi may be slow or unreachable.")
        sys.exit(1)

    if resp.status_code == 200:
        data = resp.json()
        market_count = len(data.get("markets", []))
        print(f"✓  Kalshi connection OK — HTTP 200, {market_count} market(s) in response")
        print("\nYour credentials are working. You can now start the scanner:")
        print("   uv run uvicorn arb_scanner.main:app --host 0.0.0.0 --port 8000\n")
    elif resp.status_code == 401:
        print(f"✗  HTTP 401 Unauthorized")
        print("   Possible causes:")
        print("   • The public key is not registered on Kalshi yet")
        print("     → Go to https://kalshi.com/settings/api and add it")
        print("   • The KALSHI_API_KEY doesn't match the registered key")
        print("   • The PEM file was regenerated after registering the public key")
        print("     → Re-run scripts/generate_kalshi_key.py and re-register")
        sys.exit(1)
    elif resp.status_code == 403:
        print(f"✗  HTTP 403 Forbidden — your account may not have API access enabled")
        sys.exit(1)
    else:
        print(f"✗  Unexpected HTTP {resp.status_code}: {resp.text[:200]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
