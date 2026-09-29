#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 'zero_stock' — обнуляет остатки (qty) всех товаров склада, у которых
qty >= 1.

    POST /api/v1/catalog/stocks:search   — постранично собираем id, у кого qty >= 1
    PATCH /api/v1/catalog/stocks/{id}    — {"qty": 0}

Идемпотентно без отдельного лога: фильтр qty_gte=1 сам исключает уже
обнулённые записи, так что stage можно спокойно прерывать и перезапускать.
"""
from __future__ import annotations

from ..pipeline import Stage, StageResult
from ..core.http import SessionBlockedError

STOCKS_SEARCH_URL_PART = "/api/v1/catalog/stocks:search"
STOCK_URL_PART = "/api/v1/catalog/stocks/{id}"
PAGE_LIMIT = 100


class ZeroStockStage(Stage):
    name = "zero_stock"

    @staticmethod
    def _api_fetch_nonzero_stocks(session, api_base, store_id, page_limit=PAGE_LIMIT):
        url = f"{api_base}{STOCKS_SEARCH_URL_PART}"
        items = []
        offset = 0
        while True:
            body = {
                "sort": ["id"],
                "filter": {"store_id": [store_id], "qty_gte": 1},
                "pagination": {"type": "offset", "offset": offset, "limit": page_limit},
            }
            resp = session.post(url, json=body, timeout=30)
            payload = resp.json()
            data = payload.get("data", [])
            items.extend(data)
            total = payload.get("meta", {}).get("pagination", {}).get("total", len(items))
            print(f"  Загружено {len(items)}/{total}")
            if not data or len(data) < page_limit or len(items) >= total:
                break
            offset += page_limit
        return items

    @staticmethod
    def _api_zero_stock(session, api_base, stock_id):
        url = f"{api_base}{STOCK_URL_PART.format(id=stock_id)}"
        resp = session.patch(url, json={"qty": 0}, timeout=30)
        return resp.json()

    def run(self, ctx) -> StageResult:
        cfg = ctx.config
        session = ctx.admin_session()

        print(f"Ищу остатки склада {cfg.store_id} с qty >= 1...")
        items = self._api_fetch_nonzero_stocks(session, cfg.admin_api_base, cfg.store_id)
        print(f"\nНайдено к обнулению: {len(items)}")
        if not items:
            print("Обнулять нечего.")
            return StageResult(self.name, ok=True, summary={"zeroed": 0})

        print("Примеры:")
        for item in items[:5]:
            print(f"  id={item['id']} offer_id={item['offer_id']} qty={item['qty']}")

        if not ctx.confirm(f"\nОбнулить qty у ВСЕХ {len(items)} записей склада {cfg.store_id}?"):
            print("Отменено.")
            return StageResult(self.name, ok=True, summary={"zeroed": 0, "cancelled": True})

        blocked = False
        zeroed = 0
        try:
            for item in items:
                self._api_zero_stock(session, cfg.admin_api_base, item["id"])
                zeroed += 1
                print(f"  ✔ id={item['id']} offer_id={item['offer_id']}: {item['qty']} -> 0  ({zeroed}/{len(items)})")
        except SessionBlockedError as e:
            blocked = True
            print(f"\n⛔ ОСТАНОВКА: {e}")
            print("Можно просто перезапустить — уже обнулённые запросом qty_gte=1 больше не попадутся.")

        summary = {"zeroed": zeroed, "found": len(items), "blocked": blocked}
        print("\n=== ИТОГ zero_stock ===")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        return StageResult(self.name, ok=not blocked, summary=summary)
