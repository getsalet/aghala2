#!/usr/bin/env python3
"""Independent useful-article content queue for navar-abyari.ir.

This module is INTENTIONALLY SEPARATE from automation/city_content_queue.py:
  - City queue: advertising posts about "دستگاه/نوار خرید در شهر X" (city verticals)
  - Article queue (this file): useful content articles about drip irrigation,
    crops, products, irrigation methods, and strategic topics.

Shared infrastructure (reused, not modified):
  - Agnes API client (same model, same base URL pattern)
  - Internal-link pool (same link_index refresh logic)
  - Reversible SQL output (same per-item + combined + rollback pattern)
  - 1050+ words / 4-7 internal links / 3 image markers (same QA gate)

Output: artifacts/article-content-queue/
  queue.json, items/, sql/, rollback/, create-all-completed.sql
"""
import json, os, re, time, hashlib, struct, urllib.request, urllib.error, html, datetime as dt, tempfile, shutil, itertools, threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from agnes_json_client import call as agnes_json_call
import image_prompt_policy
import russian_status_section
import image_quality_gate
import image_caption_policy

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "article-content-queue"
ITEMS = OUT / "items"; SQL = OUT / "sql"; ROLLBACK = OUT / "rollback"
QUEUE = OUT / "queue.json"; STATUS = OUT / "status.json"
TOPICS_FILE = OUT / "topics.json"
CATALOG = ROOT / "data" / "article-catalog.json"

SITE = "https://navar-abyari.ir"
TABLE = "ha_posts"; META = "ha_postmeta"

# Agnes config (same as city queue)
AGNES_BASE = (os.getenv("AGNES_API_BASE") or "https://apihub.agnes-ai.com/v1").rstrip("/")
AGNES_KEYS = []
AGNES_KEY_SLOTS = {}
# Supports AGNES_API_KEY plus AGNES_API_KEY2..AGNES_API_KEY50 (25 configured).
for _name in ["AGNES_API_KEY", *[f"AGNES_API_KEY{i}" for i in range(2, 51)]]:
    _value = os.getenv(_name, "").strip()
    if _value and _value not in AGNES_KEYS:
        AGNES_KEYS.append(_value)
        AGNES_KEY_SLOTS[_value] = _name
AGNES_KEY = AGNES_KEYS[0] if AGNES_KEYS else ""
_KEY_COUNTER = itertools.count()
_KEY_LOCK = threading.Lock()
# Keys rejected with 401/403 are skipped for the rest of the run; only the
# secret name is ever logged.
BAD_AGNES_KEYS = set()

def active_agnes_keys():
    return [k for k in AGNES_KEYS if k not in BAD_AGNES_KEYS]

def next_agnes_key():
    if not AGNES_KEYS:
        raise RuntimeError("No Agnes API key is configured")
    with _KEY_LOCK:
        pool = active_agnes_keys()
        if not pool:
            raise RuntimeError("All Agnes API keys were rejected (401/403): " + ", ".join(sorted(AGNES_KEY_SLOTS[k] for k in BAD_AGNES_KEYS)))
        return pool[next(_KEY_COUNTER) % len(pool)]

def disable_agnes_key(key, code):
    with _KEY_LOCK:
        if key not in AGNES_KEY_SLOTS or key in BAD_AGNES_KEYS:
            return
        BAD_AGNES_KEYS.add(key)
    print(f"agnes_key_disabled secret={AGNES_KEY_SLOTS[key]} http={code} active={len(active_agnes_keys())}/{len(AGNES_KEYS)}", flush=True)

AGNES_MODEL = os.getenv("AGNES_MODEL") or "agnes-3.0-flash"

# Image config uses the same Agnes endpoint/key as the working city image
# implementation. The previous GitHub Models endpoint was incompatible with
# the Agnes image model name.
IMAGE_TOKEN = (os.getenv("IMAGE_API_KEY") or AGNES_KEY).strip()
IMAGE_MODEL = os.getenv("IMAGE_MODEL") or "agnes-image-2.5-flash"
IMAGE_ENDPOINT = os.getenv("IMAGE_ENDPOINT") or f"{AGNES_BASE}/images/generations"
UPLOAD_SUBDIR = (os.getenv("ARTICLE_IMAGE_UPLOAD_SUBDIR") or "2026/09/navar-article-generated").strip("/")
PUBLIC_UPLOAD_BASE = f"{SITE}/wp-content/uploads/{UPLOAD_SUBDIR}"

BATCH = max(1, int(os.getenv("BATCH_SIZE", "2")))
ARTICLE_WORKERS = max(1, int(os.getenv("ARTICLE_WORKERS", "2")))
IMAGE_WORKERS = max(1, int(os.getenv("IMAGE_WORKERS", "4")))
API_REQUEST_ATTEMPTS = max(1, int(os.getenv("API_REQUEST_ATTEMPTS", "4")))
IMAGE_QA_REVIEW_ATTEMPTS = max(1, int(os.getenv("IMAGE_QA_REVIEW_ATTEMPTS", "4")))
MIN_WORDS = int(os.getenv("MIN_WORDS", "1050"))
MIN_LINKS = int(os.getenv("MIN_INTERNAL_LINKS", "4"))
MAX_ATTEMPTS = max(1, int(os.getenv("MAX_ATTEMPTS", "4")))
FAILED_RETRY_CYCLES = max(0, int(os.getenv("FAILED_RETRY_CYCLES", "1")))

# SQL needs term_taxonomy_id, not term_id. Keep the old variable as a
# backwards-compatible fallback for existing repository settings.
CATEGORY_TAXONOMY_ID = int(
    os.getenv("ARTICLE_CATEGORY_TAXONOMY_ID")
    or os.getenv("ARTICLE_CATEGORY_ID")
    or "35"
)

WORD_RE = re.compile(r"[\u0600-\u06ff\u200c]+|[A-Za-z]+")
HREF_RE = re.compile(r'<a\b[^>]*href=["\']([^"\']+)', re.I)
HEX_TEXT_RE = re.compile(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}){6,}(?![0-9A-Fa-f])")
PERSIAN_RELATED_PARAGRAPH_RE = re.compile(
    r"<p\b[^>]*>(?:(?!</p>).)*?(?:مطالب مرتبط|موضوعات مرتبط)(?:(?!</p>).)*?</p>",
    re.I | re.S,
)


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def fetch_json(url, headers=None, payload=None, timeout=420):
    headers = dict(headers or {})
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    for _swap in range(len(AGNES_KEYS) + 1):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8','replace')[:800]
            auth = str(headers.get("Authorization", ""))
            key = auth[7:] if auth.startswith("Bearer ") else ""
            if e.code in (401, 403) and key in AGNES_KEY_SLOTS:
                # One invalid secret must not fail every Nth request: quarantine
                # it and retry immediately with another pool key.
                disable_agnes_key(key, e.code)
                if active_agnes_keys():
                    headers["Authorization"] = "Bearer " + next_agnes_key()
                    continue
            raise RuntimeError(f"HTTP {e.code} {url}: {detail}")
    raise RuntimeError(f"HTTP 401 {url}: no usable Agnes API key")


def normalize(s):
    s = html.unescape(str(s or "")).replace("ي","ی").replace("ك","ک").replace("\u200c"," ")
    return re.sub(r"[^\u0600-\u06ff0-9]+","",s).lower()


def esc(s):
    return (s or "").replace("\\","\\\\").replace("'","\\'").replace("\0","\\0").replace("\n","\\n").replace("\r","\\r").replace("\x1a","\\Z")


def words(text):
    return len(WORD_RE.findall(re.sub(r'<[^>]+>',' ',text or '')))


def repair_percentless_utf8_hex(text):
    """Undo the legacy sitemap fallback that stripped '%' from UTF-8 URLs."""
    def replace(match):
        token = match.group(0)
        try:
            decoded = bytes.fromhex(token).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return token
        return decoded if re.search(r"[\u0600-\u06ff]", decoded) else token

    return HEX_TEXT_RE.sub(replace, str(text or ""))


