#!/usr/bin/env python3
"""
Style Config — local web app for managing style presets.

Each "style" carries:
  name / category / model / prompt-template (with {{appearance}} placeholder)
  / 1-2 reference images (uploaded to OSS, public-read).

A style may also have a "generated_preview" — a single sample image generated
by calling the configured model with the prompt (after replacing {{appearance}}
with user-supplied text) and the reference images. Stored on OSS, regenerable.

Persistence: Postgres (DATABASE_URL).

Routes:
  GET    /                              — index.html
  GET    /api/styles                    — list (newest first)
  POST   /api/styles                    — create (multipart)
  PUT    /api/styles/<id>               — edit (multipart; references optional)
  DELETE /api/styles/<id>               — remove (DB row only)
  POST   /api/styles/<id>/preview       — generate preview (json: {appearance})

Run:
  pip install flask oss2 'psycopg[binary]' google-genai
  python3 server.py
"""
from __future__ import annotations

import hmac
import io
import json
import os
import pathlib
import re
import sys
import time
import uuid
from typing import Any

import psycopg
from flask import Flask, jsonify, request, send_from_directory
from psycopg.rows import dict_row
from psycopg.types.json import Json

import secrets_helper as sh

# ─── env loader ────────────────────────────────────────────────────────

ROOT = pathlib.Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"


def load_env() -> dict[str, str]:
    env: dict[str, str] = dict(os.environ)
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip())
    return env


ENV = load_env()


def must(key: str) -> str:
    """Required config lookup.
    Sensitive names (API keys, passwords) only check env + keychain — never
    .env — so accidental .env exposure can't leak them. Non-sensitive names
    fall through to .env as before."""
    if key in sh.SENSITIVE:
        return sh.require_secret(key)
    v = ENV.get(key, "").strip()
    if not v:
        sys.exit(f"missing env: {key}")
    return v


# ─── retry helper ──────────────────────────────────────────────────────
# OSS (us-west-1) and the model API are reached over the user's proxy; that
# link is flaky for big uploads and long image-gen calls (write timeouts,
# connection resets). Retry transient failures with exponential backoff.

def _retry(fn, *, tries: int = 3, base_delay: float = 1.5, what: str = "op"):
    """Run fn(); on any exception retry with exponential backoff, re-raising the
    last error after `tries` attempts."""
    last = None
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < tries:
                delay = base_delay * (2 ** (attempt - 1))
                print(f"[style_config] {what} 第 {attempt}/{tries} 次失败: {e}；{delay:.1f}s 后重试",
                      file=sys.stderr)
                time.sleep(delay)
    raise last


# ─── OSS lazy client ───────────────────────────────────────────────────

_oss_bucket = None


def oss_bucket():
    global _oss_bucket
    if _oss_bucket is None:
        import oss2
        _oss_bucket = oss2.Bucket(
            oss2.Auth(must("OSS_ACCESS_KEY_ID"), must("OSS_ACCESS_KEY_SECRET")),
            must("OSS_ENDPOINT"),
            must("OSS_BUCKET"),
            connect_timeout=120,  # slow link via proxy — give big uploads room
        )
    return _oss_bucket


def public_url(oss_key: str) -> str:
    bucket = must("OSS_BUCKET")
    domain = must("OSS_ENDPOINT").replace("https://", "").replace("http://", "")
    return f"https://{bucket}.{domain}/{oss_key}"


def upload_to_oss(oss_key: str, data: bytes, mime: str) -> str:
    _retry(
        lambda: oss_bucket().put_object(
            oss_key,
            data,
            headers={"x-oss-object-acl": "public-read", "Content-Type": mime},
        ),
        what=f"OSS 上传 {oss_key}",
    )
    return public_url(oss_key)


# ─── Mob AI router (image generation) ──────────────────────────────────
# Generation goes through the Mob AI router. Reference images are passed by
# URL, and the generated image is returned as a URL already hosted on our OSS
# bucket — so we store that URL directly, no byte round-trip.

MODEL_MAP = {
    # legacy Zenmux names → router model ids
    "openai/gpt-image-2": "image-gpt",
    "google/gemini-3.1-flash-image-preview": "image-gemini-flash",
    "google/gemini-3-pro-image-preview": "image-gemini-pro",
    # router-native names pass straight through
    "image-gpt": "image-gpt",
    "image-gemini-pro": "image-gemini-pro",
    "image-gemini-flash": "image-gemini-flash",
}


