#!/usr/bin/env python3
"""Deterministic, language-correct alt text and captions for article images.

Every generated article has three images whose file names end with a role:
  ...-تصویر-شاخص.webp (overview), ...-کاربرد-عملی.webp (practical use),
  ...-جزئیات-فنی.webp (technical details).
The old pipeline wrote the Persian alt text into every translation and used the
internal mixed-language ``focus`` string as the caption. This module rebuilds
both from the post title in the post's own language plus the image role, in
the same format already used on the live site for fa/ar/tg/en posts:
  alt = caption = "<title> - <role>"
"""
from __future__ import annotations

import html as html_lib
import re

ROLE_LABELS = {
    "fa-IR": ("نمای کلی", "کاربرد عملی", "جزئیات فنی"),
    "ru-RU": ("Общий вид", "Практическое применение", "Технические детали"),
    "en-US": ("Overview", "Practical application", "Technical details"),
    "ar-IQ": ("نظرة عامة", "التطبيق العملي", "التفاصيل الفنية"),
    "tg-TJ": ("Намои умумӣ", "Истифодаи амалӣ", "Тафсилоти техникӣ"),
}
ROLE_SUFFIXES = ("تصویر-شاخص", "کاربرد-عملی", "جزئیات-فنی")
GENERATED_MARK = "/navar-article-generated/"
FIGURE = re.compile(r"<figure\b[^>]*>.*?</figure>", re.IGNORECASE | re.DOTALL)
IMG_SRC = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*\"([^\"]+)\"", re.IGNORECASE)
TAGS = re.compile(r"<[^>]+>")


def short_title(title: str, limit: int = 90) -> str:
    text = re.sub(r"\s+", " ", TAGS.sub(" ", html_lib.unescape(str(title or "")))).strip()
    for sep in (":", " — ", " - ", " | "):
        head = text.split(sep, 1)[0].strip()
        if sep in text and len(head) >= 15:
            text = head
            break
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:-—")
    return text


def role_index(src: str, fallback: int) -> int:
    stem = re.sub(r"\.[a-z0-9]+$", "", html_lib.unescape(src).split("?", 1)[0].rsplit("/", 1)[-1], flags=re.I)
    for index, suffix in enumerate(ROLE_SUFFIXES, 1):
        if stem.endswith(suffix):
            return index
    return min(max(fallback, 1), 3)


def image_texts(title: str, index: int, lang: str) -> tuple[str, str]:
    labels = ROLE_LABELS.get(lang, ROLE_LABELS["en-US"])
    label = labels[min(max(index, 1), 3) - 1]
    name = short_title(title)
    text = f"{name} - {label}" if name else label
    return text, text


def figure_parts(src: str, title: str, index: int, lang: str, width: int = 1200, height: int = 675) -> tuple[str, str]:
    """Figure HTML split where WordPress' ``class="wp-image-ID"`` belongs."""
    alt, caption = image_texts(title, index, lang)
    head = f'<figure class="wp-block-image size-large"><img src="{src}" alt="{html_lib.escape(alt, quote=True)}"'
    tail = (
        f' width="{int(width)}" height="{int(height)}" loading="lazy" decoding="async"/>'
        f'<figcaption class="wp-element-caption">{html_lib.escape(caption, quote=False)}</figcaption></figure>'
    )
    return head, tail


def figure_html(src: str, title: str, index: int, lang: str, attachment_id=None) -> str:
    head, tail = figure_parts(src, title, index, lang)
    klass = f' class="wp-image-{int(attachment_id)}"' if attachment_id else ""
    return head + klass + tail


def generated_figures(body: str):
    """Yield (match, src, role_index) for every generated article image figure."""
    position = 0
    for match in FIGURE.finditer(str(body or "")):
        found = IMG_SRC.search(match.group(0))
        if not found or GENERATED_MARK not in html_lib.unescape(found.group(1)):
            continue
        position += 1
        yield match, found.group(1), role_index(found.group(1), position)


def normalize_figures(body: str, title: str, lang: str) -> str:
    body = str(body or "")
    pieces, cursor = [], 0
    for match, src, index in generated_figures(body):
        pieces.append(body[cursor:match.start()])
        found = re.search(r"\bwp-image-(\d+)", match.group(0))
        pieces.append(figure_html(src, title, index, lang, found.group(1) if found else None))
        cursor = match.end()
    pieces.append(body[cursor:])
    return "".join(pieces)
