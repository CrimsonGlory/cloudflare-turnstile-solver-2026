#!/usr/bin/env python3
"""Compute a Chrome extension ID (and manifest key) from an RSA PEM."""

from __future__ import annotations

import argparse
import base64
import hashlib
import subprocess
import sys
from pathlib import Path


def public_spki_der(pem_path: Path) -> bytes:
    return subprocess.check_output(
        ["openssl", "rsa", "-in", str(pem_path), "-pubout", "-outform", "DER"],
        stderr=subprocess.DEVNULL,
    )


def extension_id(pub_der: bytes) -> str:
    digest = hashlib.sha256(pub_der).hexdigest()[:32]
    return "".join(chr(ord("a") + int(ch, 16)) for ch in digest)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pem", type=Path)
    parser.add_argument("--id-only", action="store_true")
    parser.add_argument("--key-only", action="store_true")
    args = parser.parse_args()

    pub = public_spki_der(args.pem)
    ext_id = extension_id(pub)
    key = base64.b64encode(pub).decode("ascii")

    if args.id_only:
        print(ext_id)
    elif args.key_only:
        print(key)
    else:
        print(ext_id)
        print(key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
