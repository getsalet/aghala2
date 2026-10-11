#!/usr/bin/env python3
"""Merge every completed Russian (ru-RU) translation into ONE import package.

Output (artifacts/russian-article-queue/merged/):
  russian-all-articles.zip           -> a single .sql (phpMyAdmin can import the zip directly)
  russian-all-articles-rollback.zip  -> matching single rollback .sql
The per-article SQL is the same idempotent SQL used by the 50-article packages,
so importing the merged file after some 50-article packages is safe.
"""
from __future__ import annotations
import hashlib, json, sys, zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import translate_russian_articles as ru  # noqa: E402

MERGED = ru.OUT / "merged"
NAME = "russian-all-articles"


def write_zip(path: Path, arcname: str, text: str) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        info = zipfile.ZipInfo(arcname, (2026, 1, 1, 0, 0, 0)); info.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(info, text)


def main() -> int:
    sources = ru.base.completed_sources()
    ru.ensure_artifacts(sources)
    manifest = ru.load(ru.PACKAGES / "manifest.json", {"packages": []})
    ordered = [item for item, _ in ru.ordered_sources(sources, manifest) if ru.is_done(str(item["id"]))]
    ids = [str(item["id"]) for item in ordered]
    missing = [k for k in ids if not (ru.SQL / f"{k}-{ru.LANG}.sql").exists()]
    if not ids or missing:
        raise SystemExit(f"cannot merge: articles={len(ids)} missing_sql={missing[:10]}")
    pending = len(sources) - len(ids)
    preamble = ["SET NAMES utf8mb4 COLLATE utf8mb4_unicode_520_ci;", "SET collation_connection = 'utf8mb4_unicode_520_ci';", f"USE `{ru.DB_NAME}`;"]
    create = [f"-- All {len(ids)} Russian (ru-RU) translations linked to their Persian sources (single merged file).",
              "-- Idempotent: safe to run again or after any russian-batch-*.zip package.", *preamble]
    rollback = [f"-- Roll back all {len(ids)} Russian (ru-RU) translations (single merged file).", *preamble]
    for key in ids:
        create.append(f"\n-- Russian translation: {key}\n" + (ru.SQL / f"{key}-{ru.LANG}.sql").read_text(encoding="utf-8"))
    for key in reversed(ids):
        rollback.append(f"\n-- Russian rollback: {key}\n" + (ru.ROLLBACK / f"{key}-{ru.LANG}.sql").read_text(encoding="utf-8"))
    MERGED.mkdir(parents=True, exist_ok=True)
    main_zip, rb_zip = MERGED / f"{NAME}.zip", MERGED / f"{NAME}-rollback.zip"
    write_zip(main_zip, f"{NAME}-{len(ids)}.sql", "\n".join(create) + "\n")
    write_zip(rb_zip, f"{NAME}-{len(ids)}-rollback.sql", "\n".join(rollback) + "\n")
    info = {
        "language": ru.LANG, "articles": len(ids), "pending_not_included": pending,
        "files": {p.name: {"bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in (main_zip, rb_zip)},
        "source_ids_first_last": [ids[0], ids[-1]],
    }
    (MERGED / "merged-manifest.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(info, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
