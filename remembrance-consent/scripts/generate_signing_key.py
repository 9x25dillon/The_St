#!/usr/bin/env python3
"""Generate an Ed25519 key pair for kernel authorization tokens.

Prints .env lines for the private key (keep it only in the kernel's secret
store) and the public key (share with downstream services, or let them
fetch /.well-known/consent-keys).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from app.consent.tokens import private_pem, public_pem  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--kid", default="kernel-1", help="key id placed in token headers")
    args = parser.parse_args(argv)
    key = Ed25519PrivateKey.generate()
    print(f"REMEMBRANCE_TOKEN_KEY_ID={args.kid}")
    print("REMEMBRANCE_TOKEN_SIGNING_KEY_PEM=" + json.dumps(private_pem(key)))
    print("# public key for downstream verification:")
    print("# " + json.dumps({args.kid: public_pem(key.public_key())}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
