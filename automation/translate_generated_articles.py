#!/usr/bin/env python3
"""Translate completed article-queue items before allowing new Persian articles."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import image_caption_policy
import translate_queue as translator

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "article-content-queue"
ITEMS = OUT / "items"
QUEUE = OUT / "queue.json"
TRANS = OUT / "translations"
SQL = OUT / "translation-sql"
ROLLBACK = OUT / "translation-rollback"
STATUS = OUT / "translation-status.json"
LANGUAGES = ("ar-IQ", "tg-TJ", "en-US")
LANGUAGE_CATEGORY_SLUG = {
    "ar-IQ": "maqalat-al-ray",
    "tg-TJ": "maqolahoi-obyor",
    "en-US": "en",
    # Russian is produced by the separate queue in translate_russian_articles.py.
    "ru-RU": "ru",
}
BATCH = max(1, int(os.getenv("GENERATED_TRANSLATION_BATCH_SIZE", "4")))
MAX_BACKLOG_SOURCES = max(1, int(os.getenv("MAX_TRANSLATION_BACKLOG_SOURCES", "4")))
TRANSLATION_WORKERS = max(1, int(os.getenv("TRANSLATION_WORKERS", "1")))
HEX_TEXT = re.compile(r"\b(?:[0-9A-Fa-f]{2}){6,}\b")
RELATED_LABEL = re.compile(
    r"مطالب مرتبط|موضوعات مرتبط|مواضيع ذات صلة|مواد ذات صلة|"
    r"Мавзӯъҳои алоқаманд|Маводи робита|маҳсулҳои муталлиқ|"
    r"Related (?:topics|posts|articles)|"
    r"Связанные (?:статьи|темы|материалы)|Похожие статьи|Статьи по теме",
    re.IGNORECASE,
)
PARAGRAPH = re.compile(r"<p\b[^>]*>.*?</p>", re.IGNORECASE | re.DOTALL)
GENERIC_RELATED_LABEL = {
    "fa-IR": "مقاله مرتبط",
    "ar-IQ": "مقالة ذات صلة",
    "tg-TJ": "Мақолаи марбут",
    "en-US": "Related article",
    "ru-RU": "Статья по теме",
}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def esc(value) -> str:
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'").replace("\0", "\\0").replace("\n", "\\n").replace("\r", "\\r")


def bounded_slug(value: str, suffix: str = "") -> str:
    """Keep direct-SQL post_name values inside WordPress' 200-byte limit."""
    slug = re.sub(r"-+", "-", str(value or "").strip().strip("-"))
    suffix = re.sub(r"[^a-z0-9-]+", "-", suffix.lower()).strip("-")
    budget = 200 - (len(suffix) + 1 if suffix else 0)
    raw = slug.encode("utf-8")
    if len(raw) > budget:
        raw = raw[:budget]
        while True:
            try:
                slug = raw.decode("utf-8")
                break
            except UnicodeDecodeError:
                raw = raw[:-1]
        slug = slug.rstrip("-")
    return f"{slug}-{suffix}" if suffix else slug


def repair_hex_text(html: str, lang: str) -> str:
    replacement = GENERIC_RELATED_LABEL.get(lang, "Related article")
    parts = re.split(r"(<[^>]+>)", str(html or ""))
    for index in range(0, len(parts), 2):
        parts[index] = HEX_TEXT.sub(replacement, parts[index])
    return "".join(parts)


def cleanup_translation_html(html: str, lang: str) -> str:
    """Repair cheap deterministic tail defects without another model call."""
    html = repair_hex_text(html, lang)
    related = [
        match
        for match in PARAGRAPH.finditer(html)
        if RELATED_LABEL.search(re.sub(r"<[^>]+>", " ", match.group(0)))
    ]
    if len(related) <= 1:
        return html
    keep = max(
        related,
        key=lambda match: (
            len(re.findall(r"<a\b", match.group(0), re.IGNORECASE)),
            len(re.sub(r"<[^>]+>", "", match.group(0))),
        ),
    )
    pieces = []
    cursor = 0
    for match in related:
        pieces.append(html[cursor:match.start()])
        if match.start() == keep.start():
            pieces.append(match.group(0))
        cursor = match.end()
    pieces.append(html[cursor:])
    return "".join(pieces)