def cleanup_persian_html(body):
    """Repair legacy hex labels and keep only the best related-links block."""
    body = repair_percentless_utf8_hex(body)
    related = list(PERSIAN_RELATED_PARAGRAPH_RE.finditer(body))
    if len(related) <= 1:
        return body
    keep = max(
        related,
        key=lambda match: (
            len(re.findall(r"<a\b", match.group(0), re.I)),
            len(re.sub(r"<[^>]+>", "", match.group(0))),
        ),
    )
    pieces = []
    cursor = 0
    for match in related:
        pieces.append(body[cursor:match.start()])
        if match.start() == keep.start():
            pieces.append(match.group(0))
        cursor = match.end()
    pieces.append(body[cursor:])
    return "".join(pieces)


def internal_links(text):
    from urllib.parse import urlparse
    found = set()
    for url in HREF_RE.findall(text or ""):
        host = (urlparse(url).hostname or "").lower()
        if host == "navar-abyari.ir" or host.endswith(".navar-abyari.ir"):
            found.add(url)
    return found


def sitemap_index():
    """Same as city queue: resolve sitemap posts + use link-index cache."""
    import urllib.parse as _up, json as _json, re as _re
    cache = OUT / "link-index-sitemap.json"
    if cache.exists():
        try:
            c = _json.loads(cache.read_text(encoding="utf-8"))
            if c.get("count", 0) > 0:
                links = c["links"]
                changed = False
                for link in links:
                    repaired = repair_percentless_utf8_hex(link.get("title", ""))
                    if repaired != link.get("title", ""):
                        link["title"] = repaired
                        changed = True
                if changed:
                    cache.write_text(
                        _json.dumps(
                            {"count": len(links), "links": links},
                            ensure_ascii=False,
                            indent=1,
                        ),
                        encoding="utf-8",
                    )
                return links
        except Exception:
            pass
    urls = set()
    for sm in ("post-sitemap1.xml", "post-sitemap2.xml"):
        try:
            req = urllib.request.Request(f"{SITE}/{sm}", headers={"User-Agent":"navar-article-queue"})
            with urllib.request.urlopen(req, timeout=60) as r:
                urls.update(_re.findall(r"<loc>(.*?)</loc>", r.read().decode("utf-8","replace")))
        except Exception:
            pass
    if not urls:
        return []
    links = []
    for url in sorted(urls):
        path = _up.urlparse(url).path.strip("/")
        title = None
        try:
            slug = _up.unquote(path)
            req = urllib.request.Request(f"{SITE}/wp-json/wp/v2/posts?slug={_up.quote(slug)}&per_page=1",
                                         headers={"User-Agent":"navar-article-queue"})
            with urllib.request.urlopen(req, timeout=15) as r:
                rows = _json.loads(r.read().decode("utf-8","replace"))
                if rows:
                    t = rows[0].get("title")
                    if isinstance(t, dict):
                        title = t.get("raw") or None
                    elif isinstance(t, str) and t:
                        title = t
        except Exception:
            pass
        if not title:
            title = _up.unquote(path).replace("-", " ").strip() or "مقاله"
        links.append({"title": title, "url": url, "post_type": "post"})
    OUT.mkdir(parents=True, exist_ok=True)
    cache.write_text(_json.dumps({"count":len(links),"links":links}, ensure_ascii=False, indent=1), encoding="utf-8")
    return links


def rest_index():
    """Fully independent: pull existing posts from the live WordPress REST API.

    No dependency on the city queue, no DB dump. If the REST API is
    unreachable, returns an empty link list (the queue still initializes,
    just without a link pool — the content generator will use its fallback).
    """
    import urllib.request as _ur, json as _json
    names = set(); links = []
    page = 1
    per_page = 100
    while True:
        try:
            url = f"{SITE}/wp-json/wp/v2/posts?per_page={per_page}&page={page}&_fields=id,slug,title,type,status"
            req = _ur.Request(url, headers={"User-Agent": "navar-article-queue"})
            with _ur.urlopen(req, timeout=45) as r:
                rows = _json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            break
        if not rows:
            break
        for row in rows:
            slug = (row.get("slug") or "").strip("/")
            status = row.get("status") or "publish"
            if status not in {"publish", "draft", "pending", "future", "private"}:
                continue
            title = row.get("title")
            if isinstance(title, dict):
                title = title.get("raw") or title.get("rendered") or slug
            else:
                title = title or slug
            pt = row.get("type") or "post"
            names.add(normalize(title)); names.add(normalize(slug))
            links.append({"title": title,
                         "url": f"{SITE}/{slug}/" if pt == "post" else f"{SITE}/{pt}/{slug}/",
                         "post_type": pt})
        # Pagination: stop if fewer rows than per_page
        if len(rows) < per_page:
            break
        page += 1
    return names, links


