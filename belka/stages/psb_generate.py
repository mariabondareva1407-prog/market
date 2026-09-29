#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 'psb_generate' — каталог продавца на apigw-seller (ПСБ Маркет) ->
заполненные xlsx-каталоги по категориям Белки, по одному файлу на
admin_category_id. Аналог 'generate' у Bazar, но источник товаров и
источник категорий — разные вещи:

  - Список offer_id к выгрузке — из файла-ассортимента (единственный xlsx
    во входной папке склада, input/<склад>/, см. ниже) — ЭТО и
    есть 'экспорт' для Мвидео, аналог JSON-файла Bazar: именно файл
    ассортимента решает, какие товары попадут в шаблоны, а не 'всё, что
    вообще есть у продавца в apigw'.
  - Карточка каждого товара — из apigw (belka/core/psb_api.py):
    find_product_by_offer_id (точечный поиск по offerId, по одному запросу
    на каждый offer_id из файла ассортимента) + get_product_details (полная
    карточка с атрибутами по внутреннему id, найденному первым запросом).
    09.2026, по решению пользователя: раньше вместо этого выгружался ВЕСЬ
    каталог продавца постранично (find_products_by_seller, статус
    'IMPORTED') и уже потом на него накладывались цена/остаток из файла
    ассортимента — так в шаблоны попадало и то, чего в файле ассортимента
    вообще не было. Теперь наоборот: список offer_id из файла ассортимента
    — это ЕДИНСТВЕННЫЙ источник того, что выгружается; то, чего там нет,
    в шаблоны не попадает, даже если оно есть в apigw.
  - Колонки шаблона строятся ЖИВЬЁМ из ТОЙ ЖЕ самой admin-категорийной API
    Белки, что и у Bazar (belka/core/categories.py) — шаблоны НЕ скачиваются
    с apigw (/excel/generate-product-template), по решению пользователя:
    у Белки одна категорийная схема на всех продавцов, зачем её дублировать.

Маппинг категорий: categoryId апигв -> admin_category_id Белки идёт через
ТОТ ЖЕ файл, что и у Bazar (console_to_admin_category_map.json), напрямую —
гипотеза (пока не проверена на живых данных): categoryId у ПСБ живёт в том
же пространстве id, что и console_id консоли Bazar (в psb_leaf_categories_*.xlsx
'psb_id' видны те же числа, что и известные console_id — например 26014).
Если гипотеза неверна, это сразу будет видно по большому числу
unmapped_categories в summary — тогда для этих складов понадобится свой
bridge-stage по образцу belka/stages/bridge.py.

Атрибуты (не входящие в фиксированный набор колонок ниже) сопоставляются
в первую очередь ПО ИМЕНИ — attributeName у apigw уже в терминах схемы
Белки (см. build_attributes_index в psb_api.py), так было устроено и в
присланном fill_templates.py. С 09.2026 за этим идут ещё два шага, общие
теперь для всех складов (belka/core/attr_mapping.py): имена из файла
маппинга ЭТОГО склада (attribute_mapping_<склад>.json) и дефолт оттуда же.
Вопросов про необязательные совпадения ПСБ-склады не задают
(WarehouseConfig.attribute_prompt=False) — незаполненное видно в общем
отчёте по обязательным полям.

Файл ассортимента ОБЯЗАТЕЛЕН для Мвидео — это одновременно и список
offer_id к выгрузке, и источник override'ов по цене/остатку поверх ответа
apigw для тех offer_id, где в файле заданы непустые "Розничная цена"/
"Остатки" (см. core/psb_api.py: read_assortment_overrides). 09.2026:
никакого шаблона имени больше нет — берётся единственный xlsx из
input/<склад>/ (PipelineContext.input_file), кладите свежий вместо
прежнего.

