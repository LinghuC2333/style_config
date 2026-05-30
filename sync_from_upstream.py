#!/usr/bin/env python3
"""Sync the live style library into the local Postgres so the local server can
act as a staging mirror of production.

Does NOT touch the remote database directly — it only calls the live HTTP API
(`GET /api/styles` with the shared bearer token), then replaces the local
`styles` table with whatever the live service returns. Re-run anytime to pull
the latest production data down.

    python3 sync_from_upstream.py
"""
from __future__ import annotations

import json
import sys
import urllib.request

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

import secrets_helper as sh
# Reuse the local server's env loader, required-config helper and table DDL so
# this script stays in lockstep with whatever schema the server expects.
from server import DDL, ENV, must

UPSTREAM = ENV.get("UPSTREAM_API_BASE", "https://style-config.mob-ai.cn").rstrip("/")


def fetch_upstream() -> list[dict]:
    token = (sh.get_secret("STYLE_CONFIG_TOKEN") or "").strip()
    # The live deployment sits behind a WAF that 403s the default
    # "Python-urllib" UA, so send an explicit one.
    headers = {"User-Agent": "style-config/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{UPSTREAM}/api/styles", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main() -> None:
    rows = fetch_upstream()
    print(f"[sync] 从 {UPSTREAM} 拉到 {len(rows)} 条 style")

    with psycopg.connect(must("DATABASE_URL"), row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute("TRUNCATE styles")
        for s in rows:
            cur.execute(
                """INSERT INTO styles(id,name,category,model,prompt,reference_urls,
                                      generated_preview_url,generated_preview_appearance,
                                      created_at,updated_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    s["id"], s["name"], s["category"], s["model"], s["prompt"],
                    Json(s.get("reference_urls") or []),
                    s.get("generated_preview_url"),
                    s.get("generated_preview_appearance"),
                    s["created_at"], s["updated_at"],
                ),
            )
        conn.commit()

    print(f"[sync] 本地库已刷新为线上的 {len(rows)} 条")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"[sync] 失败: {e}")
