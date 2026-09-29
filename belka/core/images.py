#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Работа с изображениями товара — одно место на все склады.

  - extract_images  — Базар: список картинок -> (основное, доп. через разделитель);
                      переехала сюда из stages/generate.py без изменений.
  - rotate_images   — Мвидео (флаг WarehouseConfig.rotate_images): убрать
                      основное и последнее дополнительное, первое дополнительное
                      сделать основным. Чистая функция: ничего не пишет и не
                      печатает, только возвращает ImageRotation — что было, что
                      стало, что удалено и под какими номерами. Лог и отчёт
                      строит вызывающий stage.

Правила rotate_images (решение пользователя, 09.2026):
  - основное есть, дополнительных нет      -> ничего не меняем, только отмечаем;
  - основное есть, дополнительное ровно 1  -> основное оставляем, доп. удаляем;
  - основное есть, дополнительных >= 2     -> удаляем основное и последнее доп.,
                                              основным становится доп. №1,
                                              в доп. остаются №2..№(n-1);
  - основного нет                          -> ничего не меняем (не из чего
                                              решать, что удалять), отмечаем.
Перед правилами убираются пустые ссылки и дубли (в т.ч. копия основного
внутри дополнительных) — иначе «первое доп.» могло бы оказаться тем же
самым файлом, что и удаляемое основное. Номера в отчёте — позиции в
ИСХОДНОМ списке, с 1.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .columns import IMAGES_SEPARATOR

#: Сколько дополнительных картинок принимает колонка шаблона Белки
#: ("Дополнительное Изображения (до 10 штук)"). Здесь только для
#: предупреждения — лишнее не обрезаем молча.
MAX_ADDITIONAL_IMAGES = 10

# коды действий rotate_images — они же попадают в отчёт и в summary
NO_IMAGES = "no_images"
SINGLE_MAIN_UNCHANGED = "single_main_unchanged"
SINGLE_ADDITIONAL_REMOVED = "single_additional_removed"
ROTATED = "rotated"
NO_MAIN_UNCHANGED = "no_main_unchanged"

ACTION_TITLES = {
    NO_IMAGES: "картинок нет",
    SINGLE_MAIN_UNCHANGED: "одна картинка (основное) — без изменений",
    SINGLE_ADDITIONAL_REMOVED: "одно доп. — удалено, основное оставлено",
    ROTATED: "основное и последнее доп. удалены, основное <- доп. №1",
    NO_MAIN_UNCHANGED: "нет основного — без изменений",
}


def extract_images(images: list):
    """Базар: первая уникальная картинка — основная, остальные — дополнительные."""
    if not images:
        return None, None
    unique_images = list(dict.fromkeys(images))
    main_image = unique_images[0]
    additional = IMAGES_SEPARATOR.join(unique_images[1:]) if len(unique_images) > 1 else None
    return main_image, additional


@dataclass
class ImageRotation:
    action: str
    main_before: str | None
    additional_before: list            # как пришло от источника (до чистки дублей)
    main_after: str | None
    additional_after: list
    # (откуда, № в исходном списке, url); откуда: "основное" | "доп."
    removed: list = field(default_factory=list)
    promoted_from: int | None = None   # № доп. картинки, ставшей основной
    duplicates: list = field(default_factory=list)   # (№ в доп., url) — убранные дубли/пустые

    @property
    def changed(self) -> bool:
        return self.main_after != self.main_before or self.additional_after != self.additional_before

    def describe(self) -> str:
        """Одна строка для лога stage'а."""
        n_add = len(self.additional_before)
        parts = [f"было осн. {1 if self.main_before else 0} + доп. {n_add}"]
        if self.duplicates:
            nums = ", ".join(f"#{i}" for i, _ in self.duplicates)
            parts.append(f"убраны дубли/пустые в доп.: {nums}")
        if self.removed:
            items = []
            for src, pos, _ in self.removed:
                if src == "основное":
                    items.append("основное (#1)")
                else:
                    items.append(f"доп. #{pos} из {n_add}")
            parts.append("удалено: " + ", ".join(items))
        if self.promoted_from:
            parts.append(f"основное <- доп. #{self.promoted_from}")
        if not self.changed and not self.duplicates:
            parts.append(ACTION_TITLES[self.action])
        parts.append(f"стало осн. {1 if self.main_after else 0} + доп. {len(self.additional_after)}")
        if len(self.additional_after) > MAX_ADDITIONAL_IMAGES:
            parts.append(f"⚠ доп. больше {MAX_ADDITIONAL_IMAGES} — Белка может не принять")
        return "; ".join(parts)