Компромисс от смены подхода: раньше список товаров получался ~N/50
запросами (постранично), теперь — 2 запроса на каждый offer_id
(find_product_by_offer_id + get_product_details), то есть при большом
файле ассортимента запросов к apigw станет заметно больше. Это плата за
точность (выгружается ровно то, что в файле, ни больше, ни меньше) — при
дневном троттлинге (WarehouseConfig.request_delay) это может ощутимо
увеличить время прогона на больших файлах ассортимента.

09.2026 (по просьбе пользователя): "Описание" и "Бренд" — тоже
необязательные колонки файла ассортимента (см. read_assortment_overrides
в core/psb_api.py), но в ОТЛИЧИЕ от price/quantity (которые ВСЕГДА
перекрывают apigw, если заданы) используются как ЗАПАСНОЙ ВАРИАНТ:
подставляются только когда у apigw соответствующее поле пустое —
"Описание" сразу в detail (прямое поле), "Бренд" — в attrs_index при
рендере строки (это атрибут категории, а не прямое поле, см.
BRAND_ATTRIBUTE_DISPLAY_NAME ниже).
Счётчики — summary.description_backfilled_from_assortment /
brand_backfilled_from_assortment.

09.2026 (правка после реального прогона): пользователь показал реальный
вывод import_check — "Бренд" оставался незаполненным почти во всех файлах,
несмотря на backfill. Причина — заголовок колонки в реальной схеме
категории оказался "Бренд(Brend) * (справочник)", то есть display_name
атрибута у категории Белки = "Бренд(Brend)" (кириллица + транслитерация в
скобках), а не голое "Бренд". Раньше backfill писал в
attrs_index["Бренд"], а _fill_attribute_value_generic искал ровно
attrs_index[col["display_name"]] == attrs_index["Бренд(Brend)"] — ключи не
совпадали, поэтому подставленное значение не находилось (и, по-видимому,
та же причина мешала попадать в ячейку значению, которое иногда всё же
приходило от apigw без суффикса). Исправлено добавлением нормализованного
сопоставления (см. _normalize_attr_key/_fill_attribute_value_generic) —
сравнение теперь идёт по "голому" русскому имени без скобочного суффикса
и с приведением регистра, а не по точной строке, так что различие в
скобочной транслитерации больше не мешает ни значению от apigw, ни
backfill'у из файла.

09.2026 (доп. правка, по просьбе пользователя): даже после backfill'а из
файла ассортимента у части товаров "Описание" оставалось пустым — ни у
apigw, ни в файле ассортимента для этих offer_id описания не было вообще
(подтверждено на реальных данных: 18 пустых ячеек "Описание" ровно
совпадали с missing_required_cells, при этом description_backfilled_from_
assortment был всего 1). Раз "Описание *" — обязательная колонка, пустая
ячейка мешает импорту. Добавлен третий, последний запасной вариант —
заглушка (DESCRIPTION_PLACEHOLDER_DEFAULT, текст задаётся ОДНОЙ константой
вверху файла, редактируется прямо в ней). Применяется, только если и
apigw, и файл ассортимента не дали значения. Счётчик —
summary.description_placeholder_used.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from collections import defaultdict

from ..core.categories import (
    fetch_admin_category, build_template_columns, write_catalog_file, default_catalog_filename,
    clean_stale_catalog_files,
)
from ..core.jsonio import load_json_file
from ..core.columns import IMAGES_SEPARATOR, b2b_direct_fields, normalize_attr_key
from ..core.fill_report import apply_placeholders
from ..core.images import (
    rotate_images, rotation_report_row, write_rotation_report,
    ROTATED, SINGLE_MAIN_UNCHANGED, SINGLE_ADDITIONAL_REMOVED, NO_MAIN_UNCHANGED, NO_IMAGES,
)
from ..core.text import strip_html
from ..core.psb_api import (
    VAT_MAP, STATUS_DEFAULT, CERT_COLUMN_DEFAULT, find_product_by_offer_id, get_product_details,
    build_attributes_index, read_assortment_overrides, load_cert_statuses_from_assortment, strip_vat,
)
from ..core.cert_report import (
    CertFilterMixin, normalize_article, resolve_cert_file,
    cert_summary_fields as _cert_summary_fields,
)
from ..pipeline import Stage, StageResult

