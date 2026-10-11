#!/usr/bin/env python3
"""Show Russian (ru-RU) queue progress inside the main article STATUS.md.

Used in two ways:
- article_content_queue.write_status() calls section_lines()/table_row() when
  it renders the full dashboard.
- The Russian workflow runs this file after every slice to patch only the
  Russian row and section of the existing STATUS.md, without re-rendering or
  touching the main queue state.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RU_STATUS = ROOT / "artifacts" / "russian-article-queue" / "status.json"
MAIN_STATUS = ROOT / "artifacts" / "article-content-queue" / "STATUS.md"
START = "<!-- russian-queue:start -->"
END = "<!-- russian-queue:end -->"
ROW_PREFIX = "| روسی (`ru-RU`)"
ROW_RE = re.compile(r"^\| روسی \(`ru-RU`\).*$\n?", re.M)


def load(path: Path = RU_STATUS) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def table_row(data: dict) -> str:
    return f"{ROW_PREFIX} — صف جداگانه | {data.get('completed', 0)} | {data.get('pending', 0)} | `/ru/` |"


def section_lines(data: dict) -> list[str]:
    total = int(data.get("source_articles_completed", 0) or 0)
    done = int(data.get("completed", 0) or 0)
    pending = int(data.get("pending", max(0, total - done)) or 0)
    pct = float(data.get("progress_percent", (100 * done / total) if total else 0.0))
    filled = min(20, int(pct // 5))
    packages = data.get("packages", {}) or {}
    last = data.get("last_run", {}) or {}
    state = "تکمیل" if total and not pending else "فعال"
    lines = [
        START,
        "## وضعیت ترجمه روسی (صف جداگانه)",
        "",
        f"- ترجمه‌های روسی انجام‌شده: **{done} از {total} ({pct:.2f}٪)**",
        f"- نمودار پیشرفت: `{'█' * filled}{'░' * (20 - filled)}`",
        f"- باقی‌مانده: **{pending}**",
        f"- وضعیت صف روسی: **{state}** — مسیر `/ru/`",
        f"- مقالات با تلاش ناموفق (در صف تلاش مجدد): **{data.get('articles_with_failed_attempts', 0)}**",
        f"- بسته‌های روسی ۵۰تایی آماده: **{packages.get('ready_batches', 0)}** — آپلودشده با FTPS: **{data.get('uploaded_packages', 0)}**",
    ]
    if last:
        lines.append(
            f"- آخرین اجرای صف روسی: **{last.get('succeeded', 0)} موفق / {last.get('failed', 0)} ناموفق از {last.get('selected', 0)}** — `{last.get('finished_at', '')}`"
        )
    lines += [
        f"- آخرین بروزرسانی صف روسی: `{data.get('updated_at', '')}`",
        "- این صف صف تولید فارسی را متوقف نمی‌کند. [داشبورد کامل صف روسی](../russian-article-queue/STATUS.md)",
        END,
    ]
    return lines


def patch(text: str, data: dict) -> str:
    if not data:
        return text
    row = table_row(data)
    if ROW_RE.search(text):
        text = ROW_RE.sub(row + "\n", text, count=1)
    else:
        en = re.search(r"^\| [^\n]*\(`en-US`\)[^\n]*$", text, re.M)
        if en:
            text = text[: en.end()] + "\n" + row + text[en.end():]
    block = "\n".join(section_lines(data))
    if START in text and END in text:
        before, rest = text.split(START, 1)
        after = rest.split(END, 1)[1]
        return before + block + after
    anchor = text.find("## وضعیت بازطراحی تصاویر")
    if anchor < 0:
        anchor = text.find("## در حال پردازش")
    if anchor >= 0:
        return text[:anchor] + block + "\n\n" + text[anchor:]
    return text.rstrip("\n") + "\n\n" + block + "\n"


def main() -> int:
    data = load()
    if not data or not MAIN_STATUS.exists():
        print("russian_status_section: nothing to patch")
        return 0
    old = MAIN_STATUS.read_text(encoding="utf-8")
    new = patch(old, data)
    if new != old:
        MAIN_STATUS.write_text(new, encoding="utf-8")
        print(f"russian_status_section: updated main STATUS.md ({data.get('completed', 0)}/{data.get('source_articles_completed', 0)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
