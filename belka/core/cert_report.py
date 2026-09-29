#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
belka.core.cert_report — ОДНО место, где проект решает «какого цвета
сертификат». Два разных входа, одна и та же трактовка цвета:

  1) Цветной xlsx-отчёт Bazar: лист на категорию, шапка на 2-й строке
     (row=2), данные с 3-й строки, статус — ЗАЛИВКА ячейки колонки
     "S_CERT:Сертификат", товар — колонка "N:Номер товара" (то же поле,
     что "Id Товара"/vendor_code во всём остальном пайплайне).
  2) Файл-ассортимент Мвидео: одна плоская таблица, статус — заливка
     ячейки колонки "Номер сертификата" (см. core/psb_api.py и
     stages/certificates_assortment.py).

Три состояния:
  - "red"   — красная заливка          — сертификат проблемный
  - "green" — зелёная заливка          — сертификат подтверждён
  - "white" — ВСЁ ОСТАЛЬНОЕ            — «не красный и не зелёный»:
              нет заливки, серая, жёлтая, любая другая

Красный и зелёный определяются НЕ по точному совпадению RGB (раньше было
именно так: только FFFF0000 и FF00B050), а по оттенку — см. classify_rgb.
Причина: тёмно-красный FFC00000, пастельные стили Excel «Bad» (FFFFC7CE) и
«Good» (FFC6EFCE) — это те же самые «красный» и «зелёный» для человека, а
при точном сравнении они молча уезжали в "white" и красный товар попадал в
шаблон при `--green --white`. Цвет, который не удалось отнести ни к
красному, ни к зелёному, но который ВСЁ ЖЕ заливка (не пустая ячейка),
считается "white" и ДОПОЛНИТЕЛЬНО попадает в счётчик unclassified — чтобы
такое было видно в логе, а не «просто меньше товаров прошло фильтр».

Используется:
  - stage'ом 'certificates' (belka/stages/certificates.py) — для него white
    трактуется как green (переключение allow_publish было и остаётся
    бинарным: красный -> off, всё остальное -> on);
  - stage'ом 'certificates' Мвидео (certificates_assortment.py) — там
    наоборот: green -> on, всё остальное -> off;
  - фильтром --green/--white/--red у генераторов шаблонов (CertFilterMixin
    ниже; belka/stages/generate.py и belka/stages/psb_generate.py), где
    различие между green и white важно — туда включаются только товары с
    выбранными статусами.
