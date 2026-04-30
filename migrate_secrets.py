#!/usr/bin/env python3
"""
One-time migration: lift sensitive values from .env (and mcp/.env) into the
macOS keychain, then rewrite the .env files without those values.

Idempotent: re-running just refreshes keychain entries from whatever is left
in .env (typically nothing) and leaves clean .env alone.

Run:
  python3 migrate_secrets.py
"""
from __future__ import annotations

import pathlib
import sys

import secrets_helper as sh

ROOT = pathlib.Path(__file__).resolve().parent
ENV_FILES = [ROOT / ".env", ROOT / "mcp" / ".env"]


def parse_env(path: pathlib.Path) -> list[tuple[str, str, str]]:
    """Returns list of (key, value, raw_line). Comment / blank lines have empty key."""
    out: list[tuple[str, str, str]] = []
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            out.append(("", "", raw))
            continue
        k, v = line.split("=", 1)
        out.append((k.strip(), v.strip(), raw))
    return out


def migrate_file(path: pathlib.Path) -> tuple[int, int]:
    """Move sensitive entries into keyring, rewrite the file without them.
    Returns (moved_count, kept_count)."""
    rows = parse_env(path)
    if not rows:
        return (0, 0)

    moved = 0
    kept_lines: list[str] = []
    for key, value, raw in rows:
        if key in sh.SENSITIVE and value:
            sh.set_secret(key, value)
            moved += 1
            print(f"  → keychain: {key}")
        else:
            kept_lines.append(raw)

    # Trim duplicate blank lines at end
    while kept_lines and kept_lines[-1].strip() == "":
        kept_lines.pop()
    kept_lines.append("")  # trailing newline

    new_text = "\n".join(kept_lines)
    if moved > 0:
        path.write_text(new_text)
        path.chmod(0o600)
    return (moved, sum(1 for k, _, _ in rows if k))


def main() -> None:
    total_moved = 0
    for f in ENV_FILES:
        if not f.exists():
            print(f"skip (missing): {f}")
            continue
        print(f"processing: {f}")
        moved, _ = migrate_file(f)
        total_moved += moved
        if moved == 0:
            print("  (no sensitive entries to move — file already clean)")

    print()
    stored = sh.list_known_secrets()
    if stored:
        print("Sensitive entries currently in keychain (service=style_config):")
        for n in stored:
            print(f"  · {n}")
    else:
        print("No sensitive entries are stored in the keychain yet.")

    if total_moved == 0 and not stored:
        print()
        print("Nothing migrated and nothing already stored — did you forget to "
              "fill .env first? Or are you running this on a machine without "
              "an OS keyring backend?", file=sys.stderr)


if __name__ == "__main__":
    main()