def completed_sources():
    if not QUEUE.exists():
        return []
    queue = json.loads(QUEUE.read_text(encoding="utf-8"))
    rows = []
    for item in queue.get("items", []):
        if item.get("status") != "completed":
            continue
        item_id = str(item.get("id") or "")
        path = ITEMS / f"{item_id}.json"
        if not item_id or not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        rows.append((item, data))
    rows.sort(key=lambda pair: pair[0].get("completed_at", ""))
    return rows


def missing_languages(item_id: str):
    folder = TRANS / item_id
    return [lang for lang in LANGUAGES if not (folder / f"{lang}.json").exists()]


def translate_small_text(text: str, lang: str) -> str:
    if not text:
        return ""
    return translator.translate_segment_batch([{"id": 0, "text": text}], lang)[0]


def build_sql(item: dict, translated: dict, lang: str) -> tuple[str, str]:
    item_id = str(item["id"])
    key = f"article-translation:{item_id}:{lang}"
    source_key = f"article-content-queue:{item_id}"
    slug = bounded_slug(translated["slug"])
    collision_suffix = f"{item_id}-{lang.split('-', 1)[0].lower()}"
    fallback_slug = bounded_slug(slug, collision_suffix)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    final_fallback_slug = bounded_slug(slug, f"{collision_suffix}-{digest}")
    category_slug = LANGUAGE_CATEGORY_SLUG[lang]
    body = cleanup_translation_html(translated["html"], lang)
    # Alt text + caption in the translation's own language ("<title> - <role>"),
    # not the Persian alt and internal focus keyword copied from the source.
    body = image_caption_policy.normalize_figures(body, translated.get("title", ""), lang)
    lines = [
        "START TRANSACTION;",
        f"SET @source_post_id=(SELECT post_id FROM `ha_postmeta` WHERE meta_key='_navar_queue_item_id' AND meta_value='{esc(source_key)}' LIMIT 1);",
        f"SET @translation_id=(SELECT post_id FROM `ha_postmeta` WHERE meta_key='_navar_translation_queue_key' AND meta_value='{esc(key)}' LIMIT 1);",
        f"SET @slug_conflict=(SELECT ID FROM `ha_posts` WHERE post_name='{esc(slug)}' AND post_type='post' AND (@translation_id IS NULL OR ID<>@translation_id) LIMIT 1);",
        f"SET @resolved_slug=IF(@slug_conflict IS NULL,'{esc(slug)}','{esc(fallback_slug)}');",
        "SET @resolved_conflict=(SELECT ID FROM `ha_posts` WHERE post_name=@resolved_slug AND post_type='post' AND (@translation_id IS NULL OR ID<>@translation_id) LIMIT 1);",
        f"SET @resolved_slug=IF(@resolved_conflict IS NULL,@resolved_slug,'{esc(final_fallback_slug)}');",
        "SET @resolved_conflict=(SELECT ID FROM `ha_posts` WHERE post_name=@resolved_slug AND post_type='post' AND (@translation_id IS NULL OR ID<>@translation_id) LIMIT 1);",
        f"INSERT INTO `ha_posts` (`post_author`,`post_date`,`post_date_gmt`,`post_content`,`post_title`,`post_excerpt`,`post_status`,`comment_status`,`ping_status`,`post_name`,`post_modified`,`post_modified_gmt`,`post_parent`,`guid`,`menu_order`,`post_type`,`post_mime_type`,`comment_count`) SELECT 1,NOW(),UTC_TIMESTAMP(),'{esc(body)}','{esc(translated['title'])}','{esc(translated.get('excerpt',''))}','publish','closed','closed',@resolved_slug,NOW(),UTC_TIMESTAMP(),0,'',0,'post','',0 WHERE @source_post_id IS NOT NULL AND @translation_id IS NULL AND @resolved_conflict IS NULL;",
        "SET @translation_id=COALESCE(@translation_id,IF(@source_post_id IS NOT NULL AND @resolved_conflict IS NULL,LAST_INSERT_ID(),NULL));",
        f"UPDATE `ha_posts` SET post_content='{esc(body)}',post_title='{esc(translated['title'])}',post_excerpt='{esc(translated.get('excerpt',''))}',post_status='publish',post_modified=NOW(),post_modified_gmt=UTC_TIMESTAMP() WHERE ID=@translation_id;",
    ]
    metadata = [
        ("_navar_translation_queue_key", key),
        ("_navar_translation_source_id", "@source_post_id"),
        ("_navar_translation_language", lang),
        ("_rank_math_title", translated.get("meta_title", "")),
        ("_rank_math_description", translated.get("meta_description", "")),
        ("rank_math_focus_keyword", translated.get("focus_keyword", "")),
    ]
    for meta_key, value in metadata:
        sql_value = value if value == "@source_post_id" else f"'{esc(value)}'"
        lines.append(
            f"INSERT INTO `ha_postmeta` (`post_id`,`meta_key`,`meta_value`) SELECT @translation_id,'{esc(meta_key)}',{sql_value} WHERE @translation_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM `ha_postmeta` WHERE post_id=@translation_id AND meta_key='{esc(meta_key)}');"
        )
    lines += [
        "INSERT INTO `ha_postmeta` (`post_id`,`meta_key`,`meta_value`) SELECT @translation_id,'_navar_inherited_thumbnail',source_thumb.meta_value FROM `ha_postmeta` source_thumb WHERE @translation_id IS NOT NULL AND source_thumb.post_id=@source_post_id AND source_thumb.meta_key='_thumbnail_id' AND NOT EXISTS (SELECT 1 FROM `ha_postmeta` WHERE post_id=@translation_id AND meta_key='_thumbnail_id') AND NOT EXISTS (SELECT 1 FROM `ha_postmeta` WHERE post_id=@translation_id AND meta_key='_navar_inherited_thumbnail') LIMIT 1;",
        "INSERT INTO `ha_postmeta` (`post_id`,`meta_key`,`meta_value`) SELECT marker.post_id,'_thumbnail_id',marker.meta_value FROM `ha_postmeta` marker WHERE marker.post_id=@translation_id AND marker.meta_key='_navar_inherited_thumbnail' AND NOT EXISTS (SELECT 1 FROM `ha_postmeta` WHERE post_id=@translation_id AND meta_key='_thumbnail_id') LIMIT 1;",
        f"INSERT INTO `ha_term_relationships` (`object_id`,`term_taxonomy_id`,`term_order`) SELECT @translation_id,tt.term_taxonomy_id,0 FROM `ha_term_taxonomy` tt JOIN `ha_terms` t ON t.term_id=tt.term_id WHERE @translation_id IS NOT NULL AND tt.taxonomy='category' AND t.slug='{esc(category_slug)}' AND NOT EXISTS (SELECT 1 FROM `ha_term_relationships` r WHERE r.object_id=@translation_id AND r.term_taxonomy_id=tt.term_taxonomy_id) LIMIT 1;",
        "COMMIT;",
    ]
    rollback = (
        "START TRANSACTION; "
        f"SET @translation_id=(SELECT post_id FROM `ha_postmeta` WHERE meta_key='_navar_translation_queue_key' AND meta_value='{esc(key)}' LIMIT 1); "
        "DELETE FROM `ha_term_relationships` WHERE object_id=@translation_id; "
        "DELETE FROM `ha_postmeta` WHERE post_id=@translation_id; "
        "DELETE FROM `ha_posts` WHERE ID=@translation_id; COMMIT;\n"
    )
    return "\n".join(lines) + "\n", rollback


