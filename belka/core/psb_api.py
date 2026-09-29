#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
belka.core.psb_api — доступ к API «ПСБ Маркет» (apigw-seller.xn--80abntiqkep.xn--p1ai),
общий источник товаров для складов №6 (Мвидео) и №7 (b2c). Оба тянут каталог
с ОДНОГО и того же apigw, различаются только seller_id НА СТОРОНЕ ПСБ (не
путать с seller_id=6 самой Белки — см. WarehouseConfig.psb_seller_id) и, для
Мвидео, отдельным регулярным файлом-ассортиментом с ценой/остатком (см.
read_assortment_overrides ниже).

Основано на двух присланных пользователем скриптах:
  - fill_templates.py  — первичная выгрузка каталога в шаблоны Белки;
  - update_prices.py   — точечное обновление цены/остатка в уже готовых
                          шаблонах.
С одной сознательной поправкой к поиску (взята из update_prices.py, т.к.
там она уже была сделана правильно): везде указываем sellerId и ходим через
/admin/product/all, а не через /product/all без sellerId, как было в
исходном fill_templates.py — без sellerId между продавцами на одном ПСБ
возможна путаница по offerId.

09.2026: та же поправка распространена и на карточку товара —
get_product_details теперь тоже ходит через /admin/ и требует seller_id
в пути (/admin/product/{seller_id}/{id}), а не голый /product/{id}, как
раньше.
"""
from __future__ import annotations

import re
from pathlib import Path

from openpyxl import load_workbook

from .xlsx import clean_header
from .cert_report import classify_fill, normalize_article, merge_status

# Колонка со статусом сертификата в файле-ассортименте (Мвидео). Значение
# ячейки — текст номера/"#N/A"/пусто, но СТАТУС определяется заливкой.
CERT_COLUMN_DEFAULT = "Номер сертификата"

PRODUCT_LIST_URL_PART = "/admin/product/all"
PRODUCT_DETAIL_URL_PART = "/admin/product/{seller_id}/{id}"
PAGE_SIZE_DEFAULT = 50
STATUS_DEFAULT = "IMPORTED"

# Справочник НДС: числовой код "vat" из ответа API -> подпись для столбца
# "НДС *" шаблона Белки (см. fill_templates.py). Код 4 (20%) в API
# неактивен, оставлен для полноты, как и в исходном скрипте.
VAT_MAP = {
    1: "Не облагается",
    2: "0%",
    3: "10%",
    4: "20%",
    5: "5%",
    6: "7%",
    7: "22%",
}

_VAT_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def vat_percent(vat_code) -> float | None:
    """Код НДС из API -> число (для арифметики с ценой). 'Не облагается' -> 0.0.
    Неизвестный код -> None (ни один из присланных скриптов не занимался этой
    арифметикой — этого справочника раньше не было)."""
    label = VAT_MAP.get(vat_code)
    if label is None:
        return None
    if label == "Не облагается":
        return 0.0
    m = _VAT_PERCENT_RE.match(label)
    return float(m.group(1)) if m else None


def strip_vat(amount, vat_code, default=None):
    """Цена, отданная apigw, -> цена БЕЗ ндс, которую пишем в шаблон Белки.
    Сайт при импорте сам умножает итоговую цену на (1 + ндс%/100) * 1.01
    (наша комиссия, см. обсуждение аудита цены) — если положить в шаблон
    цену apigw как есть, НДС применится дважды. amount*100/(100+vat%) —
    обратная операция. Код НДС неизвестен/не найден -> цена не трогается
    (лучше сохранить исходное значение, чем тихо занулить)."""
    if amount in (None, ""):
        return default
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return default
    pct = vat_percent(vat_code)
    if not pct:
        return amount
    return round(amount * 100 / (100 + pct), 2)


def find_products_by_seller(session, base_url, seller_id, page_size=PAGE_SIZE_DEFAULT,
                             status=STATUS_DEFAULT, on_page=None):
    """Постранично отдаёт товары продавца СО СТАТУСОМ `status` (по умолчанию
    только "IMPORTED" — см. STATUS_DEFAULT; товары в других статусах на
    стороне ПСБ, например черновики/на модерации/отклонённые, этим вызовом
    НЕ возвращаются, это не буквально 'весь каталог продавца'). Генератор,
    не список — вызывающий код сам решает, что делать с каждым товаром по
    мере получения.

    09.2026: `psb_generate.py` (Мвидео) раньше строил выгрузку через эту
    функцию (весь каталог продавца), теперь — через `find_product_by_offer_id`
    по каждому offer_id из файла ассортимента (по решению пользователя:
    список offer_id из файла ассортимента должен быть единственным
    источником того, что попадает в шаблон). Эта функция сейчас никем не
    вызывается, но оставлена как общий, протестированный строительный блок
    — например для будущего склада, которому нужен именно полный каталог, а
    не список по конкретным offer_id.

    `on_page(page, total_so_far)`, если передан, вызывается после каждой
    полученной страницы — без этого сама выгрузка списка не печатает вообще
    ничего, пока не заберёт все страницы, и на большом каталоге это
    выглядит как зависший процесс."""
    url = f"{base_url.rstrip('/')}{PRODUCT_LIST_URL_PART}"
    page = 1
    total = 0
    while True:
        resp = session.get(url, params={
            "page": page, "size": page_size, "status": status, "sellerId": seller_id,
        }, timeout=30)
        data = resp.json()
        content = data.get("content", []) or []
        if not content:
            break
        for item in content:
            yield item
        total += len(content)
        if on_page:
            on_page(page, total)
        if len(content) < page_size:
            break
        page += 1


def find_product_by_offer_id(session, base_url, offer_id, seller_id, page_size=PAGE_SIZE_DEFAULT, status=STATUS_DEFAULT):
    """offerIdLike — подстрока, поэтому среди content[] ищем ТОЧНОЕ совпадение
    offerId == offer_id. Не используется в psb_generate (там перебор идёт по
    ВСЕМУ каталогу продавца через find_products_by_seller) — оставлена как
    общий строительный блок на случай точечного поиска одного товара
    (по образцу update_prices.py)."""
    url = f"{base_url.rstrip('/')}{PRODUCT_LIST_URL_PART}"
    resp = session.get(url, params={
        "page": 1, "size": page_size, "status": status,
        "offerIdLike": offer_id, "sellerId": seller_id,
    }, timeout=30)
    data = resp.json()
    for item in data.get("content", []) or []:
        if str(item.get("offerId")) == str(offer_id):
            return item
    return None


def get_product_details(session, base_url, product_id, seller_id):
    """Полная карточка товара с атрибутами (attributes[]) по внутреннему id
    (НЕ offerId) — вернувшийся из find_products_by_seller/find_product_by_offer_id
    item['id']. 09.2026: раньше ходили по голому `/product/{id}` без seller_id
    (как и с product/all до правки на /admin/product/all — см. докстринг
    модуля) — тот же риск путаницы между продавцами на одном ПСБ, только для
    детальной карточки, а не списка. Теперь, как и list-ручка, идёт через
    `/admin/` и требует seller_id в самом пути."""
    url = f"{base_url.rstrip('/')}{PRODUCT_DETAIL_URL_PART.format(seller_id=seller_id, id=product_id)}"
    resp = session.get(url, timeout=30)
    return resp.json()


def build_attributes_index(attributes: list) -> dict:
    """attributeName -> attributeValue. В отличие от Bazar (где имена атрибутов
    у продавца и у Белки расходятся и нужен attribute_mapping.json),
    attributeName у ПСБ уже приходит в терминах схемы Белки — поэтому
    сопоставление с display_name колонки category API прямое, 1:1 (как в
    fill_templates.py), без файла-маппинга."""
    index = {}
    for attr in attributes or []:
        name = (attr.get("attributeName") or "").strip()
        if name:
            index[name] = attr.get("attributeValue")
    return index


def read_assortment_overrides(path: Path, offer_id_column="Материал",
                               price_column="Розничная цена", stock_column="Остатки",
                               description_column="Описание", brand_column="Бренд") -> dict:
    """Читает регулярный файл-ассортимент (сейчас только у Мвидео,
    'Ассортимент_МВМ_Сравнение_*.xlsx') и возвращает {offer_id: {"price":..,
    "quantity":.., "description":.. (опционально), "brand":.. (опционально)}}.

    price/quantity — ОБЯЗАТЕЛЬНЫЕ колонки, и семантика для них — ПЕРЕКРЫТИЕ:
    если значение непустое, оно всегда перекрывает то, что вернул apigw для
    цены/остатка, вне зависимости от того, было ли там что-то своё.

    09.2026 (по просьбе пользователя): description_column/brand_column —
    НЕОБЯЗАТЕЛЬНЫЕ колонки ("Описание"/"Бренд" — рабочее предположение о
    названиях, см. предупреждение в psb_generate.py). Если такой колонки в
    файле нет вообще — тихо пропускается, ошибки не будет (в отличие от
    offer_id/price/stock, которые обязательны). Семантика для них —
    ЗАПАСНОЙ ВАРИАНТ, а не перекрытие: сама подстановка (заполнять только
    если у apigw пусто) делается в psb_generate.py, а не здесь — эта
    функция просто кладёт в overrides то, что нашла в файле, если нашла.

    09.2026 (правка после замечания пользователя): реальная колонка в
    файле оказалась "Описание *" (со звёздочкой — как в шаблоне Белки), а
    не голое "Описание", как предполагалось изначально. Точное сравнение
    строк её не находило. Заголовки теперь сравниваются ПОСЛЕ очистки
    (`clean_header` из core/xlsx.py — та же функция, что использует
    import_check/import_attributes для разбора заголовков шаблонов, снимает
    "*"/"(справочник)"), так что колонка находится независимо от того, есть
    ли у неё в файле ассортимента звёздочка или нет."""
    wb = load_workbook(path, data_only=True)
    ws = wb.active
    headers = {}
    for idx, cell in enumerate(ws[1], start=1):
        name, _, _ = clean_header(cell.value)
        if name:
            headers[name] = idx
    missing = {offer_id_column, price_column, stock_column} - headers.keys()
    if missing:
        raise ValueError(f"В файле ассортимента {path.name} не найдены колонки: {sorted(missing)}")
    has_description = description_column in headers
    has_brand = brand_column in headers

    overrides = {}
    for row in ws.iter_rows(min_row=2, values_only=False):
        raw_offer = row[headers[offer_id_column] - 1].value
        if raw_offer in (None, ""):
            continue
        offer_id = str(int(raw_offer)) if isinstance(raw_offer, float) else str(raw_offer)
        price = row[headers[price_column] - 1].value
        stock = row[headers[stock_column] - 1].value
        entry = {"price": price, "quantity": stock}
        if has_description:
            desc = row[headers[description_column] - 1].value
            if desc not in (None, ""):
                entry["description"] = desc
        if has_brand:
            brand = row[headers[brand_column] - 1].value
            if brand not in (None, ""):
                entry["brand"] = brand
        overrides[offer_id] = entry
    return overrides


def load_cert_statuses_from_assortment(path: Path, offer_id_column="Материал",
                                       cert_column=CERT_COLUMN_DEFAULT,
                                       conflicts: list | None = None) -> dict:
    """{offer_id: 'red'|'green'|'white'} из файла-ассортимента — второй
    источник статусов сертификата в проекте (первый — цветной отчёт Bazar,
    core/cert_report.py::load_cert_statuses). Трактовка цвета общая.

    В отличие от read_assortment_overrides здесь колонка сертификата
    ОБЯЗАТЕЛЬНА: функцию зовут только тогда, когда статусы реально нужны
    (фильтр --green/--white/--red или stage 'certificates'), и молча
    вернуть пустоту в этом случае хуже, чем сказать про колонку."""
    wb = load_workbook(path, data_only=True)
    ws = wb.active
    headers = {}
    for idx, cell in enumerate(ws[1], start=1):
        name, _, _ = clean_header(cell.value)
        if name:
            headers[name] = idx
    missing = {offer_id_column, cert_column} - headers.keys()
    if missing:
        raise ValueError(f"В файле {path.name} не найдены колонки: {sorted(missing)}")

    statuses: dict[str, str] = {}
    for row in ws.iter_rows(min_row=2, values_only=False):
        raw_offer = row[headers[offer_id_column] - 1].value
        if raw_offer in (None, ""):
            continue
        offer_id = normalize_article(raw_offer)
        # Один offer_id встречается в файле много раз (999 строк на ~70
        # товаров). При расхождении между строками побеждает «худший»
        # статус — та же логика, что между листами отчёта Bazar, и по той
        # же причине: лучше ошибиться в сторону «сертификата нет», чем
        # взять последнюю попавшуюся строку.
        statuses[offer_id] = merge_status(statuses.get(offer_id),
                                          classify_fill(row[headers[cert_column] - 1]),
                                          key=offer_id, conflicts=conflicts)
    return statuses