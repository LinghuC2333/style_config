#!/usr/bin/env python3
"""
style_config MCP server.

Exposes a small set of CRUD-ish tools over a remote `styles` Postgres table.
The server keeps PG private — it tunnels through SSH to the remote
127.0.0.1:5432 and talks to PG as the `style_config` role.

Tools:
  list_styles()                                — newest first
  get_style(id)
  create_style(name, category, model, prompt, reference_urls?)
  update_style(id, name?, category?, model?, prompt?, reference_urls?)
  delete_style(id)
  set_generated_preview(id, url, appearance)

Non-sensitive config in mcp/.env:
  SSH_HOST=<your-server-ip>
  SSH_USER=root
  PG_DB=style_config
  PG_USER=style_config
  PG_REMOTE_HOST=127.0.0.1          # what PG is bound to on the server
  PG_REMOTE_PORT=5432

Sensitive values live in the OS keychain (see secrets_helper.py / README):
  SSH_PASSWORD                      # or SSH_KEY_PATH=~/.ssh/id_ed25519 in env
  PG_PASSWORD

Run:
  pip install mcp 'psycopg[binary]' sshtunnel 'paramiko<4.0' keyring
  python3 style_config_mcp.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import uuid
from typing import Any, Optional

import psycopg
from mcp.server.fastmcp import FastMCP
from psycopg.rows import dict_row
from psycopg.types.json import Json
from sshtunnel import SSHTunnelForwarder

ROOT = pathlib.Path(__file__).resolve().parent

# secrets_helper sits at the project root, one level up from mcp/.
sys.path.insert(0, str(ROOT.parent))
import secrets_helper as sh  # noqa: E402


def load_env_files() -> None:
    """Read mcp/.env then ../.env (parent project) into os.environ
    if not already set. Lets the same MCP config live next to the server.py."""
    for env_file in [ROOT / ".env", ROOT.parent / ".env"]:
        if not env_file.exists():
            continue
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k and k not in os.environ:
                os.environ[k] = v


load_env_files()


def env(key: str, default: Optional[str] = None) -> str:
    """Required config lookup.
    Sensitive names check env + keychain only, never .env. Non-sensitive
    names use the os.environ already populated by load_env_files()."""
    if key in sh.SENSITIVE:
        return sh.require_secret(key)
    v = os.environ.get(key, default)
    if v is None or v == "":
        sys.exit(f"missing env: {key}")
    return v


VALID_CATEGORIES = {
    "character series illustration",
    "character ep illustration",
    "scene series illustration",
    "scene ep illustration",
}
VALID_MODELS = {
    "openai/gpt-image-2",
    "google/gemini-3.1-flash-image-preview",
    "google/gemini-3-pro-image-preview",
}


# ─── SSH tunnel + PG connection (process-wide, lazy) ───────────────────

_tunnel: Optional[SSHTunnelForwarder] = None
_local_port: Optional[int] = None


def ensure_tunnel() -> int:
    """Open an SSH tunnel to the remote PG once and return the local port.
    Reused for the lifetime of the process."""
    global _tunnel, _local_port
    if _tunnel is not None and _tunnel.is_active:
        return _local_port  # type: ignore[return-value]

    ssh_host = env("SSH_HOST")
    ssh_user = env("SSH_USER")
    # SSH_KEY_PATH wins if set (preferred). Fall back to SSH_PASSWORD which
    # is sensitive and only read from env/keychain — never from .env.
    ssh_key = os.environ.get("SSH_KEY_PATH") or None
    ssh_password = sh.get_secret("SSH_PASSWORD") if not ssh_key else None
    if not ssh_password and not ssh_key:
        sys.exit("missing SSH_KEY_PATH or SSH_PASSWORD (set in env or keychain)")

    remote_host = os.environ.get("PG_REMOTE_HOST", "127.0.0.1")
    remote_port = int(os.environ.get("PG_REMOTE_PORT", "5432"))

    kwargs: dict[str, Any] = {
        "ssh_username": ssh_user,
        "remote_bind_address": (remote_host, remote_port),
        "local_bind_address": ("127.0.0.1", 0),  # any free port
    }
    if ssh_key:
        kwargs["ssh_pkey"] = os.path.expanduser(ssh_key)
    if ssh_password:
        kwargs["ssh_password"] = ssh_password

    _tunnel = SSHTunnelForwarder(ssh_host, **kwargs)
    _tunnel.start()
    _local_port = _tunnel.local_bind_port
    return _local_port


def db_connect() -> psycopg.Connection:
    port = ensure_tunnel()
    return psycopg.connect(
        host="127.0.0.1",
        port=port,
        dbname=env("PG_DB", "style_config"),
        user=env("PG_USER", "style_config"),
        password=env("PG_PASSWORD"),
        row_factory=dict_row,
        connect_timeout=10,
    )


def row_to_dict(row: dict[str, Any] | None) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    out = dict(row)
    # JSONB comes back as Python list/dict already; nothing to do.
    return out


# ─── MCP server ────────────────────────────────────────────────────────

mcp = FastMCP("style_config")


@mcp.tool()
def list_styles() -> list[dict[str, Any]]:
    """List all styles, newest first."""
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM styles ORDER BY created_at DESC")
        return [row_to_dict(r) for r in cur.fetchall()]  # type: ignore[misc]


@mcp.tool()
def get_style(id: str) -> Optional[dict[str, Any]]:
    """Fetch one style by id. Returns null if not found."""
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM styles WHERE id=%s", (id,))
        return row_to_dict(cur.fetchone())


@mcp.tool()
def create_style(
    name: str,
    category: str,
    model: str,
    prompt: str,
    reference_urls: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Create a new style row. `reference_urls` are stored as-is — this
    tool does NOT upload images to OSS; pass URLs that already exist there.

    `category` must be one of:
      character series illustration / character ep illustration /
      scene series illustration   / scene ep illustration
    `model` must be one of:
      openai/gpt-image-2 / google/gemini-3.1-flash-image-preview /
      google/gemini-3-pro-image-preview
    """
    if category not in VALID_CATEGORIES:
        raise ValueError(f"invalid category: {category}")
    if model not in VALID_MODELS:
        raise ValueError(f"invalid model: {model}")
    if not name.strip():
        raise ValueError("name is required")
    if not prompt.strip():
        raise ValueError("prompt is required")
    refs = list(reference_urls or [])
    if len(refs) > 2:
        raise ValueError("at most 2 reference_urls")

    style_id = uuid.uuid4().hex[:12]
    now = time.time()
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO styles(id,name,category,model,prompt,reference_urls,
                                  created_at,updated_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING *""",
            (style_id, name.strip(), category, model, prompt.strip(),
             Json(refs), now, now),
        )
        row = cur.fetchone()
        conn.commit()
    return row_to_dict(row)  # type: ignore[return-value]


@mcp.tool()
def update_style(
    id: str,
    name: Optional[str] = None,
    category: Optional[str] = None,
    model: Optional[str] = None,
    prompt: Optional[str] = None,
    reference_urls: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    """Patch fields on an existing style. Only the fields you pass are
    updated. Returns the updated row, or null if id not found."""
    if category is not None and category not in VALID_CATEGORIES:
        raise ValueError(f"invalid category: {category}")
    if model is not None and model not in VALID_MODELS:
        raise ValueError(f"invalid model: {model}")
    if reference_urls is not None and len(reference_urls) > 2:
        raise ValueError("at most 2 reference_urls")

    sets = []
    args: list[Any] = []
    if name is not None:
        sets.append("name=%s"); args.append(name.strip())
    if category is not None:
        sets.append("category=%s"); args.append(category)
    if model is not None:
        sets.append("model=%s"); args.append(model)
    if prompt is not None:
        sets.append("prompt=%s"); args.append(prompt.strip())
    if reference_urls is not None:
        sets.append("reference_urls=%s"); args.append(Json(list(reference_urls)))
    if not sets:
        return get_style(id)
    sets.append("updated_at=%s"); args.append(time.time())
    args.append(id)

    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE styles SET {', '.join(sets)} WHERE id=%s RETURNING *",
            tuple(args),
        )
        row = cur.fetchone()
        conn.commit()
    return row_to_dict(row)


@mcp.tool()
def delete_style(id: str) -> dict[str, Any]:
    """Delete a style row. Returns {deleted: bool}."""
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM styles WHERE id=%s", (id,))
        deleted = cur.rowcount > 0
        conn.commit()
    return {"deleted": deleted, "id": id}


@mcp.tool()
def set_generated_preview(id: str, url: str, appearance: str) -> Optional[dict[str, Any]]:
    """Record a generated preview against the style. Use this after you've
    rendered a preview elsewhere and uploaded it; this only updates DB.
    Returns the updated row, or null if id not found."""
    if not url.strip() or not appearance.strip():
        raise ValueError("url and appearance are required")
    now = time.time()
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE styles
               SET generated_preview_url=%s, generated_preview_appearance=%s,
                   updated_at=%s
               WHERE id=%s
               RETURNING *""",
            (url, appearance, now, id),
        )
        row = cur.fetchone()
        conn.commit()
    return row_to_dict(row)


if __name__ == "__main__":
    mcp.run()
