#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Реестр складов «Белки».

KNOWN_WAREHOUSES — из чего вообще собран маркетплейс (один продавец,
seller_id=6, несколько складов). REGISTRY — только те, под которые реально
собран пайплайн (сейчас — только Базар).

Добавить новый склад: создать belka/warehouses/<ключ>.py по образцу
bazar.py (WarehouseConfig с параметрами склада + build_pipelines(),
собранный из нужных ему stage'ов belka.stages.*) и зарегистрировать здесь.
"""
from __future__ import annotations

from . import bazar
from . import mvideo
from . import b2c

REGISTRY = {
    "bazar": bazar,
    "mvideo": mvideo,
    "b2c": b2c,
}

# Известные склады «Белки» — для справки и для внятной ошибки, если
# попросят прогнать то, под что пайплайн ещё не собран.
KNOWN_WAREHOUSES = {
    "bazar": "№5 — Базар (реализован)",
    "shkm": "№4 — ШКМ, «Шаг к моде» (обувь) — пайплайн ещё не собран",
    "mvideo": "№6 — Мвидео (собран, НЕ проверен на живых данных — см. belka/warehouses/mvideo.py)",
    "b2c": "№7 — B2C-платформа (собран, НЕ проверен на живых данных — см. belka/warehouses/b2c.py)",
}


def get_warehouse(key: str):
    module = REGISTRY.get(key)
    if module is not None:
        return module
    if key in KNOWN_WAREHOUSES:
        raise SystemExit(
            f"Склад '{key}' известен ({KNOWN_WAREHOUSES[key]}), но пайплайн под него ещё "
            f"не собран. Возьмите belka/warehouses/bazar.py за образец: WarehouseConfig "
            f"с параметрами склада + build_pipelines() из нужных stage'ов belka.stages.*."
        )
    raise SystemExit(f"Неизвестный склад '{key}'. Известные: {list(KNOWN_WAREHOUSES)}")


def list_warehouses() -> None:
    print("Склады «Белки»:")
    for key, note in KNOWN_WAREHOUSES.items():
        mark = "✔" if key in REGISTRY else "—"
        print(f"  [{mark}] {key}: {note}")