"""
from __future__ import annotations

import colorsys
from pathlib import Path

import openpyxl

HEADER_ROW = 2
DATA_START_ROW = 3
SKIP_SHEETS = {"Инструкция", "Справочники"}

CERT_COLUMN_KEY = "S_CERT"
ARTICLE_COLUMN_KEY = "N"  # именно N:Номер товара уходит в vendor_code при загрузке, не S_ART

# Эталонные цвета проекта — остаются как были (на них ссылаются
# certificates.py/certificates_assortment.py и README), но теперь это лишь
# «типичные представители», а не единственное, что распознаётся.
RED_RGB = "FFFF0000"
GREEN_RGB = "FF00B050"

# приоритет при конфликте между листами одного файла: чем "хуже" статус, тем
# он весомее — надёжнее ошибиться в сторону "сертификата нет", чем принять
# непроверенный/проблемный товар за подтверждённый.
_PRIORITY = {"white": 0, "green": 1, "red": 2}

STATUSES = ("green", "white", "red")

# Порог «это вообще цвет, а не оттенок серого»: ниже — серый/белый/чёрный.
_MIN_SATURATION = 0.12
_MIN_VALUE = 0.15
# Диапазоны оттенка (в градусах) для красного и зелёного.
_RED_HUE = ((0, 20), (330, 360))
_GREEN_HUE = ((75, 175),)


def classify_rgb(rgb: str) -> str:
    """'FFFF0000' / 'FF0000' -> 'red' | 'green' | 'white'.

    Классификация по оттенку (HSV), а не по точному совпадению строки:
    FFFF0000 и FFC00000 — оба 'red', FF00B050 и FFC6EFCE — оба 'green',
    FFCCCCCC (серый) и FFFFEB9C (жёлтый) — 'white'."""
    if not rgb:
        return "white"
    rgb = str(rgb)[-6:]
    try:
        r, g, b = (int(rgb[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return "white"
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if s < _MIN_SATURATION or v < _MIN_VALUE:
        return "white"  # серый/белый/почти чёрный — «нет отметки»
    hue = h * 360
    if any(lo <= hue <= hi for lo, hi in _RED_HUE):
        return "red"
    if any(lo <= hue <= hi for lo, hi in _GREEN_HUE):
        return "green"
    return "white"


def is_colored_fill(cell) -> bool:
    """Есть ли у ячейки вообще заливка (а не «пусто»)."""
    fill = getattr(cell, "fill", None)
    if fill is None:
        return False
    return getattr(fill, "patternType", None) not in (None, "none")


def fill_rgb(cell) -> str | None:
    """'FFRRGGBB' заливки ячейки, или None если заливки нет / цвет задан
    темой или индексом палитры (такой без самой темы не развернуть —
    вызывающий код считает его нераспознанным)."""
    if not is_colored_fill(cell):
        return None
    color = getattr(cell.fill, "fgColor", None)
    if color is None or getattr(color, "type", None) != "rgb":
        return None
    rgb = color.rgb
    if not isinstance(rgb, str) or len(rgb) not in (6, 8):
        return None
    return rgb


def classify_fill(cell, unclassified: list | None = None) -> str:
    """Статус по ЗАЛИВКЕ ячейки: 'red' | 'green' | 'white'.

    unclassified (если передан список) пополняется описанием каждой
    ячейки, у которой заливка ЕСТЬ, но отнести её к красной/зелёной не
    удалось — это ровно те случаи, которые молча уезжают в 'white'."""
    if not is_colored_fill(cell):
        return "white"
    rgb = fill_rgb(cell)
    if rgb is None:
        if unclassified is not None:
            unclassified.append("цвет темы/палитры (не RGB)")
        return "white"
    status = classify_rgb(rgb)
    if status == "white" and unclassified is not None:
        unclassified.append(rgb)
    return status


def merge_status(current: str | None, new: str, key=None, conflicts: list | None = None) -> str:
    """Слияние двух статусов одного и того же товара (разные листы отчёта
    или разные строки файла-ассортимента): побеждает red > green > white —
    исторический инвариант проекта (проблемный сертификат весомее
    подтверждённого, подтверждённый весомее «нет отметки»).

    Расхождение между строками — аномалия источника, а не норма: если
    передан список conflicts, туда попадает ключ, чтобы вызывающий stage
    мог о нём предупредить вместо тихого выбора."""
    if current is None:
        return new
    if current != new and conflicts is not None:
        conflicts.append(f"{key}: {current}/{new}")
    return new if _PRIORITY[new] > _PRIORITY[current] else current


def find_key_column(ws, key_prefix: str, header_row: int = HEADER_ROW):
    for c in range(1, ws.max_column + 1):
        val = ws.cell(row=header_row, column=c).value
        if val and str(val).startswith(f"{key_prefix}:"):
            return c
    return None


def scan_sheet_tricolor(ws, unclassified: list | None = None, conflicts: list | None = None) -> dict:
    """article (str) -> 'red' | 'green' | 'white' для одного листа."""
    cert_col = find_key_column(ws, CERT_COLUMN_KEY)
    art_col = find_key_column(ws, ARTICLE_COLUMN_KEY)
    if not cert_col or not art_col:
        return {}
    result = {}
    for r in range(DATA_START_ROW, ws.max_row + 1):
        article = ws.cell(row=r, column=art_col).value
        if article in (None, ""):
            continue
        article = normalize_article(article)
        # тот же приоритет, что и между листами — один article может
        # встретиться на листе дважды
        result[article] = merge_status(result.get(article),
                                       classify_fill(ws.cell(row=r, column=cert_col), unclassified),
                                       key=article, conflicts=conflicts)
    return result


def normalize_article(value) -> str:
    """Ключ сопоставления «строка отчёта <-> товар из источника».

    Excel часто отдаёт целое число как float (1140.0) — без этой нормализации
    ключ '1140.0' не совпадёт с id '1140' из JSON, и товар молча уедет в
    «нет в отчёте»."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def scan_workbook_tricolor(wb, unclassified: list | None = None, conflicts: list | None = None) -> dict:
    """Объединяет статусы по всем товарным листам файла. При конфликте
    (один и тот же article на нескольких листах с разным статусом)
    побеждает более 'плохой' — red > green > white.

    Лист, на котором не нашлись колонки S_CERT/N, пропускается — его имя
    попадает в skipped_sheets (см. scan_report), чтобы «молчаливая потеря
    целой категории» была видна в логе."""
    combined: dict[str, str] = {}
    for sheet_name in wb.sheetnames:
        if sheet_name in SKIP_SHEETS:
            continue
        for article, status in scan_sheet_tricolor(wb[sheet_name], unclassified, conflicts).items():
            combined[article] = merge_status(combined.get(article), status, key=article, conflicts=conflicts)
    return combined