# BARCODE_PREFIX_DEFAULT и DESCRIPTION_PLACEHOLDER_DEFAULT убраны (09.2026):
# префикс штрихкода теперь параметр склада (WarehouseConfig.barcode_prefix),
# а текст заглушки описания — запись общего реестра
# belka/core/defaults.py, переопределяемая в рецепте склада. Обе величины
# были локальными константами ОДНОГО генератора, из-за чего у складов
# расходились.

# 09.2026, по просьбе пользователя: атрибут категории Белки, в который
# подставляется "Бренд" из файла ассортимента, если у apigw этот атрибут
# пуст/отсутствует. Сравнение с реальным display_name категории идёт ПОСЛЕ
# нормализации (см. _normalize_attr_key) — скобочный суффикс вида
# "(Brend)" (транслитерация, которую добавляет схема категорий Белки, но
# не добавляет apigw) не мешает совпадению, поэтому здесь достаточно
# голого русского имени.
BRAND_ATTRIBUTE_DISPLAY_NAME = "Бренд"

# _normalize_attr_key переехал в belka/core/columns.py::normalize_attr_key
# (09.2026) — теперь это ОДНА функция на весь проект: её же использует
# generate.py Базара для поиска атрибута по имени, и она дополнительно
# снимает "*"/"(справочник)", так что работает и от полного заголовка
# колонки, и от голого display_name, и от attributeName источника.


def _lookup_factory(attributes_index: dict, normalized_index: dict):
    """lookup(name) для AttributeMapper: значение атрибута apigw по имени.

    Шаги 1-2 (точное и нормализованное совпадение) закрывают у ПСБ-складов
    почти всё — attributeName апигв уже в терминах схемы Белки. Шаги 3-4
    (имена и дефолт из файла маппинга склада) добавлены 09.2026: механизм
    стал общим для всех складов, файл — свой у каждого
    (attribute_mapping_<склад>.json)."""
    def lookup(name, normalized: bool = False):
        if normalized:
            return normalized_index.get(name)
        value = attributes_index.get(name)
        if value in (None, ""):
            value = normalized_index.get(normalize_attr_key(name))
        return value
    return lookup


