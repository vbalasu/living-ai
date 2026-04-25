"""Interactive prompts. Stdlib only."""
from __future__ import annotations

import getpass
import re
import sys


def ask(label: str, default: str | None = None, secret: bool = False,
        validate=None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        prompt = f"{label}{suffix}: "
        try:
            value = (getpass.getpass(prompt) if secret else input(prompt)).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            sys.exit(1)
        if not value and default is not None:
            value = default
        if not value:
            print("  (required)")
            continue
        if validate:
            err = validate(value)
            if err:
                print(f"  {err}")
                continue
        return value


def ask_yn(label: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            v = input(f"{label} {hint}: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            sys.exit(1)
        if not v:
            return default
        if v in ("y", "yes"):
            return True
        if v in ("n", "no"):
            return False


def validate_host(value: str) -> str | None:
    if not value.startswith("https://"):
        return "Host must start with https://"
    if " " in value:
        return "Host must not contain spaces"
    return None


def validate_pat(value: str) -> str | None:
    if not value.startswith("dapi") and not value.startswith("dkea"):
        return "PAT should start with 'dapi' or 'dkea'"
    if len(value) < 30:
        return "PAT looks too short"
    return None


def validate_identifier(value: str) -> str | None:
    if not re.fullmatch(r"[a-z0-9_]+", value):
        return "Use lowercase letters, digits, and underscores only"
    return None


def validate_app_name(value: str) -> str | None:
    if not re.fullmatch(r"[a-z0-9-]+", value):
        return "Use lowercase letters, digits, and hyphens only"
    return None


def validate_telegram_token(value: str) -> str | None:
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", value):
        return "Telegram bot tokens look like '123456:ABC-DEF...'"
    return None
