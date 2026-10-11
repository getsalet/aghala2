import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "automation"))

import translate_queue
import translate_russian_articles as ru


def sample(item_id="crop-001"):
    return {
        "title": "Капельный полив", "slug": "kapelnyy-poliv", "html": "<p>Текст статьи</p>",
        "excerpt": "Кратко", "meta_title": "Мета", "meta_description": "Описание", "focus_keyword": "Капельный полив",
        "source_id": item_id, "language": "ru-RU",
    }


class RussianQueueTests(unittest.TestCase):
    def paths(self, root):
        return patch.multiple(
            ru, OUT=root, TRANS=root / "translations", SQL=root / "sql", ROLLBACK=root / "rollback",
            PACKAGES=root / "packages", STATUS_JSON=root / "status.json", STATUS_MD=root / "STATUS.md",
            ATTEMPTS=root / "attempts.json",
        )

    def test_sql_links_source_creates_ru_category_and_unique_slug_suffix(self):
        create, rollback = ru.build_sql({"id": "crop-001"}, sample())
        self.assertIn("article-translation:crop-001:ru-RU", create)
        self.assertIn("'_navar_translation_language','ru-RU'", create)
        self.assertIn("kapelnyy-poliv-crop-001-ru", create)
        self.assertIn("INSERT INTO `ha_terms`", create)
        self.assertIn("t.slug='ru'", create)
        self.assertTrue(create.strip().endswith("COMMIT;"))
        self.assertLess(create.index("ha_terms"), create.index("INSERT INTO `ha_posts`"))
        self.assertIn("article-translation:crop-001:ru-RU", rollback)
        self.assertTrue(rollback.strip().endswith("COMMIT;"))

    def test_validation_rejects_tajik_output_for_russian(self):
        source = {"title": "t", "content": "<p>متن</p>"}
        tajik = "<p>" + "Обёрии қатрагӣ ҳосилро зиёд мекунад " * 10 + "</p>"
        russian = "<p>" + "Капельный полив повышает урожайность " * 10 + "</p>"
        meta = {"title": "t", "slug": "s", "seo_title": "t", "seo_description": "d"}
        with patch.object(translate_queue, "translate_metadata", return_value=dict(meta)):
            with patch.object(translate_queue, "translate_html", return_value=tajik):
                with self.assertRaises(ValueError):
                    translate_queue.translate_validated(source, "ru-RU")
            with patch.object(translate_queue, "translate_html", return_value=russian):
                self.assertEqual(translate_queue.translate_validated(source, "ru-RU")["html"], russian)

    def test_packages_only_full_batches_and_status_is_independent(self):
        with tempfile.TemporaryDirectory() as tmp, self.paths(Path(tmp)), patch.object(ru, "PACKAGE_BATCH_SIZE", 2):
            root = Path(tmp)
            sources = [({"id": f"crop-00{i}", "completed_at": f"2026-09-0{i}"}, {}) for i in range(1, 4)]
            for item, _ in sources:
                folder = root / "translations" / item["id"]; folder.mkdir(parents=True)
                (folder / "ru-RU.json").write_text(json.dumps(sample(item["id"]), ensure_ascii=False), encoding="utf-8")
            ru.ensure_artifacts(sources)
            packages = ru.build_packages(sources)
            self.assertEqual(packages["ready_batches"], 1)
            archive = root / "packages" / packages["packages"][0]["zip"]
            with zipfile.ZipFile(archive) as z:
                names = set(z.namelist())
                self.assertIn("translations/crop-001/ru-RU.json", names)
                self.assertIn("USE `", z.read(f"sql/create-{packages['packages'][0]['batch_name']}.sql").decode()[:400])
            first_sha = packages["packages"][0]["sha256"]
            self.assertEqual(ru.build_packages(sources)["packages"][0]["sha256"], first_sha)
            status = ru.write_status(sources, {}, packages)
            self.assertEqual((status["completed"], status["pending"]), (3, 0))
            self.assertNotIn("persian_queue_paused", status)


if __name__ == "__main__":
    unittest.main()
