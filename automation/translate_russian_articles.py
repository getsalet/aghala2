#!/usr/bin/env python3
"""Separate Russian (ru-RU) translation queue for completed Persian articles.

The Arabic/Tajik/English queue gates Persian generation. This queue does not:
it reads completed Persian articles, writes only under
artifacts/russian-article-queue/, and never changes translation-status.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import translate_generated_articles as base
import translate_queue as translator

LANG = "ru-RU"
CATEGORY_SLUG = base.LANGUAGE_CATEGORY_SLUG[LANG]
CATEGORY_NAME = "Русский"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "russian-article-queue"
TRANS = OUT / "translations"
SQL = OUT / "sql"
ROLLBACK = OUT / "rollback"
PACKAGES = OUT / "packages"
STATUS_JSON = OUT / "status.json"
STATUS_MD = OUT / "STATUS.md"
ATTEMPTS = OUT / "attempts.json"
BATCH = max(1, int(os.getenv("RUSSIAN_TRANSLATION_BATCH_SIZE", "8")))
WORKERS = max(1, int(os.getenv("RUSSIAN_TRANSLATION_WORKERS", "4")))
PACKAGE_BATCH_SIZE = max(1, int(os.getenv("RUSSIAN_PACKAGE_BATCH_SIZE", "50")))
DB_NAME = os.getenv("WORDPRESS_DB_NAME", "navaraby_wp569").strip()

ENSURE_CATEGORY = (
    f"SET @ru_term=(SELECT t.term_id FROM `ha_terms` t JOIN `ha_term_taxonomy` tt ON tt.term_id=t.term_id WHERE t.slug='{CATEGORY_SLUG}' AND tt.taxonomy='category' LIMIT 1);\n"
    f"INSERT INTO `ha_terms` (`name`,`slug`,`term_group`) SELECT '{CATEGORY_NAME}','{CATEGORY_SLUG}',0 FROM DUAL WHERE @ru_term IS NULL;\n"
    "SET @ru_term=COALESCE(@ru_term,IF(ROW_COUNT()>0,LAST_INSERT_ID(),NULL));\n"
    "INSERT INTO `ha_term_taxonomy` (`term_id`,`taxonomy`,`description`,`parent`,`count`) SELECT @ru_term,'category','',0,0 FROM DUAL WHERE @ru_term IS NOT NULL AND NOT EXISTS (SELECT 1 FROM `ha_term_taxonomy` WHERE term_id=@ru_term AND taxonomy='category');\n"
)
FIND_CATEGORY = (
    f"SET @ru_term=(SELECT t.term_id FROM `ha_terms` t JOIN `ha_term_taxonomy` tt ON tt.term_id=t.term_id WHERE t.slug='{CATEGORY_SLUG}' AND tt.taxonomy='category' LIMIT 1);\n"
)
# The correlated subquery reads only ha_term_relationships (MySQL error 1093 safe).
RECOUNT_CATEGORY = (
    "UPDATE `ha_term_taxonomy` SET `count`=(SELECT COUNT(*) FROM `ha_term_relationships` r WHERE r.term_taxonomy_id=`ha_term_taxonomy`.term_taxonomy_id) "
    "WHERE taxonomy='category' AND term_id=@ru_term;\n"
)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def load(path: Path, default):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def meta_path(item_id: str) -> Path:
    return TRANS / item_id / f"{LANG}.json"


def is_done(item_id: str) -> bool:
    return meta_path(item_id).exists()


def build_sql(item: dict, translated: dict) -> tuple[str, str]:
    """Same idempotent post SQL as the other languages, plus the /ru/ category."""
    create, rollback = base.build_sql(item, translated, LANG)
    create = create.replace("START TRANSACTION;\n", "START TRANSACTION;\n" + ENSURE_CATEGORY, 1)
    create = create[: create.rfind("COMMIT;")] + RECOUNT_CATEGORY + "COMMIT;\n"
    rollback = rollback.replace(" COMMIT;\n", " " + FIND_CATEGORY.strip() + " " + RECOUNT_CATEGORY.strip() + " COMMIT;\n")
    return create, rollback


def write_artifacts(item: dict, translated: dict) -> bool:
    item_id = str(item["id"])
    folder = TRANS / item_id
    folder.mkdir(parents=True, exist_ok=True)
    SQL.mkdir(parents=True, exist_ok=True)
    ROLLBACK.mkdir(parents=True, exist_ok=True)
    changed = False
    targets = [(folder / f"{LANG}.html", translated["html"])]
    create_sql, rollback_sql = build_sql(item, translated)
    targets += [(SQL / f"{item_id}-{LANG}.sql", create_sql), (ROLLBACK / f"{item_id}-{LANG}.sql", rollback_sql)]
    for path, text in targets:
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            changed = True
    return changed


def ensure_artifacts(sources) -> int:
    """SQL is git-ignored, so rebuild it from committed JSON on every runner."""
    repaired = 0
    for item, _ in sources:
        path = meta_path(str(item["id"]))
        if not path.exists():
            continue
        translated = json.loads(path.read_text(encoding="utf-8"))
        cleaned = base.cleanup_translation_html(translated.get("html", ""), LANG)
        if cleaned != translated.get("html", ""):
            translated["html"] = cleaned
            path.write_text(json.dumps(translated, ensure_ascii=False, indent=2), encoding="utf-8")
        repaired += write_artifacts(item, translated)
    return repaired


def translate_one(item: dict, data: dict) -> str:
    item_id = str(item["id"])
    if is_done(item_id):
        return item_id
    source = {
        "id": item_id,
        "title": data.get("title") or item.get("title"),
        "slug": item.get("slug", ""),
        "date": item.get("completed_at", ""),
        "content": data.get("html", ""),
    }
    result = translator.translate_validated(source, LANG)
    result["excerpt"] = base.translate_small_text(data.get("excerpt", ""), LANG)
    result["focus_keyword"] = result["title"]
    result["meta_title"] = result.pop("seo_title")
    result["meta_description"] = result.pop("seo_description")
    result["html"] = base.cleanup_translation_html(result["html"], LANG)
    result.update({
        "source_id": item_id,
        "language": LANG,
        "source_title": source["title"],
        "source_slug": source["slug"],
        "validated": True,
        "provider": "Agnes AI",
        "model": translator.MODEL,
        "queue": "russian-article-queue",
        "translated_at": now(),
    })
    write_artifacts(item, result)
    # JSON last: it is the completion marker.
    meta_path(item_id).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return item_id


def ordered_sources(sources, manifest):
    by_id = {str(item["id"]): (item, data) for item, data in sources}
    locked, seen = [], set()
    for package in manifest.get("packages", []):
        for key in package.get("source_ids", []):
            if key in by_id and key not in seen:
                locked.append(by_id[key]); seen.add(key)
    rest = [pair for key, pair in by_id.items() if key not in seen]
    rest.sort(key=lambda pair: (pair[0].get("completed_at", ""), str(pair[0]["id"])))
    return locked + rest


def build_packages(sources) -> dict:
    PACKAGES.mkdir(parents=True, exist_ok=True)
    old = load(PACKAGES / "manifest.json", {"packages": []})
    previous = {p.get("batch_name"): p for p in old.get("packages", [])}
    ordered = ordered_sources(sources, old)
    preamble = ["SET NAMES utf8mb4 COLLATE utf8mb4_unicode_520_ci;", "SET collation_connection = 'utf8mb4_unicode_520_ci';", f"USE `{DB_NAME}`;"]
    records = []
    for index, start in enumerate(range(0, len(ordered), PACKAGE_BATCH_SIZE), 1):
        rows = ordered[start:start + PACKAGE_BATCH_SIZE]
        if len(rows) < PACKAGE_BATCH_SIZE:
            break
        ids = [str(item["id"]) for item, _ in rows]
        if not all(is_done(key) for key in ids):
            break
        name = f"russian-batch-{index:03d}-sources-{start + 1:04d}-{start + len(rows):04d}"
        create = [f"-- Create {len(rows)} Russian (ru-RU) translations linked to Persian sources ({name})", *preamble]
        rollback = [f"-- Roll back {len(rows)} Russian (ru-RU) translations ({name})", *preamble]
        for key in ids:
            create.append(f"\n-- Russian translation: {key}\n" + (SQL / f"{key}-{LANG}.sql").read_text(encoding="utf-8"))
        for key in reversed(ids):
            rollback.append(f"\n-- Russian rollback: {key}\n" + (ROLLBACK / f"{key}-{LANG}.sql").read_text(encoding="utf-8"))
        create_text = "\n".join(create) + "\n"
        rollback_text = "\n".join(rollback) + "\n"
        input_sha = hashlib.sha256((create_text + rollback_text).encode("utf-8")).hexdigest()
        archive = PACKAGES / f"{name}.zip"
        prior = previous.get(name)
        if prior and prior.get("input_sha256") == input_sha and archive.exists() and prior.get("sha256") == sha256(archive):
            records.append(prior)
            continue
        readme = (
            f"این بسته شامل {len(rows)} ترجمه روسی (ru-RU) متصل به مقاله‌های فارسی است.\n"
            "1) ابتدا بسته‌های فارسی همین مقاله‌ها باید در وردپرس وارد شده باشند (ترجمه‌ها با _navar_queue_item_id به مقاله فارسی وصل می‌شوند).\n"
            f"2) از دیتابیس نسخه پشتیبان بگیرید و sql/create-{name}.sql را اجرا کنید. اجرای دوباره امن است.\n"
            f"3) برای بازگشت، sql/rollback-{name}.sql را اجرا کنید.\n"
            "4) دسته «Русский» با نامک ru در صورت نبودن خودکار ساخته می‌شود؛ مسیر /ru/ با افزونه Navar Multilingual Permalinks نسخه 3.1 فعال می‌شود.\n"
        )
        manifest = {
            "batch": index, "batch_name": name, "language": LANG,
            "source_article_count": len(rows), "translation_post_count": len(rows),
            "source_ids": ids, "input_sha256": input_sha,
        }
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            fixed = (2026, 1, 1, 0, 0, 0)
            def put(arcname, text):
                info = zipfile.ZipInfo(arcname, fixed); info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, text)
            put(f"sql/create-{name}.sql", create_text)
            put(f"sql/rollback-{name}.sql", rollback_text)
            put("README-fa.txt", readme)
            for key in ids:
                put(f"translations/{key}/{LANG}.json", meta_path(key).read_text(encoding="utf-8"))
                put(f"translations/{key}/{LANG}.html", (TRANS / key / f"{LANG}.html").read_text(encoding="utf-8"))
            put("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        manifest.update(zip=archive.name, bytes=archive.stat().st_size, sha256=sha256(archive))
        print(f"russian_package_built={archive.name}", flush=True)
        records.append(manifest)
    data = {"batch_size": PACKAGE_BATCH_SIZE, "ready_batches": len(records), "packages": records}
    (PACKAGES / "manifest.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def write_status(sources, attempts, packages, run=None) -> dict:
    total = len(sources)
    completed = sum(is_done(str(item["id"])) for item, _ in sources)
    pending = total - completed
    failing = {k: v for k, v in attempts.items() if not is_done(k)}
    upload = load(OUT / "ftps-upload-state.json", {"files": {}})
    payload = {
        "updated_at": now(), "language": LANG, "route": "/ru/", "category_slug": CATEGORY_SLUG,
        "source_articles_completed": total, "completed": completed, "pending": pending,
        "progress_percent": round(100 * completed / total, 2) if total else 0.0,
        "articles_with_failed_attempts": len(failing),
        "batch_size": BATCH, "workers": WORKERS, "model": translator.MODEL,
        "packages": {"batch_size": packages.get("batch_size"), "ready_batches": packages.get("ready_batches", 0)},
        "uploaded_packages": len(upload.get("files", {})),
        "last_run": run or load(STATUS_JSON, {}).get("last_run", {}),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    STATUS_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    pct = payload["progress_percent"]
    bar = "█" * int(pct // 5) + "░" * (20 - int(pct // 5))
    state = "تکمیل" if total and not pending else ("فعال" if total else "در انتظار مقاله فارسی")
    lines = [
        "# وضعیت زنده صف ترجمه روسی مقالات (ru-RU)",
        "",
        "> صف مستقل؛ ترجمه روسی مقالات فارسی تکمیل‌شده. این صف صف تولید فارسی و صف ترجمه عربی/تاجیکی/انگلیسی را متوقف نمی‌کند.",
        "",
        f"- آخرین بروزرسانی: `{payload['updated_at']}`",
        f"- وضعیت صف: **{state}**",
        f"- پیشرفت: **{completed} از {total} ({pct:.2f}٪)**",
        f"- نمودار پیشرفت: `{bar}`",
        f"- باقی‌مانده: **{pending}**",
        f"- مقالات با تلاش ناموفق (در صف تلاش مجدد): **{len(failing)}**",
        f"- مدل متن: `{translator.MODEL}` — اندازه هر اجرا: **{BATCH} مقاله** با **{WORKERS}** کارگر موازی",
        f"- مسیر وردپرس: `/ru/` — دسته: `{CATEGORY_SLUG}`",
        f"- بسته‌های ۵۰تایی آماده: **{payload['packages']['ready_batches']}** — آپلودشده با FTPS: **{payload['uploaded_packages']}**",
    ]
    last = payload["last_run"]
    if last:
        lines += ["", "## آخرین اجرا", "", f"- انتخاب‌شده: **{last.get('selected', 0)}** — موفق: **{last.get('succeeded', 0)}** — ناموفق: **{last.get('failed', 0)}** — `{last.get('finished_at', '')}`"]
    if failing:
        lines += ["", "## خطاهای اخیر", ""]
        for key, row in sorted(failing.items(), key=lambda kv: kv[1].get("updated_at", ""), reverse=True)[:10]:
            lines.append(f"- `{key}` — تلاش: **{row.get('attempts', 0)}** — `{str(row.get('last_error', ''))[:240]}`")
    if packages.get("packages"):
        lines += ["", "## بسته‌های ۵۰تایی آماده", ""]
        for p in packages["packages"]:
            lines.append(f"- [{p['zip']}](./packages/{p['zip']}) — {p['source_article_count']} مقاله — {p['bytes']} بایت — SHA256: `{p['sha256']}`")
    lines += ["", "## فایل‌ها", "", "- [وضعیت ماشینی](./status.json)", "- [ترجمه‌ها](./translations/)", "- [بسته‌ها](./packages/)", ""]
    STATUS_MD.write_text("\n".join(lines), encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status-only", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    sources = base.completed_sources()
    repaired = ensure_artifacts(sources)
    if repaired:
        print(f"russian_repaired_artifacts={repaired}", flush=True)
    attempts = load(ATTEMPTS, {})
    if args.status_only:
        write_status(sources, attempts, build_packages(sources))
        return 0
    order = {str(item["id"]): i for i, (item, _) in enumerate(sources)}
    pending = [(item, data) for item, data in sources if not is_done(str(item["id"]))]
    # Articles that keep failing move behind fresh ones instead of blocking the queue.
    pending.sort(key=lambda pair: (attempts.get(str(pair[0]["id"]), {}).get("attempts", 0), order[str(pair[0]["id"])]))
    selected = pending[:BATCH]
    succeeded = failed = 0
    if selected:
        if not translator.TOKEN:
            raise RuntimeError("AGNES_API_KEY is empty")
        print(f"russian_batch={len(selected)} workers={min(WORKERS, len(selected))} pending={len(pending)}", flush=True)
        with ThreadPoolExecutor(max_workers=min(WORKERS, len(selected)), thread_name_prefix="ru-translation") as pool:
            futures = {pool.submit(translate_one, item, data): str(item["id"]) for item, data in selected}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    future.result()
                    succeeded += 1
                    attempts.pop(key, None)
                    print(f"russian_translation source={key} language={LANG}", flush=True)
                except Exception as exc:  # keep the rest of the slice
                    failed += 1
                    row = attempts.setdefault(key, {"attempts": 0})
                    row.update(attempts=row["attempts"] + 1, last_error=f"{type(exc).__name__}: {exc}"[:500], updated_at=now())
                    print(f"russian_translation_failed source={key}: {type(exc).__name__}: {exc}", flush=True)
    ATTEMPTS.write_text(json.dumps(attempts, ensure_ascii=False, indent=2), encoding="utf-8")
    run = {"selected": len(selected), "succeeded": succeeded, "failed": failed, "finished_at": now()}
    payload = write_status(sources, attempts, build_packages(sources), run)
    print(json.dumps({k: payload[k] for k in ("completed", "pending", "progress_percent")}, ensure_ascii=False), flush=True)
    github_env = os.getenv("GITHUB_ENV")
    if github_env:
        with open(github_env, "a", encoding="utf-8") as fh:
            fh.write(f"RUSSIAN_PENDING={payload['pending']}\nRUSSIAN_SUCCEEDED={succeeded}\n")
    return 0 if succeeded or not selected else 1


if __name__ == "__main__":
    raise SystemExit(main())
