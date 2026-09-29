#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 'generate' — JSON-экспорт Bazar -> заполненные xlsx-каталоги по
категориям, по одному файлу на console_category_id.

Список колонок для категории берётся ЖИВЬЁМ из API категорий админки
(?include=properties,hidden_properties) — что требуется, то и генерируется.
Соответствие "имя атрибута у Bazar" -> "имя атрибута у Белки" хранится в
файле маппинга ЭТОГО склада (attribute_mapping.json у Базара, см.
PipelineContext.resolved_attribute_mapping_file) и пополняется интерактивно
по ходу работы; сам порядок поиска значения общий для всех генераторов —
belka/core/attr_mapping.py.

Тройной маппинг категорий (bazar_id -> console_id -> admin_id):
  category_map.json (bazar_id -> console_id, вручную) +
  category_bridge_file (console_id -> admin_id, строит stage 'bridge').
"""
from __future__ import annotations

import re
import json
from pathlib import Path
from collections import defaultdict

from ..core.text import casefold_ru
from ..core.jsonio import load_json_file, save_json_file
from ..core.categories import (
    fetch_admin_category, build_template_columns, write_catalog_file, default_catalog_filename,
    clean_stale_catalog_files,
)
from ..core.cert_report import (
    CertFilterMixin, normalize_article, cert_summary_fields as _cert_summary_fields,
)
from ..core.columns import b2b_direct_fields, normalize_attr_key
# extract_images переехала в core/images.py — вся работа с картинками в одном месте
from ..core.images import extract_images
from ..core.fill_report import apply_placeholders
from ..pipeline import Stage, StageResult

# BARCODE_PREFIX_DEFAULT убран (09.2026) — префикс штрихкода теперь параметр
# склада: WarehouseConfig.barcode_prefix (см. belka/core/columns.py::build_barcode).
VAT_DEFAULT = "22%"
# Порог схожести имён для авто-подсказки переехал в
# belka/core/attr_mapping.py::SUGGEST_THRESHOLD_DEFAULT (общий для складов).
ATTR_SUGGEST_THRESHOLD_DEFAULT = 0.55
# Наценка за упаковку: к "Цена, руб *" прибавляется const * "Количество в
# упаковке" (переопределяется через .env BELKA_PRICE_MARKUP_CONST). "Цена до
# скидки, руб" эта наценка не трогает.
PRICE_MARKUP_CONST_DEFAULT = 0.0


def get_safe_value(value, default=1):
    if value is None or value == 0 or value == "":
        return default
    return value


def get_safe_number(value, default=0):
    """Как get_safe_value, но всегда возвращает число (для арифметики с ценой)."""
    value = get_safe_value(value, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_attribute_value_by_name(attributes: list, name: str):
    """Первое непустое значение Bazar-атрибута с данным attributeName (без учёта регистра)."""
    target = casefold_ru(name)
    for attr in attributes:
        if casefold_ru(attr.get("attributeName", "")) == target:
            options = attr.get("attributeOptions") or []
            if options:
                return options[0].get("attributeOptionName")
    return None


class GenerateStage(CertFilterMixin, Stage):
    name = "generate"

    # Bazar: статусы сертификатов живут в отдельном цветном xlsx-отчёте —
    # это единственный xlsx во входной папке склада (рядом с JSON-экспортом),
    # поэтому путь указывать не нужно; --file остаётся как переопределение.
    cert_source_name = "цветной xlsx-отчёт по сертификатам"

    def __init__(self, vat_default: str = VAT_DEFAULT,
                 attr_suggest_threshold: float = ATTR_SUGGEST_THRESHOLD_DEFAULT,
                 price_markup_const: float = PRICE_MARKUP_CONST_DEFAULT,
                 cert_file=None, include_green: bool = False, include_white: bool = False,
                 include_red: bool = False):
        self.vat_default = vat_default
        self.attr_suggest_threshold = attr_suggest_threshold
        self.price_markup_const = price_markup_const
        # Фильтр по сертификату (CLI: --green/--white/--red + --file, см.
        # cli.py) — не задан ни один флаг = фильтра нет, товары включаются
        # все, как и раньше. cert_file — тот же цветной xlsx-отчёт, что
        # использует stage 'certificates' (belka/stages/certificates.py);
        # статус читается тем же общим сканом (belka/core/cert_report.py).
        self._init_cert_filter(cert_file=cert_file, include_green=include_green,
                               include_white=include_white, include_red=include_red)

    # ------------------------------------------------------- прямые поля --
    # Фиксированный набор структурных колонок товара (не зависят от
    # категории; справочники типа Бренд/Материал/Цвет приходят динамически
    # из API категории).

    def _build_direct_fields(self, ctx, barcode_seen: set, price_markup_const: float = 0.0):
        """Фаза 1: значения КАК ЕСТЬ. Нет данных — возвращаем None, ячейка
        остаётся пустой; заглушки подставляются позже, одним общим шагом с
        отчётом и подтверждением (belka/core/fill_report.py). Константы
        формата (НДС, активность) читаются из общего реестра заглушек, чтобы
        их тоже можно было переопределить в рецепте склада."""

        def barcode_filler(p):
            # только СВОЙ штрихкод товара; генерация из id — заглушка,
            # применяется после подтверждения
            bc = p.get("barcode")
            if not bc:
                return None
            if bc in barcode_seen:
                print(f"  [!] Штрихкод {bc} уже встречался у другого товара в этом прогоне "
                      f"(id={p.get('id')}) — возможна коллизия, проверьте вручную")
            barcode_seen.add(bc)
            return bc

        def images_filler_main(p):
            main, _ = extract_images(p.get("images") or [])
            return main

        def images_filler_additional(p):
            _, additional = extract_images(p.get("images") or [])
            return additional

        def order_quantity_value(p):
            attributes = (p.get("productInfo") or {}).get("attributes", [])
            return get_attribute_value_by_name(attributes, "Количество для заказа")

        def order_quantity_filler(p):
            return order_quantity_value(p)

        def price_filler(p):
            base = p.get("discountPriceWithLogistics") or p.get("priceWithLogistics")
            if not base:
                # ни скидочной, ни обычной цены нет вовсе — не подставляем 0,
                # оставляем ячейку пустой (решение пользователя, 09.2026)
                return None
            base = get_safe_number(base, 0)
            if not price_markup_const:
                return base
            qty = get_safe_number(order_quantity_value(p), 1)
            return base + price_markup_const * qty

        def weight_filler(p):
            w = p.get("weight")
            return round(w / 1000, 3) if w else None   # граммы -> кг

        vat_value = ctx.placeholder("vat")
        activity_value = ctx.placeholder("activity")

        return [
            ("Название Товара *", True, lambda p: (p.get("name") or {}).get("ru") or None),
            ("Штрихкод *", True, barcode_filler),
            ("Id Товара *", True, lambda p: str(p.get("id", ""))),
            ("Вес, Кг *", True, weight_filler),
            ("Ширина, см *", True, lambda p: p.get("width")),
            ("Высота, см *", True, lambda p: p.get("height")),
            ("Длина, см *", True, lambda p: p.get("length")),
            # нет priceWithLogistics вовсе — ячейка остаётся пустой, не 0
            # (решение пользователя, 09.2026)
            ("Цена до скидки, руб", False, lambda p: p.get("priceWithLogistics")),
            ("Цена, руб *", True, price_filler),
            ("Количество Товара *", True, lambda p: p.get("stock", 0)),
            ("Основное изображение *", True, images_filler_main),
            ("НДС *", True, lambda p: vat_value),
            ("Дополнительное Изображения (до 10 штук)", False, images_filler_additional),
            ("Группировка товара по признаку", False, lambda p: p.get("groupKey", "")),
            ("Активность товара на маркете *", True, lambda p: activity_value),
            ("Описание *", True, lambda p: (p.get("description") or {}).get("ru") or None),
            # B2B-колонки — общие для всех складов, см. belka/core/columns.py
            *b2b_direct_fields(
                tnved=lambda p: p.get("tnvedCode") or None,
                order_multiplicity=lambda p: p.get("orderMultiplicity") or None,
                min_order_quantity=lambda p: p.get("minOrderQuantity") or None,
                quantity_in_package=order_quantity_filler,
            ),
        ]

    # _suggest_bazar_name/_resolve_required_attribute/_fill_attribute_value
    # переехали в belka/core/attr_mapping.py::AttributeMapper (09.2026) —
    # один и тот же порядок поиска значения и один формат файла теперь у всех
    # складов, а сам файл у каждого склада свой (см.
    # PipelineContext.resolved_attribute_mapping_file).

    @staticmethod
    def _lookup_factory(attributes):
        """lookup(name) для AttributeMapper: значение Bazar-атрибута по имени.
        normalized=True — сравнение по нормализованному имени (снимает
        '*', '(справочник)' и скобочную транслитерацию)."""
        def lookup(name, normalized: bool = False):
            if not normalized:
                return get_attribute_value_by_name(attributes, name)
            for attr in attributes:
                if normalize_attr_key(attr.get("attributeName", "")) == name:
                    options = attr.get("attributeOptions") or []
                    if options and options[0].get("attributeOptionName"):
                        return options[0]["attributeOptionName"]
            return None
        return lookup

    def _process_category(self, console_category_id, admin_category_id, products, session, admin_api_base,
                           category_cache, mapper, out_dir, direct_fields):
        if admin_category_id not in category_cache:
            category_cache[admin_category_id] = fetch_admin_category(session, admin_api_base, admin_category_id)
        category_data = category_cache[admin_category_id]

        columns = build_template_columns(category_data, direct_fields)

        bazar_names_seen = set()
        bazar_categories_seen = {}
        for p in products:
            for attr in (p.get("productInfo") or {}).get("attributes", []):
                name = attr.get("attributeName")
                if name:
                    bazar_names_seen.add(name)
            cat_id = p.get("categoryId")
            cat_name = p.get("categoryLevel3Name") or (p.get("categoryPath") or [{}])[-1].get("name")
            if cat_id is not None:
                bazar_categories_seen[cat_id] = cat_name
        bazar_categories = sorted(bazar_categories_seen.items())

        cats_str = ", ".join(f"{cid} ({cname})" if cname else str(cid) for cid, cname in bazar_categories)
        for col in columns:
            if col["kind"] == "attribute" and col["is_required"]:
                mapper.ensure_required(col["display_name"], bazar_names_seen, [
                    f"Категория ПСБ (LockedInfo): {console_category_id}",
                    f"Категория Белки (атрибуты): {admin_category_id}",
                    f"Категория(и) Bazar: {cats_str}",
                ])

        # Фаза 1: строки КАК ЕСТЬ, без заглушек — их подставит общий шаг
        # apply_placeholders после отчёта и подтверждения (см. run()).
        rows = []
        for p in products:
            attributes = (p.get("productInfo") or {}).get("attributes", [])
            row = []
            for col in columns:
                if col["kind"] == "direct":
                    value = col["filler"](p)
                else:
                    value = mapper.value_for(col["display_name"], self._lookup_factory(attributes))
                row.append(value)
            rows.append(row)

        # имя файла — через общую default_catalog_filename (belka/core/categories.py),
        # а не своей копией строки формата: так шаблон имени у Bazar и у ПСБ-складов
        # физически один и тот же, и clean_stale_catalog_files (glob 'catalog_*.xlsx')
        # гарантированно попадает по тем же файлам, что мы пишем.
        return {
            "products": len(products),
            "columns": columns,
            "rows": rows,
            "out_path": out_dir / default_catalog_filename(console_category_id),
            "category_id": console_category_id,
        }

    def run(self, ctx) -> StageResult:
        cfg = ctx.config
        session = ctx.admin_session()

        output_dir = ctx.resolved_output_dir()  # output_dir/bazar/ГГГГ-ММ-ДД, создаётся сама

        category_map_raw = load_json_file(Path(cfg.category_map_file), {})
        category_map = {int(k): v for k, v in category_map_raw.items()}

        bridge_path = Path(cfg.category_bridge_file)
        bridge_raw = load_json_file(bridge_path, None)
        if bridge_raw is None:
            raise RuntimeError(f"Не найден {bridge_path} — сначала прогоните stage 'bridge'.")
        category_bridge = {int(k): v for k, v in bridge_raw.items()}

        # Маппинг атрибутов — свой файл у каждого склада (09.2026), общий
        # порядок поиска значения для всех генераторов (belka/core/attr_mapping.py)
        mapper = ctx.attribute_mapper()

        json_path = ctx.input_file(".json", "JSON-экспорт Bazar")
        print(f"📄 Найден JSON файл: {json_path.name}")

        json_data = json.loads(json_path.read_text(encoding="utf-8"))
        products = json_data.get("products", [])
        print(f"📊 Загружено {len(products)} товаров")

        before_cert = len(products)
        products, cert_stats = self._filter_by_cert(
            products, lambda p: normalize_article(p.get("id", "")), ctx)
        cert_summary = _cert_summary_fields(before_cert, len(products), cert_stats)

        if not products:
            print("Нет товаров для обработки.")
            return StageResult(self.name, ok=True, summary={"products": 0, **cert_summary})

        products_by_console_category = defaultdict(list)
        console_to_admin_used = {}
        unmapped_bazar = defaultdict(int)
        unmapped_bridge = defaultdict(int)
        for p in products:
            bazar_cat_id = p.get("categoryId")
            console_cat_id = category_map.get(bazar_cat_id)
            if console_cat_id is None:
                unmapped_bazar[bazar_cat_id] += 1
                continue
            admin_cat_id = category_bridge.get(console_cat_id)
            if admin_cat_id is None:
                unmapped_bridge[console_cat_id] += 1
                continue
            products_by_console_category[console_cat_id].append(p)
            console_to_admin_used[console_cat_id] = admin_cat_id

        if unmapped_bazar:
            print(f"\n⚠️ Незамапленные категории Bazar (добавьте в {cfg.category_map_file}):")
            for cat_id, count in sorted(unmapped_bazar.items()):
                print(f"   {cat_id}: {count} товаров")
        if unmapped_bridge:
            print(f"\n⚠️ Категории консоли без пары в {cfg.category_bridge_file} "
                  f"(перезапустите stage 'bridge', или их пути разошлись):")
            for cat_id, count in sorted(unmapped_bridge.items()):
                print(f"   console_id={cat_id}: {count} товаров")

        price_markup_const = float(ctx.env.get("BELKA_PRICE_MARKUP_CONST") or self.price_markup_const)

        barcode_seen = set()
        direct_fields = self._build_direct_fields(ctx, barcode_seen, price_markup_const)
        category_cache = {}
        stats = {}

        # Чистим catalog_*.xlsx прошлых прогонов за СЕГОДНЯ — ровно так же и по
        # той же причине, что в psb_generate.py (09.2026; фикс был описан для
        # обоих генераторов, но в generate.py не доехал — поймано ревью):
        # папка вывода общая на день, а имя файла содержит только ЧАС, поэтому
        # второй прогон за день не заменял файлы первого, а ложился рядом — и
        # import_check/upload, сканирующие папку целиком, видели старые файлы
        # наравне с новыми.
        #
        # Место вызова важно: ПОСЛЕ всех проверок (JSON-экспорт найден и
        # прочитан, мост категорий на месте, товары разложены по категориям) и
        # ПЕРЕД первой записью — прогон, упавший раньше этой точки, не стирает
        # результат предыдущего успешного прогона за день.
        removed = clean_stale_catalog_files(output_dir)
        if removed:
            print(f"[generate] удалены старые catalog_*.xlsx прошлого прогона за сегодня ({removed} шт.) из "
                  f"{output_dir} — чтобы не путались с файлами этого прогона")

        for console_category_id, cat_products in products_by_console_category.items():
            admin_category_id = console_to_admin_used[console_category_id]
            print(f"\n📁 Категория ПСБ {console_category_id} (Белка {admin_category_id}): {len(cat_products)} товаров")
            result = self._process_category(
                console_category_id, admin_category_id, cat_products, session, cfg.admin_api_base,
                category_cache, mapper, output_dir, direct_fields,
            )
            stats[console_category_id] = result
            print(f"  собрано строк: {result['products']}, колонок: {len(result['columns'])}")

        # Фаза 2: общий отчёт по ВСЕМ категориям сразу и один вопрос на прогон
        # (а не по вопросу на каждый из 35 файлов) — см. belka/core/fill_report.py
        fill_summary = apply_placeholders(
            ctx, [(s["columns"], s["rows"]) for s in stats.values()],
            output_dir, f"missing_required_{cfg.key}.csv",
        )

        # Фаза 3: запись файлов — уже после решения по заглушкам
        print()
        for s in stats.values():
            write_catalog_file(s["columns"], s["rows"], s["category_id"], s["out_path"])
            print(f"  ✔ {s['out_path']} — товаров: {s['products']}, колонок: {len(s['columns'])}")

        mapper_report = mapper.report()
        if mapper_report["defaults_used"]:
            print(f"\n[i] Заполнено дефолтом из {mapper.path.name} (маппинг атрибутов, вне механизма заглушек): "
                  f"{mapper_report['defaults_used']} ячеек")
            for name, count in mapper_report["defaults_by_attribute"].items():
                print(f"    {name}: {count}")
        if mapper_report["unresolved_required"]:
            print(f"[!] Обязательные атрибуты без записи в {mapper.path.name}: "
                  f"{', '.join(mapper_report['unresolved_required'])} — останутся пустыми, см. отчёт выше")

        total_missing = fill_summary["missing_required_cells"]
        summary = {
            "categories": len(stats),
            "products_placed": sum(s["products"] for s in stats.values()),
            "unmapped_bazar_categories": len(unmapped_bazar),
            "unmapped_bridge_categories": len(unmapped_bridge),
            "missing_required_cells": total_missing,
            "rows_with_missing": fill_summary["rows_with_missing"],
            "placeholders_applied": fill_summary["placeholders_applied"],
            "placeholders_declined": fill_summary["placeholders_declined"],
            "missing_report": fill_summary["missing_report"],
            "attribute_mapping_file": mapper_report["file"],
            "mapping_defaults_used": mapper_report["defaults_used"],
            "unresolved_required_attributes": len(mapper_report["unresolved_required"]),
            "output_dir": str(output_dir),
            "price_markup_const": price_markup_const,
            **cert_summary,
        }
        print("\n=== ИТОГ generate ===")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        if total_missing and fill_summary["placeholders_declined"]:
            print(f"⚠ Пустых обязательных ячеек: {total_missing}, заглушки не подставлялись — "
                  f"проверьте на stage 'import'")
        print(f"\nФайлы в {output_dir}/ — это вход для stage 'import'")
        return StageResult(self.name, ok=True, summary=summary)
