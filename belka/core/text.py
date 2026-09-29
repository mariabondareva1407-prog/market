#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Общие текстовые хелперы.

  - casefold_ru — использовалась дословно-одинаково в generate/import (и не
    только там), поэтому теперь она одна.
  - strip_html  — снимает HTML-разметку из текста (описания товаров), сохраняя
    структуру переносами строк. Общая на весь проект; где её применять —
    решает склад (сейчас только Мвидео: WarehouseConfig.strip_html_description).
"""
from __future__ import annotations

import re
from html.parser import HTMLParser


def casefold_ru(value) -> str:
    v = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return v.replace("ё", "е")


# --- strip_html ---------------------------------------------------------------

# теги, после/перед которыми в тексте должен быть перенос строки
_BLOCK_TAGS = {
    "p", "div", "br", "ul", "ol", "li", "tr", "table", "section", "article",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "hr", "dl", "dt", "dd",
}
# теги, чьё СОДЕРЖИМОЕ — не текст для покупателя
_SKIP_CONTENT_TAGS = {"script", "style", "head", "title", "noscript", "template"}
# грубая проверка «похоже на разметку»: тег, закрывающий тег, комментарий
_LOOKS_LIKE_HTML = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>|<!--|&(?:[a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);")
_TABLE_CELLS = {"td", "th"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)   # &nbsp; &amp; &#171; -> символы
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth += 1
        elif tag == "li":
            self.parts.append("\n• ")
        elif tag in ("ul", "ol", "table"):
            pass                                     # перенос даст первый <li>, без пустой строки
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in _TABLE_CELLS:
            self.parts.append(" ")

    def handle_startendtag(self, tag, attrs):   # <br/>, <hr/>
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS and tag not in ("li", "tr"):   # перенос перед пунктом/строкой ставит сам <li>/<tr>
            self.parts.append("\n")
        elif tag in _TABLE_CELLS:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def _normalize_whitespace(text: str) -> str:
    text = text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.split("\n")]
    # не больше одной пустой строки подряд
    out: list[str] = []
    for line in lines:
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()


def _strip_once(text: str) -> str:
    parser = _TextExtractor()
    parser.feed(text)
    parser.close()
    return "".join(parser.parts)


def strip_html(value):
    """Текст без HTML-тегов.

    <br>, <p>, <div>, заголовки -> перенос строки; <li> -> строка с «• »;
    <script>/<style> удаляются вместе с содержимым; сущности (&nbsp;, &amp;,
    &#171;) декодируются; пробелы схлопываются, пустых строк подряд не
    больше одной. Экранированная разметка (&lt;p&gt;) после декодирования
    снимается вторым проходом.

    Не-строка (None, число) возвращается как есть. Текст без разметки
    возвращается БЕЗ изменений — функцию безопасно вызывать на любом поле.
    Пустой результат (была одна разметка) -> None, чтобы сработала обычная
    заглушка обязательного поля.
    """
    if not isinstance(value, str) or not _LOOKS_LIKE_HTML.search(value):
        return value
    text = value
    for _ in range(2):          # второй проход — для &lt;p&gt;-экранированной разметки
        text = _strip_once(text)
        if not _LOOKS_LIKE_HTML.search(text):
            break
    return _normalize_whitespace(text) or None
