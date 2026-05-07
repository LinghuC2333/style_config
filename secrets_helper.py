"""
Secret loader for style_config.

Sensitive values (API keys, passwords) live in the OS keyring (macOS Keychain
on this machine). Non-sensitive config (hosts, ports, prefixes) lives in
.env files alongside the code.

Lookup order for any name:
  1. real environment variable — lets CI / one-off shell exports win
  2. OS keyring under service "style_config"
  3. (caller may supply a fallback default)

`require_secret(name)` exits with a clear message if a sensitive value isn't
found in env or keyring — it never falls back to plaintext .env, so a
sensitive value cannot silently leak through .env.

Run `python3 migrate_secrets.py` once to lift existing .env values into
Keychain. After that, the .env files only hold non-secrets.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

import keyring

KEYRING_SERVICE = "style_config"

# Names that must never fall through to plaintext .env.
SENSITIVE = frozenset({
    "OSS_ACCESS_KEY_ID",
    "OSS_ACCESS_KEY_SECRET",
    "ZENMUX_API_KEY",     # legacy image backend; kept as fallback
    "MOB_AI_API_KEY",     # current image backend (image-* models)
    "SSH_PASSWORD",
    "PG_PASSWORD",
    "STYLE_CONFIG_TOKEN",  # shared bearer token for the public deployment
})


def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    """Env > keyring > default. Returns None if nothing found and no default."""
    v = os.environ.get(name)
    if v:
        return v
    try:
        v = keyring.get_password(KEYRING_SERVICE, name)
    except Exception:
        v = None
    if v:
        return v
    return default


def require_secret(name: str) -> str:
    """Like get_secret but exits if not found. For startup checks."""
    v = get_secret(name)
    if not v:
        sys.exit(
            f"missing secret {name!r}: not in environment, not in keyring "
            f"(service={KEYRING_SERVICE!r}). Run `python3 migrate_secrets.py` "
            f"to load secrets from .env into the keychain, or export it directly."
        )
    return v


def set_secret(name: str, value: str) -> None:
    keyring.set_password(KEYRING_SERVICE, name, value)


def delete_secret(name: str) -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, name)
    except keyring.errors.PasswordDeleteError:
        pass


def list_known_secrets() -> list[str]:
    """Best-effort enumeration of which sensitive names are stored.
    keyring's API doesn't allow enumeration, so we just probe SENSITIVE."""
    return [n for n in sorted(SENSITIVE) if get_secret(n)]
