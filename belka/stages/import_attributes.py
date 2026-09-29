#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 'import_attributes' — собирает по всем xlsx-шаблонам значения
колонок-справочников (Бренд/Материал/Цвет и т.п.), которых ещё нет в
справочнике атрибута на сайте, отсеивает "подозрительные" (см. эвристики
в import_shared.py) и добавляет чистые через mass-add-directory.
Единственный из бывших "фаз" import'а, который реально пишет на сайт
(справочники атрибутов) — import_check только читает.

Раньше — "ФАЗА 2" внутри единого belka_import.py / ImportStage.
"""
from __future__ import annotations

import time
from pathlib import Path
from collections import Counter, defaultdict

from ..core.text import casefold_ru
from ..core.jsonio import load_json_file, save_json_file
from ..core.http import SessionBlockedError
from ..pipeline import Stage, StageResult
from .import_shared import (
    scan_file, normalize_capitalize, is_placeholder, looks_like_attr_name,
    find_near_duplicates, log_key, merge_into_log, RARE_MIN_MAX_COUNT,
)

CHUNK_SIZE = 40   # размер пачки для mass-add-directory
PROPERTIES_URL_PART = "/api/v1/catalog/properties"


class ImportAttributesStage(Stage):
    name = "import_attributes"

    def __init__(self, folder=None):
        self.folder_override = Path(folder) if folder else None

    # ------------------------------------------------------------ API calls --

    @staticmethod
    def _api_search_property(session, api_base, name):
        url = f"{api_base}{PROPERTIES_URL_PART}:search"
        payload = {
            "include": [], "sort": ["-id"],
            "filter": {"name": name, "type": []},
            "pagination": {"type": "offset", "offset": 0, "limit": 10},
        }
        resp = session.post(url, json=payload, timeout=30)
        data = resp.json().get("data", [])
        for item in data:
            if item.get("name") == name:
                return item
        return data[0] if data else None

    @staticmethod
    def _api_get_directory(session, api_base, property_id):
        url = f"{api_base}{PROPERTIES_URL_PART}/{property_id}"
        resp = session.get(url, params={"include": "directory,categories"}, timeout=30)
        return resp.json().get("data", {}).get("directory", []) or []

    @staticmethod
    def _api_add_directory_values(session, api_base, property_id, values):
        added = []
        for i in range(0, len(values), CHUNK_SIZE):
            chunk = values[i:i + CHUNK_SIZE]
            items = [{"name": v, "value": v} for v in chunk]
            url = f"{api_base}{PROPERTIES_URL_PART}/{property_id}:mass-add-directory"
            resp = session.post(url, json={"items": items}, timeout=60)
            added.extend(resp.json().get("data", []))
        return added

    # -------------------------------------------------------------------- run --

    def run(self, ctx) -> StageResult:
        cfg = ctx.config
        session = ctx.admin_session()

        folder = self.folder_override or ctx.resolved_templates_dir()
        if not folder.is_dir():
            raise RuntimeError(f"Папка не найдена: {folder}")
        files = sorted(folder.glob("*.xlsx"))
        if not files:
            raise RuntimeError(f"В папке {folder} нет .xlsx файлов.")

        print(f"[import_attributes] файлов к сканированию: {len(files)}")
        t0 = time.monotonic()
        global_counter = defaultdict(Counter)
        for i, path in enumerate(files, 1):
            state = scan_file(path)
            values_found = sum(len(c) for c in state["value_counter"].values())
            print(f"  [{i}/{len(files)}] {path.name}: уникальных значений-справочников={values_found}")
            for attr_name, counter in state["value_counter"].items():
                global_counter[attr_name].update(counter)
        print(f"[import_attributes] сканирование заняло {time.monotonic() - t0:.1f}с, "
              f"атрибутов-справочников встречено: {len(global_counter)}")

        blocked = False
        attr_property_id = {}
        attr_existing_values = {}
        attrs_not_found = []
        clean_to_send = defaultdict(list)
        suspicious = defaultdict(list)
        added_report = defaultdict(list)
        total_suspicious = 0
        suspicious_sent = False

        try:
            print("\n=== Поиск атрибутов в API ===")
            attr_names = list(global_counter)
            for idx, attr_name in enumerate(attr_names, 1):
                prop = self._api_search_property(session, cfg.admin_api_base, attr_name)
                if not prop:
                    print(f"  [{idx}/{len(attr_names)}] ⚠ атрибут не найден в API: '{attr_name}' — пропускаю")
                    attrs_not_found.append(attr_name)
                    continue
                prop_id = prop["id"]
                attr_property_id[attr_name] = prop_id
                directory = self._api_get_directory(session, cfg.admin_api_base, prop_id)
                attr_existing_values[attr_name] = [d["value"] for d in directory]
                print(f"  [{idx}/{len(attr_names)}] ✔ '{attr_name}' -> id={prop_id}, уже в справочнике: {len(directory)}")

            for attr_name, counter in global_counter.items():
                if attr_name not in attr_property_id:
                    continue
                existing_cf = {casefold_ru(v) for v in attr_existing_values.get(attr_name, [])}
                max_count = max(counter.values()) if counter else 0
                seen_new_norm = []
                for raw_value, count in counter.items():
                    norm = normalize_capitalize(raw_value)
                    if casefold_ru(norm) in existing_cf:
                        continue
                    reasons = []
                    if is_placeholder(norm):
                        reasons.append("похоже на заглушку/пустышку")
                    if looks_like_attr_name(norm, attr_name):
                        reasons.append(f"похоже на обрывок названия атрибута '{attr_name}'")
                    if count == 1 and max_count >= RARE_MIN_MAX_COUNT:
                        reasons.append(f"редкое значение (встречается 1 раз, максимум в колонке — {max_count})")
                    if len(norm) < 2 or len(norm) > 60:
                        reasons.append(f"необычная длина ({len(norm)} симв.)")
                    dup = find_near_duplicates(norm, attr_existing_values.get(attr_name, []) + seen_new_norm)
                    if dup:
                        reasons.append(f"похоже на уже существующее/новое значение '{dup}' (возможна опечатка)")
                    seen_new_norm.append(norm)
                    if reasons:
                        suspicious[attr_name].append((norm, reasons))
                    else:
                        clean_to_send[attr_name].append(norm)

            print("\n=== Отправка чистых новых значений ===")
            clean_attrs = [a for a, v in clean_to_send.items() if v]
            if not clean_attrs:
                print("  Нечего отправлять — новых однозначных значений не найдено.")
            for idx, attr_name in enumerate(clean_attrs, 1):
                values = clean_to_send[attr_name]
                added = self._api_add_directory_values(session, cfg.admin_api_base, attr_property_id[attr_name], values)
                added_report[attr_name].extend(added)
                print(f"  [{idx}/{len(clean_attrs)}] ✔ '{attr_name}': добавлено {len(added)} значений")

            total_suspicious = sum(len(v) for v in suspicious.values())
            if total_suspicious:
                print(f"\n=== Подозрительные значения ({total_suspicious}) ===")
                for attr_name, items in suspicious.items():
                    for value, reasons in items:
                        print(f"  ? [{attr_name}] '{value}' — {'; '.join(reasons)}")
                if ctx.confirm("\nОтправить все эти значения тоже?"):
                    suspicious_sent = True
                    for attr_name, items in suspicious.items():
                        values = [v for v, _ in items]
                        added = self._api_add_directory_values(session, cfg.admin_api_base, attr_property_id[attr_name], values)
                        added_report[attr_name].extend(added)
                        print(f"  ✔ '{attr_name}': добавлено {len(added)} подозрительных значений")
                else:
                    print("  Подозрительные значения НЕ отправлены — добавьте их вручную при необходимости.")
        except SessionBlockedError as e:
            blocked = True
            print(f"\n⛔ ОСТАНОВКА: {e}")
            print("Всё, что успело отправиться до этого момента, уже сохранено (см. added_values.json ниже).")

        log_path = folder / cfg.added_values_log_file
        added_log = load_json_file(log_path, {})
        for attr_name, items in added_report.items():
            if attr_name not in attr_property_id:
                continue
            key = log_key(attr_property_id[attr_name], attr_name)
            values = [item.get("value") for item in items if item.get("value")]
            merge_into_log(added_log, key, values)
        save_json_file(log_path, added_log)
        print(f"\n[i] JSON с добавленными значениями (накопительно): {log_path}")

        summary = {
            "files": len(files),
            "attrs_seen": len(global_counter),
            "attrs_not_found": len(attrs_not_found),
            "values_added": sum(len(v) for v in added_report.values()),
            "suspicious_not_sent": 0 if suspicious_sent else total_suspicious,
            "log": str(log_path),
            "blocked": blocked,
        }
        print("\n=== ИТОГ import_attributes ===")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        return StageResult(self.name, ok=not blocked, blocked=blocked, summary=summary)
