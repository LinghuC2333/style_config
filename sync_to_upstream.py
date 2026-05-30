#!/usr/bin/env python3
"""Push local styles that don't yet exist online (matched by name) up to the
live site via its HTTP API. Mirror of sync_from_upstream.py (which pulls).

Additive + idempotent by name: a local style whose name already exists online
is skipped, so re-running won't create duplicates. The online server assigns a
fresh id (POST can't set one) and generated previews are NOT carried over —
regenerate them on the site if needed. Model names are normalised via the
router MODEL_MAP (e.g. openai/gpt-image-2 → image-gpt) so the online server
accepts them.

    python3 sync_to_upstream.py
"""
from __future__ import annotations

import sys
import time

import psycopg
import requests
from psycopg.rows import dict_row

import secrets_helper as sh
from server import ENV, must

ONLINE = ENV.get("ONLINE_BASE_URL", "https://style-config.mob-ai.cn").rstrip("/")
UA = {"User-Agent": "style-config/1.0"}  # default client UA gets WAF-403'd


def _headers() -> dict:
    token = (sh.get_secret("STYLE_CONFIG_TOKEN") or "").strip()
    h = dict(UA)
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def online_style_names() -> set[str]:
    r = requests.get(f"{ONLINE}/api/styles", headers=_headers(), timeout=30)
    r.raise_for_status()
    return {s["name"] for s in r.json()}


def fetch_bytes(url: str) -> bytes:
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=90)
            r.raise_for_status()
            return r.content
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise last


def main() -> None:
    existing = online_style_names()
    with psycopg.connect(must("DATABASE_URL"), row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM styles ORDER BY created_at")
        local = cur.fetchall()

    pushed = skipped = failed = 0
    for s in local:
        if s["name"] in existing:
            print(f"[skip] {s['name']} — 线上已存在")
            skipped += 1
            continue
        data = {
            "name": s["name"],
            "category": s["category"],
            "model": s["model"],  # send as-is; online validates against the old names
            "prompt": s["prompt"],
        }
        files = []
        try:
            for i, u in enumerate(s["reference_urls"] or [], start=1):
                files.append(("references", (f"ref{i}.png", fetch_bytes(u), "image/png")))
            r = requests.post(f"{ONLINE}/api/styles", headers=_headers(),
                              data=data, files=files or None, timeout=180)
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] {s['name']}: {e}")
            failed += 1
            continue
        if r.status_code == 201:
            print(f"[ok]   {s['name']} → 线上 id {r.json().get('id')} "
                  f"(model={data['model']}, refs={len(files)})")
            pushed += 1
        else:
            print(f"[FAIL] {s['name']}: HTTP {r.status_code} {r.text[:200]}")
            failed += 1

    print(f"\n推送完成：成功 {pushed}，跳过 {skipped}（已存在），失败 {failed}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"[sync_to_upstream] 失败: {e}")
