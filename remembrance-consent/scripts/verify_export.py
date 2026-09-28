#!/usr/bin/env python3
"""Verify an export archive offline: every file against the manifest's
SHA-256, the audit chain from genesis, and (with --keys) the manifest's
Ed25519 signature.

    python scripts/verify_export.py remembrance-export.zip --keys consent-keys.json

--keys takes the JSON served at /.well-known/consent-keys (or any
{"keys": {kid: PEM}} / {kid: PEM} object). Keep a copy of it with the
archive: the signature stays checkable after the service is gone.

Exit status: 0 verified, 1 verification failed, 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives.serialization import load_pem_public_key  # noqa: E402

from app.export.manifest import verify_bundle  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a Remembrance export archive")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--keys", type=Path, help="public keys JSON (from /.well-known/consent-keys)")
    parser.add_argument("--json", action="store_true")
    try:
        args = parser.parse_args(argv)
        data = args.archive.read_bytes()
        keys = None
        if args.keys:
            document = json.loads(args.keys.read_text())
            pems = document.get("keys", document)
            keys = {kid: load_pem_public_key(pem.encode()) for kid, pem in pems.items()}
    except SystemExit:
        return 2
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    result = verify_bundle(data, keys)
    if args.json:
        print(json.dumps(result.to_json(), indent=2))
    else:
        for error in result.errors:
            print(f"FAIL  {error}")
        if result.ok:
            signed = "signature verified" if result.signature_checked else "signature not checked (no --keys)"
            print(f"OK: every file matches the manifest; audit chain intact; {signed}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
