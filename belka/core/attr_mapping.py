#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
belka.core.attr_mapping — сопоставление «колонка-атрибут в схеме Белки» ->
«имя атрибута у источника товаров», общее для всех генераторов.

09.2026, по итогам разбора бренда. Было: механизм жил только в
`generate.py` (Базар) и работал по одному файлу `attribute_mapping.json` в
корне проекта. У ПСБ-складов маппинга не было вовсе — они сопоставляли
атрибуты только по имени, потому что apigw отдаёт attributeName уже в
терминах Белки.

Стало — единый порядок поиска значения для ЛЮБОГО склада:

  1. прямое совпадение display_name колонки с именем атрибута у источника;
  2. нормализованное (normalize_attr_key: снимает "*", "(справочник)" и
     скобочную транслитерацию, так что 'Бренд(Brend)' == 'Бренд');
  3. имена из файла маппинга этого склада;
  4. дефолт оттуда же.

ФАЙЛ У КАЖДОГО СКЛАДА СВОЙ (см. PipelineContext.resolved_attribute_mapping_file).
Ключ записи — display_name колонки Белки, он общий для всех складов, а вот
значение (имя атрибута у источника) и дефолт — разные: у Bazar это имена из
JSON-экспорта поставщика, у ПСБ-складов — attributeName апигв. С одним общим
файлом дефолты Базара («Без бренда», «Не указан») начали бы молча приезжать
в карточки Мвидео.

Дефолт из файла остаётся молчаливым (решение пользователя: маппинг
атрибутов — вне механизма подтверждения заглушек, belka/core/fill_report.py),
но КАЖДАЯ такая подстановка считается и попадает в сводку: именно так
пряталась история с брендом, где 412 ячеек из 432 тихо заполнились
значением «Без бренда».
"""
from __future__ import annotations

import difflib
from collections import Counter
from pathlib import Path

from .columns import normalize_attr_key
from .jsonio import load_json_file, save_json_file

#: порог схожести имён для авто-подсказки при интерактивном заполнении
SUGGEST_THRESHOLD_DEFAULT = 0.55


class AttributeMapper:
    """Один на прогон генератора. Держит файл маппинга склада, порядок поиска
    значения и счётчик применённых дефолтов."""

    def __init__(self, path: Path, interactive: bool = True, prompt_for_missing: bool = True,
                 suggest_threshold: float = SUGGEST_THRESHOLD_DEFAULT):
        self.path = Path(path)
        self.interactive = interactive
        #: спрашивать ли про обязательный атрибут, которого нет в файле.
        #: У Базара — да (имена у поставщика и у Белки systematically разные).
        #: У ПСБ-складов по умолчанию нет: там attributeName уже в терминах
        #: Белки, шаги 1-2 закрывают почти всё, а лишние вопросы на каждую
        #: обязательную характеристику монитора только мешают. Файл при этом
        #: работает — его можно заполнить руками.
        self.prompt_for_missing = prompt_for_missing
        self.suggest_threshold = suggest_threshold
        self.mapping = load_json_file(self.path, {})
        self.defaults_used = Counter()
        self.unresolved = set()

    # ------------------------------------------------------------- поиск --

    def value_for(self, display_name: str, lookup) -> str:
        """lookup(name) -> значение атрибута источника с таким именем (или None).

        Порядок — см. докстринг модуля. Возвращает "" если ничего не нашлось
        и дефолта нет: пустая обязательная ячейка попадёт в общий отчёт
        (belka/core/fill_report.py)."""
        value = lookup(display_name)
        if value:
            return value

        normalized = normalize_attr_key(display_name)
        value = lookup(normalized, normalized=True)
        if value:
            return value

        entry = self.mapping.get(display_name)
        if not entry:
            return ""
        for source_name in entry.get("source_names", entry.get("bazar_names", [])):
            if not source_name:
                continue
            value = lookup(source_name)
            if value:
                return value

        default = entry.get("default", "")
        if default:
            self.defaults_used[display_name] += 1
        return default

    # ------------------------------------------- интерактивное заполнение --

    def _suggest(self, display_name: str, candidates):
        target = normalize_attr_key(display_name)
        best, best_ratio = None, 0.0
        for c in candidates:
            ratio = difflib.SequenceMatcher(None, target, normalize_attr_key(c)).ratio()
            if ratio > best_ratio:
                best, best_ratio = c, ratio
        return best if best_ratio >= self.suggest_threshold else None

    def ensure_required(self, display_name: str, candidate_names, context_lines: list) -> dict | None:
        """Вызывается для каждой ОБЯЗАТЕЛЬНОЙ колонки-атрибута до сборки строк.
        Если записи нет и спрашивать нельзя — ничего не пишет в файл (см. ниже)
        и помечает атрибут как неразрешённый."""
        if display_name in self.mapping:
            return self.mapping[display_name]
        if not (self.interactive and self.prompt_for_missing):
            # 09.2026: раньше в неинтерактивном прогоне сюда записывалась
            # ПУСТАЯ заглушка {"source_names": [], "default": ""} и сохранялась
            # в файл. После этого атрибут навсегда считался «замапленным на
            # пустоту» — интерактивный прогон про него уже никогда не
            # спрашивал, а колонка молча оставалась пустой во всех шаблонах.
            # Теперь ничего не пишем: пропуск виден в общем отчёте по
            # обязательным полям, файл остаётся чистым.
            self.unresolved.add(display_name)
            return None

        print(f"\nОбязательный атрибут Белки без маппинга: '{display_name}'")
        for line in context_lines:
            print(f"  {line}")

        suggestion = self._suggest(display_name, candidate_names)
        source_name = None
        if suggestion:
            answer = input(f"  Похоже, это соответствует атрибуту источника '{suggestion}'. Верно? (да/нет): ").strip().lower()
            if answer in ("да", "y", "yes", "д"):
                source_name = suggestion
        if source_name is None:
            manual = input(f"  Имя атрибута источника для '{display_name}' (Enter — нет источника, только дефолт): ").strip()
            source_name = manual or None
        default = input(f"  Дефолтное значение для '{display_name}' (Enter — оставлять пусто): ").strip()

        entry = {"source_names": [source_name] if source_name else [], "default": default}
        self.mapping[display_name] = entry
        save_json_file(self.path, self.mapping)
        print(f"  ✔ Сохранено в {self.path.name}")
        return entry

    # ------------------------------------------------------------ отчёт --

    def report(self) -> dict:
        return {
            "file": str(self.path),
            "defaults_used": sum(self.defaults_used.values()),
            "defaults_by_attribute": dict(self.defaults_used.most_common(10)),
            "unresolved_required": sorted(self.unresolved),
        }