def write_status(q, result):
    items = q.get("items", [])
    c = Counter(x.get("status", "unknown") for x in items)
    total = len(items)
    done = c["completed"]
    pct = (done / total * 100) if total else 0
    completed = sorted(
        (x for x in items if x.get("status") == "completed"),
        key=lambda x: x.get("completed_at", ""), reverse=True,
    )
    failed = sorted(
        (x for x in items if x.get("status") in {"failed", "blocked_image_model"}),
        key=lambda x: x.get("failed_at", ""), reverse=True,
    )
    processing = [x for x in items if x.get("status") == "processing"]
    eligible = select_batch(q)
    next_item = eligible[0] if eligible else None
    retry_eligible = sum(
        1 for item in items
        if item.get("status") == "failed" and is_retry_eligible(item)
    )
    retry_exhausted = sum(
        1 for item in items
        if item.get("status") == "failed" and not is_retry_eligible(item)
    )
    by_vertical = {}
    for item in items:
        vertical = item.get("vertical", "unknown")
        row = by_vertical.setdefault(vertical, Counter())
        row["total"] += 1
        row[item.get("status", "unknown")] += 1
    completed_words = [int(x.get("word_count", 0)) for x in completed if x.get("word_count")]
    average_words = round(sum(completed_words) / len(completed_words)) if completed_words else 0
    output_counts = {
        "item_json": len(list(ITEMS.glob("*.json"))) if ITEMS.exists() else 0,
        "images": len(list((OUT / "images").glob("*"))) if (OUT / "images").exists() else 0,
        "sql": len(list(SQL.glob("*.sql"))) if SQL.exists() else 0,
        "rollback": len(list(ROLLBACK.glob("*.sql"))) if ROLLBACK.exists() else 0,
    }
    package_manifest = OUT / "packages" / "manifest.json"
    package_data = {"batch_size": 50, "ready_batches": 0, "packages": []}
    if package_manifest.exists():
        try:
            package_data.update(json.loads(package_manifest.read_text(encoding="utf-8")))
        except Exception:
            pass
    translation_data = {
        "source_articles_completed": done,
        "languages": {lang: {"completed": 0, "pending": done} for lang in ("ar-IQ", "tg-TJ", "en-US")},
        "total_translation_units": done * 3,
        "completed_translation_units": 0,
        "pending_translation_units": done * 3,
        "articles_missing_any_translation": done,
        "persian_queue_paused": done > 0,
        "images": {},
    }
    translation_status = OUT / "translation-status.json"
    if translation_status.exists():
        try:
            translation_data.update(json.loads(translation_status.read_text(encoding="utf-8")))
        except Exception:
            pass
    updated = now()
    status_payload = {
        "result": result,
        "updated_at": updated,
        "total": total,
        "pending": c["pending"],
        "processing": c["processing"],
        "completed": done,
        "failed": c["failed"],
        "retry_eligible": retry_eligible,
        "retry_exhausted": retry_exhausted,
        "blocked_image_model": c["blocked_image_model"],
        "progress_percent": round(pct, 2),
        "average_completed_words": average_words,
        "text_model": AGNES_MODEL,
        "image_model": IMAGE_MODEL,
        "batch_size": BATCH,
        "category_id": CATEGORY_TAXONOMY_ID,
        "link_index_size": len(q.get("link_index", [])),
        "next_item": ({k: next_item.get(k) for k in ("id", "title", "vertical", "attempts")} if next_item else None),
        "by_vertical": {name: dict(counts) for name, counts in sorted(by_vertical.items())},
        "outputs": output_counts,
        "packages": package_data,
        "translations": translation_data,
    }
    STATUS.write_text(json.dumps(status_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    status_labels = {
        "initialized": "آماده شروع",
        "processing": "در حال پردازش",
        "ready": "آماده اجرای بعدی",
        "attention_required": "نیازمند بررسی",
        "complete": "تکمیل‌شده",
        "blocked_image_model": "مسدود به‌دلیل مدل تصویر",
        "translations_pending": "توقف موقت برای تکمیل ترجمه‌های عقب‌مانده",
    }
    bar_width = 20
    filled = min(bar_width, int(pct * bar_width / 100))
    progress_bar = "█" * filled + "░" * (bar_width - filled)
    lines = [
        "# وضعیت زنده صف تولید مقالات مفید",
        "",
        "> داشبورد خودکار تولید، کنترل کیفیت، ساخت تصویر و انتشار مستقیم در وردپرس. پس از هر اجرا به‌روزرسانی می‌شود.",
        "",
        "## نمای کلی",
        "",
        f"- آخرین بروزرسانی: `{updated}`",
        f"- وضعیت صف: **{status_labels.get(result, result)}** (`{result}`)",
        f"- پیشرفت: **{done} از {total} ({pct:.2f}٪)**",
        f"- نمودار پیشرفت: `{progress_bar}`",
        f"- تکمیل‌شده: **{done}**",
        f"- در حال پردازش: **{c['processing']}**",
        f"- در انتظار: **{c['pending']}**",
        f"- ناموفق: **{c['failed']}**",
        f"- واجد تلاش مجدد محدود: **{retry_eligible}**",
        f"- ناموفق نهایی پس از چرخه retry: **{retry_exhausted}**",
        f"- مسدود مدل تصویر: **{c['blocked_image_model']}**",
        f"- میانگین طول مقالات تکمیل‌شده: **{average_words or '—'} کلمه**",
        f"- اولویت فعلی: **{'تکمیل ترجمه‌های موجود' if translation_data.get('persian_queue_paused') else 'تولید مقاله فارسی بعدی'}**",
        "",
        "## تنظیمات تولید و انتشار",
        "",
        f"- مدل متن: `{AGNES_MODEL}`",
        f"- مدل تصویر: `{IMAGE_MODEL}`",
        f"- آبجکت الزامی تصویر: **نوار تیپ با مقیاس 03-compact (۲۰ تا ۲۳٪ عرض کادر) و جفت لی‌فلت با مقیاس 05-pair-far (۱۲ تا ۱۵٪ عرض کادر)**؛ پس‌زمینه و فعالیت متناسب با موضوع مقاله",
        f"- تعداد تصاویر هر مقاله: **{q.get('images_per_post', 3)}**",
        f"- حداقل کلمات: **{q.get('rules', {}).get('minimum_words', MIN_WORDS)}**",
        f"- لینک داخلی مجاز: **{q.get('rules', {}).get('minimum_internal_links', MIN_LINKS)} تا ۷**",
        f"- اندازه هر اجرا: **{BATCH} مقاله**",
        f"- مدیر موازی: **{ARTICLE_WORKERS} مقاله / {IMAGE_WORKERS} تصویر هم‌زمان**",
        f"- حداکثر تلاش هر مقاله: **{MAX_ATTEMPTS}**",
        f"- دسته وردپرس: **مقاله‌ها** (`{CATEGORY_TAXONOMY_ID}`)",
        f"- لینک‌های داخلی شناخته‌شده: **{len(q.get('link_index', []))}**",
        f"- روش تحویل: **بسته ZIP شامل SQL، Rollback، تصاویر و JSON — هر ۵۰ مقاله**",
        "",
        "## وضعیت بر اساس گروه موضوعی",
        "",
        "| گروه | کل | تکمیل | در انتظار | در حال پردازش | ناموفق | پیشرفت |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for vertical, counts in sorted(by_vertical.items()):
        v_total = counts["total"]
        v_done = counts["completed"]
        v_pct = v_done * 100 / v_total if v_total else 0
        lines.append(
            f"| `{vertical}` | {v_total} | {v_done} | {counts['pending']} | {counts['processing']} | "
            f"{counts['failed'] + counts['blocked_image_model']} | {v_pct:.2f}٪ |"
        )
    if not by_vertical:
        lines.append("| — | 0 | 0 | 0 | 0 | 0 | 0٪ |")

    lines += [
        "",
        "## وضعیت ترجمه مقالات تولیدشده",
        "",
        f"- مقالات فارسی تکمیل‌شده: **{translation_data.get('source_articles_completed', done)}**",
        f"- واحدهای ترجمه تکمیل‌شده: **{translation_data.get('completed_translation_units', 0)} از {translation_data.get('total_translation_units', done * 3)}**",
        f"- واحدهای ترجمه باقی‌مانده: **{translation_data.get('pending_translation_units', done * 3)}**",
        f"- مقالات فاقد حداقل یک ترجمه: **{translation_data.get('articles_missing_any_translation', done)}**",
        f"- وضعیت صف فارسی: **{'متوقف تا تکمیل ترجمه‌ها' if translation_data.get('persian_queue_paused') else 'فعال'}**",
        f"- بسته‌های ترجمه ۵۰تایی آماده: **{translation_data.get('packages', {}).get('ready_batches', 0)}**",
        "",
        "| زبان | تکمیل | باقی‌مانده | مسیر |",
        "|---|---:|---:|---|",
    ]
    language_labels = {"ar-IQ": ("عربی عراق", "/iraq/"), "tg-TJ": ("تاجیکی", "/tj/"), "en-US": ("انگلیسی", "/en/")}
    for lang in ("ar-IQ", "tg-TJ", "en-US"):
        row = translation_data.get("languages", {}).get(lang, {})
        label, prefix = language_labels[lang]
        lines.append(f"| {label} (`{lang}`) | {row.get('completed', 0)} | {row.get('pending', done)} | `{prefix}` |")
    russian = russian_status_section.load()
    if russian:
        lines.append(russian_status_section.table_row(russian))
        lines += [""] + russian_status_section.section_lines(russian)

    image_state = translation_data.get("images", {})
    lines += [
        "",
        "## وضعیت بازطراحی تصاویر",
        "",
        f"- سیاست تصویر: `{image_state.get('policy') or 'نامشخص'}`",
        f"- بازطراحی کامل: **{'بله' if image_state.get('completed') else 'خیر'}**",
        f"- تعداد مقاله‌های بررسی‌شده: **{image_state.get('total_candidates', done)}**",
        f"- خطاهای بازطراحی: **{image_state.get('failures', 0)}**",
    ]

    lines += ["", "## در حال پردازش", ""]
    if processing:
        for item in processing:
            lines.append(
                f"- **{item.get('title', '—')}** — `{item.get('id', '—')}` — مرحله: "
                f"`{item.get('failed_stage', 'generation')}` — شروع: `{item.get('started_at', '—')}`"
            )
    else:
        lines.append("- اکنون مقاله‌ای در حالت پردازش ثبت نشده است.")

    lines += ["", "## مقاله بعدی صف", ""]
    if next_item:
        lines += [
            f"- عنوان: **{next_item.get('title', '—')}**",
            f"- شناسه: `{next_item.get('id', '—')}`",
            f"- گروه: `{next_item.get('vertical', '—')}`",
            f"- تعداد تلاش قبلی: **{next_item.get('attempts', 0)}**",
        ]
    else:
        lines.append("- مورد واجد شرایطی برای اجرای بعدی وجود ندارد.")

    lines += ["", "## آخرین مقالات تکمیل‌شده", ""]
    if completed:
        for item in completed[:20]:
            title = item.get("title", "—")
            lines.append(
                f"- **{title}** — `{item.get('id', '—')}` — `{item.get('vertical', '—')}` — "
                f"{item.get('word_count', '—')} کلمه — تحویل: `SQL package` — "
                f"`{item.get('completed_at', '—')}`"
            )
    else:
        lines.append("- هنوز مقاله‌ای با موفقیت منتشر نشده است.")

    lines += ["", "## خطاهای اخیر", ""]
    if failed:
        for item in failed[:10]:
            lines.append(
                f"- **{item.get('title', '—')}** — `{item.get('id', '—')}` — مرحله: "
                f"`{item.get('failed_stage', 'unknown')}` — تلاش: **{item.get('attempts', 0)}/{MAX_ATTEMPTS}** — "
                f"`{str(item.get('last_error', 'نامشخص'))[:500]}`"
            )
    else:
        lines.append("- خطای فعالی در صف ثبت نشده است.")

    lines += ["", "## بسته‌های ۵۰تایی آماده", ""]
    packages = package_data.get("packages", [])
    if packages:
        for package in packages[-10:]:
            zip_name = package.get("zip", "—")
            lines.append(
                f"- [{zip_name}](./packages/{zip_name}) — {package.get('post_count', 0)} مقاله — "
                f"{package.get('zip_bytes', 0)} بایت — SHA256: `{package.get('zip_sha256', '—')}`"
            )
    else:
        remainder = done % int(package_data.get("batch_size", 50) or 50)
        lines.append(f"- هنوز بسته کاملی آماده نشده است؛ پیشرفت بسته جاری: **{remainder}/50 مقاله**.")

    lines += [
        "",
        "## خروجی‌های تولیدشده",
        "",
        f"- فایل JSON مقاله‌ها: **{output_counts['item_json']}**",
        f"- تصاویر تولیدشده: **{output_counts['images']}**",
        f"- فایل‌های SQL: **{output_counts['sql']}**",
        f"- فایل‌های Rollback: **{output_counts['rollback']}**",
        "",
        "## فایل‌های مدیریتی",
        "",
        "- [وضعیت ماشینی](./status.json)",
        "- [صف کامل](./queue.json)",
        "- [موضوعات تولید](./topics.json)",
        "- [SQL تجمیعی انتشار](./create-all-completed.sql)",
        "- [SQL تجمیعی بازگشت](./rollback-all-completed.sql)",
        "- [پوشه تصاویر](./images/)",
        "- [پوشه خروجی مقاله‌ها](./items/)",
        "- [وضعیت ترجمه‌ها](./translation-status.json)",
        "- [SQL تجمیعی ترجمه‌ها](./create-all-translations.sql)",
        "",
    ]
    (OUT / "STATUS.md").write_text("\n".join(lines), encoding="utf-8")


def initialize(force=False):
    for p in (OUT, ITEMS, SQL, ROLLBACK):
        p.mkdir(parents=True, exist_ok=True)
    if QUEUE.exists() and not force:
        return json.loads(QUEUE.read_text(encoding="utf-8"))

    # Load topics from the pre-seeded topics.json (produced by topic_matrix.py)
    if not TOPICS_FILE.exists():
        raise RuntimeError(f"topics.json not found at {TOPICS_FILE}. Run topic_matrix.py first.")
    topics = json.loads(TOPICS_FILE.read_text(encoding="utf-8"))
    topic_list = topics["topics"]

    # Load existing posts from the LIVE REST API to dedup (fully independent,
    # no dependency on the city queue or a DB dump).
    try:
        existing, links = rest_index()
        print(f"rest_index: {len(existing)} existing posts, {len(links)} links (live API)", flush=True)
    except Exception as e:
        existing = set(); links = []
        print(f"rest_index warning: {type(e).__name__}: {e} — proceeding with empty link pool", flush=True)

    # Build queue items from topics
    items = []
    for idx, t in enumerate(topic_list):
        norm_title = normalize(t["title"])
        # Skip if already exists in the live site
        if norm_title in existing:
            continue
        items.append({
            "id": t["id"],
            "title": t["title"],
            "slug": t["slug"],
            "focus": t["focus"],
            "vertical": t["vertical"],
            "category_id": CATEGORY_TAXONOMY_ID,
            "post_type": "post",
            "status": "pending",
            "attempts": 0
        })

    q = {
        "version": 1,
        "created_at": now(),
        "updated_at": now(),
        "source": "article-catalog.json + topics.json",
        "scope": "useful articles — separate from city queue",
        "text_model": AGNES_MODEL,
        "image_model": IMAGE_MODEL,
        "images_per_post": 3,
        "category_id": CATEGORY_TAXONOMY_ID,
        "rules": {"draft_only": False, "minimum_words": MIN_WORDS, "minimum_internal_links": MIN_LINKS},
        "items": items,
        "link_index": links
    }
    QUEUE.write_text(json.dumps(q, ensure_ascii=False, indent=2), encoding="utf-8")
    write_status(q, "initialized")
    return q


def agnes(prompt):
    # Shared client handles fenced/double-encoded JSON and bounded retries.
    import sys
    return agnes_json_call(sys.modules[__name__], prompt, attempts=4)


def ensure_minimum_internal_links(body, approved):
    body = cleanup_persian_html(body)
    used = internal_links(body)
    if len(used) >= MIN_LINKS:
        return body
    additions = []
    for link in approved:
        url = link.get("url")
        if not url or url in used:
            continue
        title = html.escape(str(link.get("title") or "مطالعه بیشتر"))
        additions.append(f'<a href="{html.escape(url, quote=True)}">{title}</a>')
        used.add(url)
        if len(used) >= MIN_LINKS:
            break
    if additions:
        related = PERSIAN_RELATED_PARAGRAPH_RE.search(body)
        addition_text = "، ".join(additions)
        if related:
            paragraph = related.group(0)
            updated = re.sub(
                r"</p>\s*$",
                ("، " if re.search(r"<a\b", paragraph, re.I) else " ")
                + addition_text
                + "</p>",
                paragraph,
                flags=re.I,
            )
            body = body[:related.start()] + updated + body[related.end():]
        else:
            body += "\n<p><strong>مطالب مرتبط:</strong> " + addition_text + "</p>"
    return body


def ensure_image_markers(body):
    clean = re.sub(r"\[\[\[IMAGE_[1-3]\]\]\]", "", body or "")
    matches = list(re.finditer(r"</p>", clean, re.I))
    if not matches:
        return clean + "\n" + "\n".join(f"[[[IMAGE_{i}]]]" for i in range(1, 4))
    positions = []
    for i in range(1, 4):
        index = min(len(matches) - 1, max(0, (len(matches) * i) // 4))
        positions.append((matches[index].end(), i))
    for pos, i in sorted(positions, reverse=True):
        clean = clean[:pos] + f"\n[[[IMAGE_{i}]]]" + clean[pos:]
    return clean


def make_content(item, links):
    CONTENT_TYPES = {"post", "page", "product", "faq"}
    approved = [x for x in links if (x.get("post_type") in CONTENT_TYPES or
              (x.get("post_type") not in {"acf-field","acf-field-group","nav_menu_item","oembed_cache","rank_math_schema","wp_global_styles","elementor_library","custom_css","gp_elements","wpcf7_contact_form","customize_changeset","gp_font"}))]
    if len(approved) > 12:
        import random
        random.Random(item["id"]).shuffle(approved)
        approved = approved[:12]
    if len(approved) < MIN_LINKS:
        raise RuntimeError(f"Text QA cannot run: only {len(approved)} approved internal links available")
    link_lines = "\n".join(f"- {x['title']} | {x['url']}" for x in approved)
    target_words = max(MIN_WORDS + 250, 1300)
    prompt = f'''برای مقاله مفید با موضوع "{item["title"]}" در حوزه آبیاری قطره‌ای و نوار تیپ، یک مقاله سئوشده و کاربردی بنویس. متن فارسی طبیعی، دست‌کم {target_words} کلمه، بدون ادعای ساختگی درباره قیمت یا نمایندگی محلی. ساختار HTML فقط با h2/h3/p/ul/ol/table/strong/a باشد و H1 نداشته باشد. موضوعات: مقدمه، بخش‌های تخصصی مرتبط با {item["focus"]}، نکات عملی، FAQ و جمع‌بندی. حداقل {MIN_LINKS} و حداکثر ۷ لینک داخلی فقط از فهرست زیر استفاده کن. سه نشانگر دقیق [[[IMAGE_1]]], [[[IMAGE_2]]], [[[IMAGE_3]]] را هرکدام یک‌بار و بین بخش‌های مناسب بگذار. JSON با کلیدهای title, meta_title, meta_description, focus_keyword, excerpt, html برگردان.
لینک‌های مجاز:
{link_lines}'''
    obj = None
    last_errors = []
    allowed = {x["url"] for x in approved}
    for attempt in range(1, 5):
        if attempt == 1:
            obj = agnes(prompt)
        else:
            repair_prompt = f'''نسخه زیر در کنترل کیفیت رد شده است. همان مقاله را ویرایش کن و کوتاه یا از نو بازنویسی نکن. خطاهای دقیق: {'; '.join(last_errors)}. متن را به دست‌کم {target_words} کلمه برسان، فقط {MIN_LINKS} تا ۷ لینک از فهرست مجاز نگه دار و هر سه نشانگر تصویر را دقیقاً یک‌بار حفظ کن. فقط JSON معتبر با همان شش کلید برگردان.
لینک‌های مجاز:
{link_lines}
نسخه قبلی:
{json.dumps(obj, ensure_ascii=False)}'''
            obj = agnes(repair_prompt)
        if not isinstance(obj, dict):
            last_errors = ["response is not a JSON object"]
            continue
        body = sanitize_links(obj.get("html", ""), allowed)
        body = ensure_minimum_internal_links(body, approved)
        body = ensure_image_markers(body)
        used = internal_links(body)
        errors = []
        word_count = words(body)
        if word_count < MIN_WORDS:
            errors.append(f"word count {word_count} below {MIN_WORDS}")
        if not (MIN_LINKS <= len(used) <= 7):
            errors.append(f"internal link count {len(used)} outside {MIN_LINKS}-7")
        if used - allowed:
            errors.append("unapproved internal links remain")
        for i in range(1, 4):
            marker_count = body.count(f"[[[IMAGE_{i}]]]")
            if marker_count != 1:
                errors.append(f"IMAGE_{i} marker count is {marker_count}, expected 1")
        for key in ("title", "meta_title", "meta_description", "focus_keyword", "excerpt", "html"):
            if not obj.get(key):
                errors.append(f"missing {key}")
        if not errors:
            obj["html"] = body
            return obj
        obj["html"] = body
        last_errors = errors
    raise RuntimeError("Text QA failed after 4 attempts: " + "; ".join(last_errors))


IMAGE_ROLE_SLUGS = {
    1: "تصویر-شاخص",
    2: "کاربرد-عملی",
    3: "جزئیات-فنی",
}


def seo_image_name(item, kind):
    raw = str(item.get("slug") or item.get("focus") or item.get("title") or item.get("id") or "article")
    slug = re.sub(r"[^0-9A-Za-z\u0600-\u06FF-]+", "-", raw.lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")[:140].strip("-") or "article"
    role = IMAGE_ROLE_SLUGS.get(int(kind), f"تصویر-{kind}")
    return f"{slug}-{role}.webp"


def image_prompt(item, kind):
    return image_prompt_policy.image_prompt(item, kind)


def _urlnorm(u):
    import urllib.parse as _up
    try:
        p = _up.urlparse(u)
        q = _up.quote(_up.unquote(p.path), safe="/")
        qs = _up.quote(_up.unquote(p.query), safe="=&") if p.query else ""
        return ("%s://%s%s" % ((p.scheme or "https"), p.netloc, q)) + (("?" + qs) if qs else "")
    except Exception:
        return u


def sanitize_links(html_text, allowed_urls):
    import re as _re
    allowed_map = {_urlnorm(u): u for u in allowed_urls}
    def _keep(match):
        tag = match.group(0)
        m = _re.search(r'href=["\']([^"\']+)', tag, _re.I)
        if not m:
            return tag
        href = m.group(1)
        canonical = allowed_map.get(_urlnorm(href))
        if canonical:
            return tag[:m.start(1)] + canonical + tag[m.end(1):]
        # Preserve visible text but strip every non-allowlisted destination.
        return _re.sub(r'</?a\b[^>]*>', '', tag)
    return _re.sub(r'<a\b[^>]*>.*?</a>', _keep, html_text, flags=_re.I | _re.S)


def image_info(blob):
    """Return extension, MIME type, width, and height for supported images."""
    if blob.startswith(b"\x89PNG\r\n\x1a\n") and len(blob) >= 24:
        width, height = struct.unpack(">II", blob[16:24])
        return ".png", "image/png", width, height
    if blob.startswith(b"\xff\xd8"):
        pos = 2
        while pos + 9 < len(blob):
            if blob[pos] != 0xFF:
                pos += 1
                continue
            marker = blob[pos + 1]
            pos += 2
            if marker in {0xD8, 0xD9}:
                continue
            if pos + 2 > len(blob):
                break
            length = int.from_bytes(blob[pos:pos + 2], "big")
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                height = int.from_bytes(blob[pos + 3:pos + 5], "big")
                width = int.from_bytes(blob[pos + 5:pos + 7], "big")
                return ".jpg", "image/jpeg", width, height
            pos += max(length, 2)
    raise RuntimeError("Generated image format is unsupported or corrupt")


def _neutral_image_bytes(Image, io):
    canvas = Image.new("RGB", (1024, 576), (232, 234, 228))
    buffer = io.BytesIO()
    canvas.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def _generate_image_once(item, kind):
    import base64
    import io
    from PIL import Image
    if not IMAGE_TOKEN:
        raise RuntimeError("IMAGE_API_KEY/AGNES_API_KEY is missing")
    data = fetch_json(
        IMAGE_ENDPOINT,
        {"Authorization": f"Bearer {next_agnes_key()}", "Content-Type": "application/json",
         "Accept": "application/json", "User-Agent": "navar-article-queue"},
        {"model": IMAGE_MODEL, "prompt": image_prompt(item, kind),
         "size": "1024x768", "return_base64": True,
         "extra_body": {"response_format": "b64_json",
                        "image": image_prompt_policy.reference_images(kind, item)}},
        600
    )
    row = data["data"][0]
    if row.get("b64_json"):
        blob = base64.b64decode(row["b64_json"])
    elif row.get("url"):
        with urllib.request.urlopen(row["url"], timeout=180) as r:
            blob = r.read()
    else:
        raise RuntimeError("Image response has neither b64_json nor url")
    if len(blob) < 10000:
        raise RuntimeError("Generated image is unexpectedly small")
    # Normalize every provider response to an optimized 16:9 WebP so the
    # extension, MIME type, dimensions, SQL metadata, and public file agree.
    image = Image.open(io.BytesIO(blob)).convert("RGB")
    width, height = image.size
    target = 16 / 9
    if width / height > target:
        new_width = int(height * target)
        left = (width - new_width) // 2
        image = image.crop((left, 0, left + new_width, height))
    else:
        new_height = int(width / target)
        top = (height - new_height) // 2
        image = image.crop((0, top, width, top + new_height))
    image = image.resize((1200, 675), Image.Resampling.LANCZOS)
    image = image_prompt_policy.add_phone_watermark(image)
    buffer = io.BytesIO()
    image.save(buffer, "WEBP", quality=60, method=6)
    blob = buffer.getvalue()
    suffix, mime, width, height = ".webp", "image/webp", 1200, 675
    img_dir = Path(item.get("_image_output_dir") or (OUT / "images"))
    img_dir.mkdir(parents=True, exist_ok=True)
    name = seo_image_name(item, kind)
    (img_dir / name).write_bytes(blob)
    return {
        "name": name,
        "sha256": hashlib.sha256(blob).hexdigest(),
        "mime": mime,
        "width": width,
        "height": height,
    }


def _is_transient_image_api_error(exc):
    message = str(exc).lower()
    return (
        isinstance(exc, (TimeoutError, urllib.error.URLError, ConnectionError, OSError))
        or (isinstance(exc, urllib.error.HTTPError) and exc.code in {408, 429, 500, 502, 503, 504})
        or any(term in message for term in (
            "http 408", "http 429", "http 500", "http 502", "http 503", "http 504",
            "too many requests", "timed out", "timeout", "remote end closed",
            "connection reset", "temporarily unavailable",
        ))
    )


def _call_image_api_with_retry(label, callback, attempts):
    last_error = None
    for request_attempt in range(1, attempts + 1):
        try:
            return callback()
        except Exception as exc:
            if not _is_transient_image_api_error(exc):
                raise
            last_error = exc
            print(
                f"{label}_retry attempt={request_attempt}/{attempts} "
                f"error={type(exc).__name__}: {str(exc)[:180]}",
                flush=True,
            )
            if request_attempt < attempts:
                time.sleep(min(16, 2 ** request_attempt))
    raise RuntimeError(
        f"{label} unavailable after {attempts} retries: "
        f"{type(last_error).__name__ if last_error else 'unknown error'}"
    )


def generate_image(item, kind):
    """Generate and replace an image only after strict visual QA passes."""
    history = []
    reviews_dir = Path(item.get("_image_review_dir") or (OUT / "image-reviews"))
    reviews_dir.mkdir(parents=True, exist_ok=True)
    review_id = str(item.get("id") or item.get("source_id") or "unknown")
    review_path = reviews_dir / f"{review_id}-{kind}.json"
    image_dir = Path(item.get("_image_output_dir") or (OUT / "images"))
    image_path = image_dir / seo_image_name(item, kind)
    feedback = ""
    try:
        prior = json.loads(review_path.read_text(encoding="utf-8"))
        prior_attempts = prior.get("attempts") or []
        if (
            prior.get("policy") == image_quality_gate.REVIEW_POLICY
            and prior.get("approved")
            and image_path.exists()
            and image_path.stat().st_size > 10000
        ):
            blob = image_path.read_bytes()
            return {
                "name": image_path.name,
                "sha256": hashlib.sha256(blob).hexdigest(),
                "mime": "image/webp",
                "width": 1200,
                "height": 675,
            }
        if prior.get("policy") == image_quality_gate.REVIEW_POLICY and prior_attempts:
            history = list(prior_attempts[-20:])
            last = prior_attempts[-1]
            feedback = str(last.get("correction_prompt") or "; ".join(last.get("reasons") or [])).strip()
            if (
                last.get("review_unavailable")
                and image_path.exists()
                and image_path.stat().st_size > 10000
            ):
                verdict = _call_image_api_with_retry(
                    "article_image_review",
                    lambda: image_quality_gate.review_image(
                        __import__(__name__), image_path, item, kind
                    ),
                    IMAGE_QA_REVIEW_ATTEMPTS,
                )
                verdict["attempt"] = "checkpoint-review"
                history.append(verdict)
                review_path.write_text(
                    json.dumps(
                        {
                            "id": review_id,
                            "kind": kind,
                            "family": image_prompt_policy.product_family(item),
                            "policy": image_quality_gate.REVIEW_POLICY,
                            "approved": bool(verdict.get("pass")),
                            "attempts": history[-20:],
                            "approved_image": image_path.name if verdict.get("pass") else None,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                if verdict.get("pass"):
                    blob = image_path.read_bytes()
                    return {
                        "name": image_path.name,
                        "sha256": hashlib.sha256(blob).hexdigest(),
                        "mime": "image/webp",
                        "width": 1200,
                        "height": 675,
                    }
                image_path.unlink(missing_ok=True)
                feedback = str(
                    verdict.get("correction_prompt")
                    or "; ".join(verdict.get("reasons", []))
                )
    except (OSError, json.JSONDecodeError):
        pass
    for attempt in range(1, image_quality_gate.MAX_IMAGE_ATTEMPTS + 1):
        candidate_item = dict(item)
        if feedback:
            candidate_item["_image_qa_feedback"] = feedback
        result = _call_image_api_with_retry(
            "article_image_generation",
            lambda: _generate_image_once(candidate_item, kind),
            API_REQUEST_ATTEMPTS,
        )
        path = image_dir / result["name"]
        try:
            verdict = _call_image_api_with_retry(
                "article_image_review",
                lambda: image_quality_gate.review_image(
                    __import__(__name__), path, item, kind
                ),
                IMAGE_QA_REVIEW_ATTEMPTS,
            )
        except Exception as exc:
            verdict = {
                "pass": False,
                "score": 0,
                "review_unavailable": True,
                "reasons": [str(exc)],
                "correction_prompt": "Retry strict visual review of the preserved candidate without regenerating it.",
            }
        verdict["attempt"] = attempt
        history.append(verdict)
        review_path.write_text(
            json.dumps(
                {
                    "id": review_id,
                    "kind": kind,
                    "family": image_prompt_policy.product_family(item),
                    "policy": image_quality_gate.REVIEW_POLICY,
                    "approved": bool(verdict.get("pass")),
                    "attempts": history[-20:],
                    "approved_image": result["name"] if verdict.get("pass") else None,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(
            f"article_image_qa id={item.get('id')} kind={kind} "
            f"attempt={attempt} pass={verdict.get('pass')} "
            f"score={verdict.get('score')} reasons={verdict.get('reasons', [])}",
            flush=True,
        )
        if verdict.get("pass"):
            return result
        if verdict.get("review_unavailable"):
            raise RuntimeError(
                "visual reviewer unavailable after retries; candidate checkpoint preserved"
            )
        path.unlink(missing_ok=True)
        feedback = str(
            verdict.get("correction_prompt")
            or "; ".join(verdict.get("reasons", []))
        )
    reasons = history[-1].get("reasons", []) if history else []
    raise RuntimeError(
        f"Image QA rejected article image {kind} after "
        f"{image_quality_gate.MAX_IMAGE_ATTEMPTS} attempts: {reasons}"
    )


def _php_attachment_metadata(image):
    relative = f"{UPLOAD_SUBDIR}/{image['name']}"
    relative_len = len(relative.encode("utf-8"))
    return (
        f'a:3:{{s:5:"width";i:{int(image["width"])};'
        f's:6:"height";i:{int(image["height"])};'
        f's:4:"file";s:{relative_len}:"{relative}";}}'
    )


def wp_post_name(value: str) -> str:
    """WordPress sanitize_title(): percent-encoded lowercase UTF-8, max 200 chars.

    WordPress looks posts up by the encoded form, so a raw UTF-8 post_name
    written by direct SQL makes the published post return 404.
    """
    text = str(value or "").lower()
    out, size, octets, need = [], 0, [], 1
    for byte in text.encode("utf-8"):
        if byte < 128:
            if size + 1 > 200:
                break
            out.append(chr(byte)); size += 1
            continue
        if not octets:
            need = 2 if byte < 224 else 3 if byte < 240 else 4
        octets.append(byte)
        if size + need * 3 > 200:
            break
        if len(octets) == need:
            out.append("".join("%" + format(b, "x") for b in octets)); size += need * 3; octets = []
    slug = "".join(out).lower()
    slug = re.sub(r"&.+?;", "", slug).replace(".", "-")
    slug = re.sub(r"[^%a-z0-9 _-]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    return re.sub(r"-+", "-", slug).strip("-")


def sql_for(item, obj, images):
    title = obj["title"]; body = obj["html"]; urls = []
    for i, image in enumerate(images, 1):
        name = image["name"]
        url = f"{PUBLIC_UPLOAD_BASE}/{name}"
        urls.append(url)
        body = body.replace(
            f"[[[IMAGE_{i}]]]",
            image_caption_policy.figure_html(url, item["title"], image_caption_policy.role_index(url, i), "fa-IR")
        )
    pt = item["post_type"]; slug = wp_post_name(item["slug"])
    queue_key = f"article-content-queue:{item['id']}"
    excerpt = obj.get("excerpt","")
    q = [
        "START TRANSACTION;",
        f"SET @queue_key='{esc(queue_key)}';",
        f"SET @post_id=(SELECT post_id FROM `{META}` WHERE meta_key='_navar_queue_item_id' AND meta_value COLLATE utf8mb4_unicode_520_ci = @queue_key COLLATE utf8mb4_unicode_520_ci LIMIT 1);",
        f"SET @slug_conflict=(SELECT ID FROM `{TABLE}` WHERE post_name='{esc(slug)}' AND post_type='{esc(pt)}' AND (@post_id IS NULL OR ID<>@post_id) LIMIT 1);",
        f"INSERT INTO `{TABLE}` (`post_author`,`post_date`,`post_date_gmt`,`post_content`,`post_title`,`post_excerpt`,`post_status`,`comment_status`,`ping_status`,`post_name`,`post_modified`,`post_modified_gmt`,`post_parent`,`guid`,`menu_order`,`post_type`,`post_mime_type`,`comment_count`) SELECT 1,NOW(),UTC_TIMESTAMP(),'{esc(body)}','{esc(title)}','{esc(excerpt)}','publish','open','open','{esc(slug)}',NOW(),UTC_TIMESTAMP(),0,'',0,'{esc(pt)}','',0 WHERE @post_id IS NULL AND @slug_conflict IS NULL;",
        "SET @post_id=COALESCE(@post_id,IF(@slug_conflict IS NULL,LAST_INSERT_ID(),NULL));",
        # A conflict deliberately leaves @post_id NULL. Every later write is
        # guarded, so an unrelated post with the same slug is never modified.
        f"INSERT INTO `{META}` (`post_id`,`meta_key`,`meta_value`) SELECT @post_id,'_navar_queue_item_id',@queue_key WHERE @post_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM `{META}` WHERE post_id=@post_id AND meta_key='_navar_queue_item_id');",
        "SELECT IF(@post_id IS NULL,'SKIPPED: slug conflict','OK') AS navar_queue_result;",
        f"UPDATE `{TABLE}` SET post_content='{esc(body)}',post_title='{esc(title)}',post_excerpt='{esc(excerpt)}',post_status='publish',post_modified=NOW(),post_modified_gmt=UTC_TIMESTAMP() WHERE ID=@post_id;",
    ]
    for key, val in [
        ("_rank_math_title", obj.get("meta_title","")),
        ("_rank_math_description", obj.get("meta_description","")),
        ("rank_math_focus_keyword", obj.get("focus_keyword","")),
        ("_navar_article_vertical", item.get("vertical","")),
    ]:
        q.append(f"UPDATE `{META}` SET meta_value='{esc(val)}' WHERE post_id=@post_id AND meta_key='{esc(key)}';")
        q.append(f"INSERT INTO `{META}` (`post_id`,`meta_key`,`meta_value`) SELECT @post_id,'{esc(key)}','{esc(val)}' WHERE @post_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM `{META}` WHERE post_id=@post_id AND meta_key='{esc(key)}');")
    # Assign category
    q.append(
        f"INSERT INTO `ha_term_relationships` (`object_id`,`term_taxonomy_id`) "
        f"SELECT @post_id,{CATEGORY_TAXONOMY_ID} WHERE @post_id IS NOT NULL AND NOT EXISTS "
        f"(SELECT 1 FROM `ha_term_relationships` WHERE object_id=@post_id AND term_taxonomy_id={CATEGORY_TAXONOMY_ID});"
    )
    for idx, (image, url) in enumerate(zip(images, urls), 1):
        name = image["name"]
        image_key = f"{queue_key}:image:{idx}"
        attached_file = f"{UPLOAD_SUBDIR}/{name}"
        metadata = _php_attachment_metadata(image)
        q.append(
            f"SET @media_{idx}=(SELECT post_id FROM `{META}` WHERE meta_key='_navar_queue_image_id' AND meta_value='{esc(image_key)}' LIMIT 1); "
            f"INSERT INTO `{TABLE}` (`post_author`,`post_date`,`post_date_gmt`,`post_content`,`post_title`,`post_excerpt`,`post_status`,`comment_status`,`ping_status`,`post_name`,`post_modified`,`post_modified_gmt`,`post_parent`,`guid`,`menu_order`,`post_type`,`post_mime_type`,`comment_count`) "
            f"SELECT 1,NOW(),UTC_TIMESTAMP(),'','{esc(item['title'])} - تصویر {idx}','','inherit','open','closed','{esc(wp_post_name(name.rsplit('.',1)[0]))}',NOW(),UTC_TIMESTAMP(),@post_id,'{esc(url)}',0,'attachment','{esc(image['mime'])}',0 WHERE @media_{idx} IS NULL AND @post_id IS NOT NULL; "
            f"SET @media_{idx}=IF(@post_id IS NULL,NULL,COALESCE(@media_{idx},LAST_INSERT_ID())); "
            f"INSERT INTO `{META}` (`post_id`,`meta_key`,`meta_value`) SELECT @media_{idx},'_navar_queue_image_id','{esc(image_key)}' WHERE @media_{idx} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM `{META}` WHERE post_id=@media_{idx} AND meta_key='_navar_queue_image_id'); "
            f"INSERT INTO `{META}` (`post_id`,`meta_key`,`meta_value`) SELECT @media_{idx},'_wp_attached_file','{esc(attached_file)}' WHERE @media_{idx} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM `{META}` WHERE post_id=@media_{idx} AND meta_key='_wp_attached_file'); "
            f"INSERT INTO `{META}` (`post_id`,`meta_key`,`meta_value`) SELECT @media_{idx},'_wp_attachment_metadata','{esc(metadata)}' WHERE @media_{idx} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM `{META}` WHERE post_id=@media_{idx} AND meta_key='_wp_attachment_metadata');"
        )
    q.append(f"UPDATE `{META}` SET meta_value=@media_1 WHERE post_id=@post_id AND meta_key='_thumbnail_id';")
    q.append(f"INSERT INTO `{META}` (`post_id`,`meta_key`,`meta_value`) SELECT @post_id,'_thumbnail_id',@media_1 WHERE @post_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM `{META}` WHERE post_id=@post_id AND meta_key='_thumbnail_id');")
    q.append("COMMIT;")
    rollback = (
        f"START TRANSACTION; SET @post_id=(SELECT post_id FROM `{META}` WHERE meta_key='_navar_queue_item_id' AND meta_value='{esc(queue_key)}' LIMIT 1); "
        f"DELETE pm FROM `{META}` pm JOIN `{TABLE}` p ON p.ID=pm.post_id WHERE p.post_parent=@post_id AND p.post_type='attachment'; "
        f"DELETE FROM `{TABLE}` WHERE post_parent=@post_id AND post_type='attachment'; "
        f"DELETE FROM `{META}` WHERE post_id=@post_id; "
        f"DELETE FROM `ha_term_relationships` WHERE object_id=@post_id; "
        f"DELETE FROM `{TABLE}` WHERE ID=@post_id; "
        f"COMMIT;\n"
    )
    return "\n".join(q) + "\n", rollback, body


def is_retry_eligible(item):
    status = item.get("status")
    attempts = int(item.get("attempts", 0))
    if status == "pending":
        return attempts < MAX_ATTEMPTS
    if status != "failed":
        return False
    return (
        attempts < MAX_ATTEMPTS
        or int(item.get("failed_retry_cycles", 0)) < FAILED_RETRY_CYCLES
    )


def prepare_attempt(item):
    """Open one bounded retry cycle after an item exhausts normal attempts."""
    if item.get("status") == "failed" and int(item.get("attempts", 0)) >= MAX_ATTEMPTS:
        item["failed_retry_cycles"] = int(item.get("failed_retry_cycles", 0)) + 1
        item["attempts"] = 0
        item["retry_cycle_started_at"] = now()
    item.update(
        status="processing",
        attempts=int(item.get("attempts", 0)) + 1,
        started_at=now(),
    )


def select_batch(q):
    """Retry failed work first, including one bounded post-exhaustion cycle."""
    eligible = [x for x in q["items"] if is_retry_eligible(x)]
    eligible.sort(
        key=lambda x: (
            x.get("status") != "failed",
            int(x.get("failed_retry_cycles", 0)),
            x.get("failed_at", ""),
            x["id"],
        )
    )
    return eligible[:BATCH]


class StageFailure(RuntimeError):
    def __init__(self, stage, error):
        self.stage = stage
        self.original = error
        super().__init__(str(error))


def generate_images_parallel(item, pool=None):
    owns_pool = pool is None
    executor = pool or ThreadPoolExecutor(max_workers=IMAGE_WORKERS, thread_name_prefix="article-image")
    results, feedback = {}, {}
    pending = {1, 2, 3}
    review_id = str(item.get("id") or item.get("source_id") or "unknown")
    set_review_path = OUT / "image-reviews" / f"{review_id}-set.json"
    set_history = []
    OUT.mkdir(parents=True, exist_ok=True)
    staging_root = OUT / ".image-role-checkpoints" / review_id
    staging_images = staging_root / "images"
    staging_reviews = staging_root / "image-reviews"
    staging_images.mkdir(parents=True, exist_ok=True)
    staging_reviews.mkdir(parents=True, exist_ok=True)
    completed = False
    try:
        max_set_attempts = max(1, int(os.getenv("IMAGE_SET_QA_ATTEMPTS", "4")))
        for set_attempt in range(1, max_set_attempts + 1):
            futures = {}
            for kind in sorted(pending):
                candidate = dict(item)
                candidate["_image_output_dir"] = str(staging_images)
                candidate["_image_review_dir"] = str(staging_reviews)
                if feedback.get(kind): candidate["_image_qa_feedback"] = feedback[kind]
                futures[executor.submit(generate_image, candidate, kind)] = kind
            for future in as_completed(futures):
                kind = futures[future]; results[kind] = future.result()
            paths = [staging_images / results[kind]["name"] for kind in range(1, 4)]
            verdict = _call_image_api_with_retry(
                "article_image_set_review",
                lambda: image_quality_gate.review_image_set(
                    __import__(__name__), paths, item
                ),
                IMAGE_QA_REVIEW_ATTEMPTS,
            )
            verdict["set_attempt"] = set_attempt; set_history.append(verdict)
            set_review_path.parent.mkdir(parents=True, exist_ok=True)
            set_review_path.write_text(json.dumps({"id":review_id,"policy":image_quality_gate.REVIEW_POLICY,"history":set_history},ensure_ascii=False,indent=2),encoding="utf-8")
            print(f"article_image_set_qa id={review_id} attempt={set_attempt} pass={verdict.get('pass')} score={verdict.get('score')} duplicate_roles={verdict.get('duplicate_roles',[])} reasons={verdict.get('reasons',[])}",flush=True)
            if verdict.get("pass"):
                final_images = OUT / "images"
                final_reviews = OUT / "image-reviews"
                final_images.mkdir(parents=True, exist_ok=True)
                final_reviews.mkdir(parents=True, exist_ok=True)
                for kind in range(1, 4):
                    staged_image = staging_images / results[kind]["name"]
                    if not staged_image.exists():
                        raise RuntimeError(f"Approved staged image is missing for role {kind}")
                    os.replace(staged_image, final_images / staged_image.name)
                    staged_review = staging_reviews / f"{review_id}-{kind}.json"
                    if staged_review.exists():
                        os.replace(staged_review, final_reviews / staged_review.name)
                completed = True
                return [results[kind] for kind in range(1, 4)]
            pending = set(verdict.get("duplicate_roles") or (1,2,3))
            correction = str(verdict.get("correction_prompt") or "; ".join(verdict.get("reasons",[])))
            for kind in pending:
                feedback[kind] = correction + f" Create a new role-{kind} scene that is clearly unlike the other two approved images."
                (staging_images / results[kind]["name"]).unlink(missing_ok=True)
                (staging_reviews / f"{review_id}-{kind}.json").unlink(missing_ok=True)
                results.pop(kind,None)
        raise RuntimeError(f"Image-set diversity gate rejected the three-image editorial set after {max_set_attempts} rounds")
    finally:
        if owns_pool:
            executor.shutdown(wait=True, cancel_futures=False)
        if completed:
            shutil.rmtree(staging_root, ignore_errors=True)
        else:
            # Keep individually approved roles for the next rebuild attempt;
            # remove only completely empty checkpoints.
            try:
                if not any(staging_images.iterdir()) and not any(staging_reviews.iterdir()):
                    shutil.rmtree(staging_root, ignore_errors=True)
            except OSError:
                pass


def _produce_item(item, links, image_pool):
    try:
        obj = make_content(item, links)
    except Exception as exc:
        raise StageFailure("text", exc) from exc
    try:
        images = generate_images_parallel({**item, **obj}, image_pool)
    except Exception as exc:
        raise StageFailure("image", exc) from exc
    return obj, images


def process(q):
    # A killed runner may leave an item in processing. Make it retryable.
    for item in q["items"]:
        if item.get("status") == "processing":
            item.update(status="failed", failed_at=now(), last_error="Previous runner stopped while processing")

    if any(x["status"] == "blocked_image_model" for x in q["items"]):
        write_status(q, "blocked_image_model")
        raise RuntimeError(q.get("image_model_error", "Image model is blocked"))

    batch = select_batch(q)
    try:
        existing_urls = {l["url"] for l in q.get("link_index", [])}
        smlinks = [l for l in sitemap_index() if l["url"] not in existing_urls]
        if smlinks:
            q["link_index"] = q.get("link_index", []) + smlinks
    except Exception as e:
        print(f"sitemap_index warning: {type(e).__name__}: {e}", flush=True)

    if not batch:
        exhausted = any(x.get("status") == "failed" for x in q["items"])
        write_status(q, "attention_required" if exhausted else "complete")
        return

    # The coordinator owns queue state. Workers only call APIs and return
    # results, preventing concurrent JSON/SQL/Git state writes.
    for item in batch:
        prepare_attempt(item)
    q["updated_at"] = now()
    QUEUE.write_text(json.dumps(q, ensure_ascii=False, indent=2), encoding="utf-8")
    write_status(q, "processing")

    batch_errors = []
    deferred_image_qa = []
    article_workers = min(ARTICLE_WORKERS, len(batch))
    with ThreadPoolExecutor(max_workers=IMAGE_WORKERS, thread_name_prefix="article-image") as image_pool:
        with ThreadPoolExecutor(max_workers=article_workers, thread_name_prefix="article-text") as article_pool:
            future_items = {
                article_pool.submit(_produce_item, item, q["link_index"], image_pool): item
                for item in batch
            }
            for future in as_completed(future_items):
                item = future_items[future]
                stage = "text"
                try:
                    obj, images = future.result()
                    stage = "sql"
                    insert, rollback, body = sql_for(item, obj, images)
                    (SQL / f"{item['id']}.sql").write_text(insert, encoding="utf-8")
                    (ROLLBACK / f"{item['id']}.sql").write_text(rollback, encoding="utf-8")
                    completed_at = now()
                    item.update(
                        status="completed", completed_at=completed_at,
                        word_count=words(body), images=[x["name"] for x in images],
                        image_sha256=[x["sha256"] for x in images],
                        image_generation_mode="reference-conditioned-3d-rerender-approved-scales-topic-first",
                        image_rebuild_policy="reference-rerender-3d-v11-no-human-no-container-full-rebuild",
                        delivery="sql_package", last_error="",
                    )
                    item.pop("failed_stage", None);item.pop("failed_at", None);item.pop("started_at", None)
                    item.pop("failed_retry_cycles", None);item.pop("retry_cycle_started_at", None)
                    (ITEMS / f"{item['id']}.json").write_text(
                        json.dumps({**item, **obj, "html": body, "images": images}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                except Exception as exc:
                    if isinstance(exc, StageFailure):
                        stage=exc.stage;error=exc.original
                    else:
                        error=exc
                    msg=str(error)[:900]
                    permanent_codes=("HTTP 400 ","HTTP 401 ","HTTP 403 ","HTTP 404 ")
                    blocked=stage=="image" and (
                        "IMAGE_API_KEY/AGNES_API_KEY is missing" in msg
                        or any(code in msg for code in permanent_codes)
                    )
                    qa_retry = stage == "image" and any(
                        signal in msg for signal in (
                            "Image QA rejected article image",
                            "Image-set diversity gate rejected",
                        )
                    )
                    if qa_retry:
                        # Quarantine a visually impossible item so a stochastic
                        # image gate cannot monopolize every scheduled batch.
                        # The stored diagnostics preserve it for later review.
                        item.update(
                            status="failed",
                            last_error=msg,
                            failed_stage="image_qa_deferred",
                            failed_at=now(),
                        )
                        item["attempts"] = MAX_ATTEMPTS
                        deferred_image_qa.append(item["id"])
                    else:
                        item.update(status="blocked_image_model" if blocked else "failed", failed_at=now(), last_error=msg, failed_stage=stage)
                    item.pop("started_at", None)
                    batch_errors.append(f"{item['id']} ({stage}): {msg}")
                    if blocked:q["image_model_error"]=msg
                q["updated_at"] = now()
                QUEUE.write_text(json.dumps(q, ensure_ascii=False, indent=2), encoding="utf-8")
                write_status(q, "processing")

    (OUT / "create-all-completed.sql").write_text(
        "\n".join(["-- Review before importing. Generated articles are intentionally published."] +
                  [p.read_text(encoding="utf-8") for p in sorted(SQL.glob("*.sql"))]), encoding="utf-8")
    (OUT / "rollback-all-completed.sql").write_text(
        "\n".join(p.read_text(encoding="utf-8") for p in sorted(ROLLBACK.glob("*.sql"))), encoding="utf-8")
    if any(x.get("status") == "blocked_image_model" for x in q["items"]):
        write_status(q, "blocked_image_model")
    else:
        exhausted=any(x.get("status")=="failed" and int(x.get("attempts",0))>=MAX_ATTEMPTS for x in q["items"])
        write_status(q, "attention_required" if exhausted else "ready")
    if deferred_image_qa:
        print("deferred_image_qa_items=" + ",".join(deferred_image_qa), flush=True)
    if batch_errors and not deferred_image_qa:
        raise RuntimeError("Queue item failed: " + " | ".join(batch_errors))


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--init-only", action="store_true")
    ap.add_argument("--force-init", action="store_true")
    a = ap.parse_args()
    q = initialize(a.force_init)
    if not a.init_only:
        process(q)


if __name__ == "__main__":
    main()
