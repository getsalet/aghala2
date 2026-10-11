#!/usr/bin/env python3
"""Publish generated articles and media through the WordPress REST API."""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import image_caption_policy

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ROOT / "artifacts" / "article-content-queue" / "images"
SITE = (os.getenv("WP_SITE_URL") or "https://navar-abyari.ir").rstrip("/")
API = f"{SITE}/wp-json/wp/v2"
USERNAME = (os.getenv("WP_USERNAME") or "").strip()
APP_PASSWORD = (os.getenv("WP_APP_PASSWORD") or "").strip()


def _auth_header() -> str:
    if not USERNAME or not APP_PASSWORD:
        raise RuntimeError("WP_USERNAME/WP_APP_PASSWORD is missing")
    token = base64.b64encode(f"{USERNAME}:{APP_PASSWORD}".encode()).decode()
    return f"Basic {token}"


def request_json(method: str, path: str, payload=None, body=None, headers=None, timeout=180):
    request_headers = {
        "Authorization": _auth_header(),
        "Accept": "application/json",
        "User-Agent": "navar-article-queue/wordpress-rest",
    }
    request_headers.update(headers or {})
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        request_headers["Content-Type"] = "application/json; charset=utf-8"
    url = path if path.startswith("http") else f"{API}{path}"
    req = urllib.request.Request(url, data=body, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1200]
        raise RuntimeError(f"WordPress REST HTTP {exc.code} {path}: {detail}") from exc


def verify_credentials():
    user = request_json("GET", "/users/me?context=edit&_fields=id,name,slug")
    if not user.get("id"):
        raise RuntimeError("WordPress authentication succeeded without a user id")
    return user


def verify_category(category_id: int):
    category = request_json(
        "GET",
        f"/categories/{int(category_id)}?context=edit&_fields=id,name,slug",
    )
    if int(category.get("id", 0)) != int(category_id):
        raise RuntimeError(f"WordPress category {category_id} is unavailable")
    return category


def verify_publish_target(category_id: int):
    return {
        "user": verify_credentials(),
        "category": verify_category(category_id),
    }


def find_media(slug: str):
    query = urllib.parse.urlencode({
        "slug": slug,
        "context": "edit",
        "per_page": 1,
        "_fields": "id,slug,source_url",
    })
    rows = request_json("GET", f"/media?{query}")
    return rows[0] if rows else None


def upload_media(image: dict, item: dict, index: int):
    path = IMAGES / image["name"]
    if not path.exists():
        raise RuntimeError(f"Generated image is missing: {path}")
    slug = path.stem.lower()
    existing = find_media(slug)
    if existing:
        return existing

    mime = image.get("mime") or mimetypes.guess_type(path.name)[0] or "image/jpeg"
    media = request_json(
        "POST",
        "/media",
        body=path.read_bytes(),
        headers={
            "Content-Type": mime,
            "Content-Disposition": f'attachment; filename="{path.name}"',
        },
        timeout=300,
    )
    media_id = media.get("id")
    if not media_id:
        raise RuntimeError(f"WordPress media upload returned no id for {path.name}")
    # Set deterministic slug and accessible metadata after the binary upload.
    updated = request_json("POST", f"/media/{media_id}", payload={
        "slug": slug,
        "title": f"{item['title']} - تصویر {index}",
        "alt_text": image_caption_policy.image_texts(item["title"], index, "fa-IR")[0],
        "caption": image_caption_policy.image_texts(item["title"], index, "fa-IR")[1],
    })
    return updated or media


def render_html(item: dict, obj: dict, media_rows: list[dict]) -> str:
    body = obj["html"]
    for index, media in enumerate(media_rows, 1):
        url = media.get("source_url")
        if not url:
            raise RuntimeError(f"WordPress media {media.get('id')} has no source_url")
        figure = image_caption_policy.figure_html(url, item["title"], index, "fa-IR", media.get("id"))
        body = body.replace(f"[[[IMAGE_{index}]]]", figure)
    if "[[[IMAGE_" in body:
        raise RuntimeError("Unresolved image marker remains in article HTML")
    return body


def find_post(slug: str):
    query = urllib.parse.urlencode({
        "slug": slug,
        "context": "edit",
        "status": "any",
        "per_page": 1,
        "_fields": "id,slug,status,link",
    })
    rows = request_json("GET", f"/posts?{query}")
    return rows[0] if rows else None


def publish_article(item: dict, obj: dict, images: list[dict]):
    verify_publish_target(int(item["category_id"]))
    media_rows = [upload_media(image, item, i) for i, image in enumerate(images, 1)]
    body = render_html(item, obj, media_rows)
    payload = {
        "title": obj["title"],
        "content": body,
        "excerpt": obj.get("excerpt", ""),
        "slug": item["slug"],
        "status": "publish",
        "categories": [int(item["category_id"])],
        "featured_media": int(media_rows[0]["id"]),
    }
    existing = find_post(item["slug"])
    post = request_json(
        "POST",
        f"/posts/{existing['id']}" if existing else "/posts",
        payload=payload,
        timeout=300,
    )
    if not post.get("id") or post.get("status") != "publish":
        raise RuntimeError(f"WordPress did not confirm a published post: {post}")
    return {
        "post_id": post["id"],
        "status": post["status"],
        "link": post.get("link"),
        "html": body,
        "media": [
            {"id": row["id"], "source_url": row.get("source_url")}
            for row in media_rows
        ],
    }