def _build_direct_fields(ctx, barcode_seen: set):
    """Фиксированные структурные колонки — те же по СМЫСЛУ, что и FIXED_COLUMNS
    /SPECIAL_COLUMNS в присланном fill_templates.py, только тут это (header,
    is_required, filler), как у Bazar, чтобы делить build_template_columns.
    Остальные обязательные поля категории Белки (ТН ВЭД, Кратность заказа,
    Минимальное количество, Количество в упаковке и т.п.), которых нет в
    ответе apigw напрямую, сюда НЕ входят — они попадают в columns как
    kind='attribute' и заполняются через attributes_index (см. run())."""

    def barcode_filler(p):
        # только СВОЙ штрихкод от apigw. Раньше здесь же клеился
        # префикс + offerId без ограничения длины — так у Мвидео выходили
        # 16-символьные штрихкоды. Теперь генерация — заглушка (префикс
        # склада + Id, ровно 13, см. belka/core/columns.py::build_barcode),
        # применяется после отчёта и подтверждения.
        bc = p.get("barcode")
        if not bc:
            return None
        if bc in barcode_seen:
            print(f"  [!] Штрихкод {bc} уже встречался у другого товара в этом прогоне "
                  f"(offerId={p.get('offerId')}) — возможна коллизия, проверьте вручную")
        barcode_seen.add(bc)
        return bc

    def price_filler(p):
        return strip_vat(p.get("price"), p.get("vat"))

    def old_price_filler(p):
        return strip_vat(p.get("oldPrice"), p.get("vat"))

    def vat_filler(p):
        vat_code = p.get("vat")
        vat_value = VAT_MAP.get(vat_code)
        if vat_value is None:
            print(f"  [!] offerId={p.get('offerId')}: неизвестный код НДС={vat_code!r} — "
                  f"ячейка пустая, попадёт в отчёт по обязательным полям")
        return vat_value

    def images_filler_additional(p):
        images = p.get("images") or []
        # разделитель общий для всех складов; был ";", у Базара ", " — приведено
        return IMAGES_SEPARATOR.join(images) if images else None

    activity_value = ctx.placeholder("activity")

    return [
        ("Название Товара *", True, lambda p: p.get("name")),
        ("Штрихкод *", True, barcode_filler),
        # без префикса склада — offer_id пишется как есть, решение пользователя
        # (09.2026), несмотря на риск коллизии с id других складов под общим
        # seller_id=6 Белки
        ("Id Товара *", True, lambda p: str(p.get("offerId", ""))),
        ("Вес, Кг *", True, lambda p: p.get("weight")),
        ("Ширина, см *", True, lambda p: p.get("width")),
        ("Высота, см *", True, lambda p: p.get("height")),
        ("Длина, см *", True, lambda p: p.get("length")),
        ("Цена до скидки, руб", False, old_price_filler),
        ("Цена, руб *", True, price_filler),
        ("Количество Товара *", True, lambda p: p.get("quantity", 0)),
        ("Основное изображение *", True, lambda p: p.get("primaryImage")),
        ("НДС *", True, vat_filler),
        ("Дополнительное Изображения (до 10 штук)", False, images_filler_additional),
        ("Группировка товара по признаку", False, lambda p: p.get("groupName")),
        ("Активность товара на маркете *", True, lambda p: activity_value),
        ("Описание *", True, lambda p: p.get("description")),
        # B2B-колонки — те же самые, что у Базара (belka/core/columns.py).
        # У apigw источника для них нет, поэтому значения придут из общего
        # реестра заглушек, но СНАЧАЛА будут показаны в отчёте и подтверждены.
        *b2b_direct_fields(
            tnved=lambda p: p.get("tnvedCode") or None,
            order_multiplicity=lambda p: p.get("orderMultiplicity") or None,
            min_order_quantity=lambda p: p.get("minOrderQuantity") or None,
            quantity_in_package=lambda p: p.get("quantityInPackage") or None,
        ),
    ]