def scan_report(xlsx_path) -> dict:
    """Полный разбор цветного отчёта Bazar: не только статусы, но и то, что
    при разборе НЕ получилось — чтобы вызывающий stage мог это напечатать.

    -> {"statuses": {article: status}, "skipped_sheets": [...],
        "unclassified_fills": [rgb, ...]}"""
    xlsx_path = Path(xlsx_path)
    if not xlsx_path.is_file():
        raise RuntimeError(f"Файл отчёта по сертификатам не найден: {xlsx_path}")
    wb = openpyxl.load_workbook(xlsx_path)
    unclassified: list[str] = []
    conflicts: list[str] = []
    skipped = [
        name for name in wb.sheetnames
        if name not in SKIP_SHEETS
        and not (find_key_column(wb[name], CERT_COLUMN_KEY) and find_key_column(wb[name], ARTICLE_COLUMN_KEY))
    ]
    statuses = scan_workbook_tricolor(wb, unclassified, conflicts)
    return {"statuses": statuses, "skipped_sheets": skipped,
            "unclassified_fills": unclassified, "conflicts": conflicts}


def load_cert_statuses(xlsx_path) -> dict:
    """Открывает файл отчёта и возвращает {article: 'red'|'green'|'white'}.
    Товар, которого в файле нет вовсе (ни на одном листе), в словаре просто
    отсутствует — вызывающий код сам решает, что с этим делать."""
    return scan_report(xlsx_path)["statuses"]


# --------------------------------------------------------------------------
# Фильтр по сертификату для генераторов шаблонов (--green/--white/--red)
# --------------------------------------------------------------------------

def apply_cert_filter(items, key_fn, statuses: dict, colors: set) -> tuple[list, dict]:
    """Оставляет из items только те, чей статус входит в colors.

    Возвращает (оставшиеся, статистика). Статистика РАЗДЕЛЯЕТ две причины
    отсева, которые раньше сливались в одно число:
      filtered_by_color    — статус есть, но он не выбран (например красный);
      missing_from_report  — товара в отчёте нет вообще (его id не нашёлся).
    Второе — это не «сертификат плохой», а «мы про товар ничего не знаем»:
    неполная выгрузка отчёта, переименованная колонка, пропущенный лист.
    Поведение при этом прежнее (такой товар в шаблон не попадает), меняется
    только видимость причины."""
    kept, by_status = [], {s: 0 for s in STATUSES}
    filtered_by_color = missing = 0
    for item in items:
        status = statuses.get(key_fn(item))
        if status is None:
            missing += 1
            continue
        by_status[status] += 1
        if status in colors:
            kept.append(item)
        else:
            filtered_by_color += 1
    return kept, {
        "kept": len(kept),
        "filtered_by_color": filtered_by_color,
        "missing_from_report": missing,
        "by_status": by_status,
    }


def resolve_cert_file(explicit, ctx, what: str = "xlsx со статусами сертификатов") -> Path:
    """Путь к файлу со статусами: явно заданный (CLI --file) или, если его
    нет, единственный xlsx входной папки склада. Одна точка на все три
    места, где такой файл нужен: 'certificates' Базара, 'certificates'
    Мвидео и фильтр --green/--white/--red у генераторов."""
    if explicit is None:
        return ctx.input_file(".xlsx", what)
    explicit = Path(explicit)
    if not explicit.is_file():
        raise RuntimeError(f"Файл не найден: {explicit}")
    return explicit


def cert_summary_fields(before: int, after: int, stats: dict) -> dict:
    """Поля фильтра для summary stage'а. Фильтр выключен — пустой словарь
    (в summary тогда нет ни одного cert_*-поля, как и до появления фильтра).

    cert_filtered_out оставлен как ОБЩЕЕ число отсеянных (на него ссылается
    README и старые логи), но рядом с ним теперь две раздельные причины —
    ровно то, чего не хватало, чтобы отличить «отсеяли красные» от «товара
    вообще не было в отчёте»."""
    if not stats:
        return {}
    return {
        "cert_source": stats.get("source"),
        "cert_filtered_out": before - after,
        "cert_filtered_by_color": stats["filtered_by_color"],
        "cert_missing_from_report": stats["missing_from_report"],
        "cert_by_status": stats["by_status"],
        "cert_skipped_sheets": stats.get("skipped_sheets") or [],
        "cert_unclassified_fills": stats.get("unclassified_fills", 0),
        "cert_status_conflicts": stats.get("status_conflicts", 0),
    }