def ensure_translation_artifacts(sources=None):
    """Rebuild disposable delivery artifacts from committed translation JSON."""
    sources = completed_sources() if sources is None else sources
    SQL.mkdir(parents=True, exist_ok=True)
    ROLLBACK.mkdir(parents=True, exist_ok=True)
    repaired = 0
    for item, _ in sources:
        item_id = str(item["id"])
        folder = TRANS / item_id
        for lang in LANGUAGES:
            meta_path = folder / f"{lang}.json"
            if not meta_path.exists():
                continue
            translated = json.loads(meta_path.read_text(encoding="utf-8"))
            cleaned_html = cleanup_translation_html(translated.get("html", ""), lang)
            if cleaned_html != translated.get("html", ""):
                translated["html"] = cleaned_html
                meta_path.write_text(
                    json.dumps(translated, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                repaired += 1
            html_path = folder / f"{lang}.html"
            sql_path = SQL / f"{item_id}-{lang}.sql"
            rollback_path = ROLLBACK / f"{item_id}-{lang}.sql"
            if (
                not html_path.exists()
                or html_path.read_text(encoding="utf-8") != translated["html"]
            ):
                html_path.write_text(translated["html"], encoding="utf-8")
                repaired += 1
            create_sql, rollback_sql = build_sql(item, translated, lang)
            if (
                not sql_path.exists()
                or not rollback_path.exists()
                or sql_path.read_text(encoding="utf-8") != create_sql
                or rollback_path.read_text(encoding="utf-8") != rollback_sql
            ):
                sql_path.write_text(create_sql, encoding="utf-8")
                rollback_path.write_text(rollback_sql, encoding="utf-8")
                repaired += 1
    return repaired


def rebuild_combined_sql():
    files = sorted(SQL.glob("*.sql")) if SQL.exists() else []
    (OUT / "create-all-translations.sql").write_text(
        "-- Generated article translations: Arabic, Tajik and English.\n"
        "SET NAMES utf8mb4 COLLATE utf8mb4_unicode_520_ci;\n"
        "SET collation_connection = 'utf8mb4_unicode_520_ci';\n\n"
        + "\n".join(path.read_text(encoding="utf-8") for path in files),
        encoding="utf-8",
    )
    rollback_files = sorted(ROLLBACK.glob("*.sql"), reverse=True) if ROLLBACK.exists() else []
    (OUT / "rollback-all-translations.sql").write_text(
        "-- Roll back generated article translations.\n"
        "SET NAMES utf8mb4 COLLATE utf8mb4_unicode_520_ci;\n"
        "SET collation_connection = 'utf8mb4_unicode_520_ci';\n\n"
        + "\n".join(path.read_text(encoding="utf-8") for path in rollback_files),
        encoding="utf-8",
    )


def status_payload():
    sources = completed_sources()
    per_language = {}
    for lang in LANGUAGES:
        complete = sum((TRANS / str(item["id"]) / f"{lang}.json").exists() for item, _ in sources)
        per_language[lang] = {"completed": complete, "pending": len(sources) - complete}
    backlog_sources = sum(bool(missing_languages(str(item["id"]))) for item, _ in sources)
    image_marker = OUT / "image-rebuild-reference-rerender-3d-v11-no-human-no-container-full-rebuild.json"
    image_data = {}
    if image_marker.exists():
        try:
            image_data = json.loads(image_marker.read_text(encoding="utf-8"))
        except Exception:
            image_data = {}
    payload = {
        "updated_at": now(),
        "source_articles_completed": len(sources),
        "languages": per_language,
        "total_translation_units": len(sources) * len(LANGUAGES),
        "completed_translation_units": sum(row["completed"] for row in per_language.values()),
        "pending_translation_units": sum(row["pending"] for row in per_language.values()),
        "articles_missing_any_translation": backlog_sources,
        "delivery_artifacts_complete": sum(
            (SQL / f"{item['id']}-{lang}.sql").exists()
            and (ROLLBACK / f"{item['id']}-{lang}.sql").exists()
            for item, _ in sources
            for lang in LANGUAGES
        ),
        # A small translation tail must not starve Persian production. Pause
        # only when the backlog grows beyond the bounded safety threshold.
        "persian_queue_paused": backlog_sources >= MAX_BACKLOG_SOURCES,
        "persian_queue_pause_threshold": MAX_BACKLOG_SOURCES,
        "images": {
            "policy": image_data.get("policy"),
            "completed": bool(image_data.get("completed")),
            "failures": len(image_data.get("failures", [])),
            "total_candidates": image_data.get("total_candidates", len(sources)),
            "remaining_candidates": image_data.get("remaining_candidates", len(sources)),
            "rebuilt_posts": len(image_data.get("rebuilt_posts", [])),
        },
    }
    package_manifest = OUT / "translation-packages" / "manifest.json"
    package_data = {"batch_size": 50, "ready_batches": 0, "packages": []}
    if package_manifest.exists():
        try:
            package_data.update(json.loads(package_manifest.read_text(encoding="utf-8")))
        except Exception:
            pass
    payload["packages"] = package_data
    STATUS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def translate_source(item: dict, data: dict) -> str:
    item_id = str(item["id"])
    missing = missing_languages(item_id)
    if not missing:
        return item_id
    source = {
        "id": item_id,
        "title": data.get("title") or item.get("title"),
        "slug": item.get("slug", ""),
        "date": item.get("completed_at", ""),
        "content": data.get("html", ""),
    }
    folder = TRANS / item_id
    folder.mkdir(parents=True, exist_ok=True)
    for lang in missing:
        result = translator.translate_validated(source, lang)
        result["excerpt"] = translate_small_text(data.get("excerpt", ""), lang)
        result["focus_keyword"] = result["title"]
        result["meta_title"] = result.pop("seo_title")
        result["meta_description"] = result.pop("seo_description")
        result["html"] = cleanup_translation_html(result["html"], lang)
        result.update({
            "source_id": item_id,
            "language": lang,
            "source_title": source["title"],
            "source_slug": source["slug"],
            "validated": True,
            "provider": "Agnes AI",
            "model": translator.MODEL,
        })
        (folder / f"{lang}.html").write_text(result["html"], encoding="utf-8")
        (folder / f"{lang}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        create_sql, rollback_sql = build_sql(item, result, lang)
        (SQL / f"{item_id}-{lang}.sql").write_text(create_sql, encoding="utf-8")
        (ROLLBACK / f"{item_id}-{lang}.sql").write_text(rollback_sql, encoding="utf-8")
        print(f"generated_translation source={item_id} language={lang}", flush=True)
    return item_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-backlog", action="store_true")
    args = parser.parse_args()
    sources = completed_sources()
    repaired = ensure_translation_artifacts(sources)
    rebuild_combined_sql()
    initial = status_payload()
    if repaired:
        print(f"repaired_translation_artifacts={repaired}", flush=True)
    if args.check_backlog:
        print(initial["articles_missing_any_translation"])
        return 2 if initial["persian_queue_paused"] else 0
    TRANS.mkdir(parents=True, exist_ok=True);SQL.mkdir(parents=True, exist_ok=True);ROLLBACK.mkdir(parents=True, exist_ok=True)
    selected = []
    for item, data in completed_sources():
        if not missing_languages(str(item["id"])):
            continue
        selected.append((item, data))
        if len(selected) >= BATCH:
            break
    workers = min(TRANSLATION_WORKERS, len(selected))
    if workers:
        if not translator.TOKEN:
            raise RuntimeError("AGNES_API_KEY is empty")
        print(
            f"translation_batch={len(selected)} translation_workers={workers}",
            flush=True,
        )
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="generated-translation"
        ) as pool:
            futures = {
                pool.submit(translate_source, item, data): str(item["id"])
                for item, data in selected
            }
            for future in as_completed(futures):
                future.result()
    ensure_translation_artifacts()
    rebuild_combined_sql()
    final = status_payload()
    print(json.dumps(final, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())