class PsbGenerateStage(CertFilterMixin, Stage):
    name = "psb_generate"

    # У ПСБ-складов отдельного цветного отчёта по сертификатам НЕТ: статус
    # лежит в колонке "Номер сертификата" ТОГО ЖЕ файла-ассортимента, что
    # stage и так читает ради цены/остатка — то есть в том же единственном
    # xlsx входной папки склада.
    cert_source_name = "файл-ассортимент с сертификатами"

    def __init__(self, cert_file=None, include_green: bool = False,
                 include_white: bool = False, include_red: bool = False):
        self._init_cert_filter(cert_file=cert_file, include_green=include_green,
                               include_white=include_white, include_red=include_red)

    def _load_cert_statuses(self, ctx):
        # Тот же файл, что stage читает ради цены/остатка — единственный
        # xlsx входной папки склада (или --file, если задан явно).
        path = resolve_cert_file(self.cert_file, ctx, self.cert_source_name)
        conflicts: list[str] = []
        try:
            statuses = load_cert_statuses_from_assortment(path, conflicts=conflicts)
        except ValueError as e:
            raise RuntimeError(
                f"{e}. Фильтр --green/--white/--red у склада '{ctx.config.key}' читает статус из колонки "
                f"'{CERT_COLUMN_DEFAULT}' файла-ассортимента — без неё фильтровать нечем "
                f"(либо укажите другой файл через --file)."
            ) from e
        return statuses, Path(path).name, {"conflicts": conflicts}

    def run(self, ctx) -> StageResult:
        cfg = ctx.config
        if not cfg.psb_api_base:
            raise RuntimeError(f"У склада '{cfg.key}' не задан psb_api_base.")
        psb = ctx.psb_session()
        admin = ctx.admin_session()

        seller_id_psb = int(ctx.override("PSB_SELLER_ID") or cfg.psb_seller_id or 0)
        if not seller_id_psb:
            raise RuntimeError(f"У склада '{cfg.key}' не задан psb_seller_id (id продавца на стороне ПСБ).")

        output_dir = ctx.resolved_output_dir()  # output_dir/<склад>/ГГГГ-ММ-ДД, создаётся сама

        bridge_path = Path(cfg.category_bridge_file)
        bridge_raw = load_json_file(bridge_path, None)
        if bridge_raw is None:
            raise RuntimeError(
                f"Не найден {bridge_path}. Для '{cfg.key}' переиспользуется тот же мост категорий, что и у "
                f"Bazar (console_id -> admin_id, собран stage'ом 'bridge' Базара) — гипотеза, что categoryId "
                f"у ПСБ живёт в том же пространстве id, что и console_id (см. psb_leaf_categories_*.xlsx). "
                f"Скопируйте файл в рабочую папку '{cfg.key}' (или запускайте отсюда же, где он лежит у Bazar)."
            )
        category_bridge = {int(k): v for k, v in bridge_raw.items()}

        # Файл ассортимента — просто единственный xlsx во входной папке
        # склада (input/<склад>/). Имя не важно: оно меняется от выгрузки к
        # выгрузке, и раньше из-за этого приходилось держать в рецепте
        # склада glob-шаблон имени (assortment_file) и следить, чтобы файл
        # под него попадал. 09.2026, по решению пользователя: в папке склада
        # лежит ровно один свежий xlsx, его и берём.
        assortment_path = ctx.input_file(".xlsx", "файл ассортимента")
        overrides = read_assortment_overrides(assortment_path)
        offer_ids = list(overrides)
        print(f"[psb_generate] файл ассортимента: {assortment_path.name}, offer_id к выгрузке: {len(offer_ids)}")

        # Фильтр по сертификату — ДО похода в apigw: незачем тянуть карточки
        # товаров, которые в шаблон всё равно не попадут (у ПСБ-складов это
        # 2 запроса на каждый offer_id, см. docstring файла).
        offer_ids_before_cert = len(offer_ids)
        offer_ids, cert_stats = self._filter_by_cert(offer_ids, normalize_article, ctx)
        cert_summary = _cert_summary_fields(offer_ids_before_cert, len(offer_ids), cert_stats)

        if not offer_ids:
            return StageResult(self.name, ok=True,
                               summary={"offer_ids_in_assortment": offer_ids_before_cert, **cert_summary})

        print(f"[psb_generate] ищу карточки в apigw по offer_id из файла ассортимента "
              f"(find_product_by_offer_id, sellerId={seller_id_psb}, статус '{STATUS_DEFAULT}')...")
        t0 = time.monotonic()

        products_by_category = defaultdict(list)
        psb_to_admin_used = {}
        unmapped = defaultdict(int)
        overridden = 0
        description_backfilled = 0
        not_found = 0
        html_cleaned = 0
        image_rows = []                       # строки images_report_<склад>.xlsx
        image_actions = defaultdict(int)
        images_removed_total = 0
        image_duplicates_total = 0
        for i, offer_id in enumerate(offer_ids, 1):
            item = find_product_by_offer_id(psb, cfg.psb_api_base, offer_id, seller_id_psb)
            if not item:
                print(f"  [!] offer_id={offer_id} не найден в apigw (sellerId={seller_id_psb}, "
                      f"статус '{STATUS_DEFAULT}') — пропущен, в шаблоны не попадёт")
                not_found += 1
                continue
            detail = get_product_details(psb, cfg.psb_api_base, item["id"], seller_id_psb)
            # price/quantity — ВСЕГДА перекрывают apigw, если заданы в файле
            # ассортимента (как и раньше).
            override_values = {k: v for k, v in overrides[offer_id].items()
                                if k in ("price", "quantity") and v not in (None, "")}
            if override_values:
                detail = {**detail, **override_values}
                overridden += 1
            # description — НАОБОРОТ, запасной вариант: подставляем из файла
            # только если у apigw своего описания нет (09.2026, по просьбе
            # пользователя — см. BRAND_ATTRIBUTE_DISPLAY_NAME выше про бренд,
            # который заполняется так же, но чуть ниже, т.к. это атрибут, а
            # не прямое поле detail).
            if not detail.get("description") and overrides[offer_id].get("description"):
                detail["description"] = overrides[offer_id]["description"]
                description_backfilled += 1
            # Заглушки описания здесь БОЛЬШЕ НЕТ (09.2026): она переехала в
            # общий реестр belka/core/defaults.py и подставляется одним шагом
            # после отчёта и подтверждения — вместе с остальными.

            # Группировка — по categoryId ПСБ, как у Базара по console_id
            # (09.2026, по итогам ревью). Раньше группировали по
            # admin_category_id, и он же уходил в LockedInfo!A1 и в имя файла —
            # а у Базара там id ПСБ/консоли. Из-за этого шаблоны двух складов
            # несли в одной и той же ячейке id из РАЗНЫХ систем. Теперь
            # одинаково: группа и LockedInfo — id ПСБ, колонки — по admin_id
            # из моста.
            category_id = detail.get("categoryId")
            admin_category_id = category_bridge.get(category_id)
            if admin_category_id is None:
                unmapped[category_id] += 1
                continue

            # --- обработка по флагам склада (09.2026, пока только Мвидео) ---
            # Делается здесь, ПОСЛЕ маппинга категории: в лог и отчёт попадают
            # ровно те товары, что попадут в каталоги. Filler'ы ниже читают уже
            # исправленные primaryImage/images/description.
            if cfg.strip_html_description:
                cleaned = strip_html(detail.get("description"))
                if cleaned != detail.get("description"):
                    detail = {**detail, "description": cleaned}
                    html_cleaned += 1
                    if cleaned is None:
                        print(f"  [html] offer_id={offer_id}: после снятия HTML описание пустое — "
                              f"будет заглушка")
            if cfg.rotate_images:
                rot = rotate_images(detail.get("primaryImage"), detail.get("images"))
                detail = {**detail, "primaryImage": rot.main_after, "images": rot.additional_after}
                print(f"  [img] offer_id={offer_id}: {rot.describe()}")
                image_rows.append(rotation_report_row(offer_id, rot))
                image_actions[rot.action] += 1
                images_removed_total += len(rot.removed)
                image_duplicates_total += len(rot.duplicates)

            products_by_category[category_id].append(detail)
            psb_to_admin_used[category_id] = admin_category_id

            if i % 25 == 0 or i == len(offer_ids):
                print(f"  [{i}/{len(offer_ids)}] карточек получено...")
        print(f"[psb_generate] карточки товаров получены за {time.monotonic() - t0:.1f}с "
              f"(найдено в apigw: {len(offer_ids) - not_found} из {len(offer_ids)}, не найдено: {not_found})")

        if unmapped:
            print(f"\n⚠️ Категории ПСБ (categoryId) без пары в {cfg.category_bridge_file}:")
            for cat_id, count in sorted(unmapped.items(), key=lambda kv: -kv[1]):
                print(f"   {cat_id}: {count} товаров")
            print("   (если таких много — проверьте гипотезу совпадения id-пространств categoryId/console_id, "
                  "см. docstring этого stage'а)")

        barcode_seen = set()
        direct_fields = _build_direct_fields(ctx, barcode_seen)
        # Маппинг атрибутов теперь есть и у ПСБ-складов — как ЗАПАСНОЙ путь
        # после прямого и нормализованного совпадения имени. Файл свой
        # (attribute_mapping_<склад>.json); вопросов про необязательные
        # совпадения не задаёт (WarehouseConfig.attribute_prompt=False) —
        # незаполненное видно в общем отчёте по обязательным полям.
        mapper = ctx.attribute_mapper()
        category_cache = {}
        stats = {}
        brand_backfilled = 0

        # Чистим старые catalog_*.xlsx этого склада за СЕГОДНЯ только теперь,
        # когда все проверки выше уже пройдены (мост категорий и файл
        # ассортимента найдены, offer_id получены) — то есть мы точно
        # собираемся записать новые файлы взамен. Если чистить раньше (сразу
        # после resolved_output_dir()) и один из шагов до этого места упадёт
        # с ошибкой, папка останется пустой без единого нового файла вместо
        # старых — то есть прогон, упавший ДО генерации, стёр бы уже готовые
        # файлы предыдущего успешного прогона за день. См. docstring
        # clean_stale_catalog_files и историю (09.2026) про путаницу
        # старых/новых catalog_*.xlsx в одной дневной папке.
        removed = clean_stale_catalog_files(output_dir)
        if removed:
            print(f"[psb_generate] удалены старые catalog_*.xlsx прошлого прогона за сегодня ({removed} шт.) из "
                  f"{output_dir} — чтобы не путались с файлами этого прогона")

        for psb_category_id, products in products_by_category.items():
            admin_category_id = psb_to_admin_used[psb_category_id]
            if admin_category_id not in category_cache:
                category_cache[admin_category_id] = fetch_admin_category(admin, cfg.admin_api_base, admin_category_id)
            category_data = category_cache[admin_category_id]
            columns = build_template_columns(category_data, direct_fields)

            attr_names_seen = set()
            for p in products:
                for a in (p.get("attributes") or []):
                    if a.get("attributeName"):
                        attr_names_seen.add(a["attributeName"])
            for col in columns:
                if col["kind"] == "attribute" and col["is_required"]:
                    mapper.ensure_required(col["display_name"], attr_names_seen, [
                        f"Категория ПСБ (LockedInfo): {psb_category_id}",
                        f"Категория Белки (атрибуты): {admin_category_id}",
                    ])

            # Фаза 1: строки КАК ЕСТЬ, заглушки подставит общий шаг ниже
            rows = []
            for p in products:
                attrs_index = build_attributes_index(p.get("attributes"))
                # normalized_index — тот же attrs_index, но ключи без
                # скобочного суффикса транслитерации и без учёта регистра
                # (см. _normalize_attr_key) — нужен и для чтения (когда
                # display_name категории отличается от attributeName apigw
                # именно суффиксом), и как цель backfill'а ниже.
                normalized_index = {normalize_attr_key(k): v for k, v in attrs_index.items() if v not in (None, "")}
                # "Бренд" — это атрибут категории (как цвет/материал), а не
                # прямое поле detail, поэтому запасной вариант из файла
                # ассортимента применяется здесь, а не в первом цикле (где
                # это сделано для "Описание") — тем же принципом: только
                # если у apigw атрибут пуст/отсутствует (проверяем через
                # normalized_index, а не точный ключ — см. докстринг файла).
                assortment_entry = overrides.get(str(p.get("offerId")), {})
                brand_key_norm = normalize_attr_key(BRAND_ATTRIBUTE_DISPLAY_NAME)
                if not normalized_index.get(brand_key_norm) and assortment_entry.get("brand"):
                    attrs_index[BRAND_ATTRIBUTE_DISPLAY_NAME] = assortment_entry["brand"]
                    normalized_index[brand_key_norm] = assortment_entry["brand"]
                    brand_backfilled += 1
                row = []
                for col in columns:
                    if col["kind"] == "direct":
                        value = col["filler"](p)
                    else:
                        value = mapper.value_for(col["display_name"],
                                                  _lookup_factory(attrs_index, normalized_index))
                    row.append(value)
                rows.append(row)

            stats[psb_category_id] = {
                "products": len(products), "columns": columns, "rows": rows,
                "out_path": output_dir / default_catalog_filename(psb_category_id),
                "category_id": psb_category_id, "admin_category_id": admin_category_id,
            }
            print(f"  собрано: категория ПСБ {psb_category_id} (Белка {admin_category_id}) — "
                  f"товаров {len(products)}, колонок {len(columns)}")

        # Фаза 2: общий отчёт по всем категориям и один вопрос на прогон
        fill_summary = apply_placeholders(
            ctx, [(s["columns"], s["rows"]) for s in stats.values()],
            output_dir, f"missing_required_{cfg.key}.csv",
        )

        # Фаза 3: запись. LockedInfo!A1 = id категории ПСБ — как у Базара
        print()
        for s in stats.values():
            write_catalog_file(s["columns"], s["rows"], s["category_id"], s["out_path"])
            print(f"  ✔ {s['out_path']} — товаров: {s['products']}, колонок: {len(s['columns'])}")

        processing_summary = {}
        if cfg.rotate_images:
            # В ПОДПАПКЕ reports/, а не рядом с catalog_*.xlsx: import_check,
            # import_attributes и upload берут из дневной папки ВСЕ *.xlsx
            # (glob без рекурсии) — отчёт рядом с каталогами ушёл бы на импорт.
            report_dir = output_dir / "reports"
            report_dir.mkdir(parents=True, exist_ok=True)
            report_path = report_dir / f"images_report_{cfg.key}.xlsx"
            write_rotation_report(image_rows, report_path)
            print(f"  ✔ {report_path} — отчёт по изображениям, товаров: {len(image_rows)}")
            processing_summary = {
                "images_rotated": image_actions[ROTATED],
                "images_single_main_unchanged": image_actions[SINGLE_MAIN_UNCHANGED],
                "images_single_additional_removed": image_actions[SINGLE_ADDITIONAL_REMOVED],
                "images_no_main_unchanged": image_actions[NO_MAIN_UNCHANGED],
                "images_none": image_actions[NO_IMAGES],
                "images_removed_total": images_removed_total,
                "images_duplicates_removed": image_duplicates_total,
                "images_report": str(report_path),
            }
        if cfg.strip_html_description:
            processing_summary["html_descriptions_cleaned"] = html_cleaned

        total_missing = fill_summary["missing_required_cells"]
        summary = {
            "seller_id_psb": seller_id_psb,
            "offer_ids_in_assortment": offer_ids_before_cert,
            "offer_ids_after_cert_filter": len(offer_ids),
            **cert_summary,
            "not_found_in_api": not_found,
            "products_placed": sum(s["products"] for s in stats.values()),
            "categories": len(stats),
            "unmapped_categories": len(unmapped),
            "assortment_overrides_applied": overridden,
            "description_backfilled_from_assortment": description_backfilled,
            "brand_backfilled_from_assortment": brand_backfilled,
            "attribute_mapping_file": mapper.report()["file"],
            "mapping_defaults_used": mapper.report()["defaults_used"],
            "unresolved_required_attributes": len(mapper.report()["unresolved_required"]),
            "missing_required_cells": total_missing,
            "rows_with_missing": fill_summary["rows_with_missing"],
            "placeholders_applied": fill_summary["placeholders_applied"],
            "placeholders_declined": fill_summary["placeholders_declined"],
            "missing_report": fill_summary["missing_report"],
            **processing_summary,
            "output_dir": str(output_dir),
        }
        print("\n=== ИТОГ psb_generate ===")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        if unmapped:
            print(f"⚠ Незамапленных категорий: {len(unmapped)} — их товары НЕ попали ни в один файл, проверьте на stage 'import'")
        print(f"\nФайлы в {output_dir}/ — это вход для import_check/import_attributes/upload (те же, что у Bazar)")
        return StageResult(self.name, ok=True, summary=summary)