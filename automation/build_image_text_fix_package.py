#!/usr/bin/env python3
"""Build one SQL package that repairs image alt text and captions in place.

The Persian, Arabic, Tajik and English posts on the site already use
"<title> - <role>" alt text and captions. The Russian (ru-RU) posts were
imported later from translation JSON that still had the Persian alt text and
the internal ``focus`` keyword string as caption. This package rewrites those
figures in place (language-correct alt + caption, class="wp-image-ID" so
WordPress adds srcset). IMAGE_FIX_LANGS selects languages (default ru-RU).
Each image is changed with REPLACE(old figure, new figure) inside the post found
by its queue meta key, so posts edited by hand or not imported are left alone.
Running it twice is harmless; the rollback swaps the figures back.

Output (artifacts/image-text-fix/):
  navar-image-text-fix.zip           -> navar-image-text-fix.sql
  navar-image-text-fix-rollback.zip  -> navar-image-text-fix-rollback.sql
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import image_caption_policy as policy  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
QUEUE_DIR = ROOT / "artifacts" / "article-content-queue"
ITEMS = QUEUE_DIR / "items"
TRANS = QUEUE_DIR / "translations"
RU_TRANS = ROOT / "artifacts" / "russian-article-queue" / "translations"
OUT = ROOT / "artifacts" / "image-text-fix"
NAME = "navar-image-text-fix"
DB_NAME = os.getenv("WORDPRESS_DB_NAME", "navaraby_wp569").strip()
LANGS = (("ar-IQ", TRANS), ("tg-TJ", TRANS), ("en-US", TRANS), ("ru-RU", RU_TRANS))


def esc(value) -> str:
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'").replace("\0", "\\0").replace("\n", "\\n").replace("\r", "\\r")


def completed_items():
    queue = json.loads((QUEUE_DIR / "queue.json").read_text(encoding="utf-8"))
    for row in queue.get("items", []):
        item_id = str(row.get("id") or "")
        path = ITEMS / f"{item_id}.json"
        if row.get("status") == "completed" and item_id and path.exists():
            yield item_id, json.loads(path.read_text(encoding="utf-8"))


def sql_str(value: str) -> str:
    return "'" + esc(value) + "'"


def new_figure_sql(src: str, title: str, index: int, lang: str, image: dict) -> str:
    """SQL expression for the new figure; adds class="wp-image-ID" when the media exists."""
    head, tail = policy.figure_parts(src, title, index, lang, image.get("width") or 1200, image.get("height") or 675)
    return f"CONCAT({sql_str(head)},IF(@m{index} IS NULL,'',CONCAT(' class=\"wp-image-',@m{index},'\"')),{sql_str(tail)})"


def post_sql(item_id: str, lang: str, translated: dict, images: list) -> tuple[str, str, int]:
    swaps = []
    for match, src, index in policy.generated_figures(translated.get("html", "")):
        image = images[index - 1] if index - 1 < len(images) and isinstance(images[index - 1], dict) else {}
        swaps.append((sql_str(match.group(0)), new_figure_sql(src, translated.get("title", ""), index, lang, image)))
    if not swaps:
        return "", "", 0
    key = f"article-translation:{item_id}:{lang}"
    find = [f"SET @pid=(SELECT post_id FROM `ha_postmeta` WHERE meta_key='_navar_translation_queue_key' AND meta_value='{esc(key)}' LIMIT 1);"]
    for index in range(1, 4):
        image_key = f"article-content-queue:{item_id}:image:{index}"
        find.append(f"SET @m{index}=(SELECT post_id FROM `ha_postmeta` WHERE meta_key='_navar_queue_image_id' AND meta_value='{esc(image_key)}' LIMIT 1);")
    forward = backward = "post_content"
    for old, new in swaps:
        forward = f"REPLACE({forward},{old},{new})"
    for old, new in reversed(swaps):
        backward = f"REPLACE({backward},{new},{old})"
    head = " ".join(find)
    create = (f"{head}\nUPDATE `ha_posts` SET post_content={forward} WHERE ID=@pid;\n"
              "SET @posts_changed=@posts_changed+IF(ROW_COUNT()>0,1,0); SET @posts_missing=@posts_missing+IF(@pid IS NULL,1,0);")
    rollback = f"{head}\nUPDATE `ha_posts` SET post_content={backward} WHERE ID=@pid;"
    return create, rollback, len(swaps)


def write_zip(path: Path, arcname: str, text: str) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        info = zipfile.ZipInfo(arcname, (2026, 1, 1, 0, 0, 0)); info.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(info, text)


def main() -> int:
    langs = [x.strip() for x in os.getenv("IMAGE_FIX_LANGS", "ru-RU").split(",") if x.strip()]
    preamble = ["SET NAMES utf8mb4 COLLATE utf8mb4_unicode_520_ci;", "SET collation_connection = 'utf8mb4_unicode_520_ci';", f"USE `{DB_NAME}`;"]
    create = [f"-- Repair image alt text and captions of generated-article translations: {', '.join(langs)}.",
              "-- Format matches the fa/ar/tg/en posts already on the site: alt = caption = '<title> - <role>'.",
              "-- Idempotent and safe to run again. Rollback: navar-image-text-fix-rollback.sql", *preamble,
              "SET @posts_changed=0; SET @posts_missing=0;", "START TRANSACTION;"]
    rollback = ["-- Roll back navar-image-text-fix.sql", *preamble, "START TRANSACTION;"]
    stats = {lang: 0 for lang in langs}
    figures = 0
    folders = dict(LANGS)
    for item_id, data in completed_items():
        for lang in langs:
            path = folders.get(lang, TRANS) / item_id / f"{lang}.json"
            if not path.exists():
                continue
            c, r, n = post_sql(item_id, lang, json.loads(path.read_text(encoding="utf-8")), data.get("images") or [])
            if n:
                create += [f"-- {item_id} {lang}", c]; rollback += [f"-- {item_id} {lang}", r]
                stats[lang] += 1; figures += n
    create += ["COMMIT;", "SELECT @posts_changed AS navar_posts_changed, @posts_missing AS navar_posts_not_found;"]
    rollback += ["COMMIT;"]
    OUT.mkdir(parents=True, exist_ok=True)
    main_zip, rb_zip = OUT / f"{NAME}.zip", OUT / f"{NAME}-rollback.zip"
    write_zip(main_zip, f"{NAME}.sql", "\n".join(create) + "\n")
    write_zip(rb_zip, f"{NAME}-rollback.sql", "\n".join(rollback) + "\n")
    info = {
        "posts_per_language": stats, "posts_total": sum(stats.values()), "figures": figures,
        "files": {p.name: {"bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in (main_zip, rb_zip)},
    }
    (OUT / "manifest.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(info, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