def _router_post(path: str, body: dict, timeout: int = 240) -> dict:
    """POST JSON to the Mob AI router with auth + retry. On HTTP errors, raise
    with the response body so the caller can surface the actual reason."""
    import urllib.error
    import urllib.request
    base = ENV.get("MOB_AI_BASE_URL", "https://ai.mob-ai.cn/api").rstrip("/")
    key = must("MOB_AI_KEY")
    payload = json.dumps(body).encode()

    def _do() -> dict:
        req = urllib.request.Request(
            f"{base}{path}", data=payload, method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "style-config/1.0",  # default urllib UA gets WAF-403'd
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as he:
            detail = he.read().decode(errors="replace")[:400]
            raise RuntimeError(f"HTTP {he.code}: {detail}") from None

    return _retry(_do, tries=2, base_delay=2.0, what=f"router {path}")


def _extract_image_url(j: dict) -> str:
    out = j.get("output") or {}
    if isinstance(out, dict) and out.get("url"):
        return out["url"]
    imgs = j.get("images") or []
    if imgs and isinstance(imgs[0], dict) and imgs[0].get("url"):
        return imgs[0]["url"]
    if isinstance(j.get("result"), str) and j["result"].startswith("http"):
        return j["result"]
    raise RuntimeError(f"router 未返回图片 url: {json.dumps(j)[:300]}")


_PLACEHOLDER_RE = re.compile(r"\{\{[^{}]+\}\}")


def render_prompt(template: str, text: str) -> str:
    """Replace every {{var}} placeholder (any name) with `text`. If the
    template has no placeholder, append `text` at the end. A prompt is expected
    to carry a single variable, but if several appear they all get `text`."""
    text = (text or "").strip()
    if _PLACEHOLDER_RE.search(template):
        # function replacement so backslashes / \g in user text aren't treated
        # as regex group references
        return _PLACEHOLDER_RE.sub(lambda _: text, template)
    if text:
        return f"{template.rstrip()}\n\n{text}"
    return template


def generate_preview_url(model: str, prompt_text: str, ref_urls: list[str],
                         aspect_ratio: str = "") -> str:
    """Generate an image via the Mob AI router and return its URL. Reference
    images are passed by URL; the result is hosted on OSS by the router, so the
    URL can be stored directly. aspect_ratio (e.g. "9:16") maps to the router's
    `input.aspectRatio` — only image-gpt honors it; other models ignore it."""
    body: dict[str, Any] = {
        "model": MODEL_MAP.get(model, model),
        "input": {"prompt": prompt_text},
    }
    if ref_urls:
        body["input"]["references"] = [{"type": "image", "url": u} for u in ref_urls]
    if aspect_ratio:
        body["input"]["aspectRatio"] = aspect_ratio
    resp = _router_post("/v1/generations", body)
    status = resp.get("status")
    if isinstance(status, str) and status not in ("succeeded", "success"):
        raise RuntimeError(f"router status={status}: {json.dumps(resp)[:300]}")
    return _extract_image_url(resp)


# ─── DB ────────────────────────────────────────────────────────────────

DDL = """
CREATE TABLE IF NOT EXISTS styles (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt TEXT NOT NULL,
  reference_urls JSONB NOT NULL DEFAULT '[]'::jsonb,
  generated_preview_url TEXT,
  generated_preview_appearance TEXT,
  generated_preview_ref_urls JSONB NOT NULL DEFAULT '[]'::jsonb,
  generated_preview_aspect TEXT,
  created_at DOUBLE PRECISION NOT NULL,
  updated_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS styles_created_at_idx ON styles (created_at DESC);
"""


# Optional remote-DB mode: if SSH_HOST is set in env, open an SSH tunnel to
# the server's local Postgres and route every connection through it. Same
# pattern as mcp/style_config_mcp.py — direct psycopg, just on a forwarded
# local port. Without SSH_HOST, fall back to DATABASE_URL (typical local dev).

_tunnel = None
_tunneled_port: int | None = None


def _ensure_tunnel() -> int | None:
    """Lazily start the SSH tunnel. Returns the local port to connect to,
    or None if remote mode is not configured."""
    global _tunnel, _tunneled_port
    ssh_host = ENV.get("SSH_HOST", "").strip()
    if not ssh_host:
        return None
    if _tunnel is not None and _tunnel.is_active:
        return _tunneled_port
    from sshtunnel import SSHTunnelForwarder
    remote_host = ENV.get("PG_REMOTE_HOST", "127.0.0.1")
    remote_port = int(ENV.get("PG_REMOTE_PORT", "5432"))
    _tunnel = SSHTunnelForwarder(
        ssh_host,
        ssh_username=must("SSH_USER"),
        ssh_password=must("SSH_PASSWORD"),
        remote_bind_address=(remote_host, remote_port),
        local_bind_address=("127.0.0.1", 0),
    )
    _tunnel.start()
    _tunneled_port = _tunnel.local_bind_port
    print(f"[style_config] SSH tunnel → {ssh_host}:{remote_port} (local port {_tunneled_port})")
    return _tunneled_port


def db_connect() -> psycopg.Connection:
    port = _ensure_tunnel()
    if port is not None:
        # remote mode
        return psycopg.connect(
            host="127.0.0.1",
            port=port,
            dbname=must("PG_DB"),
            user=must("PG_USER"),
            password=must("PG_PASSWORD"),
            row_factory=dict_row,
            connect_timeout=10,
        )
    # local mode
    return psycopg.connect(must("DATABASE_URL"), row_factory=dict_row)


def db_init() -> None:
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(DDL)
        # migrate existing tables that predate the per-preview ref column
        cur.execute(
            "ALTER TABLE styles ADD COLUMN IF NOT EXISTS "
            "generated_preview_ref_urls JSONB NOT NULL DEFAULT '[]'::jsonb"
        )
        cur.execute(
            "ALTER TABLE styles ADD COLUMN IF NOT EXISTS generated_preview_aspect TEXT"
        )
        conn.commit()


# ─── App ───────────────────────────────────────────────────────────────

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

app = Flask(__name__, static_folder=None)


# ─── auth ──────────────────────────────────────────────────────────────
# Shared bearer token. Public friends get a "magic link" URL with `?k=<token>`;
# the frontend stores it in localStorage and sends `Authorization: Bearer …`
# on every API call afterwards. If STYLE_CONFIG_TOKEN isn't set, auth is OFF
# (handy for local dev).

def _expected_token() -> str:
    return (sh.get_secret("STYLE_CONFIG_TOKEN") or "").strip()


def _request_token() -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    k = (request.args.get("k") or "").strip()
    if k:
        return k
    return (request.cookies.get("style_config_token") or "").strip()


def _auth_ok() -> bool:
    expected = _expected_token()
    if not expected:
        return True  # auth disabled
    provided = _request_token()
    return bool(provided) and hmac.compare_digest(provided, expected)


@app.before_request
def _require_auth():
    # The index page itself must be reachable without auth so the magic-link
    # bootstrap JS can run. /favicon.ico same. All /api/* needs auth.
    p = request.path
    if request.method == "GET" and p in ("/", "/favicon.ico"):
        return None
    if _auth_ok():
        return None
    return jsonify({"error": "unauthorized"}), 401


@app.route("/")
def index():
    return send_from_directory(PUBLIC_DIR, "index.html")


@app.route("/api/styles", methods=["GET"])
def list_styles():
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM styles ORDER BY created_at DESC")
        rows = cur.fetchall()
    return jsonify(rows)


@app.route("/api/styles", methods=["POST"])
def create_style():
    """Create a new style. References are required (1-2 image files)."""
    name = (request.form.get("name") or "").strip()
    category = (request.form.get("category") or "").strip()
    model = (request.form.get("model") or "").strip()
    prompt = (request.form.get("prompt") or "").strip()
    files = [f for f in request.files.getlist("references") if f and f.filename]

    field_errors: dict[str, str] = {}
    if not name:
        field_errors["name"] = "请输入风格名称"
    if not category:
        field_errors["category"] = "请选择类别"
    elif category not in VALID_CATEGORIES:
        field_errors["category"] = "类别不合法"
    if not model:
        field_errors["model"] = "请选择模型"
    elif model not in VALID_MODELS:
        field_errors["model"] = "模型不合法"
    if not prompt:
        field_errors["prompt"] = "请输入风格提示词"
    if len(files) > 2:
        field_errors["references"] = "最多 2 张参考图"
    if field_errors:
        return jsonify({"field_errors": field_errors}), 400

    style_id = uuid.uuid4().hex[:12]
    prefix = ENV.get("OSS_PREFIX", "style-config").rstrip("/")
    ref_urls: list[str] = []
    for i, fileobj in enumerate(files, start=1):
        data = fileobj.read()
        oss_key = f"{prefix}/refs/{style_id}-ref{i}.png"
        ref_urls.append(upload_to_oss(oss_key, data, fileobj.mimetype or "image/png"))

    now = time.time()
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO styles(id,name,category,model,prompt,reference_urls,
                                  created_at,updated_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING *""",
            (style_id, name, category, model, prompt, Json(ref_urls), now, now),
        )
        row = cur.fetchone()
        conn.commit()
    return jsonify(row), 201


@app.route("/api/styles/<style_id>", methods=["PUT"])
def edit_style(style_id: str):
    """Edit a style. All fields optional. If `references` is supplied it
    *replaces* the existing list (1-2 files). Existing OSS objects are not
    deleted (cheap, easier to debug)."""
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM styles WHERE id=%s", (style_id,))
        existing = cur.fetchone()
        if not existing:
            return jsonify({"error": "not found"}), 404

    name = (request.form.get("name") or "").strip()
    category = (request.form.get("category") or "").strip()
    model = (request.form.get("model") or "").strip()
    prompt = (request.form.get("prompt") or "").strip()
    files = [f for f in request.files.getlist("references") if f and f.filename]

    field_errors: dict[str, str] = {}
    if not name:
        field_errors["name"] = "请输入风格名称"
    if not category or category not in VALID_CATEGORIES:
        field_errors["category"] = "类别不合法"
    if not model or model not in VALID_MODELS:
        field_errors["model"] = "模型不合法"
    if not prompt:
        field_errors["prompt"] = "请输入风格提示词"
    if files and len(files) > 2:
        field_errors["references"] = "最多 2 张参考图"
    if field_errors:
        return jsonify({"field_errors": field_errors}), 400

    ref_urls = list(existing["reference_urls"] or [])
    if files:
        prefix = ENV.get("OSS_PREFIX", "style-config").rstrip("/")
        ref_urls = []
        for i, fileobj in enumerate(files, start=1):
            data = fileobj.read()
            # Add a short suffix so we never clobber a URL that the
            # generated_preview was made against — keeps history readable on OSS.
            tag = uuid.uuid4().hex[:4]
            oss_key = f"{prefix}/refs/{style_id}-ref{i}-{tag}.png"
            ref_urls.append(upload_to_oss(oss_key, data, fileobj.mimetype or "image/png"))

    now = time.time()
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE styles
               SET name=%s, category=%s, model=%s, prompt=%s,
                   reference_urls=%s, updated_at=%s
               WHERE id=%s
               RETURNING *""",
            (name, category, model, prompt, Json(ref_urls), now, style_id),
        )
        row = cur.fetchone()
        conn.commit()
    return jsonify(row)


@app.route("/api/styles/<style_id>", methods=["DELETE"])
def delete_style(style_id: str):
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM styles WHERE id=%s", (style_id,))
        deleted = cur.rowcount
        conn.commit()
    if deleted == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/styles/<style_id>/preview", methods=["POST"])
def generate_preview(style_id: str):
    """Generate a preview. Accepts multipart (appearance text + any number of
    ordered `references` files) or a JSON body {appearance}. Uploaded refs are
    appended *after* the style's stored refs when calling the model, and their
    OSS URLs are saved on the row so they can be inspected later."""
    appearance = (request.form.get("appearance") or "").strip()
    if not appearance:
        body = request.get_json(silent=True) or {}
        appearance = (body.get("appearance") or "").strip()
    if not appearance:
        return jsonify({"error": "appearance 不能为空"}), 400

    # Optional output aspect ratio (e.g. "9:16"); image-gpt only.
    aspect = (request.form.get("aspectRatio") or "").strip()

    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM styles WHERE id=%s", (style_id,))
        s = cur.fetchone()
    if not s:
        return jsonify({"error": "not found"}), 404

    prefix = ENV.get("OSS_PREFIX", "style-config").rstrip("/")

    # Per-preview reference uploads — unlimited, kept in the order the client
    # sent them (request.files.getlist preserves multipart field order).
    files = [f for f in request.files.getlist("references") if f and f.filename]
    uploaded_urls: list[str] = []
    try:
        for i, fileobj in enumerate(files, start=1):
            data = fileobj.read()
            tag = uuid.uuid4().hex[:4]
            oss_key = f"{prefix}/preview-refs/{style_id}-{tag}-{i}.png"
            uploaded_urls.append(upload_to_oss(oss_key, data, fileobj.mimetype or "image/png"))
    except Exception as e:
        return jsonify({"error": f"参考图上传 OSS 失败: {e}"}), 502

    # stored refs first, freshly-uploaded ones after
    gen_refs = list(s["reference_urls"] or []) + uploaded_urls

    rendered = render_prompt(s["prompt"], appearance)
    try:
        url = generate_preview_url(s["model"], rendered, gen_refs, aspect)
    except Exception as e:
        return jsonify({"error": f"模型调用失败: {e}"}), 502

    now = time.time()
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE styles
               SET generated_preview_url=%s, generated_preview_appearance=%s,
                   generated_preview_ref_urls=%s, generated_preview_aspect=%s,
                   updated_at=%s
               WHERE id=%s
               RETURNING *""",
            (url, appearance, Json(uploaded_urls), aspect or None, now, style_id),
        )
        row = cur.fetchone()
        conn.commit()
    return jsonify(row)


# ─── Bootstrap ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    db_init()
    port = int(ENV.get("PORT", "5050"))
    print(f"[style_config] http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=True, threaded=True)