def _clean(value) -> str | None:
    s = str(value).strip() if value is not None else ""
    return s or None


def rotate_images(main, additional) -> ImageRotation:
    """См. docstring модуля. main — url основного (или None), additional —
    список url дополнительных (или None)."""
    main_before = _clean(main)
    additional_before = list(additional or [])

    # 1. чистка: пустые, дубли, копия основного внутри доп. Запоминаем
    #    исходные номера (с 1), чтобы в отчёте ссылаться на них.
    kept: list[tuple[int, str]] = []
    duplicates: list[tuple[int, str]] = []
    seen = {main_before} if main_before else set()
    for pos, raw in enumerate(additional_before, 1):
        url = _clean(raw)
        if url is None or url in seen:
            duplicates.append((pos, raw))
            continue
        seen.add(url)
        kept.append((pos, url))

    def result(action, main_after, add_after, removed=(), promoted=None):
        return ImageRotation(action, main_before, additional_before, main_after,
                             [u for _, u in add_after], list(removed), promoted, duplicates)

    if not main_before:
        return result(NO_IMAGES if not kept else NO_MAIN_UNCHANGED, None, kept)
    if not kept:
        return result(SINGLE_MAIN_UNCHANGED, main_before, [])
    if len(kept) == 1:
        pos, url = kept[0]
        return result(SINGLE_ADDITIONAL_REMOVED, main_before, [], removed=[("доп.", pos, url)])

    first_pos, first_url = kept[0]
    last_pos, last_url = kept[-1]
    return result(
        ROTATED, first_url, kept[1:-1],
        removed=[("основное", 1, main_before), ("доп.", last_pos, last_url)],
        promoted=first_pos,
    )


REPORT_HEADERS = [
    "Id Товара (offer_id)", "Действие", "Было основных", "Было доп.",
    "Удалено (откуда, №)", "Основное <- доп. №", "Дубли/пустые в доп. убраны (№)",
    "Стало основных", "Стало доп.", "Старое основное (url)", "Новое основное (url)",
    "Удалённые (url)",
]


def rotation_report_row(product_id, r: ImageRotation) -> list:
    removed = ", ".join(("основное #1" if src == "основное" else f"доп. #{pos}")
                        for src, pos, _ in r.removed)
    return [
        str(product_id), ACTION_TITLES[r.action],
        1 if r.main_before else 0, len(r.additional_before),
        removed or None, r.promoted_from,
        ", ".join(f"#{i}" for i, _ in r.duplicates) or None,
        1 if r.main_after else 0, len(r.additional_after),
        r.main_before, r.main_after,
        "\n".join(u for _, _, u in r.removed) or None,
    ]


def write_rotation_report(rows: list, out_path) -> None:
    """Отдельный xlsx-отчёт по картинкам. НЕ catalog_*.xlsx — тот уходит на
    импорт в Белку, и лишняя колонка там может сломать импорт."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Изображения"
    ws.append(REPORT_HEADERS)
    for row in rows:
        ws.append(row)
    ws.freeze_panes = "A2"
    for col, width in zip("ABCDEFGHIJKL", (18, 44, 9, 9, 22, 12, 16, 9, 9, 50, 50, 50)):
        ws.column_dimensions[col].width = width
    wb.save(out_path)
