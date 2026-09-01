#!/usr/bin/env python3
"""Set the panel's Basic Auth credentials (v1.6). Writes config/auth.yaml
(gitignored) with a pbkdf2 password hash — never the plaintext.

Run from nvr-rk3576:  scripts/set_panel_password.py
"""

import getpass
import sys
from pathlib import Path

import yaml
from werkzeug.security import generate_password_hash

OUT = Path("config/auth.yaml")


def main() -> None:
    username = input("panel username: ").strip()
    if not username:
        print("username cannot be empty", file=sys.stderr)
        return 1
    password = getpass.getpass("panel password: ")
    if not password:
        print("password cannot be empty", file=sys.stderr)
        return 1
    confirm = getpass.getpass("confirm password: ")
    if password != confirm:
        print("passwords do not match", file=sys.stderr)
        return 1
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(
        yaml.safe_dump({"username": username, "password_hash": generate_password_hash(password)}),
        encoding="utf-8",
    )
    print(f"wrote {OUT} (gitignored) — restart the panel to apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