class CertFilterMixin:
    """Общая часть фильтра --green/--white/--red для генераторов шаблонов.

    Подмешивается и к GenerateStage (Bazar: источник статусов — отдельный
    цветной xlsx-отчёт), и к PsbGenerateStage (Мвидео: источник — колонка
    "Номер сертификата" того же файла-ассортимента, который stage и так
    читает). Различие между складами — только в том, ОТКУДА берутся статусы
    (метод _load_cert_statuses) и обязателен ли для этого отдельный --file.

    CLI не пересоздаёт stage ради фильтра, а вызывает set_cert_filter() на
    том объекте, который собрал рецепт склада — иначе терялись бы любые
    параметры, заданные в рецепте (vat_default, price_markup_const и т.п.)."""

    #: как назвать источник статусов в сообщении об ошибке
    cert_source_name = "xlsx со статусами сертификатов"

    def _init_cert_filter(self, cert_file=None, include_green=False,
                          include_white=False, include_red=False):
        # cert_file не обязателен: не задан — источник берётся из входной
        # папки склада (см. _load_cert_statuses у наследников).
        self.cert_file = Path(cert_file) if cert_file else None
        self.cert_colors = {c for c, flag in (
            ("green", include_green), ("white", include_white), ("red", include_red)
        ) if flag}

    def set_cert_filter(self, file=None, green=False, white=False, red=False):
        """Включить фильтр на уже созданном stage'е (так его включает CLI)."""
        self._init_cert_filter(cert_file=file, include_green=green,
                               include_white=white, include_red=red)

    @property
    def cert_filter_enabled(self) -> bool:
        return bool(getattr(self, "cert_colors", None))

    def _load_cert_statuses(self, ctx) -> tuple[dict, str, dict]:
        """-> ({ключ: статус}, имя источника, {предупреждения}).
        Переопределяется складом, у которого источник другой."""
        path = resolve_cert_file(self.cert_file, ctx, self.cert_source_name)
        report = scan_report(path)
        return report["statuses"], path.name, {
            "skipped_sheets": report["skipped_sheets"],
            "unclassified_fills": report["unclassified_fills"],
            "conflicts": report["conflicts"],
        }

    def _filter_by_cert(self, items, key_fn, ctx) -> tuple[list, dict]:
        """Общий вход для stage'ей: отфильтровать и напечатать разбор."""
        if not self.cert_filter_enabled:
            return items, {}
        statuses, source_name, warnings = self._load_cert_statuses(ctx)
        before = len(items)
        kept, stats = apply_cert_filter(items, key_fn, statuses, self.cert_colors)
        by = stats["by_status"]
        print(f"  [i] Фильтр по сертификату {sorted(self.cert_colors)} (источник: {source_name}): "
              f"{stats['kept']} из {before} товаров прошли отбор")
        print(f"      по статусам в источнике: зелёных {by['green']}, белых {by['white']}, красных {by['red']}")
        print(f"      отсеяно по цвету: {stats['filtered_by_color']}; "
              f"нет в отчёте (статус неизвестен): {stats['missing_from_report']}")
        if stats["missing_from_report"]:
            print("      ⚠ товары, которых нет в отчёте, в шаблон НЕ попадают при любом наборе флагов — "
                  "проверьте, что отчёт покрывает весь ассортимент")
        for sheet in warnings.get("skipped_sheets") or []:
            print(f"      ⚠ лист '{sheet}' пропущен: не найдены колонки S_CERT:/N: во 2-й строке — "
                  f"все его товары уйдут в 'нет в отчёте'")
        conflicts = warnings.get("conflicts") or []
        if conflicts:
            print(f"      ⚠ один и тот же товар с РАЗНЫМИ статусами в источнике: {len(conflicts)} "
                  f"({', '.join(conflicts[:5])}{'...' if len(conflicts) > 5 else ''}) — "
                  f"взят приоритет red > green > white")
        unclassified = warnings.get("unclassified_fills") or []
        if unclassified:
            uniq = sorted(set(unclassified))
            print(f"      ⚠ заливок, не опознанных ни как красная, ни как зелёная: {len(unclassified)} "
                  f"({', '.join(uniq[:5])}{'...' if len(uniq) > 5 else ''}) — считаю их 'white'")
        stats["source"] = source_name
        stats["skipped_sheets"] = warnings.get("skipped_sheets") or []
        stats["unclassified_fills"] = len(unclassified)
        stats["status_conflicts"] = len(conflicts)
        return kept, stats
