#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
belka.core.cert_log — чтение/запись журнала деактиваций по сертификату
(`deactivated_by_certificate*.json`), общего по формату для обоих
certificates-stage'ов (Bazar: belka/stages/certificates.py; Мвидео:
belka/stages/certificates_assortment.py).

Зачем отдельный модуль, а не просто load_json_file/save_json_file
(09.2026, по итогам ревью): раньше путь к журналу был ОДИН на все склады
(WarehouseConfig.cert_log_file = "deactivated_by_certificate.json", ни в
одном рецепте не переопределён), а ключ журнала — vendor_code, у складов
из РАЗНЫХ пространств нумерации (у Базара это id из JSON-экспорта, 4-6
знаков; у Мвидео — offer_id, 8-9 знаков). Оба stage'а считают

    to_activate = set(журнал) - {кто сейчас "плохой" по файлу}

то есть запуск certificates для склада B видел в журнале чужие записи
склада A как "сертификат появился" и отправлял по ним
PATCH allow_publish=true — молча возвращая на витрину товары другого
склада, скрытые из-за проблемного сертификата.

Теперь защита двойная:
  1) у каждого склада свой файл журнала (см.
     PipelineContext.resolved_cert_log_file);
  2) в самом файле лежит служебная запись META_KEY с ключом склада —
     и если журнал открыт не тем складом, который его писал, stage
     падает с понятной ошибкой ВМЕСТО того, чтобы менять публикацию
     чужих товаров. Это ловит и ручную путаницу с --file/путями, и
     случай, когда кто-то скопировал журнал между складами.

Старые журналы (без META_KEY) читаются как есть и получают маркер при
первой же записи — миграция не нужна.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .jsonio import load_json_file, save_json_file

# Служебный ключ внутри журнала. Начинается и заканчивается на "__" —
# vendor_code такой формы быть не может (это всегда число), поэтому
# коллизия с настоящей записью исключена.
META_KEY = "__meta__"


class CertLogOwnerMismatch(RuntimeError):
    """Журнал принадлежит другому складу — трогать его нельзя."""


def load_cert_log(path: Path, warehouse: str) -> dict:
    """Записи журнала (без служебной META_KEY). Если журнал помечен другим
    складом — CertLogOwnerMismatch, потому что дальше по коду разница
    множеств превратилась бы в массовую активацию чужих товаров."""
    raw = load_json_file(Path(path), {})
    if not isinstance(raw, dict):
        raise RuntimeError(f"Журнал сертификатов {path} повреждён: ожидался объект JSON, получено {type(raw).__name__}")

    meta = raw.get(META_KEY) or {}
    owner = meta.get("warehouse") if isinstance(meta, dict) else None
    if owner and owner != warehouse:
        raise CertLogOwnerMismatch(
            f"Журнал сертификатов {path} принадлежит складу '{owner}', а запущен склад '{warehouse}'. "
            f"Продолжать нельзя: записи чужого склада ушли бы на активацию (их vendor_code'ов нет в "
            f"файле текущего склада). Проверьте cert_log_file в рецепте склада "
            f"(belka/warehouses/{warehouse}.py) — у каждого склада должен быть СВОЙ журнал."
        )

    return {k: v for k, v in raw.items() if k != META_KEY}


def save_cert_log(path: Path, warehouse: str, entries: dict) -> None:
    """Пишет журнал, проставляя маркер владельца. Порядок ключей — META_KEY
    первым, чтобы владелец был виден сразу при открытии файла глазами."""
    payload = {META_KEY: {"warehouse": warehouse, "updated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}}
    payload.update(entries)
    save_json_file(Path(path), payload)
