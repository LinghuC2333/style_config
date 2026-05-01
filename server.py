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
import mimetypes
import os
import pathlib
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
        )
    return _oss_bucket


def public_url(oss_key: str) -> str:
    bucket = must("OSS_BUCKET")
    domain = must("OSS_ENDPOINT").replace("https://", "").replace("http://", "")
    return f"https://{bucket}.{domain}/{oss_key}"


def upload_to_oss(oss_key: str, data: bytes, mime: str) -> str:
    oss_bucket().put_object(
        oss_key,
        data,
        headers={"x-oss-object-acl": "public-read", "Content-Type": mime},
    )
    return public_url(oss_key)


# ─── Zenmux / google-genai lazy client ─────────────────────────────────

_genai_client = None


def genai_client():
    global _genai_client
    if _genai_client is None:
        from google import genai
        from google.genai import types as gt
        _genai_client = genai.Client(
            api_key=must("ZENMUX_API_KEY"),
            vertexai=True,
            http_options=gt.HttpOptions(
                api_version="v1",
                base_url=ENV.get("ZENMUX_VERTEX_BASE_URL", "https://zenmux.ai/api/vertex-ai"),
            ),
        )
    return _genai_client


def fetch_url_bytes(url: str) -> tuple[bytes, str]:
    """Download a URL, return (bytes, mime). Used to pull reference images
    back from OSS to feed into the generation API."""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "style-config/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
        mime = resp.headers.get("Content-Type") or mimetypes.guess_type(url)[0] or "image/png"
    return data, mime


def render_prompt(template: str, appearance: str) -> str:
    """Replace {{appearance}}; if the placeholder isn't present, append."""
    appearance = (appearance or "").strip()
    if "{{appearance}}" in template:
        return template.replace("{{appearance}}", appearance)
    if appearance:
        return f"{template.rstrip()}\n\n{appearance}"
    return template


def generate_preview_bytes(model: str, prompt_text: str, ref_urls: list[str]) -> bytes:
    """Invoke the chosen model with the rendered prompt + reference images.
    Returns PNG bytes."""
    from google.genai import types as gt
    client = genai_client()

    refs_data = [fetch_url_bytes(u) for u in ref_urls]

    if model == "openai/gpt-image-2":
        ref_objs = []
        for i, (data, mime) in enumerate(refs_data, start=1):
            ref_objs.append(gt.RawReferenceImage(
                reference_id=i,
                reference_image=gt.Image(image_bytes=data, mime_type=mime),
            ))
        if ref_objs:
            resp = client.models.edit_image(
                model=model,
                prompt=prompt_text,
                reference_images=ref_objs,
            )
        else:
            resp = client.models.generate_images(model=model, prompt=prompt_text)
        img = resp.generated_images[0].image
        return getattr(img, "image_bytes", None) or img._image_bytes  # noqa: SLF001

    # Gemini family — use generate_content with image + text parts.
    parts: list[Any] = [gt.Part.from_text(text=prompt_text)]
    for data, mime in refs_data:
        parts.append(gt.Part.from_bytes(data=data, mime_type=mime))

    resp = client.models.generate_content(
        model=model,
        contents=parts,
        config=gt.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]),
    )
    for cand in resp.candidates or []:
        for part in (cand.content.parts if cand.content else []) or []:
            if getattr(part, "inline_data", None) and part.inline_data.data:
                return part.inline_data.data
    raise RuntimeError("model returned no image part")


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
  created_at DOUBLE PRECISION NOT NULL,
  updated_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS styles_created_at_idx ON styles (created_at DESC);
"""


def db_url() -> str:
    return must("DATABASE_URL")


def db_connect() -> psycopg.Connection:
    return psycopg.connect(db_url(), row_factory=dict_row)


def db_init() -> None:
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(DDL)
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
    body = request.get_json(silent=True) or {}
    appearance = (body.get("appearance") or "").strip()
    if not appearance:
        return jsonify({"error": "appearance 不能为空"}), 400

    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM styles WHERE id=%s", (style_id,))
        s = cur.fetchone()
    if not s:
        return jsonify({"error": "not found"}), 404

    rendered = render_prompt(s["prompt"], appearance)
    try:
        png = generate_preview_bytes(s["model"], rendered, list(s["reference_urls"] or []))
    except Exception as e:
        return jsonify({"error": f"模型调用失败: {e}"}), 502

    prefix = ENV.get("OSS_PREFIX", "style-config").rstrip("/")
    tag = uuid.uuid4().hex[:6]
    oss_key = f"{prefix}/previews/{style_id}-{tag}.png"
    url = upload_to_oss(oss_key, png, "image/png")

    now = time.time()
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE styles
               SET generated_preview_url=%s, generated_preview_appearance=%s,
                   updated_at=%s
               WHERE id=%s
               RETURNING *""",
            (url, appearance, now, style_id),
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
    app.run(host="127.0.0.1", port=port, debug=True)
